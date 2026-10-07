# Copy one-time codes to the clipboard

BlueFerry can copy a verification code (2FA/OTP) from a newly received SMS
or iMessage straight to the desktop clipboard, so you can paste it into a
login form with Ctrl+V instead of typing it from the phone.

The feature is **off by default**, because it changes the clipboard without
you doing anything and every application that reads the clipboard can see
the code.

## What it does

```mermaid
flowchart LR
    A[New message from the iPhone] --> B{Just arrived?<br/>incoming, unseen,<br/>at most 10 min old}
    B -- no --> X[Ignored]
    B -- yes --> C{Code next to a<br/>keyword like 'code'?}
    C -- no --> X
    C -- yes --> D[wl-copy / xclip / xsel<br/>code fed on stdin]
    D --> E[Short popup:<br/>'Verification code copied']
    D --> F{Clear timer set?}
    F -- yes, code still on clipboard --> G[Clipboard cleared]
```

- Only messages that have **just arrived** count. Sent messages, history,
  messages that were already seen, and messages older than ten minutes are
  ignored.
- A number counts as a code only when a word such as "code",
  "verification", "Bestätigungscode", "code de vérification", "codice" or
  "código" is next to it. Amounts, dates, times, phone numbers, and order,
  tracking or invoice numbers are skipped.
- `G-123456` and `123-456` are copied as `123456`.
- A short popup confirms the copy. It is not kept in the notification
  history.

## Turn it on

Add these lines to `~/.config/blueferry/local.env`:

```bash
BLUEFERRY_OTP_AUTOCOPY=true
# Optional: clear the clipboard after this many seconds (0 = keep, max 600)
BLUEFERRY_OTP_CLEAR_SECONDS=60
```

Then restart the BlueFerry user service.

You need one clipboard helper:

| Session | Helper | Package |
| --- | --- | --- |
| Wayland | `wl-copy` | wl-clipboard (2.3 or newer recommended) |
| X11 | `xclip` or `xsel` | xclip / xsel |

Check the setup:

```bash
blueferry otp-status     # is it on, which helper is used, sensitive marking
blueferry doctor         # warns about a missing helper or an old wl-clipboard
echo 'Your code is 123456' | blueferry otp-check   # dry run, copies nothing
```

## Clipboard history (Klipper and others)

With wl-clipboard 2.3 or newer the code is marked as sensitive, so Klipper
and other clipboard managers keep it out of their history. Older
wl-clipboard versions and the X11 tools can't set that mark, and the code
then stays in the manager's history even after the clear timer fired.
`blueferry otp-status` tells you which case applies.

## Clear timer

With `BLUEFERRY_OTP_CLEAR_SECONDS=N`, BlueFerry removes the code after N
seconds, but only if it is still on the clipboard. If you copied something
else in the meantime, your own copy stays. Stopping the BlueFerry backend
also removes a code it still holds.

## Limits

- **It's a heuristic.** Unusually phrased codes are missed, and a casual
  "the door code is 4711" in a chat is copied. Use `blueferry otp-check` to
  test messages from your own providers.
- **Time zones.** The iPhone often sends message times without a time zone.
  If phone and computer use different zones, codes can look older than ten
  minutes and are skipped. The debug log then shows "ignoring a message N
  seconds old".
- **Session detection.** The backend needs the graphical session in its
  environment: `WAYLAND_DISPLAY` (or exactly one `wayland-N` socket in
  `$XDG_RUNTIME_DIR`), or `DISPLAY` and `XAUTHORITY` on X11. If `wl-copy`
  fails, BlueFerry tries `xclip`/`xsel` once.
- **Compositors.** Copying in the background relies on the Wayland
  data-control protocol, which KWin and wlroots compositors provide.
  GNOME/Mutter without it is untested.
- **No toggle in the clients.** The setting lives in `local.env` only.

## Privacy

- The code is **never logged, stored, or published** on BlueFerry's D-Bus
  API. There is deliberately no command to show the last code.
- The code goes to the helper on stdin, never on the command line, so other
  local users can't read it from the process list. The helper only gets a
  small allowlisted environment.
- The popup shows the code and sender only when
  `BLUEFERRY_SHOW_NOTIFICATION_CONTENT=true`. With the notification setting
  **None**, the code is copied without a popup.
- `GetStatus` only reports whether the feature is turned on
  (`otp_autocopy`).
- While it holds the clipboard, `wl-copy` itself keeps the text in a private
  temporary file. That is wl-clipboard's behaviour.
