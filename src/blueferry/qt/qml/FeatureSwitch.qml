import QtQuick

// One local.env switch (Messages1.GetFeatures/SetFeature). The daemon
// stores the choice in settings.json; it applies after a service restart.
// Older daemons without SetFeature show the switch disabled with the
// local.env variable instead.
SubtitleSwitch {
    id: row
    required property var bridge
    required property string feature
    property string variable: ""
    property string description: ""

    readonly property var features: row.bridge.features || ({})
    readonly property var info: (row.features.items || ({}))[row.feature] || null
    readonly property bool usable: row.features.available === true && row.info !== null

    function note() {
        if (!row.usable)
            return qsTr("Set %1=true or false in ~/.config/blueferry/local.env and restart the service.")
                .arg(row.variable)
        if (row.info.source === "environment")
            return qsTr("Set by the service's environment (%1).").arg(row.variable)
        if (row.info.restart_required === true)
            return qsTr("Applies after the service restarts.")
        return ""
    }

    objectName: "feature_" + row.feature
    horizontalPadding: 0
    checked: row.usable && row.info.value === true
    enabled: row.usable && row.info.source !== "environment"
        && (row.bridge.status || ({})).daemon === true && !row.bridge.busy
    subtitle: [row.description, row.note()].filter(part => part !== "").join("\n")
    onToggled: {
        row.bridge.setFeature(row.feature, checked)
        checked = Qt.binding(function() { return row.usable && row.info.value === true })
    }
}
