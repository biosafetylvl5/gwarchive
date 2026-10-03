{
  description = "gwarchive -- dev shells and a from-source build";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs =
    { self, nixpkgs }:
    let
      inherit (nixpkgs) lib;
      # No x86_64-darwin: nixpkgs-unstable dropped it in 26.11.
      forAllSystems = lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      pkgsFor = system: nixpkgs.legacyPackages.${system};

      # Read, not restated, so `cz bump` stays the only writer.
      inherit ((lib.importTOML ./pyproject.toml).project) version;

      # An allowlist, like the sdist's: the repo root holds a private
      # .gwarchive-offload.json that must not reach the world-readable store.
      # test_structure.py reads .github/workflows.
      src = lib.fileset.toSource {
        root = ./.;
        fileset = lib.fileset.unions [
          ./src
          ./tests
          ./.github/workflows
          ./pyproject.toml
          ./uv.lock
          ./.ruff.toml
          ./README.md
          ./LICENSE
        ];
      };

      mkGwarchive =
        python:
        python.pkgs.buildPythonApplication {
          pname = "gwarchive";
          inherit version src;
          pyproject = true;

          build-system = [ python.pkgs.hatchling ];
          dependencies = [
            python.pkgs.typer
            python.pkgs.rich
          ];

          # click>=8.2, for CliRunner's separate result.stderr.
          nativeCheckInputs = [
            python.pkgs.pytestCheckHook
            python.pkgs.click
          ];

          # Both run `sys.executable -m gwarchive` with only PATH set, and the
          # bare nix interpreter cannot import it without the check phase's
          # PYTHONPATH. CI and the dev shells still run them.
          disabledTests = [
            "test_the_emitted_launcher_actually_runs"
            "test_the_title_hook_runs_in_a_real_shell"
          ];

          meta = {
            description = "Organize folders under the GWArchive naming standard";
            homepage = "https://github.com/biosafetylvl5/gwarchive";
            license = lib.licenses.cc0;
            mainProgram = "gwarchive";
          };
        };

      # uv drives everything; nix supplies the interpreter and the binaries the
      # CLI shells out to. On NixOS the ruff and typos wheels may need
      # programs.nix-ld (untested there).
      mkDevShell =
        pkgs: python:
        pkgs.mkShell {
          packages = [
            python
            pkgs.uv
            pkgs.git
            pkgs.gh
            pkgs.pre-commit
            pkgs.rclone
            pkgs.zstd
            # Only for the `clears` greeting, which degrades silently without it.
            pkgs.pokeget-rs
          ];

          env = {
            UV_PYTHON = "${python}/bin/python";
            # Never let uv quietly swap in a Python nix did not provide.
            UV_PYTHON_DOWNLOADS = "never";
          };

          # Switching py3xx shells recreates .venv; that is expected.
          shellHook = ''
            uv sync --quiet
          '';
        };
    in
    {
      # 3.13, the top CI leg. Not python3 (3.14, outside CI), and not the 3.11
      # floor: cache.nixos.org lacks python311Packages, so that meant building
      # 62 derivations locally, numpy and boost among them.
      packages = forAllSystems (
        system:
        let
          gwarchive = mkGwarchive (pkgsFor system).python313;
        in
        {
          inherit gwarchive;
          default = gwarchive;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = lib.getExe self.packages.${system}.default;
        };
      });

      # One shell per CI leg; the default is the 3.11 floor that ruff and mypy
      # target. uv installs the packages from PyPI.
      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = mkDevShell pkgs pkgs.python311;
          py311 = mkDevShell pkgs pkgs.python311;
          py312 = mkDevShell pkgs pkgs.python312;
          py313 = mkDevShell pkgs pkgs.python313;
        }
      );

      # The package build runs the full suite through pytestCheckHook.
      checks = forAllSystems (system: {
        package = self.packages.${system}.default;
      });

      formatter = forAllSystems (system: (pkgsFor system).nixfmt);
    };
}
