{
  description = "Blake: an independent fork of Lake, the Lean 4 build system and package manager";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            # Installs and runs the Lean toolchain pinned in `lean-toolchain`
            # (`lean`, `lake`, `leanc`, ...), which is what Blake builds against.
            elan
            # Used by Blake's test suite (`tests/`).
            git
            gnumake
            gnutar
            gzip
            curl
            jq
            python3
          ] ++ pkgs.lib.optionals pkgs.stdenv.isLinux [
            # Sandbox used by `lake check`/`lake challenge`; see `tests/tests/check-*`.
            pkgs.bubblewrap
          ];
          shellHook = ''
            echo "Blake dev shell: Lean toolchain $(cat lean-toolchain 2>/dev/null) via elan"
          '';
        };
      });
    };
}
