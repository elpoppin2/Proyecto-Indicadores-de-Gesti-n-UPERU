"""Consultas de agregación para los dashboards. Usan SQL directo sobre la misma
conexión de Django (más eficiente que el ORM para agregaciones sobre tablas de
millones de filas) y devuelven DataFrames de pandas listos para graficar."""

import pandas as pd
from django.db import connection


def _query(sql, params=None):
    return pd.read_sql_query(sql, connection, params=params or [])


def fechas_inventario_disponibles():
    df = _query("SELECT DISTINCT fecha FROM gestion_inventariomensual ORDER BY fecha")
    return df["fecha"].tolist()


def valor_inventario_mensual():
    return _query("""
        SELECT fecha, SUM(valor_final) AS valor_total, SUM(stock_cierre) AS stock_total
        FROM gestion_inventariomensual
        GROUP BY fecha
        ORDER BY fecha
    """)


def valor_inventario_por_grupo(fecha, top=15):
    return _query("""
        SELECT denominacion_grupo_articulos AS grupo, SUM(valor_final) AS valor_total
        FROM gestion_inventariomensual
        WHERE fecha = %s
        GROUP BY denominacion_grupo_articulos
        ORDER BY valor_total DESC
        LIMIT %s
    """, [fecha, top])


def valor_inventario_por_familia(fecha, top=15):
    return _query("""
        SELECT COALESCE(c.familia, 'Sin categoría') AS familia, SUM(i.valor_final) AS valor_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE i.fecha = %s
        GROUP BY familia
        ORDER BY valor_total DESC
        LIMIT %s
    """, [fecha, top])


def top_materiales_inventario(fecha, top=20):
    return _query("""
        SELECT material_codigo, texto_breve, SUM(stock_cierre) AS stock, SUM(valor_final) AS valor_total
        FROM gestion_inventariomensual
        WHERE fecha = %s
        GROUP BY material_codigo, texto_breve
        ORDER BY valor_total DESC
        LIMIT %s
    """, [fecha, top])


def resumen_inventario(fecha):
    df = _query("""
        SELECT COUNT(DISTINCT material_codigo) AS materiales, SUM(valor_final) AS valor_total,
               SUM(stock_cierre) AS stock_total
        FROM gestion_inventariomensual
        WHERE fecha = %s
    """, [fecha])
    return df.iloc[0]


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
               SUM(valor_neto) AS valor_total, COUNT(*) AS lineas
        FROM gestion_lineacompra
        WHERE anio_origen = %s AND fecha_documento IS NOT NULL
        GROUP BY mes
        ORDER BY mes
    """, [anio])


def top_proveedores(anio, top=15):
    # "cuenta_proveedor" trae el nombre del proveedor en este dataset (el campo
    # "Proveedor" del SAP origen solo trae el código numérico).
    return _query("""
        SELECT COALESCE(NULLIF(cuenta_proveedor, ''), proveedor) AS proveedor,
               SUM(valor_neto) AS valor_total, COUNT(*) AS lineas
        FROM gestion_lineacompra
        WHERE anio_origen = %s AND (proveedor != '' OR cuenta_proveedor != '')
        GROUP BY proveedor
        ORDER BY valor_total DESC
        LIMIT %s
    """, [anio, top])


def top_materiales_comprados(anio, top=20):
    return _query("""
        SELECT material_codigo, texto_breve, SUM(cantidad_pedido) AS cantidad,
               SUM(valor_neto) AS valor_total
        FROM gestion_lineacompra
        WHERE anio_origen = %s AND material_codigo != ''
        GROUP BY material_codigo, texto_breve
        ORDER BY valor_total DESC
        LIMIT %s
    """, [anio, top])


def cumplimiento_entrega(anio):
    """% de líneas cuya fecha de entrega real llegó a tiempo respecto a la estimada."""
    df = _query("""
        SELECT
            SUM(CASE WHEN fecha_entrega <= fecha_entrega_estimada THEN 1 ELSE 0 END) AS a_tiempo,
            SUM(CASE WHEN fecha_entrega > fecha_entrega_estimada THEN 1 ELSE 0 END) AS tarde,
            COUNT(*) AS total
        FROM gestion_lineacompra
        WHERE anio_origen = %s AND fecha_entrega IS NOT NULL AND fecha_entrega_estimada IS NOT NULL
    """, [anio])
    return df.iloc[0]


def pendientes_por_entregar(anio, top=20):
    return _query("""
        SELECT COALESCE(NULLIF(cuenta_proveedor, ''), proveedor) AS proveedor,
               material_codigo, texto_breve, doc_compra,
               cantidad_por_entregar, valor_por_entregar
        FROM gestion_lineacompra
        WHERE anio_origen = %s AND valor_por_entregar > 0
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
