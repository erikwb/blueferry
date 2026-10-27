import QtQuick

// One local.env switch (Messages1.GetFeatures/SetFeature). The daemon
// stores the choice in settings.json; it applies after a service restart.
// Older daemons without SetFeature show the switch disabled with the
// local.env variable instead. The row's state and note come from
// features_view.view() (bridge.features.switches), shared with GTK.
SubtitleSwitch {
    id: row
    required property var bridge
    required property string feature
    property string description: ""

    readonly property var info: ((row.bridge.features || ({})).switches || ({}))[row.feature]
        || ({})

    objectName: "feature_" + row.feature
    horizontalPadding: 0
    checked: row.info.active === true
    enabled: row.info.editable === true
        && (row.bridge.status || ({})).daemon === true && !row.bridge.busy
    subtitle: [row.description, row.info.note || ""].filter(part => part !== "").join("\n")
    onToggled: {
        row.bridge.setFeature(row.feature, checked)
        checked = Qt.binding(function() { return row.info.active === true })
    }
}
