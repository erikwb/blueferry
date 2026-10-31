"""Render the Qt settings form of real plugin manifests to PNG files.

Not a test. Run from the repository root:

    PYTHONPATH=src python tests/tools/render_plugin_forms.py OUTPUT_DIR MANIFEST...

For every manifest it builds the form exactly as Settings > Plugins does
(``plugin_settings_view`` and ``PluginConfigForm.qml``) and saves it at a
narrow and a wide width with every section unfolded, plus the wide form
with the sections folded as the manifest says. Values are the manifest
defaults; no plugin runs and nothing talks to D-Bus. Rendered offscreen with
the Breeze Dark colours (``--light`` for Breeze Light), for screenshots in
plugin READMEs and for checking a form before a release.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - re-runs this script with the colour scheme set
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
QML_DIR = ROOT / "src" / "blueferry" / "qt" / "qml"
WIDTHS = (700, 1400)
MARGIN = 24

# The form inside a plain window, with a bridge that only pre-checks
# (ShowIf) and records nothing. Loaded from QML_DIR so PluginConfigForm and
# its "ui" imports resolve as in the application.
WINDOW_QML = """
import QtQuick
import QtQuick.Controls as Controls
import org.kde.kirigami as Kirigami

Controls.ApplicationWindow {
    id: window
    property var config: ({})
    color: Kirigami.Theme.backgroundColor
    visible: true

    QtObject {
        id: bridge
        function checkPluginConfig(id, values) {
            const fields = window.config.fields || []
            const merged = {}
            fields.forEach(f => merged[f.key] = (f.key in values) ? values[f.key] : f.value)
            const visible = fields.filter(f => !f.showIfKey
                || String(merged[f.showIfKey]) === f.showIfValue).map(f => f.key)
            return {errors: {}, visible: visible, valid: true}
        }
        function savePluginConfig(id, values) {}
        function testPluginConfig(id, values) {}
        function signInPlugin(id, values) {}
        function cancelPluginSignIn() {}
        function openPluginHelp(id, key) {}
        function runPluginConfigAction(id, action) {}
    }

    PluginConfigForm {
        objectName: "renderedForm"
        x: %(margin)d
        y: %(margin)d
        width: window.width - 2 * %(margin)d
        bridge: bridge
        config: window.config
    }
}
"""


def _forms(paths: list[str]) -> list[tuple[str, dict]]:
    from blueferry import plugin_settings_view as view
    from blueferry.plugin_api.manifest import parse_manifest

    result = []
    for path in paths:
        manifest = parse_manifest(Path(path).read_text(encoding="utf-8"), path=Path(path))
        if not manifest.config:
            print(f"{path}: no [Config …] settings, skipped", file=sys.stderr)
            continue
        values = {field.key: field.empty() for field in manifest.config}
        result.append((manifest.alias or manifest.id, {
            "id": manifest.id, "name": manifest.name, "loaded": True, "errors": {},
            "status": {},
            "fields": view.form_fields(manifest, values),
            "groups": view.form_groups(manifest),
            "actions": view.form_actions(manifest),
        }))
    return result


def _render(out_dir: Path, paths: list[str]) -> None:
    from PySide6.QtCore import QObject, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlComponent, QQmlEngine
    from PySide6.QtQuick import QQuickWindow  # noqa: F401 - grabWindow needs the type
    from PySide6.QtTest import QTest

    forms = _forms(paths)
    application = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlEngine()
    engine.warnings.connect(
        lambda errors: [print("QML:", e.toString(), file=sys.stderr) for e in errors]
    )
    component = QQmlComponent(engine)
    component.setData((WINDOW_QML % {"margin": MARGIN}).encode(),
                      QUrl.fromLocalFile(str(QML_DIR / "RenderPluginForms.qml")))
    window = component.create()
    if window is None:
        raise SystemExit("\n".join(error.toString() for error in component.errors()))
    form = window.findChild(QObject, "renderedForm")

    def shoot(name: str, width: int) -> None:
        window.resize(width, 400)
        QTest.qWait(300)
        height = int(form.property("implicitHeight")) + 2 * MARGIN
        window.resize(width, height)
        QTest.qWait(300)
        path = out_dir / f"form-{name}-{width}.png"
        window.grabWindow().save(str(path))
        print(path)

    for name, config in forms:
        # Folded as the manifest says, then every section open.
        window.setProperty("config", json.loads(json.dumps(config)))
        QTest.qWait(200)
        shoot(f"{name}-folded", WIDTHS[-1])
        unfolded = [dict(group, collapsed=False) for group in config["groups"]]
        window.setProperty("config", dict(config, groups=unfolded))
        QTest.qWait(200)
        for width in WIDTHS:
            shoot(name, width)
    window.deleteLater()
    application.processEvents()


def main() -> None:
    args = [arg for arg in sys.argv[1:] if arg != "--light"]
    if len(args) < 2:
        raise SystemExit(__doc__)
    out_dir = Path(args[0])
    out_dir.mkdir(parents=True, exist_ok=True)
    if os.environ.get("BF_RENDER_SCHEME"):
        _render(out_dir, args[1:])
        return
    scheme = "BreezeLight" if "--light" in sys.argv else "BreezeDark"
    with tempfile.TemporaryDirectory() as config:
        shutil.copy(f"/usr/share/color-schemes/{scheme}.colors", Path(config) / "kdeglobals")
        env = dict(
            os.environ, BF_RENDER_SCHEME=scheme, XDG_CONFIG_HOME=config,
            QT_QPA_PLATFORM="offscreen", QT_QPA_PLATFORMTHEME="kde",
            QT_QUICK_CONTROLS_STYLE="org.kde.desktop",
            PYTHONPATH=str(ROOT / "src"),
        )
        subprocess.run(  # nosec B603 - fixed interpreter and script path
            [sys.executable, __file__, *sys.argv[1:]], env=env, check=True,
        )


if __name__ == "__main__":
    main()
