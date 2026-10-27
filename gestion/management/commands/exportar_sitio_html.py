import base64
import datetime
import os
import re
import time
import unicodedata
from html import escape
from urllib.parse import quote, urlsplit

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test import Client

from gestion import views

_GESTION = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ESTATICOS = os.path.join(_GESTION, "static")
DESCARGAS = "Descargas"
INICIO = "/portada/"
# Cada archivo de la copia muestra solo su página en la barra de navegación y
# no enlaza a las demás (pedido del usuario, 2026-10-21: cada HTML se comparte
# con su área). Excepción: Planificación navega también a Logística de
# Materias Primas; esa Logística va en un archivo propio del grupo
# ("Planificacion - Logistica de Materias Primas.html") con la barra de las
# dos, y el "Logistica de Materias Primas.html" suelto solo se ve a sí mismo.
GRUPOS = {"/planificacion/": ["/planificacion/", "/logistica-materias-primas/"]}
# Páginas que resumen todas las áreas: no van en la copia por área (pedido del
# usuario, 2026-10-21; cada área debería tener las suyas, con sus segmentadores).
SIN_COPIA = {INICIO, "/overview/", "/informe-mensual/"}

# Lo que en la copia no puede funcionar (necesita el servidor): los filtros
# quedan en su valor por defecto y deshabilitados, y se ocultan la sesión, el
# reinicio de filtros, el detalle fila por fila y el asistente de IA.
_CSS_ESTATICO = """
.btn-reiniciar, details.desglose, .sesion form, .sesion-usuario,
.asistente-lanzador, .asistente-panel { display: none !important; }
form.filtros select:disabled, form.filtros input:disabled, .filtro-multi-boton:disabled,
[data-buscar-servidor]:disabled { cursor: not-allowed; opacity: .7; }
a[data-solo-localhost] { pointer-events: none; color: inherit; text-decoration: none; }
a[data-descarga-localhost] { display: none !important; }
.aviso-copia { margin: 0 0 14px; padding: 8px 12px; border-radius: 6px; font-size: 13px;
  background: var(--superficie-2, rgba(127,127,127,.08)); color: var(--texto-2, inherit); }
"""

_JS_ESTATICO = """
(function () {
  var AVISO = 'Filtro disponible en el localhost: esta copia muestra la vista por defecto.';
  function bloquear(raiz) {
    // form[data-estatico] (Año de Indicadores) trae sus vistas dibujadas: sigue activo.
    raiz.querySelectorAll('form.filtros:not([data-estatico]) select, form.filtros:not([data-estatico]) input, [data-buscar-servidor], .filtro-multi-boton')
      .forEach(function (c) { c.disabled = true; c.title = AVISO; });
  }
  window.uperuBloquearFiltros = bloquear;
  // Cada pestaña viaja en un <template> y se dibuja la primera vez que se abre
  // (zonas.js llama a esta función en vez de pedirla al servidor).
  window.uperuCargarZona = function (zona) {
    var panel = document.getElementById('zona-' + zona), plantilla = document.getElementById('plantilla-zona-' + zona);
    if (!panel || !plantilla || panel._copiaCargada) return;
    panel._copiaCargada = true;
    panel.innerHTML = plantilla.innerHTML;
    bloquear(panel);
    if (window.uperuInicializarContenido) window.uperuInicializarContenido(panel);
    if (!panel.hidden) document.dispatchEvent(new CustomEvent('uperu:zona-entrando', { detail: { panel: panel } }));
  };
  document.addEventListener('DOMContentLoaded', function () { bloquear(document); });
  // Por si algo cambia un filtro igual (atajos de las tarjetas, detalle): no se pide nada al servidor.
  window.addEventListener('change', function (e) {
    if (e.target.closest && e.target.closest('form.filtros:not([data-estatico])')) e.stopImmediatePropagation();
  }, true);
  window.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('.filtro-multi-boton, [data-material-precios], [data-material-calidad], [data-filtrar], [data-reiniciar]');
    if (t) { e.stopImmediatePropagation(); e.preventDefault(); }
  }, true);
})();
"""


def _sin_tildes(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


class Command(BaseCommand):
    help = (
        "Genera una copia estática del dashboard (cada página con todas sus pestañas, vista "
        "por defecto) en una carpeta, para compartirla sin servidor ni login. Cada archivo es "
        "autocontenido (estilos, scripts y logo incrustados) y solo muestra su página (ver "
        "GRUPOS). Sin los Excel/PDF de los botones de descarga, salvo --con-descargas."
    )

    def add_arguments(self, parser):
        parser.add_argument("carpeta", help="Carpeta destino (se sobrescriben los archivos de la copia)")
        parser.add_argument("--con-descargas", action="store_true",
                            help="Generar también los Excel/PDF de los botones de descarga (carpeta Descargas)")
        # Copias "a un corte" (pedido del usuario, 2026-10-26: cada categoría
        # acumulada a setiembre 2026; ver el script que la arma sobre una base
        # recortada a esa fecha).
        parser.add_argument("--solo-categorias", action="store_true", help="Solo las páginas de las categorías")
        parser.add_argument("--parametros", default="",
                            help="Filtros para todas las páginas y pestañas (query string, p.ej. anio_compras=2026)")
        parser.add_argument("--aviso", default="", help="Texto del aviso de la copia (en vez del de siempre)")

    def handle(self, *args, **options):
        carpeta = options["carpeta"]
        if not os.path.isdir(carpeta):
            raise CommandError(f"No existe la carpeta {carpeta}")
        usuario = get_user_model().objects.filter(is_active=True, is_superuser=True).first()
        if usuario is None:
            raise CommandError("No hay un superusuario activo para abrir las páginas")
        self.cliente = Client()
        self.cliente.force_login(usuario)
        inicio = time.monotonic()
        self.generado = datetime.datetime.now()
        self.parametros = options.get("parametros", "").lstrip("?&")
        self.aviso = options.get("aviso", "")

        # Páginas: las de la barra de navegación (con sus nombres) más el informe mensual.
        portada = self._get(INICIO)
        nav = re.findall(r'<a href="(/[^"]*)" class="nav-link[^"]*"[^>]*>([^<]+)</a>', portada)
        if not nav:
            raise CommandError("No se encontró la barra de navegación en la portada")
        archivos = {}  # ruta del localhost -> archivo suelto de la copia
        for ruta, nombre in nav:
            nombre = _sin_tildes(nombre.strip())
            archivos[ruta] = "Inicio - Indicadores Supply Chain.html" if ruta == INICIO else f"{nombre}.html"
        archivos["/informe-mensual/"] = "Informe mensual.html"
        archivos = {r: a for r, a in archivos.items() if r not in SIN_COPIA}
        if options.get("solo_categorias"):
            categorias = {f"/{views._slug(c)}/" for c in views._categorias_disponibles()}
            archivos = {r: a for r, a in archivos.items() if r in categorias}

        # Qué se escribe: cada página suelta (solo se enlaza a sí misma) y, por
        # grupo, sus otras páginas en un archivo del grupo que enlaza a todas.
        salidas = []  # (ruta, archivo, {ruta permitida: archivo})
        for ruta, archivo in archivos.items():
            grupo = GRUPOS.get(ruta, [ruta])
            mapa = {r: archivo if r == ruta else self._archivo_grupo(archivos[ruta], archivos[r]) for r in grupo if r in archivos}
            salidas.append((ruta, archivo, mapa))
        for cabeza, grupo in GRUPOS.items():
            if cabeza not in archivos:
                continue
            mapa = {r: archivos[cabeza] if r == cabeza else self._archivo_grupo(archivos[cabeza], archivos[r])
                    for r in grupo if r in archivos}
            salidas += [(r, mapa[r], mapa) for r in grupo if r != cabeza and r in mapa]

        self.descargas = {}  # ruta de descarga del localhost -> archivo en Descargas/
        if options["con_descargas"]:
            self._preparar_descargas()

        escritos, paginas = [], {}
        for ruta, archivo, mapa in salidas:
            t = time.monotonic()
            if ruta not in paginas:
                html = portada if ruta == INICIO else self._get(ruta)
                paginas[ruta] = self._autocontenido(self._con_zonas(ruta, html))
            html = self._enlaces(self._solo_permitidas(paginas[ruta], mapa), mapa)
            self._escribir(os.path.join(carpeta, archivo), html.encode("utf-8"))
            escritos.append(archivo)
            self.stdout.write(f"  {archivo}: {len(html) // 1024:,} KB ({time.monotonic() - t:.1f} s)")
        self.archivos = archivos

        if self.descargas:
            os.makedirs(os.path.join(carpeta, DESCARGAS), exist_ok=True)
            for ruta, (archivo, contenido) in self.descargas.items():
                self._escribir(os.path.join(carpeta, DESCARGAS, archivo), contenido)
                self.stdout.write(f"  {DESCARGAS}/{archivo}: {len(contenido) // 1024:,} KB")

        self.stdout.write(self.style.SUCCESS(
            f"Copia lista en {carpeta}: {len(escritos)} archivos y {len(self.descargas)} descargas "
            f"en {time.monotonic() - inicio:.0f} s."
        ))

    # --- Pedidos al dashboard ------------------------------------------------
    def _get(self, url):
        # X-Uperu-Copia: las vistas que pueden traer sus filtros ya resueltos
        # (Año de Indicadores de Planificación) los dibujan todos.
        if getattr(self, "parametros", ""):
            url += ("&" if "?" in url else "?") + self.parametros
        respuesta = self.cliente.get(url, HTTP_HOST="127.0.0.1", HTTP_X_UPERU_COPIA="1")
        if respuesta.status_code != 200:
            raise CommandError(f"{url}: HTTP {respuesta.status_code}")
        return respuesta.content.decode("utf-8")

    def _preparar_descargas(self):
        """Excel y PDF de los botones de descarga, con los filtros por defecto."""
        candidatas = ["/overview/exportar/", "/planificacion/premios/exportar/",
                      "/planificacion/energia/exportar/", "/informe-mensual/?pdf=1"]
        candidatas += [f"/{views._slug(c)}/exportar/" for c in views._categorias_disponibles()]
        for url in candidatas:
            respuesta = self.cliente.get(url, HTTP_HOST="127.0.0.1")
            if respuesta.status_code != 200 or respuesta.get("Content-Type", "").startswith("text/html"):
                self.stdout.write(self.style.WARNING(f"  sin descarga para {url} (HTTP {respuesta.status_code})"))
                continue
            nombre = re.search(r'filename="?([^";]+)"?', respuesta.get("Content-Disposition", ""))
            nombre = nombre.group(1) if nombre else url.strip("/").replace("/", "_")
            partes = urlsplit(url)
            self.descargas[self._clave_descarga(partes.path, partes.query)] = (nombre, respuesta.content)

    @staticmethod
    def _clave_descarga(ruta, query):
        # El informe mensual es página y, con ?pdf=1, descarga.
        return ruta + ("?pdf" if "pdf=1" in query else "")

    # --- Armado de cada página ----------------------------------------------
    def _con_zonas(self, ruta, html):
        """Cada pestaña que el localhost pide al abrirla viaja en un <template>
        (antes de zonas.js, que la dibuja con window.uperuCargarZona)."""
        zonas = re.findall(r'<div class="zona-panel" id="zona-([\w-]+)"[^>]*data-pendiente', html)
        if not zonas:
            return html
        plantillas = "".join(
            f'<template id="plantilla-zona-{z}">{self._get(f"{ruta}?_zona={z}")}</template>\n' for z in zonas)
        marca = re.search(r'<script src="/static/gestion/js/zonas\.js[^"]*"></script>', html)
        if not marca:
            raise CommandError(f"{ruta}: no se encontró zonas.js")
        return html[:marca.start()] + plantillas + html[marca.start():]

    def _autocontenido(self, html):
        """Estilos, scripts y logo incrustados; aviso de copia y ajustes de la
        copia estática. Los scripts con defer pasan al final del <body> (mismo
        orden de ejecución)."""
        def leer(ruta_estatica):
            with open(os.path.join(ESTATICOS, *ruta_estatica.split("/")), encoding="utf-8") as f:
                return f.read()

        html = re.sub(r'<link rel="stylesheet" href="/static/([^"?]+)[^"]*">',
                      lambda m: f"<style>{leer(m.group(1))}</style>", html)
        diferidos = []

        def script(m):
            ruta = m.group(1)
            if ruta.endswith("asistente.js"):
                return ""
            codigo = "<script>" + leer(ruta).replace("</script", "<\\/script") + "</script>"
            if m.group(2):
                diferidos.append(codigo)
                return ""
            return codigo
        html = re.sub(r'<script src="/static/([^"?]+)[^"]*"( defer)?></script>', script, html)
        with open(os.path.join(ESTATICOS, "gestion", "img", "unacem-logo.svg"), "rb") as f:
            logo = "data:image/svg+xml;base64," + base64.b64encode(f.read()).decode("ascii")
        html = re.sub(r'src="/static/gestion/img/unacem-logo\.svg[^"]*"', f'src="{logo}"', html)
        if "/static/" in html:
            raise CommandError("Quedó una referencia a /static/ sin incrustar")

        aviso = (f'<p class="aviso-copia">{escape(self.aviso)}</p>' if getattr(self, "aviso", "") else
                 f'<p class="aviso-copia">Copia del {self.generado:%d/%m/%Y %H:%M} con la vista por defecto de '
                 f'cada pestaña. Los filtros y el detalle fila por fila funcionan en el localhost.</p>')
        html = html.replace("</header>", "</header>" + aviso, 1)
        html = html.replace("</head>", f"<style>{_CSS_ESTATICO}</style>\n</head>", 1)
        html = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + f"\n<script>{_JS_ESTATICO}</script>", html, count=1)
        return html.replace("</body>", "".join(diferidos) + "\n</body>", 1)

    @staticmethod
    def _archivo_grupo(archivo_cabeza, archivo):
        """Archivo de una página dentro del grupo de otra:
        'Planificacion - Logistica de Materias Primas.html'."""
        return f"{archivo_cabeza[:-len('.html')]} - {archivo}"

    @staticmethod
    def _solo_permitidas(html, mapa):
        """Saca de la barra de navegación las páginas que este archivo no muestra."""
        def nav(m):
            ruta = m.group(1)
            return m.group(0) if (ruta == "/" and INICIO in mapa) or ruta in mapa else ""
        return re.sub(r'<a href="(/[^"]*)" class="nav-link[^"]*"[^>]*>[^<]*</a>\s*', nav, html)

    def _enlaces(self, html, mapa):
        """Enlaces del localhost -> archivos permitidos de la copia (`mapa`, con
        su #pestaña). Los botones de descarga sin archivo generado se ocultan;
        los demás enlaces (otras páginas, detalle) quedan como texto."""
        def cambiar(m):
            partes = urlsplit(m.group(1).replace("&amp;", "&"))
            ruta = INICIO if partes.path == "/" else partes.path
            clave = self._clave_descarga(partes.path, partes.query)
            if clave in self.descargas:
                destino = f"{DESCARGAS}/{quote(self.descargas[clave][0])}"
            elif ruta in mapa:
                destino = quote(mapa[ruta])
            elif partes.path.endswith("/exportar/") or "pdf=1" in partes.query or partes.path == "/informe-mensual/":
                # Descargas y el informe en otra pestaña necesitan el servidor; en
                # la copia, el Informe mensual de cada área se imprime desde su pestaña.
                return 'data-descarga-localhost'
            else:
                return 'data-solo-localhost'
            return f'href="{destino}{"#" + partes.fragment if partes.fragment else ""}"'
        return re.sub(r'href="(/[^"]*)"', cambiar, html)

    @staticmethod
    def _escribir(ruta, contenido):
        # Temporal + reemplazo: si algo falla, queda el archivo anterior.
        tmp = ruta + ".tmp"
        with open(tmp, "wb") as f:
            f.write(contenido)
        os.replace(tmp, ruta)
