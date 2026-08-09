# Flatpak status

The manifest in this directory is an untested draft for the GTK client. It is
not a supported way to install BlueFerry.

Only the UI belongs in the sandbox; the backend needs a native installation to
work with BlueZ, systemd, and Polkit. The UI can talk to that backend through
`io.weirdware.BlueFerry` on the session bus, but first-run pairing is not ready
for Flatpak because the current GTK client still launches native helper
commands directly.

Before this can be published, pairing must move behind a narrow backend D-Bus
API and the manifest needs a real build against a supported GNOME runtime. The
GTK client also uses dbus-python, which the draft builds separately; moving it
to Gio's GDBus API would simplify the package.

Do not solve the pairing problem by granting the UI unrestricted system-bus
access. Until the native setup boundary exists, use the Arch packages.
