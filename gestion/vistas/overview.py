"""Página Overview: los indicadores de todas las categorías juntas. Pestaña
Indicadores (los cuatro cuadrantes, con filtro Año) y Detalle por categoría
(compras, proveedores, plazo de pago e inventario de cada una)."""

from dashboard import queries as q
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

from .. import charts, ejecutivo
from .categoria_indicadores import cuadrantes_todas
from .comun import _construir_nav, _evolucion_inventario, _mes_abrev_guion, _por_familia_centro, _por_str, _proveedor_unico_periodo, _segmentadores_compras, _segmentadores_inventario, _slug, html_guardado
from .plan_cuadrantes import periodo_valido, periodos_cuadrantes


def _html_indicadores(request, categorias):
    """Pestaña Indicadores: los cuatro cuadrantes de todas las categorías
    juntas en el período del filtro Año (?anio_indicadores=; por defecto
    "2025 en adelante")."""
    periodos = periodos_cuadrantes()
    periodo_sel = periodo_valido(request.GET.get("anio_indicadores", ""))

    def dibujar():
        con_inventario = set(q.categorias_inventario_disponibles())
        categorias_inventario = [c for c in categorias if c in con_inventario]
        vistas = [{"valor": periodo_sel, "texto": dict(periodos)[periodo_sel],
                   "paneles": cuadrantes_todas(request, categorias, categorias_inventario, periodo_sel)}]
        return render_to_string("gestion/categoria/_indicadores.html", {
            "vistas": vistas, "periodos": [{"valor": v, "texto": t} for v, t in periodos], "periodo_sel": periodo_sel,
            "nombre_indicadores": "Todas las categorías",
        }, request=request)

    return html_guardado(request, "overview", "indicadores", periodo_sel, dibujar=dibujar)


@login_required
def overview(request):
    categorias_nav, categorias = _construir_nav("overview")

    fechas_inv = q.fechas_inventario_disponibles()
    anios_compras = q.anios_compras_disponibles()
    sin_datos = not fechas_inv and not anios_compras

    # Una pestaña (?_zona=) o una sección al cambiar su filtro (?_seccion=):
    # solo ese pedazo de HTML. Las secciones que no son de Indicadores están
    # en el Detalle por categoría.
    zona, seccion = request.GET.get("_zona"), request.GET.get("_seccion")
    if zona or seccion:
        if sin_datos:
            return HttpResponse("")
        if zona == "indicadores" or seccion == "sec-indicadores-categoria":
            return HttpResponse(_html_indicadores(request, categorias))
        return HttpResponse(html_guardado(request, "overview", "detalle", dibujar=lambda: render_to_string(
            "gestion/_overview_detalle.html", _contexto_detalle(request, categorias_nav, categorias), request=request)))

    contexto = {"categoria": "OVERVIEW", "categorias_nav": categorias_nav, "es_overview": True}
    if sin_datos:
        contexto["sin_datos"] = True
        return render(request, "gestion/overview.html", contexto)
    contexto["indicadores_html"] = mark_safe(_html_indicadores(request, categorias))
    if fechas_inv:
        criticos = q.cobertura_materiales_criticos_por_categoria(fechas_inv[-1])
        contexto["total_criticos_en_riesgo"] = sum(r["en_riesgo"] for r in criticos)
    return render(request, "gestion/overview.html", contexto)


def _contexto_detalle(request, categorias_nav, categorias):
    """Pestaña Detalle por categoría del Overview (lo que era toda la página
    hasta el 2026-10-28)."""
    fechas_inv = q.fechas_inventario_disponibles()
    contexto = {"categoria": "OVERVIEW", "categorias_nav": categorias_nav, "es_overview": True}

    # --- Bloque Compras (Compras, Pareto, Fill Rate, Lead Time): un solo
    # juego de segmentadores (Año, Desde, Hasta, Top de proveedores) ---
    sc = _segmentadores_compras(request)
    contexto["sc"] = sc
    compras_filas = []
    contexto["pareto"] = contexto["fill_rate"] = contexto["lead_time"] = []
    categorias_con_inv = set(q.categorias_inventario_disponibles())
    if sc:
        rango = sc["rango"]
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
                "categoria": cat, "slug": _slug(cat), "valor_total": valor_total,
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
        contexto["pareto"] = q.concentracion_proveedores_por_categoria_rango(
            rango["fecha_ini"], rango["fecha_fin"], top=sc["top"]
        )
        # Fill Rate: solo categorías con inventario (a pedido del usuario).
        fill_rate_todas = q.fill_rate_por_categoria_rango(rango["fecha_ini"], rango["fecha_fin"])
        # Repuestos y Suministros Industriales se miden con OTIF (sección aparte).
        con_otif = set(ejecutivo.cargar_metas()["supuestos"]["otif_categorias"])
        contexto["fill_rate"] = [r for r in fill_rate_todas if r["categoria"] in categorias_con_inv - con_otif]
        contexto["lead_time"] = q.lead_time_por_categoria_rango(rango["fecha_ini"], rango["fecha_fin"])
        contexto["unico"] = _proveedor_unico_periodo(sc)
        contexto["otif"] = ejecutivo.otif(rango["fecha_ini"], rango["fecha_fin"])
    contexto["compras"] = compras_filas

    # --- Evolución de PMP (general, todas las categorías) ---
    pmp_serie = q.pmp_mensual()
    puntos_pmp = [(_mes_abrev_guion(row["mes"]), float(row["pmp"])) for _, row in pmp_serie.iterrows()]
    contexto["pmp_evolucion"] = {
        "grafico": charts.serie_area(puntos_pmp, formato_valor=lambda v: f"{v:z.0f}"),
    }

    if not fechas_inv:
        contexto["sin_inventario"] = True
        return contexto

    # --- Bloque Inventario: un solo juego de segmentadores (Corte, Familia,
    # Centro) para Evolución, Inventario, ABC, Sin movimiento, Rotación,
    # Críticos y % Variación ---
    si = _segmentadores_inventario(request)
    contexto["si"] = si
    mes, inv = si["mes"], si["inv"]
    contexto["evolucion"] = _evolucion_inventario(request, si)

    categorias_df = (
        inv.groupby("categoria", as_index=False)["valor_total"].sum()
        .sort_values("valor_total", ascending=False)
    )
    contexto["inventario"] = {
        "materiales_con_stock": len(inv),
        "stock_total": float(inv["stock_cierre"].sum()) if not inv.empty else 0,
        "valor_total": float(inv["valor_total"].sum()) if not inv.empty else 0,
        "familias": charts.barras([
            {"etiqueta": r["categoria"], "valor": float(r["valor_total"])} for _, r in categorias_df.iterrows()
        ]),
        "top_materiales": inv.sort_values("valor_total", ascending=False).head(15).to_dict("records"),
    }

    contexto["abc"] = q.clasificacion_abc_por_categoria(mes, familia=si["familia"], centro=si["centro"])
    contexto["obsoletos"] = q.materiales_obsoletos_por_categoria(mes, familia=si["familia"], centro=si["centro"])

    # --- Índice de Rotación: resumen por categoría ---
    # El índice por material (dashboard/queries.py) está en CANTIDAD, y cada
    # material puede tener una UMB distinta (kg, ton, und, m...) incluso dentro
    # de la misma categoría (p.ej. REPUESTOS mezcla ~20 unidades diferentes) —
    # promediar o sumar esas cantidades entre materiales no es válido. Por eso
    # el resumen por categoría del OVERVIEW se calcula en VALOR (S/.), la única
    # unidad común a todos los materiales, igual que el indicador clásico de
    # rotación (Costo de Ventas $ ÷ Inventario $).
    stock_valor_cat = inv.groupby("categoria")["valor_total"].sum()
    consumo_val = q.consumo_valorizado_ventana_por_material(mes)
    catalogo = q.catalogo_materiales()
    consumo_val = _por_familia_centro(consumo_val.merge(
        catalogo[["material_codigo", "categoria", "familia", "centro"]], on="material_codigo", how="left"
    ), si)
    consumo_valor_cat = consumo_val.groupby("categoria")["consumo_valor_ventana"].sum()

    rotacion_filas = []
    for cat in categorias:
        valor_stock = float(stock_valor_cat.get(cat, 0) or 0)
        valor_consumo_ventana = float(consumo_valor_cat.get(cat, 0) or 0)
        if valor_stock <= 0:
            continue
        consumo_prom_mensual = valor_consumo_ventana / q.MESES_CONSUMO_COBERTURA
        cobertura_meses = round(valor_stock / consumo_prom_mensual, 2) if consumo_prom_mensual else None
        rotacion_filas.append({
            "categoria": cat,
            "valor_stock": valor_stock,
            "consumo_valor_ventana": valor_consumo_ventana,
            "indice_rotacion_anual": round(consumo_prom_mensual / valor_stock * 12, 2),
            "cobertura_meses": cobertura_meses,
            # Días de Inventario (DOH): la misma cobertura, en días en vez de
            # meses (mes calendario promedio = 365/12). Más intuitivo para
            # lectura ejecutiva que "cobertura en meses". Definido por el usuario.
            "dias_inventario": round(cobertura_meses * 365 / 12, 0) if cobertura_meses is not None else None,
        })
    contexto["rotacion"] = {"filas": sorted(rotacion_filas, key=lambda f: f["indice_rotacion_anual"], reverse=True)}

    # --- Cobertura de Materiales Críticos (por categoría) ---
    contexto["criticos"] = q.cobertura_materiales_criticos_por_categoria(mes, familia=si["familia"], centro=si["centro"])
    contexto["total_criticos_en_riesgo"] = sum(r["en_riesgo"] for r in contexto["criticos"])

    # --- % Variación de Inventario: el corte de los segmentadores contra el
    # Mes comparativo (propio de la sección; por defecto, el corte anterior) ---
    opciones_var_inicial = [c for c in reversed(si["cortes"]) if c < mes]
    mes_var_inicial = _por_str(request.GET.get("mes_var_inicial"), opciones_var_inicial) or (
        opciones_var_inicial[-1] if opciones_var_inicial else None
    )
    contexto["variacion"] = {"mes_inicial": mes_var_inicial, "mes_final": mes,
                             "opciones_inicial": list(reversed(opciones_var_inicial))}
    if mes_var_inicial:
        variacion = q.variacion_inventario_por_material(
            mes_var_inicial, mes, familia=si["familia"], centro=si["centro"],
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
        contexto["variacion"]["filas"] = variacion_filas

    return contexto
