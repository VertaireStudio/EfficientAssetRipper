# NixOS packaging for EfficientAssetRipper.
#
# Uses buildFHSEnv (the standard NixOS pattern for running non-Nix binaries,
# https://nixos.org/manual/nixpkgs/unstable/#sec-fhs-environments) so the
# self-contained CUE4ParseCLI ELF runs inside an FHS-style chroot with the
# glibc/dotnet shared libraries it ships against. This is the packaged
# alternative to the runtime support in core/platform_detect.py (nix-ld /
# steam-run), which still applies when running from source.
#
# Expects the published CLI at cue4parse_cli/bin/publish/CUE4ParseCLI
# (build.sh produces it); the flake hard-fails with instructions otherwise.
#
# Usage:
#   nix build .#          # → ./result/bin/efficientassetripper
#   nix flake check       # syntax + expr evaluation
#
# NOTE (shortcut: unverified in CI): this flake was written without a Nix
# toolchain available to the authoring environment — run `nix flake check`
# on a NixOS host before relying on it, and pin `pyside6` availability in
# your nixpkgs rev (it landed as python3Packages.pyside6).
{
  description = "EfficientAssetRipper — UE4/5 asset toolkit with a PySide6 GUI";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      mkOutputs = pkgs:
        let
          # Libraries the published .NET single-file binary needs at startup
          # (mirrors the official nix-ld default list, plus libvulkan for GPU
          # texture decode and icu for .NET globalization fallbacks).
          fhsLibs = with pkgs; [
            stdenv.cc.cc.lib     # libstdc++/glibc-adjacent bits
            zlib zstd curl openssl icu
            vulkan-loader        # libvulkan.so.1 (GPU texture decode)
            libGL
          ];

          pythonEnv = pkgs.python3.withPackages (ps: with ps; [
            pyside6
            pillow
            numpy
            pyopengl
          ]);

          cli = pkgs.stdenvNoCC.mkDerivation {
            name = "CUE4ParseCLI";
            src = ./cue4parse_cli;
            nativeBuildInputs = [ pkgs.autoPatchelfHook ];
            # Single-file publish is self-contained: only the ELF interpreter
            # needs fixing; no shared libraries are pulled in.
            buildInputs = [ pkgs.glibc ];
            dontConfigure = true;
            dontBuild = true;
            installPhase = ''
              runHook preInstall
              if [ ! -f bin/publish/CUE4ParseCLI ]; then
                echo "cue4parse_cli/bin/publish/CUE4ParseCLI missing — run ./build.sh first" >&2
                exit 1
              fi
              install -Dm755 bin/publish/CUE4ParseCLI "$out/bin/CUE4ParseCLI"
              runHook postInstall
            '';
          };

          # A trimmed copy of the repo the GUI runs from (source + runtime dirs).
          gui = pkgs.runCommand "EAR-gui-source" { src = ./.; } ''
            mkdir -p "$out"
            cp -a "$src"/main.py "$src"/_base.py "$src"/_version.py "$src"/config.py \
                  "$src"/core "$src"/gui "$src"/data "$src"/fonts "$src"/blender \
                  "$out/"
            # Runtime dirs the app expects next to the source tree.
            for d in profiles cache outputs logs; do mkdir -p "$out/$d"; done
          '';

          app = pkgs.buildFHSEnv {
            name = "efficientassetripper";
            targetPkgs = pkgs': [ pythonEnv cli ] ++ fhsLibs;
            # "$@" lands in the store path for the prepared gui source; the
            # FHS env exposes it because /nix/store is bind-mounted inside.
            runScript = ''
              exec python ${gui}/main.py "$@"
            '';
          };

          pkg = pkgs.stdenvNoCC.mkDerivation {
            name = "EfficientAssetRipper";
            nativeBuildInputs = [ pkgs.coreutils ];
            src = null;
            unpackPhase = ":";
            installPhase = ''
              runHook preInstall
              mkdir -p "$out/bin" "$out/share/applications"
              cp "${app}/bin/efficientassetripper" "$out/bin/"
              cat > "$out/share/applications/efficientassetripper.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=EfficientAssetRipper
Comment=Unpack and export UE4/5 game assets
Exec=efficientassetripper
Categories=Utility;Graphics;
EOF
              runHook postInstall
            '';
            meta = with pkgs.lib; {
              platforms = [ "x86_64-linux" "aarch64-linux" ];
              license = licenses.free;
            };
          };
        in
        rec { default = pkg; efficientassetripper = pkg; };
    in
    {
      packages = forAllSystems mkOutputs;

      devShells.x86_64-linux.default =
        let pkgs = nixpkgs.legacyPackages.x86_64-linux;
        in pkgs.mkShell {
          packages = with pkgs; [
            (python3.withPackages (ps: with ps; [ pyside6 pillow numpy pyopengl ]))
            dotnet-sdk_10
          ];
        };

      # Full build is heavy (download of the whole FHS closure), but it's the
      # cheapest guarantee the flake actually builds the user asked to trust.
      checks.x86_64-linux.default = self.packages.x86_64-linux.efficientassetripper;
    };
}
