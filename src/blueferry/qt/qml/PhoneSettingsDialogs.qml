pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import org.kde.kirigami as Kirigami

// Owned by the window so pairing prompts survive closing the settings page.
Item {
    id: root
    required property var bridge

    function requestStoragePolicy(policy) {
        if (policy === bridge.status.storage_policy) return
        storageChangeDialog.requestedPolicy = policy
        storageChangeDialog.open()
    }

    function requestBluetoothRestart() {
        restartBluetoothDialog.open()
    }

    function requestForget(mac) {
        forgetDialog.mac = mac
        forgetDialog.open()
    }

    function showPairingIssue() {
        pairingIssueDialog.open()
    }

    function requestPairing(mac, paired, compatibilityMode, explicitPairing) {
        if (!paired && bridge.targetSaved) {
            // Keep the target the user was asked to replace, even if setup
            // refreshes while the confirmation is open.
            replaceTargetDialog.previousMac = bridge.configuredMac
            replaceTargetDialog.mac = mac
            replaceTargetDialog.compatibilityMode = compatibilityMode
            replaceTargetDialog.explicitPairing = explicitPairing
            replaceTargetDialog.open()
        } else {
            bridge.completePairing(mac, compatibilityMode, explicitPairing)
        }
    }

    Connections {
        target: root.bridge
        function onPairingConfirmationRequested(passkey) {
            pairingConfirmationDialog.passkey = passkey
            pairingConfirmationDialog.open()
        }
    }

    Kirigami.PromptDialog {
        id: storageChangeDialog
        objectName: "storageChangeDialog"
        property string requestedPolicy: ""
        title: requestedPolicy === "none"
            ? qsTr("Stop Retaining Local Data?")
            : requestedPolicy === "plaintext"
                ? qsTr("Store Local Data Without Encryption?")
                : qsTr("Use Encrypted Local Storage?")
        subtitle: requestedPolicy === "none"
            ? qsTr("This clears message history and cached contacts, then removes BlueFerry's storage key. Nothing on the iPhone is deleted.")
            : requestedPolicy === "plaintext"
                ? qsTr("This clears existing local message history and cached contacts. New local data will be stored unencrypted and can be read by anyone with access to your files. Nothing on the iPhone is deleted.")
                : qsTr("Changing storage protection clears existing local message history and cached contacts. New local data will be encrypted with your desktop keyring. Nothing on the iPhone is deleted.")
        dialogType: Kirigami.PromptDialog.Warning
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: storageChangeDialog.requestedPolicy === "none"
                ? qsTr("Clear and Stop Retaining")
                : storageChangeDialog.requestedPolicy === "plaintext"
                    ? qsTr("Clear and Store Unencrypted")
                    : qsTr("Clear and Use Encryption")
            icon.name: "edit-delete"
            onTriggered: {
                root.bridge.setStoragePolicy(storageChangeDialog.requestedPolicy)
                storageChangeDialog.close()
            }
        }]
        onClosed: root.bridge.refresh()
    }

    Kirigami.PromptDialog {
        id: restartBluetoothDialog
        objectName: "restartBluetoothDialog"
        title: qsTr("Restart Bluetooth?")
        subtitle: qsTr("Bluetooth devices will disconnect briefly. Polkit may request authentication.")
        dialogType: Kirigami.PromptDialog.Warning
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: qsTr("Restart Bluetooth")
            icon.name: "network-bluetooth"
            onTriggered: {
                root.bridge.activateBluetooth()
                restartBluetoothDialog.close()
            }
        }]
    }

    Kirigami.PromptDialog {
        id: pairingConfirmationDialog
        objectName: "pairingConfirmationDialog"
        property string passkey: ""
        title: passkey === "" ? qsTr("Approve Bluetooth Pairing?")
            : qsTr("Do the Bluetooth Codes Match?")
        subtitle: passkey === ""
            ? qsTr("Approve only if you started this pairing from BlueFerry.")
            : qsTr("Confirm that %1 is shown on both this computer and the iPhone.").arg(passkey)
        dialogType: Kirigami.PromptDialog.Information
        standardButtons: Kirigami.Dialog.NoButton
        closePolicy: Controls.Popup.NoAutoClose
        customFooterActions: [
            Kirigami.Action {
                text: qsTr("Cancel Pairing")
                onTriggered: {
                    root.bridge.answerPairingConfirmation(false)
                    pairingConfirmationDialog.close()
                }
            },
            Kirigami.Action {
                text: pairingConfirmationDialog.passkey === ""
                    ? qsTr("Approve Pairing") : qsTr("Codes Match")
                icon.name: "dialog-ok-apply"
                onTriggered: {
                    root.bridge.answerPairingConfirmation(true)
                    pairingConfirmationDialog.close()
                }
            }
        ]
    }

    Kirigami.PromptDialog {
        id: forgetDialog
        objectName: "forgetDialog"
        property string mac: ""
        title: qsTr("Unpair This iPhone?")
        subtitle: qsTr("Also forget this computer in the iPhone Bluetooth settings before pairing again.")
        dialogType: Kirigami.PromptDialog.Warning
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: qsTr("Unpair")
            icon.name: "edit-delete-remove"
            onTriggered: {
                root.bridge.forgetDevice(forgetDialog.mac)
                forgetDialog.close()
            }
        }]
    }

    Kirigami.PromptDialog {
        id: pairingIssueDialog
        objectName: "pairingIssueDialog"
        title: qsTr("Report Pairing Issue")
        subtitle: qsTr("A pairing report was saved at %1. Attach that file to a GitHub issue and include the iPhone model and iOS version.").arg(root.bridge.pairingIssueReport)
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: qsTr("Open GitHub")
            icon.name: "internet-web-browser"
            onTriggered: {
                root.bridge.filePairingIssue()
                pairingIssueDialog.close()
            }
        }]
    }

    Kirigami.PromptDialog {
        id: replaceTargetDialog
        property string previousMac: ""
        objectName: "replaceTargetDialog"
        property string mac: ""
        property bool compatibilityMode: false
        property bool explicitPairing: false
        title: qsTr("Replace the Saved iPhone?")
        subtitle: qsTr("Pairing this iPhone will remove BlueFerry's saved phone and its local Bluetooth bond. Before continuing, also forget this computer in the old iPhone's Bluetooth settings.")
        dialogType: Kirigami.PromptDialog.Warning
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [Kirigami.Action {
            text: qsTr("Replace and Pair")
            icon.name: "edit-delete-remove"
            onTriggered: {
                root.bridge.replaceAndPair(
                    replaceTargetDialog.previousMac,
                    replaceTargetDialog.mac,
                    replaceTargetDialog.compatibilityMode,
                    replaceTargetDialog.explicitPairing
                )
                replaceTargetDialog.close()
            }
        }]
    }
}
