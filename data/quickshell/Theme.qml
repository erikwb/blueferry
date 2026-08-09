import QtQuick
import Quickshell
import Quickshell.Io

// The standalone client cannot import Omarchy's qs.Commons singleton because
// it runs in its own Quickshell instance. Read the same public theme files and
// keep the normal desktop palette as a fallback for non-Omarchy sessions.
QtObject {
  id: root

  readonly property string home: Quickshell.env("HOME")
  readonly property string currentThemePath: home + "/.local/state/omarchy/current/theme"

  property bool quattroActive: false
  property string colorsSource: ""
  property string shellSource: ""
  property string userShellSource: ""
  property var colors: ({})
  property var themeShell: ({})
  property var userShell: ({})
  property var shell: ({})
  property int hyprlandRadius: 14

  property SystemPalette fallbackPalette: SystemPalette {
    colorGroup: SystemPalette.Active
  }

  readonly property color background: quattroActive
    ? baseColor(["background", "color0"], fallbackPalette.window)
    : fallbackPalette.window
  readonly property color foreground: quattroActive
    ? baseColor(["foreground", "color7"], fallbackPalette.text)
    : fallbackPalette.text
  readonly property color accent: quattroActive
    ? baseColor(["accent", "blue", "color4"], fallbackPalette.highlight)
    : fallbackPalette.highlight
  readonly property color urgent: quattroActive
    ? baseColor(["red", "color1"], fallbackPalette.text)
    : fallbackPalette.text
  readonly property color warning: quattroActive
    ? baseColor(["yellow", "orange"], urgent)
    : fallbackPalette.text
  readonly property color muted: quattroActive
    ? baseColor(["muted", "color8"], withAlpha(foreground, 0.65))
    : fallbackPalette.mid

  // BlueFerry is a normal application window, not a shell flyout. Quattro
  // themes may deliberately make popups translucent, but the app needs an
  // opaque base so its contents remain readable over arbitrary wallpapers.
  readonly property color windowSurface: Qt.rgba(
    background.r, background.g, background.b, 1)
  readonly property color windowText: foreground

  readonly property color surface: quattroActive
    ? withAlpha(resolveShellColor("popups.background", background),
                shellNumber("popups.background-alpha", 1.0))
    : fallbackPalette.window
  readonly property color surfaceText: quattroActive
    ? resolveShellColor("popups.text", foreground)
    : fallbackPalette.text
  readonly property color surfaceBorder: quattroActive
    ? withAlpha(resolveShellColor("popups.border", accent),
                shellNumber("popups.border-alpha", 1.0))
    : fallbackPalette.mid
  readonly property color control: quattroActive
    ? blend(windowText, windowSurface,
            shellNumber("controls.normal-fill-alpha", 0.04))
    : fallbackPalette.button
  readonly property color alternate: quattroActive
    ? blend(windowText, windowSurface, 0.07)
    : fallbackPalette.alternateBase
  readonly property color hoverSurface: blend(windowText, windowSurface, 0.06)
  readonly property color selectedSurface: blend(accent, windowSurface, 0.16)
  readonly property color highlightedText: quattroActive ? background : fallbackPalette.highlightedText

  readonly property real baseFontSize: shellNumber("font.base-size", 12)
  readonly property real fontScale: baseFontSize / 12
  readonly property real spacingScale: shellNumber("spacing.scale", 1.0)
  readonly property bool spacingFollowsFont: shellBoolean("spacing.scale-with-font", true)
  readonly property int panelPadding: shellPixels("spacing.panel-padding", 18)
  readonly property int smallGap: shellPixels("spacing.lg", 8)
  readonly property int cornerRadius: quattroActive ? hyprlandRadius : 14
  readonly property int panelRadius: quattroActive ? 0 : cornerRadius
  readonly property int headingSize: Math.max(1, Math.round(baseFontSize * 1.5))
  readonly property int displaySize: Math.max(1, Math.round(baseFontSize * 1.85))
  readonly property int captionSize: Math.max(1, Math.round(baseFontSize * 0.82))
  readonly property int bodySmallSize: Math.max(1, Math.round(baseFontSize * 0.9))
  readonly property string fontFamily: "monospace"

  function scaled(px) {
    var scale = spacingScale * (spacingFollowsFont ? fontScale : 1.0)
    return Math.max(1, Math.round(px * scale))
  }

  function shellPixels(key, fallback) {
    var value = Number(shell[key])
    return isFinite(value) ? Math.max(1, Math.round(value)) : scaled(fallback)
  }

  function shellNumber(key, fallback) {
    var value = Number(shell[key])
    return isFinite(value) ? value : fallback
  }

  function shellBoolean(key, fallback) {
    var value = shell[key]
    if (value === true || String(value).toLowerCase() === "true") return true
    if (value === false || String(value).toLowerCase() === "false") return false
    return fallback
  }

  function withAlpha(color, alpha) {
    var amount = Math.max(0, Math.min(1, Number(alpha)))
    return Qt.rgba(color.r, color.g, color.b, color.a * amount)
  }

  function blend(fore, back, amount) {
    var mix = Math.max(0, Math.min(1, Number(amount)))
    return Qt.rgba(
      back.r + (fore.r - back.r) * mix,
      back.g + (fore.g - back.g) * mix,
      back.b + (fore.b - back.b) * mix,
      1)
  }

  function firstColorToken(value) {
    var parts = String(value || "").trim().split(/\s+/)
    for (var index = 0; index < parts.length; ++index) {
      if (!parts[index].match(/^-?\d+(?:\.\d+)?deg$/)) return parts[index]
    }
    return ""
  }

  function canonicalColor(value) {
    var token = firstColorToken(value)
    var hex = token.match(/^#([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$/)
    if (hex) return token
    var rgb = token.match(/^rgb\(([0-9A-Fa-f]{6})\)$/)
    if (rgb) return "#" + rgb[1]
    var rgba = token.match(/^rgba\(([0-9A-Fa-f]{8})\)$/)
    if (rgba) return "#" + rgba[1].slice(6, 8) + rgba[1].slice(0, 6)
    return ""
  }

  function baseColor(keys, fallback) {
    for (var index = 0; index < keys.length; ++index) {
      var resolved = resolveBaseToken(keys[index], {})
      if (resolved) return resolved
    }
    return fallback
  }

  // Foundational colors must not resolve through the derived QML properties:
  // a custom theme may alias roles, including accidentally cyclic aliases.
  function resolveBaseToken(key, seen) {
    if (seen[key] || colors[key] === undefined) return ""
    var nextSeen = {}
    for (var visited in seen) nextSeen[visited] = true
    nextSeen[key] = true

    var token = firstColorToken(colors[key])
    var direct = canonicalColor(token)
    if (direct) return direct

    var role = token.toLowerCase()
    if (role === "text") role = "foreground"
    else if (role === "urgent") role = "red"
    if (colors[role] !== undefined)
      return resolveBaseToken(role, nextSeen)
    return ""
  }

  function resolveColor(value, fallback) {
    var token = firstColorToken(value)
    var role = token.toLowerCase()
    if (role === "background") return background
    if (role === "foreground" || role === "text") return foreground
    if (role === "accent") return accent
    if (role === "urgent") return urgent
    if (role === "muted") return muted
    if (role === "transparent") return Qt.rgba(0, 0, 0, 0)
    var direct = canonicalColor(token)
    return direct || fallback
  }

  function resolveShellColor(key, fallback, depth) {
    var value = shell[key]
    if (value === undefined || value === null || value === "") return fallback
    var token = firstColorToken(value)
    if ((depth || 0) < 4 && shell[token] !== undefined && shell[token] !== value)
      return resolveShellColor(token, fallback, (depth || 0) + 1)
    return resolveColor(token, fallback)
  }

  function parseToml(raw) {
    var parsed = {}
    var section = ""
    var lines = String(raw || "").split("\n")
    for (var index = 0; index < lines.length; ++index) {
      var line = lines[index].trim()
      if (!line || line.charAt(0) === "#") continue
      var sectionMatch = line.match(/^\[([A-Za-z0-9_-]+)\]\s*(?:#.*)?$/)
      if (sectionMatch) {
        section = sectionMatch[1]
        continue
      }
      var match = line.match(/^([A-Za-z0-9_-]+)\s*=\s*(?:["']([^"']*)["']|([^#\s]+))(?:\s+#.*)?$/)
      if (!match) continue
      var key = section ? section + "." + match[1] : match[1]
      parsed[key] = match[2] !== undefined ? match[2] : match[3]
    }
    return parsed
  }

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
