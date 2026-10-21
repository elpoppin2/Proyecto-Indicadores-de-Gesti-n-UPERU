"""Detalle fila por fila de cada sección (ver gestion/desglose.py): qué filas de origen muestra cada una."""

import datetime

import pandas as pd
from dashboard import queries as q
from django.contrib.auth.decorators import login_required
from django.http import Http404

from .. import desglose, ejecutivo
from ..templatetags.dashboard_extras import corte as _texto_corte
from .categoria_pestanas import _mes_sop_categoria, _mes_variacion, _periodo_cuentas, _seleccion_precios, _tramo_plazo
from .comun import _categorias_disponibles, _mes_corto, _por_familia_centro, _por_str, _rango_en_anio, _segmentadores_compras, _segmentadores_inventario, _slug, _texto_rango
from .plan_precios import _nota_base


# --- Detalle fila por fila ---------------------------------------------------
# Filas de origen de cada sección de las pestañas de una categoría (ver
# gestion/desglose.py, que define columnas, filtros por clic y resumen). Cada
# constructor usa los MISMOS segmentadores y la misma consulta que la sección,
# para que la suma de las filas cuadre con el indicador. Devuelve (filas,
# info): info lleva el período en texto, chips de período y un aviso.

def _sin_filas(clave, aviso):
    columnas = [c["clave"] for c in desglose.DESGLOSES[clave]["columnas"]]
    return pd.DataFrame(columns=columnas), {"aviso": aviso}


def _numericas(df, columnas):
    for columna in columnas:
        df[columna] = pd.to_numeric(df[columna], errors="coerce")
    return df


def _fechas(df, columnas):
    for columna in columnas:
        df[columna] = pd.to_datetime(df[columna], errors="coerce").dt.date
    return df


def _periodo_compras_desglose(request, cuentas=False):
    """Período de un detalle de compras: el de los segmentadores (Compras o
    Cuentas por pagar) o el de un clic en la sección: un mes (f_mes, columna
    de Compras por mes) o el mismo rango en otro año (f_anio, fila de Compras
    por año o tarjeta de PMP por año). None si no hay compras."""
    anios = q.anios_compras_disponibles()
    if not anios:
        return None
    if cuentas:
        anio, rango = _periodo_cuentas(request, anios)
        texto = _texto_rango(rango, anio)
    else:
        sc = _segmentadores_compras(request)
        rango, texto = sc["rango"], sc["texto"]
    inicio, fin, chips = rango["fecha_ini"], rango["fecha_fin"], []
    mes = desglose.leer_mes(request.GET.get("f_mes")) if request.GET.get("f_mes") else None
    anio_clic = _por_str(request.GET.get("f_anio"), [str(a) for a in anios])
    if mes:
        inicio, fin = mes
        texto = q.formato_mes_anio(inicio)
        chips.append({"parametro": "f_mes", "texto": f"Mes: {texto}"})
    elif anio_clic:
        inicio, fin = _rango_en_anio(rango, int(anio_clic))
        texto = _texto_rango(rango, int(anio_clic))
        chips.append({"parametro": "f_anio", "texto": f"Año: {texto}"})
    return {"inicio": inicio, "fin": fin, "texto": texto, "chips": chips, "rango": rango}


def _motivo_contrato(pedido, contrato, bajo):
    """Por qué una línea cuenta como bajo contrato (FILTRO_BAJO_CONTRATO_LC)."""
    if not bajo:
        return "No"
    if contrato:
        return f"Sí · contrato {contrato}"
    numero = int(pedido) if str(pedido).isdigit() else 0
    if 4700000001 <= numero <= 4799999999:
        return "Sí · pedido 47"
    if 4000000001 <= numero <= 4099999999:
        return "Sí · pedido 40"
    return "Sí"


def _desglose_compras(request, categoria, cuentas=False):
    p = _periodo_compras_desglose(request, cuentas)
    if p is None:
        return _sin_filas("cuentas" if cuentas else "compras", "Todavía no hay compras cargadas.")
    df = q.lineas_compra_detalle(p["inicio"], p["fin"], categoria)
    df["bajo_contrato"] = df["bajo_contrato"].astype(bool)
    df["contrato_si"] = df["bajo_contrato"].map({True: "Sí", False: "No"})
    df["contrato_texto"] = [_motivo_contrato(pe, co, b) for pe, co, b in zip(df["pedido"], df["contrato"], df["bajo_contrato"])]
    df["dias"] = df["condicion_pago"].map(q.dias_ponderados_condicion_pago).astype(float)
    df["tramo"] = df["dias"].map(_tramo_plazo)
    df["peso"] = df["dias"] * df["valor"]
    return df, {"periodo": p["texto"], "chips": p["chips"]}


def _desglose_cuentas(request, categoria):
    return _desglose_compras(request, categoria, cuentas=True)


def _desglose_fillrate(request, categoria):
    p = _periodo_compras_desglose(request)
    if p is None:
        return _sin_filas("fillrate", "Todavía no hay compras cargadas.")
    df = q.lineas_compra_detalle(p["inicio"], p["fin"], categoria)
    df = df[df["clase_documento"].isin(["ZNAC", "ZIMP"]) & (df["cantidad_pedido"] > 0)].copy()
    df["pct_entregado"] = df["cantidad_entrada"] / df["cantidad_pedido"] * 100
    # Mismo criterio que queries._fill_rate (tolerancia 5%).
    df["completa"] = (df["cantidad_entrada"] >= df["cantidad_pedido"] * 0.95).map({True: "Completa", False: "Incompleta"})
    return df, {"periodo": p["texto"], "chips": p["chips"]}


def _desglose_leadtime(request, categoria):
    p = _periodo_compras_desglose(request)
    if p is None:
        return _sin_filas("leadtime", "Todavía no hay compras cargadas.")
    df = q.lead_time_lineas_detalle(p["inicio"], p["fin"], categoria)
    df = _fechas(_numericas(df, ["valor"]), ["fecha_pedido", "fecha_entrega", "fecha_recepcion"])
    df["dias"] = [(r - f).days if pd.notna(r) and pd.notna(f) else None
                  for r, f in zip(df["fecha_recepcion"], df["fecha_pedido"])]
    df["dias"] = pd.to_numeric(df["dias"], errors="coerce")
    df["estado"] = ["Válida" if d >= 0 else "Excluida: entrada antes del pedido" for d in df["dias"]]
    return df, {"periodo": p["texto"], "chips": p["chips"]}


def _desglose_otif(request, categoria):
    supuestos = ejecutivo.cargar_metas()["supuestos"]
    if categoria not in supuestos["otif_categorias"]:
        return _sin_filas("otif", "El OTIF solo se mide en Repuestos y Suministros Industriales.")
    p = _periodo_compras_desglose(request)
    if p is None:
        return _sin_filas("otif", "Todavía no hay compras cargadas.")
    dias, tolerancia = supuestos["otif_dias_tolerancia"], supuestos["otif_cantidad_tolerancia_pct"] / 100
    df = q.otif_lineas(p["inicio"], p["fin"], (categoria,), dias)
    if df.empty:
        return _sin_filas("otif", "Sin líneas con fecha de entrega en el período.")
    hoy = datetime.date.today()
    cerradas = pd.Series([f + datetime.timedelta(days=dias) < hoy for f in df["fecha_entrega"]], index=df.index)
    partes = []
    if cerradas.any():
        evaluadas = ejecutivo._evaluar_otif(df[cerradas], dias, tolerancia)
        evaluadas["estado"] = evaluadas["otif"].map({True: "Cumple", False: "No cumple"})
        evaluadas["a_tiempo_texto"] = evaluadas["a_tiempo"].map({True: "Sí", False: "No"})
        evaluadas["completo_texto"] = evaluadas["completo"].map({True: "Sí", False: "No"})
        partes.append(evaluadas)
    if (~cerradas).any():
        pendientes = df[~cerradas].copy()
        pendientes["limite"] = [f + datetime.timedelta(days=dias) for f in pendientes["fecha_entrega"]]
        pendientes = pendientes.assign(a_tiempo=False, completo=False, otif=False, estado="En plazo",
                                       a_tiempo_texto="", completo_texto="", motivo="Todavía en plazo: no se evalúa")
        partes.append(pendientes)
    return pd.concat(partes, ignore_index=True), {"periodo": p["texto"], "chips": p["chips"]}


def _desglose_unico(request, categoria):
    p = _periodo_compras_desglose(request)
    if p is None:
        return _sin_filas("unico", "Todavía no hay compras cargadas.")
    rango = p["rango"]
    # Mismo umbral de recurrencia que _proveedor_unico_periodo.
    minimo = min(ejecutivo.cargar_metas()["supuestos"]["meses_compra_recurrente"],
                 rango["mes_hasta"] - rango["mes_desde"] + 1)
    df = q.compras_por_material_proveedor_rango(p["inicio"], p["fin"])
    df = df[df["categoria"] == categoria]
    if df.empty:
        return _sin_filas("unico", "Sin compras de materiales en el período.")
    df = df.assign(valor_total=df["valor_total"].astype(float))
    por_material = df.groupby("material_codigo").agg(
        texto_breve=("texto_breve", "first"), valor=("valor_total", "sum"), proveedores=("proveedor", "nunique"),
        nombres=("proveedor", lambda s: " · ".join(sorted(set(s)))), meses=("meses", "sum"), pedidos=("pedidos", "sum"),
    ).reset_index()
    por_material = por_material[por_material["meses"] >= minimo].copy()
    por_material["un_solo"] = por_material["proveedores"] == 1
    por_material["un_solo_texto"] = por_material["un_solo"].map({True: "Sí", False: "No"})
    texto = f"{p['texto']} · recurrentes: {minimo} mes{'es' if minimo != 1 else ''} o más con compra"
    return por_material, {"periodo": texto, "chips": p["chips"]}


def _texto_inventario(si):
    return _texto_corte(si["mes"]) + (f" · {si['texto_filtros']}" if si["texto_filtros"] else "")


def _desglose_inventario(request, categoria):
    si = _segmentadores_inventario(request, categoria)
    inv = si["inv"].sort_values("valor_total", ascending=False).copy()
    # ABC igual que queries.clasificacion_abc_por_categoria (solo valor > 0).
    positivos = inv["valor_total"] > 0
    total = float(inv.loc[positivos, "valor_total"].sum())
    inv["pct"] = (inv["valor_total"] / total * 100).where(positivos) if total else None
    inv["pct_acumulado"] = (inv["valor_total"].where(positivos, 0).cumsum() / total * 100).where(positivos) if total else None
    inv["clase"] = "—"
    if total:
        inv.loc[positivos, "clase"] = pd.cut(inv.loc[positivos, "pct_acumulado"], bins=[-0.01, 80, 95, 100.01],
                                             labels=["A", "B", "C"]).astype(str)
    return inv, {"periodo": _texto_inventario(si)}


def _desglose_sinmov(request, categoria):
    si = _segmentadores_inventario(request, categoria)
    inicio, fin = q._ventana_anios(si["mes"], q.MATERIALES_SIN_MOVIMIENTO_ANIOS)
    df = si["inv"].merge(q.consumo_periodo_por_material(inicio, fin), on="material_codigo", how="left")
    df["consumo_total"] = df["consumo_total"].fillna(0.0)
    df = df[(df["valor_total"] > 0) & (df["consumo_total"] <= 0)]
    ultimos = _fechas(q.ultimo_movimiento_por_material(df["material_codigo"].tolist()), ["ultimo_consumo", "ultima_entrada"])
    df = df.merge(ultimos, on="material_codigo", how="left")
    return df, {"periodo": _texto_inventario(si)}


def _desglose_consumo(request, categoria):
    si = _segmentadores_inventario(request, categoria)
    inicio, fin = q._ventana_6_meses(si["mes"])
    df = q.movimientos_consumo_detalle(inicio, fin, categoria)
    df = _fechas(_numericas(df, ["consumo", "importe"]), ["fecha"])
    if si["familia"] or si["centro"]:
        # Familia y Centro del maestro, como la Rotación (no el centro del movimiento).
        catalogo = q.catalogo_materiales()[["material_codigo", "familia", "centro"]]
        df = _por_familia_centro(df.merge(catalogo, on="material_codigo", how="left", suffixes=("", "_maestro")), si)
    texto = f"{q.formato_mes_anio_abrev(inicio)} a {q.formato_mes_anio_abrev(fin)}"
    return df, {"periodo": texto + (f" · {si['texto_filtros']}" if si["texto_filtros"] else "")}


def _desglose_criticos(request, categoria):
    si = _segmentadores_inventario(request, categoria)
    rot = q.rotacion_inventario_por_material(si["mes"])
    rot = _por_familia_centro(rot[rot["categoria"] == categoria], si)
    criticos = set(q._query("SELECT codigo AS material_codigo FROM gestion_material WHERE material_critico = 'X'")["material_codigo"])
    df = rot[rot["material_codigo"].isin(criticos)].copy()
    df["estado"] = ["Sin consumo" if pd.isna(c) else ("En riesgo" if c < 1 else "OK") for c in df["cobertura_meses"]]
    return df, {"periodo": _texto_inventario(si)}


def _desglose_variacion(request, categoria):
    si = _segmentadores_inventario(request, categoria)
    _, mes_inicial = _mes_variacion(request, si)
    if not mes_inicial:
        return _sin_filas("variacion", "No hay un corte anterior para comparar.")
    df = q.variacion_inventario_por_material(mes_inicial, si["mes"], categoria=categoria,
                                             familia=si["familia"], centro=si["centro"])
    texto = f"{_texto_corte(mes_inicial)} → {_texto_corte(si['mes'])}"
    return df, {"periodo": texto + (f" · {si['texto_filtros']}" if si["texto_filtros"] else "")}


def _texto_precios(s):
    meses = sorted(s["inflacion"]["mes"].unique())
    return f"{_mes_corto(meses[0])} a {_mes_corto(meses[-1])} · contra {s['evaluacion']['texto'].lower()}"


def _desglose_precios(request, categoria):
    s = _seleccion_precios(request, categoria)
    inflacion = s["inflacion"]
    if inflacion.empty:
        return _sin_filas("precios", "Sin compras con base de comparación para los filtros elegidos.")
    umb = dict(zip(s["base"]["material_codigo"], s["base"]["unidad_medida_base"]))
    df = inflacion.assign(
        umb=inflacion["material_codigo"].map(umb).fillna(""),
        mes_texto=[_mes_corto(m) for m in inflacion["mes"]],
        base_texto=[_mes_corto(r["mes_base"]) + (f" ({_nota_base(r)})" if _nota_base(r) else "")
                    for r in inflacion.to_dict("records")],
        variacion_pct=inflacion["variacion"] * 100,
        atipica_texto=inflacion["atipica"].map({True: "Sí", False: ""}),
    )
    return df, {"periodo": _texto_precios(s)}


def _desglose_entradas(request, categoria):
    s = _seleccion_precios(request, categoria)
    inflacion = s["inflacion"]
    if inflacion.empty:
        return _sin_filas("entradas", "Sin compras con base de comparación para los filtros elegidos.")
    meses = sorted(inflacion["mes"].unique())
    fin = q._sumar_meses(meses[-1], 1) - datetime.timedelta(days=1)
    df = q.entradas_compra_detalle(sorted(inflacion["material_codigo"].unique()), meses[0], fin)
    if df.empty:
        return _sin_filas("entradas", "Sin entradas de mercancía para estos materiales.")
    df = _fechas(_numericas(df, ["cantidad", "importe"]), ["fecha"])
    # Solo los material/centro/mes comparados (los mismos meses que suman al IPU).
    df["mes"] = [f.replace(day=1) for f in df["fecha"]]
    df = df.merge(inflacion[["material_codigo", "centro", "mes"]].drop_duplicates(), on=["material_codigo", "centro", "mes"])
    df["precio"] = (df["importe"] / df["cantidad"]).where(df["cantidad"] != 0)
    return df, {"periodo": _texto_precios(s)}


def _desglose_abastecimiento(request, categoria):
    meses, mes = _mes_sop_categoria(request)
    if not meses:
        return _sin_filas("abastecimiento", "Todavía no hay Plan de Abastecimiento S&OP cargado.")
    return q.plan_vs_real_abastecimiento_por_material(mes, categoria=categoria), {"periodo": q.formato_mes_anio(mes)}


def _desglose_ingresos(request, categoria):
    meses, mes = _mes_sop_categoria(request)
    if not meses:
        return _sin_filas("ingresos", "Todavía no hay Plan de Abastecimiento S&OP cargado.")
    materiales = set(q.plan_vs_real_abastecimiento_por_material(mes, categoria=categoria)["material_codigo"])
    df = q.tickets_ingreso_detalle(mes)
    df = _fechas(_numericas(df[df["material_codigo"].isin(materiales)].copy(), ["toneladas"]), ["fecha"])
    df["tipo"] = df["tipo_operacion"].map(lambda t: "Propio (cantera o traslado)" if t == "5" else "Terceros")
    df["proveedor"] = df["proveedor"].fillna("").str.strip().replace("", "(sin proveedor)")
    return df, {"periodo": q.formato_mes_anio(mes)}


_CONSTRUCTORES_DESGLOSE = {
    "compras": _desglose_compras, "cuentas": _desglose_cuentas, "fillrate": _desglose_fillrate,
    "otif": _desglose_otif, "leadtime": _desglose_leadtime, "unico": _desglose_unico,
    "inventario": _desglose_inventario, "sinmov": _desglose_sinmov, "consumo": _desglose_consumo,
    "criticos": _desglose_criticos, "variacion": _desglose_variacion, "precios": _desglose_precios,
    "entradas": _desglose_entradas, "abastecimiento": _desglose_abastecimiento, "ingresos": _desglose_ingresos,
}


@login_required
def desglose_filas(request, slug, clave):
    """Detalle fila por fila de una sección de la categoría `slug` (ver
    gestion/desglose.py): HTML para desglose.js o Excel (?formato=xlsx)."""
    categoria = {_slug(c): c for c in _categorias_disponibles()}.get(slug)
    constructor = _CONSTRUCTORES_DESGLOSE.get(clave)
    if categoria is None or constructor is None:
        raise Http404("Detalle no encontrado")
    # Las filas base dependen de los segmentadores de la página y de los
    # clics de período; los demás filtros, la búsqueda y el orden se
    # aplican sobre ellas sin repetir la consulta.
    base = tuple(sorted(
        (k, v) for k, v in request.GET.items()
        if k not in desglose.PARAMETROS_UI and (not k.startswith("f_") or k in desglose.FILTROS_DE_PERIODO)
    ))
    return desglose.responder(request, clave, lambda: constructor(request, categoria), (clave, categoria, base),
                              f"detalle_{clave}_{slug}")
