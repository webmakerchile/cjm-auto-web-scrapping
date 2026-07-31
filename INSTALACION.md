# INSTALACION — Automatización de precios PS Store → Shopify

Sistema de actualización de precios de ofertas PS4/PS5 para **CJM Digitales**
(tienda Shopify, Chile).

> **Este documento asume macOS** (Llavero, launchd). Si vas a desplegar en
> **Replit** u otro Linux, lee **[REPLIT.md](REPLIT.md)**: el token va en Secrets
> y el `.plist` de launchd no aplica. Las secciones 1 a 4 (arquitectura,
> requisitos, puesta en marcha) valen igual en los dos sistemas.

---

## 1. Arquitectura

```
store.playstation.com/es-cl
        │  Selenium + Chrome
        ▼
  extracción       ── plan A: __NEXT_DATA__ → apolloState  (desde_apollo)
                   └─ plan B: tarjetas del DOM             (desde_dom)
        ▼
  filtro de juegos completos   ← clasificación del producto, o palabras clave
        ▼
  tabla_precios.csv            ← tramo de precio → precio Primaria / Secundaria
        ▼
  mapeo.csv                    ← juego PS Store → producto y variantes Shopify
        ▼
  Shopify Admin API (GraphQL)  ← precio normal a compareAtPrice, oferta a price
        ▼
  reportes/cambios_*.csv  +  reportes/revisar_*.csv
```

### Archivos

| Archivo | Qué es |
|---|---|
| `cjm_precios_ps.py` | Todo el sistema. Scraper, filtro, precios, Shopify, reportes. |
| `tabla_precios.csv` | Tramos de precio. **Editable por ti, es tu política comercial.** |
| `mapeo.csv` | Cruce juego PS Store ↔ producto Shopify. Lo genera `--bootstrap`, lo apruebas tú. |
| `correr.sh` | Lanzador para launchd o para el Scheduled Deployment de Replit. |
| `com.cjm.precios.plist` | Agente de launchd (días 1 y 15, 09:00). **Solo macOS.** |
| `.replit` / `replit.nix` | Configuración de Replit: Python, Chromium y chromedriver. |
| `pruebas_offline.py` | 55 pruebas de la lógica pura. |
| `pruebas_integracion.py` | 37 pruebas de Selenium y Shopify con dobles. |
| `pruebas_regresion.py` | 64 pruebas, una por cada bug ya corregido. |
| `estado.json` | Qué juegos estaban en oferta la corrida anterior. Se genera solo. |
| `reportes/` | Salidas de cada corrida. No se versiona. |

### Los tres candados antes de escribir en Shopify

Nada se escribe a menos que **los tres** estén abiertos:

1. `DRY_RUN = False` dentro de `cjm_precios_ps.py` (candado maestro, viene en `True`).
2. La bandera `--aplicar` en la línea de comandos.
3. Que **no** se haya pasado `--solo-simular`.

Mientras `DRY_RUN` siga en `True`, `--aplicar` se ignora y el script lo avisa por pantalla.

---

## 2. Requisitos

```bash
python3 --version          # 3.9 o superior
pip3 install --upgrade selenium webdriver-manager requests
```

Google Chrome instalado. Selenium 4.6+ descarga el driver solo; `webdriver-manager`
está como respaldo.

---

## 3. Token de Shopify (Llavero de macOS — en Replit va en Secrets)

El token **nunca** se escribe en un archivo. Se guarda una vez en el Llavero:

```bash
security add-generic-password -s cjm_shopify_token -a shopify -w
# pega el token cuando lo pida (no queda en el historial de Terminal)
```

Verifica que se lea:

```bash
security find-generic-password -s cjm_shopify_token -w | head -c 6; echo "..."
```

El token es de una app privada de Shopify con permisos:
`read_products`, `write_products`.

Fuera de macOS, el script acepta la variable de entorno `CJM_SHOPIFY_TOKEN`.

Ajusta también el dominio de tu tienda si no es el que viene por defecto:

```bash
export CJM_SHOPIFY_TIENDA="tu-tienda.myshopify.com"
```

---

## 4. Puesta en marcha, en orden

### Paso 0 — Pruebas offline

```bash
python3 pruebas_offline.py       # 55 pruebas de logica pura
python3 pruebas_integracion.py   # 37 pruebas de Selenium y Shopify con dobles
python3 pruebas_regresion.py     # 64 pruebas, una por cada bug ya corregido
```

156 pruebas en total: precios, filtro, tramos, mapeo, los dos caminos de
extracción (Apollo y DOM), reintentos, la paginación del catálogo de Shopify, la
mutación de precios y los tres candados. **Ninguna abre Chrome ni toca internet
ni escribe en Shopify**, así que se pueden correr siempre que cambies algo.

`pruebas_regresion.py` es especial: cada bloque documenta un bug que ya ocurrió
y explica qué pasaba antes. Si uno se pone rojo, volvió algo que costó encontrar.

### Paso 1 — Diagnóstico del sitio (lo primero, siempre)

```bash
python3 cjm_precios_ps.py --diagnostico
```

Abre una página real de PS Store, guarda el HTML renderizado y el estado de Apollo
en `reportes/`, y te dice en pantalla:

- si funcionó **plan A (apollo)** o cayó al **plan B (dom)**;
- cuántos productos extrajo (deberían ser ~24 por página);
- **si existe el campo de clasificación** (`FULL_GAME` / `ADD_ON` / …) y qué valores trae;
- una muestra de 8 juegos con precio normal y precio de oferta, para que la
  compares a mano abriendo la misma página en Chrome;
- una advertencia si la moneda no es CLP.

Si extrajo 0 productos, PS Store cambió su estructura: mira
`reportes/pagina_renderizada_*.html` y ajusta `JS_APOLLO`, `desde_apollo()` o los
selectores de `desde_dom()`.

Para verlo con el navegador visible: `--diagnostico --ver-navegador`.

### Paso 2 — Auditar el filtro de juegos

```bash
python3 cjm_precios_ps.py --muestra-filtro --paginas 4
```

Imprime 40 aceptados y 40 descartados con el motivo de cada decisión, y deja el
detalle completo en `reportes/filtro_*.csv`. Revisa que no esté botando juegos
completos ni dejando pasar DLC.

> El filtro usa la clasificación de PS Store cuando existe (exacto). Si no
> existe, cae a la lista `PALABRAS_DESCARTE`, que se le escapan DLC con nombres
> limpios como *"Assassin's Creed Valhalla - La Ira de los Druidas"*. En la
> práctica `mapeo.csv` los frena igual, porque un DLC nunca está en tu catálogo.

### Paso 3 — Ajustar la tabla de precios

`tabla_precios.csv` traduce el precio de oferta de PS Store a tus dos precios de venta:

```csv
desde,hasta,precio_primaria,precio_secundaria,compare_primaria,compare_secundaria
10000,14999,11990,7990,17990,12990
```

- `desde`/`hasta`: rango del precio **rebajado** que muestra PS Store (en CLP).
- `precio_primaria` / `precio_secundaria`: lo que se escribe en `price` de cada variante.
- `compare_primaria` / `compare_secundaria`: lo que se escribe en `compareAtPrice`
  (el precio tachado). Si los dejas vacíos, no se toca el `compareAtPrice`.

Los valores que vienen son un **punto de partida**: ajústalos a tu margen real.
El script valida que los tramos no se solapen y avisa si un precio queda fuera de
todos los tramos (sale en `revisar_*.csv`).

### Paso 4 — Construir mapeo.csv

```bash
python3 cjm_precios_ps.py --bootstrap --paginas 10
```

Lee tu catálogo de Shopify **en solo lectura** y propone filas donde el nombre
coincide exactamente. Todo lo demás queda en "no estoy seguro" con el motivo.

Salidas en `reportes/`:
- `mapeo_propuesto_*.csv` → filas seguras, listas para revisar;
- `mapeo_dudoso_*.csv` → lo que no se atrevió a emparejar.

**Revisa el propuesto con calma** (ojo con secuelas y ediciones: FC 25 no es
FC 26, Hellblade no es Hellblade II, Mortal Kombat 1 no es Mortal Kombat 11).
Cuando esté bien:

```bash
python3 cjm_precios_ps.py --fusionar-mapeo reportes/mapeo_propuesto_20260731-1200.csv
```

Respalda `mapeo.csv` antes de agregar nada, y nunca duplica filas existentes.

### Paso 5 — Corrida en seco completa

```bash
python3 cjm_precios_ps.py
```

Recorre las 152 páginas, calcula todo y **no escribe nada**. Revisa:

- `reportes/cambios_FECHA.csv` → qué precio quedaría en cada variante;
- `reportes/revisar_FECHA.csv` → juegos nuevos sin mapear, precios fuera de tramo,
  variantes faltantes.

Toma media hora larga. Para una prueba corta: `--paginas 3`.

### Paso 6 — Aplicar de verdad

Cuando los CSV se vean correctos:

1. Abre `cjm_precios_ps.py` y cambia `DRY_RUN = True` por `DRY_RUN = False`.
2. Corre:

```bash
python3 cjm_precios_ps.py --aplicar
```

La columna `estado` de `cambios_*.csv` dirá `aplicado` o `error: …` por variante.

---

## 5. Dejarlo automático (launchd — solo macOS)

> En Replit esto no aplica: launchd no existe en Linux. Usa un **Scheduled
> Deployment**, explicado en [REPLIT.md](REPLIT.md#4-automatizarlo-scheduled-deployment).

```bash
# 1. poner la ruta real (no la escribas de memoria)
sed -i '' "s|__HOME__|$HOME|g" com.cjm.precios.plist

# 2. permisos de ejecución
chmod +x correr.sh

# 3. instalar el agente
cp com.cjm.precios.plist ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.cjm.precios.plist 2>/dev/null
launchctl load  ~/Library/LaunchAgents/com.cjm.precios.plist

# 4. confirmar que quedó registrado
launchctl list | grep com.cjm.precios

# 5. probarlo una vez a mano
launchctl start com.cjm.precios
tail -f reportes/cron.log
```

`correr.sh` usa `--aplicar`, o sea **escribe en Shopify**. Si prefieres que el
automático solo simule, cambia esa bandera por `--solo-simular` dentro de
`correr.sh`.

### Permisos de macOS

launchd corre Chrome en segundo plano, y macOS puede pedir permisos:

- **Acceso Total al Disco** para `/bin/bash` o para Terminal
  (Ajustes → Privacidad y seguridad → Acceso total al disco), si el script no
  logra escribir en `reportes/`.
- **Automatización** para las notificaciones de `osascript`: aparece un diálogo
  la primera vez. Si launchd corre sin sesión gráfica activa, la notificación se
  omite sin romper nada.
- La primera corrida bajo launchd puede tardar más porque descarga el chromedriver.

Para desactivarlo:

```bash
launchctl unload ~/Library/LaunchAgents/com.cjm.precios.plist
```

---

## 6. Mantenimiento

**Cada quincena**, revisa `reportes/revisar_*.csv`. Los "juego nuevo sin mapear"
son juegos que salieron en oferta y todavía no están en `mapeo.csv`: corre
`--bootstrap`, revisa las propuestas y fusiona.

### ⚠️ El sistema solo BAJA precios

Cuando PS Store termina una promoción, **nadie devuelve el precio normal en
Shopify**: el juego se queda rebajado. El script no lo revierte solo, porque
sería escribir precios que nadie revisó.

Lo que sí hace: guarda en `estado.json` qué juegos estaban en oferta y a qué
precio normal, y cuando uno desaparece de las ofertas lo reporta en
`revisar_*.csv` con el motivo **"salió de oferta: revisa el precio en Shopify"**
y el precio original. Esas filas son tu lista de tareas manual cada quincena.

Si borras `estado.json`, se pierde la memoria y esos avisos no salen hasta la
siguiente vuelta completa.

**Si una corrida devuelve 0 productos por página**, PS Store cambió su HTML.
Corre `--diagnostico --ver-navegador`, mira el HTML guardado y ajusta la
extracción. No toques la lógica de precios ni la parte de Shopify: son
independientes.

---

## 7. Referencia de comandos

| Comando | Qué hace |
|---|---|
| `--diagnostico` | Inspecciona el sitio real, guarda HTML y estado de Apollo. |
| `--muestra-filtro` | Audita el filtro: aceptados vs descartados con motivo. |
| `--bootstrap` | Propone filas de `mapeo.csv` (Shopify en solo lectura). |
| `--fusionar-mapeo ARCHIVO` | Agrega a `mapeo.csv` un propuesto ya revisado. |
| *(sin banderas)* | Corrida completa en seco. |
| `--aplicar` | Escribe en Shopify (requiere `DRY_RUN = False`). |
| `--solo-simular` | Anula `--aplicar` pase lo que pase. |
| `--paginas N` | Limita las páginas por categoría, para pruebas. |
| `--categoria ID` | Usa solo esa categoría de PS Store. |
| `--ver-navegador` | Muestra Chrome en vez de correr headless. |
