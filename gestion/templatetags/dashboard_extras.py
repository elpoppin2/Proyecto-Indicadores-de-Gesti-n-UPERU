import sys
from pathlib import Path

from django import template

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "dashboard"))
import queries as q  # noqa: E402

register = template.Library()


@register.simple_tag
def version_estatico(ruta):
    """Marca de versión de un archivo estático = su fecha de modificación: el
    navegador lo guarda en caché y solo lo vuelve a pedir cuando el archivo
    cambia (antes era la hora actual, y cada página lo descargaba de nuevo)."""
    from django.contrib.staticfiles import finders

    encontrado = finders.find(ruta)
    try:
        return int(Path(encontrado).stat().st_mtime) if encontrado else 0
    except OSError:
        return 0


@register.filter
def soles(valor):
    return q.formato_soles(valor)


@register.filter
def mesanio(fecha):
    return q.formato_mes_anio(fecha) if fecha else ""


@register.filter
def numero(valor, decimales=0):
    if valor is None:
        return ""
    try:
        if valor != valor:  # NaN
            return ""
        return f"{float(valor):,.{int(decimales)}f}"
    except (TypeError, ValueError):
        return valor


@register.filter
def centro(codigo):
    """U001 -> ATOCONGO, U002 -> CONDORCOCHA (ver queries.NOMBRES_CENTRO); el
    resto ("Todos", U004...) queda igual."""
    return q.nombre_centro(codigo)


@register.filter
def fechaiso(fecha):
    return fecha.isoformat() if fecha else ""
