{
  lib,
  makeFontsConf,
  dejavu_fonts,
  src ? lib.cleanSource ../..,
  revision ? null,
  python3Packages,
  wrapGAppsHook4,
  gobject-introspection,
  gtk4,
  libadwaita,
  libsecret,
  qt6,
  kdePackages,
  bluez,
  coreutils,
  systemd,
  wireplumber,
}:
python3Packages.buildPythonApplication (finalAttrs: {
  pname = "blueferry";
  version = (builtins.fromTOML (builtins.readFile "${src}/pyproject.toml")).project.version;
  pyproject = true;
  inherit src;
  build-system = [
    python3Packages.setuptools
    python3Packages.wheel
  ];
  dependencies = with python3Packages; [
    cryptography
    dbus-python
    pygobject3
    pyside6
    phonenumbers
    regex
    textual
    typer
  ];
  nativeBuildInputs = [
    gobject-introspection
    wrapGAppsHook4
    qt6.wrapQtAppsHook
  ];
  buildInputs = [
    gtk4
    libadwaita
    libsecret
    qt6.qtdeclarative
    qt6.qtwayland
    qt6.qtimageformats
    kdePackages.kirigami
    kdePackages.qqc2-desktop-style
  ];
  dontWrapGApps = true;
  dontWrapQtApps = true;
  preFixup = ''
    export FONTCONFIG_FILE=${makeFontsConf { fontDirectories = [ dejavu_fonts ]; }}
    export XDG_CACHE_HOME="$TMPDIR/cache"
    # Pairing helpers use sys.executable -m blueferry in a fresh interpreter.
    makeWrapperArgs+=(--prefix PYTHONPATH : "$out/${python3Packages.python.sitePackages}:${python3Packages.makePythonPath finalAttrs.passthru.dependencies}")
    makeWrapperArgs+=("''${gappsWrapperArgs[@]}" "''${qtWrapperArgs[@]}")
  '';
  postPatch = ''
    for file in src/blueferry/*.py; do
      substituteInPlace "$file" \
        --replace-quiet /usr/bin/systemctl ${systemd}/bin/systemctl \
        --replace-quiet /usr/bin/btmgmt ${bluez}/bin/btmgmt \
        --replace-quiet /usr/bin/wireplumber ${wireplumber}/bin/wireplumber \
        --replace-quiet /usr/lib/blueferry/blueferry-set-cod "$out/libexec/blueferry-set-cod" \
        --replace-quiet /usr/bin/sudo /run/wrappers/bin/sudo
    done
    substituteInPlace src/blueferry/build_info.py \
      --replace-fail /usr/share/blueferry/build-sha "$out/share/blueferry/build-sha"
    substituteInPlace src/blueferry/cli.py \
      --replace-fail /usr/lib/bluetooth/obexd ${bluez}/libexec/bluetooth/obexd
    substituteInPlace src/blueferry/bluetooth_capabilities.py \
      --replace-fail /usr/lib/bluetooth/bluetoothd ${bluez}/libexec/bluetooth/bluetoothd
    # The pre-engine style probe must see the same QML paths as Nixpkgs Qt.
    substituteInPlace src/blueferry/qt/app.py \
      --replace-fail '"QML_IMPORT_PATH", "QML2_IMPORT_PATH"' '"QML_IMPORT_PATH", "QML2_IMPORT_PATH", "NIXPKGS_QT6_QML_IMPORT_PATH"'
  '';
  postInstall = ''
    for client in Gtk Qt Tray; do
      install -Dm644 "data/io.weirdware.BlueFerry.$client.desktop" "$out/share/applications/io.weirdware.BlueFerry.$client.desktop"
    done
    install -Dm644 data/io.weirdware.BlueFerry.Qt.metainfo.xml $out/share/metainfo/io.weirdware.BlueFerry.Qt.metainfo.xml
    install -Dm644 systemd/49-blueferry-cod.rules $out/share/polkit-1/rules.d/49-blueferry-cod.rules
    install -Dm644 LICENSE $out/share/licenses/blueferry/LICENSE
    install -Dm644 data/io.weirdware.BlueFerry.Gtk.metainfo.xml $out/share/metainfo/io.weirdware.BlueFerry.Gtk.metainfo.xml
    install -Dm644 data/icons/io.weirdware.BlueFerry.svg $out/share/icons/hicolor/scalable/apps/io.weirdware.BlueFerry.svg
    install -Dm644 data/io.weirdware.BlueFerry.xml $out/share/dbus-1/interfaces/io.weirdware.BlueFerry.xml
    mkdir -p $out/share/dbus-1/services $out/lib/systemd/system $out/lib/systemd/user
    substitute packaging/arch/io.weirdware.BlueFerry.service $out/share/dbus-1/services/io.weirdware.BlueFerry.service \
      --replace-fail /usr/bin/blueferry $out/bin/blueferry
    install -Dm755 systemd/blueferry-set-cod $out/libexec/blueferry-set-cod
    substituteInPlace $out/libexec/blueferry-set-cod \
      --replace-fail /usr/bin/env ${coreutils}/bin/env \
      --replace-fail /usr/bin/btmgmt ${bluez}/bin/btmgmt
    patchShebangs $out/libexec/blueferry-set-cod
    substitute systemd/blueferry-btmgmt-set-class@.service $out/lib/systemd/system/blueferry-btmgmt-set-class@.service \
      --replace-fail /usr/lib/blueferry/blueferry-set-cod $out/libexec/blueferry-set-cod
    substitute systemd/blueferry.service $out/lib/systemd/user/blueferry.service \
      --replace-fail /usr/bin/blueferry $out/bin/blueferry
  ''
  + lib.optionalString (revision != null) ''
    mkdir -p $out/share/blueferry
    printf '%s\n' ${lib.escapeShellArg revision} > $out/share/blueferry/build-sha
  '';
  pythonImportsCheck = [
    "blueferry"
    "blueferry.ui.app"
    "blueferry.qt.app"
    "blueferry.qt.tray"
  ];
  # The source suite runs separately in the existing quality workflow.
  doCheck = true;
  checkPhase = ''
    runHook preInstallCheck
    export QT_QPA_PLATFORM=offscreen
    export QT_QUICK_BACKEND=software
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$TMPDIR/no-bus"
    export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
    for program in blueferry blueferry-gtk blueferry-tray; do
      "$out/bin/$program" --help > /dev/null
    done
    # Reuse the launchers' environment to exercise fresh Python interpreters.
    makeWrapper ${python3Packages.python.interpreter} "$TMPDIR/blueferry-python" "''${makeWrapperArgs[@]}"
    "$TMPDIR/blueferry-python" -c '
    import subprocess, sys
    subprocess.run([sys.executable, "-m", "blueferry", "pairing-complete", "--help"], check=True)
    '
    "$TMPDIR/blueferry-python" packaging/smoke-qt.py
    runHook postInstallCheck
  '';
  meta = {
    description = "iPhone Bluetooth bridge and desktop clients";
    homepage = "https://github.com/erikwb/blueferry";
    license = lib.licenses.gpl2Plus;
    mainProgram = "blueferry";
    platforms = lib.platforms.linux;
  };
})
