#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pruebas de regresion: una por cada bug que se encontro y se arreglo.

Cada bloque dice que fallaba antes. Si alguna de estas pruebas se pone roja,
volvio un bug que ya costo encontrar.

    python3 pruebas_regresion.py
"""

from __future__ import annotations

import json
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


def juego(nombre, clasificacion=None, normal="59990", oferta="12000", ps_id="x", origen="apollo"):
    return cjm.JuegoPS(ps_id=ps_id, nombre=nombre, clasificacion=clasificacion, moneda="CLP",
                       precio_normal=Decimal(normal), precio_oferta=Decimal(oferta), origen=origen)


class ElementoFalso:
    def __init__(self, texto, href=""):
        self.text, self._href = texto, href

    def get_attribute(self, n):
        return self._href if n == "href" else None

    def find_element(self, by, sel):
        raise Exception("sin ancla interna")


class DriverFalso:
    def __init__(self, apollo=None, tarjetas=None, explota_en=()):
        self.apollo, self.tarjetas, self.explota_en = apollo, tarjetas or [], explota_en
        self.visitadas, self.cerrado = [], False

    def get(self, url):
        self.visitadas.append(url)
        if len(self.visitadas) in self.explota_en:
            raise Exception("timeout simulado")

    def execute_script(self, s):
        return json.dumps(self.apollo) if self.apollo is not None else None

    def find_elements(self, by, sel):
        return self.tarjetas if sel.startswith("[data-qa") else []

    def set_page_load_timeout(self, s): pass
    def set_script_timeout(self, s): pass
    def quit(self): self.cerrado = True


cjm.ESPERA_PAGINA = 0


# ---------------------------------------------------------------------------
print("\nCRITICO: el plan B inventaba ps_id posicionales ('dom-0', 'dom-1'...)")
# Antes: esos ids se repetian en cada pagina y en cada corrida apuntaban a un
# juego distinto. Si entraban a mapeo.csv, --aplicar le escribia a un producto
# de Shopify el precio de OTRO juego, sin ningun error ni advertencia.
# ---------------------------------------------------------------------------
d = DriverFalso(tarjetas=[ElementoFalso("Split Fiction\n$8.990\n$29.990")])
juegos = cjm.desde_dom(d)
revisar("una tarjeta sin href no recibe id inventado", juegos[0].ps_id, "")
revisar("pero si se lee el nombre y el precio", (juegos[0].nombre, juegos[0].precio_oferta),
        ("Split Fiction", Decimal("8990")))

d = DriverFalso(tarjetas=[ElementoFalso("Elden Ring\n$18.990\n$59.990",
                                        "https://store.playstation.com/es-cl/category/abc/2")])
revisar("un href que no es de producto tampoco produce id", cjm.desde_dom(d)[0].ps_id, "")

d = DriverFalso(tarjetas=[ElementoFalso("Elden Ring\n$18.990\n$59.990",
                                        "https://store.playstation.com/es-cl/product/EP0001-REAL_00-X")])
revisar("un href de producto si produce id", cjm.desde_dom(d)[0].ps_id, "EP0001-REAL_00-X")

revisar("_id_confiable rechaza los posicionales", cjm._id_confiable("dom-0"), False)
revisar("_id_confiable acepta uno real", cjm._id_confiable("EP0001-CUSA00001_00-XXXX"), True)
revisar("buscar_en_mapeo ignora un id posicional y cruza por nombre",
        cjm.buscar_en_mapeo({"id:dom-0": "FILA MALA", "elden ring": "FILA BUENA"},
                            juego("Elden Ring", ps_id="dom-0")), "FILA BUENA")


# ---------------------------------------------------------------------------
print("\nALTO: chromedriver reordenaba las claves y se perdia la clasificacion")
# Antes: chromedriver serializa los objetos JS en orden alfabetico, asi que
# 'Concept:...' llegaba antes que 'Product:...' y pisaba el registro bueno. El
# Concept no trae clasificacion, asi que el filtro exacto de DLC NUNCA corria.
# ---------------------------------------------------------------------------
def apollo_con_orden(concept_primero: bool) -> dict:
    precio = {"__typename": "Price", "basePrice": "$59.990", "discountedPrice": "$23.996",
              "currencyCode": "CLP", "discountText": "-60%"}
    producto = {"__typename": "Product", "id": "EP-REAL", "name": "Cyberpunk 2077: Phantom Liberty",
                "storeDisplayClassification": "ADD_ON", "price": {"__ref": "Price:p"}}
    concepto = {"__typename": "Concept", "id": "99", "name": "Cyberpunk 2077: Phantom Liberty",
                "price": {"__ref": "Price:p"}}
    if concept_primero:
        return {"Price:p": precio, "Concept:99": concepto, "Product:EP-REAL": producto}
    return {"Price:p": precio, "Product:EP-REAL": producto, "Concept:99": concepto}


for etiqueta, primero in (("Concept primero", True), ("Product primero", False)):
    js = cjm.desde_apollo(apollo_con_orden(primero))
    revisar(f"{etiqueta}: un solo registro", len(js), 1)
    revisar(f"{etiqueta}: conserva la clasificacion", js[0].clasificacion, "ADD_ON")
    revisar(f"{etiqueta}: conserva el precio", js[0].precio_oferta, Decimal("23996"))
    revisar(f"{etiqueta}: gana el id del Product", js[0].ps_id, "EP-REAL")
    revisar(f"{etiqueta}: el DLC se descarta", cjm.es_juego_completo(js[0])[0], False)

revisar("JS_APOLLO devuelve texto, no un objeto", "JSON.stringify" in cjm.JS_APOLLO, True)
revisar("leer_apollo parsea el texto",
        cjm.leer_apollo(DriverFalso(apollo={"Product:a": {"name": "X"}}))["Product:a"]["name"], "X")
revisar("leer_apollo con null devuelve None", cjm.leer_apollo(DriverFalso(apollo=None)), None)


# ---------------------------------------------------------------------------
print("\nALTO: una pagina con error abandonaba las 149 siguientes en silencio")
# Antes: un timeout se confundia con 'fin del catalogo' y cortaba el recorrido,
# terminando con exit 0 y notificacion de exito.
# ---------------------------------------------------------------------------
d = DriverFalso(apollo=None, tarjetas=[], explota_en=(1, 2, 3))
revisar("una pagina que nunca cargo se marca como error", cjm.raspar_pagina(d, "c", 1)[1], "error")
d = DriverFalso(apollo=None, tarjetas=[])
revisar("una pagina que cargo vacia se marca como vacio", cjm.raspar_pagina(d, "c", 1)[1], "vacio")

APOLLO_MINIMO = {"Price:p": {"__typename": "Price", "basePrice": "$1.990",
                             "discountedPrice": "$990", "currencyCode": "CLP"},
                 "Product:EP-A": {"__typename": "Product", "id": "EP-A", "name": "Juego A",
                                  "storeDisplayClassification": "FULL_GAME",
                                  "price": {"__ref": "Price:p"}}}


class DriverPorPagina(DriverFalso):
    """Falla solo en la pagina 2 y devuelve datos en las demas."""

    def get(self, url):
        self.visitadas.append(url)
        self.apollo = None if url.endswith("/2") else APOLLO_MINIMO
        if url.endswith("/2"):
            raise Exception("timeout en la pagina 2")


d = DriverPorPagina()
cjm.abrir_navegador = lambda headless=True: d
r = cjm.raspar_todo(paginas_max=4)
revisar("no abandona el recorrido por una pagina con error", len(d.visitadas) >= 6, True)
revisar("informa cuantas paginas fallaron", r.paginas_error, 1)
revisar("igual junta lo que si pudo leer", len(r) >= 1, True)


# ---------------------------------------------------------------------------
print("\nALTO: 'Demon's Souls' se descartaba por contener 'demo'")
# Antes el filtro buscaba substrings, no palabras completas.
# ---------------------------------------------------------------------------
revisar("Demon's Souls pasa el filtro", cjm.es_juego_completo(juego("Demon's Souls"))[0], True)
revisar("Demonschool pasa el filtro", cjm.es_juego_completo(juego("Demonschool"))[0], True)
revisar("un demo de verdad se descarta", cjm.es_juego_completo(juego("Nioh 2 - Demo"))[0], False)
revisar("el pase de temporada sigue descartado",
        cjm.es_juego_completo(juego("FIFA - Pase de Temporada"))[0], False)
revisar("'Themes' no descarta a 'The Medium'", cjm.es_juego_completo(juego("The Medium"))[0], True)


# ---------------------------------------------------------------------------
print("\nALTO: tarjetas con tres precios (precio PS Plus) daban un precio 37% menor")
# ---------------------------------------------------------------------------
d = DriverFalso(tarjetas=[ElementoFalso("-50%\nBaldur's Gate 3\n$30.990\n$19.990\n$61.990",
                                        "https://store.playstation.com/es-cl/product/EP-BG3")])
revisar("una tarjeta con 3 precios se saltea en vez de adivinar", len(cjm.desde_dom(d)), 0)

d = DriverFalso(tarjetas=[ElementoFalso("$19.990", "https://store.playstation.com/es-cl/product/EP-X"),
                          ElementoFalso("Juego Real\n$19.990\n$39.990",
                                        "https://store.playstation.com/es-cl/product/EP-Y")])
juegos = cjm.desde_dom(d)
revisar("las filas basura (nodos hijos) se descartan", len(juegos), 1)
revisar("y queda la tarjeta buena", juegos[0].nombre, "Juego Real")


# ---------------------------------------------------------------------------
print("\nALTO: mapeo.csv sin salto de linea final perdia una fila al fusionar")
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    carpeta = Path(tmp)
    mapeo = carpeta / "mapeo.csv"
    mapeo.write_text(
        "ps_id,ps_nombre,producto_id,variante_primaria,variante_secundaria,activo\n"
        "EP-1,Juego Viejo,gid://shopify/Product/1,gid://shopify/ProductVariant/11,"
        "gid://shopify/ProductVariant/12,no",  # <- sin salto final, a proposito
        encoding="utf-8")
    propuesta = carpeta / "mapeo_propuesto.csv"
    propuesta.write_text(
        "ps_id,ps_nombre,producto_id,variante_primaria,variante_secundaria,activo\n"
        "EP-2,Juego Nuevo,gid://shopify/Product/2,gid://shopify/ProductVariant/21,"
        "gid://shopify/ProductVariant/22,si\n", encoding="utf-8")

    original = cjm.ARCHIVO_MAPEO
    cjm.ARCHIVO_MAPEO = mapeo
    try:
        cjm.modo_fusionar_mapeo(propuesta)
        cargado = cjm.cargar_mapeo(mapeo)
        revisar("la fila vieja sobrevive", cargado["juego viejo"].producto_id, "gid://shopify/Product/1")
        revisar("y conserva su activo=no", cargado["juego viejo"].activo, False)
        revisar("la fila nueva se agrega", cargado["juego nuevo"].producto_id, "gid://shopify/Product/2")
    finally:
        cjm.ARCHIVO_MAPEO = original


# ---------------------------------------------------------------------------
print("\nBAJO: --fusionar-mapeo aceptaba cualquier CSV con columna ps_nombre")
# Antes, pasarle un revisar_*.csv por error dejaba juegos 'mapeados' a la nada.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    malo = Path(tmp) / "revisar.csv"
    malo.write_text("motivo,ps_nombre,precio_ps,detalle\nsin mapear,Juego X,12000,\n", encoding="utf-8")
    try:
        cjm.modo_fusionar_mapeo(malo)
        revisar("rechaza un CSV que no es un mapeo propuesto", "no se quejo", "SystemExit")
    except SystemExit:
        revisar("rechaza un CSV que no es un mapeo propuesto", True, True)


# ---------------------------------------------------------------------------
print("\nMEDIO: mapeo.csv con ids mal pegados mataba la corrida entera")
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    ruta = Path(tmp) / "mapeo.csv"
    ruta.write_text(
        "ps_id,ps_nombre,producto_id,variante_primaria,variante_secundaria,activo\n"
        # id numerico copiado de la URL del admin, sin el prefijo gid://
        "EP-A,Juego Con Id Malo,gid://shopify/Product/1,47144508948611,gid://shopify/ProductVariant/12,si\n"
        "EP-B,Juego Sano,gid://shopify/Product/2,gid://shopify/ProductVariant/21,gid://shopify/ProductVariant/22,si\n"
        "EP-B,Otro Con Id Repetido,gid://shopify/Product/3,gid://shopify/ProductVariant/31,gid://shopify/ProductVariant/32,si\n"
        "dom-0,Juego Con Id Inventado,gid://shopify/Product/4,gid://shopify/ProductVariant/41,gid://shopify/ProductVariant/42,si\n",
        encoding="utf-8")
    m = cjm.cargar_mapeo(ruta)
    revisar("detecta el id sin prefijo gid://", "47144508948611" in (m["juego con id malo"].problema or ""), True)
    revisar("la fila sana no tiene problema", m["juego sano"].problema, None)
    revisar("un ps_id duplicado anula el indice por id", "id:EP-B" in m, False)
    revisar("pero las dos filas siguen accesibles por nombre",
            ("juego sano" in m, "otro con id repetido" in m), (True, True))
    revisar("un ps_id inventado no se indexa", "id:dom-0" in m, False)


# ---------------------------------------------------------------------------
print("\nBAJO: la tabla aceptaba un precio tachado menor que el precio de venta")
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    mala = Path(tmp) / "tabla.csv"
    mala.write_text(
        "desde,hasta,precio_primaria,precio_secundaria,compare_primaria,compare_secundaria\n"
        "0,2999,7990,5490,4990,3490\n", encoding="utf-8")
    try:
        cjm.cargar_tabla(mala)
        revisar("rechaza compare_at menor que el precio", "no se quejo", "SystemExit")
    except SystemExit:
        revisar("rechaza compare_at menor que el precio", True, True)


# ---------------------------------------------------------------------------
print("\nALTO/BAJO: capa Shopify (costo, throttling, redirecciones, dominio)")
# ---------------------------------------------------------------------------
revisar("la consulta de catalogo pide 50 productos, no 100", "products(first: 50" in cjm.CONSULTA_CATALOGO, True)
revisar("y 10 variantes, no 20", "variants(first: 10" in cjm.CONSULTA_CATALOGO, True)

original_tienda = cjm.TIENDA
for dominio, valido in (("cjm-digitales.myshopify.com", True),
                        ("cjm-digitales.myshopify.com.cl", False),
                        ("evil.com", False)):
    cjm.TIENDA = dominio
    try:
        cjm._validar_tienda()
        ok = True
    except SystemExit:
        ok = False
    revisar(f"dominio '{dominio}' {'aceptado' if valido else 'rechazado'}", ok, valido)
cjm.TIENDA = original_tienda


class RespuestaFalsa:
    def __init__(self, cuerpo, codigo=200):
        self._c, self.status_code = cuerpo, codigo

    def json(self):
        return self._c

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


import os  # noqa: E402
os.environ["CJM_SHOPIFY_TOKEN"] = "shpat_de_prueba"
cjm.PAUSA_ENTRE_LLAMADAS = 0

intentos = {"n": 0}


def responder_throttled(url, headers=None, json=None, timeout=None, allow_redirects=None):
    intentos["n"] += 1
    if intentos["n"] == 1:
        return RespuestaFalsa({"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                               "extensions": {"cost": {"requestedQueryCost": 100,
                                                       "throttleStatus": {"currentlyAvailable": 0,
                                                                          "restoreRate": 100}}}})
    return RespuestaFalsa({"data": {"ok": True}})


sys.modules["requests"] = types.SimpleNamespace(post=responder_throttled)
revisar("THROTTLED se espera y se reintenta", cjm.graphql("query{x}"), {"ok": True})
revisar("y basto un reintento", intentos["n"], 2)

sys.modules["requests"] = types.SimpleNamespace(
    post=lambda url, **k: RespuestaFalsa({"errors": [{"extensions": {"code": "MAX_COST_EXCEEDED"}}]}))
try:
    cjm.graphql("query{x}")
    revisar("MAX_COST_EXCEEDED no se reintenta", "no fallo", "RuntimeError")
except RuntimeError as e:
    revisar("MAX_COST_EXCEEDED no se reintenta", "demasiado cara" in str(e), True)

sys.modules["requests"] = types.SimpleNamespace(post=lambda url, **k: RespuestaFalsa({}, codigo=307))
try:
    cjm.graphql("query{x}")
    revisar("no sigue redirecciones (el token viaja en la cabecera)", "las siguio", "RuntimeError")
except RuntimeError as e:
    revisar("no sigue redirecciones (el token viaja en la cabecera)", "redireccion" in str(e), True)

quintas = {"n": 0}


def responder_500(url, headers=None, json=None, timeout=None, allow_redirects=None):
    quintas["n"] += 1
    if quintas["n"] < 3:
        return RespuestaFalsa({}, codigo=503)
    return RespuestaFalsa({"data": {"ok": True}})


sys.modules["requests"] = types.SimpleNamespace(post=responder_500)
revisar("un 503 se reintenta", cjm.graphql("query{x}"), {"ok": True})
sys.modules.pop("requests", None)


# ---------------------------------------------------------------------------
print("\nALTO: un fallo a mitad de --aplicar borraba toda la evidencia")
# Antes los reportes se escribian DESPUES del bucle: si algo explotaba en el
# producto 40, quedaban 39 precios cambiados en Shopify y cero registro.
# ---------------------------------------------------------------------------
JUEGOS = [juego(f"Juego {i}", "FULL_GAME", ps_id=f"EP-{i}") for i in range(1, 7)]

with tempfile.TemporaryDirectory() as tmp:
    carpeta = Path(tmp)
    filas = "".join(
        f"EP-{i},Juego {i},gid://shopify/Product/{i},"
        f"gid://shopify/ProductVariant/{i}1,gid://shopify/ProductVariant/{i}2,si\n"
        for i in range(1, 7))
    (carpeta / "mapeo.csv").write_text(
        "ps_id,ps_nombre,producto_id,variante_primaria,variante_secundaria,activo\n" + filas,
        encoding="utf-8")

    llamadas = {"n": 0}

    def responder_muere(url, headers=None, json=None, timeout=None, allow_redirects=None):
        llamadas["n"] += 1
        if llamadas["n"] == 3:
            raise Exception("se corto la red a mitad de la corrida")
        return RespuestaFalsa({"data": {"productVariantsBulkUpdate": {
            "productVariants": [], "userErrors": []}}})

    sys.modules["requests"] = types.SimpleNamespace(post=responder_muere)
    originales = (cjm.raspar_todo, cjm.REPORTES, cjm.ARCHIVO_MAPEO, cjm.ARCHIVO_ESTADO, cjm.DRY_RUN)
    cjm.raspar_todo = lambda **k: cjm.ResultadoScrape(juegos=JUEGOS)
    cjm.REPORTES = carpeta / "reportes"
    cjm.ARCHIVO_MAPEO = carpeta / "mapeo.csv"
    cjm.ARCHIVO_ESTADO = carpeta / "estado.json"
    cjm.DRY_RUN = False
    cjm.REINTENTOS_SHOPIFY = 1
    try:
        codigo = cjm.main(["--aplicar"])
        revisar("la corrida informa el fallo", codigo, 1)
        cambios = list((carpeta / "reportes").glob("cambios_*.csv"))
        revisar("EL REPORTE SE ESCRIBE IGUAL", len(cambios), 1)
        texto = cambios[0].read_text(encoding="utf-8-sig")
        revisar("registra los que si se aplicaron", texto.count("aplicado") >= 2, True)
        revisar("y registra el que fallo", "excepcion" in texto, True)
    finally:
        cjm.raspar_todo, cjm.REPORTES, cjm.ARCHIVO_MAPEO, cjm.ARCHIVO_ESTADO, cjm.DRY_RUN = originales
        cjm.REINTENTOS_SHOPIFY = 5
        sys.modules.pop("requests", None)


# ---------------------------------------------------------------------------
print("\nALTO: 0 productos raspados se reportaba como corrida exitosa")
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    carpeta = Path(tmp)
    (carpeta / "mapeo.csv").write_text(
        "ps_id,ps_nombre,producto_id,variante_primaria,variante_secundaria,activo\n", encoding="utf-8")
    originales = (cjm.raspar_todo, cjm.REPORTES, cjm.ARCHIVO_MAPEO, cjm.ARCHIVO_ESTADO)
    cjm.REPORTES, cjm.ARCHIVO_MAPEO = carpeta / "reportes", carpeta / "mapeo.csv"
    cjm.ARCHIVO_ESTADO = carpeta / "estado.json"
    try:
        cjm.raspar_todo = lambda **k: cjm.ResultadoScrape(juegos=[])
        revisar("scrape vacio devuelve codigo 2 (fallo)", cjm.main([]), 2)

        cjm.raspar_todo = lambda **k: cjm.ResultadoScrape(juegos=JUEGOS, paginas_error=3)
        revisar("paginas con error devuelven codigo 1", cjm.main([]), 1)
    finally:
        cjm.raspar_todo, cjm.REPORTES, cjm.ARCHIVO_MAPEO, cjm.ARCHIVO_ESTADO = originales


# ---------------------------------------------------------------------------
print("\nALTO: cuando una oferta terminaba, el precio quedaba rebajado para siempre")
# El sistema solo baja precios. Ahora al menos avisa.
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    carpeta = Path(tmp)
    (carpeta / "mapeo.csv").write_text(
        "ps_id,ps_nombre,producto_id,variante_primaria,variante_secundaria,activo\n"
        "EP-BL4,Borderlands 4,gid://shopify/Product/9,"
        "gid://shopify/ProductVariant/91,gid://shopify/ProductVariant/92,si\n", encoding="utf-8")
    originales = (cjm.raspar_todo, cjm.REPORTES, cjm.ARCHIVO_MAPEO, cjm.ARCHIVO_ESTADO)
    cjm.REPORTES, cjm.ARCHIVO_MAPEO = carpeta / "reportes", carpeta / "mapeo.csv"
    cjm.ARCHIVO_ESTADO = carpeta / "estado.json"
    try:
        # Semana 1: el juego esta en oferta.
        cjm.raspar_todo = lambda **k: cjm.ResultadoScrape(
            juegos=[juego("Borderlands 4", "FULL_GAME", "69990", "12000", "EP-BL4")])
        cjm.main([])
        revisar("se guarda el estado de la corrida", cjm.ARCHIVO_ESTADO.exists(), True)
        guardado = json.loads(cjm.ARCHIVO_ESTADO.read_text(encoding="utf-8"))
        revisar("con el precio normal para poder restaurarlo",
                guardado["borderlands 4"]["precio_normal_ps"], "69990")

        # Semana 2: la oferta termino y el juego desaparece del listado.
        cjm.raspar_todo = lambda **k: cjm.ResultadoScrape(
            juegos=[juego("Otro Juego", "FULL_GAME", ps_id="EP-OTRO")])
        cjm.main([])
        revisar_csv = sorted((carpeta / "reportes").glob("revisar_*.csv"))[-1]
        texto = revisar_csv.read_text(encoding="utf-8-sig")
        revisar("avisa que Borderlands 4 salio de oferta", "salio de oferta" in texto, True)
        revisar("y dice a que precio estaba", "69990" in texto, True)
    finally:
        cjm.raspar_todo, cjm.REPORTES, cjm.ARCHIVO_MAPEO, cjm.ARCHIVO_ESTADO = originales


# ---------------------------------------------------------------------------
print("\nBAJO: dos corridas en el mismo minuto se pisaban el reporte")
# ---------------------------------------------------------------------------
revisar("el sello de los reportes incluye segundos",
        "%Y%m%d-%H%M%S" in Path("cjm_precios_ps.py").read_text(encoding="utf-8"), True)


# ---------------------------------------------------------------------------
print()
if fallos:
    print(f"{len(fallos)} prueba(s) fallaron: {', '.join(fallos)}")
    raise SystemExit(1)
print("Todas las regresiones cubiertas.")
