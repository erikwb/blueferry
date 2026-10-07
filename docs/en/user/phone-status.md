# Phone battery, signal, and network (optional)

With the optional [phone calls](calls.md) integration enabled, BlueFerry also
shows the iPhone's battery level, signal strength, and network (operator)
name. They come from the standard Hands-Free indicators that oFono reads from
the phone. Without calls enabled, nothing changes and nothing is shown.

## What it does

```mermaid
flowchart LR
    P[iPhone] -- HFP indicators --> O[oFono modem]
    O -- BatteryChargeLevel, Strength, Name --> B[BlueFerry daemon]
    B -- StatusChanged, no values --> C[Clients]
    C -- GetStatus --> B
```

- **Qt**: a small battery and signal indicator next to "Conversations";
  hover for the network name.
- **Terminal client, Quickshell header, GTK status page**: battery and
  signal are added to the connection line.
- **CLI**:

  ```bash
  blueferry phone-status          # Battery: about 60 % / Signal: 80 % / Network: …
  blueferry phone-status --json
  ```

- **Optional low-battery warning**: one desktop notification when the
  battery reaches a threshold.

The values appear as soon as the iPhone's hands-free modem is powered, even
if call control is still waiting for the modem to go online.

## How to enable it

1. Set up and enable [phone calls](calls.md) (`BLUEFERRY_CALLS_ENABLED=true`).
   Battery, signal, and network need nothing else.
2. For the low-battery warning, add to `~/.config/blueferry/local.env` and
   restart the backend:

   ```bash
   BLUEFERRY_PHONE_BATTERY_NOTIFY=true         # default off
   BLUEFERRY_PHONE_BATTERY_LOW_PERCENT=20      # 0-80, default 20
   ```

## Limits

- Coarse: iPhones report the battery to hands-free devices in six steps, so
  BlueFerry shows 0, 20, 40, 60, 80, or 100 %. The signal also moves in
  20 % steps. There is no charging indicator.
- The operator name is what the phone reports over HFP (up to 16
  characters) and is empty while the phone has no network.
- The warning fires again only after the phone has charged at least 20 %
  above the threshold, and once more after a restart of BlueFerry if the
  phone is still low.
- With a dual-SIM phone, only the default voice line is reported.

## Privacy

- The values are only returned by the authenticated `GetStatus` method.
  Clients are told about changes with the argument-free `StatusChanged`
  signal; no value is ever broadcast.
- oFono asks the phone for its own number when the battery is first read.
  BlueFerry discards that number and never stores, logs, or returns it.
- Logs never contain the levels or the operator name.
