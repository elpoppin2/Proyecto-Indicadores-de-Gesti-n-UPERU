"""Descarga de la presentación «Evolución de inventarios» del S&OP (PowerPoint) desde Planificación > Inventario,
con los materiales que el usuario elige (gestion/presentacion_sop.py, js/presentacion_sop.js). Pedido del usuario,
2026-11-11."""

import io

from dashboard import queries as q
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpResponseBadRequest
from django.views.decorators.http import require_GET

from .. import presentacion_sop
from .comun import _por_str

_TIPO_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _error(mensaje):
    return HttpResponseBadRequest(mensaje, content_type="text/plain; charset=utf-8")


@login_required
@require_GET
def presentacion_inventarios(request):
    """?sop=AAAA-MM-DD&materiales=material|centro,... (sin materiales: todos los del S&OP)."""
    sop = _por_str(request.GET.get("sop"), q.snapshots_sop_disponibles())
    if sop is None:
        return _error("Elige un S&OP cargado.")
    disponibles = {o["valor"]: o for o in presentacion_sop.opciones(sop)}
    pedidos = [v for v in request.GET.get("materiales", "").split(",") if v]
    elegidos = [o for v, o in disponibles.items() if not pedidos or v in pedidos]
    if not elegidos:
        return _error("Ninguno de los materiales elegidos está en ese S&OP.")
    try:
        contenido, nombre = presentacion_sop.armar(sop, elegidos)
    except presentacion_sop.SinDatos as error:
        return _error(str(error))
    return FileResponse(io.BytesIO(contenido), as_attachment=True, filename=nombre, content_type=_TIPO_PPTX)
