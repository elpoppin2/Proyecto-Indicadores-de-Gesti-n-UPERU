"""Pestañas (zonas) de cada página: Planificación, Logística de Materias Primas (con su alcance de materiales) y categorías. Solo datos: las usan las páginas, el resumen por área y los comandos (calentar_cache, revisar_paginas)."""

from dashboard import queries as q


# Zonas (pestañas): clave, nombre, qué contiene, secciones en orden.
_ZONAS_PLANIFICACION = [
    ("indicadores", "Indicadores", "Resumen de todas las zonas", []),
    ("cumplimiento", "Cumplimiento", "Plan de abastecimiento vs ingresos",
     ["sec-plan-mensual", "sec-plan-detalle", "sec-cumplimiento-mensual", "sec-entregas"]),
    ("consumo", "Consumo", "Plan vs real y consumo por T", ["sec-consumo", "sec-consumo-especifico"]),
    ("inventario", "Inventario", "Cobertura y proyección S&OP", ["sec-cobertura", "sec-proyeccion"]),
    ("precios", "Precios e IPU", "Precios de entrada e inflación", ["sec-precios", "sec-inflacion", "sec-inflacion-mensual"]),
    ("calidad", "Calidad y energía", "Análisis, premios y costo por Gcal", ["sec-calidad", "sec-premios", "sec-energia"]),
    # Pedido del usuario (2026-10-21): cada área con su resumen e informe mensual.
    ("resumen", "Resumen ejecutivo", "Metas, mensajes y oportunidades del mes", ["sec-resumen-area"]),
    ("informe", "Informe mensual", "Para imprimir o guardar en PDF", ["sec-informe-area"]),
]

# Logística de Materias Primas (pedido por el usuario el 2026-10-16): los
# (material, centro) que muestra, sus zonas y los gráficos de su Indicadores.
# Lista definida por el usuario (2026-10-16). La Puzolana Ayacucho de
# Condorcocha no tiene plan S&OP: aparece con sus ingresos y sin meta.
ALCANCE_LOGISTICA = (
    ("14-00000023", "U001"),  # Caliza Pucará (Atocongo)
    ("07-00003137", "U001"),  # Caliza Terceros Roca Fuerte (Atocongo)
    ("14-00000024", "U001"),  # Caliza Roca Extraída (Atocongo)
    ("07-00003395", "U001"),  # Caliza Terceros HCIM (Atocongo)
    ("06-00000003", "U002"),  # Carbón Importado: traslado de Atocongo a Condorcocha
    ("07-00000025", "U001"),  # Sílices de Terceros (Atocongo)
    ("07-00000055", "U001"),  # Puzolana de Terceros (Atocongo)
    ("07-00000027", "U001"),  # Puzolana Ayacucho (Atocongo)
    ("07-00000027", "U002"),  # Puzolana Ayacucho (Condorcocha)
    ("14-00000037", "U002"),  # Clinker Tipo I ATO: traslado de Atocongo a Condorcocha
)
_ZONAS_LOGISTICA = [
    ("indicadores", "Indicadores", "Resumen de cumplimiento", []),
    ("cumplimiento", "Cumplimiento", "Plan de abastecimiento vs ingresos",
     ["sec-plan-mensual", "sec-plan-detalle", "sec-cumplimiento-mensual", "sec-entregas"]),
    ("transporte", "Transporte", "Tiempos, carga, transportistas y flota", ["sec-transporte"]),
    ("fletes", "Fletes", "Costo de transporte", []),
]
_PLANTILLAS_ZONA_LOGISTICA = {"fletes": "gestion/logistica/_fletes.html"}
_PANELES_LOGISTICA = ("planificacion", "logistica_transporte")


# ---------------------------------------------------------------------------
# Páginas de categoría (CAPEX, Combustibles, Repuestos...), organizadas en
# pestañas como Planificación. Pedido por el usuario: "que cada uno tenga las
# pestañas de indicadores, inventario, precio e IPU, cuenta por pagar y demás
# pestañas de acuerdo a cada categoría". La página trae solo Indicadores;
# cada otra pestaña se pide al abrirla (?_zona=inventario) y zonas.js las
# precarga en segundo plano. Al cambiar un filtro, base.html pide
# ?_seccion=sec-xxx: se devuelve la pestaña que la contiene y de ella toma
# solo esa sección.
# ---------------------------------------------------------------------------

# Pestañas: clave, nombre, descripción, secciones (también los bloques
# internos con filtro propio, p.ej. sec-inventario-top) y a qué categorías
# aplican: None = todas, "inventario" = solo las que tienen inventario (no
# CAPEX ni servicios), "sop" = solo las del S&OP (Materias Primas y
# Combustibles).
_ZONAS_CATEGORIA = [
    ("indicadores", "Indicadores", "Resumen de la categoría", ["sec-indicadores-categoria"], None),
    ("compras", "Compras", "Proveedores, Fill Rate y Lead Time",
     ["bloque-compras", "sec-compras", "sec-pareto", "sec-fillrate", "sec-otif", "sec-leadtime", "sec-unico"], None),
    ("inventario", "Inventario", "Stock, ABC, rotación y críticos",
     ["bloque-inventario", "sec-evolucion", "sec-inventario", "sec-abc", "sec-obsoletos", "sec-rotacion",
      "sec-criticos", "sec-variacion"], "inventario"),
    ("abastecimiento", "Abastecimiento S&OP", "Plan vs ingresos por centro",
     ["sec-sop-categoria", "sec-sop-mensual"], "sop"),
    ("precios", "Precios e IPU", "Precio de compra e inflación", ["sec-ipu-categoria", "sec-inflacion-categoria"], None),
    ("cuentas", "Cuentas por pagar", "Plazo de pago (PMP)",
     ["bloque-cuentas", "sec-pmp", "sec-plazos", "sec-dispersion-cxp", "sec-proveedores-cxp", "sec-deuda"], None),
    # Pedido del usuario (2026-10-21): cada área con su resumen e informe mensual.
    ("resumen", "Resumen ejecutivo", "Metas, mensajes y oportunidades del mes", ["sec-resumen-area"], None),
    ("informe", "Informe mensual", "Para imprimir o guardar en PDF", ["sec-informe-area"], None),
]


def _zonas_categoria(categoria, tiene_inventario):
    aplica = {None: True, "inventario": tiene_inventario, "sop": categoria in q.CATEGORIAS_PLANIFICACION_SOP}
    return [{"clave": c, "nombre": n, "descripcion": d, "secciones": " ".join(s)}
            for c, n, d, s, condicion in _ZONAS_CATEGORIA if aplica[condicion]]
