# BlueFerry plugins

Plugins add features that are not about the iPhone link itself, for example a
photo gallery backed by a self-hosted service. The design is deliberately
small:

- A plugin is its **own process** with its own session-bus name. The daemon
  never loads plugin code and does not know about plugins.
- **Clients discover plugins themselves** by reading manifest files, then talk
  to the plugin over the user's session bus.
- The contract lives in `blueferry.plugin_api`, a self-contained package that
  imports nothing else from BlueFerry. Plugins import only from it. It ships
  inside BlueFerry and as its own distribution `blueferry-plugin-api`
  (`plugin-api/` in this repository; `src/blueferry/plugin_api` is a symlink
  to it).

Nothing in this document has been tested against real hardware; the plugin
path does not touch Bluetooth at all.

## Discovery

Clients read `*.plugin` files from, in this order:

1. `$XDG_DATA_HOME/blueferry/plugins/` (default `~/.local/share/blueferry/plugins/`)
2. each `$XDG_DATA_DIRS` entry + `/blueferry/plugins/` (default `/usr/local/share`, `/usr/share`)

The first manifest with a given `Id` wins, so a user manifest shadows a system
one. Discovery only parses files; it never starts anything. A manifest is
ignored, with a reason the clients can show (`blueferry plugins list`), when
it is malformed, larger than 16 KiB, not owned by the user or root, writable
by group or others, or written for an unsupported `ApiVersion`.
Distinct IDs must also have distinct derived D-Bus names: `io.example.a-b`
and `io.example.a_b` cannot coexist. Discovery ignores both conflicting IDs
and reports why; installing a conflicting ID fails before running plugin code.

## Manifest

Desktop-entry syntax, group `[BlueFerry Plugin]`:

| Key | Required | Meaning |
| --- | --- | --- |
| `Id` | yes | Reverse-DNS id, e.g. `io.weirdware.blueferry.immich_photos`. |
| `Name` | yes | Display name (plain text, ≤ 80 characters). |
| `Version` | yes | The plugin's own version. |
| `ApiVersion` | yes | Plugin contract version it implements: `MAJOR` or `MAJOR.MINOR` (currently `1.5`). Clients ignore unknown major versions, and a manifest that needs a newer minor than the running BlueFerry knows (`1.5` on a 1.4 BlueFerry) is ignored with a message. |
| `MinBlueFerry` | yes | Oldest BlueFerry release the plugin works with. |
| `Capabilities` | yes | `;`-separated list. Known: `photos`, `card`, `share`, `notify` (1.2); reserved: `conversations`. Unknown entries are dropped; a manifest with none left is ignored. |
| `Homepage`, `Source` | at least one | `https://` URLs. |
| `Exec` | yes | Command line that serves the plugin on the bus (used for its D-Bus service file). |
| `Cli` | no | Command line for the plugin's own CLI; `blueferry plugins <alias> …` forwards to it. |
| `Alias` | no | Short lowercase name for the CLI, e.g. `immich`. |
| `ConfigTest` | no | `true`: the plugin implements `TestConfig` and the form shows "Test connection" (1.3). Needs `[Config …]` settings. |
| `ConfigLogin` | no | A browser sign-in provider, today `nextcloud`: the form shows "Sign in with Nextcloud" (1.3). Clients ignore providers they do not know. Needs `[Config …]` settings. |
| `ReplacesTools` | no | `;`-separated companion tools the plugin stands in for (1.4): `localsend` (the LocalSend app under Tools) or `uxplay`. While the plugin is enabled the clients hide that tool, so the app and the plugin never fight over a port. Unknown names are ignored. |

### Settings (`[Config <key>]`, ApiVersion 1.1)

Optional. Each group describes one setting; clients build a form from them in
file order. `<key>` is lowercase letters, digits and `_` (at most 32
characters, at most 24 settings).

| Key | Required | Meaning |
| --- | --- | --- |
| `Label` | yes | Form label (plain text, ≤ 80 characters). |
| `Type` | yes | `string`, `url` (https, http only for localhost), `secret`, `bool`, `int` or `choice`. |
| `Required` | no | `true` or `false` (default). |
| `Default` | no | Value of an unset field. Not allowed for `secret`. |
| `Help` | no | One line under the field (≤ 300 characters). |
| `Choices` | for `choice` | `;`-separated simple words. |
| `Min`, `Max` | no | Bounds for `int` (only there). |

```
[Config url]
Label=Server URL
Type=url
Required=true

[Config api_key]
Label=API key
Type=secret
Required=true
```

A malformed settings group makes clients ignore the whole manifest, like any
other manifest error.

### Guided settings (ApiVersion 1.3)

Optional keys that help people fill in a form without knowing the service.
A 1.2 BlueFerry ignores them, so a plugin may use them and still declare
`ApiVersion=1.2`; declare `1.3` when it also implements `TestConfig` or
`ConfigLogin`. Every value is plain text on one line (no control or bidi
characters).

| Key in `[Config <key>]` | Meaning |
| --- | --- |
| `Placeholder` | Sample value shown greyed out in the empty field, e.g. `https://cloud.example.com` (≤ 120). For a stored secret the client shows "Stored — leave empty to keep" instead. |
| `Example` | One short example under the field, shown as "Example: …" (≤ 160). |
| `HelpUrl` | "Where do I find this?" link under the field: an `https://` URL (≤ 300), opened in the browser through the desktop launcher. |
| `Group` | Section name, `[a-z][a-z0-9_]{0,31}`. Built in without a declaration: `account` ("Account"), `options` ("Options"), `advanced` ("Advanced", folded). Others need a `[ConfigGroup <name>]` section. |
| `Advanced` | `true`: short for `Group=advanced`. |
| `Pattern` | Regular expression the whole value must match (`string`, `url`, `secret`; ≤ 200). Only plain syntax: no `(?…)` groups, no backreferences, no repeated group holding a quantifier (`(a+)+`, even through nested groups). Optional groups remain supported. Counted repeats are at most 4096, the maximum field length. The SDK uses `regex` in Version 0 mode with a 20 ms matching deadline; exceeding it is a validation error. |
| `ErrorText` | The message when `Pattern`, `Min` or `Max` fails, e.g. "Use 10 to 100." (≤ 160). Without it clients say "does not have the expected format" or "must be at least …". |
| `ShowIf` | `<key>=<value>`: show the field only while an *earlier*, non-secret setting has this value (`true`/`false` for a `bool`, one of the choices for a `choice`). A hidden field is not required, and a field that depends on a hidden field is hidden too. |

`[ConfigGroup <name>]` sections declare or relabel a section:

| Key | Meaning |
| --- | --- |
| `Label` | Section heading (≤ 40). Optional for the built-in names. |
| `Help` | One line under the heading (≤ 300). |
| `Collapsed` | `true` folds the section until the user opens it (default: `true` for `advanced`, `false` otherwise). A folded section with an error opens itself. |

At most 8 sections. Fields without a group come first, `advanced` comes last,
the others in the order of their first field.

The client pre-checks every edit (type, `Required`, `Pattern`, `Min`/`Max`
of the visible fields) and keeps **Save** disabled until the required fields
are filled and valid. The pre-check only helps: the plugin validates again in
`SetConfig` and its answer decides.

Complete example (a WebDAV plugin with a Nextcloud sign-in):

```
[BlueFerry Plugin]
Id=io.example.webdav
Name=WebDAV files
Version=0.3.0
ApiVersion=1.3
MinBlueFerry=0.9
Capabilities=share;card;
Source=https://git.example.org/blueferry-plugin-webdav
Exec=blueferry-plugin-webdav serve
Alias=webdav
ConfigTest=true
ConfigLogin=nextcloud

[ConfigGroup account]
Label=Account
Help=Sign in with Nextcloud, or enter an app password by hand.

[ConfigGroup pictures]
Label=Pictures
Collapsed=false

[Config url]
Label=Server URL
Type=url
Required=true
Group=account
Placeholder=https://cloud.example.com
Example=https://cloud.example.com/nextcloud
Help=The address you open in the browser to reach your files.

[Config user]
Label=User name
Type=string
Required=true
Group=account
Placeholder=anna

[Config app_password]
Label=App password
Type=secret
Required=true
Group=account
Pattern=[A-Za-z0-9-]{20,80}
ErrorText=Copy the whole app password, including the dashes.
Help=An app password, not your login password: it can be revoked on its own.
HelpUrl=https://docs.nextcloud.com/server/latest/user_manual/en/session_management.html

[Config folder]
Label=Folder
Type=string
Default=/Ablage
Group=options
Pattern=/.*
ErrorText=Start the folder with a /, e.g. /Ablage.
Help=Where files sent from this computer land.

[Config resize]
Label=Make pictures smaller
Type=bool
Group=pictures
Help=Saves space and upload time; the original stays on this computer.

[Config max_side]
Label=Longest side (pixels)
Type=int
Min=320
Max=8000
Default=2048
ErrorText=Use 320 to 8000 pixels.
Group=pictures
ShowIf=resize=true

[Config timeout]
Label=Timeout (seconds)
Type=int
Min=5
Max=300
Default=30
Advanced=true
```

#### Writing good settings text

- **Label**: a short noun, 1 to 3 words, no colon or trailing period
  ("Server URL", "App password"). Name the thing the user has, not the
  variable ("Server URL", not "base_url").
- **Help** answers *why* or *where from* in one sentence: "Create one in
  Immich under Account Settings > API Keys." Do not repeat the label.
  Say what an empty optional field does ("Leave empty for all cameras.").
- **Placeholder** is a realistic value in the expected format, not an
  instruction ("https://photos.example.com", not "Enter URL").
- **Example** shows a variant the placeholder does not, e.g. a sub-path or
  a local address: "https://cloud.example.com/nextcloud or
  http://localhost:8080".
- **HelpUrl** points to the page that shows where to find the value, ideally
  the service's own documentation; not to your README's top.
- **ErrorText** says how to fix it, not only what is wrong: "Use 320 to 8000
  pixels." instead of "invalid".
- **Groups**: put what is needed to connect into `account`, everyday
  choices into `options`, and tuning (timeouts, paths, limits) into
  `advanced`. A form with three fields needs no groups.
- Prefer `ConfigLogin` over asking for a token by hand when the service has
  a browser flow, and offer `ConfigTest` whenever a server is involved: a
  green "Connected as anna" is the best help text.
- Messages from `TestConfig` and the sign-in are one line, in the user's
  terms ("The server answered, but the API key was refused."), and never
  contain the secret.

## Bus contract (ApiVersion 1)

- Bus name: `io.weirdware.BlueFerry.Plugin.<Id>` with `-` replaced by `_`.
- Object path: `/io/weirdware/BlueFerry/Plugin`.
- Activation: the plugin installs a D-Bus service file for its bus name
  (`$XDG_DATA_HOME/dbus-1/services/` or `/usr/share/dbus-1/services/`), so the
  first call starts it. It may exit when idle (the base class does after 10
  minutes) and is started again on the next call.

`io.weirdware.BlueFerry.Plugin1` (every plugin):

| Member | Signature | Meaning |
| --- | --- | --- |
| `GetInfo()` | `→ s` | JSON `{id, name, version, api_version, api_minor, capabilities}`. |
| `Status()` | `→ s` | JSON `{state, detail?, server?}`; `state` is `ok`, `unconfigured`, `error` or `busy`. |
| `GetConfig()` | `→ s` | Since 1.1, plugins with settings. JSON `{values: {key: value}}` for every manifest setting. A `secret` is `"********"` when stored and `""` when not; its value never leaves the plugin. |
| `SetConfig(s json)` | `→ s` | Since 1.1. A JSON object with the settings to change (at most 16 KiB). The plugin validates it against its schema and answers `{ok: true}` or `{ok: false, errors: {key: reason}}` (`""` for the whole form). A `secret` that is missing, empty or the mask keeps its stored value; unknown keys are errors. |
| `TestConfig(s json)` | `→ s` | Since 1.3, with `ConfigTest=true`. Same argument as `SetConfig`, but nothing is stored: the plugin validates the values and checks them against its server. Answers `{ok, message, errors?}`; `message` is one line such as "Connected as anna to Nextcloud 31". Clients wait up to 60 s. |
| `ConfigLogin(s provider, s values)` | `→ s` | Since 1.3, with `ConfigLogin=<provider>`. Starts a browser sign-in. `values` is a JSON object with the form's typed **non-secret** values (not saved yet), so the plugin knows the server. Answers `{state: "open", login_id, open_uri, message?}`, `{state: "done", message}` (signed in without a browser) or `{state: "error", message}`. `login_id` matches `[A-Za-z0-9_.-]{1,64}`, `open_uri` is https (http only on localhost); clients refuse anything else. |
| `ConfigLoginStatus(s login_id)` | `→ s` | `{state, message?}` with `state` `pending`, `done`, `error`, `expired` or `cancelled`. Clients poll every 2 s for at most 20 minutes. On `done` the plugin has already stored the credentials; `message` says "Connected as …". |
| `ConfigLoginCancel(s login_id)` | `→ s` | The user cancelled or closed the form; stop polling the provider. Answers `{ok: true}`. |

`io.weirdware.BlueFerry.Photos1` (capability `photos`):

| Member | Signature | Meaning |
| --- | --- | --- |
| `ListRecent(u limit)` | `→ s` | JSON array, newest first, at most 200: `{id, taken_at, type, thumbnail, original?}`. `type` is `image`, `video` or `other`; `thumbnail`/`original` are local paths or empty. |
| `FetchOriginal(s id)` | `→ s` | Download (or reuse) the original and return its local path. |
| `Changed()` | signal | Content-free: something changed, call `ListRecent` again. |

### Generic UI surfaces (ApiVersion 1.2)

Plugins never ship UI code. With these capabilities they describe what to
show, and BlueFerry renders it in the Qt client and the CLI (popups come
from the daemon). All members live on **`Plugin1`** at the plugin's object
path; the manifest's capabilities decide which ones BlueFerry calls.
`blueferry.plugin_api` exports the names (`SURFACES_INTERFACE`,
`METHOD_GET_CARD_ITEMS`, `METHOD_INVOKE_ACTION`, `METHOD_SHARE_TARGETS`,
`METHOD_SEND_FILES`, `METHOD_GET_PENDING_NOTIFICATION`,
`SIGNAL_CARD_CHANGED`, `SIGNAL_NOTIFY_CHANGED`).

Every string is untrusted and shown as one line of plain text, cut to
title 80, subtitle 160 and label 40 characters. Ids match
`[A-Za-z0-9_.-]{1,64}` (no `:`; clients write `PLUGIN:ITEM:ACTION`), icons
are freedesktop icon names (no paths).

| Member | Capability | Signature | Meaning |
| --- | --- | --- | --- |
| `GetCardItems()` | `card` | `→ s` | JSON `{"items": [{"id", "icon", "title", "subtitle"?, "actions": [{"id", "label", "icon"?, "kind": "button"\|"primary"}]}]}`; at most 8 items with 3 actions each, more are dropped. |
| `CardChanged()` | `card` | signal | Content-free: BlueFerry calls `GetCardItems` again (coalesced). |
| `InvokeAction(s item_id, s action_id, s args_json)` | `card`, `notify` | `→ s` | JSON `{"ok", "message"?, "open_uri"?}`. `args_json` is a JSON object (at most 4 KiB, `{}` today). For a popup button `item_id` is `"notify"`. |
| `ShareTargets()` | `share` | `→ s` | JSON `{"targets": [{"id", "label", "icon"}]}`, at most 16. |
| `SendFiles(s target_id, as paths)` | `share` | `→ s` | JSON `{"ok", "message"?, "job"?}`. Absolute paths of existing regular files, 1 to 64. Return quickly; report a long transfer as a card item (progress in the subtitle) and `CardChanged()`. |
| `NotifyChanged()` | `notify` | signal | Since 1.5. Content-free: a popup waits in the plugin. |
| `GetPendingNotification()` | `notify` | `→ s` | Since 1.5. JSON `{"notification": {"title", "body", "icon", "action_label", "action_id"} \| null, "more": bool}`: the oldest waiting popup, removed from the plugin's queue. Empty `action_label`/`action_id` means no button. Only the owner of `io.weirdware.BlueFerry` may call it; anyone else gets `AccessDenied`. |

Where it shows up:

- **Card**: the phone card's "From Plugins" section (Qt) and
  `blueferry cards [--run PLUGIN:ITEM:ACTION]`.
  A click on an item runs its `primary` action; the others are buttons. A
  plugin that crashes, times out or sends broken JSON shows a dimmed line
  with the reason instead of its items.
- **Share**: "Send to…" at the end of the card's "From Plugins" section (Qt)
  and `blueferry send FILE… [--to PLUGIN[:TARGET]] [--list]` (`PLUGIN` is the id
  or alias; without `--to` the only target is used). Whether "Send to…"
  appears comes from the manifests; `ShareTargets` (which starts the plugin)
  is called only when the user opens it.
- **Notify** (content-free since 1.5): the plugin queues the popup (at most
  8, each for at most a minute) and emits `NotifyChanged()`; the BlueFerry
  daemon checks the sender and then fetches the popup with
  `GetPendingNotification()` from that same connection, so the text never
  travels on a signal other processes can read. `NotifyService` hands a
  popup out only to the connection that currently owns the daemon's name
  `io.weirdware.BlueFerry` (it asks `GetNameOwner` asynchronously and then
  follows `NameOwnerChanged`; only adding that signal's match rule is one
  synchronous call, made once when the service goes on the bus, before the
  main loop runs); every other caller gets
  `org.freedesktop.DBus.Error.AccessDenied` and the queue stays as it was,
  so other processes on the session bus can at most see that something
  waits, never read it or take it away. While the name has no owner (the
  daemon is not running) nobody can fetch, and the popups expire in the
  plugin. The daemon reads manifests
  (it still runs no plugin code) and accepts the signal only from the owner
  of an enabled `notify` plugin's bus name running as the same user (an
  unverified sender gets at most twelve lookups a minute); it re-reads the
  disabled list on every signal. It fetches and shows at most six popups a
  minute per plugin; the rest waits in the plugin and expires there. It
  shows them through its own notification sink, so the user's policy
  applies: `none` silences plugins, `messages` and `all` show them, and
  without `BLUEFERRY_SHOW_NOTIFICATION_CONTENT` only the plugin's name
  appears. The check needs the plugin to still own its name when the
  signal arrives: emit a popup and exit at once, and the first one may be
  lost (later ones from the same connection are remembered). A click on
  the button calls `InvokeAction("notify", action_id, "{}")` on the
  plugin's bus name, which starts the plugin again if it has exited
  (D-Bus activation); an `open_uri` in the answer is opened only after the
  daemon checked that the answering owner runs as the same user.
  Plugins written for 1.2 to 1.4 emitted `Notify(s title, …)` with the text
  on the bus; BlueFerry no longer listens to it, so they show no popups
  until they move to `NotifyService` from `blueferry-plugin-api` 1.5.
- **`open_uri`** is opened only when it is `http(s)://` or a `file://`
  URI of a file or folder owned by the user in the plugin's own cache
  directory: `<cache>/<Id>/` or, with an `Alias`, `<cache>/<Alias>/`, where
  `<cache>` is `$XDG_CACHE_HOME/blueferry` or `~/.cache/blueferry`. Anything
  else is dropped.

Clients call these members from worker threads with timeouts (15 s for
`GetCardItems` and `ShareTargets`, 60 s for `InvokeAction` and
`SendFiles`), and the base service runs the hooks on its worker thread.
Before `InvokeAction` and `SendFiles` a client refuses a running plugin
owned by another user. The card asks at most eight plugins; more are
named in one dimmed line. The Qt card listens for `CardChanged()` only
from the owners of the enabled card plugins' bus names.

```python
from blueferry.plugin_api.service import CardService, NotifyService, ShareService
from blueferry.plugin_api.surfaces import Action, ActionResult, CardItem, SendResult, ShareTarget

class Calendar(CardService, NotifyService):
    def card_items(self):                       # worker thread
        return [CardItem("next", "Dentist", icon="view-calendar", subtitle="in 20 min",
                         actions=[Action("open", "Open", kind="primary")])]

    def invoke_action(self, item_id, action_id, args):   # worker thread
        return ActionResult(True, open_uri="https://calendar.example/e/1")

    def remind(self):                           # any thread
        self.emit_notify("Dentist", "in 20 minutes", "view-calendar", "Open", "open")
        self.emit_card_changed()

class Drop(ShareService):
    def share_targets(self):
        return [ShareTarget("ablage", "Ablage", "folder-cloud")]

    def send_files(self, target_id, paths):
        return SendResult(True, f"Uploading {len(paths)} files", job="upload-1")
```

`blueferry.plugin_api.testing.FakeHost` plays BlueFerry's side in tests,
with the same validation: `host.card_items()`, `host.invoke(item, action)`,
`host.share_targets()`, `host.send(target, paths)`,
`host.send_action(item, action, paths)` (1.4), `host.card_changes`,
`host.notifications` (filled through `GetPendingNotification` on every
`NotifyChanged`, as the daemon does) and `host.click(notification)`.

#### Card actions that send files (ApiVersion 1.4)

A plugin with both `card` and `share` can put "Send files…" right on an
item, e.g. on each device it found. The action carries the share target:

```json
{"id": "send", "label": "Send files…", "icon": "document-send", "kind": "primary",
 "send_to": "ls-3f2a…"}
```

`send_to` matches the id rules and names one of the plugin's
`ShareTargets`. BlueFerry then does not call `InvokeAction`: it asks for
files (the Qt client opens a file dialog,
`blueferry cards --run PLUGIN:ITEM:ACTION FILE…` takes them as arguments)
and calls `SendFiles(send_to, paths)` on the same plugin. In the Qt card
files dragged onto such an item go to the `send_to` of its first sending
action. Progress and errors show up on the card as for "Send to…". A
`send_to` on a plugin without `share` is ignored (the action is an ordinary
button). Clients that only know 1.3 ignore the key and call
`InvokeAction(item, action)`; answer that with a hint such as "Use Send to…".
In Python: `Action("send", "Send files…", "document-send", "primary",
send_to=target_id)`.

### Browser sign-in: Nextcloud Login Flow v2 (ApiVersion 1.3)

With `ConfigLogin=nextcloud` the Qt settings form shows **Sign in with
Nextcloud** next to the fields, and
`blueferry plugins config ID --set url=… --login` does it from a shell. Nobody
has to create an app password by hand:

1. The user types the server URL and presses the button. BlueFerry calls
   `ConfigLogin("nextcloud", '{"url": "https://cloud.example.com"}')`.
2. The plugin sends `POST <url>/index.php/login/v2` (with a recognisable
   `User-Agent`, e.g. "BlueFerry WebDAV") and gets `{poll: {token,
   endpoint}, login}`. It keeps `token` and `endpoint` to itself and answers
   `{state: "open", login_id: "<own id>", open_uri: "<login>"}`.
3. BlueFerry opens `open_uri` in the default browser through the desktop
   launcher, shows "Waiting for the sign-in in your browser…" and a
   **Cancel sign-in** button, and calls `ConfigLoginStatus(login_id)` every
   2 seconds.
4. The plugin polls `endpoint` with `token` (`POST`, form field `token`):
   404 means `pending`; 200 brings `{server, loginName, appPassword}`. The
   plugin checks that `server` is https (or the URL the user typed), stores
   the server, the user name and the app password (keyring), and answers
   `{state: "done", message: "Connected as <loginName>"}`. A `card` plugin
   may also emit `CardChanged()`.
5. BlueFerry shows the message, reloads the form (the password now shows as
   stored) and the plugin's status. After 20 minutes without an answer it
   reports `expired` and calls `ConfigLoginCancel`; so does closing the form.

The app password goes from Nextcloud straight into the plugin; it never
passes through BlueFerry, a D-Bus reply or a log. Other providers (an OAuth
device flow, for example) can follow the same three methods under a new
`ConfigLogin` name once clients know how to label them.

```python
from blueferry.plugin_api.config import ConfigError
from blueferry.plugin_api.config_flow import ConfigTestResult, LoginStep
from blueferry.plugin_api.service import ShareService

class WebDav(ShareService):
    def test_config(self, values):              # worker thread; nothing is saved
        user = self.check_server(values["url"], values.get("app_password"))
        if user is None:
            raise ConfigError("app_password", "The server refused this app password.")
        return ConfigTestResult(True, f"Connected as {user}")

    def config_login(self, provider, values):   # worker thread
        flow = self.start_login_flow(values["url"])         # POST …/login/v2
        return LoginStep("open", login_id=flow.id, open_uri=flow.login_url)

    def config_login_status(self, login_id):    # worker thread, every 2 s
        result = self.poll_login_flow(login_id)             # 404 → None
        if result is None:
            return LoginStep("pending")
        self.store(result.server, result.login_name, result.app_password)
        return LoginStep("done", f"Connected as {result.login_name}")

    def config_login_cancel(self, login_id):
        self.forget_login_flow(login_id)
```

Errors are D-Bus errors under `io.weirdware.BlueFerry.Plugin.Error.*`
(`Failed`, `RateLimited`). Messages are short and contain no personal data.

### Plugin log file (ApiVersion 1.4)

A D-Bus activated plugin inherits the bus daemon's stdout and stderr, which
usually end on a console nobody reads. `plugin_api.service.run()` therefore
adds a standard log file for every plugin (`run(..., log_file=False)` turns
it off; `blueferry.plugin_api.logs.setup_logging(id)` does the same for other
entry points):

- `$XDG_STATE_HOME/blueferry/plugins/<Id>.log` (`~/.local/state/…` without
  the variable), directory 0700, file 0600, never through a symlink;
- at most 512 KiB, rotated to `.log.1` and `.log.2`;
- level INFO, `BLUEFERRY_PLUGIN_LOG_LEVEL=DEBUG` in the plugin's environment
  for more; uncaught exceptions (also in threads) and Python warnings are
  included;
- every line is passed through `logs.redact()`, which masks the values of
  `token`, `password`, `pin`, `api_key`, `sessionId`, `Authorization` and
  similar keys. It is a safety net: still never log secrets, message text,
  names or addresses on purpose.

The clients read the file: "Show Log" next to the plugin in the Qt settings
and `blueferry plugins log ID [--lines N] [--path]`.

### Versioning

- Additive changes (a new optional JSON key, a new method, a new capability
  interface such as `Conversations1`) keep the major `ApiVersion` and raise
  the minor (`1.5`). Clients ignore keys they do not know.
- 1.5 replaced the popup signal `Notify(s, s, s, s, s)` with the
  content-free `NotifyChanged()` plus `GetPendingNotification()`. Nothing
  can be misread: a 1.4 host does not hear `NotifyChanged`, a 1.5 host does
  not listen to `Notify`; the popups of an older plugin simply stay off.
- Anything a v1 client could misread (renamed or retyped keys, changed
  semantics) bumps `ApiVersion`. A client lists the versions it supports in
  `plugin_api.SUPPORTED_API_VERSIONS` and ignores other manifests with a
  clear message instead of guessing.
- Capability interfaces carry their own number (`Photos1`), so one capability
  can evolve without touching the others.

## Security

- Clients use the **session bus** only and refuse a plugin whose bus name is
  owned by a different Unix user. The SDK transport activates the service
  without a plugin-method payload, checks its unique owner's Unix user,
  then sends the call to that exact connection. Changing the well-known
  name's owner cannot redirect settings, credentials, files or actions.
- Plugin data is **untrusted**: replies over 512 KiB are rejected, JSON is
  validated field by field, surface ids match `[A-Za-z0-9_.-]{1,64}`, text
  is shown as plain text only (escaped where a Qt control would read it as
  markup), and a path is used only if it resolves (symlinks included)
  to a regular file owned by the user below `$XDG_CACHE_HOME/blueferry/` or
  `~/.cache/blueferry/` (a bus-activated plugin inherits the bus daemon's
  environment, which may lack the client's `XDG_CACHE_HOME`).
- Clients call plugins from worker threads with timeouts. A plugin that
  crashes, hangs or answers garbage produces an error message in the client,
  never a crash.
- The base service rate-limits each caller (120 calls/min), runs network
  and disk work on a pool of four worker threads off its main loop and
  refuses more than 32 unanswered calls at once. `run()` exits after the
  idle time only when no call is still in flight.
- Secrets (API keys) belong to the plugin; BlueFerry stores none of them.
  Keep them in the Secret Service keyring (the published plugins do), or
  in a 0600 file as a fallback, never in the manifest. Settings
  forms send a secret into the plugin only when the user typed a new one,
  and `GetConfig()` reports only whether one is stored (the base class masks
  it even if `config_values()` returns the plain value).

## Writing a plugin

```python
from blueferry.plugin_api.manifest import parse_manifest
from blueferry.plugin_api.service import PhotosService, run

class MyPhotos(PhotosService):
    def status(self):
        return {"state": "ok"}

    def list_recent(self, limit):        # worker thread
        return [{"id": "1", "taken_at": "2026-10-06T18:00:00Z",
                 "type": "image", "thumbnail": "/home/me/.cache/blueferry/my/1.webp"}]

    def fetch_original(self, photo_id):  # worker thread
        return "/home/me/.cache/blueferry/my/1.jpg"

def main():
    manifest = parse_manifest(MANIFEST_TEXT)
    return run(lambda bus: MyPhotos(manifest, bus))
```

A plugin with `[Config …]` groups also implements `config_values()` (the
stored settings; for a secret anything truthy) and `apply_config(values)`
(validated settings; raise `ConfigError(key, reason)` to reject one). Both
run on the worker thread.

Tests use `blueferry.plugin_api.testing`: `inline_service()` runs worker and
main-loop hand-offs inline, `ServiceTransport` lets a real `PluginClient`
call the service in-process through the full validation,
`ScriptedTransport` replays canned replies, and `FakeHost` drives the 1.2
surfaces (see above) and the 1.3 settings helpers: `host.test_config(values)`,
`host.sign_in(values)` (ConfigLogin, then ConfigLoginStatus until a final
state; the opened URL lands in `host.opened`), `host.cancel_sign_in(id)`,
`host.get_config()` and `host.set_config(values)`.

### Plugin from its own repository

A plugin lives in its own Git repository (for example
[blueferry-plugin-immich](https://github.com/joshii-h/blueferry-plugin-immich)).
Layout:

```
my-plugin/
├── pyproject.toml            # depends on blueferry-plugin-api
├── README.md
├── data/
│   └── io.example.my_plugin.plugin
├── src/blueferry_my_plugin/
│   ├── __init__.py
│   ├── __main__.py           # "serve" and the setup CLI
│   └── service.py            # PhotosService subclass
└── tests/
```

Rules: import only `blueferry.plugin_api` from BlueFerry and depend on the
`blueferry-plugin-api` distribution, not on `blueferry`:

```toml
dependencies = [
  "blueferry-plugin-api @ git+https://github.com/erikwb/blueferry@plugin-api-v1.5.0#subdirectory=plugin-api",
]
```

Pin the dependency to a `plugin-api-v<version>` tag (the `version` in
`plugin-api/pyproject.toml`), never to a branch. Until the plugin API is
released upstream, the `plugin-api-v*` tags live in the fork
`joshii-h/blueferry`; pin
`git+https://github.com/joshii-h/blueferry@plugin-api-v1.5.0#subdirectory=plugin-api`
there instead. Declare a
`[project.scripts]` entry point that serves the plugin and name it in `Exec=`
(and `Cli=`). Ship exactly one manifest, in `data/` or as package data in
`src/<package>/`.

## Installing plugins

```
blueferry plugins install https://github.com/me/blueferry-plugin-x [--ref TAG|COMMIT] [--yes]
blueferry plugins update [ID] [--yes]
blueferry plugins remove ID [--yes]
blueferry plugins enable|disable ID
blueferry plugins config ID [--set KEY=VALUE]... [--secret KEY]...
```

The Qt settings (Plugins) offer the same.

- Only `https://` Git URLs. The ref is pinned: `--ref` takes a tag or a
  full commit; without it the newest version tag (`v1.2.3` or `1.2.3`) is
  used, else the default branch's HEAD, pinned as its commit.
- Install is two steps. First the ref is cloned to a staging folder below
  `~/.local/share/blueferry/plugins/src/` and the manifest is read; no
  plugin code runs. The manifest must be one regular file of at most
  16 KiB, and neither it nor a folder on the way to it may be a symlink.
  The client shows source, ref and commit, capabilities, the manifest's
  `Exec` command it will run and the settings, and asks (`--yes` skips the
  question; "No" deletes the checkout). Only then the checkout moves to
  `~/.local/share/blueferry/plugins/src/<id>` and a venv is created at
  `~/.local/share/blueferry/plugins/venvs/<id>-<first 12 hex digits of the commit>` (with
  `--system-site-packages` for dbus-python and PyGObject), the package is
  installed with pip (its dependencies, including `blueferry-plugin-api`,
  come from the network), and the manifest (with `Exec`/`Cli` pointing into
  the venv) and the D-Bus service file are written below `~/.local/share`.
  `Exec` and `Cli` must name a program the plugin installs, or `python`.
  Everything runs as the invoking user; nothing asks for root.
- `installs.json` (what BlueFerry installed) is checked on every read: an
  entry whose id, https URL, tag or commit looks wrong, or whose venv is not
  the one install would have made, is ignored, so a damaged file never
  points `remove` or the process stopper elsewhere.
- Install, update, remove, enable/disable and index changes take a lock
  (`~/.local/share/blueferry/plugins/.lock`); a second one, e.g. from the
  CLI while the Qt settings install, waits a few seconds and then stops
  with a message. Commit also rechecks the installed record after the user
  confirms: if another client has installed, updated, reinstalled or removed
  that plugin in the meantime, preparation and confirmation must be repeated.
- A manifest with the same `Id` that BlueFerry did not install (for example
  from an earlier `setup` command) is replaced; the summary says so.
- `update` compares the pinned ref with the newest tag (or the branch HEAD
  for a pinned commit), shows both commits and the commit log, and asks.
  Without an `ID` it goes through every plugin BlueFerry installed.
- Wherever an `ID` is expected, the short alias shown by `list` works too.
- `remove` deletes the checkout, the venv, the manifest and the service
  file. The plugin's own settings and keyring entries stay.
- Disabling is a kill switch for BlueFerry's side: a disabled plugin stays
  installed, but no client calls it (also not `config` or its CLI alias,
  which would start it again), the card and "Send to…" leave it out, and
  the daemon drops its popups from the next signal on. A running process
  of a plugin BlueFerry installed is stopped (see below). A plugin that was
  installed some other way is not stopped, because BlueFerry has no venv to
  check its process against; it keeps running until it goes idle or you
  end it, and the clients say so. The list lives in
  `~/.config/blueferry/plugins.json`.
- A plugin process keeps running after its venv is replaced or deleted, and
  server plugins (LocalSend, Shortcuts) never exit on their own. So after a
  successful `update`, `remove` or `disable`, BlueFerry asks the session bus
  which process owns the plugin's bus name and sends it SIGTERM, but only if
  it runs as you and its executable, interpreter or script lies in the
  plugin's old venv; it waits up to three seconds and never sends SIGKILL.
  D-Bus activation starts the new version on next use. CLI and Qt say
  "Stopped the running plugin" or, when the process did not exit or runs
  from elsewhere (a developer checkout), that it was left running.
- Never install `blueferry` and `blueferry-plugin-api` into the same
  environment: both ship the files of `blueferry/plugin_api`, and pip would
  let one overwrite the other's. A plugin venv only sees a system-wide
  BlueFerry through `--system-site-packages`; there the system's
  `blueferry.plugin_api` shadows the venv's (a regular package wins over
  the namespace portion), which is harmless while both are the same
  version.

## Plugin store (indexes)

`blueferry plugins available` (and `search TERM`) list plugins from curated
index files; the Qt settings show them as cards. The default index
is `plugins-index.json` in
[joshii-h/blueferry-plugins-index](https://github.com/joshii-h/blueferry-plugins-index);
`blueferry plugins index add|remove|reset URL` changes the list.

```json
{"version": 1, "plugins": [
  {"id": "io.weirdware.blueferry.immich_photos", "name": "Immich photos",
   "description": "…", "repo": "https://github.com/joshii-h/blueferry-plugin-immich",
   "ref": "v0.2.0", "capabilities": ["photos"], "icon": "folder-pictures",
   "emoji": "", "screenshot": "", "min_blueferry": "0.8", "api_version": 1}
]}
```

- An index is untrusted: at most 256 KiB, fetched over https with a
  10-second timeout (every redirect must stay on https, at most five), every entry validated (https Git URL, tag-like ref,
  reverse-DNS id, plain-text name and description). Bad entries are
  dropped.
- Indexes are cached for six hours below `~/.cache/blueferry/plugin-index/`;
  without network the cached copy is shown with a note.
- An entry without `ref` is "coming soon" and cannot be installed.
- Installing from the store uses the same confirmed install as a URL.
