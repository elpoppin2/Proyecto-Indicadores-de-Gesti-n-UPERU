"""Planificación S&OP > Calidad: análisis de los ingresos, premios y castigos, y costo por energía de los carbones."""

import pandas as pd

from .base import _cacheado, _query
from .inventario import catalogo_materiales
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
# con más PCS entrega más calorías por tonelada, así que el precio por T no
# alcanza para comparar proveedores: se compara S/ por Gcal (millón de kcal).
# Gcal de un ingreso = toneladas secas × PCS (kcal/kg) ÷ 1,000; costo por
# Gcal = importe (con premio) ÷ Gcal. Supone que el PCS del ticket está en la
# misma base (seca) que las toneladas que se pagan.
# ---------------------------------------------------------------------------

def ingresos_con_energia(df):
    """Documentos de premios_calidad_sop con PCS analizado, con su energía
    (gcal) y costo por Gcal efectivo (costo_gcal) y a precio base
    (costo_gcal_base)."""
    df = df[df["pcs"].notna() & (df["pcs"] > 0)].copy()
    df["gcal"] = df["toneladas"] * df["pcs"] / 1000
    df["costo_gcal"] = df["importe"] / df["gcal"]
    df["costo_gcal_base"] = (df["importe"] - df["premio"]) / df["gcal"]
    return df


def resumen_energia(df):
    """Totales de `df` (salida de ingresos_con_energia, ya filtrada):
    documentos, toneladas secas, Gcal, PCS y cenizas promedio (ponderados por
    toneladas), precio base y efectivo (S/ por T) y costo por Gcal efectivo,
    a precio base y el premio/castigo por Gcal."""
    toneladas = float(df["toneladas"].sum())
    gcal = float(df["gcal"].sum())
    importe = float(df["importe"].sum())
    premio = float(df["premio"].sum())
    return {
        "documentos": int(len(df)), "toneladas": toneladas, "gcal": gcal, "importe": importe, "premio": premio,
        "pcs": gcal * 1000 / toneladas if toneladas else 0.0,
        "cenizas": promedio_calidad(df, "cenizas"),
        "precio_base": (importe - premio) / toneladas if toneladas else 0.0,
        "precio_efectivo": importe / toneladas if toneladas else 0.0,
        "costo_gcal": importe / gcal if gcal else 0.0,
        "costo_gcal_base": (importe - premio) / gcal if gcal else 0.0,
        "premio_gcal": premio / gcal if gcal else 0.0,
    }
