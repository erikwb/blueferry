# Translations

Python strings use the `blueferry` gettext domain through `_()` and
`ngettext()` from `blueferry.i18n`; QML strings use Qt's translation
functions (`qsTr()`, `qsTranslate()`, `qsTrId()` and their `QT_*_NOOP`
forms). `POTFILES.in` lists every source that marks strings this way: each
Python module that uses one of those helpers and each QML file that calls
one of those functions. `tests/test_potfiles.py` enforces this in both
directions, so a new file with marked strings fails the test suite until it
is listed, and a listed file without them fails until it is removed. The
suite also runs the two extraction commands below and fails if a listed file
yields no message.

Only the GTK and Qt/QML clients and the shared presentation modules mark
their strings. The CLI, the TUI and the Quickshell shell (`data/quickshell/`)
show English text that is not marked for translation, so they are not in
the inventory. The Quickshell shell also loads no translation catalog, so
marking its strings needs that runtime support first.

The repository contains no generated template (`.pot`) and no build step
reads `POTFILES.in` yet, so changing the inventory requires no
regeneration. Whoever adds the first real translation generates the
templates from this list, Python and QML separately, from the repository
root:

```sh
grep '\.py$' po/POTFILES.in | xgettext --files-from=- --language=Python \
    --from-code=UTF-8 --keyword=_ --keyword=ngettext:1,2 --output=po/blueferry.pot
lupdate $(grep '\.qml$' po/POTFILES.in) -ts po/blueferry_<locale>.ts
```

There are no placeholder catalogs. When a real translation is added, compile
gettext catalogs to `usr/share/locale/<language>/LC_MESSAGES/blueferry.mo` and
Qt catalogs to `usr/share/blueferry/translations/blueferry_<locale>.qm`, then
add their generation and installation to the package build.
