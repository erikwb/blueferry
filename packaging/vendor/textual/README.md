# Private Textual runtime

The DEB and RPM recipes unpack these pinned, pure-Python wheels into
`/usr/lib/blueferry/vendor`. They are part of `blueferry-backend` and are
visible only to the BlueFerry TUI process. They do not provide, replace, or
conflict with a distribution's system Python packages.

Arch does not install this bundle; its `blueferry-tui` split package depends on
the repository's `python-textual` package instead.

`SHA256SUMS` is checked during every DEB and RPM build. The wheels retain their
upstream metadata and license files. To update the bundle, resolve Textual's
complete non-extra dependency closure, replace all wheels together, update the
checksums, and run both native package builds.
