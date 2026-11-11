"""Copias HTML (la de Planificación y la del sitio) regeneradas en segundo plano al aplicar un escenario al S&OP.

Pedido del usuario (2026-11-11): "al presionar aplicar, que el servidor regenere las dos copias en segundo plano",
con la barra de la simulación diciendo "Actualizando las copias HTML…" y luego "Copias actualizadas". Sin esto, las
copias recién toman el plan nuevo con la tarea diaria de las 4 AM.

El servidor lanza `manage.py regenerar_copias` como un proceso aparte (no frena las páginas) y ese proceso deja su
estado en datos_locales/copias_html_estado.json, que la barra consulta. Las rutas de las copias son las de la tarea
diaria y van en datos_locales/copias_html.json ({"planificacion": archivo .html, "sitio": carpeta}); sin ese archivo
no se regenera nada y las copias siguen con la tarea diaria.
"""
import datetime
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from django.conf import settings
from django.core.management import call_command

CONFIG = Path(settings.BASE_DIR) / "datos_locales" / "copias_html.json"
ESTADO = Path(settings.BASE_DIR) / "datos_locales" / "copias_html_estado.json"
REGISTRO = Path(settings.BASE_DIR) / "datos_locales" / "copias_html.log"
# Un "actualizando" más viejo que esto es un proceso que se cortó sin dejar su estado.
_VENCE = datetime.timedelta(minutes=30)
_CANDADO = threading.Lock()


def _ahora():
    return datetime.datetime.now().isoformat(timespec="seconds")


def destinos():
    """{"planificacion": ruta del .html, "sitio": carpeta} o None si no está configurado."""
    try:
        datos = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return datos if datos.get("planificacion") and datos.get("sitio") else None


def _leer():
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _escribir(datos):
    ESTADO.parent.mkdir(exist_ok=True)
    tmp = Path(str(ESTADO) + ".tmp")
    tmp.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, ESTADO)


def estado():
    """Estado de la última regeneración: {"estado": "actualizando" | "listo" | "error" | "", inicio, fin, motivo,
    mensaje}. Un "actualizando" vencido pasa a error."""
    datos = _leer()
    if datos.get("estado") == "actualizando":
        try:
            inicio = datetime.datetime.fromisoformat(datos["inicio"])
        except (KeyError, ValueError):
            inicio = None
        if inicio is None or datetime.datetime.now() - inicio > _VENCE:
            datos.update(estado="error", fin=_ahora(), mensaje="La actualización se interrumpió sin terminar.")
            _escribir(datos)
    return {k: datos.get(k, "") for k in ("estado", "inicio", "fin", "motivo", "mensaje")}


def regenerar(motivo):
    """Lanza la regeneración en segundo plano (o, si ya hay una en curso, la deja para repetirse al terminar, así
    toma el último cambio). Devuelve el estado."""
    if not destinos():
        return {"estado": "sin_destino", "mensaje": "Las copias HTML se actualizan con la tarea diaria."}
    with _CANDADO:
        actual = estado()
        if actual["estado"] == "actualizando":
            datos = _leer()
            datos.update(repetir=True, motivo=motivo)
            _escribir(datos)
            return estado()
        _escribir({"estado": "actualizando", "inicio": _ahora(), "motivo": motivo, "repetir": False})
        python = Path(settings.BASE_DIR) / "venv" / "Scripts" / "python.exe"
        opciones = {}
        if os.name == "nt":  # sin ventana y fuera del grupo del servidor (reiniciarlo no corta la copia)
            opciones["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        with REGISTRO.open("w", encoding="utf-8") as registro:
            subprocess.Popen([str(python if python.exists() else sys.executable), "manage.py", "regenerar_copias"],
                             cwd=settings.BASE_DIR, stdout=registro, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, **opciones)
    return estado()


def correr(stdout=None):
    """Regenera las dos copias (lo llama `manage.py regenerar_copias`) y deja el estado; repite si mientras tanto se
    aplicó otro escenario."""
    rutas = destinos()
    if not rutas:
        _escribir({"estado": "error", "fin": _ahora(), "mensaje": "Falta datos_locales/copias_html.json."})
        return
    while True:
        datos = _leer()
        datos.update(estado="actualizando", repetir=False)
        datos.setdefault("inicio", _ahora())
        _escribir(datos)
        try:
            call_command("exportar_planificacion_html", rutas["planificacion"], stdout=stdout)
            call_command("exportar_sitio_html", rutas["sitio"], stdout=stdout)
        except Exception as error:  # el estado lo muestra la barra; el detalle queda en el registro
            datos = _leer()
            datos.update(estado="error", fin=_ahora(), mensaje=f"No se pudieron actualizar: {error}")
            _escribir(datos)
            raise
        datos = _leer()
        if datos.get("repetir"):
            datos["inicio"] = _ahora()
            _escribir(datos)
            continue
        datos.update(estado="listo", fin=_ahora(), mensaje="")
        _escribir(datos)
        return
