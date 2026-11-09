{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.blueferry;
in
{
  options.services.blueferry = {
    enable = lib.mkEnableOption "BlueFerry's iPhone Bluetooth bridge";
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      description = "BlueFerry package to install and run.";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];
    services.dbus.packages = [ cfg.package ];
    systemd.packages = [ cfg.package ];

    hardware.bluetooth = {
      enable = true;
      settings.General.Experimental = true;
      # BlueZ's hfp plugin competes with BlueFerry's registered phone profiles.
      disabledPlugins = [ "hfp" ];
    };

    systemd.user.services.blueferry = {
      wantedBy = [ "default.target" ];
      # Retain the packaged sandbox while allowing private configuration/state.
      serviceConfig.ReadWritePaths = [
        "%h"
        "%t"
      ];
    };

    security.polkit = {
      enable = true;
      extraConfig = builtins.readFile ../../systemd/49-blueferry-cod.rules;
    };
  };
}
