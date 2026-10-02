import calendar
import copy
import datetime
import hashlib
import io
import json
import math
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

# dashboard/queries.py concentra toda la lógica de agregación (compras, inventario,
# rotación, variación) ya probada en el dashboard de Streamlit; esta vista la
# reutiliza tal cual en vez de duplicar SQL, igual que hacen las vistas de Streamlit.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import queries as q  # noqa: E402

from . import asistente, charts  # noqa: E402
from .models import ConsultaAsistente  # noqa: E402

# Formato de mes abreviado con guion ("Ene - 2025") solo para el gráfico de
# Evolución de esta página HTML; el dashboard de Streamlit usa su propio
# formato ("Ene 2025", sin guion) vía queries.formato_mes_anio_abrev.
_MESES_ABREV_GUION = [
    "", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
    "Jul", "Ago", "Sep", "Oct", "Nov", "Dic",
]


def _mes_abrev_guion(fecha):
    return f"{_MESES_ABREV_GUION[fecha.month]} - {fecha.year}"


def _por_str(valor_str, opciones):
    for opcion in opciones:
        if str(opcion) == valor_str:
            return opcion
    return None


def _slug(categoria):
    return categoria.lower().replace(" ", "-")


def _categorias_disponibles():
    """Mismo criterio que dashboard/app.py: unión de categorías con inventario o
    compras en el año más reciente disponible."""
    anios = q.anios_compras_disponibles()
    if not anios:
        return []
    return q.categorias_dashboard(anios[-1])


def _construir_nav(slug_activo):
    """Barra de navegación compartida entre OVERVIEW, Planificación y las páginas
    de categoría: OVERVIEW siempre primero, luego Planificación (S&OP, no es una
    categoría de compras/inventario), luego las categorías con datos."""
    categorias = _categorias_disponibles()
    nav = [
        {"nombre": "OVERVIEW", "slug": "overview", "activo": slug_activo == "overview"},
        {"nombre": "PLANIFICACIÓN", "slug": "planificacion", "activo": slug_activo == "planificacion"},
    ]
    nav += [{"nombre": c, "slug": s, "activo": s == slug_activo} for c, s in ((c, _slug(c)) for c in categorias)]
    return nav, categorias


def _rango_meses(request, prefijo, anio_sel):
    """Selector de rango de meses (Desde/Hasta) dentro del año elegido, con
    nombre de parámetro GET propio por tabla (mes_<prefijo>_desde/hasta) — cada
    tabla que lo usa (Compras, Pareto, Fill Rate, Lead Time) filtra de forma
    independiente, cambiar una no afecta a las demás. Devuelve también el
    mismo rango un año antes, para comparativos interanuales (YoY)."""
    opciones_mes = list(range(1, 13))
    mes_desde = _por_str(request.GET.get(f"mes_{prefijo}_desde"), [str(m) for m in opciones_mes])
    mes_desde = int(mes_desde) if mes_desde else 1
    mes_hasta = _por_str(request.GET.get(f"mes_{prefijo}_hasta"), [str(m) for m in opciones_mes])
    mes_hasta = int(mes_hasta) if mes_hasta else 12
    if mes_hasta < mes_desde:
        mes_hasta = mes_desde

    fecha_ini = datetime.date(anio_sel, mes_desde, 1)
    fecha_fin = datetime.date(anio_sel, mes_hasta, calendar.monthrange(anio_sel, mes_hasta)[1])
    return {
        "mes_desde": mes_desde, "mes_hasta": mes_hasta,
        "opciones_mes": [{"numero": m, "nombre": q.nombre_mes(m)} for m in opciones_mes],
        "fecha_ini": fecha_ini, "fecha_fin": fecha_fin,
        "fecha_ini_anterior": fecha_ini.replace(year=anio_sel - 1),
        "fecha_fin_anterior": fecha_fin.replace(year=anio_sel - 1),
    }


def _selector_mes(request, prefijo, fechas_disponibles):
    """Selector de Mes de una sola fecha (indicadores 'foto a un mes': ABC,
    Obsoletos, Rotación, Críticos), con parámetro GET propio (mes_<prefijo>) —
    independiente del mes elegido en cualquier otra tabla."""
    return _por_str(request.GET.get(f"mes_{prefijo}"), fechas_disponibles) or fechas_disponibles[-1]


def _opciones_familia_centro(df):
    """Opciones de Familia/Centro presentes en `df` (debe tener esas columnas),
    con el primer valor "Todas"/"Todos" para el segmentador."""
    familias = ["Todas"] + sorted(f for f in df["familia"].dropna().unique().tolist() if f)
    centros = ["Todos"] + sorted(c for c in df["centro"].dropna().unique().tolist() if c)
    return familias, centros


def _selector_familia_centro(request, prefijo, opciones_familia, opciones_centro):
    """Selectores de Familia y Centro con parámetro GET propio por tabla
    (familia_<prefijo>/centro_<prefijo>) — cada tabla filtra estos dos ejes de
    forma independiente."""
    familia_sel = request.GET.get(f"familia_{prefijo}", "Todas")
    if familia_sel not in opciones_familia:
        familia_sel = "Todas"
    centro_sel = request.GET.get(f"centro_{prefijo}", "Todos")
    if centro_sel not in opciones_centro:
        centro_sel = "Todos"
    return familia_sel, centro_sel


def _opciones_categoria_centro(df):
    """Opciones de Categoría/Centro presentes en `df` (debe tener esas
    columnas), con el primer valor "Todas"/"Todos" para el segmentador de
    Planificación (Materias Primas / Energía y Combustibles)."""
    categorias = ["Todas"] + sorted(c for c in df["categoria"].dropna().unique().tolist() if c)
    centros = ["Todos"] + sorted(c for c in df["centro"].dropna().unique().tolist() if c)
    return categorias, centros


def _selector_categoria_centro(request, prefijo, opciones_categoria, opciones_centro):
    categoria_sel = request.GET.get(f"categoria_{prefijo}", "Todas")
    if categoria_sel not in opciones_categoria:
        categoria_sel = "Todas"
    centro_sel = request.GET.get(f"centro_{prefijo}", "Todos")
    if centro_sel not in opciones_centro:
        centro_sel = "Todos"
    return categoria_sel, centro_sel


def _opciones_material(df):
    """Opciones de Material (código — descripción) presentes en `df`, con
    "Todos" primero — segmentador en lista desplegable de Detalle por
    material/Evaluación diaria de Planificación (sin buscador de texto)."""
    filas = df[["material_codigo", "texto_breve"]].drop_duplicates().sort_values("material_codigo")
    opciones = [{"codigo": "Todos", "texto": "Todos"}]
    opciones += [
        {"codigo": fila.material_codigo, "texto": f"{fila.material_codigo} — {fila.texto_breve}".rstrip(" —")}
        for fila in filas.itertuples()
    ]
    return opciones


def _selector_material(request, prefijo, opciones_material):
    """Materiales elegidos en el filtro Material de una sección: varios,
    separados por coma en el GET (?material_consumo=07-00003138,14-00000023;
    el selector de zonas.js también permite elegir un grupo entero, p.ej.
    Calizas, y manda sus códigos). Lista vacía = todos. Los códigos que no
    están en las opciones (p.ej. de otro centro) se descartan. Pedido por el
    usuario: antes era un solo material."""
    validos = {o["codigo"] for o in opciones_material} - {"Todos"}
    crudo = request.GET.get(f"material_{prefijo}", "")
    return [c for c in dict.fromkeys(crudo.split(",")) if c in validos]


def _por_materiales(df, materiales):
    """`df` filtrado a los materiales elegidos (todos si la lista está vacía)."""
    return df if not materiales else df[df["material_codigo"].isin(materiales)]


# Tamaño de página del "scroll infinito" de Rotación > Detalle por material:
# esa tabla puede tener decenas de miles de filas (REPUESTOS ~28,000,
# SUMINISTROS INDUSTRIALES ~9,400) y mandarlas todas de una vez generaba una
# página de >13 MB, pesada para el navegador. En vez de eso se manda esta
# cantidad inicial y se van pidiendo más páginas a medida que el usuario baja
# el scroll de la tabla (ver rotacion_materiales_pagina), como una tabla de
# Power BI. Definido por el usuario.
_ROTACION_PAGINA_TAM = 200
_ROTACION_COLUMNAS_ORDENABLES = {"stock_cierre", "consumo_promedio", "indice_rotacion", "cobertura_meses"}


def _rotacion_materiales_filtrado(categoria, mes, familia=None, centro=None, buscar_codigo="",
                                   buscar_descripcion="", orden=None, direccion="desc"):
    """Detalle de rotación por material de una categoría, filtrado y ordenado
    — insumo común de categoria_detalle (primera página) y
    rotacion_materiales_pagina (páginas siguientes, vía scroll)."""
    rot = q.rotacion_inventario_por_material(mes)
    rot = rot[rot["categoria"] == categoria]
    if familia:
        rot = rot[rot["familia"] == familia]
    if centro:
        rot = rot[rot["centro"] == centro]
    if buscar_codigo:
        rot = rot[rot["material_codigo"].str.lower().str.contains(buscar_codigo.lower(), na=False, regex=False)]
    if buscar_descripcion:
        rot = rot[rot["texto_breve"].str.lower().str.contains(buscar_descripcion.lower(), na=False, regex=False)]
    columna = orden if orden in _ROTACION_COLUMNAS_ORDENABLES else "indice_rotacion"
    return rot.sort_values(columna, ascending=(direccion == "asc"), na_position="last")


@login_required
def inicio(request):
    return redirect("overview")


@login_required
def overview(request):
    categorias_nav, categorias = _construir_nav("overview")

    fechas_inv = q.fechas_inventario_disponibles()
    anios_compras = q.anios_compras_disponibles()

    contexto = {"categoria": "OVERVIEW", "categorias_nav": categorias_nav, "es_overview": True}

    if not fechas_inv and not anios_compras:
        contexto["sin_datos"] = True
        return render(request, "gestion/categoria_detalle.html", contexto)

    # --- Compras: una fila por categoría + total, para el año/rango seleccionado ---
    anio_sel = _por_str(request.GET.get("anio_compras"), [str(a) for a in anios_compras])
    anio_sel = int(anio_sel) if anio_sel else (anios_compras[-1] if anios_compras else None)

    compras_filas = []
    if anio_sel:
        rango = _rango_meses(request, "compras", anio_sel)
        df = q.compras_por_categoria_contrato_rango(rango["fecha_ini"], rango["fecha_fin"])
        pmp_df = q.pmp_por_categoria_rango(rango["fecha_ini"], rango["fecha_fin"])
        # Comparativo interanual (YoY): mismos meses, un año antes.
        df_ant = q.compras_por_categoria_contrato_rango(rango["fecha_ini_anterior"], rango["fecha_fin_anterior"])

        total_valor = total_bajo_contrato = total_valor_ant = 0.0
        for cat in categorias:
            fila = df[df["categoria"] == cat]
            if fila.empty:
                continue
            valor_total = float(fila["valor_total"].iloc[0] or 0)
            valor_bajo_contrato = float(fila["valor_bajo_contrato"].iloc[0] or 0)
            total_valor += valor_total
            total_bajo_contrato += valor_bajo_contrato
            fila_pmp = pmp_df[pmp_df["categoria"] == cat]
            pmp = float(fila_pmp["pmp"].iloc[0]) if not fila_pmp.empty else None

            fila_ant = df_ant[df_ant["categoria"] == cat]
            valor_total_ant = float(fila_ant["valor_total"].iloc[0]) if not fila_ant.empty else 0.0
            total_valor_ant += valor_total_ant

            compras_filas.append({
                "categoria": cat, "valor_total": valor_total,
                "pct_contrato": round(valor_bajo_contrato / valor_total * 100, 1) if valor_total else 0,
                "pmp": round(pmp, 0) if pmp is not None else None,
                "variacion_yoy_pct": (
                    round((valor_total - valor_total_ant) / valor_total_ant * 100, 1) if valor_total_ant else None
                ),
            })
        # Total general: no es la suma/promedio simple de las filas de arriba,
        # es el mismo cálculo ponderado (calcular_pmp) pero sobre TODAS las
        # categorías juntas — antes se dejaba en None. Definido por el usuario.
        compras_filas.append({
            "categoria": "TOTAL", "valor_total": total_valor,
            "pct_contrato": round(total_bajo_contrato / total_valor * 100, 1) if total_valor else 0,
            "pmp": round(q.calcular_pmp_rango(rango["fecha_ini"], rango["fecha_fin"]), 0),
            "variacion_yoy_pct": (
                round((total_valor - total_valor_ant) / total_valor_ant * 100, 1) if total_valor_ant else None
            ),
        })
        contexto["rango_compras"] = rango
    contexto["compras"] = compras_filas
    contexto["anio_sel"] = anio_sel
    contexto["anios_compras"] = anios_compras

    categorias_con_inv = set(q.categorias_inventario_disponibles())

    # --- Concentración de Proveedores (Pareto): segmentador de Mes propio ---
    if anio_sel:
        rango_pareto = _rango_meses(request, "pareto", anio_sel)
        contexto["pareto"] = q.concentracion_proveedores_por_categoria_rango(
            rango_pareto["fecha_ini"], rango_pareto["fecha_fin"]
        )
        contexto["rango_pareto"] = rango_pareto
    else:
        contexto["pareto"] = []

    # --- Fill Rate: solo categorías con inventario (a pedido del usuario), segmentador de Mes propio ---
    if anio_sel:
        rango_fillrate = _rango_meses(request, "fillrate", anio_sel)
        fill_rate_todas = q.fill_rate_por_categoria_rango(rango_fillrate["fecha_ini"], rango_fillrate["fecha_fin"])
        contexto["fill_rate"] = [r for r in fill_rate_todas if r["categoria"] in categorias_con_inv]
        contexto["rango_fillrate"] = rango_fillrate
    else:
        contexto["fill_rate"] = []

    # --- Lead Time de proveedores: fecha de pedido -> Entrada de Mercancía real, segmentador de Mes propio ---
    if anio_sel:
        rango_leadtime = _rango_meses(request, "leadtime", anio_sel)
        contexto["lead_time"] = q.lead_time_por_categoria_rango(rango_leadtime["fecha_ini"], rango_leadtime["fecha_fin"])
        contexto["rango_leadtime"] = rango_leadtime
    else:
        contexto["lead_time"] = []

    # --- Evolución de PMP (general, todas las categorías) ---
    pmp_serie = q.pmp_mensual()
    puntos_pmp = [(_mes_abrev_guion(row["mes"]), float(row["pmp"])) for _, row in pmp_serie.iterrows()]
    contexto["pmp_evolucion"] = {
        "grafico": charts.serie_area(puntos_pmp, formato_valor=lambda v: f"{v:.0f}"),
    }

    if not fechas_inv:
        contexto["sin_inventario"] = True
        return render(request, "gestion/categoria_detalle.html", contexto)

    # --- Evolución de inventario (total, todas las categorías) ---
    anio_max = fechas_inv[-1].year
    fechas_anio_max = [f for f in fechas_inv if f.year == anio_max]
    default_inicio_evo = fechas_anio_max[0] if fechas_anio_max else fechas_inv[0]

    mes_inicio_evo = _por_str(request.GET.get("mes_inicio_evo"), fechas_inv) or default_inicio_evo
    opciones_fin_evo = [f for f in fechas_inv if f >= mes_inicio_evo]
    mes_fin_evo = _por_str(request.GET.get("mes_fin_evo"), opciones_fin_evo) or opciones_fin_evo[-1]

    serie_total = q.valor_inventario_mensual()
    serie = serie_total[
        (serie_total["fecha"] >= mes_inicio_evo) & (serie_total["fecha"] <= mes_fin_evo)
    ].sort_values("fecha")
    puntos = [(_mes_abrev_guion(row["fecha"]), float(row["valor_total"])) for _, row in serie.iterrows()]

    contexto["evolucion"] = {
        "grafico": charts.serie_area(puntos),
        "mes_inicio": mes_inicio_evo, "mes_fin": mes_fin_evo,
        "opciones_inicio": fechas_inv, "opciones_fin": opciones_fin_evo,
    }

    # --- Inventario (foto del mes: métricas, por categoría, top materiales) ---
    mes_cierre = _por_str(request.GET.get("mes_cierre"), fechas_inv) or fechas_inv[-1]
    inv = q.inventario_por_material(mes_cierre)

    familias_top, centros_top = _opciones_familia_centro(inv)
    familia_top_sel, centro_top_sel = _selector_familia_centro(request, "top", familias_top, centros_top)
    inv_top = inv
    if familia_top_sel != "Todas":
        inv_top = inv_top[inv_top["familia"] == familia_top_sel]
    if centro_top_sel != "Todos":
        inv_top = inv_top[inv_top["centro"] == centro_top_sel]

    categorias_df = (
        inv.groupby("categoria", as_index=False)["valor_total"].sum()
        .sort_values("valor_total", ascending=False)
    )
    categorias_items = charts.barras([
        {"etiqueta": r["categoria"], "valor": float(r["valor_total"])}
        for _, r in categorias_df.iterrows()
    ])

    top_materiales = inv_top.sort_values("valor_total", ascending=False).head(15).to_dict("records")

    contexto["inventario"] = {
        "mes_cierre": mes_cierre, "opciones_mes": fechas_inv,
        "materiales_con_stock": len(inv),
        "stock_total": float(inv["stock_cierre"].sum()) if not inv.empty else 0,
        "valor_total": float(inv["valor_total"].sum()) if not inv.empty else 0,
        "familias": categorias_items,
        "familias_top": familias_top, "familia_top_sel": familia_top_sel,
        "centros_top": centros_top, "centro_top_sel": centro_top_sel,
        "top_materiales": top_materiales,
    }

    # --- Clasificación ABC de Inventario (por categoría): Mes/Familia/Centro propios ---
    mes_abc = _selector_mes(request, "abc", fechas_inv)
    inv_abc_base = q.inventario_por_material(mes_abc)
    familias_abc, centros_abc = _opciones_familia_centro(inv_abc_base)
    familia_abc_sel, centro_abc_sel = _selector_familia_centro(request, "abc", familias_abc, centros_abc)
    contexto["abc"] = q.clasificacion_abc_por_categoria(
        mes_abc,
        familia=None if familia_abc_sel == "Todas" else familia_abc_sel,
        centro=None if centro_abc_sel == "Todos" else centro_abc_sel,
    )
    contexto["filtros_abc"] = {
        "mes": mes_abc, "opciones_mes": fechas_inv,
        "familias": familias_abc, "familia_sel": familia_abc_sel,
        "centros": centros_abc, "centro_sel": centro_abc_sel,
    }

    # --- Materiales obsoletos (sin consumo en 6 meses, capital inmovilizado): Mes/Familia/Centro propios ---
    mes_obsoletos = _selector_mes(request, "obsoletos", fechas_inv)
    inv_obsoletos_base = q.inventario_por_material(mes_obsoletos)
    familias_obsoletos, centros_obsoletos = _opciones_familia_centro(inv_obsoletos_base)
    familia_obsoletos_sel, centro_obsoletos_sel = _selector_familia_centro(
        request, "obsoletos", familias_obsoletos, centros_obsoletos
    )
    contexto["obsoletos"] = q.materiales_obsoletos_por_categoria(
        mes_obsoletos,
        familia=None if familia_obsoletos_sel == "Todas" else familia_obsoletos_sel,
        centro=None if centro_obsoletos_sel == "Todos" else centro_obsoletos_sel,
    )
    contexto["filtros_obsoletos"] = {
        "mes": mes_obsoletos, "opciones_mes": fechas_inv,
        "familias": familias_obsoletos, "familia_sel": familia_obsoletos_sel,
        "centros": centros_obsoletos, "centro_sel": centro_obsoletos_sel,
    }

    # --- Índice de Rotación: resumen por categoría ---
    # El índice por material (dashboard/queries.py) está en CANTIDAD, y cada
    # material puede tener una UMB distinta (kg, ton, und, m...) incluso dentro
    # de la misma categoría (p.ej. REPUESTOS mezcla ~20 unidades diferentes) —
    # promediar o sumar esas cantidades entre materiales no es válido. Por eso
    # el resumen por categoría del OVERVIEW se calcula en VALOR (S/.), la única
    # unidad común a todos los materiales, igual que el indicador clásico de
    # rotación (Costo de Ventas $ ÷ Inventario $). Las páginas de categoría y
    # Streamlit siguen en cantidad, a nivel de material, sin cambios.
    mes_rotacion = _selector_mes(request, "rotacion", fechas_inv)
    inv_valor_rot = q.inventario_por_material(mes_rotacion)
    familias_rotacion, centros_rotacion = _opciones_familia_centro(inv_valor_rot)
    familia_rotacion_sel, centro_rotacion_sel = _selector_familia_centro(
        request, "rotacion", familias_rotacion, centros_rotacion
    )
    if familia_rotacion_sel != "Todas":
        inv_valor_rot = inv_valor_rot[inv_valor_rot["familia"] == familia_rotacion_sel]
    if centro_rotacion_sel != "Todos":
        inv_valor_rot = inv_valor_rot[inv_valor_rot["centro"] == centro_rotacion_sel]
    stock_valor_cat = inv_valor_rot.groupby("categoria")["valor_total"].sum()

    consumo_val = q.consumo_valorizado_6_meses_por_material(mes_rotacion)
    catalogo = q.catalogo_materiales()
    consumo_val = consumo_val.merge(
        catalogo[["material_codigo", "categoria", "familia", "centro"]], on="material_codigo", how="left"
    )
    if familia_rotacion_sel != "Todas":
        consumo_val = consumo_val[consumo_val["familia"] == familia_rotacion_sel]
    if centro_rotacion_sel != "Todos":
        consumo_val = consumo_val[consumo_val["centro"] == centro_rotacion_sel]
    consumo_valor_cat = consumo_val.groupby("categoria")["consumo_valor_6meses"].sum()

    rotacion_filas = []
    for cat in categorias:
        valor_stock = float(stock_valor_cat.get(cat, 0) or 0)
        valor_consumo_6m = float(consumo_valor_cat.get(cat, 0) or 0)
        if valor_stock <= 0:
            continue
        consumo_prom_mensual = valor_consumo_6m / 6
        cobertura_meses = round(valor_stock / consumo_prom_mensual, 2) if consumo_prom_mensual else None
        rotacion_filas.append({
            "categoria": cat,
            "valor_stock": valor_stock,
            "consumo_valor_6meses": valor_consumo_6m,
            "indice_rotacion_anual": round(consumo_prom_mensual / valor_stock * 12, 2),
            "cobertura_meses": cobertura_meses,
            # Días de Inventario (DOH): la misma cobertura, en días en vez de
            # meses (mes calendario promedio = 365/12). Más intuitivo para
            # lectura ejecutiva que "cobertura en meses". Definido por el usuario.
            "dias_inventario": round(cobertura_meses * 365 / 12, 0) if cobertura_meses is not None else None,
        })

    contexto["rotacion"] = {
        "mes": mes_rotacion, "opciones_mes": fechas_inv,
        "familias": familias_rotacion, "familia_sel": familia_rotacion_sel,
        "centros": centros_rotacion, "centro_sel": centro_rotacion_sel,
        "filas": sorted(rotacion_filas, key=lambda f: f["indice_rotacion_anual"], reverse=True),
    }

    # --- Cobertura de Materiales Críticos (por categoría): Mes/Familia/Centro propios ---
    mes_criticos = _selector_mes(request, "criticos", fechas_inv)
    rot_criticos_base = q.rotacion_inventario_por_material(mes_criticos)
    familias_criticos, centros_criticos = _opciones_familia_centro(rot_criticos_base)
    familia_criticos_sel, centro_criticos_sel = _selector_familia_centro(
        request, "criticos", familias_criticos, centros_criticos
    )
    contexto["criticos"] = q.cobertura_materiales_criticos_por_categoria(
        mes_criticos,
        familia=None if familia_criticos_sel == "Todas" else familia_criticos_sel,
        centro=None if centro_criticos_sel == "Todos" else centro_criticos_sel,
    )
    contexto["total_criticos_en_riesgo"] = sum(r["en_riesgo"] for r in contexto["criticos"])
    contexto["filtros_criticos"] = {
        "mes": mes_criticos, "opciones_mes": fechas_inv,
        "familias": familias_criticos, "familia_sel": familia_criticos_sel,
        "centros": centros_criticos, "centro_sel": centro_criticos_sel,
    }

    # --- % Variación de Inventario: resumen por categoría ---
    mes_var_final = _por_str(request.GET.get("mes_var_final"), fechas_inv) or fechas_inv[-1]
    opciones_var_inicial = [f for f in fechas_inv if f < mes_var_final]
    mes_var_inicial = _por_str(request.GET.get("mes_var_inicial"), opciones_var_inicial) or (
        opciones_var_inicial[-1] if opciones_var_inicial else None
    )

    inv_var_base = q.inventario_por_material(mes_var_final)
    familias_var, centros_var = _opciones_familia_centro(inv_var_base)
    familia_var_sel, centro_var_sel = _selector_familia_centro(request, "var", familias_var, centros_var)

    if mes_var_inicial:
        variacion = q.variacion_inventario_por_material(
            mes_var_inicial, mes_var_final,
            familia=None if familia_var_sel == "Todas" else familia_var_sel,
            centro=None if centro_var_sel == "Todos" else centro_var_sel,
        )
        variacion_filas = []
        total_inicial = total_final = 0.0
        for cat in categorias:
            var_cat = variacion[variacion["categoria"] == cat]
            if var_cat.empty:
                continue
            v_inicial = float(var_cat["valor_mes_inicial"].sum())
            v_final = float(var_cat["valor_mes_final"].sum())
            total_inicial += v_inicial
            total_final += v_final
            variacion_filas.append({
                "categoria": cat, "valor_mes_inicial": v_inicial, "valor_mes_final": v_final,
                "variacion_valor": v_final - v_inicial,
                "variacion_pct": round((v_final - v_inicial) / v_inicial * 100, 1) if v_inicial else None,
            })
        variacion_filas.append({
            "categoria": "TOTAL", "valor_mes_inicial": total_inicial, "valor_mes_final": total_final,
            "variacion_valor": total_final - total_inicial,
            "variacion_pct": round((total_final - total_inicial) / total_inicial * 100, 1) if total_inicial else None,
        })
        contexto["variacion"] = {
            "mes_inicial": mes_var_inicial, "mes_final": mes_var_final,
            "opciones_final": fechas_inv, "opciones_inicial": opciones_var_inicial,
            "familias": familias_var, "familia_sel": familia_var_sel,
            "centros": centros_var, "centro_sel": centro_var_sel,
            "filas": variacion_filas,
        }
    else:
        contexto["variacion"] = {
            "mes_final": mes_var_final, "opciones_final": fechas_inv,
            "opciones_inicial": [], "mes_inicial": None,
            "familias": familias_var, "familia_sel": familia_var_sel,
            "centros": centros_var, "centro_sel": centro_var_sel,
        }

    return render(request, "gestion/overview.html", contexto)


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
    seccion, zona = request.GET.get("_seccion"), request.GET.get("_zona")
    if seccion or zona:
        if not q.meses_planificacion_disponibles():
            return HttpResponse("")
        if seccion == "sec-indicadores":  # filtro Mes de Indicadores
            html = _html_indicadores(request)
        elif seccion:
            html = _html_seccion(request, seccion) if seccion in _SECCIONES_PLANIFICACION else ""
        elif zona == "indicadores":
            html = _html_indicadores(request)
        else:
            # Números clave de la zona arriba, luego sus secciones.
            html = "\n".join([_html_kpis_zona(request, zona), *(_html_seccion(request, s) for s in _SECCIONES_POR_ZONA.get(zona, []))])
        return HttpResponse(html)
    if request.GET.get("_kpis"):
        return HttpResponse(_html_kpis_zona(request, request.GET["_kpis"]) if q.meses_planificacion_disponibles() else "")

    categorias_nav, _ = _construir_nav("planificacion")
    contexto = {"categoria": "PLANIFICACIÓN", "categorias_nav": categorias_nav, "es_planificacion": True}
    if not q.meses_planificacion_disponibles():
        contexto["sin_datos"] = True
        return render(request, "gestion/planificacion.html", contexto)
    contexto["zonas"] = [{"clave": c, "nombre": n, "descripcion": d, "secciones": " ".join(s)} for c, n, d, s in _ZONAS_PLANIFICACION]
    contexto["indicadores_html"] = mark_safe(_html_indicadores(request))
    contexto["asistente_activo"] = asistente.activo()
    return render(request, "gestion/planificacion.html", contexto)


def _meses_plan():
    """Meses con Plan de Abastecimiento y el mes por defecto. El Plan proyecta
    ~16 meses hacia adelante, así que el último mes de la lista casi siempre es
    futuro sin ingresos reales: por defecto va el mes actual si está en el
    rango, si no el último mes pasado con datos (el selector GET sigue
    permitiendo elegir un mes futuro a propósito)."""
    meses = q.meses_planificacion_disponibles()
    hoy_mes = datetime.date.today().replace(day=1)
    hasta_hoy = [m for m in meses if m <= hoy_mes]
    return meses, (hasta_hoy[-1] if hasta_hoy else (meses[0] if meses else None))


def _opciones_plan(request):
    """Mes de la Evaluación mensual y sus opciones de Categoría/Centro (las
    mismas que usa Cumplimiento mensual)."""
    meses, mes_defecto = _meses_plan()
    mes = _por_str(request.GET.get("mes_plan"), meses) or mes_defecto
    base = q.plan_vs_real_abastecimiento_por_material(mes)
    categorias, centros = _opciones_categoria_centro(base)
    return meses, mes, base, categorias, centros


def _contexto_plan_mensual(request):
    """Evaluación mensual: Mes/Categoría/Centro propios."""
    meses, mes_plan_sel, base_plan, categorias_plan_op, centros_plan_op = _opciones_plan(request)
    categoria_plan_sel, centro_plan_sel = _selector_categoria_centro(
        request, "plan", categorias_plan_op, centros_plan_op
    )
    resumen = q.resumen_abastecimiento_por_categoria(
        mes_plan_sel,
        categoria=None if categoria_plan_sel == "Todas" else categoria_plan_sel,
        centro=None if centro_plan_sel == "Todos" else centro_plan_sel,
    )
    totales_plan = base_plan
    if categoria_plan_sel != "Todas":
        totales_plan = totales_plan[totales_plan["categoria"] == categoria_plan_sel]
    if centro_plan_sel != "Todos":
        totales_plan = totales_plan[totales_plan["centro"] == centro_plan_sel]
    return {
        "mes": mes_plan_sel, "opciones_mes": meses,
        "categorias": categorias_plan_op, "categoria_sel": categoria_plan_sel,
        "centros": centros_plan_op, "centro_sel": centro_plan_sel,
        "resumen": resumen,
        "total_plan": float(totales_plan["cantidad_plan"].sum()),
        "total_real": float(totales_plan["cantidad_real"].sum()),
    }


def _contexto_cumplimiento_mensual(request):
    """Cumplimiento mensual: tendencia mes a mes (Categoría/Centro propios,
    mismas opciones que Evaluación mensual). Hoy solo Setiembre tiene Plan Y
    Real a la vez (los meses pasados no tienen Plan retroactivo, los futuros
    no tienen Real todavía) — se va completando solo con cada snapshot nuevo
    de Plan de Abastecimiento. Pedido por el usuario."""
    _, _, _, categorias_plan_op, centros_plan_op = _opciones_plan(request)
    categoria_cm_sel, centro_cm_sel = _selector_categoria_centro(
        request, "cumplmes", categorias_plan_op, centros_plan_op
    )
    cumplimiento_mensual = q.cumplimiento_mensual_abastecimiento(
        categoria=None if categoria_cm_sel == "Todas" else categoria_cm_sel,
        centro=None if centro_cm_sel == "Todos" else centro_cm_sel,
    )
    puntos_cumplimiento = [
        (q.formato_mes_anio_abrev(f["mes"]), f["cumplimiento_pct"]) for f in cumplimiento_mensual
    ]
    return {
        "categorias": categorias_plan_op, "categoria_sel": categoria_cm_sel,
        "centros": centros_plan_op, "centro_sel": centro_cm_sel,
        "filas": cumplimiento_mensual,
        "grafico": charts.barras_mensuales(puntos_cumplimiento, alto=240, meta=100),
    }


def _contexto_plan_detalle(request):
    """Detalle por material + Evaluación diaria: Mes/Centro/Material propios
    (todos en lista desplegable, sin buscador de texto libre). Sin filtro de
    Categoría aparte: Material ya alcanza para eso, y así el filtro relevante
    es Centro -> Material. El Material se acota al Centro elegido (no se
    ofrecen combinaciones sin datos, que antes mostraban un gráfico vacío).
    Definido por el usuario."""
    meses, mes_defecto = _meses_plan()
    mes_detalle_sel = _por_str(request.GET.get("mes_detalle"), meses) or mes_defecto
    base_detalle = q.plan_vs_real_abastecimiento_por_material(mes_detalle_sel)
    _, centros_det_op = _opciones_categoria_centro(base_detalle)
    centro_det_sel = request.GET.get("centro_detalle", "Todos")
    if centro_det_sel not in centros_det_op:
        centro_det_sel = "Todos"

    base_detalle_por_centro = (
        base_detalle if centro_det_sel == "Todos" else base_detalle[base_detalle["centro"] == centro_det_sel]
    )
    materiales_det_op = _opciones_material(base_detalle_por_centro)
    material_det_sel = _selector_material(request, "detalle", materiales_det_op)

    detalle = _por_materiales(base_detalle_por_centro, material_det_sel)
    detalle = detalle.sort_values("cantidad_plan", ascending=False)

    filtro_centro_det = None if centro_det_sel == "Todos" else centro_det_sel
    filtro_material_det = tuple(material_det_sel) or None

    # Evaluación diaria + curva S: Plan mensual (con los mismos filtros de
    # arriba) repartido entre los días calendario del mes vs Ingresos Físicos
    # reales día a día, con ambos acumulados.
    diario = q.evaluacion_diaria_abastecimiento(
        mes_detalle_sel, centro=filtro_centro_det, material=filtro_material_det
    )
    # Título "Material - Centro" (ej. "Caliza Andina - Atocongo") del
    # gráfico/tabla: sin esto, el bloque que baja el botón "Descargar /
    # Imprimir" queda sin nada que lo identifique una vez separado del resto
    # de la página. Definido por el usuario.
    if not material_det_sel:
        material_titulo = "Todos los materiales"
    elif len(material_det_sel) > 1:
        material_titulo = f"{len(material_det_sel)} materiales"
    else:
        fila_material = base_detalle[base_detalle["material_codigo"] == material_det_sel[0]]
        material_titulo = fila_material["texto_breve"].iloc[0] if not fila_material.empty else material_det_sel[0]
    if centro_det_sel == "Todos":
        centro_titulo = "Todos los centros"
    else:
        centro_titulo = q.nombre_centro(centro_det_sel)

    curva_s = None
    if diario:
        # El Objetivo (rojo) sigue hasta fin de mes; el Real (negro) se corta
        # en el último día YA CERRADO (ayer, no hoy) — el día en curso todavía
        # está recibiendo camiones. Si el mes elegido ya terminó, no se recorta
        # nada. alto=380: a la par de la tabla con scroll de al lado. Pedido
        # por el usuario.
        hoy = datetime.date.today() - datetime.timedelta(days=1)
        puntos_plan = [(str(f["dia"]), f["acumulado_plan"]) for f in diario]
        dias_reales = [f for f in diario if f["fecha"] <= hoy] or diario[:1]
        puntos_real = [(str(f["dia"]), f["acumulado_real"]) for f in dias_reales]
        curva_s = charts.curva_doble(puntos_plan, puntos_real, alto=380, formato_valor=lambda v: f"{v:,.0f} T")

    # Ingresos de cada material separados por proveedor (ZMM056), como filas
    # hijas que se despliegan debajo del material. El plan por proveedor
    # llegará con la tabla de distribución que pasará el usuario.
    filas = detalle.to_dict("records")
    por_proveedor = q.ingresos_fisicos_por_proveedor(mes_detalle_sel)
    grupos = {clave: g.sort_values("cantidad_real", ascending=False)
              for clave, g in por_proveedor.groupby(["material_codigo", "centro"])} if not por_proveedor.empty else {}
    for i, f in enumerate(filas):
        g = grupos.get((f["material_codigo"], f["centro"]))
        f["grupo"] = i
        f["carros"] = int(g["carros"].sum()) if g is not None else 0
        f["proveedores"] = [] if g is None else [{
            "proveedor": r.proveedor or "(sin proveedor)", "codigo": r.proveedor_codigo,
            "tipo": "Propio" if str(r.tipo_operacion) == "5" else "Terceros",
            "real": float(r.cantidad_real), "carros": int(r.carros),
            "participacion": r.cantidad_real / f["cantidad_real"] * 100 if f["cantidad_real"] else None,
        } for r in g.itertuples()]

    return {
        "mes": mes_detalle_sel, "opciones_mes": meses,
        "centros": centros_det_op, "centro_sel": centro_det_sel,
        "materiales": materiales_det_op, "material_sel": material_det_sel,
        "titulo_curva": f"{material_titulo} - {centro_titulo}",
        "filas": filas, "expandir": len(filas) == 1,
        "diario": diario,
        "curva_s": curva_s,
    }


def _contexto_cobertura(request):
    """Cobertura de stock: Mes/Categoría/Centro/Material propios. El Mes elige
    QUÉ foto de inventario (y su ventana de consumo de 6 meses hacia atrás) se
    usa. Categoría -> Centro -> Material en cascada, mismo criterio que
    Detalle por material. Pedido por el usuario."""
    meses_inv = q.meses_inventario_disponibles()
    meses_cob_op = sorted(meses_inv.keys())
    mes_cob_sel = _por_str(request.GET.get("mes_cobertura"), meses_cob_op) or (meses_cob_op[-1] if meses_cob_op else None)
    fecha_stock_sel = meses_inv.get(mes_cob_sel)

    base_cobertura = q.cobertura_stock_planificacion(fecha_stock=fecha_stock_sel)
    categorias_cob_op, _ = _opciones_categoria_centro(base_cobertura)
    categoria_cob_sel = request.GET.get("categoria_cobertura", "Todas")
    if categoria_cob_sel not in categorias_cob_op:
        categoria_cob_sel = "Todas"
    base_cobertura_por_categoria = (
        base_cobertura if categoria_cob_sel == "Todas"
        else base_cobertura[base_cobertura["categoria"] == categoria_cob_sel]
    )
    _, centros_cob_op = _opciones_categoria_centro(base_cobertura_por_categoria)
    centro_cob_sel = request.GET.get("centro_cobertura", "Todos")
    if centro_cob_sel not in centros_cob_op:
        centro_cob_sel = "Todos"
    base_cobertura_por_centro = (
        base_cobertura_por_categoria if centro_cob_sel == "Todos"
        else base_cobertura_por_categoria[base_cobertura_por_categoria["centro"] == centro_cob_sel]
    )
    materiales_cob_op = _opciones_material(base_cobertura_por_centro)
    material_cob_sel = _selector_material(request, "cobertura", materiales_cob_op)
    cobertura = _por_materiales(base_cobertura_por_centro, material_cob_sel)
    return {
        "mes": mes_cob_sel, "opciones_mes": meses_cob_op, "fecha_stock": fecha_stock_sel,
        "categorias": categorias_cob_op, "categoria_sel": categoria_cob_sel,
        "centros": centros_cob_op, "centro_sel": centro_cob_sel,
        "materiales": materiales_cob_op, "material_sel": material_cob_sel,
        "filas": cobertura.to_dict("records"),
        "en_riesgo": int((cobertura["cobertura_dias"] < 30).sum()),
    }


# Evolución de entregas por proveedor: proveedores con color propio (los de
# más toneladas; el resto va junto como "Otros"), meses que se muestran y
# qué se grafica.
_ENTREGAS_PROVEEDORES_COLOR = 7
_ENTREGAS_MESES = 12
_VER_ENTREGAS = [("toneladas", "Toneladas"), ("carros", "Carros"), ("participacion", "Participación (%)")]


def _contexto_entregas(request):
    """Evolución de entregas por proveedor (pestaña Cumplimiento): toneladas,
    carros o participación de cada proveedor mes a mes (ZMM056 Tipo 1/5, ver
    queries.entregas_por_proveedor_sop), con filtros Centro/Material/Ver
    propios. Gráfico de columnas apiladas por proveedor y tabla
    proveedor × material. Pedido por el usuario."""
    base = q.entregas_por_proveedor_sop()
    if base.empty:
        return None
    meses = sorted(base["mes"].unique())[-_ENTREGAS_MESES:]
    base = base[base["mes"].isin(meses)]
    centros = sorted(base["centro"].unique())
    centro_sel = request.GET.get("centro_entregas", "Todos")
    if centro_sel not in centros:
        centro_sel = "Todos"
    por_centro = base if centro_sel == "Todos" else base[base["centro"] == centro_sel]
    materiales_op = _opciones_material(por_centro)
    material_sel = _selector_material(request, "entregas", materiales_op)
    df = _por_materiales(por_centro, material_sel)
    ver = request.GET.get("ver_entregas", "toneladas")
    if ver not in dict(_VER_ENTREGAS):
        ver = "toneladas"
    contexto = {
        "centros": [{"codigo": "Todos", "texto": "Todos"}] + [
            {"codigo": c, "texto": q.nombre_centro(c)} for c in centros],
        "centro_sel": centro_sel, "materiales": materiales_op, "material_sel": material_sel,
        "opciones_ver": _VER_ENTREGAS, "ver": ver, "grafico": None, "filas": [],
    }
    if df.empty:
        return contexto

    # Un mismo proveedor puede venir con dos razones sociales (p.ej. "CALIZA
    # ANDINA EIRL" y "CALIZA ANDINA E.I.R.L."): se agrupa por el nombre sin
    # razón social ni puntuación, y se muestra el nombre con más toneladas.
    # Orden por toneladas del período (desempate por nombre): los primeros
    # tienen color propio, en el mismo orden en gráfico y tabla.
    df = df.assign(clave=df["proveedor"].map(_clave_proveedor))
    por_nombre = df.groupby(["clave", "proveedor"])["toneladas"].sum().reset_index()
    nombre_principal = (por_nombre.sort_values(["toneladas", "proveedor"], ascending=[False, True])
                        .drop_duplicates("clave").set_index("clave")["proveedor"])
    totales = df.groupby("clave").agg(tipo=("tipo", "max"), toneladas=("toneladas", "sum"))
    totales["proveedor"] = nombre_principal
    orden = sorted(totales.index, key=lambda k: (-totales.at[k, "toneladas"], totales.at[k, "proveedor"]))
    color = {k: (f"col-prov-{i}" if i < _ENTREGAS_PROVEEDORES_COLOR else "col-prov-otros") for i, k in enumerate(orden)}
    con_color = orden[:_ENTREGAS_PROVEEDORES_COLOR]
    otros = orden[_ENTREGAS_PROVEEDORES_COLOR:]

    def etiqueta(clave):
        return _nombre_corto(totales.at[clave, "proveedor"], 30) + (" (propio)" if totales.at[clave, "tipo"] == "Propio" else "")

    meses_df = sorted(df["mes"].unique())
    por_mes = df.groupby(["mes", "clave"])[["toneladas", "carros"]].sum()
    total_mes = df.groupby("mes")[["toneladas", "carros"]].sum()

    def valor(t, c, total_t):
        return t if ver == "toneladas" else c if ver == "carros" else (t / total_t * 100 if total_t else 0.0)

    pilas, textos = [], []
    for m in meses_df:
        tm, cm = float(total_mes.at[m, "toneladas"]), int(total_mes.at[m, "carros"])
        de_mes = {k: (float(r.toneladas), int(r.carros)) for k, r in por_mes.loc[m].iterrows()}
        lineas = [f"{_mes_corto(m)} · {tm:,.0f} T · {cm:,} carros"]
        for k in sorted(de_mes, key=lambda k: -de_mes[k][0])[:6]:
            t, c = de_mes[k]
            lineas.append(f"{etiqueta(k)}: {t:,.0f} T ({t / tm * 100 if tm else 0:,.1f}%) · {c:,} carros")
        if len(de_mes) > 6:
            lineas.append(f"y {len(de_mes) - 6} proveedores más")
        segmentos = [{"valor": valor(*de_mes.get(k, (0.0, 0)), tm), "clase": color[k]} for k in con_color]
        if otros:
            t_o = sum(de_mes.get(k, (0.0, 0))[0] for k in otros)
            c_o = sum(de_mes.get(k, (0.0, 0))[1] for k in otros)
            segmentos.append({"valor": valor(t_o, c_o, tm), "clase": "col-prov-otros"})
        segmentos[0]["detalle"] = "\n".join(lineas)
        pilas.append(segmentos)
        textos.append(charts._toneladas_corto(tm) if ver == "toneladas" else f"{cm:,}" if ver == "carros" else "")
    formato = (charts._toneladas_corto if ver == "toneladas" else (lambda v: f"{v:,.0f}") if ver == "carros"
               else (lambda v: f"{v:,.0f}%"))
    grafico = charts.columnas([_mes_corto(m) for m in meses_df], pilas, formato, textos=textos, ancho=960, alto=260)

    # Tabla: una fila por proveedor y material, de más a menos toneladas.
    filas = []
    total_t = float(df["toneladas"].sum())
    for (clave, material), g in df.groupby(["clave", "material_codigo"]):
        # Un mes puede traer varias filas (dos razones sociales o dos centros).
        del_mes = {m: (float(r.toneladas), int(r.carros)) for m, r in g.groupby("mes")[["toneladas", "carros"]].sum().iterrows()}
        t, c = float(g["toneladas"].sum()), int(g["carros"].sum())
        celdas = []
        for m in meses_df:
            tm_, cm_ = del_mes.get(m, (0.0, 0))
            celdas.append(_texto_entrega(ver, tm_, cm_, float(total_mes.at[m, "toneladas"])))
        filas.append({
            "proveedor": totales.at[clave, "proveedor"], "tipo": totales.at[clave, "tipo"], "clase": color[clave],
            "material_codigo": material, "texto_breve": g["texto_breve"].iloc[0],
            "centro": ", ".join(q.nombre_centro(x) for x in sorted(g["centro"].unique())),
            "celdas": celdas, "total": _texto_entrega(ver, t, c, total_t), "_t": t,
        })
    filas.sort(key=lambda f: (-f["_t"], f["proveedor"], f["material_codigo"]))
    contexto.update({
        "grafico": grafico, "filas": filas, "meses": [_mes_corto(m) for m in meses_df],
        "totales_mes": [_texto_entrega(ver, float(total_mes.at[m, "toneladas"]), int(total_mes.at[m, "carros"]),
                                       float(total_mes.at[m, "toneladas"])) for m in meses_df],
        "total": _texto_entrega(ver, total_t, int(df["carros"].sum()), total_t),
        "leyenda": [(color[k], etiqueta(k)) for k in con_color] + ([("col-prov-otros", f"Otros ({len(otros)})")] if otros else []),
        "en_curso": meses_df[-1] == datetime.date.today().replace(day=1),
        "unidad": dict(_VER_ENTREGAS)[ver],
    })
    return contexto


def _clave_proveedor(nombre):
    """Nombre de proveedor comparable: sin razón social, mayúsculas, solo
    letras y números ("CALIZA ANDINA E.I.R.L." == "CALIZA ANDINA EIRL")."""
    original, anterior = nombre, None
    while anterior != nombre:
        anterior, nombre = nombre, _RAZON_SOCIAL.sub("", nombre.strip())
    return re.sub(r"[^A-Z0-9Ñ]", "", nombre.upper()) or original.upper()


def _texto_entrega(ver, toneladas, carros, total_toneladas):
    """Celda de la tabla de Evolución de entregas según lo que se ve."""
    if ver == "toneladas":
        return f"{toneladas:,.0f}" if toneladas else "—"
    if ver == "carros":
        return f"{carros:,}" if carros else "—"
    return f"{toneladas / total_toneladas * 100:,.1f}%" if toneladas and total_toneladas else "—"


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
}

# Zonas (pestañas): clave, nombre, qué contiene, secciones en orden.
_ZONAS_PLANIFICACION = [
    ("indicadores", "Indicadores", "Resumen de todas las zonas", []),
    ("cumplimiento", "Cumplimiento", "Plan de abastecimiento vs ingresos",
     ["sec-plan-mensual", "sec-plan-detalle", "sec-cumplimiento-mensual", "sec-entregas"]),
    ("consumo", "Consumo", "Plan vs real y consumo por T", ["sec-consumo", "sec-consumo-especifico"]),
    ("inventario", "Inventario", "Cobertura y proyección S&OP", ["sec-cobertura", "sec-proyeccion"]),
    ("precios", "Precios e IPU", "Precios de entrada e inflación", ["sec-precios", "sec-inflacion", "sec-inflacion-mensual"]),
    ("calidad", "Calidad y energía", "Análisis, premios y costo por Gcal", ["sec-calidad", "sec-premios", "sec-energia"]),
]
_SECCIONES_POR_ZONA = {clave: secciones for clave, _, _, secciones in _ZONAS_PLANIFICACION}

# Contextos que usan los gráficos de la pestaña Indicadores y los números
# clave de arriba de cada zona (nombre -> sección).
_SECCIONES_INDICADORES = {
    "planificacion": "sec-plan-mensual", "planificacion_cumplimiento": "sec-cumplimiento-mensual",
    "planificacion_consumo": "sec-consumo", "planificacion_factor": "sec-consumo-especifico",
    "planificacion_proyeccion": "sec-proyeccion", "planificacion_cobertura": "sec-cobertura",
    "planificacion_inflacion": "sec-inflacion", "planificacion_premios": "sec-premios",
    "planificacion_energia": "sec-energia",
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
}


def _huella_vistas():
    """Huella del código de las vistas, gráficos y plantillas: si cambian, lo
    guardado en caché con el código anterior deja de usarse (la de queries.py
    ya va en la versión de la caché)."""
    base = Path(__file__).resolve().parent
    huella = hashlib.sha1()
    for ruta in sorted([base / "views.py", base / "charts.py", *(base / "templates").rglob("*.html")]):
        huella.update(ruta.read_bytes())
    return huella.hexdigest()[:10]


_HUELLA_VISTAS = _huella_vistas()


def _parametros_seccion(request, sufijos):
    return tuple(sorted(
        (clave, valor) for clave, valor in request.GET.items()
        if any(clave.endswith("_" + sufijo) for sufijo in sufijos)
    ))


def _clave_planificacion(tipo, *partes):
    return ("planificacion", tipo, *partes, datetime.date.today().isoformat(), _HUELLA_VISTAS)


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


def _html_indicadores(request, mes_forzado=None):
    """HTML de la pestaña Indicadores: un gráfico por indicador (usa los
    contextos de las otras secciones, con sus filtros). Con el filtro Mes
    (?mes_indicadores=2026-08-01) cada indicador pasa a ese mes; sin él, el
    último mes cerrado. `mes_forzado`: lo usa el exportador del HTML estático
    para dibujar cada mes. Pedido por el usuario."""
    secciones = {nombre: _SECCIONES_INDICADORES[nombre] for nombre in _PANELES_OVERVIEW}
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
            "paneles": _paneles_indicadores(request, contextos, mes),
            "meses": meses, "mes_sel": mes,
        })

    return q._cache_obtener(_clave_planificacion("indicadores", valor_mes, _parametros_seccion(request, sufijos)), dibujar)


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


def _pct_texto(valor, signo=False):
    """Porcentaje con un decimal para los números clave (sin "-0.0%")."""
    if valor is None:
        return "—"
    valor = 0.0 if abs(valor) < 0.05 else valor
    return f"{'+' if signo and valor > 0 else ''}{valor:,.1f}%"


def _kpis_zona(zona, c):
    """Números clave de una zona (franja de arriba de su pestaña), tomados de
    los contextos ya calculados, o sea con los filtros que tenga cada
    sección. Cada uno: valor, etiqueta y tono ("alerta" / "ok" / ""). Antes
    eran las tarjetas de la pestaña Indicadores; el usuario las pidió dentro
    de cada pestaña."""
    def kpi(valor, etiqueta, tono=""):
        return {"valor": valor, "etiqueta": etiqueta, "tono": tono}

    pct = _pct_texto
    kpis = []
    if zona == "cumplimiento":
        pl = c["planificacion"]
        cumpl = pl["total_real"] / pl["total_plan"] * 100 if pl["total_plan"] else None
        cm = [f for f in c["planificacion_cumplimiento"]["filas"] if f.get("cumplimiento_pct") is not None]
        kpis = [
            kpi(pct(cumpl), f"Cumplimiento del plan de abastecimiento · {q.formato_mes_anio(pl['mes'])}",
                "" if cumpl is None else "ok" if cumpl >= 95 else "alerta" if cumpl < 80 else ""),
            kpi(f"{pl['total_real']:,.0f} T", f"Ingresos físicos (báscula) de {pl['total_plan']:,.0f} T planificadas"),
        ]
        if len(cm) > 1:
            kpis.append(kpi(pct(cm[-2]["cumplimiento_pct"]), f"Mes anterior con plan · {q.formato_mes_anio(cm[-2]['mes'])}"))
    elif zona == "consumo":
        co, fa = c.get("planificacion_consumo"), c.get("planificacion_factor")
        if co and co["filas"]:
            valor = co["total_cumplimiento_fecha"] if co["en_curso"] else co["total_cumplimiento"]
            kpis.append(kpi(pct(valor), f"Consumo real ÷ plan S&OP · {q.formato_mes_anio(co['mes_sel'])}"))
            kpis.append(kpi(f"{co['n_sobre']} ▲ · {co['n_bajo']} ▼", f"Materiales sobre / bajo el plan (±{co['tolerancia_mes']}%)",
                            "alerta" if co["n_sobre"] + co["n_bajo"] else "ok"))
            kpis.append(kpi(str(co["n_alertas_plan"]), "Materiales cuyo plan futuro se aleja más de ±20% del consumo reciente",
                            "alerta" if co["n_alertas_plan"] else "ok"))
        if fa and fa["filas"]:
            kpis.append(kpi(f"{fa['n_alto']} ▲ · {fa['n_bajo']} ▼", f"Materiales que gastan más / menos por T producida (±{fa['tolerancia']}%)",
                            "alerta" if fa["n_alto"] else ""))
    elif zona == "inventario":
        p, cob = c.get("planificacion_proyeccion"), c["planificacion_cobertura"]
        if p and p.get("matriz"):
            kpis.append(kpi(str(p["con_quiebre"]), f"Materiales con quiebre de stock proyectado (S&OP {q.formato_mes_anio(p['snapshot'])})",
                            "alerta" if p["con_quiebre"] else "ok"))
            kpis.append(kpi(str(p["n_bajo_minimo"]), "Materiales que caen bajo su mínimo", "alerta" if p["n_bajo_minimo"] else "ok"))
            kpis.append(kpi(str(p["n_sobrecubiertos"]), "Materiales sobre su máximo (capital inmovilizado)"))
        kpis.append(kpi(str(cob["en_riesgo"]), "Materiales con menos de 30 días de cobertura hoy", "alerta" if cob["en_riesgo"] else "ok"))
    elif zona == "precios":
        inf = c.get("planificacion_inflacion")
        if inf and inf.get("resumen"):
            r = inf["resumen"]
            if r["por_anio"]:
                a = r["por_anio"][-1]
                kpis.append(kpi(f"IPU {pct(a['pct'], True)}", f"Índice de Precios Unacem {a['anio']}, acumulado a {a['hasta']} vs {a['anio_base']}",
                                "alerta" if a["pct"] > 0 else "ok"))
                kpis.append(kpi(charts.soles_corto(a["bolsa"]), f"Bolsa {a['anio']}: costo extra por precio (a cantidades {a['anio']})",
                                "alerta" if a["bolsa"] > 0 else "ok"))
            kpis.append(kpi(pct(r["ultima_pct"], True), f"Variación interanual de precios · {r['ultimo_mes']}"))
    elif zona == "calidad":
        pm, en = c.get("planificacion_premios"), c.get("planificacion_energia")
        if pm and pm.get("resumen"):
            periodo = dict((x["valor"], x["texto"]) for x in pm["periodos"])[pm["periodo_sel"]].lower()
            kpis.append(kpi(charts.soles_corto(pm["resumen"]["neto"]), f"Premios − castigos por calidad ({periodo})"))
            kpis.append(kpi(pct(pm["resumen"]["pct_sobre_base"], True), "Sobre el precio base de contrato"))
        if en and en.get("resumen"):
            kpis.append(kpi(f"S/ {en['resumen']['costo_gcal']:,.1f}", "Costo de los carbones por Gcal"))
    return kpis


# Gráficos de la pestaña Indicadores: cuántos materiales se muestran en los
# gráficos de barras (los de más peso), y los umbrales de color.
_PANEL_FILAS = 8
_CUMPLIMIENTO_BIEN, _CUMPLIMIENTO_MAL = 95, 80
_COBERTURA_RIESGO, _COBERTURA_AJUSTADA = 30, 60


def _nombre_material(texto):
    return texto.strip().title()


# Umbrales de los paneles por centro de la pestaña Indicadores (pedidos por el
# usuario el 2026-10-01): brecha del ritmo de abastecimiento en días de plan,
# precisión del plan de consumo y % de materiales en rango mín–máx.
_RITMO_AVISO, _RITMO_ALERTA = 1, 3
_PRECISION_BUENA, _PRECISION_REGULAR = 85, 70
_SALUD_BUENA, _SALUD_REGULAR = 80, 60


def _signo(valor, decimales=1):
    """+1.5 / −3.4 (con el signo menos tipográfico)."""
    valor = 0.0 if abs(valor) < 0.5 * 10 ** -decimales else valor
    return f"{'+' if valor > 0 else '−' if valor < 0 else ''}{abs(valor):,.{decimales}f}"


def _panel_traslados(panel, vacio, tope, pl, centro, hoy_mes):
    """Traslados de Atocongo a Condorcocha: lo que Condorcocha recibió por
    traslado desde Atocongo (ZMM056 Tipo 5, origen ATOCONGO) contra su plan de
    abastecimiento, de los materiales que solo le llegan así
    (queries.MATERIALES_TRASLADO_ATOCONGO_CONDORCOCHA). Mes de la Evaluación
    mensual; no usa sus filtros de categoría/centro (el indicador ya es de
    un centro a otro)."""
    titulo = "Traslados de Atocongo a Condorcocha"
    if not pl:
        vacio("cumplimiento", titulo)
        return
    mes = pl["mes"]
    mes_txt = q.formato_mes_anio(mes)
    origen = centro(q.CENTRO_ATOCONGO)
    plan = q.plan_abastecimiento_por_material(mes)
    plan = plan[plan["centro"] == q.CENTRO_CONDORCOCHA].groupby("material_codigo")["cantidad_plan"].sum()
    recibidos = q.traslados_recibidos(mes, q.CENTRO_CONDORCOCHA)
    nombres = q.catalogo_materiales().drop_duplicates("material_codigo").set_index("material_codigo")["texto_breve"]

    def nombre(codigo):
        return _nombre_material(nombres.get(codigo) or codigo)

    filas, total_plan, total_real = [], 0.0, 0.0
    for material in q.MATERIALES_TRASLADO_ATOCONGO_CONDORCOCHA:
        plan_mat = float(plan.get(material, 0.0))
        del_material = recibidos[recibidos["material_codigo"] == material]
        desde_origen = del_material[del_material["origen"] == origen]
        real, carros = float(desde_origen["toneladas"].sum()), int(desde_origen["carros"].sum())
        if plan_mat <= 0:
            continue
        total_plan += plan_mat
        total_real += real
        v = real / plan_mat * 100
        detalle = (f"{nombre(material)} · {mes_txt}\nPlan para {centro(q.CENTRO_CONDORCOCHA)} {plan_mat:,.0f} T · "
                   f"trasladado desde {origen} {real:,.0f} T ({carros:,} carros) · {v:,.1f}%")
        for r in del_material[del_material["origen"] != origen].itertuples():
            detalle += f"\nAdemás llegó directo desde {r.origen} {r.toneladas:,.0f} T"
        filas.append({
            "nombre": nombre(material), "sub": f"{carros:,} carros", "valor": v, "texto": f"{v:,.1f}%",
            "clase": "bien" if v >= _CUMPLIMIENTO_BIEN else "mal" if v < _CUMPLIMIENTO_MAL else "regular",
            "detalle": detalle,
        })
    if not filas:
        vacio("cumplimiento", titulo)
        return
    otros = recibidos[(recibidos["origen"] == origen)
                      & ~recibidos["material_codigo"].isin(q.MATERIALES_TRASLADO_ATOCONGO_CONDORCOCHA)]
    nota = "Tickets de báscula de Condorcocha con origen Atocongo (ZMM056 Tipo 5) ÷ su plan de abastecimiento."
    if not otros.empty:
        nota += " También llegó de Atocongo: " + ", ".join(
            f"{nombre(r.material_codigo)} {r.toneladas:,.0f} T" for r in otros.itertuples()) + " (su plan mezcla compras directas)."
    cumpl = total_real / total_plan * 100
    tono, estado = (("ok", "En meta") if cumpl >= _CUMPLIMIENTO_BIEN else
                    ("alerta", "Crítico") if cumpl < _CUMPLIMIENTO_MAL else ("aviso", "Bajo la meta"))
    sub = f"{mes_txt} · {total_real:,.0f} de {total_plan:,.0f} T planificadas para Condorcocha"
    if mes == hoy_mes:
        sub += " · mes en curso"
    panel("cumplimiento", titulo, _pct_texto(cumpl), sub, "escala",
          charts.barras_escala(filas, 0, tope(filas), referencias=[(100, "Plan")]), tono, estado,
          leyenda=[("bien", f"≥ {_CUMPLIMIENTO_BIEN}%"), ("regular", f"{_CUMPLIMIENTO_MAL}–{_CUMPLIMIENTO_BIEN}%"),
                   ("mal", f"< {_CUMPLIMIENTO_MAL}%")], nota=nota)


def _panel_ritmo(panel, vacio, pl, centro, hoy_mes):
    """Ritmo del abastecimiento en el mes (curva S) por centro: ingresos
    físicos acumulados día a día como % del plan del mes de cada centro,
    contra el plan lineal (el plan del mes repartido en partes iguales por
    día, igual que la Evaluación diaria). La cifra es la brecha al último día
    cerrado en días de plan: (real − plan a la fecha) ÷ plan diario. Mismo mes
    y categoría/centro que la Evaluación mensual."""
    titulo = "Ritmo del abastecimiento en el mes"
    if not pl:
        vacio("cumplimiento", titulo, ancho=True)
        return
    mes = pl["mes"]
    dias = calendar.monthrange(mes.year, mes.month)[1]
    hoy = datetime.date.today()
    ultimo = dias if mes < hoy_mes else (hoy.day - 1 if mes == hoy_mes else 0)
    categoria = None if pl["categoria_sel"] == "Todas" else pl["categoria_sel"]
    base = q.plan_vs_real_abastecimiento_por_material(
        mes, categoria=categoria, centro=None if pl["centro_sel"] == "Todos" else pl["centro_sel"])
    plan_centro = base.groupby("centro")["cantidad_plan"].sum()
    centros = [c for c in plan_centro.sort_values(ascending=False).index if plan_centro[c] > 0]
    if not centros or ultimo < 1:
        vacio("cumplimiento", titulo, ancho=True)
        return

    series, brechas = [], []
    suma_real = suma_plan = suma_diario = 0.0
    for i, cod in enumerate(centros):
        filas = q.evaluacion_diaria_abastecimiento(mes, categoria=categoria, centro=cod)
        plan_mes = filas[-1]["acumulado_plan"]
        puntos = [(0, 0.0, f"{centro(cod)} · inicio del mes")]
        for f in filas[:ultimo]:
            pct = f["acumulado_real"] / plan_mes * 100
            puntos.append((f["dia"], pct,
                           f"{centro(cod)} · {f['fecha']:%d/%m}\nReal acumulado {f['acumulado_real']:,.0f} T "
                           f"({pct:,.1f}% del plan del mes)\nPlan a la fecha {f['acumulado_plan']:,.0f} T "
                           f"({f['dia'] / dias * 100:,.1f}%)\nIngresó ese día {f['real_diario']:,.0f} T · {f['carros']:,} carros"))
        corte = filas[ultimo - 1]
        brecha = (corte["acumulado_real"] - corte["acumulado_plan"]) / corte["plan_diario"]
        brechas.append((cod, brecha))
        suma_real += corte["acumulado_real"]
        suma_plan += corte["acumulado_plan"]
        suma_diario += corte["plan_diario"]
        series.append({"nombre": centro(cod).title(), "clase": f"serie-{i}", "puntos": puntos})
    series.append({"nombre": "Plan lineal", "clase": "serie-plan", "continua": True,
                   "puntos": [(0, 0.0, "Plan lineal: 0% al empezar el mes"),
                              (dias, 100.0, f"Plan lineal: 100% el día {dias}")]})
    maximo = max(v for s in series for _, v, _ in s["puntos"])
    g = charts.lineas_indice(series, [str(d) if d else "" for d in range(dias + 1)], lambda v: f"{v:,.0f}%",
                             piso=0.0, techo=max(110.0, maximo * 1.06), ancho=1040, alto=240, pad_izq=44,
                             pad_der=64, max_etiquetas_eje=16)
    total = (suma_real - suma_plan) / suma_diario
    tono, estado = (("ok", "Adelantado" if total > _RITMO_AVISO else "Al ritmo") if total >= -_RITMO_AVISO else
                    ("aviso", "Atrasado") if total >= -_RITMO_ALERTA else ("alerta", "Muy atrasado"))
    fecha_corte = datetime.date(mes.year, mes.month, ultimo)
    sub = (f"Días de plan {'adelantados' if total > 0 else 'de atraso'} al {fecha_corte:%d/%m/%Y} · "
           + " · ".join(f"{centro(c).title()} {_signo(b)} d" for c, b in brechas))
    panel("cumplimiento", titulo, f"{_signo(total)} días", sub, "lineas", g, tono, estado,
          leyenda=[(f"linea-{i}", centro(c).title()) for i, c in enumerate(centros)] + [("linea-plan", "Plan lineal")],
          nota="Ingresos físicos acumulados como % del plan del mes de cada centro. Días de plan = (real − plan a la "
               "fecha) ÷ plan diario: cuántos días de abastecimiento faltan (−) o sobran (+).", ancho=True)


def _panel_precision_consumo(panel, vacio, co, centro):
    """Precisión del plan de consumo por centro: 100% − error ponderado
    (Σ|real − plan| ÷ Σ plan, material por material) con el plan S&OP
    vigente del mes (el de la Evaluación de consumo, con sus filtros). El
    sesgo (Σ real − Σ plan) ÷ Σ plan dice si el plan se quedó corto (+) o se
    pasó (−). Mes en curso: contra el plan prorrateado a la fecha."""
    titulo = "Precisión del plan de consumo"
    if not (co and co["filas"]):
        vacio("consumo", titulo)
        return
    if co["en_curso"]:
        # A mitad de mes el error mide sobre todo lo que falta contabilizar en
        # MB51, no la calidad del plan: se mide con el mes cerrado.
        vacio("consumo", titulo, mensaje=f"{q.formato_mes_anio(co['mes_sel'])} está en curso: la precisión del plan se "
                                         "mide con el mes cerrado. Elige un mes anterior en el filtro Mes.")
        return
    en_curso = co["en_curso"]
    mes_txt = q.formato_mes_anio(co["mes_sel"])
    por_centro = {}
    for f in co["filas"]:
        plan = f["plan_fecha"] if en_curso else f["plan"]
        a = por_centro.setdefault(f["centro"], {"plan": 0.0, "real": 0.0, "error": 0.0, "n": 0, "peor": None})
        a["plan"] += plan or 0.0
        a["real"] += f["real"] or 0.0
        a["error"] += abs((f["real"] or 0.0) - (plan or 0.0))
        a["n"] += 1
        desvio = (f["real"] or 0.0) - (plan or 0.0)
        if a["peor"] is None or abs(desvio) > abs(a["peor"][1]):
            a["peor"] = (f["texto_breve"], desvio)
    filas = []
    for cod, a in sorted(por_centro.items(), key=lambda x: -x[1]["plan"]):
        if a["plan"] <= 0:
            continue
        error = a["error"] / a["plan"] * 100
        precision = max(0.0, 100 - error)
        sesgo = (a["real"] - a["plan"]) / a["plan"] * 100
        nombre, desvio = a["peor"]
        filas.append({
            "nombre": centro(cod).title(), "sub": f"sesgo {_signo(sesgo)}%", "valor": precision, "texto": f"{precision:,.1f}%",
            "clase": "bien" if precision >= _PRECISION_BUENA else "regular" if precision >= _PRECISION_REGULAR else "mal",
            "detalle": f"{centro(cod)} · {mes_txt} · {a['n']} materiales\nPrecisión {precision:,.1f}% · error ponderado "
                       f"{error:,.1f}%\nSesgo {_signo(sesgo)}% ({'se consumió más' if sesgo > 0 else 'se consumió menos'} "
                       f"de lo planificado)\nMayor desvío: {_nombre_material(nombre)} {_signo(desvio, 0)} T",
        })
    if not filas:
        vacio("consumo", titulo)
        return
    plan_t = sum(a["plan"] for a in por_centro.values())
    error_t = sum(a["error"] for a in por_centro.values()) / plan_t * 100
    sesgo_t = (sum(a["real"] for a in por_centro.values()) - plan_t) / plan_t * 100
    precision_t = max(0.0, 100 - error_t)
    tono, estado = (("ok", "Precisa") if precision_t >= _PRECISION_BUENA else
                    ("aviso", "Mejorable") if precision_t >= _PRECISION_REGULAR else ("alerta", "Imprecisa"))
    sub = (f"{mes_txt} · error ponderado {error_t:,.1f}% · sesgo {_signo(sesgo_t)}% "
           f"({'se consumió más' if sesgo_t > 0 else 'se consumió menos'} de lo planificado)")
    if en_curso:
        sub += " · plan a la fecha"
    panel("consumo", titulo, _pct_texto(precision_t), sub, "escala",
          charts.barras_escala(filas, 0, 100, referencias=[(_PRECISION_BUENA, f"{_PRECISION_BUENA}%")]), tono, estado,
          leyenda=[("bien", f"≥ {_PRECISION_BUENA}%"), ("regular", f"{_PRECISION_REGULAR}–{_PRECISION_BUENA}%"),
                   ("mal", f"< {_PRECISION_REGULAR}%")],
          nota=f"Precisión = 100% − Σ|real − plan| ÷ Σ plan, material por material, con el plan S&OP vigente "
               f"del mes (S&OP {q.formato_mes_anio(co['snapshot'])}). La serie mes a mes se completa con cada S&OP nuevo.")


def _panel_salud_inventario(panel, vacio, cob, centro):
    """Salud del inventario por centro: cuántos materiales tienen su stock de
    cierre (MB5B) bajo el mínimo, en rango o sobre el máximo S&OP de ese mes
    (queries.minmax_sop), y la cobertura ponderada (stock total ÷ consumo
    diario total). Mismo mes y filtros que Cobertura de stock."""
    titulo = "Salud del inventario"
    if not (cob and cob["filas"] and cob["fecha_stock"]):
        vacio("inventario", titulo)
        return
    fecha = cob["fecha_stock"]
    mes = fecha.replace(day=1)
    minmax = q.minmax_sop(mes)
    limites = {} if minmax.empty else {
        (r.material_codigo, r.centro): (r.stock_minimo, r.stock_maximo)
        for r in minmax[minmax["mes"] == mes].itertuples()
    }
    estados = [("quiebre", "Quiebre"), ("bajo_minimo", "Bajo mínimo"), ("en_rango", "En rango"),
               ("sobre_maximo", "Sobre máximo"), ("sin_parametro", "Sin mín/máx")]
    por_centro = {}
    for f in cob["filas"]:
        minimo, maximo = limites.get((f["material_codigo"], f["centro"]), (float("nan"), float("nan")))
        estado = q._estado_stock(f["stock_cierre"], minimo, maximo)
        a = por_centro.setdefault(f["centro"], {"estados": {e: [] for e, _ in estados}, "stock": 0.0, "diario": 0.0})
        a["estados"][estado].append(_nombre_material(f["texto_breve"]))
        a["stock"] += f["stock_cierre"]
        a["diario"] += f["consumo_diario"] or 0.0
    filas = []
    for cod in sorted(por_centro, key=lambda c: centro(c)):
        a = por_centro[cod]
        n = sum(len(v) for v in a["estados"].values())
        dias = a["stock"] / a["diario"] if a["diario"] > 0 else None
        filas.append({
            "nombre": centro(cod).title(), "sub": f"{n} materiales", "texto": f"{dias:,.0f} d" if dias is not None else "—",
            "segmentos": [{
                "valor": len(a["estados"][e]), "clase": f"col-{e}",
                "detalle": f"{centro(cod)} · {t}: {len(a['estados'][e])} de {n} materiales\n"
                           + ", ".join(a["estados"][e][:8]) + ("…" if len(a["estados"][e]) > 8 else ""),
            } for e, t in estados],
        })
    conteo = {e: sum(len(a["estados"][e]) for a in por_centro.values()) for e, _ in estados}
    con_parametro = sum(conteo.values()) - conteo["sin_parametro"]
    stock_t, diario_t = sum(a["stock"] for a in por_centro.values()), sum(a["diario"] for a in por_centro.values())
    cobertura_t = f" · cobertura {stock_t / diario_t:,.0f} días" if diario_t > 0 else ""
    if con_parametro:
        en_rango = conteo["en_rango"] / con_parametro * 100
        cifra = _pct_texto(en_rango)
        tono, estado = (("ok", "Saludable") if en_rango >= _SALUD_BUENA else
                        ("aviso", "Revisar") if en_rango >= _SALUD_REGULAR else ("alerta", "Desbalanceado"))
        sub = f"Materiales entre su mínimo y máximo · stock al {fecha:%d/%m/%Y}{cobertura_t}"
    else:
        cifra, tono, estado = (f"{stock_t / diario_t:,.0f} d" if diario_t > 0 else "—"), "", "Sin mín/máx"
        sub = f"Cobertura ponderada · stock al {fecha:%d/%m/%Y} (sin mínimos/máximos S&OP para ese mes)"
    panel("inventario", titulo, cifra, sub, "pilas", charts.pilas_horizontales(filas), tono, estado,
          leyenda=[(f"col-{e}", t) for e, t in estados if conteo[e] or e in ("bajo_minimo", "en_rango", "sobre_maximo")],
          nota="Materiales por estado de su stock de cierre (MB5B) frente al mínimo/máximo S&OP del mes. A la derecha, "
               "cobertura ponderada: stock total ÷ consumo diario total de los últimos 6 meses.")


def _panel_primer_quiebre(panel, vacio, p, centro):
    """Meses hasta el primer quiebre por centro, según el S&OP elegido en
    Evolución de inventario (con sus filtros): meses desde el mes del S&OP
    hasta el primer mes en que un material del centro proyecta stock negativo
    (quiebre) o cae bajo su mínimo."""
    titulo = "Meses hasta el primer quiebre"
    if not (p and p.get("matriz")):
        vacio("inventario", titulo)
        return
    snapshot = p["snapshot"]
    meses = [m["fecha"] for m in p["meses"]]
    horizonte = (meses[-1].year - snapshot.year) * 12 + meses[-1].month - snapshot.month

    def meses_hasta(mes):
        return (mes.year - snapshot.year) * 12 + mes.month - snapshot.month

    primero = {}
    for f in p["matriz"]:
        if len(f["celdas"]) != len(meses):
            continue
        cod = f["centro"]
        quiebre = f["quiebre"]
        bajo = next((m for m, celda in zip(meses, f["celdas"]) if celda["estado"] in ("bajo_minimo", "quiebre")), None)
        a = primero.setdefault(cod, {"quiebre": None, "bajo_minimo": None})
        for clave, mes in (("quiebre", quiebre), ("bajo_minimo", bajo)):
            if mes and (a[clave] is None or mes < a[clave][0]):
                a[clave] = (mes, _nombre_material(f["texto_breve"]))
    if not primero:
        vacio("inventario", titulo)
        return
    filas = []
    for cod in sorted(primero, key=lambda c: centro(c)):
        for clave, texto in (("quiebre", "quiebre"), ("bajo_minimo", "bajo mínimo")):
            hallado = primero[cod][clave]
            if hallado:
                mes, material = hallado
                n = meses_hasta(mes)
                filas.append({
                    "nombre": f"{centro(cod).title()} · {texto}", "sub": material, "valor": max(n, 0.15),
                    "texto": f"{n} m", "clase": "mal" if n <= 1 else "regular" if n <= 3 else "bien",
                    "detalle": f"{centro(cod)} · primer {texto}: {material} en {_mes_corto(mes)}\n"
                               f"{n} meses desde el S&OP {_mes_corto(snapshot)}",
                })
            else:
                filas.append({
                    "nombre": f"{centro(cod).title()} · {texto}", "sub": "ninguno", "valor": horizonte, "texto": "No",
                    "clase": "neutra",
                    "detalle": f"{centro(cod)}: ningún material con {texto} hasta {_mes_corto(meses[-1])}",
                })
    quiebres = [(h[0], h[1], cod) for cod, a in primero.items() if (h := a["quiebre"])]
    if quiebres:
        mes, material, cod = min(quiebres)
        n = meses_hasta(mes)
        cifra = "Este mes" if n <= 0 else f"{n} {'mes' if n == 1 else 'meses'}"
        tono, estado = ("alerta", "Urgente") if n <= 3 else ("aviso", "Anticipar") if n <= 6 else ("ok", "Con margen")
        sub = f"Primer quiebre: {material} ({centro(cod)}) en {_mes_corto(mes)} · S&OP {_mes_corto(snapshot)}"
    else:
        cifra, tono, estado = "Sin quiebre", "ok", "Cubierto"
        sub = f"Ningún material queda en negativo hasta {_mes_corto(meses[-1])} · S&OP {_mes_corto(snapshot)}"
    panel("inventario", titulo, cifra, sub, "escala",
          charts.barras_escala(filas, 0, max(horizonte, 1), referencias=[(3, "3 meses")]), tono, estado,
          leyenda=[("mal", "≤ 1 mes"), ("regular", "2–3 meses"), ("bien", "> 3 meses"), ("neutra", "No ocurre")],
          nota="Meses desde el mes del S&OP hasta el primer mes en que un material del centro proyecta stock "
               "negativo (quiebre) o cae bajo su mínimo. 3 meses = lead time del Carbón Importado.")


def _paneles_indicadores(request, c, mes=None):
    """Pestaña Indicadores: un gráfico por indicador con su resultado (la
    cifra clave, su estado y el gráfico que la explica), con los filtros que
    tenga cada pestaña. Pedido por el usuario: la pestaña era solo tarjetas
    con números; las tarjetas pasaron a cada pestaña y acá van gráficos.
    Cada panel: zona, título, cifra, tono/estado (pastilla), sub (contexto de
    la cifra), tipo de gráfico ("escala", "columnas" o "lineas"), g (datos
    del gráfico), leyenda, nota y si ocupa el ancho completo.
    `mes`: el del filtro Mes (None = el último de cada indicador). Los
    contextos con mes propio ya vienen calculados para ese mes (None si no
    tienen datos de él); la serie mes a mes del IPU se corta en ese mes y su
    cifra es la de ese mes. Los centros se muestran por su nombre (ATOCONGO,
    CONDORCOCHA), no por código, pedido por el usuario."""
    nombres_zona = {clave: nombre for clave, nombre, _, _ in _ZONAS_PLANIFICACION}
    hoy_mes = datetime.date.today().replace(day=1)
    paneles = []
    centro = q.nombre_centro

    def panel(zona, titulo, cifra, sub, tipo, g, tono="", estado="", leyenda=(), nota="", ancho=False):
        paneles.append({
            "zona": zona, "zona_nombre": nombres_zona[zona], "titulo": titulo, "cifra": cifra, "sub": sub,
            "tipo": tipo, "g": g, "tono": tono, "estado": estado, "leyenda": list(leyenda), "nota": nota,
            "ancho": ancho, "vacio": "",
        })

    def vacio(zona, titulo, ancho=False, mensaje=None):
        """Panel sin datos (el mes elegido no tiene, o los filtros de la pestaña)."""
        panel(zona, titulo, "—", "", "", None, ancho=ancho)
        paneles[-1]["vacio"] = mensaje or (f"Sin datos de {q.formato_mes_anio(mes)}." if mes else
                                           "Sin datos para graficar con los filtros de esta pestaña.")

    def tope(filas, minimo=120, maximo=200):
        return min(max(minimo, max(f["valor"] for f in filas) * 1.08), maximo)

    # 1. Cumplimiento del plan de abastecimiento por centro (mes, categoría
    # y centro de la Evaluación mensual): una barra por planta, pedido por el
    # usuario (antes eran los materiales con más plan). Suma real ÷ suma plan
    # de las mismas filas que el total, así las barras cuadran con la cifra.
    pl = c.get("planificacion")
    titulo = "Cumplimiento del plan de abastecimiento"
    if not pl:
        vacio("cumplimiento", titulo)
    else:
        mes_txt = q.formato_mes_anio(pl["mes"])
        base = q.plan_vs_real_abastecimiento_por_material(pl["mes"])
        if pl["categoria_sel"] != "Todas":
            base = base[base["categoria"] == pl["categoria_sel"]]
        if pl["centro_sel"] != "Todos":
            base = base[base["centro"] == pl["centro_sel"]]
        por_centro = base.groupby("centro").agg(plan=("cantidad_plan", "sum"), real=("cantidad_real", "sum"),
                                                materiales=("cantidad_plan", lambda s: int((s > 0).sum())))
        filas = []
        for cod, r in por_centro[por_centro["plan"] > 0].sort_values("plan", ascending=False).iterrows():
            v, n = r.real / r.plan * 100, int(r.materiales)
            filas.append({
                "nombre": centro(cod).title(), "sub": f"{n} materiales", "valor": v, "texto": f"{v:,.1f}%",
                "clase": "bien" if v >= _CUMPLIMIENTO_BIEN else "mal" if v < _CUMPLIMIENTO_MAL else "regular",
                "detalle": f"{centro(cod)} · {mes_txt}\nPlan {r.plan:,.0f} T · Real {r.real:,.0f} T · {v:,.1f}%\n"
                           f"{n} materiales con plan",
            })
        if not filas:
            vacio("cumplimiento", titulo)
        else:
            cumpl = pl["total_real"] / pl["total_plan"] * 100 if pl["total_plan"] else None
            tono, estado = ("", "")
            if cumpl is not None:
                tono, estado = (("ok", "En meta") if cumpl >= _CUMPLIMIENTO_BIEN else
                                ("alerta", "Crítico") if cumpl < _CUMPLIMIENTO_MAL else ("aviso", "Bajo la meta"))
            sub = f"{mes_txt} · {pl['total_real']:,.0f} de {pl['total_plan']:,.0f} T planificadas"
            if pl["mes"] == hoy_mes:
                sub += " · mes en curso: el plan es del mes completo"
            panel("cumplimiento", titulo, _pct_texto(cumpl), sub, "escala",
                  charts.barras_escala(filas, 0, tope(filas), referencias=[(100, "Plan")]), tono, estado,
                  leyenda=[("bien", f"≥ {_CUMPLIMIENTO_BIEN}%"), ("regular", f"{_CUMPLIMIENTO_MAL}–{_CUMPLIMIENTO_BIEN}%"),
                           ("mal", f"< {_CUMPLIMIENTO_MAL}%")],
                  nota="Ingresos físicos (báscula) ÷ plan de abastecimiento de cada centro, todos sus materiales.")

    _panel_traslados(panel, vacio, tope, pl, centro, hoy_mes)
    _panel_ritmo(panel, vacio, pl, centro, hoy_mes)

    # 2. Consumo real vs plan S&OP del mes, por centro (igual que el 1).
    co = c.get("planificacion_consumo")
    titulo = "Consumo real vs plan S&OP"
    filas = []
    if co and co["filas"]:
        en_curso = co["en_curso"]
        tol = co["tolerancia_mes"]
        por_centro = {}
        for f in co["filas"]:
            plan = f["plan_fecha"] if en_curso else f["plan"]
            acum = por_centro.setdefault(f["centro"], {"plan": 0.0, "real": 0.0, "materiales": 0})
            acum["plan"] += plan or 0.0
            acum["real"] += f["real"] or 0.0
            acum["materiales"] += 1
        for cod, a in sorted(por_centro.items(), key=lambda x: -x[1]["plan"]):
            if a["plan"] <= 0:
                continue
            v = a["real"] / a["plan"] * 100
            filas.append({
                "nombre": centro(cod).title(), "sub": f"{a['materiales']} materiales", "valor": v, "texto": f"{v:,.1f}%",
                "clase": "bien" if abs(v - 100) <= tol else "regular" if v > 100 else "mal",
                "detalle": f"{centro(cod)} · {q.formato_mes_anio(co['mes_sel'])}\n"
                           f"Plan{' a la fecha' if en_curso else ''} {a['plan']:,.0f} T · Consumo real {a['real']:,.0f} T · {v:,.1f}%\n"
                           f"{a['materiales']} materiales con plan",
            })
    if not filas:
        vacio("consumo", titulo)
    else:
        total = co["total_cumplimiento_fecha"] if en_curso else co["total_cumplimiento"]
        tono, estado = ("", "")
        if total is not None:
            tono, estado = (("ok", "En línea") if abs(total - 100) <= tol else
                            ("aviso", "Sobre el plan") if total > 100 else ("alerta", "Bajo el plan"))
        plan_total = co["total_plan_fecha"] if en_curso else co["total_plan"]
        sub = f"{q.formato_mes_anio(co['mes_sel'])} · {co['total_real']:,.0f} de {plan_total:,.0f} T"
        if en_curso and co["fraccion_pct"] < 99.5:
            sub += f" (plan al {co['fecha_corte']:%d/%m})"
        panel("consumo", titulo, _pct_texto(total), sub, "escala",
              charts.barras_escala(filas, 0, tope(filas), referencias=[(100, "Plan")], banda=(100 - tol, 100 + tol)),
              tono, estado,
              leyenda=[("bien", f"En línea (±{tol}%)"), ("regular", "Sobre el plan"), ("mal", "Bajo el plan")],
              nota="Consumo real (MB51) ÷ plan de consumos S&OP de cada centro, todos sus materiales.")

    _panel_precision_consumo(panel, vacio, co, centro)

    # 3. Consumo por tonelada producida: últimos 3 meses vs 12 meses.
    fa = c.get("planificacion_factor")
    titulo = "Consumo por tonelada producida"
    con_var = sorted((f for f in (fa["filas"] if fa else []) if f["var_3_vs_12"] is not None), key=lambda f: -abs(f["var_3_vs_12"]))
    if not con_var:
        vacio("consumo", titulo)
    else:
        filas = [{
            "nombre": _nombre_material(f["texto_breve"]), "sub": f"{centro(f['centro'])} · {f['producto_texto'].lower()}",
            "valor": f["var_3_vs_12"], "texto": f"{f['var_3_vs_12']:+,.0f}%",
            "clase": {"alto": "regular", "bajo": "mal", "normal": "bien"}.get(f["estado"], "neutra"),
            "detalle": f"{f['texto_breve']} ({centro(f['centro'])}) ·kg por T de {f['producto_texto'].lower()}\n"
                       f"Últimos 3 meses {f['factor_3']:,.0f} kg/T · {fa['meses_factor']} meses {f['factor_12']:,.0f} kg/T "
                       f"({f['var_3_vs_12']:+,.1f}%)",
        } for f in con_var[:_PANEL_FILAS + 2]]
        tol = fa["tolerancia"]
        limite = min(max(40, max(abs(f["valor"]) for f in filas) * 1.1), 100)
        panel("consumo", titulo, f"{fa['n_alto']} ▲ · {fa['n_bajo']} ▼",
              f"Materiales que gastan más / menos por T · {q.formato_mes_anio(fa['mes_sel'])}", "escala",
              charts.barras_escala(filas, -limite, limite, referencias=[(0, "")], banda=(-tol, tol)),
              "aviso" if fa["n_alto"] else "ok", "Revisar" if fa["n_alto"] else "Estable",
              leyenda=[("bien", f"Dentro de ±{tol}%"), ("regular", "Gasta más por T"), ("mal", "Gasta menos por T")],
              nota=f"kg por T de los 3 meses hasta este vs los {fa['meses_factor']} anteriores; los {len(filas)} que más se alejan.")

    # 4. Cobertura de stock: los materiales con menos días.
    cob = c.get("planificacion_cobertura")
    titulo = "Cobertura de stock más baja"
    con_dias = sorted((f for f in (cob["filas"] if cob else [])
                       if f["cobertura_dias"] is not None and math.isfinite(f["cobertura_dias"])),
                      key=lambda f: f["cobertura_dias"])
    if not con_dias:
        vacio("inventario", titulo)
    else:
        filas = [{
            "nombre": _nombre_material(f["texto_breve"]), "sub": centro(f["centro"]), "valor": f["cobertura_dias"],
            "texto": f"{f['cobertura_dias']:,.0f} d",
            "clase": "mal" if f["cobertura_dias"] < _COBERTURA_RIESGO else "regular" if f["cobertura_dias"] < _COBERTURA_AJUSTADA else "neutra",
            "detalle": f"{f['texto_breve']} ({centro(f['centro'])})\nStock {f['stock_cierre']:,.0f} T · consumo {f['consumo_mensual']:,.0f} T/mes\n"
                       f"Cobertura {f['cobertura_dias']:,.0f} días ({f['cobertura_meses']:,.1f} meses)",
        } for f in con_dias[:_PANEL_FILAS]]
        panel("inventario", titulo, str(cob["en_riesgo"]),
              f"Materiales con menos de {_COBERTURA_RIESGO} días · stock al {cob['fecha_stock']:%d/%m/%Y}" if cob["fecha_stock"] else
              f"Materiales con menos de {_COBERTURA_RIESGO} días", "escala",
              charts.barras_escala(filas, 0, tope(filas, 90, 180), referencias=[(_COBERTURA_RIESGO, f"{_COBERTURA_RIESGO} días")]),
              "alerta" if cob["en_riesgo"] else "ok", "En riesgo" if cob["en_riesgo"] else "Cubierto",
              leyenda=[("mal", f"< {_COBERTURA_RIESGO} días"), ("regular", f"{_COBERTURA_RIESGO}–{_COBERTURA_AJUSTADA} días"),
                       ("neutra", f"≥ {_COBERTURA_AJUSTADA} días")],
              nota=f"Días que alcanza el stock al ritmo de consumo; los {len(filas)} más bajos (de {len(con_dias)}).")

    _panel_salud_inventario(panel, vacio, cob, centro)
    _panel_primer_quiebre(panel, vacio, c.get("planificacion_proyeccion"), centro)

    # 5. Stock proyectado S&OP: cuántos materiales quedan en cada estado, mes a mes.
    p = c.get("planificacion_proyeccion")
    titulo = "Stock proyectado: estado de los materiales mes a mes"
    if not (p and p.get("matriz")):
        vacio("inventario", titulo, ancho=True)
    else:
        estados = [("quiebre", "Quiebre"), ("bajo_minimo", "Bajo mínimo"), ("en_rango", "En rango"),
                   ("sobre_maximo", "Sobre máximo"), ("sin_parametro", "Sin mín/máx")]
        conteos = [dict.fromkeys((e for e, _ in estados), 0) for _ in p["meses"]]
        for f in p["matriz"]:
            if len(f["celdas"]) == len(p["meses"]):
                for i, celda in enumerate(f["celdas"]):
                    conteos[i][celda["estado"]] = conteos[i].get(celda["estado"], 0) + 1
        pilas = []
        for m, conteo in zip(p["meses"], conteos):
            detalle = f"{m['corto']} · {sum(conteo.values())} materiales\n" + " · ".join(
                f"{conteo[e]} {t.lower()}" for e, t in estados if conteo[e])
            pilas.append([{"valor": conteo[e], "clase": f"col-{e}", "detalle": detalle} for e, _ in estados])
        tono, estado = (("alerta", "Quiebre") if p["con_quiebre"] else ("aviso", "Bajo mínimo") if p["n_bajo_minimo"]
                        else ("ok", "En rango"))
        panel("inventario", titulo, str(p["con_quiebre"]),
              f"Materiales con quiebre proyectado · S&OP {q.formato_mes_anio(p['snapshot'])} · "
              f"{p['n_bajo_minimo']} caen bajo su mínimo · {p['n_sobrecubiertos']} sobre su máximo", "columnas",
              charts.columnas([m["corto"] for m in p["meses"]], pilas, lambda v: f"{v:,.0f}", ancho=1040, alto=230),
              tono, estado, leyenda=[(f"col-{e}", t) for e, t in estados],
              nota="Stock final proyectado de cada material frente a su mínimo y máximo.", ancho=True)

    # 6. Precios: variación interanual de cada mes (columnas) e IPU acumulado
    # del año (línea). Con un mes elegido, la serie llega hasta ese mes (los
    # 24 anteriores) y la cifra es el IPU acumulado a ese mes.
    inf = c.get("planificacion_inflacion")
    titulo = "Precios de compra: inflación interanual e IPU"
    meses_inf = (inf.get("meses_fecha") or []) if inf and inf.get("resumen") else []
    hasta = len(meses_inf) - 1 if not mes else max((i for i, m in enumerate(meses_inf) if m <= mes), default=-1)
    if hasta < 0:
        vacio("precios", titulo, ancho=True)
    else:
        desde = max(0, hasta - 23) if mes else 0
        r = inf["resumen"]
        etiquetas = inf["meses"][desde:hasta + 1]
        canasta = inf["canasta"][desde:hasta + 1]
        ipu_fila = inf["ipu_fila"][desde:hasta + 1]
        pilas = [[{"valor": x["pct"], "clase": "col-sube" if x["pct"] > 0 else "col-baja", "detalle": x["detalle"]}]
                 for x in canasta]
        ipu = [None if x is None else x["pct"] for x in ipu_fila]
        linea = None
        if any(v is not None for v in ipu):
            ultimo = next(v for v in reversed(ipu) if v is not None)
            linea = {"valores": ipu, "clase": "linea-ipu", "etiqueta_final": f"IPU {_pct_texto(ultimo, True)}",
                     "cortes": [i for i in range(1, len(etiquetas)) if etiquetas[i][-2:] != etiquetas[i - 1][-2:]],
                     "detalles": ["" if x is None else x["detalle"] for x in ipu_fila]}
        cifra, sub, tono, estado = (_pct_texto(canasta[-1]["pct"], True), f"Variación interanual · {etiquetas[-1]}", "", "")
        if mes:
            ultimo_ipu = ipu_fila[-1]
            if ultimo_ipu is not None:
                m = meses_inf[hasta]
                cifra = f"IPU {_pct_texto(ultimo_ipu['pct'], True)}"
                sub = (f"{m.year}, acumulado a {etiquetas[-1]} vs {m.year - 1} · bolsa {charts.soles_corto(ultimo_ipu['bolsa'])}"
                       + (f" · {q.formato_mes_anio(mes)} sin compras" if m != mes else ""))
                tono, estado = (("alerta", "Sube") if ultimo_ipu["pct"] > 0.05 else ("ok", "Baja") if ultimo_ipu["pct"] < -0.05
                                else ("", "Estable"))
        elif r["por_anio"]:
            a = r["por_anio"][-1]
            cifra = f"IPU {_pct_texto(a['pct'], True)}"
            sub = f"{a['anio']}, acumulado a {a['hasta']} vs {a['anio_base']} · bolsa {charts.soles_corto(a['bolsa'])}"
            tono, estado = ("alerta", "Sube") if a["pct"] > 0.05 else ("ok", "Baja") if a["pct"] < -0.05 else ("", "Estable")
        panel("precios", titulo, cifra, sub, "columnas",
              charts.columnas(etiquetas, pilas, lambda v: f"{v:+,.0f}%" if v else "0%", linea=linea, ancho=1040, alto=240),
              tono, estado,
              leyenda=[("col-sube", "Precio sube vs el mismo mes del año anterior"), ("col-baja", "Precio baja"),
                       ("linea-ipu", "IPU acumulado del año")],
              nota="Canasta de materiales comprados cada mes, ponderada por lo comprado.", ancho=True)

    return paneles


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


# Documentos de ingreso que se listan en la página (los más recientes); el
# detalle completo va en el Excel (exportar_premios).
_PREMIOS_DOCUMENTOS_EN_PAGINA = 300


def _filtrar_premios(request, sufijo="premios", base=None):
    """Filtros de Premios y castigos (Período/Centro/Material/Proveedor, en
    cascada), compartidos por la sección y su descarga a Excel — y, con
    sufijo "energia" y su propia `base`, por Costo por energía. Devuelve
    (df filtrado, dict con opciones y selección)."""
    if base is None:
        base = q.premios_calidad_sop()
    if base.empty:
        return base, None
    periodo_sel = request.GET.get(f"periodo_{sufijo}", "todo")
    if periodo_sel not in _PERIODOS_CALIDAD:
        periodo_sel = "todo"
    meses_atras = _PERIODOS_CALIDAD[periodo_sel][1]
    if meses_atras:
        base = base[base["fecha"] >= q._sumar_meses(max(base["fecha"]).replace(day=1), -(meses_atras - 1))]
    _, centros_op = _opciones_categoria_centro(base)
    centro_sel = request.GET.get(f"centro_{sufijo}", "Todos")
    if centro_sel not in centros_op:
        centro_sel = "Todos"
    por_centro = base if centro_sel == "Todos" else base[base["centro"] == centro_sel]
    materiales_op = _opciones_material(por_centro)
    material_sel = _selector_material(request, sufijo, materiales_op)
    por_material = _por_materiales(por_centro, material_sel)
    proveedores_op = ["Todos"] + sorted(por_material["proveedor"].unique().tolist())
    proveedor_sel = request.GET.get(f"proveedor_{sufijo}", "Todos")
    if proveedor_sel not in proveedores_op:
        proveedor_sel = "Todos"
    df = por_material if proveedor_sel == "Todos" else por_material[por_material["proveedor"] == proveedor_sel]
    return df, {
        "periodos": [{"valor": k, "texto": v[0]} for k, v in _PERIODOS_CALIDAD.items()], "periodo_sel": periodo_sel,
        "centros": centros_op, "centro_sel": centro_sel,
        "materiales": materiales_op, "material_sel": material_sel,
        "proveedores": proveedores_op, "proveedor_sel": proveedor_sel,
    }


def _con_barra(filas, campo="neto_por_t"):
    """Agrega a cada fila el ancho (% del mayor valor absoluto) y el sentido de
    su barra divergente de premio (+) / castigo (−)."""
    mayor = max((abs(f[campo]) for f in filas), default=0) or 1
    for f in filas:
        f["barra_pct"] = f"{abs(f[campo]) / mayor * 50:.1f}"
        f["barra_sentido"] = "premio" if f[campo] > 0 else ("castigo" if f[campo] < 0 else "")
    return filas


def _contexto_premios(request):
    """Premios y castigos por calidad (ZMM056 + MB51, ver
    queries.premios_calidad_sop): cuánto se pagó de más (premio) o se
    descontó (castigo) sobre el precio base del contrato, por material, por
    proveedor y por documento de ingreso. Pedido por el usuario."""
    df, filtros = _filtrar_premios(request)
    if filtros is None:
        return None
    contexto = {**filtros, "resumen": None, "por_material": [], "por_proveedor": [], "documentos": [],
                "columnas_calidad": [], "total_documentos": 0}
    if df.empty:
        return contexto
    contexto["resumen"] = q.resumen_premios(df)

    un_material = len(filtros["material_sel"]) == 1 or df["material_codigo"].nunique() == 1
    if not un_material:
        contexto["por_material"] = _con_barra([
            {"material_codigo": m, "texto_breve": g["texto_breve"].iloc[0], "centro": c, **q.resumen_premios(g)}
            for (m, c), g in df.groupby(["material_codigo", "centro"])
        ], "pct_sobre_base")
        contexto["por_material"].sort(key=lambda f: -abs(f["neto"]))

    # Calidad promedio de cada proveedor (solo con un material: cada material
    # mide parámetros distintos, mezclarlos en columnas no tiene sentido).
    parametros = q.parametros_con_datos(df) if un_material else []
    contexto["columnas_calidad"] = [{"etiqueta": e, "unidad": u} for _, e, u, _ in parametros]
    por_proveedor = []
    for (proveedor, material, centro), g in df.groupby(["proveedor", "material_codigo", "centro"]):
        fila = {"proveedor": proveedor, "material_codigo": material, "texto_breve": g["texto_breve"].iloc[0],
                "centro": centro, **q.resumen_premios(g)}
        fila["calidad"] = [
            (f"{v:,.{d}f}" if (v := q.promedio_calidad(g, c)) is not None else "—")
            for c, _, _, d in parametros
        ]
        por_proveedor.append(fila)
    por_proveedor.sort(key=lambda f: (-f["neto_por_t"]))
    contexto["por_proveedor"] = _con_barra(por_proveedor)

    if un_material:
        recientes = df.head(_PREMIOS_DOCUMENTOS_EN_PAGINA)
        contexto["documentos"] = [
            {**r, "calidad": [(f"{r[c]:,.{d}f}" if r[c] == r[c] and r[c] is not None else "—") for c, _, _, d in parametros]}
            for r in recientes.to_dict("records")
        ]
        contexto["total_documentos"] = len(df)
    contexto["un_material"] = un_material
    contexto["limite_documentos"] = _PREMIOS_DOCUMENTOS_EN_PAGINA
    return contexto


@login_required
def exportar_premios(request):
    """Excel de Premios y castigos con los mismos filtros de la sección:
    resumen por material y por proveedor, y el detalle completo por
    documento de ingreso (con su calidad)."""
    df, filtros = _filtrar_premios(request)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        if filtros is None or df.empty:
            pd.DataFrame({"Aviso": ["Sin premios/castigos para los filtros elegidos."]}).to_excel(writer, sheet_name="Resumen", index=False)
        else:
            nombres = {
                "documentos": "Documentos", "toneladas": "Toneladas secas", "importe": "Importe (S/)",
                "premios": "Premios (S/)", "castigos": "Castigos (S/)", "neto": "Neto (S/)",
                "neto_por_t": "Neto (S/ por T)", "precio_base": "Precio base (S/ por T)",
                "precio_efectivo": "Precio efectivo (S/ por T)", "pct_sobre_base": "% sobre precio base",
                "pct_con_premio": "% documentos con premio", "pct_con_castigo": "% documentos con castigo",
            }
            for hoja, claves in (("Por material", ["material_codigo", "texto_breve", "centro"]),
                                 ("Por proveedor", ["proveedor", "material_codigo", "texto_breve", "centro"])):
                resumen = pd.DataFrame([
                    {**dict(zip(claves, k)), **q.resumen_premios(g)} for k, g in df.groupby(claves)
                ]).rename(columns={"material_codigo": "Material", "texto_breve": "Descripción", "centro": "Centro",
                                   "proveedor": "Proveedor", **nombres})
                resumen.to_excel(writer, sheet_name=hoja, index=False)
            parametros = q.parametros_con_datos(df)
            detalle = df[["fecha", "material_codigo", "texto_breve", "centro", "proveedor", "ticket", "documento",
                          "toneladas", "precio_base", "premio", "premio_por_t", "precio_efectivo", "importe",
                          *[c for c, _, _, _ in parametros]]].rename(columns={
                "fecha": "Fecha", "material_codigo": "Material", "texto_breve": "Descripción", "centro": "Centro",
                "proveedor": "Proveedor", "ticket": "Ticket", "documento": "Documento material",
                "toneladas": "Toneladas secas", "precio_base": "Precio base (S/ por T)", "premio": "Premio/castigo (S/)",
                "premio_por_t": "Premio/castigo (S/ por T)", "precio_efectivo": "Precio efectivo (S/ por T)",
                "importe": "Importe (S/)",
                **{c: f"{e} ({u})" if u else e for c, e, u, _ in parametros},
            })
            detalle.to_excel(writer, sheet_name="Por documento", index=False)
        for hoja in writer.book.worksheets:
            for columna in hoja.columns:
                hoja.column_dimensions[columna[0].column_letter].width = max(12, min(40, len(str(columna[0].value or "")) + 2))
            hoja.freeze_panes = "A2"
    respuesta = HttpResponse(buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    respuesta["Content-Disposition"] = f'attachment; filename="Premios_castigos_calidad_{datetime.date.today():%Y%m%d}.xlsx"'
    return respuesta


def _filtrar_energia(request):
    """Filtros de Costo por energía: los de Premios (sufijo "energia") sobre
    los ingresos de carbón con PCS analizado."""
    base = q.premios_calidad_sop()
    if base.empty:
        return base, None
    return _filtrar_premios(request, "energia", q.ingresos_con_energia(base))


# Palabras que no distinguen a un proveedor de otro (razón social): se quitan
# de las etiquetas del gráfico, si no varios quedaban como "SOCIEDAD MINERA…".
_RAZON_SOCIAL = re.compile(
    r"^(SOCIEDAD MINERA( R\.?L\.?)?|INVERSIONES|EMPRESA MINERA|COMPA[NÑ]IA MINERA)\s+"
    r"|\s+(S\.?A\.?C\.?|S\.?A\.?A\.?|S\.?A\.?|E\.?I\.?R\.?L\.?|S\.?R\.?L\.?)$"
)


def _nombre_corto(texto, largo=22):
    """Nombre de proveedor sin la razón social, recortado en un límite de
    palabra, para las etiquetas del gráfico."""
    anterior = None
    while anterior != texto:
        anterior, texto = texto, _RAZON_SOCIAL.sub("", texto.strip())
    texto = texto.title()
    if len(texto) <= largo:
        return texto
    return texto[:largo].rsplit(" ", 1)[0] + "…"


def _contexto_energia(request):
    """Costo por energía de los carbones (ver queries.ingresos_con_energia):
    S/ por Gcal efectivo de cada material y proveedor, comparado con el
    promedio de su material, un gráfico PCS vs precio y la evolución mensual.
    Pedido por el usuario."""
    df, filtros = _filtrar_energia(request)
    if filtros is None:
        return None
    contexto = {**filtros, "resumen": None, "por_material": [], "por_proveedor": [], "grafico": None,
                "mensual": None}
    if df.empty:
        return contexto
    resumen = q.resumen_energia(df)
    contexto["resumen"] = resumen
    un_material = len(filtros["material_sel"]) == 1 or df["material_codigo"].nunique() == 1
    contexto["un_material"] = un_material

    def con_diferencia(fila, referencia):
        fila["referencia"] = referencia
        fila["dif_pct"] = (fila["costo_gcal"] / referencia - 1) * 100 if referencia else 0.0
        fila["dif_soles"] = (fila["costo_gcal"] - referencia) * fila["gcal"]
        return fila

    # Con varios materiales, cada uno contra el promedio de toda la selección;
    # cada proveedor, contra el promedio de SU material (el carbón de otro
    # material o centro no es un reemplazo directo).
    por_material = [
        con_diferencia({"material_codigo": m, "texto_breve": g["texto_breve"].iloc[0], "centro": c,
                        **q.resumen_energia(g)}, resumen["costo_gcal"])
        for (m, c), g in df.groupby(["material_codigo", "centro"])
    ]
    referencia_material = {(f["material_codigo"], f["centro"]): f["costo_gcal"] for f in por_material}
    por_proveedor = [
        con_diferencia({"proveedor": p, "material_codigo": m, "texto_breve": g["texto_breve"].iloc[0], "centro": c,
                        **q.resumen_energia(g)}, referencia_material[(m, c)])
        for (p, m, c), g in df.groupby(["proveedor", "material_codigo", "centro"])
    ]
    por_proveedor.sort(key=lambda f: (f["texto_breve"], f["centro"], f["costo_gcal"]))
    por_material.sort(key=lambda f: f["costo_gcal"])
    for filas in (por_material, por_proveedor):
        mayor = max((abs(f["dif_pct"]) for f in filas), default=0) or 1
        for f in filas:
            f["barra_pct"] = f"{abs(f['dif_pct']) / mayor * 50:.1f}"
            f["barra_sentido"] = "barato" if f["dif_pct"] < -0.05 else ("caro" if f["dif_pct"] > 0.05 else "")
    contexto["por_material"] = por_material if not un_material else []
    contexto["por_proveedor"] = por_proveedor

    # Gráfico: proveedores del material elegido, o un punto por material.
    if un_material:
        puntos = [{"nombre": _nombre_corto(f["proveedor"]), "pcs": f["pcs"], "precio": f["precio_efectivo"],
                   "toneladas": f["toneladas"], "costo_gcal": f["costo_gcal"]} for f in por_proveedor]
    else:
        puntos = [{"nombre": f"{f['texto_breve'].title()} {q.nombre_centro(f['centro'])}", "pcs": f["pcs"],
                   "precio": f["precio_efectivo"], "toneladas": f["toneladas"], "costo_gcal": f["costo_gcal"]}
                  for f in por_material]
    contexto["grafico"] = charts.dispersion_energia(puntos, resumen["costo_gcal"])

    # Evolución mensual del S/ por Gcal (filas: proveedores o materiales).
    df = df.assign(mes=df["fecha"].map(lambda f: f.replace(day=1)))
    meses = sorted(df["mes"].unique())
    claves = ["proveedor"] if un_material else ["material_codigo", "centro"]
    filas_mes = []
    for clave, g in df.groupby(claves):
        clave = clave if isinstance(clave, tuple) else (clave,)
        por_mes = {m: q.resumen_energia(gm)["costo_gcal"] for m, gm in g.groupby("mes")}
        nombre = clave[0] if un_material else f"{g['texto_breve'].iloc[0]} ({q.nombre_centro(clave[1])})"
        filas_mes.append({"nombre": nombre, "valores": [por_mes.get(m) for m in meses],
                          "total": q.resumen_energia(g)["costo_gcal"]})
    filas_mes.sort(key=lambda f: f["total"])
    total_mes = {m: q.resumen_energia(gm)["costo_gcal"] for m, gm in df.groupby("mes")}
    contexto["mensual"] = {
        "meses": [q.formato_mes_anio_abrev(m) for m in meses], "meses_fecha": meses, "filas": filas_mes,
        "total": [total_mes.get(m) for m in meses], "total_periodo": resumen["costo_gcal"],
    }
    return contexto


@login_required
def exportar_energia(request):
    """Excel de Costo por energía con los mismos filtros de la sección: por
    material, por proveedor, mensual y el detalle por documento de ingreso."""
    df, filtros = _filtrar_energia(request)
    buffer = io.BytesIO()
    nombres = {
        "material_codigo": "Material", "texto_breve": "Descripción", "centro": "Centro", "proveedor": "Proveedor",
        "documentos": "Documentos", "toneladas": "Toneladas secas", "gcal": "Energía (Gcal)",
        "importe": "Importe (S/)", "premio": "Premio/castigo (S/)", "pcs": "PCS (kcal/kg)", "cenizas": "Cenizas (%)",
        "precio_base": "Precio base (S/ por T)", "precio_efectivo": "Precio efectivo (S/ por T)",
        "costo_gcal": "Costo efectivo (S/ por Gcal)", "costo_gcal_base": "Costo a precio base (S/ por Gcal)",
        "premio_gcal": "Premio/castigo (S/ por Gcal)",
    }
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        if filtros is None or df.empty:
            pd.DataFrame({"Aviso": ["Sin ingresos de carbón con PCS para los filtros elegidos."]}).to_excel(writer, sheet_name="Resumen", index=False)
        else:
            for hoja, claves in (("Por material", ["material_codigo", "texto_breve", "centro"]),
                                 ("Por proveedor", ["proveedor", "material_codigo", "texto_breve", "centro"])):
                pd.DataFrame([
                    {**dict(zip(claves, k)), **q.resumen_energia(g)} for k, g in df.groupby(claves)
                ]).rename(columns=nombres).to_excel(writer, sheet_name=hoja, index=False)
            mensual = df.assign(Mes=df["fecha"].map(lambda f: f.replace(day=1)))
            pd.DataFrame([
                {"Mes": k[0].strftime("%Y-%m"), **dict(zip(["proveedor", "material_codigo", "texto_breve", "centro"], k[1:])),
                 **q.resumen_energia(g)}
                for k, g in mensual.groupby(["Mes", "proveedor", "material_codigo", "texto_breve", "centro"])
            ]).rename(columns=nombres).to_excel(writer, sheet_name="Mensual", index=False)
            df[["fecha", "material_codigo", "texto_breve", "centro", "proveedor", "ticket", "documento", "toneladas",
                "pcs", "cenizas", "gcal", "precio_base", "premio", "precio_efectivo", "importe",
                "costo_gcal_base", "costo_gcal"]].rename(columns={
                    **nombres, "fecha": "Fecha", "ticket": "Ticket", "documento": "Documento material",
                }).to_excel(writer, sheet_name="Por documento", index=False)
        for hoja in writer.book.worksheets:
            for columna in hoja.columns:
                hoja.column_dimensions[columna[0].column_letter].width = max(12, min(40, len(str(columna[0].value or "")) + 2))
            hoja.freeze_panes = "A2"
    respuesta = HttpResponse(buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    respuesta["Content-Disposition"] = f'attachment; filename="Costo_energia_carbones_{datetime.date.today():%Y%m%d}.xlsx"'
    return respuesta


# Período de Calidad: meses hacia atrás desde el último ticket analizado
# (None = toda la historia de ZMM056 cargada).
_PERIODOS_CALIDAD = {"3": ("Últimos 3 meses", 3), "6": ("Últimos 6 meses", 6), "todo": ("Toda la historia", None)}


def _fecha_corta(fecha):
    return f"{fecha.day:02d}-{_MESES_ABREV_GUION[fecha.month]}-{fecha.year % 100:02d}"


def _contexto_calidad(request):
    """Calidad de los materiales del S&OP: análisis químico de los ingresos
    (ZMM056, ver queries.calidad_ingresos_sop). Filtros propios en cascada
    (Período/Categoría/Centro/Material). Con varios materiales: una tarjeta
    por material con el promedio de sus parámetros; con uno: un gráfico por
    parámetro (semanal), el detalle mensual y la comparación por proveedor.
    Pedido por el usuario."""
    base = q.calidad_ingresos_sop()
    if base.empty:
        return None
    periodo_sel = request.GET.get("periodo_calidad", "todo")
    if periodo_sel not in _PERIODOS_CALIDAD:
        periodo_sel = "todo"
    meses_atras = _PERIODOS_CALIDAD[periodo_sel][1]
    if meses_atras:
        base = base[base["fecha"] >= q._sumar_meses(max(base["fecha"]).replace(day=1), -(meses_atras - 1))]

    categorias_op, _ = _opciones_categoria_centro(base)
    categoria_sel = request.GET.get("categoria_calidad", "Todas")
    if categoria_sel not in categorias_op:
        categoria_sel = "Todas"
    por_categoria = base if categoria_sel == "Todas" else base[base["categoria"] == categoria_sel]
    _, centros_op = _opciones_categoria_centro(por_categoria)
    centro_sel = request.GET.get("centro_calidad", "Todos")
    if centro_sel not in centros_op:
        centro_sel = "Todos"
    por_centro = por_categoria if centro_sel == "Todos" else por_categoria[por_categoria["centro"] == centro_sel]
    materiales_op = _opciones_material(por_centro)
    material_sel = _selector_material(request, "calidad", materiales_op)
    por_material = _por_materiales(por_centro, material_sel)
    # Proveedor (en cascada, después de Material): todo lo de la sección se
    # calcula solo con sus ingresos; `por_material` (todos los proveedores)
    # queda como referencia para comparar. Pedido por el usuario.
    por_material = por_material.assign(proveedor=por_material["proveedor"].replace("", "(sin proveedor)"))
    proveedores_op = ["Todos"] + sorted(por_material["proveedor"].unique().tolist())
    proveedor_sel = request.GET.get("proveedor_calidad", "Todos")
    if proveedor_sel not in proveedores_op:
        proveedor_sel = "Todos"
    df = por_material if proveedor_sel == "Todos" else por_material[por_material["proveedor"] == proveedor_sel]
    con_proveedor = proveedor_sel != "Todos"

    def formateador(decimales):
        return lambda v: f"{v:,.{decimales}f}"

    tarjetas = []
    for (material, centro), g in df.groupby(["material_codigo", "centro"]):
        ultimo_mes = max(g["fecha"]).replace(day=1)
        del_ultimo_mes = g[g["fecha"] >= ultimo_mes]
        parametros = []
        for campo, etiqueta, unidad, decimales in q.parametros_con_datos(g):
            promedio = q.promedio_calidad(g, campo)
            reciente = q.promedio_calidad(del_ultimo_mes, campo)
            parametros.append({
                "campo": campo, "etiqueta": etiqueta, "unidad": unidad,
                "promedio": formateador(decimales)(promedio),
                "reciente": formateador(decimales)(reciente) if reciente is not None else "—",
                # Sin juicio de bueno/malo (depende del parámetro y de la
                # especificación, que todavía no está cargada): solo sube/baja.
                "tendencia": "" if reciente is None or promedio == 0 or abs(reciente / promedio - 1) < 0.01
                             else ("sube" if reciente > promedio else "baja"),
            })
        tarjetas.append({
            "material_codigo": material, "texto_breve": g["texto_breve"].iloc[0], "centro": centro,
            "toneladas": float(g["toneladas"].sum()),
            "muestras": int(g.loc[g["numero_muestra"] != "", "numero_muestra"].nunique()),
            "proveedores": int(g["proveedor"].replace("(sin proveedor)", pd.NA).nunique()),
            "ultimo_mes": _mes_corto(ultimo_mes), "ultima_fecha": max(g["fecha"]),
            "parametros": parametros,
        })
    tarjetas.sort(key=lambda t: (t["centro"], t["material_codigo"]))

    un_material = len(tarjetas) == 1
    graficos, mensual, por_proveedor, columnas = [], [], [], []
    participacion = None
    if un_material:
        g = df.copy()
        # Referencia = el mismo material y centro (con Material en "Todos" y un
        # proveedor que entrega un solo material, por_material trae los demás).
        por_material = por_material[(por_material["material_codigo"] == tarjetas[0]["material_codigo"])
                                    & (por_material["centro"] == tarjetas[0]["centro"])]
        total_material = float(por_material["toneladas"].sum())
        participacion = float(g["toneladas"].sum()) / total_material * 100 if total_material else 0.0
        parametros = q.parametros_con_datos(g)
        columnas = [{"etiqueta": e, "unidad": u} for _, e, u, _ in parametros]
        g["semana"] = [f - datetime.timedelta(days=f.weekday()) for f in g["fecha"]]
        for campo, etiqueta, unidad, decimales in parametros:
            resumen = q.resumen_calidad(g, campo)
            serie = [
                (semana, q.promedio_calidad(sem, campo))
                for semana, sem in g.groupby("semana")
            ]
            serie = [(s, v) for s, v in serie if v is not None]
            fmt = formateador(decimales)
            # Con un proveedor elegido: promedio de TODOS los proveedores del
            # material en el mismo período, para comparar.
            referencia = q.promedio_calidad(por_material, campo) if con_proveedor else None
            diferencia = None
            if referencia is not None:
                delta = resumen["promedio"] - referencia
                diferencia = ("+" if delta >= 0 else "−") + fmt(abs(delta))
            graficos.append({
                "etiqueta": etiqueta, "unidad": unidad,
                "promedio": fmt(resumen["promedio"]), "minimo": fmt(resumen["minimo"]), "maximo": fmt(resumen["maximo"]),
                "desviacion": fmt(resumen["desviacion"]) if decimales else f"{resumen['desviacion']:,.1f}",
                "muestras": resumen["muestras"],
                "referencia": fmt(referencia) if referencia is not None else None, "diferencia": diferencia,
                "grafico": charts.evolucion_calidad(serie, resumen["promedio"], fmt, _fecha_corta, referencia=referencia),
            })
        g["mes"] = [f.replace(day=1) for f in g["fecha"]]
        for mes, m in sorted(g.groupby("mes"), key=lambda x: x[0], reverse=True):
            mensual.append({
                "mes": mes, "toneladas": float(m["toneladas"].sum()),
                "muestras": int(m.loc[m["numero_muestra"] != "", "numero_muestra"].nunique()),
                "valores": [
                    (formateador(d)(v) if (v := q.promedio_calidad(m, c)) is not None else "—")
                    for c, _, _, d in parametros
                ],
            })
        # Todos los proveedores del material (no solo el elegido), con el
        # elegido resaltado: así se compara contra los demás.
        for proveedor, m in por_material.groupby("proveedor"):
            por_proveedor.append({
                "proveedor": proveedor, "seleccionado": proveedor == proveedor_sel,
                "toneladas": float(m["toneladas"].sum()),
                "muestras": int(m.loc[m["numero_muestra"] != "", "numero_muestra"].nunique()),
                "valores": [
                    (formateador(d)(v) if (v := q.promedio_calidad(m, c)) is not None else "—")
                    for c, _, _, d in parametros
                ],
            })
        por_proveedor.sort(key=lambda p: -p["toneladas"])

    return {
        "periodos": [{"valor": k, "texto": v[0]} for k, v in _PERIODOS_CALIDAD.items()], "periodo_sel": periodo_sel,
        "categorias": categorias_op, "categoria_sel": categoria_sel,
        "centros": centros_op, "centro_sel": centro_sel,
        "materiales": materiales_op, "material_sel": material_sel,
        "proveedores": proveedores_op, "proveedor_sel": proveedor_sel, "con_proveedor": con_proveedor,
        "participacion": participacion,
        "tarjetas": tarjetas, "un_material": un_material,
        "graficos": graficos, "columnas": columnas, "mensual": mensual, "por_proveedor": por_proveedor,
        "desde": min(df["fecha"]) if not df.empty else None, "hasta": max(df["fecha"]) if not df.empty else None,
    }


# Período del gráfico de precios de entrada: meses hacia atrás desde el
# último mes con compras (None = toda la historia de MB51).
_PERIODOS_PRECIOS = {"12": ("Últimos 12 meses", 12), "24": ("Últimos 24 meses", 24), "todo": ("Toda la historia", None)}


def _contexto_precios_entrada(request):
    """Evolución de precios de entrada (S/ por T) de los materiales del S&OP,
    desde MB51 (ver queries.precios_entrada_sop). Filtros propios en cascada
    (Categoría/Centro/Material) y Período. Con varios materiales: una tarjeta
    por material con su minigráfico; con uno: gráfico grande con las
    toneladas compradas y su detalle mensual. Pedido por el usuario."""
    base = q.precios_entrada_sop()
    if base.empty:
        return None
    periodo_sel = request.GET.get("periodo_precios", "24")
    if periodo_sel not in _PERIODOS_PRECIOS:
        periodo_sel = "24"
    meses_atras = _PERIODOS_PRECIOS[periodo_sel][1]
    if meses_atras:
        desde = q._sumar_meses(max(base["mes"]), -(meses_atras - 1))
        base = base[base["mes"] >= desde]

    categorias_op, _ = _opciones_categoria_centro(base)
    categoria_sel = request.GET.get("categoria_precios", "Todas")
    if categoria_sel not in categorias_op:
        categoria_sel = "Todas"
    por_categoria = base if categoria_sel == "Todas" else base[base["categoria"] == categoria_sel]
    _, centros_op = _opciones_categoria_centro(por_categoria)
    centro_sel = request.GET.get("centro_precios", "Todos")
    if centro_sel not in centros_op:
        centro_sel = "Todos"
    por_centro = por_categoria if centro_sel == "Todos" else por_categoria[por_categoria["centro"] == centro_sel]
    materiales_op = _opciones_material(por_centro)
    material_sel = _selector_material(request, "precios", materiales_op)
    df = _por_materiales(por_centro, material_sel)

    tarjetas = q.resumen_precios_entrada(df)
    for t in tarjetas:
        t["mes_ultimo_corto"] = _mes_corto(t["mes_ultimo"])
        t["mes_base_anual_corto"] = _mes_corto(t["mes_base_anual"]) if t["mes_base_anual"] else ""
        t["variacion_anual_pct"] = None if t["variacion_anual"] is None else t["variacion_anual"] * 100
        t["variacion_anterior_pct"] = None if t["variacion_anterior"] is None else t["variacion_anterior"] * 100
        t["minigrafico"] = charts.minigrafico_precio(t["serie"])
        t["importado"] = t["centro"] == q.CENTRO_IMPORTACION
    # Primero los que más subieron en el año (lo que más le importa a
    # gerencia); los que no tienen base anual, al final.
    tarjetas.sort(key=lambda t: (t["variacion_anual"] is None, -(t["variacion_anual"] or 0)))

    detalle, grafico = [], None
    if len(tarjetas) == 1:
        g = df.sort_values("mes")
        anterior = None
        for r in g.itertuples():
            detalle.append({
                "mes": r.mes, "toneladas": r.toneladas, "importe": r.importe, "precio": r.precio,
                "entradas": r.entradas, "proveedores": r.proveedores,
                "variacion_pct": None if anterior is None else (r.precio / anterior - 1) * 100,
            })
            anterior = r.precio
        grafico = charts.evolucion_precio(
            [(r.mes, float(r.precio), float(r.toneladas)) for r in g.itertuples()], _mes_corto,
        )
        detalle.reverse()  # lo más reciente arriba

    return {
        "periodos": [{"valor": k, "texto": v[0]} for k, v in _PERIODOS_PRECIOS.items()], "periodo_sel": periodo_sel,
        "categorias": categorias_op, "categoria_sel": categoria_sel,
        "centros": centros_op, "centro_sel": centro_sel, "centro_importacion": q.CENTRO_IMPORTACION,
        "materiales": materiales_op, "material_sel": material_sel,
        "tarjetas": tarjetas, "un_material": len(tarjetas) == 1,
        "grafico": grafico, "detalle": detalle,
        "desde": min(df["mes"]) if not df.empty else None, "hasta": max(df["mes"]) if not df.empty else None,
    }


def _mes_corto(fecha):
    return f"{_MESES_ABREV_GUION[fecha.month]}-{fecha.year % 100:02d}"


# Las dos evaluaciones de precios de Planificación (ver
# queries.inflacion_precios): interanual (T vs T−12) y mensual (T vs T−1).
# Cada una es una sección con sus propios filtros (sufijo de los parámetros
# GET). Pedido por el usuario.
_EVALUACIONES_PRECIOS = {
    "inflacion": {
        "desfase": 12, "id": "sec-inflacion", "titulo": "Inflación de precios (interanual)",
        "contra": "el mismo mes del año anterior", "ejemplo": "Ene-26 vs Ene-25",
        "sustituto": "Dic-24, Nov-24…", "base_titulo": "un año antes",
    },
    "mensual": {
        "desfase": 1, "id": "sec-inflacion-mensual", "titulo": "Evaluación mensual de precios",
        "contra": "el mes anterior", "ejemplo": "Mar-26 vs Feb-26",
        "sustituto": "Ene-26, Dic-25…", "base_titulo": "el mes anterior",
    },
}
# Por defecto se evalúa desde este año (2025 contra 2024 en adelante).
_ANIO_INICIO_INFLACION = 2025


def _periodos_inflacion(meses):
    """Opciones de Período según los meses T con comparación: desde 2025
    (por defecto), cada año, los últimos 12 meses y, si hay meses
    anteriores a 2025, toda la historia."""
    anios = sorted({m.year for m in meses if m.year >= _ANIO_INICIO_INFLACION})
    opciones = [("desde", f"{_ANIO_INICIO_INFLACION} en adelante")]
    opciones += [(str(a), f"Solo {a}") for a in anios]
    opciones.append(("12", "Últimos 12 meses"))
    if meses and min(meses).year < _ANIO_INICIO_INFLACION:
        opciones.append(("todo", "Toda la historia"))
    return opciones


def _filtrar_periodo_inflacion(inflacion, periodo):
    if periodo == "desde":
        return inflacion[inflacion["mes"] >= datetime.date(_ANIO_INICIO_INFLACION, 1, 1)]
    if periodo == "12":
        return inflacion[inflacion["mes"] >= q._sumar_meses(max(inflacion["mes"]), -11)]
    if periodo.isdigit():
        return inflacion[[m.year == int(periodo) for m in inflacion["mes"]]]
    return inflacion


def _contexto_inflacion(request, sufijo="inflacion"):
    """Evaluación de precios de compra (ver queries.inflacion_precios), en
    variación % o en bolsa (S/ = diferencia de precio × toneladas del mes
    evaluado). Filtros propios (Período/Categoría/Centro/Material/Ver). Una
    matriz material × mes con la bolsa por año, la selección ponderada mes a
    mes y, con un material, su detalle. Pedido por el usuario."""
    evaluacion = _EVALUACIONES_PRECIOS[sufijo]
    base = q.precios_entrada_sop()
    if base.empty:
        return None

    categorias_op, _ = _opciones_categoria_centro(base)
    categoria_sel = request.GET.get(f"categoria_{sufijo}", "Todas")
    if categoria_sel not in categorias_op:
        categoria_sel = "Todas"
    por_categoria = base if categoria_sel == "Todas" else base[base["categoria"] == categoria_sel]
    _, centros_op = _opciones_categoria_centro(por_categoria)
    centro_sel = request.GET.get(f"centro_{sufijo}", "Todos")
    if centro_sel not in centros_op:
        centro_sel = "Todos"
    por_centro = por_categoria if centro_sel == "Todos" else por_categoria[por_categoria["centro"] == centro_sel]
    materiales_op = _opciones_material(por_centro)
    material_sel = _selector_material(request, sufijo, materiales_op)
    df = _por_materiales(por_centro, material_sel)
    ver_sel = request.GET.get(f"ver_{sufijo}", "pct")
    if ver_sel not in ("pct", "soles", "precio"):
        ver_sel = "pct"
    en_soles = ver_sel == "soles"
    en_precio = ver_sel == "precio"
    # Análisis de la tabla: cada mes por sí solo (individual) o el IPU
    # acumulado del año hasta ese mes. Pedido por el usuario.
    analisis_sel = request.GET.get(f"analisis_{sufijo}", "individual")
    if analisis_sel not in ("individual", "acumulado"):
        analisis_sel = "individual"
    acumulado = analisis_sel == "acumulado"

    # Sobre TODA la historia (la base de un mes puede ser anterior al período
    # mostrado, y el IPU acumulado arranca en enero aunque el período empiece
    # después); el período solo recorta los meses T que se ven.
    inflacion = q.inflacion_precios(df, evaluacion["desfase"])
    inflacion_total = inflacion
    periodos = _periodos_inflacion(sorted(inflacion["mes"].unique()) if not inflacion.empty else [])
    periodo_sel = request.GET.get(f"periodo_{sufijo}", "desde")
    if periodo_sel not in {v for v, _ in periodos}:
        periodo_sel = "desde"
    contexto = {
        **evaluacion, "sufijo": sufijo,
        "periodos": [{"valor": v, "texto": t} for v, t in periodos], "periodo_sel": periodo_sel,
        "categorias": categorias_op, "categoria_sel": categoria_sel,
        "centros": centros_op, "centro_sel": centro_sel,
        "materiales": materiales_op, "material_sel": material_sel,
        "ver": [{"valor": "pct", "texto": "Variación %"}, {"valor": "soles", "texto": "Bolsa (S/)"},
                {"valor": "precio", "texto": "Precio (S/ por T)"}],
        "ver_sel": ver_sel, "en_soles": en_soles, "en_precio": en_precio,
        "analisis": [{"valor": "individual", "texto": "Individual (cada mes)"},
                     {"valor": "acumulado", "texto": "Acumulado del año (IPU)"}],
        "analisis_sel": analisis_sel, "acumulado": acumulado,
        "filas": [], "meses": [], "anios": [], "canasta": [], "ipu_fila": [], "grafico": None, "detalle": [],
        "resumen": None,
    }
    if not inflacion.empty:
        inflacion = _filtrar_periodo_inflacion(inflacion, periodo_sel)
    if inflacion.empty:
        return contexto
    inflacion = inflacion.assign(anio=[m.year for m in inflacion["mes"]])
    meses = sorted(inflacion["mes"].unique())
    anios = sorted(inflacion["anio"].unique())
    mayor_bolsa = float(inflacion.loc[~inflacion["atipica"], "bolsa"].abs().max() or 0) or 1.0

    def celda(pct, bolsa, extra, precio=None):
        valor = bolsa if en_soles else pct
        intensidad = abs(bolsa) / mayor_bolsa if en_soles else abs(pct) / 30
        # Ver precio: el precio del mes y, debajo, su variación (el color
        # sigue la variación). La fila Selección no tiene precio (sumar
        # precios de materiales distintos no tiene sentido): muestra el %.
        if en_precio and precio is not None:
            texto, subtexto = f"{precio:,.2f}", f"{pct:+.1f}%"
        else:
            texto, subtexto = (charts.soles_corto(bolsa) if en_soles else f"{pct:+.1f}%"), ""
        return {
            "pct": pct, "bolsa": bolsa, "sube": valor > 0, "intensidad": f"{min(intensidad, 1):.2f}",
            "texto": texto, "subtexto": subtexto, **extra,
        }

    def detalle_celda(r):
        base_txt = _mes_corto(r["mes_base"])
        if r["base_sustituida"]:
            base_txt += f" (último registrado; {_mes_corto(r['mes_referencia'])} sin compra)"
        signo = "+" if r["bolsa"] >= 0 else "−"
        texto = (f"{r['texto_breve']} ({q.nombre_centro(r['centro'])}) · {_mes_corto(r['mes'])}: {r['variacion'] * 100:+.1f}%\n"
                 f"{_mes_corto(r['mes'])}: S/ {r['precio']:,.2f} por T\n"
                 f"vs {base_txt}: S/ {r['precio_base']:,.2f} por T\n"
                 f"Bolsa: (S/ {r['precio']:,.2f} − S/ {r['precio_base']:,.2f}) × {r['toneladas']:,.0f} T = "
                 f"{signo}S/ {abs(r['bolsa']):,.0f}")
        if r["atipica"]:
            texto += "\nATÍPICA: el precio más que se duplica o cae a menos de la mitad (dato a revisar en MB51); no suma en la bolsa ni en la selección."
        return texto

    def detalle_acumulado(mes, datos, ipu, nombre=""):
        """Texto del recuadro de un valor acumulado (IPU) del mes `mes`."""
        inicio = min(m for m in ipu if m.year == mes.year)
        tramo = f"{_mes_corto(inicio)}–{_mes_corto(mes)}" if inicio != mes else _mes_corto(mes)
        signo = "+" if datos["bolsa_acumulada"] >= 0 else "−"
        if evaluacion["desfase"] == 12:
            texto = (f"{nombre}IPU acumulado {tramo} vs el mismo tramo del año anterior: {datos['acumulado'] * 100:+.1f}%\n"
                     f"Pagado: S/ {datos['importe_acumulado']:,.0f} · al precio del año anterior: "
                     f"S/ {datos['base_acumulada']:,.0f}\n")
        else:
            texto = f"{nombre}IPU acumulado {tramo} (variaciones de cada mes encadenadas): {datos['acumulado'] * 100:+.1f}%\n"
        return texto + (f"Bolsa acumulada: {signo}S/ {abs(datos['bolsa_acumulada']):,.0f} "
                        f"({datos['toneladas_acumuladas']:,.0f} T)")

    filas = []
    for (material, centro), g in inflacion.groupby(["material_codigo", "centro"]):
        por_mes = {r["mes"]: r for r in g.to_dict("records")}
        ultimo = g.iloc[-1]
        if acumulado:
            ipu_material = q.ipu_por_mes(
                inflacion_total[(inflacion_total["material_codigo"] == material) & (inflacion_total["centro"] == centro)],
                evaluacion["desfase"],
            )
        celdas = []
        for m in meses:
            r = por_mes.get(m)
            if r is None:
                celdas.append(None)
            elif acumulado and m in ipu_material:
                d = ipu_material[m]
                celdas.append(celda(
                    d["acumulado"] * 100, d["bolsa_acumulada"],
                    {"sustituida": False, "atipica": False,
                     "detalle": detalle_acumulado(m, d, ipu_material, f"{r['texto_breve']} ({q.nombre_centro(centro)}) · ")},
                    precio=d["importe_acumulado"] / d["toneladas_acumuladas"] if d["toneladas_acumuladas"] else None))
            else:
                celdas.append(celda(
                    r["variacion"] * 100, r["bolsa"],
                    {"sustituida": bool(r["base_sustituida"]), "atipica": bool(r["atipica"]), "detalle": detalle_celda(r)},
                    precio=r["precio"]))
        total = q.inflacion_canasta(g)
        validas = g[~g["atipica"]]
        filas.append({
            "material_codigo": material, "texto_breve": ultimo["texto_breve"], "centro": centro, "celdas": celdas,
            "ultima_pct": ultimo["variacion"] * 100, "ultimo_mes": _mes_corto(ultimo["mes"]),
            "periodo_pct": (total["variacion"] or 0) * 100, "bolsa": total["bolsa"],
            "bolsa_anios": [float(validas.loc[validas["anio"] == a, "bolsa"].sum()) if (g["anio"] == a).any() else None
                            for a in anios],
        })
    # Primero lo que más costó (bolsa más alta), al final lo que más ahorró.
    filas.sort(key=lambda f: -f["bolsa"])

    base_txt = "mismo mes del año anterior" if evaluacion["desfase"] == 12 else "mes anterior"
    ipu = q.ipu_por_mes(inflacion_total, evaluacion["desfase"])
    canasta, ipu_fila, puntos = [], [], []
    for m in meses:
        c = q.inflacion_canasta(inflacion[inflacion["mes"] == m])
        c["variacion"] = c["variacion"] or 0.0  # mes con solo comparaciones atípicas
        signo = "+" if c["bolsa"] >= 0 else "−"
        detalle = (f"{_mes_corto(m)} vs {base_txt}: {c['variacion'] * 100:+.1f}%\n"
                   f"Bolsa: {signo}S/ {abs(c['bolsa']):,.0f}\n"
                   f"{c['materiales']} material{'es' if c['materiales'] != 1 else ''} comprado{'s' if c['materiales'] != 1 else ''}"
                   f" · {c['toneladas']:,.0f} T · S/ {c['importe']:,.0f}")
        canasta.append(celda(c["variacion"] * 100, c["bolsa"], {"detalle": detalle}))
        d = ipu.get(m)
        if d is None:
            ipu_fila.append(None)
            continue
        texto_acumulado = detalle_acumulado(m, d, ipu)
        ipu_fila.append(celda(d["acumulado"] * 100, d["bolsa_acumulada"], {"detalle": texto_acumulado}))
        puntos.append({
            "mes": m, "individual": d["bolsa"] if en_soles else d["individual"],
            "acumulado": d["bolsa_acumulada"] if en_soles else d["acumulado"],
            "detalle_individual": detalle, "detalle_acumulado": texto_acumulado,
        })

    # IPU de cada año: el acumulado al último mes de ese año dentro del
    # período (año completo = Ene–Dic).
    por_anio = []
    for a in anios:
        meses_anio = [m for m in meses if m.year == a and m in ipu]
        if not meses_anio:
            continue
        d = ipu[meses_anio[-1]]
        por_anio.append({"anio": a, "anio_base": a - 1 if evaluacion["desfase"] == 12 else None,
                         "hasta": _mes_corto(meses_anio[-1]), "anio_completo": meses_anio[-1].month == 12,
                         "pct": d["acumulado"] * 100, "bolsa": d["bolsa_acumulada"], "toneladas": d["toneladas_acumuladas"]})
    total = q.inflacion_canasta(inflacion)
    total["variacion"] = total["variacion"] or 0.0
    contexto.update({
        "meses": [_mes_corto(m) for m in meses], "meses_fecha": meses, "anios": anios, "filas": filas, "canasta": canasta,
        "ipu_fila": ipu_fila,
        "grafico": charts.evolucion_ipu(puntos, _mes_corto, formato=charts.soles_corto if en_soles else None),
        # Ver precio con UN material: evolución del precio del mes (línea) y
        # del precio base contra el que se compara (punteada).
        "grafico_precio": charts.evolucion_precio(
            [(r.mes, float(r.precio), float(r.toneladas)) for r in inflacion.itertuples()], _mes_corto,
            base=[(r.mes, float(r.precio_base)) for r in inflacion.itertuples()], ancho=960, alto=320,
        ) if en_precio and len(filas) == 1 else None,
        "resumen": {
            "ultimo_mes": _mes_corto(meses[-1]), "ultima_pct": canasta[-1]["pct"], "ultima_bolsa": canasta[-1]["bolsa"],
            "periodo_pct": total["variacion"] * 100, "bolsa": total["bolsa"], "toneladas": total["toneladas"],
            "desde": _mes_corto(meses[0]), "hasta": _mes_corto(meses[-1]), "por_anio": por_anio,
            "suben": sum(1 for f in filas if f["ultima_pct"] > 0), "bajan": sum(1 for f in filas if f["ultima_pct"] < 0),
            "sustituidas": int(inflacion["base_sustituida"].sum()), "comparaciones": int(len(inflacion)),
            "atipicas": int(inflacion["atipica"].sum()),
        },
        # Bolsa de cada año DENTRO del período (columnas de la tabla).
        "bolsa_anios_total": [q.inflacion_canasta(inflacion[inflacion["anio"] == a])["bolsa"] for a in anios],
        "un_material": len(filas) == 1,
    })
    if len(filas) == 1:
        contexto["detalle"] = [
            {**r, "mes_corto": _mes_corto(r["mes"]), "mes_base_corto": _mes_corto(r["mes_base"]),
             "mes_referencia_corto": _mes_corto(r["mes_referencia"]), "pct": r["variacion"] * 100}
            for r in reversed(inflacion.to_dict("records"))
        ]
    return contexto


# Evaluación de consumo: desviación (en %) a partir de la cual un material se
# marca sobre/bajo el plan, y a partir de la cual el plan futuro se marca como
# alejado del consumo real reciente.
_CONSUMO_TOLERANCIA_MES = 10
_CONSUMO_TOLERANCIA_PLAN = 20
_CONSUMO_MESES_REFERENCIA = 6


def _contexto_consumo(request):
    """Evaluación de consumo (S&OP): Plan de Consumos vs consumo real (MB51)
    del mes elegido por material, el gráfico de consumo real mes a mes contra
    el plan, y si el plan de los próximos meses se aleja del consumo real
    reciente y del mismo período del año anterior. Filtros propios
    (Mes/Categoría/Centro/Material). Pedido por el usuario."""
    plan = q.plan_consumo_sop()
    real = q.consumo_real_mensual_sop()
    if plan.empty:
        return None
    hoy_mes = datetime.date.today().replace(day=1)
    meses_op = sorted({m for m, s in zip(plan["mes"], plan["snapshot"]) if s <= m <= hoy_mes})
    if not meses_op:
        return None
    mes_sel = _por_str(request.GET.get("mes_consumo"), meses_op) or meses_op[-1]
    plan_mes, snapshot = q.plan_consumo_del_mes(mes_sel)

    # Base del mes: los materiales/centros del plan con su consumo real.
    real_mes = real[real["mes"] == mes_sel][["material_codigo", "centro", "consumo"]]
    base = plan_mes.merge(real_mes, on=["material_codigo", "centro"], how="left")
    base["consumo"] = base["consumo"].fillna(0.0)
    nombres = real.drop_duplicates("material_codigo").set_index("material_codigo")
    catalogo = q.catalogo_materiales().drop_duplicates("material_codigo").set_index("material_codigo")
    base["texto_breve"] = [
        nombres["texto_breve"].get(m) or catalogo["texto_breve"].get(m) or d
        for m, d in zip(base["material_codigo"], base["descripcion"])
    ]
    base["categoria"] = [
        (nombres["categoria"].get(m) if m in nombres.index else None) or catalogo["categoria"].get(m) or "MATERIAS PRIMAS"
        for m in base["material_codigo"]
    ]
    base["categoria"] = base["categoria"].fillna("MATERIAS PRIMAS")

    categorias_op, _ = _opciones_categoria_centro(base)
    categoria_sel = request.GET.get("categoria_consumo", "Todas")
    if categoria_sel not in categorias_op:
        categoria_sel = "Todas"
    por_categoria = base if categoria_sel == "Todas" else base[base["categoria"] == categoria_sel]
    _, centros_op = _opciones_categoria_centro(por_categoria)
    centro_sel = request.GET.get("centro_consumo", "Todos")
    if centro_sel not in centros_op:
        centro_sel = "Todos"
    por_centro = por_categoria if centro_sel == "Todos" else por_categoria[por_categoria["centro"] == centro_sel]
    materiales_op = _opciones_material(por_centro)
    material_sel = _selector_material(request, "consumo", materiales_op)
    df = _por_materiales(por_centro, material_sel)
    claves = set(zip(df["material_codigo"], df["centro"]))

    # Mes en curso: se compara también contra el plan prorrateado a la fecha
    # de la última carga de MB51 (el mes todavía no cierra).
    en_curso = mes_sel == hoy_mes
    ultima = q._query("SELECT MAX(fecha_contabilizacion) AS f FROM gestion_movimientomaterial")["f"].iloc[0]
    fecha_corte = datetime.date.fromisoformat(str(ultima)[:10])
    ultimo_dia_mes_actual = hoy_mes.replace(day=calendar.monthrange(hoy_mes.year, hoy_mes.month)[1])
    fraccion = 1.0
    if en_curso:
        fraccion = min(fecha_corte.day / ultimo_dia_mes_actual.day, 1.0) if fecha_corte >= mes_sel else 0.0

    # Referencia: promedio real de los 6 meses completos anteriores.
    desde_ref = q._sumar_meses(mes_sel, -_CONSUMO_MESES_REFERENCIA)
    ref = real[(real["mes"] >= desde_ref) & (real["mes"] < mes_sel)]
    promedio_ref = (ref.groupby(["material_codigo", "centro"])["consumo"].sum() / _CONSUMO_MESES_REFERENCIA).to_dict()
    anio_anterior = real[real["mes"] == q._sumar_meses(mes_sel, -12)].set_index(["material_codigo", "centro"])["consumo"].to_dict()

    def estado(pct):
        if pct is None:
            return ""
        if pct > 100 + _CONSUMO_TOLERANCIA_MES:
            return "sobre"
        if pct < 100 - _CONSUMO_TOLERANCIA_MES:
            return "bajo"
        return "en_linea"

    filas = []
    for r in df.itertuples():
        plan_fecha = r.plan * fraccion
        cumplimiento = r.consumo / r.plan * 100 if r.plan else None
        cumplimiento_fecha = r.consumo / plan_fecha * 100 if plan_fecha else None
        clave = (r.material_codigo, r.centro)
        filas.append({
            "material_codigo": r.material_codigo, "texto_breve": r.texto_breve, "centro": r.centro, "categoria": r.categoria,
            "plan": r.plan, "real": r.consumo, "diferencia": r.consumo - r.plan, "cumplimiento": cumplimiento,
            "plan_fecha": plan_fecha, "cumplimiento_fecha": cumplimiento_fecha,
            "estado": estado(cumplimiento_fecha if en_curso else cumplimiento),
            "promedio_ref": promedio_ref.get(clave), "anio_anterior": anio_anterior.get(clave),
            "plan_vs_promedio": (r.plan / promedio_ref[clave] - 1) * 100 if promedio_ref.get(clave) else None,
        })
    filas.sort(key=lambda f: -abs(f["diferencia"]))
    total_plan, total_real = sum(f["plan"] for f in filas), sum(f["real"] for f in filas)
    total_fecha = total_plan * fraccion

    # Gráfico: consumo real mes a mes (desde 2025) vs el plan del último S&OP,
    # de los materiales/centros elegidos.
    ultimo_snapshot = max(plan["snapshot"])
    plan_ultimo = plan[plan["snapshot"] == ultimo_snapshot]
    plan_sel = plan_ultimo[[k in claves for k in zip(plan_ultimo["material_codigo"], plan_ultimo["centro"])]]
    en_claves = pd.Series([k in claves for k in zip(real["material_codigo"], real["centro"])], index=real.index, dtype=bool)
    real_sel = real[en_claves & (real["mes"] >= datetime.date(2025, 1, 1))]
    serie_real = [(m, float(v)) for m, v in real_sel.groupby("mes")["consumo"].sum().items() if m <= hoy_mes]
    serie_plan = [(m, float(v)) for m, v in plan_sel.groupby("mes")["plan"].sum().items()]
    grafico = charts.evolucion_consumo(serie_real, serie_plan, _mes_corto,
                                       mes_parcial=hoy_mes if fecha_corte < ultimo_dia_mes_actual else None)

    # Plan futuro vs consumo real: próximos 6 meses del último S&OP contra el
    # promedio de los últimos 6 meses cerrados y el mismo período un año antes.
    futuros = sorted(m for m in plan_sel["mes"].unique() if m > hoy_mes)[:_CONSUMO_MESES_REFERENCIA]
    cerrados = [q._sumar_meses(hoy_mes, -i) for i in range(_CONSUMO_MESES_REFERENCIA, 0, -1)]
    anterior = [q._sumar_meses(m, -12) for m in futuros]
    futuro = []
    if futuros:
        nombres_plan = {(r.material_codigo, r.centro): r.texto_breve for r in df.itertuples()}
        for (material, centro), g in plan_sel[plan_sel["mes"].isin(futuros)].groupby(["material_codigo", "centro"]):
            del_material = real[(real["material_codigo"] == material) & (real["centro"] == centro)]
            plan_prom = float(g["plan"].sum()) / len(futuros)
            reciente = float(del_material[del_material["mes"].isin(cerrados)]["consumo"].sum()) / len(cerrados)
            hace_un_anio = float(del_material[del_material["mes"].isin(anterior)]["consumo"].sum()) / len(anterior)
            vs_reciente = (plan_prom / reciente - 1) * 100 if reciente > 0 else None
            futuro.append({
                "material_codigo": material, "centro": centro,
                "texto_breve": nombres_plan.get((material, centro)) or g["descripcion"].iloc[0],
                "plan_promedio": plan_prom, "real_reciente": reciente, "real_anio_anterior": hace_un_anio,
                "vs_reciente": vs_reciente,
                "vs_anio_anterior": (plan_prom / hace_un_anio - 1) * 100 if hace_un_anio > 0 else None,
                "alerta": vs_reciente is not None and abs(vs_reciente) > _CONSUMO_TOLERANCIA_PLAN,
                "sin_historia": reciente <= 0,
            })
        futuro.sort(key=lambda f: -abs(f["vs_reciente"] or 0))

    return {
        "meses": meses_op, "mes_sel": mes_sel, "snapshot": snapshot, "ultimo_snapshot": ultimo_snapshot,
        "categorias": categorias_op, "categoria_sel": categoria_sel,
        "centros": centros_op, "centro_sel": centro_sel,
        "materiales": materiales_op, "material_sel": material_sel,
        "en_curso": en_curso, "fecha_corte": fecha_corte, "fraccion_pct": fraccion * 100,
        "filas": filas, "total_plan": total_plan, "total_real": total_real, "total_plan_fecha": total_fecha,
        "total_cumplimiento": total_real / total_plan * 100 if total_plan else None,
        "total_cumplimiento_fecha": total_real / total_fecha * 100 if total_fecha else None,
        "n_sobre": sum(1 for f in filas if f["estado"] == "sobre"), "n_bajo": sum(1 for f in filas if f["estado"] == "bajo"),
        "tolerancia_mes": _CONSUMO_TOLERANCIA_MES, "tolerancia_plan": _CONSUMO_TOLERANCIA_PLAN,
        "meses_referencia": _CONSUMO_MESES_REFERENCIA,
        "grafico": grafico,
        "futuro": futuro, "n_alertas_plan": sum(1 for f in futuro if f["alerta"]),
        "futuro_desde": futuros[0] if futuros else None, "futuro_hasta": futuros[-1] if futuros else None,
        "cerrados_desde": cerrados[0], "cerrados_hasta": cerrados[-1],
    }


_FACTOR_TOLERANCIA = 15
_FACTOR_MESES_REFERENCIA = 12
_PRODUCTOS_TEXTO = {"clinker": "Clinker", "cemento": "Cemento"}


def _kg_por_t(factor):
    return f"{factor * 1000:,.0f} kg/T"


def _contexto_consumo_especifico(request):
    """Consumo por tonelada producida (ver queries.consumo_por_tonelada_sop):
    cuántos kg de cada material se gastan por T de clinker o de cemento, y si
    la diferencia de consumo del mes contra su promedio viene de producir más
    (efecto producción) o de gastar más por tonelada (efecto consumo por T).
    Además, qué producción supone el plan de consumos de los próximos meses.
    Filtros propios (Mes/Centro/Producto/Material). Pedido por el usuario."""
    base = q.consumo_por_tonelada_sop()
    produccion = q.produccion_mensual_planta()
    if base.empty:
        return None
    hoy_mes = datetime.date.today().replace(day=1)
    meses_op = sorted(m for m in base["mes"].unique() if m <= hoy_mes)
    if not meses_op:
        return None
    mes_sel = _por_str(request.GET.get("mes_factor"), meses_op) or meses_op[-1]

    centros_op = ["Todos"] + sorted(base["centro"].unique().tolist())
    centro_sel = request.GET.get("centro_factor", "Todos")
    if centro_sel not in centros_op:
        centro_sel = "Todos"
    por_centro = base if centro_sel == "Todos" else base[base["centro"] == centro_sel]
    productos_op = [{"valor": "Todos", "texto": "Todos"}] + [
        {"valor": p, "texto": _PRODUCTOS_TEXTO[p]} for p in ("clinker", "cemento") if p in set(por_centro["producto"])
    ]
    producto_sel = request.GET.get("producto_factor", "Todos")
    if producto_sel not in {p["valor"] for p in productos_op}:
        producto_sel = "Todos"
    por_producto = por_centro if producto_sel == "Todos" else por_centro[por_centro["producto"] == producto_sel]
    materiales_op = _opciones_material(por_producto)
    material_sel = _selector_material(request, "factor", materiales_op)
    df = _por_materiales(por_producto, material_sel)

    # Mes en curso sin cerrar: el promedio de referencia se prorratea a la
    # fecha de la última carga de MB51 (si no, todo sale como "producción baja").
    ultima = q._query("SELECT MAX(fecha_contabilizacion) AS f FROM gestion_movimientomaterial")["f"].iloc[0]
    fecha_corte = datetime.date.fromisoformat(str(ultima)[:10])
    ultimo_dia = mes_sel.replace(day=calendar.monthrange(mes_sel.year, mes_sel.month)[1])
    fraccion = min(fecha_corte.day / ultimo_dia.day, 1.0) if mes_sel == hoy_mes and fecha_corte < ultimo_dia else 1.0

    desde_12 = q._sumar_meses(mes_sel, -_FACTOR_MESES_REFERENCIA)
    desde_6 = q._sumar_meses(mes_sel, -_CONSUMO_MESES_REFERENCIA)
    desde_3 = q._sumar_meses(mes_sel, -2)
    filas = []
    for (material, centro), g in df.groupby(["material_codigo", "centro"]):
        actual = g[g["mes"] == mes_sel]
        if actual.empty:
            continue
        r = actual.iloc[0]
        previos12 = g[(g["mes"] >= desde_12) & (g["mes"] < mes_sel)]
        previos6 = g[(g["mes"] >= desde_6) & (g["mes"] < mes_sel)]
        ultimos3 = g[(g["mes"] >= desde_3) & (g["mes"] <= mes_sel)]
        f12 = previos12["consumo"].sum() / previos12["produccion"].sum() if not previos12.empty else None
        f3 = ultimos3["consumo"].sum() / ultimos3["produccion"].sum()
        fila = {
            "material_codigo": material, "texto_breve": r["texto_breve"], "centro": centro,
            "producto": r["producto"], "producto_texto": _PRODUCTOS_TEXTO[r["producto"]],
            "participacion": r["participacion"] * 100,
            "consumo": r["consumo"], "produccion": r["produccion"], "factor": r["factor"] * 1000,
            "factor_3": f3 * 1000, "factor_12": f12 * 1000 if f12 else None,
            "var_3_vs_12": (f3 / f12 - 1) * 100 if f12 else None,
            "consumo_ref": None, "diferencia": None, "efecto_produccion": None, "efecto_factor": None,
        }
        fila["estado"] = (
            "" if fila["var_3_vs_12"] is None else
            "alto" if fila["var_3_vs_12"] > _FACTOR_TOLERANCIA else
            "bajo" if fila["var_3_vs_12"] < -_FACTOR_TOLERANCIA else "normal"
        )
        # Diferencia de consumo contra el promedio de los 6 meses anteriores
        # = efecto producción + efecto consumo por T (suman exacto):
        # (P − Pref) × Fref  +  (F − Fref) × P.
        if not previos6.empty:
            p_ref = previos6["produccion"].sum() / len(previos6) * fraccion
            c_ref = previos6["consumo"].sum() / len(previos6) * fraccion
            f_ref = c_ref / p_ref
            fila.update({
                "consumo_ref": c_ref, "diferencia": r["consumo"] - c_ref,
                "efecto_produccion": (r["produccion"] - p_ref) * f_ref,
                "efecto_factor": (r["factor"] - f_ref) * r["produccion"],
            })
        filas.append(fila)
    filas.sort(key=lambda f: (f["centro"], f["producto"], -f["consumo"]))

    # Producción de la selección (clinker y cemento), mes a mes.
    centros_sel = centros_op[1:] if centro_sel == "Todos" else [centro_sel]
    prod_sel = produccion[produccion["centro"].isin(centros_sel)]
    prod_mes = prod_sel.groupby(["producto", "mes"])["produccion"].sum()
    resumen_produccion = []
    for producto in ("clinker", "cemento"):
        if producto_sel not in ("Todos", producto) or producto not in prod_mes.index.get_level_values(0):
            continue
        serie = prod_mes.loc[producto]
        previos = serie[(serie.index >= desde_6) & (serie.index < mes_sel)]
        actual = float(serie.get(mes_sel, 0.0))
        promedio = float(previos.mean()) * fraccion if not previos.empty else None
        resumen_produccion.append({
            "producto": producto, "texto": _PRODUCTOS_TEXTO[producto], "actual": actual, "promedio": promedio,
            "var": (actual / promedio - 1) * 100 if promedio else None,
        })

    # Gráfico: con un material, su consumo por T mes a mes (una línea por
    # centro); si no, la producción de clinker y cemento de la selección.
    desde_grafico = datetime.date(2024, 1, 1)
    claves = df[["material_codigo", "centro"]].drop_duplicates()
    if len(material_sel) == 1 and len(claves) <= 2:
        series = []
        for i, ((material, centro), g) in enumerate(df.groupby(["material_codigo", "centro"])):
            g = g[(g["mes"] >= desde_grafico) & (g["mes"] <= hoy_mes)].sort_values("mes")
            producto = _PRODUCTOS_TEXTO[g["producto"].iloc[0]].lower() if not g.empty else ""
            series.append({
                "nombre": f"{q.nombre_centro(centro)} · kg por T de {producto}", "clase": f"serie-{i}",
                "puntos": [(r.mes, r.factor * 1000,
                            f"{_mes_corto(r.mes)} · {q.nombre_centro(centro)}: {_kg_por_t(r.factor)} de {producto}\n"
                            f"Consumo {r.consumo:,.0f} T · Producción de {producto} {r.produccion:,.0f} T")
                           for r in g.itertuples()],
            })
        referencias = [{"valor": f["factor_12"], "texto": f"Prom. 12 meses {q.nombre_centro(f['centro'])}: {f['factor_12']:,.0f} kg/T",
                        "clase": f"serie-{i}"} for i, f in enumerate(filas) if f["factor_12"]]
        grafico = charts.lineas_mensuales(series, _mes_corto, lambda v: f"{v:,.0f}", referencias=referencias)
        titulo_grafico = "Consumo por tonelada producida, mes a mes (kg por T)"
    else:
        series = []
        for i, producto in enumerate(("clinker", "cemento")):
            if producto_sel not in ("Todos", producto) or producto not in prod_mes.index.get_level_values(0):
                continue
            serie = prod_mes.loc[producto]
            serie = serie[(serie.index >= desde_grafico) & (serie.index <= hoy_mes)]
            series.append({
                "nombre": f"{_PRODUCTOS_TEXTO[producto]} producido", "clase": f"serie-{i}",
                "puntos": [(m, float(v), f"{_mes_corto(m)} · {_PRODUCTOS_TEXTO[producto]} producido "
                                         f"({' + '.join(map(q.nombre_centro, centros_sel))}): {v:,.0f} T") for m, v in serie.items()],
            })
        grafico = charts.lineas_mensuales(series, _mes_corto, charts._toneladas_corto, desde_cero=True)
        titulo_grafico = f"Producción mensual de clinker y cemento · {' + '.join(map(q.nombre_centro, centros_sel))} (T)"

    # ¿Qué producción supone el plan? Plan de consumos de los próximos 6
    # meses ÷ consumo por T de los últimos 12 meses = producción de clinker o
    # cemento que ese plan da por hecha. Todos los materiales de una misma
    # planta y producto deberían suponer más o menos la misma producción.
    plan = q.plan_consumo_sop()
    implicita = []
    if not plan.empty:
        ultimo_snapshot = max(plan["snapshot"])
        futuro = plan[(plan["snapshot"] == ultimo_snapshot) & (plan["mes"] > hoy_mes)]
        meses_futuros = sorted(futuro["mes"].unique())[:_CONSUMO_MESES_REFERENCIA]
        futuro = futuro[futuro["mes"].isin(meses_futuros)]
        ultimo_mes = meses_op[-1]
        cerrados = [q._sumar_meses(hoy_mes, -i) for i in range(_CONSUMO_MESES_REFERENCIA, 0, -1)]
        for (material, centro), g in futuro.groupby(["material_codigo", "centro"]):
            hist = df[(df["material_codigo"] == material) & (df["centro"] == centro)]
            ref = hist[(hist["mes"] > q._sumar_meses(ultimo_mes, -_FACTOR_MESES_REFERENCIA)) & (hist["mes"] <= ultimo_mes)]
            if ref.empty or ref["produccion"].sum() <= 0:
                continue
            factor = ref["consumo"].sum() / ref["produccion"].sum()
            producto = ref["producto"].iloc[0]
            real_prod = prod_sel[(prod_sel["centro"] == centro) & (prod_sel["producto"] == producto) & (prod_sel["mes"].isin(cerrados))]
            prod_reciente = float(real_prod["produccion"].mean()) if not real_prod.empty else None
            plan_prom = float(g["plan"].sum()) / len(meses_futuros)
            prod_plan = plan_prom / factor if factor > 0 else None
            dif = (prod_plan / prod_reciente - 1) * 100 if prod_plan and prod_reciente else None
            implicita.append({
                "material_codigo": material, "texto_breve": ref["texto_breve"].iloc[0], "centro": centro,
                "producto_texto": _PRODUCTOS_TEXTO[producto], "plan_promedio": plan_prom, "factor_12": factor * 1000,
                "produccion_plan": prod_plan, "produccion_reciente": prod_reciente, "diferencia": dif,
                "alerta": dif is not None and abs(dif) > _CONSUMO_TOLERANCIA_PLAN,
            })
        implicita.sort(key=lambda f: (f["centro"], f["producto_texto"], -(f["diferencia"] or 0)))
        futuro_desde = meses_futuros[0] if meses_futuros else None
        futuro_hasta = meses_futuros[-1] if meses_futuros else None
    else:
        futuro_desde = futuro_hasta = ultimo_snapshot = None

    return {
        "meses": meses_op, "mes_sel": mes_sel,
        "centros": centros_op, "centro_sel": centro_sel,
        "productos": productos_op, "producto_sel": producto_sel,
        "materiales": materiales_op, "material_sel": material_sel,
        "parcial": fraccion < 1.0, "fecha_corte": fecha_corte,
        "filas": filas, "resumen_produccion": resumen_produccion,
        "n_alto": sum(1 for f in filas if f["estado"] == "alto"), "n_bajo": sum(1 for f in filas if f["estado"] == "bajo"),
        "efecto_produccion_total": sum(f["efecto_produccion"] or 0 for f in filas),
        "efecto_factor_total": sum(f["efecto_factor"] or 0 for f in filas),
        "grafico": grafico, "titulo_grafico": titulo_grafico,
        "implicita": implicita, "n_alertas_plan": sum(1 for f in implicita if f["alerta"]),
        "ultimo_snapshot": ultimo_snapshot, "futuro_desde": futuro_desde, "futuro_hasta": futuro_hasta,
        "tolerancia": _FACTOR_TOLERANCIA, "tolerancia_plan": _CONSUMO_TOLERANCIA_PLAN,
        "meses_referencia": _CONSUMO_MESES_REFERENCIA, "meses_factor": _FACTOR_MESES_REFERENCIA,
    }


def _contexto_proyeccion_sop(request):
    """Evolución de inventario proyectada (S&OP): inventario de cierre del mes
    anterior + Abastecimiento − Consumo, mes a mes (ver
    queries.proyeccion_inventario_sop). S&OP/Categoría/Centro/Material
    propios, en cascada igual que Cobertura. Pedido por el usuario."""
    snapshots = q.snapshots_sop_disponibles()
    if not snapshots:
        return None
    snapshot_sel = _por_str(request.GET.get("sop_proyeccion"), snapshots) or snapshots[-1]
    base = q.proyeccion_inventario_sop(snapshot_sel)
    fecha_inicial = q.fecha_inventario_inicial_sop(snapshot_sel)
    contexto = {
        "snapshot": snapshot_sel, "opciones_snapshot": snapshots, "fecha_inicial": fecha_inicial,
        "categorias": ["Todas"], "categoria_sel": "Todas", "centros": ["Todos"], "centro_sel": "Todos",
        "materiales": [{"codigo": "Todos", "texto": "Todos"}], "material_sel": [],
        "grafico": None, "meses": [], "resumen": [], "matriz": [],
    }
    if base.empty:
        return contexto

    categorias_op, _ = _opciones_categoria_centro(base)
    categoria_sel = request.GET.get("categoria_proyeccion", "Todas")
    if categoria_sel not in categorias_op:
        categoria_sel = "Todas"
    por_categoria = base if categoria_sel == "Todas" else base[base["categoria"] == categoria_sel]
    _, centros_op = _opciones_categoria_centro(por_categoria)
    centro_sel = request.GET.get("centro_proyeccion", "Todos")
    if centro_sel not in centros_op:
        centro_sel = "Todos"
    por_centro = por_categoria if centro_sel == "Todos" else por_categoria[por_categoria["centro"] == centro_sel]
    materiales_op = _opciones_material(por_centro)
    material_sel = _selector_material(request, "proyeccion", materiales_op)
    df = _por_materiales(por_centro, material_sel)

    meses = sorted(df["mes"].unique())
    # Traslado interno: si en la selección están el centro que envía y el que
    # recibe (p.ej. Carbón Importado Atocongo -> Condorcocha), el traslado no
    # es ni abastecimiento ni salida para el total: se descuenta de los dos.
    materiales_por_centro = df.groupby("material_codigo")["centro"].nunique()
    con_ambos = df["material_codigo"].map(materiales_por_centro) > 1
    df = df.assign(traslado_interno=df["traslado_recibido"].where(con_ambos, 0.0))
    totales = df.groupby("mes", as_index=False)[
        ["stock_inicial", "abastecimiento", "consumo", "traslado", "traslado_interno", "stock_final"]
    ].sum()
    resumen = [
        {"mes": r.mes, "stock_inicial": r.stock_inicial,
         "abastecimiento": r.abastecimiento - r.traslado_interno, "consumo": r.consumo,
         "traslado": r.traslado - r.traslado_interno, "stock_final": r.stock_final}
        for r in totales.sort_values("mes").itertuples()
    ]
    hay_traslados = any(abs(r["traslado"]) > 0.5 for r in resumen)
    etiqueta_inicial = _mes_corto(fecha_inicial) if fecha_inicial else "Inicio"
    puntos = [(etiqueta_inicial, resumen[0]["stock_inicial"])] + [(_mes_corto(r["mes"]), r["stock_final"]) for r in resumen]

    # Con UN solo material/centro seleccionado, la banda mínimo–máximo va en el
    # gráfico y en el Detalle mensual (sumar mínimos de materiales distintos
    # no tiene sentido). El punto de partida se compara con el mínimo/máximo
    # del primer mes.
    grupos = df.groupby(["material_codigo", "centro"])
    un_material = grupos.ngroups == 1
    banda = None
    if un_material:
        unico = df.sort_values("mes")
        limites = [
            (None if mi != mi else float(mi), None if ma != ma else float(ma))
            for mi, ma in zip(unico["stock_minimo"], unico["stock_maximo"])
        ]
        if any(mi is not None or ma is not None for mi, ma in limites):
            banda = [limites[0]] + limites
        for fila, (mi, ma), estado in zip(resumen, limites, unico["estado"]):
            fila.update({"stock_minimo": mi, "stock_maximo": ma, "estado": estado,
                         "estado_texto": q.ESTADOS_STOCK[estado]})

    # Recuadro al pasar el mouse por cada punto/etiqueta del gráfico: de dónde
    # sale el stock de ese mes y, con un material, su mínimo/máximo; con
    # varios, cuántos quedan bajo el mínimo o sobre el máximo. Pedido por el
    # usuario.
    def toneladas(valor):
        return f"{valor:,.0f} T"

    detalles = [
        f"Stock de partida{f' (cierre {fecha_inicial:%d/%m/%Y})' if fecha_inicial else ''}: "
        f"{toneladas(resumen[0]['stock_inicial'])}"
    ]
    estados_por_mes = {} if un_material else {
        mes: g["estado"].value_counts().to_dict() for mes, g in df.groupby("mes")
    }
    for r in resumen:
        variacion = r["stock_final"] - r["stock_inicial"]
        lineas = [
            f"{_mes_corto(r['mes'])} · Stock final proyectado: {toneladas(r['stock_final'])}",
            f"Stock inicial: {toneladas(r['stock_inicial'])}",
            f"+ Abastecimiento: {toneladas(r['abastecimiento'])}",
            f"− Consumo: {toneladas(r['consumo'])}",
        ]
        if hay_traslados:
            lineas.append(f"− Traslados a otro centro: {toneladas(r['traslado'])}")
        lineas.append(f"Variación del mes: {'+' if variacion >= 0 else '−'}{toneladas(abs(variacion))}")
        if un_material:
            if r.get("stock_minimo") is not None or r.get("stock_maximo") is not None:
                lineas.append(
                    f"Mínimo {toneladas(r['stock_minimo']) if r.get('stock_minimo') is not None else '—'} · "
                    f"Máximo {toneladas(r['stock_maximo']) if r.get('stock_maximo') is not None else '—'}"
                )
            if r.get("estado_texto"):
                lineas.append(f"Estado: {r['estado_texto']}")
        else:
            conteo = estados_por_mes.get(r["mes"], {})
            texto = (f"Materiales ({sum(conteo.values())}): {conteo.get('quiebre', 0)} en quiebre · "
                     f"{conteo.get('bajo_minimo', 0)} bajo mínimo · {conteo.get('en_rango', 0)} en rango · "
                     f"{conteo.get('sobre_maximo', 0)} sobre máximo")
            if conteo.get("sin_parametro"):
                texto += f" · {conteo['sin_parametro']} sin mín/máx"
            lineas.append(texto)
        detalles.append("\n".join(lineas))

    # Matriz: una fila por material/centro con el stock final de cada mes y su
    # estado frente al mínimo/máximo (color de la celda).
    matriz = []
    for (material, centro), grupo in grupos:
        grupo = grupo.sort_values("mes")
        negativos = grupo[grupo["stock_final"] < 0]
        minimo, maximo = grupo["stock_minimo"].iloc[0], grupo["stock_maximo"].iloc[0]
        matriz.append({
            "material_codigo": material, "texto_breve": grupo["texto_breve"].iloc[0], "centro": centro,
            "stock_inicial": float(grupo["stock_inicial"].iloc[0]),
            "stock_minimo": None if minimo != minimo else float(minimo),
            "stock_maximo": None if maximo != maximo else float(maximo),
            "celdas": [{"valor": float(v), "estado": e} for v, e in zip(grupo["stock_final"], grupo["estado"])],
            "quiebre": negativos["mes"].iloc[0] if not negativos.empty else None,
            "quiebre_corto": _mes_corto(negativos["mes"].iloc[0]) if not negativos.empty else "",
        })
    matriz.sort(key=lambda f: (f["quiebre"] is None, f["quiebre"] or datetime.date.max, f["centro"], f["material_codigo"]))

    # Diagnóstico de mínimos/máximos, con los mismos filtros de la sección.
    diagnostico = q.diagnostico_minmax_sop(snapshot_sel)
    if not diagnostico.empty:
        claves = set(zip(df["material_codigo"], df["centro"]))
        diagnostico = diagnostico[[k in claves for k in zip(diagnostico["material_codigo"], diagnostico["centro"])]]
    filas_diag = [] if diagnostico.empty else [
        {**r, "primer_mes_corto": _mes_corto(r["primer_mes_bajo_minimo"]) if r["primer_mes_bajo_minimo"] else "",
         "pedir_corto": (
             "" if not r["pedir_a_mas_tardar"] else
             f"{_mes_corto(snapshot_sel)} (ya)" if r["pedir_a_mas_tardar"] <= snapshot_sel else
             _mes_corto(r["pedir_a_mas_tardar"])
         ),
         "stock_minimo": None if r["stock_minimo"] != r["stock_minimo"] else r["stock_minimo"],
         "stock_maximo": None if r["stock_maximo"] != r["stock_maximo"] else r["stock_maximo"]}
        for r in diagnostico.to_dict("records")
    ]
    conteo_diag = {}
    for f in filas_diag:
        conteo_diag[f["diagnostico"]] = conteo_diag.get(f["diagnostico"], 0) + 1

    contexto.update({
        "categorias": categorias_op, "categoria_sel": categoria_sel,
        "centros": centros_op, "centro_sel": centro_sel,
        "materiales": materiales_op, "material_sel": material_sel,
        "meses": [{"fecha": m, "corto": _mes_corto(m)} for m in meses],
        "resumen": resumen, "matriz": matriz, "un_material": un_material,
        "stock_partida": resumen[0]["stock_inicial"], "stock_cierre": resumen[-1]["stock_final"],
        "mes_cierre": meses[-1], "con_quiebre": sum(1 for f in matriz if f["quiebre"]),
        "diagnostico": filas_diag,
        "n_sobrecubiertos": conteo_diag.get("sobrecubierto", 0) + conteo_diag.get("sobre_maximo_puntual", 0),
        "n_bajo_minimo": conteo_diag.get("bajo_minimo", 0) + conteo_diag.get("quiebre", 0),
        "recorte_total": sum(f["recorte_posible"] for f in filas_diag if f["diagnostico"] in ("sobrecubierto", "sobre_maximo_puntual")),
        "faltante_total": sum(f["faltante"] for f in filas_diag),
        "deficit_plazo_total": sum(f["deficit_en_plazo"] for f in filas_diag),
        "hay_traslados": hay_traslados,
        "grafico": charts.evolucion_stock(
            puntos, barras=[None] + [(r["abastecimiento"], r["consumo"] + r["traslado"]) for r in resumen], banda=banda,
            detalles=detalles,
        ),
    })
    return contexto


@login_required
def categoria_detalle(request, slug):
    categorias_nav, categorias = _construir_nav(slug)
    mapa_slug = {_slug(c): c for c in categorias}
    categoria = mapa_slug.get(slug)
    if categoria is None:
        raise Http404("Categoría no encontrada")

    fechas_inv = q.fechas_inventario_disponibles()
    anios_compras = q.anios_compras_disponibles()

    contexto = {"categoria": categoria, "categorias_nav": categorias_nav, "slug": slug}

    if not fechas_inv and not anios_compras:
        contexto["sin_datos"] = True
        return render(request, "gestion/categoria_detalle.html", contexto)

    # --- Compras: una fila por año disponible, con variación interanual (YoY) ---
    compras_filas = []
    valor_anio_anterior = None
    for anio in anios_compras:
        df = q.compras_por_categoria_contrato(anio)
        fila = df[df["categoria"] == categoria]
        if fila.empty:
            continue
        valor_total = float(fila["valor_total"].iloc[0] or 0)
        valor_bajo_contrato = float(fila["valor_bajo_contrato"].iloc[0] or 0)
        pct_contrato = (valor_bajo_contrato / valor_total * 100) if valor_total else 0
        pmp_df = q.pmp_por_categoria(anio)
        fila_pmp = pmp_df[pmp_df["categoria"] == categoria]
        pmp = float(fila_pmp["pmp"].iloc[0]) if not fila_pmp.empty else None
        compras_filas.append({
            "anio": anio, "valor_total": valor_total,
            "pct_contrato": round(pct_contrato, 1),
            "pmp": round(pmp, 0) if pmp is not None else None,
            "variacion_yoy_pct": (
                round((valor_total - valor_anio_anterior) / valor_anio_anterior * 100, 1)
                if valor_anio_anterior else None
            ),
        })
        valor_anio_anterior = valor_total
    contexto["compras"] = compras_filas

    # Categorías que alguna vez tienen inventario (CAPEX, Servicios Administrativos
    # y Servicios Industriales son solo de compras/servicios y nunca lo tienen) —
    # se usa para Fill Rate y para ocultar toda la sección de inventario más abajo.
    categoria_tiene_inventario = categoria in set(q.categorias_inventario_disponibles())

    # --- Concentración de Proveedores (Pareto), Fill Rate y Lead Time: año más
    # reciente, cada uno con su propio segmentador de Mes (independiente) ---
    anio_indicadores = anios_compras[-1] if anios_compras else None
    if anio_indicadores:
        contexto["anio_indicadores"] = anio_indicadores

        rango_pareto = _rango_meses(request, "pareto", anio_indicadores)
        pareto_todas = q.concentracion_proveedores_por_categoria_rango(
            rango_pareto["fecha_ini"], rango_pareto["fecha_fin"]
        )
        contexto["pareto"] = next((r for r in pareto_todas if r["categoria"] == categoria), None)
        contexto["rango_pareto"] = rango_pareto

        # Fill Rate: solo si esta categoría tiene inventario (a pedido del usuario).
        if categoria_tiene_inventario:
            rango_fillrate = _rango_meses(request, "fillrate", anio_indicadores)
            fill_rate_todas = q.fill_rate_por_categoria_rango(rango_fillrate["fecha_ini"], rango_fillrate["fecha_fin"])
            contexto["fill_rate"] = next((r for r in fill_rate_todas if r["categoria"] == categoria), None)
            contexto["rango_fillrate"] = rango_fillrate
        else:
            contexto["fill_rate"] = None

        # --- Lead Time de proveedores: fecha de pedido -> Entrada de Mercancía real ---
        rango_leadtime = _rango_meses(request, "leadtime", anio_indicadores)
        lead_time_todas = q.lead_time_por_categoria_rango(rango_leadtime["fecha_ini"], rango_leadtime["fecha_fin"])
        contexto["lead_time"] = next((r for r in lead_time_todas if r["categoria"] == categoria), None)
        contexto["rango_leadtime"] = rango_leadtime
    else:
        contexto["pareto"] = None
        contexto["fill_rate"] = None
        contexto["lead_time"] = None

    # --- Evolución de PMP (esta categoría) ---
    pmp_serie_cat = q.pmp_mensual(categoria)
    puntos_pmp_cat = [(_mes_abrev_guion(row["mes"]), float(row["pmp"])) for _, row in pmp_serie_cat.iterrows()]
    contexto["pmp_evolucion"] = {
        "grafico": charts.serie_area(puntos_pmp_cat, formato_valor=lambda v: f"{v:.0f}"),
    }

    # Sin inventario: ni globalmente (fechas_inv vacío) ni para esta categoría en
    # particular (CAPEX/Servicios Administrativos/Servicios Industriales) — se
    # oculta toda la sección de indicadores de inventario (Evolución, Inventario,
    # ABC, Obsoletos, Rotación, Críticos, Variación), no tiene sentido mostrarla.
    if not fechas_inv or not categoria_tiene_inventario:
        contexto["sin_inventario"] = True
        return render(request, "gestion/categoria_detalle.html", contexto)

    # --- Evolución de inventario ---
    anio_max = fechas_inv[-1].year
    fechas_anio_max = [f for f in fechas_inv if f.year == anio_max]
    default_inicio_evo = fechas_anio_max[0] if fechas_anio_max else fechas_inv[0]

    mes_inicio_evo = _por_str(request.GET.get("mes_inicio_evo"), fechas_inv) or default_inicio_evo
    opciones_fin_evo = [f for f in fechas_inv if f >= mes_inicio_evo]
    mes_fin_evo = _por_str(request.GET.get("mes_fin_evo"), opciones_fin_evo) or opciones_fin_evo[-1]

    serie_cat = q.valor_inventario_mensual_por_categoria(fecha_desde=mes_inicio_evo)
    serie = serie_cat[
        (serie_cat["categoria"] == categoria)
        & (serie_cat["fecha"] >= mes_inicio_evo)
        & (serie_cat["fecha"] <= mes_fin_evo)
    ].sort_values("fecha")
    puntos = [(_mes_abrev_guion(row["fecha"]), float(row["valor_total"])) for _, row in serie.iterrows()]

    contexto["evolucion"] = {
        "grafico": charts.serie_area(puntos),
        "mes_inicio": mes_inicio_evo, "mes_fin": mes_fin_evo,
        "opciones_inicio": fechas_inv, "opciones_fin": opciones_fin_evo,
    }

    # --- Inventario (foto del mes: métricas, familias, top materiales) ---
    mes_cierre = _por_str(request.GET.get("mes_cierre"), fechas_inv) or fechas_inv[-1]
    inv_mat = q.inventario_por_material(mes_cierre)
    inv = inv_mat[inv_mat["categoria"] == categoria]

    familias_top, centros_top = _opciones_familia_centro(inv)
    familia_top_sel, centro_top_sel = _selector_familia_centro(request, "top", familias_top, centros_top)
    inv_top = inv
    if familia_top_sel != "Todas":
        inv_top = inv_top[inv_top["familia"] == familia_top_sel]
    if centro_top_sel != "Todos":
        inv_top = inv_top[inv_top["centro"] == centro_top_sel]

    familias_df = (
        inv.groupby("familia", as_index=False)["valor_total"].sum()
        .sort_values("valor_total", ascending=False).head(10)
    )
    familias_items = charts.barras([
        {"etiqueta": r["familia"], "valor": float(r["valor_total"])}
        for _, r in familias_df.iterrows()
    ])

    contexto["inventario"] = {
        "mes_cierre": mes_cierre, "opciones_mes": fechas_inv,
        "materiales_con_stock": len(inv),
        "stock_total": float(inv["stock_cierre"].sum()) if not inv.empty else 0,
        "valor_total": float(inv["valor_total"].sum()) if not inv.empty else 0,
        "familias": familias_items,
        "familias_top": familias_top, "familia_top_sel": familia_top_sel,
        "centros_top": centros_top, "centro_top_sel": centro_top_sel,
        "top_materiales": inv_top.sort_values("valor_total", ascending=False).head(15).to_dict("records"),
    }

    # --- Clasificación ABC de Inventario: Mes/Familia/Centro propios ---
    mes_abc = _selector_mes(request, "abc", fechas_inv)
    inv_abc_cat = q.inventario_por_material(mes_abc)
    inv_abc_cat = inv_abc_cat[inv_abc_cat["categoria"] == categoria]
    familias_abc, centros_abc = _opciones_familia_centro(inv_abc_cat)
    familia_abc_sel, centro_abc_sel = _selector_familia_centro(request, "abc", familias_abc, centros_abc)
    abc_todas = q.clasificacion_abc_por_categoria(
        mes_abc,
        familia=None if familia_abc_sel == "Todas" else familia_abc_sel,
        centro=None if centro_abc_sel == "Todos" else centro_abc_sel,
    )
    contexto["abc"] = next((r for r in abc_todas if r["categoria"] == categoria), None)
    contexto["filtros_abc"] = {
        "mes": mes_abc, "opciones_mes": fechas_inv,
        "familias": familias_abc, "familia_sel": familia_abc_sel,
        "centros": centros_abc, "centro_sel": centro_abc_sel,
    }

    # --- Materiales obsoletos (sin consumo en 6 meses, capital inmovilizado): Mes/Familia/Centro propios ---
    mes_obsoletos = _selector_mes(request, "obsoletos", fechas_inv)
    inv_obsoletos_cat = q.inventario_por_material(mes_obsoletos)
    inv_obsoletos_cat = inv_obsoletos_cat[inv_obsoletos_cat["categoria"] == categoria]
    familias_obsoletos, centros_obsoletos = _opciones_familia_centro(inv_obsoletos_cat)
    familia_obsoletos_sel, centro_obsoletos_sel = _selector_familia_centro(
        request, "obsoletos", familias_obsoletos, centros_obsoletos
    )
    obsoletos_todas = q.materiales_obsoletos_por_categoria(
        mes_obsoletos,
        familia=None if familia_obsoletos_sel == "Todas" else familia_obsoletos_sel,
        centro=None if centro_obsoletos_sel == "Todos" else centro_obsoletos_sel,
    )
    contexto["obsoletos"] = next((r for r in obsoletos_todas if r["categoria"] == categoria), None)
    contexto["filtros_obsoletos"] = {
        "mes": mes_obsoletos, "opciones_mes": fechas_inv,
        "familias": familias_obsoletos, "familia_sel": familia_obsoletos_sel,
        "centros": centros_obsoletos, "centro_sel": centro_obsoletos_sel,
    }

    # --- Índice de Rotación ---
    mes_rotacion = _por_str(request.GET.get("mes_rotacion"), fechas_inv) or fechas_inv[-1]
    rotacion = q.rotacion_inventario_por_material(mes_rotacion)
    rot = rotacion[rotacion["categoria"] == categoria]

    familias_rot, centros_rot = _opciones_familia_centro(rot)
    familia_rot_sel, centro_rot_sel = _selector_familia_centro(request, "rotacion", familias_rot, centros_rot)

    # Rotación anual de la categoría (indicador clásico, en valor): mismo criterio
    # que el OVERVIEW, en cantidad no sería válido porque los materiales de una
    # categoría pueden tener distinta UMB. inventario_por_material/catalogo_materiales
    # están cacheados, así que si mes_rotacion == mes_cierre esto no repite trabajo.
    # Respeta el mismo Familia/Centro elegido para el detalle por material de arriba.
    inv_valor_rot = q.inventario_por_material(mes_rotacion)
    inv_valor_rot = inv_valor_rot[inv_valor_rot["categoria"] == categoria]
    if familia_rot_sel != "Todas":
        inv_valor_rot = inv_valor_rot[inv_valor_rot["familia"] == familia_rot_sel]
    if centro_rot_sel != "Todos":
        inv_valor_rot = inv_valor_rot[inv_valor_rot["centro"] == centro_rot_sel]
    valor_stock_cat = float(inv_valor_rot["valor_total"].sum())

    consumo_val = q.consumo_valorizado_6_meses_por_material(mes_rotacion)
    catalogo = q.catalogo_materiales()
    consumo_val = consumo_val.merge(
        catalogo[["material_codigo", "categoria", "familia", "centro"]], on="material_codigo", how="left"
    )
    consumo_val = consumo_val[consumo_val["categoria"] == categoria]
    if familia_rot_sel != "Todas":
        consumo_val = consumo_val[consumo_val["familia"] == familia_rot_sel]
    if centro_rot_sel != "Todos":
        consumo_val = consumo_val[consumo_val["centro"] == centro_rot_sel]
    consumo_valor_cat = float(consumo_val["consumo_valor_6meses"].sum())

    rotacion_anual_cat = cobertura_cat = dias_inventario_cat = None
    if valor_stock_cat > 0:
        consumo_prom_mensual_cat = consumo_valor_cat / 6
        rotacion_anual_cat = round(consumo_prom_mensual_cat / valor_stock_cat * 12, 2)
        cobertura_cat = round(valor_stock_cat / consumo_prom_mensual_cat, 2) if consumo_prom_mensual_cat else None
        # Días de Inventario (DOH): la misma cobertura, en días. Definido por el usuario.
        dias_inventario_cat = round(cobertura_cat * 365 / 12, 0) if cobertura_cat is not None else None

    # Detalle por material: solo la primera página (ver _ROTACION_PAGINA_TAM):
    # el resto se pide por scroll infinito a rotacion_materiales_pagina.
    rot_ordenado = _rotacion_materiales_filtrado(
        categoria, mes_rotacion,
        familia=None if familia_rot_sel == "Todas" else familia_rot_sel,
        centro=None if centro_rot_sel == "Todos" else centro_rot_sel,
    )
    total_rot = len(rot_ordenado)
    pagina_rot = rot_ordenado.iloc[:_ROTACION_PAGINA_TAM]

    contexto["rotacion"] = {
        "mes": mes_rotacion, "opciones_mes": fechas_inv,
        "familias": familias_rot, "familia_sel": familia_rot_sel,
        "centros": centros_rot, "centro_sel": centro_rot_sel,
        "filas": pagina_rot.to_dict("records"),
        "total": total_rot, "hay_mas": total_rot > _ROTACION_PAGINA_TAM,
        "valor_stock": valor_stock_cat, "consumo_valor_6meses": consumo_valor_cat,
        "rotacion_anual": rotacion_anual_cat, "cobertura_meses_categoria": cobertura_cat,
        "dias_inventario": dias_inventario_cat,
    }

    # --- Cobertura de Materiales Críticos: Mes/Familia/Centro propios ---
    mes_criticos = _selector_mes(request, "criticos", fechas_inv)
    rot_criticos_cat = q.rotacion_inventario_por_material(mes_criticos)
    rot_criticos_cat = rot_criticos_cat[rot_criticos_cat["categoria"] == categoria]
    familias_criticos, centros_criticos = _opciones_familia_centro(rot_criticos_cat)
    familia_criticos_sel, centro_criticos_sel = _selector_familia_centro(
        request, "criticos", familias_criticos, centros_criticos
    )
    criticos_todas = q.cobertura_materiales_criticos_por_categoria(
        mes_criticos,
        familia=None if familia_criticos_sel == "Todas" else familia_criticos_sel,
        centro=None if centro_criticos_sel == "Todos" else centro_criticos_sel,
    )
    contexto["criticos"] = next((r for r in criticos_todas if r["categoria"] == categoria), None)
    contexto["total_criticos_en_riesgo"] = contexto["criticos"]["en_riesgo"] if contexto["criticos"] else 0
    contexto["filtros_criticos"] = {
        "mes": mes_criticos, "opciones_mes": fechas_inv,
        "familias": familias_criticos, "familia_sel": familia_criticos_sel,
        "centros": centros_criticos, "centro_sel": centro_criticos_sel,
    }

    # --- % Variación de Inventario ---
    mes_var_final = _por_str(request.GET.get("mes_var_final"), fechas_inv) or fechas_inv[-1]
    opciones_var_inicial = [f for f in fechas_inv if f < mes_var_final]
    mes_var_inicial = _por_str(request.GET.get("mes_var_inicial"), opciones_var_inicial) or (
        opciones_var_inicial[-1] if opciones_var_inicial else None
    )

    inv_var_cat = q.inventario_por_material(mes_var_final)
    inv_var_cat = inv_var_cat[inv_var_cat["categoria"] == categoria]
    familias_var, centros_var = _opciones_familia_centro(inv_var_cat)
    familia_var_sel, centro_var_sel = _selector_familia_centro(request, "var", familias_var, centros_var)

    if mes_var_inicial:
        var = q.variacion_inventario_por_material(
            mes_var_inicial, mes_var_final, categoria=categoria,
            familia=None if familia_var_sel == "Todas" else familia_var_sel,
            centro=None if centro_var_sel == "Todos" else centro_var_sel,
        )
        contexto["variacion"] = {
            "mes_inicial": mes_var_inicial, "mes_final": mes_var_final,
            "opciones_final": fechas_inv, "opciones_inicial": opciones_var_inicial,
            "familias": familias_var, "familia_sel": familia_var_sel,
            "centros": centros_var, "centro_sel": centro_var_sel,
            "filas": var.sort_values("variacion_valor", ascending=False).head(20).to_dict("records"),
        }
    else:
        contexto["variacion"] = {
            "mes_final": mes_var_final, "opciones_final": fechas_inv,
            "opciones_inicial": [], "mes_inicial": None,
            "familias": familias_var, "familia_sel": familia_var_sel,
            "centros": centros_var, "centro_sel": centro_var_sel,
        }

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

    fechas_inv = q.fechas_inventario_disponibles()
    anios_compras = q.anios_compras_disponibles()

    anio_sel = _por_str(request.GET.get("anio_compras"), [str(a) for a in anios_compras])
    anio_sel = int(anio_sel) if anio_sel else (anios_compras[-1] if anios_compras else None)
    mes_cierre = _por_str(request.GET.get("mes_cierre"), fechas_inv) or (fechas_inv[-1] if fechas_inv else None)
    mes_rotacion = _por_str(request.GET.get("mes_rotacion"), fechas_inv) or mes_cierre

    buffer = io.BytesIO()
    hojas_escritas = False
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:

        def hoja(nombre, datos):
            nonlocal hojas_escritas
            df = datos if isinstance(datos, pd.DataFrame) else pd.DataFrame(datos)
            if df.empty:
                return
            df.to_excel(writer, sheet_name=nombre[:31], index=False)
            hojas_escritas = True

        if anio_sel:
            if categoria:
                df_compras = q.compras_por_categoria_contrato(anio_sel)
                df_compras = df_compras[df_compras["categoria"] == categoria]
                pmp_df = q.pmp_por_categoria(anio_sel)
            else:
                rango = _rango_meses(request, "compras", anio_sel)
                df_compras = q.compras_por_categoria_contrato_rango(rango["fecha_ini"], rango["fecha_fin"])
                pmp_df = q.pmp_por_categoria_rango(rango["fecha_ini"], rango["fecha_fin"])
            hoja("Compras", df_compras.merge(pmp_df, on="categoria", how="left"))

            pareto_todas = q.concentracion_proveedores_por_categoria(anio_sel)
            if categoria:
                pareto_todas = [r for r in pareto_todas if r["categoria"] == categoria]
            hoja("Pareto Proveedores", [
                {
                    "categoria": r["categoria"], "pct_concentracion_top5": r["pct_concentracion_top"],
                    "proveedor": p["proveedor"], "valor": p["valor"], "pct_del_total": p["pct"],
                }
                for r in pareto_todas for p in r["top_proveedores"]
            ])

            fill_rate_todas = q.fill_rate_por_categoria(anio_sel)
            if categoria:
                fill_rate_todas = [r for r in fill_rate_todas if r["categoria"] == categoria]
            else:
                categorias_con_inv = set(q.categorias_inventario_disponibles())
                fill_rate_todas = [r for r in fill_rate_todas if r["categoria"] in categorias_con_inv]
            hoja("Fill Rate", fill_rate_todas)

            lead_time_todas = q.lead_time_por_categoria(anio_sel)
            if categoria:
                lead_time_todas = [r for r in lead_time_todas if r["categoria"] == categoria]
            hoja("Lead Time", lead_time_todas)

        if categoria is None or categoria in set(q.categorias_inventario_disponibles()):
            if mes_cierre:
                inv = q.inventario_por_material(mes_cierre)
                if categoria:
                    inv = inv[inv["categoria"] == categoria]
                columnas_inv = [
                    "material_codigo", "texto_breve", "categoria", "familia", "centro",
                    "unidad_medida_base", "stock_cierre", "valor_total",
                ]
                hoja("Inventario", inv[columnas_inv].sort_values("valor_total", ascending=False))

                abc_todas = q.clasificacion_abc_por_categoria(mes_cierre)
                if categoria:
                    abc_todas = [r for r in abc_todas if r["categoria"] == categoria]
                hoja("ABC", [
                    {"categoria": r["categoria"], "clase": c["clase"], "materiales": c["materiales"], "valor": c["valor"]}
                    for r in abc_todas for c in r["clases"]
                ])

                obsoletos_todas = q.materiales_obsoletos_por_categoria(mes_cierre)
                if categoria:
                    obsoletos_todas = [r for r in obsoletos_todas if r["categoria"] == categoria]
                hoja("Sin Movimiento", obsoletos_todas)

            if mes_rotacion:
                criticos_todas = q.cobertura_materiales_criticos_por_categoria(mes_rotacion)
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
