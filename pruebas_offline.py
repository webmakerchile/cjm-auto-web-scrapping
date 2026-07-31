#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pruebas de la logica que no necesita internet ni Shopify.

Cubren el parseo de precios, la lectura del estado de Apollo, el filtro de
juegos completos, los tramos de precio y el mapeo. No abren Chrome ni tocan la
Admin API, asi que se pueden correr en cualquier momento:

    python3 pruebas_offline.py
"""

from __future__ import annotations

import tempfile
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
print("\nprecios")
# ---------------------------------------------------------------------------
revisar("CLP con punto de miles", cjm.parse_precio("$ 29.990", "CLP"), Decimal("29990"))
revisar("CLP de seis digitos", cjm.parse_precio("$ 119.990", "CLP"), Decimal("119990"))
revisar("USD con decimales", cjm.parse_precio("US$29.99", "USD"), Decimal("29.99"))
revisar("EUR formato europeo", cjm.parse_precio("1.234,56 €", "EUR"), Decimal("1234.56"))
revisar("texto vacio", cjm.parse_precio("", "CLP"), None)
revisar("gratis", cjm.parse_precio("Gratis", "CLP"), None)
revisar("valor de apollo en CLP", cjm.valor_apollo(2999000, "CLP"), Decimal("29990"))
revisar("valor de apollo en USD", cjm.valor_apollo(2999, "USD"), Decimal("29.99"))


# ---------------------------------------------------------------------------
print("\nnormalizacion de nombres")
# ---------------------------------------------------------------------------
revisar("tildes y mayusculas", cjm.normalizar("Assassin's Creed Valhalla"), "assassin s creed valhalla")
revisar("acentos", cjm.normalizar("Pokémon™ Legends"), "pokemon legends")
revisar("secuelas distintas",
        cjm.normalizar("EA SPORTS FC 25") == cjm.normalizar("EA SPORTS FC 26"), False)


# ---------------------------------------------------------------------------
print("\nestado de apollo")
# ---------------------------------------------------------------------------
# Reproduce como normaliza Apollo: el precio vive aparte y se referencia con __ref.
ESTADO = {
    "Price:precio-1": {
        "__typename": "Price",
        "basePrice": "$59.990",
        "basePriceValue": 5999000,
        "discountedPrice": "$29.990",
        "discountedValue": 2999000,
        "currencyCode": "CLP",
        "discountText": "-50%",
        "isFree": False,
    },
    "Price:precio-2": {
        "__typename": "Price",
        "basePrice": "$9.990",
        "discountedPrice": "$4.990",
        "currencyCode": "CLP",
    },
    "Product:EP0001-CUSA00001_00-JUEGOCOMPLETO01": {
        "__typename": "Product",
        "id": "EP0001-CUSA00001_00-JUEGOCOMPLETO01",
        "name": "Ghost of Tsushima",
        "storeDisplayClassification": "FULL_GAME",
        "platforms": ["PS4", "PS5"],
        "price": {"__ref": "Price:precio-1"},
    },
    "Concept:10000001": {
        "__typename": "Concept",
        "id": "10000001",
        "name": "Ghost of Tsushima",
        "price": {"__ref": "Price:precio-1"},
    },
    "Product:EP0001-CUSA00002_00-DLC0000000000001": {
        "__typename": "Product",
        "id": "EP0001-CUSA00002_00-DLC0000000000001",
        "name": "Ghost of Tsushima - Expansion Iki",
        "storeDisplayClassification": "ADD_ON",
        "price": {"__ref": "Price:precio-2"},
    },
    "ROOT_QUERY": {"__typename": "Query"},
}

juegos = cjm.desde_apollo(ESTADO)
por_nombre = {j.nombre: j for j in juegos}
revisar("deduplica Product y Concept del mismo juego", len(juegos), 2)

g = por_nombre["Ghost of Tsushima"]
revisar("resuelve el __ref del precio normal", g.precio_normal, Decimal("59990"))
revisar("resuelve el precio de oferta", g.precio_oferta, Decimal("29990"))
revisar("lee la clasificacion", g.clasificacion, "FULL_GAME")
revisar("lee la moneda", g.moneda, "CLP")
revisar("detecta que esta en oferta", g.en_oferta, True)
revisar("usa el precio rebajado como referencia", g.precio_referencia, Decimal("29990"))
revisar("lee las plataformas", g.plataformas, ["PS4", "PS5"])

dlc = por_nombre["Ghost of Tsushima - Expansion Iki"]
revisar("clasifica el DLC", dlc.clasificacion, "ADD_ON")


# ---------------------------------------------------------------------------
print("\nfiltro de juegos completos")
# ---------------------------------------------------------------------------
def juego(nombre: str, clasificacion=None) -> cjm.JuegoPS:
    return cjm.JuegoPS(ps_id="x", nombre=nombre, clasificacion=clasificacion,
                       moneda="CLP", precio_normal=Decimal("10000"),
                       precio_oferta=Decimal("5000"))


revisar("FULL_GAME pasa", cjm.es_juego_completo(juego("Elden Ring", "FULL_GAME"))[0], True)
revisar("ADD_ON se descarta", cjm.es_juego_completo(juego("Nombre Limpio", "ADD_ON"))[0], False)
revisar("PREMIUM_EDITION pasa", cjm.es_juego_completo(juego("Elden Ring Deluxe", "PREMIUM_EDITION"))[0], True)
revisar("la clasificacion gana sobre el nombre",
        cjm.es_juego_completo(juego("Pack de Monedas Doradas", "FULL_GAME"))[0], True)
revisar("sin clasificacion, pase de temporada se descarta",
        cjm.es_juego_completo(juego("FIFA 24 - Pase de Temporada"))[0], False)
revisar("sin clasificacion, moneda virtual se descarta",
        cjm.es_juego_completo(juego("GTA V - 1.250.000 de creditos"))[0], False)
revisar("sin clasificacion, juego normal pasa",
        cjm.es_juego_completo(juego("Red Dead Redemption 2"))[0], True)
# El caso que el filtro por palabras no puede resolver: queda documentado.
revisar("DLC con nombre limpio se escapa sin clasificacion",
        cjm.es_juego_completo(juego("Assassin's Creed Valhalla - La Ira de los Druidas"))[0], True)


# ---------------------------------------------------------------------------
print("\ntramos de precio")
# ---------------------------------------------------------------------------
tramos = cjm.cargar_tabla(Path(__file__).resolve().parent / "tabla_precios.csv")
revisar("carga todos los tramos", len(tramos) > 0, True)

t = cjm.buscar_tramo(tramos, Decimal("12000"))
revisar("encuentra el tramo de 12.000", (t.precio_primaria, t.precio_secundaria), (11990, 7990))
revisar("la primaria siempre cuesta mas que la secundaria",
        all(x.precio_primaria > x.precio_secundaria for x in tramos), True)
revisar("el compareAt es mayor que el precio",
        all(x.compare_primaria > x.precio_primaria for x in tramos if x.compare_primaria), True)
revisar("borde inferior del tramo", cjm.buscar_tramo(tramos, Decimal("10000")).precio_primaria, 11990)
revisar("borde superior del tramo", cjm.buscar_tramo(tramos, Decimal("14999")).precio_primaria, 11990)
revisar("precio fuera de todos los tramos", cjm.buscar_tramo(tramos, Decimal("999999")), None)
revisar("precio nulo", cjm.buscar_tramo(tramos, None), None)

with tempfile.TemporaryDirectory() as tmp:
    solapada = Path(tmp) / "solapada.csv"
    solapada.write_text(
        "desde,hasta,precio_primaria,precio_secundaria,compare_primaria,compare_secundaria\n"
        "0,5000,1000,900,,\n"
        "4000,9000,2000,1800,,\n",
        encoding="utf-8",
    )
    try:
        cjm.cargar_tabla(solapada)
        revisar("detecta tramos solapados", "no aviso", "SystemExit")
    except SystemExit:
        revisar("detecta tramos solapados", True, True)


# ---------------------------------------------------------------------------
print("\nmapeo")
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    ruta = Path(tmp) / "mapeo.csv"
    ruta.write_text(
        "ps_id,ps_nombre,producto_id,variante_primaria,variante_secundaria,activo\n"
        "EP0001-CUSA00001_00-JUEGOCOMPLETO01,Ghost of Tsushima,gid://shopify/Product/1,"
        "gid://shopify/ProductVariant/11,gid://shopify/ProductVariant/12,si\n"
        "EP0002-CUSA00002_00-OTRO000000000001,God of War Ragnarök,gid://shopify/Product/2,"
        "gid://shopify/ProductVariant/21,gid://shopify/ProductVariant/22,no\n",
        encoding="utf-8",
    )
    mapeo = cjm.cargar_mapeo(ruta)

    encontrado = cjm.buscar_en_mapeo(mapeo, juego("Ghost of Tsushima"))
    revisar("busca por nombre", encontrado.producto_id, "gid://shopify/Product/1")

    por_id = cjm.JuegoPS(ps_id="EP0001-CUSA00001_00-JUEGOCOMPLETO01", nombre="Otro nombre",
                         clasificacion=None, moneda="CLP", precio_normal=None, precio_oferta=None)
    revisar("busca por ps_id aunque cambie el nombre",
            cjm.buscar_en_mapeo(mapeo, por_id).producto_id, "gid://shopify/Product/1")

    revisar("respeta tildes en el nombre",
            cjm.buscar_en_mapeo(mapeo, juego("God of War Ragnarok")).producto_id,
            "gid://shopify/Product/2")
    revisar("lee la columna activo",
            cjm.buscar_en_mapeo(mapeo, juego("God of War Ragnarök")).activo, False)
    revisar("juego que no esta en el mapeo", cjm.buscar_en_mapeo(mapeo, juego("Juego Nuevo 2026")), None)

    respaldo = cjm.respaldar(ruta)
    revisar("el respaldo queda escrito", respaldo.exists() and respaldo.read_bytes() == ruta.read_bytes(), True)


# ---------------------------------------------------------------------------
print("\ncorrida completa en seco (sin red ni Shopify)")
# ---------------------------------------------------------------------------
# Reemplazamos el scraper por datos fijos y corremos el flujo entero: filtro,
# tramos, mapeo y escritura de reportes. Nada toca internet.
CATALOGO_FALSO = [
    # mapeado y en oferta -> tiene que generar 2 filas de cambio
    cjm.JuegoPS("id-1", "Ghost of Tsushima", "FULL_GAME", "CLP", Decimal("59990"), Decimal("12000")),
    # en oferta pero sin mapear -> revisar
    cjm.JuegoPS("id-2", "Juego Nuevo 2026", "FULL_GAME", "CLP", Decimal("39990"), Decimal("19990")),
    # DLC -> se descarta antes de todo
    cjm.JuegoPS("id-3", "Pack de Monedas", "ADD_ON", "CLP", Decimal("9990"), Decimal("4990")),
    # sin descuento -> no se toca
    cjm.JuegoPS("id-4", "Juego Sin Oferta", "FULL_GAME", "CLP", Decimal("29990"), Decimal("29990")),
    # precio fuera de todos los tramos -> revisar
    cjm.JuegoPS("id-5", "Juego Carisimo", "FULL_GAME", "CLP", Decimal("900000"), Decimal("800000")),
]

with tempfile.TemporaryDirectory() as tmp:
    carpeta = Path(tmp)
    (carpeta / "mapeo.csv").write_text(
        "ps_id,ps_nombre,producto_id,variante_primaria,variante_secundaria,activo\n"
        "id-1,Ghost of Tsushima,gid://shopify/Product/1,"
        "gid://shopify/ProductVariant/11,gid://shopify/ProductVariant/12,si\n",
        encoding="utf-8",
    )
    originales = (cjm.raspar_todo, cjm.REPORTES, cjm.ARCHIVO_MAPEO)
    cjm.raspar_todo = lambda **kwargs: CATALOGO_FALSO
    cjm.REPORTES = carpeta / "reportes"
    cjm.ARCHIVO_MAPEO = carpeta / "mapeo.csv"
    try:
        codigo = cjm.main([])
        revisar("la corrida termina bien", codigo, 0)

        cambios = list((carpeta / "reportes").glob("cambios_*.csv"))
        revisar("escribe cambios_*.csv", len(cambios), 1)
        filas = cambios[0].read_text(encoding="utf-8-sig").strip().split("\n")[1:]
        revisar("una fila por variante del juego mapeado", len(filas), 2)
        revisar("usa el tramo del precio rebajado (12.000 -> 11990/7990)",
                sorted(f.split(",")[6] for f in filas), ["11990", "7990"])
        revisar("marca las filas como simuladas", all(f.strip().endswith("simulado") for f in filas), True)

        revisar_csv = list((carpeta / "reportes").glob("revisar_*.csv"))[0]
        texto_revisar = revisar_csv.read_text(encoding="utf-8-sig")
        revisar("reporta el juego sin mapear", "Juego Nuevo 2026" in texto_revisar, True)
        revisar("reporta el precio fuera de tramo", "Juego Carisimo" in texto_revisar, True)
        revisar("no reporta el DLC descartado", "Pack de Monedas" in texto_revisar, False)
        revisar("no reporta el juego sin descuento", "Juego Sin Oferta" in texto_revisar, False)
    finally:
        cjm.raspar_todo, cjm.REPORTES, cjm.ARCHIVO_MAPEO = originales


# ---------------------------------------------------------------------------
print("\ncandados de seguridad")
# ---------------------------------------------------------------------------
revisar("DRY_RUN viene activado", cjm.DRY_RUN, True)
revisar("correr.sh no trae el token", "shpat_" not in Path(
    Path(__file__).resolve().parent / "correr.sh").read_text(encoding="utf-8"), True)
revisar("el script lee el token del Llavero",
        "find-generic-password" in Path(
            Path(__file__).resolve().parent / "cjm_precios_ps.py").read_text(encoding="utf-8"), True)


# ---------------------------------------------------------------------------
print()
if fallos:
    print(f"{len(fallos)} prueba(s) fallaron: {', '.join(fallos)}")
    raise SystemExit(1)
print("Todas las pruebas pasaron.")
