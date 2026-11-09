{
  description = "BlueFerry iPhone Bluetooth bridge for Linux";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          blueferry = pkgs.callPackage ./packaging/nix/package.nix {
            src = self;
            revision = self.rev or null;
          };
        in
        {
          inherit blueferry;
          default = blueferry;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.blueferry}/bin/blueferry";
          meta.description = self.packages.${system}.blueferry.meta.description;
        };
      });

      checks = forAllSystems (system: {
        package = self.packages.${system}.blueferry;
        nixos-module = import ./packaging/nix/module-check.nix {
          pkgs = nixpkgs.legacyPackages.${system};
          inherit nixpkgs;
          module = self.nixosModules.default;
          package = self.packages.${system}.blueferry;
        };
      });

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt);

      nixosModules = {
        blueferry = { pkgs, ... }: {
          imports = [ ./packaging/nix/module.nix ];
          services.blueferry.package =
            nixpkgs.lib.mkDefault
              self.packages.${pkgs.stdenv.hostPlatform.system}.blueferry;
        };
        default = self.nixosModules.blueferry;
      };
    };
}
