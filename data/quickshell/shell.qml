pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io

ShellRoot {
  id: root
  property var threads: []
  property var contactResults: []
  property string selectedThreadKey: ""
  property string pendingThreadKey: ""
  property string pendingMessageHandle: ""
  property var confirmedGroupSignatures: ({})
  property var groupParticipantsThread: null
  property var rosterChangedThread: null
  property string errorText: ""
  property bool phoneSettingsVisible: false
  property var backendStatus: ({})
  property string notificationPolicy: "messages"
  property bool contactsOnlyNotifications: false
  property string storagePolicy: "encrypted"
  property string storageState: "locked"
  property string storageDetail: ""
  property bool storageUnlockAttempted: false
  property string statusErrorText: ""
  property bool statusBusy: false
  property bool threadsBusy: false
  property bool contactsBusy: false
  property bool sendBusy: false
  property bool newMessageSendBusy: false
  property bool groupParticipantsBusy: false
  property bool deleteThreadsBusy: false
  property bool notificationPolicyBusy: false
  property bool contactsOnlyNotificationsBusy: false
  property bool storagePolicyBusy: false
  property bool storageUnlockBusy: false

  ConversationLogic { id: conversationLogic }

  Theme { id: theme }
  OnboardingState {
    id: onboarding
    notificationsSupported: setupController.notificationsSupported
                            && setupController.ancsEnabled
                            && !setupController.compatibilityModeOverride
    bluezActive: setupController.bluezActive
    configured: setupController.configured
    backendStatus: root.backendStatus
  }

  Connections {
    target: Quickshell
    function onLastWindowClosed() { Qt.quit() }
  }

  function reload() {
    if (!setupController.configured) return
    if (!threadsBusy) {
      threadsBusy = true
      backendBridge.request("threads", {limit: 200})
    }
    if (!statusBusy) {
      statusBusy = true
      backendBridge.request("status", {})
    }
  }

  function searchContacts(query) {
    contactResults = []
    const selected = query.trim()
    contactsBusy = selected !== ""
    if (contactsBusy) backendBridge.requestLatest("contacts", {query: selected})
    else backendBridge.cancelLatest("contacts")
  }

  function maybeUnlockStorage() {
    if (setupController.configured && storagePolicy === "encrypted" && storageState !== "ready"
        && !storageUnlockAttempted && !storageUnlockBusy) {
      storageUnlockAttempted = true
      storageUnlockBusy = true
      backendBridge.request("unlock_storage", {})
    }
  }

  function threadByKey(key) {
    return conversationLogic.threadByKey(threads, key)
  }

  function selectedThread() {
    return threadByKey(selectedThreadKey)
  }

  function setThreadStarred(key, starred) {
    if (!key) return
    backendBridge.request("set_thread_starred", {
      thread_key: String(key),
      starred: starred === true
    })
  }

  function threadIsUnread(thread) {
    return conversationLogic.threadIsUnread(thread)
  }

  function markSelectedThreadRead() {
    if (!window.visible || !applicationSurface.Window.active || phoneSettingsVisible) return
    var thread = selectedThread()
    if (!thread || !threadIsUnread(thread)) return
    backendBridge.request("mark_thread_read", {thread_key: String(thread.key)})
  }

  onSelectedThreadKeyChanged: root.markSelectedThreadRead()
  onPhoneSettingsVisibleChanged: root.markSelectedThreadRead()

  function selectMessage(handle) {
    const thread = conversationLogic.threadForMessage(threads, handle)
    if (!thread) return false
    selectedThreadKey = thread.key
    pendingMessageHandle = ""
    phoneSettingsVisible = false
    return true
  }

  function presentWindow() {
    phoneSettingsVisible = false
    window.visible = true
  }

  function openThread(key) {
    pendingThreadKey = key
    selectedThreadKey = key
    pendingMessageHandle = ""
    presentWindow()
  }

  function openMessage(handle) {
    pendingMessageHandle = handle
    presentWindow()
    if (!selectMessage(handle)) reload()
  }

  function groupSignature(thread) {
    return conversationLogic.groupSignature(thread)
  }

  function groupIsConfirmed(thread) {
    if (!thread || !thread.is_group) return true
    if (thread.group_confirmed === true) return true
    var signature = root.groupSignature(thread)
    return signature !== "" &&
      (root.confirmedGroupSignatures[thread.key] || "") === signature
  }

  function setGroupConfirmed(thread, confirmed) {
    if (!thread || !thread.is_group) return
    var next = Object.assign({}, root.confirmedGroupSignatures)
    if (confirmed) {
      var signature = root.groupSignature(thread)
      if (signature === "") return
      next[thread.key] = signature
    } else {
      delete next[thread.key]
    }
    root.confirmedGroupSignatures = next
  }

  function participantLines(value) {
    return conversationLogic.participantLines(value)
  }

  function warnAboutRosterChanges() {
    const thread = conversationLogic.nextRosterWarning(threads)
    if (!thread) return
    rosterChangedThread = thread
    rosterChangedPopup.open()
  }

  function markStatusUnavailable(message) {
    backendStatus = ({})
    statusErrorText = message
  }

  SetupController {
    id: setupController
    onExecuteRequested: (id, kind, command, interactive) => setupTransport.execute(id, kind, command, interactive)
    onCancelRequested: id => setupTransport.cancel(id)
    onInputRequested: (id, text) => setupTransport.write(id, text)
    onConfigurationUpdated: configured => {
      if (!configured) root.phoneSettingsVisible = true
      else root.reload()
    }
    onReloadRequested: root.reload()
    onHistoryReset: {
      root.threads = []
      root.selectedThreadKey = ""
      root.backendStatus = ({})
      root.phoneSettingsVisible = true
    }
    onConfirmationRequested: root.phoneSettingsVisible = true
    onReplacementRequested: replaceTargetPopup.open()
    onIssueUrlReady: url => Qt.openUrlExternally(url)
  }
  SetupTransport {
    id: setupTransport
    onLineReceived: (id, kind, line) => setupController.receiveLine(id, kind, line)
    onFinished: (id, kind, code, output, diagnostic) => setupController.finish(id, kind, code, output, diagnostic)
  }

  BackendBridge { id: backendBridge }

  IpcHandler {
    target: "blueferry"
    function open(): void { root.presentWindow() }
    function openThread(key: string): void { root.openThread(key) }
    function openMessage(handle: string): void { root.openMessage(handle) }
  }

  Connections {
    target: backendBridge

    function onResponse(method, requestId, result) {
      if (method === "status") {
        root.statusBusy = false
        if (typeof result !== "object" || result === null) {
          root.markStatusUnavailable("BlueFerry backend returned invalid status data")
          return
        }
        root.backendStatus = result
        var policy = result.notification_policy || "messages"
        root.notificationPolicy = ["all", "messages", "none"].indexOf(policy) >= 0
          ? policy : "messages"
        root.contactsOnlyNotifications =
          result.contacts_only_notifications === true
        var storagePolicy = result.storage_policy || "encrypted"
        root.storagePolicy = ["encrypted", "plaintext", "none"].indexOf(storagePolicy) >= 0
          ? storagePolicy : "encrypted"
        root.storageState = result.storage_state || "locked"
        root.storageDetail = result.storage_detail || "Storage status unavailable"
        root.statusErrorText = ""
        root.maybeUnlockStorage()
      } else if (method === "threads") {
        root.threadsBusy = false
        root.threads = Array.isArray(result) ? result : []
        if (root.pendingThreadKey !== "") {
          root.selectedThreadKey = root.pendingThreadKey
          if (root.selectedThread() !== null) root.pendingThreadKey = ""
        }
        const selected = root.selectedThread()
        root.selectedThreadKey = selected ? selected.key : ""
        if (root.pendingMessageHandle !== "")
          root.selectMessage(root.pendingMessageHandle)
        root.warnAboutRosterChanges()
        root.markSelectedThreadRead()
        root.errorText = ""
      } else if (method === "contacts") {
        root.contactsBusy = false
        root.contactResults = Array.isArray(result) ? result : []
      } else if (method === "send_to_thread") {
        root.sendBusy = false
        composer.text = ""
        messageList.stickToBottom = true
        root.reload()
      } else if (method === "send") {
        root.newMessageSendBusy = false
        newMessagePopup.close()
        newRecipient.text = ""
        newMessageBody.text = ""
        root.reload()
      } else if (method === "set_group_participants") {
        root.groupParticipantsBusy = false
        if (root.groupParticipantsThread)
          root.setGroupConfirmed(root.groupParticipantsThread, false)
        groupParticipantsPopup.close()
        root.reload()
      } else if (method === "mark_thread_read") {
        // HistoryChanged reloads the thread list.
      } else if (method === "set_thread_starred") {
        // HistoryChanged reloads the thread list.
      } else if (method === "delete_threads") {
        root.deleteThreadsBusy = false
        root.reload()
      } else if (method === "set_notification_policy") {
        root.notificationPolicyBusy = false
        root.notificationPolicy = String(result)
        root.reload()
      } else if (method === "set_contacts_only_notifications") {
        root.contactsOnlyNotificationsBusy = false
        root.contactsOnlyNotifications = result === true
        root.reload()
      } else if (method === "set_storage_policy") {
        root.storagePolicyBusy = false
        if (typeof result === "object" && result !== null) {
          root.storagePolicy = result.storage_policy || root.storagePolicy
          root.storageState = result.storage_state || root.storageState
          root.storageDetail = result.storage_detail || root.storageDetail
        }
        root.reload()
      } else if (method === "unlock_storage") {
        root.storageUnlockBusy = false
        if (typeof result === "object" && result !== null) {
          root.storageState = result.storage_state || root.storageState
          root.storageDetail = result.storage_detail || root.storageDetail
        }
        root.reload()
      }
    }

    function onFailure(method, requestId, message) {
      if (method === "status") {
        root.statusBusy = false
        root.markStatusUnavailable(message || "BlueFerry backend is unavailable")
      } else if (method === "threads") {
        root.threadsBusy = false
        root.errorText = message || "BlueFerry daemon is unavailable"
      } else if (method === "contacts") {
        root.contactsBusy = false
        root.errorText = message || "Contact search failed"
      } else if (method === "send_to_thread") {
        root.sendBusy = false
        root.errorText = message
      } else if (method === "send") {
        root.newMessageSendBusy = false
        root.errorText = message
      } else if (method === "set_group_participants") {
        root.groupParticipantsBusy = false
        root.errorText = message
      } else if (method === "mark_thread_read") {
        // Keep the conversation open even if MAP write-back fails.
      } else if (method === "set_thread_starred") {
        root.errorText = message || "Could not update starred conversations"
      } else if (method === "delete_threads") {
        root.deleteThreadsBusy = false
        root.errorText = message || "Could not delete local conversations"
      } else if (method === "set_notification_policy") {
        root.notificationPolicyBusy = false
        root.errorText = message || "Could not save notification preference"
      } else if (method === "set_contacts_only_notifications") {
        root.contactsOnlyNotificationsBusy = false
        root.errorText = message || "Could not save notification preference"
        root.reload()
      } else if (method === "set_storage_policy") {
        root.storagePolicyBusy = false
        root.errorText = message
      } else if (method === "unlock_storage") {
        root.storageUnlockBusy = false
        root.errorText = message
      } else {
        root.statusBusy = false
        root.threadsBusy = false
        root.contactsBusy = false
        root.sendBusy = false
        root.newMessageSendBusy = false
        root.groupParticipantsBusy = false
        root.deleteThreadsBusy = false
        root.notificationPolicyBusy = false
        root.contactsOnlyNotificationsBusy = false
        root.storagePolicyBusy = false
        root.storageUnlockBusy = false
        root.errorText = message
      }
    }

    function onEventReceived(name, data) {
      if (name === "open-message") root.openMessage(String(data || ""))
      else if (name === "history-changed" || name === "status-changed") root.reload()
    }
  }

  Timer {
    interval: 3000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: {
      root.reload()
      setupController.refreshConfiguration()
    }
  }

  Component.onCompleted: {
    setupController.start()
    var messageHandle = String(Quickshell.env("BLUEFERRY_OPEN_MESSAGE_HANDLE") || "")
    var threadKey = String(Quickshell.env("BLUEFERRY_OPEN_THREAD_KEY") || "")
    if (messageHandle !== "") root.openMessage(messageHandle)
    else if (threadKey !== "") root.openThread(threadKey)
  }

  FloatingWindow {
    id: window
    title: "BlueFerry"
    implicitWidth: 900
    implicitHeight: 660
    color: theme.windowSurface

    Pane {
      id: applicationSurface
      Window.onActiveChanged: root.markSelectedThreadRead()
      anchors.fill: parent
      padding: 0
      font.family: theme.fontFamily
      font.pixelSize: theme.baseFontSize
      palette.window: theme.windowSurface
      palette.windowText: theme.windowText
      palette.base: theme.windowSurface
      palette.alternateBase: theme.alternate
      palette.text: theme.windowText
      palette.button: theme.control
      palette.buttonText: theme.windowText
      palette.highlight: theme.accent
      palette.highlightedText: theme.highlightedText
      palette.placeholderText: theme.muted
      palette.mid: theme.surfaceBorder
      palette.toolTipBase: theme.surface
      palette.toolTipText: theme.surfaceText

      background: Rectangle {
        radius: theme.panelRadius
        color: theme.windowSurface
      }

      ColumnLayout {
        anchors.fill: parent
        anchors.margins: theme.panelPadding
        spacing: theme.scaled(8)

        RowLayout {
          Layout.fillWidth: true
          spacing: theme.scaled(8)
          FerryLabel {
            ferryTheme: theme
            text: "BLUEFERRY"
            font.bold: true
            font.letterSpacing: 1
            color: theme.accent
          }
          Item { Layout.fillWidth: true }
          Rectangle {
            implicitWidth: theme.scaled(5)
            implicitHeight: implicitWidth
            color: root.backendStatus.map ? theme.accent : theme.muted
          }
          FerryLabel {
            ferryTheme: theme
            text: root.backendStatus.map ? "IPHONE CONNECTED"
              : setupController.configured ? "CONNECTING" : "SETUP"
            color: theme.muted
            font.pixelSize: theme.captionSize
          }
        }

        FerryLabel {
          ferryTheme: theme
          visible: root.errorText !== "" || root.statusErrorText !== ""
          text: root.errorText !== "" ? root.errorText : root.statusErrorText
          textFormat: Text.PlainText
          color: theme.urgent
          wrapMode: Text.Wrap
          Layout.fillWidth: true
          leftPadding: theme.scaled(14)
          rightPadding: theme.scaled(14)
          topPadding: theme.scaled(10)
          bottomPadding: theme.scaled(10)

          background: Rectangle {
            color: Qt.rgba(theme.urgent.r, theme.urgent.g, theme.urgent.b, 0.12)
            border.color: theme.urgent
            radius: theme.controlRadius
          }
        }

        Rectangle {
          Layout.fillWidth: true
          implicitHeight: mapRefusedLabel.implicitHeight + theme.scaled(16)
          visible: !root.phoneSettingsVisible && onboarding.mapConnectionRefused()
          color: Qt.rgba(theme.warning.r, theme.warning.g, theme.warning.b, 0.14)
          border.color: theme.warning
          radius: theme.controlRadius

          FerryLabel {
            ferryTheme: theme
            id: mapRefusedLabel
            anchors.fill: parent
            anchors.margins: theme.scaled(8)
            text: "iPhone is refusing message connections; is it connected to another computer?"
            textFormat: Text.PlainText
            color: theme.windowText
            font.bold: true
            wrapMode: Text.Wrap
          }
        }

        SplitView {
          visible: !root.phoneSettingsVisible
          Layout.fillWidth: true
          Layout.fillHeight: true
          handle: Rectangle {
            implicitWidth: theme.scaled(10)
            color: "transparent"
            Rectangle {
              anchors.centerIn: parent
              width: 1
              height: parent.height - theme.scaled(24)
              color: theme.divider
            }
          }

          Rectangle {
            SplitView.preferredWidth: theme.scaled(250)
            SplitView.minimumWidth: theme.scaled(190)
            color: theme.windowSurface
            radius: theme.panelRadius
            border.color: theme.divider

            ColumnLayout {
              anchors.fill: parent
              anchors.margins: theme.scaled(10)
              spacing: theme.scaled(6)

              RowLayout {
                Layout.fillWidth: true
                FerryLabel {
                  ferryTheme: theme
                  Layout.fillWidth: true
                  text: "CONVERSATIONS"
                  color: theme.muted
                  font.family: theme.fontFamily
                  font.pixelSize: theme.captionSize
                  font.bold: true
                  font.letterSpacing: 1
                  leftPadding: theme.scaled(4)
                }
                FerryButton {
                  ferryTheme: theme
                  text: "+"
                  implicitWidth: implicitHeight
                  highlighted: true
                  Accessible.name: "New message"
                  ToolTip.visible: hovered
                  ToolTip.text: "New message"
                  onClicked: newMessagePopup.open()
                }
              }

              ListView {
                id: threadList
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: theme.scaled(5)
                model: root.threads
                delegate: ItemDelegate {
                  id: threadDelegate
                  required property var modelData
                  width: threadList.width
                  implicitHeight: theme.scaled(54)
                  highlighted: modelData.key === root.selectedThreadKey
                  leftPadding: theme.scaled(8)
                  rightPadding: theme.scaled(8)
                  onClicked: root.selectedThreadKey = modelData.key
                  contentItem: Row {
                    clip: true
                    spacing: theme.scaled(10)

                    Rectangle {
                      width: theme.scaled(26)
                      height: width
                      anchors.verticalCenter: parent.verticalCenter
                      radius: theme.controlRadius
                      color: theme.control
                      border.color: theme.divider
                      Text {
                        anchors.centerIn: parent
                        text: threadDelegate.modelData.is_group ? "#"
                          : String(threadDelegate.modelData.name || "?").charAt(0).toUpperCase()
                        textFormat: Text.PlainText
                        color: threadDelegate.highlighted ? theme.accent : theme.muted
                        font.family: theme.fontFamily
                        font.pixelSize: theme.baseFontSize
                        font.bold: true
                      }
                    }

                    Column {
                      id: threadContent
                      width: parent.width - x - theme.scaled(28)
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: theme.scaled(3)
                      Text {
                        width: parent.width
                        text: threadDelegate.modelData.name
                        textFormat: Text.PlainText
                        color: theme.windowText
                        font.family: theme.fontFamily
                        font.pixelSize: theme.baseFontSize
                        font.bold: root.threadIsUnread(threadDelegate.modelData)
                        wrapMode: Text.NoWrap
                        maximumLineCount: 1
                        elide: Text.ElideRight
                        clip: true
                      }
                      QuickshellThreadPreview {
                        width: parent.width
                        thread: threadDelegate.modelData
                        ferryTheme: theme
                      }
                    }
                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      text: threadDelegate.modelData.starred ? "★" : "☆"
                      textFormat: Text.PlainText
                      color: threadDelegate.modelData.starred
                        ? theme.accent : theme.muted
                      font.family: theme.fontFamily
                      font.pixelSize: theme.baseFontSize
                      Accessible.name: threadDelegate.modelData.starred
                        ? "Unstar conversation" : "Star conversation"
                      MouseArea {
                        anchors.fill: parent
                        anchors.margins: -theme.scaled(6)
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                          root.setThreadStarred(
                            threadDelegate.modelData.key,
                            !threadDelegate.modelData.starred
                          )
                        }
                      }
                    }
                  }
                  background: Rectangle {
                    color: threadDelegate.highlighted ? theme.selectedSurface
                      : threadDelegate.hovered ? theme.hoverSurface : "transparent"
                    border.color: "transparent"
                    Rectangle {
                      width: 2
                      height: parent.height
                      color: theme.accent
                      visible: threadDelegate.highlighted
                    }
                    radius: theme.controlRadius
                  }
                  TapHandler {
                    acceptedButtons: Qt.RightButton
                    onTapped: eventPoint => {
                      threadContextMenu.threadKey = threadDelegate.modelData.key
                      threadContextMenu.popup(
                        threadDelegate,
                        eventPoint.position.x,
                        eventPoint.position.y
                      )
                    }
                  }
                }

                Column {
                  anchors.centerIn: parent
                  visible: threadList.count === 0
                  spacing: theme.scaled(6)
                  FerryLabel {
                    ferryTheme: theme
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "NO CONVERSATIONS"
                    color: theme.windowText
                    font.bold: true
                    font.letterSpacing: 1
                  }
                  FerryLabel {
                    ferryTheme: theme
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "New messages will dock here."
                    color: theme.muted
                    font.pixelSize: theme.captionSize
                  }
                }
              }

              RowLayout {
                Layout.fillWidth: true
                FerryButton {
                  ferryTheme: theme
                  text: "SETTINGS"
                  labelSize: theme.captionSize
                  labelBold: true
                  labelLetterSpacing: 1
                  bare: true
                  subtle: true
                  leftPadding: theme.scaled(4)
                  Accessible.name: "iPhone settings"
                  ToolTip.visible: hovered
                  ToolTip.text: "iPhone settings"
                  onClicked: root.phoneSettingsVisible = true
                }
                Item { Layout.fillWidth: true }
              }
            }
          }

          Rectangle {
            id: conversationPane
            SplitView.fillWidth: true
            SplitView.minimumWidth: theme.scaled(320)
            property var thread: root.selectedThread()
            color: theme.windowSurface
            radius: theme.panelRadius
            border.color: theme.divider

            ColumnLayout {
              anchors.fill: parent
              anchors.margins: theme.scaled(12)
              spacing: theme.scaled(10)

              RowLayout {
                Layout.fillWidth: true
                FerryLabel {
                  ferryTheme: theme
                  Layout.fillWidth: true
                  text: conversationPane.thread
                    ? conversationPane.thread.name : "SELECT A CONVERSATION"
                  textFormat: Text.PlainText
                  color: conversationPane.thread ? theme.windowText : theme.muted
                  font.bold: true
                  font.pixelSize: theme.baseFontSize
                  elide: Text.ElideRight
                }
                FerryButton {
                  ferryTheme: theme
                  visible: conversationPane.thread
                    && conversationPane.thread.group_origin === "named"
                  text: "Members"
                  bare: true
                  onClicked: {
                    root.groupParticipantsThread = conversationPane.thread
                    groupParticipantsPopup.open()
                  }
                }
                FerryLabel {
                  ferryTheme: theme
                  visible: conversationPane.thread !== null
                  text: conversationPane.thread && conversationPane.thread.is_group
                    ? "GROUP" : "DIRECT"
                  color: theme.muted
                  font.pixelSize: theme.captionSize
                  font.bold: true
                  font.letterSpacing: 1
                }
              }

              FerryLabel {
                ferryTheme: theme
                Layout.fillWidth: true
                visible: conversationPane.thread !== null && !conversationPane.thread.is_group
                text: visible ? "Reply to: " + conversationPane.thread.recipients.join(", ") : ""
                textFormat: Text.PlainText
                color: theme.muted
                elide: Text.ElideRight
              }

              Rectangle {
                Layout.fillWidth: true
                implicitHeight: 1
                color: theme.divider
              }

              Label {
                Layout.fillWidth: true
                visible: conversationPane.thread !== null
                  && conversationPane.thread.messages_truncated === true
                text: "Showing recent messages. Older messages remain in local history."
                wrapMode: Text.Wrap
                color: theme.windowText
              }

              ListView {
                id: messageList
                property bool stickToBottom: true
                property string threadKey: conversationPane.thread
                  ? conversationPane.thread.key : ""

                function scrollToBottom() {
                  if (stickToBottom && count > 0) positionViewAtEnd()
                }

                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                spacing: theme.scaled(8)
                model: conversationPane.thread ? conversationPane.thread.messages : []
                onThreadKeyChanged: {
                  stickToBottom = true
                  Qt.callLater(scrollToBottom)
                }
                onCountChanged: Qt.callLater(scrollToBottom)
                onContentHeightChanged: Qt.callLater(scrollToBottom)
                onMovementStarted: stickToBottom = false
                onMovementEnded: stickToBottom = atYEnd
                delegate: Item {
                  id: messageRow
                  required property var modelData
                  width: messageList.width
                  height: bubble.height > 0
                    ? bubble.height + theme.scaled(3) : 0

                  QuickshellMessageBubble {
                    id: bubble
                    message: messageRow.modelData
                    availableWidth: messageList.width
                    availableHeight: messageList.height
                    showSender: conversationPane.thread
                      && conversationPane.thread.is_group
                    ferryTheme: theme
                    anchors.right: messageRow.modelData.outgoing ? parent.right : undefined
                    anchors.left: messageRow.modelData.outgoing ? undefined : parent.left
                  }
                }

                Column {
                  anchors.centerIn: parent
                  visible: conversationPane.thread === null
                  spacing: theme.scaled(8)
                  FerryLabel {
                    ferryTheme: theme
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "✦"
                    color: theme.muted
                    font.pixelSize: theme.displaySize
                  }
                  FerryLabel {
                    ferryTheme: theme
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "PICK A THREAD"
                    color: theme.windowText
                    font.bold: true
                    font.letterSpacing: 1
                  }
                  FerryLabel {
                    ferryTheme: theme
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Your messages stay local to this machine."
                    color: theme.muted
                    font.pixelSize: theme.captionSize
                  }
                }
              }

              Rectangle {
                Layout.fillWidth: true
                implicitHeight: groupRosterPrompt.implicitHeight + theme.scaled(20)
                visible: conversationPane.thread
                  && conversationPane.thread.participants_required === true
                color: theme.control
                border.color: theme.accent
                radius: theme.controlRadius

                RowLayout {
                  id: groupRosterPrompt
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  anchors.margins: theme.scaled(10)
                  spacing: theme.scaled(10)

                  FerryLabel {
                    ferryTheme: theme
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: conversationPane.thread
                      ? conversationPane.thread.roster_changed
                        ? (conversationPane.thread.unexpected_sender || "Someone new")
                          + " is not in BlueFerry's saved participant list for "
                          + conversationPane.thread.name
                          + ". Review the list before replying."
                        : (conversationPane.thread.prompt_sender || "Someone")
                          + " has sent a message to the group "
                          + conversationPane.thread.name
                          + ". BlueFerry needs its participant list before you can reply."
                      : ""
                  }
                  FerryButton {
                    ferryTheme: theme
                    text: "Add participants"
                    highlighted: true
                    onClicked: {
                      root.groupParticipantsThread = conversationPane.thread
                      groupParticipantsPopup.open()
                    }
                  }
                }
              }

              FerryCheckBox {
                ferryTheme: theme
                id: confirmGroup
                property var thread: conversationPane.thread
                property string signature: root.groupSignature(thread)
                visible: thread && thread.is_group && thread.reply_ready
                text: thread
                  ? "Confirm group: " + thread.recipients.join(", ") : ""
                checked: root.groupIsConfirmed(thread) && signature !== ""
                enabled: !(thread && thread.group_confirmed === true)
                onToggled: root.setGroupConfirmed(thread, checked)
                Layout.fillWidth: true
              }

              Rectangle {
                Layout.fillWidth: true
                implicitHeight: composerRow.implicitHeight + theme.scaled(12)
                color: theme.control
                border.color: theme.divider
                radius: theme.controlRadius

                RowLayout {
                  id: composerRow
                  anchors.fill: parent
                  anchors.margins: theme.scaled(6)
                  FerryMessageComposer {
                    ferryTheme: theme
                    id: composer
                    Layout.fillWidth: true
                    placeholderText: "Write a message…"
                    flat: true
                    enabled: conversationPane.thread && conversationPane.thread.reply_ready
                    onAccepted: {
                      if (sendMessageButton.enabled) sendMessageButton.clicked()
                    }
                  }
                  FerryButton {
                    ferryTheme: theme
                    id: sendMessageButton
                    Layout.alignment: composer.multiline ? Qt.AlignBottom : Qt.AlignVCenter
                    text: root.sendBusy ? "SENDING" : "SEND"
                    highlighted: true
                    enabled: composer.enabled && composer.text.trim() !== "" &&
                             root.groupIsConfirmed(conversationPane.thread) &&
                             !root.sendBusy
                    onClicked: {
                      var thread = conversationPane.thread
                      root.sendBusy = true
                      backendBridge.request("send_to_thread", {
                        thread_key: thread.key,
                        body: composer.text,
                        confirm_group: thread.is_group,
                        expected_group_token: root.groupSignature(thread)
                      })
                    }
                  }
                }
              }
            }
          }
        }

        PhoneSettingsPage {
          ferryTheme: theme
          setup: setupController
          status: Object.assign({}, root.backendStatus, {
            notification_policy: root.notificationPolicy,
            contacts_only_notifications: root.contactsOnlyNotifications,
            storage_policy: root.storagePolicy
          })
          busy: ({notifications: root.notificationPolicyBusy,
                  contactsOnly: root.contactsOnlyNotificationsBusy,
                  storage: root.storagePolicyBusy})
          visible: root.phoneSettingsVisible
          Layout.fillWidth: true
          Layout.fillHeight: true
          onCloseRequested: root.phoneSettingsVisible = false
          onPairingIssueRequested: pairingIssuePopup.open()
          onErrorRequested: message => root.errorText = message
          onReloadRequested: root.reload()
          onOperationRequested: (method, args) => {
            if (method === "set_notification_policy") root.notificationPolicyBusy = true
            if (method === "set_contacts_only_notifications") {
              root.contactsOnlyNotifications = args.enabled
              root.contactsOnlyNotificationsBusy = true
            }
            if (method === "set_storage_policy") {
              if (args.policy === "encrypted") root.storageUnlockAttempted = true
              root.storagePolicyBusy = true
            }
            backendBridge.request(method, args)
          }
        }
      }

      Popup {
        id: replaceTargetPopup
        onClosed: setupController.pendingReplacement = null
        parent: applicationSurface
        x: Math.max(0, (applicationSurface.width - width) / 2)
        y: Math.max(0, (applicationSurface.height - height) / 2)
        width: Math.min(theme.scaled(440), applicationSurface.width - theme.scaled(24))
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: theme.scaled(18)

        background: Rectangle {
          color: theme.windowSurface
          border.color: theme.divider
          radius: theme.panelRadius
        }

        Overlay.modal: Rectangle {
          color: Qt.rgba(theme.windowSurface.r, theme.windowSurface.g,
                         theme.windowSurface.b, 0.72)
        }

        contentItem: ColumnLayout {
          spacing: theme.scaled(12)
          FerryLabel {
            ferryTheme: theme
            text: "Replace the saved iPhone?"
            font.bold: true
            font.pixelSize: theme.headingSize
          }
          FerryLabel {
            ferryTheme: theme
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: "Pairing this iPhone will remove BlueFerry's saved phone and its local Bluetooth bond. Before continuing, also forget this computer in the old iPhone's Bluetooth settings."
          }
          RowLayout {
            Layout.alignment: Qt.AlignRight
            FerryButton {
              ferryTheme: theme
              text: "Cancel"
              onClicked: replaceTargetPopup.close()
            }
            FerryButton {
              ferryTheme: theme
              text: "Replace and pair"
              highlighted: true
              onClicked: {
                setupController.confirmReplacement()
                replaceTargetPopup.close()
              }
            }
          }
        }
      }

      Popup {
        id: pairingIssuePopup
        parent: applicationSurface
        x: Math.max(0, (applicationSurface.width - width) / 2)
        y: Math.max(0, (applicationSurface.height - height) / 2)
        width: Math.min(theme.scaled(440), applicationSurface.width - theme.scaled(24))
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: theme.scaled(18)

        background: Rectangle {
          color: theme.windowSurface
          border.color: theme.divider
          radius: theme.panelRadius
        }

        Overlay.modal: Rectangle {
          color: Qt.rgba(theme.windowSurface.r, theme.windowSurface.g,
                         theme.windowSurface.b, 0.72)
        }

        contentItem: ColumnLayout {
          spacing: theme.scaled(12)
          FerryLabel {
            ferryTheme: theme
            text: "Report pairing issue"
            font.bold: true
            font.pixelSize: theme.headingSize
          }
          FerryLabel {
            ferryTheme: theme
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: "A pairing report was saved at " + setupController.pairingIssueReport
              + ". Attach that file to a GitHub issue and include the iPhone model and iOS version."
          }
          RowLayout {
            Layout.alignment: Qt.AlignRight
            FerryButton {
              ferryTheme: theme
              text: "Cancel"
              onClicked: pairingIssuePopup.close()
            }
            FerryButton {
              ferryTheme: theme
              text: "Open GitHub"
              highlighted: true
              onClicked: {
                pairingIssuePopup.close()
                setupController.openPairingIssue()
              }
            }
          }
        }
      }

      Popup {
        id: deleteThreadsPopup
        property string threadKey: ""
        parent: applicationSurface
        x: Math.max(0, (applicationSurface.width - width) / 2)
        y: Math.max(0, (applicationSurface.height - height) / 2)
        width: Math.min(theme.scaled(440), applicationSurface.width - theme.scaled(24))
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: theme.scaled(18)

        background: Rectangle {
          color: theme.windowSurface
          border.color: theme.divider
          radius: theme.panelRadius
        }

        Overlay.modal: Rectangle {
          color: Qt.rgba(theme.windowSurface.r, theme.windowSurface.g,
                         theme.windowSurface.b, 0.72)
        }

        contentItem: ColumnLayout {
          spacing: theme.scaled(12)
          FerryLabel {
            ferryTheme: theme
            text: "Delete conversation?"
            font.bold: true
            font.pixelSize: theme.headingSize
          }
          FerryLabel {
            ferryTheme: theme
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: "This permanently deletes this local message history and group metadata. Nothing is deleted from your iPhone. A new message can create the conversation again."
          }
          RowLayout {
            Layout.alignment: Qt.AlignRight
            FerryButton {
              ferryTheme: theme
              text: "Cancel"
              onClicked: deleteThreadsPopup.close()
            }
            FerryButton {
              ferryTheme: theme
              text: root.deleteThreadsBusy ? "Deleting…" : "Delete locally"
              highlighted: true
              enabled: !root.deleteThreadsBusy
              onClicked: {
                root.deleteThreadsBusy = true
                backendBridge.request("delete_threads", {
                  thread_keys: [deleteThreadsPopup.threadKey]
                })
                deleteThreadsPopup.close()
              }
            }
          }
        }
      }

      Menu {
        id: threadContextMenu
        property string threadKey: ""

        MenuItem {
          text: {
            var thread = root.threadByKey(threadContextMenu.threadKey)
            return thread && thread.starred
              ? "Unstar Conversation" : "Star Conversation"
          }
          onTriggered: {
            var thread = root.threadByKey(threadContextMenu.threadKey)
            root.setThreadStarred(
              threadContextMenu.threadKey,
              !(thread && thread.starred)
            )
          }
        }
        MenuItem {
          text: "Delete Conversation"
          onTriggered: {
            deleteThreadsPopup.threadKey = threadContextMenu.threadKey
            deleteThreadsPopup.open()
          }
        }
      }

      Popup {
        id: newMessagePopup
        parent: applicationSurface
        x: Math.max(0, (applicationSurface.width - width) / 2)
        y: Math.max(0, (applicationSurface.height - height) / 2)
        width: Math.min(theme.scaled(440), applicationSurface.width - theme.scaled(24))
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: theme.scaled(18)

        onOpened: {
          newRecipient.text = ""
          newMessageBody.text = ""
          root.searchContacts("")
          newRecipient.forceActiveFocus()
        }

        Timer {
          id: contactSearchTimer
          interval: 180
          onTriggered: root.searchContacts(newRecipient.text)
        }

        background: Rectangle {
          color: theme.windowSurface
          border.color: theme.divider
          radius: theme.panelRadius
        }

        Overlay.modal: Rectangle {
          color: Qt.rgba(theme.windowSurface.r, theme.windowSurface.g,
                         theme.windowSurface.b, 0.72)
        }

        contentItem: ColumnLayout {
          spacing: theme.scaled(8)

          FerryLabel {
            ferryTheme: theme
            text: "New message"
            font.bold: true
            font.pixelSize: theme.headingSize
          }
          FerryLabel {
            ferryTheme: theme
            text: "To"
            color: theme.muted
          }
          FerryTextField {
            ferryTheme: theme
            id: newRecipient
            Layout.fillWidth: true
            placeholderText: "Contact, phone number, or email address"
            Accessible.name: "Recipient"
            onTextEdited: contactSearchTimer.restart()
          }
          ListView {
            id: newContactResults
            Layout.fillWidth: true
            Layout.preferredHeight: count > 0
              ? Math.min(contentHeight, theme.scaled(180)) : 0
            visible: count > 0
            clip: true
            model: root.contactResults
            delegate: ItemDelegate {
              id: newContactDelegate
              required property var modelData
              width: newContactResults.width
              implicitHeight: newContactText.implicitHeight + theme.scaled(12)
              contentItem: Column {
                id: newContactText
                Text {
                  text: newContactDelegate.modelData.name
                  textFormat: Text.PlainText
                  color: theme.windowText
                  font.family: theme.fontFamily
                  font.pixelSize: theme.baseFontSize
                }
                Text {
                  text: newContactDelegate.modelData.address.indexOf("@") >= 0
                    ? newContactDelegate.modelData.address
                    : "+" + newContactDelegate.modelData.address
                  textFormat: Text.PlainText
                  color: theme.muted
                  font.family: theme.fontFamily
                  font.pixelSize: theme.captionSize
                }
              }
              onClicked: {
                newRecipient.text = modelData.address
                root.searchContacts("")
                newMessageBody.forceActiveFocus()
              }
            }
          }
          FerryLabel {
            ferryTheme: theme
            text: "Message"
            color: theme.muted
          }
          FerryMessageComposer {
            ferryTheme: theme
            id: newMessageBody
            Layout.fillWidth: true
            placeholderText: "Write a message"
            Accessible.name: "Message text"
            onAccepted: {
              if (newMessageSendButton.enabled) newMessageSendButton.clicked()
            }
          }
          RowLayout {
            Layout.alignment: Qt.AlignRight
            FerryButton {
              ferryTheme: theme
              text: "Cancel"
              onClicked: newMessagePopup.close()
            }
            FerryButton {
              ferryTheme: theme
              id: newMessageSendButton
              text: root.newMessageSendBusy ? "Sending…" : "Send"
              highlighted: true
              enabled: newRecipient.text.trim() !== ""
                && newMessageBody.text.trim() !== ""
                && !root.newMessageSendBusy
              onClicked: {
                root.newMessageSendBusy = true
                backendBridge.request("send", {
                  recipient: newRecipient.text,
                  body: newMessageBody.text
                })
              }
            }
          }
        }
      }

      Popup {
        id: rosterChangedPopup
        parent: applicationSurface
        x: Math.max(0, (applicationSurface.width - width) / 2)
        y: Math.max(0, (applicationSurface.height - height) / 2)
        width: Math.min(theme.scaled(500), applicationSurface.width - theme.scaled(24))
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: theme.scaled(18)

        background: Rectangle {
          color: theme.windowSurface
          border.color: theme.warning
          radius: theme.panelRadius
        }

        Overlay.modal: Rectangle {
          color: Qt.rgba(theme.windowSurface.r, theme.windowSurface.g,
                         theme.windowSurface.b, 0.72)
        }

        contentItem: ColumnLayout {
          spacing: theme.scaled(12)
          FerryLabel {
            ferryTheme: theme
            text: "Group membership may have changed"
            font.bold: true
            font.pixelSize: theme.headingSize
          }
          FerryLabel {
            ferryTheme: theme
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: root.rosterChangedThread
              ? (root.rosterChangedThread.unexpected_sender || "Someone new")
                + " sent a message to " + root.rosterChangedThread.name
                + ", but is not in BlueFerry's saved participant list. Replies are disabled until you review the list. This can also happen if you have multiple groups named "
                + root.rosterChangedThread.name
                + ", because BlueFerry cannot distinguish them."
              : ""
          }
          RowLayout {
            Layout.alignment: Qt.AlignRight
            FerryButton {
              ferryTheme: theme
              text: "Not now"
              onClicked: rosterChangedPopup.close()
            }
            FerryButton {
              ferryTheme: theme
              text: "Review participants"
              highlighted: true
              onClicked: {
                root.groupParticipantsThread = root.rosterChangedThread
                rosterChangedPopup.close()
                groupParticipantsPopup.open()
              }
            }
          }
        }
      }

      Popup {
        id: groupParticipantsPopup
        parent: applicationSurface
        x: Math.max(0, (applicationSurface.width - width) / 2)
        y: Math.max(0, (applicationSurface.height - height) / 2)
        width: Math.min(theme.scaled(520), applicationSurface.width - theme.scaled(24))
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        padding: theme.scaled(18)

        onOpened: {
          groupParticipantsEditor.text = root.groupParticipantsThread
            ? (root.groupParticipantsThread.recipients || []).join("\n") : ""
          groupParticipantsEditor.forceActiveFocus()
        }

        background: Rectangle {
          color: theme.windowSurface
          border.color: theme.divider
          radius: theme.panelRadius
        }

        Overlay.modal: Rectangle {
          color: Qt.rgba(theme.windowSurface.r, theme.windowSurface.g,
                         theme.windowSurface.b, 0.72)
        }

        contentItem: ColumnLayout {
          spacing: theme.scaled(10)

          FerryLabel {
            ferryTheme: theme
            text: root.groupParticipantsThread
              ? "Who is in " + root.groupParticipantsThread.name + "?" : "Group participants"
            font.bold: true
            font.pixelSize: theme.headingSize
          }
          FerryLabel {
            ferryTheme: theme
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: root.groupParticipantsThread
              ? (root.groupParticipantsThread.prompt_sender || "Someone")
                + " has sent a message to a group named "
                + root.groupParticipantsThread.name
                + ", which you're a member of. BlueFerry can't determine the participants of this group chat, but if you fill in the members, it can work."
              : ""
          }
          FerryLabel {
            ferryTheme: theme
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: theme.muted
            text: "Enter every other participant's phone number or Apple ID email, one per line."
          }
          FerryLabel {
            ferryTheme: theme
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: theme.windowText
            text: "Changing this list only updates BlueFerry's local understanding of the group. It does not add or remove anyone in Messages on your iPhone."
          }
          Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: theme.scaled(150)
            color: theme.control
            border.color: groupParticipantsEditor.activeFocus
              ? theme.accent : theme.divider
            radius: theme.controlRadius
            ScrollView {
              anchors.fill: parent
              anchors.margins: theme.scaled(2)
              TextArea {
                id: groupParticipantsEditor
                color: theme.windowText
                selectionColor: theme.accent
                selectedTextColor: theme.highlightedText
                font.family: theme.fontFamily
                font.pixelSize: theme.baseFontSize
                placeholderText: "One participant per line"
                placeholderTextColor: theme.muted
                wrapMode: TextEdit.NoWrap
                background: null
                Accessible.name: "Group participants"
              }
            }
          }
          FerryLabel {
            ferryTheme: theme
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: theme.warning
            text: root.groupParticipantsThread
              ? "BlueFerry identifies named groups by name. If you have multiple groups named "
                + root.groupParticipantsThread.name
                + ", BlueFerry may combine them and use the wrong participant list. This list can also become outdated if the group is renamed or its membership changes."
              : ""
          }
          RowLayout {
            Layout.alignment: Qt.AlignRight
            FerryButton {
              ferryTheme: theme
              text: "Cancel"
              onClicked: groupParticipantsPopup.close()
            }
            FerryButton {
              ferryTheme: theme
              text: root.groupParticipantsBusy ? "Saving…" : "Save participants"
              highlighted: true
              enabled: root.participantLines(groupParticipantsEditor.text).length >= 2
                && !root.groupParticipantsBusy
              onClicked: {
                var recipients = root.participantLines(groupParticipantsEditor.text)
                root.groupParticipantsBusy = true
                backendBridge.request("set_group_participants", {
                  thread_key: root.groupParticipantsThread.key,
                  recipients: recipients
                })
              }
            }
          }
        }
      }
    }
  }

}
