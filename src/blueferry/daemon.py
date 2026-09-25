"""Orchestrate D-Bus, Bluetooth profiles, state, and desktop sinks.

The control API is published before Bluetooth initialization. Profile failures
leave it available in degraded mode and are retried periodically.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Callable

import dbus
from gi.repository import GLib

from blueferry import __version__, bluez_setup, config
from blueferry.adapter_class_supervisor import AdapterClassSupervisor
from blueferry.ancs.client import AncsClient
from blueferry.backend_lifecycle import installed_release
from blueferry.backend_operations import BackendDependencies
from blueferry.bearer_supervisor import BearerSupervisor
from blueferry.bluetooth_capabilities import ancs_limited_vendor, controller_hardware
from blueferry.bluetooth_recovery import (
    BluetoothRecovery,
    BluezRecoveryAdapter,
    RecoveryObservation,
    probe_map,
)
from blueferry.build_info import build_id, installed_build_sha, running_build_sha
from blueferry.bus import get_system_bus, main_loop
from blueferry.confirmed_groups import ConfirmedGroupsStore
from blueferry.connectivity import Connectivity
from blueferry.contacts import ContactsResolver, pull_phonebook
from blueferry.dbus_service import MessagesService, claim_bus_name
from blueferry.event_dispatcher import EventDispatcher
from blueferry.history import (
    history_count,
    mark_event_handles_read,
)
from blueferry.limits import MAX_OBEX_PENDING_OPERATIONS
from blueferry.notification_policy import (
    ALL_NOTIFICATIONS,
    NotificationPolicyStore,
)
from blueferry.obex.map_events import MapEventListener
from blueferry.obex.mns_watch import MnsWatch
from blueferry.obex.sessions import SessionManager
from blueferry.obex.worker import ObexWorker
from blueferry.pair_setup import bond_status
from blueferry.profile_supervisor import ProfileSessions, ProfileSupervisor
from blueferry.protocol import BUS_NAME
from blueferry.read_receipts import ReadReceiptQueue
from blueferry.setup_verification import (
    CONTACTS,
    MESSAGE_NOTIFICATIONS,
    NOTIFICATION_ACCESS,
    SetupVerification,
)
from blueferry.solicitation_supervisor import SolicitationSupervisor
from blueferry.starred_threads import StarredThreadsStore
from blueferry.storage_preparation import PreparedStorage, prepare_storage
from blueferry.storage_security import StorageSecurity
from blueferry.wireplumber_policy import WirePlumberPhoneAudioPolicy

log = logging.getLogger(__name__)


def classic_reachable(bearers: BearerSupervisor, sessions: ProfileSessions) -> bool:
    """True when Classic is up or an OBEX session already proves it."""
    return (
        bearers.bredr_connected
        or sessions.map is not None
        or sessions.pbap is not None
    )


# How often to re-pull the iPhone's phonebook (so the cache picks up new contacts)
CONTACTS_REFRESH_SEC = 24 * 60 * 60  # 24h
# Give initial MAP retries the permission window before starting a bulk PBAP
# transfer, but keep contact sync available when only PBAP ever connects.
CONTACTS_MAP_GRACE_SECONDS = 180

# Notice package replacement promptly without relying on pacman to reach into
# every logged-in user's systemd instance.
PACKAGE_RELEASE_CHECK_SEC = 10
TARGET_CONFIG_CHECK_SEC = 2
STORAGE_RETRY_SEC = 5
RESTART_AFTER_UPGRADE_EXIT = 75


class PairingRequiredError(RuntimeError):
    """Saved configuration exists, but BlueZ has no corresponding bond."""


class Daemon:
    def __init__(self) -> None:
        self.sessions = SessionManager()
        self.obex_worker = ObexWorker()
        self.read_receipts = ReadReceiptQueue(
            self.sessions,
            submit=self.obex_worker.submit,
            schedule=GLib.timeout_add_seconds,
            cancel=GLib.source_remove,
        )
        # Claim the application bus name before touching the keyring. That
        # prevents two simultaneous first launches from creating different
        # keys for the same database.
        self.storage = StorageSecurity(initialize=False)
        self.storage.require_preparation()
        self.contacts = ContactsResolver(storage=self.storage)
        self.connectivity = Connectivity()
        self.notification_policy = NotificationPolicyStore()
        self.starred_threads = StarredThreadsStore(storage=self.storage)
        self.confirmed_groups = ConfirmedGroupsStore(storage=self.storage)
        self.setup_verification = SetupVerification(config.IPHONE_MAC)
        self.events = EventDispatcher(
            self.contacts,
            defer_mark_read=self.read_receipts.defer_path,
            notification_policy=lambda: self.notification_policy.value,
            contacts_only_notifications=(
                lambda: self.notification_policy.contacts_only
            ),
            storage=self.storage,
            on_incoming_message=lambda: self._verify_setup_task(MESSAGE_NOTIFICATIONS),
        )
        self.listener: MapEventListener | None = None
        self.mns_watch: MnsWatch | None = None
        # One MAP reconnect per MNS outage; seeing MNS again rearms it.
        self._mns_reconnect_spent = False
        self.ancs: AncsClient | None = None
        self.adapter_class = AdapterClassSupervisor(config.ADAPTER)
        self.solicitation = SolicitationSupervisor(config.ADAPTER)
        self.phone_audio = WirePlumberPhoneAudioPolicy()
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
            on_le_state=self._observe_le_state,
            on_le_dial=self.solicitation.set_dialing,
            inbound_le_primed=self.solicitation.active,
        )
        self._contacts_refresh_id: int | None = None
        self._bus_name = None
        self._dbus_service: MessagesService | None = None
        self._sleep_match = None
        self._power_match = None
        self._bluez_owner_match = None
        self._bluez_owner_generation = 0
        packaged_release = installed_release()
        self._packaged = packaged_release is not None
        self._running_release = packaged_release or __version__
        self._running_build_sha = (
            installed_build_sha() if self._packaged else running_build_sha()
        )
        self._running_build_id = build_id(
            self._running_release,
            self._running_build_sha,
        )
        self._release_check_id: int | None = None
        self._target_config_check_id: int | None = None
        self._storage_retry_id: int | None = None
        self._restart_after_upgrade = False
        self._release_missing_checks = 0
        self._startup_id: int | None = None
        self._initialization_retry_id: int | None = None
        self._initializing = True
        self._bluetooth_initialized = False
        self._contacts_refresh_pending = False
        self._contacts_initial_sync_done = False
        self._contacts_storage_generation = 0
        self._contacts_sync_waiters: list[
            tuple[Callable[[int], None], Callable[[Exception], None]]
        ] = []
        self._contacts_refresh_deferred = False
        self._contacts_map_wait_id: int | None = None
        self._contacts_map_wait_finished = False
        self.profiles = ProfileSupervisor(
            self.sessions,
            self.obex_worker,
            self.connectivity,
            on_ready=self._post_sessions_setup,
            on_lost=self._profiles_lost,
            on_status=self._emit_status,
            on_partial_ready=self._post_available_sessions_setup,
            on_attempt_pending=(
                self.bearers.hold_le if config.ANCS_ENABLED else None
            ),
            on_first_attempt_complete=(
                self.bearers.enable_le if config.ANCS_ENABLED else None
            ),
            attempt_ready=lambda: classic_reachable(self.bearers, self.sessions),
        )
        self.recovery = BluetoothRecovery(
            config.IPHONE_MAC,
            BluezRecoveryAdapter(config.ADAPTER, config.IPHONE_MAC),
            self.obex_worker,
            observe=self._recovery_observation,
            probe=lambda: probe_map(self.sessions.map_path),
            probe_health=lambda: self.ancs.probe_health() if self.ancs else None,
            reset_le=lambda: self.bearers.recover_le_transport(allow_disconnected=True),
            pause=self._pause_for_recovery,
            resume=self._resume_after_recovery,
        )

    def _recovery_observation(self) -> RecoveryObservation:
        ancs = self.ancs
        return RecoveryObservation(
            healthy=bool(ancs and ancs.connected and not ancs.permission_denied),
            health_proof=ancs.health_proof if ancs else None,
            eligible=bool(
                config.ANCS_ENABLED and ancs and not ancs.permission_denied
                and not self._initializing and self.profiles.ready
                and self.bearers.bredr_connected and self.bearers.le_state is not None
                and self.solicitation.active()
            ),
            busy=self.bearers.busy,
        )

    def _pause_for_recovery(self) -> None:
        if not self._bluetooth_initialized:
            return  # Startup restoration precedes all Bluetooth supervision.
        self.adapter_class.stop()
        self.bearers.stop()
        self.profiles.pause()
        self.solicitation.stop()
        self.sessions.close_all(remove_remote=False)
        if self.ancs is not None:
            self.ancs.observe_bearer_state(False)

    def _resume_after_recovery(self) -> None:
        if not self._bluetooth_initialized:
            # Startup restoration must finish before creating profile sessions
            # or advertisements on a radio whose power-off may still be pending.
            if self._initialization_retry_id is None:
                self._initialization_retry_id = GLib.idle_add(self._initialize)
            return
        self.adapter_class.start()
        self.solicitation.start()
        self.bearers.hold_le()
        self.bearers.reset_after_bluez_restart()
        self.profiles.resume()
        self.bearers.start()

    def _emit_status(self) -> None:
        emit = getattr(self._dbus_service, "emit_status", None)
        if emit is not None:
            emit()

    def _observe_le_state(self, connected: bool | None) -> None:
        if connected is not True:
            self.solicitation.set_needed(True)
        if self.ancs is not None:
            self.ancs.observe_bearer_state(connected)

    def _on_ancs_status(self) -> None:
        # StartNotify is not the success boundary.  Keep solicitation on air
        # until a Control Point/Data Source round trip proves ANCS usable.
        self._sync_solicitation()
        self._emit_status()

    def _sync_solicitation(self) -> None:
        end_to_end_ready = bool(
            config.ANCS_ENABLED
            and self.ancs is not None
            and self.ancs.connected
            and self.profiles.ready
        )
        self.solicitation.set_needed(not end_to_end_ready)

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
        self._dbus_service = MessagesService(
            self._bus_name,
            self.sessions,
            BackendDependencies(
                on_sent=self.events.sent,
                on_group_sent=self.events.group_sent,
                submit_obex=self.obex_worker.submit,
                defer_mark_read=self.read_receipts.defer,
                sync_contacts=self._sync_contacts,
                contacts=self.contacts,
                status_provider=self._status,
                notification_policy=self.notification_policy,
                on_notification_policy_changed=self._emit_status,
                starred_threads=self.starred_threads,
                confirmed_groups=self.confirmed_groups,
                storage=self.storage,
                prepare_storage=prepare_storage,
                on_storage_prepared=self._apply_storage_preparation,
                on_storage_changed=self._on_storage_changed,
            ),
        )
        self.events.set_dbus_service(self._dbus_service)
        self._initialize_storage()
        log.info("DBus service ready: %s", BUS_NAME)
        self._emit_status()

        if self._packaged:
            self._release_check_id = GLib.timeout_add_seconds(
                PACKAGE_RELEASE_CHECK_SEC, self._check_package_release
            )
        self._target_config_check_id = GLib.timeout_add_seconds(
            TARGET_CONFIG_CHECK_SEC, self._check_target_config
        )
        self._storage_retry_id = GLib.timeout_add_seconds(
            STORAGE_RETRY_SEC, self._retry_storage
        )

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._signal)

        # Let the GLib loop begin dispatching D-Bus before potentially slow
        # profile setup. This makes activation and GetStatus deterministic.
        self._startup_id = GLib.timeout_add(250, self._initialize)

    def _retry_storage(self) -> bool:
        # A daemon activated before the desktop keyring opens must recover
        # without requiring a client launch. Wallet I/O stays off the GLib loop.
        try:
            if self._dbus_service is not None:
                self._dbus_service.retry_storage_unlock()
        except Exception:
            log.warning("could not schedule background storage retry", exc_info=True)
        return True

    def _initialize_storage(self) -> None:
        if self._dbus_service is not None:
            self._dbus_service.retry_storage_unlock(initialize=True)

    def _apply_storage_preparation(self, prepared: PreparedStorage) -> None:
        self.contacts.adopt_cache(prepared.contacts)
        # Preparing storage can replace an archive cleared by a policy change.
        # An older in-flight pull must not satisfy this cache's initial sync.
        self._contacts_storage_generation += 1
        self._contacts_initial_sync_done = False
        self.events.seed_historical_ancs(prepared.historical_ancs)
        if prepared.has_messages:
            self._mark_setup_task(MESSAGE_NOTIFICATIONS)

    def _on_storage_changed(self) -> None:
        if self.storage.status.can_write:
            if self.contacts.count() > 0:
                self._mark_setup_task(CONTACTS)
            if self._contacts_refresh_needed():
                self._refresh_contacts()
        self._emit_status()

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
        if (config.ANCS_ENABLED or self.recovery.adapter.restore_pending
                or self.recovery.adapter.cleanup_pending):
            self.recovery.start()
        if self.recovery.active or self.recovery.adapter.restore_pending:
            return
        if bond_status(config.IPHONE_MAC, config.ADAPTER) is not True:
            raise PairingRequiredError(
                "the saved iPhone is not currently paired; open a client to pair it"
            )

        self.phone_audio.reconcile(enabled=config.KEEP_PHONE_AUDIO_ON_PHONE)

        # Class-of-Device is controller state, not durable configuration.
        # Repair it before opening either bearer and continue supervising it
        # for bluetoothd/controller resets during this daemon generation.
        self.adapter_class.start()
        if not bluez_setup.prepare_classic():
            log.warning(
                "adapter preparation reported issues — continuing anyway, "
                "but MAP/PBAP may be refused. Re-pair on iPhone after the "
                "adapter is in A/V Hands-Free CoD if the toggles aren't there."
            )
        self._watch_bluez_owner()
        self.solicitation.start()

        # A bond records trust but does not guarantee a live connection. The
        # bearer supervisor establishes BR/EDR but deliberately holds its own
        # outbound LE bootstrap until ProfileSupervisor completes its first
        # MAP/PBAP attempt. Phone-initiated LE remains usable for ANCS.
        self.bearers.start()

        # ANCS — per-app notifications via BLE GATT. Independent of MAP/PBAP.
        # The bearer supervisor connects LE alongside BR/EDR; the client waits
        # for the three ANCS characteristics and subscribes when they appear.
        device_path = f"/org/bluez/{config.ADAPTER}/dev_{config.IPHONE_MAC.replace(':', '_')}"
        if config.ANCS_ENABLED and self.ancs is None:
            candidate = AncsClient(
                device_path,
                on_event=self.events.ancs,
                on_status=self._on_ancs_status,
                on_transport_failure=self.bearers.recover_le_transport,
                include_non_message_notifications=lambda: (
                    self.notification_policy.value == ALL_NOTIFICATIONS
                ),
                include_app_notification=config.include_ancs_app,
                previously_authorized=(
                    NOTIFICATION_ACCESS in self.setup_verification.verified
                ),
            )
            # Publish before start(): its initial D-Bus sweep can dispatch
            # an owner change that must invalidate the in-progress scan.
            self.ancs = candidate
            try:
                candidate.observe_bearer_state(self.bearers.le_state)
                candidate.start()
            except Exception:
                self.ancs = None
                candidate.stop()
                raise
        elif not config.ANCS_ENABLED:
            log.info("ANCS connection disabled by pairing compatibility policy")
        self._watch_sleep_resume()

        # Sinks don't need the OBEX sessions — set them up now so ANCS events
        # still reach the desktop while MAP/PBAP are degraded.
        self.events.setup()

        # Signal subscriptions belong to the GLib thread; the blocking session
        # creation itself belongs to the serialized OBEX worker.
        self.profiles.start()
        self._bluetooth_initialized = True
        if not config.ANCS_ENABLED and not self.recovery.adapter.cleanup_pending:
            self.recovery.stop()

        if not self.profiles.ready:
            log.warning("=== BlueFerry running in DEGRADED mode ===")
            log.warning("    No MAP/PBAP session yet; retry is automatic.")
        # The "ready" line in the happy path is emitted by
        # _post_sessions_setup, so we don't duplicate it here.

    def _watch_bluez_owner(self) -> None:
        """Supervise bluetoothd even when compatibility mode disables ANCS."""
        if self._bluez_owner_match is None:
            self._bluez_owner_match = get_system_bus().add_signal_receiver(
                self._on_bluez_owner_changed,
                dbus_interface="org.freedesktop.DBus",
                signal_name="NameOwnerChanged",
                bus_name="org.freedesktop.DBus",
                arg0="org.bluez",
            )

    def _on_bluez_owner_changed(self, _name, old_owner, new_owner) -> None:
        if self._bluez_owner_match is None:
            return
        self._bluez_owner_generation += 1
        generation = self._bluez_owner_generation
        # Invalidate before discovery or advertising can dispatch more D-Bus
        # work. An interrupted power restoration keeps ordinary reconnects
        # paused until the recovery controller explicitly resumes them.
        self.recovery.invalidate()
        if old_owner:
            bluez_setup.forget_advert_registration()
        if new_owner and not self.recovery.active:
            # Reset before ANCS publishes status: that callback can already
            # register an advert with the replacement owner. Forgetting it
            # afterwards would discard a new registration as if it were old.
            self.solicitation.reset_after_bluez_restart()
        # Clear ANCS's old bearer observation before resetting the supervisor
        # that publishes its replacement. Otherwise the ANCS reset can erase
        # the new observation until the next physical link transition.
        if self.ancs is not None:
            self.ancs.observe_bluez_owner(old_owner, new_owner)
        if (
            new_owner
            and not self.recovery.active
            and self._bluez_owner_match is not None
            and generation == self._bluez_owner_generation
        ):
            self._on_bluez_restart()

    def _on_bluez_restart(self) -> None:
        """Reapply MAP-first ordering before accepting the new BlueZ owner."""
        self.adapter_class.poke()
        # Hold first because resetting bearer observations immediately probes
        # the replacement daemon. The old OBEX transport is already gone, so
        # discard its local sessions without asking obexd to remove them.
        self.bearers.hold_le()
        self.profiles.reconnect(
            "bluetoothd restarted",
            remove_remote_sessions=False,
        )
        self.bearers.reset_after_bluez_restart()

    def _profiles_lost(self, _reason: str) -> None:
        """Stop consumers that hold objects belonging to old sessions."""
        self.solicitation.set_needed(True)
        if self.mns_watch is not None:
            self.mns_watch.stop()
            self.mns_watch = None
        if self.listener is not None:
            self.listener.stop()
            self.listener = None

    def _watch_sleep_resume(self) -> None:
        """Refresh profile sessions after suspend without waiting for a send."""
        if self._power_match is None:
            self._power_match = get_system_bus().add_signal_receiver(
                self._on_adapter_power_changed,
                dbus_interface="org.freedesktop.DBus.Properties",
                signal_name="PropertiesChanged",
                bus_name="org.bluez",
                path=f"/org/bluez/{config.ADAPTER}",
                arg0="org.bluez.Adapter1",
            )
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

    def _on_adapter_power_changed(self, _interface, changed, invalidated) -> None:
        if not self.recovery.active and ("Powered" in changed or "Powered" in invalidated):
            self.recovery.invalidate()

    def _on_prepare_for_sleep(self, sleeping) -> None:
        self.recovery.invalidate(suspended=bool(sleeping))
        if bool(sleeping) or self.recovery.active:
            return
        log.info("system resumed — refreshing Bluetooth profile sessions")
        self.bearers.poke()
        self.profiles.reconnect("system resumed")

    def _post_available_sessions_setup(self) -> None:
        """Start consumers for whichever OBEX profiles are currently live."""
        # Bulk PBAP transfers share the MAP worker. Defer automatic pulls
        # during MAP's initial grace period so message access can retry promptly.
        if self.sessions.pbap is not None and self._contacts_refresh_needed():
            self._refresh_contacts()

        # Schedule periodic contacts refresh
        if self.sessions.pbap is not None and self._contacts_refresh_id is None:
            self._contacts_refresh_id = GLib.timeout_add_seconds(
                CONTACTS_REFRESH_SEC, self._periodic_refresh_contacts
            )

        # Wire up MAP MNS listener.
        # Resolve through the current cache; contacts refreshes in place.
        if self.sessions.map is not None and self.listener is None:
            self.listener = MapEventListener(
                sessions=self.sessions,
                on_sms=self.events.message,
                on_read=self._message_read,
                resolve_contact=lambda raw: self.contacts.resolve(raw),
                submit_obex=self.obex_worker.submit,
            )
            self.listener.start()
            self.mns_watch = MnsWatch(
                config.IPHONE_MAC,
                on_missing=self._mns_missing,
                on_present=self._mns_present,
            )
            self.mns_watch.start()

    def _mns_present(self) -> None:
        self._mns_reconnect_spent = False

    def _mns_missing(self, reason: str) -> None:
        """Recreate MAP, whose registration is what makes the iPhone open MNS."""
        if self._mns_reconnect_spent:
            log.warning(
                "%s again after reconnecting MAP; new messages will not arrive "
                "until the iPhone reconnects",
                reason,
            )
            return
        self._mns_reconnect_spent = True
        log.warning("%s; reconnecting MAP", reason)
        self.profiles.reconnect(reason)

    def _message_read(self, handle: str) -> None:
        if mark_event_handles_read([handle], storage=self.storage):
            if self._dbus_service is not None:
                self._dbus_service.emit_history_changed()

    def _post_sessions_setup(self) -> None:
        """Finish setup once both MAP and PBAP are live."""
        self._post_available_sessions_setup()

        self._sync_solicitation()

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
        # Manual sync also satisfies a deferred automatic refresh.
        self._finish_contacts_map_wait()
        count = self.contacts.refresh()
        # Completing PullAll proves that the iPhone granted Sync Contacts,
        # even when its phonebook is empty.
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
            "_build_id": self._running_build_id,
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
            "contacts_only_notifications": (
                self.notification_policy.contacts_only
            ),
            "storage_policy": self.storage.status.policy,
            "storage_state": self.storage.status.state,
            "storage_detail": self.storage.status.detail,
            **self._controller_identity(),
            **self.connectivity.snapshot(),
        }

    def _controller_identity(self) -> dict[str, object]:
        cached = getattr(self, "_controller_identity_cache", None)
        if cached is None:
            vendor = str(
                controller_hardware(config.ADAPTER).get("vendor") or ""
            )
            cached = {
                "controller_vendor": vendor,
                "ancs_limited_controller": ancs_limited_vendor(vendor),
            }
            self._controller_identity_cache = cached
        return cached

    def _contacts_refresh_needed(self) -> bool:
        """Whether startup or recovery still owes an automatic contact sync."""
        return self._contacts_refresh_deferred or (
            not self._contacts_initial_sync_done and self.contacts.count() == 0
        )

    def _refresh_contacts(self) -> None:
        """Best-effort wrapper used by startup and the periodic timer."""
        if (
            self._contacts_refresh_pending
            or self.sessions.pbap is None
            or not self.storage.status.can_write
        ):
            return
        if self.sessions.map is None and not self._contacts_map_wait_finished:
            self._contacts_refresh_deferred = True
            if self._contacts_map_wait_id is None:
                self._contacts_map_wait_id = GLib.timeout_add_seconds(
                    CONTACTS_MAP_GRACE_SECONDS, self._resume_deferred_contacts,
                )
            return
        self._finish_contacts_map_wait()
        self._sync_contacts()

    def _sync_contacts(
        self,
        success: Callable[[int], None] | None = None,
        failure: Callable[[Exception], None] | None = None,
    ) -> None:
        """Join or start one contact pull; called and completed on GLib."""
        if success is not None and failure is not None:
            # Coalescing must retain the worker queue's bound on callers.
            if len(self._contacts_sync_waiters) >= MAX_OBEX_PENDING_OPERATIONS:
                failure(RuntimeError("too many pending contact sync requests"))
                return
            self._contacts_sync_waiters.append((success, failure))
        if self._contacts_refresh_pending:
            return
        self._contacts_refresh_pending = True
        generation = self._contacts_storage_generation

        def succeeded(pulled):
            try:
                count = self._contacts_pulled(pulled)
            except Exception as error:
                failed(error)
            else:
                self._contacts_sync_finished(generation, count=count)

        def failed(error):
            self._contacts_sync_finished(generation, error=error)

        try:
            self.obex_worker.submit(self._pull_contacts, on_success=succeeded, on_error=failed)
        except Exception as error:
            failed(error)

    def _contacts_sync_finished(
        self, generation: int, *, count: int = 0, error: Exception | None = None,
    ) -> None:
        waiters, self._contacts_sync_waiters = self._contacts_sync_waiters, []
        self._contacts_refresh_pending = False
        if error is None and generation == self._contacts_storage_generation:
            # Zero usable destinations is still a successful sync. Retrying
            # MAP must not repeatedly download the same empty contact cache.
            self._contacts_initial_sync_done = True
        if error is not None:
            log.error("contacts refresh failed; using previous cache: %s", error)
            try:
                self.sessions.report_error(error)
            except Exception:
                log.exception("could not report contact sync transport failure")
        for success, failure in waiters:
            try:
                if error is None:
                    success(count)
                else:
                    failure(error)
            except Exception:
                log.exception("contact sync completion callback failed")
        if (
            error is not None
            and self._contacts_refresh_deferred
            and self._contacts_map_wait_finished
        ):
            # A manual pull can span the grace deadline. Its failure must not
            # consume the deferred automatic request. Automatic attempts clear
            # this flag before queuing, so this cannot form a retry loop.
            self._refresh_contacts()
        elif generation != self._contacts_storage_generation and self._contacts_refresh_needed():
            # Storage became ready while this older pull was pending, so its
            # recovery callback could not queue the replacement download yet.
            self._refresh_contacts()

    def _finish_contacts_map_wait(self) -> None:
        self._contacts_map_wait_finished = True
        self._contacts_refresh_deferred = False
        if self._contacts_map_wait_id is not None:
            GLib.source_remove(self._contacts_map_wait_id)
            self._contacts_map_wait_id = None

    def _resume_deferred_contacts(self) -> bool:
        self._contacts_map_wait_id = None
        self._contacts_map_wait_finished = True
        # If PBAP or storage is unavailable at expiry, leave the deferred
        # flag set so their recovery triggers the download without a new wait.
        self._refresh_contacts()
        return False

    def _periodic_refresh_contacts(self) -> bool:
        """GLib timeout callback. Return True to keep the timer running."""
        log.info("periodic contacts refresh tick")
        self._refresh_contacts()
        return True

    def _check_package_release(self) -> bool:
        current = installed_release()
        current_sha = installed_build_sha()
        current_build = build_id(current, current_sha) if current is not None else None
        if current_build == self._running_build_id:
            self._release_missing_checks = 0
            return True
        if current is None or (
            self._running_build_sha is not None and current_sha is None
        ):
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
            self._running_build_id,
            current_build,
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
            self.recovery.forget_phone()
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
        owner_match, self._bluez_owner_match = self._bluez_owner_match, None
        if owner_match is not None:
            try:
                owner_match.remove()
            except Exception:
                log.debug("could not remove BlueZ owner watch", exc_info=True)
        self.recovery.stop()
        self.read_receipts.close()
        self.adapter_class.stop()
        self.bearers.stop()
        self.profiles.stop()
        for tid_attr in (
            "_contacts_refresh_id",
            "_contacts_map_wait_id",
            "_release_check_id",
            "_target_config_check_id",
            "_storage_retry_id",
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
        if self.mns_watch is not None:
            self.mns_watch.stop()
        if self.listener is not None:
            self.listener.stop()
        if self.ancs is not None:
            self.ancs.stop()
        self.solicitation.stop()
        self.events.stop()
        if self._sleep_match is not None:
            try:
                self._sleep_match.remove()
            except Exception:
                log.debug("could not remove sleep monitor", exc_info=True)
            self._sleep_match = None
        if self._power_match is not None:
            try:
                self._power_match.remove()
            except Exception:
                log.debug("could not remove power monitor", exc_info=True)
            self._power_match = None
        if self._dbus_service is not None:
            self._dbus_service.close()
        # BlueZ 5.87 SIGSEGVs in gobex when RemoveSession runs on shutdown,
        # including after a healthy MAP/PBAP lifetime. Drop local session
        # state only; the D-Bus disconnect and the next open's stale cleanup
        # release whatever obexd still holds.
        self.obex_worker.shutdown(
            cleanup=self._cleanup_bluetooth_worker,
        )
        self.storage.close()
        self.sessions.stop_monitoring()
        main_loop.quit()

    def _cleanup_bluetooth_worker(self) -> None:
        self.recovery.adapter.finish_shutdown()
        self.sessions.close_all(remove_remote=False)

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
