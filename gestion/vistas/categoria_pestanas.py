"""Pestañas de cada categoría: Compras, Inventario, Abastecimiento S&OP, Precios e IPU y Cuentas por pagar."""

import datetime

import pandas as pd
from dashboard import queries as q

from .. import charts, desglose, ejecutivo
from .comun import _ROTACION_PAGINA_TAM, _evolucion_inventario, _mes_abrev_guion, _mes_corto, _nombre_corto, _opciones_material, _por_familia_centro, _por_materiales, _por_str, _proveedor_unico_periodo, _rango_en_anio, _rango_meses, _rotacion_materiales_filtrado, _segmentadores_compras, _segmentadores_inventario, _selector_material, _soles_eje, _soles_marca, _texto_rango
from .plan_cumplimiento import _meses_plan
from .plan_precios import _contexto_evaluacion_precios, _filtrar_periodo_inflacion, _nota_base, _periodos_inflacion


# Meses de la gráfica Compras por mes (pestaña Compras y su panel en Indicadores).
_COMPRAS_MESES = 24


def _cat_compras(request, categoria, tiene_inventario):
    """Pestaña Compras: un solo juego de segmentadores (Año, Desde, Hasta,
    Top de proveedores) para Compras por año, Compras por mes, Pareto, Fill
    Rate (solo con inventario, a pedido del usuario) y Lead Time."""
    sc = _segmentadores_compras(request)
    # Repuestos y Suministros Industriales miden OTIF en lugar de Fill Rate
    # (pedido por el usuario el 2026-10-04); el resto con inventario sigue
    # con Fill Rate.
    con_otif = categoria in ejecutivo.cargar_metas()["supuestos"]["otif_categorias"]
    contexto = {"sc": sc, "compras": [], "compras_mensual": None, "compras_por_mes": [],
                "pareto": None, "fill_rate": None, "lead_time": None, "otif": None,
                "con_fill_rate": tiene_inventario and not con_otif}
    if not sc:
        return contexto
    rango = sc["rango"]

    # --- Compras por año, en los MISMOS meses (Desde–Hasta) de cada año: la
    # variación contra el año anterior compara períodos equivalentes ---
    valor_anio_anterior = None
    for anio in reversed(sc["anios"]):
        inicio, fin = _rango_en_anio(rango, anio)
        df = q.compras_por_categoria_contrato_rango(inicio, fin)
        fila = df[df["categoria"] == categoria]
        if fila.empty:
            valor_anio_anterior = None
            continue
        valor_total = float(fila["valor_total"].iloc[0] or 0)
        valor_bajo_contrato = float(fila["valor_bajo_contrato"].iloc[0] or 0)
        pmp_df = q.pmp_por_categoria_rango(inicio, fin)
        fila_pmp = pmp_df[pmp_df["categoria"] == categoria]
        pmp = float(fila_pmp["pmp"].iloc[0]) if not fila_pmp.empty else None
        contexto["compras"].append({
            "anio": anio, "periodo": _texto_rango(rango, anio), "valor_total": valor_total,
            "pct_contrato": round(valor_bajo_contrato / valor_total * 100, 1) if valor_total else 0,
            "pmp": round(pmp, 0) if pmp is not None else None, "seleccionado": anio == sc["anio"],
            "variacion_yoy_pct": (
                round((valor_total - valor_anio_anterior) / valor_anio_anterior * 100, 1)
                if valor_anio_anterior else None
            ),
        })
        valor_anio_anterior = valor_total

    # --- Compras por mes: los _COMPRAS_MESES que terminan en el "Hasta"
    # elegido; los meses del período elegido en rojo, el resto en gris ---
    serie = q.compras_por_mes_y_condicion_pago(categoria)
    if not serie.empty:
        por_mes = serie.groupby("mes")["valor_total"].sum()
        meses = sorted((datetime.date.fromisoformat(str(m)[:10]), float(v)) for m, v in por_mes.items())
        contexto["compras_por_mes"] = meses
        ultimos = [(m, v) for m, v in meses if m <= rango["fecha_fin"]][-_COMPRAS_MESES:]
        if ultimos:
            contexto["compras_mensual"] = {
                "meses": len(ultimos),
                "grafico": charts.columnas(
                    [_mes_corto(m) for m, _ in ultimos],
                    [[{"valor": v,
                       "clase": "col-compras" if rango["fecha_ini"] <= m <= rango["fecha_fin"] else "col-compras-fuera",
                       "detalle": f"{q.formato_mes_anio(m)}\nCompras: S/ {v:z,.0f}",
                       "filtrar": desglose.filtro(f_mes=desglose.mes_param(m))}] for m, v in ultimos],
                    _soles_eje,
                ),
            }

    pareto_todas = q.concentracion_proveedores_por_categoria_rango(rango["fecha_ini"], rango["fecha_fin"], top=sc["top"])
    contexto["pareto"] = next((r for r in pareto_todas if r["categoria"] == categoria), None)
    if contexto["con_fill_rate"]:
        fill_rate_todas = q.fill_rate_por_categoria_rango(rango["fecha_ini"], rango["fecha_fin"])
        contexto["fill_rate"] = next((r for r in fill_rate_todas if r["categoria"] == categoria), None)
    lead_time_todas = q.lead_time_por_categoria_rango(rango["fecha_ini"], rango["fecha_fin"])
    contexto["lead_time"] = next((r for r in lead_time_todas if r["categoria"] == categoria), None)
    contexto["unico"] = None if categoria == "CAPEX" else _proveedor_unico_periodo(sc, categoria)
    if con_otif:
        contexto["otif"] = ejecutivo.otif(rango["fecha_ini"], rango["fecha_fin"], categoria)
    return contexto


def _mes_variacion(request, si):
    """Cortes anteriores al de los segmentadores y el Mes comparativo elegido
    de % Variación (por defecto, el corte anterior)."""
    opciones = [c for c in reversed(si["cortes"]) if c < si["mes"]]
    elegido = _por_str(request.GET.get("mes_var_inicial"), opciones) or (opciones[-1] if opciones else None)
    return opciones, elegido


def _cat_inventario(request, categoria, tiene_inventario):
    """Pestaña Inventario (solo categorías con inventario): un solo juego de
    segmentadores (Corte, Familia, Centro) para Evolución, Inventario del
    corte, ABC, Sin movimiento, Rotación, Críticos y % Variación. Cada
    sección conserva solo lo propio: el rango de la Evolución, el mes
    comparativo de la Variación y el buscador de la Rotación."""
    si = _segmentadores_inventario(request, categoria)
    mes, inv = si["mes"], si["inv"]
    familia, centro = si["familia"], si["centro"]
    contexto = {"si": si, "evolucion": _evolucion_inventario(request, si, categoria)}

    # --- Inventario del corte: métricas, valor por familia (o por centro si
    # hay una familia elegida) y top materiales ---
    eje = "centro" if familia else "familia"
    agrupado = inv.groupby(eje, as_index=False)["valor_total"].sum().sort_values("valor_total", ascending=False).head(10)
    contexto["inventario"] = {
        "materiales_con_stock": len(inv),
        "stock_total": float(inv["stock_cierre"].sum()) if not inv.empty else 0,
        "valor_total": float(inv["valor_total"].sum()) if not inv.empty else 0,
        "eje": "Centro" if familia else "Familia",
        "familias": charts.barras([
            {"etiqueta": q.nombre_centro(r[eje]) if familia else r[eje], "valor": float(r["valor_total"]),
             "filtrar": desglose.filtro(f_centro=r[eje]) if familia else desglose.filtro(f_familia=r[eje])}
            for _, r in agrupado.iterrows()
        ]),
        "top_materiales": inv.sort_values("valor_total", ascending=False).head(15).to_dict("records"),
    }

    abc_todas = q.clasificacion_abc_por_categoria(mes, familia=familia, centro=centro)
    contexto["abc"] = next((r for r in abc_todas if r["categoria"] == categoria), None)
    obsoletos_todas = q.materiales_obsoletos_por_categoria(mes, familia=familia, centro=centro)
    contexto["obsoletos"] = next((r for r in obsoletos_todas if r["categoria"] == categoria), None)

    # --- Índice de Rotación: anual de la categoría en valor (en cantidad no
    # sería válido: distintas UMB) y detalle por material en cantidad ---
    valor_stock_cat = float(inv["valor_total"].sum())
    consumo_val = q.consumo_valorizado_ventana_por_material(mes)
    catalogo = q.catalogo_materiales()
    consumo_val = consumo_val.merge(
        catalogo[["material_codigo", "categoria", "familia", "centro"]], on="material_codigo", how="left"
    )
    consumo_val = _por_familia_centro(consumo_val[consumo_val["categoria"] == categoria], si)
    consumo_valor_cat = float(consumo_val["consumo_valor_ventana"].sum())

    rotacion_anual_cat = cobertura_cat = dias_inventario_cat = None
    if valor_stock_cat > 0:
        consumo_prom_mensual_cat = consumo_valor_cat / q.MESES_CONSUMO_COBERTURA
        rotacion_anual_cat = round(consumo_prom_mensual_cat / valor_stock_cat * 12, 2)
        cobertura_cat = round(valor_stock_cat / consumo_prom_mensual_cat, 2) if consumo_prom_mensual_cat else None
        # Días de Inventario (DOH): la misma cobertura, en días. Definido por el usuario.
        dias_inventario_cat = round(cobertura_cat * 365 / 12, 0) if cobertura_cat is not None else None

    # Detalle por material: solo la primera página (ver _ROTACION_PAGINA_TAM):
    # el resto se pide por scroll infinito a rotacion_materiales_pagina.
    rot_ordenado = _rotacion_materiales_filtrado(categoria, mes, familia=familia, centro=centro)
    total_rot = len(rot_ordenado)
    contexto["rotacion"] = {
        "mes": mes, "familia_sel": familia or "", "centro_sel": centro or "",
        "filas": rot_ordenado.iloc[:_ROTACION_PAGINA_TAM].to_dict("records"),
        "total": total_rot, "hay_mas": total_rot > _ROTACION_PAGINA_TAM,
        "valor_stock": valor_stock_cat, "consumo_valor_ventana": consumo_valor_cat,
        "rotacion_anual": rotacion_anual_cat, "cobertura_meses_categoria": cobertura_cat,
        "dias_inventario": dias_inventario_cat,
    }

    criticos_todas = q.cobertura_materiales_criticos_por_categoria(mes, familia=familia, centro=centro)
    contexto["criticos"] = next((r for r in criticos_todas if r["categoria"] == categoria), None)

    # --- % Variación: el corte elegido contra el Mes comparativo (propio de
    # la sección; por defecto, el corte anterior) ---
    opciones_var_inicial, mes_var_inicial = _mes_variacion(request, si)
    contexto["variacion"] = {"mes_inicial": mes_var_inicial, "mes_final": mes,
                             "opciones_inicial": list(reversed(opciones_var_inicial)), "filas": []}
    if mes_var_inicial:
        var = q.variacion_inventario_por_material(mes_var_inicial, mes, categoria=categoria, familia=familia, centro=centro)
        contexto["variacion"]["filas"] = var.sort_values("variacion_valor", ascending=False).head(20).to_dict("records")
    return contexto


def _mes_sop_categoria(request):
    """Meses del Plan S&OP y el elegido en la pestaña Abastecimiento de una
    categoría: por defecto, el último mes cerrado (el mes en curso recién
    empieza), igual que la pestaña Indicadores de Planificación."""
    meses, mes_defecto = _meses_plan()
    if not meses:
        return meses, None
    cerrado = q._sumar_meses(datetime.date.today().replace(day=1), -1)
    return meses, _por_str(request.GET.get("mes_sopcat"), meses) or (cerrado if cerrado in meses else mes_defecto)


def _cat_abastecimiento(request, categoria, tiene_inventario):
    """Pestaña Abastecimiento S&OP (Materias Primas y Combustibles): Plan de
    Abastecimiento vs ingresos físicos del mes por centro y cumplimiento mes
    a mes, de esta categoría. El detalle sigue en Planificación."""
    meses, mes = _mes_sop_categoria(request)
    if not meses:
        return {"ab": None}
    detalle = q.plan_vs_real_abastecimiento_por_material(mes, categoria=categoria)
    centros = []
    if not detalle.empty:
        por_centro = detalle.groupby("centro").agg(plan=("cantidad_plan", "sum"), real=("cantidad_real", "sum"))
        for cod, r in sorted(por_centro.iterrows(), key=lambda x: q.nombre_centro(x[0])):
            plan, real = float(r["plan"]), float(r["real"])
            centros.append({"centro": cod, "plan": plan, "real": real, "diferencia": real - plan,
                            "pct": round(real / plan * 100, 1) if plan else None})
    total_plan = sum(f["plan"] for f in centros)
    total_real = sum(f["real"] for f in centros)
    # Hasta el mes en curso: los meses futuros del plan todavía no tienen real.
    hoy_mes = datetime.date.today().replace(day=1)
    filas = [f for f in q.cumplimiento_mensual_abastecimiento(categoria=categoria) if f["mes"] <= hoy_mes]
    return {"ab": {
        "mes": mes, "opciones_mes": meses, "centros": centros,
        "total_plan": total_plan, "total_real": total_real,
        "cumplimiento_pct": round(total_real / total_plan * 100, 1) if total_plan else None,
        "grafico": charts.barras_mensuales(
            [(q.formato_mes_anio_abrev(f["mes"]), f["cumplimiento_pct"]) for f in filas], alto=240, meta=100,
        ),
    }}


# Comparación de la pestaña Precios e IPU de una categoría (mismo cálculo que
# la Inflación de precios de Planificación, queries.inflacion_precios).
_EVALUACIONES_IPU_CATEGORIA = {
    "interanual": {"desfase": 12, "texto": "Mismo mes del año anterior",
                   "contra": "el mismo mes del año anterior (Ene-26 vs Ene-25)", "base_titulo": "un año antes"},
    "mensual": {"desfase": 1, "texto": "Mes anterior",
                "contra": "el mes anterior (Mar-26 vs Feb-26)", "base_titulo": "el mes anterior"},
}


def _seleccion_precios(request, categoria):
    """Segmentadores de Precios e IPU de una categoría (Comparar contra,
    Familia, Centro, Material en cascada y Período) y los datos que dejan:
    base (compras de la categoría), df (filtrado), inflacion_total (todas
    las comparaciones) e inflacion (las del período). Lo usan la pestaña y
    su detalle fila por fila."""
    base = q.precios_entrada_materiales_real()
    base = base[base["categoria"] == categoria]
    evaluacion_sel = request.GET.get("evaluacion_ipu", "interanual")
    if evaluacion_sel not in _EVALUACIONES_IPU_CATEGORIA:
        evaluacion_sel = "interanual"
    evaluacion = _EVALUACIONES_IPU_CATEGORIA[evaluacion_sel]

    familias = ["Todas"] + sorted(f for f in base["familia"].unique() if f)
    familia_sel = request.GET.get("familia_ipu", "Todas")
    if familia_sel not in familias:
        familia_sel = "Todas"
    por_familia = base if familia_sel == "Todas" else base[base["familia"] == familia_sel]
    centros = ["Todos"] + sorted(c for c in por_familia["centro"].unique() if c)
    centro_sel = request.GET.get("centro_ipu", "Todos")
    if centro_sel not in centros:
        centro_sel = "Todos"
    por_centro = por_familia if centro_sel == "Todos" else por_familia[por_familia["centro"] == centro_sel]
    materiales_op = _opciones_material(por_centro) if not por_centro.empty else [{"codigo": "Todos", "texto": "Todos"}]
    material_sel = _selector_material(request, "ipu", materiales_op)
    df = _por_materiales(por_centro, material_sel)
    s = {"base": base, "df": df, "evaluacion": evaluacion, "evaluacion_sel": evaluacion_sel,
         "familias": familias, "familia_sel": familia_sel, "centros": centros, "centro_sel": centro_sel,
         "materiales_op": materiales_op, "material_sel": material_sel,
         "inflacion_total": pd.DataFrame(), "inflacion": pd.DataFrame(), "periodos": [], "periodo_sel": "desde"}
    if base.empty:
        return s
    s["inflacion_total"] = inflacion_total = q.inflacion_precios(df, evaluacion["desfase"])
    if inflacion_total.empty:
        return s
    s["periodos"] = periodos = _periodos_inflacion(sorted(inflacion_total["mes"].unique()))
    periodo_sel = request.GET.get("periodo_ipu", "desde")
    if periodo_sel not in {v for v, _ in periodos}:
        periodo_sel = "desde"
    s["periodo_sel"] = periodo_sel
    s["inflacion"] = _filtrar_periodo_inflacion(inflacion_total, periodo_sel)
    return s


def _cat_precios(request, categoria, tiene_inventario):
    """Pestaña Precios e IPU de una categoría: IPU (Índice de Precios Unacem)
    de sus materiales comprados, con el mismo cálculo que Planificación
    (precio real de compra: MB51 + ajustes FBL3N, ver queries.con_precio_real), y los
    materiales que más subieron y bajaron. Segmentadores: Período, contra qué
    se compara, Familia, Centro y Material (varios), en cascada; con un solo
    material, su detalle mes a mes. Una categoría sin materiales con entrada
    de mercancía (servicios) muestra un aviso."""
    s = _seleccion_precios(request, categoria)
    base, evaluacion, evaluacion_sel = s["base"], s["evaluacion"], s["evaluacion_sel"]
    familias, familia_sel, centros, centro_sel = s["familias"], s["familia_sel"], s["centros"], s["centro_sel"]
    materiales_op, material_sel = s["materiales_op"], s["material_sel"]
    pr = {
        "evaluaciones": [{"valor": k, "texto": v["texto"]} for k, v in _EVALUACIONES_IPU_CATEGORIA.items()],
        "evaluacion_sel": evaluacion_sel, "contra": evaluacion["contra"], "base_titulo": evaluacion["base_titulo"],
        "familias": familias, "familia_sel": familia_sel, "centros": centros, "centro_sel": centro_sel,
        "materiales": materiales_op, "material_sel": material_sel,
        "periodos": [], "periodo_sel": "desde",
        "resumen": None, "grafico": None, "puntos": [], "tablas": [], "aviso": "", "detalle": [], "grafico_precio": None,
    }
    if base.empty:
        pr["aviso"] = ("Esta categoría no tiene compras de materiales con entrada de mercancía (MB51) para medir su "
                       "precio unitario; las compras de servicios o proyectos se evaluarán cuando se defina con qué "
                       "dato medirlas.")
        return {"pr": pr}
    inflacion_total, inflacion = s["inflacion_total"], s["inflacion"]
    if inflacion_total.empty:
        return {"pr": pr}
    pr["periodos"] = [{"valor": v, "texto": t} for v, t in s["periodos"]]
    pr["periodo_sel"] = s["periodo_sel"]
    if inflacion.empty:
        return {"pr": pr}

    # IPU mes a mes sobre TODA la historia (el acumulado arranca en enero
    # aunque el período empiece después); el período solo recorta los meses.
    ipu = q.ipu_por_mes(inflacion_total, evaluacion["desfase"])
    meses = [m for m in sorted(inflacion["mes"].unique()) if m in ipu]
    puntos = []
    for m in meses:
        d = ipu[m]
        c = q.inflacion_canasta(inflacion[inflacion["mes"] == m])
        inicio = min(x for x in ipu if x.year == m.year)
        tramo = f"{_mes_corto(inicio)}–{_mes_corto(m)}" if inicio != m else _mes_corto(m)
        puntos.append({
            "mes": m, "individual": d["individual"], "acumulado": d["acumulado"],
            "detalle_individual": (f"{_mes_corto(m)} vs {evaluacion['base_titulo']}: {d['individual'] * 100:+z.1f}%\n"
                                   f"Bolsa: {'+' if d['bolsa'] >= 0 else '−'}S/ {abs(d['bolsa']):z,.0f}\n"
                                   f"{c['materiales']} material{'es' if c['materiales'] != 1 else ''} comprado"
                                   f"{'s' if c['materiales'] != 1 else ''} · S/ {c['importe']:z,.0f}"),
            "detalle_acumulado": (f"IPU acumulado {tramo}: {d['acumulado'] * 100:+z.1f}%\n"
                                  f"Pagado: S/ {d['importe_acumulado']:z,.0f} · al precio base: S/ {d['base_acumulada']:z,.0f}\n"
                                  f"Bolsa acumulada: {'+' if d['bolsa_acumulada'] >= 0 else '−'}S/ {abs(d['bolsa_acumulada']):z,.0f}"),
        })
    pr["puntos"] = puntos
    pr["grafico"] = charts.evolucion_ipu(puntos, _mes_corto)

    # IPU de cada año: el acumulado al último mes de ese año dentro del período.
    por_anio = []
    for a in sorted({m.year for m in meses}):
        ultimo = max(m for m in meses if m.year == a)
        d = ipu[ultimo]
        por_anio.append({"anio": a, "hasta": _mes_corto(ultimo), "anio_completo": ultimo.month == 12,
                         "pct": d["acumulado"] * 100, "bolsa": d["bolsa_acumulada"]})

    # Por material (sin las comparaciones atípicas): variación ponderada del
    # período (lo pagado ÷ lo que habría costado al precio base − 1) y bolsa.
    validas = inflacion[~inflacion["atipica"]].sort_values("mes")
    umb = dict(zip(base["material_codigo"], base["unidad_medida_base"]))
    materiales = validas.assign(base_importe=validas["precio_base"] * validas["toneladas"]).groupby(
        ["material_codigo", "centro"]).agg(
        texto_breve=("texto_breve", "last"), importe=("importe", "sum"), base_importe=("base_importe", "sum"),
        precio=("precio", "last"), mes=("mes", "last"), ultima=("variacion", "last"),
    ).reset_index()
    materiales["bolsa"] = materiales["importe"] - materiales["base_importe"]
    materiales["pct"] = (materiales["importe"] / materiales["base_importe"] - 1) * 100

    # Todos los materiales que subieron (y los que bajaron), con su total:
    # lo pagado, lo que habría costado al precio base y la variación
    # ponderada del conjunto. La tabla se desplaza con encabezado, código y
    # descripción fijos, y el buscador recalcula el total de lo encontrado
    # (pedido del usuario, 2026-10-22).
    def tabla(df_tabla, id_tabla, titulo, orden, vacio):
        filas = [{"material_codigo": r.material_codigo, "texto_breve": r.texto_breve, "centro": r.centro,
                  "umb": umb.get(r.material_codigo, ""), "precio": r.precio, "mes": _mes_corto(r.mes),
                  "importe": r.importe, "base_importe": r.base_importe, "pct": r.pct, "bolsa": r.bolsa}
                 for r in df_tabla.itertuples()]
        importe, base_importe = float(df_tabla["importe"].sum()), float(df_tabla["base_importe"].sum())
        total = {"materiales": len(filas), "importe": importe, "bolsa": importe - base_importe,
                 "pct": (importe / base_importe - 1) * 100 if base_importe else 0.0}
        return {"id": id_tabla, "titulo": titulo, "orden": orden, "filas": filas, "total": total, "vacio": vacio}

    suben = materiales[materiales["bolsa"] > 0].sort_values("bolsa", ascending=False)
    bajan = materiales[materiales["bolsa"] < 0].sort_values("bolsa")
    pr["tablas"] = [
        tabla(suben, "suben", f"Materiales que subieron de precio ({len(suben):,} en el período)", "desc",
              "Ningún material subió de precio en el período."),
        tabla(bajan, "bajan", f"Materiales que bajaron de precio ({len(bajan):,} en el período)", "asc",
              "Ningún material bajó de precio en el período."),
    ]
    ultimo = ipu[meses[-1]] if meses else None
    pr["resumen"] = {
        "por_anio": por_anio, "ultimo_mes": _mes_corto(meses[-1]) if meses else "",
        "ultima_pct": ultimo["individual"] * 100 if ultimo else 0.0,
        "materiales": int(len(materiales)), "desde": _mes_corto(min(inflacion["mes"])), "hasta": _mes_corto(max(inflacion["mes"])),
        "suben": int((materiales["ultima"] > 0).sum()), "bajan": int((materiales["ultima"] < 0).sum()),
        "atipicas": int(inflacion["atipica"].sum()),
        # Todo el período, ponderado (lo pagado ÷ lo que habría costado al
        # precio base − 1): la cifra del IPU de Indicadores con "2025 en adelante".
        "periodo_pct": (float(materiales["importe"].sum()) / float(materiales["base_importe"].sum()) - 1) * 100
        if float(materiales["base_importe"].sum()) else 0.0,
        "bolsa": float(materiales["bolsa"].sum()),
    } if meses else None

    # Un solo material (en un solo centro): su precio mes a mes contra el
    # precio base y el detalle de cada comparación.
    if inflacion[["material_codigo", "centro"]].drop_duplicates().shape[0] == 1:
        r0 = inflacion.iloc[0]
        pr["un_material"] = f"{r0['texto_breve']} ({q.nombre_centro(r0['centro'])}) · S/ por {umb.get(r0['material_codigo'], 'UMB')}"
        pr["grafico_precio"] = charts.evolucion_precio(
            [(r.mes, float(r.precio), float(r.toneladas)) for r in inflacion.itertuples()], _mes_corto,
            base=[(r.mes, float(r.precio_base)) for r in inflacion.itertuples()], ancho_minimo=960,
        )
        pr["detalle"] = [
            {"mes": _mes_corto(r["mes"]), "cantidad": r["toneladas"], "precio": r["precio"],
             "base": _mes_corto(r["mes_base"]) + (f" ({_nota_base(r)})" if _nota_base(r) else ""),
             "precio_base": r["precio_base"], "pct": r["variacion"] * 100, "bolsa": r["bolsa"], "atipica": bool(r["atipica"])}
            for r in reversed(inflacion.to_dict("records"))
        ]
    return {"pr": pr}


# Inflación de precios interanual con el esquema de Planificación (matriz
# material × mes con Ver precio TCO / bolsa / variación, Análisis individual o
# IPU acumulado del año, y la participación de cada familia en el IPU).
# Pedido del usuario (2026-10-22) para Suministros Industriales; para
# sumarlo a otra categoría basta agregarla aquí.
_CATEGORIAS_CON_INFLACION = {"SUMINISTROS INDUSTRIALES"}
_INFLACION_CATEGORIA = {
    "desfase": 12, "id": "sec-inflacion-categoria", "titulo": "Inflación de precios (interanual)",
    "contra": "el mismo mes del año anterior", "ejemplo": "Ene-26 vs Ene-25",
    "sustituto": "Dic-24, Nov-24…", "base_titulo": "un año antes",
}
_INFLACION_CATEGORIA_FILAS = 40


def _cat_precios_zona(request, categoria, tiene_inventario):
    """Pestaña Precios e IPU completa: la de siempre (_cat_precios) y, en
    _CATEGORIAS_CON_INFLACION, la Inflación de precios interanual."""
    contexto = _cat_precios(request, categoria, tiene_inventario)
    if categoria in _CATEGORIAS_CON_INFLACION:
        base = q.precios_entrada_materiales_real()
        contexto["inf"] = _contexto_evaluacion_precios(
            request, base[base["categoria"] == categoria], _INFLACION_CATEGORIA, "infcat",
            campo="familia", campo_titulo="Familia", unidad=None, max_filas=_INFLACION_CATEGORIA_FILAS, por_familia=True,
        )
    return contexto


# Tramos de plazo de pago de Cuentas por pagar: (días hasta, nombre).
_TRAMOS_PLAZO_PAGO = [(0, "Contado o anticipado"), (30, "Hasta 30 días"), (60, "31 a 60 días"),
                      (90, "61 a 90 días"), (float("inf"), "Más de 90 días")]


def _tramo_plazo(dias):
    """Nombre del tramo de plazo de pago (_TRAMOS_PLAZO_PAGO) de unos días."""
    return next(nombre for hasta, nombre in _TRAMOS_PLAZO_PAGO if dias <= hasta)


def _periodo_cuentas(request, anios):
    """Año y rango (Desde/Hasta) de los segmentadores de Cuentas por pagar."""
    anio_sel = _por_str(request.GET.get("anio_cxp"), [str(a) for a in anios])
    anio_sel = int(anio_sel) if anio_sel else anios[-1]
    return anio_sel, _rango_meses(request, "cxp", anio_sel)


def _cat_cuentas(request, categoria, tiene_inventario):
    """Pestaña Cuentas por pagar: segmentadores propios (Año, Desde, Hasta;
    se alinean con los de Compras, ver zonas.js) para el PMP del período,
    las compras por plazo de pago (tramos y condición) y por proveedor. El
    PMP por año usa los mismos meses de cada año; la evolución mensual es
    toda la historia. El saldo por pagar y su antigüedad quedan pendientes
    de data (partidas abiertas de proveedores)."""
    anios = q.anios_compras_disponibles()
    cx = {"anios": list(reversed(anios)), "anio_sel": None, "pmp_anios": [], "tramos": [], "condiciones": [],
          "proveedores": [], "pmp_grafico": None, "pmp_serie": []}
    if not anios:
        return {"cx": cx}
    anio_sel, rango = _periodo_cuentas(request, anios)
    cx.update({"anio_sel": anio_sel, "rango": rango, "texto": _texto_rango(rango, anio_sel)})

    # PMP de cada año en los mismos meses del período elegido.
    anterior = None
    for anio in anios:
        fila = q.pmp_por_categoria_rango(*_rango_en_anio(rango, anio))
        fila = fila[fila["categoria"] == categoria]
        if fila.empty:
            anterior = None
            continue
        pmp = float(fila["pmp"].iloc[0])
        cx["pmp_anios"].append({"anio": anio, "periodo": _texto_rango(rango, anio), "pmp": pmp,
                                "diferencia": None if anterior is None else pmp - anterior,
                                "seleccionado": anio == anio_sel})
        anterior = pmp

    serie = q.pmp_mensual(categoria)
    cx["pmp_serie"] = [(r.mes, float(r.pmp)) for r in serie.itertuples()]
    cx["pmp_grafico"] = charts.serie_area([(_mes_abrev_guion(m), v) for m, v in cx["pmp_serie"]],
                                          formato_valor=lambda v: f"{v:z.0f}")

    df = q.compras_por_categoria_y_condicion_pago_rango(rango["fecha_ini"], rango["fecha_fin"])
    df = df[df["categoria"] == categoria].copy()
    df["valor_total"] = df["valor_total"].astype(float)
    total = float(df["valor_total"].sum())
    if df.empty or not total:
        return {"cx": cx}
    df["dias"] = df["condicion_pago"].map(q.dias_ponderados_condicion_pago)
    tramos, desde = [], -1.0
    for hasta, nombre in _TRAMOS_PLAZO_PAGO:
        valor = float(df.loc[(df["dias"] > desde) & (df["dias"] <= hasta), "valor_total"].sum())
        tramos.append({"etiqueta": nombre, "valor": valor, "participacion": valor / total * 100,
                       "filtrar": desglose.filtro(f_tramo=nombre)})
        desde = hasta
    cx["tramos"] = charts.barras(tramos)
    condiciones = df.groupby("condicion_pago", as_index=False, dropna=False).agg(
        valor_total=("valor_total", "sum"), dias=("dias", "first"))
    condiciones["participacion"] = condiciones["valor_total"] / total * 100
    cx["condiciones"] = condiciones.sort_values("valor_total", ascending=False).to_dict("records")
    cx["valor_total"] = total
    # En soles: cada día más de plazo de pago es un día de compras que se
    # queda en caja (compras del período ÷ días del período).
    fin_caja = min(rango["fecha_fin"], datetime.date.today())
    cx["caja_por_dia"] = total / max((fin_caja - rango["fecha_ini"]).days + 1, 1)
    cx["pmp_anio"] = float((df["dias"] * df["valor_total"]).sum() / total)
    cx["pct_60"] = float(df.loc[df["dias"] >= 60, "valor_total"].sum() / total * 100)

    # Por proveedor: valor, PMP (días ponderados por valor) y su condición
    # de pago principal (la de más valor), en el período elegido.
    pv = q.compras_por_categoria_proveedor_condicion_rango(rango["fecha_ini"], rango["fecha_fin"])
    pv = pv[pv["categoria"] == categoria].copy()
    if not pv.empty:
        pv["valor_total"] = pv["valor_total"].astype(float)
        pv["dias"] = pv["condicion_pago"].map(q.dias_ponderados_condicion_pago)
        pv["peso"] = pv["dias"] * pv["valor_total"]
        principal = pv.sort_values("valor_total", ascending=False).drop_duplicates("proveedor").set_index("proveedor")
        agr = pv.groupby("proveedor").agg(valor=("valor_total", "sum"), peso=("peso", "sum"),
                                          pedidos=("pedidos", "sum")).sort_values("valor", ascending=False)
        cx["proveedores"] = [{
            "proveedor": nombre, "valor": float(r["valor"]), "pedidos": int(r["pedidos"]),
            "pmp": float(r["peso"] / r["valor"]) if r["valor"] else 0.0,
            "condicion": principal.loc[nombre, "condicion_pago"] or "—",
            "participacion": float(r["valor"]) / total * 100,
        } for nombre, r in agr.iterrows()]

        # Dispersión monto vs días de pago (pedido por el usuario), con el
        # PMP de la categoría como referencia, y su lectura: cuánto se paga
        # antes que el promedio y a qué proveedores grandes (dónde negociar
        # plazo). "Grandes" = los 15 de más monto.
        referencia = cx["pmp_anio"]
        cx["dispersion"] = charts.dispersion_pago(
            [{"nombre": _nombre_corto(p["proveedor"]), "monto": p["valor"], "dias": p["pmp"],
              "detalle": (f"{p['proveedor']}\nCompras: S/ {p['valor']:z,.0f} ({p['participacion']:z.1f}% de la categoría)\n"
                          f"Días de pago: {p['pmp']:z.0f} (PMP de la categoría: {referencia:z.0f})\n"
                          f"Condición principal: {p['condicion']} · {p['pedidos']:,} pedidos"),
              "filtrar": desglose.filtro(f_proveedor=p["proveedor"])}
             for p in cx["proveedores"]],
            referencia, _soles_marca,
        )
        antes = [p for p in cx["proveedores"] if p["pmp"] < referencia - 0.5]
        cx["antes"] = {"proveedores": len(antes), "valor": sum(p["valor"] for p in antes),
                       "pct": sum(p["valor"] for p in antes) / total * 100}
        cx["oportunidades"] = [p for p in cx["proveedores"][:15] if p["pmp"] < referencia - 5][:5]
    return {"cx": cx}
