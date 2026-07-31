#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pruebas de integracion sin red: camino de Selenium y capa de Shopify.

`pruebas_offline.py` cubre la logica pura. Este archivo cubre las dos fronteras
del sistema, reemplazandolas por dobles:

  - el driver de Selenium, por un objeto que imita lo justo que usa el scraper
    (get, execute_script, find_elements, quit);
  - la Admin API de Shopify, por un requests.post falso que responde como
    responderia Shopify.

Asi se puede verificar el flujo completo, incluido --aplicar, sin abrir Chrome,
sin tocar internet y sin escribir un solo precio de verdad.

    python3 pruebas_integracion.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from decimal import Decimal
from pathlib import Path

import cjm_precios_ps as cjm

fallos: list[str] = []


def revisar(nombre: str, obtenido, esperado) -> None:
    if obtenido == esperado:
        print(f"  ok   {nombre}")
    else:
        print(f"  FALLA {nombre}: esperaba {esperado!r}, obtuve {obtenido!r}")
        fallos.append(nombre)


# ---------------------------------------------------------------------------
# Un apolloState como el que sirve PS Store: precios normalizados aparte y
# referenciados con __ref, y el mismo juego repetido como Product y Concept.
# ---------------------------------------------------------------------------
CATALOGO_FALSO = [
    ("Ghost of Tsushima DIRECTOR'S CUT", "FULL_GAME", 5999000, 2399600, "-60%"),
    ("EA SPORTS FC 26", "FULL_GAME", 6999000, 3499500, "-50%"),
    ("Hollow Knight: Silksong", "FULL_GAME", 1990000, 1393000, "-30%"),
    ("Assassin's Creed Valhalla - La Ira de los Druidas", "ADD_ON", 1999000, 999500, "-50%"),
    ("Pack de 1.000 Monedas", "VIRTUAL_CURRENCY", 990000, 792000, "-20%"),
    ("Cyberpunk 2077: Ultimate Edition", "PREMIUM_EDITION", 8999000, 3599600, "-60%"),
]


def _pesos(centavos: int) -> str:
    return f"${centavos // 100:,}".replace(",", ".")


def construir_apollo() -> dict:
    estado: dict = {"ROOT_QUERY": {"__typename": "Query"}}
    for i, (nombre, clas, base, desc, texto) in enumerate(CATALOGO_FALSO):
        clave_precio = f"Price:precio-{i}"
        estado[clave_precio] = {
            "__typename": "Price",
            "basePrice": _pesos(base), "basePriceValue": base,
            "discountedPrice": _pesos(desc), "discountedValue": desc,
            "currencyCode": "CLP", "discountText": texto, "isFree": False,
        }
        estado[f"Product:EP0000-CUSA{i:05d}_00-PRODUCTO{i:08d}"] = {
            "__typename": "Product", "id": f"EP0000-CUSA{i:05d}_00-PRODUCTO{i:08d}",
            "name": nombre, "storeDisplayClassification": clas,
            "platforms": ["PS4", "PS5"], "price": {"__ref": clave_precio},
        }
        # El mismo juego repetido como Concept: el scraper tiene que deduplicar.
        estado[f"Concept:{10000 + i}"] = {
            "__typename": "Concept", "id": str(10000 + i), "name": nombre,
            "price": {"__ref": clave_precio},
        }
    return estado


APOLLO = construir_apollo()


class ElementoFalso:
    def __init__(self, texto: str, href: str):
        self.text, self._href = texto, href

    def get_attribute(self, nombre: str):
        return self._href if nombre == "href" else None


class DriverFalso:
    """Imita lo justo de Selenium que usa el scraper."""

    def __init__(self, apollo=None, tarjetas=None, explota_en=()):
        self.apollo = apollo
        self.tarjetas = tarjetas or []
        self.explota_en = explota_en
        self.visitadas: list[str] = []
        self.cerrado = False
        self.timeout_pagina = None

    def get(self, url):
        self.visitadas.append(url)
        if len(self.visitadas) in self.explota_en:
            raise Exception("timeout simulado del navegador")

    def execute_script(self, script):
        # JS_APOLLO devuelve texto, no un objeto: chromedriver reordena las
        # claves de los objetos y eso rompia la deduplicacion.
        return json.dumps(self.apollo) if self.apollo is not None else None

    def find_elements(self, by, selector):
        return self.tarjetas if selector.startswith("[data-qa") else []

    def set_page_load_timeout(self, s):
        self.timeout_pagina = s

    def set_script_timeout(self, s):
        pass

    def quit(self):
        self.cerrado = True


cjm.ESPERA_PAGINA = 0  # las pruebas no necesitan esperar al render


# ---------------------------------------------------------------------------
print("\nplan A: estado de apollo")
# ---------------------------------------------------------------------------
d = DriverFalso(apollo=APOLLO)
juegos, origen = cjm.raspar_pagina(d, "cat", 1)
revisar("usa el plan A cuando hay apolloState", origen, "apollo")
revisar("deduplica Product y Concept", len(juegos), 6)
revisar("arma bien la URL", d.visitadas[0], "https://store.playstation.com/es-cl/category/cat/1")

por_nombre = {j.nombre: j for j in juegos}
g = por_nombre["Ghost of Tsushima DIRECTOR'S CUT"]
revisar("precio normal", g.precio_normal, Decimal("59990"))
revisar("precio de oferta", g.precio_oferta, Decimal("23996"))
revisar("clasificacion", g.clasificacion, "FULL_GAME")

completos = [j for j in juegos if cjm.es_juego_completo(j)[0]]
revisar("descarta el DLC y la moneda virtual", len(completos), 4)
revisar("el DLC queda fuera",
        "Assassin's Creed Valhalla - La Ira de los Druidas" in {j.nombre for j in completos}, False)


# ---------------------------------------------------------------------------
print("\nplan B: tarjetas del DOM")
# ---------------------------------------------------------------------------
tarjetas = [
    ElementoFalso("Ghost of Tsushima\n$23.996\n$59.990", "https://store.playstation.com/es-cl/product/EP-ABC"),
    ElementoFalso("EA SPORTS FC 26\n$34.995\n$69.990", "https://store.playstation.com/es-cl/product/EP-DEF"),
]
d = DriverFalso(apollo=None, tarjetas=tarjetas)
juegos, origen = cjm.raspar_pagina(d, "cat", 1)
revisar("cae al plan B sin apolloState", origen, "dom")
revisar("lee las tarjetas", len(juegos), 2)
revisar("toma el nombre", juegos[0].nombre, "Ghost of Tsushima")
revisar("el precio menor es la oferta", juegos[0].precio_oferta, Decimal("23996"))
revisar("el precio mayor es el normal", juegos[0].precio_normal, Decimal("59990"))
revisar("saca el id del href", juegos[0].ps_id, "EP-ABC")


# ---------------------------------------------------------------------------
print("\nreintentos y errores")
# ---------------------------------------------------------------------------
d = DriverFalso(apollo=None, tarjetas=[])
juegos, origen = cjm.raspar_pagina(d, "cat", 1)
revisar("pagina sin nada devuelve vacio", origen, "vacio")
revisar("reintenta 3 veces en total", len(d.visitadas), 3)

d = DriverFalso(apollo=APOLLO, explota_en=(1,))
juegos, origen = cjm.raspar_pagina(d, "cat", 1)
revisar("se recupera de una excepcion del navegador", origen, "apollo")
revisar("y devuelve los productos igual", len(juegos), 6)


# ---------------------------------------------------------------------------
print("\nrecorrido de varias paginas")
# ---------------------------------------------------------------------------
d = DriverFalso(apollo=APOLLO)
cjm.abrir_navegador = lambda headless=True: d
revisar("recorre las paginas pedidas", (cjm.raspar_todo(paginas_max=3), len(d.visitadas))[1], 3)
revisar("cierra el navegador al terminar", d.cerrado, True)

d = DriverFalso(apollo=None, tarjetas=[])
cjm.abrir_navegador = lambda headless=True: d
revisar("corta cuando el catalogo se acaba", len(cjm.raspar_todo(paginas_max=5)), 0)
revisar("no recorre las 5 paginas si vienen vacias", len(d.visitadas) < 15, True)


# ---------------------------------------------------------------------------
print("\ncapa Shopify")
# ---------------------------------------------------------------------------
llamadas: list[dict] = []


class RespuestaFalsa:
    def __init__(self, cuerpo, codigo=200):
        self._cuerpo, self.status_code = cuerpo, codigo

    def json(self):
        return self._cuerpo

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


def _variantes(pid, base):
    return {"nodes": [
        {"id": f"gid://shopify/ProductVariant/{base}1", "title": "Cuenta Primaria", "price": "9990", "compareAtPrice": None},
        {"id": f"gid://shopify/ProductVariant/{base}2", "title": "Cuenta Secundaria", "price": "6990", "compareAtPrice": None},
    ]}


def responder(url, headers=None, json=None, timeout=None, allow_redirects=None):
    llamadas.append({"url": url, "headers": headers, "body": json})
    if "products(first" in json["query"]:
        if json["variables"].get("cursor") is None:
            return RespuestaFalsa({"data": {"products": {
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                "nodes": [{"id": "gid://shopify/Product/1", "title": "Ghost of Tsushima",
                           "status": "ACTIVE", "variants": _variantes(1, 1)}]}}})
        return RespuestaFalsa({"data": {"products": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"id": "gid://shopify/Product/2", "title": "EA SPORTS FC 26",
                       "status": "ACTIVE", "variants": _variantes(2, 2)}]}}})
    return RespuestaFalsa({"data": {"productVariantsBulkUpdate": {
        "productVariants": [{"id": v["id"], "price": v["price"],
                             "compareAtPrice": v.get("compareAtPrice")}
                            for v in json["variables"]["variants"]],
        "userErrors": []}}})


requests_falso = types.SimpleNamespace(post=responder)
sys.modules["requests"] = requests_falso
os.environ["CJM_SHOPIFY_TOKEN"] = "shpat_token_de_prueba"

revisar("lee el token del entorno (asi funciona en Replit)", cjm._token(), "shpat_token_de_prueba")
productos = cjm.leer_catalogo()
revisar("pagina el catalogo completo", len(productos), 2)
revisar("manda el token en la cabecera", llamadas[0]["headers"]["X-Shopify-Access-Token"], "shpat_token_de_prueba")
revisar("usa la version de API configurada", f"/admin/api/{cjm.API_VERSION}/graphql.json" in llamadas[0]["url"], True)
revisar("pasa el cursor en la segunda pagina", llamadas[1]["body"]["variables"]["cursor"], "cursor-1")
revisar("sin userErrors devuelve lista vacia",
        cjm.aplicar_precios("gid://shopify/Product/1",
                            [{"id": "gid://shopify/ProductVariant/11", "price": "11990"}]), [])

requests_falso.post = lambda url, headers=None, json=None, timeout=None, allow_redirects=None: RespuestaFalsa(
    {"data": {"productVariantsBulkUpdate": {"productVariants": [], "userErrors": [
        {"field": ["variants", "0", "price"], "message": "Price must be greater than 0"}]}}})
revisar("reporta los userErrors de Shopify",
        cjm.aplicar_precios("gid://shopify/Product/1", [{"id": "x", "price": "-1"}]),
        ["['variants', '0', 'price']: Price must be greater than 0"])
requests_falso.post = responder


# ---------------------------------------------------------------------------
print("\ncorrida con --aplicar (Shopify simulado)")
# ---------------------------------------------------------------------------
JUEGOS = [
    cjm.JuegoPS("id-1", "Ghost of Tsushima", "FULL_GAME", "CLP", Decimal("59990"), Decimal("12000")),
    cjm.JuegoPS("id-2", "EA SPORTS FC 26", "FULL_GAME", "CLP", Decimal("69990"), Decimal("35000")),
]

with tempfile.TemporaryDirectory() as tmp:
    carpeta = Path(tmp)
    (carpeta / "mapeo.csv").write_text(
        "ps_id,ps_nombre,producto_id,variante_primaria,variante_secundaria,activo\n"
        "id-1,Ghost of Tsushima,gid://shopify/Product/1,"
        "gid://shopify/ProductVariant/11,gid://shopify/ProductVariant/12,si\n"
        "id-2,EA SPORTS FC 26,gid://shopify/Product/2,"
        "gid://shopify/ProductVariant/21,gid://shopify/ProductVariant/22,si\n",
        encoding="utf-8")

    originales = (cjm.raspar_todo, cjm.REPORTES, cjm.ARCHIVO_MAPEO, cjm.DRY_RUN)
    cjm.raspar_todo = lambda **kwargs: JUEGOS
    cjm.REPORTES = carpeta / "reportes"
    cjm.ARCHIVO_MAPEO = carpeta / "mapeo.csv"
    cjm.DRY_RUN = False  # el candado maestro abierto, como en produccion
    try:
        llamadas.clear()
        revisar("la corrida termina bien", cjm.main(["--aplicar"]), 0)
        revisar("una mutacion por producto", len(llamadas), 2)
        revisar("agrupa las dos variantes en una sola llamada",
                len(llamadas[0]["body"]["variables"]["variants"]), 2)
        revisar("manda price como texto", llamadas[0]["body"]["variables"]["variants"][0]["price"], "11990")
        revisar("manda compareAtPrice", llamadas[0]["body"]["variables"]["variants"][0]["compareAtPrice"], "17990")
        filas = list((carpeta / "reportes").glob("cambios_*.csv"))[0].read_text(
            encoding="utf-8-sig").strip().split("\n")[1:]
        revisar("marca las 4 filas como aplicadas",
                sum(1 for f in filas if f.strip().endswith("aplicado")), 4)

        llamadas.clear()
        cjm.main(["--aplicar", "--solo-simular"])
        revisar("--solo-simular no llama a Shopify", len(llamadas), 0)

        cjm.DRY_RUN = True
        llamadas.clear()
        cjm.main(["--aplicar"])
        revisar("DRY_RUN bloquea aunque venga --aplicar", len(llamadas), 0)
    finally:
        cjm.raspar_todo, cjm.REPORTES, cjm.ARCHIVO_MAPEO, cjm.DRY_RUN = originales
        sys.modules.pop("requests", None)


# ---------------------------------------------------------------------------
print()
if fallos:
    print(f"{len(fallos)} prueba(s) fallaron: {', '.join(fallos)}")
    raise SystemExit(1)
print("Integracion OK: Selenium (plan A, plan B, reintentos) y Shopify (lectura, escritura, candados)")
