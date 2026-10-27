"""Planificación > Cumplimiento: plan vs real del mes, cumplimiento mensual, detalle por material, cobertura y entregas por proveedor."""

import datetime
import re

from dashboard import queries as q

from .. import charts
from .comun import _RAZON_SOCIAL, _mes_corto, _nombre_corto, _opciones_categoria_centro, _opciones_material, _por_materiales, _por_str, _selector_categoria_centro, _selector_material


def _meses_plan(sop=None):
    """Meses con Plan de Abastecimiento y el mes por defecto. El Plan proyecta
    ~16 meses hacia adelante, así que el último mes de la lista casi siempre es
    futuro sin ingresos reales: por defecto va el mes actual si está en el
    rango, si no el último mes pasado con datos (el selector GET sigue
    permitiendo elegir un mes futuro a propósito). Con una emisión del S&OP
    elegida, solo los meses que ella planifica (desde su mes de emisión)."""
    meses = q.meses_planificacion_disponibles()
    if sop:
        meses = [m for m in meses if m >= sop]
    hoy_mes = datetime.date.today().replace(day=1)
    hasta_hoy = [m for m in meses if m <= hoy_mes]
    return meses, (hasta_hoy[-1] if hasta_hoy else (meses[0] if meses else None))


def _selector_sop(request, sufijo):
    """Segmentador S&OP (pedido del usuario, 2026-10-23: cada S&OP mensual se
    carga aparte y se comparan): "" = el más reciente que trae cada mes, o
    una emisión. Devuelve (opciones, valor elegido, fecha o None)."""
    sops = q.sops_abastecimiento_disponibles()
    opciones = [{"valor": "", "texto": "El más reciente de cada mes"}] + [
        {"valor": s.isoformat(), "texto": f"S&OP {q.formato_mes_anio(s)}"} for s in sops]
    valor = request.GET.get(f"sop_{sufijo}", "")
    sop = next((s for s in sops if s.isoformat() == valor), None)
    return opciones, (sop.isoformat() if sop else ""), sop


def _opciones_plan(request):
    """Mes y S&OP de la Evaluación mensual y sus opciones de Categoría/Centro
    (las mismas que usa Cumplimiento mensual)."""
    sops, sop_sel, sop = _selector_sop(request, "plan")
    meses, mes_defecto = _meses_plan(sop)
    mes = _por_str(request.GET.get("mes_plan"), meses) or mes_defecto
    base = q.plan_vs_real_abastecimiento_por_material(mes, sop=sop)
    categorias, centros = _opciones_categoria_centro(base)
    return meses, mes, base, categorias, centros, {"sops": sops, "sop_sel": sop_sel, "sop": sop}


def _contexto_plan_mensual(request):
    """Evaluación mensual: Mes/Categoría/Centro propios."""
    meses, mes_plan_sel, base_plan, categorias_plan_op, centros_plan_op, s = _opciones_plan(request)
    categoria_plan_sel, centro_plan_sel = _selector_categoria_centro(
        request, "plan", categorias_plan_op, centros_plan_op
    )
    resumen = q.resumen_abastecimiento_por_categoria(
        mes_plan_sel,
        categoria=None if categoria_plan_sel == "Todas" else categoria_plan_sel,
        centro=None if centro_plan_sel == "Todos" else centro_plan_sel,
        sop=s["sop"],
    )
    totales_plan = base_plan
    if categoria_plan_sel != "Todas":
        totales_plan = totales_plan[totales_plan["categoria"] == categoria_plan_sel]
    if centro_plan_sel != "Todos":
        totales_plan = totales_plan[totales_plan["centro"] == centro_plan_sel]
    return {
        "mes": mes_plan_sel, "opciones_mes": meses, "sops": s["sops"], "sop_sel": s["sop_sel"],
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
    _, _, _, categorias_plan_op, centros_plan_op, _ = _opciones_plan(request)
    categoria_cm_sel, centro_cm_sel = _selector_categoria_centro(
        request, "cumplmes", categorias_plan_op, centros_plan_op
    )
    sops, sop_sel, sop = _selector_sop(request, "cumplmes")
    cumplimiento_mensual = q.cumplimiento_mensual_abastecimiento(
        categoria=None if categoria_cm_sel == "Todas" else categoria_cm_sel,
        centro=None if centro_cm_sel == "Todos" else centro_cm_sel,
        sop=sop,
    )
    # El gráfico llega hasta el mes en curso (los meses futuros no tienen real
    # y dejaban 14 columnas vacías) y su ancho sigue a la cantidad de meses:
    # dos meses no ocupan todo el ancho de la página (pulido, 2026-10-23). La
    # tabla de abajo sigue con todos los meses del plan.
    hoy_mes = datetime.date.today().replace(day=1)
    puntos_cumplimiento = [
        (q.formato_mes_anio_abrev(f["mes"]), f["cumplimiento_pct"]) for f in cumplimiento_mensual if f["mes"] <= hoy_mes
    ]
    return {
        "categorias": categorias_plan_op, "categoria_sel": categoria_cm_sel,
        "centros": centros_plan_op, "centro_sel": centro_cm_sel, "sops": sops, "sop_sel": sop_sel,
        "filas": cumplimiento_mensual,
        "grafico": charts.barras_mensuales(puntos_cumplimiento, ancho=max(320, 72 * len(puntos_cumplimiento) + 40),
                                           alto=240, meta=100) if puntos_cumplimiento else None,
    }


def _contexto_plan_detalle(request):
    """Detalle por material + Evaluación diaria: Mes/Centro/Material propios
    (todos en lista desplegable, sin buscador de texto libre). Sin filtro de
    Categoría aparte: Material ya alcanza para eso, y así el filtro relevante
    es Centro -> Material. El Material se acota al Centro elegido (no se
    ofrecen combinaciones sin datos, que antes mostraban un gráfico vacío).
    Definido por el usuario."""
    sops, sop_sel, sop = _selector_sop(request, "detalle")
    meses, mes_defecto = _meses_plan(sop)
    mes_detalle_sel = _por_str(request.GET.get("mes_detalle"), meses) or mes_defecto
    base_detalle = q.plan_vs_real_abastecimiento_por_material(mes_detalle_sel, sop=sop)
    # Sin plan ni ingresos en el mes no hay nada que evaluar (p.ej. la Caliza
    # Terceros HCIM en set-26): no se muestra ni se ofrece en el filtro
    # (pedido del usuario, 2026-10-23).
    base_detalle = base_detalle[(base_detalle["cantidad_plan"] != 0) | (base_detalle["cantidad_real"] != 0)]
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
        mes_detalle_sel, centro=filtro_centro_det, material=filtro_material_det, sop=sop
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
        curva_s = charts.curva_doble(puntos_plan, puntos_real, alto=380, formato_valor=lambda v: f"{v:z,.0f} T")

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
            "tipo": {"5": "Propio", "MB51": "Importado"}.get(str(r.tipo_operacion), "Terceros"),
            "real": float(r.cantidad_real), "carros": int(r.carros),
            "participacion": r.cantidad_real / f["cantidad_real"] * 100 if f["cantidad_real"] else None,
        } for r in g.itertuples()]
    # Total del detalle (pedido del usuario, 2026-10-23): el cumplimiento es
    # el real ÷ el plan de todo lo listado, como la Evaluación mensual.
    total_plan = sum(f["cantidad_plan"] for f in filas)
    total_real = sum(f["cantidad_real"] for f in filas)
    total = {"materiales": len(filas), "plan": total_plan, "real": total_real,
             "carros": sum(f["carros"] for f in filas), "diferencia": total_real - total_plan,
             "cumplimiento_pct": total_real / total_plan * 100 if total_plan else None} if filas else None

    return {
        "mes": mes_detalle_sel, "opciones_mes": meses, "sops": sops, "sop_sel": sop_sel,
        "centros": centros_det_op, "centro_sel": centro_det_sel,
        "materiales": materiales_det_op, "material_sel": material_det_sel,
        "titulo_curva": f"{material_titulo} - {centro_titulo}",
        "filas": filas, "total": total, "expandir": len(filas) == 1,
        "diario": diario,
        "curva_s": curva_s,
    }


def _contexto_cobertura(request):
    """Cobertura de stock: Mes/Categoría/Centro/Material propios. El Mes elige
    QUÉ foto de inventario (y su ventana de consumo de 12 meses hacia atrás) se
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
        lineas = [f"{_mes_corto(m)} · {tm:z,.0f} T · {cm:,} carros"]
        for k in sorted(de_mes, key=lambda k: -de_mes[k][0])[:6]:
            t, c = de_mes[k]
            lineas.append(f"{etiqueta(k)}: {t:z,.0f} T ({t / tm * 100 if tm else 0:z,.1f}%) · {c:,} carros")
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
    formato = (charts._toneladas_corto if ver == "toneladas" else (lambda v: f"{v:z,.0f}") if ver == "carros"
               else (lambda v: f"{v:z,.0f}%"))
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
        return f"{toneladas:z,.0f}" if toneladas else "—"
    if ver == "carros":
        return f"{carros:,}" if carros else "—"
    return f"{toneladas / total_toneladas * 100:z,.1f}%" if toneladas and total_toneladas else "—"
