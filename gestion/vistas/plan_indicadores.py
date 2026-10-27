"""Planificación > Indicadores: números clave de cada pestaña y los paneles (traslados, ritmo, precisión del consumo, salud del inventario, primer quiebre)."""

import calendar
import datetime
import math

from dashboard import queries as q

from .. import charts
from .comun import _PANEL_FILAS, _mes_corto, _nombre_material, _signo
from .plan_transporte import _clase_tiempo, _minutos_texto, _paneles_transporte
from .zonas import _ZONAS_LOGISTICA, _ZONAS_PLANIFICACION


def _pct_texto(valor, signo=False):
    """Porcentaje con un decimal para los números clave (sin "-0.0%")."""
    if valor is None:
        return "—"
    valor = 0.0 if abs(valor) < 0.05 else valor
    return f"{'+' if signo and valor > 0 else ''}{valor:z,.1f}%"


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
            kpi(f"{pl['total_real']:z,.0f} T", f"Ingresos físicos (báscula) de {pl['total_plan']:z,.0f} T planificadas"),
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
            kpis.append(kpi(f"S/ {en['resumen']['costo_gcal']:z,.1f}", "Costo de los carbones por Gcal"))
    elif zona == "transporte":
        tr = c.get("logistica_transporte")
        if tr and not tr.get("vacio"):
            r, ti, ag = tr["resumen"], tr["tiempo"], tr["agua"]
            kpis.append(kpi(f"{r['viajes']:,}", f"Viajes recibidos · {tr['mes_texto']} ({r['toneladas']:z,.0f} T)"))
            kpis.append(kpi(f"{r['t_viaje']:z,.1f} T", "Carga promedio por viaje"))
            if ti["mediana"] is not None:
                kpis.append(kpi(_minutos_texto(ti["mediana"]), "Tiempo en planta (mediana)",
                                {"bien": "ok", "mal": "alerta"}.get(_clase_tiempo(ti["mediana"]), "")))
            if ag["pct"] is not None:
                kpis.append(kpi(f"{ag['pct']:z,.1f}%", "Agua transportada (húmedo − seco)"))
    return kpis
_CUMPLIMIENTO_BIEN, _CUMPLIMIENTO_MAL = 95, 80
_COBERTURA_RIESGO, _COBERTURA_AJUSTADA = 30, 60


# Umbrales de los paneles por centro de la pestaña Indicadores (pedidos por el
# usuario el 2026-10-01): brecha del ritmo de abastecimiento en días de plan,
# precisión del plan de consumo y % de materiales en rango mín–máx.
_RITMO_AVISO, _RITMO_ALERTA = 1, 3
_PRECISION_BUENA, _PRECISION_REGULAR = 85, 70
_SALUD_BUENA, _SALUD_REGULAR = 80, 60


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
        detalle = (f"{nombre(material)} · {mes_txt}\nPlan para {centro(q.CENTRO_CONDORCOCHA)} {plan_mat:z,.0f} T · "
                   f"trasladado desde {origen} {real:z,.0f} T ({carros:,} carros) · {v:z,.1f}%")
        for r in del_material[del_material["origen"] != origen].itertuples():
            detalle += f"\nAdemás llegó directo desde {r.origen} {r.toneladas:z,.0f} T"
        filas.append({
            "nombre": nombre(material), "sub": f"{carros:,} carros", "valor": v, "texto": f"{v:z,.1f}%",
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
            f"{nombre(r.material_codigo)} {r.toneladas:z,.0f} T" for r in otros.itertuples()) + " (su plan mezcla compras directas)."
    cumpl = total_real / total_plan * 100
    tono, estado = (("ok", "En meta") if cumpl >= _CUMPLIMIENTO_BIEN else
                    ("alerta", "Crítico") if cumpl < _CUMPLIMIENTO_MAL else ("aviso", "Bajo la meta"))
    sub = f"{mes_txt} · {total_real:z,.0f} de {total_plan:z,.0f} T planificadas para Condorcocha"
    if mes == hoy_mes:
        sub += " · mes en curso"
    panel("cumplimiento", titulo, _pct_texto(cumpl), sub, "escala",
          charts.barras_escala(filas, 0, tope(filas), referencias=[(100, "Plan")]), tono, estado,
          leyenda=[("bien", f"≥ {_CUMPLIMIENTO_BIEN}%"), ("regular", f"{_CUMPLIMIENTO_MAL}–{_CUMPLIMIENTO_BIEN}%"),
                   ("mal", f"< {_CUMPLIMIENTO_MAL}%")], nota=nota)


def _panel_cumplimiento_material(panel, vacio, tope, pl, base, centro, mes_txt):
    """Cumplimiento del plan de abastecimiento material por material (mismo
    mes y filtros que la Evaluación mensual), para las páginas con pocos
    materiales (Logística de Materias Primas)."""
    titulo = "Cumplimiento por material"
    con_plan = base[base["cantidad_plan"] > 0].sort_values("cantidad_plan", ascending=False)
    if con_plan.empty:
        vacio("cumplimiento", titulo)
        return
    filas = []
    for r in con_plan.itertuples():
        v = r.cantidad_real / r.cantidad_plan * 100
        filas.append({
            "nombre": _nombre_material(r.texto_breve or r.material_codigo), "sub": centro(r.centro).title(),
            "valor": v, "texto": f"{v:z,.1f}%",
            "clase": "bien" if v >= _CUMPLIMIENTO_BIEN else "mal" if v < _CUMPLIMIENTO_MAL else "regular",
            "detalle": f"{r.texto_breve} ({centro(r.centro)}) · {mes_txt}\nPlan {r.cantidad_plan:z,.0f} T · "
                       f"Real {r.cantidad_real:z,.0f} T · diferencia {_signo(r.cantidad_real - r.cantidad_plan, 0)} T",
        })
    en_meta = sum(f["clase"] == "bien" for f in filas)
    tono, estado = (("ok", "Todos en meta") if en_meta == len(filas) else
                    ("alerta", "Revisar") if any(f["clase"] == "mal" for f in filas) else ("aviso", "Bajo la meta"))
    panel("cumplimiento", titulo, f"{en_meta} de {len(filas)}", f"Materiales en meta (≥ {_CUMPLIMIENTO_BIEN}%) · {mes_txt}",
          "escala", charts.barras_escala(filas, 0, tope(filas), referencias=[(100, "Plan")]), tono, estado,
          leyenda=[("bien", f"≥ {_CUMPLIMIENTO_BIEN}%"), ("regular", f"{_CUMPLIMIENTO_MAL}–{_CUMPLIMIENTO_BIEN}%"),
                   ("mal", f"< {_CUMPLIMIENTO_MAL}%")],
          nota="Ingresos físicos (báscula) ÷ plan de abastecimiento de cada material en su centro.")


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
                           f"{centro(cod)} · {f['fecha']:%d/%m}\nReal acumulado {f['acumulado_real']:z,.0f} T "
                           f"({pct:z,.1f}% del plan del mes)\nPlan a la fecha {f['acumulado_plan']:z,.0f} T "
                           f"({f['dia'] / dias * 100:z,.1f}%)\nIngresó ese día {f['real_diario']:z,.0f} T · {f['carros']:,} carros"))
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
    g = charts.lineas_indice(series, [str(d) if d else "" for d in range(dias + 1)], lambda v: f"{v:z,.0f}%",
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
            "nombre": centro(cod).title(), "sub": f"sesgo {_signo(sesgo)}%", "valor": precision, "texto": f"{precision:z,.1f}%",
            "clase": "bien" if precision >= _PRECISION_BUENA else "regular" if precision >= _PRECISION_REGULAR else "mal",
            "detalle": f"{centro(cod)} · {mes_txt} · {a['n']} materiales\nPrecisión {precision:z,.1f}% · error ponderado "
                       f"{error:z,.1f}%\nSesgo {_signo(sesgo)}% ({'se consumió más' if sesgo > 0 else 'se consumió menos'} "
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
    sub = (f"{mes_txt} · error ponderado {error_t:z,.1f}% · sesgo {_signo(sesgo_t)}% "
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
    # El mín/máx es del stock físico: lo propio y lo en acopio de un centro de
    # acopio se juntan (el consumo, solo el del centro: el de la fila de
    # acopio es el del destino).
    fisico = {}
    for f in cob["filas"]:
        k = (f["material_codigo"], f["centro"])
        a = fisico.setdefault(k, {**f, "stock_cierre": 0.0, "consumo_diario": 0.0})
        a["stock_cierre"] += f["stock_cierre"]
        if not str(f.get("parte") or "").startswith("acopio"):
            a["consumo_diario"] += f["consumo_diario"] or 0.0
    por_centro = {}
    for f in fisico.values():
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
            "nombre": centro(cod).title(), "sub": f"{n} materiales", "texto": f"{dias:z,.0f} d" if dias is not None else "—",
            "segmentos": [{
                "valor": len(a["estados"][e]), "clase": f"col-{e}",
                "detalle": f"{centro(cod)} · {t}: {len(a['estados'][e])} de {n} materiales\n"
                           + ", ".join(a["estados"][e][:8]) + ("…" if len(a["estados"][e]) > 8 else ""),
            } for e, t in estados],
        })
    conteo = {e: sum(len(a["estados"][e]) for a in por_centro.values()) for e, _ in estados}
    con_parametro = sum(conteo.values()) - conteo["sin_parametro"]
    stock_t, diario_t = sum(a["stock"] for a in por_centro.values()), sum(a["diario"] for a in por_centro.values())
    cobertura_t = f" · cobertura {stock_t / diario_t:z,.0f} días" if diario_t > 0 else ""
    if con_parametro:
        en_rango = conteo["en_rango"] / con_parametro * 100
        cifra = _pct_texto(en_rango)
        tono, estado = (("ok", "Saludable") if en_rango >= _SALUD_BUENA else
                        ("aviso", "Revisar") if en_rango >= _SALUD_REGULAR else ("alerta", "Desbalanceado"))
        sub = f"Materiales entre su mínimo y máximo · stock al {fecha:%d/%m/%Y}{cobertura_t}"
    else:
        cifra, tono, estado = (f"{stock_t / diario_t:z,.0f} d" if diario_t > 0 else "—"), "", "Sin mín/máx"
        sub = f"Cobertura ponderada · stock al {fecha:%d/%m/%Y} (sin mínimos/máximos S&OP para ese mes)"
    panel("inventario", titulo, cifra, sub, "pilas", charts.pilas_horizontales(filas), tono, estado,
          leyenda=[(f"col-{e}", t) for e, t in estados if conteo[e] or e in ("bajo_minimo", "en_rango", "sobre_maximo")],
          nota="Materiales por estado de su stock de cierre (MB5B) frente al mínimo/máximo S&OP del mes. A la derecha, "
               "cobertura ponderada: stock total ÷ consumo diario total de los últimos 12 meses.")


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


def _paneles_indicadores(request, c, mes=None, zonas=None, por_material=False):
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
    CONDORCOCHA), no por código, pedido por el usuario.
    `zonas`: solo los paneles de esas zonas (None = todos). `por_material`:
    agrega el cumplimiento por material (páginas con pocos materiales)."""
    nombres_zona = {clave: nombre for clave, nombre, _, _ in [*_ZONAS_PLANIFICACION, *_ZONAS_LOGISTICA]}
    hoy_mes = datetime.date.today().replace(day=1)
    paneles = []
    centro = q.nombre_centro

    def panel(zona, titulo, cifra, sub, tipo, g, tono="", estado="", leyenda=(), nota="", ancho=False):
        if zonas is not None and zona not in zonas:
            return
        paneles.append({
            "zona": zona, "zona_nombre": nombres_zona[zona], "titulo": titulo, "cifra": cifra, "sub": sub,
            "tipo": tipo, "g": g, "tono": tono, "estado": estado, "leyenda": list(leyenda), "nota": nota,
            "ancho": ancho, "vacio": "",
        })

    def vacio(zona, titulo, ancho=False, mensaje=None):
        """Panel sin datos (el mes elegido no tiene, o los filtros de la pestaña)."""
        if zonas is not None and zona not in zonas:
            return
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
                "nombre": centro(cod).title(), "sub": f"{n} materiales", "valor": v, "texto": f"{v:z,.1f}%",
                "clase": "bien" if v >= _CUMPLIMIENTO_BIEN else "mal" if v < _CUMPLIMIENTO_MAL else "regular",
                "detalle": f"{centro(cod)} · {mes_txt}\nPlan {r.plan:z,.0f} T · Real {r.real:z,.0f} T · {v:z,.1f}%\n"
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
            sub = f"{mes_txt} · {pl['total_real']:z,.0f} de {pl['total_plan']:z,.0f} T planificadas"
            if pl["mes"] == hoy_mes:
                sub += " · mes en curso: el plan es del mes completo"
            panel("cumplimiento", titulo, _pct_texto(cumpl), sub, "escala",
                  charts.barras_escala(filas, 0, tope(filas), referencias=[(100, "Plan")]), tono, estado,
                  leyenda=[("bien", f"≥ {_CUMPLIMIENTO_BIEN}%"), ("regular", f"{_CUMPLIMIENTO_MAL}–{_CUMPLIMIENTO_BIEN}%"),
                           ("mal", f"< {_CUMPLIMIENTO_MAL}%")],
                  nota="Ingresos físicos (báscula) ÷ plan de abastecimiento de cada centro, "
                       + ("los materiales de esta página." if q.alcance_actual() else "todos sus materiales."))
        if por_material:
            _panel_cumplimiento_material(panel, vacio, tope, pl, base, centro, mes_txt)

    _panel_traslados(panel, vacio, tope, pl, centro, hoy_mes)
    _panel_ritmo(panel, vacio, pl, centro, hoy_mes)
    if zonas is not None and "transporte" in zonas:  # solo Logística de Materias Primas
        _paneles_transporte(panel, vacio, c.get("logistica_transporte"))

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
                "nombre": centro(cod).title(), "sub": f"{a['materiales']} materiales", "valor": v, "texto": f"{v:z,.1f}%",
                "clase": "bien" if abs(v - 100) <= tol else "regular" if v > 100 else "mal",
                "detalle": f"{centro(cod)} · {q.formato_mes_anio(co['mes_sel'])}\n"
                           f"Plan{' a la fecha' if en_curso else ''} {a['plan']:z,.0f} T · Consumo real {a['real']:z,.0f} T · {v:z,.1f}%\n"
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
        sub = f"{q.formato_mes_anio(co['mes_sel'])} · {co['total_real']:z,.0f} de {plan_total:z,.0f} T"
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
            "valor": f["var_3_vs_12"], "texto": f"{f['var_3_vs_12']:+z,.0f}%",
            "clase": {"alto": "regular", "bajo": "mal", "normal": "bien"}.get(f["estado"], "neutra"),
            "detalle": f"{f['texto_breve']} ({centro(f['centro'])}) ·kg por T de {f['producto_texto'].lower()}\n"
                       f"Últimos 3 meses {f['factor_3']:z,.0f} kg/T · {fa['meses_factor']} meses {f['factor_12']:z,.0f} kg/T "
                       f"({f['var_3_vs_12']:+z,.1f}%)",
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
            "nombre": _nombre_material(f["texto_breve"]),
            "sub": centro(f["centro"]) + (f" · {f['parte']}" if f.get("parte") else ""), "valor": f["cobertura_dias"],
            "texto": f"{f['cobertura_dias']:z,.0f} d",
            "clase": "mal" if f["cobertura_dias"] < _COBERTURA_RIESGO else "regular" if f["cobertura_dias"] < _COBERTURA_AJUSTADA else "neutra",
            "detalle": f"{f['texto_breve']} ({centro(f['centro'])}{', ' + f['parte'] if f.get('parte') else ''})\n"
                       f"Stock {f['stock_cierre']:z,.0f} T · consumo {f['consumo_mensual']:z,.0f} T/mes\n"
                       f"Cobertura {f['cobertura_dias']:z,.0f} días ({f['cobertura_meses']:z,.1f} meses)",
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
              charts.columnas([m["corto"] for m in p["meses"]], pilas, lambda v: f"{v:z,.0f}", ancho=1040, alto=230),
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
              charts.columnas(etiquetas, pilas, lambda v: f"{v:+z,.0f}%" if v else "0%", linea=linea, ancho=1040, alto=240),
              tono, estado,
              leyenda=[("col-sube", "Precio sube vs el mismo mes del año anterior"), ("col-baja", "Precio baja"),
                       ("linea-ipu", "IPU acumulado del año")],
              nota="Canasta de materiales comprados cada mes, ponderada por lo comprado.", ancho=True)

    return paneles
