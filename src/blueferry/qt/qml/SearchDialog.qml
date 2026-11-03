pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls as Controls
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import "ui"

// Global search (Ctrl+K or Ctrl+F): one field over conversations, messages,
// contacts and calls, with the hits grouped as search_view orders them.
// Choosing a hit emits targetActivated with its search_view target; Main
// opens the conversation, contact card or call. The bridge searches after a
// pause in typing and forgets the results when the dialog closes. Every
// field comes from the iPhone and is rendered as plain text; only the
// escaped match in a message snippet is emphasised.
Kirigami.Dialog {
    id: dialog
    required property var bridge
    signal targetActivated(var target)

    readonly property var results: dialog.bridge.searchResults || ({})
    // The grouped rows as one list, each with its section title.
    readonly property var rows: {
        const flat = []
        const sections = dialog.results.sections || []
        for (let index = 0; index < sections.length; ++index) {
            const section = sections[index]
            for (let row = 0; row < section.rows.length; ++row)
                flat.push(Object.assign({}, section.rows[row], {
                    // "Messages · 3 more matches" when the limit cut the group.
                    section_title: section.more
                        ? section.title + " · " + section.more : section.title,
                }))
        }
        return flat
    }

    title: qsTr("Search")
    preferredWidth: Kirigami.Units.gridUnit * 30
    preferredHeight: Kirigami.Units.gridUnit * 28
    standardButtons: Kirigami.Dialog.NoButton
    padding: 0

    function htmlEscape(value: string): string {
        return String(value || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
    }

    // UTF-16 index of code point ``points`` in ``text``: the daemon counts
    // code points (Python), JavaScript strings count UTF-16 units.
    function utf16Index(text: string, points: int): int {
        let index = 0
        for (let seen = 0; seen < points && index < text.length; ++seen) {
            const unit = text.charCodeAt(index)
            index += unit >= 0xD800 && unit <= 0xDBFF && index + 1 < text.length ? 2 : 1
        }
        return index
    }

    // The snippet as escaped rich text with the match in bold.
    function snippetMarkup(row: var): string {
        const snippet = String(row.snippet || "")
        const highlight = row.highlight || []
        if (highlight.length !== 2)
            return "<span>" + dialog.htmlEscape(snippet) + "</span>"
        const start = dialog.utf16Index(snippet, highlight[0])
        const end = dialog.utf16Index(snippet, highlight[0] + highlight[1])
        return "<span>" + dialog.htmlEscape(snippet.slice(0, start))
            + "<b>" + dialog.htmlEscape(snippet.slice(start, end)) + "</b>"
            + dialog.htmlEscape(snippet.slice(end)) + "</span>"
    }

    function activate(index: int): void {
        if (index < 0 || index >= dialog.rows.length)
            return
        const target = dialog.rows[index].target
        dialog.close()
        dialog.targetActivated(target)
    }

    onOpened: {
        searchField.text = ""
        searchField.forceActiveFocus()
    }
    onClosed: {
        searchField.text = ""
        dialog.bridge.clearSearch()
    }

    ColumnLayout {
        spacing: 0

        Kirigami.SearchField {
            id: searchField
            objectName: "globalSearchField"
            Layout.fillWidth: true
            Layout.margins: Kirigami.Units.largeSpacing
            placeholderText: dialog.results.placeholder || ""
            maximumLength: 128
            Accessible.name: placeholderText
            Controls.ToolTip.text: dialog.results.scope || ""
            Controls.ToolTip.visible: hovered && Controls.ToolTip.text !== ""
            // The bridge waits for a pause in typing before it searches.
            onTextChanged: {
                dialog.bridge.setSearchQuery(text)
                resultList.currentIndex = 0
            }
            Keys.onDownPressed: resultList.incrementCurrentIndex()
            Keys.onUpPressed: resultList.decrementCurrentIndex()
            Keys.onReturnPressed: dialog.activate(resultList.currentIndex)
            Keys.onEnterPressed: dialog.activate(resultList.currentIndex)
        }

        Kirigami.InlineMessage {
            objectName: "globalSearchError"
            Layout.fillWidth: true
            Layout.margins: Kirigami.Units.smallSpacing
            visible: (dialog.results.error || "") !== ""
            type: Kirigami.MessageType.Error
            text: dialog.results.error || ""
        }

        Controls.Label {
            objectName: "globalSearchLimit"
            Layout.fillWidth: true
            Layout.leftMargin: Kirigami.Units.largeSpacing
            Layout.rightMargin: Kirigami.Units.largeSpacing
            visible: text !== ""
            text: dialog.results.limit_text || ""
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            font: Kirigami.Theme.smallFont
            color: Kirigami.Theme.disabledTextColor
        }

        ListView {
            id: resultList
            objectName: "globalSearchResults"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: Kirigami.Units.gridUnit * 16
            clip: true
            model: dialog.rows
            currentIndex: 0
            keyNavigationEnabled: true
            highlightFollowsCurrentItem: true
            section.property: "section_title"
            section.criteria: ViewSection.FullString
            section.delegate: Kirigami.ListSectionHeader {
                required property string section
                width: ListView.view.width
                text: section
            }
            Controls.ScrollBar.vertical: Controls.ScrollBar { id: resultScrollBar }
            // Rows end before a scroll bar that takes room, so it never covers
            // their times.
            rightMargin: resultScrollBar.visible
                ? resultScrollBar.width : 0

            delegate: ListRow {
                id: hit
                objectName: "globalSearchHit"
                required property var modelData
                required property int index
                partPrefix: "hit"
                density: "compact"
                highlighted: ListView.isCurrentItem
                title: hit.modelData.title
                subtitle: hit.modelData.subtitle
                meta: hit.modelData.time
                iconName: hit.modelData.target.kind === "contact" ? "view-pim-contacts"
                    : hit.modelData.target.kind === "call" ? "call-start"
                    : hit.modelData.target.kind === "message" ? "dialog-messages"
                    : "mail-message"
                Accessible.name: [hit.modelData.section_title, hit.modelData.title,
                    hit.modelData.subtitle, hit.modelData.snippet, hit.modelData.time]
                    .filter(part => part !== "").join(", ")
                // A message hit shows the snippet with the match emphasised.
                escapedBody: hit.modelData.snippet ? dialog.snippetMarkup(hit.modelData) : ""
                bodyLines: 2
                onClicked: dialog.activate(hit.index)
            }

            EmptyState {
                objectName: "globalSearchHint"
                anchors.centerIn: parent
                visible: resultList.count === 0 && dialog.results.loading !== true
                icon.name: "edit-find"
                text: dialog.results.supported === false
                    ? qsTr("Search Unavailable") : qsTr("Search")
                explanation: dialog.results.supported === false
                    ? qsTr("The running BlueFerry service cannot search. Restart it after upgrading.")
                    : (dialog.results.hint || dialog.results.scope || "")
            }

            Controls.BusyIndicator {
                anchors.centerIn: parent
                visible: resultList.count === 0 && dialog.results.loading === true
                running: visible
            }
        }
    }
}
