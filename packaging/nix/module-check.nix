{
  pkgs,
  nixpkgs,
  module,
  package,
}:
let
  evaluate =
    settings:
    (nixpkgs.lib.nixosSystem {
      system = pkgs.stdenv.hostPlatform.system;
      modules = [
        module
        settings
      ];
    }).config;
  enabled = evaluate {
    services.blueferry.enable = true;
    hardware.bluetooth.disabledPlugins = [ "sap" ];
  };
  disabled = evaluate { };
in
assert enabled.services.blueferry.package == package;
assert builtins.elem package enabled.environment.systemPackages;
assert builtins.elem package enabled.services.dbus.packages;
assert builtins.elem package enabled.systemd.packages;
assert enabled.hardware.bluetooth.enable;
assert enabled.hardware.bluetooth.settings.General.Experimental;
assert builtins.elem "hfp" enabled.hardware.bluetooth.disabledPlugins;
assert builtins.elem "sap" enabled.hardware.bluetooth.disabledPlugins;
assert enabled.systemd.user.services.blueferry.wantedBy == [ "default.target" ];
assert
  enabled.systemd.user.services.blueferry.serviceConfig.ReadWritePaths == [
    "%h"
    "%t"
  ];
assert enabled.security.polkit.enable;
assert nixpkgs.lib.hasInfix "blueferry-btmgmt-set-class@" enabled.security.polkit.extraConfig;
assert !disabled.hardware.bluetooth.enable;
assert !(builtins.elem package disabled.environment.systemPackages);
pkgs.runCommand "blueferry-nixos-module" { } ''
  touch "$out"
''
