"""Load the installed Qt client with its default style and no backend I/O.

Run with /usr/bin/python3 -I packaging/smoke-qt.py so checkout sources and
user-installed Python modules cannot hide missing package dependencies.
"""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def main() -> int:
    with TemporaryDirectory(prefix="blueferry-qt-smoke-") as temporary:
        runtime = Path(temporary)
        os.environ.update(
            QT_QPA_PLATFORM="offscreen",
            QT_QUICK_BACKEND="software",
            NO_AT_BRIDGE="1",
            XDG_RUNTIME_DIR=temporary,
            XDG_CONFIG_HOME=str(runtime / "config"),
            XDG_CACHE_HOME=str(runtime / "cache"),
            XDG_DATA_HOME=str(runtime / "data"),
            XDG_STATE_HOME=str(runtime / "state"),
            DBUS_SESSION_BUS_ADDRESS=f"unix:path={runtime / 'no-bus'}",
            DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={runtime / 'no-bus'}",
        )
        for variable in (
            "QT_QPA_PLATFORMTHEME", "QT_STYLE_OVERRIDE", "QT_QUICK_CONTROLS_STYLE",
            "QML_IMPORT_PATH", "QML2_IMPORT_PATH", "QT_PLUGIN_PATH",
        ):
            os.environ.pop(variable, None)

        from PySide6.QtCore import QTimer

        from blueferry.qt import app as qt_app

        if Path(qt_app.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1]):
            raise RuntimeError("Smoke test must load the installed package, not checkout sources")

        controller_type = qt_app.BridgeController

        def inert_controller(*, parent):
            controller = controller_type(subscribe=False, autostart=False, parent=parent)
            QTimer.singleShot(250, parent.quit)
            return controller

        with (
            patch.object(qt_app, "BridgeController", inert_controller),
            patch.object(qt_app, "_create_system_tray", return_value=None),
        ):
            result = qt_app.main()
        if result == 0:
            print("Installed BlueFerry Qt client loaded successfully")
        return result


if __name__ == "__main__":
    raise SystemExit(main())
