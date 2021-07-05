{ pkgs ? import <nixpkgs> { } }: with pkgs;
pkgs.mkShell {
  propagatedBuildInputs = with python3Packages; [
    python-language-server
    python3
    rnix-lsp
    python3Packages.psycopg2
  ];

  buildInputs = [
    pre-commit
  ];

  shellHook = ''
    rm -rf .venv
  '';
}
