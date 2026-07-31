#!/bin/bash
# Lanzador para launchd. Corre el script, deja registro y avisa por notificacion.
#
# OJO: la linea de abajo lleva --aplicar, o sea escribe precios en Shopify de
# verdad (siempre que DRY_RUN este en False dentro de cjm_precios_ps.py).
# Si quieres que el automatico solo simule, cambia --aplicar por --solo-simular.

set -uo pipefail

CARPETA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$CARPETA" || exit 1

mkdir -p reportes
REGISTRO="reportes/cron.log"

# launchd arranca con un PATH minimo: agregamos las rutas usuales de Homebrew.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

PYTHON="$(command -v python3 || echo /usr/bin/python3)"

{
  echo ""
  echo "=============================================================="
  echo "Corrida: $(date '+%Y-%m-%d %H:%M:%S')"
  echo "=============================================================="
} >> "$REGISTRO"

"$PYTHON" cjm_precios_ps.py --aplicar >> "$REGISTRO" 2>&1
ESTADO=$?

ULTIMO_CAMBIOS="$(ls -t reportes/cambios_*.csv 2>/dev/null | head -1)"
ULTIMO_REVISAR="$(ls -t reportes/revisar_*.csv 2>/dev/null | head -1)"

# -1 por la fila de encabezado.
contar() { [ -f "$1" ] && echo $(( $(wc -l < "$1") - 1 )) || echo 0; }
CAMBIOS=$(contar "$ULTIMO_CAMBIOS")
REVISAR=$(contar "$ULTIMO_REVISAR")

case $ESTADO in
  0)
    TITULO="CJM precios: listo"
    MENSAJE="${CAMBIOS} precios actualizados, ${REVISAR} por revisar"
    ;;
  2)
    # El scraper no extrajo NI UN producto: Chrome no arranco, PS Store cambio,
    # o la red esta bloqueada. Esto antes se anunciaba como exito.
    TITULO="CJM precios: NO se extrajo nada"
    MENSAJE="El scraper devolvio 0 productos. Corre --diagnostico"
    ;;
  *)
    TITULO="CJM precios: con errores"
    MENSAJE="Codigo ${ESTADO}. Revisa reportes/cron.log y cambios_*.csv"
    ;;
esac

echo "$(date '+%H:%M:%S') ${TITULO} — ${MENSAJE}" >> "$REGISTRO"

if command -v osascript >/dev/null 2>&1; then
  osascript -e "display notification \"${MENSAJE}\" with title \"${TITULO}\"" || true
fi

exit $ESTADO
