pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts

// One local.env switch (Messages1.GetFeatures/SetFeature), like the Qt
// FeatureSwitch. `features` is the bridge's features view: `switches`
// rows with name, variable, usable, active, editable and note (shared
// features_view wording). Older daemons report
// no switches; the row is then disabled and its note names the local.env
// variable. `pending` holds a choice that is being saved, so the box does
// not jump back before the bridge answers.
ColumnLayout {
  id: control
  required property var ferryTheme
  property var features: ({})
  property string feature: ""
  property string text: ""
  property string description: ""
  property var pending: ({})
  property bool allowed: true
  readonly property var info: control.lookup()
  signal toggledTo(string feature, bool enabled)

  function lookup() {
    const value = control.features.switches
    const rows = value && value.length !== undefined ? Array.prototype.slice.call(value) : []
    return rows.find(row => row && row.name === control.feature) || ({})
  }

  spacing: control.ferryTheme.scaled(2)
  Layout.fillWidth: true

  FerryCheckBox {
    id: box
    objectName: "feature_" + control.feature
    ferryTheme: control.ferryTheme
    Layout.fillWidth: true
    text: control.text || String(control.info.variable || control.feature)
    checked: control.pending[control.feature] !== undefined
      ? control.pending[control.feature] === true : control.info.active === true
    enabled: control.allowed && control.info.editable === true
      && control.pending[control.feature] === undefined
    Accessible.description: note.text
    onClicked: {
      const choice = checked
      checked = Qt.binding(() => control.pending[control.feature] !== undefined
        ? control.pending[control.feature] === true : control.info.active === true)
      control.toggledTo(control.feature, choice)
    }
  }
  FerryLabel {
    id: note
    objectName: "featureNote_" + control.feature
    ferryTheme: control.ferryTheme
    Layout.fillWidth: true
    visible: text !== ""
    wrapMode: Text.Wrap
    text: [control.description, String(control.info.note || "")]
      .filter(part => part !== "").join("\n")
  }
}
