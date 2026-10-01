"""Seguimiento de qué archivos fuente SAP ya se importaron, para que
import_todo (corre a diario, desatendido) solo reprocese los archivos nuevos o
modificados en vez de los ~70 archivos completos cada vez."""

import datetime
import os
import re

from django.db import transaction

from dashboard.queries import marcar_datos_actualizados
from gestion.models import ArchivoImportado

# Ventana de actualización de las tablas mensuales (MB51, MB5B, ZMM056),
# definida por el usuario según cómo descarga el robot de SAP: hasta el día 10
# de cada mes (cierre contable) se actualizan el mes en curso y el anterior;
# desde el 11, solo el mes en curso. Los meses más antiguos ya están cerrados
# y no se vuelven a tocar aunque su archivo aparezca modificado (re-sync de
# OneDrive, una corrección manual...), salvo con --forzar.
DIA_CIERRE_MES_ANTERIOR = 10

_MESES = {
    "ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5, "JUNIO": 6, "JULIO": 7,
    "AGOSTO": 8, "SEPTIEMBRE": 9, "SETIEMBRE": 9, "OCTUBRE": 10, "NOVIEMBRE": 11, "DICIEMBRE": 12,
}
_MES_RE = re.compile("|".join(_MESES))
_ANIO_RE = re.compile(r"20\d{2}")


def _stat_archivo(ruta):
    st = os.stat(ruta)
    mtime = datetime.datetime.fromtimestamp(st.st_mtime, tz=datetime.timezone.utc)
    return st.st_size, mtime


def necesita_importar(ruta):
    """True si `ruta` es nuevo o cambió (tamaño o fecha de modificación) desde
    la última vez que se importó con éxito."""
    tamano, mtime = _stat_archivo(ruta)
    registro = ArchivoImportado.objects.filter(ruta=os.path.abspath(ruta)).first()
    if registro is None:
        return True
    return registro.tamano_bytes != tamano or registro.fecha_modificacion != mtime


def mes_de_archivo(ruta):
    """Primer día del último mes que cubre el archivo, según su nombre
    ('MB51_MAT_SUP_SEPTIEMBRE_2026' -> 2026-09-01; 'NOVIEMBRE_DICIEMBRE_2025'
    o 'FEBRERO - JULIO 2024' -> el último mes nombrado). None si el nombre no
    trae mes y año."""
    nombre = os.path.basename(ruta).upper()
    meses = _MES_RE.findall(nombre)
    anios = _ANIO_RE.findall(nombre)
    if not meses or not anios:
        return None
    return datetime.date(int(anios[-1]), _MESES[meses[-1]], 1)


def meses_en_ventana(hoy=None):
    hoy = hoy or datetime.date.today()
    actual = hoy.replace(day=1)
    if hoy.day <= DIA_CIERRE_MES_ANTERIOR:
        anterior = (actual - datetime.timedelta(days=1)).replace(day=1)
        return {actual, anterior}
    return {actual}


def fuera_de_ventana(ruta, hoy=None):
    """True si el archivo es de un mes ya cerrado y no hay que reimportarlo.
    Un archivo que nunca se importó se carga igual (p.ej. un mes histórico
    agregado recién), y uno sin mes reconocible en el nombre sigue la regla
    de siempre (solo si cambió)."""
    mes = mes_de_archivo(ruta)
    if mes is None or mes in meses_en_ventana(hoy):
        return False
    return ArchivoImportado.objects.filter(ruta=os.path.abspath(ruta)).exists()


def registrar_importado(ruta, filas):
    tamano, mtime = _stat_archivo(ruta)
    ArchivoImportado.objects.update_or_create(
        ruta=os.path.abspath(ruta),
        defaults={"tamano_bytes": tamano, "fecha_modificacion": mtime, "filas_importadas": filas},
    )
    # Datos nuevos en la base: el dashboard deja de usar la caché anterior
    # (sin esperar vencimientos ni reiniciar el servidor). Recién al confirmar
    # la transacción del archivo: antes, una consulta del dashboard en ese
    # instante guardaría datos viejos bajo la versión nueva.
    transaction.on_commit(marcar_datos_actualizados)
