#!/usr/bin/env python3
"""
Maneja el archivo state.json que persiste entre corridas del workflow
(se guarda mediante un commit automático desde el propio workflow).

Campos:
- started_at: timestamp ISO de la primera corrida (para calcular las 12h)
- attempts: cuántas corridas se han hecho en total
- start_email_sent: si ya se envió el correo de inicio
- last_summary_email_at: timestamp ISO del último correo de resumen enviado
- finished: si el proceso ya terminó (instancia creada)
"""

import json
import os
from datetime import datetime, timezone

# La ruta de state.json SIEMPRE se recibe explícitamente por variable de
# entorno (definida en el workflow de GitHub Actions, apuntando a la raíz
# del repo). Si no está definida (ej. corriendo localmente para pruebas),
# usamos "../state.json" como respaldo, asumiendo que este archivo vive en
# scripts/ y el repo root es un nivel arriba.
STATE_PATH = os.environ.get("STATE_FILE_PATH")
if not STATE_PATH:
    _SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
    STATE_PATH = os.path.join(_SCRIPTS_DIR, "..", "state.json")
STATE_PATH = os.path.abspath(STATE_PATH)
print(f"[state.py] Usando STATE_PATH = {STATE_PATH}")

DEFAULT_STATE = {
    "started_at": None,
    "attempts": 0,
    "start_email_sent": False,
    "last_summary_email_at": None,
    "finished": False,
}


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return dict(DEFAULT_STATE)
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Aseguramos que existan todas las claves esperadas, por si el
        # archivo es de una versión anterior del script.
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hours_since(iso_timestamp: str) -> float:
    if not iso_timestamp:
        return 0.0
    started = datetime.fromisoformat(iso_timestamp)
    return (datetime.now(timezone.utc) - started).total_seconds() / 3600
