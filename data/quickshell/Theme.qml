import QtQuick
import Quickshell
import Quickshell.Io

// The standalone client cannot import Omarchy's qs.Commons singleton because
// it runs in its own Quickshell instance. Read the same public theme files and
// keep the normal desktop palette as a fallback for non-Omarchy sessions.
ThemePalette {
  id: root

  readonly property string home: Quickshell.env("HOME")
  readonly property string currentThemePath: home + "/.local/state/omarchy/current/theme"

  property string colorsSource: ""
  property string shellSource: ""
  property string userShellSource: ""
  property var themeShell: ({})
  property var userShell: ({})

  function mergeShell() {
    var merged = {}
    for (var themeKey in themeShell) merged[themeKey] = themeShell[themeKey]
    for (var userKey in userShell) merged[userKey] = userShell[userKey]
    shell = merged
  }

  function loadColors(raw) {
    var text = String(raw || "")
    if (!text.trim()) return
    var changed = text !== colorsSource
    colorsSource = text
    colors = parseToml(text)
    quattroActive = colors.background !== undefined || colors.foreground !== undefined
    if (changed && quattroActive && !radiusProcess.running) radiusProcess.running = true
  }

  function loadThemeShell(raw) {
    var text = String(raw || "")
    if (text === shellSource) return
    shellSource = text
    themeShell = parseToml(text)
    mergeShell()
  }

  function loadUserShell(raw) {
    var text = String(raw || "")
    if (text === userShellSource) return
    userShellSource = text
    userShell = parseToml(text)
    mergeShell()
  }

  property FileView colorsFile: FileView {
    path: root.currentThemePath + "/colors.toml"
    printErrors: false
    watchChanges: true
    onLoaded: root.loadColors(text())
    onFileChanged: reload()
    onLoadFailed: root.quattroActive = false
  }

  property FileView shellFile: FileView {
    path: root.currentThemePath + "/shell.toml"
    printErrors: false
    watchChanges: true
    onLoaded: root.loadThemeShell(text())
    onFileChanged: reload()
    onLoadFailed: root.loadThemeShell("")
  }

  property FileView userShellFile: FileView {
    path: root.home + "/.config/omarchy/shell.toml"
    printErrors: false
    watchChanges: true
    onLoaded: root.loadUserShell(text())
    onFileChanged: reload()
    onLoadFailed: root.loadUserShell("")
  }

  // A theme switch replaces the `current/theme` symlink. Some file watchers
  // keep watching the old target, so a cheap periodic reload closes that gap.
  property Timer themeReloadTimer: Timer {
    interval: 1500
    repeat: true
    running: true
    onTriggered: {
      root.colorsFile.reload()
      root.shellFile.reload()
      root.userShellFile.reload()
    }
  }

  property Process radiusProcess: Process {
    command: ["/usr/bin/hyprctl", "getoption", "decoration:rounding", "-j"]
    stdout: StdioCollector {
      onStreamFinished: {
        try {
          var parsed = JSON.parse(text)
          if (isFinite(Number(parsed.int))) root.hyprlandRadius = Math.max(0, Number(parsed.int))
        } catch (error) { }
      }
    }
    stderr: StdioCollector { }
  }
}
