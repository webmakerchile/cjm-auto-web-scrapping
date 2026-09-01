#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CJM Digitales — actualizacion automatica de precios de ofertas PS4/PS5.

Flujo general
-------------
1. Recorre las categorias de ofertas de store.playstation.com/es-cl con Selenium.
2. Se queda solo con juegos completos (descarta DLC, temas, monedas virtuales,
   suscripciones, demos).
3. Convierte el precio de oferta en dos precios de venta usando tramos definidos
   en tabla_precios.csv (Cuenta Primaria y Cuenta Secundaria).
4. Cruza con mapeo.csv (juego de PS Store -> producto/variantes de Shopify).
5. Actualiza Shopify por Admin API: el precio normal queda en compareAtPrice y
   el precio de oferta en price.

Seguridad
---------
- El token de Shopify NUNCA se guarda en disco. Se lee del Llavero de macOS
  (servicio cjm_shopify_token) o de la variable de entorno CJM_SHOPIFY_TOKEN.
- Por defecto el script corre en seco (DRY_RUN). Solo escribe en Shopify si se
  pasa --aplicar de forma explicita.
- Nunca inventa emparejamientos: si un juego no esta en mapeo.csv, se reporta en
  revisar_FECHA.csv y no se toca nada.

Uso
---
    python3 cjm_precios_ps.py                      # corrida en seco completa
    python3 cjm_precios_ps.py --paginas 3          # prueba corta
    python3 cjm_precios_ps.py --diagnostico        # inspecciona el sitio real
    python3 cjm_precios_ps.py --muestra-filtro     # audita el filtro de juegos
    python3 cjm_precios_ps.py --bootstrap          # propone filas de mapeo.csv
    python3 cjm_precios_ps.py --aplicar            # escribe en Shopify
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------------------

BASE = Path(__file__).resolve().parent
REPORTES = BASE / "reportes"
ARCHIVO_TABLA = BASE / "tabla_precios.csv"
ARCHIVO_MAPEO = BASE / "mapeo.csv"
# Que juegos estaban en oferta la corrida anterior, para poder avisar cuando una
# promocion termina (el sistema solo baja precios, nunca los sube solo).
ARCHIVO_ESTADO = BASE / "estado.json"

# Categorias de PS Store a recorrer: (id_categoria, cantidad_de_paginas).
# El id largo es la categoria de ofertas de la tienda chilena.
CATEGORIAS: tuple[tuple[str, int], ...] = (
    ("3f772501-f6f8-49b7-abac-874a88ca4897", 152),
)

LOCALE = "es-cl"
URL_CATEGORIA = "https://store.playstation.com/{locale}/category/{cat}/{pagina}"

# La tienda es-cl publica precios en pesos chilenos. Si algun dia PS Store
# devuelve otra moneda, el script avisa en vez de convertir a ciegas.
MONEDA_ESPERADA = "CLP"

# Shopify
TIENDA = os.environ.get("CJM_SHOPIFY_TIENDA", "cjm-digitales.myshopify.com")
# Shopify soporta cada version por 12 meses. Si esta expira, la API sirve otra
# version en silencio y el comportamiento puede cambiar sin avisar: convive
# revisar https://shopify.dev/docs/api/usage/versioning una vez al ano.
API_VERSION = os.environ.get("CJM_SHOPIFY_API_VERSION", "2026-04")
SERVICIO_LLAVERO = "cjm_shopify_token"
REINTENTOS_SHOPIFY = 5
PAUSA_ENTRE_LLAMADAS = 0.5   # segundos, para no drenar el bucket de puntos
FALLOS_SEGUIDOS_MAX = 5      # cortacircuito al aplicar precios

# Escribir en Shopify solo cuando se pide explicitamente con --aplicar.
DRY_RUN = True

ESPERA_PAGINA = 2.5      # segundos de espera tras cargar cada pagina
REINTENTOS_PAGINA = 2    # reintentos por pagina antes de darla por perdida
PRODUCTOS_POR_PAGINA = 24  # lo que PS Store devuelve normalmente
TIMEOUT_PAGINA = 45      # segundos maximos esperando que cargue una pagina
TIMEOUT_SCRIPT = 30      # segundos maximos para execute_script


# ---------------------------------------------------------------------------
# EXTRACCION DEL ESTADO DE APOLLO (plan A)
# ---------------------------------------------------------------------------

# PS Store es un Next.js: el HTML inicial trae un <script id="__NEXT_DATA__">
# con la cache normalizada de Apollo. Preferimos leer eso antes que el DOM
# porque incluye la clasificacion del producto y los precios ya separados.
#
# Devolvemos el estado como TEXTO y no como objeto: chromedriver serializa los
# objetos JS ordenando las claves alfabeticamente, y con eso 'Concept:...'
# siempre le gana a 'Product:...' al deduplicar. El Concept no trae
# clasificacion, asi que el filtro exacto de DLC no correria nunca en
# produccion. Con JSON.stringify el orden de insercion se respeta.
JS_APOLLO = """
function buscarEstado() {
  const el = document.getElementById('__NEXT_DATA__');
  if (el && el.textContent) {
    try {
      const d = JSON.parse(el.textContent);
      const p = d.props || {};
      if (p.apolloState) return p.apolloState;
      if (p.pageProps && p.pageProps.apolloState) return p.pageProps.apolloState;
      if (p.initialApolloState) return p.initialApolloState;
      if (p.pageProps && p.pageProps.initialApolloState) return p.pageProps.initialApolloState;
    } catch (e) { /* seguimos con los otros intentos */ }
  }
  if (window.__APOLLO_STATE__) return window.__APOLLO_STATE__;
  if (window.__NEXT_DATA__ && window.__NEXT_DATA__.props) {
    const p = window.__NEXT_DATA__.props;
    if (p.apolloState) return p.apolloState;
    if (p.pageProps && p.pageProps.apolloState) return p.pageProps.apolloState;
  }
  return null;
}
const estado = buscarEstado();
return estado ? JSON.stringify(estado) : null;
"""

# Claves de la cache de Apollo que representan un producto vendible.
PREFIJOS_PRODUCTO = ("Product:", "Concept:", "ConceptRetail:", "ProductRetail:")

# Nombres posibles del campo de clasificacion. PS Store lo ha llamado distinto
# segun la version del front, asi que probamos varios.
CAMPOS_CLASIFICACION = (
    "storeDisplayClassification",
    "localizedStoreDisplayClassification",
    "topCategory",
    "classification",
    "productType",
)

# Clasificaciones que SI son juego completo.
CLASIF_JUEGO = {
    "FULL_GAME",
    "GAME",
    "GAME_BUNDLE",
    "PREMIUM_EDITION",
    "PS_VR_GAME",
    "BUNDLE",
}

# Clasificaciones que NO queremos tocar.
CLASIF_DESCARTE = {
    "ADD_ON",
    "ADDON",
    "SEASON_PASS",
    "DEMO",
    "THEME",
    "AVATAR",
    "VIRTUAL_CURRENCY",
    "CURRENCY",
    "SUBSCRIPTION",
    "VIDEO",
    "APP",
    "TRIAL",
}

# Plan C: si no hay clasificacion, filtramos por palabras. Menos exacto.
PALABRAS_DESCARTE = (
    "dlc", "add-on", "addon", "complemento", "expansion", "expansión",
    "pase de temporada", "season pass", "pase de batalla", "battle pass",
    "paquete de", "pack de", "moneda", "monedas", "creditos", "créditos",
    "puntos", "coins", "currency", "v-bucks", "vbucks", "gemas",
    "tema ", "theme", "avatar", "banda sonora", "soundtrack",
    "demo", "prueba gratuita", "beta", "suscripcion", "suscripción",
    "playstation plus", "ps plus", "upgrade", "mejora a ps5", "actualizacion a ps5",
    "skin", "aspecto", "personaje adicional", "mapa adicional",
)


# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# PS Store llena los titulos de simbolos legales. Hay que sacarlos ANTES de
# normalizar: NFKD convierte '™' en las letras 'tm', y entonces
# "Pokemon™ Legends" no cruza nunca con "Pokemon Legends" de Shopify.
SIMBOLOS_LEGALES = str.maketrans({c: " " for c in "™®©℠"})


def normalizar(texto: str) -> str:
    """Minusculas, sin tildes y sin puntuacion. Para comparar nombres."""
    texto = (texto or "").translate(SIMBOLOS_LEGALES)
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.lower()
    texto = re.sub(r"[^a-z0-9]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


# Monedas sin decimales: el separador que aparezca es de miles, no decimal.
MONEDAS_SIN_DECIMALES = {"CLP", "COP", "PYG", "JPY", "KRW", "VND", "ISK"}


def parse_precio(texto: str | None, moneda: str | None) -> Decimal | None:
    """Convierte '$ 29.990' o 'US$29.99' en Decimal, segun la moneda."""
    if not texto:
        return None
    limpio = re.sub(r"[^\d.,]", "", str(texto))
    if not limpio:
        return None

    if (moneda or "").upper() in MONEDAS_SIN_DECIMALES:
        # CLP no usa decimales: todo separador es de miles.
        solo_digitos = re.sub(r"\D", "", limpio)
        return Decimal(solo_digitos) if solo_digitos else None

    # Moneda con decimales: si los ultimos dos digitos van tras un separador,
    # ese separador es el decimal ('1.234,56' y '1,234.56' se leen igual).
    if re.search(r"[.,]\d{2}$", limpio):
        entero = re.sub(r"\D", "", limpio[:-3]) or "0"
        try:
            return Decimal(f"{entero}.{limpio[-2:]}")
        except InvalidOperation:
            return None
    solo_digitos = re.sub(r"\D", "", limpio)
    return Decimal(solo_digitos) if solo_digitos else None


def valor_apollo(valor: Any, moneda: str | None) -> Decimal | None:
    """PS Store entrega *Value en unidades menores (x100). Lo normalizamos."""
    if valor is None:
        return None
    try:
        d = Decimal(str(valor)) / 100
    except InvalidOperation:
        return None
    if (moneda or "").upper() in MONEDAS_SIN_DECIMALES:
        return d.quantize(Decimal("1"))
    return d


def a_entero_clp(valor: Decimal) -> int:
    return int(valor.quantize(Decimal("1")))


# ---------------------------------------------------------------------------
# MODELO
# ---------------------------------------------------------------------------

@dataclass
class JuegoPS:
    ps_id: str
    nombre: str
    clasificacion: str | None
    moneda: str | None
    precio_normal: Decimal | None
    precio_oferta: Decimal | None
    descuento: str | None = None
    origen: str = "apollo"          # apollo | dom
    plataformas: list[str] = field(default_factory=list)
    es_producto: bool = False       # vino de un Product (no de un Concept)

    @property
    def en_oferta(self) -> bool:
        return (
            self.precio_oferta is not None
            and self.precio_normal is not None
            and self.precio_oferta < self.precio_normal
        )

    @property
    def precio_referencia(self) -> Decimal | None:
        """El precio que usamos para buscar tramo: siempre el rebajado."""
        return self.precio_oferta if self.precio_oferta is not None else self.precio_normal


@dataclass
class ResultadoScrape:
    """Lo raspado mas el estado de la corrida.

    Se comporta como una lista de juegos para que el resto del codigo (y las
    pruebas) puedan seguir iterandolo y midiendolo directamente.
    """
    juegos: list[JuegoPS]
    paginas_error: int = 0
    paginas_dom: int = 0
    paginas_apollo: int = 0

    def __iter__(self):
        return iter(self.juegos)

    def __len__(self) -> int:
        return len(self.juegos)


@dataclass
class Tramo:
    desde: Decimal
    hasta: Decimal
    precio_primaria: int
    precio_secundaria: int
    compare_primaria: int | None = None
    compare_secundaria: int | None = None


@dataclass
class FilaMapeo:
    ps_id: str
    ps_nombre: str
    producto_id: str
    variante_primaria: str
    variante_secundaria: str
    activo: bool = True
    problema: str | None = None   # id de Shopify mal formado, etc.


# ---------------------------------------------------------------------------
# SCRAPER
# ---------------------------------------------------------------------------

def _buscar_ejecutable(*nombres: str) -> str | None:
    """Primer ejecutable de la lista que exista en el PATH."""
    for nombre in nombres:
        ruta = shutil.which(nombre)
        if ruta:
            return ruta
    return None


def abrir_navegador(headless: bool = True):
    """Crea el driver de Chrome. Importamos aca para no exigir Selenium
    en los modos que no scrapean (por ejemplo --fusionar-mapeo)."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opciones = Options()
    if headless:
        opciones.add_argument("--headless=new")
    opciones.add_argument("--window-size=1440,2400")
    opciones.add_argument("--disable-gpu")
    opciones.add_argument("--no-sandbox")
    # Sin esto Chrome se cae en cualquier contenedor (Replit, Docker): el
    # /dev/shm por defecto es de 64 MB y no le alcanza.
    opciones.add_argument("--disable-dev-shm-usage")
    opciones.add_argument("--disable-blink-features=AutomationControlled")
    opciones.add_argument("--lang=es-CL")
    opciones.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    opciones.add_experimental_option("excludeSwitches", ["enable-automation"])

    # En Replit y en cualquier contenedor con Nix, Chrome y el chromedriver no
    # estan donde Selenium los busca. Primero miramos las variables de entorno
    # y despues el PATH, que es donde los deja Nix.
    binario = os.environ.get("CJM_CHROME_BINARY") or _buscar_ejecutable(
        "chromium", "chromium-browser", "google-chrome", "google-chrome-stable"
    )
    if binario:
        opciones.binary_location = binario
        log(f"Chrome: {binario}")
    ruta_driver = os.environ.get("CJM_CHROMEDRIVER") or _buscar_ejecutable("chromedriver")

    if ruta_driver:
        driver = webdriver.Chrome(service=Service(ruta_driver), options=opciones)
    else:
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opciones)
        except Exception as e:  # noqa: BLE001 - Selenium 4.6+ trae driver propio
            log(f"webdriver-manager no disponible ({e}); uso Selenium Manager")
            driver = webdriver.Chrome(options=opciones)

    # Sin timeout, una pagina colgada deja el proceso esperando para siempre y
    # launchd o el scheduler nunca se entera.
    driver.set_page_load_timeout(TIMEOUT_PAGINA)
    driver.set_script_timeout(TIMEOUT_SCRIPT)
    return driver


def _resolver(nodo: Any, estado: dict, profundidad: int = 0) -> Any:
    """Apollo normaliza con {'__ref': 'Price:xyz'}. Esto lo desreferencia."""
    if profundidad > 6:
        return nodo
    if isinstance(nodo, dict):
        if "__ref" in nodo and isinstance(nodo["__ref"], str):
            return _resolver(estado.get(nodo["__ref"], {}), estado, profundidad + 1)
        return {k: _resolver(v, estado, profundidad + 1) for k, v in nodo.items()}
    if isinstance(nodo, list):
        return [_resolver(v, estado, profundidad + 1) for v in nodo]
    return nodo


def leer_apollo(driver) -> dict | None:
    """Ejecuta JS_APOLLO y devuelve el estado ya parseado (o None)."""
    crudo = driver.execute_script(JS_APOLLO)
    if not crudo:
        return None
    if isinstance(crudo, dict):  # driver antiguo que no respeta el stringify
        return crudo
    try:
        return json.loads(crudo)
    except (TypeError, ValueError) as e:
        log(f"  no pude parsear el estado de Apollo: {e}")
        return None


def _buscar_bloque_precio(nodo: dict) -> dict | None:
    """Encuentra el sub-diccionario que trae basePrice/discountedPrice."""
    marcas = ("basePrice", "discountedPrice", "basePriceValue", "discountedValue")
    if any(m in nodo for m in marcas):
        return nodo
    for valor in nodo.values():
        if isinstance(valor, dict):
            hallado = _buscar_bloque_precio(valor)
            if hallado:
                return hallado
        elif isinstance(valor, list):
            for item in valor:
                if isinstance(item, dict):
                    hallado = _buscar_bloque_precio(item)
                    if hallado:
                        return hallado
    return None


def desde_apollo(estado: dict) -> list[JuegoPS]:
    """Plan A: leer la cache de Apollo embebida en la pagina."""
    juegos: list[JuegoPS] = []
    if not isinstance(estado, dict):
        return juegos

    for clave, bruto in estado.items():
        if not isinstance(bruto, dict):
            continue
        if not clave.startswith(PREFIJOS_PRODUCTO):
            continue

        entrada = _resolver(bruto, estado)
        nombre = entrada.get("name") or entrada.get("title")
        if not nombre:
            continue

        bloque = _buscar_bloque_precio(entrada) or {}
        moneda = bloque.get("currencyCode") or entrada.get("currencyCode")

        normal = parse_precio(bloque.get("basePrice"), moneda)
        if normal is None:
            normal = valor_apollo(bloque.get("basePriceValue"), moneda)
        oferta = parse_precio(bloque.get("discountedPrice"), moneda)
        if oferta is None:
            oferta = valor_apollo(bloque.get("discountedValue"), moneda)

        clasificacion = None
        for campo in CAMPOS_CLASIFICACION:
            if entrada.get(campo):
                clasificacion = str(entrada[campo]).upper().replace(" ", "_")
                break

        plataformas = entrada.get("platforms") or entrada.get("platform") or []
        if isinstance(plataformas, str):
            plataformas = [plataformas]

        juegos.append(
            JuegoPS(
                ps_id=str(entrada.get("id") or clave.split(":", 1)[-1]),
                nombre=str(nombre).strip(),
                clasificacion=clasificacion,
                moneda=moneda,
                precio_normal=normal,
                precio_oferta=oferta,
                descuento=bloque.get("discountText"),
                origen="apollo",
                plataformas=[str(p) for p in plataformas if p],
                es_producto=clave.startswith(("Product:", "ProductRetail:")),
            )
        )

    # La cache trae el mismo juego como Product y como Concept, y cada uno trae
    # cosas distintas: el Product tiene la clasificacion, el Concept a veces
    # tiene el precio. En vez de que uno pise al otro, los fusionamos; asi el
    # resultado no depende del orden en que vengan las claves.
    unicos: dict[str, JuegoPS] = {}
    for j in juegos:
        clave = normalizar(j.nombre)
        previo = unicos.get(clave)
        unicos[clave] = _fusionar(j, previo) if previo else j
    return list(unicos.values())


def _fusionar(a: JuegoPS, b: JuegoPS) -> JuegoPS:
    """Combina dos registros del mismo juego quedandose con lo mejor de cada uno.

    El ps_id del Product es el id real de la ficha de tienda, asi que gana
    sobre el del Concept.
    """
    principal, otro = (a, b) if a.es_producto or not b.es_producto else (b, a)
    return JuegoPS(
        ps_id=principal.ps_id or otro.ps_id,
        nombre=principal.nombre,
        clasificacion=principal.clasificacion or otro.clasificacion,
        moneda=principal.moneda or otro.moneda,
        precio_normal=principal.precio_normal if principal.precio_normal is not None else otro.precio_normal,
        precio_oferta=principal.precio_oferta if principal.precio_oferta is not None else otro.precio_oferta,
        descuento=principal.descuento or otro.descuento,
        origen=principal.origen,
        plataformas=principal.plataformas or otro.plataformas,
        es_producto=principal.es_producto or otro.es_producto,
    )


def desde_dom(driver) -> list[JuegoPS]:
    """Plan B: leer las tarjetas renderizadas. Menos fiable, sin clasificacion."""
    from selenium.webdriver.common.by import By

    selectores_tarjeta = (
        "[data-qa^='ems-sdk-grid#productTile']",
        "li[data-qa^='ems-sdk-grid']",
        "a[data-track-click='ctaWithPrice']",
        "div.psw-product-tile",
    )
    tarjetas = []
    for selector in selectores_tarjeta:
        tarjetas = driver.find_elements(By.CSS_SELECTOR, selector)
        if tarjetas:
            log(f"  DOM: use el selector {selector} ({len(tarjetas)} tarjetas)")
            break
    if not tarjetas:
        return []

    juegos: list[JuegoPS] = []
    vistos: set[str] = set()
    for i, tarjeta in enumerate(tarjetas):
        try:
            texto = tarjeta.text or ""
            lineas = [l.strip() for l in texto.split("\n") if l.strip()]
            if not lineas:
                continue
            # Nombre: primera linea que no sea un precio ni un porcentaje.
            nombre = next(
                (l for l in lineas if not re.match(r"^[-+]?\s*[\d$%.,\s]+$", l)),
                "",
            )
            # El selector tambien matchea nodos hijos de la tarjeta, que
            # producen filas basura sin nombre o con un precio por nombre.
            if not nombre or "$" in nombre:
                continue

            precios = [l for l in lineas if l.count("$") == 1]
            if len(precios) > 2:
                # Tres precios = hay un precio de PS Plus de por medio. No
                # sabemos cual de los dos rebajados aplica, y adivinar publica
                # un precio mas barato que el real: mejor no tocarlo.
                log(f"  '{nombre[:40]}' trae {len(precios)} precios: la salteo (¿precio PS Plus?)")
                continue
            valores = [p for p in (parse_precio(x, MONEDA_ESPERADA) for x in precios) if p is not None]
            if not valores:
                continue
            oferta, normal = min(valores), max(valores)

            # NUNCA inventar un id. Un id posicional tipo 'dom-3' se repite en
            # cada pagina y en cada corrida apunta a un juego distinto: si entra
            # a mapeo.csv, termina escribiendole a un producto de Shopify el
            # precio de otro juego. Sin id, el cruce se hace por nombre.
            href = tarjeta.get_attribute("href") or ""
            if not href:
                try:
                    href = tarjeta.find_element(By.CSS_SELECTOR, "a[href]").get_attribute("href") or ""
                except Exception:  # noqa: BLE001
                    href = ""
            ps_id = ""
            if "/product/" in href or "/concept/" in href:
                ps_id = href.rstrip("/").split("/")[-1]

            clave = ps_id or normalizar(nombre)
            if clave in vistos:
                continue
            vistos.add(clave)

            juegos.append(
                JuegoPS(
                    ps_id=ps_id,
                    nombre=nombre,
                    clasificacion=None,
                    moneda=MONEDA_ESPERADA,
                    precio_normal=normal,
                    precio_oferta=oferta,
                    origen="dom",
                )
            )
        except Exception as e:  # noqa: BLE001 - una tarjeta rota no aborta la pagina
            log(f"  tarjeta {i} ilegible: {e}")
    return juegos


def raspar_pagina(driver, categoria: str, pagina: int) -> tuple[list[JuegoPS], str]:
    """Devuelve (juegos, origen) para una pagina de categoria.

    El origen distingue dos finales muy distintos que antes se confundian:
      'vacio' = la pagina cargo bien y no tiene productos (fin del catalogo).
      'error' = la pagina nunca cargo (timeout, red, navegador caido).
    Confundirlos hacia que un timeout se leyera como "se acabo el catalogo" y
    abandonara en silencio las 149 paginas siguientes.
    """
    url = URL_CATEGORIA.format(locale=LOCALE, cat=categoria, pagina=pagina)
    hubo_excepcion = False
    for intento in range(REINTENTOS_PAGINA + 1):
        try:
            driver.get(url)
            time.sleep(ESPERA_PAGINA)
            estado = leer_apollo(driver)
            if estado:
                juegos = desde_apollo(estado)
                if juegos:
                    return juegos, "apollo"
            juegos = desde_dom(driver)
            if juegos:
                return juegos, "dom"
            hubo_excepcion = False
            if intento < REINTENTOS_PAGINA:
                log(f"  pagina {pagina} vino vacia, reintento {intento + 1}")
                time.sleep(3 * (intento + 1))
        except Exception as e:  # noqa: BLE001
            hubo_excepcion = True
            log(f"  error en pagina {pagina}: {e}")
            if intento < REINTENTOS_PAGINA:
                time.sleep(3 * (intento + 1))
    return [], ("error" if hubo_excepcion else "vacio")


def raspar_todo(
    paginas_max: int | None = None,
    headless: bool = True,
    solo_categoria: str | None = None,
) -> list[JuegoPS]:
    categorias = ((solo_categoria, CATEGORIAS[0][1]),) if solo_categoria else CATEGORIAS
    driver = abrir_navegador(headless=headless)
    encontrados: dict[str, JuegoPS] = {}
    conteo = {"apollo": 0, "dom": 0, "vacio": 0, "error": 0}
    try:
        for categoria, paginas in categorias:
            total = paginas if paginas_max is None else min(paginas, paginas_max)
            vacias_seguidas = 0
            for pagina in range(1, total + 1):
                juegos, origen = raspar_pagina(driver, categoria, pagina)
                conteo[origen] = conteo.get(origen, 0) + 1
                log(f"pagina {pagina}/{total}: {len(juegos)} productos (via {origen})")
                for j in juegos:
                    encontrados.setdefault(normalizar(j.nombre), j)

                if origen == "error":
                    # No sabemos si hay catalogo mas alla: seguimos y lo
                    # contamos como fallo, para no abandonar 149 paginas por
                    # un timeout.
                    vacias_seguidas = 0
                    continue
                if origen == "vacio":
                    vacias_seguidas += 1
                    if vacias_seguidas >= 2:
                        log("  dos paginas vacias seguidas: doy la categoria por terminada")
                        break
                else:
                    vacias_seguidas = 0
    finally:
        driver.quit()
    log(f"origen de datos -> apollo: {conteo['apollo']} paginas, dom: {conteo['dom']}, "
        f"vacias: {conteo['vacio']}, con error: {conteo['error']}")
    if conteo["dom"]:
        log(f"AVISO: {conteo['dom']} pagina(s) cayeron al plan B (DOM): sin clasificacion "
            f"de producto el filtro de DLC es menos exacto.")
    if conteo["error"]:
        log(f"AVISO: {conteo['error']} pagina(s) nunca cargaron. El resultado esta incompleto.")
    return ResultadoScrape(
        juegos=list(encontrados.values()),
        paginas_error=conteo["error"],
        paginas_dom=conteo["dom"],
        paginas_apollo=conteo["apollo"],
    )


# ---------------------------------------------------------------------------
# FILTRO DE JUEGOS COMPLETOS
# ---------------------------------------------------------------------------

def es_juego_completo(j: JuegoPS) -> tuple[bool, str]:
    """Devuelve (es_juego, motivo). El motivo sirve para auditar el filtro."""
    if j.clasificacion:
        c = j.clasificacion.upper()
        if c in CLASIF_DESCARTE:
            return False, f"clasificacion={c}"
        if c in CLASIF_JUEGO:
            return True, f"clasificacion={c}"
        # Clasificacion desconocida: no adivinamos, cae al filtro por palabras.

    nombre = normalizar(j.nombre)
    for palabra in PALABRAS_DESCARTE:
        # Palabra completa, no substring: 'demo' NO puede descartar
        # "Demon's Souls", ni 'tema' a "Tematica".
        patron = rf"(?<![a-z0-9]){re.escape(normalizar(palabra))}(?![a-z0-9])"
        if re.search(patron, nombre):
            return False, f"palabra='{palabra}'"
    return True, "sin senales de DLC"


# ---------------------------------------------------------------------------
# TABLA DE PRECIOS
# ---------------------------------------------------------------------------

def cargar_tabla(ruta: Path | None = None) -> list[Tramo]:
    ruta = ruta or ARCHIVO_TABLA
    if not ruta.exists():
        raise SystemExit(f"Falta {ruta.name}. Sin tabla de precios no se puede calcular nada.")
    tramos: list[Tramo] = []
    with ruta.open(encoding="utf-8-sig", newline="") as fh:
        for n, fila in enumerate(csv.DictReader(fh), start=2):
            if not fila.get("desde"):
                continue
            try:
                tramos.append(
                    Tramo(
                        desde=Decimal(fila["desde"].strip()),
                        hasta=Decimal(fila["hasta"].strip()),
                        precio_primaria=int(fila["precio_primaria"]),
                        precio_secundaria=int(fila["precio_secundaria"]),
                        compare_primaria=int(fila["compare_primaria"]) if fila.get("compare_primaria") else None,
                        compare_secundaria=int(fila["compare_secundaria"]) if fila.get("compare_secundaria") else None,
                    )
                )
            except (KeyError, ValueError, InvalidOperation) as e:
                raise SystemExit(f"{ruta.name} linea {n}: fila invalida ({e})")
    tramos.sort(key=lambda t: t.desde)
    for a, b in zip(tramos, tramos[1:]):
        if b.desde <= a.hasta:
            raise SystemExit(
                f"{ruta.name}: los tramos {a.desde}-{a.hasta} y {b.desde}-{b.hasta} se solapan"
            )
    # Un compareAtPrice menor que el precio deja la ficha mostrando un "descuento"
    # negativo. Shopify lo acepta sin quejarse, asi que hay que atajarlo aca.
    for t in tramos:
        for etiqueta, precio, compare in (
            ("primaria", t.precio_primaria, t.compare_primaria),
            ("secundaria", t.precio_secundaria, t.compare_secundaria),
        ):
            if compare is not None and compare <= precio:
                raise SystemExit(
                    f"{ruta.name}: en el tramo {t.desde}-{t.hasta}, compare_{etiqueta}={compare} "
                    f"no es mayor que precio_{etiqueta}={precio}. El precio tachado tiene que ser "
                    f"el mas alto."
                )
    return tramos


def buscar_tramo(tramos: list[Tramo], precio: Decimal | None) -> Tramo | None:
    if precio is None:
        return None
    for t in tramos:
        if t.desde <= precio <= t.hasta:
            return t
    return None


# ---------------------------------------------------------------------------
# MAPEO PS STORE <-> SHOPIFY
# ---------------------------------------------------------------------------

CAMPOS_MAPEO = ["ps_id", "ps_nombre", "producto_id", "variante_primaria", "variante_secundaria", "activo"]


def cargar_mapeo(ruta: Path | None = None) -> dict[str, FilaMapeo]:
    ruta = ruta or ARCHIVO_MAPEO
    if not ruta.exists():
        log(f"{ruta.name} no existe todavia: corre --bootstrap para generarlo.")
        return {}
    mapeo: dict[str, FilaMapeo] = {}
    with ruta.open(encoding="utf-8-sig", newline="") as fh:
        for numero, fila in enumerate(csv.DictReader(fh), start=2):
            if not fila.get("ps_nombre"):
                continue
            f = FilaMapeo(
                ps_id=(fila.get("ps_id") or "").strip(),
                ps_nombre=fila["ps_nombre"].strip(),
                producto_id=(fila.get("producto_id") or "").strip(),
                variante_primaria=(fila.get("variante_primaria") or "").strip(),
                variante_secundaria=(fila.get("variante_secundaria") or "").strip(),
                activo=(fila.get("activo") or "si").strip().lower() not in ("no", "0", "false"),
            )
            f.problema = _revisar_ids(f, numero, ruta.name)
            mapeo[normalizar(f.ps_nombre)] = f

            if not f.ps_id:
                continue
            if not _id_confiable(f.ps_id):
                log(f"aviso: {ruta.name} linea {numero}: ignoro el ps_id '{f.ps_id}' "
                    f"(parece inventado); '{f.ps_nombre}' se cruzara por nombre")
                continue
            clave = f"id:{f.ps_id}"
            if clave in mapeo:
                # Dos filas con el mismo ps_id: no hay forma de saber cual es la
                # buena, asi que no gana ninguna y las dos caen al cruce por nombre.
                log(f"aviso: {ruta.name} linea {numero}: ps_id '{f.ps_id}' duplicado; "
                    f"anulo el indice por id para ese id")
                mapeo.pop(clave, None)
            else:
                mapeo[clave] = f
    return mapeo


# Un id de PS Store real se parece a 'EP0001-CUSA12345_00-XXXXXXXXXXXXXXXX' o a
# un numero de concepto. Lo que no puede ser es un id posicional inventado.
def _id_confiable(ps_id: str) -> bool:
    return bool(ps_id) and not ps_id.startswith("dom-")


PREFIJO_GID = "gid://shopify/"


def _revisar_ids(f: FilaMapeo, numero: int, archivo: str) -> str | None:
    """Detecta ids de Shopify mal pegados ANTES de mandarselos a la API.

    Un id numerico pelado (copiado de la URL del admin) no produce un userError:
    produce un error de GraphQL que mata la corrida entera a mitad de camino.
    """
    malos = [
        f"{campo}='{valor}'"
        for campo, valor in (
            ("producto_id", f.producto_id),
            ("variante_primaria", f.variante_primaria),
            ("variante_secundaria", f.variante_secundaria),
        )
        if valor and not valor.startswith(PREFIJO_GID)
    ]
    if not malos:
        return None
    return (f"{archivo} linea {numero}: {', '.join(malos)} no empieza con "
            f"{PREFIJO_GID} (¿copiaste el numero de la URL del admin?)")


def buscar_en_mapeo(mapeo: dict[str, FilaMapeo], j: JuegoPS) -> FilaMapeo | None:
    if _id_confiable(j.ps_id):
        fila = mapeo.get(f"id:{j.ps_id}")
        if fila:
            return fila
    return mapeo.get(normalizar(j.nombre))


def respaldar(ruta: Path) -> Path | None:
    if not ruta.exists():
        return None
    destino = ruta.with_name(f"{ruta.stem}.backup-{datetime.now():%Y%m%d-%H%M%S}{ruta.suffix}")
    destino.write_bytes(ruta.read_bytes())
    log(f"respaldo: {destino.name}")
    return destino


# ---------------------------------------------------------------------------
# SHOPIFY
# ---------------------------------------------------------------------------

def _token() -> str:
    """Token de Shopify. Nunca se guarda en disco ni se imprime.

    En Replit / Linux viene de la variable de entorno CJM_SHOPIFY_TOKEN
    (pestana Secrets). En macOS, del Llavero.
    """
    del_entorno = os.environ.get("CJM_SHOPIFY_TOKEN")
    if del_entorno:
        return del_entorno.strip()
    try:
        salida = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICIO_LLAVERO, "-w"],
            capture_output=True, text=True, check=True,
        )
        return salida.stdout.strip()
    except FileNotFoundError:
        raise SystemExit(
            "No hay token de Shopify.\n"
            "  En Replit: agregalo en Secrets como CJM_SHOPIFY_TOKEN.\n"
            "  En Linux:  export CJM_SHOPIFY_TOKEN='shpat_...'\n"
            "  (el Llavero de macOS no existe en este sistema)"
        )
    except subprocess.CalledProcessError:
        raise SystemExit(
            f"No hay token en el Llavero para el servicio '{SERVICIO_LLAVERO}'.\n"
            f"Guardalo con:\n"
            f"  security add-generic-password -s {SERVICIO_LLAVERO} -a shopify -w\n"
            f"O usa la variable de entorno CJM_SHOPIFY_TOKEN."
        )


def _validar_tienda() -> str:
    """El token viaja en una cabecera propia, y requests reenvia las cabeceras
    personalizadas en las redirecciones. Un dominio mal escrito en la config
    podria mandarle el token a un tercero, asi que lo validamos aca y ademas
    prohibimos seguir redirecciones."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", TIENDA or "", re.IGNORECASE):
        raise SystemExit(
            f"El dominio de la tienda ('{TIENDA}') no tiene la forma "
            f"tu-tienda.myshopify.com. Corrige CJM_SHOPIFY_TIENDA antes de seguir."
        )
    return TIENDA


def graphql(consulta: str, variables: dict | None = None) -> dict:
    import requests

    url = f"https://{_validar_tienda()}/admin/api/{API_VERSION}/graphql.json"
    cabeceras = {"X-Shopify-Access-Token": _token(), "Content-Type": "application/json"}
    cuerpo = {"query": consulta, "variables": variables or {}}

    for intento in range(REINTENTOS_SHOPIFY):
        try:
            r = requests.post(url, headers=cabeceras, json=cuerpo, timeout=45,
                              allow_redirects=False)
        except Exception as e:  # noqa: BLE001 - ConnectionError, ReadTimeout, etc.
            if intento == REINTENTOS_SHOPIFY - 1:
                raise
            espera = 2 ** intento
            log(f"  fallo de red hablando con Shopify ({type(e).__name__}), reintento en {espera}s")
            time.sleep(espera)
            continue

        if r.status_code in (301, 302, 303, 307, 308):
            raise RuntimeError(
                f"Shopify respondio una redireccion ({r.status_code}). No la sigo para no "
                f"filtrar el token. Revisa el dominio de la tienda."
            )
        if r.status_code == 429 or r.status_code >= 500:
            if intento == REINTENTOS_SHOPIFY - 1:
                r.raise_for_status()
            espera = 2 ** intento
            log(f"  Shopify respondio {r.status_code}, reintento en {espera}s")
            time.sleep(espera)
            continue

        r.raise_for_status()
        datos = r.json()
        errores = datos.get("errors")
        if errores:
            codigos = {
                (e.get("extensions") or {}).get("code")
                for e in errores if isinstance(e, dict)
            }
            # THROTTLED llega con HTTP 200: si no se atiende, mata la corrida.
            if "THROTTLED" in codigos and intento < REINTENTOS_SHOPIFY - 1:
                espera = _espera_por_throttle(datos) or (2 ** intento)
                log(f"  Shopify sin puntos disponibles, espero {espera:.1f}s")
                time.sleep(espera)
                continue
            if "MAX_COST_EXCEEDED" in codigos:
                # Deterministico: reintentar es un bucle infinito.
                raise RuntimeError(
                    f"La consulta a Shopify es demasiado cara y siempre lo sera: {errores}. "
                    f"Baja los 'first:' de CONSULTA_CATALOGO."
                )
            raise RuntimeError(f"Shopify GraphQL: {errores}")
        return datos["data"]
    raise RuntimeError(f"Shopify no respondio bien despues de {REINTENTOS_SHOPIFY} intentos")


def _espera_por_throttle(datos: dict) -> float | None:
    """Segundos a esperar segun el estado del bucket que informa Shopify."""
    try:
        estado = datos["extensions"]["cost"]["throttleStatus"]
        faltan = datos["extensions"]["cost"]["requestedQueryCost"] - estado["currentlyAvailable"]
        return max(1.0, faltan / estado["restoreRate"])
    except (KeyError, TypeError, ZeroDivisionError):
        return None


# Shopify rechaza cualquier consulta que pida mas de 1000 puntos, y lo hace
# ANTES de ejecutarla. 100 productos x 20 variantes pedia ~2300 y fallaba
# siempre. Con 50 x 10 el costo pedido es ~650.
CONSULTA_CATALOGO = """
query catalogo($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      status
      variants(first: 10) {
        nodes { id title price compareAtPrice }
      }
    }
  }
}
"""


def leer_catalogo() -> list[dict]:
    """Lectura pura: no modifica nada en Shopify."""
    productos: list[dict] = []
    cursor = None
    while True:
        datos = graphql(CONSULTA_CATALOGO, {"cursor": cursor})
        bloque = datos["products"]
        productos.extend(bloque["nodes"])
        if not bloque["pageInfo"]["hasNextPage"]:
            break
        cursor = bloque["pageInfo"]["endCursor"]
        log(f"  catalogo: {len(productos)} productos leidos...")
        time.sleep(PAUSA_ENTRE_LLAMADAS)
    return productos


MUTACION_PRECIOS = """
mutation actualizar($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id price compareAtPrice }
    userErrors { field message }
  }
}
"""


def aplicar_precios(producto_id: str, variantes: list[dict]) -> list[str]:
    datos = graphql(MUTACION_PRECIOS, {"productId": producto_id, "variants": variantes})
    errores = datos["productVariantsBulkUpdate"]["userErrors"]
    return [f"{e.get('field')}: {e['message']}" for e in errores]


# ---------------------------------------------------------------------------
# REPORTES
# ---------------------------------------------------------------------------

def cargar_estado(ruta: Path | None = None) -> dict:
    """Que juegos estaban en oferta la corrida anterior y a que precio normal."""
    ruta = ruta or ARCHIVO_ESTADO
    if not ruta.exists():
        return {}
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        log(f"aviso: no pude leer {ruta.name} ({e}); arranco con estado vacio")
        return {}


def guardar_estado(previo: dict, en_oferta: list[JuegoPS], mapeo: dict[str, FilaMapeo],
                   ruta: Path | None = None) -> None:
    ruta = ruta or ARCHIVO_ESTADO
    nuevo = {}
    for j in en_oferta:
        fila = buscar_en_mapeo(mapeo, j)
        if not fila or not fila.activo:
            continue
        nuevo[normalizar(j.nombre)] = {
            "ps_nombre": j.nombre,
            "producto_id": fila.producto_id,
            "precio_normal_ps": str(j.precio_normal) if j.precio_normal is not None else "",
            "precio_oferta_ps": str(j.precio_oferta) if j.precio_oferta is not None else "",
            "fecha": f"{datetime.now():%Y-%m-%d}",
        }
    try:
        ruta.write_text(json.dumps(nuevo, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        log(f"aviso: no pude guardar {ruta.name} ({e})")


def detectar_salidas_de_oferta(previo: dict, en_oferta: list[JuegoPS],
                               mapeo: dict[str, FilaMapeo]) -> list[dict]:
    """Juegos que estaban en oferta y ya no lo estan.

    El sistema solo BAJA precios: cuando PS Store termina una promocion, nadie
    devuelve el precio normal en Shopify y el juego se queda rebajado para
    siempre. No lo revertimos solos (seria escribir precios que nadie reviso),
    pero si lo dejamos anotado en revisar_*.csv con el precio al que estaba.
    """
    siguen = {normalizar(j.nombre) for j in en_oferta}
    salidas = []
    for clave, datos in previo.items():
        if clave in siguen:
            continue
        fila = mapeo.get(clave)
        if not fila or not fila.activo:
            continue
        salidas.append({
            "motivo": "salio de oferta: revisa el precio en Shopify",
            "ps_nombre": datos.get("ps_nombre", clave),
            "precio_ps": datos.get("precio_normal_ps", ""),
            "detalle": f"estaba a {datos.get('precio_oferta_ps', '?')} el "
                       f"{datos.get('fecha', '?')}; producto {datos.get('producto_id', '?')}",
        })
    if salidas:
        log(f"{len(salidas)} juego(s) salieron de oferta: van a revisar_*.csv")
    return salidas


def escribir_csv(ruta: Path, campos: list[str], filas: Iterable[dict]) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with ruta.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=campos)
        w.writeheader()
        for fila in filas:
            w.writerow(fila)
    return ruta


# ---------------------------------------------------------------------------
# MODOS
# ---------------------------------------------------------------------------

def modo_diagnostico(args) -> int:
    """Abre una pagina real y reporta como esta estructurado el sitio hoy."""
    categoria = args.categoria or CATEGORIAS[0][0]
    url = URL_CATEGORIA.format(locale=LOCALE, cat=categoria, pagina=1)
    REPORTES.mkdir(parents=True, exist_ok=True)
    driver = abrir_navegador(headless=not args.ver_navegador)
    try:
        log(f"abriendo {url}")
        driver.get(url)
        time.sleep(max(ESPERA_PAGINA, 4))

        destino_html = REPORTES / f"pagina_renderizada_{datetime.now():%Y%m%d-%H%M%S}.html"
        destino_html.write_text(driver.page_source, encoding="utf-8")
        log(f"HTML renderizado guardado en {destino_html}")

        estado = leer_apollo(driver)
        if estado:
            destino_json = destino_html.with_suffix(".apollo.json")
            destino_json.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")
            log(f"estado de Apollo encontrado ({len(estado)} claves) -> {destino_json.name}")
            juegos = desde_apollo(estado)
            log(f"desde_apollo() extrajo {len(juegos)} productos "
                f"(se esperan ~{PRODUCTOS_POR_PAGINA} por pagina)")
            con_clasif = [j for j in juegos if j.clasificacion]
            if con_clasif:
                valores = sorted({j.clasificacion for j in con_clasif})
                log(f"SI hay campo de clasificacion. Valores vistos: {', '.join(valores)}")
                log("  -> el filtro usa este campo, que es exacto (mejor que las palabras clave)")
            else:
                log("NO hay campo de clasificacion en la cache: el filtro cae a palabras clave.")
                log(f"  campos disponibles en una entrada de ejemplo: "
                    f"{sorted(list(estado.values())[0].keys())[:25] if estado else '-'}")
        else:
            log("NO encontre el estado de Apollo. El plan A no aplica en esta version del sitio.")
            juegos = []

        if not juegos:
            juegos = desde_dom(driver)
            log(f"desde_dom() extrajo {len(juegos)} productos")

        log("")
        log("Muestra de lo extraido (verificalo a mano en Chrome):")
        for j in juegos[:8]:
            log(f"  - {j.nombre[:58]:58} | normal {j.precio_normal} | oferta {j.precio_oferta} "
                f"| {j.moneda} | clas={j.clasificacion} | {j.descuento or ''}")

        monedas = {j.moneda for j in juegos if j.moneda}
        if monedas and MONEDA_ESPERADA not in monedas:
            log(f"OJO: esperaba {MONEDA_ESPERADA} y el sitio devolvio {monedas}. "
                f"Revisa tabla_precios.csv antes de aplicar nada.")
        return 0 if juegos else 1
    finally:
        driver.quit()


def modo_muestra_filtro(args) -> int:
    juegos = raspar_todo(
        paginas_max=args.paginas or 4,
        headless=not args.ver_navegador,
        solo_categoria=args.categoria,
    )
    aceptados, descartados = [], []
    for j in juegos:
        ok, motivo = es_juego_completo(j)
        (aceptados if ok else descartados).append((j, motivo))

    log(f"\n=== ACEPTADOS ({len(aceptados)}) — primeros 40 ===")
    for j, motivo in aceptados[:40]:
        log(f"  + {j.nombre[:62]:62} [{motivo}]")
    log(f"\n=== DESCARTADOS ({len(descartados)}) — primeros 40 ===")
    for j, motivo in descartados[:40]:
        log(f"  - {j.nombre[:62]:62} [{motivo}]")

    escribir_csv(
        REPORTES / f"filtro_{datetime.now():%Y%m%d-%H%M}.csv",
        ["decision", "motivo", "nombre", "clasificacion", "precio_normal", "precio_oferta"],
        [
            {"decision": d, "motivo": m, "nombre": j.nombre, "clasificacion": j.clasificacion,
             "precio_normal": j.precio_normal, "precio_oferta": j.precio_oferta}
            for lista, d in ((aceptados, "acepta"), (descartados, "descarta"))
            for j, m in lista
        ],
    )
    log(f"\nDetalle completo en reportes/filtro_*.csv")
    return 0


def modo_bootstrap(args) -> int:
    """Propone filas de mapeo.csv. Nunca escribe sin confirmacion explicita."""
    todos = raspar_todo(
        paginas_max=args.paginas,
        headless=not args.ver_navegador,
        solo_categoria=args.categoria,
    )
    if getattr(todos, "paginas_dom", 0):
        # El plan B no trae clasificacion ni ids fiables: un mapeo construido
        # con esos datos queda mal desde el dia uno.
        log("")
        log(f"NO construyo el mapeo: {todos.paginas_dom} pagina(s) se leyeron con el plan B "
            f"(DOM), que no trae clasificacion de producto ni ids de PS Store.")
        log("Arregla primero la extraccion con --diagnostico y vuelve a intentar.")
        return 2
    juegos = [j for j in todos if es_juego_completo(j)[0]]
    log(f"{len(juegos)} juegos completos en PS Store")

    log("leyendo catalogo de Shopify (solo lectura)...")
    productos = leer_catalogo()
    log(f"{len(productos)} productos en Shopify")

    indice: dict[str, dict] = {normalizar(p["title"]): p for p in productos}
    mapeo_actual = cargar_mapeo()

    seguras, dudosas = [], []
    for j in juegos:
        if buscar_en_mapeo(mapeo_actual, j):
            continue
        clave = normalizar(j.nombre)
        producto = indice.get(clave)
        if not producto:
            # Coincidencia parcial, pero solo la ofrecemos como "dudosa".
            candidatos = [p for k, p in indice.items() if clave and (clave in k or k in clave)]
            if len(candidatos) == 1:
                dudosas.append((j, candidatos[0], "coincidencia parcial, revisar secuela/edicion"))
            elif candidatos:
                dudosas.append((j, candidatos[0], f"{len(candidatos)} candidatos posibles"))
            else:
                dudosas.append((j, None, "sin candidato en Shopify"))
            continue

        variantes = producto["variants"]["nodes"]
        primaria = next((v for v in variantes if "primaria" in normalizar(v["title"])), None)
        secundaria = next((v for v in variantes if "secundaria" in normalizar(v["title"])), None)
        if not primaria or not secundaria:
            dudosas.append((j, producto, "faltan variantes Primaria/Secundaria"))
            continue
        seguras.append(
            {
                # Solo propagamos ids que vengan de Apollo: los del plan B no
                # identifican al juego y envenenarian mapeo.csv.
                "ps_id": j.ps_id if (j.origen == "apollo" and _id_confiable(j.ps_id)) else "",
                "ps_nombre": j.nombre,
                "producto_id": producto["id"],
                "variante_primaria": primaria["id"],
                "variante_secundaria": secundaria["id"],
                "activo": "si",
            }
        )

    log(f"\n=== FILAS PROPUESTAS ({len(seguras)}) — nombre identico ===")
    for f in seguras[:60]:
        log(f"  {f['ps_nombre'][:52]:52} -> {f['producto_id'].split('/')[-1]}")
    log(f"\n=== NO ESTOY SEGURO ({len(dudosas)}) — no se proponen ===")
    for j, p, motivo in dudosas[:60]:
        destino = p["title"][:40] if p else "-"
        log(f"  {j.nombre[:46]:46} ~ {destino:40} [{motivo}]")

    salida = escribir_csv(
        REPORTES / f"mapeo_propuesto_{datetime.now():%Y%m%d-%H%M}.csv", CAMPOS_MAPEO, seguras
    )
    escribir_csv(
        REPORTES / f"mapeo_dudoso_{datetime.now():%Y%m%d-%H%M}.csv",
        ["ps_nombre", "candidato_shopify", "motivo"],
        [{"ps_nombre": j.nombre, "candidato_shopify": p["title"] if p else "", "motivo": m}
         for j, p, m in dudosas],
    )
    log(f"\nPropuestas en {salida.name}. Revisalas y, si estan bien, agregalas a mapeo.csv:")
    log(f"  python3 {Path(__file__).name} --fusionar-mapeo {salida}")
    return 0


def modo_fusionar_mapeo(ruta_propuesta: Path) -> int:
    if not ruta_propuesta.exists():
        raise SystemExit(f"No existe {ruta_propuesta}")

    with ruta_propuesta.open(encoding="utf-8-sig", newline="") as fh:
        lector = csv.DictReader(fh)
        columnas = set(lector.fieldnames or [])
        obligatorias = {"ps_nombre", "producto_id", "variante_primaria", "variante_secundaria"}
        faltan = obligatorias - columnas
        if faltan:
            # Sin esto, pasarle un revisar_*.csv por equivocacion agregaba filas
            # sin ids: los juegos quedaban "mapeados" a la nada y no volvian a
            # aparecer nunca mas en los reportes.
            raise SystemExit(
                f"{ruta_propuesta.name} no parece un mapeo_propuesto_*.csv: le faltan las "
                f"columnas {', '.join(sorted(faltan))}.\n"
                f"Usa uno de los archivos reportes/mapeo_propuesto_*.csv que genera --bootstrap."
            )
        filas_crudas = list(lector)

    respaldar(ARCHIVO_MAPEO)
    existentes = cargar_mapeo()
    nuevas: list[dict] = []
    nombres_del_archivo: set[str] = set()
    for fila in filas_crudas:
        nombre = normalizar(fila.get("ps_nombre", ""))
        if not nombre or nombre in existentes or nombre in nombres_del_archivo:
            continue
        limpia = {c: (fila.get(c) or "").strip() for c in CAMPOS_MAPEO}
        if not _id_confiable(limpia["ps_id"]):
            limpia["ps_id"] = ""   # nunca dejar entrar un id posicional
        if not limpia["activo"]:
            limpia["activo"] = "si"
        nombres_del_archivo.add(nombre)
        nuevas.append(limpia)

    if not nuevas:
        log("No hay filas nuevas que agregar.")
        return 0

    nuevo_archivo = not ARCHIVO_MAPEO.exists()
    if not nuevo_archivo:
        # Si el archivo no termina en salto de linea, un append pega la fila
        # nueva sobre la ultima y se pierden las dos.
        contenido = ARCHIVO_MAPEO.read_bytes()
        if contenido and not contenido.endswith(b"\n"):
            with ARCHIVO_MAPEO.open("ab") as fh:
                fh.write(b"\r\n")
            log(f"{ARCHIVO_MAPEO.name} no terminaba en salto de linea: se lo agregue")

    with ARCHIVO_MAPEO.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CAMPOS_MAPEO)
        if nuevo_archivo:
            w.writeheader()
        w.writerows(nuevas)
    log(f"{len(nuevas)} filas agregadas a {ARCHIVO_MAPEO.name}")
    return 0


def modo_principal(args) -> int:
    # Tres candados para escribir en Shopify, y los tres tienen que abrirse:
    #   1. DRY_RUN = False arriba en este archivo (candado maestro).
    #   2. la bandera --aplicar en la linea de comandos.
    #   3. que NO se haya pasado --solo-simular.
    aplicar = args.aplicar and not args.solo_simular and not DRY_RUN
    if args.aplicar and DRY_RUN:
        log("--aplicar ignorado: DRY_RUN sigue en True dentro de cjm_precios_ps.py")
    tramos = cargar_tabla()
    mapeo = cargar_mapeo()
    log(f"{len(tramos)} tramos de precio, {len({id(v) for v in mapeo.values()})} juegos mapeados")

    resultado = raspar_todo(
        paginas_max=args.paginas,
        headless=not args.ver_navegador,
        solo_categoria=args.categoria,
    )
    juegos = list(resultado)
    paginas_con_error = getattr(resultado, "paginas_error", 0)
    log(f"{len(juegos)} productos unicos extraidos")

    if not juegos:
        # Antes esto devolvia 0 y correr.sh anunciaba "listo, 0 precios
        # actualizados": un fallo total del scraper se veia igual que una
        # quincena sin ofertas.
        log("NO SE EXTRAJO NI UN PRODUCTO. Algo esta roto: Chrome no arranco, "
            "PS Store cambio su estructura, o la red esta bloqueada.")
        log("Corre 'python3 cjm_precios_ps.py --diagnostico' para ver que paso.")
        return 2

    completos = [j for j in juegos if es_juego_completo(j)[0]]
    log(f"{len(completos)} son juegos completos")
    en_oferta = [j for j in completos if j.en_oferta]
    log(f"{len(en_oferta)} estan en oferta")

    estado_previo = cargar_estado()
    cambios, revisar = [], []
    revisar.extend(detectar_salidas_de_oferta(estado_previo, en_oferta, mapeo))

    for j in en_oferta:
        if j.moneda and j.moneda.upper() != MONEDA_ESPERADA:
            revisar.append({"motivo": f"moneda inesperada ({j.moneda})", "ps_nombre": j.nombre,
                            "precio_ps": j.precio_referencia, "detalle": ""})
            continue
        fila = buscar_en_mapeo(mapeo, j)
        if not fila:
            revisar.append({"motivo": "juego nuevo sin mapear", "ps_nombre": j.nombre,
                            "precio_ps": j.precio_referencia, "detalle": j.descuento or ""})
            continue
        if not fila.activo:
            continue
        if fila.problema:
            # Un id mal pegado no es un userError: es un error de GraphQL que
            # mata la corrida entera. Lo atajamos antes de llamar a Shopify.
            revisar.append({"motivo": "id de Shopify mal formado", "ps_nombre": j.nombre,
                            "precio_ps": j.precio_referencia, "detalle": fila.problema})
            continue
        tramo = buscar_tramo(tramos, j.precio_referencia)
        if not tramo:
            revisar.append({"motivo": "precio fuera de todos los tramos", "ps_nombre": j.nombre,
                            "precio_ps": j.precio_referencia, "detalle": "revisa tabla_precios.csv"})
            continue
        for etiqueta, variante_id, precio, compare in (
            ("Cuenta Primaria", fila.variante_primaria, tramo.precio_primaria, tramo.compare_primaria),
            ("Cuenta Secundaria", fila.variante_secundaria, tramo.precio_secundaria, tramo.compare_secundaria),
        ):
            if not variante_id:
                revisar.append({"motivo": f"falta id de variante {etiqueta}", "ps_nombre": j.nombre,
                                "precio_ps": j.precio_referencia, "detalle": fila.producto_id})
                continue
            cambios.append(
                {
                    "producto_id": fila.producto_id,
                    "variante_id": variante_id,
                    "variante": etiqueta,
                    "ps_nombre": j.nombre,
                    "precio_ps_normal": j.precio_normal,
                    "precio_ps_oferta": j.precio_oferta,
                    "precio_nuevo": precio,
                    "compare_at": compare or "",
                    "estado": "pendiente",
                }
            )

    # Segundos en el sello: dos corridas en el mismo minuto se pisaban el
    # reporte, y con --aplicar eso borra la evidencia de lo que ya se escribio.
    sello = f"{datetime.now():%Y%m%d-%H%M%S}"
    interrumpido: Exception | None = None

    try:
        if aplicar:
            log(f"APLICANDO {len(cambios)} cambios en Shopify...")
            por_producto: dict[str, list[dict]] = {}
            for c in cambios:
                por_producto.setdefault(c["producto_id"], []).append(c)

            fallos_seguidos = 0
            for numero, (producto_id, lista) in enumerate(por_producto.items(), start=1):
                variantes = []
                for c in lista:
                    v = {"id": c["variante_id"], "price": str(c["precio_nuevo"])}
                    if c["compare_at"]:
                        v["compareAtPrice"] = str(c["compare_at"])
                    variantes.append(v)
                try:
                    errores = aplicar_precios(producto_id, variantes)
                except Exception as e:  # noqa: BLE001 - un producto no tumba la corrida
                    errores = [f"excepcion: {type(e).__name__}: {e}"]
                for c in lista:
                    c["estado"] = "error: " + "; ".join(errores) if errores else "aplicado"
                if errores:
                    fallos_seguidos += 1
                    log(f"  {producto_id}: {errores}")
                    if fallos_seguidos >= FALLOS_SEGUIDOS_MAX:
                        log(f"  {FALLOS_SEGUIDOS_MAX} productos seguidos con error: corto aca "
                            f"y escribo el reporte con lo hecho")
                        break
                else:
                    fallos_seguidos = 0
                if numero < len(por_producto):
                    time.sleep(PAUSA_ENTRE_LLAMADAS)
        else:
            for c in cambios:
                c["estado"] = "simulado"
            log("MODO SIMULACION: no se escribio nada en Shopify (usa --aplicar para escribir)")
    except BaseException as e:  # noqa: BLE001
        # Pase lo que pase (incluido Ctrl-C), los reportes se escriben: son la
        # unica forma de saber que precios alcanzaron a cambiar en la tienda.
        interrumpido = e
    finally:
        ruta_cambios = escribir_csv(
            REPORTES / f"cambios_{sello}.csv",
            ["producto_id", "variante_id", "variante", "ps_nombre", "precio_ps_normal",
             "precio_ps_oferta", "precio_nuevo", "compare_at", "estado"],
            cambios,
        )
        ruta_revisar = escribir_csv(
            REPORTES / f"revisar_{sello}.csv",
            ["motivo", "ps_nombre", "precio_ps", "detalle"],
            revisar,
        )
        log(f"\nResumen: {len(cambios)} cambios -> {ruta_cambios.name}")
        log(f"         {len(revisar)} para revisar -> {ruta_revisar.name}")

    if interrumpido is not None:
        log(f"LA CORRIDA SE CORTO: {type(interrumpido).__name__}: {interrumpido}")
        log(f"Mira {ruta_cambios.name}: la columna 'estado' dice que alcanzo a aplicarse.")
        if isinstance(interrumpido, KeyboardInterrupt):
            raise interrumpido
        return 1

    guardar_estado(estado_previo, en_oferta, mapeo)

    con_error = [c for c in cambios if str(c["estado"]).startswith("error")]
    if con_error:
        log(f"         {len(con_error)} con error al aplicar")
        return 1
    if paginas_con_error:
        log(f"         OJO: {paginas_con_error} pagina(s) de PS Store nunca cargaron; "
            f"el recorrido quedo incompleto")
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Precios de ofertas PS Store -> Shopify (CJM Digitales)")
    p.add_argument("--aplicar", action="store_true", help="escribe los precios en Shopify de verdad")
    p.add_argument("--paginas", type=int, help="limita las paginas por categoria (para pruebas)")
    p.add_argument("--categoria", help="usa solo esta categoria (id de PS Store)")
    p.add_argument("--ver-navegador", action="store_true", help="muestra Chrome en vez de headless")
    p.add_argument("--diagnostico", action="store_true", help="inspecciona la estructura real del sitio")
    p.add_argument("--muestra-filtro", action="store_true", help="audita el filtro de juegos completos")
    p.add_argument("--bootstrap", action="store_true", help="propone filas nuevas para mapeo.csv")
    p.add_argument("--fusionar-mapeo", type=Path, help="agrega a mapeo.csv un csv de propuestas ya revisado")
    p.add_argument("--solo-simular", action="store_true", help="ignora --aplicar pase lo que pase")
    args = p.parse_args(argv)

    REPORTES.mkdir(parents=True, exist_ok=True)

    if args.diagnostico:
        return modo_diagnostico(args)
    if args.muestra_filtro:
        return modo_muestra_filtro(args)
    if args.bootstrap:
        return modo_bootstrap(args)
    if args.fusionar_mapeo:
        return modo_fusionar_mapeo(args.fusionar_mapeo)
    return modo_principal(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("cancelado por el usuario")
        sys.exit(130)
