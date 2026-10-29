import time

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test import Client

from gestion import views


class Command(BaseCommand):
    help = (
        "Abre todas las páginas del dashboard (con sus filtros por defecto) para dejar "
        "sus consultas calculadas en la caché en disco. Correr después de cada carga de "
        "datos (lo hace la tarea programada): así la primera apertura del día es inmediata."
    )

    def handle(self, *args, **options):
        usuario = (
            get_user_model().objects.filter(is_active=True, is_superuser=True).first()
            or get_user_model().objects.filter(is_active=True).first()
        )
        if usuario is None:
            raise CommandError("No hay usuarios activos para abrir las páginas")

        cliente = Client()
        cliente.force_login(usuario)
        # Planificación trae solo Indicadores y pide cada zona aparte al
        # abrirla: se calientan también esas (con sus filtros por defecto).
        paginas = ["/portada/", "/overview/", "/overview/?_zona=detalle", "/informe-mensual/", "/planificacion/"] + [
            f"/planificacion/?_zona={clave}" for clave, _, _, secciones in views._ZONAS_PLANIFICACION if secciones
        ]
        # Logística de Materias Primas: mismas secciones con menos materiales.
        paginas += ["/logistica-materias-primas/"] + [
            f"/logistica-materias-primas/?_zona={clave}" for clave, _, _, secciones in views._ZONAS_LOGISTICA if secciones
        ]
        # Igual las categorías: la página trae Indicadores y cada pestaña se pide aparte.
        con_inventario = set(views.q.categorias_inventario_disponibles())
        for c in views._categorias_disponibles():
            paginas.append(f"/{views._slug(c)}/")
            paginas += [f"/{views._slug(c)}/?_zona={z['clave']}"
                        for z in views._zonas_categoria(c, c in con_inventario)[1:]]
        inicio = time.monotonic()
        fallidas = 0
        for pagina in paginas:
            t = time.monotonic()
            respuesta = cliente.get(pagina, HTTP_HOST="127.0.0.1")
            estado = "OK" if respuesta.status_code == 200 else f"HTTP {respuesta.status_code}"
            fallidas += respuesta.status_code != 200
            self.stdout.write(f"  {pagina}: {estado} ({time.monotonic() - t:.1f} s)")
        self.stdout.write(self.style.SUCCESS(
            f"Caché lista: {len(paginas)} páginas en {time.monotonic() - inicio:.0f} s"
            + (f" ({fallidas} con error)" if fallidas else "")
        ))
        if fallidas:
            raise CommandError(f"{fallidas} página(s) no se pudieron abrir")
