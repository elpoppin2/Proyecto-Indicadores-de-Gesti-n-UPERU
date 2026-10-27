"""Utilidades compartidas por las vistas: formatos (soles, meses, nombres), navegación, rangos de meses y segmentadores (filtros) de compras, inventario, categoría/centro y material."""

import calendar
import datetime
import re
from urllib.parse import urlencode

from dashboard import queries as q
from django.urls import reverse

from .. import charts, ejecutivo


# Formato de mes abreviado con guion ("Ene - 2025") para los gráficos de
# Evolución (queries.formato_mes_anio_abrev da "Ene 2025", sin guion).
_MESES_ABREV_GUION = [
    "", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
    "Jul", "Ago", "Sep", "Oct", "Nov", "Dic",
]


def _mes_abrev_guion(fecha):
    return f"{_MESES_ABREV_GUION[fecha.month]} - {fecha.year}"


def _por_str(valor_str, opciones):
    for opcion in opciones:
        if str(opcion) == valor_str:
            return opcion
    return None


def _slug(categoria):
    return categoria.lower().replace(" ", "-")


def _categorias_disponibles():
    """Unión de categorías con inventario o compras en el año más reciente
    disponible."""
    anios = q.anios_compras_disponibles()
    if not anios:
        return []
    return q.categorias_dashboard(anios[-1])


def _construir_nav(slug_activo):
    """Barra de navegación compartida por todas las páginas: PORTADA (resumen
    ejecutivo) primero, luego OVERVIEW, Planificación (S&OP, no es una
    categoría de compras/inventario) y las categorías con datos. Cada entrada
    trae su url."""
    categorias = _categorias_disponibles()
    nav = [
        {"nombre": "PORTADA", "slug": "portada", "url": reverse("portada")},
        {"nombre": "OVERVIEW", "slug": "overview", "url": reverse("overview")},
        {"nombre": "PLANIFICACIÓN", "slug": "planificacion", "url": reverse("planificacion")},
        # Nombre tal como lo pidió el usuario (sin pasar por nombre_visible).
        {"nombre": "Logística de Materias Primas", "slug": "logistica", "url": reverse("logistica_materias_primas"),
         "nombre_fijo": True},
    ]
    nav += [{"nombre": c, "slug": s, "url": reverse("categoria_detalle", kwargs={"slug": s})}
            for c, s in ((c, _slug(c)) for c in categorias)]
    for entrada in nav:
        entrada["activo"] = entrada["slug"] == slug_activo
    return nav, categorias


def _rango_meses(request, prefijo, anio_sel):
    """Selector de rango de meses (Desde/Hasta) dentro del año elegido, con
    nombre de parámetro GET propio por tabla (mes_<prefijo>_desde/hasta) — cada
    tabla que lo usa (Compras, Pareto, Fill Rate, Lead Time) filtra de forma
    independiente, cambiar una no afecta a las demás. Devuelve también el
    mismo rango un año antes, para comparativos interanuales (YoY)."""
    opciones_mes = list(range(1, 13))
    mes_desde = _por_str(request.GET.get(f"mes_{prefijo}_desde"), [str(m) for m in opciones_mes])
    mes_desde = int(mes_desde) if mes_desde else 1
    mes_hasta = _por_str(request.GET.get(f"mes_{prefijo}_hasta"), [str(m) for m in opciones_mes])
    mes_hasta = int(mes_hasta) if mes_hasta else _hasta_defecto(anio_sel)
    if mes_hasta < mes_desde:
        mes_hasta = mes_desde

    fecha_ini = datetime.date(anio_sel, mes_desde, 1)
    fecha_fin = datetime.date(anio_sel, mes_hasta, calendar.monthrange(anio_sel, mes_hasta)[1])
    return {
        "mes_desde": mes_desde, "mes_hasta": mes_hasta,
        "opciones_mes": [{"numero": m, "nombre": q.nombre_mes(m)} for m in opciones_mes],
        "fecha_ini": fecha_ini, "fecha_fin": fecha_fin,
        "fecha_ini_anterior": fecha_ini.replace(year=anio_sel - 1),
        "fecha_fin_anterior": fecha_fin.replace(year=anio_sel - 1),
    }


def _hasta_defecto(anio):
    """Mes "Hasta" por defecto de un año de compras: diciembre en un año
    cerrado y el mes en curso en el año actual, así la comparación contra el
    año anterior es de los mismos meses y no de un año parcial contra uno
    completo. El mes en curso es el de la última compra cargada si es anterior
    a hoy (en una copia a un corte, p.ej. a setiembre, no dice "Ene–Oct")."""
    hoy = datetime.date.today()
    ultima = q.ultima_fecha_compras()
    if ultima and ultima < hoy:
        hoy = ultima
    return hoy.month if anio == hoy.year else 12


def _texto_rango(rango, anio):
    """'Ene–Oct 2026', o '2025' si el rango es el año completo."""
    if rango["mes_desde"] == 1 and rango["mes_hasta"] == 12:
        return str(anio)
    desde, hasta = _MESES_ABREV_GUION[rango["mes_desde"]], _MESES_ABREV_GUION[rango["mes_hasta"]]
    return f"{desde} {anio}" if desde == hasta else f"{desde}–{hasta} {anio}"


def _rango_en_anio(rango, anio):
    """Las fechas del mismo rango de meses (Desde–Hasta) en otro año."""
    fin = datetime.date(anio, rango["mes_hasta"], calendar.monthrange(anio, rango["mes_hasta"])[1])
    return datetime.date(anio, rango["mes_desde"], 1), fin


# Cuántos proveedores ver en Concentración de proveedores (Pareto).
_TOPS_PROVEEDORES = (5, 10, 20)


def _segmentadores_compras(request):
    """Segmentadores del bloque Compras (Overview y pestaña Compras de cada
    categoría): Año, Desde, Hasta y cuántos proveedores ver. Rigen todas las
    secciones del bloque (Compras, Pareto, Fill Rate, Lead Time). Pedido por
    el usuario ("afina los segmentadores"): antes cada sección tenía su
    propio Desde/Hasta y el año solo se elegía en Compras del Overview."""
    anios = q.anios_compras_disponibles()
    if not anios:
        return None
    anio = _por_str(request.GET.get("anio_compras"), [str(a) for a in anios])
    anio = int(anio) if anio else anios[-1]
    top = _por_str(request.GET.get("top_compras"), [str(t) for t in _TOPS_PROVEEDORES])
    rango = _rango_meses(request, "compras", anio)
    top = int(top) if top else _TOPS_PROVEEDORES[0]
    return {"anios": list(reversed(anios)), "anio": anio, "rango": rango, "texto": _texto_rango(rango, anio),
            "tops": _TOPS_PROVEEDORES, "top": top,
            # Para abrir una categoría desde el Overview con el mismo período.
            "qs": urlencode({"anio_compras": anio, "mes_compras_desde": rango["mes_desde"],
                             "mes_compras_hasta": rango["mes_hasta"], "top_compras": top})}


def _proveedor_unico_periodo(sc, categoria=None):
    """Dependencia de proveedor único en el período de los segmentadores de
    compras (ver ejecutivo.proveedor_unico): un material es recurrente si se
    compró en 3 meses o más (o en todos, si el período es más corto)."""
    rango = sc["rango"]
    meses = rango["mes_hasta"] - rango["mes_desde"] + 1
    minimo = min(ejecutivo.cargar_metas()["supuestos"]["meses_compra_recurrente"], meses)
    return {**ejecutivo.proveedor_unico(rango["fecha_ini"], rango["fecha_fin"], categoria, minimo), "meses_minimos": minimo}


def _cortes_inventario():
    """Un corte de inventario por mes: el último de cada mes (fin de mes o, en
    el mes en curso, el último día cargado). Desde Set-26 llegan cortes
    diarios y los selectores repetían "Septiembre - 2026" una vez por día."""
    return sorted({(f.year, f.month): f for f in q.fechas_inventario_disponibles()}.values())


def _segmentadores_inventario(request, categoria=None):
    """Segmentadores del bloque Inventario (Overview y pestaña Inventario de
    cada categoría): Corte, Familia y Centro, en cascada (las familias del
    corte; los centros de la familia elegida). Rigen todas las secciones del
    bloque; antes cada una tenía su propio Mes/Familia/Centro. Devuelve
    también el inventario del corte ya filtrado ("inv")."""
    cortes = _cortes_inventario()
    mes = _por_str(request.GET.get("mes_inv"), cortes) or cortes[-1]
    inv = q.inventario_por_material(mes)
    if categoria:
        inv = inv[inv["categoria"] == categoria]
    familias = ["Todas"] + sorted(f for f in inv["familia"].dropna().unique().tolist() if f)
    familia_sel = request.GET.get("familia_inv", "Todas")
    if familia_sel not in familias:
        familia_sel = "Todas"
    if familia_sel != "Todas":
        inv = inv[inv["familia"] == familia_sel]
    centros = ["Todos"] + sorted(c for c in inv["centro"].dropna().unique().tolist() if c)
    centro_sel = request.GET.get("centro_inv", "Todos")
    if centro_sel not in centros:
        centro_sel = "Todos"
    if centro_sel != "Todos":
        inv = inv[inv["centro"] == centro_sel]
    filtros = [f for f in (None if familia_sel == "Todas" else familia_sel,
                           None if centro_sel == "Todos" else q.nombre_centro(centro_sel)) if f]
    return {
        "cortes": list(reversed(cortes)), "mes": mes, "familias": familias, "familia_sel": familia_sel,
        "centros": centros, "centro_sel": centro_sel,
        "familia": None if familia_sel == "Todas" else familia_sel,
        "centro": None if centro_sel == "Todos" else centro_sel,
        "texto_filtros": " · ".join(filtros), "inv": inv,
    }


def _por_familia_centro(df, si):
    """`df` (con columnas familia y centro) filtrado por la Familia y el Centro
    de los segmentadores de Inventario `si`."""
    if si["familia"]:
        df = df[df["familia"] == si["familia"]]
    if si["centro"]:
        df = df[df["centro"] == si["centro"]]
    return df


def _serie_inventario(cortes, categoria=None, familia=None, centro=None):
    """Valor del inventario en cada corte de `cortes` (fecha, valor). Sin
    Familia ni Centro usa las series ya agregadas por fecha; con alguno,
    suma inventario_por_material de cada corte (en caché)."""
    if not cortes:
        return []
    if familia is None and centro is None:
        if categoria:
            s = q.valor_inventario_mensual_por_categoria(fecha_desde=cortes[0])
            s = s[s["categoria"] == categoria]
        else:
            s = q.valor_inventario_mensual()
        valores = {f: float(v) for f, v in zip(s["fecha"], s["valor_total"])}
        return [(c, valores[c]) for c in cortes if c in valores]
    serie = []
    for c in cortes:
        inv = q.inventario_por_material(c)
        if categoria:
            inv = inv[inv["categoria"] == categoria]
        if familia:
            inv = inv[inv["familia"] == familia]
        if centro:
            inv = inv[inv["centro"] == centro]
        serie.append((c, float(inv["valor_total"].sum())))
    return serie


def _evolucion_inventario(request, si, categoria=None):
    """Evolución del valor del inventario con su rango propio (Desde/Hasta,
    entre los cortes mensuales): por defecto, los 12 meses que terminan en el
    corte de los segmentadores; con su Familia y Centro."""
    cortes = list(reversed(si["cortes"]))
    fin = _por_str(request.GET.get("mes_fin_evo"), cortes) or si["mes"]
    opciones_inicio = [c for c in cortes if c <= fin]
    inicio = (_por_str(request.GET.get("mes_inicio_evo"), opciones_inicio)
              or opciones_inicio[max(0, len(opciones_inicio) - 13)])
    rango = [c for c in cortes if inicio <= c <= fin]
    serie = _serie_inventario(rango, categoria, si["familia"], si["centro"])
    return {
        "grafico": charts.serie_area([(_mes_abrev_guion(f), v) for f, v in serie]),
        "mes_inicio": inicio, "mes_fin": fin,
        "opciones_inicio": list(reversed(opciones_inicio)), "opciones_fin": list(reversed(cortes)),
    }


def _opciones_categoria_centro(df):
    """Opciones de Categoría/Centro presentes en `df` (debe tener esas
    columnas), con el primer valor "Todas"/"Todos" para el segmentador de
    Planificación (Materias Primas / Energía y Combustibles)."""
    categorias = ["Todas"] + sorted(c for c in df["categoria"].dropna().unique().tolist() if c)
    centros = ["Todos"] + sorted(c for c in df["centro"].dropna().unique().tolist() if c)
    return categorias, centros


def _selector_categoria_centro(request, prefijo, opciones_categoria, opciones_centro):
    categoria_sel = request.GET.get(f"categoria_{prefijo}", "Todas")
    if categoria_sel not in opciones_categoria:
        categoria_sel = "Todas"
    centro_sel = request.GET.get(f"centro_{prefijo}", "Todos")
    if centro_sel not in opciones_centro:
        centro_sel = "Todos"
    return categoria_sel, centro_sel


def _opciones_material(df):
    """Opciones de Material (código — descripción) presentes en `df`, con
    "Todos" primero — segmentador en lista desplegable de Detalle por
    material/Evaluación diaria de Planificación (sin buscador de texto)."""
    filas = df[["material_codigo", "texto_breve"]].drop_duplicates().sort_values("material_codigo")
    opciones = [{"codigo": "Todos", "texto": "Todos"}]
    opciones += [
        {"codigo": fila.material_codigo, "texto": f"{fila.material_codigo} — {fila.texto_breve}".rstrip(" —")}
        for fila in filas.itertuples()
    ]
    return opciones


def _selector_material(request, prefijo, opciones_material):
    """Materiales elegidos en el filtro Material de una sección: varios,
    separados por coma en el GET (?material_consumo=07-00003138,14-00000023;
    el selector de zonas.js también permite elegir un grupo entero, p.ej.
    Calizas, y manda sus códigos). Lista vacía = todos. Los códigos que no
    están en las opciones (p.ej. de otro centro) se descartan. Pedido por el
    usuario: antes era un solo material."""
    validos = {o["codigo"] for o in opciones_material} - {"Todos"}
    crudo = request.GET.get(f"material_{prefijo}", "")
    return [c for c in dict.fromkeys(crudo.split(",")) if c in validos]


def _por_materiales(df, materiales):
    """`df` filtrado a los materiales elegidos (todos si la lista está vacía)."""
    return df if not materiales else df[df["material_codigo"].isin(materiales)]


# Tamaño de página del "scroll infinito" de Rotación > Detalle por material:
# esa tabla puede tener decenas de miles de filas (REPUESTOS ~28,000,
# SUMINISTROS INDUSTRIALES ~9,400) y mandarlas todas de una vez generaba una
# página de >13 MB, pesada para el navegador. En vez de eso se manda esta
# cantidad inicial y se van pidiendo más páginas a medida que el usuario baja
# el scroll de la tabla (ver rotacion_materiales_pagina), como una tabla de
# Power BI. Definido por el usuario.
_ROTACION_PAGINA_TAM = 200
_ROTACION_COLUMNAS_ORDENABLES = {"stock_cierre", "consumo_promedio", "indice_rotacion", "cobertura_meses"}


def _rotacion_materiales_filtrado(categoria, mes, familia=None, centro=None, buscar_codigo="",
                                   buscar_descripcion="", orden=None, direccion="desc"):
    """Detalle de rotación por material de una categoría, filtrado y ordenado
    — insumo común de categoria_detalle (primera página) y
    rotacion_materiales_pagina (páginas siguientes, vía scroll)."""
    rot = q.rotacion_inventario_por_material(mes)
    rot = rot[rot["categoria"] == categoria]
    if familia:
        rot = rot[rot["familia"] == familia]
    if centro:
        rot = rot[rot["centro"] == centro]
    if buscar_codigo:
        rot = rot[rot["material_codigo"].str.lower().str.contains(buscar_codigo.lower(), na=False, regex=False)]
    if buscar_descripcion:
        rot = rot[rot["texto_breve"].str.lower().str.contains(buscar_descripcion.lower(), na=False, regex=False)]
    columna = orden if orden in _ROTACION_COLUMNAS_ORDENABLES else "indice_rotacion"
    return rot.sort_values(columna, ascending=(direccion == "asc"), na_position="last")


# Gráficos de la pestaña Indicadores: cuántos materiales se muestran en los
# gráficos de barras (los de más peso), y los umbrales de color.
_PANEL_FILAS = 8


def _nombre_material(texto):
    return texto.strip().title()


def _signo(valor, decimales=1):
    """+1.5 / −3.4 (con el signo menos tipográfico)."""
    valor = 0.0 if abs(valor) < 0.5 * 10 ** -decimales else valor
    return f"{'+' if valor > 0 else '−' if valor < 0 else ''}{abs(valor):,.{decimales}f}"


def _centros_por_nombre(df):
    """Copia de `df` con la columna de centro (centro/Centro) por nombre (U001
    -> ATOCONGO...), para las descargas a Excel. Pedido por el usuario."""
    columnas = [c for c in ("centro", "Centro") if c in df.columns]
    if not columnas:
        return df
    return df.assign(**{c: df[c].map(lambda v: q.nombre_centro(v) if isinstance(v, str) else v) for c in columnas})


# Palabras que no distinguen a un proveedor de otro (razón social): se quitan
# de las etiquetas del gráfico, si no varios quedaban como "SOCIEDAD MINERA…".
_RAZON_SOCIAL = re.compile(
    r"^(SOCIEDAD MINERA( R\.?L\.?)?|INVERSIONES|EMPRESA MINERA|COMPA[NÑ]IA MINERA)\s+"
    r"|\s+(S\.?A\.?C\.?|S\.?A\.?A\.?|S\.?A\.?|E\.?I\.?R\.?L\.?|S\.?R\.?L\.?)$"
)


def _nombre_corto(texto, largo=22):
    """Nombre de proveedor sin la razón social, recortado en un límite de
    palabra, para las etiquetas del gráfico."""
    anterior = None
    while anterior != texto:
        anterior, texto = texto, _RAZON_SOCIAL.sub("", texto.strip())
    texto = texto.title()
    if len(texto) <= largo:
        return texto
    return texto[:largo].rsplit(" ", 1)[0] + "…"


def _fecha_corta(fecha):
    return f"{fecha.day:02d}-{_MESES_ABREV_GUION[fecha.month]}-{fecha.year % 100:02d}"


def _mes_corto(fecha):
    return f"{_MESES_ABREV_GUION[fecha.month]}-{fecha.year % 100:02d}"


# Gráficos de líneas de los paneles de Indicadores: la misma proporción que
# las columnas del IPU (480 x 200), así los paneles de una fila terminan a la
# misma altura y las etiquetas (texto de tamaño fijo) no se pisan con los ejes
# al dibujarse a media página (pulido de diseño, 2026-10-23).
_PANEL_LINEAS = {"ancho": 480, "alto": 200, "pad_izq": 46, "pad_der": 30, "pad_arr": 26, "pad_abj": 26}


def _soles_mm(valor):
    """Soles abreviados para las cifras de los paneles: S/ 12.3 MM, S/ 450 K."""
    if abs(valor) >= 1e6:
        return f"S/ {valor / 1e6:z,.1f} MM"
    if abs(valor) >= 1e3:
        return f"S/ {valor / 1e3:z,.0f} K"
    return f"S/ {valor:z,.0f}"


def _soles_eje(valor):
    """Soles abreviados para la grilla de un gráfico: 12.3M, 450K."""
    if abs(valor) >= 1e6:
        return f"{valor / 1e6:z,.1f}M"
    if abs(valor) >= 1e3:
        return f"{valor / 1e3:z,.0f}K"
    return f"{valor:z,.0f}"


def _soles_marca(valor):
    """Marcas del eje de montos en escala logarítmica: S/ 10K, S/ 1M."""
    if valor >= 1e6:
        return f"S/ {valor / 1e6:z,.0f}M"
    if valor >= 1e3:
        return f"S/ {valor / 1e3:z,.0f}K"
    return f"S/ {valor:z,.0f}"
