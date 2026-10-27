"""Base de todas las consultas: caché versionada en dos niveles (memoria y disco), alcance de materiales, formatos y utilidades de fechas y SQL."""

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
#      compartido entre procesos: el servidor y la tarea programada.
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
    # raíz del proyecto (este archivo está en dashboard/consultas/)
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".cache_consultas"),
)
_ARCHIVO_VERSION = os.path.join(_DIR_CACHE, "version.txt")
# Huella del código de las consultas (todos los módulos de dashboard/consultas/
# y dashboard/queries.py): si cambia una consulta o un cálculo (sin que entren
# datos nuevos), lo guardado con el código anterior deja de usarse — si no, la
# caché seguiría devolviendo el resultado viejo.
_huella = hashlib.sha1()
_carpeta = os.path.dirname(os.path.abspath(__file__))
for _archivo in sorted(os.listdir(_carpeta)) + ["../queries.py"]:
    if _archivo.endswith(".py"):
        with open(os.path.join(_carpeta, _archivo), "rb") as _f:
            _huella.update(_f.read())
_HUELLA_CODIGO = _huella.hexdigest()[:10]
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
    except (OSError, EOFError, pickle.UnpicklingError, ImportError, AttributeError):
        pass  # sin guardar, o guardado con otra versión de las librerías: se recalcula
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
                 clave_alcance())
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
# Reglas propias del alcance (Logística, pedidos del usuario del 2026-10-23;
# ver zonas.py): (excluir, proveedores, plan_extra).
# - excluir: meses que se dejan fuera de un (material, centro): (material,
#   centro, mes) con mes = día 1 (Carbón Importado de set-26).
# - proveedores: los únicos proveedores (código de la ZMM056) cuyos ingresos
#   cuentan para un material: (material, (códigos...)) (Puzolana Ayacucho:
#   solo J&G Exmicom).
# - plan_extra: plan provisional que se suma al del S&OP: (material, centro,
#   mes, toneladas) (Puzolana Ayacucho en Condorcocha, set-26).
_REGLAS_ALCANCE = contextvars.ContextVar("reglas_alcance", default=((), (), ()))


@contextlib.contextmanager
def alcance_materiales(pares, excluir=(), proveedores=None, plan_extra=()):
    token = _ALCANCE_MATERIALES.set(tuple(sorted(pares)) if pares else None)
    reglas = (tuple(sorted(excluir)),
              tuple(sorted((m, tuple(sorted(c))) for m, c in (proveedores or {}).items())),
              tuple(sorted(plan_extra)))
    token_reglas = _REGLAS_ALCANCE.set(reglas)
    try:
        yield
    finally:
        _ALCANCE_MATERIALES.reset(token)
        _REGLAS_ALCANCE.reset(token_reglas)


def alcance_actual():
    """Pares (material, centro) del alcance vigente, o None (sin acotar)."""
    return _ALCANCE_MATERIALES.get()


def clave_alcance():
    """El alcance vigente con sus reglas, para las claves de caché."""
    reglas = _REGLAS_ALCANCE.get()
    return (_ALCANCE_MATERIALES.get(), reglas) if any(reglas) else _ALCANCE_MATERIALES.get()


def _sql_proveedores_alcance(material="material_codigo", proveedor="proveedor_codigo"):
    """Condición SQL (con su AND) que deja fuera los ingresos de la ZMM056
    de proveedores no admitidos por el alcance; "" si no hay regla."""
    partes = [f"({material} = {_texto_sql(m)} AND COALESCE({proveedor}, '') NOT IN "
              f"({','.join(_texto_sql(c) for c in codigos)}))"
              for m, codigos in _REGLAS_ALCANCE.get()[1]]
    return f" AND NOT ({' OR '.join(partes)})" if partes else ""


def _plan_extra_alcance(mes):
    """Plan provisional del alcance para `mes`: [(material, centro, toneladas)]."""
    mes = _mes_fecha(mes)
    return [(m, c, t) for m, c, x, t in _REGLAS_ALCANCE.get()[2] if x == mes]


def _en_alcance(df, centro=None, mes=None):
    """Filas de `df` (material_codigo + centro, o el `centro` dado si la
    consulta ya viene de un solo centro) dentro del alcance vigente, sin los
    meses excluidos (el `mes` dado si la consulta es de un solo mes, si no la
    columna "mes" de `df`)."""
    pares = _ALCANCE_MATERIALES.get()
    if pares is None or df.empty:
        return df
    pares = set(pares)
    centros = [centro] * len(df) if centro else list(df["centro"])
    dentro = [par in pares for par in zip(df["material_codigo"], centros)]
    excluir = set(_REGLAS_ALCANCE.get()[0])
    if excluir and (mes is not None or "mes" in df.columns):
        meses = [mes] * len(df) if mes is not None else list(df["mes"])
        dentro = [d and (m, c, _mes_fecha(x)) not in excluir
                  for d, m, c, x in zip(dentro, df["material_codigo"], centros, meses)]
    return df[dentro].reset_index(drop=True)


def _mes_fecha(valor):
    """Día 1 del mes de `valor` (date, datetime o texto 'AAAA-MM-DD')."""
    if isinstance(valor, str):
        valor = datetime.date.fromisoformat(valor[:10])
    if isinstance(valor, datetime.datetime):
        valor = valor.date()
    return valor.replace(day=1)


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


def _lista_materiales(material):
    """`material` de un filtro: un código o varios (lista/tupla; el filtro
    Material de Planificación permite elegir varios)."""
    return [material] if isinstance(material, str) else list(material)


def _limites_mes(fecha):
    fecha_inicio = fecha.replace(day=1)
    fecha_fin = fecha.replace(day=calendar.monthrange(fecha.year, fecha.month)[1])
    return fecha_inicio, fecha_fin


# Nombre con que se muestra cada centro en pantalla (filtros, tablas,
# gráficos): el código SAP sigue siendo el valor interno de los filtros y de
# los datos. Pedido por el usuario (2026-10-02). Otros centros (U004
# importación...) se muestran con su código.
NOMBRES_CENTRO = {"U001": "ATOCONGO", "U002": "CONDORCOCHA"}


def nombre_centro(codigo):
    return NOMBRES_CENTRO.get(codigo, codigo)


def _sumar_meses(fecha, meses):
    total = fecha.year * 12 + fecha.month - 1 + meses
    return datetime.date(total // 12, total % 12 + 1, 1)
