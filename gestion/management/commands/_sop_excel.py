"""Lectura común de los Excel mensuales de S&OP que llena Planificación a mano
('Abastecimiento SOP - <mes> <año>.xlsx', 'Consumos SOP - <mes> <año>.xlsx'):
misma forma en los dos — una fila por material/centro y una columna por mes
proyectado, con la fecha del mes en la cabecera."""

import datetime
import os
import re

from dashboard.queries import PREFIJO_SIN_CODIGO_SOP

from ._excel_utils import _filas_xlsx, decimal, texto

# Encabezado fijo: Cod. SAP, Centro Destino, Material, Destino, seguido de una
# columna por mes proyectado (fecha en la cabecera, ya viene como datetime).
COLUMNAS_FIJAS = 4

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
_PATRON_NOMBRE = re.compile(
    r"(?P<mes>" + "|".join(_MESES) + r")\s+(?P<anio>\d{4})", re.IGNORECASE
)


def mes_snapshot_desde_nombre(archivo):
    """Mes de emisión del S&OP según el nombre del archivo, p.ej.
    'Abastecimiento SOP - Setiembre 2026.xlsx' -> 2026-09-01."""
    nombre = os.path.splitext(os.path.basename(archivo))[0]
    match = _PATRON_NOMBRE.search(nombre)
    if not match:
        return None
    return datetime.date(int(match.group("anio")), _MESES[match.group("mes").lower()], 1)


def leer_matriz_mensual(archivo, hoja=None):
    """Genera un dict por celda con dato de mes: material_codigo, centro,
    descripcion, destino, mes (día 1) y valor (Decimal o None si la celda está
    vacía). Una fila sin Cod. SAP (p.ej. CALCEM) se guarda con el código
    sintético "S/C-<descripción>" (ver queries.PREFIJO_SIN_CODIGO_SOP): se
    muestra en la Evolución de inventario, pero no se cruza con datos reales.
    Sin código ni descripción, la fila se salta."""
    filas = _filas_xlsx(archivo, hoja)
    encabezado = next(filas, None)
    if not encabezado:
        return
    meses_columnas = []
    for i, valor in enumerate(encabezado[COLUMNAS_FIJAS:], start=COLUMNAS_FIJAS):
        if isinstance(valor, (datetime.datetime, datetime.date)):
            fecha_col = valor.date() if isinstance(valor, datetime.datetime) else valor
            meses_columnas.append((i, fecha_col.replace(day=1)))

    for fila in filas:
        if not fila or not any(v is not None and v != "" for v in fila):
            continue
        codigo = texto(fila[0])
        if not codigo:
            descripcion = texto(fila[2])
            if not descripcion:
                continue
            codigo = (PREFIJO_SIN_CODIGO_SOP + descripcion.upper())[:40]
        for indice, mes in meses_columnas:
            yield {
                "material_codigo": codigo,
                "centro": texto(fila[1]),
                "descripcion": texto(fila[2]),
                "destino": texto(fila[3]),
                "mes": mes,
                "valor": decimal(fila[indice]) if indice < len(fila) else None,
            }
