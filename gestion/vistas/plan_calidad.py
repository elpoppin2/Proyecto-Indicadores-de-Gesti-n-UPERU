"""Planificación > Calidad: premios y castigos por calidad, costo de energía de los carbones y calidad de los ingresos, con sus descargas a Excel."""

import datetime
import io

import pandas as pd
from dashboard import queries as q
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse

from .. import charts
from .comun import _centros_por_nombre, _fecha_corta, _mes_corto, _nombre_corto, _opciones_categoria_centro, _opciones_material, _por_materiales, _selector_material


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
        f["barra_pct"] = f"{abs(f[campo]) / mayor * 50:z.1f}"
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
    df = _centros_por_nombre(df)
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
            f["barra_pct"] = f"{abs(f['dif_pct']) / mayor * 50:z.1f}"
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
    df = _centros_por_nombre(df)
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
                "desviacion": fmt(resumen["desviacion"]) if decimales else f"{resumen['desviacion']:z,.1f}",
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
