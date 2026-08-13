"""Orchestrate D-Bus, Bluetooth profiles, state, and desktop sinks.

The control API is published before Bluetooth initialization. Profile failures
leave it available in degraded mode and are retried periodically.
"""

from __future__ import annotations

import logging
import signal

import dbus
from gi.repository import GLib

from blueferry import __version__, bluez_setup, config
from blueferry.ancs.client import AncsClient
from blueferry.backend_lifecycle import installed_release
from blueferry.backend_operations import BackendDependencies
from blueferry.bearer_supervisor import BearerSupervisor
from blueferry.bus import get_system_bus, main_loop
from blueferry.connectivity import Connectivity
from blueferry.contacts import ContactsResolver, clear_contact_cache, pull_phonebook
from blueferry.dbus_service import MessagesService, claim_bus_name
from blueferry.event_dispatcher import EventDispatcher
from blueferry.history import (
    clear_events,
    history_count,
    minimize_ancs_history,
    prune_events,
    read_events,
    scrub_unprotected_events,
)
from blueferry.notification_policy import (
    ALL_NOTIFICATIONS,
    NotificationPolicyStore,
)
from blueferry.obex.map_events import MapEventListener
from blueferry.obex.sessions import SessionManager
from blueferry.obex.worker import ObexWorker
from blueferry.pair_setup import bond_status
from blueferry.profile_supervisor import ProfileSupervisor
from blueferry.protocol import BUS_NAME
from blueferry.setup_verification import (
    CONTACTS,
    MESSAGE_NOTIFICATIONS,
    NOTIFICATION_ACCESS,
    SetupVerification,
)
from blueferry.storage_security import NO_STORAGE, StorageSecurity

log = logging.getLogger(__name__)

# How often to re-pull the iPhone's phonebook (so the cache picks up new contacts)
CONTACTS_REFRESH_SEC = 24 * 60 * 60  # 24h

# Notice package replacement promptly without relying on pacman to reach into
# every logged-in user's systemd instance.
PACKAGE_RELEASE_CHECK_SEC = 10
TARGET_CONFIG_CHECK_SEC = 2
RESTART_AFTER_UPGRADE_EXIT = 75


class PairingRequiredError(RuntimeError):
    """Saved configuration exists, but BlueZ has no corresponding bond."""


class Daemon:
    def __init__(self) -> None:
        self.sessions = SessionManager()
        self.obex_worker = ObexWorker()
        # Claim the application bus name before touching the keyring. That
        # prevents two simultaneous first launches from creating different
        # keys for the same database.
        self.storage = StorageSecurity(initialize=False)
        self.contacts = ContactsResolver(storage=self.storage)
        self.connectivity = Connectivity()
        self.notification_policy = NotificationPolicyStore()
        self.setup_verification = SetupVerification(config.IPHONE_MAC)
        self.events = EventDispatcher(
            self.contacts,
            submit_obex=self.obex_worker.submit,
            notification_policy=lambda: self.notification_policy.value,
            storage=self.storage,
            on_incoming_message=lambda: self._verify_setup_task(MESSAGE_NOTIFICATIONS),
        )
        self.listener: MapEventListener | None = None
        self.ancs: AncsClient | None = None
        device_path = (
            f"/org/bluez/{config.ADAPTER}/"
            f"dev_{config.IPHONE_MAC.replace(':', '_')}"
        )
        # Keep LE out of the initial post-pair window.  The first OBEX
        # attempt below either establishes ordinary Classic MAP/PBAP or gives
        # iOS a chance to reject it before ANCS is allowed to connect.
        self.bearers = BearerSupervisor(
            device_path,
            le_enabled=False,
            on_status=self._emit_status,
        )
        self._contacts_refresh_id: int | None = None
        self._bus_name = None
        self._dbus_service: MessagesService | None = None
        self._sleep_match = None
        packaged_release = installed_release()
        self._packaged = packaged_release is not None
        self._running_release = packaged_release or __version__
        self._release_check_id: int | None = None
        self._target_config_check_id: int | None = None
        self._restart_after_upgrade = False
        self._release_missing_checks = 0
        self._startup_id: int | None = None
        self._initialization_retry_id: int | None = None
        self._initializing = True
        self._contacts_refresh_pending = False
        self.profiles = ProfileSupervisor(
            self.sessions,
            self.obex_worker,
            self.connectivity,
            on_ready=self._post_sessions_setup,
            on_lost=self._profiles_lost,
            on_status=self._emit_status,
            on_first_attempt_complete=self.bearers.enable_le,
        )

    def _emit_status(self) -> None:
        emit = getattr(self._dbus_service, "emit_status", None)
        if emit is not None:
            emit()

    def _mark_setup_task(self, task: str) -> bool:
        try:
            return self.setup_verification.mark(task)
        except (OSError, ValueError):
            log.warning("could not persist iPhone setup verification", exc_info=True)
            return False

    def _verify_setup_task(self, task: str) -> None:
        if self._mark_setup_task(task):
            self._emit_status()

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        log.info("=== BlueFerry starting ===")
        config.ensure_dirs()

        # Type=dbus means owning the name is our readiness boundary. Publish
        # the control surface before any Bluetooth operation that can wait on
        # hardware or the phone.
        self._bus_name = claim_bus_name()
        self._initialize_storage()
        self._dbus_service = MessagesService(
            self._bus_name,
            self.sessions,
            BackendDependencies(
                on_sent=self.events.sent,
                on_group_sent=self.events.group_sent,
                submit_obex=self.obex_worker.submit,
                pull_contacts=self._pull_contacts,
                on_contacts_pulled=self._contacts_pulled,
                contacts=self.contacts,
                status_provider=self._status,
                notification_policy=self.notification_policy,
                on_notification_policy_changed=self._emit_status,
                storage=self.storage,
                on_storage_changed=self._emit_status,
            ),
        )
        self.events.set_dbus_service(self._dbus_service)
        log.info("DBus service ready: %s", BUS_NAME)
        self._emit_status()

        if self._packaged:
            self._release_check_id = GLib.timeout_add_seconds(
                PACKAGE_RELEASE_CHECK_SEC, self._check_package_release
            )
        self._target_config_check_id = GLib.timeout_add_seconds(
            TARGET_CONFIG_CHECK_SEC, self._check_target_config
        )

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._signal)

        # Let the GLib loop begin dispatching D-Bus before potentially slow
        # profile setup. This makes activation and GetStatus deterministic.
        self._startup_id = GLib.timeout_add(250, self._initialize)

    def _initialize_storage(self) -> None:
        status = self.storage.refresh(allow_prompt=False)
        if status.policy == NO_STORAGE:
            clear_events()
            clear_contact_cache()
            return
        if not status.can_read:
            log.info("private storage unavailable: %s", status.detail)
            return
        scrubbed = scrub_unprotected_events(storage=self.storage)
        if scrubbed:
            log.info("scrubbed %d unprotected history events", scrubbed)
        prune_events(storage=self.storage)
        discarded, minimized = minimize_ancs_history(storage=self.storage)
        if discarded or minimized:
            log.info(
                "minimized ANCS history (discarded=%d, compacted=%d)",
                discarded,
                minimized,
            )
        self.contacts.refresh()
        if self.contacts.count() > 0:
            self._mark_setup_task(CONTACTS)
        if read_events(kinds={"sms_received"}, limit=1, storage=self.storage):
            self._mark_setup_task(MESSAGE_NOTIFICATIONS)
        self.events.seed_historical_ancs(
            read_events(
                kinds={"ancs_notification"},
                limit=2_000,
                storage=self.storage,
            )
        )

    def _initialize(self) -> bool:
        self._startup_id = None
        self._initialization_retry_id = None
        try:
            self._initialize_bluetooth()
        except PairingRequiredError as error:
            log.warning("Bluetooth setup is incomplete: %s", error)
            delay = self.connectivity.failed(error)
            self._initialization_retry_id = GLib.timeout_add_seconds(delay, self._initialize)
            log.info("waiting %ds for pairing before checking again", delay)
        except Exception as error:
            log.exception("Bluetooth initialization failed; running degraded")
            delay = self.connectivity.failed(error)
            self._initialization_retry_id = GLib.timeout_add_seconds(delay, self._initialize)
            log.warning("retrying Bluetooth initialization in %ds", delay)
        finally:
            self._initializing = False
            self._emit_status()
        return False

    def _initialize_bluetooth(self) -> None:
        if bond_status(config.IPHONE_MAC, config.ADAPTER) is not True:
            raise PairingRequiredError(
                "the saved iPhone is not currently paired; open a client to pair it"
            )

        if not bluez_setup.prepare():
            log.warning(
                "bluez_setup.prepare reported issues — continuing anyway, "
                "but MAP/PBAP may be refused. Re-pair on iPhone after the "
                "adapter is in A/V Hands-Free CoD if the toggles aren't there."
            )

        # A bond records trust but does not guarantee a live connection.
        # The bearer supervisor establishes BR/EDR but deliberately holds LE
        # until ProfileSupervisor completes its first MAP/PBAP attempt.
        self.bearers.start()

        # ANCS — per-app notifications via BLE GATT. Independent of MAP/PBAP.
        # The bearer supervisor connects LE alongside BR/EDR; the client waits
        # for the three ANCS characteristics and subscribes when they appear.
        device_path = f"/org/bluez/{config.ADAPTER}/dev_{config.IPHONE_MAC.replace(':', '_')}"
        if self.ancs is None:
            candidate = AncsClient(
                device_path,
                on_event=self.events.ancs,
                on_status=self._emit_status,
                include_non_message_notifications=lambda: (
                    self.notification_policy.value == ALL_NOTIFICATIONS
                ),
            )
            try:
                candidate.start()
            except Exception:
                candidate.stop()
                raise
            self.ancs = candidate
        self._watch_sleep_resume()

        # Sinks don't need the OBEX sessions — set them up now so ANCS events
        # still reach the desktop while MAP/PBAP are degraded.
        self.events.setup()

        # Signal subscriptions belong to the GLib thread; the blocking session
        # creation itself belongs to the serialized OBEX worker.
        self.profiles.start()

        if not self.profiles.ready:
            log.warning("=== BlueFerry running in DEGRADED mode ===")
            log.warning("    No MAP/PBAP session yet; retry is automatic.")
        # The "ready" line in the happy path is emitted by
        # _post_sessions_setup, so we don't duplicate it here.

    def _profiles_lost(self, _reason: str) -> None:
        """Stop consumers that hold objects belonging to old sessions."""
        if self.listener is not None:
            self.listener.stop()
            self.listener = None

    def _watch_sleep_resume(self) -> None:
        """Refresh profile sessions after suspend without waiting for a send."""
        if self._sleep_match is not None:
            return
        try:
            manager = dbus.Interface(
                get_system_bus().get_object("org.freedesktop.login1", "/org/freedesktop/login1"),
                "org.freedesktop.login1.Manager",
            )
            self._sleep_match = manager.connect_to_signal(
                "PrepareForSleep", self._on_prepare_for_sleep
            )
        except dbus.exceptions.DBusException:
            log.debug("logind sleep monitoring unavailable", exc_info=True)

    def _on_prepare_for_sleep(self, sleeping) -> None:
        if bool(sleeping):
            return
        log.info("system resumed — refreshing Bluetooth profile sessions")
        self.bearers.poke()
        self.profiles.reconnect("system resumed")

    def _post_sessions_setup(self) -> None:
        """Everything that requires live MAP+PBAP sessions. Idempotent so
        we can call it either at first-attempt success or at retry success."""
        # Warm contacts; if empty, do a one-time pull. PBAP pull is cheap.
        if self.contacts.count() == 0:
            log.info("contacts cache empty — pulling from iPhone via PBAP")
            self._refresh_contacts()

        # Schedule periodic contacts refresh
        if self._contacts_refresh_id is None:
            self._contacts_refresh_id = GLib.timeout_add_seconds(
                CONTACTS_REFRESH_SEC, self._periodic_refresh_contacts
            )

        # Wire up MAP MNS listener.
        # Resolve through the current cache; contacts refreshes in place.
        if self.listener is None:
            self.listener = MapEventListener(
                sessions=self.sessions,
                on_sms=self.events.message,
                resolve_contact=lambda raw: self.contacts.resolve(raw),
                submit_obex=self.obex_worker.submit,
            )
            self.listener.start()

        log.info(
            "=== BlueFerry ready (contacts=%d, sinks=%s) ===",
            self.contacts.count(),
            self.events.names,
        )

    def _pull_contacts(self) -> int:
        """Worker-side PBAP operation."""
        return pull_phonebook(self.sessions, storage=self.storage)

    def _contacts_pulled(self, pulled: int) -> int:
        """GLib-side cache refresh after a successful PBAP pull."""
        count = self.contacts.refresh()
        if count > 0:
            self._mark_setup_task(CONTACTS)
        if self._dbus_service is not None:
            self._dbus_service.operations.invalidate_conversations()
            self._dbus_service.emit_history_changed()
        log.info("contacts refresh: pulled %d, cached %d", pulled, count)
        self._emit_status()
        return count

    def _status(self) -> dict:
        if self.contacts.count() > 0:
            self._mark_setup_task(CONTACTS)
        if self.ancs and self.ancs.connected:
            self._mark_setup_task(NOTIFICATION_ACCESS)
        ancs = self.ancs
        return {
            "backend_release": self._running_release,
            "initializing": self._initializing,
            "ancs": bool(ancs and ancs.connected),
            "ancs_subscribed": bool(ancs and ancs.subscribed),
            "ancs_authorized": bool(ancs and ancs.authorized),
            **self.bearers.snapshot(),
            "contacts": self.contacts.count(),
            "events": history_count(storage=self.storage),
            "verified_iphone_setup": list(self.setup_verification.verified),
            "history_retention_days": config.HISTORY_RETENTION_DAYS,
            "notification_timeout_ms": config.NOTIFICATION_TIMEOUT_MS,
            "notification_policy": self.notification_policy.value,
            "storage_policy": self.storage.status.policy,
            "storage_state": self.storage.status.state,
            "storage_detail": self.storage.status.detail,
            **self.connectivity.snapshot(),
        }

    def _refresh_contacts(self) -> None:
        """Best-effort wrapper used by startup and the periodic timer."""
        if self._contacts_refresh_pending or self.sessions.pbap is None:
            return
        self._contacts_refresh_pending = True

        def succeeded(pulled):
            self._contacts_refresh_pending = False
            self._contacts_pulled(pulled)

        def failed(error):
            self._contacts_refresh_pending = False
            log.error("contacts refresh failed; using previous cache: %s", error)
            self.sessions.report_error(error)

        self.obex_worker.submit(self._pull_contacts, on_success=succeeded, on_error=failed)

    def _periodic_refresh_contacts(self) -> bool:
        """GLib timeout callback. Return True to keep the timer running."""
        log.info("periodic contacts refresh tick")
        self._refresh_contacts()
        return True

    def _check_package_release(self) -> bool:
        current = installed_release()
        if current == self._running_release:
            self._release_missing_checks = 0
            return True
        if current is None:
            # Pacman may briefly replace the marker during an upgrade. Three
            # consecutive misses distinguish removal from that transient.
            self._release_missing_checks += 1
            if self._release_missing_checks < 3:
                return True
            log.info("package release marker remains absent; stopping daemon")
            main_loop.quit()
            return False
        self._release_missing_checks = 0
        log.info(
            "installed backend changed from %s to %s; restarting",
            self._running_release,
            current,
        )
        self._restart_after_upgrade = True
        main_loop.quit()
        return False

    def _check_target_config(self) -> bool:
        mac, adapter = config.current_target()
        if mac == config.IPHONE_MAC and adapter == config.ADAPTER:
            bonded = bond_status(mac, adapter)
            if bonded is not False:
                return True
            # The daemon may already have initialized its supervisors when a
            # user removes the bond through a desktop Bluetooth client. Stop
            # the current process so those supervisors cannot keep issuing
            # Connect/CreateSession calls for an explicitly forgotten phone.
            # ``None`` is deliberately ignored above because it represents an
            # unavailable adapter or transient BlueZ inspection failure.
            log.info("saved iPhone bond was removed; stopping daemon")
            main_loop.quit()
            return False
        if not mac:
            log.info("saved iPhone target was cleared; stopping daemon")
        else:
            log.info(
                "saved iPhone target changed; restarting daemon for %s on %s",
                mac,
                adapter,
            )
            self._restart_after_upgrade = True
        main_loop.quit()
        return False

    def stop(self) -> None:
        log.info("=== BlueFerry stopping ===")
        self.bearers.stop()
        self.profiles.stop()
        for tid_attr in (
            "_contacts_refresh_id",
            "_release_check_id",
            "_target_config_check_id",
            "_startup_id",
            "_initialization_retry_id",
        ):
            tid = getattr(self, tid_attr, None)
            if tid is not None:
                try:
                    GLib.source_remove(tid)
                except Exception:
                    log.debug("could not remove daemon timer", exc_info=True)
                setattr(self, tid_attr, None)
        if self.listener is not None:
            self.listener.stop()
        if self.ancs is not None:
            self.ancs.stop()
        if self._sleep_match is not None:
            try:
                self._sleep_match.remove()
            except Exception:
                log.debug("could not remove sleep monitor", exc_info=True)
            self._sleep_match = None
        if self._dbus_service is not None:
            self._dbus_service.close()
        self.obex_worker.shutdown(cleanup=self.sessions.close_all)
        self.storage.close()
        self.sessions.stop_monitoring()
        bluez_setup.unregister_advert()
        main_loop.quit()

    def run(self) -> int:
        # `start()` must be inside the try.  A partially-completed startup can
        # already own a hardware-offloaded BLE advertisement; previously an
        # exception later in startup skipped stop()/UnregisterAdvertisement
        # and repeated systemd restarts exhausted all 20 controller slots.
        try:
            self.start()
            main_loop.run()
        finally:
            self.stop()
        return RESTART_AFTER_UPGRADE_EXIT if self._restart_after_upgrade else 0

    # ---- internals -------------------------------------------------------

    def _signal(self, signum, _frame):
        log.info("received signal %d, stopping", signum)
        main_loop.quit()
