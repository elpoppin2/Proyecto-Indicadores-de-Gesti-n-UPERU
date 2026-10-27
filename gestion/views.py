"""Punto de entrada de las vistas para gestion/urls.py y los comandos de
management. El código vive en gestion/vistas/, un módulo por área (antes todo
estaba aquí, ~5,200 líneas; reordenado el 2026-10-21 a pedido del usuario):

- comun: formatos, navegación y segmentadores compartidos
- zonas: pestañas de cada página
- portada: Portada, Informe mensual y su versión por área
- overview: Overview
- planificacion (+ plan_cumplimiento, plan_consumo, plan_precios, plan_calidad,
  plan_transporte, plan_indicadores): Planificación y Logística de Materias Primas
- categoria (+ categoria_pestanas, categoria_indicadores): página de cada categoría
- detalle: detalle fila por fila
"""

from dashboard import queries as q

from .vistas.categoria import categoria_detalle, exportar_excel, rotacion_materiales_pagina
from .vistas.comun import _categorias_disponibles, _slug
from .vistas.detalle import desglose_filas
from .vistas.overview import overview
from .vistas.plan_calidad import _contexto_energia, exportar_energia, exportar_premios
from .vistas.plan_consumo import _contexto_consumo, _contexto_consumo_especifico
from .vistas.plan_cumplimiento import _contexto_entregas
from .vistas.planificacion import _html_indicadores, _html_indicadores_cuadrantes, asistente_planificacion, logistica_materias_primas, planificacion
from .vistas.portada import _area_ejecutiva, informe_mensual, inicio, portada
from .vistas.zonas import _ZONAS_LOGISTICA, _ZONAS_PLANIFICACION, _zonas_categoria

__all__ = [
    # Vistas de las URLs
    "inicio", "portada", "informe_mensual", "overview", "planificacion", "logistica_materias_primas",
    "asistente_planificacion", "exportar_premios", "exportar_energia", "categoria_detalle", "exportar_excel",
    "rotacion_materiales_pagina", "desglose_filas",
    # Usados por los comandos (calentar_cache, exportar_*_html, revisar_paginas, verificar_calculos)
    "q", "_categorias_disponibles", "_slug", "_zonas_categoria", "_ZONAS_PLANIFICACION", "_ZONAS_LOGISTICA",
    "_area_ejecutiva", "_html_indicadores", "_html_indicadores_cuadrantes", "_contexto_consumo", "_contexto_consumo_especifico",
    "_contexto_energia", "_contexto_entregas",
]
