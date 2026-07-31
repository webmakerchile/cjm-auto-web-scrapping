# Prompts para Claude Code — Automatización de precios CJM Digitales

Claude Code corre **en tu Mac**, no en un servidor. Eso significa que sí puede
entrar a store.playstation.com, abrir Chrome de verdad y comprobar si el scraper
funciona. Eso es justo lo que yo no pude hacer desde acá.

Documentación: https://docs.claude.com/en/docs/claude-code/overview

Antes de empezar: deja los 5 archivos (`cjm_precios_ps.py`, `tabla_precios.csv`,
`correr.sh`, `com.cjm.precios.plist`, `INSTALACION.md`) dentro de
`~/CJM-precios`, abre Terminal ahí y arranca Claude Code.

---

## PROMPT 1 — Instalación y validación

El más importante. Pégalo completo.

```
Estoy automatizando la actualización de precios de ofertas PS4/PS5 de mi tienda
Shopify (CJM Digitales, Chile). En esta carpeta ya están los archivos del
sistema. Léelos completos antes de tocar nada, sobre todo INSTALACION.md que
explica la arquitectura.

Resumen: cjm_precios_ps.py recorre categorías de store.playstation.com/es-cl,
filtra para quedarse solo con juegos completos, convierte el precio USD a dos
precios CLP (Cuenta Primaria y Cuenta Secundaria) usando tabla_precios.csv, y
actualiza Shopify por Admin API moviendo el precio normal a compareAtPrice y el
de oferta a price.

TU TRABAJO PRINCIPAL es validar que el scraper realmente funciona contra el
sitio en vivo. Yo escribí ese código sin poder probarlo porque el entorno donde
lo generé no tiene acceso a PlayStation Store. Tú sí lo tienes.

Haz esto en orden:

1. Verifica el entorno: python3, pip3, Chrome instalado. Instala lo que falte
   con: pip3 install --upgrade selenium webdriver-manager requests

2. Prueba el scraping SOLO con 3 páginas, no con 152. Modifica temporalmente
   CATEGORIAS a ("3f772501-f6f8-49b7-abac-874a88ca4897", 3) y córrelo. Quiero
   saber concretamente:
   - ¿Cuántos productos devuelve por página? Deberían ser 24.
   - ¿Funcionó la extracción vía JS_APOLLO / desde_apollo(), o cayó al plan B
     desde_dom()? Dímelo explícitamente, es importante.
   - ¿Los precios que saca son el precio CON descuento o el precio normal?
     Necesito el precio final rebajado. Compáralo abriendo la página en Chrome
     normal y mirando 3 o 4 juegos a mano.
   - Si desde_apollo() sí funciona, ¿trae el campo de clasificación del producto
     (GAME / ADDON / etc.)? Si lo trae, dime cómo se llama exactamente, porque
     ese campo es mucho mejor filtro que la lista de palabras clave.

3. Si algo de eso falla, arregla los selectores CSS o el parseo del estado de
   Apollo hasta que funcione. Inspecciona el HTML real, no adivines. Documenta
   en un comentario en el código qué cambiaste y por qué.

4. Cuando el scraping funcione, verifica el filtro de juegos: imprime una
   muestra de 40 productos descartados y 40 aceptados. Quiero ver si está
   botando juegos completos por error o dejando pasar DLC.

5. Deja CATEGORIAS de vuelta en 152 páginas antes de terminar.

REGLAS:
- NO me pidas el token de Shopify ni lo escribas en ningún archivo. Yo lo pongo
  en el Llavero de macOS. El script ya lo lee de ahí.
- NO ejecutes nada contra Shopify que escriba datos. Solo lectura. DRY_RUN se
  queda en True.
- NO reescribas el script completo. Haz cambios quirúrgicos y explícame cada uno.
- NO inventes emparejamientos entre juegos de PS Store y productos de Shopify.
  Ese cruce lo hace --bootstrap y lo reviso yo.
- Si algo te bloquea, pregúntame. En lo demás avanza sin consultarme.

Al final dame un resumen corto en español: qué funcionó, qué arreglaste, y qué
tengo que hacer yo antes de correr --bootstrap.
```

---

## PROMPT 2 — Si el scraper deja de funcionar

PlayStation cambia su sitio cada tanto. Cuando la corrida devuelva 0 productos
por página, pega esto:

```
El scraper de cjm_precios_ps.py dejó de encontrar productos: está devolviendo 0
por página. PlayStation Store probablemente cambió su HTML o su estructura
interna de datos.

Diagnostica y arregla:
1. Abre https://store.playstation.com/es-cl/category/3f772501-f6f8-49b7-abac-874a88ca4897/1
   con Selenium sin headless para que yo pueda ver, y guarda el HTML renderizado
   en un archivo para inspeccionarlo.
2. Averigua dónde están ahora los nombres y los precios: en qué selectores del
   DOM, o en qué script JSON embebido.
3. Actualiza JS_APOLLO, desde_apollo() y/o desde_dom() según corresponda.
4. Prueba con 3 páginas y confirma que devuelve 24 productos por página con el
   precio rebajado correcto.

No toques la lógica de precios, el filtro ni la parte de Shopify. Solo la
extracción. Explícame qué cambió en el sitio.
```

---

## PROMPT 3 — Mantenimiento quincenal (juegos nuevos)

Cuando en `revisar_FECHA.csv` aparezcan juegos nuevos que sí quieres poner en
oferta:

```
En reportes/ está el último revisar_*.csv con juegos que salieron en oferta en
PS Store pero que todavía no están en mapeo.csv, así que no se les aplicó precio.

Necesito que:
1. Leas ese CSV y saques la lista de "juego nuevo sin mapear".
2. Para cada uno, busques en mi Shopify el producto que le corresponde. Usa la
   Admin API en modo SOLO LECTURA (el token está en el Llavero de macOS, servicio
   cjm_shopify_token; el script tiene la función _token() que ya lo lee).
3. Me propongas las filas nuevas para mapeo.csv, con producto_id y los IDs de las
   variantes Cuenta Primaria y Cuenta Secundaria.

CRÍTICO: no adivines. Si el nombre de PS Store no corresponde con certeza a un
producto de Shopify, déjalo fuera y dímelo. Cuidado especial con secuelas y
ediciones: FC 25 no es FC 26, Code Vein no es Code Vein II, Hellblade no es
Hellblade II, Mortal Kombat 1 no es Mortal Kombat 11, Monster Hunter Stories no
es Monster Hunter Stories 3. Si dudas, va a la lista de "no estoy seguro".

Muéstrame las filas propuestas en una tabla antes de escribir nada en mapeo.csv.
Respalda mapeo.csv antes de modificarlo.
```

---

## PROMPT 4 — Activar el automático

Después de que una corrida en seco te haya dejado conforme:

```
Ya validé una corrida en modo simulación y los precios de cambios_*.csv se ven
correctos. Ahora quiero dejarlo corriendo solo con launchd.

1. En com.cjm.precios.plist reemplaza la ruta de usuario por la real (sácala con
   echo $HOME, no la asumas).
2. chmod +x correr.sh
3. Copia el plist a ~/Library/LaunchAgents/ y cárgalo con launchctl.
4. Verifica con launchctl list que quedó registrado como com.cjm.precios.
5. Córrelo una vez manualmente con launchctl start com.cjm.precios y confirma
   que reportes/cron.log se está escribiendo y que la notificación de macOS
   aparece.

Ojo: correr.sh usa --aplicar, o sea escribe en Shopify de verdad. Antes de
cargarlo confírmame que estoy de acuerdo con eso. Si prefiero que el automático
solo simule, quítale --aplicar y dímelo.

Avísame también si macOS necesita algún permiso extra (Acceso Total al Disco
para Terminal, o permisos de automatización) para que launchd pueda correr
Chrome en segundo plano.
```

---

## Un detalle que vale la pena

En el Prompt 1, el punto sobre el campo de clasificación de PS Store
(`GAME` / `ADDON`) es el que más puede mejorar el sistema. Mi filtro por palabras
clave acierta en casi todo, pero se le escapan DLC con nombres limpios — por
ejemplo *"Assassin's Creed Valhalla - La Ira de los Druidas"* no tiene ninguna
palabra sospechosa. Hoy eso no rompe nada porque `mapeo.csv` lo frena igual (un
DLC nunca va a estar en tu catálogo), pero si PS Store entrega ese campo, el
filtro pasa de bueno a exacto y `revisar.csv` te llega mucho más limpio.
