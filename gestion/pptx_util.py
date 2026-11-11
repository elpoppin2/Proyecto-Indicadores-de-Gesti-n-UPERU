"""Ayudas de python-pptx para las presentaciones que arma el tablero: textos, etiquetas, tablas con bordes finos,
gráficos y fuentes del tema (estilo de la presentación S&OP del usuario)."""
import re

from lxml import etree
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

TITULO = "Verdana"  # pedido del usuario: Verdana en títulos, Aptos Narrow en el resto
CUERPO = "Aptos Narrow"


def rgb(hexa):
    return RGBColor.from_string(hexa)


def fuentes_tema(prs, mayor=TITULO, menor=CUERPO):
    """Fuente de títulos y de cuerpo del tema (lo que el usuario agregue a mano sale igual)."""
    parte = prs.slide_master.part.part_related_by(RT.THEME)
    xml = parte.blob.decode("utf-8")
    xml = re.sub(r'(<a:majorFont>\s*<a:latin typeface=")[^"]*', r"\g<1>" + mayor, xml)
    xml = re.sub(r'(<a:minorFont>\s*<a:latin typeface=")[^"]*', r"\g<1>" + menor, xml)
    parte._blob = xml.encode("utf-8")


def texto(slide, x, y, w, h, partes, tam=12, color="1B1E21", negrita=False, fuente=CUERPO, alinear=PP_ALIGN.LEFT,
          ancla=MSO_ANCHOR.TOP, nombre=None):
    """Caja de texto sin márgenes. `partes`: texto, o lista de párrafos; cada párrafo es texto o lista de
    (texto, {tam, color, negrita, fuente, cursiva})."""
    caja = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    if nombre:
        caja.name = nombre
    tf = caja.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = ancla
    parrafos = partes if isinstance(partes, list) else [partes]
    for i, par in enumerate(parrafos):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = alinear
        corridas = par if isinstance(par, list) else [(par, {})]
        for t, o in corridas:
            r = p.add_run()
            r.text = t
            f = r.font
            f.name = o.get("fuente", fuente)
            f.size = Pt(o.get("tam", tam))
            f.bold = o.get("negrita", negrita)
            f.italic = o.get("cursiva", False)
            f.color.rgb = rgb(o.get("color", color))
    return caja


def rect(slide, x, y, w, h, relleno, linea=None, redondeado=False, nombre=None, radio=0.08):
    forma = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE if redondeado else MSO_SHAPE.RECTANGLE,
                                   Inches(x), Inches(y), Inches(w), Inches(h))
    if nombre:
        forma.name = nombre
    if redondeado:
        forma.adjustments[0] = radio
    if relleno:
        forma.fill.solid()
        forma.fill.fore_color.rgb = rgb(relleno)
    else:
        forma.fill.background()
    if linea:
        forma.line.color.rgb = rgb(linea)
        forma.line.width = Pt(0.75)
    else:
        forma.line.fill.background()
    forma.shadow.inherit = False
    forma.text_frame.text = ""
    return forma


def pastilla(slide, x, y, texto_p, fondo, color, tam=10, alto=0.26, nombre=None):
    """Etiqueta redondeada de estado; el ancho sale del largo del texto. Devuelve el ancho."""
    ancho = 0.26 + len(texto_p) * tam * 0.0074
    forma = rect(slide, x, y, ancho, alto, fondo, redondeado=True, nombre=nombre, radio=0.5)
    tf = forma.text_frame
    tf.word_wrap = False
    tf.margin_left = tf.margin_right = Inches(0.08)
    tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = texto_p
    r.font.size = Pt(tam)
    r.font.bold = True
    r.font.name = CUERPO
    r.font.color.rgb = rgb(color)
    return ancho


# --- Tablas ---------------------------------------------------------------------------------------
_SIN_ESTILO = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"  # "Sin estilo, sin cuadrícula"


def tabla(slide, x, y, w, anchos, filas_alto, n_filas, nombre=None):
    forma = slide.shapes.add_table(n_filas, len(anchos), Inches(x), Inches(y), Inches(w), Inches(filas_alto * n_filas))
    if nombre:
        forma.name = nombre
    t = forma.table
    tbl_pr = t._tbl.tblPr
    tbl_pr.set("firstRow", "0")
    tbl_pr.set("bandRow", "0")
    estilo = tbl_pr.find(qn("a:tableStyleId"))
    if estilo is None:
        estilo = etree.SubElement(tbl_pr, qn("a:tableStyleId"))
    estilo.text = _SIN_ESTILO
    total = sum(anchos)
    for i, a in enumerate(anchos):
        t.columns[i].width = Emu(int(Inches(w) * a / total))
    for fila in t.rows:
        fila.height = Inches(filas_alto)
    return t


def _borde(tc_pr, lado, color, ancho_pt):
    etiqueta = {"L": "a:lnL", "R": "a:lnR", "T": "a:lnT", "B": "a:lnB"}[lado]
    viejo = tc_pr.find(qn(etiqueta))
    if viejo is not None:
        tc_pr.remove(viejo)
    ln = etree.SubElement(tc_pr, qn(etiqueta), w=str(int(ancho_pt * 12700)), cap="flat", cmpd="sng", algn="ctr")
    if color:
        relleno = etree.SubElement(ln, qn("a:solidFill"))
        etree.SubElement(relleno, qn("a:srgbClr"), val=color)
    else:
        etree.SubElement(ln, qn("a:noFill"))
    # Orden del esquema: lnL, lnR, lnT, lnB, ..., relleno al final.
    orden = ["a:lnL", "a:lnR", "a:lnT", "a:lnB"]
    hijos = {c.tag: c for c in tc_pr}
    for e in orden:
        if qn(e) in hijos:
            tc_pr.remove(hijos[qn(e)])
    pos = 0
    for e in orden:
        if qn(e) in hijos:
            tc_pr.insert(pos, hijos[qn(e)])
            pos += 1


def celda(t, fila, col, valor, tam=10, color="1B1E21", negrita=False, alinear=PP_ALIGN.LEFT, fondo=None,
          borde_abajo=None, borde_arriba=None, grosor=0.5):
    c = t.cell(fila, col)
    c.margin_left = Inches(0.06)
    c.margin_right = Inches(0.06)
    c.margin_top = Inches(0.01)
    c.margin_bottom = Inches(0.01)
    c.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf = c.text_frame
    tf.word_wrap = False
    p = tf.paragraphs[0]
    p.alignment = alinear
    for r in list(p.runs):
        r._r.getparent().remove(r._r)
    # Sin esto, una celda vacía toma el tamaño por defecto (18 pt) y la fila crece.
    p._p.get_or_add_endParaRPr().set("sz", str(int(tam * 100)))
    r = p.add_run()
    r.text = valor
    r.font.size = Pt(tam)
    r.font.bold = negrita
    r.font.name = CUERPO
    r.font.color.rgb = rgb(color)
    if fondo:
        c.fill.solid()
        c.fill.fore_color.rgb = rgb(fondo)
    else:
        c.fill.background()
    tc_pr = c._tc.get_or_add_tcPr()
    for lado in "LR":
        _borde(tc_pr, lado, None, 0)
    _borde(tc_pr, "T", borde_arriba, grosor if borde_arriba else 0)
    _borde(tc_pr, "B", borde_abajo, grosor if borde_abajo else 0)
    # El relleno va después de los bordes.
    for etq in ("a:solidFill", "a:noFill"):
        rel = tc_pr.find(qn(etq))
        if rel is not None:
            tc_pr.remove(rel)
            tc_pr.append(rel)
    return c


# --- Gráficos -------------------------------------------------------------------------------------
def sin_invertir(serie):
    """Cada punto con color propio conserva su color aunque sea negativo (si no, sale en blanco)."""
    for dpt in serie._element.findall(qn("c:dPt")):
        inv = dpt.find(qn("c:invertIfNegative"))
        if inv is None:
            inv = etree.Element(qn("c:invertIfNegative"))
            dpt.find(qn("c:idx")).addnext(inv)
        inv.set("val", "0")


def quitar_lineas_ocultas(grafico):
    """Los puntos vacíos de una línea se dejan como hueco (no como cero)."""
    chart = grafico._chartSpace.chart
    disp = chart.find(qn("c:dispBlanksAs"))
    if disp is None:
        disp = etree.SubElement(chart, qn("c:dispBlanksAs"))
        # Debe ir después de plotVisOnly.
        pvo = chart.find(qn("c:plotVisOnly"))
        if pvo is not None:
            pvo.addnext(disp)
    disp.set("val", "gap")
