"""Qt drop zone of the main window: adapter, bridge and QML layer, fakes only.

The decisions are tested without Qt in ``tests/test_file_drop_view.py``;
these tests cover the pool, URL conversion, the bridge slots and the QML
picker and explanation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine

from blueferry import plugin_surfaces as surfaces
from blueferry.qt.plugin_surfaces import PluginSurfaces, uri_text
from tests.qt_settle import Settler, main_thread_gc, wait

pytestmark = pytest.mark.usefixtures(main_thread_gc.__name__)

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = Path(__file__).resolve().parents[1]
PHONE = surfaces.ShareChoice("io.example.ls", "LocalSend", "ls-1", "iPhone", "smartphone")
ABLAGE = surfaces.ShareChoice("io.example.ablage", "Ablage", "drop", "Ablage")


@pytest.fixture(scope="module")
def app():
    return QGuiApplication.instance() or QGuiApplication([])


@pytest.fixture
def track():
    settler = Settler()
    yield settler
    settler.settle()


def _variant(value):
    return value.toVariant() if hasattr(value, "toVariant") else value


def _adapter(track, choices, sent, notices):
    adapter = track(PluginSurfaces(
        load_cards=lambda: [], share_available=lambda: True,
        load_targets=lambda: surfaces.ShareTargets(list(choices), []),
        send=lambda target, files: sent.append((target.key, files))
        or surfaces.Outcome(True, "Sent."),
    ))
    adapter.dropNotice.connect(lambda text, ok: notices.append((text, ok)))
    return adapter


def test_dropped_qurls_keep_percent_signs_and_go_to_the_only_target(app, tmp_path, track):
    payload = tmp_path / "100% done.txt"
    payload.write_text("x")
    sent: list[tuple] = []
    notices: list[tuple] = []
    adapter = _adapter(track, [PHONE], sent, notices)
    assert uri_text(QUrl.fromLocalFile(str(payload))).endswith("100%25%20done.txt")
    adapter.drop([QUrl.fromLocalFile(str(payload))])
    wait(lambda: ("Sent.", True) in notices)
    assert sent == [(PHONE.key, [str(payload.resolve())])]
    assert notices[0] == ("Sending a file to iPhone…", True)
    assert adapter.file_drop.state()["phase"] == "idle"


def test_several_targets_wait_for_the_pick_and_a_folder_is_skipped(app, tmp_path, track):
    files = [tmp_path / "a.txt", tmp_path / "b.txt"]
    for path in files:
        path.write_text("x")
    folder = tmp_path / "folder"
    folder.mkdir()
    sent: list[tuple] = []
    notices: list[tuple] = []
    adapter = _adapter(track, [PHONE, ABLAGE], sent, notices)
    adapter.drop([QUrl.fromLocalFile(str(path)) for path in [*files, folder]]
                 + ["https://example.org/x"])
    wait(lambda: adapter.file_drop.state()["phase"] == "picking")
    assert sent == []
    assert any("Folders cannot be sent" in text for text, _ok in notices)
    adapter.file_drop.pick(ABLAGE.key)
    wait(lambda: sent != [])
    assert sent == [(ABLAGE.key, [str(path.resolve()) for path in files])]


def test_a_drop_waits_while_send_to_is_sending(app, tmp_path, track):
    payload = tmp_path / "a.txt"
    payload.write_text("x")
    sent: list[tuple] = []
    notices: list[tuple] = []
    adapter = _adapter(track, [PHONE], sent, notices)
    adapter.model._update(busy="send:" + PHONE.key)  # "Send to…" is running
    adapter.drop([QUrl.fromLocalFile(str(payload))])
    assert notices == [("Still busy with the last drop; try again in a moment.", False)]
    assert adapter.file_drop.state()["phase"] == "idle"
    adapter.model._update(busy="")
    adapter.drop([QUrl.fromLocalFile(str(payload))])
    wait(lambda: ("Sent.", True) in notices)
    assert sent == [(PHONE.key, [str(payload.resolve())])]


def test_no_target_leaves_the_explanation_for_the_window(app, tmp_path, track):
    payload = tmp_path / "a.txt"
    payload.write_text("x")
    adapter = _adapter(track, [], [], [])
    adapter.drop([QUrl.fromLocalFile(str(payload))])
    wait(lambda: adapter.file_drop.state()["phase"] == "none")
    assert "Settings > Plugins" in adapter.file_drop.state()["body"]


def test_the_bridge_exposes_the_drop_state_and_slots(app, tmp_path, track):
    from blueferry.qt.controller import BridgeController

    payload = tmp_path / "a.txt"
    payload.write_text("x")
    sent: list[tuple] = []
    notices: list[tuple] = []
    surfaces_adapter = _adapter(track, [PHONE, ABLAGE], sent, notices)
    controller = BridgeController(backend=object(), setup=object(), subscribe=False,
                                  autostart=False, plugin_surfaces=surfaces_adapter)
    heard: list[tuple] = []
    controller.fileDropNotice.connect(lambda text, ok: heard.append((text, ok)))
    state = controller.fileDrop
    assert state["phase"] == "idle"
    assert state["overlayTitle"] == "Drop to send to iPhone"
    controller.dropFiles([QUrl.fromLocalFile(str(payload))])
    wait(lambda: controller.fileDrop["phase"] == "picking")
    controller.cancelDrop()
    assert controller.fileDrop["phase"] == "idle"
    controller.dropFiles([QUrl.fromLocalFile(str(payload))])
    wait(lambda: controller.fileDrop["phase"] == "picking")
    controller.pickDropTarget(PHONE.key)
    wait(lambda: ("Sent.", True) in heard)
    assert sent == [(PHONE.key, [str(payload.resolve())])]


BRIDGE = """
import QtQuick
QtObject {
    property var calls: []
    property var fileDrop: ({phase: "idle", choices: [], selected: "", title: "", body: "",
        overlayTitle: "Drop to send to iPhone", overlaySubtitle: "Sub",
        settingsLabel: "Open Plugin Settings", sendLabel: "Send"})
    function pickDropTarget(key) { calls = calls.concat(["pick " + key]) }
    function cancelDrop() { calls = calls.concat(["cancel"]) }
}
"""

_FIND_JS = """(function find(item, name) {
    const children = (item.children || []).concat(item.contentItem ? [item.contentItem] : []);
    for (let index = 0; index < children.length; ++index) {
        const child = children[index];
        if (!child) continue;
        if (child.objectName === name) return child;
        const found = find(child, name);
        if (found) return found;
    }
    return null;
})"""


def _find(engine, root, name):
    found = engine.evaluate(_FIND_JS).call([engine.newQObject(root), name])
    return found.toQObject() if found.isQObject() else None


WINDOW = """
import QtQuick
Window {
    id: testWindow
    required property var bridge
    property Item layer: dropLayer
    width: 600; height: 400
    FileDropLayer { id: dropLayer; anchors.fill: parent; bridge: testWindow.bridge }
}
"""


def _layer(engine):
    """FileDropLayer in a window: its dialogs are popups and need one."""
    bridge_component = QQmlComponent(engine)
    bridge_component.setData(BRIDGE.encode(), QUrl())
    bridge = bridge_component.create()
    component = QQmlComponent(engine)
    # Resolved next to the QML files, so FileDropLayer is found by name.
    component.setData(WINDOW.encode(), QUrl.fromLocalFile(
        str(ROOT / "src/blueferry/qt/qml/DropTest.qml")))
    window = component.createWithInitialProperties({"bridge": bridge})
    assert window is not None, [error.toString() for error in component.errors()]
    # Keep the components and the window alive as long as the test runs.
    engine._keep = (bridge_component, component, window)
    return bridge, window.property("layer")


def _state(**values):
    base = {"phase": "idle", "choices": [], "selected": "", "title": "", "body": "",
            "overlayTitle": "Drop to send to iPhone", "overlaySubtitle": "Sub",
            "settingsLabel": "Open Plugin Settings", "sendLabel": "Send"}
    return {**base, **values}


def test_the_overlay_follows_the_hover(app):
    engine = QQmlEngine()
    bridge, layer = _layer(engine)
    overlay = _find(engine, layer, "fileDropOverlay")
    assert overlay.property("visible") is False
    layer.setProperty("active", True)
    assert overlay.property("visible") is True
    title = _find(engine, layer, "fileDropOverlayTitle")
    assert title.property("text") == "Drop to send to iPhone"
    layer.deleteLater()
    bridge.deleteLater()


def test_the_picker_preselects_and_a_dismissal_cancels(app):
    engine = QQmlEngine()
    bridge, layer = _layer(engine)
    choices = [{"key": PHONE.key, "label": "iPhone", "icon": "smartphone"},
               {"key": ABLAGE.key, "label": "Ablage", "icon": "document-send"}]
    bridge.setProperty("fileDrop", _state(phase="picking", choices=choices,
                                          selected=ABLAGE.key, title="Send To",
                                          body="Where should the file go?"))
    picker = _picker(engine, layer, "fileDropPicker")
    assert picker.property("opened") or picker.property("visible")
    assert picker.property("selectedKey") == ABLAGE.key
    # Send keeps the selection: a pick refused while "Send to…" is busy
    # leaves the picker open with the same target.
    engine.evaluate("(function (d) { d.customFooterActions[0].trigger() })").call(
        [engine.newQObject(picker)])
    assert _variant(bridge.property("calls")) == ["pick " + ABLAGE.key]
    assert picker.property("selectedKey") == ABLAGE.key
    bridge.setProperty("calls", [])
    picker.close()
    wait(lambda: "cancel" in json.dumps(_variant(bridge.property("calls"))))
    assert _variant(bridge.property("calls")) == ["cancel"]
    layer.deleteLater()
    bridge.deleteLater()


def test_no_target_offers_the_plugin_settings(app):
    engine = QQmlEngine()
    bridge, layer = _layer(engine)
    engine.evaluate("""(function (layer, bridge) {
        layer.pluginSettingsRequested.connect(() => { bridge.calls = bridge.calls.concat(["settings"]) })
    })""").call([engine.newQObject(layer), engine.newQObject(bridge)])
    bridge.setProperty("fileDrop", _state(phase="none", title="No Way",
                                          body="Install LocalSend."))
    dialog = _picker(engine, layer, "fileDropNoTarget")
    assert dialog.property("opened") or dialog.property("visible")
    engine.evaluate("(function (d) { d.customFooterActions[0].trigger() })").call(
        [engine.newQObject(dialog)])
    assert _variant(bridge.property("calls")) == ["cancel", "settings"]
    layer.deleteLater()
    bridge.deleteLater()


def _picker(engine, layer, name):
    """Dialogs are non-visual children: look them up by objectName."""
    from PySide6.QtCore import QObject

    found = layer.findChild(QObject, name)
    assert found is not None, name
    return found
