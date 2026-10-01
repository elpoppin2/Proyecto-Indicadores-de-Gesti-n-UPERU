import datetime
import os
from decimal import Decimal, InvalidOperation

import openpyxl

try:
    from python_calamine import CalamineWorkbook
except ImportError:  # sin calamine instalado se lee con openpyxl, igual que antes
    CalamineWorkbook = None


def _celda_a_texto(valor):
    """Normaliza un valor de celda openpyxl a texto, imitando lo que traería la
    misma columna si viniera de un CSV (para poder reusar los helpers de texto,
    decimal y fecha de _csv_utils sin cambios)."""
    if valor is None:
        return ""
    if isinstance(valor, (datetime.datetime, datetime.date)):
        return str(valor)
    return str(valor)


def _encabezados_unicos(encabezado):
    """Renombra encabezados repetidos como pandas (mangle_dupe_cols): la primera
    aparición conserva el nombre, las siguientes agregan '.1', '.2', etc. Los
    importadores de compras ya esperan nombres como 'Pos..1' por este motivo."""
    vistos = {}
    resultado = []
    for nombre in encabezado:
        nombre = "" if nombre is None else str(nombre)
        if nombre in vistos:
            vistos[nombre] += 1
            resultado.append(f"{nombre}.{vistos[nombre]}")
        else:
            vistos[nombre] = 0
            resultado.append(nombre)
    return resultado


def _filas_openpyxl(path, hoja):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[hoja] if hoja else wb[wb.sheetnames[0]]
        yield from ws.iter_rows(values_only=True)
    finally:
        wb.close()


def _valor_como_openpyxl(v):
    if v == "":
        return None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, datetime.date) and not isinstance(v, datetime.datetime):
        return datetime.datetime(v.year, v.month, v.day)
    return v


def _filas_calamine(path, hoja):
    """Mismas filas que _filas_openpyxl, pero con calamine (lector en Rust,
    ~4-5 veces más rápido: el MB51 de un mes pasa de ~20-80 s a ~4 s), y con
    los MISMOS tipos que entrega openpyxl, para que el resto de la importación
    no cambie en nada: calamine da las fechas como date (openpyxl: datetime a
    medianoche), los números enteros como float (38.0; openpyxl: 38 — y como
    texto "38.0" rompería, p.ej., el cruce por posición de pedido), las
    celdas vacías como "" (openpyxl: None; para los importadores es lo mismo),
    y corta las filas en la última celda con dato (openpyxl: mismo ancho)."""
    libro = CalamineWorkbook.from_path(path)
    hoja_c = libro.get_sheet_by_name(hoja) if hoja else libro.get_sheet_by_index(0)
    ancho = None
    for fila in hoja_c.iter_rows():
        fila = [_valor_como_openpyxl(v) for v in fila]
        if ancho is None:
            ancho = len(fila)
        elif len(fila) < ancho:
            fila.extend([None] * (ancho - len(fila)))
        yield tuple(fila)


def _filas_xlsx(path, hoja=None):
    # UPERU_LECTOR_EXCEL=openpyxl fuerza el lector anterior (respaldo, o para
    # comparar resultados si alguna vez un archivo se lee distinto).
    if CalamineWorkbook is not None and os.environ.get("UPERU_LECTOR_EXCEL") != "openpyxl":
        return _filas_calamine(path, hoja)
    return _filas_openpyxl(path, hoja)


def leer_filas_excel_dict(path, hoja=None):
    """Generador de dicts (encabezado -> texto) para un .xlsx, en modo streaming.
    Pensado como reemplazo directo de csv.DictReader para los importadores que
    hasta ahora solo leían CSV (inventario, compras, materiales), ahora que las
    fuentes SAP llegan como Excel crudo en vez de CSV pre-convertido."""
    filas = _filas_xlsx(path, hoja)
    encabezado_crudo = next(filas, None)
    if not encabezado_crudo:
        return
    encabezado = _encabezados_unicos(encabezado_crudo)
    for fila in filas:
        if fila and any(v is not None and v != "" for v in fila):
            yield {encabezado[i]: _celda_a_texto(v) for i, v in enumerate(fila)}


def leer_filas_excel(path, hoja=None):
    """Generador de tuplas (sin encabezado) para un .xlsx, leyendo en modo streaming
    para no cargar archivos grandes completos en memoria."""
    filas = _filas_xlsx(path, hoja)
    next(filas, None)  # encabezado
    for fila in filas:
        if fila and any(v is not None and v != "" for v in fila):
            yield fila


def texto(valor):
    if valor is None:
        return ""
    if isinstance(valor, (int, float)):
        if valor == 0:
            return ""
        return str(int(valor)) if float(valor).is_integer() else str(valor)
    valor = str(valor).strip()
    return "" if valor.lower() in ("", "nan", "none", "null") else valor


def decimal(valor):
    if valor is None or valor == "":
        return None
    try:
        return Decimal(str(valor))
    except InvalidOperation:
        return None


def fecha(valor):
    if isinstance(valor, datetime.datetime):
        return valor.date()
    if isinstance(valor, datetime.date):
        return valor
    return None


def hora(valor):
    if isinstance(valor, datetime.time):
        return valor
    return None
