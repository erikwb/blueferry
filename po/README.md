# Translations

Python strings use the `blueferry` gettext domain; QML strings use `qsTr()`.
`POTFILES.in` lists every translatable source: each Python module that
imports `blueferry.i18n` and each QML file that calls `qsTr()`.
`tests/test_potfiles.py` enforces this, so a new file with translatable
strings fails the test suite until it is listed.

The repository contains no generated template (`.pot`) and no build step
reads `POTFILES.in` yet, so changing the inventory requires no
regeneration. Whoever adds the first real translation generates the
template from this list (for example with `xgettext --files-from` for the
Python sources and `lupdate` for the QML files).

There are no placeholder catalogs. When a real translation is added, compile
gettext catalogs to `usr/share/locale/<language>/LC_MESSAGES/blueferry.mo` and
Qt catalogs to `usr/share/blueferry/translations/blueferry_<locale>.qm`, then
add their generation and installation to the package build.
