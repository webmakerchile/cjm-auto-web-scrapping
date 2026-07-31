{ pkgs }: {
  deps = [
    pkgs.python311
    pkgs.python311Packages.pip

    # Chromium y su driver tienen que venir del mismo canal de Nix para que las
    # versiones coincidan. Si no coinciden, Selenium falla con
    # "This version of ChromeDriver only supports Chrome version NNN".
    pkgs.chromium
    pkgs.chromedriver
  ];

  env = {
    # cjm_precios_ps.py los busca solos en el PATH, pero dejarlos explicitos
    # evita sorpresas si Nix cambia los nombres de los binarios.
    CJM_CHROME_BINARY = "${pkgs.chromium}/bin/chromium";
    CJM_CHROMEDRIVER = "${pkgs.chromedriver}/bin/chromedriver";
  };
}
