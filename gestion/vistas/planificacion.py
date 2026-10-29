"""Páginas Planificación y Logística de Materias Primas: registro de secciones, caché del HTML de cada sección, pestaña Indicadores y el asistente de IA."""

import copy
import datetime
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode

from dashboard import queries as q
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from .. import asistente, ejecutivo
from ..models import ConsultaAsistente
from .comun import _construir_nav, _por_str
from .plan_calidad import _contexto_calidad, _contexto_energia, _contexto_premios
from .plan_cuadrantes import _paneles_cuadrantes, periodo_valido, periodos_cuadrantes
from .plan_consumo import _contexto_consumo, _contexto_consumo_especifico, _contexto_proyeccion_sop
from .plan_cumplimiento import _contexto_cobertura, _contexto_cumplimiento_mensual, _contexto_entregas, _contexto_plan_detalle, _contexto_plan_mensual
from .plan_indicadores import _kpis_zona, _paneles_indicadores
from .plan_precios import _EVALUACIONES_PRECIOS, _contexto_evaluacion_precios, _contexto_inflacion, _contexto_precios_entrada
from .plan_transporte import _contexto_transporte
from .portada import _contexto_informe_area, _contexto_resumen_area
from .zonas import ALCANCE_LOGISTICA, EXCLUSIONES_LOGISTICA, PLAN_PROVISIONAL_LOGISTICA, PROVEEDORES_LOGISTICA, _PANELES_LOGISTICA, _PLANTILLAS_ZONA_LOGISTICA, _PLANTILLAS_ZONA_PLANIFICACION, _ZONAS_LOGISTICA, _ZONAS_PLANIFICACION


@login_required
def planificacion(request):
    """Planificación (S&OP): Plan de Abastecimiento vs Ingresos Físicos
    (báscula, ZMM056), consumo, inventario, precios/IPU y calidad/energía de
    Materias Primas y Energía/Combustibles — el resto de categorías no tiene
    proceso S&OP. Organizada en zonas (pestañas). Para que sea fluida (pedido
    por el usuario):
    - la página trae solo la zona Indicadores; cada otra zona se pide al
      abrirla (?_zona=consumo) y zonas.js las precarga en segundo plano;
    - al cambiar un filtro se pide solo esa sección (?_seccion=sec-consumo,
      ver base.html), no la página entera;
    - el contexto y el HTML de cada sección se guardan en la caché versionada
      de queries (por sus propios filtros), así que lo ya visto sale al toque.
    Cada sección tiene sus segmentadores independientes (parámetros GET con
    su sufijo propio: mes_plan, centro_consumo, material_factor...)."""
    return _pagina_zonas(request, "planificacion", "Planificación", _ZONAS_PLANIFICACION, _PANELES_OVERVIEW,
                         plantillas_zona=_PLANTILLAS_ZONA_PLANIFICACION, asistente_visible=True)


@login_required
def logistica_materias_primas(request):
    """Logística de Materias Primas: la misma página de Planificación, pero
    solo con los materiales de ALCANCE_LOGISTICA y las pestañas Indicadores,
    Cumplimiento y Fletes (esta última la definirá el usuario). Las secciones
    son las de Planificación: el alcance acota las consultas base (ver
    queries.alcance_materiales), así las dos páginas no se desfasan. Pedido
    por el usuario el 2026-10-16."""
    with q.alcance_materiales(ALCANCE_LOGISTICA, excluir=EXCLUSIONES_LOGISTICA, proveedores=PROVEEDORES_LOGISTICA,
                              plan_extra=PLAN_PROVISIONAL_LOGISTICA):
        return _pagina_zonas(request, "logistica", "Logística de Materias Primas", _ZONAS_LOGISTICA,
                             _PANELES_LOGISTICA, zonas_paneles={"cumplimiento", "transporte"}, por_material=True,
                             plantillas_zona=_PLANTILLAS_ZONA_LOGISTICA)


def _pagina_zonas(request, slug, titulo, zonas, paneles, zonas_paneles=None, por_material=False,
                  plantillas_zona=None, asistente_visible=False):
    """Página organizada en zonas (pestañas) con las secciones de
    Planificación: la página trae solo Indicadores y cada zona, sección o
    franja de números clave se pide aparte (?_zona= / ?_seccion= / ?_kpis=,
    ver zonas.js y base.html). `paneles`/`zonas_paneles`/`por_material`: qué
    gráficos dibuja Indicadores (ver _html_indicadores). `plantillas_zona`:
    zonas sin secciones de Planificación que se dibujan con su propia
    plantilla."""
    secciones_por_zona = {clave: secciones for clave, _, _, secciones in zonas}
    plantillas_zona = plantillas_zona or {}

    def indicadores():
        if slug == "planificacion":
            # La copia de SharePoint (exportar_sitio_html) lleva todos los períodos del filtro Año.
            return _html_indicadores_cuadrantes(request, estatico=request.headers.get("X-Uperu-Copia") == "1")
        return _html_indicadores(request, paneles=paneles, zonas=zonas_paneles, por_material=por_material)

    seccion, zona = request.GET.get("_seccion"), request.GET.get("_zona")
    if seccion or zona:
        if not q.meses_planificacion_disponibles():
            return HttpResponse("")
        if seccion == "sec-indicadores":  # filtro Mes de Indicadores
            html = indicadores()
        elif seccion:
            propias = {s for lista in secciones_por_zona.values() for s in lista}
            html = _html_seccion(request, seccion) if seccion in propias else ""
        elif zona == "indicadores":
            html = indicadores()
        elif zona in plantillas_zona:
            html = render_to_string(plantillas_zona[zona])
        else:
            # Números clave de la zona arriba, luego sus secciones.
            html = "\n".join([_html_kpis_zona(request, zona), *(_html_seccion(request, s) for s in secciones_por_zona.get(zona, []))])
        return HttpResponse(html)
    if request.GET.get("_kpis"):
        return HttpResponse(_html_kpis_zona(request, request.GET["_kpis"]) if q.meses_planificacion_disponibles() else "")

    categorias_nav, _ = _construir_nav(slug)
    contexto = {"categoria": titulo.upper(), "titulo_pagina": titulo, "categorias_nav": categorias_nav,
                "es_planificacion": True, "asistente_visible": asistente_visible}
    if not q.meses_planificacion_disponibles():
        contexto["sin_datos"] = True
        return render(request, "gestion/planificacion.html", contexto)
    contexto["zonas"] = [{"clave": c, "nombre": n, "descripcion": d, "secciones": " ".join(s)} for c, n, d, s in zonas]
    contexto["indicadores_html"] = mark_safe(indicadores())
    contexto["asistente_activo"] = asistente.activo()
    return render(request, "gestion/planificacion.html", contexto)


# Secciones de Planificación: id -> (plantilla, nombre de la variable en la
# plantilla, función de contexto, sufijos de sus parámetros GET). Los sufijos
# dicen de qué filtros depende cada sección (mes_plan, centro_consumo...):
# su caché se reusa aunque cambien los filtros de las demás.
_SECCIONES_PLANIFICACION = {
    "sec-plan-mensual": ("gestion/planificacion/_plan_mensual.html", "planificacion", _contexto_plan_mensual, ("plan",)),
    "sec-plan-detalle": ("gestion/planificacion/_plan_detalle.html", "planificacion_detalle", _contexto_plan_detalle, ("detalle",)),
    "sec-cumplimiento-mensual": ("gestion/planificacion/_cumplimiento_mensual.html", "planificacion_cumplimiento",
                                 _contexto_cumplimiento_mensual, ("cumplmes", "plan")),
    "sec-entregas": ("gestion/planificacion/_entregas_proveedor.html", "en", _contexto_entregas, ("entregas",)),
    "sec-consumo": ("gestion/planificacion/_consumo.html", "planificacion_consumo", lambda r: _contexto_consumo(r), ("consumo",)),
    "sec-consumo-especifico": ("gestion/planificacion/_consumo_especifico.html", "fa",
                               lambda r: _contexto_consumo_especifico(r), ("factor",)),
    "sec-cobertura": ("gestion/planificacion/_cobertura.html", "planificacion_cobertura", _contexto_cobertura, ("cobertura",)),
    "sec-proyeccion": ("gestion/planificacion/_proyeccion.html", "planificacion_proyeccion",
                       lambda r: _contexto_proyeccion_sop(r), ("proyeccion",)),
    "sec-precios": ("gestion/planificacion/_precios.html", "planificacion_precios", lambda r: _contexto_precios_entrada(r), ("precios",)),
    "sec-inflacion": ("gestion/_evaluacion_precios.html", "inf", lambda r: _contexto_inflacion(r, "inflacion"), ("inflacion",)),
    "sec-inflacion-mensual": ("gestion/_evaluacion_precios.html", "inf", lambda r: _contexto_inflacion(r, "mensual"), ("mensual",)),
    "sec-calidad": ("gestion/planificacion/_calidad.html", "planificacion_calidad", lambda r: _contexto_calidad(r), ("calidad",)),
    "sec-premios": ("gestion/planificacion/_premios.html", "planificacion_premios", lambda r: _contexto_premios(r), ("premios",)),
    "sec-energia": ("gestion/planificacion/_energia.html", "planificacion_energia", lambda r: _contexto_energia(r), ("energia",)),
    "sec-transporte": ("gestion/logistica/_transporte.html", "tr", _contexto_transporte, ("transp",)),
    # Resumen e Informe mensual del área (Materias Primas y Combustibles).
    "sec-resumen-area": ("gestion/_resumen_area.html", "ra",
                         lambda r: _contexto_resumen_area(r, "planificacion")["ra"], ("resumen",)),
    "sec-informe-area": ("gestion/_informe_area.html", "ia",
                         lambda r: _contexto_informe_area(r, "planificacion")["ia"], ("informe",)),
}

# Contextos que usan los gráficos de la pestaña Indicadores y los números
# clave de arriba de cada zona (nombre -> sección).
_SECCIONES_INDICADORES = {
    "planificacion": "sec-plan-mensual", "planificacion_cumplimiento": "sec-cumplimiento-mensual",
    "planificacion_consumo": "sec-consumo", "planificacion_factor": "sec-consumo-especifico",
    "planificacion_proyeccion": "sec-proyeccion", "planificacion_cobertura": "sec-cobertura",
    "planificacion_inflacion": "sec-inflacion", "planificacion_premios": "sec-premios",
    "planificacion_energia": "sec-energia", "logistica_transporte": "sec-transporte",
}
# Los que dibuja la pestaña Indicadores. Premios/castigos y costo de energía
# quedaron solo en su pestaña (Calidad y energía), pedido por el usuario.
_PANELES_OVERVIEW = (
    "planificacion", "planificacion_consumo", "planificacion_factor",
    "planificacion_proyeccion", "planificacion_cobertura", "planificacion_inflacion",
)
# Contextos de los números clave de cada zona (ver _kpis_zona).
_CONTEXTOS_KPIS = {
    "cumplimiento": ("planificacion", "planificacion_cumplimiento"),
    "consumo": ("planificacion_consumo", "planificacion_factor"),
    "inventario": ("planificacion_proyeccion", "planificacion_cobertura"),
    "precios": ("planificacion_inflacion",),
    "calidad": ("planificacion_premios", "planificacion_energia"),
    "transporte": ("logistica_transporte",),
}


def _huella_vistas():
    """Huella del código de las vistas, gráficos y plantillas: si cambian, lo
    guardado en caché con el código anterior deja de usarse (la de queries.py
    ya va en la versión de la caché). ejecutivo.py y desglose.py también: el
    Resumen y el Informe por área de Planificación se guardan ya dibujados."""
    base = Path(__file__).resolve().parent.parent  # gestion/
    huella = hashlib.sha1()
    for ruta in sorted([*(base / "vistas").glob("*.py"), base / "charts.py", base / "ejecutivo.py", base / "desglose.py",
                        *(base / "templates").rglob("*.html")]):
        huella.update(ruta.read_bytes())
    return huella.hexdigest()[:10]


_HUELLA_VISTAS = _huella_vistas()


def _parametros_seccion(request, sufijos):
    return tuple(sorted(
        (clave, valor) for clave, valor in request.GET.items()
        if any(clave.endswith("_" + sufijo) for sufijo in sufijos)
    ))


def _clave_planificacion(tipo, *partes):
    # Con el alcance: Logística de Materias Primas usa las mismas secciones
    # con menos materiales y no debe tomar lo guardado por Planificación.
    # Con la fecha de metas.json: si se editan las metas, el Resumen y el
    # Informe por área (guardados ya dibujados) se recalculan como la Portada.
    return ("planificacion", tipo, *partes, datetime.date.today().isoformat(), _HUELLA_VISTAS, q.clave_alcance(),
            ejecutivo._RUTA_METAS.stat().st_mtime_ns)


def _contexto_seccion(request, seccion):
    """Contexto de una sección, guardado en la caché por sus propios filtros."""
    _, _, funcion, sufijos = _SECCIONES_PLANIFICACION[seccion]
    clave = _clave_planificacion("contexto", seccion, _parametros_seccion(request, sufijos))
    return q._cache_obtener(clave, lambda: funcion(request))


def _html_seccion(request, seccion):
    """HTML de una sección (el <section> completo), guardado en la caché."""
    plantilla, variable, _, sufijos = _SECCIONES_PLANIFICACION[seccion]
    parametros = _parametros_seccion(request, sufijos)

    def dibujar():
        contexto = _contexto_seccion(request, seccion)
        if contexto is None:
            return ""
        return render_to_string(plantilla, {variable: contexto, "qs_seccion": urlencode(parametros)})

    return q._cache_obtener(_clave_planificacion("html", seccion, parametros), dibujar)


# Filtro Mes de la pestaña Indicadores: qué parámetro de mes de cada sección
# fija y en qué clave de su contexto están los meses que tiene (por defecto,
# cada sección usa su propio último mes).
_MES_INDICADORES = {
    "planificacion": ("mes_plan", "opciones_mes"),
    "planificacion_consumo": ("mes_consumo", "meses"),
    "planificacion_factor": ("mes_factor", "meses"),
    "planificacion_cobertura": ("mes_cobertura", "opciones_mes"),
    "planificacion_proyeccion": ("sop_proyeccion", "opciones_snapshot"),
    "logistica_transporte": ("mes_transp", "opciones_mes"),
}


def _meses_indicadores(c):
    """Meses que ofrece el filtro de Indicadores: los que tiene al menos un
    indicador, hasta el mes en curso (el Plan llega meses adelante, sin real)."""
    hoy_mes = datetime.date.today().replace(day=1)
    meses = set()
    for nombre, (_, clave) in _MES_INDICADORES.items():
        if c.get(nombre):
            meses.update(c[nombre].get(clave) or [])
    if c.get("planificacion_inflacion"):
        meses.update(c["planificacion_inflacion"].get("meses_fecha") or [])
    return sorted((m for m in meses if m <= hoy_mes), reverse=True)


def _html_indicadores(request, mes_forzado=None, paneles=_PANELES_OVERVIEW, zonas=None, por_material=False):
    """HTML de la pestaña Indicadores: un gráfico por indicador (usa los
    contextos de las otras secciones, con sus filtros). Con el filtro Mes
    (?mes_indicadores=2026-08-01) cada indicador pasa a ese mes; sin él, el
    último mes cerrado. `mes_forzado`: lo usa el exportador del HTML estático
    para dibujar cada mes. `paneles`: contextos que usa; `zonas`: solo los
    gráficos de esas zonas (None = todos); `por_material`: agrega el
    cumplimiento por material (Logística de Materias Primas). Pedido por el
    usuario."""
    secciones = {nombre: _SECCIONES_INDICADORES[nombre] for nombre in paneles}
    sufijos = sorted({s for sid in secciones.values() for s in _SECCIONES_PLANIFICACION[sid][3]} | {"indicadores"})
    valor_mes = request.GET.get("mes_indicadores", "") if mes_forzado is None else mes_forzado

    def dibujar():
        contextos = {nombre: _contexto_seccion(request, sid) for nombre, sid in secciones.items()}
        meses = _meses_indicadores(contextos)
        # Por defecto, el último mes cerrado: el mes en curso recién empieza
        # (p.ej. el día 1 daba 0% de cumplimiento) y se elige a propósito.
        cerrado = q._sumar_meses(datetime.date.today().replace(day=1), -1)
        mes = _por_str(valor_mes, meses) or (cerrado if cerrado in meses else None)
        if mes:
            # Las secciones con mes propio se recalculan con ese mes (y sus
            # demás filtros); si no tienen datos de ese mes quedan en None.
            parametros = request.GET.copy()
            for nombre, (param, clave) in _MES_INDICADORES.items():
                parametros[param] = str(mes)
            con_mes = copy.copy(request)
            con_mes.GET = parametros
            for nombre, (param, clave) in _MES_INDICADORES.items():
                disponible = contextos.get(nombre) and mes in (contextos[nombre].get(clave) or [])
                contextos[nombre] = _contexto_seccion(con_mes, _SECCIONES_INDICADORES[nombre]) if disponible else None
        return render_to_string("gestion/planificacion/_indicadores.html", {
            "paneles": _paneles_indicadores(request, contextos, mes, zonas=zonas, por_material=por_material),
            "meses": meses, "mes_sel": mes,
        })

    return q._cache_obtener(_clave_planificacion("indicadores", valor_mes, _parametros_seccion(request, sufijos)), dibujar)


def _html_indicadores_cuadrantes(request, estatico=False):
    """HTML de la pestaña Indicadores de Planificación: los cuatro cuadrantes
    (IPU, plazo medio de pago, valor del inventario e IRI) con la vista por
    defecto de Precios e IPU, recortados por el filtro Año
    (?anio_indicadores=2025; por defecto "2025 en adelante"). Pedidos del
    usuario (2026-10-23 y 2026-10-24). Filtro Categoría (2026-10-26): Materias
    Primas, Combustible Sólido y/o Combustible Líquido, uno o varios
    (?seg_indicadores=MP&seg_indicadores=CL; por defecto los tres).
    `estatico`: el HTML estático trae todas las combinaciones de Año y
    Categoría ya dibujadas y el filtro muestra la elegida (graficos.js)."""
    periodos = periodos_cuadrantes()
    periodo_sel = periodo_valido(request.GET.get("anio_indicadores", ""))
    codigos_sel = tuple(c for c in _SEGMENTOS_INDICADORES if c in request.GET.getlist("seg_indicadores")) \
        or tuple(_SEGMENTOS_INDICADORES)

    def paneles(periodo, codigos):
        segmentos = tuple(_SEGMENTOS_INDICADORES[c] for c in codigos)
        todos = len(segmentos) == len(_SEGMENTOS_INDICADORES)
        con_periodo = copy.copy(request)
        con_periodo.GET = request.GET.__class__(mutable=True)
        con_periodo.GET["periodo_inflacion"] = periodo  # el IPU con el mismo período
        if todos:
            inf = _contexto_seccion(con_periodo, _SECCIONES_INDICADORES["planificacion_inflacion"])
        else:
            # El IPU de solo las categorías elegidas (las del IPU de Precios e IPU).
            def calcular():
                base = q.precios_entrada_sop_real()
                return _contexto_evaluacion_precios(con_periodo, base[base["categoria"].isin(segmentos)],
                                                    _EVALUACIONES_PRECIOS["inflacion"], "inflacion", unidad="auto")
            inf = q._cache_obtener(_clave_planificacion("cuadrantes-ipu", periodo, codigos), calcular)
        return _paneles_cuadrantes(inf, periodo, None if todos else segmentos)

    combinaciones = [tuple(c for i, c in enumerate(_SEGMENTOS_INDICADORES) if n >> i & 1)
                     for n in range(1, 2 ** len(_SEGMENTOS_INDICADORES))]

    def dibujar():
        vistas = [{"clave": _clave_vista_indicadores(v, c), "paneles": paneles(v, c)}
                  for v, _ in periodos for c in combinaciones
                  if estatico or (v == periodo_sel and c == codigos_sel)]
        return render_to_string("gestion/planificacion/_indicadores_cuadrantes.html", {
            "periodos": [{"valor": v, "texto": t} for v, t in periodos], "periodo_sel": periodo_sel,
            "segmentos": [{"codigo": c, "nombre": s.capitalize(), "activo": c in codigos_sel}
                          for c, s in _SEGMENTOS_INDICADORES.items()],
            "clave_sel": _clave_vista_indicadores(periodo_sel, codigos_sel), "vistas": vistas, "estatico": estatico})

    return q._cache_obtener(_clave_planificacion("cuadrantes", "estatico" if estatico else (periodo_sel, codigos_sel)),
                            dibujar)


# Filtro Categoría de Indicadores: código del parámetro -> segmento de Planificación.
_SEGMENTOS_INDICADORES = {"MP": q.SEGMENTO_MATERIAS_PRIMAS, "CS": q.SEGMENTO_COMBUSTIBLE_SOLIDO,
                          "CL": q.SEGMENTO_COMBUSTIBLE_LIQUIDO}


def _clave_vista_indicadores(periodo, codigos):
    """Clave de una vista dibujada: la misma que arma graficos.js con los
    filtros del formulario (nombres en orden, valores en orden y unidos por coma)."""
    return f"anio_indicadores={periodo}&seg_indicadores={','.join(sorted(codigos))}"


def _html_kpis_zona(request, zona):
    """Franja de números clave de arriba de una zona (vacía si la zona no
    tiene). Se vuelve a pedir sola (?_kpis=consumo) cuando cambia un filtro
    de una sección de esa zona (zonas.js)."""
    nombres = _CONTEXTOS_KPIS.get(zona)
    if not nombres:
        return ""
    secciones = [_SECCIONES_INDICADORES[n] for n in nombres]
    sufijos = sorted({s for sid in secciones for s in _SECCIONES_PLANIFICACION[sid][3]})

    def dibujar():
        contextos = {n: _contexto_seccion(request, _SECCIONES_INDICADORES[n]) for n in nombres}
        return render_to_string("gestion/planificacion/_kpis_zona.html", {"zona": zona, "kpis": _kpis_zona(zona, contextos)})

    return q._cache_obtener(_clave_planificacion("kpis", zona, _parametros_seccion(request, sufijos)), dibujar)


# Conversación con el asistente de IA: se guarda en la sesión del usuario solo
# el texto de las últimas preguntas/respuestas (no los datos consultados), para
# que pueda repreguntar ("¿y en Condorcocha?") sin repetir todo.
_ASISTENTE_HISTORIAL = "asistente_planificacion_historial"
_ASISTENTE_TURNOS = 6


@login_required
@require_POST
def asistente_planificacion(request):
    """Pregunta al asistente de IA de Planificación (ver gestion/asistente.py).
    Recibe JSON {"pregunta": "..."} (o {"reiniciar": true} para empezar una
    conversación nueva) y devuelve la respuesta, las secciones de datos que
    consultó y si hubo error. Cada consulta queda registrada en
    ConsultaAsistente. Pedido por el usuario."""
    try:
        datos = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "Solicitud no válida."}, status=400)
    if datos.get("reiniciar"):
        request.session.pop(_ASISTENTE_HISTORIAL, None)
        return JsonResponse({"ok": True})
    pregunta = str(datos.get("pregunta") or "").strip()[:2000]
    if not pregunta:
        return JsonResponse({"error": "Escribe una pregunta."}, status=400)
    if not asistente.activo():
        return JsonResponse({
            "activo": False, "error": True,
            "respuesta": "El asistente todavía no está activado: falta configurar la clave de API del servicio de IA.",
        })

    historial = request.session.get(_ASISTENTE_HISTORIAL, [])
    resultado = asistente.responder(pregunta, historial)
    ConsultaAsistente.objects.create(
        usuario=request.user, pregunta=pregunta, respuesta=resultado["respuesta"],
        herramientas=resultado["herramientas"], modelo=resultado["modelo"],
        tokens_entrada=resultado["tokens_entrada"], tokens_salida=resultado["tokens_salida"],
        duracion_segundos=resultado["duracion_segundos"], error=resultado["error"],
    )
    if not resultado["error"]:
        historial = historial + [{"role": "user", "content": pregunta},
                                 {"role": "assistant", "content": resultado["respuesta"]}]
        request.session[_ASISTENTE_HISTORIAL] = historial[-2 * _ASISTENTE_TURNOS:]
    secciones = list(dict.fromkeys(h["etiqueta"] for h in resultado["herramientas"] if not h["error"]))
    return JsonResponse({
        "activo": True, "respuesta": resultado["respuesta"], "secciones": secciones,
        "error": bool(resultado["error"]), "duracion": resultado["duracion_segundos"],
    })
