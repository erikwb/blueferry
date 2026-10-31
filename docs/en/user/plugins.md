# Plugins and companion tools

BlueFerry stays a messaging bridge. Everything else runs as a **plugin**,
a separate process on your session bus, or is a **companion tool**, a
program BlueFerry only starts for you. None of it runs inside the
background service, and nothing is installed by default.

## Companion tools

The **Tools** section of the KDE client's phone card and `blueferry tools`
start three programs BlueFerry does not ship. Each is greyed out, with what
to install, until its tool is there.

| Entry | Needs | What it does |
| --- | --- | --- |
| Mirror iPhone screen | [UxPlay](https://github.com/FDH2/UxPlay) | Starts an AirPlay receiver; on the iPhone open Control Center › Screen Mirroring and pick this computer. The same entry stops it. A `uxplay.desktop` entry with your own options is used when present. |
| Send a file (LocalSend) | LocalSend (Flathub or `localsend`) | Opens LocalSend. Hidden while the LocalSend plugin is enabled. |
| iPhone photos (USB) | `ifuse`, libimobiledevice, a USB cable | Mounts the camera roll in a private folder and opens it; **Eject** unmounts it. If the iPhone asks, confirm "Trust This Computer". |

If a plugin replaces UxPlay, you can still stop a receiver BlueFerry already
started. **Stop mirroring** stays visible until it exits; new starts are
blocked while the replacement is enabled.

```bash
blueferry tools            # which tools are ready
blueferry tools mirror
blueferry tools photos
blueferry tools eject
```

## Installing a plugin

Open **Settings › Plugins** in the KDE client. The store lists curated
plugins; **Install** shows the source, the version, the features it provides
(its capabilities) and the command it runs (the manifest's `Exec`), and
asks first. You can also paste any https Git URL. From a terminal:

```bash
blueferry plugins available
blueferry plugins install https://github.com/joshii-h/blueferry-plugin-immich
blueferry plugins list
```

Each plugin gets its own Python environment under
`~/.local/share/blueferry/plugins`; its dependencies come from the network.
Plugins, their dependencies, and their installation code run with your user
permissions. They are not sandboxed: capabilities describe features and do
not restrict access to your files, network, or session services. Only install
plugins and updates from sources you trust. Nothing asks for root.
**Update**, **Remove** and the switch next to a plugin (or
`blueferry plugins update`, `remove`, `disable`) stop a running plugin process
BlueFerry installed.
Disabling also stops BlueFerry from calling the plugin at once.

## Setting a plugin up

**Settings** next to a plugin opens a form made from what the plugin asks
for: fields with examples and "More information" links (some plugins name
them, e.g. "Which port?"), **Test connection**, and for some services a
browser sign-in. Some plugins add their own buttons above the fields, such
as **Set up iPhone**; they use the saved settings, so save your changes
first. Save only becomes available once the required fields are valid.
Passwords and keys are never shown again, only typed anew. BlueFerry stores
no plugin secrets: they go into the plugin, which keeps them itself (the
published plugins use the desktop keyring).

```bash
blueferry plugins config immich --set url=https://photos.example.org --secret api_key --test
blueferry plugins config ID                  # settings and own buttons
blueferry plugins config ID --action ACTION  # press one of them
```

## What plugins add

- **From Plugins** on the phone card: for example the next calendar entry
  or a running transfer, with buttons. A plugin that fails shows a dimmed
  line.
- **Send to…** at the end of that section: hands files to a plugin target
  such as "iPhone (LocalSend)"; some card items take files directly (drop
  them on the item).
- **Card items and Send to** on the command line: `blueferry cards` and
  `blueferry send`.
- **Popups** that follow your notification settings.
- **Photos**: a plugin with photos, such as the Immich plugin, lists the
  newest ones and fills the **Photos** tab.

```bash
blueferry cards
blueferry send photo.jpg --to localsend
blueferry photos recent
```

## Logs and privacy

Plugins write `~/.local/state/blueferry/plugins/<id>.log` (only you can read
it, secrets masked); **Show Log** or `blueferry plugins log ID` opens it.
BlueFerry shows plugin text as plain text and only lets a plugin open web
links or files in its own cache. A plugin's popups follow your notification
settings: none with **None**, only the plugin's name while notification
content is hidden. Their text never travels on a D-Bus signal other programs
could read: BlueFerry fetches it from the plugin it checked.

Writing a plugin: see [PLUGINS.md](https://github.com/erikwb/blueferry/blob/main/PLUGINS.md).
