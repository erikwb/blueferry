# iPhone sound on this computer

BlueFerry can move the iPhone's media playback (music, podcasts, videos) to
this computer's speakers and back to the iPhone, from a switch or the command
line. It does not restart PipeWire; it asks BlueZ to open or close the
iPhone's Bluetooth audio stream (A2DP).

The switch is **off by default**: out of the box BlueFerry keeps calls and
music on the iPhone.

## Requirements

- `BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE=false` in
  `~/.config/blueferry/local.env`, then restart the backend. This lets this
  computer keep the Bluetooth audio sink role; with the default `true`,
  BlueFerry's WirePlumber fragment removes that role and the switch reports
  why it is unavailable.
- The iPhone connected over Bluetooth Classic (the connection that carries
  messages).
- Optional: [media control](media-control.md). With it, BlueFerry can resume
  playback after handing the sound back and can tell where the sound actually
  plays.

## Use it

In the Qt, GTK or Quickshell client, open the iPhone settings and use
**iPhone Sound**: on plays the iPhone's sound here, off sends it back. In the
terminal client press `a`. From a terminal:

```bash
blueferry audio            # where the sound goes and where it plays
blueferry audio pc         # play the iPhone's sound on this computer
blueferry audio phone      # back to the iPhone
```

The switch is an action, not a saved preference. It follows what BlueZ
reports, so it also moves when you change the output on the iPhone.

## What to expect

- **Moving the sound here** opens the stream. The iPhone usually picks this
  computer as its output at once; what plays is still up to the iPhone.
- **Moving it back** closes the stream. iOS pauses playback when its
  Bluetooth output goes away, as with unplugged headphones. If something was
  playing and media control is on, BlueFerry sends Play once after the
  switch.
- **AirPlay menu on the iPhone:** if you pick the iPhone there, iOS keeps the
  stream to this computer open but plays on its own speaker. With media
  control on, the clients then say *The iPhone is playing on its own speaker
  (chosen on the iPhone)* and `blueferry audio` prints the same.

## Privacy

The switch and its status carry no track or app data: only the route
(`pc`, `phone`, `unavailable`), a reason code and where the sound plays
(`pc`, `phone`, `none`, `unknown`).

## Troubleshooting

- *Phone audio switching is unavailable. BlueFerry keeps phone audio on the
  iPhone.* Set `BLUEFERRY_KEEP_PHONE_AUDIO_ON_PHONE=false` and restart the
  backend.
- *Bluetooth is still changing the iPhone's audio connection.* bluetoothd is
  busy with an earlier change; try again after a few seconds.
- The switch says this computer, but nothing is heard: check the iPhone's
  AirPlay menu and the volume of the new Bluetooth output in your desktop's
  sound settings.
