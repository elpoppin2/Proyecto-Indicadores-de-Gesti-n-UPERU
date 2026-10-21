"""Consultas de agregación para los dashboards. Usan SQL directo sobre la misma
conexión de Django (más eficiente que el ORM para agregaciones sobre tablas de
millones de filas) y devuelven DataFrames de pandas listos para graficar."""

import calendar
import collections
import contextlib
import contextvars
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
# Memoria: los más recientes primero en salir por el final (LRU), con un tope
# por el tamaño que ocupan guardados (pickle). Sin tope, cada combinación de
# filtros que se abre en el día quedaba en la memoria del servidor hasta la
# carga siguiente; lo que sale de la memoria se sigue leyendo del disco.
_MAX_BYTES_MEMORIA = int(os.environ.get("UPERU_CACHE_MEMORIA_MB", "1024")) * 1024 * 1024
_query_cache = collections.OrderedDict()  # clave_version -> (valor, bytes)
_query_cache_bytes = 0
_query_cache_lock = threading.Lock()
_version_leida = {"mtime": None, "version": None}


def _version_datos():
    """Versión actual de los datos (texto en version.txt). Se relee solo si el
    archivo cambió (un os.stat por consulta, microsegundos)."""
    global _query_cache_bytes
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
                _query_cache_bytes = 0
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
    global _query_cache_bytes
    version = f"{_version_datos()}-{_HUELLA_CODIGO}"
    clave_version = (version, clave)
    with _query_cache_lock:
        guardado = _query_cache.get(clave_version)
        if guardado is not None:
            _query_cache.move_to_end(clave_version)
    if guardado is not None:
        return _copia(guardado[0])

    huella = hashlib.sha1(repr(clave).encode("utf-8")).hexdigest()
    ruta = os.path.join(_DIR_CACHE, version, huella + ".pkl")
    valor = _SIN_VALOR
    try:
        with open(ruta, "rb") as f:
            datos = f.read()
        valor = pickle.loads(datos)
    except (OSError, EOFError, pickle.UnpicklingError):
        pass
    if valor is _SIN_VALOR:
        valor = calcular()
        datos = pickle.dumps(valor, protocol=pickle.HIGHEST_PROTOCOL)
        try:
            os.makedirs(os.path.dirname(ruta), exist_ok=True)
            tmp = f"{ruta}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as f:
                f.write(datos)
            os.replace(tmp, ruta)
        except OSError:
            pass  # sin disco disponible la caché en memoria sigue funcionando
    with _query_cache_lock:
        anterior = _query_cache.pop(clave_version, None)
        if anterior is not None:
            _query_cache_bytes -= anterior[1]
        _query_cache[clave_version] = (valor, len(datos))
        _query_cache_bytes += len(datos)
        while _query_cache_bytes > _MAX_BYTES_MEMORIA and len(_query_cache) > 1:
            _, (_, tamano) = _query_cache.popitem(last=False)
            _query_cache_bytes -= tamano
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
        clave = ("fn", funcion.__qualname__, repr(args), repr(sorted(kwargs.items())), datetime.date.today(),
                 alcance_actual())
        return _cache_obtener(clave, lambda: funcion(*args, **kwargs))
    return envoltura


# Alcance de materiales: una página puede mostrar las mismas secciones de
# Planificación solo para algunos (material, centro), p.ej. Logística de
# Materias Primas (pedido por el usuario el 2026-10-16). Mientras rige
# (`with alcance_materiales(pares):`), las consultas base del Plan vs Real
# (plan, ingresos de báscula, entregas, traslados) devuelven solo esos pares,
# y todo lo que se calcula encima queda acotado. Va en la clave de @_cacheado
# y de la caché de vistas, así una página no pisa lo guardado por la otra.
_ALCANCE_MATERIALES = contextvars.ContextVar("alcance_materiales", default=None)


@contextlib.contextmanager
def alcance_materiales(pares):
    token = _ALCANCE_MATERIALES.set(tuple(sorted(pares)) if pares else None)
    try:
        yield
    finally:
        _ALCANCE_MATERIALES.reset(token)


def alcance_actual():
    """Pares (material, centro) del alcance vigente, o None (sin acotar)."""
    return _ALCANCE_MATERIALES.get()


def _en_alcance(df, centro=None):
    """Filas de `df` (material_codigo + centro, o el `centro` dado si la
    consulta ya viene de un solo centro) dentro del alcance vigente."""
    pares = _ALCANCE_MATERIALES.get()
    if pares is None or df.empty:
        return df
    pares = set(pares)
    centros = [centro] * len(df) if centro else df["centro"]
    dentro = [par in pares for par in zip(df["material_codigo"], centros)]
    return df[dentro].reset_index(drop=True)


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
# Definido por el usuario, no se deduce de los datos. Calificada con el alias
# "lc" porque "grupo_compras" existe en más de una tabla de los JOIN (p.ej.
# gestion_categoriaservicio). G02 ya no está aquí: se excluye por categoría
# (ENERGÍA ELÉCTRICA, ver FILTRO_CATEGORIA_VALIDA_LC) en vez de por código de
# grupo de compras.
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


# ---------------------------------------------------------------------------
# Detalle fila por fila (botón "Ver detalle fila por fila" de cada sección,
# ver gestion/desglose.py). Pedido por el usuario: ver de qué filas de SAP
# sale cada indicador. Mismos filtros que la consulta agregada de cada
# indicador, para que la suma de las filas cuadre con el número mostrado.
# Sin la caché versionada de _query: son DataFrames grandes que se piden
# pocas veces; desglose.py guarda en memoria solo los últimos consultados.
# ---------------------------------------------------------------------------

def _query_directo(sql, params=None):
    return pd.read_sql_query(sql, connection, params=list(params or []))


def _texto_sql(valor):
    return "'" + str(valor).replace("'", "''") + "'"


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
    consumo_valorizado_6_meses_por_material. `consumo` e `importe` con el
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


# "Materiales sin Movimiento" (antes "Obsoletos"): sin ningún consumo en los
# últimos 3 años, no 6 meses como el resto de indicadores de consumo — es un
# umbral mucho más exigente pensado para detectar capital realmente
# inmovilizado, no solo baja rotación reciente. Definido por el usuario.
MATERIALES_SIN_MOVIMIENTO_ANIOS = 3


@_cacheado
def materiales_obsoletos_por_categoria(fecha, familia=None, centro=None):
    """Materiales con stock de cierre > 0 pero sin ningún consumo (MB51,
    FILTRO_CLASE_MOVIMIENTO_CONSUMO) en los últimos MATERIALES_SIN_MOVIMIENTO_ANIOS
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


# Clases de movimiento de MB51 que cuentan como "consumo" en TODAS las pestañas
# (Rotación, Materiales sin Movimiento, Críticos, Planificación): Z01 (SM
# Vta.Relacionadas), Z04 (EM AnulTransf.Gratui), 353 (aún sin datos) y todas
# las que empiezan con "2", de la 200 a la 299 (201/202 a centro de costo,
# 221/222 a proyecto, 261/262 a orden, 281/282 a red...). Las anulaciones
# (202, 262...) vienen con el signo contrario y se restan solas. Definido por
# el usuario el 2026-10-04: antes solo Z01/Z04/353 fuera de Planificación, y
# la 261 (1.5 M movimientos, el grueso de los repuestos) no contaba.
CLASES_MOVIMIENTO_CONSUMO = ("Z01", "Z04", "353")
_IN_CLASES_MOVIMIENTO_CONSUMO = ",".join(f"'{c}'" for c in CLASES_MOVIMIENTO_CONSUMO)
# BETWEEN (no substr/LIKE) para que SQLite use el índice idx_mov_consumo_rotacion;
# todas las clases "2xx" cargadas son numéricas.
FILTRO_CLASE_MOVIMIENTO_CONSUMO = (
    f"(clase_movimiento BETWEEN '200' AND '299' OR clase_movimiento IN ({_IN_CLASES_MOVIMIENTO_CONSUMO}))"
)

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
        WHERE {FILTRO_CLASE_MOVIMIENTO_CONSUMO}
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
        WHERE {FILTRO_CLASE_MOVIMIENTO_CONSUMO}
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
        WHERE {FILTRO_CLASE_MOVIMIENTO_CONSUMO}
              AND fecha_contabilizacion BETWEEN %s AND %s
              AND material_codigo != ''
              AND {FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS}
        GROUP BY material_codigo
    """, [fecha_inicio, fecha_fin])


@_cacheado
def consumo_valorizado_mensual_por_categoria():
    """Consumo en valor (S/, mismas reglas que consumo_valorizado_6_meses_por_material)
    por categoría y mes: insumo del IRI de 12 meses (pestaña Indicadores de las
    categorías). Columnas: categoria, mes (date, día 1), consumo_valor."""
    df = _query(f"""
        SELECT material_codigo AS material_codigo, substr(fecha_contabilizacion, 1, 7) || '-01' AS mes,
               -SUM(importe_moneda_local) AS consumo_valor
        FROM gestion_movimientomaterial
        WHERE {FILTRO_CLASE_MOVIMIENTO_CONSUMO}
              AND fecha_contabilizacion IS NOT NULL
              AND material_codigo != ''
              AND {FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS}
        GROUP BY material_codigo, mes
    """)
    if df.empty:
        return pd.DataFrame(columns=["categoria", "mes", "consumo_valor"])
    df = df.merge(catalogo_materiales()[["material_codigo", "categoria"]].drop_duplicates("material_codigo"),
                  on="material_codigo", how="left")
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    df["consumo_valor"] = df["consumo_valor"].astype(float)
    return df.groupby(["categoria", "mes"], as_index=False)["consumo_valor"].sum()


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
    return _en_alcance(_query(f"""
        SELECT material_codigo AS material_codigo, centro_destino AS centro,
               SUM(cantidad_plan) AS cantidad_plan
        FROM gestion_planabastecimientosop
        WHERE mes_planificado = %s AND {_FILTRO_CON_CODIGO_SOP}
              AND mes_snapshot = (SELECT MAX(mes_snapshot) FROM gestion_planabastecimientosop)
        GROUP BY material_codigo, centro_destino
    """, [mes]))


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


def ingresos_fisicos_por_material(mes):
    """Ingresos físicos reales de Recepción (báscula, ZMM056 — solo Tipo de
    operación 1/5, ver _TIPOS_OPERACION_RECEPCION) por material/centro en el
    mes calendario `mes` (por FECHA_INGRESO_EFECTIVA). Definido por el
    usuario: no se toma de MB51 para estos materiales."""
    fecha_inicio, fecha_fin = _limites_mes(mes)
    return _en_alcance(_query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro,
               SUM(cantidad) AS cantidad_real
        FROM gestion_ingresofisicosop
        WHERE {FECHA_INGRESO_EFECTIVA} BETWEEN %s AND %s
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION})
        GROUP BY material_codigo, centro
    """, [fecha_inicio, fecha_fin]))


def ingresos_fisicos_por_proveedor(mes):
    """Ingresos físicos de Recepción (mismas reglas que
    ingresos_fisicos_por_material) por material/centro/proveedor en el mes
    `mes`, con la cantidad de carros (tickets). En los ingresos propios (Tipo
    5: cantera o traslado entre plantas) el "proveedor" de ZMM056 es la planta
    de origen (ATOCONGO, PUCARA...). Usado por el Detalle por material de
    Cumplimiento. Pedido por el usuario (después llegará el plan por proveedor)."""
    fecha_inicio, fecha_fin = _limites_mes(mes)
    return _en_alcance(_query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro, proveedor_codigo AS proveedor_codigo,
               proveedor AS proveedor, MAX(tipo_operacion) AS tipo_operacion,
               SUM(cantidad) AS cantidad_real, COUNT(*) AS carros
        FROM gestion_ingresofisicosop
        WHERE {FECHA_INGRESO_EFECTIVA} BETWEEN %s AND %s
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION})
        GROUP BY material_codigo, centro, proveedor_codigo, proveedor
    """, [fecha_inicio, fecha_fin]))


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
               substr({FECHA_INGRESO_EFECTIVA}, 1, 7) || '-01' AS mes, SUM(cantidad) AS toneladas, COUNT(*) AS carros
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
    df = _en_alcance(df[df["categoria"].isin(CATEGORIAS_PLANIFICACION_SOP)])
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

# Transporte (indicadores de Logística de Materias Primas, pedido del usuario
# 2026-10-19). Menos de este tiempo entre el pesaje lleno y el vacío = el
# camión se pesó una sola vez con su tara registrada (en Atocongo, la Caliza
# Pucará, Andina y Roca Fuerte): no mide el tiempo en planta. Más de un día =
# dato de báscula erróneo.
MINUTOS_TARA_REGISTRADA = 2
MINUTOS_MAXIMO_EN_PLANTA = 24 * 60


@_cacheado
def viajes_recepcion():
    """Un ticket de recepción (tipo 1/5) por fila, de los (material, centro)
    del alcance, con lo del transporte: centro, material_codigo,
    descripcion, fecha (efectiva), mes, toneladas (húmedas), toneladas_secas
    (None si el ticket no trae peso seco), proveedor, transportista_codigo,
    transportista (nombre del ticket; si no trae, el del maestro de
    proveedores de ME2N o de la ZMM056), placa, llegada (pesaje lleno, hora
    local), minutos (entre el pesaje lleno y el vacío; None si no se puede
    medir) y tara_registrada (se pesó una sola vez)."""
    df = _query(f"""
        SELECT centro AS centro, material_codigo AS material_codigo, descripcion_material AS descripcion,
               {FECHA_INGRESO_EFECTIVA} AS fecha, cantidad AS toneladas, cantidad_seca AS toneladas_secas,
               proveedor AS proveedor, transportista_codigo AS transportista_codigo, transportista AS transportista,
               placa AS placa, fecha_pesaje_lleno AS f_lleno, hora_pesaje_lleno AS h_lleno,
               fecha_pesaje_vacio AS f_vacio, hora_pesaje_vacio AS h_vacio
        FROM gestion_ingresofisicosop
        WHERE tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION}) AND fecha_ingreso IS NOT NULL
    """)
    df = _en_alcance(df)
    if df.empty:
        return df
    nombres = _query("""
        SELECT proveedor_codigo AS codigo, MAX(proveedor) AS nombre FROM gestion_ingresofisicosop
        WHERE proveedor_codigo <> '' AND proveedor <> '' GROUP BY proveedor_codigo
        UNION ALL
        SELECT proveedor AS codigo, MAX(cuenta_proveedor) AS nombre FROM gestion_lineacompra
        WHERE proveedor <> '' AND cuenta_proveedor <> '' GROUP BY proveedor
    """).drop_duplicates("codigo").set_index("codigo")["nombre"]
    sin_nombre = df["transportista"].fillna("") == ""
    df.loc[sin_nombre, "transportista"] = [nombres.get(c) or (f"Transportista {c}" if c else "Sin transportista")
                                          for c in df.loc[sin_nombre, "transportista_codigo"]]
    df["fecha"] = pd.to_datetime(df["fecha"]).dt.date
    df["mes"] = [f.replace(day=1) for f in df["fecha"]]
    df["toneladas"] = df["toneladas"].astype(float)
    df["toneladas_secas"] = pd.to_numeric(df["toneladas_secas"], errors="coerce").where(lambda s: s > 0)
    llegada = pd.to_datetime(df["f_lleno"].astype(str) + " " + df["h_lleno"].astype(str), errors="coerce")
    salida = pd.to_datetime(df["f_vacio"].astype(str) + " " + df["h_vacio"].astype(str), errors="coerce")
    minutos = (salida - llegada).dt.total_seconds().abs() / 60
    df["llegada"] = llegada
    df["tara_registrada"] = minutos < MINUTOS_TARA_REGISTRADA
    df["minutos"] = minutos.where((minutos >= MINUTOS_TARA_REGISTRADA) & (minutos <= MINUTOS_MAXIMO_EN_PLANTA))
    return df.drop(columns=["f_lleno", "h_lleno", "f_vacio", "h_vacio"]).reset_index(drop=True)


def traslados_recibidos(mes, centro):
    """Ingresos por traslado (ZMM056 Tipo 5) que recibió `centro` en el mes
    `mes`, por material y planta de origen (el "proveedor" del ticket), con
    toneladas y carros."""
    fecha_inicio, fecha_fin = _limites_mes(mes)
    return _en_alcance(_query(f"""
        SELECT material_codigo AS material_codigo, MAX(descripcion_material) AS descripcion,
               proveedor AS origen, SUM(cantidad) AS toneladas, COUNT(*) AS carros
        FROM gestion_ingresofisicosop
        WHERE {FECHA_INGRESO_EFECTIVA} BETWEEN %s AND %s AND centro = %s AND tipo_operacion = '5'
        GROUP BY material_codigo, proveedor
    """, [fecha_inicio, fecha_fin, centro]), centro=centro)


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
        SELECT {FECHA_INGRESO_EFECTIVA} AS fecha, material_codigo AS material_codigo, centro AS centro,
               SUM(cantidad) AS cantidad_real, COUNT(*) AS carros
        FROM gestion_ingresofisicosop
        WHERE {FECHA_INGRESO_EFECTIVA} BETWEEN %s AND %s
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION})
        GROUP BY {FECHA_INGRESO_EFECTIVA}, material_codigo, centro
    """, [fecha_inicio, fecha_fin])

    catalogo = catalogo_materiales()[["material_codigo", "categoria"]]
    real = _en_alcance(real).merge(catalogo, on="material_codigo", how="left")
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
    # La serie arranca en el primer mes con Plan: la ZMM056 trae ingresos desde
    # ene-2025 (cargada el 2026-10-20) y el Plan recién desde set-2026; sin
    # esto el gráfico abría con 20 meses vacíos.
    primero = next((i for i, f in enumerate(filas) if f["cantidad_plan"]), len(filas))
    return filas[primero:]


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
_MINIMO_TRASLADO = 0.02


# Materiales de producción propia que el usuario mide en el IPU/TCO como uno
# comprado (pedido del usuario, oct-2026: la "Caliza CC" de su reporte):
# (material, centro que la recibe) -> centro que la envía. La caliza extraída
# entra a Atocongo por traslado desde Condorcocha (al valor estándar de U002) y
# su costo real llega con la liquidación mensual de la orden CAL_ROCA_AT (KO88/
# CO88, clase PP; X2 = anulación) a la cuenta 2310002000 (FBL3N). Además, en
# cada cierre del Material Ledger el centro que envía le pasa sus diferencias
# de precio (FBL3N clase ML, el mismo documento en los dos centros con signo
# contrario: "ZUPRV: Diferencias de precio de material entrada"), que la
# CKM3N muestra en Entradas > Traslado (pedido del usuario 2026-10-19: ene-26,
# S/ 16,646.38). Precio = (valor del traslado + liquidación + esas
# diferencias) ÷ toneladas de entrada (traslado neto + ajustes de
# humedad/traslado Z15/Z16/Z31/Z32): el mismo valor y las mismas toneladas de
# la fila Entradas de la CKM3N (verificado: ene-26 S/ 2,341,741.71 y
# 36,647.83 T; set-26 39,624.01 T).
PRODUCCION_PROPIA = {("14-00000024", "U001"): "U002"}
CLASES_LIQUIDACION_PRODUCCION = ("PP", "X2")
# Anulaciones de traslado (642) que fueron un error de SAP y no se descuentan
# (pedido del usuario, 2026-10-20): (material, fecha). El 26-jun-26 se
# anularon 137 traslados de la Caliza Roca Extraída (4,607 T) que sí llegaron;
# sin esto jun-26 quedaba con 18,401 T en vez de ~23,000 (precio 83.75 en vez
# de ~67). Las otras anulaciones de junio (un camión cada una) sí cuentan.
ANULACIONES_ERRONEAS = [("14-00000024", datetime.date(2026, 6, 26))]


def _sql_sin_anulaciones_erroneas(material):
    """Condición SQL que deja fuera las 642 de ANULACIONES_ERRONEAS de `material`."""
    fechas = [f"'{f.isoformat()}'" for m, f in ANULACIONES_ERRONEAS if m == material]
    if not fechas:
        return "1"
    return f"NOT (clase_movimiento = '642' AND fecha_contabilizacion IN ({', '.join(fechas)}))"


def _produccion_propia(df, materiales=None):
    """`df` (salida del SQL de precios) más una fila por mes de cada material
    de PRODUCCION_PROPIA (si `materiales` lo incluye): toneladas de entrada e
    importe = valor del traslado (la liquidación entra como ajuste en
    con_precio_real). Un mes con el traslado sin valor en MB51 (pasó en
    jul-25) toma el valor por tonelada del mes anterior."""
    filas = []
    for (material, centro), origen in PRODUCCION_PROPIA.items():
        if materiales is not None and material not in materiales:
            continue
        m = _query(f"""
            SELECT substr(fecha_contabilizacion, 1, 7) || '-01' AS mes, centro, clase_movimiento AS cl,
                   SUM(CASE WHEN cantidad > 0 THEN cantidad ELSE 0 END) AS t_pos,
                   SUM(CASE WHEN cantidad < 0 THEN cantidad ELSE 0 END) AS t_neg,
                   SUM(cantidad) AS t, SUM(importe_moneda_local) AS imp,
                   SUM(CASE WHEN cantidad > 0 THEN 1 ELSE 0 END) AS n_pos
            FROM gestion_movimientomaterial
            WHERE material_codigo = {_texto_sql(material)} AND centro IN ({_texto_sql(centro)}, {_texto_sql(origen)})
                  AND clase_movimiento IN ('641', '642', 'Z15', 'Z16', 'Z31', 'Z32') AND fecha_contabilizacion IS NOT NULL
                  AND {_sql_sin_anulaciones_erroneas(material)}
            GROUP BY 1, 2, 3
        """)
        if m.empty:
            continue
        for c in ("t_pos", "t_neg", "t", "imp"):
            m[c] = m[c].astype(float)
        valor_anterior = None
        for mes, g in sorted(m.groupby("mes"), key=lambda x: x[0]):
            d, o = g[g["centro"] == centro], g[g["centro"] == origen]
            traslado = d.loc[d["cl"] == "641", "t_pos"].sum() + d.loc[d["cl"] == "642", "t_neg"].sum()
            toneladas = traslado + d.loc[d["cl"].isin(["Z15", "Z16", "Z31", "Z32"]), "t"].sum()
            valor = -o.loc[o["cl"].isin(["641", "642"]), "imp"].sum()
            if traslado <= 0.5:
                continue
            if valor <= 0 and valor_anterior:
                valor = valor_anterior * traslado
            valor_anterior = valor / traslado if valor > 0 else valor_anterior
            filas.append({"material_codigo": material, "centro": centro, "mes": mes, "toneladas": toneladas,
                          "importe": valor, "entradas": int(d.loc[d["cl"] == "641", "n_pos"].sum()), "proveedores": 0})
    if not filas:
        return df
    return pd.concat([df, pd.DataFrame(filas)[df.columns]], ignore_index=True)


def liquidaciones_produccion_propia():
    """Costo real de los materiales de PRODUCCION_PROPIA en su centro, por
    mes (FBL3N): la liquidación de sus órdenes (clases PP/X2) más las
    diferencias de precio que le pasa el centro que envía en el cierre del
    Material Ledger (clase ML, el mismo documento en el centro de origen con
    importe contrario). Columnas: material_codigo, centro, mes, liquidacion."""
    claves = " OR ".join(
        f"""(d.material_codigo = {_texto_sql(m)} AND d.centro = {_texto_sql(c)} AND (
                d.clase_documento IN {CLASES_LIQUIDACION_PRODUCCION}
                OR (d.clase_documento = 'ML' AND EXISTS (
                    SELECT 1 FROM gestion_ajustepreciofbl3n o
                    WHERE o.material_codigo = d.material_codigo AND o.centro = {_texto_sql(origen)}
                          AND o.numero_documento = d.numero_documento
                          AND o.fecha_contabilizacion = d.fecha_contabilizacion
                          AND ABS(o.importe + d.importe) < 0.01))))"""
        for (m, c), origen in PRODUCCION_PROPIA.items())
    df = _query(f"""
        SELECT d.material_codigo AS material_codigo, d.centro AS centro,
               substr(d.fecha_contabilizacion, 1, 7) || '-01' AS mes, SUM(d.importe) AS liquidacion
        FROM gestion_ajustepreciofbl3n d
        WHERE {claves}
        GROUP BY 1, 2, 3
    """)
    if not df.empty:
        df["mes"] = pd.to_datetime(df["mes"]).dt.date
    return df


@_cacheado
def materiales_importados():
    """Materiales comprados en el centro de importación (101 con proveedor en
    U004) y trasladados a planta: su precio se mide puesto en destino."""
    # Agrupado en una sola pasada (~1 s; con EXISTS correlacionado eran ~60 s).
    df = _query(f"""
        SELECT material_codigo FROM gestion_movimientomaterial
        WHERE +centro = '{CENTRO_IMPORTACION}' AND clase_movimiento IN ('101', '641')
        GROUP BY material_codigo
        HAVING SUM(clase_movimiento = '101' AND proveedor <> '') > 0 AND SUM(clase_movimiento = '641') > 0
    """)
    return set(df["material_codigo"])


def _traslados_desde_importacion(materiales):
    """Lo que cada planta recibe de U004 por traslado (641, neto de
    anulaciones 642, lado receptor; solo el primer destino), por
    material/centro/mes: toneladas, importe (valor del traslado), entradas.
    Sin los traslados de corrección (ver _MINIMO_TRASLADO)."""
    destino = _query(f"""
        SELECT r.material_codigo AS material_codigo, r.centro AS centro,
               substr(r.fecha_contabilizacion, 1, 7) || '-01' AS mes,
               SUM(r.cantidad) AS toneladas, SUM(r.importe_moneda_local) AS importe,
               SUM(CASE WHEN r.clase_movimiento = '641' THEN 1 ELSE 0 END) AS entradas
        FROM gestion_movimientomaterial r
        WHERE r.material_codigo IN ({",".join(_texto_sql(m) for m in materiales)})
              AND r.centro <> '{CENTRO_IMPORTACION}'
              AND ((r.clase_movimiento = '641' AND r.cantidad > 0) OR (r.clase_movimiento = '642' AND r.cantidad < 0))
              AND EXISTS (SELECT 1 FROM gestion_movimientomaterial o
                          WHERE o.documento_material = r.documento_material
                                AND o.ejercicio_documento_material = r.ejercicio_documento_material
                                AND o.material_codigo = r.material_codigo AND o.centro = '{CENTRO_IMPORTACION}')
        GROUP BY r.material_codigo, r.centro, mes
    """).copy()
    # Traslados de corrección (unas decenas de T con valor revaluado, p.ej.
    # 31 T a S/ 5,000 por T) no son un barco llegando a planta: fuera los
    # meses con menos del _MINIMO_TRASLADO del mayor traslado mensual del material.
    destino["toneladas"] = destino["toneladas"].astype(float)
    destino["importe"] = destino["importe"].astype(float)
    mayor = destino.groupby("material_codigo")["toneladas"].transform("max")
    return destino[destino["toneladas"] >= mayor * _MINIMO_TRASLADO]


# ---------------------------------------------------------------------------
# Importados por embarque (pedido del usuario, 2026-10-15). Los cargos de un
# barco (flete, desestiba, demurrage, aduanas, diferencias de factura) llegan
# a la FBL3N semanas o meses después del ingreso a U004: asignados a su mes
# de contabilización distorsionaban el precio de otro mes y, como revalúan el
# stock de U004, terminaban viajando en el traslado de OTRO barco. Ahora:
# - Embarque = ingresos (101/102 con proveedor en U004) de un mismo pedido
#   separados por menos de _DIAS_ENTRE_BARCOS días (un pedido marco trae
#   varios barcos: 4500011752 = Sep-24, Mar-25 y Jul-25).
# - Cada partida de la FBL3N del material (en U004, U001 o U002) va a su
#   embarque: por el pedido 45xxxxxxxx que trae en el pedido o en el texto
#   ("PD4500011752-DESESTIBA", "45/10996 DEMURRAGE"), al barco de ese pedido
#   llegado más recientemente antes de la partida; sin pedido, al último
#   barco llegado antes de la partida. Lo anterior al primer barco cargado
#   (embarques de 2023) queda fuera.
# - Costo del embarque = (valor del ingreso + sus partidas) ÷ toneladas
#   recibidas. Lo que cada planta recibe de U004 se valoriza por FIFO con el
#   costo de los barcos de donde salió (un barco solo abastece desde su
#   llegada). Toneladas sin barco cargado que las cubra (falta la MB51 de
#   ese ingreso) quedan al valor del traslado, sin ajustes.
# - Ajustes por humedad (pedido del usuario, 2026-10-19): MB51 clases Z15
#   (entrada) / Z16 (salida), solo cantidad y sin valor ("Ajuste Humedad
#   SET-25", "M/V EVA BRIGHT"). Van al último barco llegado antes del ajuste,
#   en cualquier centro, y bajan sus toneladas: el costo del barco se reparte
#   en las toneladas secas (Escoria ~6-8% por barco). Cada tonelada recibida
#   lleva la parte de humedad de su barco (FIFO).
# Solo para importados que se compran únicamente en U004 (Carbón Importado,
# Escoria japonesa...); los demás siguen con el reparto por mes.
# ---------------------------------------------------------------------------
_PEDIDO_EN_TEXTO = re.compile(r"(?<!\d)45(\d{8})(?!\d)|(?<!\d)45/(\d{4,8})(?!\d)")
_DIAS_ENTRE_BARCOS = 45
CLASES_AJUSTE_HUMEDAD = ("Z15", "Z16")


def _pedido_de_partida(*campos):
    """Pedido de importación (45xxxxxxxx) citado en una partida, o None."""
    for campo in campos:
        for m in _PEDIDO_EN_TEXTO.finditer(campo or ""):
            return "45" + (m.group(1) or m.group(2).zfill(8))
    return None


def _embarque_de_partidas(partidas, barcos):
    """Embarque de cada partida de la FBL3N (columnas material_codigo,
    fecha, documento_compras, texto, referencia) según los `barcos` de
    embarques_importados: (embarques, criterios), en el orden de `partidas`.
    Criterio: "pedido" (cita el pedido del barco), "fecha" (último barco
    llegado antes) o "anterior" (antes del primer barco cargado: fuera)."""
    # Listas simples por material (ordenadas por llegada): miles de partidas.
    por_material = {}
    for b in barcos.sort_values("llegada").itertuples():
        por_material.setdefault(b.material_codigo, []).append((b.llegada, b.pedido, b.embarque))
    asignados, criterios = [], []
    for material, fecha, doc, texto, ref in zip(partidas["material_codigo"], partidas["fecha"],
                                                partidas["documento_compras"], partidas["texto"], partidas["referencia"]):
        b = por_material.get(material)
        if b is None:
            asignados.append(None); criterios.append("")
            continue
        pedido = _pedido_de_partida(doc, texto, ref)
        del_pedido = [x for x in b if x[1] == pedido] if pedido else []
        llegados = [x for x in b if x[0] <= fecha]
        if del_pedido:
            antes = [x for x in del_pedido if x[0] <= fecha]
            asignados.append((antes[-1] if antes else del_pedido[0])[2]); criterios.append("pedido")
        elif llegados:
            asignados.append(llegados[-1][2]); criterios.append("fecha")
        else:
            asignados.append(None); criterios.append("anterior")
    return asignados, criterios


@_cacheado
def embarques_importados():
    """(embarques, partidas) de los importados comprados solo en U004.
    embarques: material_codigo, embarque, pedido, llegada, toneladas (las
    del ingreso), importe (MB51), ajuste (FBL3N al precio), ajuste_ordenes,
    partidas, humedad (T de sus ajustes Z15/Z16, negativo = se secó).
    partidas: material_codigo, numero_documento, fecha, centro, importe,
    embarque (None = fuera) y criterio (pedido / fecha / anterior)."""
    vacio = (pd.DataFrame(columns=["material_codigo", "embarque", "pedido", "llegada", "toneladas", "importe",
                                   "ajuste", "ajuste_ordenes", "partidas", "humedad"]),
             pd.DataFrame(columns=["material_codigo", "numero_documento", "fecha", "centro", "importe",
                                   "embarque", "criterio"]))
    importados = sorted(materiales_importados())
    if not importados:
        return vacio
    lista = ",".join(_texto_sql(m) for m in importados)
    compras = _query(f"""
        SELECT material_codigo, centro, pedido, fecha_contabilizacion AS fecha,
               SUM(cantidad) AS toneladas, SUM(importe_moneda_local) AS importe
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({lista}) AND +clase_movimiento IN {_CLASES_ENTRADA_COMPRA} AND proveedor <> ''
              AND fecha_contabilizacion IS NOT NULL
        GROUP BY material_codigo, centro, pedido, fecha_contabilizacion
    """)
    if compras.empty:
        return vacio
    solo_u004 = compras.groupby("material_codigo")["centro"].agg(lambda s: set(s) == {CENTRO_IMPORTACION})
    compras = compras[compras["material_codigo"].isin(set(solo_u004[solo_u004].index))].copy()
    compras["fecha"] = pd.to_datetime(compras["fecha"]).dt.date
    compras["toneladas"] = compras["toneladas"].astype(float)
    compras["importe"] = compras["importe"].astype(float)

    barcos = []
    for (material, pedido), g in compras.sort_values("fecha").groupby(["material_codigo", "pedido"]):
        actual, anterior = None, None
        for r in g.itertuples():
            if actual is None or (r.fecha - anterior).days > _DIAS_ENTRE_BARCOS:
                actual = {"material_codigo": material, "pedido": pedido, "llegada": r.fecha, "toneladas": 0.0, "importe": 0.0}
                barcos.append(actual)
            actual["toneladas"] += r.toneladas
            actual["importe"] += r.importe
            anterior = r.fecha
    barcos = pd.DataFrame([b for b in barcos if b["toneladas"] > 0.5])
    if barcos.empty:
        return vacio
    barcos = barcos.sort_values(["material_codigo", "llegada"]).reset_index(drop=True)
    barcos["embarque"] = [f"{p} ({formato_mes_anio_abrev(f)})" for p, f in zip(barcos["pedido"], barcos["llegada"])]

    partidas = _query(f"""
        SELECT material_codigo, numero_documento, fecha_contabilizacion AS fecha, centro, clase_documento,
               documento_compras, texto, referencia, importe
        FROM gestion_ajustepreciofbl3n
        WHERE material_codigo IN ({",".join(_texto_sql(m) for m in barcos["material_codigo"].unique())}) AND importe <> 0
    """)
    partidas["fecha"] = pd.to_datetime(partidas["fecha"]).dt.date
    partidas["importe"] = partidas["importe"].astype(float)
    partidas["embarque"], partidas["criterio"] = _embarque_de_partidas(partidas, barcos)
    ordenes = partidas["clase_documento"].isin(CLASES_LIQUIDACION_ORDENES)
    suma = partidas[partidas["embarque"].notna()].assign(
        ajuste=partidas["importe"].where(~ordenes, 0.0), ajuste_ordenes=partidas["importe"].where(ordenes, 0.0)
    ).groupby("embarque").agg(ajuste=("ajuste", "sum"), ajuste_ordenes=("ajuste_ordenes", "sum"),
                              partidas=("importe", "size"))
    barcos = barcos.merge(suma, left_on="embarque", right_index=True, how="left")
    for c in ("ajuste", "ajuste_ordenes"):
        barcos[c] = barcos[c].fillna(0.0)
    barcos["partidas"] = barcos["partidas"].fillna(0).astype(int)

    # Ajustes por humedad: al último barco llegado antes (sin pedido ni texto
    # que lo cite, criterio "fecha"); los anteriores al primer barco, fuera.
    humedad = _query(f"""
        SELECT material_codigo, fecha_contabilizacion AS fecha, SUM(cantidad) AS toneladas
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({",".join(_texto_sql(m) for m in barcos["material_codigo"].unique())})
              AND clase_movimiento IN {CLASES_AJUSTE_HUMEDAD} AND fecha_contabilizacion IS NOT NULL
        GROUP BY material_codigo, fecha_contabilizacion
    """)
    barcos["humedad"] = 0.0
    if not humedad.empty:
        humedad["fecha"] = pd.to_datetime(humedad["fecha"]).dt.date
        humedad["toneladas"] = humedad["toneladas"].astype(float)
        humedad = humedad.assign(documento_compras="", texto="", referencia="")
        humedad["embarque"], _ = _embarque_de_partidas(humedad, barcos)
        por_barco = humedad[humedad["embarque"].notna()].groupby("embarque")["toneladas"].sum()
        barcos["humedad"] = barcos["embarque"].map(por_barco).fillna(0.0)
    return (barcos[vacio[0].columns].reset_index(drop=True),
            partidas[vacio[1].columns].reset_index(drop=True))


# Centro de acopio (regla del usuario, 2026-10-20): Atocongo recibe Carbón
# Importado de U004 y reenvía una parte a Condorcocha. Para no contar dos
# veces ni cargarle a Atocongo lo que no consume, Atocongo se queda solo con lo
# que no reenvía y Condorcocha cuenta lo que recibe de Atocongo (641 desde
# U001, neto de anulaciones 642), cada tonelada con el costo del barco del
# que salió: lo reenviado en un mes se resta del último barco llegado a
# Atocongo (y si no alcanza, del anterior). centro de acopio -> centro al que
# reenvía.
ACOPIO_IMPORTADOS = {"U001": "U002"}


def _reenvios_acopio(materiales):
    """Lo que cada centro de ACOPIO_IMPORTADOS reenvía a su destino (641 con
    cantidad > 0 en el destino, neto de 642), por material/mes: origen,
    centro (destino), mes, toneladas, entradas."""
    columnas = ["material_codigo", "origen", "centro", "mes", "toneladas", "entradas"]
    partes = []
    for origen, destino in ACOPIO_IMPORTADOS.items():
        partes.append(_query(f"""
            SELECT r.material_codigo AS material_codigo, '{origen}' AS origen, r.centro AS centro,
                   substr(r.fecha_contabilizacion, 1, 7) || '-01' AS mes, SUM(r.cantidad) AS toneladas,
                   SUM(CASE WHEN r.clase_movimiento = '641' THEN 1 ELSE 0 END) AS entradas
            FROM gestion_movimientomaterial r
            WHERE r.material_codigo IN ({",".join(_texto_sql(m) for m in materiales)}) AND r.centro = '{destino}'
                  AND ((r.clase_movimiento = '641' AND r.cantidad > 0) OR (r.clase_movimiento = '642' AND r.cantidad < 0))
                  AND EXISTS (SELECT 1 FROM gestion_movimientomaterial o
                              WHERE o.documento_material = r.documento_material
                                    AND o.ejercicio_documento_material = r.ejercicio_documento_material
                                    AND o.material_codigo = r.material_codigo AND o.centro = '{origen}')
            GROUP BY r.material_codigo, r.centro, mes
        """))
    df = pd.concat(partes, ignore_index=True) if partes else pd.DataFrame(columns=columnas)
    df["toneladas"] = df["toneladas"].astype(float)
    return df[columnas]


@_cacheado
def reparto_por_embarque():
    """Lo que entra a cada planta desde U004 (material/centro/mes, mismos
    traslados que _puesto_en_destino) valorizado por FIFO con sus embarques,
    en toneladas secas (menos la humedad de sus barcos) y con el reenvío del
    centro de acopio (ACOPIO_IMPORTADOS): toneladas, importe (MB51 de los
    barcos + valor del traslado de lo que no tiene barco cargado), ajuste,
    ajuste_ordenes, entradas, reenviado (T que llegaron desde el centro de
    acopio), sin_embarque (T) y embarques (texto)."""
    columnas = ["material_codigo", "centro", "mes", "toneladas", "importe", "ajuste", "ajuste_ordenes",
                "entradas", "reenviado", "sin_embarque", "embarques"]
    barcos, _ = embarques_importados()
    if barcos.empty:
        return pd.DataFrame(columns=columnas)
    materiales = sorted(barcos["material_codigo"].unique())
    traslados = _traslados_desde_importacion(materiales)
    reenvios = _reenvios_acopio(materiales)
    filas = []
    for material, t in traslados.groupby("material_codigo"):
        # Costos por tonelada SECA de cada barco (la humedad baja sus toneladas).
        cola = []
        for b in barcos[barcos["material_codigo"] == material].itertuples():
            secas = b.toneladas + b.humedad
            cola.append({"embarque": b.embarque, "llegada": b.llegada.replace(day=1), "quedan": b.toneladas,
                         "seca": secas / b.toneladas, "mb51": b.importe / secas, "ajuste": b.ajuste / secas,
                         "ordenes": b.ajuste_ordenes / secas})
        # 1) Lo que cada planta recibe de U004, por FIFO de barcos: un lote por
        #    barco (o sin barco) y mes, con sus costos por tonelada seca.
        lotes = []
        for r in t.sort_values(["mes", "centro"]).itertuples():
            mes = datetime.date.fromisoformat(r.mes)
            falta, primero = r.toneladas, True
            for b in cola:
                if falta <= 1e-6:
                    break
                if b["quedan"] <= 1e-6 or b["llegada"] > mes:
                    continue
                toma = min(falta, b["quedan"])
                lotes.append({"centro": r.centro, "mes": r.mes, "embarque": b["embarque"], "cantidad": toma * b["seca"],
                              "mb51": b["mb51"], "ajuste": b["ajuste"], "ordenes": b["ordenes"], "sale": 0.0,
                              "entradas": r.entradas if primero else 0})
                b["quedan"] -= toma; falta -= toma; primero = False
            if falta > 1e-6:  # sin barco cargado: al valor del traslado, sin ajustes
                lotes.append({"centro": r.centro, "mes": r.mes, "embarque": None, "cantidad": falta,
                              "mb51": r.importe / r.toneladas, "ajuste": 0.0, "ordenes": 0.0, "sale": 0.0,
                              "entradas": r.entradas if primero else 0})
        # 2) Reenvíos del centro de acopio: lo reenviado en un mes sale del
        #    último barco llegado al acopio hasta ese mes (y si no alcanza, del
        #    anterior) y entra al destino ese mes, con el costo de su barco. No
        #    FIFO: el acopio también consume lo suyo, así que lo de un barco
        #    viejo ya no está para reenviarse meses después.
        recibidos = []
        for f in reenvios[reenvios["material_codigo"] == material].sort_values("mes").itertuples():
            falta = f.toneladas
            disponibles = [x for x in lotes if x["centro"] == f.origen and x["mes"] <= f.mes][::-1]
            primero = True
            for x in disponibles:
                if falta <= 1e-6:
                    break
                libre = x["cantidad"] - x["sale"]
                if libre <= 1e-6:
                    continue
                toma = min(falta, libre)
                x["sale"] += toma; falta -= toma
                recibidos.append({**x, "centro": f.centro, "mes": f.mes, "cantidad": toma, "sale": 0.0,
                                  "entradas": f.entradas if primero else 0, "reenviado": toma})
                primero = False
            if falta > 1e-6 and disponibles:  # más de lo recibido: al costo del último lote
                ultimo = disponibles[0]
                recibidos.append({**ultimo, "centro": f.centro, "mes": f.mes, "cantidad": falta, "sale": 0.0,
                                  "entradas": f.entradas if primero else 0, "reenviado": falta})
        # 3) Por planta y mes: lo que se queda (cantidad − reenviado) y lo recibido.
        por_fila = {}
        for x in [{**x, "cantidad": x["cantidad"] - x["sale"], "reenviado": 0.0} for x in lotes] + recibidos:
            f = por_fila.setdefault((x["centro"], x["mes"]), {
                "material_codigo": material, "centro": x["centro"], "mes": x["mes"], "toneladas": 0.0, "importe": 0.0,
                "ajuste": 0.0, "ajuste_ordenes": 0.0, "entradas": 0, "reenviado": 0.0, "sin_embarque": 0.0,
                "embarques": []})
            f["toneladas"] += x["cantidad"]; f["importe"] += x["cantidad"] * x["mb51"]
            f["ajuste"] += x["cantidad"] * x["ajuste"]; f["ajuste_ordenes"] += x["cantidad"] * x["ordenes"]
            f["entradas"] += x["entradas"]; f["reenviado"] += x["reenviado"]
            if x["embarque"] is None:
                f["sin_embarque"] += x["cantidad"]
            elif x["cantidad"] > 1e-6 and x["embarque"] not in f["embarques"]:
                f["embarques"].append(x["embarque"])
        for f in por_fila.values():
            f["embarques"] = ", ".join(f["embarques"])
            filas.append(f)
    return pd.DataFrame(filas, columns=columnas).sort_values(["material_codigo", "centro", "mes"]).reset_index(drop=True)


def _puesto_en_destino(df):
    """Regla del usuario (2026-10-14): el IPU no considera el centro de
    importación U004, sino el valor PUESTO EN DESTINO (U001/U002). Las
    compras de `df` en U004 se reemplazan por lo que cada planta recibe de
    U004 por traslado (641, neto de anulaciones 642, lado receptor): misma
    cantidad, al valor con que entra a la planta. Lo que el centro de acopio
    (Atocongo) reenvía a Condorcocha cuenta en Condorcocha y no en Atocongo
    (ACOPIO_IMPORTADOS, importados por embarque). Lo comprado en U004 que
    nunca se traslada (repuestos y suministros del muelle, ~S/ 8 MM) queda
    fuera del análisis.
    `df`: material_codigo, centro, mes (texto 'AAAA-MM-01'), toneladas,
    importe, entradas, proveedores (salida del SQL de los precios)."""
    if df.empty or not (df["centro"] == CENTRO_IMPORTACION).any():
        return df
    materiales = sorted(df.loc[df["centro"] == CENTRO_IMPORTACION, "material_codigo"].unique())
    destino = _traslados_desde_importacion(materiales).copy()
    # Importados por embarque: lo recibido sale de reparto_por_embarque (el
    # valor MB51 del barco de donde salió por FIFO, no el valor del traslado
    # que trae revaluaciones de U004 de otros barcos; toneladas secas; el
    # reenvío del centro de acopio ya repartido entre las plantas).
    reparto = reparto_por_embarque()
    if not reparto.empty:
        # Un mes en que el acopio reenvió todo lo recibido queda sin toneladas.
        propios = reparto[reparto["material_codigo"].isin(materiales) & (reparto["toneladas"] >= 0.5)]
        destino = pd.concat([destino[~destino["material_codigo"].isin(set(reparto["material_codigo"]))],
                             propios[["material_codigo", "centro", "mes", "toneladas", "importe", "entradas"]]],
                            ignore_index=True)
    proveedores = df[df["centro"] == CENTRO_IMPORTACION].groupby("material_codigo")["proveedores"].max()
    destino["proveedores"] = destino["material_codigo"].map(proveedores).fillna(1).astype(int)
    resto = df[df["centro"] != CENTRO_IMPORTACION]
    # Un material con compra local y traslado desde U004 en el mismo centro y
    # mes: se suman (entra a la planta por las dos vías).
    junto = pd.concat([resto, destino[resto.columns]], ignore_index=True)
    junto["toneladas"] = junto["toneladas"].astype(float)
    junto["importe"] = junto["importe"].astype(float)
    return junto.groupby(["material_codigo", "centro", "mes"], as_index=False).agg(
        toneladas=("toneladas", "sum"), importe=("importe", "sum"),
        entradas=("entradas", "sum"), proveedores=("proveedores", "max"))


# Cambios de código (definidos por el usuario, 2026-10-19): para los precios y
# el IPU, las compras del código antiguo cuentan como del código nuevo, así la
# serie de precios arranca en 2024 y no en el cambio de código.
# - 07-00000005 (Caliza Terceros, Atocongo) se separó en jul-2025 por
#   proveedor: Roca Fuerte (1000717) -> 07-00003137 y Andina (10006751) ->
#   07-00003138. Sus ajustes de la FBL3N (casi todos cambios de precio MR22,
#   sin proveedor) van todos a Roca Fuerte (AJUSTES_CODIGOS_ANTERIORES).
# - 07-00000008 Pelets Chips Recuperados (Atocongo) -> 07-00003147.
CODIGOS_ANTERIORES_POR_PROVEEDOR = {"07-00000005": {"1000717": "07-00003137", "10006751": "07-00003138"}}
CODIGOS_ANTERIORES = {"07-00000008": "07-00003147"}
# Código al que van los ajustes de la FBL3N de cada código antiguo.
AJUSTES_CODIGOS_ANTERIORES = {**CODIGOS_ANTERIORES, "07-00000005": "07-00003137"}


def _sql_material_vigente(material="material_codigo", proveedor="proveedor"):
    """Expresión SQL con el código vigente de cada línea (ver CODIGOS_ANTERIORES)."""
    casos = [f"WHEN {material} = '{a}' AND {proveedor} = '{p}' THEN '{n}'"
             for a, por in CODIGOS_ANTERIORES_POR_PROVEEDOR.items() for p, n in por.items()]
    casos += [f"WHEN {material} = '{a}' THEN '{n}'" for a, n in CODIGOS_ANTERIORES.items()]
    return f"(CASE {' '.join(casos)} ELSE {material} END)" if casos else material


def _con_codigos_anteriores(materiales):
    """`materiales` más los códigos antiguos que pasan a alguno de ellos."""
    nuevos = set(materiales)
    anteriores = {a for a, n in CODIGOS_ANTERIORES.items() if n in nuevos}
    anteriores |= {a for a, por in CODIGOS_ANTERIORES_POR_PROVEEDOR.items() if nuevos & set(por.values())}
    return sorted(nuevos | anteriores)


def _sql_traspasos_cambio_codigo():
    """Condición SQL de las entradas por traspaso entre materiales (309) con
    que el stock de un código antiguo pasó a su código nuevo: cuentan como
    compra del código nuevo, con su cantidad y valor (pedido del usuario,
    2026-10-20; así lo mide su Excel de IPU). Jul-25 "CAMBIO COD. COORD
    COSTOS": 07-00000005 -> Roca Fuerte 5,778 T y Andina 8,667 T, a S/ 55.75.
    Solo el lado que entra (cantidad > 0) y solo documentos donde sale un
    código antiguo; "0" si no hay ninguno."""
    antiguos = sorted(set(CODIGOS_ANTERIORES) | set(CODIGOS_ANTERIORES_POR_PROVEEDOR))
    if not antiguos:
        return "0"
    docs = _query(f"""
        SELECT DISTINCT documento_material AS doc FROM gestion_movimientomaterial
        WHERE material_codigo IN ({",".join(_texto_sql(m) for m in antiguos)}) AND clase_movimiento = '309' AND cantidad < 0
    """)
    if docs.empty:
        return "0"
    lista = ",".join(_texto_sql(d) for d in docs["doc"])
    return (f"(clase_movimiento = '309' AND cantidad > 0 AND documento_material IN ({lista}) "
            f"AND material_codigo NOT IN ({','.join(_texto_sql(m) for m in antiguos)}))")


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
    # 101 de todos los materiales). Mismo resultado. Los códigos antiguos
    # (CODIGOS_ANTERIORES) entran con su código nuevo.
    vigente = _sql_material_vigente()
    df = _query(f"""
        SELECT {vigente} AS material_codigo, centro AS centro,
               substr(fecha_contabilizacion, 1, 7) || '-01' AS mes,
               SUM(cantidad) AS toneladas, SUM(importe_moneda_local) AS importe,
               SUM(CASE WHEN clase_movimiento = '101' THEN 1 ELSE 0 END) AS entradas,
               COUNT(DISTINCT NULLIF(proveedor, '')) AS proveedores
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({",".join(f"'{m}'" for m in _con_codigos_anteriores(materiales))})
              AND ((+clase_movimiento IN {_CLASES_ENTRADA_COMPRA} AND proveedor <> '') OR {_sql_traspasos_cambio_codigo()})
              AND fecha_contabilizacion IS NOT NULL
        GROUP BY {vigente}, centro, mes
    """)
    df = _produccion_propia(_puesto_en_destino(df), set(materiales))
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
    vigente = _sql_material_vigente()  # códigos antiguos con su código nuevo
    df = _query(f"""
        SELECT {vigente} AS material_codigo, centro AS centro,
               substr(fecha_contabilizacion, 1, 7) || '-01' AS mes,
               SUM(cantidad) AS toneladas, SUM(importe_moneda_local) AS importe,
               SUM(CASE WHEN clase_movimiento = '101' THEN 1 ELSE 0 END) AS entradas,
               COUNT(DISTINCT NULLIF(proveedor, '')) AS proveedores
        FROM gestion_movimientomaterial
        WHERE ((clase_movimiento IN {_CLASES_ENTRADA_COMPRA} AND proveedor <> '') OR {_sql_traspasos_cambio_codigo()})
              AND material_codigo <> '' AND fecha_contabilizacion IS NOT NULL
        GROUP BY {vigente}, centro, mes
    """)
    df = _produccion_propia(_puesto_en_destino(df))
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


def concepto_ajuste(clase, transaccion, texto):
    """De qué trata un ajuste de la FBL3N, según su clase de documento, código
    de transacción y texto (reglas vistas en las extracciones 2024-2026)."""
    texto = (texto or "").upper()
    if clase == "PR":
        return "Cambio de precio (MR22)"
    if clase in CLASES_LIQUIDACION_ORDENES:
        return "Liquidación de órdenes (KO88)"
    if transaccion == "MR8M" or clase in ("XK", "XS"):
        return "Anulación de factura (MR8M)"
    if transaccion == "MIRO":
        if "ADUAN" in texto or "DUA" in texto:
            return "Cargos de importación"
        if "FLETE" in texto or "TRANSP" in texto:
            return "Flete / transporte"
        if "DESESTIBA" in texto or "DESCARGA" in texto:
            return "Desestiba / descarga"
        if "ANTICIPO" in texto:
            return "Liquidación de anticipo"
        return "Factura de proveedor (MIRO)"
    if transaccion.startswith("FB"):
        return "Asiento contable manual"
    return "Otros"


# Partidas de la FBL3N que NO entran al precio real (pedido del usuario,
# 2026-10-20), solo para ese material: (material, número de documento). En la
# CKM3N de mar-26 de la Puzolana de Terceros (U001) aparecen como "Otras
# entradas/consumos > Debe/haber" (cargo/descargo de material, MR22), fuera
# de Entradas (855.517 T, S/ 110,573.92): sumarlas bajaba el precio de 129.25
# a ~112.
AJUSTES_EXCLUIDOS = [("07-00000055", "3100005265"), ("07-00000055", "3100005280")]


def _sql_ajuste_excluido(material="material_codigo", documento="numero_documento"):
    """Condición SQL verdadera para las partidas de AJUSTES_EXCLUIDOS."""
    if not AJUSTES_EXCLUIDOS:
        return "0"
    return "(" + " OR ".join(f"({material} = {_texto_sql(m)} AND {documento} = {_texto_sql(d)})"
                             for m, d in AJUSTES_EXCLUIDOS) + ")"


@_cacheado
def ajustes_precio_fbl3n():
    """Ajustes de la FBL3N sumados por material/centro/mes: ajuste (S/, entra
    al precio real), ajuste_ordenes (S/, liquidaciones de órdenes, aparte) y
    lineas. Vacío si todavía no se cargó la FBL3N. Los códigos antiguos
    pasan al código nuevo de AJUSTES_CODIGOS_ANTERIORES. Sin las partidas de
    AJUSTES_EXCLUIDOS."""
    df = _query(f"""
        SELECT material_codigo, centro, substr(fecha_contabilizacion, 1, 7) || '-01' AS mes,
               SUM(CASE WHEN clase_documento IN {CLASES_LIQUIDACION_ORDENES} THEN 0 ELSE importe END) AS ajuste,
               SUM(CASE WHEN clase_documento IN {CLASES_LIQUIDACION_ORDENES} THEN importe ELSE 0 END) AS ajuste_ordenes,
               COUNT(*) AS lineas
        FROM gestion_ajustepreciofbl3n
        WHERE NOT {_sql_ajuste_excluido()}
        GROUP BY material_codigo, centro, mes
    """)
    if not df.empty:
        df["mes"] = pd.to_datetime(df["mes"]).dt.date
        df["material_codigo"] = df["material_codigo"].replace(AJUSTES_CODIGOS_ANTERIORES)
        df = df.groupby(["material_codigo", "centro", "mes"], as_index=False).agg(
            ajuste=("ajuste", "sum"), ajuste_ordenes=("ajuste_ordenes", "sum"), lineas=("lineas", "sum"))
    return df


def meses_con_fbl3n():
    """Meses cubiertos por alguna extracción de la FBL3N (para no mostrar como
    "precio real" un mes que no tiene sus ajustes cargados)."""
    df = _query("SELECT DISTINCT substr(fecha_contabilizacion, 1, 7) || '-01' AS mes FROM gestion_ajustepreciofbl3n")
    return {datetime.date.fromisoformat(m) for m in df["mes"]}


def con_precio_real(base):
    """`base` (salida de precios_entrada_sop / precios_entrada_materiales) con
    el precio real: importe_mb51 y precio_mb51 (lo de antes), ajuste,
    ajuste_trasladado (la parte que vino de meses sin compra o de otro
    centro), ajuste_ordenes (aparte), con_fbl3n (el mes tiene la FBL3N
    cargada), y "importe"/"precio" pasan a ser los reales, para que el resto
    (resumen, gráficos, inflación) funcione igual con cualquiera de los dos."""
    df = base.copy()
    df["importe_mb51"] = df["importe"]
    df["precio_mb51"] = df["precio"]
    for c in ("ajuste", "ajuste_trasladado", "ajuste_ordenes"):
        df[c] = 0.0
    meses = meses_con_fbl3n()
    df["con_fbl3n"] = df["mes"].isin(meses)
    # Producción propia sin la FBL3N del mes (la liquidación de la orden se
    # registra en el cierre): sin la liquidación su precio sería solo el
    # traslado estándar (~S/ 7 por T), así que ese mes todavía no tiene precio.
    propio = pd.Series([(m, c) in PRODUCCION_PROPIA for m, c in zip(df["material_codigo"], df["centro"])], index=df.index)
    df = df[~(propio & ~df["con_fbl3n"])]
    aj = ajustes_precio_fbl3n()
    if df.empty or aj.empty:
        return df
    aj = aj[aj["material_codigo"].isin(set(df["material_codigo"]))].copy()
    # Lo ajustado en U004 ya viaja en el valor del traslado a la planta
    # (puesto en destino, ver _puesto_en_destino): no se suma otra vez.
    aj = aj[aj["centro"] != CENTRO_IMPORTACION]
    # Producción propia: solo la liquidación de su orden y las diferencias
    # que le pasa el centro de origen (abajo, liquidaciones_produccion_propia),
    # no el resto del cierre del Material Ledger ni lo demás de la cuenta 2310002000.
    propios = {m for m, _ in PRODUCCION_PROPIA}
    aj = aj[~aj["material_codigo"].isin(propios)]
    importados = materiales_importados()
    # Importados por embarque: sus ajustes vienen del FIFO de embarques
    # (abajo), no del mes de contabilización.
    reparto = reparto_por_embarque()
    por_embarque = set(reparto["material_codigo"])
    aj = aj[~aj["material_codigo"].isin(por_embarque)]
    columnas = ["ajuste", "ajuste_ordenes", "ajuste_trasladado"]

    # Materiales locales: al mismo centro (si nunca se compra en el centro del
    # ajuste, al único centro de compra) y al mes de contabilización (si no
    # hubo compra, al último mes anterior con compra).
    local = aj[~aj["material_codigo"].isin(importados)].copy()
    centros_compra = df.groupby("material_codigo")["centro"].agg(lambda s: set(s)).to_dict()

    def centro_destino(material, centro):
        compra = centros_compra.get(material, set())
        if centro in compra:
            return centro
        return next(iter(compra)) if len(compra) == 1 else None

    local["centro_destino"] = [centro_destino(m, c) for m, c in zip(local["material_codigo"], local["centro"])]
    local = local[local["centro_destino"].notna()]
    local = _prorratear_por_contrato(local)
    compras = df[["material_codigo", "centro", "mes"]].rename(columns={"centro": "centro_destino", "mes": "mes_destino"})
    compras["_clave"] = pd.to_datetime(compras["mes_destino"])
    local["_clave"] = pd.to_datetime(local["mes"])
    local = pd.merge_asof(local.sort_values("_clave"), compras.sort_values("_clave"), on="_clave",
                          by=["material_codigo", "centro_destino"], direction="backward")
    local = local[local["mes_destino"].notna()]
    trasladado = (local["mes_destino"] != local["mes_origen"]) | (local["centro_destino"] != local["centro"])
    local["ajuste_trasladado"] = local["ajuste"].where(trasladado, 0.0)
    partes = [local.groupby(["material_codigo", "centro_destino", "mes_destino"])[columnas].sum().reset_index()
              .rename(columns={"centro_destino": "centro", "mes_destino": "mes"})]

    # Importados (puestos en destino): el carbón que ajusta Condorcocha llegó
    # por Atocongo, así que el ajuste no se ata a la planta donde se registró:
    # va al material y mes (o al último mes anterior con llegada a planta) y se
    # reparte entre las plantas que recibieron ese mes según sus toneladas.
    imp = aj[aj["material_codigo"].isin(importados)]
    if not imp.empty:
        imp = imp.groupby(["material_codigo", "mes"], as_index=False)[["ajuste", "ajuste_ordenes"]].sum()
        llegadas = df[df["material_codigo"].isin(importados)].groupby(["material_codigo", "mes"], as_index=False)["toneladas"].sum()
        meses_llegada = llegadas[["material_codigo", "mes"]].rename(columns={"mes": "mes_destino"})
        meses_llegada["_clave"] = pd.to_datetime(meses_llegada["mes_destino"])
        imp["_clave"] = pd.to_datetime(imp["mes"])
        imp = pd.merge_asof(imp.sort_values("_clave"), meses_llegada.sort_values("_clave"), on="_clave",
                            by="material_codigo", direction="backward")
        imp = imp[imp["mes_destino"].notna()]
        imp["ajuste_trasladado"] = imp["ajuste"].where(imp["mes_destino"] != imp["mes"], 0.0)
        imp = imp.groupby(["material_codigo", "mes_destino"], as_index=False)[columnas].sum()
        filas = df.loc[df["material_codigo"].isin(importados), ["material_codigo", "centro", "mes", "toneladas"]]
        filas = filas.merge(imp.rename(columns={"mes_destino": "mes"}), on=["material_codigo", "mes"])
        participacion = filas["toneladas"] / filas.groupby(["material_codigo", "mes"])["toneladas"].transform("sum")
        for c in columnas:
            filas[c] = filas[c] * participacion
        partes.append(filas[["material_codigo", "centro", "mes", *columnas]])

    if not reparto.empty:
        r = reparto.assign(mes=pd.to_datetime(reparto["mes"]).dt.date)
        r = r.merge(df[["material_codigo", "centro", "mes"]], on=["material_codigo", "centro", "mes"])
        partes.append(r.assign(ajuste_trasladado=0.0)[["material_codigo", "centro", "mes", *columnas]])

    # Producción propia: la liquidación de la orden del mes (o, si ese mes no
    # hubo traslado, del último mes anterior con traslado).
    liq = liquidaciones_produccion_propia()
    liq = liq.merge(df[["material_codigo", "centro"]].drop_duplicates(), on=["material_codigo", "centro"]) if not liq.empty else liq
    if not liq.empty:
        destinos = df[["material_codigo", "centro", "mes"]].rename(columns={"mes": "mes_destino"})
        destinos["_clave"] = pd.to_datetime(destinos["mes_destino"])
        liq = liq.assign(_clave=pd.to_datetime(liq["mes"]))
        liq = pd.merge_asof(liq.sort_values("_clave"), destinos.sort_values("_clave"), on="_clave",
                            by=["material_codigo", "centro"], direction="backward")
        liq = liq[liq["mes_destino"].notna()]
        partes.append(pd.DataFrame({
            "material_codigo": liq["material_codigo"], "centro": liq["centro"], "mes": liq["mes_destino"],
            "ajuste": liq["liquidacion"].astype(float), "ajuste_ordenes": 0.0,
            "ajuste_trasladado": liq["liquidacion"].astype(float).where(liq["mes_destino"] != liq["mes"], 0.0)}))
    suma = pd.concat(partes, ignore_index=True).groupby(["material_codigo", "centro", "mes"], as_index=False)[columnas].sum()
    df = df.drop(columns=["ajuste", "ajuste_trasladado", "ajuste_ordenes"]).merge(
        suma, on=["material_codigo", "centro", "mes"], how="left")
    for c in ("ajuste", "ajuste_trasladado", "ajuste_ordenes"):
        df[c] = df[c].fillna(0.0)
    df["importe"] = df["importe_mb51"] + df["ajuste"]
    df["precio"] = df["importe"] / df["toneladas"]
    return df


# Ajustes de la FBL3N que se reparten entre lo recibido con un contrato marco
# (definido por el usuario, 2026-10-19): (material, mes del ajuste) ->
# contrato. El cambio de precio MR22 de mar-2026 de los Pelets ("Traslado de
# valor", S/ 726 mil, sin pedido) es de lo recibido con el contrato
# 4600000955 (nov-25 a ene-26): va a esos meses según sus toneladas, no todo
# al último mes con compra.
AJUSTES_POR_CONTRATO = {("07-00003147", datetime.date(2026, 3, 1)): "4600000955"}


def _prorratear_por_contrato(local):
    """`local` (ajustes por material/centro/mes, con centro_destino) con los
    de AJUSTES_POR_CONTRATO repartidos entre los meses en que se recibió con
    ese contrato (MB51, según toneladas). Agrega mes_origen: el mes del ajuste."""
    local = local.assign(mes_origen=local["mes"])
    regla = pd.Series([(m, mes) in AJUSTES_POR_CONTRATO for m, mes in zip(local["material_codigo"], local["mes"])],
                      index=local.index, dtype=bool)
    if not regla.any():
        return local
    vigente = _sql_material_vigente()
    materiales = _con_codigos_anteriores({m for m, _ in AJUSTES_POR_CONTRATO})
    recibido = _query(f"""
        SELECT {vigente} AS material_codigo, centro AS centro_destino, contrato_marco AS contrato,
               substr(fecha_contabilizacion, 1, 7) || '-01' AS mes_recibido, SUM(cantidad) AS toneladas
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({",".join(_texto_sql(m) for m in materiales)})
              AND contrato_marco IN ({",".join(_texto_sql(c) for c in set(AJUSTES_POR_CONTRATO.values()))})
              AND +clase_movimiento IN {_CLASES_ENTRADA_COMPRA} AND proveedor <> '' AND fecha_contabilizacion IS NOT NULL
        GROUP BY {vigente}, centro, contrato_marco, mes_recibido
    """)
    recibido = recibido[recibido["toneladas"] > 0].assign(mes_recibido=lambda r: pd.to_datetime(r["mes_recibido"]).dt.date)
    recibido["parte"] = recibido["toneladas"] / recibido.groupby(["material_codigo", "centro_destino", "contrato"])["toneladas"].transform("sum")
    partes = local[regla].assign(contrato=[AJUSTES_POR_CONTRATO[(m, mes)] for m, mes in
                                           zip(local.loc[regla, "material_codigo"], local.loc[regla, "mes"])])
    partes = partes.merge(recibido[["material_codigo", "centro_destino", "contrato", "mes_recibido", "parte"]],
                          on=["material_codigo", "centro_destino", "contrato"])
    if partes.empty:  # sin recepciones de ese contrato: el ajuste queda como estaba
        return local
    for c in ("ajuste", "ajuste_ordenes"):
        partes[c] = partes[c] * partes["parte"]
    partes["mes"] = partes["mes_recibido"]
    return pd.concat([local[~regla], partes[local.columns]], ignore_index=True)


@_cacheado
def precios_entrada_sop_real():
    """precios_entrada_sop con el precio real (ver con_precio_real)."""
    return con_precio_real(precios_entrada_sop())


@_cacheado
def precios_entrada_materiales_real():
    """precios_entrada_materiales (todos los materiales, pestañas de cada
    categoría y resumen ejecutivo) con el precio real. Desde 2026-10-14 todo
    el IPU usa el precio real (pedido del usuario)."""
    return con_precio_real(precios_entrada_materiales())


def ajustes_fbl3n_detalle(materiales, desde=None, hasta=None):
    """Las partidas de la FBL3N de `materiales` (todas sus plantas) entre dos
    meses, con su concepto, para ver de qué trata cada importe. Incluye las
    del código antiguo que van a alguno de ellos (AJUSTES_CODIGOS_ANTERIORES),
    con ese código al inicio del texto, como en el precio."""
    if not materiales:
        return pd.DataFrame()
    anteriores = [a for a, n in AJUSTES_CODIGOS_ANTERIORES.items() if n in set(materiales)]
    filtros = [f"material_codigo IN ({','.join(_texto_sql(m) for m in sorted(set(materiales) | set(anteriores)))})"]
    if desde:
        filtros.append(f"fecha_contabilizacion >= '{desde.isoformat()}'")
    if hasta:
        filtros.append(f"fecha_contabilizacion < '{_sumar_meses(hasta, 1).isoformat()}'")
    df = _query_directo(f"""
        SELECT fecha_contabilizacion, material_codigo, centro, numero_documento, clase_documento,
               codigo_transaccion, importe, moneda_documento, importe_moneda_documento, cantidad,
               unidad_medida_base, documento_compras, referencia, texto, usuario, cuenta_mayor
        FROM gestion_ajustepreciofbl3n WHERE {' AND '.join(filtros)}
        ORDER BY fecha_contabilizacion DESC, numero_documento
    """)
    if not df.empty:
        df["concepto"] = [concepto_ajuste(c, t, x) for c, t, x in
                          zip(df["clase_documento"], df["codigo_transaccion"], df["texto"])]
        df["fecha_contabilizacion"] = pd.to_datetime(df["fecha_contabilizacion"]).dt.date
        antiguo = df["material_codigo"].isin(anteriores)
        df.loc[antiguo, "texto"] = ["[" + m + "] " + (t or "") for m, t in zip(df.loc[antiguo, "material_codigo"], df.loc[antiguo, "texto"])]
        excluidos = set(AJUSTES_EXCLUIDOS)
        df["en_precio"] = ~df["clase_documento"].isin(CLASES_LIQUIDACION_ORDENES) & pd.Series(
            [(m, str(d)) not in excluidos for m, d in zip(df["material_codigo"], df["numero_documento"])], index=df.index)
        # Importados por embarque: a qué barco se asignó cada partida.
        barcos, _ = embarques_importados()
        barcos = barcos[barcos["material_codigo"].isin(materiales)]
        if not barcos.empty:
            df["embarque"], df["criterio_embarque"] = _embarque_de_partidas(
                df.rename(columns={"fecha_contabilizacion": "fecha"}), barcos)
    return df


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
# Interanual (regla del usuario, 2026-10-20): la base siempre es un precio
# del año anterior; si el último precio hasta T−12 es más antiguo, se usa el
# primero del año anterior después de T−12, y sin compra ese año no hay
# comparación (Puzolana Ica, Caliza HCIM y Puzolana Junín en 2026; Pelets
# en 2025).
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

# Precio de referencia (S/ por T) de los meses SIN compra de un material,
# definido por el usuario (2026-10-19; su referencia es el Excel "IPU Final
# 2025 P.2026 O1 ACTUALIZABLE V2", hoja MP Ext): solo sirve de base de comparación del
# IPU, no es una compra (no aparece en la evolución del precio ni suma
# toneladas). Se muestra en la matriz de precios TCO. (material, centro o
# None = todos, primer mes, último mes, precio).
PRECIOS_REFERENCIA_IPU = [
    ("07-00000002", None, datetime.date(2024, 1, 1), datetime.date(2025, 10, 1), 42.0),  # Arcilla Terceros, hasta su ingreso de nov-2025
    ("07-00001677", None, datetime.date(2024, 1, 1), datetime.date(2024, 12, 1), 69.0),  # Arcilla Ferrosa, meses de 2024 sin compra
    ("07-00001181", None, datetime.date(2024, 1, 1), datetime.date(2024, 3, 1), 103.5),  # Yeso Terceros ATO-NAC
    ("07-00000026", None, datetime.date(2024, 1, 1), datetime.date(2024, 1, 1), 55.5),  # Yeso Terceros CDC
    ("07-00000028", None, datetime.date(2024, 1, 1), datetime.date(2024, 1, 1), 76.0),  # Óxido de Fierro Terceros
    # Puzolana Ayacucho, Condorcocha: hasta may-25 (su primera compra de 2025 es
    # jun-25), como la hoja "MP Ext" del Excel IPU del usuario (2026-10-20).
    ("07-00000027", "U002", datetime.date(2024, 1, 1), datetime.date(2025, 5, 1), 93.0),
    ("07-00000027", "U001", datetime.date(2024, 1, 1), datetime.date(2025, 8, 1), 103.6),  # Puzolana Ayacucho, Atocongo
]


def bases_referencia_ipu(df):
    """Precios de referencia (PRECIOS_REFERENCIA_IPU) de los material/centro
    de `df` en los meses que `df` no tiene compra: material_codigo, centro,
    mes, precio."""
    filas = []
    con_compra = set(zip(df["material_codigo"], df["centro"], df["mes"]))
    for material, centro in df[["material_codigo", "centro"]].drop_duplicates().itertuples(index=False):
        for codigo, centro_regla, desde, hasta, precio in PRECIOS_REFERENCIA_IPU:
            if codigo != material or (centro_regla and centro_regla != centro):
                continue
            mes = desde
            while mes <= hasta:
                if (material, centro, mes) not in con_compra:
                    filas.append({"material_codigo": material, "centro": centro, "mes": mes, "precio": precio})
                mes = _sumar_meses(mes, 1)
    return pd.DataFrame(filas, columns=["material_codigo", "centro", "mes", "precio"])


def inflacion_precios(df, desfase=12):
    """Una fila por material/centro/mes con compra de `df` (salida de
    precios_entrada_sop, ya filtrada) que tenga base de comparación: precio,
    toneladas e importe del mes, mes_referencia (T − `desfase` meses),
    mes_base y precio_base usados, base_sustituida (True si el mes de
    referencia no tuvo compra y se tomó el último precio anterior),
    base_referencia (True si la base es un precio de referencia del usuario,
    PRECIOS_REFERENCIA_IPU), variacion (precio ÷ precio_base − 1), bolsa (S/)
    y atipica (ver _RAZON_ATIPICA)."""
    columnas = ["material_codigo", "texto_breve", "centro", "categoria", "mes", "precio", "toneladas", "importe",
                "mes_referencia", "mes_base", "precio_base", "base_sustituida", "base_referencia",
                "variacion", "bolsa", "atipica"]
    if df.empty:
        return pd.DataFrame(columns=columnas)
    actual = df.assign(mes_referencia=[_sumar_meses(m, -desfase) for m in df["mes"]])
    actual["_clave"] = pd.to_datetime(actual["mes_referencia"])
    bases = df[["material_codigo", "centro", "mes", "precio"]].assign(base_referencia=False)
    referencia = bases_referencia_ipu(df)
    if not referencia.empty:
        referencia = referencia.astype({"material_codigo": df["material_codigo"].dtype, "centro": df["centro"].dtype})
        bases = pd.concat([bases, referencia.assign(base_referencia=True)], ignore_index=True)
    bases = bases.rename(columns={"mes": "mes_base", "precio": "precio_base"})
    bases["_clave"] = pd.to_datetime(bases["mes_base"])
    # merge_asof "backward": el último mes_base <= mes_referencia del mismo
    # material y centro (exacto si T−12 tuvo compra).
    unido = pd.merge_asof(actual.sort_values("_clave"), bases.sort_values("_clave"), on="_clave",
                          by=["material_codigo", "centro"], direction="backward")
    if desfase == 12:
        # Regla del usuario (2026-10-20): el IPU de un año compara contra
        # precios del año anterior; un material sin compra (ni precio de
        # referencia) ese año no tiene comparación. Si el último precio hasta
        # T−12 es de antes del año anterior (o no hay), la base es el primer
        # precio del año anterior después de T−12 (Pelets: ene-26 contra
        # may-25, su primera compra); si tampoco hay, no se compara.
        anio_previo = [m.year - 1 for m in unido["mes"]]
        fuera = [b is None or pd.isna(b) or b.year != a for b, a in zip(unido["mes_base"], anio_previo)]
        fuera = pd.Series(fuera, index=unido.index)
        if fuera.any():
            adelante = pd.merge_asof(unido.loc[fuera, actual.columns].sort_values("_clave"), bases.sort_values("_clave"),
                                     on="_clave", by=["material_codigo", "centro"], direction="forward")
            adelante = adelante[[b is not None and not pd.isna(b) and b.year == m.year - 1
                                 for b, m in zip(adelante["mes_base"], adelante["mes"])]]
            unido = pd.concat([unido[~fuera], adelante], ignore_index=True)
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
