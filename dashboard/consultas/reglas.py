"""Reglas de negocio en SQL que comparten varias consultas: qué compras cuentan, valor de compra, proveedor y categoría efectivos, contrato, condiciones de pago (PMP) y categoría/familia efectiva del inventario y del material."""

import json
import re
from pathlib import Path

from .base import _query, _sql_solicitantes_alcance


# Cantidades de las reglas que no van al repositorio (público): están en
# datos_locales/reglas_locales.json, excluido de git. Sin el archivo, cada
# regla que las usa queda sin efecto.
_ARCHIVO_REGLAS_LOCALES = Path(__file__).resolve().parents[2] / "datos_locales" / "reglas_locales.json"


def regla_local(clave, defecto):
    """Valor `clave` de datos_locales/reglas_locales.json, o `defecto`."""
    try:
        return json.loads(_ARCHIVO_REGLAS_LOCALES.read_text(encoding="utf-8")).get(clave, defecto)
    except (OSError, ValueError):
        return defecto


# Regla de negocio para el apartado de Compras: excluir grupos de compra que no
# son relevantes para los indicadores (GCp), descartar líneas marcadas como "L"
# en el campo B, excluir el proveedor 4000002 y las líneas sin proveedor.
# Definido por el usuario, no se deduce de los datos. Calificada con el alias
# "lc" porque "grupo_compras" existe en más de una tabla de los JOIN (p.ej.
# gestion_categoriaservicio). G02 ya no está aquí: se excluye por categoría
# (ENERGÍA ELÉCTRICA, ver FILTRO_CATEGORIA_VALIDA_LC) en vez de por código de
# grupo de compras.
_FILTRO_COMPRAS_VALIDAS_BASE = (
    "lc.grupo_compras NOT IN ('G67','G60','G59','G52','G29','G20','G14','G15')"
    " AND lc.campo_b != 'L'"
    " AND lc.proveedor != '4000002'"
    " AND NOT (lc.proveedor = '' AND lc.cuenta_proveedor = '')"
)


class _FiltroComprasValidas:
    """FILTRO_COMPRAS_VALIDAS_LC: la regla de arriba más el alcance de
    solicitantes vigente (base.alcance_solicitantes, filtro GEP y Proyectos
    de Suministros Industriales). Se arma al ponerlo en el f-string de cada
    consulta, así todas las de compras lo respetan sin cambiar su firma."""

    def __str__(self):
        return _FILTRO_COMPRAS_VALIDAS_BASE + _sql_solicitantes_alcance()

    def __format__(self, especificacion):
        return format(str(self), especificacion)


FILTRO_COMPRAS_VALIDAS_LC = _FiltroComprasValidas()

# "Valor de Compras" = Tp. cambio * Valor neto (convierte a soles las líneas en USD/EUR).
# Definido por el usuario. Se usa COALESCE a 1 porque las pocas líneas sin tipo de
# cambio (9 en total) tampoco tienen moneda, es decir, ya están en soles.
VALOR_COMPRA_EXPR = "COALESCE(tipo_cambio, 1) * valor_neto"
VALOR_COMPRA_EXPR_LC = "COALESCE(lc.tipo_cambio, 1) * lc.valor_neto"

# "cuenta_proveedor" trae el nombre del proveedor en este dataset (el campo
# "Proveedor" del SAP origen solo trae el código numérico). Se usa para mostrar
# el proveedor por nombre en vez de por código. Definido por el usuario.
PROVEEDOR_EFECTIVO_LC = "COALESCE(NULLIF(lc.cuenta_proveedor, ''), lc.proveedor)"

# Categoría efectiva de una línea de compra: la del grupo de artículos (CATEGORÍAS
# OFICIAL) o, si no tiene, la Categoría de Servicio de su grupo de compras (GCp).
# Requiere JOIN con gestion_categoria (alias c) y gestion_categoriaservicio (alias cs).
# CAPEX se disolvió como categoría (pedido del usuario, 2026-10-28): sus grupos
# de artículos (proyectos, vehículo pesado) ya no definen la categoría y la
# línea toma la de su grupo de compras, como un servicio.
CATEGORIAS_DISUELTAS = ("CAPEX",)
# Grupos de compras sin fila en la tabla de categorías de servicio y su
# categoría: G70 (nuevo desde set-2026, equipos eléctricos y repuestos de
# puesta en marcha) va a Repuestos (confirmado por el usuario, 2026-10-28).
CATEGORIA_GRUPO_COMPRAS_SIN_TABLA = {"G70": "REPUESTOS"}
_IN_DISUELTAS = ",".join(f"'{c}'" for c in CATEGORIAS_DISUELTAS)
_CATEGORIA_GCP_LC = (
    "COALESCE(NULLIF(cs.categoria_servicio, ''), CASE lc.grupo_compras"
    + "".join(f" WHEN '{g}' THEN '{cat}'" for g, cat in CATEGORIA_GRUPO_COMPRAS_SIN_TABLA.items())
    + " END)"
)
_CATEGORIA_BASE_LC = (
    f"COALESCE(CASE WHEN c.categoria IN ({_IN_DISUELTAS}) THEN NULL ELSE NULLIF(c.categoria, '') END, "
    f"{_CATEGORIA_GCP_LC})"
)

# Pedidos con categoría puesta a mano, por encima de la regla anterior
# (pedido del usuario, 2026-10-26): Doc. compra -> categoría.
# - 4400542795: reubicación de la red de gas natural (3 posiciones a A000,
#   ago-2026). La compró el G11 y caía en MATERIAS PRIMAS, donde bajaba el PMP
#   de agosto; no es materia prima.
CATEGORIA_POR_PEDIDO_LC = {"4400542795": "COMBUSTIBLES"}

# Fletes de materias primas (pedido del usuario, 2026-10-26: cada compra de
# materia prima tiene su flete y cuenta en el valor de compra de MATERIAS
# PRIMAS): los servicios "FLETE DE <material>" del G33 (Atocongo: yeso,
# clínker, sílice, escoria, puzolana, caliza, fierro), que por su GCp caían en
# SERVICIOS INDUSTRIALES. Salvo las posiciones abiertas 140 y 150 de la OC
# 4000000935 (set-2025: cantidad de referencia sin entregas, valor fuera de
# escala), que siguen donde estaban.
GRUPO_COMPRAS_FLETES_MP = "G33"
FLETES_MP_EXCLUIDOS = (("4000000935", "140"), ("4000000935", "150"))
_SQL_FLETE_MP_LC = (
    f"lc.grupo_compras = '{GRUPO_COMPRAS_FLETES_MP}' AND substr(UPPER(lc.texto_breve), 1, 5) = 'FLETE'"
    + "".join(f" AND NOT (lc.doc_compra = '{d}' AND lc.pos_doc_compra = '{p}')" for d, p in FLETES_MP_EXCLUIDOS)
)
CATEGORIA_EFECTIVA_LC = (
    "(CASE"
    + "".join(f" WHEN lc.doc_compra = '{d}' THEN '{cat}'" for d, cat in CATEGORIA_POR_PEDIDO_LC.items())
    + f" WHEN {_SQL_FLETE_MP_LC} THEN 'MATERIAS PRIMAS'"
    + f" ELSE {_CATEGORIA_BASE_LC} END)"
)

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

# Categoría efectiva por línea de inventario, para el segmentador de "% Variación de
# Inventario": los dos materiales de la excepción (14-00000024/14-00000023 en U001)
# se clasifican como MATERIAS PRIMAS (son Caliza, materia prima de cemento) en vez de
# quedar como "Sin categoría"; y se consolidan CONSUMIBLES + SUMINISTROS en
# SUMINISTROS INDUSTRIALES. NO APLICA se excluye aparte, en cada consulta que lo use.
# Definido por el usuario.
CONSOLIDACION_CATEGORIAS_INVENTARIO = {
    "CONSUMIBLES": "SUMINISTROS INDUSTRIALES",
    "SUMINISTROS": "SUMINISTROS INDUSTRIALES",
    "MROE": "REPUESTOS",
    "MROM": "REPUESTOS",
    "MRO": "REPUESTOS",  # categoría de servicio de los GCp de mantenimiento (G08, G55, G58, G64)
}
_CASE_CONSOLIDACION_INVENTARIO = " ".join(
    f"WHEN '{origen}' THEN '{destino}'" for origen, destino in CONSOLIDACION_CATEGORIAS_INVENTARIO.items()
)
_CATEGORIA_INVENTARIO_BASE = (
    "CASE WHEN i.material_codigo IN ('14-00000024', '14-00000023') AND i.area_valoracion = 'U001'"
    " THEN 'MATERIAS PRIMAS' ELSE c.categoria END"
)
CATEGORIA_INVENTARIO_EFECTIVA = (
    f"CASE {_CATEGORIA_INVENTARIO_BASE} {_CASE_CONSOLIDACION_INVENTARIO} ELSE {_CATEGORIA_INVENTARIO_BASE} END"
)

# Categorías excluidas del modelo de Inventario (no aplican / se retiran del reporte).
# Definido por el usuario.
CATEGORIAS_EXCLUIDAS_INVENTARIO = ("NO APLICA", "SERVICIOS ADMINISTRATIVOS")
_IN_CATEGORIAS_EXCLUIDAS_INVENTARIO = ",".join(f"'{c}'" for c in CATEGORIAS_EXCLUIDAS_INVENTARIO)
FILTRO_CATEGORIA_INVENTARIO_VALIDA = (
    f"{CATEGORIA_INVENTARIO_EFECTIVA} NOT IN ({_IN_CATEGORIAS_EXCLUIDAS_INVENTARIO})"
)

# Familia efectiva por línea de inventario, para el segmentador adicional de Familia.
# Los dos materiales de la excepción van a MP CEMENTO (igual criterio que su categoría
# MATERIAS PRIMAS). Definido por el usuario.
FAMILIA_INVENTARIO_EFECTIVA = (
    "CASE WHEN i.material_codigo IN ('14-00000024', '14-00000023') AND i.area_valoracion = 'U001'"
    " THEN 'MP CEMENTO' ELSE c.familia END"
)

# Mismas reglas pero a nivel de material (sobre gestion_material m + gestion_categoria c),
# para poder clasificar materiales fuera del contexto de una línea de inventario
# (p.ej. consumos de MB51). Aquí la excepción de Caliza no distingue centro. Un
# material de una categoría disuelta (CAPEX) toma la de servicio del grupo de
# compras de su maestro, como sus compras.
_CATEGORIA_MATERIAL_BASE = (
    "CASE WHEN m.codigo IN ('14-00000024', '14-00000023') THEN 'MATERIAS PRIMAS'"
    f" WHEN c.categoria IN ({_IN_DISUELTAS}) THEN COALESCE("
    "(SELECT NULLIF(cs.categoria_servicio, '') FROM gestion_categoriaservicio cs WHERE cs.grupo_compras = m.grupo_compras),"
    " CASE m.grupo_compras"
    + "".join(f" WHEN '{g}' THEN '{cat}'" for g, cat in CATEGORIA_GRUPO_COMPRAS_SIN_TABLA.items())
    + " END)"
    " ELSE c.categoria END"
)
CATEGORIA_MATERIAL_EFECTIVA = (
    f"CASE {_CATEGORIA_MATERIAL_BASE} {_CASE_CONSOLIDACION_INVENTARIO} ELSE {_CATEGORIA_MATERIAL_BASE} END"
)
FAMILIA_MATERIAL_EFECTIVA = (
    "CASE WHEN m.codigo IN ('14-00000024', '14-00000023')"
    " THEN 'MP CEMENTO' ELSE c.familia END"
)


# ---------------------------------------------------------------------------
# Precio real de entrada = MB51 + ajustes de valor de la FBL3N. Pedido por el
# usuario (oct-2026). El 101 de MB51 valoriza la entrada al precio del pedido;
# después, sin cantidad y fuera de MB51, llegan a la cuenta de existencias:
# diferencias de precio de la factura (MIRO), fletes, desestiba y cargos de
# importación, anulaciones de factura (MR8M) y cargos/abonos por cambio de
# precio (MR22). Todo eso suma al precio real. Las liquidaciones de órdenes
# (KO88: reparación de repuestos) también caen en la cuenta de existencias,
# pero no son costo de compra: se muestran aparte y no entran al precio
# (propuesto al usuario y aceptado).
# Asignación: al material/centro/mes de contabilización del ajuste. Un ajuste
# de un mes sin compra de ese material (p.ej. la desestiba de un barco
# facturada el mes siguiente) pasa al último mes anterior con compra; uno
# registrado en un centro donde el material nunca se compra (la desestiba del
# Carbón Importado va a U001, la compra es en U004) pasa al centro de compra.
# Los importados comprados solo en U004 no siguen esta regla: sus ajustes se
# juntan con su barco (ver embarques_importados / reparto_por_embarque).
# ---------------------------------------------------------------------------
CLASES_LIQUIDACION_ORDENES = ("PM", "X1")
