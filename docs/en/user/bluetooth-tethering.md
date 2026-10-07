# Internet sharing (Bluetooth tethering)

BlueFerry can use your iPhone's Personal Hotspot over Bluetooth (PAN), so
your computer gets internet through the phone it is already connected to.
No Wi-Fi hotspot setup is needed.

This feature is **experimental**. It has only been tested against simulated
BlueZ and NetworkManager services, not yet with a real iPhone.

## What it does

- You turn sharing on and off yourself. BlueFerry never starts it unless you
  ask, or unless you enable automatic tethering (see below).
- It reuses the Bluetooth link BlueFerry already keeps to your iPhone. It
  never connects or disconnects the phone itself, so messages, contacts and
  notifications keep working while you share.
- With NetworkManager, NetworkManager gets the address and DNS for you.
  Without it, BlueFerry only brings up the link and you run a DHCP client.

```mermaid
flowchart LR
    U["You: switch or<br/>blueferry tether on"] --> D[BlueFerry daemon]
    D -->|NetworkManager running| NM[Activate PAN profile]
    D -->|no NetworkManager| BZ["BlueZ Network1<br/>Connect(nap)"]
    NM --> I[bnep0 with address]
    BZ --> M["bnep0 without address<br/>(run your own DHCP client)"]
```

## How to use it

1. On the iPhone, open **Settings → Personal Hotspot** and turn on **Allow
   Others to Join**. If it is off, the connection usually fails and BlueFerry
   tells you to turn it on.
2. Make sure BlueFerry is connected to the iPhone as usual.
3. Turn sharing on:
   - KDE client: **iPhone Settings → Internet Sharing → Share iPhone
     Internet**.
   - Command line: `blueferry tether on`.
4. Turn it off the same way, or with `blueferry tether off`.

`blueferry tether` (or `blueferry tether status`) shows the current state.
`--json` prints it as JSON, and `--wait SECONDS` sets how long `on` and `off`
wait for the result (default 60, `0` returns at once). Exit status: `0`
reached (or accepted with `--wait 0`), `1` not reached, `2` rejected.

### With NetworkManager

BlueFerry asks NetworkManager to activate the phone's Bluetooth network
profile. If one already exists, for example because you connected through
the Plasma network applet before, BlueFerry uses it exactly as it is and
never changes or deletes it. Only if there is none does BlueFerry create
"BlueFerry iPhone hotspot": visible only to your user and with autoconnect
off.

If NetworkManager reports a permission error, polkit probably does not see
the BlueFerry daemon as part of your active desktop session.

### Without NetworkManager

BlueFerry brings up only the Bluetooth link and shows the interface name,
usually `bnep0`. Run your own DHCP client on it, for example
`sudo dhcpcd bnep0`. BlueFerry never runs privileged commands. This link
belongs to the daemon's D-Bus connection, so it ends when the daemon stops or
restarts.

## Settings

Both settings are optional and go into `~/.config/blueferry/local.env`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BLUEFERRY_TETHER_AUTOCONNECT` | `false` | Tether automatically once messages are connected. `blueferry tether off`, or turning it off in the network applet, pauses this until the next explicit `on`. |
| `BLUEFERRY_TETHER_BACKEND` | `auto` | `auto` prefers NetworkManager, `networkmanager` forces it, `bluez` forces the link-only mode. |

Restart the daemon after changing `local.env`.

## Requirements

- Kernel with Bluetooth BNEP support (`CONFIG_BT_BNEP`).
- BlueZ with its network plugin.
- For the NetworkManager path: NetworkManager built with Bluetooth support.

## Limits

- Not yet verified with a real iPhone. In particular it is open whether iOS
  accepts the connection from a computer paired by BlueFerry, and whether
  messages stay healthy while sharing.
- If the iPhone did not advertise its network service when you paired, the
  state shows `not-supported` until BlueZ refreshes the phone's services.
- An existing network profile is used as configured. If it has autoconnect
  on, NetworkManager may start sharing on its own; BlueFerry does not change
  profiles it did not create.
- "Personal Hotspot is off" is a best guess: BlueZ reports a generic failure,
  so the message is worded carefully.
- Only the command line and the KDE client have controls. The GTK, Quickshell
  and terminal clients do not.

## Changes even if you never use it

- The daemon offers a `Tether1` D-Bus interface and watches the phone's
  Bluetooth network state (read-only).
- Sharing started elsewhere, for example from the Plasma network applet, is
  shown as active in BlueFerry and can be turned off there.
- While a sharing link exists, BlueFerry skips its last-resort Bluetooth
  adapter power cycle so it does not cut your connection. With an unknown
  interface this waits at most 10 minutes.

## Privacy

- The change signal carries no data. Clients ask for the state, which holds
  only the state, interface name, backend name, whether it was started
  elsewhere, an error code and two flags.
- No IP address, MAC address or device name is shown, broadcast or logged.
  Logs contain only error names, error codes and NetworkManager reason
  numbers.
- The profile BlueFerry creates has a neutral name and is visible only to
  your user.
