Name:           blueferry-backend
Version:        0.7.1
Release:        1%{?dist}
Summary:        iPhone Bluetooth bridge backend, daemon, and CLI
License:        GPL-2.0-only AND MIT AND BSD-2-Clause AND PSF-2.0
URL:            https://github.com/erikwb/blueferry
Source0:        blueferry-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  dbus-daemon
BuildRequires:  desktop-file-utils
BuildRequires:  appstream
BuildRequires:  python3-devel
BuildRequires:  python3-dbus
BuildRequires:  python3-gobject
BuildRequires:  python3dist(cryptography) >= 41
BuildRequires:  python3dist(hypothesis)
BuildRequires:  python3dist(pytest)
BuildRequires:  python3dist(setuptools) >= 68
BuildRequires:  python3dist(typer) >= 0.9
BuildRequires:  python3dist(wheel)
BuildRequires:  pyproject-rpm-macros
BuildRequires:  systemd-rpm-macros
BuildRequires:  gtk4
BuildRequires:  libadwaita

Requires:       bluez >= 5.86
Requires:       bluez-obexd
Requires:       libsecret
Requires:       polkit
Requires:       python3-dbus
Requires:       python3-gobject
Requires:       python3dist(cryptography) >= 41
Requires:       python3dist(typer) >= 0.9
Requires:       systemd
Recommends:     gnome-keyring
Provides:       bundled(python3dist(linkify-it-py)) = 2.1.0
Provides:       bundled(python3dist(markdown-it-py)) = 4.2.0
Provides:       bundled(python3dist(mdit-py-plugins)) = 0.6.1
Provides:       bundled(python3dist(mdurl)) = 0.1.2
Provides:       bundled(python3dist(platformdirs)) = 4.11.2
Provides:       bundled(python3dist(pygments)) = 2.20.0
Provides:       bundled(python3dist(rich)) = 15.0.0
Provides:       bundled(python3dist(textual)) = 8.2.8
Provides:       bundled(python3dist(typing-extensions)) = 4.16.0
Provides:       bundled(python3dist(uc-micro-py)) = 2.0.0

%description
BlueFerry connects an iPhone to a Linux desktop over Bluetooth for messages,
notifications, and contacts. This package contains the backend service,
command-line client, D-Bus activation, and BlueZ configuration.
The Textual terminal client uses a private dependency bundle that is not
visible to other Python applications.

%package -n blueferry-gtk
Summary:        GTK4/libadwaita client for BlueFerry
Requires:       %{name} = %{version}-%{release}
Requires:       gtk4
Requires:       libadwaita
Requires:       python3-gobject

%description -n blueferry-gtk
BlueFerry's GTK4 graphical client for GNOME and other GTK-based desktops.

%package -n blueferry-qt
Summary:        Qt/Kirigami client for BlueFerry
Requires:       %{name} = %{version}-%{release}
Requires:       kf6-kirigami
Requires:       python3-pyside6

%description -n blueferry-qt
BlueFerry's Qt 6 and Kirigami graphical client for KDE Plasma and other
Qt-based desktops.

%prep
%autosetup -n blueferry-%{version}

%build
%pyproject_wheel

%install
%pyproject_install
cd packaging/vendor/textual
sha256sum -c SHA256SUMS
cd -
install -d %{buildroot}%{_prefix}/lib/blueferry/vendor
for wheel in packaging/vendor/textual/wheels/*.whl; do
    %{python3} -m zipfile -e "$wheel" \
        %{buildroot}%{_prefix}/lib/blueferry/vendor
done

install -Dm0644 systemd/blueferry.service \
    %{buildroot}%{_userunitdir}/blueferry.service
install -Dm0644 systemd/blueferry-btmgmt-set-class@.service \
    %{buildroot}%{_unitdir}/blueferry-btmgmt-set-class@.service
install -Dm0755 systemd/blueferry-set-cod \
    %{buildroot}%{_prefix}/lib/blueferry/blueferry-set-cod
install -Dm0644 systemd/49-blueferry-cod.rules \
    %{buildroot}%{_datadir}/polkit-1/rules.d/49-blueferry-cod.rules
install -d %{buildroot}%{_userunitdir}/default.target.wants
ln -s ../blueferry.service \
    %{buildroot}%{_userunitdir}/default.target.wants/blueferry.service
install -Dm0644 packaging/rpm/io.weirdware.BlueFerry.service \
    %{buildroot}%{_datadir}/dbus-1/services/io.weirdware.BlueFerry.service
install -Dm0644 data/io.weirdware.BlueFerry.xml \
    %{buildroot}%{_datadir}/dbus-1/interfaces/io.weirdware.BlueFerry.xml
install -Dm0644 data/icons/io.weirdware.BlueFerry.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/io.weirdware.BlueFerry.svg
install -Dm0644 packaging/rpm/blueferry-bluetooth.conf \
    %{buildroot}%{_unitdir}/bluetooth.service.d/blueferry.conf
install -d %{buildroot}%{_datadir}/blueferry
printf '%s\n' '%{version}-%{release}' \
    > %{buildroot}%{_datadir}/blueferry/package-release
install -Dm0644 .blueferry-build-sha \
    %{buildroot}%{_datadir}/blueferry/build-sha

install -Dm0644 data/io.weirdware.BlueFerry.Gtk.desktop \
    %{buildroot}%{_datadir}/applications/io.weirdware.BlueFerry.Gtk.desktop
install -Dm0644 data/io.weirdware.BlueFerry.Gtk.metainfo.xml \
    %{buildroot}%{_metainfodir}/io.weirdware.BlueFerry.Gtk.metainfo.xml
install -Dm0644 data/io.weirdware.BlueFerry.Qt.desktop \
    %{buildroot}%{_datadir}/applications/io.weirdware.BlueFerry.Qt.desktop
install -Dm0644 data/io.weirdware.BlueFerry.Qt.metainfo.xml \
    %{buildroot}%{_metainfodir}/io.weirdware.BlueFerry.Qt.metainfo.xml

%check
dbus-run-session --config-file=tests/dbus-test.conf -- sh -ec '
    export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
    export BLUEFERRY_TEST_DBUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
    export PYTHONPATH="%{buildroot}%{_prefix}/lib/blueferry/vendor:$PWD/src"
    %{python3} -m pytest -q --ignore=tests/test_qml_presenters.py
'
desktop-file-validate data/io.weirdware.BlueFerry.Gtk.desktop
desktop-file-validate data/io.weirdware.BlueFerry.Qt.desktop
appstreamcli validate --no-net data/io.weirdware.BlueFerry.Gtk.metainfo.xml
appstreamcli validate --no-net data/io.weirdware.BlueFerry.Qt.metainfo.xml

%post
%systemd_user_post blueferry.service
# Apply the BlueZ drop-in now, but do not start an inactive service. Ignore
# failures in build roots and containers where systemd is not PID 1.
/usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :
/usr/bin/systemctl try-restart bluetooth.service >/dev/null 2>&1 || :

%preun
%systemd_user_preun blueferry.service

%postun
%systemd_user_postun_with_restart blueferry.service
if [ $1 -eq 0 ]; then
    # The drop-in has been removed. Restore the vendor command line for a
    # Bluetooth daemon that is already running.
    /usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :
    /usr/bin/systemctl try-restart bluetooth.service >/dev/null 2>&1 || :
fi

%files
%license LICENSE
%doc README.md ARCHITECTURE.md PROTOCOL.md
%{_bindir}/blueferry
%{_bindir}/blueferry-tui
%{_bindir}/blueferry-quickshell-bridge
%dir %{python3_sitelib}/blueferry
%{python3_sitelib}/blueferry/*.py
%{python3_sitelib}/blueferry/tui.tcss
%{python3_sitelib}/blueferry/__pycache__
%{python3_sitelib}/blueferry/ancs
%{python3_sitelib}/blueferry/obex
%{python3_sitelib}/blueferry/sinks
%{python3_sitelib}/blueferry-*.dist-info
%{_prefix}/lib/blueferry/vendor
%{_userunitdir}/blueferry.service
%{_userunitdir}/default.target.wants/blueferry.service
%{_unitdir}/blueferry-btmgmt-set-class@.service
%{_prefix}/lib/blueferry/blueferry-set-cod
%{_datadir}/polkit-1/rules.d/49-blueferry-cod.rules
%{_unitdir}/bluetooth.service.d/blueferry.conf
%{_datadir}/blueferry/package-release
%{_datadir}/blueferry/build-sha
%{_datadir}/dbus-1/interfaces/io.weirdware.BlueFerry.xml
%{_datadir}/dbus-1/services/io.weirdware.BlueFerry.service
%{_datadir}/icons/hicolor/scalable/apps/io.weirdware.BlueFerry.svg

%files -n blueferry-gtk
%license LICENSE
%{_bindir}/blueferry-gtk
%{python3_sitelib}/blueferry/ui
%{_datadir}/applications/io.weirdware.BlueFerry.Gtk.desktop
%{_metainfodir}/io.weirdware.BlueFerry.Gtk.metainfo.xml

%files -n blueferry-qt
%license LICENSE
%{_bindir}/blueferry-qt
%{python3_sitelib}/blueferry/qt
%{_datadir}/applications/io.weirdware.BlueFerry.Qt.desktop
%{_metainfodir}/io.weirdware.BlueFerry.Qt.metainfo.xml

%changelog
* Sat Aug 15 2026 BlueFerry Contributors <blueferry@weirdware.io> - 0.7.1-1
- Make controller capability checks advisory and fix D-Bus advertisement typing.
- Use packaged systemd units for privileged Bluetooth setup.
- Remove Qt startup warnings and polish connection health and group messaging.
- Repair tagged release publication.

* Fri Aug 14 2026 BlueFerry Contributors <blueferry@weirdware.io> - 0.7.0-1
- Add native packages for supported Arch, Debian, and Fedora systems.
- Add compatibility and explicit iPhone pairing modes.
- Restart stale backend builds and recover ANCS after BlueZ restarts.
- Ship the terminal client as part of every backend package.

* Thu Aug 13 2026 BlueFerry Contributors <blueferry@weirdware.io> - 0.6.3-1
- Initial Fedora package scaffold with private Textual runtime.
