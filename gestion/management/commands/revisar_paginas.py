import collections
import random
import time
import traceback
from html.parser import HTMLParser
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test import Client

from gestion import views

# Valores que no vienen de ninguna opción (enlaces viejos, URLs escritas a
# mano): cada página debe responder igual, con sus filtros por defecto.
_INVALIDOS = ["x", "", "-7", "2099-13-45", "2026-02-30", "9" * 25]


class _Filtros(HTMLParser):
    """Formularios de filtros (form.filtros) de una página: su data-target y,
    por cada campo, los valores que ofrece."""

    def __init__(self):
        super().__init__()
        self.formularios, self._actual, self._select = [], None, None
        self.enlaces = []  # detalles fila por fila y descargas a Excel

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "details" and "desglose" in (a.get("class") or "").split() and a.get("data-url"):
            self.enlaces += [a["data-url"], a["data-url"] + "?_parte=filas&offset=50",
                             a["data-url"] + "?orden=x&dir=asc&q=zz&offset=x", a["data-url"] + "?formato=xlsx"]
        elif tag == "a" and "/exportar/" in (a.get("href") or ""):
            self.enlaces.append(a["href"])
        if tag == "form" and "filtros" in (a.get("class") or "").split():
            self._actual = {"target": a.get("data-target"), "campos": collections.OrderedDict()}
            self.formularios.append(self._actual)
        elif self._actual is None or not (a.get("name") or tag == "option"):
            return
        elif tag == "select":
            self._select = a["name"]
            self._actual["campos"].setdefault(self._select, [])
        elif tag == "option" and self._select:
            self._actual["campos"][self._select].append(a.get("value", ""))
        elif tag == "input" and a.get("type", "text") in ("checkbox", "radio", "hidden", "text", "search"):
            self._actual["campos"].setdefault(a["name"], []).append(a.get("value") or "x")

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None
        elif tag == "form":
            self._actual = None


class Command(BaseCommand):
    help = (
        "Prueba de humo del dashboard: abre todas las páginas y pestañas y, en cada "
        "filtro, una muestra de sus opciones reales y valores inválidos. Informa "
        "cualquier error del servidor (500) con su traza. Correr después de cada cambio."
    )

    def add_arguments(self, parser):
        parser.add_argument("--opciones", type=int, default=6,
                            help="Opciones a probar por filtro (las primeras, la última y al azar). 0 = todas.")
        parser.add_argument("--sin-invalidos", action="store_true", help="No probar valores inválidos.")

    def handle(self, *args, **options):
        usuario = get_user_model().objects.filter(is_active=True, is_superuser=True).first()
        if usuario is None:
            raise CommandError("No hay un superusuario activo para abrir las páginas")
        cliente = Client(raise_request_exception=True)
        cliente.force_login(usuario)
        azar = random.Random(1)
        fallos, pedidos, inicio, probados = {}, 0, time.monotonic(), set()

        def pedir(url):
            nonlocal pedidos
            pedidos += 1
            try:
                respuesta = cliente.get(url, HTTP_HOST="127.0.0.1")
            except Exception as error:  # noqa: BLE001 — se informa, no se detiene la revisión
                fallos[url] = "".join(traceback.format_exception(error)[-6:])
                return None
            if respuesta.status_code >= 500:
                fallos[url] = f"HTTP {respuesta.status_code}"
                return None
            return respuesta

        for pagina in self._paginas():
            respuesta = pedir(pagina)
            if respuesta is None:
                continue
            base, _, consulta = pagina.partition("?")
            zona = dict(p.split("=", 1) for p in consulta.split("&")) if consulta else {}
            lector = _Filtros()
            lector.feed(respuesta.content.decode("utf-8", "replace"))
            for enlace in dict.fromkeys(lector.enlaces):
                if enlace not in probados:
                    probados.add(enlace)
                    pedir(enlace.replace("&amp;", "&"))
            for formulario in lector.formularios:
                for nombre, valores in formulario["campos"].items():
                    valores = list(dict.fromkeys(valores))
                    n = options["opciones"]
                    if n and len(valores) > n:
                        valores = valores[:2] + [valores[-1]] + azar.sample(valores[2:-1], max(0, n - 3))
                    if not options["sin_invalidos"]:
                        valores += _INVALIDOS
                    for valor in valores:
                        parametros = {**zona, nombre: valor}
                        if formulario["target"]:
                            parametros["_seccion"] = formulario["target"]
                        pedir(f"{base}?{urlencode(parametros)}")
            self.stdout.write(f"  {pagina}: {'con error' if any(k.startswith(base) for k in fallos) else 'OK'}")
            self.stdout.flush()

        self.stdout.write(f"{pedidos} pedidos en {time.monotonic() - inicio:.0f} s")
        if not fallos:
            self.stdout.write(self.style.SUCCESS("Sin errores."))
            return
        vistos = set()
        for url, traza in fallos.items():
            self.stdout.write(self.style.ERROR(f"ERROR {url}"))
            ultima = traza.strip().splitlines()[-1]
            if ultima not in vistos:
                vistos.add(ultima)
                self.stdout.write(traza)
        raise CommandError(f"{len(fallos)} pedido(s) con error")

    @staticmethod
    def _paginas():
        paginas = ["/portada/", "/overview/", "/informe-mensual/", "/planificacion/"]
        paginas += [f"/planificacion/?_zona={c}" for c, _, _, secciones in views._ZONAS_PLANIFICACION if secciones]
        paginas += ["/logistica-materias-primas/"] + [
            f"/logistica-materias-primas/?_zona={c}" for c, _, _, secciones in views._ZONAS_LOGISTICA if secciones
        ]
        con_inventario = set(views.q.categorias_inventario_disponibles())
        for c in views._categorias_disponibles():
            slug = views._slug(c)
            paginas.append(f"/{slug}/")
            paginas += [f"/{slug}/?_zona={z['clave']}" for z in views._zonas_categoria(c, c in con_inventario)[1:]]
            paginas.append(f"/informe-mensual/?area={slug}")
        return paginas
