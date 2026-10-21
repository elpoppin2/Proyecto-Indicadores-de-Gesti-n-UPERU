import datetime
from pathlib import Path

from django import template

from dashboard import queries as q

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


_SIGLAS = {"CAPEX", "S&OP", "IPU", "PMP", "ABC", "OTIF", "TCO", "SAP"}


@register.filter
def nombre_visible(texto):
    """Nombre de página o categoría en mayúsculas y minúsculas, como en un
    informe ('MATERIAS PRIMAS' -> 'Materias primas'); las siglas quedan en
    mayúsculas (CAPEX, S&OP)."""
    if not texto:
        return texto
    palabras = [p if p.upper() in _SIGLAS else p.lower() for p in str(texto).split()]
    palabras = [p.upper() if p.upper() in _SIGLAS else p for p in palabras]
    nombre = " ".join(palabras)
    return nombre[:1].upper() + nombre[1:]


@register.filter
def soles(valor):
    return q.formato_soles(valor)


@register.filter
def soles_mm(valor):
    """Soles abreviados de la portada y el informe: S/ 12.3 MM, S/ 450 K."""
    try:
        valor = float(valor)
    except (TypeError, ValueError):  # sin dato (p.ej. inventario de CAPEX)
        return "—"
    if valor != valor:
        return "—"
    v, signo = abs(valor), "−" if valor <= -0.5 else ""  # sin "−S/ 0"
    if v >= 1e6:
        return f"{signo}S/ {v / 1e6:,.1f} MM"
    if v >= 1e3:
        return f"{signo}S/ {v / 1e3:,.0f} K"
    return f"{signo}S/ {v:,.0f}"


@register.filter
def mesanio(fecha):
    return q.formato_mes_anio(fecha) if fecha else ""


@register.filter
def corte(fecha):
    """Corte de inventario para los selectores: el mes ("Septiembre - 2026")
    si es fin de mes, o el mes y el día del último corte si el mes sigue en
    curso ("Octubre - 2026 (al 04/10)"). Ver views._cortes_inventario."""
    if not fecha:
        return ""
    texto = q.formato_mes_anio(fecha)
    siguiente = fecha + datetime.timedelta(days=1)
    return texto if siguiente.month != fecha.month else f"{texto} (al {fecha:%d/%m})"


@register.filter
def numero(valor, decimales=0):
    if valor is None:
        return ""
    try:
        if valor != valor:  # NaN
            return ""
        return f"{float(valor):z,.{int(decimales)}f}"  # z: -0.04 -> "0.0", no "-0.0"
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
