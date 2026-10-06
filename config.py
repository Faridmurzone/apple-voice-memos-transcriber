"""Configuración compartida: lee .env (si existe) y expone rutas y valores del proyecto.

El .env es local y está en .gitignore; ver .env.example. Las variables de entorno ya
definidas tienen prioridad sobre el archivo.
"""
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _cargar_env(ruta):
    if not ruta.exists():
        return
    for linea in ruta.read_text().splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


_cargar_env(BASE / ".env")

OUTPUT = Path(os.environ.get("TRANSCRIPCIONES_OUTPUT") or BASE / "output").expanduser()
PROYECTO = os.environ.get("GCP_PROYECTO", "")
SECRETO = os.environ.get("TRANSCRIPCIONES_SECRETO", "")
ORGANIZACION = os.environ.get("ORGANIZACION", "")


def proyecto():
    """Proyecto de GCP obligatorio para STT/GCS/Secret Manager."""
    if not PROYECTO:
        raise SystemExit("falta GCP_PROYECTO: copiá .env.example a .env y completalo")
    return PROYECTO
