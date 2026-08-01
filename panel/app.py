"""Panel web privado para CJM Digitales.

- Login con sesiones (SESSION_SECRET).
- Superadmin: webmakerchile@gmail.com, contraseña en el Secret SUPERADMIN_PASSWORD.
- El superadmin puede crear usuarios y activarlos/desactivarlos.
- Botón que corre el scraper EN SECO (--solo-simular): nunca escribe en Shopify.
"""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import secrets

import psycopg2
import psycopg2.extras

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

# Reutilizamos la misma logica de filtrado del scraper (una sola fuente de verdad).
from cjm_precios_ps import PALABRAS_DESCARTE, normalizar  # noqa: E402
DB_SQLITE_VIEJA = Path(__file__).resolve().parent / "usuarios.db"
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("Falta DATABASE_URL: el panel necesita la base PostgreSQL de Replit")
REPORTES = RAIZ / "reportes"
SUPERADMIN = "webmakerchile@gmail.com"

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET")
if not app.secret_key:
    raise SystemExit("Falta el Secret SESSION_SECRET")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


# ---------------------------------------------------------------- CSRF
@app.context_processor
def _inyectar_csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return {"csrf_token": session["csrf"]}


@app.before_request
def _verificar_csrf():
    if request.method == "POST":
        esperado = session.get("csrf")
        if not esperado or not secrets.compare_digest(
            esperado, request.form.get("csrf_token", "")
        ):
            abort(400, "Token CSRF inválido: recarga la página e intenta de nuevo.")


def _destino_seguro(url: str | None) -> str:
    # Solo rutas locales relativas: evita open redirect.
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return url_for("inicio")


# ---------------------------------------------------------------- base de datos
def _conectar():
    con = psycopg2.connect(DATABASE_URL)
    con.autocommit = False
    return con


def db():
    if "db" not in g:
        g.db = _conectar()
    return g.db


def consulta(sql: str, params: tuple = ()):  # SELECT
    with db().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def ejecutar(sql: str, params: tuple = ()) -> None:  # INSERT/UPDATE/DELETE
    with db().cursor() as cur:
        cur.execute(sql, params)
    db().commit()


@app.teardown_appcontext
def _cerrar_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def init_db() -> None:
    con = _conectar()
    with con.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS usuarios (
                   email TEXT PRIMARY KEY,
                   clave_hash TEXT NOT NULL,
                   activo INTEGER NOT NULL DEFAULT 1,
                   creado TEXT NOT NULL
               )"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS reportes (
                   nombre TEXT PRIMARY KEY,
                   contenido TEXT NOT NULL,
                   modificado TIMESTAMP NOT NULL DEFAULT NOW()
               )"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS migraciones (
                   nombre TEXT PRIMARY KEY,
                   aplicada TIMESTAMP NOT NULL DEFAULT NOW()
               )"""
        )
        # Migración única desde la vieja base SQLite del workspace: solo si
        # nunca se hizo antes (queda registrada en la tabla migraciones para
        # que un reinicio no resucite usuarios ya borrados del panel).
        cur.execute("SELECT 1 FROM migraciones WHERE nombre = %s", ("usuarios_sqlite",))
        ya_migrada = cur.fetchone() is not None
        if not ya_migrada and DB_SQLITE_VIEJA.exists():
            vieja = sqlite3.connect(DB_SQLITE_VIEJA)
            vieja.row_factory = sqlite3.Row
            try:
                filas = vieja.execute(
                    "SELECT email, clave_hash, activo, creado FROM usuarios"
                ).fetchall()
            except sqlite3.Error:
                filas = []
            vieja.close()
            for f in filas:
                cur.execute(
                    """INSERT INTO usuarios (email, clave_hash, activo, creado)
                       VALUES (%s, %s, %s, %s) ON CONFLICT (email) DO NOTHING""",
                    (f["email"], f["clave_hash"], f["activo"], f["creado"]),
                )
            cur.execute(
                "INSERT INTO migraciones (nombre) VALUES (%s) ON CONFLICT DO NOTHING",
                ("usuarios_sqlite",),
            )
    con.commit()
    con.close()
    sincronizar_reportes()


def sincronizar_reportes() -> None:
    """Copia los CSV de reportes/ a la base para que sobrevivan a un deploy."""
    if not REPORTES.exists():
        return
    con = _conectar()
    try:
        with con.cursor() as cur:
            for p in REPORTES.glob("*.csv"):
                try:
                    contenido = p.read_text(encoding="utf-8")
                except OSError:
                    continue
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
                cur.execute(
                    """INSERT INTO reportes (nombre, contenido, modificado)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (nombre) DO UPDATE
                       SET contenido = EXCLUDED.contenido,
                           modificado = EXCLUDED.modificado
                       WHERE reportes.modificado < EXCLUDED.modificado""",
                    (p.name, contenido, mtime),
                )
        con.commit()
    finally:
        con.close()


def verificar_credenciales(email: str, clave: str) -> bool:
    email = email.strip().lower()
    if email == SUPERADMIN:
        esperada = os.environ.get("SUPERADMIN_PASSWORD")
        return bool(esperada) and clave == esperada
    filas = consulta(
        "SELECT clave_hash, activo FROM usuarios WHERE email = %s", (email,)
    )
    fila = filas[0] if filas else None
    return bool(fila) and bool(fila["activo"]) and check_password_hash(fila["clave_hash"], clave)


def requiere_login(f):
    from functools import wraps

    @wraps(f)
    def envoltura(*a, **kw):
        if not session.get("email"):
            return redirect(url_for("login", siguiente=request.path))
        return f(*a, **kw)

    return envoltura


def requiere_superadmin(f):
    from functools import wraps

    @wraps(f)
    def envoltura(*a, **kw):
        if session.get("email") != SUPERADMIN:
            abort(403)
        return f(*a, **kw)

    return envoltura


# ---------------------------------------------------------------- scraper
_lock = threading.Lock()
_corrida = {"activa": False, "log": [], "inicio": None, "fin": None, "codigo": None}


def _correr_scraper(paginas: int | None) -> None:
    cmd = [sys.executable, str(RAIZ / "cjm_precios_ps.py"), "--solo-simular"]
    if paginas:
        cmd += ["--paginas", str(paginas)]
    _corrida["log"].append(f"$ {' '.join(cmd[1:])}")
    try:
        proc = subprocess.Popen(
            cmd, cwd=RAIZ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for linea in proc.stdout:  # type: ignore[union-attr]
            _corrida["log"].append(linea.rstrip("\n"))
        proc.wait()
        _corrida["codigo"] = proc.returncode
        try:
            sincronizar_reportes()
        except Exception as e:  # noqa: BLE001
            _corrida["log"].append(f"AVISO: no se pudieron respaldar los reportes en la base: {e}")
    except Exception as e:  # noqa: BLE001
        _corrida["log"].append(f"ERROR al lanzar el scraper: {e}")
        _corrida["codigo"] = -1
    finally:
        _corrida["fin"] = datetime.now().strftime("%H:%M:%S")
        _corrida["activa"] = False


def ultimos_reportes() -> list[dict]:
    filas = consulta(
        "SELECT nombre, modificado FROM reportes ORDER BY modificado DESC LIMIT 10"
    )
    return [
        {"nombre": f["nombre"], "modificado": f["modificado"].strftime("%d-%m-%Y %H:%M:%S")}
        for f in filas
    ]


def leer_csv(nombre: str) -> tuple[list[str], list[list[str]]]:
    if not re.fullmatch(r"[\w.\-]+\.csv", nombre):
        abort(404)
    filas_db = consulta("SELECT contenido FROM reportes WHERE nombre = %s", (nombre,))
    if not filas_db:
        abort(404)
    filas = list(csv.reader(io.StringIO(filas_db[0]["contenido"])))
    if not filas:
        return [], []
    return filas[0], filas[1:501]


# Filtro pedido por la clienta: en los reportes "revisar" poder ocultar
# DLC, monedas, mapas, packs y otros extras que no son juegos completos.
def _fila_es_extra(nombre_juego: str) -> bool:
    nombre = normalizar(nombre_juego)
    for palabra in PALABRAS_DESCARTE:
        # Palabra completa, no substring: 'demo' NO descarta "Demon's Souls".
        patron = rf"(?<![a-z0-9]){re.escape(normalizar(palabra))}(?![a-z0-9])"
        if re.search(patron, nombre):
            return True
    return False


def filtrar_extras(encabezado: list[str], filas: list[list[str]]) -> tuple[list[list[str]], int]:
    """Devuelve (filas sin extras, cuantas se ocultaron). Usa la columna del nombre."""
    try:
        col = encabezado.index("ps_nombre")
    except ValueError:
        try:
            col = encabezado.index("nombre")
        except ValueError:
            return filas, 0
    limpias = [f for f in filas if len(f) <= col or not _fila_es_extra(f[col])]
    return limpias, len(filas) - len(limpias)


# ---------------------------------------------------------------- rutas
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        clave = request.form.get("clave", "")
        if not email or not clave:
            flash("Completa el correo y la contraseña.")
        elif email == SUPERADMIN and not os.environ.get("SUPERADMIN_PASSWORD"):
            flash("Falta configurar el Secret SUPERADMIN_PASSWORD en Replit.")
        elif verificar_credenciales(email, clave):
            session.clear()
            session["email"] = email
            session["csrf"] = secrets.token_urlsafe(32)
            return redirect(_destino_seguro(request.args.get("siguiente")))
        else:
            flash("Correo o contraseña incorrectos, o usuario desactivado.")
    return render_template("login.html")


@app.route("/salir")
def salir():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@requiere_login
def inicio():
    return render_template(
        "inicio.html",
        corrida=_corrida,
        reportes=ultimos_reportes(),
        es_superadmin=session["email"] == SUPERADMIN,
    )


@app.route("/scraper/correr", methods=["POST"])
@requiere_login
def scraper_correr():
    try:
        paginas = int(request.form.get("paginas") or 0) or None
    except ValueError:
        paginas = None
    with _lock:
        if _corrida["activa"]:
            flash("Ya hay una corrida en curso.")
            return redirect(url_for("inicio"))
        _corrida.update(
            activa=True, log=[], codigo=None, fin=None,
            inicio=datetime.now().strftime("%H:%M:%S"),
        )
        threading.Thread(target=_correr_scraper, args=(paginas,), daemon=True).start()
    return redirect(url_for("inicio"))


@app.route("/scraper/estado")
@requiere_login
def scraper_estado():
    return {
        "activa": _corrida["activa"],
        "log": _corrida["log"][-400:],
        "inicio": _corrida["inicio"],
        "fin": _corrida["fin"],
        "codigo": _corrida["codigo"],
    }


@app.route("/reporte/<nombre>")
@requiere_login
def ver_reporte(nombre: str):
    encabezado, filas = leer_csv(nombre)
    filtrable = nombre.startswith("revisar_")
    mostrar_todo = request.args.get("todo") == "1"
    ocultados = 0
    if filtrable and not mostrar_todo:
        filas, ocultados = filtrar_extras(encabezado, filas)
    return render_template(
        "reporte.html",
        nombre=nombre,
        encabezado=encabezado,
        filas=filas,
        filtrable=filtrable,
        mostrar_todo=mostrar_todo,
        ocultados=ocultados,
    )


@app.route("/reporte/<nombre>/descargar")
@requiere_login
def descargar_reporte(nombre: str):
    if not re.fullmatch(r"[\w.\-]+\.csv", nombre):
        abort(404)
    filas_db = consulta("SELECT contenido FROM reportes WHERE nombre = %s", (nombre,))
    if not filas_db:
        abort(404)
    contenido = filas_db[0]["contenido"]
    if nombre.startswith("revisar_") and request.args.get("todo") != "1":
        todas = list(csv.reader(io.StringIO(contenido)))
        if todas:
            limpias, _ = filtrar_extras(todas[0], todas[1:])
            salida = io.StringIO()
            csv.writer(salida).writerows([todas[0]] + limpias)
            contenido = salida.getvalue()
    return app.response_class(
        contenido,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={nombre}"},
    )


# ---------------------------------------------------------------- usuarios (superadmin)
@app.route("/usuarios", methods=["GET", "POST"])
@requiere_login
@requiere_superadmin
def usuarios():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        clave = request.form.get("clave", "")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Correo inválido.")
        elif email == SUPERADMIN:
            flash("Ese correo es el superadmin; su contraseña vive en Secrets.")
        elif len(clave) < 8:
            flash("La contraseña debe tener al menos 8 caracteres.")
        else:
            try:
                ejecutar(
                    "INSERT INTO usuarios (email, clave_hash, activo, creado) VALUES (%s,%s,1,%s)",
                    (email, generate_password_hash(clave), datetime.now().isoformat(timespec="seconds")),
                )
                flash(f"Usuario {email} creado.")
            except psycopg2.IntegrityError:
                db().rollback()
                flash("Ese correo ya existe.")
        return redirect(url_for("usuarios"))
    lista = consulta("SELECT email, activo, creado FROM usuarios ORDER BY creado")
    return render_template("usuarios.html", lista=lista)


@app.route("/usuarios/<email>/alternar", methods=["POST"])
@requiere_login
@requiere_superadmin
def usuario_alternar(email: str):
    ejecutar("UPDATE usuarios SET activo = 1 - activo WHERE email = %s", (email.lower(),))
    return redirect(url_for("usuarios"))


@app.route("/usuarios/<email>/borrar", methods=["POST"])
@requiere_login
@requiere_superadmin
def usuario_borrar(email: str):
    ejecutar("DELETE FROM usuarios WHERE email = %s", (email.lower(),))
    flash(f"Usuario {email} eliminado.")
    return redirect(url_for("usuarios"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
