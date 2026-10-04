"""Consultas de agregación para los dashboards. Usan SQL directo sobre la misma
conexión de Django (más eficiente que el ORM para agregaciones sobre tablas de
millones de filas) y devuelven DataFrames de pandas listos para graficar."""

import calendar
import copy
import datetime
import functools
import hashlib
import os
import pickle
import re
import shutil
import threading
import uuid
import warnings

import pandas as pd
from django.db import connection

# pandas avisa en CADA consulta que "solo soporta SQLAlchemy" al recibir la
# conexión de Django; funciona igual (es sqlite3 por debajo) y el aviso solo
# ensuciaba el log de la carga diaria (salía como un "error" en rojo).
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")


# --- Caché de consultas, en dos niveles, invalidada por versión de datos ---
# Las agregaciones sobre tablas de millones de filas tardan segundos en SQLite, y
# los datos cambian una vez al día (carga desatendida de la madrugada). Así que:
#   1. Cada resultado se guarda en disco (.cache_consultas/<versión>/<hash>.pkl),
#      compartido entre procesos: el servidor, la tarea programada y Streamlit.
#      `calentar_cache` (tras la carga diaria) recorre todas las páginas y las
#      deja calculadas, así gerencia nunca espera una consulta pesada.
#   2. Encima, un dict en memoria por proceso, para no releer el disco.
# No hay vencimiento por tiempo: cada importación que carga algo llama a
# marcar_datos_actualizados(), que cambia la versión, y todo lo anterior deja de
# usarse de inmediato (antes: TTL de 1 hora, y había que reiniciar el servidor
# para ver datos nuevos). Se devuelve siempre una copia, para que el llamador
# pueda modificar el DataFrame sin tocar el guardado.
_DIR_CACHE = os.environ.get(
    "UPERU_DIR_CACHE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache_consultas"),
)
_ARCHIVO_VERSION = os.path.join(_DIR_CACHE, "version.txt")
# Huella del propio código de este archivo: si cambia una consulta o un
# cálculo (sin que entren datos nuevos), lo guardado con el código anterior
# deja de usarse — si no, la caché seguiría devolviendo el resultado viejo.
with open(os.path.abspath(__file__), "rb") as _f:
    _HUELLA_CODIGO = hashlib.sha1(_f.read()).hexdigest()[:10]
_query_cache = {}
_query_cache_lock = threading.Lock()
_version_leida = {"mtime": None, "version": None}


def _version_datos():
    """Versión actual de los datos (texto en version.txt). Se relee solo si el
    archivo cambió (un os.stat por consulta, microsegundos)."""
    try:
        mtime = os.stat(_ARCHIVO_VERSION).st_mtime_ns
    except FileNotFoundError:
        return "inicial"
    if mtime != _version_leida["mtime"]:
        try:
            with open(_ARCHIVO_VERSION, encoding="utf-8") as f:
                version = f.read().strip() or "inicial"
        except OSError:
            return _version_leida["version"] or "inicial"
        with _query_cache_lock:
            if version != _version_leida["version"]:
                _query_cache.clear()
            _version_leida.update(mtime=mtime, version=version)
    return _version_leida["version"]


def marcar_datos_actualizados():
    """Llamar después de cargar datos nuevos: invalida toda la caché (memoria
    de todos los procesos y disco) y borra las versiones anteriores."""
    os.makedirs(_DIR_CACHE, exist_ok=True)
    version = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    tmp = _ARCHIVO_VERSION + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(version)
    os.replace(tmp, _ARCHIVO_VERSION)
    for nombre in os.listdir(_DIR_CACHE):
        ruta = os.path.join(_DIR_CACHE, nombre)
        if os.path.isdir(ruta) and not nombre.startswith(version):
            shutil.rmtree(ruta, ignore_errors=True)  # un archivo abierto por otro proceso se limpia la próxima vez
    return version


def _copia(valor):
    return valor.copy() if isinstance(valor, pd.DataFrame) else copy.deepcopy(valor)


def _cache_obtener(clave, calcular):
    """Devuelve el valor guardado para `clave` en la versión de datos actual
    (memoria, luego disco) o lo calcula con `calcular()` y lo guarda."""
    version = f"{_version_datos()}-{_HUELLA_CODIGO}"
    clave_version = (version, clave)
    with _query_cache_lock:
        valor = _query_cache.get(clave_version, _SIN_VALOR)
    if valor is not _SIN_VALOR:
        return _copia(valor)

    huella = hashlib.sha1(repr(clave).encode("utf-8")).hexdigest()
    ruta = os.path.join(_DIR_CACHE, version, huella + ".pkl")
    valor = _SIN_VALOR
    try:
        with open(ruta, "rb") as f:
            valor = pickle.load(f)
    except (OSError, EOFError, pickle.UnpicklingError):
        pass
    if valor is _SIN_VALOR:
        valor = calcular()
        try:
            os.makedirs(os.path.dirname(ruta), exist_ok=True)
            tmp = f"{ruta}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as f:
                pickle.dump(valor, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, ruta)
        except OSError:
            pass  # sin disco disponible la caché en memoria sigue funcionando
    with _query_cache_lock:
        _query_cache[clave_version] = valor
    return _copia(valor)


_SIN_VALOR = object()


def _query(sql, params=None):
    params = list(params or [])
    return _cache_obtener(
        ("sql", sql, tuple(params)),
        lambda: pd.read_sql_query(sql, connection, params=params),
    )


def _cacheado(funcion):
    """Guarda el resultado de una función de cálculo (pandas sobre resultados
    de _query) con la misma caché versionada: algunas se repiten muchas veces
    por página (inventario_por_material, 13 veces en una categoría) y su parte
    de pandas pesa más que la consulta. Solo para funciones que dependen
    únicamente de sus argumentos y de los datos cargados; la fecha de hoy va en
    la clave por si alguna ventana de fechas depende de ella."""
    @functools.wraps(funcion)
    def envoltura(*args, **kwargs):
        clave = ("fn", funcion.__qualname__, repr(args), repr(sorted(kwargs.items())), datetime.date.today())
        return _cache_obtener(clave, lambda: funcion(*args, **kwargs))
    return envoltura


def formato_soles(valor):
    """Formatea un monto como moneda: 'S/. 000,000,000.00'. Definido por el usuario."""
    if valor is None or valor != valor:  # None o NaN
        return ""
    return f"S/. {valor:,.2f}"


_MESES_ES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

_MESES_ES_ABREV = [
    "", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
    "Jul", "Ago", "Sep", "Oct", "Nov", "Dic",
]


def formato_mes_anio(fecha):
    """Formatea una fecha como 'Mes - Año' (p.ej. 'Agosto - 2026'). Definido por el
    usuario. No depende del locale del sistema, usa nombres en español fijos."""
    return f"{_MESES_ES[fecha.month]} - {fecha.year}"


def formato_mes_anio_abrev(fecha):
    """Formatea una fecha como 'Mes 2026' con el mes abreviado (p.ej. 'Ago 2026'),
    para usar como etiqueta de eje en gráficos con muchos puntos."""
    return f"{_MESES_ES_ABREV[fecha.month]} {fecha.year}"


def nombre_mes(numero_mes):
    """Nombre del mes en español a partir de su número (1-12)."""
    return _MESES_ES[numero_mes]


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

# "cuenta_proveedor" trae el nombre del proveedor en este dataset (el campo
# "Proveedor" del SAP origen solo trae el código numérico). Se usa para mostrar
# el proveedor por nombre en vez de por código. Definido por el usuario.
PROVEEDOR_EFECTIVO_LC = "COALESCE(NULLIF(lc.cuenta_proveedor, ''), lc.proveedor)"

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
# (p.ej. consumos de MB51). Aquí la excepción de Caliza no distingue centro.
_CATEGORIA_MATERIAL_BASE = (
    "CASE WHEN m.codigo IN ('14-00000024', '14-00000023')"
    " THEN 'MATERIAS PRIMAS' ELSE c.categoria END"
)
CATEGORIA_MATERIAL_EFECTIVA = (
    f"CASE {_CATEGORIA_MATERIAL_BASE} {_CASE_CONSOLIDACION_INVENTARIO} ELSE {_CATEGORIA_MATERIAL_BASE} END"
)
FAMILIA_MATERIAL_EFECTIVA = (
    "CASE WHEN m.codigo IN ('14-00000024', '14-00000023')"
    " THEN 'MP CEMENTO' ELSE c.familia END"
)


@_cacheado
def catalogo_materiales():
    """Maestro de materiales (ZMM060) con su categoría y familia efectivas. Es la
    fuente única para clasificar materiales al cruzar datasets en pandas."""
    return _query(f"""
        SELECT m.codigo AS material_codigo,
               m.descripcion AS texto_breve,
               m.unidad_medida_base AS unidad_medida_base,
               m.centro AS centro,
               {CATEGORIA_MATERIAL_EFECTIVA} AS categoria,
               {FAMILIA_MATERIAL_EFECTIVA} AS familia
        FROM gestion_material m
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
    """)


@_cacheado
def inventario_por_material(fecha):
    """Stock y valor de cierre por material a una fecha, ya clasificado por categoría
    y familia. Es el dataset base del que se derivan (en pandas) el resumen, el top de
    materiales y el valor por familia, evitando repetir agregaciones pesadas."""
    inv = _query(f"""
        SELECT i.material_codigo AS material_codigo,
               SUM(i.stock_cierre) AS stock_cierre,
               SUM(i.valor_final) AS valor_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE i.fecha = %s AND {FILTRO_INVENTARIO_CON_CATEGORIA}
              AND {FILTRO_CATEGORIA_INVENTARIO_VALIDA}
        GROUP BY i.material_codigo
    """, [fecha])
    catalogo = catalogo_materiales()
    df = inv.merge(catalogo, on="material_codigo", how="left")
    df["texto_breve"] = df["texto_breve"].fillna("")
    df["unidad_medida_base"] = df["unidad_medida_base"].fillna("")
    df["centro"] = df["centro"].fillna("")
    return df


def fechas_inventario_disponibles():
    # Salta de fecha en fecha por el índice (MIN(fecha) > la anterior) en vez
    # de un DISTINCT que lee ~1.5 M filas: son ~40 fechas, así que son ~40
    # búsquedas puntuales. Con el disco en frío (primera apertura del día) el
    # DISTINCT tardaba ~2.6 s. La CTE devuelve texto, de ahí la conversión.
    df = _query("""
        WITH RECURSIVE f(fecha) AS (
            SELECT MIN(fecha) FROM gestion_inventariomensual
            UNION ALL
            SELECT (SELECT MIN(fecha) FROM gestion_inventariomensual WHERE fecha > f.fecha)
            FROM f WHERE f.fecha IS NOT NULL
        )
        SELECT fecha FROM f WHERE fecha IS NOT NULL
    """)
    return [datetime.date.fromisoformat(str(f)[:10]) for f in df["fecha"]]


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


def valor_inventario_mensual_por_categoria(fecha_desde=None):
    """Serie mensual de Valor Final desagregada por categoría (misma clasificación
    que el segmentador de % Variación de Inventario: excepción -> MATERIAS PRIMAS,
    MROE/MROM -> REPUESTOS, CONSUMIBLES/SUMINISTROS -> SUMINISTROS INDUSTRIALES,
    NO APLICA y SERVICIOS ADMINISTRATIVOS excluidos).

    `fecha_desde` acota el rango leído: recorrer el histórico completo (1.26M filas)
    tarda ~20 s en SQLite, así que conviene pedir solo los años que se van a graficar."""
    filtro_fecha = "AND i.fecha >= %s" if fecha_desde else ""
    params = [fecha_desde] if fecha_desde else []
    return _query(f"""
        SELECT i.fecha AS fecha,
               {CATEGORIA_INVENTARIO_EFECTIVA} AS categoria,
               SUM(i.valor_final) AS valor_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE {FILTRO_INVENTARIO_CON_CATEGORIA}
              AND {FILTRO_CATEGORIA_INVENTARIO_VALIDA}
              {filtro_fecha}
        GROUP BY i.fecha, {CATEGORIA_INVENTARIO_EFECTIVA}
        ORDER BY i.fecha
    """, params)


def valor_inventario_por_grupo(fecha, top=15):
    # Solo materiales con categoría designada: excluye NO APLICA además del filtro
    # general de Inventario (FILTRO_INVENTARIO_CON_CATEGORIA).
    return _query(f"""
        SELECT i.denominacion_grupo_articulos AS grupo, SUM(i.valor_final) AS valor_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE i.fecha = %s AND {FILTRO_INVENTARIO_CON_CATEGORIA}
              AND {FILTRO_CATEGORIA_INVENTARIO_VALIDA}
        GROUP BY i.denominacion_grupo_articulos
        ORDER BY valor_total DESC
        LIMIT %s
    """, [fecha, top])


def valor_inventario_por_familia(fecha, top=15):
    # Solo materiales con categoría designada: excluye NO APLICA, y usa la misma
    # FAMILIA_INVENTARIO_EFECTIVA del segmentador (los dos materiales de la excepción
    # van a MP CEMENTO en vez de "Sin categoría").
    return _query(f"""
        SELECT {FAMILIA_INVENTARIO_EFECTIVA} AS familia, SUM(i.valor_final) AS valor_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE i.fecha = %s AND {FILTRO_INVENTARIO_CON_CATEGORIA}
              AND {FILTRO_CATEGORIA_INVENTARIO_VALIDA}
        GROUP BY {FAMILIA_INVENTARIO_EFECTIVA}
        ORDER BY valor_total DESC
        LIMIT %s
    """, [fecha, top])


def top_materiales_inventario(fecha, top=20):
    # La descripción de MB5B (texto_breve) viene vacía en ~9% de las filas; se prioriza
    # la descripción del material en ZMM060 (gestion_material.descripcion) y se usa
    # MB5B solo como respaldo. La unidad de medida base (UMB) también viene de ZMM060.
    return _query(f"""
        SELECT i.material_codigo,
               COALESCE(NULLIF(m.descripcion, ''), NULLIF(i.texto_breve, '')) AS texto_breve,
               m.unidad_medida_base AS unidad_medida_base,
               SUM(i.stock_cierre) AS stock, SUM(i.valor_final) AS valor_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE i.fecha = %s AND {FILTRO_INVENTARIO_CON_CATEGORIA}
        GROUP BY i.material_codigo, COALESCE(NULLIF(m.descripcion, ''), NULLIF(i.texto_breve, '')),
                 m.unidad_medida_base
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


def categorias_inventario_disponibles():
    """Categorías disponibles para el segmentador de % Variación de Inventario. Usa
    CATEGORIA_INVENTARIO_EFECTIVA: los materiales de la excepción van a MATERIAS
    PRIMAS, CONSUMIBLES/SUMINISTROS se consolidan en SUMINISTROS INDUSTRIALES, y
    NO APLICA queda excluido.

    Se usa en las pestañas de TODAS las páginas: la categoría solo depende de
    (material, centro), así que se cruzan con el maestro los ~pares distintos
    en vez de las ~1.5 M líneas de inventario (11.8 s -> ~1 s en frío)."""
    df = _query(f"""
        SELECT DISTINCT {CATEGORIA_INVENTARIO_EFECTIVA} AS categoria
        FROM (SELECT DISTINCT material_codigo, area_valoracion FROM gestion_inventariomensual) i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE {FILTRO_INVENTARIO_CON_CATEGORIA}
              AND {FILTRO_CATEGORIA_INVENTARIO_VALIDA}
        ORDER BY categoria
    """)
    return df["categoria"].tolist()


def familias_inventario_disponibles(categoria=None):
    """Familias disponibles para el segmentador de Familia. Si se indica `categoria`,
    solo devuelve las familias que pertenecen a esa categoría (segmentador en cascada)."""
    filtro_categoria = f"AND {CATEGORIA_INVENTARIO_EFECTIVA} = %s" if categoria else ""
    params = [categoria] if categoria else []
    df = _query(f"""
        SELECT DISTINCT {FAMILIA_INVENTARIO_EFECTIVA} AS familia
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE {FILTRO_INVENTARIO_CON_CATEGORIA} {filtro_categoria}
        ORDER BY familia
    """, params)
    return df["familia"].tolist()


@_cacheado
def variacion_inventario_por_material(fecha_inicial, fecha_final, categoria=None, familia=None, centro=None):
    """Variación de Valor Final y Stock de Cierre (MB5B) por código de material entre
    dos meses. fecha_final debe ser posterior a fecha_inicial. Respeta el mismo filtro
    de categoría que el resto del modelo de Inventario (FILTRO_INVENTARIO_CON_CATEGORIA),
    excluye NO APLICA y, si se indica `categoria`, filtra además por esa categoría
    (según CATEGORIA_INVENTARIO_EFECTIVA: excepción -> MATERIAS PRIMAS,
    CONSUMIBLES/SUMINISTROS consolidados en SUMINISTROS INDUSTRIALES). La descripción
    prioriza ZMM060 (gestion_material.descripcion) sobre el texto_breve de MB5B, que
    viene vacío en ~9% de las filas; la UMB también viene de ZMM060. `familia` filtra
    además por FAMILIA_INVENTARIO_EFECTIVA."""
    mes1 = inventario_por_material(fecha_inicial)
    mes2 = inventario_por_material(fecha_final)
    df = mes1.merge(mes2, on="material_codigo", how="outer", suffixes=("_ini", "_fin"))

    # Los datos descriptivos se toman del mes final y, si el material ya no existe ahí,
    # del inicial.
    for columna in ("texto_breve", "unidad_medida_base", "categoria", "familia", "centro"):
        df[columna] = df[f"{columna}_fin"].fillna(df[f"{columna}_ini"])

    df["stock_mes_inicial"] = df["stock_cierre_ini"].fillna(0.0).astype(float)
    df["stock_mes_final"] = df["stock_cierre_fin"].fillna(0.0).astype(float)
    df["variacion_stock"] = df["stock_mes_final"] - df["stock_mes_inicial"]
    df["valor_mes_inicial"] = df["valor_total_ini"].fillna(0.0).astype(float)
    df["valor_mes_final"] = df["valor_total_fin"].fillna(0.0).astype(float)
    df["variacion_valor"] = df["valor_mes_final"] - df["valor_mes_inicial"]
    df["variacion_pct"] = (
        df["variacion_valor"] / df["valor_mes_inicial"] * 100
    ).where(df["valor_mes_inicial"] != 0)

    if categoria:
        df = df[df["categoria"] == categoria]
    if familia:
        df = df[df["familia"] == familia]
    if centro:
        df = df[df["centro"] == centro]

    columnas = [
        "material_codigo", "texto_breve", "unidad_medida_base", "categoria", "familia", "centro",
        "stock_mes_inicial", "stock_mes_final", "variacion_stock",
        "valor_mes_inicial", "valor_mes_final", "variacion_valor", "variacion_pct",
    ]
    return df[columnas].sort_values("variacion_valor", ascending=False).reset_index(drop=True)


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


# "Materiales sin Movimiento" (antes "Obsoletos"): sin ningún consumo en los
# últimos 3 años, no 6 meses como el resto de indicadores de consumo — es un
# umbral mucho más exigente pensado para detectar capital realmente
# inmovilizado, no solo baja rotación reciente. Definido por el usuario.
MATERIALES_SIN_MOVIMIENTO_ANIOS = 3


@_cacheado
def materiales_obsoletos_por_categoria(fecha, familia=None, centro=None):
    """Materiales con stock de cierre > 0 pero sin ningún consumo (MB51,
    CLASES_MOVIMIENTO_CONSUMO) en los últimos MATERIALES_SIN_MOVIMIENTO_ANIOS
    años (3) que terminan en `fecha` — "Materiales sin Movimiento": capital
    inmovilizado por categoría. Se compara cantidad consumida con 0 (no
    cantidad contra cantidad de distinta UMB), así que no hay problema de
    unidades; el valor sí se puede sumar entre materiales sin problema.
    `familia`/`centro`: segmentadores propios de esta tabla (no afectan otros
    indicadores)."""
    fecha_inicio, fecha_fin = _ventana_anios(fecha, MATERIALES_SIN_MOVIMIENTO_ANIOS)
    inv = inventario_por_material(fecha)
    consumo = consumo_periodo_por_material(fecha_inicio, fecha_fin)
    df = inv.merge(consumo, on="material_codigo", how="left")
    df["consumo_total"] = df["consumo_total"].fillna(0.0)
    if familia:
        df = df[df["familia"] == familia]
    if centro:
        df = df[df["centro"] == centro]
    obsoletos = df[(df["valor_total"] > 0) & (df["consumo_total"] <= 0)]
    if obsoletos.empty:
        return []
    resumen = obsoletos.groupby("categoria", as_index=False).agg(
        materiales_obsoletos=("material_codigo", "count"),
        valor_obsoleto=("valor_total", "sum"),
    )
    return resumen.to_dict("records")


@_cacheado
def clasificacion_abc_por_categoria(fecha, familia=None, centro=None):
    """Clasificación ABC (Pareto de valor) de los materiales con stock de cada
    categoría: A = acumulan hasta 80% del valor de la categoría, B = hasta 95%,
    C = el resto. Ayuda a priorizar qué materiales vigilar de cerca.
    `familia`/`centro`: segmentadores propios de esta tabla (no afectan otros
    indicadores)."""
    inv = inventario_por_material(fecha)
    inv = inv[inv["valor_total"] > 0]
    if familia:
        inv = inv[inv["familia"] == familia]
    if centro:
        inv = inv[inv["centro"] == centro]
    resultado = []
    for categoria, grupo in inv.groupby("categoria"):
        grupo = grupo.sort_values("valor_total", ascending=False)
        total = grupo["valor_total"].sum()
        acumulado = grupo["valor_total"].cumsum() / total * 100
        clase = pd.cut(acumulado, bins=[-0.01, 80, 95, 100.01], labels=["A", "B", "C"])
        tabla = pd.DataFrame({"clase_abc": clase.values, "valor_total": grupo["valor_total"].values})
        resumen = (
            tabla.groupby("clase_abc", observed=True)
            .agg(materiales=("valor_total", "count"), valor=("valor_total", "sum"))
            .reindex(["A", "B", "C"]).fillna(0)
        )
        resultado.append({
            "categoria": categoria,
            "valor_total": float(total),
            "clases": [
                {"clase": c, "materiales": int(resumen.loc[c, "materiales"]), "valor": float(resumen.loc[c, "valor"])}
                for c in ["A", "B", "C"]
            ],
        })
    return resultado


@_cacheado
def cobertura_materiales_criticos_por_categoria(fecha, familia=None, centro=None):
    """Cobertura (meses) de los materiales marcados como críticos (ZMM060,
    campo 'Mat. Critico' = 'X'), por categoría, y cuántos están en riesgo
    (cobertura menor a 1 mes al ritmo de consumo actual). `familia`/`centro`:
    segmentadores propios de esta tabla (no afectan otros indicadores)."""
    criticos = _query("SELECT codigo AS material_codigo FROM gestion_material WHERE material_critico = 'X'")
    rot = rotacion_inventario_por_material(fecha)
    rot_criticos = rot[rot["material_codigo"].isin(set(criticos["material_codigo"]))].dropna(subset=["cobertura_meses"])
    if familia:
        rot_criticos = rot_criticos[rot_criticos["familia"] == familia]
    if centro:
        rot_criticos = rot_criticos[rot_criticos["centro"] == centro]
    if rot_criticos.empty:
        return []
    resultado = []
    for categoria, grupo in rot_criticos.groupby("categoria"):
        resultado.append({
            "categoria": categoria,
            "materiales_criticos": int(len(grupo)),
            "cobertura_promedio": round(float(grupo["cobertura_meses"].mean()), 2),
            "en_riesgo": int((grupo["cobertura_meses"] < 1).sum()),
        })
    return sorted(resultado, key=lambda r: r["en_riesgo"], reverse=True)


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


# Clases de movimiento de MB51 que cuentan como "consumo" en todos los indicadores
# de consumo (Índice de Rotación de Inventario, Materiales sin Movimiento): Z01
# (SM Vta.Relacionadas), Z04 (EM AnulTransf.Gratui) y 353 (aún no presente en la
# data cargada, se deja programada). Definido por el usuario.
CLASES_MOVIMIENTO_CONSUMO = ("Z01", "Z04", "353")
_IN_CLASES_MOVIMIENTO_CONSUMO = ",".join(f"'{c}'" for c in CLASES_MOVIMIENTO_CONSUMO)

# Para REPUESTOS (categorías origen MROE/MROM), los movimientos con Lote "USADO"
# son repuestos reacondicionados que reingresan a almacén, no consumo real: se
# excluyen de los indicadores de consumo. El resto de categorías sí cuenta todos
# sus lotes. Definido por el usuario.
_CATEGORIAS_ORIGEN_REPUESTOS = tuple(
    origen for origen, destino in CONSOLIDACION_CATEGORIAS_INVENTARIO.items() if destino == "REPUESTOS"
)
_IN_CATEGORIAS_ORIGEN_REPUESTOS = ",".join(f"'{c}'" for c in _CATEGORIAS_ORIGEN_REPUESTOS)
FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS = (
    "NOT (lote = 'USADO' AND material_codigo IN ("
    "SELECT m.codigo FROM gestion_material m"
    " INNER JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id"
    f" WHERE c.categoria IN ({_IN_CATEGORIAS_ORIGEN_REPUESTOS})))"
)


def _ventana_6_meses(fecha_objetivo):
    """Rango de fechas (inicio, fin) de los 6 meses calendario que terminan en el
    mes de `fecha_objetivo` (inclusive)."""
    anio, mes = fecha_objetivo.year, fecha_objetivo.month
    fecha_fin = datetime.date(anio, mes, calendar.monthrange(anio, mes)[1])
    total_meses = (anio * 12 + (mes - 1)) - 5
    inicio_anio, inicio_mes = divmod(total_meses, 12)
    fecha_inicio = datetime.date(inicio_anio, inicio_mes + 1, 1)
    return fecha_inicio, fecha_fin


def consumo_6_meses_por_material(fecha):
    """Consumo (MB51) por material en los 6 meses calendario que terminan en `fecha`.
    Solo lee columnas del índice de cobertura idx_mov_consumo_rotacion, por eso no
    trae descripción ni UMB (esas salen del maestro de materiales)."""
    fecha_inicio, fecha_fin = _ventana_6_meses(fecha)
    return _query(f"""
        SELECT material_codigo AS material_codigo,
               -SUM(cantidad) AS consumo_total
        FROM gestion_movimientomaterial
        WHERE clase_movimiento IN ({_IN_CLASES_MOVIMIENTO_CONSUMO})
              AND fecha_contabilizacion BETWEEN %s AND %s
              AND material_codigo != ''
              AND {FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS}
        GROUP BY material_codigo
    """, [fecha_inicio, fecha_fin])


def _ventana_anios(fecha_objetivo, anios):
    """Rango de fechas (inicio, fin) de los últimos `anios` años exactos (día a
    día, no por mes calendario como _ventana_6_meses) que terminan en
    `fecha_objetivo` (inclusive). Si esa historia todavía no está cargada (los
    datos de MB51 arrancan en 2024), la ventana simplemente no encuentra
    movimientos en la parte que falta — no hace falta ningún caso especial:
    en cuanto se cargue esa data (p.ej. el año 2023), el mismo cálculo la
    toma en cuenta sin cambiar el código."""
    try:
        fecha_inicio = fecha_objetivo.replace(year=fecha_objetivo.year - anios)
    except ValueError:
        # 29 de febrero en un año no bisiesto tres años atrás: cae al 28.
        fecha_inicio = fecha_objetivo.replace(year=fecha_objetivo.year - anios, day=28)
    return fecha_inicio, fecha_objetivo


def consumo_periodo_por_material(fecha_inicio, fecha_fin):
    """Consumo (MB51) por material entre `fecha_inicio` y `fecha_fin` (ambas
    inclusive). Generalización de consumo_6_meses_por_material para ventanas
    de cualquier tamaño (p.ej. los 3 años de Materiales sin Movimiento)."""
    return _query(f"""
        SELECT material_codigo AS material_codigo,
               -SUM(cantidad) AS consumo_total
        FROM gestion_movimientomaterial
        WHERE clase_movimiento IN ({_IN_CLASES_MOVIMIENTO_CONSUMO})
              AND fecha_contabilizacion BETWEEN %s AND %s
              AND material_codigo != ''
              AND {FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS}
        GROUP BY material_codigo
    """, [fecha_inicio, fecha_fin])


def consumo_valorizado_6_meses_por_material(fecha):
    """Igual que consumo_6_meses_por_material, pero en valor (S/., campo
    'Importe en moneda local' de MB51, mismo signo que cantidad: negativo en
    salidas H, positivo en sus anulativos S) en vez de cantidad. Sirve para
    agregar consumo entre materiales con distinta unidad de medida (p.ej. la
    rotación de inventario a nivel de categoría en el OVERVIEW, donde sumar
    cantidades de materiales en KG, UND, M, etc. no sería válido)."""
    fecha_inicio, fecha_fin = _ventana_6_meses(fecha)
    return _query(f"""
        SELECT material_codigo AS material_codigo,
               -SUM(importe_moneda_local) AS consumo_valor_6meses
        FROM gestion_movimientomaterial
        WHERE clase_movimiento IN ({_IN_CLASES_MOVIMIENTO_CONSUMO})
              AND fecha_contabilizacion BETWEEN %s AND %s
              AND material_codigo != ''
              AND {FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS}
        GROUP BY material_codigo
    """, [fecha_inicio, fecha_fin])


@_cacheado
def rotacion_inventario_por_material(fecha):
    """Índice de Rotación de Inventario por material (veces) = Consumo promedio (MB51,
    6 últimos meses calendario que terminan en `fecha`) ÷ Inventario de cierre (MB5B,
    a `fecha`); y su inverso, Cobertura (meses) = Inventario de cierre ÷ Consumo
    promedio. Ambos en cantidad (unidades), no en valor. Incluye la categoría/familia
    de cada material para poder filtrar sin repetir la consulta.

    El cruce inventario-consumo se hace en pandas: en SQL, unir ambas agregaciones
    como CTEs obligaba a SQLite a un join anidado sin índice (~20 s); así baja a ~1 s."""
    inv = inventario_por_material(fecha)
    cons = consumo_6_meses_por_material(fecha)
    catalogo = catalogo_materiales()

    # Materiales con consumo pero sin stock: se completan desde el maestro.
    cons = cons.merge(catalogo, on="material_codigo", how="left")
    df = inv.merge(cons[["material_codigo", "consumo_total"]], on="material_codigo", how="outer")

    solo_consumo = df["stock_cierre"].isna()
    if solo_consumo.any():
        faltantes = df.loc[solo_consumo, "material_codigo"]
        relleno = catalogo.set_index("material_codigo").reindex(faltantes)
        for columna in ("texto_breve", "unidad_medida_base", "centro", "categoria", "familia"):
            df.loc[solo_consumo, columna] = relleno[columna].values

    df["stock_cierre"] = df["stock_cierre"].fillna(0.0).astype(float)
    df["valor_total"] = df["valor_total"].fillna(0.0).astype(float)
    df["consumo_promedio"] = df["consumo_total"].fillna(0.0).astype(float) / 6.0
    df["indice_rotacion"] = (df["consumo_promedio"] / df["stock_cierre"]).where(df["stock_cierre"] != 0)
    df["cobertura_meses"] = (df["stock_cierre"] / df["consumo_promedio"]).where(df["consumo_promedio"] != 0)

    # Los materiales con consumo pero sin stock traen su categoría del maestro, que no
    # pasa por el filtro de inventario: se excluyen aquí las categorías descartadas.
    df = df[~df["categoria"].isin(CATEGORIAS_EXCLUIDAS_INVENTARIO)]

    columnas = [
        "material_codigo", "texto_breve", "unidad_medida_base", "centro", "categoria", "familia",
        "stock_cierre", "consumo_promedio", "indice_rotacion", "cobertura_meses",
    ]
    return df[columnas].sort_values("indice_rotacion", ascending=False, na_position="last").reset_index(drop=True)


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


def _lista_materiales(material):
    """`material` de un filtro: un código o varios (lista/tupla; el filtro
    Material de Planificación permite elegir varios)."""
    return [material] if isinstance(material, str) else list(material)


def _limites_mes(fecha):
    fecha_inicio = fecha.replace(day=1)
    fecha_fin = fecha.replace(day=calendar.monthrange(fecha.year, fecha.month)[1])
    return fecha_inicio, fecha_fin


def meses_planificacion_disponibles():
    """Unión de meses con Plan de Abastecimiento y/o Ingresos Físicos
    cargados, para el segmentador de Mes de Planificación."""
    plan = _query("SELECT DISTINCT mes_planificado AS mes FROM gestion_planabastecimientosop")
    meses_plan = plan["mes"].tolist()

    ingresos = _query(
        "SELECT DISTINCT strftime('%Y-%m-01', fecha_ingreso) AS mes FROM gestion_ingresofisicosop "
        "WHERE fecha_ingreso IS NOT NULL"
    )
    meses_ingresos = pd.to_datetime(ingresos["mes"]).dt.date.tolist() if not ingresos.empty else []

    return sorted(set(meses_plan) | set(meses_ingresos))


# Nombre con que se muestra cada centro en pantalla (filtros, tablas,
# gráficos): el código SAP sigue siendo el valor interno de los filtros y de
# los datos. Pedido por el usuario (2026-10-02). Otros centros (U004
# importación...) se muestran con su código.
NOMBRES_CENTRO = {"U001": "ATOCONGO", "U002": "CONDORCOCHA"}


def nombre_centro(codigo):
    return NOMBRES_CENTRO.get(codigo, codigo)


def descripcion_centros_planificacion():
    """Mapea centro_destino -> nombre de planta (columna 'Destino' del Excel
    de Plan de Abastecimiento SOP, p.ej. U001 -> ATOCONGO), para mostrar un
    título legible en Detalle por material/Evaluación diaria."""
    df = _query("SELECT DISTINCT centro_destino AS centro, destino FROM gestion_planabastecimientosop")
    return dict(zip(df["centro"], df["destino"]))


def plan_abastecimiento_por_material(mes):
    """Plan de abastecimiento (S&OP) por material/centro para `mes`, siempre
    con la versión más reciente del plan cargado (el snapshot más nuevo): si
    se recarga el plan al mes siguiente, la proyección se actualiza sola, no
    queda una foto vieja congelada."""
    return _query(f"""
        SELECT material_codigo AS material_codigo, centro_destino AS centro,
               SUM(cantidad_plan) AS cantidad_plan
        FROM gestion_planabastecimientosop
        WHERE mes_planificado = %s AND {_FILTRO_CON_CODIGO_SOP}
              AND mes_snapshot = (SELECT MAX(mes_snapshot) FROM gestion_planabastecimientosop)
        GROUP BY material_codigo, centro_destino
    """, [mes])


def ingresos_fisicos_por_material(mes):
    """Ingresos físicos reales de Recepción (báscula, ZMM056 — solo Tipo de
    operación 1/5, ver _TIPOS_OPERACION_RECEPCION) por material/centro en el
    mes calendario `mes`. Definido por el usuario: no se toma de MB51 para
    estos materiales."""
    fecha_inicio, fecha_fin = _limites_mes(mes)
    return _query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro,
               SUM(cantidad) AS cantidad_real
        FROM gestion_ingresofisicosop
        WHERE fecha_ingreso BETWEEN %s AND %s
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION})
        GROUP BY material_codigo, centro
    """, [fecha_inicio, fecha_fin])


def ingresos_fisicos_por_proveedor(mes):
    """Ingresos físicos de Recepción (mismas reglas que
    ingresos_fisicos_por_material) por material/centro/proveedor en el mes
    `mes`, con la cantidad de carros (tickets). En los ingresos propios (Tipo
    5: cantera o traslado entre plantas) el "proveedor" de ZMM056 es la planta
    de origen (ATOCONGO, PUCARA...). Usado por el Detalle por material de
    Cumplimiento. Pedido por el usuario (después llegará el plan por proveedor)."""
    fecha_inicio, fecha_fin = _limites_mes(mes)
    return _query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro, proveedor_codigo AS proveedor_codigo,
               proveedor AS proveedor, MAX(tipo_operacion) AS tipo_operacion,
               SUM(cantidad) AS cantidad_real, COUNT(*) AS carros
        FROM gestion_ingresofisicosop
        WHERE fecha_ingreso BETWEEN %s AND %s
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION})
        GROUP BY material_codigo, centro, proveedor_codigo, proveedor
    """, [fecha_inicio, fecha_fin])


@_cacheado
def entregas_por_proveedor_sop():
    """Entregas mes a mes por material/centro/proveedor (ZMM056, mismas reglas
    de Recepción que ingresos_fisicos_por_material: Tipo 1/5), en toneladas y
    carros, de los materiales de Planificación (misma categoría efectiva que
    Plan vs Real). En los Tipo 5 el "proveedor" es la planta o cantera de
    origen. Para Evolución de entregas por proveedor (pestaña Cumplimiento).
    Pedido por el usuario."""
    columnas = ["material_codigo", "texto_breve", "centro", "proveedor_codigo", "proveedor", "tipo",
                "mes", "toneladas", "carros"]
    df = _query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro, proveedor_codigo AS proveedor_codigo,
               proveedor AS proveedor, MAX(tipo_operacion) AS tipo_operacion,
               MAX(descripcion_material) AS descripcion,
               substr(fecha_ingreso, 1, 7) || '-01' AS mes, SUM(cantidad) AS toneladas, COUNT(*) AS carros
        FROM gestion_ingresofisicosop
        WHERE fecha_ingreso IS NOT NULL AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION})
        GROUP BY material_codigo, centro, proveedor_codigo, proveedor, mes
    """)
    if df.empty:
        return pd.DataFrame(columns=columnas)
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    df["toneladas"] = df["toneladas"].fillna(0.0).astype(float)
    df["carros"] = df["carros"].astype(int)
    df["proveedor"] = df["proveedor"].fillna("").str.strip().replace("", "(sin proveedor)")
    df["tipo"] = df["tipo_operacion"].map(lambda t: "Propio" if t == "5" else "Terceros")
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "categoria"]].drop_duplicates("material_codigo")
    df = df.merge(catalogo, on="material_codigo", how="left")
    df["texto_breve"] = df["texto_breve"].where(df["texto_breve"].notna() & (df["texto_breve"] != ""), df["descripcion"]).fillna("")
    sin_categoria_en_plan = df["categoria"].isna() & df["material_codigo"].isin(materiales_planificados_sop())
    df.loc[sin_categoria_en_plan, "categoria"] = "MATERIAS PRIMAS"
    df = df[df["categoria"].isin(CATEGORIAS_PLANIFICACION_SOP)]
    return df[columnas].sort_values(["mes", "material_codigo", "centro", "proveedor"]).reset_index(drop=True)


# Materiales que Condorcocha recibe SOLO por traslado desde Atocongo: todo su
# plan de abastecimiento en U002 es traslado (el clinker se produce en
# Atocongo; el Carbón Importado llega por barco a Atocongo, ver
# _TRASLADOS_SOP). El Carbón Trujillo también se traslada a veces, pero su plan
# en U002 mezcla compras directas: no se puede medir como traslado. Para el
# indicador de Traslados de la pestaña Indicadores. Visto en ZMM056 (Tipo 5,
# "proveedor" = planta de origen).
CENTRO_ATOCONGO, CENTRO_CONDORCOCHA = "U001", "U002"
MATERIALES_TRASLADO_ATOCONGO_CONDORCOCHA = ("14-00000037", "06-00000003")


def traslados_recibidos(mes, centro):
    """Ingresos por traslado (ZMM056 Tipo 5) que recibió `centro` en el mes
    `mes`, por material y planta de origen (el "proveedor" del ticket), con
    toneladas y carros."""
    fecha_inicio, fecha_fin = _limites_mes(mes)
    return _query("""
        SELECT material_codigo AS material_codigo, MAX(descripcion_material) AS descripcion,
               proveedor AS origen, SUM(cantidad) AS toneladas, COUNT(*) AS carros
        FROM gestion_ingresofisicosop
        WHERE fecha_ingreso BETWEEN %s AND %s AND centro = %s AND tipo_operacion = '5'
        GROUP BY material_codigo, proveedor
    """, [fecha_inicio, fecha_fin, centro])


def _con_categoria_efectiva_planificacion(df, plan):
    """Completa `categoria` (columna ya mergeada desde catalogo_materiales) en
    `df` para los materiales del Plan de Abastecimiento SOP que no tienen
    categoría propia en el maestro (p.ej. 14-00000037 CLINKER I ATO: es un
    intermedio de producción, no tiene grupo de artículo/categoría de compras
    asignado) aunque el Plan sí los incluya como parte del abastecimiento de
    Materias Primas. El alcance de Planificación no es solo "categoría =
    Materias Primas/Combustibles": también entra cualquier material que el
    propio Plan ya trae (siempre es de este dominio, lo define el equipo de
    S&OP), etiquetado MATERIAS PRIMAS si no tiene categoría propia. Usada por
    plan_vs_real_abastecimiento_por_material y evaluacion_diaria_abastecimiento
    — antes vivía duplicada en las dos y una de las copias quedó desactualizada,
    dejando sin Ingresos Físicos a estos materiales en la evaluación diaria/
    curva S aunque sí aparecían en la tabla mensual. Definido por el usuario."""
    materiales_en_plan = set(plan["material_codigo"].unique())
    sin_categoria_pero_en_plan = df["categoria"].isna() & df["material_codigo"].isin(materiales_en_plan)
    df.loc[sin_categoria_pero_en_plan, "categoria"] = "MATERIAS PRIMAS"
    return df


@_cacheado
def plan_vs_real_abastecimiento_por_material(mes, categoria=None, centro=None, material=None):
    """Detalle Plan vs Real de abastecimiento por material (toneladas), solo
    Materias Primas y Energía/Combustibles. `categoria`/`centro`/`material`:
    segmentadores propios de esta tabla."""
    plan = plan_abastecimiento_por_material(mes)
    real = ingresos_fisicos_por_material(mes)
    df = plan.merge(real, on=["material_codigo", "centro"], how="outer")
    df["cantidad_plan"] = df["cantidad_plan"].fillna(0.0).astype(float)
    df["cantidad_real"] = df["cantidad_real"].fillna(0.0).astype(float)

    # Solo texto_breve/categoria/familia del catálogo: "centro" ya viene del
    # propio plan/ingreso (destino real), no del centro por defecto del maestro
    # de materiales, así que no se trae esa columna para evitar pisarla.
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "categoria", "familia"]]
    df = df.merge(catalogo, on="material_codigo", how="left")
    df["texto_breve"] = df["texto_breve"].fillna("")
    df["familia"] = df["familia"].fillna("")

    df = _con_categoria_efectiva_planificacion(df, plan)
    df = df[df["categoria"].isin(CATEGORIAS_PLANIFICACION_SOP)]
    if categoria:
        df = df[df["categoria"] == categoria]
    if centro:
        df = df[df["centro"] == centro]
    if material:
        df = df[df["material_codigo"].isin(_lista_materiales(material))]

    df["diferencia"] = df["cantidad_real"] - df["cantidad_plan"]
    df["cumplimiento_pct"] = (df["cantidad_real"] / df["cantidad_plan"] * 100).where(df["cantidad_plan"] != 0)

    columnas = [
        "material_codigo", "texto_breve", "centro", "categoria", "familia",
        "cantidad_plan", "cantidad_real", "diferencia", "cumplimiento_pct",
    ]
    return df[columnas].sort_values("cantidad_plan", ascending=False).reset_index(drop=True)


def evaluacion_diaria_abastecimiento(mes, categoria=None, centro=None, material=None):
    """Evaluación diaria de abastecimiento: el Plan mensual (ya filtrado por
    categoría/centro/material, misma versión más reciente que la evaluación
    mensual) repartido en partes iguales entre los días calendario de `mes`
    (estimado, no un plan diario real) vs los Ingresos Físicos reales de cada
    día, con ambos acumulados, más la cantidad de carros (tickets de báscula)
    recibidos ese día — insumo de la tabla diaria y de la curva S de
    cumplimiento. `carros` usa las mismas reglas de Recepción que `cantidad`
    (Tipo de operación 1/5, Estado Ticket != 'ANU', Centro = Centro Salida):
    cada fila de IngresoFisicoSOP ya es un ticket/camión único (constraint
    (centro, ticket_registro)), así que "carros" es simplemente cuántas filas
    entran ese día bajo el mismo filtro que ya usa `cantidad`. Definido por
    el usuario."""
    plan_total = float(
        plan_vs_real_abastecimiento_por_material(
            mes, categoria=categoria, centro=centro, material=material
        )["cantidad_plan"].sum()
    )

    fecha_inicio, fecha_fin = _limites_mes(mes)
    dias_mes = (fecha_fin - fecha_inicio).days + 1
    plan_diario = plan_total / dias_mes if dias_mes else 0.0

    real = _query(f"""
        SELECT fecha_ingreso AS fecha, material_codigo AS material_codigo, centro AS centro,
               SUM(cantidad) AS cantidad_real, COUNT(*) AS carros
        FROM gestion_ingresofisicosop
        WHERE fecha_ingreso BETWEEN %s AND %s
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION})
        GROUP BY fecha_ingreso, material_codigo, centro
    """, [fecha_inicio, fecha_fin])

    catalogo = catalogo_materiales()[["material_codigo", "categoria"]]
    real = real.merge(catalogo, on="material_codigo", how="left")
    real = _con_categoria_efectiva_planificacion(real, plan_abastecimiento_por_material(mes))
    real = real[real["categoria"].isin(CATEGORIAS_PLANIFICACION_SOP)]
    if categoria:
        real = real[real["categoria"] == categoria]
    if centro:
        real = real[real["centro"] == centro]
    if material:
        real = real[real["material_codigo"].isin(_lista_materiales(material))]

    real_por_dia = (
        real.groupby("fecha", as_index=False).agg(cantidad_real=("cantidad_real", "sum"), carros=("carros", "sum"))
        if not real.empty else real
    )
    mapa_real = {}
    mapa_carros = {}
    if not real_por_dia.empty:
        fechas_idx = pd.to_datetime(real_por_dia["fecha"]).dt.date
        mapa_real = dict(zip(fechas_idx, real_por_dia["cantidad_real"]))
        mapa_carros = dict(zip(fechas_idx, real_por_dia["carros"]))

    filas = []
    acumulado_plan = acumulado_real = 0.0
    fecha = fecha_inicio
    dia_n = 1
    while fecha <= fecha_fin:
        real_dia = float(mapa_real.get(fecha, 0.0))
        carros_dia = int(mapa_carros.get(fecha, 0))
        acumulado_plan += plan_diario
        acumulado_real += real_dia
        filas.append({
            "dia": dia_n, "fecha": fecha,
            "plan_diario": plan_diario, "real_diario": real_dia, "carros": carros_dia,
            "acumulado_plan": acumulado_plan, "acumulado_real": acumulado_real,
            "cumplimiento_pct": round(acumulado_real / acumulado_plan * 100, 1) if acumulado_plan else None,
        })
        fecha += datetime.timedelta(days=1)
        dia_n += 1

    return filas


def resumen_abastecimiento_por_categoria(mes, categoria=None, centro=None):
    """Resumen Plan vs Real por categoría (Materias Primas / Energía y
    Combustibles), para la fila-resumen de Planificación. `categoria`/`centro`:
    mismos segmentadores que plan_vs_real_abastecimiento_por_material."""
    detalle = plan_vs_real_abastecimiento_por_material(mes, categoria=categoria, centro=centro)
    if detalle.empty:
        return []
    resumen = detalle.groupby("categoria", as_index=False).agg(
        cantidad_plan=("cantidad_plan", "sum"),
        cantidad_real=("cantidad_real", "sum"),
    )
    filas = []
    for _, fila in resumen.iterrows():
        plan_total = float(fila["cantidad_plan"])
        real_total = float(fila["cantidad_real"])
        filas.append({
            "categoria": fila["categoria"],
            "cantidad_plan": plan_total,
            "cantidad_real": real_total,
            "diferencia": real_total - plan_total,
            "cumplimiento_pct": round(real_total / plan_total * 100, 1) if plan_total else None,
        })
    return filas


def cumplimiento_mensual_abastecimiento(categoria=None, centro=None):
    """Serie mensual de Plan vs Real de Abastecimiento (todos los meses con
    snapshot cargado), para ver la tendencia de cumplimiento mes a mes en vez
    de un solo mes a la vez. Un mes sin Plan (todavía no llegó ese snapshot) o
    sin Real (mes futuro, todavía no pasa) simplemente no tiene cumplimiento
    definido — se va completando solo a medida que se cargan más archivos de
    Plan de Abastecimiento mes a mes. Pedido por el usuario."""
    hoy_mes = datetime.date.today().replace(day=1)
    filas = []
    for mes in meses_planificacion_disponibles():
        detalle = plan_vs_real_abastecimiento_por_material(mes, categoria=categoria, centro=centro)
        plan_total = float(detalle["cantidad_plan"].sum()) if not detalle.empty else 0.0
        real_total = float(detalle["cantidad_real"].sum()) if not detalle.empty else 0.0
        # Un mes futuro (todavía no llega ni empieza) no tiene 0% de
        # cumplimiento, tiene "todavía no aplica" — 0% sugeriría que ya falló.
        # Un mes sin Plan (pasado, sin snapshot retroactivo) tampoco tiene con
        # qué comparar: Real - 0 no es una "diferencia positiva" real.
        sin_comparacion = mes > hoy_mes or not plan_total
        filas.append({
            "mes": mes,
            "cantidad_plan": plan_total,
            "cantidad_real": real_total,
            "diferencia": None if sin_comparacion else real_total - plan_total,
            "cumplimiento_pct": None if sin_comparacion else round(real_total / plan_total * 100, 1),
        })
    return filas


# Consumo real para Cobertura de Stock de Planificación (Materias Primas/
# Combustibles): el Índice de Rotación de Inventario general usa Z01/Z04/353
# como "consumo" (pensado para Repuestos), pero esas clases casi no tienen
# movimientos para estos materiales en Atocongo/Condorcocha. Acá el consumo
# real es la salida del material hacia el proceso: TODAS las clases de
# movimiento MB51 que empiezan con "2" (201/202 costo, 221 proyecto, 261/262
# orden de producción, 281/282 red...) más Z01/Z04/353, estas últimas
# agregadas siempre aunque no tengan datos para estos materiales/plantas, por
# consistencia con el indicador general. Definido por el usuario.
_FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP = (
    # substr(...) = '2' en vez de LIKE '2%': un "%" literal en el SQL choca con
    # el "%s" de los parámetros cuando Django arma el log de la consulta.
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


def meses_inventario_disponibles():
    """Un dato por mes calendario para el selector de Cobertura de Stock: el
    mes en curso a veces tiene varias fechas de corte de MB5B (recargas
    intermedias), así que se queda con la más reciente de cada mes. Devuelve
    dict {mes (día 1): fecha real de esa foto de inventario}."""
    fechas = fechas_inventario_disponibles()
    por_mes = {}
    for f in fechas:
        clave = f.replace(day=1)
        if clave not in por_mes or f > por_mes[clave]:
            por_mes[clave] = f
    return por_mes


@_cacheado
def cobertura_stock_planificacion(fecha_stock=None, centro=None, material=None):
    """Cobertura de stock de los materiales del Plan de Abastecimiento SOP, por
    material y centro (Atocongo/Condorcocha), en dos esquemas a la vez (días y
    meses). Cobertura = Stock de cierre (a `fecha_stock`, o la más reciente
    cargada si no se indica) ÷ Consumo promedio de los 6 meses calendario que
    terminan en `fecha_stock` (ver _FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP)."""
    columnas = [
        "material_codigo", "texto_breve", "centro", "categoria", "familia",
        "stock_cierre", "consumo_diario", "consumo_mensual", "cobertura_dias", "cobertura_meses",
    ]
    if fecha_stock is None:
        fecha_df = _query("SELECT MAX(fecha) AS fecha FROM gestion_inventariomensual")
        fecha_stock = fecha_df["fecha"].iloc[0] if not fecha_df.empty else None
    if not fecha_stock:
        return pd.DataFrame(columns=columnas)

    catalogo = catalogo_materiales()
    materiales_sop = materiales_planificados_sop()
    if not materiales_sop:
        return pd.DataFrame(columns=columnas)
    ids = ",".join(f"'{m}'" for m in materiales_sop)

    fecha_inicio, fecha_fin = _ventana_6_meses(
        fecha_stock if isinstance(fecha_stock, datetime.date) else datetime.date.fromisoformat(str(fecha_stock)[:10])
    )
    dias_ventana = (fecha_fin - fecha_inicio).days + 1

    stock = _query(f"""
        SELECT material_codigo AS material_codigo, area_valoracion AS centro,
               SUM(stock_cierre) AS stock_cierre
        FROM gestion_inventariomensual
        WHERE material_codigo IN ({ids}) AND area_valoracion IN ('U001', 'U002') AND fecha = %s
        GROUP BY material_codigo, area_valoracion
    """, [fecha_stock])

    # "+centro" (y no "centro") en las dos consultas de consumo de acá abajo: el
    # "+" solo le quita a SQLite la opción de usar el índice por centro, que
    # elegía por su cuenta y lo hacía recorrer casi todo MB51 de U001/U002
    # (~3.5 M filas); así usa el de (material, fecha), que con ~23 materiales
    # del Plan SOP es muchísimo más selectivo (99 s -> 5 s para 3 años). Mismo
    # resultado.
    consumo = _query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro,
               -SUM(cantidad) AS consumo_total
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({ids}) AND +centro IN ('U001', 'U002')
              AND {_FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP}
              AND fecha_contabilizacion BETWEEN %s AND %s
        GROUP BY material_codigo, centro
    """, [fecha_inicio, fecha_fin])

    extras = {}
    for origen, (mat_real, centro_real) in _MATERIALES_CONSUMO_VIA_OTRO.items():
        extra = _query(f"""
            SELECT -SUM(cantidad) AS consumo_total
            FROM gestion_movimientomaterial
            WHERE material_codigo = %s AND +centro = %s
                  AND {_FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP}
                  AND fecha_contabilizacion BETWEEN %s AND %s
        """, [mat_real, centro_real, fecha_inicio, fecha_fin])
        extras[origen] = float(extra["consumo_total"].iloc[0] or 0.0) if not extra.empty else 0.0
    consumo = _sumar_consumo_via_otro(consumo, extras)

    df = _armar_cobertura(stock, consumo, dias_ventana, catalogo)
    if centro:
        df = df[df["centro"] == centro]
    if material:
        df = df[df["material_codigo"].isin(_lista_materiales(material))]
    return df.reset_index(drop=True)


def cobertura_stock_planificacion_por_mes(fechas_stock):
    """Lo mismo que cobertura_stock_planificacion(fecha_stock=f) para varias
    fechas a la vez, devuelto como {fecha_stock: DataFrame}. Para el HTML
    estático (exportar_planificacion_html), que necesita la cobertura de todos
    los meses de inventario: llamando a la función de un mes por cada uno, cada
    llamada volvía a recorrer 6 meses de MB51 (~33 meses x 6 = el mismo
    movimiento leído hasta 6 veces, ~5 min). Acá el consumo se agrupa por mes
    calendario en UNA consulta y cada ventana de 6 meses se suma en memoria —
    da el mismo resultado porque la ventana siempre abarca meses completos.
    Ver el comentario de "+centro" en cobertura_stock_planificacion."""
    fechas = sorted({
        f if isinstance(f, datetime.date) else datetime.date.fromisoformat(str(f)[:10]) for f in fechas_stock
    })
    if not fechas:
        return {}
    catalogo = catalogo_materiales()
    materiales_sop = materiales_planificados_sop()
    if not materiales_sop:
        return {}
    sop = set(materiales_sop)
    ids_sop = ",".join(f"'{m}'" for m in materiales_sop)
    reales = {mat for mat, _centro in _MATERIALES_CONSUMO_VIA_OTRO.values()}
    ids_consumo = ",".join(f"'{m}'" for m in sorted(sop | reales))

    stock_todos = _query(f"""
        SELECT fecha AS fecha, material_codigo AS material_codigo, area_valoracion AS centro,
               SUM(stock_cierre) AS stock_cierre
        FROM gestion_inventariomensual
        WHERE material_codigo IN ({ids_sop}) AND area_valoracion IN ('U001', 'U002')
              AND fecha IN ({",".join(["%s"] * len(fechas))})
        GROUP BY fecha, material_codigo, area_valoracion
    """, fechas)
    stock_todos["fecha"] = stock_todos["fecha"].astype(str).str[:10]

    consumo_mes = _query(f"""
        SELECT substr(fecha_contabilizacion, 1, 7) AS mes, material_codigo AS material_codigo,
               centro AS centro, -SUM(cantidad) AS consumo_total
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({ids_consumo}) AND +centro IN ('U001', 'U002')
              AND {_FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP}
              AND fecha_contabilizacion BETWEEN %s AND %s
        GROUP BY mes, material_codigo, centro
    """, [_ventana_6_meses(fechas[0])[0], _ventana_6_meses(fechas[-1])[1]])

    resultado = {}
    for fecha_stock in fechas:
        fecha_inicio, fecha_fin = _ventana_6_meses(fecha_stock)
        meses_ventana = {
            f"{(fecha_inicio.year * 12 + fecha_inicio.month - 1 + i) // 12:04d}-"
            f"{(fecha_inicio.month - 1 + i) % 12 + 1:02d}"
            for i in range(6)
        }
        en_ventana = consumo_mes[consumo_mes["mes"].isin(meses_ventana)]
        consumo = (
            en_ventana[en_ventana["material_codigo"].isin(sop)]
            .groupby(["material_codigo", "centro"], as_index=False)["consumo_total"].sum()
        )
        extras = {
            origen: float(en_ventana.loc[
                (en_ventana["material_codigo"] == mat_real) & (en_ventana["centro"] == centro_real),
                "consumo_total",
            ].sum())
            for origen, (mat_real, centro_real) in _MATERIALES_CONSUMO_VIA_OTRO.items()
        }
        consumo = _sumar_consumo_via_otro(consumo, extras)
        stock = stock_todos[stock_todos["fecha"] == fecha_stock.isoformat()].drop(columns="fecha")
        resultado[fecha_stock] = _armar_cobertura(
            stock, consumo, (fecha_fin - fecha_inicio).days + 1, catalogo
        ).reset_index(drop=True)
    return resultado


def _sumar_consumo_via_otro(consumo, extras):
    """Suma a cada material de _MATERIALES_CONSUMO_VIA_OTRO el consumo del
    material por el que realmente sale al proceso (`extras`: {(material,
    centro): consumo}), agregando la fila si no tenía consumo propio."""
    for (mat_origen, centro_origen), valor_extra in extras.items():
        if valor_extra == 0.0:
            continue
        mascara = (consumo["material_codigo"] == mat_origen) & (consumo["centro"] == centro_origen)
        if mascara.any():
            consumo.loc[mascara, "consumo_total"] += valor_extra
        else:
            fila_nueva = pd.DataFrame([{"material_codigo": mat_origen, "centro": centro_origen, "consumo_total": valor_extra}])
            consumo = pd.concat([consumo, fila_nueva], ignore_index=True)
    return consumo


def _armar_cobertura(stock, consumo, dias_ventana, catalogo):
    """Parte común de cobertura_stock_planificacion y su versión por mes: con
    el stock y el consumo total de la ventana por material/centro, calcula las
    coberturas y agrega descripción/categoría/familia del maestro."""
    columnas = [
        "material_codigo", "texto_breve", "centro", "categoria", "familia",
        "stock_cierre", "consumo_diario", "consumo_mensual", "cobertura_dias", "cobertura_meses",
    ]
    df = stock.merge(consumo, on=["material_codigo", "centro"], how="outer")
    df["stock_cierre"] = df["stock_cierre"].fillna(0.0).astype(float)
    df["consumo_total"] = df["consumo_total"].fillna(0.0).astype(float)
    df["consumo_diario"] = df["consumo_total"] / dias_ventana
    df["cobertura_dias"] = (df["stock_cierre"] / df["consumo_diario"]).where(df["consumo_diario"] > 0)
    # Mismo cálculo en meses (igual criterio que rotacion_inventario_por_material:
    # promedio de los 6 meses calendario), para tener las dos lecturas juntas
    # en la tabla. Pedido por el usuario.
    df["consumo_mensual"] = df["consumo_total"] / 6.0
    df["cobertura_meses"] = (df["stock_cierre"] / df["consumo_mensual"]).where(df["consumo_mensual"] > 0)

    df = df.merge(catalogo[["material_codigo", "texto_breve", "categoria", "familia"]], on="material_codigo", how="left")
    # El material_codigo ya viene acotado al Plan de Abastecimiento SOP (arriba),
    # así que no hace falta filtrar por categoría — de hecho no se puede: el
    # Clinker (14-00000037) no tiene categoría en el maestro (es un intermedio
    # de producción, no se compra con grupo de artículo) y un filtro por
    # categoría lo excluiría en silencio, igual que pasó una vez en Plan vs
    # Real. Mismo fallback que allá. Definido por el usuario.
    df.loc[df["categoria"].isna(), "categoria"] = "MATERIAS PRIMAS"
    # Mismo caso: sin familia en el maestro. A diferencia de categoría, acá no
    # hay un valor de reemplazo razonable — se deja en blanco (NaN se vería
    # como "nan" literal en la plantilla si no se limpia).
    df["familia"] = df["familia"].fillna("")
    df["texto_breve"] = df["texto_breve"].fillna("")
    # Sin consumo en la ventana de 6 meses = sin señal real para calcular
    # cobertura (aunque tenga stock): no interesa en esta tabla. Pedido por el
    # usuario (ej. Puzolana de Terceros/Escoria de Altos Hornos en U002).
    df = df[df["consumo_total"] != 0]
    return df[columnas].sort_values("cobertura_dias", ascending=True, na_position="last")


# ---------------------------------------------------------------------------
# Evolución de inventario proyectada (S&OP). Para el S&OP de un mes (p.ej.
# Setiembre 2026), el punto de partida es el inventario real de cierre del mes
# anterior (MB5B de Agosto 2026), y cada mes proyectado se calcula como:
#     Stock final = Stock inicial + Abastecimiento S&OP − Consumo S&OP
# encadenando el stock final de un mes como inicial del siguiente, hasta el
# último mes que traigan los planes (hoy, Diciembre 2027). Definido por el
# usuario.
# ---------------------------------------------------------------------------

def snapshots_sop_disponibles():
    """Meses de emisión del S&OP con Plan de Abastecimiento y/o de Consumos."""
    df = _query("""
        SELECT mes_snapshot AS mes FROM gestion_planabastecimientosop
        UNION
        SELECT mes_snapshot AS mes FROM gestion_planconsumosop
        ORDER BY mes
    """)
    return [datetime.date.fromisoformat(str(m)[:10]) for m in df["mes"]]


def fecha_inventario_inicial_sop(mes_snapshot):
    """Fecha del inventario (MB5B) de cierre del mes anterior a `mes_snapshot`,
    o None si ese cierre todavía no está cargado."""
    mes_anterior = (mes_snapshot - datetime.timedelta(days=1)).replace(day=1)
    return meses_inventario_disponibles().get(mes_anterior)


def _plan_sop_por_mes(tabla, columna, mes_snapshot):
    return _query(f"""
        SELECT material_codigo AS material_codigo, centro_destino AS centro,
               mes_planificado AS mes, SUM({columna}) AS cantidad,
               MAX(material_descripcion) AS descripcion
        FROM {tabla}
        WHERE mes_snapshot = %s
        GROUP BY material_codigo, centro_destino, mes_planificado
    """, [mes_snapshot])


# Traslados entre centros: el Carbón Importado llega por barco a Atocongo y lo
# que el S&OP planifica "abastecer" a Condorcocha no es una compra aparte, es
# un traslado desde el stock de Atocongo. Sin esto, cada embarque se contaba
# dos veces (en Atocongo y otra vez en Condorcocha) y Atocongo se veía
# acumulando carbón sin fin. {(material, centro que recibe): centro que envía}.
# Definido por el usuario.
_TRASLADOS_SOP = {
    ("06-00000003", "U002"): "U001",
}

# Lead time (meses) para cumplir la cuota de abastecimiento de un mes: lo que
# está dentro de ese plazo ya está comprometido (en tránsito / en ejecución) y
# no se puede cambiar. Todos los materiales tienen 1 mes; los importados más.
# Definido por el usuario.
LEAD_TIME_SOP_DEFECTO = 1
LEAD_TIME_SOP = {
    "06-00000003": 3,  # Carbón importado
    "07-00001301": 4,  # Escoria japonesa (altos hornos)
}


def lead_time_sop(material):
    return LEAD_TIME_SOP.get(material, LEAD_TIME_SOP_DEFECTO)


def _sumar_meses(fecha, meses):
    total = fecha.year * 12 + fecha.month - 1 + meses
    return datetime.date(total // 12, total % 12 + 1, 1)


@_cacheado
def proyeccion_inventario_sop(mes_snapshot):
    """Una fila por material/centro/mes proyectado: stock_inicial,
    abastecimiento, consumo y stock_final (toneladas). Materiales: los que
    trae el Plan de Abastecimiento y/o el de Consumos de ese S&OP, con el
    inventario inicial en 0 si no tenían stock de cierre (y siempre en 0 para
    los que no tienen Cod. SAP, p.ej. CALCEM: ver PREFIJO_SIN_CODIGO_SOP)."""
    columnas = [
        "material_codigo", "texto_breve", "centro", "categoria", "familia",
        "mes", "stock_inicial", "abastecimiento", "consumo", "traslado", "traslado_recibido", "stock_final",
        "stock_minimo", "stock_maximo", "estado",
    ]
    abast = _plan_sop_por_mes("gestion_planabastecimientosop", "cantidad_plan", mes_snapshot)
    cons = _plan_sop_por_mes("gestion_planconsumosop", "cantidad_consumo", mes_snapshot)
    if abast.empty and cons.empty:
        return pd.DataFrame(columns=columnas)

    for df in (abast, cons):
        df["mes"] = pd.to_datetime(df["mes"]).dt.date
        df["cantidad"] = df["cantidad"].fillna(0.0).astype(float)

    # Carbón Terceros (06-00000005/U002) y su Chancado (14-00001112/U002, ver
    # _MATERIALES_CONSUMO_VIA_OTRO) son el mismo carbón: se proyectan como UNA
    # sola fila, la del material que se compra (06-00000005), con:
    #   - stock inicial = stock de los dos,
    #   - abastecimiento = el del 06-00000005,
    #   - consumo = SOLO el del chancado (el que realmente va al proceso; el
    #     Excel de Consumos trae el mismo número en las dos filas, y sumar o
    #     mezclar ambos confunde).
    # Definido por el usuario.
    def _es(df, clave):
        return (df["material_codigo"] == clave[0]) & (df["centro"] == clave[1])

    fusiones = {}
    for origen, real in _MATERIALES_CONSUMO_VIA_OTRO.items():
        aparece_real = _es(cons, real).any() or _es(abast, real).any()
        aparece_origen = _es(cons, origen).any() or _es(abast, origen).any()
        if not (aparece_real and aparece_origen):
            continue
        fusiones[real] = origen
        if _es(cons, real).any():
            cons = cons[~_es(cons, origen)]
        for df in (abast, cons):
            df.loc[_es(df, real), "material_codigo"] = origen[0]
            df.loc[_es(df, (origen[0], real[1])), "centro"] = origen[1]
    abast = abast.groupby(["material_codigo", "centro", "mes"], as_index=False).agg(
        cantidad=("cantidad", "sum"), descripcion=("descripcion", "last"))
    cons = cons.groupby(["material_codigo", "centro", "mes"], as_index=False).agg(
        cantidad=("cantidad", "sum"), descripcion=("descripcion", "last"))
    meses = sorted(m for m in set(abast["mes"]) | set(cons["mes"]) if m >= mes_snapshot)

    claves = sorted(
        set(zip(abast["material_codigo"], abast["centro"])) | set(zip(cons["material_codigo"], cons["centro"]))
    )
    codigos_stock = {m for m, _ in claves} | {real[0] for real in fusiones}
    ids = ",".join(f"'{m}'" for m in sorted(codigos_stock))
    fecha_inicial = fecha_inventario_inicial_sop(mes_snapshot)
    stock = {}
    if fecha_inicial:
        df_stock = _query(f"""
            SELECT material_codigo AS material_codigo, area_valoracion AS centro,
                   SUM(stock_cierre) AS stock
            FROM gestion_inventariomensual
            WHERE fecha = %s AND material_codigo IN ({ids}) AND area_valoracion IN ('U001', 'U002')
            GROUP BY material_codigo, area_valoracion
        """, [fecha_inicial])
        stock = {(r.material_codigo, r.centro): float(r.stock or 0.0) for r in df_stock.itertuples()}
        for real, origen in fusiones.items():
            stock[origen] = stock.get(origen, 0.0) + stock.pop(real, 0.0)

    a = {(r.material_codigo, r.centro, r.mes): r.cantidad for r in abast.itertuples()}
    c = {(r.material_codigo, r.centro, r.mes): r.cantidad for r in cons.itertuples()}
    descripcion_plan = {}
    for df in (cons, abast):  # la de Abastecimiento pisa a la de Consumos
        descripcion_plan.update({(r.material_codigo, r.centro): r.descripcion for r in df.itertuples()})

    # Salidas por traslado: lo que recibe un centro por traslado sale del stock
    # del centro que lo envía, en el mismo mes (ver _TRASLADOS_SOP).
    t = {}
    for (material, recibe), envia in _TRASLADOS_SOP.items():
        for (mat, centro, mes), cantidad in a.items():
            if mat == material and centro == recibe:
                t[(material, envia, mes)] = t.get((material, envia, mes), 0.0) + cantidad
                claves = sorted(set(claves) | {(material, envia)})

    filas = []
    for material, centro in claves:
        saldo = stock.get((material, centro), 0.0)
        for mes in meses:
            entra = a.get((material, centro, mes), 0.0)
            sale = c.get((material, centro, mes), 0.0)
            traslado = t.get((material, centro, mes), 0.0)
            final = saldo + entra - sale - traslado
            filas.append({
                "material_codigo": material, "centro": centro, "mes": mes,
                "stock_inicial": saldo, "abastecimiento": entra, "consumo": sale, "traslado": traslado,
                # Parte del abastecimiento que en realidad es un traslado desde
                # otro centro: al sumar los dos centros juntos, se descuenta de
                # abastecimiento y de traslados (es un movimiento interno).
                "traslado_recibido": entra if (material, centro) in _TRASLADOS_SOP else 0.0,
                "stock_final": final, "descripcion_plan": descripcion_plan.get((material, centro), ""),
            })
            saldo = final

    df = pd.DataFrame(filas)
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "categoria", "familia"]]
    df = df.merge(catalogo, on="material_codigo", how="left")
    df["texto_breve"] = df["texto_breve"].where(df["texto_breve"].notna() & (df["texto_breve"] != ""), df["descripcion_plan"])
    # Mismo criterio que el resto de Planificación: todo lo que trae el S&OP
    # es de este dominio; sin categoría en el maestro (Clinker) = MATERIAS PRIMAS.
    df.loc[df["categoria"].isna(), "categoria"] = "MATERIAS PRIMAS"
    df["familia"] = df["familia"].fillna("")

    minmax = minmax_sop(mes_snapshot)
    if minmax.empty:
        df["stock_minimo"] = float("nan")
        df["stock_maximo"] = float("nan")
    else:
        df = df.merge(minmax, on=["material_codigo", "centro", "mes"], how="left")
    df["estado"] = [
        _estado_stock(final, minimo, maximo)
        for final, minimo, maximo in zip(df["stock_final"], df["stock_minimo"], df["stock_maximo"])
    ]
    return df[columnas].sort_values(["centro", "material_codigo", "mes"]).reset_index(drop=True)


ESTADOS_STOCK = {
    "quiebre": "Quiebre (stock negativo)",
    "bajo_minimo": "Bajo mínimo",
    "en_rango": "En rango",
    "sobre_maximo": "Sobre máximo",
    "sin_parametro": "Sin mín/máx",
}


def _estado_stock(final, minimo, maximo):
    if final < 0:
        return "quiebre"
    if minimo == minimo and minimo is not None and final < minimo:
        return "bajo_minimo"
    if maximo == maximo and maximo is not None and final > maximo:
        return "sobre_maximo"
    if (minimo == minimo and minimo is not None) or (maximo == maximo and maximo is not None):
        return "en_rango"
    return "sin_parametro"


def minmax_sop(mes_snapshot):
    """Stock mínimo/máximo por material/centro/mes para el S&OP de
    `mes_snapshot`: el archivo de ese mes de emisión, o el más reciente
    anterior si todavía no se cargó uno nuevo. Un mes vacío repite el último
    valor definido del material (solo se escribe el mes en que cambia).
    Definido por el usuario."""
    df = _query("""
        SELECT material_codigo AS material_codigo, centro_destino AS centro, mes_planificado AS mes,
               stock_minimo AS stock_minimo, stock_maximo AS stock_maximo
        FROM gestion_stockminmaxsop
        WHERE mes_snapshot = (
            SELECT COALESCE(
                (SELECT MAX(mes_snapshot) FROM gestion_stockminmaxsop WHERE mes_snapshot <= %s),
                (SELECT MIN(mes_snapshot) FROM gestion_stockminmaxsop)
            )
        )
        ORDER BY material_codigo, centro_destino, mes_planificado
    """, [mes_snapshot])
    if df.empty:
        return df
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    for columna in ("stock_minimo", "stock_maximo"):
        df[columna] = pd.to_numeric(df[columna], errors="coerce").astype(float)
        df[columna] = df.groupby(["material_codigo", "centro"])[columna].ffill()
    return df


# Clasificación del diagnóstico de mínimos/máximos, de más a menos urgente.
DIAGNOSTICOS_MINMAX = {
    "quiebre": "Riesgo de quiebre",
    "bajo_minimo": "Cae bajo el mínimo",
    "sobrecubierto": "Sobrecubierto",
    "sobre_maximo_puntual": "Sobre máximo en algunos meses",
    "equilibrado": "Equilibrado",
    "sin_parametro": "Sin mín/máx",
}
# Sobrecubierto = sobre el máximo en al menos la mitad de los meses proyectados.
_UMBRAL_SOBRECUBIERTO = 0.5


@_cacheado
def diagnostico_minmax_sop(mes_snapshot):
    """Una fila por material/centro con el diagnóstico de su stock proyectado
    frente a su mínimo/máximo en todo el horizonte del S&OP. Considera el lead
    time (lead_time_sop): la cuota de los meses dentro de ese plazo ya está
    comprometida y no se puede cambiar, así que:
    - primer_mes_modificable = mes del S&OP + lead time;
    - faltante: abastecimiento adicional (a más tardar en el primer mes
      modificable) para no bajar del mínimo desde ahí = max(mínimo − stock);
    - deficit_en_plazo: lo que falta bajo el mínimo en meses que ya no alcanza
      el lead time (solo se puede mitigar con medidas extraordinarias);
    - pedir_a_mas_tardar: para el primer mes bajo el mínimo que sí alcanza a
      corregirse, mes límite para colocar el pedido (ese mes − lead time);
    - recorte_posible: abastecimiento que se podría dejar de traer desde el
      primer mes modificable sin bajar del mínimo en ningún mes, y sin
      recortar más de lo que hay planificado desde ese mes;
    - exceso_maximo: el mayor exceso sobre el máximo (el máximo es un
      objetivo, no un límite de almacén: sobre el máximo = capital
      inmovilizado). Pedido por el usuario."""
    df = proyeccion_inventario_sop(mes_snapshot)
    if df.empty:
        return pd.DataFrame()
    filas = []
    for (material, centro), g in df.groupby(["material_codigo", "centro"]):
        g = g.sort_values("mes").reset_index(drop=True)
        meses = len(g)
        conteo = g["estado"].value_counts()
        tiene_min = g["stock_minimo"].notna().any()
        tiene_max = g["stock_maximo"].notna().any()
        lead_time = lead_time_sop(material)
        primer_modificable = _sumar_meses(mes_snapshot, lead_time)
        modificable = g["mes"] >= primer_modificable
        holgura_min = (g["stock_final"] - g["stock_minimo"]) if tiene_min else None
        exceso_max = (g["stock_final"] - g["stock_maximo"]) if tiene_max else None
        bajo = g[g["estado"].isin(["bajo_minimo", "quiebre"])]
        meses_sobre = int(conteo.get("sobre_maximo", 0))

        faltante = deficit_en_plazo = recorte = 0.0
        pedir = None
        if tiene_min:
            h_mod, h_plazo = holgura_min[modificable], holgura_min[~modificable]
            if not h_plazo.empty and h_plazo.min() < 0:
                deficit_en_plazo = float(-h_plazo.min())
            if not h_mod.empty and h_mod.min() < 0:
                faltante = float(-h_mod.min())
                primer_bajo = g.loc[modificable & (holgura_min < 0), "mes"].iloc[0]
                pedir = _sumar_meses(primer_bajo, -lead_time)
            if not h_mod.empty and h_mod.min() > 0:
                recorte = min(float(h_mod.min()), float(g.loc[modificable, "abastecimiento"].sum()))

        if not (tiene_min or tiene_max):
            clave = "sin_parametro"
        elif conteo.get("quiebre", 0):
            clave = "quiebre"
        elif conteo.get("bajo_minimo", 0):
            clave = "bajo_minimo"
        elif meses_sobre >= meses * _UMBRAL_SOBRECUBIERTO:
            clave = "sobrecubierto"
        elif meses_sobre:
            clave = "sobre_maximo_puntual"
        else:
            clave = "equilibrado"

        filas.append({
            "material_codigo": material, "texto_breve": g["texto_breve"].iloc[0], "centro": centro,
            "categoria": g["categoria"].iloc[0],
            "stock_minimo": g["stock_minimo"].iloc[0], "stock_maximo": g["stock_maximo"].iloc[0],
            "stock_hoy": float(g["stock_inicial"].iloc[0]),
            "stock_promedio": float(g["stock_final"].mean()),
            "stock_final": float(g["stock_final"].iloc[-1]),
            "meses": meses,
            "meses_bajo_minimo": int(len(bajo)),
            "meses_en_rango": int(conteo.get("en_rango", 0)),
            "meses_sobre_maximo": meses_sobre,
            "primer_mes_bajo_minimo": bajo["mes"].iloc[0] if not bajo.empty else None,
            "lead_time": lead_time,
            "primer_mes_modificable": primer_modificable,
            "pedir_a_mas_tardar": pedir,
            "exceso_maximo": float(exceso_max.max()) if tiene_max and exceso_max.max() > 0 else 0.0,
            "recorte_posible": recorte,
            "faltante": faltante,
            "deficit_en_plazo": deficit_en_plazo,
            "diagnostico": clave,
            "diagnostico_texto": DIAGNOSTICOS_MINMAX[clave],
        })
    orden = {clave: i for i, clave in enumerate(DIAGNOSTICOS_MINMAX)}
    resultado = pd.DataFrame(filas)
    resultado["_orden"] = resultado["diagnostico"].map(orden)
    return resultado.sort_values(["_orden", "exceso_maximo", "faltante"], ascending=[True, False, False]).drop(columns="_orden").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Precios de entrada (S&OP): precio de compra por tonelada de los materiales
# del Plan de Abastecimiento, desde MB51. Pedido por el usuario. Reglas
# (descubiertas inspeccionando MB51):
#   - Solo clases 101 (entrada de mercancía por pedido) y 102 (su anulación)
#     CON proveedor: esa es la compra a un tercero, valorizada al precio del
#     pedido. Un 101 sin proveedor es la recepción de un traslado entre
#     centros (entra con importe 0, el valor viaja en el 641) o de una orden
#     de producción (calizas de cantera propia, Clinker: costo estándar, no
#     un precio de compra) — no cuentan.
#   - Los importados (Carbón Importado, Escoria japonesa) se compran en el
#     centro U004 (importación) y de ahí se trasladan a planta: su precio es
#     el del 101 en U004, un barco por mes de llegada.
#   - Precio del mes = importe total ÷ toneladas totales (promedio ponderado
#     por cantidad), en soles (importe en moneda local).
# ---------------------------------------------------------------------------
_CLASES_ENTRADA_COMPRA = "('101', '102')"
CENTRO_IMPORTACION = "U004"


@_cacheado
def precios_entrada_sop():
    """Una fila por material/centro/mes con compras: toneladas, importe (S/),
    precio (S/ por T), entradas y proveedores distintos, más texto_breve y
    categoría. Materiales: los del Plan de Abastecimiento S&OP."""
    columnas = ["material_codigo", "texto_breve", "centro", "categoria", "mes",
                "toneladas", "importe", "precio", "entradas", "proveedores"]
    materiales = materiales_planificados_sop()
    if not materiales:
        return pd.DataFrame(columns=columnas)
    # "+clase_movimiento": que SQLite use el índice por material (muy
    # selectivo, ~25 materiales) y no el de clase de movimiento (millones de
    # 101 de todos los materiales). Mismo resultado.
    df = _query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro,
               substr(fecha_contabilizacion, 1, 7) || '-01' AS mes,
               SUM(cantidad) AS toneladas, SUM(importe_moneda_local) AS importe,
               SUM(CASE WHEN clase_movimiento = '101' THEN 1 ELSE 0 END) AS entradas,
               COUNT(DISTINCT proveedor) AS proveedores
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({",".join(f"'{m}'" for m in materiales)})
              AND +clase_movimiento IN {_CLASES_ENTRADA_COMPRA} AND proveedor <> ''
              AND fecha_contabilizacion IS NOT NULL
        GROUP BY material_codigo, centro, mes
    """)
    if df.empty:
        return pd.DataFrame(columns=columnas)
    df["toneladas"] = df["toneladas"].astype(float)
    df["importe"] = df["importe"].astype(float)
    # Un mes con la entrada anulada por completo (101 + 102) no tiene precio.
    df = df[(df["toneladas"] > 0.5) & (df["importe"] > 0)].copy()
    df["precio"] = df["importe"] / df["toneladas"]
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "categoria"]]
    df = df.merge(catalogo, on="material_codigo", how="left")
    df.loc[df["categoria"].isna(), "categoria"] = "MATERIAS PRIMAS"
    df["texto_breve"] = df["texto_breve"].fillna("")
    return df[columnas].sort_values(["material_codigo", "centro", "mes"]).reset_index(drop=True)


@_cacheado
def precios_entrada_materiales():
    """Lo mismo que precios_entrada_sop, pero de TODOS los materiales con
    compra (101/102 con proveedor), para la pestaña Precios e IPU de cada
    categoría. Pedido por el usuario. La columna "toneladas" es la cantidad en
    la UMB del material (KG, UND, M...; en T solo para materias primas y
    combustibles), con el mismo nombre para reusar inflacion_precios e
    ipu_por_mes; "precio" queda en S/ por UMB. Sin filtro de material, la
    consulta recorre todos los 101/102 (~11 s en frío), por eso va en caché."""
    columnas = ["material_codigo", "texto_breve", "unidad_medida_base", "centro", "categoria", "familia", "mes",
                "toneladas", "importe", "precio", "entradas", "proveedores"]
    df = _query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro,
               substr(fecha_contabilizacion, 1, 7) || '-01' AS mes,
               SUM(cantidad) AS toneladas, SUM(importe_moneda_local) AS importe,
               SUM(CASE WHEN clase_movimiento = '101' THEN 1 ELSE 0 END) AS entradas,
               COUNT(DISTINCT proveedor) AS proveedores
        FROM gestion_movimientomaterial
        WHERE clase_movimiento IN {_CLASES_ENTRADA_COMPRA} AND proveedor <> '' AND material_codigo <> ''
              AND fecha_contabilizacion IS NOT NULL
        GROUP BY material_codigo, centro, mes
    """)
    if df.empty:
        return pd.DataFrame(columns=columnas)
    df["toneladas"] = df["toneladas"].astype(float)
    df["importe"] = df["importe"].astype(float)
    df = df[(df["toneladas"] > 0) & (df["importe"] > 0)].copy()
    df["precio"] = df["importe"] / df["toneladas"]
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "unidad_medida_base", "categoria", "familia"]]
    df = df.merge(catalogo, on="material_codigo", how="inner")
    df["texto_breve"] = df["texto_breve"].fillna("")
    df["unidad_medida_base"] = df["unidad_medida_base"].fillna("")
    df["familia"] = df["familia"].fillna("")
    return df[columnas].sort_values(["material_codigo", "centro", "mes"]).reset_index(drop=True)


def resumen_precios_entrada(df):
    """Una fila por material/centro de `df` (salida de precios_entrada_sop,
    ya filtrada): último precio y su mes, variación contra la compra anterior
    y contra el mismo mes del año anterior (o la compra más cercana antes de
    esa fecha), precio mínimo/máximo/promedio ponderado del período y
    toneladas compradas."""
    filas = []
    for (material, centro), g in df.groupby(["material_codigo", "centro"]):
        g = g.sort_values("mes")
        ultimo = g.iloc[-1]
        anterior = g.iloc[-2] if len(g) > 1 else None
        hace_un_anio = g[g["mes"] <= _sumar_meses(ultimo["mes"], -12)]
        base_anual = hace_un_anio.iloc[-1] if not hace_un_anio.empty else None
        filas.append({
            "material_codigo": material, "texto_breve": ultimo["texto_breve"], "centro": centro,
            "categoria": ultimo["categoria"],
            "mes_ultimo": ultimo["mes"], "precio_ultimo": float(ultimo["precio"]),
            "mes_anterior": anterior["mes"] if anterior is not None else None,
            "variacion_anterior": (float(ultimo["precio"] / anterior["precio"] - 1) if anterior is not None else None),
            "mes_base_anual": base_anual["mes"] if base_anual is not None else None,
            "variacion_anual": (float(ultimo["precio"] / base_anual["precio"] - 1) if base_anual is not None else None),
            "precio_minimo": float(g["precio"].min()), "precio_maximo": float(g["precio"].max()),
            "precio_promedio": float(g["importe"].sum() / g["toneladas"].sum()),
            "toneladas": float(g["toneladas"].sum()), "importe": float(g["importe"].sum()),
            "meses_con_compra": int(len(g)),
            "serie": [(r.mes, float(r.precio)) for r in g.itertuples()],
        })
    return filas


# ---------------------------------------------------------------------------
# Inflación de precios (S&OP). Pedido por el usuario: precio de compra del
# mes T contra el de un mes de referencia — interanual: el mismo mes un año
# antes (T−12); mensual: el mes anterior (T−1). Si el mes de referencia no
# tuvo compra, contra el último precio registrado ANTES de esa fecha (T−13,
# T−14... / T−2, T−3...). Sin ninguna compra hasta ese mes, T no se compara.
# Bolsa = (precio T − precio base) × toneladas de T: el impacto en soles de
# la variación, ponderado siempre con la cantidad del período más reciente
# de la comparación (2025 vs 2024 con las toneladas de 2025, 2026 vs 2025
# con las de 2026).
# ---------------------------------------------------------------------------

# Una comparación con el precio más que duplicado o reducido a menos de la
# mitad no es inflación sino un dato anómalo de MB51 (p.ej. Ene-24: Sílices
# a S/ 16.60 y Puzolana de Terceros a S/ 17.50 por T, cuando su precio de
# todo el año es ~70 y ~130): se muestra marcada pero no suma en la bolsa ni
# en la selección ponderada.
_RAZON_ATIPICA = 2.0


def inflacion_precios(df, desfase=12):
    """Una fila por material/centro/mes con compra de `df` (salida de
    precios_entrada_sop, ya filtrada) que tenga base de comparación: precio,
    toneladas e importe del mes, mes_referencia (T − `desfase` meses),
    mes_base y precio_base usados, base_sustituida (True si el mes de
    referencia no tuvo compra y se tomó el último precio anterior),
    variacion (precio ÷ precio_base − 1), bolsa (S/) y atipica (ver
    _RAZON_ATIPICA)."""
    columnas = ["material_codigo", "texto_breve", "centro", "categoria", "mes", "precio", "toneladas", "importe",
                "mes_referencia", "mes_base", "precio_base", "base_sustituida", "variacion", "bolsa", "atipica"]
    if df.empty:
        return pd.DataFrame(columns=columnas)
    actual = df.assign(mes_referencia=[_sumar_meses(m, -desfase) for m in df["mes"]])
    actual["_clave"] = pd.to_datetime(actual["mes_referencia"])
    bases = df[["material_codigo", "centro", "mes", "precio"]].rename(columns={"mes": "mes_base", "precio": "precio_base"})
    bases["_clave"] = pd.to_datetime(bases["mes_base"])
    # merge_asof "backward": el último mes_base <= mes_referencia del mismo
    # material y centro (exacto si T−12 tuvo compra).
    unido = pd.merge_asof(actual.sort_values("_clave"), bases.sort_values("_clave"), on="_clave",
                          by=["material_codigo", "centro"], direction="backward")
    unido = unido[unido["precio_base"].notna()].copy()
    unido["base_sustituida"] = unido["mes_base"] != unido["mes_referencia"]
    unido["variacion"] = unido["precio"] / unido["precio_base"] - 1
    unido["bolsa"] = (unido["precio"] - unido["precio_base"]) * unido["toneladas"]
    razon = unido["precio"] / unido["precio_base"]
    unido["atipica"] = (razon > _RAZON_ATIPICA) | (razon < 1 / _RAZON_ATIPICA)
    return unido[columnas].sort_values(["material_codigo", "centro", "mes"]).reset_index(drop=True)


def inflacion_canasta(df):
    """Inflación ponderada de un conjunto de materiales/meses (`df`: salida
    de inflacion_precios): lo pagado ÷ lo que habrían costado esas mismas
    toneladas al precio base − 1 (ponderado por las toneladas compradas en
    cada mes T). Devuelve variacion, bolsa (S/: lo pagado − lo que habría
    costado al precio base), toneladas, importe y materiales. Las
    comparaciones atípicas no cuentan."""
    df = df[~df["atipica"]]
    base = float((df["precio_base"] * df["toneladas"]).sum())
    importe = float(df["importe"].sum())
    return {
        "variacion": importe / base - 1 if base else None, "bolsa": importe - base,
        "toneladas": float(df["toneladas"].sum()),
        "importe": importe, "materiales": int(df[["material_codigo", "centro"]].drop_duplicates().shape[0]),
    }


def ipu_por_mes(inflacion, desfase=12):
    """IPU (Índice de Precios Unacem), nombre definido por el usuario: por
    cada mes T con comparación de `inflacion` (salida de inflacion_precios,
    uno o varios materiales, SIN recortar por período: el acumulado arranca
    en enero), un dict mes -> {individual, acumulado, bolsa,
    bolsa_acumulada, importe_acumulado, base_acumulada, toneladas_acumuladas}.
    - individual: variación ponderada del mes (inflacion_canasta).
    - acumulado, interanual (desfase 12): enero..T de este año contra
      enero..T del anterior, ponderado con las toneladas de este año: lo
      pagado ÷ lo que habría costado al precio base − 1.
    - acumulado, mensual (desfase 1): variaciones del mes encadenadas desde
      enero, (1 + v1) × (1 + v2) × ... − 1 (inflación acumulada del año).
    Se reinicia cada enero; las comparaciones atípicas no cuentan."""
    resultado = {}
    validas = inflacion[~inflacion["atipica"]]
    for anio, g in validas.groupby([m.year for m in validas["mes"]]):
        factor, bolsa, importe, base, toneladas = 1.0, 0.0, 0.0, 0.0, 0.0
        for mes, gm in sorted(g.groupby("mes"), key=lambda x: x[0]):
            c = inflacion_canasta(gm)
            if c["variacion"] is None:
                continue
            bolsa += c["bolsa"]
            importe += c["importe"]
            base += c["importe"] - c["bolsa"]
            toneladas += c["toneladas"]
            factor *= 1 + c["variacion"]
            resultado[mes] = {
                "individual": c["variacion"], "bolsa": c["bolsa"],
                "acumulado": (importe / base - 1 if base else 0.0) if desfase == 12 else factor - 1,
                "bolsa_acumulada": bolsa, "importe_acumulado": importe, "base_acumulada": base,
                "toneladas_acumuladas": toneladas,
            }
    return resultado


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


# ---------------------------------------------------------------------------
# Evaluación de consumo (S&OP). Pedido por el usuario: el Plan de Consumos del
# S&OP (archivo mensual) contra el consumo real de MB51 (mismas clases que
# Cobertura: 2xx + Z01/Z04/353), por material/centro y mes. Carbón Terceros
# en Condorcocha se consume vía su chancado (14-00001112): real y plan se
# toman del chancado y se muestran como 06-00000005 (mismo criterio que la
# Evolución de inventario, donde el Excel repite el mismo número en las dos
# filas).
# ---------------------------------------------------------------------------

def _como_material_comprado(df):
    """Renombra las filas del material por el que realmente se consume
    (_MATERIALES_CONSUMO_VIA_OTRO) al material que se compra/planifica,
    descartando la fila propia de este último si trae las dos."""
    for (origen, centro_origen), (real, centro_real) in _MATERIALES_CONSUMO_VIA_OTRO.items():
        es_real = (df["material_codigo"] == real) & (df["centro"] == centro_real)
        if es_real.any():
            df = df[~((df["material_codigo"] == origen) & (df["centro"] == centro_origen))].copy()
            df.loc[(df["material_codigo"] == real) & (df["centro"] == centro_real), ["material_codigo", "centro"]] = [origen, centro_origen]
    return df


@_cacheado
def plan_consumo_sop():
    """Plan de Consumos S&OP de todos los archivos cargados: una fila por
    mes_snapshot (emisión), material/centro y mes planificado, en toneladas
    (positivas), con la descripción del Excel."""
    df = _query(f"""
        SELECT mes_snapshot AS snapshot, material_codigo AS material_codigo, centro_destino AS centro,
               mes_planificado AS mes, SUM(cantidad_consumo) AS plan, MAX(material_descripcion) AS descripcion
        FROM gestion_planconsumosop
        WHERE {_FILTRO_CON_CODIGO_SOP}
        GROUP BY mes_snapshot, material_codigo, centro_destino, mes_planificado
    """)
    if df.empty:
        return df
    df["snapshot"] = pd.to_datetime(df["snapshot"]).dt.date
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    df["plan"] = df["plan"].fillna(0.0).astype(float)
    partes = [_como_material_comprado(g) for _, g in df.groupby("snapshot")]
    df = pd.concat(partes, ignore_index=True)
    return df[df["plan"] > 0].reset_index(drop=True)


@_cacheado
def consumo_real_mensual_sop():
    """Consumo real (MB51) por material/centro y mes de los materiales del
    S&OP (planes de abastecimiento y de consumos), en toneladas positivas,
    con descripción y categoría (sin categoría en el maestro -> MATERIAS
    PRIMAS, como en Plan vs Real)."""
    columnas = ["material_codigo", "texto_breve", "centro", "categoria", "mes", "consumo"]
    planificados = set(materiales_planificados_sop())
    plan = plan_consumo_sop()
    if not plan.empty:
        planificados |= set(plan["material_codigo"])
    planificados |= {real for real, _centro in _MATERIALES_CONSUMO_VIA_OTRO.values()}
    if not planificados:
        return pd.DataFrame(columns=columnas)
    df = _query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro,
               substr(fecha_contabilizacion, 1, 7) || '-01' AS mes, -SUM(cantidad) AS consumo
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({",".join(f"'{m}'" for m in sorted(planificados))})
              AND +centro IN ('U001', 'U002') AND {_FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP}
              AND fecha_contabilizacion IS NOT NULL
        GROUP BY material_codigo, centro, mes
    """)
    if df.empty:
        return pd.DataFrame(columns=columnas)
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    df["consumo"] = df["consumo"].astype(float)
    df = _como_material_comprado(df)
    df = df.groupby(["material_codigo", "centro", "mes"], as_index=False)["consumo"].sum()
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "categoria"]].drop_duplicates("material_codigo")
    df = df.merge(catalogo, on="material_codigo", how="left")
    df.loc[df["categoria"].isna(), "categoria"] = "MATERIAS PRIMAS"
    df["texto_breve"] = df["texto_breve"].fillna("")
    return df[columnas].sort_values(["material_codigo", "centro", "mes"]).reset_index(drop=True)


def plan_consumo_del_mes(mes):
    """Plan de consumo vigente para `mes`: el del S&OP más reciente emitido
    hasta ese mes que lo incluya (idealmente el del propio mes). Devuelve
    (DataFrame material_codigo/centro/plan/descripcion, snapshot usado)."""
    plan = plan_consumo_sop()
    if plan.empty:
        return plan, None
    candidatos = plan[(plan["mes"] == mes) & (plan["snapshot"] <= mes)]
    if candidatos.empty:
        return candidatos, None
    snapshot = max(candidatos["snapshot"])
    return candidatos[candidatos["snapshot"] == snapshot][["material_codigo", "centro", "plan", "descripcion"]], snapshot


# ---------------------------------------------------------------------------
# Consumo por tonelada producida (S&OP). Pedido por el usuario (2026-09-30):
# saber si un consumo mayor viene de producir más o de gastar más por
# tonelada. MB51 trae la producción (101/102 de órdenes de producción) y lo que
# consume cada orden (261/262), así que se sigue la cadena de cada material
# hasta el producto final de su planta: CLINKER (calizas, arcillas,
# correctivos y carbones, vía crudo o combustible sólido) o CEMENTO (yeso,
# puzolana, escoria y el propio clinker). Factor = consumo real del mes (el
# mismo de la Evaluación de consumo) / producción del mes de ese producto en
# el mismo centro.
# ---------------------------------------------------------------------------

_PRODUCTOS_FINALES = {"CLINKER": "clinker", "CEMENTO": "cemento"}
_MESES_CADENA_PRODUCCION = 12


def _producto_final(descripcion):
    texto = (descripcion or "").strip().upper()
    for prefijo, producto in _PRODUCTOS_FINALES.items():
        if texto.startswith(prefijo):
            return producto
    return None


@_cacheado
def produccion_mensual_planta():
    """Producción neta mensual (101 menos 102 de órdenes de producción) de
    clinker y cemento por centro, en toneladas: columnas centro, mes,
    producto ('clinker'/'cemento'), produccion."""
    columnas = ["centro", "mes", "producto", "produccion"]
    df = _query("""
        SELECT centro AS centro, substr(fecha_contabilizacion, 1, 7) || '-01' AS mes,
               descripcion_material AS descripcion, SUM(cantidad) AS produccion
        FROM gestion_movimientomaterial
        WHERE clase_movimiento IN ('101', '102') AND orden <> '' AND substr(material_codigo, 1, 3) = '14-'
              AND substr(upper(descripcion_material), 1, 7) IN ('CLINKER', 'CEMENTO')
              AND fecha_contabilizacion IS NOT NULL
        GROUP BY centro, mes, descripcion_material
    """)
    if df.empty:
        return pd.DataFrame(columns=columnas)
    df["producto"] = df["descripcion"].map(_producto_final)
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    df["produccion"] = df["produccion"].astype(float)
    df = df.groupby(["centro", "mes", "producto"], as_index=False)["produccion"].sum()
    # Mes con MB51 incompleto (p.ej. Feb-2026, 8.4K filas de ~80K): producción
    # menor al 30% de la mediana de ese centro/producto -> se descarta, si no
    # el factor de ese mes se dispara.
    mediana = df.groupby(["centro", "producto"])["produccion"].transform("median")
    return df[(df["produccion"] > 0) & (df["produccion"] >= 0.3 * mediana)][columnas].reset_index(drop=True)


@_cacheado
def destino_produccion_sop():
    """A qué producto final va el consumo de cada material/centro, siguiendo
    las órdenes de producción de los últimos 12 meses (cadena material ->
    producto de la orden -> ... -> clinker o cemento). Devuelve un dict
    (material, centro) -> {"clinker": fracción, "cemento": fracción}; las
    fracciones pueden sumar menos de 1 si parte del consumo va a algo que no
    termina en ninguno de los dos (p.ej. harina cruda recuperada)."""
    ultima = _query("SELECT MAX(fecha_contabilizacion) AS f FROM gestion_movimientomaterial")["f"].iloc[0]
    if ultima is None:
        return {}
    desde = _sumar_meses(datetime.date.fromisoformat(str(ultima)[:10]).replace(day=1), -_MESES_CADENA_PRODUCCION)
    salidas = _query("""
        SELECT orden AS orden, material_codigo AS material, centro AS centro,
               MAX(descripcion_material) AS descripcion, SUM(cantidad) AS cantidad
        FROM gestion_movimientomaterial
        WHERE clase_movimiento IN ('101', '102') AND orden <> '' AND fecha_contabilizacion >= %s
        GROUP BY orden, material_codigo, centro
    """, [desde.isoformat()])
    entradas = _query("""
        SELECT orden AS orden, material_codigo AS material, centro AS centro, -SUM(cantidad) AS cantidad
        FROM gestion_movimientomaterial
        WHERE clase_movimiento IN ('261', '262') AND orden <> '' AND fecha_contabilizacion >= %s
        GROUP BY orden, material_codigo, centro
    """, [desde.isoformat()])
    if salidas.empty or entradas.empty:
        return {}
    # Una orden produce un material: el de mayor cantidad neta.
    salidas = salidas.sort_values("cantidad").drop_duplicates("orden", keep="last")
    finales = {(m, c): _producto_final(d) for m, c, d in zip(salidas["material"], salidas["centro"], salidas["descripcion"])}
    aristas = entradas.merge(
        salidas[["orden", "material", "centro"]].rename(columns={"material": "salida", "centro": "centro_salida"}), on="orden"
    )
    aristas = aristas[(aristas["cantidad"] > 0) & (aristas["material"] != aristas["salida"])]
    aristas = aristas.groupby(["material", "centro", "salida", "centro_salida"], as_index=False)["cantidad"].sum()
    hacia = {}
    for r in aristas.itertuples():
        hacia.setdefault((r.material, r.centro), []).append(((r.salida, r.centro_salida), float(r.cantidad)))

    memo = {}

    def destino(nodo, visitados):
        if nodo in memo:
            return memo[nodo]
        total = sum(t for _, t in hacia.get(nodo, []))
        resultado = {"clinker": 0.0, "cemento": 0.0}
        for salida, t in hacia.get(nodo, []):
            if salida in visitados:
                continue
            final = finales.get(salida)
            parte = {final: 1.0} if final else destino(salida, visitados | {salida})
            for producto, fraccion in parte.items():
                resultado[producto] += t / total * fraccion
        memo[nodo] = resultado
        return resultado

    resultado = {nodo: destino(nodo, {nodo}) for nodo in hacia}
    # Mismo empalme que el consumo: Carbón Terceros U002 va vía su chancado.
    for origen, real in _MATERIALES_CONSUMO_VIA_OTRO.items():
        if real in resultado:
            resultado[origen] = resultado[real]
    return resultado


@_cacheado
def consumo_por_tonelada_sop():
    """Consumo por tonelada producida, mes a mes, de cada material/centro del
    S&OP: columnas material_codigo, texto_breve, centro, categoria, producto
    ('clinker'/'cemento', el que recibe la mayor parte de su consumo),
    participacion (esa fracción), mes, consumo, produccion, factor (T de
    material por T de producto). Materiales cuyo consumo no llega a clinker
    ni cemento quedan fuera."""
    columnas = ["material_codigo", "texto_breve", "centro", "categoria", "producto", "participacion",
                "mes", "consumo", "produccion", "factor"]
    real = consumo_real_mensual_sop()
    produccion = produccion_mensual_planta()
    destinos = destino_produccion_sop()
    if real.empty or produccion.empty or not destinos:
        return pd.DataFrame(columns=columnas)
    principal = {}
    for clave, partes in destinos.items():
        producto = max(partes, key=partes.get)
        if partes[producto] >= 0.5:
            principal[clave] = (producto, partes[producto])
    df = real.copy()
    claves = list(zip(df["material_codigo"], df["centro"]))
    df["producto"] = [principal.get(k, (None, None))[0] for k in claves]
    df["participacion"] = [principal.get(k, (None, None))[1] for k in claves]
    df = df[df["producto"].notna()].merge(produccion, on=["centro", "mes", "producto"], how="inner")
    df = df[(df["produccion"] > 0) & (df["consumo"] > 0)].copy()
    df["factor"] = df["consumo"] / df["produccion"]
    return df[columnas].sort_values(["material_codigo", "centro", "mes"]).reset_index(drop=True)
