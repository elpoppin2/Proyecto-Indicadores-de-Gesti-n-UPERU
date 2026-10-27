"""Planificación > Consumo e Inventario: consumo vs plan, consumo específico (kg por T) y proyección de inventario S&OP."""

import calendar
import datetime

import pandas as pd
from dashboard import queries as q

from .. import charts
from .comun import _mes_corto, _opciones_categoria_centro, _opciones_material, _por_materiales, _por_str, _selector_material


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
    return f"{factor * 1000:z,.0f} kg/T"


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
                            f"Consumo {r.consumo:z,.0f} T · Producción de {producto} {r.produccion:z,.0f} T")
                           for r in g.itertuples()],
            })
        referencias = [{"valor": f["factor_12"], "texto": f"Prom. 12 meses {q.nombre_centro(f['centro'])}: {f['factor_12']:z,.0f} kg/T",
                        "clase": f"serie-{i}"} for i, f in enumerate(filas) if f["factor_12"]]
        grafico = charts.lineas_mensuales(series, _mes_corto, lambda v: f"{v:z,.0f}", referencias=referencias)
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
                                         f"({' + '.join(map(q.nombre_centro, centros_sel))}): {v:z,.0f} T") for m, v in serie.items()],
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


_COBERTURA_MESES_SIGUIENTES = 3


def _cobertura_meses(stock, salidas):
    """Meses que cubre `stock` al ritmo promedio de `salidas` (T por mes);
    None sin salidas planificadas. Un stock negativo (quiebre) cubre 0 meses."""
    promedio = sum(salidas) / len(salidas) if salidas else 0.0
    return max(stock, 0.0) / promedio if promedio > 0 else None


def _estado_cobertura(stock, cobertura, minimo, maximo):
    """Estado de una celda de la matriz de cobertura, en meses (mínimo y
    máximo ya pasados a meses; None si no hay)."""
    if stock < 0:
        return "quiebre"
    if cobertura is None:
        return "sin_parametro"
    if minimo is not None and cobertura < minimo:
        return "bajo_minimo"
    if maximo is not None and cobertura > maximo:
        return "sobre_maximo"
    return "en_rango" if minimo is not None or maximo is not None else "sin_parametro"


def _fila_cobertura(material, texto, centro, inicial, finales, flujo, minimos, maximos, parte=""):
    """Una fila de la matriz de cobertura proyectada (meses): stock ÷ `flujo`
    promedio (T por mes) de los _COBERTURA_MESES_SIGUIENTES meses siguientes.
    Color por meses (pedido del usuario, 2026-10-26: Carbón Nacional de
    Atocongo salía "bajo mínimo" en dic-26 con 9.3 meses porque ene–mar casi no
    consume): mínimo y máximo (T) ÷ flujo promedio de todo el horizonte. Las
    filas de una `parte` de un centro de acopio van sin color: el mínimo y
    máximo del S&OP son del stock físico."""
    promedio = sum(flujo) / len(flujo) if flujo else 0.0

    def a_meses(valor):
        return None if parte or valor is None or valor != valor or promedio <= 0 else float(valor) / promedio

    celdas = []
    for i, v in enumerate(finales):
        cobertura = _cobertura_meses(v, flujo[i + 1:i + 1 + _COBERTURA_MESES_SIGUIENTES] or flujo[i:i + 1])
        celdas.append({"valor": v, "cobertura": cobertura,
                       "estado": _estado_cobertura(v, cobertura, a_meses(minimos[i]), a_meses(maximos[i]))})
    return {"material_codigo": material, "texto_breve": texto, "centro": centro, "parte": parte, "stock_inicial": inicial,
            "minimo_meses": a_meses(minimos[0]), "maximo_meses": a_meses(maximos[0]),
            "cobertura_inicial": _cobertura_meses(inicial, flujo[:_COBERTURA_MESES_SIGUIENTES]), "celdas": celdas}


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
        return f"{valor:z,.0f} T"

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
    # estado frente al mínimo/máximo (color de la celda). La misma matriz en
    # cobertura (meses), pedido del usuario (2026-10-21): stock ÷ salidas
    # planificadas (consumo + traslados a otro centro) promedio de los
    # _COBERTURA_MESES_SIGUIENTES meses que siguen (los que queden en el
    # horizonte; el último mes, con las suyas); ver _fila_cobertura. En un
    # centro de acopio (q.ACOPIO_COBERTURA) la cobertura va en dos filas, con
    # la proporción de la Cobertura de stock al cierre de partida: lo propio
    # (contra su consumo) y lo en acopio (contra el consumo del destino).
    partes = q.participacion_propia_acopio(fecha_inicial)
    consumo_de = {(r.material_codigo, r.centro, r.mes): float(r.consumo) for r in base.itertuples()}
    matriz = []
    for (material, centro), grupo in grupos:
        grupo = grupo.sort_values("mes")
        negativos = grupo[grupo["stock_final"] < 0]
        minimo, maximo = grupo["stock_minimo"].iloc[0], grupo["stock_maximo"].iloc[0]
        texto = grupo["texto_breve"].iloc[0]
        inicial = float(grupo["stock_inicial"].iloc[0])
        salidas = [float(c + t) for c, t in zip(grupo["consumo"], grupo["traslado"])]
        finales = [float(v) for v in grupo["stock_final"]]
        minimos, maximos = list(grupo["stock_minimo"]), list(grupo["stock_maximo"])
        if (material, centro) in partes:
            propia, destino = partes[(material, centro)]
            filas_cobertura = [
                _fila_cobertura(material, texto, centro, inicial * propia, [v * propia for v in finales],
                                [float(v) for v in grupo["consumo"]], minimos, maximos, parte="propio"),
                _fila_cobertura(material, texto, centro, inicial * (1 - propia), [v * (1 - propia) for v in finales],
                                [consumo_de.get((material, destino, m), 0.0) for m in grupo["mes"]], minimos, maximos,
                                parte=f"acopio para {q.nombre_centro(destino).title()}"),
            ]
        else:
            filas_cobertura = [_fila_cobertura(material, texto, centro, inicial, finales, salidas, minimos, maximos)]
        matriz.append({
            "material_codigo": material, "texto_breve": texto, "centro": centro,
            "stock_inicial": inicial,
            "stock_minimo": None if minimo != minimo else float(minimo),
            "stock_maximo": None if maximo != maximo else float(maximo),
            "celdas": [{"valor": v, "estado": e} for v, e in zip(finales, grupo["estado"])],
            "quiebre": negativos["mes"].iloc[0] if not negativos.empty else None,
            "quiebre_corto": _mes_corto(negativos["mes"].iloc[0]) if not negativos.empty else "",
            "filas_cobertura": filas_cobertura,
        })
    matriz.sort(key=lambda f: (f["quiebre"] is None, f["quiebre"] or datetime.date.max, f["centro"], f["material_codigo"]))
    matriz_cobertura = [fc for f in matriz for fc in f["filas_cobertura"]]

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
        "resumen": resumen, "matriz": matriz, "matriz_cobertura": matriz_cobertura, "un_material": un_material,
        "cobertura_meses_siguientes": _COBERTURA_MESES_SIGUIENTES,
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
