# cjm-auto-web-scrapping

Automatización de precios de ofertas PS4/PS5 para **CJM Digitales** (Shopify, Chile).

Recorre las categorías de ofertas de `store.playstation.com/es-cl`, se queda solo
con juegos completos, traduce el precio de oferta a los dos precios de venta
(Cuenta Primaria y Cuenta Secundaria) según tramos, y actualiza Shopify por Admin
API dejando el precio normal en `compareAtPrice` y el de oferta en `price`.

```bash
pip3 install -r requirements.txt
python3 pruebas_offline.py                 # 55 pruebas de logica, sin red
python3 pruebas_integracion.py             # 37 pruebas de Selenium y Shopify, sin red
python3 pruebas_regresion.py               # 64 pruebas, una por bug ya corregido
python3 cjm_precios_ps.py --diagnostico    # primero: validar contra el sitio real
python3 cjm_precios_ps.py                  # corrida completa en seco
```

- **[INSTALACION.md](INSTALACION.md)** — arquitectura y puesta en marcha en macOS (launchd).
- **[REPLIT.md](REPLIT.md)** — despliegue en Replit (Linux): Secrets, Chromium por Nix, Scheduled Deployments.
- **[AGENTS.md](AGENTS.md)** — reglas para asistentes de IA que trabajen en este repo.
- **[PROMPTS.md](PROMPTS.md)** — prompts de apoyo para Claude Code.

## Seguridad

- El token de Shopify vive en el Llavero de macOS (`cjm_shopify_token`), nunca en disco.
- Por defecto el sistema **solo simula**. Para escribir hacen falta tres candados:
  `DRY_RUN = False` en el script, la bandera `--aplicar`, y no pasar `--solo-simular`.
- No se inventan emparejamientos: un juego que no esté en `mapeo.csv` se reporta,
  no se toca.
