import QtQuick

// Pure theme tokens; Theme.qml supplies the public Omarchy files.
QtObject {
  property bool quattroActive: false
  property var colors: ({})
  property var shell: ({})
  property int hyprlandRadius: 0
  property SystemPalette fallbackPalette: SystemPalette { colorGroup: SystemPalette.Active }

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
  // SystemPalette.mid can be darker than a dark SystemPalette.window (the
  // Qt fallback observed here was #1c1f21 over #202326). Derive secondary
  // text from the actual foreground/background pair in every theme mode.
  readonly property color muted: blend(foreground, background, 0.62)

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
  readonly property color hoverSurface: blend(windowText, windowSurface, 0.09)
  readonly property color selectedSurface: blend(accent, windowSurface, 0.22)
  readonly property color primarySurface: blend(accent, windowSurface, 0.18)
  readonly property color primaryText: windowText
  readonly property color divider: blend(windowText, windowSurface, 0.14)
  readonly property color highlightedText: quattroActive ? background : fallbackPalette.highlightedText

  readonly property real baseFontSize: shellNumber("font.base-size", 12)
  readonly property real fontScale: baseFontSize / 12
  readonly property real spacingScale: shellNumber("spacing.scale", 1.0)
  readonly property bool spacingFollowsFont: shellBoolean("spacing.scale-with-font", true)
  readonly property int panelPadding: shellPixels("spacing.panel-padding", 14)
  readonly property int cornerRadius: quattroActive ? hyprlandRadius : 0
  readonly property int panelRadius: cornerRadius
  readonly property int controlRadius: Math.min(scaled(4), cornerRadius)
  readonly property int headingSize: Math.max(1, Math.round(baseFontSize * 1.2))
  readonly property int displaySize: Math.max(1, Math.round(baseFontSize * 1.5))
  readonly property int captionSize: Math.max(1, Math.round(baseFontSize * 0.85))
  readonly property int bodySmallSize: Math.max(1, Math.round(baseFontSize * 0.95))
  // Omarchy changes the system monospace alias when the user selects a font.
  readonly property string fontFamily: "monospace"
  // BlueFerry's message identity remains blue with every shell accent.
  readonly property color outgoingBubble: "#245baf"
  readonly property color outgoingText: "#ffffff"
  readonly property color outgoingMuted: "#d2e2fa"

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

}
