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
    A[New message from the iPhone] --> B{Just arrived?<br/>incoming, unread,<br/>at most 5 min old,<br/>not a contact or group}
    B -- no --> X[Ignored]
    B -- yes --> C{Number tied to<br/>a code word?}
    C -- no --> X
    C -- yes --> M[5 s for group metadata<br/>check phone time and read flag<br/>wait for clipboard capabilities]
    M --> D[wl-copy / xclip / xsel<br/>code fed on stdin]
    D --> E[Line added to the message popup:<br/>'Verification code copied']
    D --> F{Clear timer set?}
    F -- yes, BlueFerry still owns selection --> G[Own clipboard source released]
```

- Only unread messages that have **just arrived** count. Sent messages,
  history, messages that were already read, and messages without a time from
  the last five minutes are ignored. Future timestamps are ignored too.
  BlueFerry refreshes the time and current read flag for that exact message
  in a bounded inbox listing, never using another message's time or the
  desktop arrival time. Read-state changes while copying is pending also
  cancel the copy. Recent messages can qualify even
  when they were received before the backend started. If the lookup fails
  or the message is absent from the latest 20 entries, nothing is copied.
- Codes come from services, so messages from **saved contacts** and **group
  conversations** are ignored when group metadata is available. Candidates
  wait five seconds for live Apple Messages notifications to supply it.
  At most three eligibility checks and three copies per minute are allowed;
  failed checks also consume the budget so bursts cannot flood phone work.
- A number counts as a code only when it is tied to a code word:
  - an OTP-specific word near it: "verification code",
    "Bestätigungscode", "Sicherheitscode", "mTAN", "OTP", "Steam Guard code",
    "Bestätigungsnummer", "code de vérification", ...;
  - "code" followed by the number: "code: 123456", "Code lautet 123456";
  - "123456 is your ... code";
  - "code" immediately followed by the number, as in "Telegram code 58291";
  - "enter 123456" or "geben Sie 123456 ein" in a sentence about
    authentication, including resetting a password.

  Plain PINs and passwords require authentication context. Door and Wi-Fi
  codes are excluded, as are numbers followed by units such as steps or
  messages. A code noun in another sentence never binds an unrelated number.

  Words like "verify", "one-time" or "Einmal" alone never pick a number,
  so "Verify your email to get 5000 points" or "Einmalzahlung von 1500"
  copy nothing. Promotions and bookings need an OTP-specific word. Amounts,
  dates, times, phone numbers, and order, tracking or invoice numbers are
  skipped.
- `G-123456` and `123-456` are copied as `123456`.
- The message popup gets one extra line saying the code was copied. If
  there is no message popup (for example with notifications limited to
  contacts), a short popup of its own confirms the copy; it is not kept in
  the notification history.

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

With wl-clipboard 2.3 or newer the code is marked as sensitive. Clipboard
managers that honor the hint keep it out of their history. The first
copy waits for the capability probe; a failed probe skips pending copies
and can be retried for the next message. Older
wl-clipboard versions and the X11 tools can't set that mark, and the code
then stays in the manager's history even after the clear timer fired.
`blueferry otp-status` tells you which case applies.

## Clear timer

With `BLUEFERRY_OTP_CLEAR_SECONDS=N`, BlueFerry releases its clipboard source
after N seconds. If you copied something else, your selection stays.
Stopping the backend releases its source even with the timer disabled.

Clipboard persistence tools such as wl-clip-persist can take the selection
over immediately. BlueFerry cannot clear those copies safely: a clipboard
read followed by a global clear could erase text you copy between the two
operations. Cleanup therefore only stops BlueFerry's own helper, never
reads the clipboard back and never invokes a global clear. Copies retained
by another program remain under that program's control, including at
shutdown. Clearing the selection cannot remove saved clipboard history.
The [Wayland data-control protocol](https://wayland.app/protocols/ext-data-control-v1)
provides source ownership, but no atomic check-and-clear of another source.

## Limits

- **It's a heuristic.** Unusually phrased codes can be missed and unrelated
  text can still resemble an authentication message.
  Use `blueferry otp-check` to test messages from your own providers; it
  checks only the text, not the sender rules.
- **Group conversations.** The iPhone's message push does not say whether a
  message belongs to a group. Live Apple Messages notifications supply the
  evidence, with a five-second grace period and another check immediately
  before copying. Missing or later metadata can leave a group unrecognized;
  saved contacts are skipped regardless.
- **Time zones.** The iPhone often sends message times without a time zone.
  If phone and computer use different zones, codes can look older than five
  minutes and are skipped. The debug log then shows "ignoring a message N
  seconds old".
- **Session detection.** The backend needs the graphical session in its
  environment: `WAYLAND_DISPLAY` (or exactly one `wayland-N` socket in
  `$XDG_RUNTIME_DIR`), or `DISPLAY` and `XAUTHORITY` on X11. If `wl-copy`
  fails, BlueFerry tries `xclip`/`xsel` once.
- **X11 and the systemd unit.** The unit sets `PrivateTmp=true`, which hides
  `/tmp`. X clients still reach the server through its abstract socket, but
  an `XAUTHORITY` file under `/tmp` is invisible; the log says so when that
  is why `xclip`/`xsel` failed.
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
  `BLUEFERRY_SHOW_NOTIFICATION_CONTENT=true`. Then the desktop's
  notification server receives them, as it does for every message popup.
  The line added to the message popup never repeats the code. With the
  notification setting **None**, the code is copied without a popup.
- `GetStatus` only reports whether the feature is turned on
  (`otp_autocopy`).
- While it holds the clipboard, `wl-copy` itself keeps the text in a private
  temporary file. That is wl-clipboard's behaviour.
