"""Pestaña Simulación de Planificación (pedido del usuario, 2026-11-03): inventario valorizado proyectado del S&OP con su versión de precios (a precio promedio móvil), cambio contra el S&OP anterior, la versión de precios con sus cambios y la precisión del pronóstico; descargas de la versión y de la plantilla del mes siguiente."""

import io

from dashboard import queries as q
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse

from .. import charts
from ..precios_sop import CARPETA, armar_version, escribir_plantilla, nombre_archivo, proyectados_de_version
from .comun import _mes_corto, _opciones_categoria_centro, _opciones_material, _por_materiales, _por_str, _selector_material

# Colores de las columnas apiladas por categoría (dashboard.css).
_CLASE_CATEGORIA = {"MATERIAS PRIMAS": "col-sim-mp", "COMBUSTIBLES": "col-sim-comb"}
# Cambio de precio contra la versión anterior que se marca en la matriz.
_UMBRAL_CAMBIO_PRECIO = 0.005
# Meses reales que muestra la matriz de precios antes de la emisión.
_MESES_REALES_MATRIZ = 3


def _millones(valor):
    return f"{valor / 1e6:z,.1f}"


def _filtrar(request, base, sufijo):
    """Categoría, Centro y Material (en cascada, como Evolución de inventario)."""
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
    filtros = {"categorias": categorias_op, "categoria_sel": categoria_sel, "centros": centros_op,
               "centro_sel": centro_sel, "materiales": materiales_op, "material_sel": material_sel}
    return _por_materiales(por_centro, material_sel), filtros


def _contexto_valorizado(request):
    """Inventario valorizado proyectado (S&OP): stock del S&OP (abastecimiento −
    consumo − traslados) valorizado con la versión de precios del MISMO S&OP a
    precio promedio móvil (ver queries.inventario_valorizado_sop), por mes,
    categoría y material, y el cambio contra el S&OP anterior separado en
    efecto cantidad y efecto precio. Pedido por el usuario."""
    opciones = q.sop_valorizables()
    if not opciones:
        return None
    con_precios = [s for s, c in opciones if c]
    snapshot = _por_str(request.GET.get("sop_simulacion"), [s for s, _ in opciones]) or (
        con_precios[-1] if con_precios else opciones[-1][0])
    contexto = {
        "snapshot": snapshot, "opciones_snapshot": [{"fecha": s, "con_precios": c} for s, c in opciones],
        "fecha_inicial": q.fecha_inventario_inicial_sop(snapshot), "tiene_precios": snapshot in con_precios,
        "archivo_precios": nombre_archivo(snapshot), "carpeta_precios": CARPETA,
        "categorias": ["Todas"], "categoria_sel": "Todas", "centros": ["Todos"], "centro_sel": "Todos",
        "materiales": [{"codigo": "Todos", "texto": "Todos"}], "material_sel": [], "resumen": [],
    }
    if not contexto["tiene_precios"]:
        return contexto
    base = q.inventario_valorizado_sop(snapshot)
    df, filtros = _filtrar(request, base, "simulacion")
    contexto.update(filtros)
    if df.empty:
        return contexto

    meses = sorted(df["mes"].unique())
    sumas = df.groupby("mes")[["valor_inicial", "compras", "valor_compras", "recibido", "valor_recibido",
                               "salidas", "valor_salidas", "stock_final", "valor_final"]].sum()
    un_material = df.groupby(["material_codigo", "centro"]).ngroups == 1
    resumen = []
    for mes in meses:
        s = sumas.loc[mes]
        fila = {"mes": mes, "corto": _mes_corto(mes), "valor_inicial": s.valor_inicial, "compras": s.compras,
                "valor_compras": s.valor_compras, "valor_recibido": s.valor_recibido, "valor_salidas": s.valor_salidas,
                "valor_final": s.valor_final, "variacion": s.valor_final - s.valor_inicial,
                "precio_compra": s.valor_compras / s.compras if s.compras > 0 else None}
        if un_material:
            unico = df[df["mes"] == mes].iloc[0]
            fila.update({"stock_final": unico.stock_final, "pmp": unico.pmp, "precio_sop": unico.precio_sop})
        resumen.append(fila)
    partida = resumen[0]["valor_inicial"]
    cierre = resumen[-1]
    mayor = max(resumen, key=lambda r: r["valor_final"])

    # Gráfico: valor al cierre de cada mes apilado por categoría, con la partida (MB5B) primero.
    categorias = [c for c in _CLASE_CATEGORIA if c in set(df["categoria"])] + sorted(set(df["categoria"]) - set(_CLASE_CATEGORIA))
    por_categoria = df.groupby(["mes", "categoria"])["valor_final"].sum()
    inicial_categoria = df[df["mes"] == meses[0]].groupby("categoria")["valor_inicial"].sum()
    etiqueta_partida = _mes_corto(contexto["fecha_inicial"]) if contexto["fecha_inicial"] else "Inicio"
    etiquetas, pilas, textos = [etiqueta_partida], [], []

    def pila(valores, titulo, proyeccion=True):
        # Notación IBCS: la partida es real (sólida) y los meses del S&OP son proyección (rayados).
        total = sum(valores.values())
        detalle = "\n".join([f"{titulo}: S/ {_millones(total)} MM"] +
                            [f"{c.capitalize()}: S/ {_millones(valores.get(c, 0.0))} MM" for c in categorias])
        segmentos = [{"valor": valores.get(c, 0.0),
                      "clase": _CLASE_CATEGORIA.get(c, "col-prov-otros") + (" col-fc" if proyeccion else "")}
                     for c in categorias]
        if segmentos:
            segmentos[0]["detalle"] = detalle
        return segmentos, _millones(total)

    segmentos, texto = pila({c: float(inicial_categoria.get(c, 0.0)) for c in categorias},
                            f"Partida{' (MB5B ' + contexto['fecha_inicial'].strftime('%d/%m/%Y') + ')' if contexto['fecha_inicial'] else ''}",
                            proyeccion=False)
    pilas.append(segmentos)
    textos.append(texto)
    for r in resumen:
        segmentos, texto = pila({c: float(por_categoria.get((r["mes"], c), 0.0)) for c in categorias},
                                f"{r['corto']} · valor al cierre")
        segmentos[0]["detalle"] += (f"\nCompras: S/ {_millones(r['valor_compras'])} MM"
                                    f"\nSalidas: S/ {_millones(r['valor_salidas'])} MM")
        etiquetas.append(r["corto"])
        pilas.append(segmentos)
        textos.append(texto)
    grafico = charts.columnas(etiquetas, pilas, lambda v: _millones(v), textos=textos, alto=270)

    # Matriz: valor al cierre de cada mes por material, partida primero y variación al final.
    matriz = []
    for (material, centro), g in df.groupby(["material_codigo", "centro"]):
        g = g.sort_values("mes")
        matriz.append({
            "material_codigo": material, "texto_breve": g["texto_breve"].iloc[0], "centro": centro,
            "partida": g["valor_inicial"].iloc[0], "celdas": list(g["valor_final"]),
            "variacion": g["valor_final"].iloc[-1] - g["valor_inicial"].iloc[0],
            "sin_precio": bool(g["sin_precio"].any()),
        })
    matriz.sort(key=lambda f: -max(f["celdas"] + [f["partida"]]))
    sin_precio = sorted({f"{f['texto_breve']} ({q.nombre_centro(f['centro'])})" for f in matriz if f["sin_precio"]})

    contexto.update({
        "resumen": resumen, "meses": [{"mes": m, "corto": _mes_corto(m)} for m in meses], "un_material": un_material,
        "hay_traslados": any(abs(r["valor_recibido"]) > 0.5 for r in resumen),
        "partida": partida, "cierre": cierre, "mayor": mayor, "variacion_total": cierre["valor_final"] - partida,
        "variacion_pct": f"{cierre['valor_final'] / partida - 1:+.1%}" if partida else "",
        "grafico": grafico, "leyenda": [{"texto": c.capitalize(), "clase": _CLASE_CATEGORIA.get(c, "col-prov-otros")} for c in categorias],
        "matriz": matriz, "total_partida": sum(f["partida"] for f in matriz),
        "total_celdas": [r["valor_final"] for r in resumen], "sin_precio": sin_precio,
        "comparativo": _comparativo(request, snapshot, filtros),
    })
    return contexto


def _comparativo(request, snapshot, filtros):
    """Cambio contra el S&OP anterior con versión de precios, con los mismos
    filtros: por mes (total) y por material en el último mes que tienen los dos."""
    anterior = q.sop_anterior_con_precios(snapshot)
    if anterior is None:
        return {"anterior": None}
    df = q.comparativo_valorizado_sop(snapshot, anterior)
    if df.empty:
        return {"anterior": anterior, "meses": []}
    if filtros["categoria_sel"] != "Todas":
        df = df[df["categoria"] == filtros["categoria_sel"]]
    if filtros["centro_sel"] != "Todos":
        df = df[df["centro"] == filtros["centro_sel"]]
    df = _por_materiales(df, filtros["material_sel"])
    if df.empty:
        return {"anterior": anterior, "meses": []}
    columnas = ["valor_anterior", "efecto_cantidad", "efecto_precio", "valor_final"]
    por_mes = df.groupby("mes")[columnas].sum()
    meses = [{"mes": m, "corto": _mes_corto(m), **por_mes.loc[m].to_dict()} for m in sorted(por_mes.index)]
    ultimo = meses[-1]["mes"]
    materiales = df[df["mes"] == ultimo].copy()
    materiales["cambio"] = materiales["valor_final"] - materiales["valor_anterior"]
    materiales = materiales[materiales["cambio"].abs() > 0.5].sort_values("cambio", key=abs, ascending=False)
    return {"anterior": anterior, "meses": meses, "mes_materiales": ultimo,
            "materiales": materiales.to_dict("records")}


def _contexto_precios_version(request):
    """Versión de precios S&OP: precio proyectado por material y mes con los
    últimos meses reales de referencia, su supuesto y lo que cambió contra la
    versión anterior. Pedido por el usuario."""
    versiones = q.versiones_precios_sop()
    if not versiones:
        return {"versiones": [], "carpeta_precios": CARPETA, "mes_siguiente": q.mes_siguiente_sop(),
                "archivo_siguiente": nombre_archivo(q.mes_siguiente_sop())}
    version = _por_str(request.GET.get("sop_simprecios"), versiones) or versiones[-1]
    anteriores = [v for v in versiones if v < version]
    anterior = anteriores[-1] if anteriores else None
    precios = q.precios_sop(version)
    catalogo = q.catalogo_materiales().drop_duplicates("material_codigo").set_index("material_codigo")
    precios = precios.assign(
        texto_breve=[catalogo["texto_breve"].get(m) if m in catalogo.index else d for m, d in zip(precios["material_codigo"], precios["descripcion"])],
        categoria=[(catalogo["categoria"].get(m) if m in catalogo.index else None) or "MATERIAS PRIMAS" for m in precios["material_codigo"]],
    )
    df, filtros = _filtrar(request, precios, "simprecios")
    meses_proy = sorted(df["mes"].unique())
    meses_reales = [q._sumar_meses(version, -i) for i in range(_MESES_REALES_MATRIZ, 0, -1)]
    real = q.precios_entrada_sop_real()
    real = real[real["mes"].isin(meses_reales) & (real["precio"] > 0)]
    precio_real = {(m, c, mes): float(p) for m, c, mes, p in zip(real["material_codigo"], real["centro"], real["mes"], real["precio"])}
    previo = {}
    if anterior:
        p_ant = q.precios_sop(anterior)
        previo = {(m, c, mes): float(p) for m, c, mes, p in zip(p_ant["material_codigo"], p_ant["centro"], p_ant["mes"], p_ant["precio"]) if p == p}

    filas, cambios = [], []
    for (material, centro), g in df.groupby(["material_codigo", "centro"]):
        g = g.set_index("mes")
        celdas, variaciones = [], []
        for mes in meses_proy:
            precio = g["precio"].get(mes)
            precio = None if precio is None or precio != precio else float(precio)
            antes = previo.get((material, centro, mes))
            cambio = (precio / antes - 1) if precio and antes else None
            marca = ""
            if cambio is not None and abs(cambio) >= _UMBRAL_CAMBIO_PRECIO:
                marca = "precio-sube" if cambio > 0 else "precio-baja"
                variaciones.append((mes, cambio))
            titulo = (f"Versión {q.formato_mes_anio(anterior)}: {antes:z,.2f} ({cambio:+.1%})" if cambio is not None
                      else (f"Versión {q.formato_mes_anio(anterior)}: sin precio" if anterior and precio else ""))
            celdas.append({"valor": precio, "marca": marca, "titulo": titulo})
        fila = {"material_codigo": material, "texto_breve": g["texto_breve"].iloc[0], "centro": centro,
                "unidad": g["unidad"].iloc[0], "supuesto": next((s for s in g["supuesto"] if s), ""),
                "reales": [precio_real.get((material, centro, m)) for m in meses_reales], "celdas": celdas}
        filas.append(fila)
        if variaciones:
            mayor = max(variaciones, key=lambda x: abs(x[1]))
            promedio = sum(v for _, v in variaciones) / len(variaciones)
            cambios.append({"material_codigo": material, "texto_breve": fila["texto_breve"], "centro": centro,
                            "meses": len(variaciones), "promedio": promedio, "promedio_texto": f"{promedio:+.1%}",
                            "mayor": mayor[1], "mayor_texto": f"{mayor[1]:+.1%}", "mes_mayor": _mes_corto(mayor[0])})
    cambios.sort(key=lambda c: -abs(c["mayor"]))
    mes_siguiente = q.mes_siguiente_sop()
    return {
        "versiones": sorted(versiones, reverse=True), "version": version, "anterior": anterior, **filtros,
        "meses_reales": [{"mes": m, "corto": _mes_corto(m)} for m in meses_reales],
        "meses": [{"mes": m, "corto": _mes_corto(m)} for m in meses_proy], "filas": filas, "cambios": cambios,
        "rango": f"{_mes_corto(meses_proy[0])} a {_mes_corto(meses_proy[-1])}" if meses_proy else "",
        "sin_precio": sum(not any(c["valor"] for c in f["celdas"]) for f in filas),
        "carpeta_precios": CARPETA, "mes_siguiente": mes_siguiente, "archivo_siguiente": nombre_archivo(mes_siguiente),
    }


def _contexto_precision(request):
    """Precisión del pronóstico de precios: lo que proyectó cada versión
    contra el precio real de compra de los meses cerrados, por horizonte y
    por material (error ponderado por el importe real). Pedido por el usuario."""
    df = q.precision_precios_sop()
    contexto = {"versiones": q.versiones_precios_sop(), "horizontes": [], "materiales": [], "centros": ["Todos"],
                "centro_sel": "Todos"}
    if df.empty:
        return contexto
    centros = ["Todos"] + sorted(set(df["centro"]))
    centro_sel = request.GET.get("centro_simprecision", "Todos")
    if centro_sel not in centros:
        centro_sel = "Todos"
    if centro_sel != "Todos":
        df = df[df["centro"] == centro_sel]
    df = df.assign(abs_imp=df["error"].abs() * df["importe"], err_imp=df["error"] * df["importe"])

    def resumen(g):
        importe = g["importe"].sum()
        error = g["abs_imp"].sum() / importe if importe else None
        sesgo = g["err_imp"].sum() / importe if importe else None
        return {"n": len(g), "error": error, "sesgo": sesgo, "importe": importe,
                "error_texto": f"{error:.1%}" if error is not None else "—",
                "sesgo_texto": f"{sesgo:+.1%}" if sesgo is not None else "—"}

    horizontes = [{"horizonte": h, **resumen(g)} for h, g in df.groupby("horizonte")]
    materiales = [{"material_codigo": m, "texto_breve": g["texto_breve"].iloc[0], "centro": c, **resumen(g)}
                  for (m, c), g in df.groupby(["material_codigo", "centro"])]
    materiales.sort(key=lambda x: -x["importe"])
    versiones = [{"version": v, **resumen(g)} for v, g in df.groupby("version")]
    n_versiones = df["version"].nunique()
    contexto.update({"horizontes": horizontes, "materiales": materiales, "por_version": versiones,
                     "centros": centros, "centro_sel": centro_sel, "total": resumen(df),
                     "versiones_texto": f"{n_versiones} versión" if n_versiones == 1 else f"{n_versiones} versiones"})
    return contexto


def _respuesta_excel(version):
    buffer = io.BytesIO()
    escribir_plantilla(buffer, version)
    respuesta = HttpResponse(buffer.getvalue(),
                             content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    respuesta["Content-Disposition"] = f'attachment; filename="{nombre_archivo(version["mes"])}"'
    return respuesta


@login_required
def descargar_precios_sop(request):
    """La versión de precios cargada (?sop=2026-10-01) en el formato de la plantilla."""
    versiones = q.versiones_precios_sop()
    version = _por_str(request.GET.get("sop", ""), versiones) or (versiones[-1] if versiones else None)
    if version is None:
        return HttpResponse("Todavía no hay versiones de precios S&OP cargadas.", status=404)
    return _respuesta_excel(armar_version(version, proyectados_de_version(version, version),
                                          origen=f"la versión de precios de {q.formato_mes_anio(version)} cargada en el tablero"))


@login_required
def plantilla_precios_sop(request):
    """Plantilla del próximo S&OP: meses reales del tablero y los precios de la última versión para editar."""
    mes = q.mes_siguiente_sop()
    base = [v for v in q.versiones_precios_sop() if v < mes]
    if not base:
        return HttpResponse("Todavía no hay una versión de precios S&OP cargada para armar la plantilla.", status=404)
    return _respuesta_excel(armar_version(mes, proyectados_de_version(base[-1], mes),
                                          origen=f"la versión de precios de {q.formato_mes_anio(base[-1])}"))
