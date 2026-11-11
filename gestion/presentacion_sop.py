"""Presentación «Evolución de inventarios» del S&OP (PowerPoint), que se descarga desde Planificación > Inventario.

Pedido del usuario (2026-11-11): "un botón que genere las diapositivas que estamos revisando", con un selector para
elegir los materiales. Una lámina por material (stock real de los últimos meses cerrados y proyección del S&OP, frente
a su mínimo y máximo) y, al cerrar cada centro, la tabla de coberturas proyectadas. Mismos cálculos que la Evolución
de inventario y la matriz de cobertura del tablero (q.proyeccion_inventario_sop, plan_consumo._fila_cobertura). El
estilo replica las ediciones que el usuario hizo a mano en la presentación S&OP: títulos y cifra clave en rojo,
Verdana en títulos y Aptos Narrow en el resto, sin línea de fuente al pie (va a las notas).
"""
import copy
import datetime
import io
from pathlib import Path

from dashboard import queries as q
from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_MARKER_STYLE, XL_TICK_LABEL_POSITION, XL_TICK_MARK
from pptx.enum.dml import MSO_LINE_DASH_STYLE, MSO_PATTERN_TYPE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

from . import pptx_util as u

LOGO = Path(__file__).resolve().parent / "static" / "gestion" / "img" / "unacem-logo.png"
# Meses cerrados antes de la partida que muestra cada gráfico (jun–oct para el S&OP de noviembre).
MESES_REALES = 5
# Materiales que vienen marcados en el selector, con el nombre corto de sus láminas: los de la presentación que el
# usuario revisó con la jefatura (07-11-2026; Puzolana Ayacucho de Condorcocha agregada el 11-11). En este orden van
# las láminas; los demás materiales del S&OP, después y con el nombre del maestro.
MATERIALES_DEFECTO = [
    ("U001", "14-00000024", "Caliza Roca Extraída"), ("U001", "07-00003138", "Caliza Andina"),
    ("U001", "07-00003137", "Caliza Roca Fuerte"), ("U001", "07-00001181", "Yeso Terceros"),
    ("U001", "07-00000055", "Puzolana Terceros"),
    ("U002", "07-00000026", "Yeso Terceros"), ("U002", "07-00000002", "Arcilla Terceros"),
    ("U002", "07-00001677", "Arcilla Ferrosa"), ("U002", "07-00000028", "Óxido de Fierro"),
    ("U002", "07-00000027", "Puzolana Ayacucho"),
    ("U002", "06-00000005", "Carbón Terceros"), ("U002", "06-00000004", "Carbón Pampahuay"),
    ("U002", "06-00000003", "Carbón Importado"), ("U002", "06-00001601", "Carbón Trujillo"),
]
_ORDEN_DEFECTO = {(c, m): i for i, (c, m, _) in enumerate(MATERIALES_DEFECTO)}
_NOMBRE_DEFECTO = {(c, m): n for c, m, n in MATERIALES_DEFECTO}
# Filas por lámina de coberturas: con más materiales la tabla se parte en varias láminas.
MAX_FILAS_COBERTURA = 14

# Paleta de la presentación S&OP: títulos y cifra clave en rojo FF0000, líneas en negro.
TINTA, GRIS, LINEA, FONDO3 = "1B1E21", "5D646C", "E0E3E7", "E8EBEE"
NEGRO, ROJO, ROJO_TEXTO, VERDE, AMBAR = "000000", "FF0000", "D00303", "2F7A4E", "B7791F"
# Barras del tablero (pedido del usuario, 2026-11-04): ingresos en blanco, consumo en gris; lo real liso y lo
# proyectado rayado (en PowerPoint, trama diagonal: el degradé de las líneas no existe en sus gráficos).
BLANCO, GRIS_BARRA = "FFFFFF", "9A9A9A"
ESTADO = {  # fondo, texto, negrita, nombre
    "quiebre": ("FFADAD", ROJO_TEXTO, True, "Quiebre"),
    "bajo_minimo": ("FFDBDB", ROJO_TEXTO, False, "Bajo mínimo"),
    "en_rango": ("E1F1E7", TINTA, False, "En rango"),
    "sobre_maximo": ("F7ECD4", TINTA, False, "Sobre máximo"),
    "sin_parametro": (None, GRIS, False, "Sin mín/máx"),
}
MESES = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
MESES_LARGO = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "setiembre", "octubre",
               "noviembre", "diciembre"]
ANCHO, ALTO, MARGEN = 13.333, 7.5, 0.5
TAM = 11
# Área del gráfico dentro de su marco (fracciones): fija para ubicar la regla real/proyección sobre los meses.
PLOT_X, PLOT_Y, PLOT_W, PLOT_H = 0.062, 0.05, 0.928, 0.84
_CONECTORES = {"de", "del", "la", "las", "los", "y", "en"}


class SinDatos(Exception):
    """El S&OP elegido no tiene con qué armar la presentación (se muestra tal cual al usuario)."""


def _mes(v):
    return v.date() if isinstance(v, datetime.datetime) else v


def corto(f):
    return f"{MESES[f.month]}-{f.year % 100:02d}"


def miles(v, decimales=0):
    texto = f"{v:,.{decimales}f}"
    return "−" + texto[1:] if texto.startswith("-") else texto


def k(v):
    """Toneladas en miles, como las etiquetas del tablero (12K, 8.0K, −3K)."""
    a = abs(v)
    texto = f"{a / 1000:,.0f}K" if a >= 10000 else f"{a / 1000:,.1f}K" if a >= 1000 else f"{a:,.0f}"
    return ("−" if v < -0.5 else "") + texto


def meses_o_raya(v):
    return "—" if v is None else f"{v:.1f}"


def centro_texto(centro):
    return q.nombre_centro(centro).title()


def _titulo(texto):
    """Descripción del maestro en tipo título ("OXIDO DE FIERRO" -> "Oxido de Fierro")."""
    palabras = (texto or "").lower().split()
    return " ".join(p if i and p in _CONECTORES else p[:1].upper() + p[1:] for i, p in enumerate(palabras))


# --- Datos ------------------------------------------------------------------------------------------------
def opciones(sop):
    """Materiales del S&OP `sop` para el selector: {valor "material|centro", material, centro, nombre, defecto},
    en el orden de las láminas. Sin las filas sin código SAP (no tienen stock real)."""
    proy = q.proyeccion_inventario_sop(sop)
    if proy.empty:
        return []
    pares = proy[~proy["material_codigo"].astype(str).str.startswith(q.PREFIJO_SIN_CODIGO_SOP)]
    pares = pares.drop_duplicates(["material_codigo", "centro"])
    filas = [{"valor": f"{r.material_codigo}|{r.centro}", "material": r.material_codigo, "centro": r.centro,
              "nombre": _NOMBRE_DEFECTO.get((r.centro, r.material_codigo)) or _titulo(r.texto_breve),
              "defecto": (r.centro, r.material_codigo) in _ORDEN_DEFECTO}
             for r in pares.itertuples()]
    return sorted(filas, key=lambda f: (f["centro"], _ORDEN_DEFECTO.get((f["centro"], f["material"]), len(_ORDEN_DEFECTO)),
                                        f["nombre"], f["material"]))


def datos(sop, elegidos):
    """Cifras de la presentación para los materiales `elegidos` (filas de opciones()): por material, el stock real
    al cierre de los MESES_REALES meses antes del S&OP (MB5B) con sus ingresos y consumos (MB51), y la proyección
    del S&OP con mínimo/máximo y cobertura en meses."""
    from .vistas import plan_consumo as pc  # la vista importa este módulo

    proy = q.proyeccion_inventario_sop(sop)
    if proy.empty:
        raise SinDatos("Ese S&OP no tiene proyección de inventario.")
    fecha_inicial = q.fecha_inventario_inicial_sop(sop)
    if not fecha_inicial:
        raise SinDatos("Todavía no está cargado el inventario de cierre del mes anterior a este S&OP (MB5B): "
                       "la proyección no tiene stock de partida.")
    proy = proy.assign(mes=proy["mes"].map(_mes))
    cierres = q.stock_real_cierres_sop()
    cierres = cierres.assign(mes=cierres["mes"].map(_mes))
    movs = q.movimientos_reales_sop()
    movs = movs.assign(mes=movs["mes"].map(_mes))
    desde = q._sumar_meses(sop, -MESES_REALES)
    meses_reales = sorted(m for m in set(cierres["mes"]) if desde <= m < sop)
    if not meses_reales:
        raise SinDatos("No hay cierres de inventario (MB5B) de los meses anteriores a este S&OP.")
    fin = max(proy["mes"])
    # Último mes con consumo planificado de cada centro: más allá, sin consumo, la cobertura saldría inflada.
    fin_consumo = {}
    for centro, g in proy.groupby("centro"):
        con_consumo = g.loc[g["consumo"] > 0.5, "mes"]
        fin_consumo[centro] = max(con_consumo) if len(con_consumo) else fin
    salida = {"sop": sop, "fecha_inicial": fecha_inicial, "fin": fin, "meses_reales": meses_reales,
              "cobertura_meses_siguientes": pc._COBERTURA_MESES_SIGUIENTES, "fin_consumo": fin_consumo,
              "materiales": []}
    for o in elegidos:
        material, centro = o["material"], o["centro"]
        g = proy[(proy["material_codigo"] == material) & (proy["centro"] == centro)].sort_values("mes")
        if g.empty:
            continue
        c = cierres[(cierres["material_codigo"] == material) & (cierres["centro"] == centro)]
        stock_real = {r.mes: float(r.stock) for r in c.itertuples()}
        m = movs[(movs["material_codigo"] == material) & (movs["centro"] == centro)]
        flujo = {(r.mes, r.tipo): float(r.cantidad)
                 for r in m.groupby(["mes", "tipo"], as_index=False)["cantidad"].sum().itertuples()}
        real = []
        for mes in meses_reales:
            consumo = flujo.get((mes, "consumo"), 0.0)
            envios = flujo.get((mes, "envio"), 0.0)
            real.append({"mes": mes, "stock": stock_real.get(mes), "ingresos": flujo.get((mes, "ingreso"), 0.0),
                         "salidas": consumo + envios})
        horizonte = g[g["mes"] <= fin_consumo[centro]]
        salidas = [float(a + b) for a, b in zip(horizonte["consumo"], horizonte["traslado"])]
        cob = pc._fila_cobertura(material, o["nombre"], centro, float(g["stock_inicial"].iloc[0]),
                                 [float(v) for v in horizonte["stock_final"]], salidas,
                                 list(horizonte["stock_minimo"]), list(horizonte["stock_maximo"]))
        proyeccion = []
        for i, r in enumerate(g.itertuples()):
            celda = cob["celdas"][i] if i < len(cob["celdas"]) else {"cobertura": None, "estado": "sin_parametro"}
            proyeccion.append({
                "mes": r.mes, "abastecimiento": float(r.abastecimiento), "traslado": float(r.traslado),
                "salidas": float(r.consumo + r.traslado), "stock_final": float(r.stock_final),
                "minimo": _numero(r.stock_minimo), "maximo": _numero(r.stock_maximo), "estado": r.estado,
                "cobertura": celda["cobertura"], "estado_cobertura": celda["estado"],
            })
        salida["materiales"].append({
            "centro": centro, "material": material, "nombre": o["nombre"], "texto_breve": g["texto_breve"].iloc[0],
            "stock_partida": float(g["stock_inicial"].iloc[0]), "real": real, "proyeccion": proyeccion,
            "minimo_meses": cob["minimo_meses"], "maximo_meses": cob["maximo_meses"],
            "cobertura_inicial": cob["cobertura_inicial"], "consumo_minimo": cob["consumo_minimo"],
        })
    if not salida["materiales"]:
        raise SinDatos("Ninguno de los materiales elegidos está en ese S&OP.")
    return salida


def _numero(v):
    if v is None:
        return None
    v = float(v)
    return None if v != v else v


def armar(sop, elegidos):
    """(contenido del .pptx, nombre del archivo) para los materiales `elegidos` (filas de opciones())."""
    d = datos(sop, elegidos)
    p = _Presentacion(d)
    centros = sorted({m["centro"] for m in d["materiales"]})
    for centro in centros:
        del_centro = [m for m in d["materiales"] if m["centro"] == centro]
        for m in del_centro:
            p.lamina_material(m)
        partes = [del_centro[i:i + MAX_FILAS_COBERTURA] for i in range(0, len(del_centro), MAX_FILAS_COBERTURA)]
        for i, parte in enumerate(partes, start=1):
            p.lamina_coberturas(centro, parte, f" ({i} de {len(partes)})" if len(partes) > 1 else "")
    salida = io.BytesIO()
    p.prs.save(salida)
    return salida.getvalue(), f"Evolución de inventarios {p.texto_sop}.pptx"


# --- Láminas ----------------------------------------------------------------------------------------------
def _rellenar(fill, real, ingreso):
    if real:
        fill.solid()
        fill.fore_color.rgb = u.rgb(BLANCO if ingreso else GRIS_BARRA)
    else:
        fill.patterned()
        fill.pattern = MSO_PATTERN_TYPE.WIDE_UPWARD_DIAGONAL
        fill.fore_color.rgb = u.rgb(NEGRO if ingreso else BLANCO)
        fill.back_color.rgb = u.rgb(BLANCO if ingreso else GRIS_BARRA)


def _combinar_lineas(grafico, indices):
    """Pasa las series `indices` (de un gráfico de columnas) a UN grupo de líneas sobre los mismos ejes."""
    plot_area = grafico._chartSpace.chart.plotArea
    bar = plot_area.find(qn("c:barChart"))
    series = bar.findall(qn("c:ser"))
    linea = etree.Element(qn("c:lineChart"))
    etree.SubElement(linea, qn("c:grouping"), val="standard")
    etree.SubElement(linea, qn("c:varyColors"), val="0")
    for i in indices:
        ser = series[i]
        bar.remove(ser)
        inv = ser.find(qn("c:invertIfNegative"))
        if inv is not None:
            ser.remove(inv)
        etree.SubElement(ser, qn("c:smooth"), val="0")
        linea.append(ser)
    etree.SubElement(linea, qn("c:marker"), val="1")
    for ax in bar.findall(qn("c:axId")):
        linea.append(copy.deepcopy(ax))
    bar.addnext(linea)


def _layout_manual(grafico):
    plot_area = grafico._chartSpace.chart.plotArea
    viejo = plot_area.find(qn("c:layout"))
    if viejo is not None:
        plot_area.remove(viejo)
    lay = etree.Element(qn("c:layout"))
    ml = etree.SubElement(lay, qn("c:manualLayout"))
    etree.SubElement(ml, qn("c:layoutTarget"), val="inner")
    etree.SubElement(ml, qn("c:xMode"), val="edge")
    etree.SubElement(ml, qn("c:yMode"), val="edge")
    for etq, v in (("c:x", PLOT_X), ("c:y", PLOT_Y), ("c:w", PLOT_W), ("c:h", PLOT_H)):
        etree.SubElement(ml, qn(etq), val=f"{v:.4f}")
    plot_area.insert(0, lay)


def _ejes(chart):
    chart.font.size = Pt(10)
    chart.font.name = u.CUERPO
    chart.font.color.rgb = u.rgb(GRIS)
    va = chart.value_axis
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = u.rgb(LINEA)
    va.major_gridlines.format.line.width = Pt(0.5)
    va.format.line.fill.background()
    va.major_tick_mark = XL_TICK_MARK.NONE
    va.tick_labels.number_format = '#,##0,"K";−#,##0,"K";0'
    va.tick_labels.number_format_is_linked = False
    va.tick_labels.font.size = Pt(10)
    va.tick_labels.font.color.rgb = u.rgb(GRIS)
    ca = chart.category_axis
    ca.format.line.color.rgb = u.rgb("8C96A1")
    ca.major_tick_mark = XL_TICK_MARK.NONE
    ca.tick_label_position = XL_TICK_LABEL_POSITION.LOW
    ca.tick_labels.font.size = Pt(10)
    ca.tick_labels.font.color.rgb = u.rgb(GRIS)
    chart.has_legend = False


def _formato_linea(fmt, color, ancho, punteada):
    fmt.line.color.rgb = u.rgb(color)
    fmt.line.width = Pt(ancho)
    fmt.line.dash_style = MSO_LINE_DASH_STYLE.DASH if punteada else MSO_LINE_DASH_STYLE.SOLID


def _etiqueta(serie, indice, texto_e, color=TINTA, posicion=XL_LABEL_POSITION.ABOVE, negrita=False, tam=9):
    dl = serie.points[indice].data_label
    dl.has_text_frame = True
    dl.text_frame.text = texto_e
    dl.position = posicion
    run = dl.text_frame.paragraphs[0].runs[0]
    run.font.size = Pt(tam)
    run.font.bold = negrita
    run.font.name = u.CUERPO
    run.font.color.rgb = u.rgb(color)
    # Fondo blanco translúcido: la cifra se lee aunque caiga sobre una barra rayada o la línea del mínimo.
    sp = dl._get_or_add_dLbl().get_or_add_spPr()
    relleno = etree.SubElement(sp, qn("a:solidFill"))
    etree.SubElement(etree.SubElement(relleno, qn("a:srgbClr"), val="FFFFFF"), qn("a:alpha"), val="80000")


def _resumen(m):
    """Mensaje de la lámina, estado de la etiqueta y mes del menor stock."""
    proy = m["proyeccion"]
    n = len(proy)
    finales = [p["stock_final"] for p in proy]
    i_min = min(range(n), key=lambda i: finales[i])
    quiebre = [p for p in proy if p["estado"] == "quiebre"]
    bajo = [p for p in proy if p["estado"] == "bajo_minimo"]
    sobre = [p for p in proy if p["estado"] == "sobre_maximo"]
    abast = sum(p["abastecimiento"] for p in proy) / n
    sal = sum(p["salidas"] for p in proy) / n
    ritmo = f"abastecimiento de {miles(abast)} T/mes contra salidas de {miles(sal)} T/mes"

    def meses_texto(cuantos):
        return f"{cuantos} {'mes' if cuantos == 1 else 'meses'}"
    if quiebre:
        mensaje = (f"Rotura desde {corto(quiebre[0]['mes'])}: el stock proyectado llega a {miles(finales[i_min])} T en "
                   f"{corto(proy[i_min]['mes'])}; {ritmo}")
        estado = ("malo", f"Rotura desde {corto(quiebre[0]['mes'])}")
    elif bajo:
        mensaje = (f"Bajo el mínimo en {len(bajo)} de {n} meses, desde {corto(bajo[0]['mes'])}; el menor stock es "
                   f"{miles(finales[i_min])} T en {corto(proy[i_min]['mes'])}")
        estado = ("malo", f"Bajo mínimo en {meses_texto(len(bajo))}")
    elif sal > 0 and finales[i_min] / sal < 0.5:
        # Sin rotura ni mínimo que lo marque (p. ej. meses sin mín/máx), pero con menos de medio mes de salidas.
        mensaje = (f"Baja a {miles(finales[i_min])} T en {corto(proy[i_min]['mes'])}, menos de medio mes de salidas "
                   f"({finales[i_min] / sal:.1f} meses)" + (f"; antes, sobre el máximo en {len(sobre)} de {n} meses"
                                                             if sobre else f"; {ritmo}"))
        estado = ("malo", "En vigilancia")
    elif len(sobre) >= n / 2:
        mensaje = f"Sobre el máximo en {len(sobre)} de {n} meses: {ritmo}"
        estado = ("alerta", f"Sobre máximo en {meses_texto(len(sobre))}")
    elif sobre:
        mensaje = f"En rango salvo {meses_texto(len(sobre))} sobre el máximo; {ritmo}"
        estado = ("", "En rango")
    elif any(p["minimo"] is not None for p in proy):
        mensaje = f"Dentro del rango mínimo–máximo todo el horizonte; {ritmo}"
        estado = ("bueno", "En rango")
    else:
        mensaje = f"Sin mínimo/máximo en el S&OP; {ritmo}"
        estado = ("", "Sin mín/máx")
    return mensaje[0].upper() + mensaje[1:], estado, i_min


def _ultimo_con_minmax(m):
    """Último mes proyectado con mínimo/máximo: el archivo MinMax S&OP puede terminar antes que el horizonte."""
    return next((p for p in reversed(m["proyeccion"]) if p["minimo"] is not None), None)


def _leyenda_minmax(m):
    ref = _ultimo_con_minmax(m)
    if ref is None:
        return []
    return [(f"Mínimo {k(ref['minimo'])}", "minimo")] + (
        [] if ref["maximo"] is None else [(f"Máximo {k(ref['maximo'])}", "maximo")])


def _nota_minmax(materiales):
    """Nota cuando el mínimo/máximo termina antes que la proyección (los meses siguientes van sin líneas ni color,
    igual que en el tablero)."""
    refs = [r for r in (_ultimo_con_minmax(m) for m in materiales) if r is not None]
    fin = materiales[0]["proyeccion"][-1]["mes"]
    if not refs or all(r["mes"] == fin for r in refs):
        return ""
    hasta = max(r["mes"] for r in refs)
    return (f"\n• El archivo MinMax S&OP llega a {corto(hasta).lower()}: de ahí a {corto(fin).lower()} no hay mínimo ni "
            "máximo (sin líneas en el gráfico y sin color en la cobertura, igual que en el tablero).")


def _nota_consumo_via_otro(materiales):
    """Materiales que se consumen por otro (Carbón Terceros por su Chancado, q._MATERIALES_CONSUMO_VIA_OTRO)."""
    return "".join(
        f"\n• {m['nombre']}: stock junto con el {q._MATERIALES_CONSUMO_VIA_OTRO[(m['material'], m['centro'])][0]}, "
        "el material por el que se consume; su consumo es el de ese material, el que va al proceso."
        for m in materiales if (m["material"], m["centro"]) in q._MATERIALES_CONSUMO_VIA_OTRO)


def _nota_consumo_minimo(materiales):
    return "".join(
        f"\n• {m['nombre']}: en los promedios de la cobertura solo entran los meses con salidas mayores a "
        f"{miles(m['consumo_minimo'])} T (meses de producción); si en los meses siguientes no hay ninguno, el promedio "
        "de esos meses en todo el horizonte."
        for m in materiales if m["consumo_minimo"])


class _Presentacion:
    """Presentación en curso: una por descarga (el servidor atiende varias a la vez)."""

    def __init__(self, d):
        self.d = d
        self.sop, self.partida = d["sop"], d["fecha_inicial"]
        self.texto_sop = f"S&OP {MESES_LARGO[self.sop.month]} {self.sop.year}"
        self.texto_partida = f"{self.partida:%d/%m/%Y}"
        reales = d["meses_reales"]
        self.prs = Presentation()
        self.prs.slide_width, self.prs.slide_height = Inches(ANCHO), Inches(ALTO)
        u.fuentes_tema(self.prs)
        self.prs.core_properties.title = f"Evolución de inventarios · {self.texto_sop}"
        self.prs.core_properties.author = "Planificación UPERU"
        self.titulo_solo = self.prs.slide_layouts[5]
        self.notas_metodo = (
            "Cómo se calcula (mismo cálculo que la Evolución de inventario del tablero, Planificación > Inventario)\n"
            f"• Real, {corto(reales[0]).lower()} a {corto(reales[-1]).lower()}: stock al cierre de cada mes (MB5B). "
            "Barras reales (MB51): ingresos = 101/102 (compra, producción o recepción de traslado); consumo = clases "
            "2xx, Z01, Z04 y 353, más las salidas por traslado a otro centro (641/642).\n"
            f"• Proyección {self.texto_sop}: stock inicial = MB5B al {self.texto_partida} (cierre de "
            f"{MESES_LARGO[self.partida.month]}); cada mes, stock final = inicial + abastecimiento − consumo − "
            f"traslados a otro centro, de los archivos de abastecimiento y consumos del {self.texto_sop}.\n"
            "• Barras: ingresos en blanco y consumo en gris; lisas = real, rayadas = proyectado. Línea continua = stock "
            "real; punteada = stock proyectado. Mínimo (rojo) y máximo (ámbar) del archivo MinMax S&OP; los meses "
            "después de su última columna repiten el último valor (como el tablero).\n"
            f"• Cobertura (meses) = stock final ÷ salidas planificadas (consumo + traslados) promedio de los "
            f"{d['cobertura_meses_siguientes']} meses siguientes.")

    def lamina(self, titulo, bajada, notas):
        s = self.prs.slides.add_slide(self.titulo_solo)
        t = s.shapes.title
        t.left, t.top, t.width, t.height = Inches(MARGEN), Inches(0.36), Inches(10.6), Inches(0.5)
        tf = t.text_frame
        tf.auto_size = MSO_AUTO_SIZE.NONE
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = MSO_ANCHOR.TOP
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.alignment = PP_ALIGN.LEFT
        r = p.add_run()
        r.text = titulo.upper()
        r.font.name, r.font.size, r.font.bold = u.TITULO, Pt(24), True
        r.font.color.rgb = u.rgb(ROJO)
        u.texto(s, MARGEN, 0.86, 10.6, 0.3, bajada, tam=14, color=TINTA, nombre="Bajada")
        if LOGO.exists():
            s.shapes.add_picture(str(LOGO), Inches(ANCHO - MARGEN - 1.45), Inches(0.4), Inches(1.45)).name = "Logo"
        u.texto(s, ANCHO - MARGEN - 0.6, ALTO - 0.4, 0.6, 0.22, str(len(self.prs.slides)), tam=10, color=GRIS,
                alinear=PP_ALIGN.RIGHT, nombre="Número")
        s.notes_slide.notes_text_frame.text = notas
        return s

    @staticmethod
    def raya(s, x1, y1, x2, y2, color=LINEA, ancho=0.75, estilo=None, nombre="Línea"):
        ln = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
        ln.line.color.rgb = u.rgb(color)
        ln.line.width = Pt(ancho)
        if estilo:
            ln.line.dash_style = estilo
        ln.name = nombre
        return ln

    @staticmethod
    def muestra_barra(s, x, y, real, ingreso, nombre):
        """Cuadrito de leyenda con el mismo relleno que la barra del gráfico."""
        forma = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(0.13), Inches(0.17))
        forma.name = nombre
        _rellenar(forma.fill, real, ingreso)
        if ingreso:
            forma.line.color.rgb = u.rgb(NEGRO)
            forma.line.width = Pt(0.75)
        else:
            forma.line.fill.background()
        forma.shadow.inherit = False
        return forma

    def leyenda(self, s, x, y, items):
        """items: (texto, tipo) con tipo en barra-ingreso(-real), barra-consumo(-real), linea, punteada, minimo,
        maximo; de izquierda a derecha."""
        for texto_i, tipo in items:
            if tipo.startswith("barra"):
                self.muestra_barra(s, x, y + 0.03, tipo.endswith("-real"), "ingreso" in tipo, f"Muestra {texto_i}")
                x += 0.2
            else:
                color = {"minimo": ROJO, "maximo": AMBAR}.get(tipo, NEGRO)
                estilo = None if tipo == "linea" else MSO_LINE_DASH_STYLE.DASH
                self.raya(s, x, y + 0.115, x + 0.32, y + 0.115, color=color,
                          ancho=2.0 if tipo in ("linea", "punteada") else 1.25, estilo=estilo, nombre=f"Muestra {texto_i}")
                x += 0.4
            ancho = 0.1 + len(texto_i) * 0.062
            u.texto(s, x, y, ancho, 0.23, texto_i, tam=10, color=GRIS, ancla=MSO_ANCHOR.MIDDLE,
                    nombre=f"Leyenda {texto_i}")
            x += ancho + 0.16

    def grafico_material(self, s, m, x, y, w, h):
        real, proy = m["real"], m["proyeccion"]
        n_real = len(real)
        meses = [r["mes"] for r in real] + [p["mes"] for p in proy]
        stock = [r["stock"] for r in real] + [p["stock_final"] for p in proy]
        ingresos = [r["ingresos"] for r in real] + [p["abastecimiento"] for p in proy]
        salidas = [r["salidas"] for r in real] + [p["salidas"] for p in proy]
        minimos = [None] * n_real + [p["minimo"] for p in proy]
        maximos = [None] * n_real + [p["maximo"] for p in proy]
        hay_minmax = any(v is not None for v in minimos)
        cd = CategoryChartData(number_format="#,##0")
        cd.categories = [corto(mes) for mes in meses]
        cd.add_series("Ingresos (T)", ingresos)
        cd.add_series("Consumo + traslados (T)", salidas)
        if hay_minmax:
            cd.add_series("Stock mínimo (T)", minimos)
            cd.add_series("Stock máximo (T)", maximos)
        cd.add_series("Stock al cierre (T)", stock)
        g = s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(x), Inches(y), Inches(w), Inches(h), cd)
        g.name = f"Gráfico {m['nombre']}"
        ch = g.chart
        _combinar_lineas(ch, [2, 3, 4] if hay_minmax else [2])
        _layout_manual(ch)
        u.quitar_lineas_ocultas(ch)
        _ejes(ch)
        barras = ch.plots[0]
        barras.gap_width = 70
        barras.overlap = -8
        for j, ingreso in ((0, True), (1, False)):
            serie = barras.series[j]
            serie.invert_if_negative = False
            for i in range(len(meses)):
                fmt = serie.points[i].format
                _rellenar(fmt.fill, i < n_real, ingreso)
                if ingreso:
                    fmt.line.color.rgb = u.rgb(NEGRO)
                    fmt.line.width = Pt(0.75)
                else:
                    fmt.line.fill.background()
            u.sin_invertir(serie)
        lineas = ch.plots[1].series
        if hay_minmax:
            for serie, color in ((lineas[0], ROJO), (lineas[1], AMBAR)):
                _formato_linea(serie.format, color, 1.25, True)
                serie.marker.style = XL_MARKER_STYLE.NONE
        sl = lineas[-1]
        _formato_linea(sl.format, NEGRO, 2.25, False)
        sl.marker.style = XL_MARKER_STYLE.CIRCLE
        sl.marker.size = 6
        partida = n_real - 1
        for i, v in enumerate(stock):
            pt = sl.points[i]
            proyectado = i > partida
            # El tramo que llega a un mes proyectado va punteado (el de la partida al primer mes, también).
            _formato_linea(pt.format, NEGRO, 2.25 if not proyectado else 2.0, proyectado)
            negativo = v is not None and v < 0
            color = ROJO if negativo else NEGRO
            pt.marker.style = XL_MARKER_STYLE.CIRCLE
            pt.marker.size = 8 if i == partida else 6
            pt.marker.format.fill.solid()
            pt.marker.format.fill.fore_color.rgb = u.rgb(BLANCO if proyectado and not negativo else color)
            pt.marker.format.line.color.rgb = u.rgb(color)
            pt.marker.format.line.width = Pt(1.25)
            if v is not None:
                _etiqueta(sl, i, k(v), color=ROJO_TEXTO if negativo else TINTA, negrita=i in (partida, len(stock) - 1),
                          posicion=XL_LABEL_POSITION.BELOW if negativo else XL_LABEL_POSITION.ABOVE)
        # Regla real / proyección sobre el gráfico y corte en la partida (como el tablero).
        n = len(meses)

        def x_mes(i):
            return x + w * (PLOT_X + PLOT_W * (i + 0.5) / n)
        y_regla = y - 0.12
        self.raya(s, x_mes(0), y_regla, x_mes(partida), y_regla, color=TINTA, ancho=1.5, nombre="Regla real")
        self.raya(s, x_mes(partida), y_regla, x_mes(n - 1), y_regla, color=TINTA, ancho=1.5,
                  estilo=MSO_LINE_DASH_STYLE.DASH, nombre="Regla proyección")
        u.texto(s, x_mes(0), y_regla - 0.27, x_mes(partida) - x_mes(0), 0.22, "Real: cierres de mes (MB5B)", tam=10,
                color=GRIS, nombre="Regla real texto")
        u.texto(s, x_mes(partida) + 0.06, y_regla - 0.27, 4.0, 0.22,
                f"Proyección: {self.texto_sop} (partida {self.texto_partida})", tam=10, color=GRIS,
                nombre="Regla proyección texto")
        self.raya(s, x_mes(partida), y + h * PLOT_Y, x_mes(partida), y + h * (PLOT_Y + PLOT_H), color="8C96A1",
                  ancho=0.75, estilo=MSO_LINE_DASH_STYLE.ROUND_DOT, nombre="Corte partida")
        return ch

    def franja(self, s, m, y, estado, i_min):
        proy = m["proyeccion"]
        fin = proy[-1]
        self.raya(s, MARGEN, y, ANCHO - MARGEN, y, nombre="Separador")
        x = MARGEN
        u.texto(s, x, y + 0.1, 2.6, 0.22, f"Stock proyectado a {corto(fin['mes'])}", tam=10, color=GRIS)
        u.texto(s, x, y + 0.3, 2.6, 0.45, f"{miles(fin['stock_final'])} T", tam=24, negrita=True, color=ROJO,
                nombre="Cifra")
        tono, texto_estado = estado
        fondo, color = {"bueno": ("DCEFE3", VERDE), "malo": ("FFDBDB", ROJO_TEXTO), "alerta": ("F7ECD4", AMBAR)}.get(
            tono, (FONDO3, GRIS))
        u.pastilla(s, x, y + 0.78, texto_estado, fondo, color, tam=10, alto=0.22)
        abast = sum(p["abastecimiento"] for p in proy)
        sal = sum(p["salidas"] for p in proy)
        tramo = f"{corto(proy[0]['mes']).lower()} a {corto(fin['mes']).lower()}"
        con_traslados = any(p["traslado"] > 0.5 for p in proy)
        detalle = [
            (f"Stock al {self.texto_partida}", f"{miles(m['stock_partida'])} T"),
            (f"Menor stock · {corto(proy[i_min]['mes'])}", f"{miles(proy[i_min]['stock_final'])} T"),
            (f"Ingresos {tramo}", f"{miles(abast)} T"),
            (f"{'Salidas' if con_traslados else 'Consumo'} {tramo}", f"{miles(sal)} T"),
            (f"Cobertura a {corto(fin['mes'])}", "—" if fin["cobertura"] is None else f"{fin['cobertura']:.1f} meses"),
        ]
        x0, ancho = MARGEN + 2.9, (ANCHO - 2 * MARGEN - 2.9) / len(detalle)
        for i, (nombre, valor) in enumerate(detalle):
            xi = x0 + i * ancho
            color_v = ROJO_TEXTO if i == 1 and proy[i_min]["stock_final"] < 0 else TINTA
            u.texto(s, xi, y + 0.12, ancho - 0.1, 0.22, nombre, tam=10, color=GRIS)
            u.texto(s, xi, y + 0.34, ancho - 0.1, 0.3, valor, tam=16, negrita=True, color=color_v)

    def lamina_material(self, m):
        mensaje, estado, i_min = _resumen(m)
        centro = centro_texto(m["centro"])
        notas = f"{m['material']} {m['texto_breve']} · {centro}.\n" + self.notas_metodo
        notas += _nota_consumo_via_otro([m]) + _nota_consumo_minimo([m]) + _nota_minmax([m])
        s = self.lamina(f"{m['nombre']} · {centro}", mensaje, notas)
        y_chart, h_chart = 1.86, 3.72
        salida_txt = "Consumo + traslados" if any(p["traslado"] > 0.5 for p in m["proyeccion"]) else "Consumo"
        self.leyenda(s, MARGEN, 1.24, [("Stock real", "linea"), ("Stock proyectado", "punteada")] + _leyenda_minmax(m) + [
            ("Ingresos reales", "barra-ingreso-real"), ("Ingresos proyectados", "barra-ingreso"),
            (f"{salida_txt} real", "barra-consumo-real"), (f"{salida_txt} proyectado", "barra-consumo")])
        u.texto(s, MARGEN - 0.05, y_chart - 0.02, 1.2, 0.2, "Toneladas", tam=9, color=GRIS, nombre="Unidad")
        self.grafico_material(s, m, MARGEN - 0.15, y_chart + 0.12, ANCHO - 2 * MARGEN + 0.2, h_chart)
        self.franja(s, m, 5.85, estado, i_min)
        return s

    def lamina_coberturas(self, centro, materiales, parte=""):
        meses = [p["mes"] for p in materiales[0]["proyeccion"]]
        n = len(meses)
        nc = centro_texto(centro)
        siguientes = self.d["cobertura_meses_siguientes"]
        fijos = [2.05, 0.6, 0.6, 1.2, 0.85]
        anchos = fijos + [(ANCHO - 2 * MARGEN - sum(fijos)) / n] * n
        notas = (f"Cobertura proyectada de {nc}, {self.texto_sop}.\n"
                 f"• Stock inicial = MB5B al {self.texto_partida} (cierre de {MESES_LARGO[self.partida.month]} "
                 f"{self.partida.year}); cada mes, stock final = inicial + abastecimiento − consumo − traslados "
                 f"({self.texto_sop}).\n"
                 f"• Cobertura (meses) = stock final del mes ÷ salidas planificadas (consumo + traslados) promedio de "
                 f"los {siguientes} meses siguientes; quiebre = stock negativo (0 meses). Cob. {self.texto_partida[:5]} = "
                 f"stock inicial con las salidas de {corto(meses[0]).lower()} a "
                 f"{corto(meses[min(siguientes, n) - 1]).lower()}.\n"
                 "• Color = cobertura frente al mínimo y máximo del S&OP pasados a meses (mínimo y máximo en T ÷ "
                 "salidas planificadas promedio del horizonte). Material en rojo: cae bajo el mínimo o en quiebre en "
                 "algún mes.\n"
                 "• Mismo cálculo que la matriz de cobertura del tablero (Planificación > Inventario).")
        fin_consumo, fin = self.d["fin_consumo"].get(centro, self.d["fin"]), self.d["fin"]
        if fin_consumo < fin:
            notas += (f"\n• {nc}: el archivo de consumos trae hasta {corto(fin_consumo).lower()}, así que la cobertura "
                      f"de los últimos meses usa las salidas que quedan hasta {corto(fin_consumo).lower()}.")
        else:
            notas += (f"\n• Últimos meses: la cobertura usa las salidas que quedan hasta {corto(fin).lower()} (en "
                      f"{corto(fin).lower()}, las del mismo mes).")
        notas += _nota_consumo_via_otro(materiales) + _nota_consumo_minimo(materiales) + _nota_minmax(materiales)
        alertas = [m for m in materiales
                   if any(p["estado_cobertura"] in ("bajo_minimo", "quiebre") for p in m["proyeccion"])]
        quiebres = [m["nombre"] for m in materiales if any(p["estado"] == "quiebre" for p in m["proyeccion"])]
        if quiebres:
            bajada = (f"{self.texto_sop} · stock inicial al {self.texto_partida} · rotura en " + ", ".join(quiebres)
                      + f"; {len(alertas)} de {len(materiales)} materiales caen bajo el mínimo")
        else:
            bajada = (f"{self.texto_sop} · stock inicial al {self.texto_partida} · {len(alertas)} de {len(materiales)} "
                      "materiales caen bajo el mínimo en algún mes")
        s = self.lamina(f"Coberturas proyectadas · {nc}{parte}", bajada, notas)
        y_t = 1.45
        fila = min(0.48, (ALTO - 1.45 - y_t) / (len(materiales) + 1))
        t = u.tabla(s, MARGEN, y_t, sum(anchos), anchos, fila, len(materiales) + 1, nombre=f"Coberturas {nc}")
        # Con muchos meses la columna no alcanza para "Nov-26": mes y año en dos líneas, igual en todas.
        partido = anchos[-1] < 0.45
        encabezado = ["Material", "Mín", "Máx", f"Stock {self.texto_partida[:5]} (T)", f"Cob. {self.texto_partida[:5]}"] + [
            corto(mes).split("-")[0] if partido else corto(mes) for mes in meses]
        for j, h in enumerate(encabezado):
            c = u.celda(t, 0, j, h, tam=10, color=TINTA if j == 5 else GRIS, negrita=True, borde_abajo=TINTA,
                        grosor=0.75, alinear=PP_ALIGN.LEFT if j == 0 else PP_ALIGN.RIGHT if j < 5 else PP_ALIGN.CENTER)
            if j >= 5:
                c.margin_left = c.margin_right = Inches(0.01)
                if partido:
                    p = c.text_frame.paragraphs[0]
                    primera = p.runs[0].font
                    p.add_line_break()
                    r = p.add_run()
                    r.text = corto(meses[j - 5]).split("-")[1]
                    r.font.size, r.font.bold, r.font.name = primera.size, primera.bold, primera.name
                    r.font.color.rgb = primera.color.rgb
        for i, m in enumerate(materiales, start=1):
            alerta = m in alertas
            valores = [m["nombre"], meses_o_raya(m["minimo_meses"]), meses_o_raya(m["maximo_meses"]),
                       miles(m["stock_partida"]), meses_o_raya(m["cobertura_inicial"])]
            for j, v in enumerate(valores):
                u.celda(t, i, j, v, tam=TAM, borde_abajo=LINEA, negrita=j == 0 and alerta,
                        alinear=PP_ALIGN.LEFT if j == 0 else PP_ALIGN.RIGHT, color=ROJO if j == 0 and alerta else TINTA)
            for kk, p in enumerate(m["proyeccion"][:n]):
                fondo, color, negrita, _ = ESTADO.get(p["estado_cobertura"], ESTADO["sin_parametro"])
                cob = p["cobertura"]
                valor = "—" if cob is None else f"{cob:.0f}" if cob >= 99.95 else f"{cob:.1f}"
                c = u.celda(t, i, 5 + kk, valor, tam=TAM, fondo=fondo, color=color, negrita=negrita,
                            alinear=PP_ALIGN.CENTER, borde_abajo="FFFFFF")
                c.margin_left = c.margin_right = Inches(0.01)
        y_ley = y_t + (len(materiales) + 1) * fila + 0.22
        u.texto(s, MARGEN, y_ley, 3.6, 0.24, "Color = cobertura frente al mín/máx del S&OP en meses:", tam=10,
                color=GRIS, ancla=MSO_ANCHOR.MIDDLE, nombre="Leyenda")
        xc = MARGEN + 3.45
        for clave in ("quiebre", "bajo_minimo", "en_rango", "sobre_maximo"):
            fondo, color, _, nombre = ESTADO[clave]
            xc += u.pastilla(s, xc, y_ley, nombre, fondo, color, tam=10, alto=0.24) + 0.1
        sin_minmax = any(p["minimo"] is None for m in materiales for p in m["proyeccion"])
        u.texto(s, xc + 0.1, y_ley, ANCHO - MARGEN - xc - 0.1, 0.24,
                "Cifras en meses · — = sin salidas planificadas" + (" · sin color = mes sin mín/máx" if sin_minmax
                                                                      else ""), tam=10, color=GRIS,
                ancla=MSO_ANCHOR.MIDDLE)
        return s
