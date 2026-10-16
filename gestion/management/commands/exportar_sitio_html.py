import base64
import datetime
import os
import re
import time
import unicodedata
from urllib.parse import quote, urlsplit

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test import Client

from gestion import views

_GESTION = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ESTATICOS = os.path.join(_GESTION, "static")
DESCARGAS = "Descargas"
INICIO = "/portada/"

# Lo que en la copia no puede funcionar (necesita el servidor): los filtros
# quedan en su valor por defecto y deshabilitados, y se ocultan la sesión, el
# reinicio de filtros, el detalle fila por fila y el asistente de IA.
_CSS_ESTATICO = """
.btn-reiniciar, details.desglose, .sesion form, .sesion-usuario,
.asistente-lanzador, .asistente-panel { display: none !important; }
form.filtros select:disabled, form.filtros input:disabled, .filtro-multi-boton:disabled,
[data-buscar-servidor]:disabled { cursor: not-allowed; opacity: .7; }
a[data-solo-localhost] { pointer-events: none; color: inherit; text-decoration: none; }
.aviso-copia { margin: 0 0 14px; padding: 8px 12px; border-radius: 6px; font-size: 13px;
  background: var(--superficie-2, rgba(127,127,127,.08)); color: var(--texto-2, inherit); }
"""

_JS_ESTATICO = """
(function () {
  var AVISO = 'Filtro disponible en el localhost: esta copia muestra la vista por defecto.';
  function bloquear(raiz) {
    raiz.querySelectorAll('form.filtros select, form.filtros input, [data-buscar-servidor], .filtro-multi-boton')
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
    if (e.target.closest && e.target.closest('form.filtros')) e.stopImmediatePropagation();
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
        "Genera una copia estática de todo el dashboard (cada página con todas sus pestañas, "
        "vista por defecto) en una carpeta, para compartirla sin servidor ni login. Cada "
        "archivo es autocontenido (estilos, scripts y logo incrustados) y los enlaces entre "
        "páginas apuntan a los demás archivos de la carpeta."
    )

    def add_arguments(self, parser):
        parser.add_argument("carpeta", help="Carpeta destino (se sobrescriben los archivos de la copia)")
        parser.add_argument("--sin-descargas", action="store_true",
                            help="No generar los Excel/PDF de los botones de descarga")

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

        # Páginas: las de la barra de navegación (con sus nombres) más el informe mensual.
        portada = self._get(INICIO)
        nav = re.findall(r'<a href="(/[^"]*)" class="nav-link[^"]*"[^>]*>([^<]+)</a>', portada)
        if not nav:
            raise CommandError("No se encontró la barra de navegación en la portada")
        self.archivos = {}  # ruta del localhost -> archivo de la copia
        for ruta, nombre in nav:
            nombre = _sin_tildes(nombre.strip())
            self.archivos[ruta] = "Inicio - Indicadores Supply Chain.html" if ruta == INICIO else f"{nombre}.html"
        self.archivos["/"] = self.archivos[INICIO]
        self.archivos["/informe-mensual/"] = "Informe mensual.html"

        self.descargas = {}  # ruta de descarga del localhost -> archivo en Descargas/
        if not options["sin_descargas"]:
            self._preparar_descargas()

        escritos = []
        for ruta, archivo in self.archivos.items():
            if ruta == "/":
                continue
            t = time.monotonic()
            html = portada if ruta == INICIO else self._get(ruta)
            html = self._con_zonas(ruta, html)
            html = self._autocontenido(html)
            html = self._enlaces(html)
            self._escribir(os.path.join(carpeta, archivo), html.encode("utf-8"))
            escritos.append(archivo)
            self.stdout.write(f"  {archivo}: {len(html) // 1024:,} KB ({time.monotonic() - t:.1f} s)")

        if self.descargas:
            os.makedirs(os.path.join(carpeta, DESCARGAS), exist_ok=True)
            for ruta, (archivo, contenido) in self.descargas.items():
                self._escribir(os.path.join(carpeta, DESCARGAS, archivo), contenido)
                self.stdout.write(f"  {DESCARGAS}/{archivo}: {len(contenido) // 1024:,} KB")

        self.stdout.write(self.style.SUCCESS(
            f"Copia lista en {carpeta}: {len(escritos)} páginas y {len(self.descargas)} descargas "
            f"en {time.monotonic() - inicio:.0f} s. Abrir: {self.archivos[INICIO]}"
        ))

    # --- Pedidos al dashboard ------------------------------------------------
    def _get(self, url):
        respuesta = self.cliente.get(url, HTTP_HOST="127.0.0.1")
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

        aviso = (f'<p class="aviso-copia">Copia del {self.generado:%d/%m/%Y %H:%M} con la vista por defecto de '
                 f'cada pestaña. Los filtros y el detalle fila por fila funcionan en el localhost.</p>')
        html = html.replace("</header>", "</header>" + aviso, 1)
        html = html.replace("</head>", f"<style>{_CSS_ESTATICO}</style>\n</head>", 1)
        html = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + f"\n<script>{_JS_ESTATICO}</script>", html, count=1)
        return html.replace("</body>", "".join(diferidos) + "\n</body>", 1)

    def _enlaces(self, html):
        """Enlaces del localhost -> archivos de la copia (con su #pestaña). Los
        que no tienen copia quedan como texto."""
        def cambiar(m):
            partes = urlsplit(m.group(1).replace("&amp;", "&"))
            clave = self._clave_descarga(partes.path, partes.query)
            if clave in self.descargas:
                destino = f"{DESCARGAS}/{quote(self.descargas[clave][0])}"
            elif partes.path in self.archivos:
                destino = quote(self.archivos[partes.path])
            else:
                return 'data-solo-localhost title="Disponible en el localhost"'
            return f'href="{destino}{"#" + partes.fragment if partes.fragment else ""}"'
        return re.sub(r'href="(/[^"]*)"', cambiar, html)

    @staticmethod
    def _escribir(ruta, contenido):
        # Temporal + reemplazo: si algo falla, queda el archivo anterior.
        tmp = ruta + ".tmp"
        with open(tmp, "wb") as f:
            f.write(contenido)
        os.replace(tmp, ruta)
