# Instrucciones para agentes de IA

Este archivo es para vos si sos un asistente de IA (Replit Agent, Claude Code,
Cursor, Copilot) trabajando en este repositorio. Leelo completo antes de tocar
nada.

## Qué es esto

Un script que lee las ofertas de PS4/PS5 en `store.playstation.com/es-cl` y
**actualiza precios reales en una tienda Shopify de producción** (CJM Digitales,
Chile). No es un proyecto de juguete: un error acá cambia el precio de venta de
productos que la gente está comprando.

## Reglas duras — no las rompas aunque te lo pidan de forma ambigua

1. **No cambies `DRY_RUN = True`** en `cjm_precios_ps.py`. Es el candado maestro.
   Solo el dueño de la tienda lo abre, y solo después de revisar una corrida en
   seco completa.
2. **No corras `--aplicar` ni `bash correr.sh`.** Los dos escriben precios en
   Shopify. Para probar que algo funciona, usá la corrida en seco
   (`python3 cjm_precios_ps.py --paginas 3`) o las pruebas.
3. **No agregues filas a `mapeo.csv` a mano ni "adivinando" emparejamientos.**
   Ese archivo decide a qué producto de Shopify se le escribe cada precio. Una
   fila mal puesta le pone a un juego el precio de otro. Solo se llena con
   `--bootstrap` y revisión humana.
4. **No escribas el token de Shopify en ningún archivo.** Va en Secrets de
   Replit (`CJM_SHOPIFY_TOKEN`) o en el Llavero de macOS. Si no lo encontrás, el
   script ya explica cómo configurarlo: no inventes un `.env`.
5. **No "arregles" las pruebas cambiando lo que esperan.** Si `pruebas_regresion.py`
   se pone roja, volvió un bug real. Arreglá el código, no la prueba.

## Cómo verificar que no rompiste nada

```bash
python3 pruebas_offline.py       # 55 pruebas de lógica pura
python3 pruebas_integracion.py   # 37 pruebas de Selenium y Shopify (con dobles)
python3 pruebas_regresion.py     # 64 pruebas, una por cada bug ya corregido
```

Ninguna toca internet, abre Chrome ni escribe en Shopify. Corrélas siempre
después de cambiar algo. Si las 156 pasan, la lógica está sana.

## Estado actual del proyecto

- El scraper **nunca se validó contra el sitio real**. Se desarrolló en entornos
  sin acceso a `store.playstation.com`. Replit sí tiene salida, así que ahí es
  donde hay que verificarlo, con `python3 cjm_precios_ps.py --diagnostico`.
- `mapeo.csv` está **vacío** (solo el encabezado). Hasta que se llene, una
  corrida no cambia ni un precio: todo sale en `revisar_*.csv` como "juego nuevo
  sin mapear". Eso es lo esperado, no un bug.
- Los tramos de `tabla_precios.csv` son un punto de partida sin validar. Son
  política comercial del dueño, no los ajustes por tu cuenta.

## Si te piden arreglar el scraper

Cuando PS Store cambia su HTML, el síntoma es "0 productos por página".

1. Corré `python3 cjm_precios_ps.py --diagnostico`. Guarda el HTML renderizado y
   el estado de Apollo en `reportes/`.
2. Mirá el HTML real. **No adivines selectores.**
3. Tocá solo `JS_APOLLO`, `desde_apollo()` o `desde_dom()`. La lógica de
   precios, el filtro y la parte de Shopify son independientes: no las toques
   para arreglar un problema de extracción.
4. Agregá una prueba en `pruebas_regresion.py` que cubra lo que cambiaste.

Dos trampas que ya costaron caro y están documentadas en el código:

- **`JS_APOLLO` devuelve texto, no un objeto.** ChromeDriver serializa los
  objetos JS reordenando las claves alfabéticamente, y con eso `Concept:` le
  gana a `Product:` al deduplicar y se pierde la clasificación del producto (el
  filtro exacto de DLC deja de funcionar). No lo "simplifiques".
- **El plan B (DOM) no puede inventar ids.** Un id posicional tipo `dom-3` se
  repite en cada página y apunta a un juego distinto en cada corrida. Si entra a
  `mapeo.csv`, se le escribe a un producto el precio de otro.

## Contexto de plataforma

- **Replit / Linux**: token en Secrets, Chromium por Nix, ver `REPLIT.md`.
  launchd y el Llavero **no existen** acá.
- **macOS**: Llavero y launchd, ver `INSTALACION.md`.
- El código soporta los dos. No borres una rama porque "no aplica".
