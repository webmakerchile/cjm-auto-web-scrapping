"""Panel web privado para CJM Digitales.

- Login con sesiones (SESSION_SECRET).
- Superadmin: webmakerchile@gmail.com, contraseña en el Secret SUPERADMIN_PASSWORD.
- El superadmin puede crear usuarios y activarlos/desactivarlos.
- Botón que corre el scraper EN SECO (--solo-simular): nunca escribe en Shopify.
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import secrets

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

RAIZ = Path(__file__).resolve().parent.parent
DB = Path(__file__).resolve().parent / "usuarios.db"
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


# ---------------------------------------------------------------- usuarios
def db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def _cerrar_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def init_db() -> None:
    con = sqlite3.connect(DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS usuarios (
               email TEXT PRIMARY KEY,
               clave_hash TEXT NOT NULL,
               activo INTEGER NOT NULL DEFAULT 1,
               creado TEXT NOT NULL
           )"""
    )
    con.commit()
    con.close()


def verificar_credenciales(email: str, clave: str) -> bool:
    email = email.strip().lower()
    if email == SUPERADMIN:
        esperada = os.environ.get("SUPERADMIN_PASSWORD")
        return bool(esperada) and clave == esperada
    fila = db().execute(
        "SELECT clave_hash, activo FROM usuarios WHERE email = ?", (email,)
    ).fetchone()
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
    except Exception as e:  # noqa: BLE001
        _corrida["log"].append(f"ERROR al lanzar el scraper: {e}")
        _corrida["codigo"] = -1
    finally:
        _corrida["fin"] = datetime.now().strftime("%H:%M:%S")
        _corrida["activa"] = False


def ultimos_reportes() -> list[dict]:
    if not REPORTES.exists():
        return []
    archivos = sorted(
        (p for p in REPORTES.glob("*.csv")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:10]
    return [
        {"nombre": p.name, "modificado": datetime.fromtimestamp(p.stat().st_mtime).strftime("%d-%m-%Y %H:%M:%S")}
        for p in archivos
    ]


def leer_csv(nombre: str) -> tuple[list[str], list[list[str]]]:
    if not re.fullmatch(r"[\w.\-]+\.csv", nombre):
        abort(404)
    ruta = (REPORTES / nombre).resolve()
    if ruta.parent != REPORTES.resolve() or not ruta.is_file() or ruta.is_symlink():
        abort(404)
    with ruta.open(newline="", encoding="utf-8") as f:
        filas = list(csv.reader(f))
    if not filas:
        return [], []
    return filas[0], filas[1:501]


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
    return render_template("reporte.html", nombre=nombre, encabezado=encabezado, filas=filas)


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
                db().execute(
                    "INSERT INTO usuarios (email, clave_hash, activo, creado) VALUES (?,?,1,?)",
                    (email, generate_password_hash(clave), datetime.now().isoformat(timespec="seconds")),
                )
                db().commit()
                flash(f"Usuario {email} creado.")
            except sqlite3.IntegrityError:
                flash("Ese correo ya existe.")
        return redirect(url_for("usuarios"))
    lista = db().execute("SELECT email, activo, creado FROM usuarios ORDER BY creado").fetchall()
    return render_template("usuarios.html", lista=lista)


@app.route("/usuarios/<email>/alternar", methods=["POST"])
@requiere_login
@requiere_superadmin
def usuario_alternar(email: str):
    db().execute("UPDATE usuarios SET activo = 1 - activo WHERE email = ?", (email.lower(),))
    db().commit()
    return redirect(url_for("usuarios"))


@app.route("/usuarios/<email>/borrar", methods=["POST"])
@requiere_login
@requiere_superadmin
def usuario_borrar(email: str):
    db().execute("DELETE FROM usuarios WHERE email = ?", (email.lower(),))
    db().commit()
    flash(f"Usuario {email} eliminado.")
    return redirect(url_for("usuarios"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
