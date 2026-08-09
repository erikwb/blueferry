# Translations

Python strings use the `blueferry` gettext domain; QML strings use `qsTr()`.
`POTFILES.in` lists the translatable Python sources.

There are no placeholder catalogs. When a real translation is added, compile
gettext catalogs to `usr/share/locale/<language>/LC_MESSAGES/blueferry.mo` and
Qt catalogs to `usr/share/blueferry/translations/blueferry_<locale>.qm`, then
add their generation and installation to the package build.
