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
API_VERSION = "2025-01"
SERVICIO_LLAVERO = "cjm_shopify_token"

# Escribir en Shopify solo cuando se pide explicitamente con --aplicar.
DRY_RUN = True

ESPERA_PAGINA = 2.5      # segundos de espera tras cargar cada pagina
REINTENTOS_PAGINA = 2    # reintentos por pagina antes de darla por perdida
PRODUCTOS_POR_PAGINA = 24  # lo que PS Store devuelve normalmente


# ---------------------------------------------------------------------------
# EXTRACCION DEL ESTADO DE APOLLO (plan A)
# ---------------------------------------------------------------------------

# PS Store es un Next.js: el HTML inicial trae un <script id="__NEXT_DATA__">
# con la cache normalizada de Apollo. Preferimos leer eso antes que el DOM
# porque incluye la clasificacion del producto y los precios ya separados.
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
return buscarEstado();
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


# ---------------------------------------------------------------------------
# SCRAPER
# ---------------------------------------------------------------------------

def abrir_navegador(headless: bool = True):
    """Crea el driver de Chrome. Importamos aca para no exigir Selenium
    en los modos que no scrapean (por ejemplo --validar-tabla)."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opciones = Options()
    if headless:
        opciones.add_argument("--headless=new")
    opciones.add_argument("--window-size=1440,2400")
    opciones.add_argument("--disable-gpu")
    opciones.add_argument("--no-sandbox")
    opciones.add_argument("--disable-blink-features=AutomationControlled")
    opciones.add_argument("--lang=es-CL")
    opciones.add_argument(
        "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    opciones.add_experimental_option("excludeSwitches", ["enable-automation"])

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        servicio = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=servicio, options=opciones)
    except Exception as e:  # noqa: BLE001 - Selenium 4.6+ trae driver propio
        log(f"webdriver-manager no disponible ({e}); uso Selenium Manager")
        return webdriver.Chrome(options=opciones)


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
            )
        )

    # La cache trae Product y Concept del mismo juego: deduplicamos por nombre.
    unicos: dict[str, JuegoPS] = {}
    for j in juegos:
        clave = normalizar(j.nombre)
        previo = unicos.get(clave)
        if previo is None or (previo.precio_oferta is None and j.precio_oferta is not None):
            unicos[clave] = j
    return list(unicos.values())


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
    for i, tarjeta in enumerate(tarjetas):
        try:
            texto = tarjeta.text or ""
            lineas = [l.strip() for l in texto.split("\n") if l.strip()]
            if not lineas:
                continue
            # Nombre: primera linea que no sea un precio ni un porcentaje.
            nombre = next(
                (l for l in lineas if not re.match(r"^[-+]?\s*[\d$%.,\s]+$", l)),
                lineas[0],
            )
            precios = [l for l in lineas if "$" in l]
            oferta = parse_precio(precios[0], MONEDA_ESPERADA) if precios else None
            normal = parse_precio(precios[1], MONEDA_ESPERADA) if len(precios) > 1 else None
            if normal is not None and oferta is not None and normal < oferta:
                normal, oferta = oferta, normal
            href = tarjeta.get_attribute("href") or ""
            ps_id = href.rstrip("/").split("/")[-1] if href else f"dom-{i}"
            juegos.append(
                JuegoPS(
                    ps_id=ps_id,
                    nombre=nombre,
                    clasificacion=None,
                    moneda=MONEDA_ESPERADA,
                    precio_normal=normal or oferta,
                    precio_oferta=oferta,
                    origen="dom",
                )
            )
        except Exception as e:  # noqa: BLE001 - una tarjeta rota no aborta la pagina
            log(f"  tarjeta {i} ilegible: {e}")
    return juegos


def raspar_pagina(driver, categoria: str, pagina: int) -> tuple[list[JuegoPS], str]:
    """Devuelve (juegos, origen) para una pagina de categoria."""
    url = URL_CATEGORIA.format(locale=LOCALE, cat=categoria, pagina=pagina)
    for intento in range(REINTENTOS_PAGINA + 1):
        try:
            driver.get(url)
            time.sleep(ESPERA_PAGINA)
            estado = driver.execute_script(JS_APOLLO)
            if estado:
                juegos = desde_apollo(estado)
                if juegos:
                    return juegos, "apollo"
            juegos = desde_dom(driver)
            if juegos:
                return juegos, "dom"
            if intento < REINTENTOS_PAGINA:
                log(f"  pagina {pagina} vino vacia, reintento {intento + 1}")
                time.sleep(3 * (intento + 1))
        except Exception as e:  # noqa: BLE001
            log(f"  error en pagina {pagina}: {e}")
            if intento < REINTENTOS_PAGINA:
                time.sleep(3 * (intento + 1))
    return [], "vacio"


def raspar_todo(
    paginas_max: int | None = None,
    headless: bool = True,
    solo_categoria: str | None = None,
) -> list[JuegoPS]:
    categorias = ((solo_categoria, CATEGORIAS[0][1]),) if solo_categoria else CATEGORIAS
    driver = abrir_navegador(headless=headless)
    encontrados: dict[str, JuegoPS] = {}
    conteo_origen = {"apollo": 0, "dom": 0, "vacio": 0}
    try:
        for categoria, paginas in categorias:
            total = paginas if paginas_max is None else min(paginas, paginas_max)
            for pagina in range(1, total + 1):
                juegos, origen = raspar_pagina(driver, categoria, pagina)
                conteo_origen[origen] = conteo_origen.get(origen, 0) + 1
                log(f"pagina {pagina}/{total}: {len(juegos)} productos (via {origen})")
                for j in juegos:
                    encontrados.setdefault(normalizar(j.nombre), j)
                if not juegos and pagina > 1:
                    log("  pagina vacia despues de la primera: doy la categoria por terminada")
                    break
    finally:
        driver.quit()
    log(f"origen de datos -> apollo: {conteo_origen['apollo']} paginas, "
        f"dom: {conteo_origen['dom']}, vacias: {conteo_origen['vacio']}")
    return list(encontrados.values())


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
        if normalizar(palabra) in nombre:
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
        for fila in csv.DictReader(fh):
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
            # Indexamos por nombre normalizado y tambien por id cuando lo hay.
            mapeo[normalizar(f.ps_nombre)] = f
            if f.ps_id:
                mapeo[f"id:{f.ps_id}"] = f
    return mapeo


def buscar_en_mapeo(mapeo: dict[str, FilaMapeo], j: JuegoPS) -> FilaMapeo | None:
    return mapeo.get(f"id:{j.ps_id}") or mapeo.get(normalizar(j.nombre))


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
    """Lee el token del Llavero de macOS. Nunca se guarda en disco."""
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
            "No encuentro el comando 'security' (solo existe en macOS).\n"
            "Fuera de macOS exporta CJM_SHOPIFY_TOKEN en el entorno."
        )
    except subprocess.CalledProcessError:
        raise SystemExit(
            f"No hay token en el Llavero para el servicio '{SERVICIO_LLAVERO}'.\n"
            f"Guardalo con:\n"
            f"  security add-generic-password -s {SERVICIO_LLAVERO} -a shopify -w"
        )


def graphql(consulta: str, variables: dict | None = None) -> dict:
    import requests

    url = f"https://{TIENDA}/admin/api/{API_VERSION}/graphql.json"
    cabeceras = {"X-Shopify-Access-Token": _token(), "Content-Type": "application/json"}
    for intento in range(4):
        r = requests.post(url, headers=cabeceras, json={"query": consulta, "variables": variables or {}}, timeout=45)
        if r.status_code == 429:
            espera = 2 ** intento
            log(f"  Shopify pidio esperar (429), duermo {espera}s")
            time.sleep(espera)
            continue
        r.raise_for_status()
        datos = r.json()
        if datos.get("errors"):
            raise RuntimeError(f"Shopify GraphQL: {datos['errors']}")
        return datos["data"]
    raise RuntimeError("Shopify sigue respondiendo 429 despues de 4 intentos")


CONSULTA_CATALOGO = """
query catalogo($cursor: String) {
  products(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      status
      variants(first: 20) {
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

        estado = driver.execute_script(JS_APOLLO)
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
                "ps_id": j.ps_id,
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
    respaldar(ARCHIVO_MAPEO)
    existentes = cargar_mapeo()
    nuevas = []
    with ruta_propuesta.open(encoding="utf-8-sig", newline="") as fh:
        for fila in csv.DictReader(fh):
            if normalizar(fila.get("ps_nombre", "")) in existentes:
                continue
            nuevas.append({c: fila.get(c, "") for c in CAMPOS_MAPEO})
    if not nuevas:
        log("No hay filas nuevas que agregar.")
        return 0
    nuevo_archivo = not ARCHIVO_MAPEO.exists()
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

    juegos = raspar_todo(
        paginas_max=args.paginas,
        headless=not args.ver_navegador,
        solo_categoria=args.categoria,
    )
    log(f"{len(juegos)} productos unicos extraidos")

    completos = [j for j in juegos if es_juego_completo(j)[0]]
    log(f"{len(completos)} son juegos completos")
    en_oferta = [j for j in completos if j.en_oferta]
    log(f"{len(en_oferta)} estan en oferta")

    cambios, revisar = [], []
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

    sello = f"{datetime.now():%Y%m%d-%H%M}"
    if aplicar:
        log(f"APLICANDO {len(cambios)} cambios en Shopify...")
        por_producto: dict[str, list[dict]] = {}
        for c in cambios:
            por_producto.setdefault(c["producto_id"], []).append(c)
        for producto_id, lista in por_producto.items():
            variantes = []
            for c in lista:
                v = {"id": c["variante_id"], "price": str(c["precio_nuevo"])}
                if c["compare_at"]:
                    v["compareAtPrice"] = str(c["compare_at"])
                variantes.append(v)
            errores = aplicar_precios(producto_id, variantes)
            for c in lista:
                c["estado"] = "error: " + "; ".join(errores) if errores else "aplicado"
            if errores:
                log(f"  {producto_id}: {errores}")
    else:
        for c in cambios:
            c["estado"] = "simulado"
        log("MODO SIMULACION: no se escribio nada en Shopify (usa --aplicar para escribir)")

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
    errores = [c for c in cambios if str(c["estado"]).startswith("error")]
    if errores:
        log(f"         {len(errores)} con error al aplicar")
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
