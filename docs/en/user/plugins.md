# Plugins

BlueFerry stays a messaging bridge. Everything else runs as a **plugin**,
a separate process on your session bus. None of it runs inside the
background service, and nothing is installed by default.

## Installing a plugin

The store lists curated plugins. An install shows the source, the version,
what the plugin may do (its capabilities) and the command it runs (the
manifest's `Exec`), and asks first. Any https Git URL works too:

```bash
blueferry plugins available
blueferry plugins install https://github.com/joshii-h/blueferry-plugin-immich
blueferry plugins list
```

Each plugin gets its own Python environment under
`~/.local/share/blueferry/plugins`; its dependencies come from the network.
Everything runs as you, nothing asks for root. `blueferry plugins update`,
`remove` and `disable` stop a running plugin process BlueFerry installed.
Disabling also stops BlueFerry from calling the plugin at once.

## Setting a plugin up

`blueferry plugins config ID` shows a plugin's settings; `--set`
changes them, `--secret` asks for a password or key without echo, `--test`
checks them and `--login` starts a browser sign-in where the plugin offers
one. BlueFerry stores no plugin secrets: they go into the plugin,
which keeps them itself (the published plugins use the desktop keyring).

```bash
blueferry plugins config immich --set url=https://photos.example.org --secret api_key --test
```

## Logs and privacy

Plugins write `~/.local/state/blueferry/plugins/<id>.log` (only you can read
it, secrets masked); `blueferry plugins log ID` opens it.
BlueFerry shows plugin text as plain text and only lets a plugin open web
links or files in its own cache.

Writing a plugin: see [PLUGINS.md](https://github.com/erikwb/blueferry/blob/main/PLUGINS.md).
