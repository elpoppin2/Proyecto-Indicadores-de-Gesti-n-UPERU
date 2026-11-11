"""Escenarios de la simulación de compras y traslados del Movimiento de
inventario (gestion/escenarios_sop.py, js/simulacion_inventario.js): listar,
guardar, eliminar y aplicar al archivo del S&OP. Pedido del usuario, 2026-11-11."""

import datetime
import json

from dashboard import queries as q
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .. import escenarios_sop
from .reglas import puede_editar


def _datos(request):
    try:
        datos = json.loads(request.body or b"{}")
    except ValueError:
        raise escenarios_sop.ErrorEscenario("Solicitud no válida.")
    if not isinstance(datos, dict):
        raise escenarios_sop.ErrorEscenario("Solicitud no válida.")
    return datos


def _respuesta(funcion):
    """Un ErrorEscenario vuelve como {"error": mensaje} (400) para mostrarlo tal cual."""
    def envoltura(request, *args, **kwargs):
        try:
            return funcion(request, *args, **kwargs)
        except escenarios_sop.ErrorEscenario as e:
            return JsonResponse({"error": str(e)}, status=400)
    envoltura.__name__ = funcion.__name__
    envoltura.__doc__ = funcion.__doc__
    return envoltura


@login_required
@require_GET
@ensure_csrf_cookie
def escenarios_lista(request):
    """Escenarios de un S&OP (?sop=AAAA-MM-DD) y si se pueden aplicar a su archivo."""
    try:
        sop = datetime.date.fromisoformat(request.GET.get("sop", ""))
    except ValueError:
        return JsonResponse({"error": "Falta el S&OP."}, status=400)
    motivo = escenarios_sop.motivo_no_aplicable(sop)
    if not motivo and not puede_editar(request.user):
        motivo = "Solo un administrador puede aplicar un escenario al archivo del S&OP."
    ruta = escenarios_sop.archivo_abastecimiento(sop)
    return JsonResponse({
        "escenarios": escenarios_sop.listar(sop.isoformat()),
        "usuario": request.user.username, "administrador": puede_editar(request.user),
        "puede_aplicar": not motivo, "motivo_no_aplicar": motivo,
        "archivo": ruta.rsplit("\\", 1)[-1] if ruta else "", "sop_texto": q.formato_mes_anio(sop),
    })


@login_required
@require_POST
@_respuesta
def escenarios_guardar(request):
    return JsonResponse({"escenario": escenarios_sop.guardar(_datos(request), request.user)})


@login_required
@require_POST
@_respuesta
def escenarios_eliminar(request):
    escenarios_sop.eliminar(str(_datos(request).get("id") or ""), request.user, puede_editar(request.user))
    return JsonResponse({"ok": True})


@login_required
@require_POST
@_respuesta
def escenarios_aplicar(request):
    """Reemplaza el archivo de abastecimiento del S&OP vigente con el escenario
    guardado (con respaldo en _RETIRADOS) y lo vuelve a cargar."""
    if not puede_editar(request.user):
        return JsonResponse({"error": "Solo un administrador puede aplicar un escenario al archivo del S&OP."}, status=403)
    return JsonResponse({"ok": True, **escenarios_sop.aplicar(str(_datos(request).get("id") or ""), request.user)})
