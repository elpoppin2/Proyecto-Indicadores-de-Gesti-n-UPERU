"""Portada ejecutiva, Informe mensual y su versión por área (pestañas Resumen ejecutivo e Informe mensual de cada página). Los cálculos están en gestion/ejecutivo.py."""

import datetime

from dashboard import queries as q
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse

from .. import ejecutivo
from .comun import _construir_nav, _por_str, _slug
from .zonas import _ZONAS_PLANIFICACION, _zonas_categoria


@login_required
def inicio(request):
    return redirect("portada")


def _mes_ejecutivo(request):
    meses = ejecutivo.meses_disponibles()
    return meses, _por_str(request.GET.get("mes"), meses) or ejecutivo.mes_defecto()


@login_required
def portada(request):
    """Portada ejecutiva (ver gestion/ejecutivo.py): los indicadores clave con
    meta, semáforo y tendencia, los 3 mensajes del mes, la cartera de
    oportunidades y el resumen por categoría, todo en soles, para el mes
    elegido (por defecto, el último cerrado)."""
    categorias_nav, _ = _construir_nav("portada")
    meses, mes = _mes_ejecutivo(request)
    contexto = {"categoria": "PORTADA", "categorias_nav": categorias_nav, "meses": meses, "mes_sel": mes,
                "en_curso": mes == datetime.date.today().replace(day=1)}
    if not q.anios_compras_disponibles():
        contexto["sin_datos"] = True
    else:
        contexto["e"] = ejecutivo.contexto(mes)
    return render(request, "gestion/portada.html", contexto)


def _area_ejecutiva(slug):
    """Área del Resumen / Informe mensual de una página (pedido del usuario,
    2026-10-21: cada área con los suyos): "planificacion" = Materias Primas y
    Combustibles (S&OP); el slug de una categoría = esa categoría. None si no
    es un área (Portada / Informe general)."""
    if slug == "planificacion":
        return ejecutivo.area_planificacion(reverse("planificacion"), [c for c, _, _, _ in _ZONAS_PLANIFICACION])
    _, categorias = _construir_nav(slug)
    categoria = {_slug(c): c for c in categorias}.get(slug)
    if categoria is None:
        return None
    tiene_inventario = bool(q.fechas_inventario_disponibles()) and categoria in set(q.categorias_inventario_disponibles())
    return ejecutivo.area_categoria(categoria, reverse("categoria_detalle", kwargs={"slug": slug}),
                                    [z["clave"] for z in _zonas_categoria(categoria, tiene_inventario)])


def _html_informe(mes, area, usuario):
    return render_to_string("gestion/informe_mensual.html", {
        "e": ejecutivo.contexto(mes, area), "logo": ejecutivo.logo_data_uri(), "usuario": usuario,
    })


def _contexto_resumen_area(request, slug):
    """Pestaña Resumen de un área: el resumen ejecutivo de la Portada (meta,
    semáforo, mensajes del mes y cartera de oportunidades) solo con sus
    categorías, con su propio Mes (mes_resumen)."""
    meses = ejecutivo.meses_disponibles()
    mes = _por_str(request.GET.get("mes_resumen"), meses) or ejecutivo.mes_defecto()
    return {"ra": {"e": ejecutivo.contexto(mes, _area_ejecutiva(slug)), "meses": meses, "mes_sel": mes,
                   "en_curso": mes == datetime.date.today().replace(day=1),
                   "url_informe": f"{reverse('informe_mensual')}?area={slug}"}}


def _contexto_informe_area(request, slug):
    """Pestaña Informe mensual de un área: el mismo documento del Informe
    mensual (A4, para imprimir o guardar en PDF) solo con sus categorías,
    con su propio Mes (mes_informe)."""
    meses = ejecutivo.meses_disponibles()
    mes = _por_str(request.GET.get("mes_informe"), meses) or ejecutivo.mes_defecto()
    area = _area_ejecutiva(slug)
    return {"ia": {"html": _html_informe(mes, area, request.user.get_username() if request.user.is_authenticated else ""),
                   "meses": meses, "mes_sel": mes, "area": area["nombre"],
                   "url_informe": f"{reverse('informe_mensual')}?area={slug}&mes={mes.isoformat()}"}}


@login_required
def informe_mensual(request):
    """Informe mensual de Supply Chain: la portada del mes en formato de
    documento (A4 horizontal), autocontenido (estilos y logo dentro). Con
    ?pdf=1 se descarga en PDF, generado con Edge en el servidor (un clic);
    sin él, se ve en el navegador. ?area=<slug>: el de esa área (categoría o
    "planificacion")."""
    _, mes = _mes_ejecutivo(request)
    area = _area_ejecutiva(request.GET.get("area", ""))
    html = _html_informe(mes, area, request.user.get_username())
    if request.GET.get("pdf"):
        pdf = ejecutivo.pdf_desde_html(html)
        if pdf:
            nombre = _slug(area["nombre"]).replace("-", "_").title() if area else "Supply_Chain"
            respuesta = HttpResponse(pdf, content_type="application/pdf")
            respuesta["Content-Disposition"] = f'attachment; filename="Informe_{nombre}_{mes:%Y-%m}.pdf"'
            return respuesta
        # Sin Edge en el servidor: el mismo informe para imprimir desde el navegador.
        html = html.replace("</body>", "<script>window.print()</script></body>")
    return HttpResponse(html)
