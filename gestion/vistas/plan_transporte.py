"""Logística de Materias Primas > Transporte: viajes de recepción, tiempos en planta y placas."""

import datetime

import pandas as pd
from dashboard import queries as q

from .. import charts
from .comun import _PANEL_FILAS, _mes_corto, _nombre_corto, _nombre_material, _opciones_material, _por_materiales, _por_str, _selector_material


# Transporte de Logística de Materias Primas (pedido del usuario, 2026-10-19):
# indicadores de los tickets de báscula ZMM056 (queries.viajes_recepcion).
_DIAS_SEMANA = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
# Tramos de carga por viaje (pesaje neto húmedo, T): (desde, hasta, texto, color).
_TRAMOS_CARGA = ((None, 30, "Menos de 30 T", "col-carga-0"), (30, 33, "30 a 33 T", "col-carga-1"),
                 (33, 36, "33 a 36 T", "col-carga-2"), (36, None, "36 T o más", "col-carga-3"))
# Tiempo en planta (mediana, minutos): hasta 1 h bien, de 1 a 2 h regular, más de 2 h mal.
_TIEMPO_BIEN, _TIEMPO_MAL = 60, 120
_TIEMPO_MEDIBLE = 0.5  # parte mínima de viajes con los dos pesajes para medir el tiempo de un material
_TRANSPORTE_FILAS = 15


def _minutos_texto(minutos):
    if minutos is None or minutos != minutos:
        return "—"
    minutos = int(round(minutos))
    return f"{minutos // 60} h {minutos % 60:02d} min" if minutos >= 60 else f"{minutos} min"


def _clase_tiempo(minutos):
    return "bien" if minutos <= _TIEMPO_BIEN else "regular" if minutos <= _TIEMPO_MAL else "mal"


def _contexto_transporte(request):
    """Pestaña Transporte de Logística: tiempo en planta, carga por viaje,
    transportistas, llegadas por día y hora, flota y agua transportada de los
    viajes recibidos en el mes (Mes/Centro/Material propios). Cada bloque trae
    su gráfico listo para la sección y para la pestaña Indicadores."""
    viajes = q.viajes_recepcion()
    if viajes.empty:
        return None
    hoy_mes = datetime.date.today().replace(day=1)
    meses = sorted(viajes["mes"].unique(), reverse=True)
    mes = _por_str(request.GET.get("mes_transp", ""), meses) or (hoy_mes if hoy_mes in meses else meses[0])
    centros = sorted(viajes["centro"].unique())
    centro_sel = request.GET.get("centro_transp", "Todos")
    if centro_sel not in centros:
        centro_sel = "Todos"
    base = viajes if centro_sel == "Todos" else viajes[viajes["centro"] == centro_sel]
    materiales_op = _opciones_material(base.rename(columns={"descripcion": "texto_breve"}))
    material_sel = _selector_material(request, "transp", materiales_op)
    base = _por_materiales(base, material_sel)
    # Un material que en el mes se pesó casi siempre con tara registrada (la
    # Caliza Pucará y Roca Fuerte en Atocongo) no mide su tiempo en planta: los
    # pocos viajes con dos pesajes no lo representan. Cuenta solo si al menos
    # la mitad de sus viajes del mes tiene los dos pesajes.
    medible = base.groupby(["mes", "material_codigo", "centro"])["minutos"].transform(lambda s: s.notna().mean() >= _TIEMPO_MEDIBLE)
    base = base.assign(minutos=base["minutos"].where(medible))
    d = base[base["mes"] == mes]
    mes_txt = q.formato_mes_anio(mes)
    ctx = {
        "opciones_mes": meses, "mes": mes, "mes_texto": mes_txt, "en_curso": mes == hoy_mes,
        "centros": [{"codigo": "Todos", "texto": "Todos"}] + [{"codigo": c, "texto": q.nombre_centro(c)} for c in centros],
        "centro_sel": centro_sel, "materiales": materiales_op, "material_sel": material_sel, "vacio": d.empty,
    }
    if d.empty:
        return ctx
    centro = q.nombre_centro
    n, toneladas = len(d), float(d["toneladas"].sum())

    def etiqueta(g):
        return _nombre_material(g["descripcion"].iloc[0])

    ctx["resumen"] = {"viajes": n, "toneladas": toneladas, "t_viaje": toneladas / n,
                      "transportistas": int(d["transportista"].nunique()),
                      "placas": int(d.loc[d["placa"] != "", "placa"].nunique()), "dias": int(d["fecha"].nunique())}

    # 1. Tiempo en planta: entre el pesaje lleno y el vacío.
    filas = []
    for (cod, cen), g in d.groupby(["material_codigo", "centro"]):
        m = g["minutos"].dropna()
        filas.append({"material": cod, "nombre": etiqueta(g), "centro": cen, "viajes": len(g), "medidos": len(m),
                      "tara": int(g["tara_registrada"].sum()),
                      "mediana": float(m.median()) if len(m) else None, "p90": float(m.quantile(0.9)) if len(m) else None})
    filas.sort(key=lambda f: (f["mediana"] is None, -(f["mediana"] or 0)))
    medidos = d["minutos"].dropna()
    con_tiempo = [f for f in filas if f["mediana"] is not None]
    barras = [{"nombre": f["nombre"], "sub": centro(f["centro"]).title(), "valor": f["mediana"],
               "texto": f"{f['mediana']:z,.0f} min", "clase": _clase_tiempo(f["mediana"]),
               "detalle": f"{f['nombre']} ({centro(f['centro'])}) · {mes_txt}\nMediana {_minutos_texto(f['mediana'])} · "
                          f"peor 10%: más de {_minutos_texto(f['p90'])}\n{f['medidos']:,} de {f['viajes']:,} viajes medidos"}
              for f in con_tiempo[:_PANEL_FILAS]]
    tope = min(max(150, max((b["valor"] for b in barras), default=0) * 1.1), 480)
    tendencia = []
    for i, (cen, g) in enumerate(base.groupby("centro")):
        por_mes = g.groupby("mes")["minutos"].agg(["median", "count"])
        tendencia.append({"nombre": centro(cen).title(), "clase": f"serie-{i}", "puntos": [
            (m, float(r["median"]), f"{centro(cen)} · {q.formato_mes_anio(m)}\nMediana {_minutos_texto(r['median'])} ({int(r['count']):,} viajes medidos)")
            for m, r in por_mes.iterrows() if r["count"]]})
    ctx["tiempo"] = {
        "filas": filas, "mediana": float(medidos.median()) if len(medidos) else None,
        "p90": float(medidos.quantile(0.9)) if len(medidos) else None, "medidos": len(medidos),
        "sin_medir": sorted({f"{f['nombre']} ({centro(f['centro']).title()})" for f in filas if not f["medidos"]}),
        "grafico": {"titulo": "Tiempo en planta por material", "tipo": "escala",
                    "g": charts.barras_escala(barras, 0, tope, referencias=[(_TIEMPO_BIEN, "1 h"), (_TIEMPO_MAL, "2 h")])},
        "tendencia": {"titulo": "Tiempo en planta por mes", "tipo": "lineas",
                      "g": charts.lineas_mensuales(tendencia, _mes_corto, lambda v: f"{v:z,.0f} min", desde_cero=True)},
        "leyenda_tendencia": [(s["clase"], s["nombre"]) for s in tendencia if s["puntos"]],
    }

    # 3. Carga por viaje: viajes por tramo de toneladas.
    filas = []
    for (cod, cen), g in sorted(d.groupby(["material_codigo", "centro"]), key=lambda x: -len(x[1])):
        t = g["toneladas"]
        segmentos = []
        for desde, hasta, texto, clase in _TRAMOS_CARGA:
            dentro = pd.Series(True, index=t.index)
            if desde is not None:
                dentro &= t >= desde
            if hasta is not None:
                dentro &= t < hasta
            k = int(dentro.sum())
            segmentos.append({"valor": k, "clase": clase,
                              "detalle": f"{etiqueta(g)} ({centro(cen)}) · {texto}\n{k:,} de {len(g):,} viajes ({k / len(g) * 100:z,.0f}%)"})
        filas.append({"nombre": etiqueta(g), "sub": centro(cen).title(), "texto": f"{t.mean():z,.1f} T", "segmentos": segmentos,
                      "material": cod, "centro": cen, "viajes": len(g), "toneladas": float(t.sum()), "t_viaje": float(t.mean()),
                      "p10": float(t.quantile(0.1)), "p90": float(t.quantile(0.9)),
                      "bajo_30": segmentos[0]["valor"] / len(g) * 100})
    ctx["carga"] = {
        "filas": filas, "bajo_30": float((d["toneladas"] < 30).mean() * 100),
        "grafico": {"titulo": "Carga por viaje", "tipo": "pilas", "g": charts.pilas_horizontales(filas[:_PANEL_FILAS])},
        "leyenda": [(clase, texto) for _, _, texto, clase in _TRAMOS_CARGA],
    }

    # 4. Transportistas (por código; el nombre más frecuente).
    clave = d["transportista_codigo"].where(d["transportista_codigo"] != "", d["transportista"])
    filas = []
    for _, g in d.groupby(clave):
        m = g["minutos"].dropna()
        filas.append({"nombre": g["transportista"].mode().iloc[0], "codigo": g["transportista_codigo"].iloc[0],
                      "viajes": len(g), "toneladas": float(g["toneladas"].sum()),
                      "pct": float(g["toneladas"].sum()) / toneladas * 100, "t_viaje": float(g["toneladas"].mean()),
                      "mediana": float(m.median()) if len(m) else None, "p90": float(m.quantile(0.9)) if len(m) else None,
                      "placas": int(g.loc[g["placa"] != "", "placa"].nunique()),
                      "materiales": ", ".join(sorted({_nombre_material(x) for x in g["descripcion"]}))})
    filas.sort(key=lambda f: -f["toneladas"])
    barras = [{"nombre": _nombre_corto(f["nombre"], 26), "sub": f"{f['viajes']:,} viajes", "valor": f["pct"],
               "texto": f"{f['pct']:z,.1f}%", "clase": "neutra",
               "detalle": f"{f['nombre']} · {mes_txt}\n{f['toneladas']:z,.0f} T en {f['viajes']:,} viajes "
                          f"({f['t_viaje']:z,.1f} T por viaje)\n{f['placas']} camiones · {f['materiales']}"}
              for f in filas[:6]]
    ctx["transportistas"] = {
        "filas": filas, "top3": sum(f["pct"] for f in filas[:3]),
        "grafico": {"titulo": "Transportistas", "tipo": "escala",
                    "g": charts.barras_escala(barras, 0, min(100, max(b["valor"] for b in barras) * 1.15))},
    }

    # 5. Llegadas por día de la semana y hora (pesaje lleno).
    con_hora = d[d["llegada"].notna()]
    tabla = pd.crosstab(con_hora["llegada"].dt.dayofweek, con_hora["llegada"].dt.hour).reindex(
        index=range(7), columns=range(24), fill_value=0)
    maximo = int(tabla.values.max()) or 1
    por_hora = tabla.sum(axis=0)
    pico = int(por_hora.idxmax())
    noche = int(por_hora[[h for h in range(24) if h >= 20 or h < 6]].sum())
    total_hora = int(por_hora.sum()) or 1
    ctx["llegadas"] = {
        "horas": [f"{h:02d}" for h in range(24)],
        "filas": [{"dia": _DIAS_SEMANA[i], "celdas": [
            {"n": int(v), "nivel": f"{v / maximo:z.2f}",
             "detalle": f"{_DIAS_SEMANA[i]} de {h:02d}:00 a {h + 1:02d}:00 · {int(v):,} camiones en {mes_txt}"}
            for h, v in tabla.loc[i].items()]} for i in range(7)],
        "hora_pico": f"{pico:02d}:00 a {pico + 1:02d}:00", "pct_pico": por_hora[pico] / total_hora * 100,
        "pct_noche": noche / total_hora * 100, "con_hora": len(con_hora),
        "dia_pico": _DIAS_SEMANA[int(tabla.sum(axis=1).idxmax())],
        "grafico": {"titulo": "Llegadas por hora", "tipo": "columnas", "g": charts.columnas(
            [f"{h:02d}" for h in range(24)],
            [[{"valor": int(v), "clase": "col-compras", "detalle": f"{h:02d}:00 a {h + 1:02d}:00 · {int(v):,} camiones"}]
             for h, v in por_hora.items()], lambda v: f"{v:z,.0f}")},
    }

    # 6. Flota: camiones (placas) del mes.
    con_placa = d[d["placa"] != ""]
    por_placa = con_placa.groupby("placa").agg(
        viajes=("toneladas", "size"), toneladas=("toneladas", "sum"), dias=("fecha", "nunique"),
        transportista=("transportista", lambda s: s.mode().iloc[0]), mediana=("minutos", "median"),
    ).sort_values(["viajes", "toneladas"], ascending=False)
    por_placa["viajes_dia"] = por_placa["viajes"] / por_placa["dias"]
    top = por_placa.head(_TRANSPORTE_FILAS)
    barras = [{"nombre": placa, "sub": _nombre_corto(r.transportista, 20), "valor": float(r.viajes_dia),
               "texto": f"{r.viajes_dia:z,.1f}", "clase": "neutra",
               "detalle": f"Placa {placa} · {r.transportista}\n{int(r.viajes)} viajes en {int(r.dias)} días "
                          f"({r.viajes_dia:z,.1f} por día) · {r.toneladas:z,.0f} T"}
              for placa, r in top.head(_PANEL_FILAS).sort_values("viajes_dia", ascending=False).iterrows()]
    ctx["flota"] = {
        "placas": len(por_placa), "viajes_por_placa": float(por_placa["viajes"].mean()) if len(por_placa) else 0.0,
        "viajes_placa_dia": float(por_placa["viajes"].sum() / por_placa["dias"].sum()) if len(por_placa) else 0.0,
        "top10": float(por_placa["viajes"].head(10).sum() / len(con_placa) * 100) if len(con_placa) else 0.0,
        "filas": [{"placa": placa, "transportista": r.transportista, "viajes": int(r.viajes), "toneladas": float(r.toneladas),
                   "dias": int(r.dias), "viajes_dia": float(r.viajes_dia),
                   "mediana": None if r.mediana != r.mediana else float(r.mediana)} for placa, r in top.iterrows()],
        "grafico": {"titulo": "Viajes por día de los camiones con más viajes", "tipo": "escala",
                    "g": charts.barras_escala(barras, 0, max([b["valor"] for b in barras] + [1]) * 1.15) if barras else None},
    }

    # 8. Agua transportada: pesaje húmedo − seco (viajes con descuento de humedad).
    con_seco = d[d["toneladas_secas"].notna()]
    filas = []
    for (cod, cen), g in con_seco.groupby(["material_codigo", "centro"]):
        humedas, secas = float(g["toneladas"].sum()), float(g["toneladas_secas"].sum())
        filas.append({"nombre": etiqueta(g), "material": cod, "centro": cen, "viajes": len(g), "humedas": humedas,
                      "secas": secas, "agua": humedas - secas, "pct": (humedas - secas) / humedas * 100 if humedas else 0.0})
    filas.sort(key=lambda f: -f["agua"])
    humedas, secas = float(con_seco["toneladas"].sum()), float(con_seco["toneladas_secas"].sum())
    barras = [{"nombre": f["nombre"], "sub": centro(f["centro"]).title(), "valor": f["pct"], "texto": f"{f['pct']:z,.1f}%",
               "clase": "neutra",
               "detalle": f"{f['nombre']} ({centro(f['centro'])}) · {mes_txt}\n{f['agua']:z,.0f} T de agua en "
                          f"{f['humedas']:z,.0f} T húmedas ({f['viajes']:,} viajes)"} for f in filas[:_PANEL_FILAS]]
    ctx["agua"] = {
        "filas": filas, "humedas": humedas, "secas": secas, "agua": humedas - secas,
        "pct": (humedas - secas) / humedas * 100 if humedas else None, "viajes": len(con_seco),
        "cobertura": len(con_seco) / n * 100,
        "grafico": {"titulo": "Agua transportada por material", "tipo": "escala",
                    "g": charts.barras_escala(barras, 0, max([b["valor"] for b in barras] + [1]) * 1.2) if barras else None},
    }
    return ctx


def _paneles_transporte(panel, vacio, tr):
    """Gráficos de la pestaña Transporte en Indicadores (Logística)."""
    titulos = ("Tiempo en planta", "Carga por viaje", "Transportistas", "Llegadas por hora", "Flota", "Agua transportada")
    if not tr or tr.get("vacio"):
        for t in titulos:
            vacio("transporte", t)
        return
    mes_txt, r = tr["mes_texto"], tr["resumen"]
    ti = tr["tiempo"]
    if ti["mediana"] is None:
        vacio("transporte", titulos[0], mensaje="Ningún viaje con los dos pesajes registrados.")
    else:
        tono, estado = {"bien": ("ok", "Hasta 1 h"), "regular": ("aviso", "De 1 a 2 h"),
                        "mal": ("alerta", "Más de 2 h")}[_clase_tiempo(ti["mediana"])]
        nota = "Mediana entre el pesaje lleno y el vacío de cada camión (ZMM056)."
        if ti["sin_medir"]:
            nota += " Sin medir (se pesan casi siempre una sola vez, con tara registrada): " + ", ".join(ti["sin_medir"]) + "."
        panel("transporte", titulos[0], _minutos_texto(ti["mediana"]),
              f"{mes_txt} · {ti['medidos']:,} viajes medidos · el 10% más lento, más de {_minutos_texto(ti['p90'])}",
              "escala", ti["grafico"]["g"], tono, estado,
              leyenda=[("bien", "Hasta 1 h"), ("regular", "1 a 2 h"), ("mal", "Más de 2 h")], nota=nota)
    ca = tr["carga"]
    panel("transporte", titulos[1], f"{r['t_viaje']:z,.1f} T", f"{mes_txt} · {r['viajes']:,} viajes · "
          f"{ca['bajo_30']:z,.0f}% con menos de 30 T", "pilas", ca["grafico"]["g"], leyenda=ca["leyenda"],
          nota="Pesaje neto húmedo de cada viaje (ZMM056). Cifra: toneladas por viaje del mes.")
    tp = tr["transportistas"]
    panel("transporte", titulos[2], f"{r['transportistas']}", f"{mes_txt} · los 3 principales llevan el {tp['top3']:z,.0f}% de las T",
          "escala", tp["grafico"]["g"], nota="Participación de cada transportista en las toneladas recibidas (ZMM056).")
    ll = tr["llegadas"]
    panel("transporte", titulos[3], ll["hora_pico"], f"{mes_txt} · hora con más llegadas ({ll['pct_pico']:z,.0f}% de los camiones) · "
          f"{ll['pct_noche']:z,.0f}% llega entre las 20:00 y las 06:00", "columnas", ll["grafico"]["g"],
          nota="Camiones por hora del pesaje lleno (llegada a la báscula), todo el mes.")
    fl = tr["flota"]
    panel("transporte", titulos[4], f"{fl['placas']:,} camiones", f"{mes_txt} · {fl['viajes_placa_dia']:z,.1f} viajes por camión al día · "
          f"los 10 con más viajes hacen el {fl['top10']:z,.0f}%", "escala", fl["grafico"]["g"],
          nota="Placas distintas que entregaron en el mes; barras: viajes por día trabajado de los camiones con más viajes.")
    ag = tr["agua"]
    if ag["pct"] is None:
        vacio("transporte", titulos[5], mensaje="Ningún viaje del mes trae peso seco.")
    else:
        panel("transporte", titulos[5], f"{ag['pct']:z,.1f}%", f"{mes_txt} · {ag['agua']:z,.0f} T de agua en {ag['humedas']:z,.0f} T húmedas",
              "escala", ag["grafico"]["g"],
              nota=f"Pesaje húmedo − seco de los viajes con descuento de humedad ({ag['cobertura']:z,.0f}% de los viajes). "
                   "El flete se paga por el peso húmedo.")
