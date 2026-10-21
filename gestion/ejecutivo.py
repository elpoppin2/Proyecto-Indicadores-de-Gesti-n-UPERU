"""Portada ejecutiva, Cartera de oportunidades e Informe mensual de Supply
Chain. Pedido por el usuario (2026-10-04, "qué cambios harías para que el
gerente se sorprenda"): una portada de 60 segundos con los indicadores
clave, su meta, semáforo y tendencia, los 3 mensajes del mes y todo
expresado en soles; una cartera de oportunidades con su valor (caja que se
puede liberar y ahorro potencial); y el informe mensual en PDF con un clic.

Las metas y los supuestos están en gestion/metas.json (editable sin tocar
código); las que no confirmó la gerencia se muestran como "meta propuesta".
Todo se calcula para un mes (por defecto, el último mes cerrado): compras y
PMP de enero a ese mes, inventario al último corte de ese mes, riesgos y
oportunidades de los 12 meses que terminan en él."""

import base64
import calendar
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd
from django.urls import reverse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))
import queries as q  # noqa: E402

from . import charts  # noqa: E402

_RUTA_METAS = Path(__file__).with_name("metas.json")
_HUELLA = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:10]
_MESES = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
_DIAS_MES = 365 / 12
_PESO_ESTADO = {"alerta": 3, "aviso": 2, "ok": 1, "neutro": 0}


# --- Utilidades -------------------------------------------------------------

def cargar_metas():
    with open(_RUTA_METAS, encoding="utf-8") as f:
        return json.load(f)


def _fin_mes(mes):
    return datetime.date(mes.year, mes.month, calendar.monthrange(mes.year, mes.month)[1])


def _mes_corto(mes):
    return f"{_MESES[mes.month]}-{mes.year % 100:02d}"


def _tramo(inicio, fin):
    """'Ene–Sep 2026' (o 'Sep 2026' si es un solo mes)."""
    if inicio.month == fin.month:
        return f"{_MESES[fin.month]} {fin.year}"
    return f"{_MESES[inicio.month]}–{_MESES[fin.month]} {fin.year}"


def soles(valor, signo=False):
    """Soles abreviados para cifras ejecutivas: S/ 12.3 MM, S/ 450 K."""
    if valor is None or valor != valor:
        return "—"
    s = ("+" if valor > 0 else "−" if valor < 0 else "") if signo else ("−" if valor < 0 else "")
    v = abs(valor)
    if v >= 1e6:
        return f"{s}S/ {v / 1e6:z,.1f} MM"
    if v >= 1e3:
        return f"{s}S/ {v / 1e3:z,.0f} K"
    return f"{s}S/ {v:z,.0f}"


def _pct(valor, signo=False, decimales=1):
    if valor is None or valor != valor:
        return "—"
    return f"{valor:+.{decimales}f}%" if signo else f"{valor:.{decimales}f}%"


def _estado(valor, cfg):
    """Semáforo de un indicador contra su meta: ok (cumple), aviso (fuera,
    pero dentro de la tolerancia) o alerta."""
    if valor is None or cfg is None:
        return "neutro"
    meta, mayor = cfg["meta"], cfg["sentido"] == "mayor"
    if (valor >= meta) if mayor else (valor <= meta):
        return "ok"
    brecha = (meta - valor) if mayor else (valor - meta)
    return "aviso" if brecha <= cfg.get("tolerancia", 0) else "alerta"


_TEXTO_ESTADO = {"ok": "En meta", "aviso": "Cerca de la meta", "alerta": "Fuera de meta", "neutro": ""}


def _texto_meta(cfg, formato):
    if not cfg:
        return ""
    signo = "≥" if cfg["sentido"] == "mayor" else "≤"
    return f"Meta {signo} {formato(cfg['meta'])}" + ("" if cfg.get("confirmada") else " (propuesta)")


def _sparkline(serie):
    """serie: [(mes, valor)] -> minigráfico (o None con menos de 2 puntos)."""
    serie = [(m, float(v)) for m, v in serie if v is not None and v == v]
    return charts.minigrafico_precio(serie, ancho=160, alto=38) if len(serie) >= 2 else None


def _cortes_inventario():
    return sorted({(f.year, f.month): f for f in q.fechas_inventario_disponibles()}.values())


def meses_disponibles():
    """Meses que ofrece el selector de la portada: los últimos 24 hasta el
    mes en curso, el más reciente primero."""
    hoy = datetime.date.today().replace(day=1)
    return [q._sumar_meses(hoy, -i) for i in range(24)]


def mes_defecto():
    """El último mes cerrado: el mes en curso recién empieza."""
    return q._sumar_meses(datetime.date.today().replace(day=1), -1)


# --- Proveedor único --------------------------------------------------------

def proveedor_unico(inicio, fin, categoria=None, meses_minimos=3, excluir=("CAPEX",)):
    """Dependencia de proveedor único: de los materiales RECURRENTES (con
    compra en al menos `meses_minimos` meses del período), cuánto del valor
    se compra a un solo proveedor. Se mide sobre recurrentes porque un
    repuesto o un equipo de CAPEX comprado una vez es casi siempre de un
    solo fabricante (95% de los códigos), y ahí no hay alternativa que
    gestionar. `categoria`: una categoría o varias (tupla). Devuelve el % del
    valor, los totales, el detalle por categoría y los materiales de más valor."""
    df = q.compras_por_material_proveedor_rango(inicio, fin)
    if categoria:
        df = df[df["categoria"].isin((categoria,) if isinstance(categoria, str) else categoria)]
    df = df[~df["categoria"].isin(excluir)]
    vacio = {"pct": None, "valor_unico": 0.0, "valor_recurrente": 0.0, "materiales_unicos": 0,
             "materiales_recurrentes": 0, "por_categoria": [], "materiales": []}
    if df.empty:
        return vacio
    df = df.assign(valor_total=df["valor_total"].astype(float))
    por_material = df.groupby(["categoria", "material_codigo"]).agg(
        texto_breve=("texto_breve", "first"), valor=("valor_total", "sum"), proveedores=("proveedor", "nunique"),
        meses=("meses", "sum"), proveedor=("proveedor", "first"), pedidos=("pedidos", "sum"),
    ).reset_index()
    recurrentes = por_material[por_material["meses"] >= meses_minimos]
    if recurrentes.empty:
        return vacio
    unicos = recurrentes[recurrentes["proveedores"] == 1]
    por_categoria = []
    for cat, g in recurrentes.groupby("categoria"):
        u = g[g["proveedores"] == 1]
        por_categoria.append({
            "categoria": cat, "materiales": int(len(g)), "unicos": int(len(u)),
            "valor": float(g["valor"].sum()), "valor_unico": float(u["valor"].sum()),
            "pct": float(u["valor"].sum() / g["valor"].sum() * 100) if g["valor"].sum() else 0.0,
        })
    por_categoria.sort(key=lambda r: -r["valor_unico"])
    return {
        "pct": float(unicos["valor"].sum() / recurrentes["valor"].sum() * 100) if recurrentes["valor"].sum() else None,
        "valor_unico": float(unicos["valor"].sum()), "valor_recurrente": float(recurrentes["valor"].sum()),
        "materiales_unicos": int(len(unicos)), "materiales_recurrentes": int(len(recurrentes)),
        "por_categoria": por_categoria,
        "materiales": unicos.sort_values("valor", ascending=False).head(25).to_dict("records"),
    }


# --- OTIF -------------------------------------------------------------------

def _evaluar_otif(df, dias, tolerancia):
    """Marca cada línea: a tiempo (primera entrada hasta la fecha prometida +
    `dias`; antes de la fecha no penaliza), completa (lo recibido en total
    entre −/+ `tolerancia` de lo pedido) y OTIF (lo recibido HASTA la fecha
    límite dentro de esa banda), con el motivo de la falla."""
    limite = [f + datetime.timedelta(days=dias) for f in df["fecha_entrega"]]
    minimo, maximo = df["cantidad_pedido"] * (1 - tolerancia), df["cantidad_pedido"] * (1 + tolerancia)
    primera = df["primera_entrada"]
    df = df.assign(
        limite=limite,
        a_tiempo=[pd.notna(p) and p <= li for p, li in zip(primera, limite)],
        completo=(df["recibido_total"] >= minimo) & (df["recibido_total"] <= maximo),
        otif=(df["recibido_a_tiempo"] >= minimo) & (df["recibido_a_tiempo"] <= maximo),
    )
    motivos = []
    for r in df.itertuples():
        if r.otif:
            motivos.append("")
        elif pd.isna(r.primera_entrada) or r.recibido_total <= 0:
            motivos.append("Sin entrega")
        elif not r.a_tiempo:
            motivos.append(f"Llegó {(r.primera_entrada - r.fecha_entrega).days} días después de la fecha")
        elif r.recibido_a_tiempo > r.cantidad_pedido * (1 + tolerancia):
            motivos.append(f"Excedido: {r.recibido_a_tiempo / r.cantidad_pedido * 100:z.0f}% de lo pedido")
        else:
            motivos.append(f"Incompleto a la fecha: {r.recibido_a_tiempo / r.cantidad_pedido * 100:z.0f}% de lo pedido")
    return df.assign(motivo=motivos)


def _pct_valor(df, columna):
    total = float(df["valor"].sum())
    return float(df.loc[df[columna], "valor"].sum() / total * 100) if total else None


def otif(inicio, fin, categoria=None):
    """OTIF (On Time In Full) ponderado por valor de las líneas de pedido de
    Repuestos y Suministros Industriales (o de `categoria`) con fecha de
    entrega prometida en el período, definido por el usuario el 2026-10-04:
    a tiempo = hasta 5 días después de la fecha prometida; completo = ±5% de
    lo pedido; por valor. Una línea se evalúa recién cuando venció su plazo
    con tolerancia. Devuelve el % OTIF, a tiempo y completo, el detalle por
    categoría y proveedor, la evolución mensual y las fallas de más valor."""
    config = cargar_metas()
    supuestos, meta = config["supuestos"], config["metas"]["otif_pct"]["meta"]
    dias, tolerancia = supuestos["otif_dias_tolerancia"], supuestos["otif_cantidad_tolerancia_pct"] / 100
    categorias = tuple(supuestos["otif_categorias"]) if categoria is None else (categoria,)
    if categoria is not None and categoria not in supuestos["otif_categorias"]:
        return None
    hoy = datetime.date.today()
    df = q.otif_lineas(inicio, fin, categorias, dias)
    if df.empty:
        return None
    cerradas = pd.Series([f + datetime.timedelta(days=dias) < hoy for f in df["fecha_entrega"]], index=df.index)
    pendientes = int((~cerradas).sum())
    df = df[cerradas]
    if df.empty or not df["valor"].sum():
        return None
    df = _evaluar_otif(df, dias, tolerancia)

    def resumen(g):
        return {"otif": _pct_valor(g, "otif"), "a_tiempo": _pct_valor(g, "a_tiempo"), "completo": _pct_valor(g, "completo"),
                "lineas": int(len(g)), "valor": float(g["valor"].sum()),
                "valor_falla": float(g.loc[~g["otif"], "valor"].sum())}

    proveedores = []
    for nombre, g in df.groupby("proveedor"):
        tarde = g[~g["a_tiempo"] & g["primera_entrada"].notna()]
        atraso = [(p - f).days for p, f in zip(tarde["primera_entrada"], tarde["fecha_entrega"])]
        proveedores.append({"proveedor": nombre, **resumen(g),
                            "atraso": sum(atraso) / len(atraso) if atraso else None})
    proveedores.sort(key=lambda p: -p["valor"])
    fallas = df[~df["otif"]].sort_values("valor", ascending=False).head(15)
    meses = {}
    for r in df.itertuples():
        meses.setdefault(r.fecha_entrega.replace(day=1), []).append(r.Index)
    serie = [(m, _pct_valor(df.loc[idx], "otif")) for m, idx in sorted(meses.items())]
    return {
        **resumen(df), "pendientes": pendientes, "dias": dias, "tolerancia": supuestos["otif_cantidad_tolerancia_pct"],
        "categorias": [{"categoria": c, **resumen(g)} for c, g in df.groupby("categoria")],
        "proveedores": proveedores[:25], "proveedores_total": len(proveedores),
        "fallas": [{"pedido": r.pedido, "posicion": r.posicion, "material_codigo": r.material_codigo,
                    "texto_breve": r.texto_breve, "proveedor": r.proveedor, "fecha_entrega": r.fecha_entrega,
                    "valor": r.valor, "motivo": r.motivo, "categoria": r.categoria} for r in fallas.itertuples()],
        "serie": serie, "spark": _sparkline(serie),
        "grafico": charts.barras_mensuales([(_mes_corto(m), v) for m, v in serie], alto=220, meta=meta),
    }


# --- Portada ----------------------------------------------------------------

def contexto(mes, area=None):
    """Todo lo de la portada y el informe para `mes`, guardado en la caché
    versionada de queries (se recalcula si cambian los datos, este código o
    metas.json). `area`: None = toda la compañía (portada e informe
    general); si no, un dict de area_categoria / area_planificacion (solo
    sus categorías, con los enlaces a sus pestañas), para el Resumen y el
    Informe mensual de cada área (pedido del usuario, 2026-10-21)."""
    clave = ("ejecutivo", mes.isoformat(), _HUELLA, os.stat(_RUTA_METAS).st_mtime_ns, datetime.date.today(),
             tuple(area["categorias"]) if area else None)
    return q._cache_obtener(clave, lambda: _calcular(mes, cargar_metas(), area))


def area_categoria(categoria, url, zonas):
    """Área de una página de categoría: sus enlaces van a sus pestañas."""
    nombre = categoria if categoria == "CAPEX" else categoria.capitalize()  # como en la barra de navegación
    return {"nombre": nombre, "categorias": (categoria,), "url": url, "zonas": set(zonas)}


def area_planificacion(url, zonas):
    """Área de Planificación: las categorías del S&OP (Materias Primas y Combustibles)."""
    return {"nombre": "Planificación", "categorias": tuple(sorted(q.CATEGORIAS_PLANIFICACION_SOP)), "url": url,
            "zonas": set(zonas)}


def _pmp_de(df):
    """PMP ponderado por valor de `df` (condicion_pago, valor_total)."""
    if df.empty or not df["valor_total"].sum():
        return 0.0
    dias = df["condicion_pago"].map(q.dias_ponderados_condicion_pago)
    return float((dias * df["valor_total"]).sum() / df["valor_total"].sum())


def _calcular(mes, config, area=None):
    metas, supuestos = config["metas"], config["supuestos"]
    categorias_area = set(area["categorias"]) if area else None

    def del_area(df):
        """`df` (con columna categoria) solo con las categorías del área."""
        return df[df["categoria"].isin(categorias_area)] if area and not df.empty else df

    def en_area(categoria):
        return not area or categoria in categorias_area

    def enlace(zona, ancla_general):
        """Enlace de un indicador: en la portada, al Overview (o la página
        indicada); en un área, a su pestaña `zona` (vacío si no la tiene)."""
        if not area:
            return ancla_general
        return f"{area['url']}#{zona}" if zona in area["zonas"] else ""
    hoy = datetime.date.today()
    fin = min(_fin_mes(mes), hoy)
    inicio = datetime.date(mes.year, 1, 1)
    inicio_ant = inicio.replace(year=mes.year - 1)
    fin_ant = datetime.date(mes.year - 1, fin.month, min(fin.day, calendar.monthrange(mes.year - 1, fin.month)[1]))
    inicio_12 = q._sumar_meses(mes.replace(day=1), -11)
    excluidas = set(supuestos["categorias_sin_formula_contrato"])
    tramo = _tramo(inicio, fin)
    kpis, mensajes = [], []

    def kpi(clave, titulo, cifra, sub, estado="neutro", meta="", impacto="", spark=None, enlace="", valor=None):
        kpis.append({"clave": clave, "titulo": titulo, "cifra": cifra, "sub": sub, "estado": estado,
                     "estado_texto": _TEXTO_ESTADO[estado], "meta": meta, "impacto": impacto,
                     "spark": spark, "enlace": enlace, "valor": valor})

    def mensaje(estado, peso_soles, texto):
        mensajes.append({"estado": estado, "peso": (_PESO_ESTADO[estado], abs(peso_soles or 0)), "texto": texto})

    url_overview = reverse("overview")

    # 1-2. Compras del año y % bajo contrato (sin CAPEX ni Empaques: la
    # fórmula de contrato no los alcanza y se ajustan a mano).
    df = del_area(q.compras_por_categoria_contrato_rango(inicio, fin))
    df_ant = del_area(q.compras_por_categoria_contrato_rango(inicio_ant, fin_ant))
    total, total_ant = float(df["valor_total"].sum()), float(df_ant["valor_total"].sum())
    dias = (fin - inicio).days + 1
    diario = total / dias if dias else 0.0
    # Compras por mes y condición de pago (insumo también del PMP mensual).
    por_mes_condicion = (pd.concat([q.compras_por_mes_y_condicion_pago(c) for c in area["categorias"]], ignore_index=True)
                         if area else q.compras_por_mes_y_condicion_pago(None))
    serie_compras = por_mes_condicion.assign(mes=pd.to_datetime(por_mes_condicion["mes"]).dt.date).groupby("mes")["valor_total"].sum()
    spark_compras = [(m, v) for m, v in serie_compras.items() if q._sumar_meses(mes, -12) <= m <= mes]
    variacion = (total / total_ant - 1) * 100 if total_ant else None
    # Compras del año: cifra de contexto en la cabecera (no tiene meta); la
    # grilla queda para los indicadores que se evalúan contra una meta.
    compras = {"total": total, "variacion": variacion, "diario": diario, "anio_anterior": mes.year - 1,
               "spark": _sparkline(spark_compras)}

    con_formula = df[~df["categoria"].isin(excluidas)]
    base_contrato = float(con_formula["valor_total"].sum())
    contrato = float(con_formula["valor_bajo_contrato"].sum())
    contrato_pct = contrato / base_contrato * 100 if base_contrato else None
    fuera = base_contrato - contrato
    estado = _estado(contrato_pct, metas["contrato_pct"])
    # Un área de solo CAPEX o Empaques no tiene este indicador (la fórmula no los alcanza).
    if not area or categorias_area - excluidas:
        kpi("contrato", "Compras bajo contrato", _pct(contrato_pct),
            f"{tramo} · sin CAPEX ni Empaques (se ajustan a mano)" if not area else tramo,
            estado, _texto_meta(metas["contrato_pct"], lambda v: f"{v:z.0f}%"),
            f"{soles(fuera)} comprados fuera de contrato", enlace=enlace("compras", f"{url_overview}#sec-compras"),
            valor=contrato_pct)
        if contrato_pct is not None:
            mensaje(estado, fuera * supuestos["ahorro_contrato_marco_pct"] / 100,
                    f"El {_pct(contrato_pct, decimales=0)} de las compras se hace bajo contrato "
                    f"({soles(fuera)} fuera de contrato en {tramo}{'' if area else ', sin CAPEX ni Empaques'}).")

    # 3. Plazo medio de pago, con su equivalente en caja.
    if area:
        pmp = _pmp_de(del_area(q.compras_por_categoria_y_condicion_pago_rango(inicio, fin)))
        pmp_ant = _pmp_de(del_area(q.compras_por_categoria_y_condicion_pago_rango(inicio_ant, fin_ant)))
        serie_pmp = [(datetime.date.fromisoformat(str(m)[:10]), _pmp_de(g)) for m, g in por_mes_condicion.groupby("mes")]
    else:
        pmp = q.calcular_pmp_rango(inicio, fin)
        pmp_ant = q.calcular_pmp_rango(inicio_ant, fin_ant)
        serie_pmp = [(r.mes, r.pmp) for r in q.pmp_mensual().itertuples()]
    cfg = metas["pmp_dias"]
    estado = _estado(pmp, cfg)
    brecha = cfg["meta"] - pmp
    caja_meta = brecha * diario
    dif_pmp = round(pmp - pmp_ant)
    kpi("pmp", "Plazo medio de pago", f"{pmp:z.0f} días",
        f"{tramo} · " + (f"{dif_pmp:+d} días vs {mes.year - 1}" if dif_pmp else f"igual que {mes.year - 1}"),
        estado, _texto_meta(cfg, lambda v: f"{v:z.0f} días"),
        (f"Llegar a la meta liberaría {soles(caja_meta)} de caja" if brecha > 0 else
         f"{-brecha:z.0f} días sobre la meta: {soles(-caja_meta)} de caja a favor")
        + f" · cada día = {soles(diario)}",
        _sparkline([(m, v) for m, v in serie_pmp if q._sumar_meses(mes, -12) <= m <= mes]),
        enlace("cuentas", f"{url_overview}#sec-compras"), pmp)
    mensaje(estado, caja_meta,
            f"Pagamos a {pmp:z.0f} días en promedio "
            + (f"({dif_pmp:+d} vs {mes.year - 1})" if dif_pmp else f"(igual que {mes.year - 1})") + "; cada día adicional de plazo "
            f"libera {soles(diario)} de caja" + (f", y llegar a la meta de {cfg['meta']:z.0f} días, {soles(caja_meta)}."
                                                 if brecha > 0 else "."))

    # 4. IPU: inflación de los precios de compra del año.
    base_precios = del_area(q.precios_entrada_materiales_real())
    inflacion = q.inflacion_precios(base_precios, 12) if not base_precios.empty else pd.DataFrame()
    ipu = q.ipu_por_mes(inflacion, 12) if not inflacion.empty else {}
    meses_ipu = [m for m in ipu if m.year == mes.year and m <= mes]
    cfg = metas["ipu_pct"]
    if meses_ipu:
        d = ipu[max(meses_ipu)]
        ipu_pct, bolsa = d["acumulado"] * 100, d["bolsa_acumulada"]
        estado = _estado(ipu_pct, cfg)
        kpi("ipu", "IPU · inflación de compras", _pct(ipu_pct, True),
            f"{_MESES[1]}–{_MESES[max(meses_ipu).month]} {mes.year} vs el mismo tramo de {mes.year - 1}",
            estado, _texto_meta(cfg, lambda v: f"{v:+z.1f}%"),
            f"{'Sobrecosto' if bolsa > 0 else 'Ahorro'} de {soles(abs(bolsa))} por precio",
            _sparkline([(m, ipu[m]["individual"] * 100) for m in sorted(ipu) if q._sumar_meses(mes, -12) <= m <= mes]),
            enlace("precios", ""), ipu_pct)
        mensaje(estado, bolsa,
                f"Los precios de compra {'subieron' if ipu_pct > 0 else 'bajaron'} {abs(ipu_pct):z.1f}% en {mes.year} (IPU): "
                f"{'un sobrecosto' if bolsa > 0 else 'un ahorro'} de {soles(abs(bolsa))} frente a {mes.year - 1}.")
    elif not area:  # un área sin precios comparables (servicios) no lo muestra
        kpi("ipu", "IPU · inflación de compras", "—", "Sin compras comparables en el año")

    # 5-6. Inventario y capital inmovilizado (al último corte del mes).
    cortes = [c for c in _cortes_inventario() if c <= fin]
    corte = cortes[-1] if cortes else None
    resumen_categorias = {}
    valor_inv = valor_obs = 0.0
    inv = del_area(q.inventario_por_material(corte)) if corte else pd.DataFrame()
    # Un área sin inventario (CAPEX, servicios) no tiene estos indicadores.
    if corte and not inv.empty:
        valor_inv = float(inv["valor_total"].sum())
        dic = next((c for c in reversed(cortes) if c.year == mes.year - 1 and c.month == 12), None)
        valor_dic = float(del_area(q.inventario_por_material(dic))["valor_total"].sum()) if dic else None
        consumo = q.consumo_valorizado_6_meses_por_material(corte)
        consumo = consumo[consumo["material_codigo"].isin(set(inv["material_codigo"]))]
        consumo = consumo.merge(q.catalogo_materiales()[["material_codigo", "categoria"]], on="material_codigo", how="left")
        consumo_mes = float(consumo["consumo_valor_6meses"].sum()) / 6
        doh = valor_inv / consumo_mes * _DIAS_MES if consumo_mes > 0 else None
        var_inv = (valor_inv / valor_dic - 1) * 100 if valor_dic else None
        cfg = metas["inventario_var_pct"]
        estado = _estado(var_inv, cfg)
        serie = del_area(q.valor_inventario_mensual_por_categoria(fecha_desde=cortes[max(0, len(cortes) - 13)]))
        serie = serie[serie["fecha"].isin(cortes)].groupby("fecha")["valor_total"].sum()
        kpi("inventario", "Inventario", soles(valor_inv),
            f"Al {corte:%d/%m/%Y} · {_pct(var_inv, True)} vs diciembre {mes.year - 1}" if var_inv is not None else f"Al {corte:%d/%m/%Y}",
            estado, _texto_meta(cfg, lambda v: f"{v:+z.0f}% vs diciembre"),
            (f"{doh:z,.0f} días de inventario (DOH)" if doh else "")
            + (f" · {soles(valor_inv - valor_dic, True)} en el año" if valor_dic else ""),
            _sparkline([(f.replace(day=1), v) for f, v in serie.items()]),
            enlace("inventario", f"{url_overview}#bloque-inventario"), var_inv)
        if valor_dic:
            mensaje(estado, valor_inv - valor_dic,
                    f"El inventario {'creció' if valor_inv > valor_dic else 'bajó'} {soles(abs(valor_inv - valor_dic))} "
                    f"en el año ({_pct(var_inv, True)}) y cubre {doh:z,.0f} días de consumo." if doh else
                    f"El inventario {'creció' if valor_inv > valor_dic else 'bajó'} {soles(abs(valor_inv - valor_dic))} en el año.")

        obsoletos = [r for r in q.materiales_obsoletos_por_categoria(corte) if en_area(r["categoria"])]
        valor_obs = sum(float(r["valor_obsoleto"]) for r in obsoletos)
        obs_pct = valor_obs / valor_inv * 100 if valor_inv else None
        cfg = metas["inmovilizado_pct"]
        estado = _estado(obs_pct, cfg)
        mayor = max(obsoletos, key=lambda r: r["valor_obsoleto"]) if obsoletos else None
        # En un área de una sola categoría no se repite su nombre.
        una_categoria = area and len(categorias_area) == 1
        kpi("inmovilizado", "Capital inmovilizado", soles(valor_obs),
            f"{_pct(obs_pct, decimales=0)} del inventario sin consumo en 3 años",
            estado, _texto_meta(cfg, lambda v: f"{v:z.0f}% del inventario"),
            (f"{mayor['materiales_obsoletos']:,} materiales" if una_categoria else
             f"{mayor['categoria'].title()}: {soles(mayor['valor_obsoleto'])}") if mayor else "",
            enlace=enlace("inventario", f"{url_overview}#sec-obsoletos"), valor=obs_pct)
        if mayor:
            mensaje(estado, valor_obs,
                    f"{soles(valor_obs)} de inventario ({_pct(obs_pct, decimales=0)}) no tiene consumo hace 3 años"
                    + ("." if una_categoria else f"; {soles(mayor['valor_obsoleto'])} es de {mayor['categoria'].title()}."))
        for cat, g in inv.groupby("categoria"):
            c_mes = float(consumo.loc[consumo["categoria"] == cat, "consumo_valor_6meses"].sum()) / 6
            resumen_categorias.setdefault(cat, {})["inventario"] = float(g["valor_total"].sum())
            resumen_categorias[cat]["doh"] = float(g["valor_total"].sum()) / c_mes * _DIAS_MES if c_mes > 0 else None

    # OTIF de Repuestos y Suministros Industriales (del año, por valor), con
    # su evolución de los últimos 12 meses.
    # Un área sin categorías con OTIF (todas menos Repuestos y Suministros) no lo muestra.
    cfg = metas["otif_pct"]
    categorias_otif = [c for c in supuestos["otif_categorias"] if en_area(c)]
    nombre_otif = (" y ".join(c.title() for c in categorias_otif) if area else "Repuestos y Suministros")
    otif_anio = otif_12 = None
    if categorias_otif:
        if area and len(categorias_otif) == 1:
            otif_anio, otif_12 = otif(inicio, fin, categorias_otif[0]), otif(inicio_12, fin, categorias_otif[0])
        else:
            otif_anio, otif_12 = otif(inicio, fin), otif(inicio_12, fin)
    if otif_anio:
        estado = _estado(otif_anio["otif"], cfg)
        kpi("otif", "OTIF · a tiempo y completo", _pct(otif_anio["otif"]),
            f"{tramo} · {nombre_otif} · a tiempo {otif_anio['a_tiempo']:z.0f}% · completo {otif_anio['completo']:z.0f}%",
            estado, _texto_meta(cfg, lambda v: f"{v:z.0f}%"),
            f"{soles(otif_anio['valor_falla'])} entregados fuera de plazo o incompletos",
            otif_12["spark"] if otif_12 else None,
            enlace("compras", reverse("categoria_detalle", kwargs={"slug": "repuestos"}) + "#sec-otif"), otif_anio["otif"])
        if estado != "ok":
            problema = "la puntualidad" if otif_anio["a_tiempo"] < otif_anio["completo"] else "las cantidades"
            mensaje(estado, otif_anio["valor_falla"],
                    f"Solo el {otif_anio['otif']:z.0f}% del valor de {nombre_otif} llegó a tiempo y completo en "
                    f"{tramo} (meta {cfg['meta']:z.0f}%); el problema es {problema} "
                    f"(a tiempo {otif_anio['a_tiempo']:z.0f}%, completo {otif_anio['completo']:z.0f}%).")
    elif categorias_otif:
        kpi("otif", "OTIF · a tiempo y completo", "—", "Sin entregas evaluables en el período")

    # 7. Cumplimiento del plan de abastecimiento S&OP del mes (solo áreas con S&OP).
    cfg = metas["cumplimiento_sop_pct"]
    categorias_sop = [c for c in sorted(q.CATEGORIAS_PLANIFICACION_SOP) if en_area(c)]
    categoria_sop = categorias_sop[0] if area and len(categorias_sop) == 1 else None
    nombre_sop = categoria_sop.lower() if categoria_sop else "materias primas y combustibles"
    if categorias_sop and mes in set(q.meses_planificacion_disponibles()):
        det = q.plan_vs_real_abastecimiento_por_material(mes, categoria=categoria_sop)
        plan, real = float(det["cantidad_plan"].sum()), float(det["cantidad_real"].sum())
        cumpl = real / plan * 100 if plan else None
        estado = _estado(cumpl, cfg)
        serie = [(f["mes"], f["cumplimiento_pct"]) for f in q.cumplimiento_mensual_abastecimiento(categoria=categoria_sop)
                 if f["mes"] <= mes]
        kpi("sop", "Cumplimiento del plan de abastecimiento", _pct(cumpl),
            f"{q.formato_mes_anio(mes)} · {real:z,.0f} de {plan:z,.0f} T ({nombre_sop.capitalize()})",
            estado, _texto_meta(cfg, lambda v: f"{v:z.0f}%"),
            f"{plan - real:z,.0f} T por debajo del plan" if real < plan else f"{real - plan:z,.0f} T sobre el plan",
            _sparkline(serie), enlace("abastecimiento" if categoria_sop else "cumplimiento",
                                      reverse("planificacion") + "#cumplimiento"), cumpl)
        if cumpl is not None and estado != "ok":
            mensaje(estado, 0, f"El abastecimiento de {nombre_sop} cumplió el {cumpl:z.0f}% del plan en "
                               f"{q.formato_mes_anio(mes)} ({plan - real:z,.0f} T por debajo).")
    elif categorias_sop:
        kpi("sop", "Cumplimiento del plan de abastecimiento", "—", f"Sin plan S&OP de {q.formato_mes_anio(mes)}")

    # 8. Dependencia de proveedor único (materiales recurrentes, 12 meses; sin CAPEX).
    unico = proveedor_unico(inicio_12, fin, categoria=area["categorias"] if area else None,
                            meses_minimos=supuestos["meses_compra_recurrente"])
    cfg = metas["proveedor_unico_pct"]
    estado = _estado(unico["pct"], cfg)
    if not area or unico["pct"] is not None:
        kpi("unico", "Dependencia de proveedor único", _pct(unico["pct"], decimales=0),
            f"De las compras recurrentes de {_tramo(inicio_12, fin)}" + ("" if area else " (sin CAPEX)"),
            estado, _texto_meta(cfg, lambda v: f"{v:z.0f}%"),
            f"{soles(unico['valor_unico'])} en {unico['materiales_unicos']:,} materiales con un solo proveedor",
            enlace=enlace("compras", f"{url_overview}#sec-unico"), valor=unico["pct"])
    if unico["pct"] is not None and estado != "ok":
        mensaje(estado, unico["valor_unico"],
                f"El {unico['pct']:z.0f}% de las compras recurrentes depende de un solo proveedor "
                f"({soles(unico['valor_unico'])}, {unico['materiales_unicos']:,} materiales).")

    # 9. Materiales críticos en riesgo de quiebre.
    cfg = metas["criticos_en_riesgo"]
    criticos = [r for r in (q.cobertura_materiales_criticos_por_categoria(corte) if corte else []) if en_area(r["categoria"])]
    en_riesgo = sum(r["en_riesgo"] for r in criticos)
    total_criticos = sum(r["materiales_criticos"] for r in criticos)
    estado = _estado(en_riesgo, cfg) if criticos else "neutro"
    if not area or criticos:
        kpi("criticos", "Críticos en riesgo", f"{en_riesgo:,}" if criticos else "—",
            f"De {total_criticos:,} materiales críticos, con menos de 1 mes de cobertura" if criticos else "Sin datos de cobertura",
            estado, _texto_meta(cfg, lambda v: f"{v:z.0f}"),
            "" if area and len(categorias_area) == 1 else
            ", ".join(f"{r['categoria'].title()}: {r['en_riesgo']}" for r in criticos if r["en_riesgo"]),
            enlace=enlace("inventario", f"{url_overview}#sec-criticos"), valor=en_riesgo)
    if en_riesgo:
        mensaje(estado, 0, f"{en_riesgo} materiales críticos tienen menos de 1 mes de cobertura.")

    # Resumen por categoría (compras, contrato, PMP, IPU y proveedor único).
    pmp_cat = del_area(q.pmp_por_categoria_rango(inicio, fin)).set_index("categoria")["pmp"]
    for r in df.itertuples():
        anterior = df_ant.loc[df_ant["categoria"] == r.categoria, "valor_total"]
        fila = resumen_categorias.setdefault(r.categoria, {})
        fila.update({
            "compras": float(r.valor_total),
            "variacion": (float(r.valor_total) / float(anterior.iloc[0]) - 1) * 100 if len(anterior) and anterior.iloc[0] else None,
            "contrato": None if r.categoria in excluidas else (float(r.valor_bajo_contrato) / float(r.valor_total) * 100 if r.valor_total else None),
            "pmp": float(pmp_cat.get(r.categoria)) if r.categoria in pmp_cat else None,
        })
    ipu_categoria = {}
    if not inflacion.empty:
        del_anio = inflacion[[m.year == mes.year and m <= mes for m in inflacion["mes"]]]
        for cat, g in del_anio.groupby("categoria"):
            canasta = q.inflacion_canasta(g)
            if canasta["variacion"] is not None:
                ipu_categoria[cat] = canasta["variacion"]
                resumen_categorias.setdefault(cat, {})["ipu"] = canasta["variacion"] * 100
    for r in unico["por_categoria"]:
        resumen_categorias.setdefault(r["categoria"], {})["unico"] = r["pct"]
    categorias = [{"categoria": c, "slug": c.lower().replace(" ", "-"), **v} for c, v in sorted(resumen_categorias.items())
                  if (v.get("compras") or v.get("inventario")) and en_area(c)]

    cartera = _cartera(mes, inicio_12, fin, inicio, corte, supuestos, inflacion, ipu_categoria, valor_obs, en_area)
    if area:  # en un área, solo las palancas que tienen casos
        cartera["palancas"] = [p for p in cartera["palancas"] if p["casos"]]

    # Los 3 mensajes del mes: la oportunidad total, la alerta de más impacto
    # y la mejor noticia (un gerente también necesita ver qué va bien); si no
    # hay buena noticia, la segunda alerta.
    malos = sorted((m for m in mensajes if m["estado"] in ("alerta", "aviso")), key=lambda m: m["peso"], reverse=True)
    buenos = sorted((m for m in mensajes if m["estado"] == "ok"), key=lambda m: m["peso"], reverse=True)
    elegidos = []
    if cartera["total_caja"] or cartera["total_ahorro"]:
        elegidos.append({"estado": "oportunidad", "texto":
                         f"Hay {soles(cartera['total_caja'])} de caja que se puede liberar y {soles(cartera['total_ahorro'])} "
                         f"de ahorro potencial; la mayor palanca es "
                         f"{cartera['palancas'][0]['nombre'][:1].lower()}{cartera['palancas'][0]['nombre'][1:]} "
                         f"({soles(cartera['palancas'][0]['valor'])})."})
    elegidos += malos[:1] + buenos[:1]
    elegidos += [m for m in malos[1:] if m not in elegidos][:3 - len(elegidos)]
    return {
        "mes": mes, "tramo": tramo, "corte": corte, "kpis": kpis, "mensajes": elegidos[:3], "compras": compras,
        "cartera": cartera, "categorias": categorias, "unico": unico,
        "metas_propuestas": any(not m.get("confirmada") for m in metas.values()), "supuestos": supuestos,
        "generado": datetime.datetime.now(),
        "area": area["nombre"] if area else None,
        # El enlace de la cifra de compras de la cabecera.
        "enlace_compras": enlace("compras", f"{url_overview}#bloque-compras"),
        "con_inventario": bool(corte) and not inv.empty,
    }


# --- Cartera de oportunidades --------------------------------------------

def _cartera(mes, inicio_12, fin, inicio_anio, corte, supuestos, inflacion, ipu_categoria, valor_obs,
             en_area=lambda c: True):
    """Palancas con su valor en soles. Caja = capital de trabajo que se puede
    liberar una vez; Ahorro = menor costo anual. Cada palanca trae su
    supuesto a la vista y sus casos de más valor. `en_area`: qué categorías
    entran (Resumen de un área); el valor de cada palanca es la suma de sus casos."""
    palancas, dias_12 = [], (fin - inicio_12).days + 1

    def palanca(clave, nombre, tipo, valor, descripcion, items, supuesto=""):
        todos = len(items)
        items = [i for i in items if en_area(i["categoria"])]
        if len(items) != todos:
            valor = sum(i["valor"] for i in items)
        items = sorted(items, key=lambda i: -i["valor"])
        palancas.append({"clave": clave, "nombre": nombre, "tipo": tipo, "valor": float(valor),
                         "descripcion": descripcion, "supuesto": supuesto, "casos": len(items), "items": items[:12]})

    # 1. Plazo de pago: proveedores relevantes que se pagan antes que el PMP
    # de su categoría; caja = compras diarias × días que faltan.
    pv = q.compras_por_categoria_proveedor_condicion_rango(inicio_12, fin)
    items = []
    if not pv.empty:
        pv = pv.assign(valor_total=pv["valor_total"].astype(float),
                       dias=pv["condicion_pago"].map(q.dias_ponderados_condicion_pago))
        pv["peso"] = pv["dias"] * pv["valor_total"]
        for cat, g in pv.groupby("categoria"):
            ref = g["peso"].sum() / g["valor_total"].sum() if g["valor_total"].sum() else 0
            prov = g.groupby("proveedor").agg(valor=("valor_total", "sum"), peso=("peso", "sum"))
            prov["pmp"] = prov["peso"] / prov["valor"]
            prov = prov[(prov["valor"] >= supuestos["monto_minimo_proveedor_plazo"]) & (prov["pmp"] < ref - 1)]
            for nombre, r in prov.iterrows():
                items.append({"categoria": cat, "detalle": f"{nombre}: se le paga a {r['pmp']:z.0f} días (categoría: {ref:z.0f})",
                              "base": float(r["valor"]), "valor": float(r["valor"] / dias_12 * (ref - r["pmp"]))})
    palanca("plazo", "Plazo de pago", "Caja", sum(i["valor"] for i in items),
            "Llevar al plazo medio de su categoría a los proveedores (con más de "
            f"{soles(supuestos['monto_minimo_proveedor_plazo'])} al año) que hoy se pagan antes.", items,
            "Caja = compras diarias del proveedor × días de plazo que faltan.")

    # 2. Inventario sobre el máximo S&OP (Materias Primas y Combustibles).
    items = []
    if corte:
        cobertura = q.cobertura_stock_planificacion(fecha_stock=corte)
        minmax = q.minmax_sop(mes)
        if not cobertura.empty and not minmax.empty:
            minmax = minmax[[pd.Timestamp(m).date().replace(day=1) == mes for m in minmax["mes"]]]
            unidad = q.inventario_por_material(corte)
            unidad = unidad[unidad["stock_cierre"] > 0]
            precio = dict(zip(unidad["material_codigo"], unidad["valor_total"] / unidad["stock_cierre"]))
            cruce = cobertura.merge(minmax[["material_codigo", "centro", "stock_maximo"]], on=["material_codigo", "centro"])
            for r in cruce.itertuples():
                exceso = float(r.stock_cierre) - float(r.stock_maximo) if r.stock_maximo == r.stock_maximo else 0
                if exceso > 0 and precio.get(r.material_codigo):
                    items.append({"categoria": r.categoria,
                                  "detalle": f"{r.texto_breve} ({q.nombre_centro(r.centro)}): {exceso:z,.0f} T sobre el máximo",
                                  "base": exceso, "valor": exceso * float(precio[r.material_codigo])})
    palanca("exceso", "Stock sobre el máximo S&OP", "Caja", sum(i["valor"] for i in items),
            "Consumir antes de volver a comprar el stock que supera el máximo del S&OP del mes.", items,
            "Caja = toneladas sobre el máximo × costo unitario del inventario.")

    # 3. Inventario inmovilizado (sin consumo en 3 años).
    recuperable = supuestos["inmovilizado_recuperable_pct"]
    items = [{"categoria": r["categoria"], "detalle": f"{r['materiales_obsoletos']:,} materiales sin consumo en 3 años",
              "base": float(r["valor_obsoleto"]), "valor": float(r["valor_obsoleto"]) * recuperable / 100}
             for r in (q.materiales_obsoletos_por_categoria(corte) if corte else [])]
    palanca("inmovilizado", "Inventario inmovilizado", "Caja", valor_obs * recuperable / 100,
            f"Vender, devolver o reasignar el inventario sin consumo en 3 años ({soles(valor_obs)}).", items,
            f"Supuesto: se recupera el {recuperable}% de su valor (editable en metas.json).")

    # 4. Contratos marco: compras recurrentes de un material sin contrato.
    ahorro_pct = supuestos["ahorro_contrato_marco_pct"]
    df = q.compras_por_material_proveedor_rango(inicio_12, fin)
    items = []
    if not df.empty:
        df = df[~df["categoria"].isin(supuestos["categorias_sin_formula_contrato"])]
        mat = df.assign(valor_total=df["valor_total"].astype(float)).groupby(["categoria", "material_codigo"]).agg(
            texto_breve=("texto_breve", "first"), valor=("valor_total", "sum"), contrato=("valor_contrato", "sum"),
            meses=("meses", "sum"), proveedores=("proveedor", "nunique")).reset_index()
        mat = mat[(mat["contrato"] <= 0) & (mat["meses"] >= supuestos["meses_compra_recurrente"])]
        items = [{"categoria": r.categoria,
                  "detalle": f"{r.texto_breve} ({r.material_codigo}): {r.meses} meses con compra, "
                             f"{r.proveedores} proveedor{'es' if r.proveedores > 1 else ''}, sin contrato",
                  "base": float(r.valor), "valor": float(r.valor) * ahorro_pct / 100} for r in mat.itertuples()]
    palanca("contrato", "Contratos marco", "Ahorro", sum(i["valor"] for i in items),
            "Negociar contrato marco para los materiales que se compran todos los meses sin contrato "
            "(sin CAPEX ni Empaques).", items,
            f"Supuesto: un contrato marco ahorra el {ahorro_pct}% (editable en metas.json).")

    # 5. Precio sobre la inflación de su categoría (año en curso).
    items = []
    if not inflacion.empty:
        del_anio = pd.Series([m.year == mes.year and m <= mes for m in inflacion["mes"]], index=inflacion.index)
        anio = inflacion[del_anio & ~inflacion["atipica"].astype(bool)]
        if not anio.empty:
            anio = anio.assign(base_importe=anio["precio_base"] * anio["toneladas"])
            por_material = anio.groupby(["categoria", "material_codigo", "centro"]).agg(
                texto_breve=("texto_breve", "last"), importe=("importe", "sum"), base=("base_importe", "sum")).reset_index()
            for r in por_material.itertuples():
                tope = max(ipu_categoria.get(r.categoria, 0.0), 0.0)
                exceso = float(r.importe) - float(r.base) * (1 + tope)
                if r.base > 0 and exceso > 0:
                    items.append({"categoria": r.categoria,
                                  "detalle": f"{r.texto_breve} ({q.nombre_centro(r.centro)}): subió "
                                             f"{(r.importe / r.base - 1) * 100:+z.1f}% vs su categoría {tope * 100:+z.1f}%",
                                  "base": float(r.importe), "valor": exceso})
    palanca("precio", "Precios sobre la inflación", "Ahorro", sum(i["valor"] for i in items),
            f"Renegociar los materiales cuyo precio subió en {mes.year} más que el IPU de su categoría.", items,
            "Ahorro = lo pagado de más frente a haber subido como su categoría.")

    palancas.sort(key=lambda p: -p["valor"])
    total_caja = sum(p["valor"] for p in palancas if p["tipo"] == "Caja")
    total_ahorro = sum(p["valor"] for p in palancas if p["tipo"] == "Ahorro")
    mayor = max([p["valor"] for p in palancas] + [1.0])
    for p in palancas:
        p["pct"] = f"{p['valor'] / mayor * 100:z.1f}"
    casos = sorted(({**i, "palanca": p["nombre"], "tipo": p["tipo"]} for p in palancas for i in p["items"]),
                   key=lambda i: -i["valor"])
    return {"palancas": palancas, "total_caja": total_caja, "total_ahorro": total_ahorro, "casos": casos[:30],
            "periodo": _tramo(inicio_12, fin)}


# --- Informe mensual en PDF ------------------------------------------------

_RUTAS_EDGE = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def pdf_desde_html(html):
    """Convierte el HTML del informe (autocontenido) en PDF con Edge sin
    ventana (--print-to-pdf), el mismo navegador del servidor. Devuelve los
    bytes del PDF, o None si Edge no está o falla."""
    edge = next((r for r in _RUTAS_EDGE if os.path.exists(r)), None)
    if not edge:
        return None
    carpeta = tempfile.mkdtemp(prefix="informe_")
    try:
        entrada, salida = os.path.join(carpeta, "informe.html"), os.path.join(carpeta, "informe.pdf")
        with open(entrada, "w", encoding="utf-8") as f:
            f.write(html)
        proceso = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
             "--virtual-time-budget=8000", f"--user-data-dir={os.path.join(carpeta, 'perfil')}",
             f"--print-to-pdf={salida}", Path(entrada).as_uri()],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # A veces Edge escribe el PDF pero no termina de cerrarse (se vio en
        # pruebas: el pedido esperaba 2 minutos). Se espera solo hasta que el
        # archivo está completo (mismo tamaño dos veces seguidas) y, si Edge
        # sigue abierto, se cierra con todos sus procesos.
        limite, tamano = time.monotonic() + 60, -1
        while time.monotonic() < limite and proceso.poll() is None:
            if os.path.exists(salida):
                actual = os.path.getsize(salida)
                if actual > 0 and actual == tamano:
                    break
                tamano = actual
            time.sleep(0.5)
        if proceso.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proceso.pid), "/T", "/F"], capture_output=True, check=False)
            proceso.wait(timeout=10)
        if os.path.exists(salida) and os.path.getsize(salida) > 0:
            with open(salida, "rb") as f:
                return f.read()
        return None
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


def logo_data_uri():
    ruta = Path(__file__).resolve().parent / "static" / "gestion" / "img" / "unacem-logo.svg"
    return "data:image/svg+xml;base64," + base64.b64encode(ruta.read_bytes()).decode()
