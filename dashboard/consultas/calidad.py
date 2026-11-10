"""Planificación S&OP > Calidad: análisis de los ingresos, premios y castigos, y costo por energía de los carbones."""

import pandas as pd

from .base import _cacheado, _query
from .importados import embarques_importados
from .inventario import catalogo_materiales
from .precios import precios_entrada_sop_real
from .reglas import regla_local
from .sop_base import _IN_TIPOS_OPERACION_RECEPCION, materiales_planificados_sop


# ---------------------------------------------------------------------------
# Calidad (S&OP): análisis químico de los ingresos, desde los tickets de
# báscula de ZMM056 (columnas "Factor ..."). Pedido por el usuario. Cada
# material tiene sus propios parámetros (calizas: CaO/MgO/SO3; carbones:
# cenizas/volátiles/PCS/HGI; óxido de fierro: Fe2O3...); los que un material
# nunca trae no se muestran. Promedios ponderados por las toneladas de los
# tickets analizados (un mismo número de muestra cubre varios tickets).
# ---------------------------------------------------------------------------
# (campo, etiqueta, unidad, decimales), en el orden en que se muestran.
PARAMETROS_CALIDAD = [
    ("cao", "CaO", "%", 2), ("sio2", "SiO₂", "%", 2), ("al2o3", "Al₂O₃", "%", 2), ("fe2o3", "Fe₂O₃", "%", 2),
    ("mgo", "MgO", "%", 2), ("so3", "SO₃", "%", 2), ("sialfe", "SiO₂+Al₂O₃+Fe₂O₃", "%", 2),
    ("insolubles", "Insolubles", "%", 2), ("yeso", "Gypsum", "%", 2),
    ("pcs", "PCS", "kcal/kg", 0), ("cenizas", "Cenizas", "%", 2), ("volatiles", "Volátiles", "%", 2),
    ("azufre", "Azufre", "%", 2), ("azufre_elt", "S (Elt)", "%", 2), ("hgi", "HGI", "", 0),
    ("humedad", "Humedad", "%", 2), ("humedad_total", "Humedad total", "%", 2),
]
_CAMPOS_CALIDAD = [p[0] for p in PARAMETROS_CALIDAD]


@_cacheado
def calidad_ingresos_sop():
    """Un ticket de Recepción (tipo 1/5, ver _TIPOS_OPERACION_RECEPCION) por
    fila, de los materiales del Plan S&OP, con al menos un análisis:
    material_codigo, texto_breve, centro, categoria, fecha, proveedor,
    numero_muestra, toneladas y un campo por parámetro de
    PARAMETROS_CALIDAD (None = no analizado)."""
    materiales = materiales_planificados_sop()
    if not materiales:
        return pd.DataFrame()
    df = _query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro, fecha_ingreso AS fecha,
               proveedor AS proveedor, numero_muestra AS numero_muestra, cantidad AS toneladas,
               {", ".join(_CAMPOS_CALIDAD)}
        FROM gestion_ingresofisicosop
        WHERE material_codigo IN ({",".join(f"'{m}'" for m in materiales)})
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION}) AND fecha_ingreso IS NOT NULL
              AND ({" OR ".join(f"{c} IS NOT NULL" for c in _CAMPOS_CALIDAD)})
    """)
    if df.empty:
        return df
    df["fecha"] = pd.to_datetime(df["fecha"]).dt.date
    df["toneladas"] = df["toneladas"].astype(float)
    for c in _CAMPOS_CALIDAD:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "categoria"]]
    df = df.merge(catalogo, on="material_codigo", how="left")
    df.loc[df["categoria"].isna(), "categoria"] = "MATERIAS PRIMAS"
    df["texto_breve"] = df["texto_breve"].fillna("")
    return df.sort_values(["material_codigo", "centro", "fecha"]).reset_index(drop=True)


def parametros_con_datos(df):
    """Parámetros de PARAMETROS_CALIDAD que tienen al menos un análisis en `df`."""
    return [p for p in PARAMETROS_CALIDAD if df[p[0]].notna().any()]


def promedio_calidad(df, campo):
    """Promedio ponderado por toneladas de `campo` en `df` (solo tickets
    analizados en ese parámetro), o None."""
    con_dato = df[df[campo].notna() & (df["toneladas"] > 0)]
    if con_dato.empty:
        return None
    return float((con_dato[campo] * con_dato["toneladas"]).sum() / con_dato["toneladas"].sum())


def resumen_calidad(df, campo):
    """Estadísticos de `campo` en `df`: promedio ponderado por toneladas,
    mínimo y máximo por muestra, desviación entre muestras (cada muestra
    cuenta una vez: varios tickets repiten el mismo análisis), muestras y
    toneladas analizadas."""
    con_dato = df[df[campo].notna()]
    if con_dato.empty:
        return None
    por_muestra = con_dato.groupby(con_dato["numero_muestra"].where(con_dato["numero_muestra"] != "", con_dato.index.astype(str)))[campo].mean()
    return {
        "promedio": promedio_calidad(con_dato, campo),
        "minimo": float(por_muestra.min()), "maximo": float(por_muestra.max()),
        "desviacion": float(por_muestra.std()) if len(por_muestra) > 1 else 0.0,
        "muestras": int(len(por_muestra)), "toneladas": float(con_dato["toneladas"].sum()),
    }


# ---------------------------------------------------------------------------
# Premios y castigos por calidad (S&OP). Pedido por el usuario. Cada ticket
# de báscula de un tercero (ZMM056, Tipo 1) trae "Premio o Castigo" en soles
# (IngresoFisicoSOP.premio), y su entrada en MB51 (101, mismo Documento
# material) ya lo incluye: importe = precio base × toneladas secas + premio.
# Verificado en todos los materiales: la cantidad del 101 es el pesaje neto
# seco, y (importe − premio) ÷ cantidad da un precio base fijo por proveedor
# (el del contrato: p.ej. Carbón Terceros 270, 297.5 o 314 S/ por T).
# ---------------------------------------------------------------------------

@_cacheado
def premios_calidad_sop():
    """Un documento de ingreso por fila (ticket de tercero con su entrada de
    MB51): fecha, material_codigo, texto_breve, centro, categoria, proveedor,
    ticket, documento, toneladas (secas, las de MB51), importe (S/, con el
    premio incluido), premio (S/, + premio / − castigo), precio_base y
    precio_efectivo (S/ por T), premio_por_t, más los parámetros de calidad
    del ticket (PARAMETROS_CALIDAD)."""
    materiales = materiales_planificados_sop()
    if not materiales:
        return pd.DataFrame()
    ids = ",".join(f"'{m}'" for m in materiales)
    df = _query(f"""
        SELECT i.fecha_ingreso AS fecha, i.material_codigo AS material_codigo, i.centro AS centro,
               i.proveedor AS proveedor, i.ticket_registro AS ticket, i.documento_material AS documento,
               COALESCE(i.premio, 0) AS premio, m.cantidad AS toneladas, m.importe AS importe,
               {", ".join(f"i.{c} AS {c}" for c in _CAMPOS_CALIDAD)}
        FROM gestion_ingresofisicosop i
        JOIN (
            SELECT documento_material AS documento, material_codigo AS material,
                   SUM(cantidad) AS cantidad, SUM(importe_moneda_local) AS importe
            FROM gestion_movimientomaterial
            WHERE +clase_movimiento = '101' AND documento_material IN (
                SELECT documento_material FROM gestion_ingresofisicosop
                WHERE tipo_operacion = '1' AND documento_material <> '' AND material_codigo IN ({ids})
            )
            GROUP BY documento_material, material_codigo
        ) m ON m.documento = i.documento_material AND m.material = i.material_codigo
        WHERE i.tipo_operacion = '1' AND i.material_codigo IN ({ids}) AND i.fecha_ingreso IS NOT NULL
    """)
    if df.empty:
        return df
    df["fecha"] = pd.to_datetime(df["fecha"]).dt.date
    for c in ("premio", "toneladas", "importe", *_CAMPOS_CALIDAD):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["toneladas"] > 0].copy()
    df["premio"] = df["premio"].fillna(0.0)
    df["precio_efectivo"] = df["importe"] / df["toneladas"]
    df["premio_por_t"] = df["premio"] / df["toneladas"]
    df["precio_base"] = df["precio_efectivo"] - df["premio_por_t"]
    df["proveedor"] = df["proveedor"].replace("", "(sin proveedor)")
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "categoria"]]
    df = df.merge(catalogo, on="material_codigo", how="left")
    df.loc[df["categoria"].isna(), "categoria"] = "MATERIAS PRIMAS"
    df["texto_breve"] = df["texto_breve"].fillna("")
    return df.sort_values(["fecha", "ticket"], ascending=False).reset_index(drop=True)


def resumen_premios(df):
    """Totales de premios/castigos de `df` (salida de premios_calidad_sop, ya
    filtrada): toneladas, importe, premios (+), castigos (−), neto, neto por T,
    precio base y efectivo promedio ponderado (S/ por T), y % de documentos
    con premio y con castigo."""
    toneladas = float(df["toneladas"].sum())
    premios = float(df.loc[df["premio"] > 0, "premio"].sum())
    castigos = float(df.loc[df["premio"] < 0, "premio"].sum())
    neto = premios + castigos
    importe = float(df["importe"].sum())
    base = importe - neto
    return {
        "documentos": int(len(df)), "toneladas": toneladas, "importe": importe,
        "premios": premios, "castigos": castigos, "neto": neto,
        "neto_por_t": neto / toneladas if toneladas else 0.0,
        "precio_base": base / toneladas if toneladas else 0.0,
        "precio_efectivo": importe / toneladas if toneladas else 0.0,
        "pct_sobre_base": neto / base * 100 if base else 0.0,
        "pct_con_premio": float((df["premio"] > 0).mean() * 100) if len(df) else 0.0,
        "pct_con_castigo": float((df["premio"] < 0).mean() * 100) if len(df) else 0.0,
    }


# ---------------------------------------------------------------------------
# Costo por energía de los carbones (S&OP). Pedido por el usuario. Un carbón
# con más PCS entrega más energía por tonelada, así que el precio por T no
# alcanza para comparar proveedores: se compara S/ por GJ (desde el
# 2026-11-10, pedido del usuario; antes S/ por Gcal). GJ de un ingreso =
# toneladas × PCS (kcal/kg) × 4.1868 ÷ 1,000; costo por GJ = importe ÷ GJ.
# Supone que el PCS del ticket está en la misma base (seca) que las toneladas
# que se pagan.
# Importe = el de la entrada en MB51 (precio de contrato + premio o castigo)
# más los valores agregados: los ajustes de la FBL3N del material en ese mes
# (cambios de precio MR22, fletes y cargos facturados después), repartidos
# entre sus ingresos según las toneladas. Es el mismo precio real de Precios e
# IPU (precios_entrada_sop_real). Desde el 2026-11-10, pedido del usuario: sin
# ellos los carbones salían muy por debajo (sobre todo el Carbón Trujillo, con
# cambios de precio grandes). Un mes sin la FBL3N cargada todavía va sin ellos.
# PCS que no vienen del ticket (definidos por el usuario el 2026-11-10):
# - PCS fijo: el Carbón Importado no trae análisis en la ZMM056 (solo pasa por
#   la báscula en traslados): se usa un PCS fijo, que está en
#   datos_locales/reglas_locales.json ("pcs_fijo_energia": {material: kcal/kg}).
#   Sus ingresos son las llegadas a planta de precios_entrada_sop_real (puesto
#   en destino, con sus valores agregados), una fila por centro y mes.
# - PCS_DE_OTRO_CENTRO: el Carbón Trujillo de Condorcocha lleva el PCS
#   promedio del de Atocongo (el del mismo mes, ponderado por toneladas; si
#   ese mes Atocongo no tiene análisis, el de toda su historia).
# ---------------------------------------------------------------------------
KJ_POR_KCAL = 4.1868
PCS_DE_OTRO_CENTRO = {("06-00001601", "U002"): "U001"}


def gj(toneladas, pcs):
    """GJ de `toneladas` de un carbón con `pcs` kcal/kg."""
    return toneladas * pcs * KJ_POR_KCAL / 1000


def _valores_agregados_por_t():
    """{(material, centro, mes): S/ por T} de los ajustes de la FBL3N que
    suma el precio real (precios_entrada_sop_real)."""
    precios = precios_entrada_sop_real()
    if precios.empty:
        return {}
    precios = precios[(precios["toneladas"] > 0) & (precios["ajuste"] != 0)]
    return {(m, c, mes): a / t for m, c, mes, a, t in
            zip(precios["material_codigo"], precios["centro"], precios["mes"], precios["ajuste"], precios["toneladas"])}


def _pcs_de_otro_centro(df):
    """`df` (tickets) con el PCS de PCS_DE_OTRO_CENTRO: el promedio del mes
    del centro de referencia (o el de toda su historia)."""
    analisis = calidad_ingresos_sop()
    for (material, centro), referencia in PCS_DE_OTRO_CENTRO.items():
        filas = (df["material_codigo"] == material) & (df["centro"] == centro)
        if not filas.any() or analisis.empty:
            continue
        ref = analisis[(analisis["material_codigo"] == material) & (analisis["centro"] == referencia)
                       & analisis["pcs"].notna() & (analisis["toneladas"] > 0)]
        if ref.empty:
            continue
        ref = ref.assign(mes=[f.replace(day=1) for f in ref["fecha"]], kcal=ref["pcs"] * ref["toneladas"])
        por_mes = ref.groupby("mes")["kcal"].sum() / ref.groupby("mes")["toneladas"].sum()
        historia = float(ref["kcal"].sum() / ref["toneladas"].sum())
        df.loc[filas, "pcs"] = [float(por_mes.get(f.replace(day=1), historia)) for f in df.loc[filas, "fecha"]]
    return df


def _proveedores_importado(material):
    """Función mes -> nombre del proveedor de las llegadas de un importado:
    el del último embarque que llegó hasta ese mes (su pedido, en ME2N)."""
    barcos, _ = embarques_importados()
    barcos = barcos[barcos["material_codigo"] == material]
    if barcos.empty:
        return lambda mes: ""
    pedidos = ",".join(f"'{p}'" for p in barcos["pedido"].unique())
    nombres = _query(f"""
        SELECT doc_compra AS pedido, MAX(COALESCE(NULLIF(cuenta_proveedor, ''), proveedor)) AS nombre
        FROM gestion_lineacompra WHERE doc_compra IN ({pedidos}) GROUP BY doc_compra
    """)
    nombre_pedido = dict(zip(nombres["pedido"], nombres["nombre"]))
    llegadas = sorted((pd.Timestamp(l).date(), nombre_pedido.get(p, "")) for l, p in zip(barcos["llegada"], barcos["pedido"]))

    def proveedor(mes):
        fin = (pd.Timestamp(mes) + pd.offsets.MonthEnd(0)).date()
        anteriores = [n for l, n in llegadas if l <= fin]
        return anteriores[-1] if anteriores else llegadas[0][1]
    return proveedor


def _llegadas_con_pcs_fijo(desde):
    """Una fila por centro y mes (desde `desde`) de los materiales con PCS
    fijo, con las columnas de los tickets (premios_calidad_sop) y ese PCS:
    toneladas, importe (precio real) y documentos (entradas)."""
    fijos = regla_local("pcs_fijo_energia", {})
    precios = precios_entrada_sop_real()
    if not fijos or precios.empty:
        return pd.DataFrame()
    filas = []
    for material, pcs in fijos.items():
        p = precios[(precios["material_codigo"] == material) & (precios["toneladas"] > 0)]
        if desde is not None:
            p = p[p["mes"] >= desde]
        proveedor = _proveedores_importado(material)
        for r in p.itertuples():
            filas.append({
                "fecha": r.mes, "material_codigo": material, "centro": r.centro,
                "proveedor": proveedor(r.mes) or "(importado)", "ticket": "", "documento": "",
                "documentos": int(r.entradas), "premio": 0.0, "toneladas": float(r.toneladas),
                "importe": float(r.importe_mb51), "ajuste": float(r.ajuste), "pcs": float(pcs),
                "texto_breve": r.texto_breve, "categoria": r.categoria,
            })
    return pd.DataFrame(filas)


def ingresos_energia_sop():
    """Los ingresos de carbón con PCS para el costo por energía: los
    documentos de premios_calidad_sop (PCS del ticket, o el de
    PCS_DE_OTRO_CENTRO) y las llegadas de los de PCS fijo. Columnas de
    premios_calidad_sop más documentos (1 por ticket), ajuste (valores
    agregados, S/), importe_mb51, importe (con ellos), gj, costo_gj (efectivo)
    y costo_gj_base (a precio de contrato)."""
    tickets = premios_calidad_sop()
    if tickets.empty:
        return tickets
    tickets = _pcs_de_otro_centro(tickets.copy())
    tickets = tickets[tickets["pcs"].notna() & (tickets["pcs"] > 0)].copy()
    agregados = _valores_agregados_por_t()
    tickets["ajuste"] = [t * agregados.get((m, c, f.replace(day=1)), 0.0)
                         for m, c, f, t in zip(tickets["material_codigo"], tickets["centro"], tickets["fecha"], tickets["toneladas"])]
    tickets["documentos"] = 1
    desde = min(tickets["fecha"]).replace(day=1) if not tickets.empty else None
    df = pd.concat([tickets, _llegadas_con_pcs_fijo(desde)], ignore_index=True)
    df["importe_mb51"] = df["importe"]
    df["importe"] = df["importe_mb51"] + df["ajuste"]
    df["gj"] = gj(df["toneladas"], df["pcs"])
    df["costo_gj"] = df["importe"] / df["gj"]
    df["costo_gj_base"] = (df["importe_mb51"] - df["premio"]) / df["gj"]
    return df.sort_values(["fecha", "ticket"], ascending=False).reset_index(drop=True)


def resumen_energia(df):
    """Totales de `df` (salida de ingresos_energia_sop, ya filtrada):
    documentos, toneladas, GJ, PCS y cenizas promedio (ponderados por
    toneladas), precio de contrato (precio_base), valores agregados
    (ajuste_t) y precio efectivo (S/ por T), y costo por GJ efectivo, a
    precio de contrato, el premio/castigo por GJ y los valores agregados
    por GJ."""
    toneladas = float(df["toneladas"].sum())
    energia = float(df["gj"].sum())
    importe = float(df["importe"].sum())
    premio = float(df["premio"].sum())
    ajuste = float(df["ajuste"].sum())
    contrato = importe - premio - ajuste
    return {
        "documentos": int(df["documentos"].sum()), "toneladas": toneladas, "gj": energia, "importe": importe,
        "premio": premio, "ajuste": ajuste,
        "pcs": energia * 1000 / KJ_POR_KCAL / toneladas if toneladas else 0.0,
        "cenizas": promedio_calidad(df, "cenizas"),
        "precio_base": contrato / toneladas if toneladas else 0.0,
        "ajuste_t": ajuste / toneladas if toneladas else 0.0,
        "precio_efectivo": importe / toneladas if toneladas else 0.0,
        "costo_gj": importe / energia if energia else 0.0,
        "costo_gj_base": contrato / energia if energia else 0.0,
        "premio_gj": premio / energia if energia else 0.0,
        "ajuste_gj": ajuste / energia if energia else 0.0,
    }
