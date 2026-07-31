# Desplegar en Replit

Replit es **Linux**. Tres piezas de este repo son exclusivas de macOS y **no
aplican acá**:

| Pieza | En Replit |
|---|---|
| Llavero (`security find-generic-password`) | Se reemplaza por **Secrets** (`CJM_SHOPIFY_TOKEN`) |
| `com.cjm.precios.plist` (launchd) | Se reemplaza por **Scheduled Deployment** |
| Notificación con `osascript` | No existe; `correr.sh` la omite sin romperse |

El paso a paso de `INSTALACION.md` sigue siendo válido para tu Mac. Este archivo
es el equivalente para Replit.

---

## 1. Secrets

En la pestaña **Secrets** (🔒) del Repl:

| Clave | Valor |
|---|---|
| `CJM_SHOPIFY_TOKEN` | `shpat_...` — token de tu app privada de Shopify |
| `CJM_SHOPIFY_TIENDA` | `cjm-digitales.myshopify.com` (solo si tu dominio es otro) |

El script lee `CJM_SHOPIFY_TOKEN` antes que el Llavero, así que en Replit
funciona sin tocar nada. **Nunca** pongas el token en un archivo del repo: el
`.gitignore` cubre `.env`, pero un Secret es más seguro.

Permisos necesarios en la app de Shopify: `read_products`, `write_products`.

## 2. Dependencias

`replit.nix` ya declara Python 3.11, `chromium` y `chromedriver` del mismo canal
de Nix — que coincidan es obligatorio, si no Selenium falla con
*"This version of ChromeDriver only supports Chrome version NNN"*.

En la Shell del Repl:

```bash
pip install -r requirements.txt
python3 pruebas_offline.py     # 55 pruebas, sin red ni Shopify
```

Si las 55 pasan, la lógica está sana.

## 3. Verificar el scraper contra PS Store

Replit **sí** tiene salida a internet, así que acá es donde por fin se valida lo
que ningún entorno anterior pudo:

```bash
python3 cjm_precios_ps.py --diagnostico
```

Te dirá si funcionó el plan A (Apollo) o el plan B (DOM), cuántos productos sacó
por página, si existe el campo de clasificación (`FULL_GAME` / `ADD_ON`) y una
muestra de precios para que compares a mano. Guarda el HTML renderizado en
`reportes/` por si hay que ajustar selectores.

Luego:

```bash
python3 cjm_precios_ps.py --muestra-filtro --paginas 4   # auditar el filtro
python3 cjm_precios_ps.py --paginas 3                    # prueba corta en seco
```

## 4. Automatizarlo (Scheduled Deployment)

En vez de launchd:

1. **Deploy** → **Scheduled**.
2. Comando: `bash correr.sh`
3. Frecuencia: `0 9 1,15 * *` (días 1 y 15 a las 09:00 UTC — **ojo, UTC**: para
   las 09:00 de Chile usa `0 12 1,15 * *` en horario de invierno y
   `0 13 1,15 * *` en horario de verano).
4. Los Secrets del Repl se heredan en el deployment.

`correr.sh` corre con `--aplicar`, o sea escribe en Shopify (siempre que hayas
puesto `DRY_RUN = False`). Si quieres que el automático solo simule, cambia esa
bandera por `--solo-simular` dentro de `correr.sh`.

### ⚠️ Los reportes no sobreviven al Scheduled Deployment

Cada corrida programada arranca en un contenedor limpio y **su disco se borra al
terminar**. Los CSV de `reportes/` se pierden, y son justamente lo que necesitas
revisar cada quincena (`revisar_*.csv` con los juegos nuevos sin mapear).

Opciones, de más simple a más robusta:

- **Correrlo a mano desde la Shell del Repl** en vez de programarlo: ahí el disco
  sí persiste. Es lo más razonable para una tarea quincenal.
- Programarlo, pero que el script mande el resumen a algún lado (correo, un
  webhook de Slack, una hoja de cálculo).
- Guardar los CSV en Replit Object Storage o en una base.

Mientras no resuelvas esto, **usa la opción 1**: un Scheduled Deployment que
escribe precios y tira los reportes a la basura te deja sin forma de auditar qué
cambió.

## 5. Diferencias de comandos con INSTALACION.md

| `INSTALACION.md` dice (macOS) | En Replit |
|---|---|
| `security add-generic-password ...` | Secret `CJM_SHOPIFY_TOKEN` |
| `sed -i '' "s\|__HOME__\|$HOME\|g" ...` | No aplica (es para el plist) |
| `launchctl load ...` | Scheduled Deployment |
| `pip3` / `python3` | Igual |

`sed -i ''` es sintaxis de BSD y falla en Linux; el equivalente GNU es
`sed -i "s|...|...|g"` sin las comillas vacías. Solo lo necesitas si vas a usar
el plist en tu Mac.
