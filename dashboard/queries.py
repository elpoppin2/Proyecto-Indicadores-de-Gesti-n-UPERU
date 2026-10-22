"""Consultas de agregación para los dashboards. Usan SQL directo sobre la misma
conexión de Django (más eficiente que el ORM para agregaciones sobre tablas de
millones de filas) y devuelven DataFrames de pandas listos para graficar.

El código vive en dashboard/consultas/, un módulo por tema (antes todo estaba
aquí, ~4,400 líneas; reordenado el 2026-10-21 a pedido del usuario):

- base: caché versionada, alcance de materiales, formatos y utilidades
- reglas: reglas de negocio en SQL compartidas (compras válidas, contrato, PMP...)
- inventario, compras, detalle (filas del detalle fila por fila)
- sop_base, sop_abastecimiento, sop_inventario, consumo_sop, calidad: Planificación S&OP
- importados (embarques, producción propia), precios (precio real, IPU)

Este archivo re-exporta todos sus nombres: el resto del código sigue usando
`from dashboard import queries as q` y `q.<nombre>`.
"""

from .consultas.base import (
    NOMBRES_CENTRO, _ALCANCE_MATERIALES, _ARCHIVO_VERSION, _DIR_CACHE, _HUELLA_CODIGO, _MAX_BYTES_MEMORIA,
    _MESES_ES, _MESES_ES_ABREV, _SIN_VALOR, _cache_obtener, _cacheado, _copia, _en_alcance, _limites_mes,
    _lista_materiales, _query, _query_cache, _query_cache_bytes, _query_cache_lock, _query_directo,
    _sumar_meses, _texto_sql, _version_datos, _version_leida, alcance_actual, alcance_materiales,
    formato_mes_anio, formato_mes_anio_abrev, formato_soles, marcar_datos_actualizados, nombre_centro,
    nombre_mes,
)
from .consultas.reglas import (
    CATEGORIAS_EXCLUIDAS_INVENTARIO, CATEGORIAS_EXCLUIDAS_MODELO, CATEGORIA_EFECTIVA_LC,
    CATEGORIA_INVENTARIO_EFECTIVA, CATEGORIA_MATERIAL_EFECTIVA, CLASES_LIQUIDACION_ORDENES,
    CONSOLIDACION_CATEGORIAS_INVENTARIO, FAMILIA_INVENTARIO_EFECTIVA, FAMILIA_MATERIAL_EFECTIVA,
    FILTRO_BAJO_CONTRATO_LC, FILTRO_CATEGORIA_INVENTARIO_VALIDA, FILTRO_CATEGORIA_VALIDA_LC,
    FILTRO_COMPRAS_VALIDAS_LC, FILTRO_INVENTARIO_CON_CATEGORIA, PMP_DIAS_CONDICION_A, PROVEEDOR_EFECTIVO_LC,
    VALOR_COMPRA_EXPR, VALOR_COMPRA_EXPR_LC, _CASE_CONSOLIDACION_INVENTARIO, _CATEGORIA_INVENTARIO_BASE,
    _CATEGORIA_MATERIAL_BASE, _IN_CATEGORIAS_EXCLUIDAS, _IN_CATEGORIAS_EXCLUIDAS_INVENTARIO, calcular_pmp,
    compras_por_condicion_pago, dias_ponderados_condicion_pago,
)
from .consultas.inventario import (
    CLASES_MOVIMIENTO_CONSUMO, FILTRO_CLASE_MOVIMIENTO_CONSUMO, FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS,
    MATERIALES_SIN_MOVIMIENTO_ANIOS, MESES_CONSUMO_COBERTURA, _CATEGORIAS_ORIGEN_REPUESTOS,
    _IN_CATEGORIAS_ORIGEN_REPUESTOS, _IN_CLASES_MOVIMIENTO_CONSUMO, _ventana_anios, _ventana_consumo,
    catalogo_materiales, categorias_inventario_disponibles, clasificacion_abc_por_categoria,
    cobertura_materiales_criticos_por_categoria, consumo_periodo_por_material,
    consumo_valorizado_mensual_por_categoria, consumo_valorizado_ventana_por_material,
    consumo_ventana_por_material, fechas_inventario_disponibles, inventario_por_material,
    materiales_obsoletos_por_categoria, materiales_por_fabricante, materiales_por_familia, resumen_materiales,
    rotacion_inventario_por_material, valor_inventario_mensual, valor_inventario_mensual_por_categoria,
    variacion_inventario_por_material,
)
from .consultas.compras import (
    CATEGORIA_COMPRA_CONSOLIDADA_LC, CLASE_MOVIMIENTO_ENTRADA_MERCANCIA, CONSOLIDACION_CATEGORIAS_COMPRA,
    _CASE_CONSOLIDACION_CATEGORIAS, _CLASES_DOCUMENTO_FILL_RATE, _IN_CLASES_DOCUMENTO_FILL_RATE,
    _concentracion_proveedores, _fill_rate, _lead_time, anios_compras_disponibles, calcular_pmp_rango,
    categorias_compras_disponibles, categorias_dashboard, compras_lead_time_lineas,
    compras_lead_time_lineas_rango, compras_lineas_fill_rate, compras_lineas_fill_rate_rango,
    compras_por_categoria, compras_por_categoria_contrato, compras_por_categoria_contrato_rango,
    compras_por_categoria_proveedor_condicion_rango, compras_por_categoria_y_condicion_pago,
    compras_por_categoria_y_condicion_pago_rango, compras_por_categoria_y_proveedor,
    compras_por_categoria_y_proveedor_rango, compras_por_material_proveedor_rango, compras_por_mes,
    compras_por_mes_y_condicion_pago, concentracion_proveedores_por_categoria,
    concentracion_proveedores_por_categoria_rango, fill_rate_por_categoria, fill_rate_por_categoria_rango,
    lead_time_por_categoria, lead_time_por_categoria_rango, otif_lineas, pmp_mensual, pmp_por_categoria,
    pmp_por_categoria_rango, top_proveedores,
)
from .consultas.detalle import (
    entradas_compra_detalle, lead_time_lineas_detalle, lineas_compra_detalle, movimientos_consumo_detalle,
    tickets_ingreso_detalle, ultimo_movimiento_por_material,
)
from .consultas.sop_base import (
    CATEGORIAS_PLANIFICACION_SOP, FECHA_INGRESO_EFECTIVA, INGRESOS_CON_OTRA_FECHA, PREFIJO_SIN_CODIGO_SOP,
    _FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP, _FILTRO_CON_CODIGO_SOP, _IN_TIPOS_OPERACION_RECEPCION,
    _MATERIALES_CONSUMO_VIA_OTRO, _TIPOS_OPERACION_RECEPCION, materiales_planificados_sop,
    pares_planificados_sop,
)
from .consultas.sop_abastecimiento import (
    CENTRO_ATOCONGO, CENTRO_CONDORCOCHA, MATERIALES_TRASLADO_ATOCONGO_CONDORCOCHA, MINUTOS_MAXIMO_EN_PLANTA,
    MINUTOS_TARA_REGISTRADA, _con_categoria_efectiva_planificacion, cumplimiento_mensual_abastecimiento,
    descripcion_centros_planificacion, entregas_por_proveedor_sop, evaluacion_diaria_abastecimiento,
    ingresos_fisicos_por_material, ingresos_fisicos_por_proveedor, meses_planificacion_disponibles,
    plan_abastecimiento_por_material, plan_vs_real_abastecimiento_por_material,
    resumen_abastecimiento_por_categoria, traslados_recibidos, viajes_recepcion,
)
from .consultas.sop_inventario import (
    DIAGNOSTICOS_MINMAX, ESTADOS_STOCK, LEAD_TIME_SOP, LEAD_TIME_SOP_DEFECTO, _COBERTURA_OCULTOS,
    _TRASLADOS_SOP, _UMBRAL_SOBRECUBIERTO, _a_codigo_vigente, _armar_cobertura, _con_anteriores,
    _estado_stock, _plan_sop_por_mes, _sumar_consumo_via_otro, cobertura_stock_planificacion,
    cobertura_stock_planificacion_por_mes, diagnostico_minmax_sop, fecha_inventario_inicial_sop,
    lead_time_sop, meses_inventario_disponibles, minmax_sop, proyeccion_inventario_sop,
    snapshots_sop_disponibles,
)
from .consultas.importados import (
    ACOPIO_IMPORTADOS, AJUSTES_CODIGOS_ANTERIORES, ANULACIONES_ERRONEAS, CENTRO_IMPORTACION,
    CLASES_AJUSTE_HUMEDAD, CLASES_LIQUIDACION_PRODUCCION, CODIGOS_ANTERIORES,
    CODIGOS_ANTERIORES_POR_PROVEEDOR, PRODUCCION_PROPIA, _CLASES_ENTRADA_COMPRA, _DIAS_ENTRE_BARCOS,
    _MINIMO_TRASLADO, _PEDIDO_EN_TEXTO, _con_codigos_anteriores, _embarque_de_partidas, _pedido_de_partida,
    _produccion_propia, _puesto_en_destino, _reenvios_acopio, _sql_material_vigente,
    _sql_sin_anulaciones_erroneas, _sql_traspasos_cambio_codigo, _traslados_desde_importacion,
    embarques_importados, liquidaciones_produccion_propia, materiales_importados, reparto_por_embarque,
)
from .consultas.precios import (
    AJUSTES_EXCLUIDOS, AJUSTES_POR_CONTRATO, PRECIOS_REFERENCIA_IPU, _RAZON_ATIPICA, _prorratear_por_contrato,
    _sql_ajuste_excluido, ajustes_fbl3n_detalle, ajustes_precio_fbl3n, bases_referencia_ipu, con_precio_real,
    concepto_ajuste, inflacion_canasta, inflacion_precios, ipu_por_mes, meses_con_fbl3n,
    precios_entrada_materiales, precios_entrada_materiales_real, precios_entrada_sop,
    precios_entrada_sop_real, resumen_precios_entrada,
)
from .consultas.calidad import (
    PARAMETROS_CALIDAD, _CAMPOS_CALIDAD, calidad_ingresos_sop, ingresos_con_energia, parametros_con_datos,
    premios_calidad_sop, promedio_calidad, resumen_calidad, resumen_energia, resumen_premios,
)
from .consultas.consumo_sop import (
    _MESES_CADENA_PRODUCCION, _PRODUCTOS_FINALES, _como_material_comprado, _producto_final,
    consumo_por_tonelada_sop, consumo_real_mensual_sop, destino_produccion_sop, plan_consumo_del_mes,
    plan_consumo_sop, produccion_mensual_planta,
)

__all__ = [
    'NOMBRES_CENTRO', '_ALCANCE_MATERIALES', '_ARCHIVO_VERSION', '_DIR_CACHE', '_HUELLA_CODIGO',
    '_MAX_BYTES_MEMORIA', '_MESES_ES', '_MESES_ES_ABREV', '_SIN_VALOR', '_cache_obtener', '_cacheado',
    '_copia', '_en_alcance', '_limites_mes', '_lista_materiales', '_query', '_query_cache',
    '_query_cache_bytes', '_query_cache_lock', '_query_directo', '_sumar_meses', '_texto_sql',
    '_version_datos', '_version_leida', 'alcance_actual', 'alcance_materiales', 'formato_mes_anio',
    'formato_mes_anio_abrev', 'formato_soles', 'marcar_datos_actualizados', 'nombre_centro', 'nombre_mes',
    'CATEGORIAS_EXCLUIDAS_INVENTARIO', 'CATEGORIAS_EXCLUIDAS_MODELO', 'CATEGORIA_EFECTIVA_LC',
    'CATEGORIA_INVENTARIO_EFECTIVA', 'CATEGORIA_MATERIAL_EFECTIVA', 'CLASES_LIQUIDACION_ORDENES',
    'CONSOLIDACION_CATEGORIAS_INVENTARIO', 'FAMILIA_INVENTARIO_EFECTIVA', 'FAMILIA_MATERIAL_EFECTIVA',
    'FILTRO_BAJO_CONTRATO_LC', 'FILTRO_CATEGORIA_INVENTARIO_VALIDA', 'FILTRO_CATEGORIA_VALIDA_LC',
    'FILTRO_COMPRAS_VALIDAS_LC', 'FILTRO_INVENTARIO_CON_CATEGORIA', 'PMP_DIAS_CONDICION_A',
    'PROVEEDOR_EFECTIVO_LC', 'VALOR_COMPRA_EXPR', 'VALOR_COMPRA_EXPR_LC', '_CASE_CONSOLIDACION_INVENTARIO',
    '_CATEGORIA_INVENTARIO_BASE', '_CATEGORIA_MATERIAL_BASE', '_IN_CATEGORIAS_EXCLUIDAS',
    '_IN_CATEGORIAS_EXCLUIDAS_INVENTARIO', 'calcular_pmp', 'compras_por_condicion_pago',
    'dias_ponderados_condicion_pago', 'CLASES_MOVIMIENTO_CONSUMO', 'FILTRO_CLASE_MOVIMIENTO_CONSUMO',
    'FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS', 'MATERIALES_SIN_MOVIMIENTO_ANIOS', 'MESES_CONSUMO_COBERTURA',
    '_CATEGORIAS_ORIGEN_REPUESTOS', '_IN_CATEGORIAS_ORIGEN_REPUESTOS', '_IN_CLASES_MOVIMIENTO_CONSUMO',
    '_ventana_anios', '_ventana_consumo', 'catalogo_materiales', 'categorias_inventario_disponibles',
    'clasificacion_abc_por_categoria', 'cobertura_materiales_criticos_por_categoria',
    'consumo_periodo_por_material', 'consumo_valorizado_mensual_por_categoria',
    'consumo_valorizado_ventana_por_material', 'consumo_ventana_por_material',
    'fechas_inventario_disponibles', 'inventario_por_material', 'materiales_obsoletos_por_categoria',
    'materiales_por_fabricante', 'materiales_por_familia', 'resumen_materiales',
    'rotacion_inventario_por_material', 'valor_inventario_mensual', 'valor_inventario_mensual_por_categoria',
    'variacion_inventario_por_material', 'CATEGORIA_COMPRA_CONSOLIDADA_LC',
    'CLASE_MOVIMIENTO_ENTRADA_MERCANCIA', 'CONSOLIDACION_CATEGORIAS_COMPRA', '_CASE_CONSOLIDACION_CATEGORIAS',
    '_CLASES_DOCUMENTO_FILL_RATE', '_IN_CLASES_DOCUMENTO_FILL_RATE', '_concentracion_proveedores',
    '_fill_rate', '_lead_time', 'anios_compras_disponibles', 'calcular_pmp_rango',
    'categorias_compras_disponibles', 'categorias_dashboard', 'compras_lead_time_lineas',
    'compras_lead_time_lineas_rango', 'compras_lineas_fill_rate', 'compras_lineas_fill_rate_rango',
    'compras_por_categoria', 'compras_por_categoria_contrato', 'compras_por_categoria_contrato_rango',
    'compras_por_categoria_proveedor_condicion_rango', 'compras_por_categoria_y_condicion_pago',
    'compras_por_categoria_y_condicion_pago_rango', 'compras_por_categoria_y_proveedor',
    'compras_por_categoria_y_proveedor_rango', 'compras_por_material_proveedor_rango', 'compras_por_mes',
    'compras_por_mes_y_condicion_pago', 'concentracion_proveedores_por_categoria',
    'concentracion_proveedores_por_categoria_rango', 'fill_rate_por_categoria',
    'fill_rate_por_categoria_rango', 'lead_time_por_categoria', 'lead_time_por_categoria_rango',
    'otif_lineas', 'pmp_mensual', 'pmp_por_categoria', 'pmp_por_categoria_rango', 'top_proveedores',
    'entradas_compra_detalle', 'lead_time_lineas_detalle', 'lineas_compra_detalle',
    'movimientos_consumo_detalle', 'tickets_ingreso_detalle', 'ultimo_movimiento_por_material',
    'CATEGORIAS_PLANIFICACION_SOP', 'FECHA_INGRESO_EFECTIVA', 'INGRESOS_CON_OTRA_FECHA',
    'PREFIJO_SIN_CODIGO_SOP', '_FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP', '_FILTRO_CON_CODIGO_SOP',
    '_IN_TIPOS_OPERACION_RECEPCION', '_MATERIALES_CONSUMO_VIA_OTRO', '_TIPOS_OPERACION_RECEPCION',
    'materiales_planificados_sop', 'pares_planificados_sop', 'CENTRO_ATOCONGO', 'CENTRO_CONDORCOCHA',
    'MATERIALES_TRASLADO_ATOCONGO_CONDORCOCHA', 'MINUTOS_MAXIMO_EN_PLANTA', 'MINUTOS_TARA_REGISTRADA',
    '_con_categoria_efectiva_planificacion', 'cumplimiento_mensual_abastecimiento',
    'descripcion_centros_planificacion', 'entregas_por_proveedor_sop', 'evaluacion_diaria_abastecimiento',
    'ingresos_fisicos_por_material', 'ingresos_fisicos_por_proveedor', 'meses_planificacion_disponibles',
    'plan_abastecimiento_por_material', 'plan_vs_real_abastecimiento_por_material',
    'resumen_abastecimiento_por_categoria', 'traslados_recibidos', 'viajes_recepcion', 'DIAGNOSTICOS_MINMAX',
    'ESTADOS_STOCK', 'LEAD_TIME_SOP', 'LEAD_TIME_SOP_DEFECTO', '_COBERTURA_OCULTOS', '_TRASLADOS_SOP',
    '_UMBRAL_SOBRECUBIERTO', '_a_codigo_vigente', '_armar_cobertura', '_con_anteriores', '_estado_stock',
    '_plan_sop_por_mes', '_sumar_consumo_via_otro', 'cobertura_stock_planificacion',
    'cobertura_stock_planificacion_por_mes', 'diagnostico_minmax_sop', 'fecha_inventario_inicial_sop',
    'lead_time_sop', 'meses_inventario_disponibles', 'minmax_sop', 'proyeccion_inventario_sop',
    'snapshots_sop_disponibles', 'ACOPIO_IMPORTADOS', 'AJUSTES_CODIGOS_ANTERIORES', 'ANULACIONES_ERRONEAS',
    'CENTRO_IMPORTACION', 'CLASES_AJUSTE_HUMEDAD', 'CLASES_LIQUIDACION_PRODUCCION', 'CODIGOS_ANTERIORES',
    'CODIGOS_ANTERIORES_POR_PROVEEDOR', 'PRODUCCION_PROPIA', '_CLASES_ENTRADA_COMPRA', '_DIAS_ENTRE_BARCOS',
    '_MINIMO_TRASLADO', '_PEDIDO_EN_TEXTO', '_con_codigos_anteriores', '_embarque_de_partidas',
    '_pedido_de_partida', '_produccion_propia', '_puesto_en_destino', '_reenvios_acopio',
    '_sql_material_vigente', '_sql_sin_anulaciones_erroneas', '_sql_traspasos_cambio_codigo',
    '_traslados_desde_importacion', 'embarques_importados', 'liquidaciones_produccion_propia',
    'materiales_importados', 'reparto_por_embarque', 'AJUSTES_EXCLUIDOS', 'AJUSTES_POR_CONTRATO',
    'PRECIOS_REFERENCIA_IPU', '_RAZON_ATIPICA', '_prorratear_por_contrato', '_sql_ajuste_excluido',
    'ajustes_fbl3n_detalle', 'ajustes_precio_fbl3n', 'bases_referencia_ipu', 'con_precio_real',
    'concepto_ajuste', 'inflacion_canasta', 'inflacion_precios', 'ipu_por_mes', 'meses_con_fbl3n',
    'precios_entrada_materiales', 'precios_entrada_materiales_real', 'precios_entrada_sop',
    'precios_entrada_sop_real', 'resumen_precios_entrada', 'PARAMETROS_CALIDAD', '_CAMPOS_CALIDAD',
    'calidad_ingresos_sop', 'ingresos_con_energia', 'parametros_con_datos', 'premios_calidad_sop',
    'promedio_calidad', 'resumen_calidad', 'resumen_energia', 'resumen_premios', '_MESES_CADENA_PRODUCCION',
    '_PRODUCTOS_FINALES', '_como_material_comprado', '_producto_final', 'consumo_por_tonelada_sop',
    'consumo_real_mensual_sop', 'destino_produccion_sop', 'plan_consumo_del_mes', 'plan_consumo_sop',
    'produccion_mensual_planta',
]
