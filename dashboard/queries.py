"""Consultas de agregación para los dashboards. Usan SQL directo sobre la misma
conexión de Django (más eficiente que el ORM para agregaciones sobre tablas de
millones de filas) y devuelven DataFrames de pandas listos para graficar."""

import re

import pandas as pd
from django.db import connection


def _query(sql, params=None):
    return pd.read_sql_query(sql, connection, params=params or [])


# Regla de negocio para el apartado de Compras: excluir grupos de compra que no
# son relevantes para los indicadores (GCp), descartar líneas marcadas como "L"
# en el campo B, excluir el proveedor 4000002 y las líneas sin proveedor.
# Definido por el usuario, no se deduce de los datos.
FILTRO_COMPRAS_VALIDAS = (
    "grupo_compras NOT IN ('G67','G60','G59','G52','G29','G20','G14','G15')"
    " AND campo_b != 'L'"
    " AND proveedor != '4000002'"
    " AND NOT (proveedor = '' AND cuenta_proveedor = '')"
)

# Misma regla, calificada con el alias "lc" para consultas con JOIN donde
# "grupo_compras" existe en más de una tabla (p.ej. gestion_categoriaservicio).
# G02 ya no está aquí: se excluye por categoría (ENERGÍA ELÉCTRICA, ver
# FILTRO_CATEGORIA_VALIDA_LC) en vez de por código de grupo de compras.
FILTRO_COMPRAS_VALIDAS_LC = (
    "lc.grupo_compras NOT IN ('G67','G60','G59','G52','G29','G20','G14','G15')"
    " AND lc.campo_b != 'L'"
    " AND lc.proveedor != '4000002'"
    " AND NOT (lc.proveedor = '' AND lc.cuenta_proveedor = '')"
)

# "Valor de Compras" = Tp. cambio * Valor neto (convierte a soles las líneas en USD/EUR).
# Definido por el usuario. Se usa COALESCE a 1 porque las pocas líneas sin tipo de
# cambio (9 en total) tampoco tienen moneda, es decir, ya están en soles.
VALOR_COMPRA_EXPR = "COALESCE(tipo_cambio, 1) * valor_neto"
VALOR_COMPRA_EXPR_LC = "COALESCE(lc.tipo_cambio, 1) * lc.valor_neto"

# Categoría efectiva de una línea de compra: la del grupo de artículos (CATEGORÍAS
# OFICIAL) o, si no tiene, la Categoría de Servicio de su grupo de compras (GCp).
# Requiere JOIN con gestion_categoria (alias c) y gestion_categoriaservicio (alias cs).
CATEGORIA_EFECTIVA_LC = "COALESCE(NULLIF(c.categoria, ''), NULLIF(cs.categoria_servicio, ''))"

# El modelo de "Valor de Compras" excluye líneas sin categoría resuelta (en blanco o
# sin match) y las categorías NO APLICA / ENERGÍA ELÉCTRICA. Definido por el usuario.
CATEGORIAS_EXCLUIDAS_MODELO = ("NO APLICA", "ENERGÍA ELÉCTRICA")
_IN_CATEGORIAS_EXCLUIDAS = ",".join(f"'{c}'" for c in CATEGORIAS_EXCLUIDAS_MODELO)
FILTRO_CATEGORIA_VALIDA_LC = (
    f"{CATEGORIA_EFECTIVA_LC} IS NOT NULL"
    f" AND {CATEGORIA_EFECTIVA_LC} NOT IN ({_IN_CATEGORIAS_EXCLUIDAS})"
)

# Una línea de compra se considera "bajo contrato" si su Doc. Compra cae en alguno de
# los dos rangos de numeración de SAP reservados para pedidos bajo contrato, o si
# referencia un número de Contrato (columna "Contr.", normalmente empieza con "46").
# Definido por el usuario.
FILTRO_BAJO_CONTRATO_LC = (
    "("
    "(lc.doc_compra != '' AND CAST(lc.doc_compra AS INTEGER) BETWEEN 4700000001 AND 4799999999)"
    " OR lc.contrato != ''"
    " OR (lc.doc_compra != '' AND CAST(lc.doc_compra AS INTEGER) BETWEEN 4000000001 AND 4099999999)"
    ")"
)

# PMP (Plazo Medio de Pago): réplica exacta de la medida DAX "Monto Calculado PMP" de
# Power BI. Cada condición de pago (columna "CPag") tiene un número de días ponderado;
# el PMP final es el promedio de esos días ponderado por el Valor de Compra de cada
# línea: PMP = SUM(dias_ponderados * Valor de Compra) / SUM(Valor de Compra).
# Códigos "A..": peso (% del monto) * días. Códigos "C.."/"T../vacío: 0 (contado).
# Códigos "F..", "L..", "N..": los días son el número que trae el propio código
# (p.ej. F045 -> 45, N030 -> 30); no se listan todos, se extraen con regex.
PMP_DIAS_CONDICION_A = {
    "A002": 0.9 * 15, "A003": 0.9 * 30,
    "A005": 0.85 * 15, "A006": 0.85 * 30,
    "A008": 0.8 * 15, "A009": 0.8 * 30,
    "A011": 0.75 * 15, "A012": 0.75 * 30,
    "A014": 0.7 * 15, "A015": 0.7 * 30,
    "A017": 0.65 * 15, "A018": 0.65 * 30,
    "A020": 0.6 * 15, "A021": 0.6 * 30,
    "A023": 0.55 * 15, "A024": 0.55 * 30,
    "A026": 0.5 * 15, "A027": 0.5 * 30, "A028": 0.5 * 7,
    "A030": 0.45 * 15, "A031": 0.45 * 30,
    "A033": 0.4 * 15, "A034": 0.4 * 30,
    "A036": 0.35 * 15, "A037": 0.35 * 30,
    "A039": 0.3 * 15, "A040": 0.3 * 30,
    "A042": 0.25 * 15, "A043": 0.25 * 30,
    "A045": 0.2 * 15, "A046": 0.2 * 30,
    "A048": 0.15 * 15, "A049": 0.15 * 30,
    "A051": 0.1 * 15, "A052": 0.1 * 30,
    "A053": 0.9 * 45, "A054": 0.9 * 60,
    "A055": 0.85 * 45, "A056": 0.85 * 60,
    "A057": 0.8 * 45, "A058": 0.8 * 60,
    "A059": 0.75 * 45, "A060": 0.75 * 60,
    "A061": 0.7 * 45, "A062": 0.7 * 60,
    "A063": 0.65 * 45, "A064": 0.65 * 60,
    "A065": 0.6 * 45, "A066": 0.6 * 60,
    "A067": 0.55 * 45, "A068": 0.55 * 60,
    "A069": 0.5 * 45, "A070": 0.5 * 60,
    "A071": 0.45 * 45, "A072": 0.45 * 60,
    "A073": 0.4 * 45, "A074": 0.4 * 60,
    "A075": 0.35 * 45, "A076": 0.35 * 60,
    "A077": 0.3 * 45, "A078": 0.3 * 60,
    "A079": 0.25 * 45, "A080": 0.25 * 60,
    "A081": 0.2 * 45, "A082": 0.2 * 60,
    "A083": 0.15 * 45, "A084": 0.15 * 60,
    "A085": 0.1 * 45, "A086": 0.1 * 60,
}


def dias_ponderados_condicion_pago(codigo):
    codigo = (codigo or "").strip().upper()
    if codigo in PMP_DIAS_CONDICION_A:
        return PMP_DIAS_CONDICION_A[codigo]
    if codigo[:1] in ("F", "L", "N"):
        digitos = re.sub(r"\D", "", codigo)
        return float(digitos) if digitos else 0.0
    return 0.0  # "A.." sin ponderación, "C..", "T..", vacío o cualquier otro: 0


def compras_por_condicion_pago(anio):
    return _query(f"""
        SELECT lc.condicion_pago AS condicion_pago, SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.anio_origen = %s AND {FILTRO_COMPRAS_VALIDAS_LC} AND {FILTRO_CATEGORIA_VALIDA_LC}
        GROUP BY lc.condicion_pago
    """, [anio])


def calcular_pmp(anio):
    """PMP en días: promedio de días ponderados por condición de pago, ponderado
    a su vez por el Valor de Compra de cada condición (ver PMP_DIAS_CONDICION_A)."""
    df = compras_por_condicion_pago(anio)
    if df.empty:
        return 0.0
    dias = df["condicion_pago"].map(dias_ponderados_condicion_pago)
    valor_total = df["valor_total"].sum()
    if not valor_total:
        return 0.0
    return float((dias * df["valor_total"]).sum() / valor_total)


# Regla de negocio para Inventario: solo considerar materiales que tengan categoría
# asignada, relacionando Material.categoria_id (grupo de artículos del material) con
# Categoria.grupo_articulo (CATEGORÍAS OFICIAL). Los materiales sin esa relación (o sin
# registro en Material) se excluyen de todo el modelo. Definido por el usuario: excluye
# ~27% del valor de inventario, incluyendo materias primas núcleo (Clinker, Caliza,
# Cemento, Yeso, Puzolana) que nunca tuvieron categoría asignada — confirmado con el
# usuario antes de aplicarlo. Excepción explícita: 14-00000024 y 14-00000023 (Caliza)
# sí se consideran, pero solo en el centro U001 (área de valoración), no en sus otros
# centros (U002 / U011 respectivamente).
FILTRO_INVENTARIO_CON_CATEGORIA = (
    "(c.grupo_articulo IS NOT NULL"
    " OR (i.material_codigo = '14-00000024' AND i.area_valoracion = 'U001')"
    " OR (i.material_codigo = '14-00000023' AND i.area_valoracion = 'U001'))"
)


def fechas_inventario_disponibles():
    df = _query("SELECT DISTINCT fecha FROM gestion_inventariomensual ORDER BY fecha")
    return df["fecha"].tolist()


def valor_inventario_mensual():
    return _query(f"""
        SELECT i.fecha AS fecha, SUM(i.valor_final) AS valor_total, SUM(i.stock_cierre) AS stock_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE {FILTRO_INVENTARIO_CON_CATEGORIA}
        GROUP BY i.fecha
        ORDER BY i.fecha
    """)


def valor_inventario_por_grupo(fecha, top=15):
    return _query(f"""
        SELECT i.denominacion_grupo_articulos AS grupo, SUM(i.valor_final) AS valor_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE i.fecha = %s AND {FILTRO_INVENTARIO_CON_CATEGORIA}
        GROUP BY i.denominacion_grupo_articulos
        ORDER BY valor_total DESC
        LIMIT %s
    """, [fecha, top])


def valor_inventario_por_familia(fecha, top=15):
    # COALESCE cubre solo a los dos materiales de la excepción (14-00000024/14-00000023
    # en U001), que no tienen categoría y por lo tanto tampoco familia.
    return _query(f"""
        SELECT COALESCE(c.familia, 'Sin categoría (excepción)') AS familia, SUM(i.valor_final) AS valor_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE i.fecha = %s AND {FILTRO_INVENTARIO_CON_CATEGORIA}
        GROUP BY COALESCE(c.familia, 'Sin categoría (excepción)')
        ORDER BY valor_total DESC
        LIMIT %s
    """, [fecha, top])


def top_materiales_inventario(fecha, top=20):
    return _query(f"""
        SELECT i.material_codigo, i.texto_breve, SUM(i.stock_cierre) AS stock, SUM(i.valor_final) AS valor_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE i.fecha = %s AND {FILTRO_INVENTARIO_CON_CATEGORIA}
        GROUP BY i.material_codigo, i.texto_breve
        ORDER BY valor_total DESC
        LIMIT %s
    """, [fecha, top])


def resumen_inventario(fecha):
    df = _query(f"""
        SELECT COUNT(DISTINCT i.material_codigo) AS materiales, SUM(i.valor_final) AS valor_total,
               SUM(i.stock_cierre) AS stock_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE i.fecha = %s AND {FILTRO_INVENTARIO_CON_CATEGORIA}
    """, [fecha])
    return df.iloc[0]


def variacion_inventario_por_material(fecha_inicial, fecha_final):
    """Variación de Valor Final (MB5B) por código de material entre dos meses.
    fecha_final debe ser posterior a fecha_inicial. Respeta el mismo filtro de
    categoría que el resto del modelo de Inventario (FILTRO_INVENTARIO_CON_CATEGORIA)."""
    return _query(f"""
        WITH mes1 AS (
            SELECT i.material_codigo AS material_codigo, MAX(i.texto_breve) AS texto_breve,
                   SUM(i.valor_final) AS valor
            FROM gestion_inventariomensual i
            LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
            LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
            WHERE i.fecha = %s AND {FILTRO_INVENTARIO_CON_CATEGORIA}
            GROUP BY i.material_codigo
        ),
        mes2 AS (
            SELECT i.material_codigo AS material_codigo, MAX(i.texto_breve) AS texto_breve,
                   SUM(i.valor_final) AS valor
            FROM gestion_inventariomensual i
            LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
            LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
            WHERE i.fecha = %s AND {FILTRO_INVENTARIO_CON_CATEGORIA}
            GROUP BY i.material_codigo
        )
        SELECT
            mes1.material_codigo AS material_codigo,
            COALESCE(mes1.texto_breve, mes2.texto_breve) AS texto_breve,
            mes1.valor AS valor_mes_inicial,
            COALESCE(mes2.valor, 0) AS valor_mes_final,
            COALESCE(mes2.valor, 0) - mes1.valor AS variacion_valor,
            CASE WHEN mes1.valor != 0
                 THEN (COALESCE(mes2.valor, 0) - mes1.valor) * 100.0 / mes1.valor
                 ELSE NULL END AS variacion_pct
        FROM mes1
        LEFT JOIN mes2 ON mes2.material_codigo = mes1.material_codigo

        UNION ALL

        SELECT
            mes2.material_codigo AS material_codigo,
            mes2.texto_breve AS texto_breve,
            0 AS valor_mes_inicial,
            mes2.valor AS valor_mes_final,
            mes2.valor AS variacion_valor,
            NULL AS variacion_pct
        FROM mes2
        LEFT JOIN mes1 ON mes1.material_codigo = mes2.material_codigo
        WHERE mes1.material_codigo IS NULL

        ORDER BY variacion_valor DESC
    """, [fecha_inicial, fecha_final])


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
    # "cuenta_proveedor" trae el nombre del proveedor en este dataset (el campo
    # "Proveedor" del SAP origen solo trae el código numérico).
    return _query(f"""
        SELECT COALESCE(NULLIF(lc.cuenta_proveedor, ''), lc.proveedor) AS proveedor,
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


def top_materiales_comprados(anio, top=20):
    return _query(f"""
        SELECT lc.material_codigo, lc.texto_breve, SUM(lc.cantidad_pedido) AS cantidad,
               SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.anio_origen = %s AND lc.material_codigo != ''
              AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
        GROUP BY lc.material_codigo, lc.texto_breve
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


def resumen_compras_contrato(anio):
    """Valor de Compras (Tp.cambio * Valor neto) bajo contrato vs. el total del modelo."""
    df = _query(f"""
        SELECT
            SUM(CASE WHEN {FILTRO_BAJO_CONTRATO_LC} THEN {VALOR_COMPRA_EXPR_LC} ELSE 0 END) AS valor_bajo_contrato,
            SUM({VALOR_COMPRA_EXPR_LC}) AS valor_total
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.anio_origen = %s AND {FILTRO_COMPRAS_VALIDAS_LC} AND {FILTRO_CATEGORIA_VALIDA_LC}
    """, [anio])
    return df.iloc[0]


def cumplimiento_entrega(anio):
    """% de líneas cuya fecha de entrega real llegó a tiempo respecto a la estimada."""
    df = _query(f"""
        SELECT
            SUM(CASE WHEN fecha_entrega <= fecha_entrega_estimada THEN 1 ELSE 0 END) AS a_tiempo,
            SUM(CASE WHEN fecha_entrega > fecha_entrega_estimada THEN 1 ELSE 0 END) AS tarde,
            COUNT(*) AS total
        FROM gestion_lineacompra
        WHERE anio_origen = %s AND fecha_entrega IS NOT NULL AND fecha_entrega_estimada IS NOT NULL
              AND {FILTRO_COMPRAS_VALIDAS}
    """, [anio])
    return df.iloc[0]


def pendientes_por_entregar(anio, top=20):
    return _query(f"""
        SELECT COALESCE(NULLIF(cuenta_proveedor, ''), proveedor) AS proveedor,
               material_codigo, texto_breve, doc_compra,
               cantidad_por_entregar, valor_por_entregar
        FROM gestion_lineacompra
        WHERE anio_origen = %s AND valor_por_entregar > 0
              AND {FILTRO_COMPRAS_VALIDAS}
        ORDER BY valor_por_entregar DESC
        LIMIT %s
    """, [anio, top])


def resumen_materiales():
    df = _query("""
        SELECT
            (SELECT COUNT(*) FROM gestion_material) AS total_materiales,
            (SELECT COUNT(*) FROM gestion_material WHERE material_critico != '') AS criticos,
            (SELECT COUNT(*) FROM gestion_categoria) AS total_categorias
    """)
    return df.iloc[0]


def materiales_por_familia(top=20):
    return _query("""
        SELECT COALESCE(c.familia, 'Sin categoría') AS familia, COUNT(*) AS materiales
        FROM gestion_material m
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        GROUP BY familia
        ORDER BY materiales DESC
        LIMIT %s
    """, [top])


def materiales_por_fabricante(top=20):
    return _query("""
        SELECT fabricante, COUNT(*) AS materiales
        FROM gestion_material
        WHERE fabricante != ''
        GROUP BY fabricante
        ORDER BY materiales DESC
        LIMIT %s
    """, [top])
