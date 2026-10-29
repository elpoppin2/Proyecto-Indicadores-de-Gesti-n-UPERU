"""Página de cada categoría (con sus pestañas), la descarga a Excel y la rotación por material."""

import io

import pandas as pd
from dashboard import queries as q
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

from .categoria_indicadores import _cat_indicadores
from .categoria_pestanas import _cat_abastecimiento, _cat_compras, _cat_cuentas, _cat_inventario, _cat_precios_zona
from .comun import (_ROTACION_PAGINA_TAM, _centros_por_nombre, _construir_nav, _por_str, _rotacion_materiales_filtrado,
                    _segmentadores_compras, _segmentadores_inventario, _slug, alcance_categoria, filtro_solicitante,
                    html_guardado)
from .portada import _contexto_informe_area, _contexto_resumen_area
from .zonas import _zonas_categoria


# Pestaña -> (plantilla, función de contexto).
_CONTENIDO_ZONA_CATEGORIA = {
    "indicadores": ("gestion/categoria/_indicadores.html", _cat_indicadores),
    "compras": ("gestion/categoria/_compras.html", _cat_compras),
    "inventario": ("gestion/categoria/_inventario.html", _cat_inventario),
    "abastecimiento": ("gestion/categoria/_abastecimiento.html", _cat_abastecimiento),
    "precios": ("gestion/categoria/_precios.html", _cat_precios_zona),
    "cuentas": ("gestion/categoria/_cuentas.html", _cat_cuentas),
    "resumen": ("gestion/_resumen_area.html", lambda r, c, t: _contexto_resumen_area(r, _slug(c))),
    "informe": ("gestion/_informe_area.html", lambda r, c, t: _contexto_informe_area(r, _slug(c))),
}


def _html_zona_categoria(request, categoria, slug, zona, tiene_inventario):
    """HTML de una pestaña, guardado en la caché por sus filtros. La de
    Indicadores devuelve también los críticos en riesgo (aviso de la cabecera)."""
    plantilla, funcion = _CONTENIDO_ZONA_CATEGORIA[zona]

    def dibujar():
        contexto = funcion(request, categoria, tiene_inventario)
        html = render_to_string(plantilla, {**contexto, "categoria": categoria, "slug": slug}, request=request)
        return (html, contexto["criticos_en_riesgo"]) if zona == "indicadores" else html

    return html_guardado(request, "categoria", categoria, zona, tiene_inventario, dibujar=dibujar)


@login_required
def categoria_detalle(request, slug):
    categorias_nav, categorias = _construir_nav(slug)
    mapa_slug = {_slug(c): c for c in categorias}
    categoria = mapa_slug.get(slug)
    if categoria is None:
        raise Http404("Categoría no encontrada")
    with alcance_categoria(request, categoria):
        return _categoria_detalle(request, slug, categoria, categorias_nav)


def _categoria_detalle(request, slug, categoria, categorias_nav):
    fechas_inv = q.fechas_inventario_disponibles()
    sin_datos = not fechas_inv and not q.anios_compras_disponibles()
    # Categorías que alguna vez tienen inventario (Servicios Administrativos y
    # Servicios Industriales son solo de compras/servicios y nunca lo tienen):
    # sin pestaña Inventario ni Fill Rate.
    tiene_inventario = bool(fechas_inv) and categoria in set(q.categorias_inventario_disponibles())
    zonas = _zonas_categoria(categoria, tiene_inventario)

    # Una pestaña (?_zona=) o la pestaña que contiene una sección (?_seccion=,
    # al cambiar un filtro): solo ese pedazo de HTML.
    zona, seccion = request.GET.get("_zona"), request.GET.get("_seccion")
    if zona or seccion:
        if not zona:
            zona = next((z["clave"] for z in zonas if seccion in z["secciones"].split()), "")
        if sin_datos or zona not in {z["clave"] for z in zonas}:
            return HttpResponse("")
        html = _html_zona_categoria(request, categoria, slug, zona, tiene_inventario)
        return HttpResponse(html[0] if zona == "indicadores" else html)

    contexto = {"categoria": categoria, "categorias_nav": categorias_nav, "slug": slug,
                "filtro_solicitante": filtro_solicitante(request, categoria)}
    if sin_datos:
        contexto["sin_datos"] = True
        return render(request, "gestion/categoria_detalle.html", contexto)
    indicadores_html, criticos_en_riesgo = _html_zona_categoria(request, categoria, slug, "indicadores", tiene_inventario)
    contexto.update({
        "zonas": zonas,
        "indicadores_html": mark_safe(indicadores_html),
        "total_criticos_en_riesgo": criticos_en_riesgo,
    })
    return render(request, "gestion/categoria_detalle.html", contexto)


@login_required
def exportar_excel(request, slug=None):
    """Exporta a un .xlsx (una hoja por indicador) los mismos datos que se ven en
    el OVERVIEW (slug=None) o en la página de una categoría, con los mismos
    filtros de la URL actual (año, mes de cierre, etc.)."""
    categorias_nav, categorias = _construir_nav(slug or "overview")
    categoria = None
    if slug:
        mapa_slug = {_slug(c): c for c in categorias}
        categoria = mapa_slug.get(slug)
        if categoria is None:
            raise Http404("Categoría no encontrada")
    with alcance_categoria(request, categoria):
        return _exportar_excel(request, categoria)


def _exportar_excel(request, categoria):
    fechas_inv = q.fechas_inventario_disponibles()

    # Los mismos segmentadores que la página: período de compras (Año,
    # Desde, Hasta, Top) e inventario (Corte, Familia, Centro).
    sc = _segmentadores_compras(request)
    si = _segmentadores_inventario(request, categoria) if fechas_inv else None
    mes_cierre = mes_rotacion = si["mes"] if si else None

    buffer = io.BytesIO()
    hojas_escritas = False
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:

        def hoja(nombre, datos):
            nonlocal hojas_escritas
            df = datos if isinstance(datos, pd.DataFrame) else pd.DataFrame(datos)
            if df.empty:
                return
            _centros_por_nombre(df).to_excel(writer, sheet_name=nombre[:31], index=False)
            hojas_escritas = True

        if sc:
            rango = sc["rango"]
            fi, ff = rango["fecha_ini"], rango["fecha_fin"]
            df_compras = q.compras_por_categoria_contrato_rango(fi, ff)
            pmp_df = q.pmp_por_categoria_rango(fi, ff)
            if categoria:
                df_compras = df_compras[df_compras["categoria"] == categoria]
            hoja("Compras", df_compras.merge(pmp_df, on="categoria", how="left").assign(periodo=sc["texto"]))

            pareto_todas = q.concentracion_proveedores_por_categoria_rango(fi, ff, top=sc["top"])
            if categoria:
                pareto_todas = [r for r in pareto_todas if r["categoria"] == categoria]
            hoja("Pareto Proveedores", [
                {
                    "periodo": sc["texto"], "categoria": r["categoria"],
                    f"pct_concentracion_top{sc['top']}": r["pct_concentracion_top"],
                    "proveedor": p["proveedor"], "valor": p["valor"], "pct_del_total": p["pct"],
                }
                for r in pareto_todas for p in r["top_proveedores"]
            ])

            fill_rate_todas = q.fill_rate_por_categoria_rango(fi, ff)
            if categoria:
                fill_rate_todas = [r for r in fill_rate_todas if r["categoria"] == categoria]
            else:
                categorias_con_inv = set(q.categorias_inventario_disponibles())
                fill_rate_todas = [r for r in fill_rate_todas if r["categoria"] in categorias_con_inv]
            hoja("Fill Rate", fill_rate_todas)

            lead_time_todas = q.lead_time_por_categoria_rango(fi, ff)
            if categoria:
                lead_time_todas = [r for r in lead_time_todas if r["categoria"] == categoria]
            hoja("Lead Time", lead_time_todas)

        if si and (categoria is None or categoria in set(q.categorias_inventario_disponibles())):
            familia, centro = si["familia"], si["centro"]
            columnas_inv = [
                "material_codigo", "texto_breve", "categoria", "familia", "centro",
                "unidad_medida_base", "stock_cierre", "valor_total",
            ]
            hoja("Inventario", si["inv"][columnas_inv].sort_values("valor_total", ascending=False).assign(corte=mes_cierre))

            abc_todas = q.clasificacion_abc_por_categoria(mes_cierre, familia=familia, centro=centro)
            if categoria:
                abc_todas = [r for r in abc_todas if r["categoria"] == categoria]
            hoja("ABC", [
                {"categoria": r["categoria"], "clase": c["clase"], "materiales": c["materiales"], "valor": c["valor"]}
                for r in abc_todas for c in r["clases"]
            ])

            obsoletos_todas = q.materiales_obsoletos_por_categoria(mes_cierre, familia=familia, centro=centro)
            if categoria:
                obsoletos_todas = [r for r in obsoletos_todas if r["categoria"] == categoria]
            hoja("Sin Movimiento", obsoletos_todas)

            criticos_todas = q.cobertura_materiales_criticos_por_categoria(mes_rotacion, familia=familia, centro=centro)
            if categoria:
                criticos_todas = [r for r in criticos_todas if r["categoria"] == categoria]
            hoja("Críticos", criticos_todas)

        if not hojas_escritas:
            hoja("Aviso", [{"Aviso": "No hay datos para exportar con los filtros actuales."}])

    buffer.seek(0)
    nombre_archivo = f"indicadores_{_slug(categoria) if categoria else 'overview'}.xlsx"
    respuesta = HttpResponse(
        buffer.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    respuesta["Content-Disposition"] = f'attachment; filename="{nombre_archivo}"'
    return respuesta


@login_required
def rotacion_materiales_pagina(request, slug):
    """Una página del "scroll infinito" de Rotación > Detalle por material
    (ver _ROTACION_PAGINA_TAM). Devuelve solo las filas <tr> (no una página
    completa) más 2 cabeceras propias que lee el JS: X-Hay-Mas (si queda más
    para pedir) y X-Total (cuántos materiales cumplen el filtro actual)."""
    categorias_nav, categorias = _construir_nav(slug)
    mapa_slug = {_slug(c): c for c in categorias}
    categoria = mapa_slug.get(slug)
    if categoria is None:
        raise Http404("Categoría no encontrada")

    fechas_inv = q.fechas_inventario_disponibles()
    if not fechas_inv:
        return HttpResponse("")

    mes = _por_str(request.GET.get("mes"), fechas_inv) or fechas_inv[-1]
    familia = request.GET.get("familia") or None
    if familia == "Todas":
        familia = None
    centro = request.GET.get("centro") or None
    if centro == "Todos":
        centro = None
    buscar_codigo = request.GET.get("buscar_codigo", "")
    buscar_descripcion = request.GET.get("buscar_descripcion", "")
    orden = request.GET.get("orden")
    direccion = request.GET.get("dir", "desc")
    try:
        offset = max(0, int(request.GET.get("offset", 0)))
    except ValueError:
        offset = 0

    rot_ordenado = _rotacion_materiales_filtrado(
        categoria, mes, familia=familia, centro=centro,
        buscar_codigo=buscar_codigo, buscar_descripcion=buscar_descripcion,
        orden=orden, direccion=direccion,
    )
    total = len(rot_ordenado)
    pagina = rot_ordenado.iloc[offset:offset + _ROTACION_PAGINA_TAM]

    respuesta = render(request, "gestion/_rotacion_filas.html", {"filas": pagina.to_dict("records")})
    respuesta["X-Hay-Mas"] = "true" if offset + _ROTACION_PAGINA_TAM < total else "false"
    respuesta["X-Total"] = str(total)
    return respuesta
