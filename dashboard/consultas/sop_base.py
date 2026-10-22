"""Reglas comunes del S&OP: categorías y materiales planificados, tipos de operación de Recepción (ZMM056), fecha de ingreso efectiva y filtros de consumo."""

import datetime

from .base import _query
from .inventario import _IN_CLASES_MOVIMIENTO_CONSUMO


# ---------------------------------------------------------------------------
# Planificación (S&OP): Plan de Abastecimiento vs Ingresos Físicos reales.
# Solo Materias Primas y Energía/Combustibles — el resto de categorías no
# tiene proceso S&OP. No se toma de MB51: para estos materiales la báscula
# (ZMM056) es la fuente de verdad de lo que realmente ingresó a planta.
# Definido por el usuario.
# ---------------------------------------------------------------------------

CATEGORIAS_PLANIFICACION_SOP = ("MATERIAS PRIMAS", "COMBUSTIBLES")

# Tipo de operación del ticket de báscula (ZMM056) que cuenta como "Recepción"
# (ingreso real): 1 = de terceros (compra externa), 5 = propia (cantera o
# traslado entre plantas). 4 = Despacho/salida (p.ej. clínker o cemento
# saliendo hacia otra planta) y 6 = otro despacho no cuentan como ingreso —
# se excluyen. Deducido cruzando materiales/centros de los Excel reales de
# ambas plantas (los Tipo 1/5 siempre llegan a un centro propio de esa
# planta; los Tipo 4/6 van a destinos de despacho variados). Definido por
# el usuario.
_TIPOS_OPERACION_RECEPCION = ("1", "5")
_IN_TIPOS_OPERACION_RECEPCION = ",".join(f"'{t}'" for t in _TIPOS_OPERACION_RECEPCION)


# Filas del S&OP sin Cod. SAP (p.ej. CALCEM): se guardan igual, con un código
# sintético "S/C-<descripción>", porque el usuario quiere verlas en la
# Evolución de inventario proyectada (solo abastecimiento, sin stock ni
# consumo). En todo lo que se compara contra datos reales por código (Plan vs
# Ingresos Físicos, Cobertura) se excluyen: sin código no hay con qué cruzar.
# GLOB y no LIKE: un "%" literal choca con los "%s" de los parámetros.
PREFIJO_SIN_CODIGO_SOP = "S/C-"
_FILTRO_CON_CODIGO_SOP = f"material_codigo NOT GLOB '{PREFIJO_SIN_CODIGO_SOP}*'"


# Ingresos de báscula que se cuentan con otra fecha que la del ticket
# (material, centro, fecha del ticket) -> fecha con la que se cuentan.
# Excepciones puntuales definidas por el usuario, solo para ese material,
# centro y día:
# - 2026-10-06: el Carbón Importado que llegó a Condorcocha el 30-set-2026
#   (traslado desde Atocongo, 223 T) es del abastecimiento de OCTUBRE: se
#   cuenta el 1-oct y sale de setiembre (el usuario eligió moverlo, no
#   contarlo dos veces).
# Rige el Plan vs Real (Detalle por material, Evaluación diaria, Curva S,
# Cumplimiento mensual), las entregas por proveedor y los traslados; no la
# calidad ni los premios (esos van por la fecha real del análisis).
INGRESOS_CON_OTRA_FECHA = {
    ("06-00000003", "U002", datetime.date(2026, 9, 30)): datetime.date(2026, 10, 1),
}
FECHA_INGRESO_EFECTIVA = "(CASE " + " ".join(
    f"WHEN material_codigo = '{m}' AND centro = '{c}' AND fecha_ingreso = '{f.isoformat()}' THEN '{n.isoformat()}'"
    for (m, c, f), n in INGRESOS_CON_OTRA_FECHA.items()
) + " ELSE fecha_ingreso END)" if INGRESOS_CON_OTRA_FECHA else "fecha_ingreso"


# Consumo real de Planificación (Materias Primas/Combustibles): la misma regla
# de todo el reporte (FILTRO_CLASE_MOVIMIENTO_CONSUMO: clases 2xx más
# Z01/Z04/353). Planificación la usaba desde antes; el 2026-10-04 el usuario
# la extendió a todas las pestañas. Escrita con substr (mismo resultado que
# el BETWEEN, todas las clases "2" son numéricas) para no cambiar el plan de
# SQLite de estas consultas, que filtran primero por material o centro; un
# "%" literal (LIKE '2%') chocaría con el "%s" de los parámetros en el log.
_FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP = (
    f"(substr(clase_movimiento, 1, 1) = '2' OR clase_movimiento IN ({_IN_CLASES_MOVIMIENTO_CONSUMO}))"
)

# Carbón Terceros (06-00000005) en Condorcocha no se consume directo: se manda
# a chancar (movimientos de subcontratación 541/543/544, no clase "2") y el
# consumo real hacia el proceso queda contabilizado contra el material que
# resulta de eso, Carbón Terceros Chancado (14-00001112) — que no está en el
# Plan de Abastecimiento SOP (no se compra aparte, es un intermedio interno),
# así que sin este empalme 06-00000005/U002 se veía sin consumo y se
# descartaba. El stock que se muestra sigue siendo el de 06-00000005 (el que
# sí se planifica); solo el consumo se toma prestado del material chancado.
# Definido por el usuario.
_MATERIALES_CONSUMO_VIA_OTRO = {
    ("06-00000005", "U002"): ("14-00001112", "U002"),
}


def materiales_planificados_sop():
    """Materiales que alguna vez tuvieron una fila en el Plan de Abastecimiento
    SOP (cualquier snapshot/mes) — el universo que sí le interesa a
    Planificación. Bastante más chico que "todo lo clasificado como Materias
    Primas/Combustibles en el maestro" (esto último incluye materiales que
    nunca se planifican). Pedido por el usuario."""
    df = _query(f"SELECT DISTINCT material_codigo FROM gestion_planabastecimientosop WHERE {_FILTRO_CON_CODIGO_SOP}")
    return sorted(df["material_codigo"].tolist())


def pares_planificados_sop():
    """(material, centro) que alguna vez trajo el Plan de Abastecimiento o el
    de Consumos del S&OP, en cualquier emisión."""
    df = _query(f"""
        SELECT material_codigo, centro_destino AS centro FROM gestion_planabastecimientosop WHERE {_FILTRO_CON_CODIGO_SOP}
        UNION
        SELECT material_codigo, centro_destino AS centro FROM gestion_planconsumosop WHERE {_FILTRO_CON_CODIGO_SOP}
    """)
    return set(zip(df["material_codigo"], df["centro"]))
