"""Filas de origen del detalle fila por fila de cada sección (líneas de compra, movimientos, entradas, tickets)."""

import pandas as pd

from .base import _limites_mes, _query_directo, _texto_sql
from .compras import CATEGORIA_COMPRA_CONSOLIDADA_LC, CLASE_MOVIMIENTO_ENTRADA_MERCANCIA, _IN_CLASES_DOCUMENTO_FILL_RATE
from .importados import _CLASES_ENTRADA_COMPRA
from .inventario import FILTRO_CLASE_MOVIMIENTO_CONSUMO, FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS
from .reglas import CATEGORIA_MATERIAL_EFECTIVA, FILTRO_BAJO_CONTRATO_LC, FILTRO_CATEGORIA_VALIDA_LC, FILTRO_COMPRAS_VALIDAS_LC, PROVEEDOR_EFECTIVO_LC, VALOR_COMPRA_EXPR_LC
from .sop_base import FECHA_INGRESO_EFECTIVA, _IN_TIPOS_OPERACION_RECEPCION


def lineas_compra_detalle(fecha_inicio, fecha_fin, categoria=None):
    """Líneas de pedido (ME2N) con Fecha doc. en el rango, con los mismos
    filtros que compras_por_categoria_contrato_rango (Compras, Pareto, PMP,
    Fill Rate). `valor` = Tp.cambio × Valor neto (S/); `bajo_contrato` = 1 si
    cumple FILTRO_BAJO_CONTRATO_LC."""
    filtro_categoria, params = "", [fecha_inicio, fecha_fin]
    if categoria is not None:
        filtro_categoria = f" AND {CATEGORIA_COMPRA_CONSOLIDADA_LC} = %s"
        params.append(categoria)
    df = _query_directo(f"""
        SELECT {CATEGORIA_COMPRA_CONSOLIDADA_LC} AS categoria,
               lc.fecha_documento AS fecha_documento, lc.doc_compra AS pedido, lc.pos_doc_compra AS posicion,
               lc.clase_documento AS clase_documento, lc.material_codigo AS material_codigo,
               lc.texto_breve AS texto_breve, {PROVEEDOR_EFECTIVO_LC} AS proveedor,
               lc.grupo_compras AS grupo_compras, lc.centro AS centro, lc.cantidad_pedido AS cantidad_pedido,
               lc.unidad_medida_pedido AS unidad, lc.precio_neto AS precio_neto, lc.moneda AS moneda,
               lc.tipo_cambio AS tipo_cambio, lc.valor_neto AS valor_neto, {VALOR_COMPRA_EXPR_LC} AS valor,
               lc.contrato AS contrato,
               CASE WHEN {FILTRO_BAJO_CONTRATO_LC} THEN 1 ELSE 0 END AS bajo_contrato,
               lc.condicion_pago AS condicion_pago, lc.fecha_entrega AS fecha_entrega,
               lc.cantidad_entrada AS cantidad_entrada, lc.solicitante AS solicitante
        FROM gestion_lineacompra lc
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.fecha_documento BETWEEN %s AND %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}{filtro_categoria}
    """, params)
    for columna in ("cantidad_pedido", "precio_neto", "tipo_cambio", "valor_neto", "valor", "cantidad_entrada"):
        df[columna] = pd.to_numeric(df[columna], errors="coerce")
    for columna in ("fecha_documento", "fecha_entrega"):
        df[columna] = pd.to_datetime(df[columna], errors="coerce").dt.date
    df["valor"] = df["valor"].fillna(0.0)
    return df


def lead_time_lineas_detalle(fecha_inicio, fecha_fin, categoria):
    """Líneas ZNAC/ZIMP de `categoria` con Fecha doc. en el rango y su primera
    Entrada de Mercancía (MB51 101): las filas de compras_lead_time_lineas_rango,
    con material, proveedor y valor."""
    return _query_directo(f"""
        SELECT lc.doc_compra AS pedido, lc.pos_doc_compra AS posicion, lc.clase_documento AS clase_documento,
               lc.material_codigo AS material_codigo, MAX(lc.texto_breve) AS texto_breve,
               MAX({PROVEEDOR_EFECTIVO_LC}) AS proveedor, lc.fecha_documento AS fecha_pedido,
               MAX(lc.fecha_entrega) AS fecha_entrega, MAX({VALOR_COMPRA_EXPR_LC}) AS valor,
               MIN(mm.fecha_contabilizacion) AS fecha_recepcion, COUNT(*) AS entradas
        FROM gestion_lineacompra lc
        JOIN gestion_movimientomaterial mm
             ON mm.pedido = lc.doc_compra AND mm.posicion = lc.pos_doc_compra
             AND +mm.clase_movimiento = '{CLASE_MOVIMIENTO_ENTRADA_MERCANCIA}'
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = lc.grupo_articulo
        LEFT JOIN gestion_categoriaservicio cs ON cs.grupo_compras = lc.grupo_compras
        WHERE lc.fecha_documento BETWEEN %s AND %s AND {FILTRO_COMPRAS_VALIDAS_LC}
              AND {FILTRO_CATEGORIA_VALIDA_LC}
              AND lc.clase_documento IN ({_IN_CLASES_DOCUMENTO_FILL_RATE})
              AND {CATEGORIA_COMPRA_CONSOLIDADA_LC} = %s
        GROUP BY lc.doc_compra, lc.pos_doc_compra, lc.fecha_documento
    """, [fecha_inicio, fecha_fin, categoria])


def movimientos_consumo_detalle(fecha_inicio, fecha_fin, categoria):
    """Movimientos de consumo de MB51 (FILTRO_CLASE_MOVIMIENTO_CONSUMO, sin
    los lotes USADO de Repuestos) entre las fechas, de los materiales de
    `categoria` (categoría del maestro, la misma de Rotación): las filas de
    consumo_valorizado_ventana_por_material. `consumo` e `importe` con el
    signo del consumo (positivo = salida de almacén). Los filtros de consumo
    van sin alias: clase_movimiento, lote y material_codigo solo existen en
    MB51 (mm), no en el maestro (m) ni en categorías (c)."""
    return _query_directo(f"""
        SELECT mm.fecha_contabilizacion AS fecha, mm.documento_material AS documento,
               mm.posicion_documento_material AS posicion, mm.clase_movimiento AS clase_movimiento,
               mm.texto_clase_movimiento AS texto_clase, mm.material_codigo AS material_codigo,
               mm.descripcion_material AS texto_breve, mm.centro AS centro_movimiento, mm.almacen AS almacen,
               -mm.cantidad AS consumo, mm.unidad_medida_entrada AS unidad,
               -mm.importe_moneda_local AS importe, mm.orden AS orden, mm.centro_coste AS centro_coste,
               mm.lote AS lote, mm.usuario AS usuario
        FROM gestion_movimientomaterial mm
        JOIN gestion_material m ON m.codigo = mm.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE {FILTRO_CLASE_MOVIMIENTO_CONSUMO}
              AND mm.fecha_contabilizacion BETWEEN %s AND %s
              AND mm.material_codigo != ''
              AND {FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS}
              AND {CATEGORIA_MATERIAL_EFECTIVA} = %s
    """, [fecha_inicio, fecha_fin, categoria])


def ultimo_movimiento_por_material(materiales):
    """Último consumo (mismas clases y lotes que el resto de indicadores) y
    última Entrada de Mercancía (101) de cada material de `materiales`, en
    toda la historia cargada de MB51. Para Materiales sin Movimiento."""
    if not materiales:
        return pd.DataFrame(columns=["material_codigo", "ultimo_consumo", "ultima_entrada"])
    lista = ",".join(_texto_sql(m) for m in materiales)
    return _query_directo(f"""
        SELECT material_codigo AS material_codigo,
               MAX(CASE WHEN {FILTRO_CLASE_MOVIMIENTO_CONSUMO} AND {FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS}
                        THEN fecha_contabilizacion END) AS ultimo_consumo,
               MAX(CASE WHEN clase_movimiento = '101' THEN fecha_contabilizacion END) AS ultima_entrada
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({lista})
        GROUP BY material_codigo
    """)


def entradas_compra_detalle(materiales, fecha_inicio, fecha_fin):
    """Entradas de mercancía de compra (MB51 101/102 con proveedor) de
    `materiales` entre las fechas: las filas de precios_entrada_materiales
    (precio del mes = importe ÷ cantidad)."""
    if not materiales:
        return pd.DataFrame()
    lista = ",".join(_texto_sql(m) for m in materiales)
    return _query_directo(f"""
        SELECT fecha_contabilizacion AS fecha, documento_material AS documento,
               posicion_documento_material AS posicion, clase_movimiento AS clase_movimiento,
               material_codigo AS material_codigo, descripcion_material AS texto_breve, centro AS centro,
               proveedor AS proveedor, pedido AS pedido, posicion AS posicion_pedido,
               cantidad AS cantidad, unidad_medida_entrada AS unidad, importe_moneda_local AS importe
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({lista})
              AND +clase_movimiento IN {_CLASES_ENTRADA_COMPRA} AND proveedor <> ''
              AND fecha_contabilizacion BETWEEN %s AND %s
    """, [fecha_inicio, fecha_fin])


def tickets_ingreso_detalle(mes):
    """Tickets de báscula de Recepción (ZMM056, Tipo 1/5) del mes: las filas
    de ingresos_fisicos_por_material."""
    fecha_inicio, fecha_fin = _limites_mes(mes)
    return _query_directo(f"""
        SELECT fecha_ingreso AS fecha, ticket_registro AS ticket, centro AS centro,
               material_codigo AS material_codigo, descripcion_material AS texto_breve,
               proveedor_codigo AS proveedor_codigo, proveedor AS proveedor, tipo_operacion AS tipo_operacion,
               cantidad AS toneladas, documento_material AS documento
        FROM gestion_ingresofisicosop
        WHERE {FECHA_INGRESO_EFECTIVA} BETWEEN %s AND %s
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION})
    """, [fecha_inicio, fecha_fin])
