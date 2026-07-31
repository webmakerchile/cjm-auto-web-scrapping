# Contexto del proyecto para el Agent de Replit

Script en Python que lee las ofertas de PS4/PS5 de `store.playstation.com/es-cl`
y **actualiza precios reales en una tienda Shopify de producción** (CJM
Digitales, Chile).

## ⛔ Reglas duras

1. **No cambies `DRY_RUN = True`** en `cjm_precios_ps.py`. Es el candado maestro
   que impide escribir en la tienda. Solo lo abre el dueño, y solo después de
   revisar una corrida en seco completa.
2. **No ejecutes `--aplicar` ni `bash correr.sh`.** Escriben precios reales en
   Shopify. Para probar, usá `python3 cjm_precios_ps.py --paginas 3`, que solo
   simula.
3. **No agregues filas a `mapeo.csv` a mano ni adivinando.** Ese archivo decide a
   qué producto de Shopify se le escribe cada precio; una fila mal puesta le pone
   a un juego el precio de otro. Solo se llena con `--bootstrap` + revisión
   humana.
4. **El token va en Secrets** (`CJM_SHOPIFY_TOKEN`), nunca en un archivo. No
   crees un `.env`.
5. **No "arregles" pruebas cambiando lo que esperan.** Si una se pone roja,
   volvió un bug real.

## Cómo verificar cambios

```bash
python3 pruebas_offline.py       # 55 pruebas
python3 pruebas_integracion.py   # 37 pruebas
python3 pruebas_regresion.py     # 64 pruebas, una por bug ya corregido
```

Ninguna toca internet ni Shopify. Si las 156 pasan, la lógica está sana.

## Primer paso en Replit

El scraper nunca se validó contra el sitio real (se desarrolló en entornos sin
acceso a PS Store). Replit sí tiene salida:

```bash
pip install -r requirements.txt
python3 cjm_precios_ps.py --diagnostico
```

Tiene que reportar **plan A (apollo)** y ~24 productos por página. Si reporta
plan B (DOM), la extracción necesita ajuste: mirá el HTML que deja en
`reportes/`, no adivines selectores.

## Estado actual

- `mapeo.csv` está vacío. Hasta que se llene, ninguna corrida cambia un precio y
  todo sale en `revisar_*.csv` como "juego nuevo sin mapear". Eso es lo
  esperado, no un bug.
- Los tramos de `tabla_precios.csv` son un punto de partida sin validar. Son
  política comercial del dueño.

## Panel web privado

- `panel/app.py` (Flask, puerto 5000, workflow "Start application"): login, gestión
  de usuarios y botón que corre el scraper **siempre con `--solo-simular`**.
- Superadmin: `webmakerchile@gmail.com`, contraseña en el Secret `SUPERADMIN_PASSWORD`.
- Usuarios comunes en la base PostgreSQL de Replit (tabla `usuarios`, hash de
  contraseña). Los CSV de `reportes/` también se respaldan en la tabla `reportes`
  al arrancar el panel y al terminar cada corrida, así sobreviven a un deploy.
  La vieja `panel/usuarios.db` (SQLite) solo se usa una vez para migrar.
- CSRF por token de sesión en todos los POST. Correr con **un solo proceso**
  (el estado de la corrida vive en memoria).

## Más detalle

- **[AGENTS.md](AGENTS.md)** — guía completa para agentes, incluidas las dos
  trampas del scraper que ya costaron caro.
- **[REPLIT.md](REPLIT.md)** — despliegue: Secrets, Chromium por Nix, Scheduled
  Deployments y por qué conviene correrlo a mano.
- **[INSTALACION.md](INSTALACION.md)** — arquitectura y puesta en marcha.
