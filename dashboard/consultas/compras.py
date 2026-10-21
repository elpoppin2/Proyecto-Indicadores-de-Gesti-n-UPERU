"""Compras (ME2N): por mes, categoría, proveedor y condición de pago, PMP, concentración de proveedores, Fill Rate, Lead Time y OTIF."""

import pandas as pd
from django.db import connection

from .base import _cacheado, _query
from .inventario import categorias_inventario_disponibles
from .reglas import CATEGORIA_EFECTIVA_LC, FILTRO_BAJO_CONTRATO_LC, FILTRO_CATEGORIA_VALIDA_LC, FILTRO_COMPRAS_VALIDAS_LC, PROVEEDOR_EFECTIVO_LC, VALOR_COMPRA_EXPR_LC, dias_ponderados_condicion_pago


def anios_compras_disponibles():
    df = _query("SELECT DISTINCT anio_origen FROM gestion_lineacompra ORDER BY anio_origen")
    return df["anio_origen"].tolist()


def compras_por_mes(anio):
    # SQLite y PostgreSQL truncan fechas a "YYYY-MM" con funciones distintas.
    mes_expr = (
        "to_char(fecha_documento, 'YYYY-MM')" if connection.vendor == "postgresql"
        else "strftime('%%Y-%%m', fecha_documento)"
    )
    return _query(f"""
        SELECT {mes_expr} AS mes,
               SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total, COUNT(*) AS lineas
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.anio_origen = %s AND lc.fecha_documento IS NOT NULL
              AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
        GROUP BY mes
        ORDER BY mes
    """, [anio])


def top_proveedores(anio, top=15):
    return _query(f"""
        SELECT {PROVEEDOR_EFECTIVO_LC} AS proveedor,
               SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total, COUNT(*) AS lineas
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.anio_origen = %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
        GROUP BY proveedor
        ORDER BY valor_total DESC
        LIMIT %s
    """, [anio, top])


# Consolidación de categorías para el indicador "Compras por categoría": unifica
# categorías equivalentes/redundantes del catálogo. Definido por el usuario.
CONSOLIDACION_CATEGORIAS_COMPRA = {
    "MROM": "REPUESTOS",
    "MROE": "REPUESTOS",
    "CONSUMIBLES": "SUMINISTROS INDUSTRIALES",
    "SUMINISTROS": "SUMINISTROS INDUSTRIALES",
    "SUMINISTROS INDUSTRIALES": "SUMINISTROS INDUSTRIALES",
}

_CASE_CONSOLIDACION_CATEGORIAS = " ".join(
    f"WHEN '{origen}' THEN '{destino}'" for origen, destino in CONSOLIDACION_CATEGORIAS_COMPRA.items()
)

# Categoría de compra ya consolidada, reutilizable en cualquier consulta de compras.
CATEGORIA_COMPRA_CONSOLIDADA_LC = (
    f"CASE {CATEGORIA_EFECTIVA_LC} {_CASE_CONSOLIDACION_CATEGORIAS} ELSE {CATEGORIA_EFECTIVA_LC} END"
)


def categorias_compras_disponibles(anio):
    """Categorías (consolidadas) presentes en las compras de un año."""
    df = _query(f"""
        SELECT DISTINCT {CATEGORIA_COMPRA_CONSOLIDADA_LC} AS categoria
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.anio_origen = %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
        ORDER BY categoria
    """, [anio])
    return df["categoria"].tolist()


def categorias_dashboard(anio):
    """Categorías para las pestañas del dashboard: unión de las categorías con datos
    de Inventario y las que tienen datos de Compras en el año indicado."""
    inventario = set(categorias_inventario_disponibles())
    compras = set(categorias_compras_disponibles(anio))
    return sorted(inventario | compras)


def compras_por_categoria(anio, top=15):
    # Prioridad: categoría del grupo de artículos (CATEGORÍAS OFICIAL); si el material
    # no tiene grupo de artículos con categoría, usar la Categoría de Servicio asociada
    # al grupo de compras (GCp) como respaldo (compras de servicios, sin material); y
    # por último, se consolidan categorías equivalentes (ver CONSOLIDACION_CATEGORIAS_COMPRA).
    # Las líneas sin categoría resuelta y las categorías NO APLICA / ENERGÍA ELÉCTRICA
    # ya quedan excluidas por FILTRO_CATEGORIA_VALIDA_LC (no forman parte del modelo).
    # OJO: se agrupa por la expresión CASE/COALESCE completa, no por un alias que
    # coincida con el nombre de una columna real — SQLite, al tener una columna llamada
    # "categoria" en gestion_categoria, agruparía por esa columna cruda en vez de por el
    # resultado calculado, mezclando de forma arbitraria los distintos valores de
    # respaldo (bug ya detectado y corregido).
    return _query(f"""
        SELECT CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END AS categoria,
               SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.anio_origen = %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
        GROUP BY CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END
        ORDER BY valor_total DESC
        LIMIT %s
    """, [anio, top])


def compras_por_categoria_contrato(anio, top=15):
    """Valor total y valor bajo contrato por categoría (misma agrupación que
    compras_por_categoria), para comparar ambos en un gráfico de barras agrupadas."""
    return _query(f"""
        SELECT CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END AS categoria,
               SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total,
               SUM(CASE WHEN {FILTRO_BAJO_CONTRATO_LC} THEN {VALOR_COMPRA_EXPR_LC} ELSE 0 END) AS valor_bajo_contrato
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.anio_origen = %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
        GROUP BY CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END
        ORDER BY valor_total DESC
        LIMIT %s
    """, [anio, top])


def compras_por_categoria_contrato_rango(fecha_inicio, fecha_fin):
    """Igual que compras_por_categoria_contrato, pero acota el período por
    fecha_documento (rango de meses) en vez de por año completo (anio_origen).
    Insumo del selector de rango de fechas más flexible de Compras."""
    return _query(f"""
        SELECT CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END AS categoria,
               SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total,
               SUM(CASE WHEN {FILTRO_BAJO_CONTRATO_LC} THEN {VALOR_COMPRA_EXPR_LC} ELSE 0 END) AS valor_bajo_contrato
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.fecha_documento BETWEEN %s AND %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
        GROUP BY CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END
    """, [fecha_inicio, fecha_fin])


def compras_por_categoria_y_condicion_pago(anio):
    """Valor de Compras por categoría y condición de pago (CPag), insumo de pmp_por_categoria."""
    return _query(f"""
        SELECT CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END AS categoria,
               lc.condicion_pago AS condicion_pago,
               SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.anio_origen = %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
        GROUP BY CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END,
                 lc.condicion_pago
    """, [anio])


def pmp_por_categoria(anio):
    """PMP (mismo cálculo que calcular_pmp) desagregado por categoría."""
    df = compras_por_categoria_y_condicion_pago(anio)
    if df.empty:
        return pd.DataFrame(columns=["categoria", "pmp"])
    df = df.copy()
    df["dias_ponderados"] = df["condicion_pago"].map(dias_ponderados_condicion_pago)
    df["peso"] = df["dias_ponderados"] * df["valor_total"]
    agg = df.groupby("categoria", as_index=False).agg(peso=("peso", "sum"), valor_total=("valor_total", "sum"))
    agg["pmp"] = (agg["peso"] / agg["valor_total"].replace(0, pd.NA)).fillna(0)
    return agg[["categoria", "pmp"]].sort_values("pmp", ascending=False).reset_index(drop=True)


def compras_por_categoria_y_condicion_pago_rango(fecha_inicio, fecha_fin):
    """Igual que compras_por_categoria_y_condicion_pago, pero acotado por
    fecha_documento en vez de por año completo. Insumo de calcular_pmp_rango y
    pmp_por_categoria_rango, para el selector de rango de fechas de Compras."""
    return _query(f"""
        SELECT CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END AS categoria,
               lc.condicion_pago AS condicion_pago,
               SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.fecha_documento BETWEEN %s AND %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
        GROUP BY CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END,
                 lc.condicion_pago
    """, [fecha_inicio, fecha_fin])


def pmp_por_categoria_rango(fecha_inicio, fecha_fin):
    """PMP por categoría (mismo cálculo que pmp_por_categoria) para un rango de
    fechas arbitrario en vez de un año completo."""
    df = compras_por_categoria_y_condicion_pago_rango(fecha_inicio, fecha_fin)
    if df.empty:
        return pd.DataFrame(columns=["categoria", "pmp"])
    df = df.copy()
    df["dias_ponderados"] = df["condicion_pago"].map(dias_ponderados_condicion_pago)
    df["peso"] = df["dias_ponderados"] * df["valor_total"]
    agg = df.groupby("categoria", as_index=False).agg(peso=("peso", "sum"), valor_total=("valor_total", "sum"))
    agg["pmp"] = (agg["peso"] / agg["valor_total"].replace(0, pd.NA)).fillna(0)
    return agg[["categoria", "pmp"]].sort_values("pmp", ascending=False).reset_index(drop=True)


def calcular_pmp_rango(fecha_inicio, fecha_fin):
    """PMP general (todas las categorías) para un rango de fechas arbitrario,
    mismo cálculo que calcular_pmp."""
    df = compras_por_categoria_y_condicion_pago_rango(fecha_inicio, fecha_fin)
    if df.empty:
        return 0.0
    dias = df["condicion_pago"].map(dias_ponderados_condicion_pago)
    valor_total = df["valor_total"].sum()
    if not valor_total:
        return 0.0
    return float((dias * df["valor_total"]).sum() / valor_total)


def compras_por_mes_y_condicion_pago(categoria=None):
    """Valor de Compras por mes (Fecha doc.) y condición de pago, insumo de
    pmp_mensual. Si `categoria` es None agrega TODAS las categorías (PMP
    general de la compañía); si se indica, solo esa categoría."""
    filtro_categoria = ""
    params = []
    if categoria is not None:
        filtro_categoria = (
            f" AND CASE {CATEGORIA_EFECTIVA_LC} {_CASE_CONSOLIDACION_CATEGORIAS} "
            f"ELSE {CATEGORIA_EFECTIVA_LC} END = %s"
        )
        params = [categoria]
    # '%%' (no '%'): Django reconstruye el SQL "tal como se ejecutó" para el log
    # de depuración haciendo sql % params, así que un '%' literal (de strftime)
    # choca con el placeholder %s salvo que se escape como %%.
    return _query(f"""
        SELECT strftime('%%Y-%%m-01', lc.fecha_documento) AS mes,
               lc.condicion_pago AS condicion_pago,
               SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.fecha_documento IS NOT NULL AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}{filtro_categoria}
        GROUP BY strftime('%%Y-%%m-01', lc.fecha_documento), lc.condicion_pago
    """, params)


def pmp_mensual(categoria=None):
    """Serie mensual de PMP (mismo cálculo ponderado que pmp_por_categoria/
    calcular_pmp): general de la compañía si `categoria` es None, o de una sola
    categoría. Insumo del gráfico de Evolución de PMP."""
    df = compras_por_mes_y_condicion_pago(categoria)
    if df.empty:
        return pd.DataFrame(columns=["mes", "pmp"])
    df = df.copy()
    df["dias_ponderados"] = df["condicion_pago"].map(dias_ponderados_condicion_pago)
    df["peso"] = df["dias_ponderados"] * df["valor_total"]
    agg = df.groupby("mes", as_index=False).agg(peso=("peso", "sum"), valor_total=("valor_total", "sum"))
    agg["pmp"] = (agg["peso"] / agg["valor_total"].replace(0, pd.NA)).fillna(0)
    agg["mes"] = pd.to_datetime(agg["mes"]).dt.date
    return agg[["mes", "pmp"]].sort_values("mes").reset_index(drop=True)


def compras_por_categoria_y_proveedor(anio):
    """Valor de compras por categoría y proveedor (por nombre, vía
    PROVEEDOR_EFECTIVO_LC). Insumo del indicador de concentración de
    proveedores (Pareto)."""
    return _query(f"""
        SELECT CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END AS categoria,
               {PROVEEDOR_EFECTIVO_LC} AS proveedor,
               SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.anio_origen = %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
              AND {PROVEEDOR_EFECTIVO_LC} != ''
        GROUP BY CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END,
                 {PROVEEDOR_EFECTIVO_LC}
    """, [anio])


def compras_por_categoria_y_proveedor_rango(fecha_inicio, fecha_fin):
    """Igual que compras_por_categoria_y_proveedor, pero acotado por
    fecha_documento en vez de por año completo. Insumo del segmentador de Mes
    propio de Concentración de Proveedores (Pareto)."""
    return _query(f"""
        SELECT CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END AS categoria,
               {PROVEEDOR_EFECTIVO_LC} AS proveedor,
               SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.fecha_documento BETWEEN %s AND %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
              AND {PROVEEDOR_EFECTIVO_LC} != ''
        GROUP BY CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END,
                 {PROVEEDOR_EFECTIVO_LC}
    """, [fecha_inicio, fecha_fin])


def compras_por_categoria_proveedor_condicion_rango(fecha_inicio, fecha_fin):
    """Valor de compras por categoría, proveedor (por nombre) y condición de
    pago en un rango de fecha_documento. Insumo de la tabla Por proveedor de
    Cuentas por pagar (PMP de cada proveedor y su condición principal)."""
    return _query(f"""
        SELECT CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END AS categoria,
               {PROVEEDOR_EFECTIVO_LC} AS proveedor,
               lc.condicion_pago AS condicion_pago,
               SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total,
               COUNT(DISTINCT lc.doc_compra) AS pedidos
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.fecha_documento BETWEEN %s AND %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
              AND {PROVEEDOR_EFECTIVO_LC} != ''
        GROUP BY CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END,
                 {PROVEEDOR_EFECTIVO_LC}, lc.condicion_pago
    """, [fecha_inicio, fecha_fin])


def compras_por_material_proveedor_rango(fecha_inicio, fecha_fin):
    """Compras de materiales (con código) por categoría, material y proveedor
    en un rango de fecha_documento: valor total, valor bajo contrato, meses
    distintos con compra y pedidos. Insumo de "proveedor único" (materiales
    comprados a un solo proveedor) y de la oportunidad de contratos marco
    (compras recurrentes fuera de contrato)."""
    return _query(f"""
        SELECT CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END AS categoria,
               lc.material_codigo AS material_codigo, MAX(lc.texto_breve) AS texto_breve,
               {PROVEEDOR_EFECTIVO_LC} AS proveedor,
               SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total,
               SUM(CASE WHEN {FILTRO_BAJO_CONTRATO_LC} THEN {VALOR_COMPRA_EXPR_LC} ELSE 0 END) AS valor_contrato,
               COUNT(DISTINCT strftime('%%Y-%%m', lc.fecha_documento)) AS meses,
               COUNT(DISTINCT lc.doc_compra) AS pedidos
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.fecha_documento BETWEEN %s AND %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
              AND lc.material_codigo != '' AND {PROVEEDOR_EFECTIVO_LC} != ''
        GROUP BY CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END,
                 lc.material_codigo, {PROVEEDOR_EFECTIVO_LC}
    """, [fecha_inicio, fecha_fin])


def _concentracion_proveedores(df, top):
    """Cálculo de concentración de proveedores compartido por
    concentracion_proveedores_por_categoria (año) y su variante _rango (mes)."""
    resultado = []
    for categoria, grupo in df.groupby("categoria"):
        grupo = grupo.sort_values("valor_total", ascending=False)
        total = float(grupo["valor_total"].sum())
        top_n = grupo.head(top)
        pct_top = float(top_n["valor_total"].sum()) / total * 100 if total else 0
        resultado.append({
            "categoria": categoria,
            "valor_total": total,
            "pct_concentracion_top": round(pct_top, 1),
            "top_proveedores": [
                {
                    "proveedor": r["proveedor"], "valor": float(r["valor_total"]),
                    "pct": round(float(r["valor_total"]) / total * 100, 1) if total else 0,
                }
                for _, r in top_n.iterrows()
            ],
        })
    return sorted(resultado, key=lambda r: r["pct_concentracion_top"], reverse=True)


def concentracion_proveedores_por_categoria(anio, top=5):
    """% del valor de compras de cada categoría concentrado en sus `top`
    proveedores más grandes (indicador de riesgo de dependencia, Pareto de
    proveedores), con el detalle de esos proveedores. Definido por el usuario."""
    return _concentracion_proveedores(compras_por_categoria_y_proveedor(anio), top)


def concentracion_proveedores_por_categoria_rango(fecha_inicio, fecha_fin, top=5):
    """Igual que concentracion_proveedores_por_categoria, para un rango de
    fechas arbitrario (segmentador de Mes propio de esta tabla)."""
    return _concentracion_proveedores(compras_por_categoria_y_proveedor_rango(fecha_inicio, fecha_fin), top)


# Solo las líneas de compra de material/importación (Cl. ZNAC/ZIMP) tienen
# Ctd.pedido y Cantidad entrada comparables 1 a 1; en otras clases de documento
# (ZPM, ZOP: servicios/pedidos marco) Ctd.pedido no representa una cantidad
# física real (se vieron casos de Ctd.pedido=1 con Cantidad entrada=22).
# Definido por el usuario, verificado contra los datos.
_CLASES_DOCUMENTO_FILL_RATE = ("ZNAC", "ZIMP")
_IN_CLASES_DOCUMENTO_FILL_RATE = ",".join(f"'{c}'" for c in _CLASES_DOCUMENTO_FILL_RATE)


def compras_lineas_fill_rate(anio):
    """Líneas de compra de material/importación (Cl. ZNAC/ZIMP) con su cantidad
    pedida, cantidad recibida y valor. Insumo del Fill Rate."""
    return _query(f"""
        SELECT CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END AS categoria,
               lc.cantidad_pedido AS cantidad_pedido,
               lc.cantidad_entrada AS cantidad_entrada,
               {VALOR_COMPRA_EXPR_LC} AS valor_neto
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.anio_origen = %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
              AND lc.clase_documento IN ({_IN_CLASES_DOCUMENTO_FILL_RATE})
              AND lc.cantidad_pedido > 0
    """, [anio])


def compras_lineas_fill_rate_rango(fecha_inicio, fecha_fin):
    """Igual que compras_lineas_fill_rate, pero acotado por fecha_documento en
    vez de por año completo. Insumo del segmentador de Mes propio de Fill Rate."""
    return _query(f"""
        SELECT CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END AS categoria,
               lc.cantidad_pedido AS cantidad_pedido,
               lc.cantidad_entrada AS cantidad_entrada,
               {VALOR_COMPRA_EXPR_LC} AS valor_neto
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.fecha_documento BETWEEN %s AND %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
              AND lc.clase_documento IN ({_IN_CLASES_DOCUMENTO_FILL_RATE})
              AND lc.cantidad_pedido > 0
    """, [fecha_inicio, fecha_fin])


def _fill_rate(df, tolerancia):
    """Cálculo de Fill Rate compartido por fill_rate_por_categoria (año) y su
    variante _rango (mes)."""
    if df.empty:
        return []
    df = df.copy()
    df["cumplido"] = df["cantidad_entrada"] >= df["cantidad_pedido"] * (1 - tolerancia)
    resultado = []
    for categoria, grupo in df.groupby("categoria"):
        total_valor = float(grupo["valor_neto"].sum())
        valor_cumplido = float(grupo.loc[grupo["cumplido"], "valor_neto"].sum())
        resultado.append({
            "categoria": categoria,
            "lineas": int(len(grupo)),
            "valor_total": total_valor,
            "fill_rate_pct": round(valor_cumplido / total_valor * 100, 1) if total_valor else None,
        })
    return sorted(resultado, key=lambda r: (r["fill_rate_pct"] is None, r["fill_rate_pct"]))


def fill_rate_por_categoria(anio, tolerancia=0.05):
    """Fill Rate por categoría: % del valor de las líneas ZNAC/ZIMP entregadas
    completas (cantidad_entrada >= cantidad_pedido, con `tolerancia` de holgura)
    sobre el valor total de esas líneas."""
    return _fill_rate(compras_lineas_fill_rate(anio), tolerancia)


def fill_rate_por_categoria_rango(fecha_inicio, fecha_fin, tolerancia=0.05):
    """Igual que fill_rate_por_categoria, para un rango de fechas arbitrario
    (segmentador de Mes propio de esta tabla)."""
    return _fill_rate(compras_lineas_fill_rate_rango(fecha_inicio, fecha_fin), tolerancia)


# Clase de movimiento de MB51 que representa una Entrada de Mercancía real contra
# un pedido de compra ("101": EM para orden/imputación/entr.mercancías/stock en
# tránsito; "102" es su anulación). Verificado contra los datos: es la clase de
# movimiento correcta para medir cuándo llegó físicamente un pedido.
CLASE_MOVIMIENTO_ENTRADA_MERCANCIA = "101"

# En el cruce ME2N -> MB51 de Lead Time va "+mm.clase_movimiento" (no
# "mm.clase_movimiento"): sin el "+", SQLite arrancaba por TODAS las entradas
# 101 de MB51 (índice por clase) y buscaba el pedido de cada una (~74 s); con
# él parte de las líneas de compra del periodo y busca sus entradas por
# (pedido, posición) con idx_mov_pedido_posicion (~1 s). Mismo resultado.


def compras_lead_time_lineas(anio):
    """Lead time real (fecha de pedido -> primera Entrada de Mercancía) de cada
    línea de compra de material/importación (Cl. ZNAC/ZIMP, mismo criterio que
    Fill Rate). Cruza LineaCompra (ME2N) con MovimientoMaterial (MB51) por
    pedido/posición (doc_compra/pos_doc_compra = pedido/posición), usando la
    fecha real de Entrada de Mercancía (MB51, clase 101) en vez de la "Fe.entrega"
    de ME2N, que es solo la fecha comprometida al crear el pedido y no lo que pasó
    en la práctica. Definido por el usuario, verificado contra los datos."""
    return _query(f"""
        SELECT CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END AS categoria,
               lc.doc_compra, lc.pos_doc_compra, lc.fecha_documento AS fecha_pedido,
               MIN(mm.fecha_contabilizacion) AS fecha_recepcion
        FROM gestion_lineacompra lc
        JOIN gestion_movimientomaterial mm
             ON mm.pedido = lc.doc_compra AND mm.posicion = lc.pos_doc_compra
             AND +mm.clase_movimiento = '{CLASE_MOVIMIENTO_ENTRADA_MERCANCIA}'
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.anio_origen = %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
              AND lc.clase_documento IN ({_IN_CLASES_DOCUMENTO_FILL_RATE})
              AND lc.fecha_documento IS NOT NULL
        GROUP BY CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END,
                 lc.doc_compra, lc.pos_doc_compra, lc.fecha_documento
    """, [anio])


def compras_lead_time_lineas_rango(fecha_inicio, fecha_fin):
    """Igual que compras_lead_time_lineas, pero acotado por fecha_documento en
    vez de por año completo. Insumo del segmentador de Mes propio de Lead Time."""
    return _query(f"""
        SELECT CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END AS categoria,
               lc.doc_compra, lc.pos_doc_compra, lc.fecha_documento AS fecha_pedido,
               MIN(mm.fecha_contabilizacion) AS fecha_recepcion
        FROM gestion_lineacompra lc
        JOIN gestion_movimientomaterial mm
             ON mm.pedido = lc.doc_compra AND mm.posicion = lc.pos_doc_compra
             AND +mm.clase_movimiento = '{CLASE_MOVIMIENTO_ENTRADA_MERCANCIA}'
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.fecha_documento BETWEEN %s AND %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
              AND lc.clase_documento IN ({_IN_CLASES_DOCUMENTO_FILL_RATE})
        GROUP BY CASE {CATEGORIA_EFECTIVA_LC}
                   {_CASE_CONSOLIDACION_CATEGORIAS}
                   ELSE {CATEGORIA_EFECTIVA_LC}
               END,
                 lc.doc_compra, lc.pos_doc_compra, lc.fecha_documento
    """, [fecha_inicio, fecha_fin])


@_cacheado
def otif_lineas(fecha_inicio, fecha_fin, categorias, dias_tolerancia):
    """Líneas de pedido de material/importación (Cl. ZNAC/ZIMP) de las
    `categorias` con fecha de entrega prometida entre `fecha_inicio` y
    `fecha_fin`, con lo recibido (MB51 101 menos 102, en la unidad del
    pedido: cantidad_um_entrada) hasta la fecha prometida + `dias_tolerancia`
    y en total, y la primera y última entrada. Insumo del OTIF (ver
    gestion/ejecutivo.otif). Definido por el usuario el 2026-10-04: solo
    Repuestos y Suministros Industriales, 5 días de tolerancia."""
    marcas = ",".join(["%s"] * len(categorias))
    cantidad = ("CASE WHEN mm.clase_movimiento = '102' THEN -ABS(mm.cantidad_um_entrada) "
                "ELSE ABS(mm.cantidad_um_entrada) END")
    df = _query(f"""
        SELECT {CATEGORIA_COMPRA_CONSOLIDADA_LC} AS categoria,
               lc.doc_compra AS pedido, lc.pos_doc_compra AS posicion, lc.material_codigo AS material_codigo,
               lc.texto_breve AS texto_breve, {PROVEEDOR_EFECTIVO_LC} AS proveedor,
               lc.fecha_entrega AS fecha_entrega, lc.cantidad_pedido AS cantidad_pedido,
               {VALOR_COMPRA_EXPR_LC} AS valor,
               SUM(CASE WHEN mm.fecha_contabilizacion <= date(lc.fecha_entrega, '+' || %s || ' days')
                        THEN {cantidad} ELSE 0 END) AS recibido_a_tiempo,
               SUM(CASE WHEN mm.pedido IS NOT NULL THEN {cantidad} ELSE 0 END) AS recibido_total,
               MIN(CASE WHEN mm.clase_movimiento = '101' THEN mm.fecha_contabilizacion END) AS primera_entrada,
               MAX(CASE WHEN mm.clase_movimiento = '101' THEN mm.fecha_contabilizacion END) AS ultima_entrada
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_movimientomaterial mm
             ON mm.pedido = lc.doc_compra AND mm.posicion = lc.pos_doc_compra
             AND +mm.clase_movimiento IN ('101', '102')
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.fecha_entrega BETWEEN %s AND %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
              AND lc.clase_documento IN ({_IN_CLASES_DOCUMENTO_FILL_RATE})
              AND lc.cantidad_pedido > 0
              AND {CATEGORIA_COMPRA_CONSOLIDADA_LC} IN ({marcas})
        GROUP BY lc.doc_compra, lc.pos_doc_compra
    """, [int(dias_tolerancia), fecha_inicio, fecha_fin, *categorias])
    for columna in ("cantidad_pedido", "valor", "recibido_a_tiempo", "recibido_total"):
        df[columna] = df[columna].astype(float).fillna(0.0)
    for columna in ("fecha_entrega", "primera_entrada", "ultima_entrada"):
        df[columna] = pd.to_datetime(df[columna]).dt.date
    return df


def _lead_time(df):
    """Cálculo de Lead Time compartido por lead_time_por_categoria (año) y su
    variante _rango (mes). Se descartan los pocos casos (~0.25% de las líneas)
    con lead time negativo: contabilización de la Entrada de Mercancía anterior
    a la fecha del pedido, inconsistencia de datos y no un lead time real."""
    if df.empty:
        return []
    df = df.copy()
    df["fecha_pedido"] = pd.to_datetime(df["fecha_pedido"])
    df["fecha_recepcion"] = pd.to_datetime(df["fecha_recepcion"])
    df["lead_dias"] = (df["fecha_recepcion"] - df["fecha_pedido"]).dt.days
    df = df[df["lead_dias"] >= 0]
    if df.empty:
        return []
    resultado = []
    for categoria, grupo in df.groupby("categoria"):
        resultado.append({
            "categoria": categoria,
            "lineas": int(len(grupo)),
            "lead_time_promedio": round(float(grupo["lead_dias"].mean()), 1),
            "lead_time_mediana": round(float(grupo["lead_dias"].median()), 1),
        })
    return sorted(resultado, key=lambda r: r["lead_time_promedio"], reverse=True)


def lead_time_por_categoria(anio):
    """Lead time promedio y mediano (en días) por categoría. Ver
    compras_lead_time_lineas."""
    return _lead_time(compras_lead_time_lineas(anio))


def lead_time_por_categoria_rango(fecha_inicio, fecha_fin):
    """Igual que lead_time_por_categoria, para un rango de fechas arbitrario
    (segmentador de Mes propio de esta tabla)."""
    return _lead_time(compras_lead_time_lineas_rango(fecha_inicio, fecha_fin))
