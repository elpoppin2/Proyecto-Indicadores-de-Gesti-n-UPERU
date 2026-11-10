"""Compara la copia HTML de Planificación con el localhost, sección por sección.

La copia (exportar_planificacion_html) vuelve a calcular en JavaScript lo que
el servidor calcula en Python, para que sus filtros funcionen sin servidor.
Eso son dos versiones de cada cálculo, y pueden separarse sin que nadie lo
note (pedido del usuario, 2026-11-10: "que cada cálculo se escriba una sola
vez"). Este comando abre la copia en Edge sin ventana y, para cada sección y
con los filtros por defecto y algunas opciones de cada filtro, compara las
cifras que muestra con las que muestra el localhost para los mismos filtros.
Una cifra que difiere solo en el último dígito se informa aparte como
redondeo; cualquier otra diferencia es error.

    venv\\Scripts\\python.exe manage.py verificar_copia            (exporta una copia nueva y la revisa)
    venv\\Scripts\\python.exe manage.py verificar_copia --html <archivo> --secciones sec-energia
"""

import collections
import os
import pathlib
import re
import tempfile
import time
from decimal import Decimal
from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.test import Client

from gestion import views

from ._navegador import Navegador

# Cifras: no las pegadas a letras ("ZMM056", "MB51", "Oct-26"); fuera también
# los códigos de material, las fechas y los años sueltos (rótulos y notas).
_NUMERO = re.compile(r"(?<![-A-Za-zÁÉÍÓÚáéíóúÑñ\d.,])[-−+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![A-Za-z\d])")
_NO_CIFRAS = re.compile(r"\b\d{2}-\d{8}\b|\b\d{1,2}/\d{1,2}/\d{4}\b|(?<![\d,.])20[0-3]\d(?!\d)(?![.,]\d)")
# Lo que la copia todavía no trae a propósito (sección -> qué falta): se
# informa, pero no cuenta como error mientras la copia no muestre cifras de más.
DIFERENCIAS_CONOCIDAS = {
    # La matriz TCO ya va dibujada por el localhost (2026-11-10); dibujar también el detalle
    # de cada material (con las partidas de la FBL3N) pesaba ~58 MB más.
    "sec-precios": "la copia no trae el selector Real/MB51 y, con un material elegido, su detalle no lleva los "
                   "embarques, las columnas de MB51 y FBL3N ni las partidas de la FBL3N",
}
_VACIOS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
# Lo que no cuenta en ninguna de las dos: filtros (sus opciones), textos de
# "Cómo se calcula" y notas (prosa escrita aparte en cada una), lo oculto y
# el texto de los gráficos (qué puntos llevan etiqueta depende de cada
# dibujo; sus cifras ya se comparan en las tablas y métricas).
_LEER_COPIA = """(function(id){var s=document.getElementById(id);if(!s)return null;var c=s.cloneNode(true);
c.querySelectorAll('form.filtros, details.metodo, p.nota, .chart-wrap, [hidden], script, style, template').forEach(function(e){e.remove()});
var w=document.createTreeWalker(c,NodeFilter.SHOW_TEXT),t=[],x;while((x=w.nextNode()))t.push(x.data);return t.join(' ');})"""
_FILTROS_COPIA = """(function(id){var s=document.getElementById(id);if(!s)return [];
return [].map.call(s.querySelectorAll('form.filtros select[id]:not(:disabled)'),function(e){
return {nombre:e.id, actual:e.value, opciones:[].map.call(e.options,function(o){return o.value})};});})"""
# Filtros bloqueados en la copia (Simulación: solo el S&OP) no se prueban.
_FIJAR_COPIA = """(function(id,valor){var s=document.getElementById(id);var antes=s.value;s.value=valor;
s.dispatchEvent(new Event('change',{bubbles:true}));return antes;})"""


class _Texto(HTMLParser):
    """Texto de un HTML sin los filtros, el "Cómo se calcula" ni lo oculto."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.partes, self.pila, self.saltar = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _VACIOS:
            return
        a = dict(attrs)
        clases = (a.get("class") or "").split()
        excluir = ("hidden" in a or tag in ("script", "style", "template")
                   or (tag == "form" and "filtros" in clases) or (tag == "details" and "metodo" in clases)
                   or (tag == "p" and "nota" in clases) or "chart-wrap" in clases)
        self.pila.append((tag, excluir))
        self.saltar += excluir

    def handle_endtag(self, tag):
        if tag in _VACIOS or not any(t == tag for t, _ in self.pila):
            return
        while self.pila:
            t, excluir = self.pila.pop()
            self.saltar -= excluir
            if t == tag:
                break

    def handle_data(self, data):
        if not self.saltar:
            self.partes.append(data)


def texto_html(html):
    lector = _Texto()
    lector.feed(html)
    return " ".join(lector.partes)


def cifras(texto):
    texto = _NO_CIFRAS.sub(" ", texto or "")
    return collections.Counter(n.replace("−", "-").lstrip("+") for n in _NUMERO.findall(texto))


def _valor(token):
    return Decimal(token.replace(",", ""))


def comparar(servidor, copia):
    """(redondeos, solo_servidor, solo_copia): las cifras que difieren solo en
    una unidad del último dígito se emparejan como redondeo."""
    a, b = cifras(servidor), cifras(copia)
    solo_s, solo_c = list((a - b).elements()), list((b - a).elements())
    redondeos = 0
    for token in list(solo_s):
        decimales = len(token.split(".")[1]) if "." in token else 0
        paso = Decimal(1).scaleb(-decimales)
        par = next((t for t in solo_c if (len(t.split(".")[1]) if "." in t else 0) == decimales
                    and abs(_valor(t) - _valor(token)) <= paso), None)
        if par is not None:
            solo_s.remove(token)
            solo_c.remove(par)
            redondeos += 1
    return redondeos, solo_s, solo_c


class Command(BaseCommand):
    help = ("Compara las cifras de la copia HTML de Planificación con las del localhost, sección por sección, "
            "con los filtros por defecto y algunas opciones de cada filtro. Falla si alguna cifra difiere "
            "(más allá de un redondeo en el último dígito).")

    def add_arguments(self, parser):
        parser.add_argument("--html", help="Copia a revisar (por defecto, exporta una nueva a una carpeta temporal).")
        parser.add_argument("--secciones", nargs="*", help="Solo estas secciones (p.ej. sec-energia).")
        parser.add_argument("--opciones", type=int, default=2,
                            help="Opciones que se prueban de cada filtro, además de la por defecto (0 = solo por defecto).")

    def handle(self, *args, **options):
        html = options["html"]
        if not html:
            html = os.path.join(tempfile.mkdtemp(prefix="uperu_copia_"), "Planificacion_copia.html")
            call_command("exportar_planificacion_html", html, stdout=open(os.devnull, "w"))
        if not os.path.exists(html):
            raise CommandError(f"No existe {html}")
        usuario = get_user_model().objects.filter(is_superuser=True, is_active=True).first()
        if usuario is None:
            raise CommandError("No hay un superusuario activo para abrir el localhost")
        cliente = Client(HTTP_HOST="127.0.0.1")
        cliente.force_login(usuario)

        zonas = {s: z for z, _, _, secciones in views._ZONAS_PLANIFICACION for s in secciones}
        if options["secciones"]:
            zonas = {s: z for s, z in zonas.items() if s in options["secciones"]}
        url = pathlib.Path(html).resolve().as_uri()
        resultados = {"ok": 0, "redondeo": 0, "conocida": 0, "distinto": 0}
        inicio = time.monotonic()

        def servidor(seccion, parametros):
            consulta = "&".join(f"{k}={v}" for k, v in parametros.items() if v != "")
            r = cliente.get(f"/planificacion/?_seccion={seccion}" + (f"&{consulta}" if consulta else ""),
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            return texto_html(r.content.decode("utf-8"))

        def informar(seccion, parametros, texto_servidor, texto_copia):
            redondeos, solo_s, solo_c = comparar(texto_servidor, texto_copia)
            escenario = ", ".join(f"{k}={v or 'Todos'}" for k, v in parametros.items()) or "por defecto"
            if (solo_s or solo_c) and seccion in DIFERENCIAS_CONOCIDAS:
                resultados["conocida"] += 1
                self.stdout.write(self.style.WARNING(
                    f"  {seccion} ({escenario}): diferencia conocida ({DIFERENCIAS_CONOCIDAS[seccion]}): "
                    f"{len(solo_s)} cifras solo en el localhost y {len(solo_c)} solo en la copia"))
            elif solo_s or solo_c:
                resultados["distinto"] += 1
                self.stdout.write(self.style.ERROR(
                    f"  {seccion} ({escenario}): DISTINTO — solo en el localhost {len(solo_s)} {solo_s[:6]}; "
                    f"solo en la copia {len(solo_c)} {solo_c[:6]}"))
            elif redondeos:
                resultados["redondeo"] += 1
                self.stdout.write(f"  {seccion} ({escenario}): igual salvo {redondeos} redondeo(s) en el último dígito")
            else:
                resultados["ok"] += 1
                self.stdout.write(f"  {seccion} ({escenario}): igual ({sum(cifras(texto_servidor).values())} cifras)")

        def leer_estable(nav, seccion, espera=8.0):
            """Texto de la sección en la copia cuando deja de cambiar (algunas
            se redibujan en dos pasos después de un filtro)."""
            anterior, limite = None, time.monotonic() + espera
            while True:
                time.sleep(0.4)
                actual = nav.js(f"{_LEER_COPIA}({seccion!r})")
                if actual == anterior or time.monotonic() > limite:
                    return actual
                anterior = actual

        with Navegador() as nav:
            def abrir_seccion(seccion, zona):
                nav.abrir(url)
                nav.js(f"window.uperuZonas.activar({zona!r})")
                time.sleep(1.0)
                return leer_estable(nav, seccion)

            for seccion, zona in zonas.items():
                texto_copia = texto_defecto = abrir_seccion(seccion, zona)
                if texto_copia is None:
                    self.stdout.write(f"  {seccion}: no está en la copia")
                    continue
                informar(seccion, {}, servidor(seccion, {}), texto_copia)
                for filtro in nav.js(f"{_FILTROS_COPIA}({seccion!r})") if options["opciones"] else []:
                    otras = [o for o in filtro["opciones"] if o not in (filtro["actual"], "")][:options["opciones"]]
                    for valor in otras:
                        antes = nav.js(f"{_FIJAR_COPIA}({filtro['nombre']!r}, {valor!r})")
                        texto_copia = leer_estable(nav, seccion)
                        informar(seccion, {filtro["nombre"]: valor}, servidor(seccion, {filtro["nombre"]: valor}), texto_copia)
                        nav.js(f"{_FIJAR_COPIA}({filtro['nombre']!r}, {antes!r})")
                        # Volver al filtro anterior no siempre deja todo como al abrir
                        # (otro filtro pudo cambiar en cascada): entonces se reabre.
                        if leer_estable(nav, seccion) != texto_defecto:
                            abrir_seccion(seccion, zona)
            errores = nav.js("window.__errores")

        self.stdout.write(f"{sum(resultados.values())} comparaciones en {time.monotonic() - inicio:.0f} s: "
                          f"{resultados['ok']} iguales, {resultados['redondeo']} con redondeos, "
                          f"{resultados['conocida']} con diferencias conocidas, {resultados['distinto']} distintas.")
        if errores:
            raise CommandError(f"Errores de JavaScript en la copia: {errores}")
        if resultados["distinto"]:
            raise CommandError(f"{resultados['distinto']} comparación(es) con cifras distintas entre la copia y el localhost")
