"""Orchestrate D-Bus, Bluetooth profiles, state, and desktop sinks.

The control API is published before Bluetooth initialization. Profile failures
leave it available in degraded mode and are retried periodically.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable

import dbus
from gi.repository import GLib

from blueferry import __version__, bluez_setup, config
from blueferry.adapter_class_supervisor import AdapterClassSupervisor
from blueferry.ancs.client import AncsClient
from blueferry.backend_lifecycle import installed_release
from blueferry.backend_operations import BackendDependencies
from blueferry.bearer_supervisor import BearerSupervisor
from blueferry.bluetooth_capabilities import (
    ancs_limited_vendor,
    bluetoothd_argv,
    bluez_hfp_plugin_active,
    bluez_hfp_plugin_possible,
    bluez_stack,
    controller_hardware,
)
from blueferry.bluetooth_recovery import (
    BluetoothRecovery,
    BluezRecoveryAdapter,
    RecoveryObservation,
    probe_map,
)
from blueferry.build_info import build_id, installed_build_sha, running_build_sha
from blueferry.bus import get_system_bus, main_loop
from blueferry.calls.controller import CallController
from blueferry.calls.settings import CallsSettings
from blueferry.calls.phone_status import PhoneStatus
from blueferry.commands import run_command
from blueferry.confirmed_groups import ConfirmedGroupsStore
from blueferry.connectivity import Connectivity
from blueferry.contact_sync import ContactSync
from blueferry.contacts import ContactsResolver
from blueferry.dbus_service import MessagesService, claim_bus_name
from blueferry.errors import BlueFerryError
from blueferry.event_dispatcher import EventDispatcher
from blueferry.glib_timers import schedule_periodic
from blueferry.group_routes import GroupRoutesStore
from blueferry.history import (
    history_count,
    mark_event_handles_read,
)
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
from blueferry.proximity_lock import (
    INHIBIT_ADAPTER_OFF,
    INHIBIT_DISCOVERING,
    INHIBIT_FORGOTTEN,
    INHIBIT_RECOVERY,
    ProximityLock,
    ProximityLockSettings,
    presence_from_bearers,
)
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


# Notice package replacement promptly without relying on pacman to reach into
# every logged-in user's systemd instance.
PACKAGE_RELEASE_CHECK_SEC = 10
TARGET_CONFIG_CHECK_SEC = 2
# The periodic bond check runs on the GLib loop. A timeout reads as "cannot
# inspect", which the check already treats as transient.
BOND_CHECK_TIMEOUT_SEC = 2.0
STORAGE_RETRY_SEC = 5
RESTART_AFTER_UPGRADE_EXIT = 75


class PairingRequiredError(RuntimeError):
    """Saved configuration exists, but BlueZ has no corresponding bond."""


def _in_background(target: Callable[[], None], name: str) -> None:
    threading.Thread(target=target, name=name, daemon=True).start()


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
        self.group_routes = GroupRoutesStore(storage=self.storage)
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
            on_call_action=self._notification_call_action,
        )
        self.listener: MapEventListener | None = None
        self.mns_watch: MnsWatch | None = None
        # One MAP reconnect per MNS outage; seeing MNS again rearms it.
        self._mns_reconnect_spent = False
        self.ancs: AncsClient | None = None
        self.adapter_class = AdapterClassSupervisor(config.ADAPTER)
        self.solicitation = SolicitationSupervisor(config.ADAPTER)
        # The saved phone-calls opt-in decides both the call controller and
        # whether the WirePlumber fragment keeps the hands-free roles.
        self.calls_settings = CallsSettings()
        self.phone_audio = WirePlumberPhoneAudioPolicy(
            allow_calls=self.calls_settings.enabled,
        )
        self._phone_audio_lock = threading.Lock()
        # None until looked up, "" when it could not be determined.
        self._bluez_version: str | None = None
        self._bluez_version_requested = False
        # Opt-in convenience lock. It only reads bearer state the supervisor
        # below already polls and never unlocks anything.
        self.proximity_settings = ProximityLockSettings()
        self.proximity = ProximityLock(
            enabled=self.proximity_settings.enabled,
            grace_sec=self.proximity_settings.grace_sec,
            read_presence=self._proximity_presence,
            on_status=self._emit_status,
            schedule=GLib.timeout_add_seconds,
            cancel=GLib.source_remove,
        )
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
            on_status=self._bearer_status_changed,
            on_le_state=self._observe_le_state,
            on_le_dial=self.solicitation.set_dialing,
            inbound_le_primed=self.solicitation.active,
        )
        # Optional HFP calls through oFono. Inert unless explicitly enabled;
        # construction performs no I/O. oFono is only asked to page the phone
        # (Modem.Powered) while the Classic bearer is up.
        self.calls = CallController(
            enabled=self.calls_settings.enabled,
            mac=config.IPHONE_MAC,
            adapter=config.ADAPTER,
            resolve_contact=self.contacts.resolve,
            on_calls_changed=self._emit_calls_changed,
            on_state_changed=self._emit_status,
            on_event=self.events.call,
            phone_reachable=lambda: self.bearers.bredr_connected,
            hfp_conflict=self._bluez_hfp_conflict,
            on_phone_status=self._on_phone_status,
        )
        self.contact_sync = ContactSync(
            sessions=self.sessions,
            storage=self.storage,
            contacts=self.contacts,
            submit=lambda *args, **kwargs: self.obex_worker.submit(*args, **kwargs),
            on_refreshed=self._contacts_refreshed,
        )
        self._bus_name = None
        self._dbus_service: MessagesService | None = None
        self._sleep_match = None
        self._power_match = None
        self._disconnect_match = None
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
            # A recovery power cycle drops Classic, and with it the audio of a
            # call routed to this computer. An LE hiccup is not worth that.
            busy=self.bearers.busy or self.calls.in_call,
        )

    def _pause_for_recovery(self) -> None:
        # The recovery power cycle drops the link on purpose.
        self.proximity.inhibit(INHIBIT_RECOVERY)
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
            self.proximity.inhibit(INHIBIT_RECOVERY, False)
            return
        self.adapter_class.start()
        self.solicitation.start()
        self.bearers.hold_le()
        self.bearers.reset_after_bluez_restart()
        self.profiles.resume()
        self.bearers.start()
        self.proximity.inhibit(INHIBIT_RECOVERY, False)

    def _emit_status(self) -> None:
        emit = getattr(self._dbus_service, "emit_status", None)
        if emit is not None:
            emit()

    def _bearer_status_changed(self) -> None:
        self.proximity.bearer_changed()
        self.calls.poke()
        self._emit_status()

    def _proximity_presence(self) -> bool | None:
        return presence_from_bearers(self.bearers.bredr_state, self.bearers.le_state)

    def _set_proximity_lock(self, enabled: bool, grace_sec: int) -> dict:
        selected, grace = self.proximity_settings.set(enabled, grace_sec)
        self.proximity.configure(selected, grace)
        log.info(
            "proximity lock %s (grace %ds)",
            "enabled" if selected else "disabled",
            grace,
        )
        return self.proximity.snapshot()

    def _set_calls_enabled(self, enabled: bool) -> dict:
        selected = self.calls_settings.set(enabled)
        self.calls.set_enabled(selected)
        log.info("phone calls %s", "enabled" if selected else "disabled")
        self._apply_phone_audio_roles()
        self._emit_status()
        return self.calls.snapshot()

    def _apply_phone_audio_roles(self) -> None:
        """Rewrite the WirePlumber fragment off the main loop.

        Reconciling may run ``wireplumber --version`` and restart WirePlumber
        (bounded, but seconds), which must not stall D-Bus replies. Threads
        of quick successive toggles take the lock in any order, so each
        writes the setting saved at that moment, not the one it was started
        for: whichever runs last leaves the fragment matching the setting.
        """
        if not config.KEEP_PHONE_AUDIO_ON_PHONE:
            return

        def apply() -> None:
            with self._phone_audio_lock:
                self.phone_audio = WirePlumberPhoneAudioPolicy(
                    allow_calls=self.calls_settings.enabled,
                )
                self.phone_audio.reconcile(enabled=True)

        _in_background(apply, "blueferry-phone-audio")

    def _bluez_hfp_conflict(self) -> bool:
        """Whether bluetoothd's own HFP plugin can be what blocks oFono.

        Asked by the call controller only after repeated power-up failures.
        The version lookup runs a command, so it happens off the main loop
        and the first answer is "no"; the controller asks again after its
        next failed attempt.
        """
        if not bluez_hfp_plugin_active(bluetoothd_argv()):
            return False
        if self._bluez_version is None:
            if not self._bluez_version_requested:
                self._bluez_version_requested = True
                _in_background(self._read_bluez_version, "blueferry-bluez-version")
            return False
        return bluez_hfp_plugin_possible(self._bluez_version)

    def _read_bluez_version(self) -> None:
        version = ""
        try:
            stack = bluez_stack(run_command=run_command, experimental=False)
            version = str(stack.get("bluez_version") or "")
        finally:
            self._bluez_version = version

    def _emit_calls_changed(self) -> None:
        emit = getattr(self._dbus_service, "emit_calls_changed", None)
        if emit is not None:
            emit()

    def _notification_call_action(self, call_id: str, action: str) -> None:
        """Answer or decline from an incoming-call desktop notification."""
        operation = self.calls.answer if action == "answer" else self.calls.hangup

        def failed(error: Exception) -> None:
            # The controller already logged oFono's error name; never log the
            # raw oFono text here, it can contain the caller's number.
            log.debug(
                "notification %s failed: %s", action,
                getattr(error, "dbus_suffix", type(error).__name__),
            )

        try:
            operation(call_id, lambda _result: None, failed)
        except BlueFerryError as error:
            log.info("could not %s the call from its notification (%s)", action, error.dbus_suffix)

    def _on_phone_status(self, status: PhoneStatus) -> None:
        """The phone's battery/signal/operator changed: content-free signal."""
        del status  # clients fetch the values with GetStatus
        self._emit_status()

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
        # Construction may overlap the previous daemon finishing recovery.
        # Only its successor may refresh or act on the persisted obligation.
        self.recovery.adapter.reload_journal()
        self._dbus_service = MessagesService(
            self._bus_name,
            self.sessions,
            BackendDependencies(
                on_sent=self.events.sent,
                on_group_sent=self.events.group_sent,
                submit_obex=self.obex_worker.submit,
                defer_mark_read=self.read_receipts.defer,
                sync_contacts=self.contact_sync.sync,
                contacts=self.contacts,
                status_provider=self._status,
                notification_policy=self.notification_policy,
                on_notification_policy_changed=self._emit_status,
                starred_threads=self.starred_threads,
                confirmed_groups=self.confirmed_groups,
                group_routes=self.group_routes,
                storage=self.storage,
                prepare_storage=prepare_storage,
                on_storage_prepared=self._apply_storage_preparation,
                on_storage_changed=self._on_storage_changed,
                set_proximity_lock=self._set_proximity_lock,
                calls=self.calls,
                set_calls_enabled=self._set_calls_enabled,
            ),
        )
        self.events.set_dbus_service(self._dbus_service)
        self._initialize_storage()
        log.info("DBus service ready: %s", BUS_NAME)
        self._emit_status()

        if self._packaged:
            self._schedule_periodic(
                "_release_check_id", PACKAGE_RELEASE_CHECK_SEC, self._check_package_release
            )
        self._schedule_periodic(
            "_target_config_check_id", TARGET_CONFIG_CHECK_SEC, self._check_target_config
        )
        self._schedule_periodic("_storage_retry_id", STORAGE_RETRY_SEC, self._retry_storage)

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._signal)

        # Let the GLib loop begin dispatching D-Bus before potentially slow
        # profile setup. This makes activation and GetStatus deterministic.
        self._startup_id = GLib.timeout_add(250, self._initialize)

    def _schedule_periodic(
        self, attr: str, seconds: int, callback: Callable[[], bool]
    ) -> None:
        """Run ``callback`` every ``seconds`` and keep its source id in ``attr``.

        The id is forgotten as soon as GLib destroys the source, so ``stop()``
        only removes timers that are still live.
        """
        setattr(self, attr, schedule_periodic(
            GLib.timeout_add_seconds, seconds, callback, lambda: setattr(self, attr, None),
        ))

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
        self.contact_sync.storage_prepared()
        self.events.seed_historical_ancs(prepared.historical_ancs)
        if prepared.has_messages:
            self._mark_setup_task(MESSAGE_NOTIFICATIONS)

    def _on_storage_changed(self) -> None:
        if self.storage.status.can_write and self.contacts.count() > 0:
            self._mark_setup_task(CONTACTS)
        self.contact_sync.storage_changed()
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
        if bond_status(
            config.IPHONE_MAC, config.ADAPTER, timeout=BOND_CHECK_TIMEOUT_SEC,
        ) is not True:
            raise PairingRequiredError(
                "the saved iPhone is not currently paired; open a client to pair it"
            )

        with self._phone_audio_lock:
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
        # Optional calls only observe oFono; failures there never degrade
        # messaging, and a missing oFono is retried in the background.
        self.calls.start()

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
            # Adapter power and discovery state belonged to the old
            # bluetoothd; the replacement is read again in _on_bluez_restart.
            self.proximity.inhibit(INHIBIT_ADAPTER_OFF, False)
            self.proximity.inhibit(INHIBIT_DISCOVERING, False)
        # Bearer observations from the old bluetoothd no longer prove presence.
        self.proximity.reset()
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
        self._read_adapter_inhibitors()

    def _read_adapter_inhibitors(self) -> None:
        """Seed the adapter-derived proximity inhibitors from bluetoothd.

        PropertiesChanged only reports changes, so after startup or a
        bluetoothd restart the current Powered/Discovering values are read
        once, asynchronously. A reply from a superseded owner is ignored.
        """
        generation = self._bluez_owner_generation

        def reply(properties) -> None:
            if generation == self._bluez_owner_generation:
                self._apply_adapter_inhibitors(properties)

        def failed(error) -> None:
            log.debug("could not read adapter state for proximity lock: %s", error)

        try:
            get_system_bus().call_async(
                "org.bluez",
                f"/org/bluez/{config.ADAPTER}",
                "org.freedesktop.DBus.Properties",
                "GetAll",
                "s",
                ("org.bluez.Adapter1",),
                reply,
                failed,
                timeout=5.0,
            )
        except dbus.exceptions.DBusException as error:
            failed(error)

    def _apply_adapter_inhibitors(self, properties) -> None:
        # Turning Bluetooth off on the desktop, or discovery for pairing, is a
        # deliberate local action, not the phone walking away.
        if "Powered" in properties:
            self.proximity.inhibit(INHIBIT_ADAPTER_OFF, not bool(properties["Powered"]))
        if "Discovering" in properties:
            self.proximity.inhibit(INHIBIT_DISCOVERING, bool(properties["Discovering"]))

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
            self._read_adapter_inhibitors()
        if self._disconnect_match is None:
            # Device1.Disconnected(reason, message) is documented in BlueZ
            # 5.87; on builds without it the match simply never fires.
            try:
                self._disconnect_match = get_system_bus().add_signal_receiver(
                    self._on_device_disconnected,
                    dbus_interface="org.bluez.Device1",
                    signal_name="Disconnected",
                    bus_name="org.bluez",
                    path=(
                        f"/org/bluez/{config.ADAPTER}/"
                        f"dev_{config.IPHONE_MAC.replace(':', '_')}"
                    ),
                )
            except dbus.exceptions.DBusException:
                log.debug("BlueZ disconnect reasons unavailable", exc_info=True)
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

    def _on_device_disconnected(self, reason="", _message="") -> None:
        # Only the reason code is inspected or logged, never the message.
        code = str(reason)[:128]
        log.debug("iPhone disconnected (%s)", code)
        if code.rsplit(".", 1)[-1].casefold() == "local":
            self.proximity.local_disconnect()

    def _on_adapter_power_changed(self, _interface, changed, invalidated) -> None:
        if not self.recovery.active and ("Powered" in changed or "Powered" in invalidated):
            self.recovery.invalidate()
        self._apply_adapter_inhibitors(changed)

    def _on_prepare_for_sleep(self, sleeping) -> None:
        self.recovery.invalidate(suspended=bool(sleeping))
        if bool(sleeping):
            self.proximity.suspending()
            return
        if self.recovery.active:
            self.proximity.resumed()
            return
        log.info("system resumed — refreshing Bluetooth profile sessions")
        self.bearers.poke()
        # Ending the sleep inhibitor never arms on the pre-suspend cache; the
        # lock waits for a bearer transition or a post-resume poll.
        self.proximity.resumed()
        self.profiles.reconnect("system resumed")

    def _post_available_sessions_setup(self) -> None:
        """Start consumers for whichever OBEX profiles are currently live."""
        self.contact_sync.profiles_available()

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

    def _contacts_refreshed(self) -> None:
        """GLib-side follow-up after a pull replaced the contact cache."""
        # Completing PullAll proves that the iPhone granted Sync Contacts,
        # even when its phonebook is empty.
        self._mark_setup_task(CONTACTS)
        if self._dbus_service is not None:
            self._dbus_service.operations.invalidate_conversations()
            self._dbus_service.emit_history_changed()
        self._emit_status()

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
            **self.proximity.snapshot(),
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
            bonded = bond_status(mac, adapter, timeout=BOND_CHECK_TIMEOUT_SEC)
            if bonded is not False:
                return True
            # The daemon may already have initialized its supervisors when a
            # user removes the bond through a desktop Bluetooth client. Stop
            # the current process so those supervisors cannot keep issuing
            # Connect/CreateSession calls for an explicitly forgotten phone.
            # ``None`` is deliberately ignored above because it represents an
            # unavailable adapter or transient BlueZ inspection failure.
            log.info("saved iPhone bond was removed; stopping daemon")
            self.proximity.inhibit(INHIBIT_FORGOTTEN)
            self.recovery.forget_phone()
            main_loop.quit()
            return False
        self.proximity.inhibit(INHIBIT_FORGOTTEN)
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
        self.proximity.stop()
        self.recovery.stop()
        self.calls.stop()
        self.read_receipts.close()
        self.adapter_class.stop()
        self.bearers.stop()
        self.profiles.stop()
        self.contact_sync.stop()
        for tid_attr in (
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
        if self._disconnect_match is not None:
            try:
                self._disconnect_match.remove()
            except Exception:
                log.debug("could not remove disconnect monitor", exc_info=True)
            self._disconnect_match = None
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
        # A second daemon still runs stop() when claiming the name fails. It
        # must not replay or clear recovery state belonging to the live owner.
        if self._bus_name is not None:
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
