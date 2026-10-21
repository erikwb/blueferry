#!/usr/bin/env python3
"""Flag code that can stall the daemon's GLib main loop or confuse dbus-python.

The daemon runs every BlueZ, OBEX and client callback on one GLib main loop.
Review kept finding the same three mistakes, so this AST check catches them
before a reviewer has to:

``sync-dbus``
    A dbus-python proxy method call (``proxy.StartNotify()``) without
    ``reply_handler``/``error_handler``, ``call_blocking``, or one of the
    blocking bus helpers (``release_name``, ``list_names``,
    ``name_has_owner``, ``get_name_owner``). Such a call waits for the reply
    while nothing else is dispatched.
``blocking``
    ``time.sleep``, ``subprocess.run``/``call``/``check_call``/
    ``check_output`` without a short ``timeout``, ``sqlite3.connect``
    without a short ``timeout`` (its default lock wait is five seconds), or
    a synchronous Gio D-Bus API (``call_sync``, ``bus_get_sync``). Import
    aliases are resolved, so ``from time import sleep`` or ``import
    subprocess as sp`` is caught too.
``untyped-empty``
    A bare ``{}``/``[]`` passed to a D-Bus method, or ``dbus.Dictionary``/
    ``dbus.Array`` built without ``signature=``. With ``introspect=False``
    dbus-python guesses a signature from the value and an empty container
    gives it nothing to guess from (``WriteValue(..., {})`` → ValueError).

``sync-dbus`` and ``blocking`` apply to modules the daemon imports
(transitively from ``blueferry.daemon``); ``untyped-empty`` applies to every
module, because the ValueError hits clients just as hard. Deliberate
exceptions go in ``ALLOWLIST`` with a reason; findings that predate the check
and still need fixing are listed in ``KNOWN_DEBT`` (``RUNTIME_KNOWN_DEBT`` for
calls only the test-suite guard sees). An entry is keyed by
module, function and rule, so it exempts every matching call in that whole
function, not one line: keep exempted functions small, and re-check the
reason when such a function grows. Exit status is 1 on new findings or
stale entries.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT / "src" / "blueferry"
DAEMON_ENTRY = "blueferry.daemon"

# Longest wait that still counts as "short" for blocking calls on the loop.
SHORT_TIMEOUT_SEC = 1.0

# (module, enclosing function qualname, rule) -> why it is acceptable.
# Every entry here is safe: the call never runs on the daemon's main loop
# (worker thread, CLI or helper process) or is bounded by design. Keep
# reasons specific: they are the review record for each exception.
# An entry covers every call of that rule in the whole function.
_WORKER = "runs on the OBEX worker thread (ObexWorker.submit), never on the loop"
_SETUP_CLI = (
    "setup/pairing CLI path in a separate process; the daemon imports this "
    "module only for other helpers"
)
_HELPER_PROCESS = "runs in the short-lived client-activation helper process"
ALLOWLIST: dict[tuple[str, str, str], str] = {
    ("blueferry.backend_lifecycle", "_status", "sync-dbus"):
        "client-side backend check (cli/tui/ui); the daemon only uses installed_release",
    ("blueferry.bluetooth_capabilities", "compatibility", "sync-dbus"):
        "only called from pair_setup's CLI compatibility report",
    ("blueferry.bluetooth_recovery", "BluezRecoveryAdapter._bus_id", "sync-dbus"): _WORKER,
    ("blueferry.bluetooth_recovery", "BluezRecoveryAdapter.cycle", "sync-dbus"): _WORKER,
    ("blueferry.bluetooth_recovery", "BluezRecoveryAdapter.restore", "sync-dbus"): _WORKER,
    ("blueferry.bluetooth_recovery", "BluezRecoveryAdapter.finish_shutdown", "blocking"):
        "shutdown cleanup on the worker after main_loop.run() has returned",
    ("blueferry.bluetooth_recovery", "probe_map", "sync-dbus"): _WORKER,
    ("blueferry.bluez_setup", "register_advert", "blocking"):
        "sleeps only with settle_for_pairing=True, which only pair_setup passes",
    ("blueferry.client_activation", "_open_legacy_gtk", "sync-dbus"): _HELPER_PROCESS,
    ("blueferry.client_activation", "forward_to_legacy_gtk", "sync-dbus"):
        "GTK client start-up preflight on its own private bus, in the client process",
    ("blueferry.client_activation", "open_message", "sync-dbus"): _HELPER_PROCESS,
    ("blueferry.client_activation", "start_transient_service", "sync-dbus"): _HELPER_PROCESS,
    ("blueferry.contacts", "open_vcard_listing", "blocking"): _WORKER,
    ("blueferry.notification_open", "open_target", "sync-dbus"):
        "runs in the blueferry-open-notification helper process (its main), on a private bus",
    ("blueferry.contacts", "open_vcard_listing", "sync-dbus"): _WORKER,
    ("blueferry.obex.map_events", "_fetch_bmessage", "blocking"): _WORKER,
    ("blueferry.obex.map_events", "_fetch_bmessage", "sync-dbus"): _WORKER,
    ("blueferry.obex.map_query", "_navigate_to_folder", "sync-dbus"): _WORKER,
    ("blueferry.obex.map_query", "lookup_otp_metadata", "sync-dbus"): _WORKER,
    ("blueferry.obex.map_query", "list_recent_messages", "sync-dbus"): _WORKER,
    ("blueferry.obex.map_read", "set_message_read", "sync-dbus"): _WORKER,
    ("blueferry.obex.map_send", "_push_bmessage", "sync-dbus"): _WORKER,
    ("blueferry.obex.sessions", "_create_session", "sync-dbus"): _WORKER,
    ("blueferry.obex.transfer", "wait_for_transfer", "sync-dbus"): _WORKER,
    ("blueferry.obex.transfer", "wait_for_transfer.read_status", "sync-dbus"): _WORKER,
    **{
        ("blueferry.pair_setup", function, rule): _SETUP_CLI
        for function, rule in (
            ("_activate_obex_mns", "sync-dbus"),
            ("_adapter_dbus_fields", "sync-dbus"),
            ("_adapter_identity", "sync-dbus"),
            ("_bluetooth_session_owners", "sync-dbus"),
            ("_bluez_device_snapshot", "sync-dbus"),
            ("_complete_pairing_transaction", "sync-dbus"),
            ("_device_is_paired", "sync-dbus"),
            ("_dispatching_wait", "blocking"),
            ("_le_bearer_snapshot", "sync-dbus"),
            ("_pairable_window", "sync-dbus"),
            ("_prefer_bearer", "sync-dbus"),
            ("_rediscover_pairing_device", "blocking"),
            ("_rediscover_pairing_device", "sync-dbus"),
            ("_stop_discovery", "sync-dbus"),
            ("_wait_for_classic_settled", "blocking"),
            ("_wait_for_classic_settled", "sync-dbus"),
            ("_wait_for_daemon_transports", "blocking"),
            ("_wait_for_paired_device", "blocking"),
            ("discover_devices", "blocking"),
            ("discover_devices", "sync-dbus"),
            ("forget_device", "sync-dbus"),
            ("list_devices", "sync-dbus"),
            ("trust_device", "sync-dbus"),
        )
    },
    ("blueferry.service_manager", "BusActivatedServices._owner", "sync-dbus"):
        "backend_service_manager is used by CLI/setup clients, not the daemon",
    ("blueferry.service_manager", "BusActivatedServices._owner_pid", "sync-dbus"):
        "backend_service_manager is used by CLI/setup clients, not the daemon",
    ("blueferry.service_manager", "BusActivatedServices._start", "sync-dbus"):
        "backend_service_manager is used by CLI/setup clients, not the daemon",
}

# Real findings that predate this check, NOT safe: each one blocks the main
# loop today (usually briefly, against a local service) and is
# waiting for a migration (see ARCHITECTURE.md, "D-Bus calls"). Remove the
# entry with the fix; never add new ones.
_LIBNOTIFY = "Notify returns the id the popup tracker needs; move bookkeeping to reply"
_SQLITE = "default 5 s lock wait on the loop for some callers; open once or off-loop"
KNOWN_DEBT: dict[tuple[str, str, str], str] = {
    ("blueferry.ancs.client", "AncsClient._bind_manager_once", "sync-dbus"):
        "GetManagedObjects at start and on BlueZ owner change",
    ("blueferry.ancs.client", "AncsClient._stop_bluez_notifications", "sync-dbus"):
        "StopNotify per characteristic on reset",
    ("blueferry.ancs.client", "AncsClient._start_notify", "sync-dbus"):
        "StartNotify per characteristic; needs an async subscribe "
        "state machine like AMS",
    ("blueferry.bearer_supervisor", "BearerSupervisor._prefer_bluez", "sync-dbus"):
        "Get+Set of PreferredBearer before an async connect",
    ("blueferry.bearer_supervisor", "BearerSupervisor._connected_property", "sync-dbus"):
        "Device1/Bearer property reads from timers and signals",
    ("blueferry.bluetooth_recovery", "BluezRecoveryAdapter.read_async", "sync-dbus"):
        "get_name_owner before the asynchronous GetManagedObjects",
    ("blueferry.obex.transfer", "_add_transfer_receiver", "sync-dbus"):
        "get_name_owner() when a transfer receiver is added",
    ("blueferry.bluetooth_recovery", "BluezRecoveryAdapter.read", "sync-dbus"):
        "get_name_owner + GetManagedObjects; also called from GLib health checks, "
        "not only from the worker",
    ("blueferry.bluez_setup", "current_cod", "sync-dbus"):
        "adapter class check from the adapter-class supervisor timer",
    ("blueferry.call_history_repository", "CallHistoryRepository._open", "blocking"): _SQLITE,
    ("blueferry.commands", "run_command", "blocking"):
        "btmgmt/WirePlumber helpers run on the loop from adapter-class repair and setup",
    ("blueferry.contact_repository", "_open_db", "blocking"): _SQLITE,
    ("blueferry.contact_repository", "_open_read_only", "blocking"): _SQLITE,
    ("blueferry.dbus_security", "CallerGuard._bus_credentials", "sync-dbus"):
        "GetConnectionCredentials once per new caller, inside method dispatch",
    ("blueferry.dbus_service", "MessagesService._open_legacy_gtk_message", "sync-dbus"):
        "get_name_owner, list_names and GetConnectionUnixProcessID inside method dispatch",
    ("blueferry.event_dispatcher", "EventDispatcher._notification_server_owned", "sync-dbus"):
        "name_has_owner for the desktop notification server; watch NameOwnerChanged instead",
    ("blueferry.history", "_open_database", "blocking"): _SQLITE,
    ("blueferry.pair_setup", "bond_status", "sync-dbus"):
        "daemon target check with a 2 s timeout; bounded but synchronous",
    ("blueferry.sinks.libnotify", "LibnotifySink._ancs_action_result", "sync-dbus"): _LIBNOTIFY,
    ("blueferry.sinks.libnotify", "LibnotifySink._notify_missed_call", "sync-dbus"): _LIBNOTIFY,
    ("blueferry.sinks.libnotify", "LibnotifySink.handle", "sync-dbus"): _LIBNOTIFY,
    ("blueferry.sinks.libnotify", "LibnotifySink.handle_ancs", "sync-dbus"): _LIBNOTIFY,
    ("blueferry.sinks.libnotify", "LibnotifySink.handle_call", "sync-dbus"): _LIBNOTIFY,
    ("blueferry.sinks.libnotify", "LibnotifySink.handle_phone_battery_low", "sync-dbus"):
        _LIBNOTIFY,
    ("blueferry.sinks.libnotify", "_notification_hints", "sync-dbus"):
        "list_names per popup to pick the desktop-entry hint; track client names by signal",
}

# Blocking calls the AST cannot see (a method fetched with getattr), found by
# the sync_dbus_guard in tests/conftest.py at run time. Same rules as
# KNOWN_DEBT: NOT safe, remove the entry with the fix, never add new ones.
# Keyed by (module, enclosing function qualname).
RUNTIME_KNOWN_DEBT: dict[tuple[str, str], str] = {
    ("blueferry.ancs.client", "AncsClient._cached_notifying"):
        "Properties.Get(Notifying) through getattr before each StartNotify decision",
}

_DBUS_METHOD = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_ASYNC_KWARGS = frozenset({"reply_handler", "error_handler"})
_SUBPROCESS_BLOCKING = frozenset({"run", "call", "check_call", "check_output"})
# dbus-python Bus helpers that wrap a blocking call to the bus daemon.
_BUS_HELPERS = frozenset({"release_name", "list_names", "name_has_owner", "get_name_owner"})
# Synchronous Gio D-Bus entry points (Gio.DBusConnection/DBusProxy).
_GIO_SYNC = frozenset({"call_sync", "bus_get_sync"})
# dbus-python type constructors and other capitalised callables that are not
# remote method calls.
_NOT_REMOTE = frozenset({
    "Array", "Boolean", "Byte", "ByteArray", "Dictionary", "Double", "Int16",
    "Int32", "Int64", "Interface", "ObjectPath", "Signature", "String",
    "Struct", "UInt16", "UInt32", "UInt64", "UnixFd",
})


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    module: str
    function: str
    rule: str
    message: str

    def key(self) -> tuple[str, str, str]:
        return (self.module, self.function, self.rule)

    def __str__(self) -> str:
        where = self.path.relative_to(ROOT) if self.path.is_relative_to(ROOT) else self.path
        return f"{where}:{self.line}: [{self.rule}] {self.function}: {self.message}"


def module_name(path: Path, package_dir: Path = PACKAGE_DIR) -> str:
    parts = list(path.relative_to(package_dir.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(tree: ast.AST, module: str, is_package: bool) -> set[str]:
    """Absolute blueferry module names one module imports at import time.

    Function-local imports are not followed: they are how CLI-only helpers in
    shared modules (``pair_setup`` importing ``client``) stay out of the
    daemon, and a call inside them still shows up if the daemon reaches the
    importing module on its own.
    """
    found: set[str] = set()
    package = module if is_package else module.rpartition(".")[0]
    for node in _import_time_nodes(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return {name for name in found if name.split(".")[0] == "blueferry"}


def _import_time_nodes(tree: ast.Module) -> Iterator[ast.stmt]:
    """Statements that run on import: the module body and its if/try blocks."""
    todo: list[ast.stmt] = list(tree.body)
    while todo:
        node = todo.pop()
        yield node
        if isinstance(node, ast.If):
            todo.extend(node.body + node.orelse)
        elif isinstance(node, ast.Try):
            todo.extend(node.body + node.orelse + node.finalbody)
            for handler in node.handlers:
                todo.extend(handler.body)


def daemon_modules(sources: dict[str, tuple[Path, ast.Module]]) -> set[str]:
    """Modules reachable from the daemon entry point through imports."""
    seen: set[str] = set()
    todo = [DAEMON_ENTRY]
    while todo:
        name = todo.pop()
        if name in seen or name not in sources:
            continue
        seen.add(name)
        path, tree = sources[name]
        todo.extend(_imports(tree, name, path.name == "__init__.py"))
        # Importing a submodule runs its parent packages too.
        parent = name.rpartition(".")[0]
        if parent:
            todo.append(parent)
    return seen


def _numeric_constants(tree: ast.Module) -> dict[str, float]:
    values: dict[str, float] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, (int, float))
            and not isinstance(value.value, bool)
        ):
            values[target.id] = float(value.value)
    return values


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else None
    return None


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> dotted name it stands for, for ``import x as y`` and
    ``from x import y [as z]``, so ``from time import sleep`` or
    ``import subprocess as sp`` cannot hide a blocking call."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                if alias.name != "*":
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _is_empty_container(node: ast.AST) -> bool:
    return (
        (isinstance(node, ast.Dict) and not node.keys)
        or (isinstance(node, (ast.List, ast.Tuple)) and not node.elts)
    )


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: Path, module: str, tree: ast.Module, main_loop: bool) -> None:
        self.path = path
        self.module = module
        self.main_loop = main_loop
        self.constants = _numeric_constants(tree)
        self.imported = _imported_names(tree)
        self.aliases = _import_aliases(tree)
        self.scope: list[str] = []
        self.findings: list[Finding] = []

    def _report(self, node: ast.AST, rule: str, message: str) -> None:
        self.findings.append(Finding(
            self.path, getattr(node, "lineno", 0), self.module,
            ".".join(self.scope) or "<module>", rule, message,
        ))

    def _scoped(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_ClassDef = _scoped

    def _timeout(self, call: ast.Call) -> float | None:
        for keyword in call.keywords:
            if keyword.arg == "timeout":
                value = keyword.value
                if isinstance(value, ast.Constant) and isinstance(value.value, (int, float)):
                    return float(value.value)
                if isinstance(value, ast.Name):
                    return self.constants.get(value.id)
                return None
        return None

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func)
        keywords = {keyword.arg for keyword in node.keywords}
        if self.main_loop:
            self._check_blocking(node, self._resolve(name))
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if name in ("dbus.Dictionary", "dbus.Array") and "signature" not in keywords:
                self._report(node, "untyped-empty", f"{name} without signature=")
            elif self._is_remote_call(node.func):
                if any(_is_empty_container(arg) for arg in node.args):
                    self._report(node, "untyped-empty", f"bare empty container passed to {attr}")
                if self.main_loop and not _ASYNC_KWARGS <= keywords:
                    self._report(
                        node, "sync-dbus",
                        f"{attr}() without reply_handler/error_handler",
                    )
            elif attr == "call_async" and len(node.args) >= 6:
                args = node.args[5]
                if isinstance(args, (ast.Tuple, ast.List)) and any(
                    _is_empty_container(arg) for arg in args.elts
                ):
                    self._report(node, "untyped-empty", "bare empty container in call_async args")
            elif attr == "call_blocking" and self.main_loop:
                self._report(node, "sync-dbus", "call_blocking on the main loop")
            elif attr in _BUS_HELPERS and self.main_loop and not (
                isinstance(node.func.value, ast.Name) and node.func.value.id in ("self", "cls")
            ):
                self._report(node, "sync-dbus", f"blocking bus helper {attr}() on the main loop")
        self.generic_visit(node)

    def _is_remote_call(self, func: ast.Attribute) -> bool:
        if not _DBUS_METHOD.match(func.attr) or func.attr in _NOT_REMOTE:
            return False
        receiver = func.value
        # Signals emitted by our own service objects, and classes reached
        # through an imported module (``errors.NotReadyError(...)``).
        if isinstance(receiver, ast.Name) and receiver.id in ("self", "cls"):
            return False
        root = _dotted(receiver)
        return not (root and root.split(".")[0] in self.imported)

    def _resolve(self, name: str | None) -> str | None:
        if not name:
            return name
        head, dot, rest = name.partition(".")
        target = self.aliases.get(head)
        return f"{target}{dot}{rest}" if target else name

    def _check_blocking(self, node: ast.Call, name: str | None) -> None:
        attr = node.func.attr if isinstance(node.func, ast.Attribute) else (
            name.rpartition(".")[2] if name else name
        )
        if attr in _GIO_SYNC:
            self._report(node, "blocking", f"synchronous Gio D-Bus call {attr}() on the main loop")
        elif name == "time.sleep":
            self._report(node, "blocking", "time.sleep on the main loop")
        elif name and name.startswith("subprocess.") and (
            name.split(".", 1)[1] in _SUBPROCESS_BLOCKING
        ):
            timeout = self._timeout(node)
            if timeout is None or timeout > SHORT_TIMEOUT_SEC:
                self._report(node, "blocking", f"{name} without a timeout <= {SHORT_TIMEOUT_SEC}s")
        elif name == "sqlite3.connect":
            timeout = self._timeout(node)
            if timeout is None or timeout > SHORT_TIMEOUT_SEC:
                self._report(
                    node, "blocking", f"sqlite3.connect without timeout <= {SHORT_TIMEOUT_SEC}s",
                )


def load_sources(package_dir: Path = PACKAGE_DIR) -> dict[str, tuple[Path, ast.Module]]:
    sources = {}
    for path in sorted(package_dir.rglob("*.py")):
        sources[module_name(path, package_dir)] = (
            path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
    return sources


def lint(package_dir: Path = PACKAGE_DIR) -> tuple[list[Finding], list[Finding], set]:
    """Return (findings, allowed findings, stale allowlist keys)."""
    sources = load_sources(package_dir)
    main_loop = daemon_modules(sources)
    findings: list[Finding] = []
    for module, (path, tree) in sources.items():
        visitor = _Visitor(path, module, tree, module in main_loop)
        visitor.visit(tree)
        findings.extend(visitor.findings)
    exempt = {**ALLOWLIST, **KNOWN_DEBT}
    allowed = [finding for finding in findings if finding.key() in exempt]
    remaining = [finding for finding in findings if finding.key() not in exempt]
    stale = set(exempt) - {finding.key() for finding in allowed}
    return remaining, allowed, stale


def iter_report(verbose: bool) -> Iterator[str]:
    remaining, allowed, stale = lint()
    yield from map(str, remaining)
    for key in sorted(stale):
        yield f"stale allowlist entry (nothing matches it any more): {key}"
    if verbose:
        for finding in allowed:
            if finding.key() in KNOWN_DEBT:
                yield f"known debt: {finding} -- {KNOWN_DEBT[finding.key()]}"
            else:
                yield f"allowed: {finding} -- {ALLOWLIST[finding.key()]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true", help="also list allowed findings")
    options = parser.parse_args(argv)
    status = 0
    for line in iter_report(options.verbose):
        print(line)
        if not line.startswith(("allowed: ", "known debt: ")):
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
