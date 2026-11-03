pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// The panel's contact search and global search, compact. With an empty
// field it lists the contact cards (`contacts`, the bridge's contact_cards
// reply: contacts_view rows plus count_text, hint, has_more, next_offset).
// With two or more characters and a daemon that can search, it shows the
// bridge's search reply (`results`: flat search_view rows with their
// section, the snippet split around the match and a flattened target);
// otherwise it filters the contact cards. A contact opens `card` (the
// bridge's contact_card reply: title, subtitle and detail rows with
// can_call and can_message). The panel only offers Call and Message; a
// call is confirmed by the shell, and the running call is handled on the
// iPhone or in another client. Every field is shown as plain text; the
// labels are English like the rest of this shell (no translation catalog).
ColumnLayout {
  id: root
  required property var ferryTheme
  property var contacts: ({})
  property var results: ({})
  property var card: ({})
  property bool searchSupported: false
  property bool contactsSupported: false
  property bool busy: false
  property string error: ""
  // The shared calls hint (status dial_hint); "" while calls are ready.
  property string dialHint: ""
  readonly property string query: searchField.text.trim()
  // Without contact cards, a short query shows the search's own hint.
  readonly property bool globalMode: root.searchSupported
    && (root.query.length >= 2 || !root.contactsSupported)
  readonly property bool cardShown: root.card.found === true || String(root.card.message || "") !== ""
  readonly property var contactRows: root.listOf(root.contacts.rows)
  readonly property var resultRows: root.listOf(root.results.rows)
  readonly property var cardRows: root.listOf(root.card.rows)
  signal queryEdited(string query)
  signal moreContactsRequested()
  signal contactRequested(string contactId)
  signal cardClosed()
  signal conversationRequested(string threadKey, string handle)
  signal callRequested(string number)
  signal messageRequested(string address)
  spacing: root.ferryTheme.scaled(6)

  function listOf(value): var {
    // A JS array from shell.qml's plainValue, or a Qt sequence; both index.
    return value && value.length !== undefined ? Array.prototype.slice.call(value) : []
  }

  function focusField(): void {
    searchField.forceActiveFocus()
  }

  function clear(): void {
    searchField.text = ""
  }

  function openResult(row: var): void {
    if (row.kind === "contact") root.contactRequested(String(row.contact_id || ""))
    else if (row.kind === "conversation" || row.kind === "message")
      root.conversationRequested(String(row.thread_key || ""), String(row.handle || ""))
  }

  FerryTextField {
    id: searchField
    objectName: "searchField"
    ferryTheme: root.ferryTheme
    Layout.fillWidth: true
    visible: !root.cardShown
    placeholderText: root.searchSupported ? "Search messages, contacts and calls" : "Search contacts"
    Accessible.name: placeholderText
    onTextEdited: root.queryEdited(searchField.text.trim())
  }

  FerryLabel {
    objectName: "searchError"
    ferryTheme: root.ferryTheme
    Layout.fillWidth: true
    visible: text !== ""
    wrapMode: Text.Wrap
    color: root.ferryTheme.warning
    text: root.error
  }

  // ---- one contact card --------------------------------------------------
  RowLayout {
    Layout.fillWidth: true
    visible: root.cardShown
    spacing: root.ferryTheme.scaled(8)
    FerryButton {
      objectName: "searchCardBack"
      ferryTheme: root.ferryTheme
      text: "‹ Back"
      Accessible.name: "Back to the search"
      onClicked: root.cardClosed()
    }
    ColumnLayout {
      Layout.fillWidth: true
      spacing: 0
      FerryLabel {
        objectName: "searchCardTitle"
        ferryTheme: root.ferryTheme
        Layout.fillWidth: true
        elide: Text.ElideRight
        font.bold: true
        text: String(root.card.title || "")
      }
      FerryLabel {
        objectName: "searchCardSubtitle"
        ferryTheme: root.ferryTheme
        Layout.fillWidth: true
        visible: text !== ""
        elide: Text.ElideRight
        color: root.ferryTheme.muted
        font.pixelSize: root.ferryTheme.captionSize
        text: String(root.card.subtitle || "")
      }
    }
  }
  FerryLabel {
    objectName: "searchCardMessage"
    ferryTheme: root.ferryTheme
    Layout.fillWidth: true
    visible: root.cardShown && text !== ""
    wrapMode: Text.Wrap
    color: root.ferryTheme.warning
    text: String(root.card.message || "")
  }
  FerryLabel {
    objectName: "searchDialHint"
    ferryTheme: root.ferryTheme
    Layout.fillWidth: true
    visible: root.cardShown && text !== "" && root.cardRows.some(row => row.can_call === true)
    wrapMode: Text.Wrap
    color: root.ferryTheme.muted
    font.pixelSize: root.ferryTheme.captionSize
    text: root.dialHint
  }

  ScrollView {
    id: scroller
    Layout.fillWidth: true
    Layout.fillHeight: true
    Layout.preferredHeight: root.ferryTheme.scaled(320)
    clip: true
    contentWidth: availableWidth

    ColumnLayout {
      width: scroller.availableWidth
      spacing: root.ferryTheme.scaled(4)

      Repeater {
        objectName: "searchCardRows"
        model: root.cardShown ? root.cardRows : []
        delegate: ColumnLayout {
          id: detail
          required property int index
          required property var modelData
          readonly property var row: detail.modelData || ({})
          Layout.fillWidth: true
          spacing: root.ferryTheme.scaled(1)
          FerrySectionLabel {
            ferryTheme: root.ferryTheme
            visible: detail.index === 0
              || root.cardRows[detail.index - 1].section !== detail.row.section
            text: String(detail.row.section_title || "")
          }
          RowLayout {
            Layout.fillWidth: true
            spacing: root.ferryTheme.scaled(6)
            ColumnLayout {
              Layout.fillWidth: true
              spacing: 0
              FerryLabel {
                ferryTheme: root.ferryTheme
                color: root.ferryTheme.muted
                font.pixelSize: root.ferryTheme.captionSize
                text: String(detail.row.label || "")
              }
              FerryLabel {
                objectName: "searchCardValue" + detail.index
                ferryTheme: root.ferryTheme
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: String(detail.row.value || "")
              }
            }
            FerryButton {
              objectName: "searchCardCall" + detail.index
              ferryTheme: root.ferryTheme
              visible: detail.row.can_call === true
              enabled: root.dialHint === ""
              text: "Call"
              labelSize: root.ferryTheme.captionSize
              Accessible.name: "Call " + String(detail.row.value || "")
              Accessible.description: root.dialHint
              ToolTip.visible: hovered && root.dialHint !== ""
              ToolTip.text: root.dialHint
              onClicked: root.callRequested(String(detail.row.target || ""))
            }
            FerryButton {
              objectName: "searchCardMessage" + detail.index
              ferryTheme: root.ferryTheme
              visible: detail.row.can_message === true
              text: "Message"
              labelSize: root.ferryTheme.captionSize
              Accessible.name: "Send a message to " + String(detail.row.value || "")
              onClicked: root.messageRequested(String(detail.row.target || ""))
            }
          }
        }
      }

      // ---- global search hits ------------------------------------------
      FerryLabel {
        objectName: "searchHint"
        ferryTheme: root.ferryTheme
        Layout.fillWidth: true
        visible: !root.cardShown && root.globalMode && text !== ""
        wrapMode: Text.Wrap
        color: root.ferryTheme.muted
        text: String(root.results.hint || "")
      }
      FerryLabel {
        objectName: "searchLimit"
        ferryTheme: root.ferryTheme
        Layout.fillWidth: true
        visible: !root.cardShown && root.globalMode && text !== ""
        wrapMode: Text.Wrap
        color: root.ferryTheme.muted
        font.pixelSize: root.ferryTheme.captionSize
        text: String(root.results.limit_text || "")
      }
      Repeater {
        objectName: "searchResultRows"
        model: !root.cardShown && root.globalMode ? root.resultRows : []
        delegate: ColumnLayout {
          id: hit
          required property int index
          required property var modelData
          readonly property var row: hit.modelData || ({})
          readonly property bool opens: hit.row.kind !== "call"
          Layout.fillWidth: true
          spacing: root.ferryTheme.scaled(1)
          FerrySectionLabel {
            objectName: "searchSection" + hit.index
            ferryTheme: root.ferryTheme
            visible: hit.index === 0
              || root.resultRows[hit.index - 1].section !== hit.row.section
            text: String(hit.row.section_title || "")
              + (hit.row.section_more ? "  ·  " + String(hit.row.section_more) : "")
          }
          RowLayout {
            Layout.fillWidth: true
            spacing: root.ferryTheme.scaled(6)
            ItemDelegate {
              id: hitButton
              objectName: "searchResult" + hit.index
              Layout.fillWidth: true
              enabled: hit.opens
              padding: root.ferryTheme.scaled(4)
              Accessible.name: String(hit.row.title || "") + ", " + String(hit.row.subtitle || "")
              onClicked: root.openResult(hit.row)
              background: Rectangle {
                color: hitButton.hovered && hitButton.enabled ? root.ferryTheme.hoverSurface : "transparent"
                radius: root.ferryTheme.controlRadius
              }
              contentItem: ColumnLayout {
                spacing: 0
                RowLayout {
                  Layout.fillWidth: true
                  FerryLabel {
                    objectName: "searchResultTitle" + hit.index
                    ferryTheme: root.ferryTheme
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                    font.bold: true
                    text: String(hit.row.title || "")
                  }
                  FerryLabel {
                    ferryTheme: root.ferryTheme
                    visible: text !== ""
                    color: root.ferryTheme.muted
                    font.pixelSize: root.ferryTheme.captionSize
                    text: String(hit.row.time || "")
                  }
                }
                FerryLabel {
                  objectName: "searchResultSubtitle" + hit.index
                  ferryTheme: root.ferryTheme
                  Layout.fillWidth: true
                  visible: text !== ""
                  elide: Text.ElideRight
                  color: root.ferryTheme.muted
                  font.pixelSize: root.ferryTheme.captionSize
                  text: String(hit.row.subtitle || "")
                }
                // The message snippet on one line, the match in bold.
                RowLayout {
                  Layout.fillWidth: true
                  visible: String(hit.row.snippet_match || "") !== ""
                    || String(hit.row.snippet_before || "") !== ""
                  spacing: 0
                  FerryLabel {
                    objectName: "searchSnippetBefore" + hit.index
                    ferryTheme: root.ferryTheme
                    // Whole pixels: the layout rounds the fractional text
                    // width down, which elided the first words with room
                    // to spare.
                    Layout.maximumWidth: Math.ceil(implicitWidth)
                    Layout.fillWidth: true
                    elide: Text.ElideLeft
                    font.pixelSize: root.ferryTheme.bodySmallSize
                    text: String(hit.row.snippet_before || "")
                  }
                  FerryLabel {
                    objectName: "searchSnippetMatch" + hit.index
                    ferryTheme: root.ferryTheme
                    font.bold: true
                    color: root.ferryTheme.accent
                    font.pixelSize: root.ferryTheme.bodySmallSize
                    text: String(hit.row.snippet_match || "")
                  }
                  FerryLabel {
                    objectName: "searchSnippetAfter" + hit.index
                    ferryTheme: root.ferryTheme
                    Layout.fillWidth: true
                    elide: Text.ElideRight
                    font.pixelSize: root.ferryTheme.bodySmallSize
                    text: String(hit.row.snippet_after || "")
                  }
                }
              }
            }
            // A call hit has no list to open in the panel: call back or write.
            FerryButton {
              objectName: "searchResultCall" + hit.index
              ferryTheme: root.ferryTheme
              visible: !hit.opens && String(hit.row.number || "") !== ""
              enabled: root.dialHint === ""
              text: "Call"
              labelSize: root.ferryTheme.captionSize
              Accessible.name: "Call " + String(hit.row.number || "")
              Accessible.description: root.dialHint
              ToolTip.visible: hovered && root.dialHint !== ""
              ToolTip.text: root.dialHint
              onClicked: root.callRequested(String(hit.row.number || ""))
            }
            FerryButton {
              objectName: "searchResultMessage" + hit.index
              ferryTheme: root.ferryTheme
              visible: !hit.opens && String(hit.row.number || "") !== ""
              text: "Message"
              labelSize: root.ferryTheme.captionSize
              Accessible.name: "Send a message to " + String(hit.row.number || "")
              onClicked: root.messageRequested(String(hit.row.number || ""))
            }
          }
        }
      }

      // ---- contact cards -----------------------------------------------
      FerryLabel {
        objectName: "searchContactsCount"
        ferryTheme: root.ferryTheme
        Layout.fillWidth: true
        visible: !root.cardShown && !root.globalMode && root.contactsSupported
          && root.contactRows.length > 0 && text !== ""
        color: root.ferryTheme.muted
        font.pixelSize: root.ferryTheme.captionSize
        text: String(root.contacts.count_text || "")
      }
      FerryLabel {
        objectName: "searchContactsHint"
        ferryTheme: root.ferryTheme
        Layout.fillWidth: true
        visible: !root.cardShown && !root.globalMode && root.contactsSupported && text !== ""
        wrapMode: Text.Wrap
        color: root.ferryTheme.muted
        text: String(root.contacts.hint || "")
      }
      Repeater {
        objectName: "searchContactRows"
        model: !root.cardShown && !root.globalMode && root.contactsSupported
          ? root.contactRows : []
        delegate: ItemDelegate {
          id: contact
          required property int index
          required property var modelData
          readonly property var row: contact.modelData || ({})
          objectName: "searchContact" + contact.index
          Layout.fillWidth: true
          padding: root.ferryTheme.scaled(4)
          Accessible.name: String(contact.row.title || "")
          onClicked: root.contactRequested(String(contact.row.id || ""))
          background: Rectangle {
            color: contact.hovered ? root.ferryTheme.hoverSurface : "transparent"
            radius: root.ferryTheme.controlRadius
          }
          contentItem: ColumnLayout {
            spacing: 0
            FerryLabel {
              objectName: "searchContactTitle" + contact.index
              ferryTheme: root.ferryTheme
              Layout.fillWidth: true
              elide: Text.ElideRight
              text: String(contact.row.title || "")
            }
            FerryLabel {
              ferryTheme: root.ferryTheme
              Layout.fillWidth: true
              visible: text !== ""
              elide: Text.ElideRight
              color: root.ferryTheme.muted
              font.pixelSize: root.ferryTheme.captionSize
              text: String(contact.row.subtitle || "")
            }
          }
        }
      }
      FerryButton {
        objectName: "searchContactsMore"
        ferryTheme: root.ferryTheme
        visible: !root.cardShown && !root.globalMode && root.contactsSupported
          && root.contacts.has_more === true
        enabled: !root.busy
        text: "More contacts"
        onClicked: root.moreContactsRequested()
      }
    }
  }
}
