# iPhone sound on this computer

BlueFerry can move the iPhone's media playback (music, podcasts, videos) to
this computer's speakers and back to the iPhone, from a switch or the command
line. It does not restart PipeWire; it asks BlueZ to open or close the
iPhone's Bluetooth audio stream (A2DP).

The switch reflects the current Bluetooth audio route. Until you choose a
route in BlueFerry, routing follows the desktop and iPhone settings.
BlueFerry keeps the adapter's audio roles available for pairing.

## Requirements

- The desktop audio server must offer the Bluetooth audio sink role (`a2dp_sink`).
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
blueferry audio auto       # forget the choice; follow desktop routing
```

PC and phone choices are remembered for the selected iPhone and restored
after reconnecting. Choosing a different phone does not inherit this choice.
The switch still follows the actual route, including changes made on the
iPhone. `blueferry audio` also prints the remembered choice.

With WirePlumber 0.5 or newer, the first choice installs a rule that disables
automatic profile connections for this phone only. Loading or removing the
rule restarts WirePlumber and can briefly interrupt other Bluetooth audio.
Switching between PC and phone afterwards does not reload it. Your desktop's
other audio rules and the adapter's advertised roles stay intact.

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
- **Reconnecting:** BlueFerry waits for pairing to finish and for messaging
  and, when enabled, ANCS to be ready. It makes at most one automatic route
  change per connection, using the same playback-resume behavior as a manual
  switch: playback that was already paused is left paused.
  It does not repeatedly fight an audio manager or the phone reopening a
  stream. Without supported WirePlumber, the remembered route still works,
  but BlueFerry cannot suppress the audio server's own connection attempts.
- **Calls:** this preference controls media (A2DP). The separate phone-calls
  opt-in and the desktop's HFP roles are unchanged.

## Privacy

The switch and its status carry no track or app data: only the route
(`pc`, `phone`, `unavailable`), a reason code and where the sound plays
(`pc`, `phone`, `none`, `unknown`).
The remembered choice and phone address are stored locally in `settings.json`.
The WirePlumber rule is `zz-blueferry-phone-audio.conf`; `audio auto` removes it.

## Troubleshooting

- *Bluetooth is still changing the iPhone's audio connection.* bluetoothd is
  busy with an earlier change; try again after a few seconds.
- The switch says this computer, but nothing is heard: check the iPhone's
  AirPlay menu and the volume of the new Bluetooth output in your desktop's
  sound settings.
