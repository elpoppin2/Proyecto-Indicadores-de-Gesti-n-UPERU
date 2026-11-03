import os

from django.core.management.base import BaseCommand, CommandError

from dashboard import queries as q
from gestion.precios_sop import armar_version, escribir_plantilla, nombre_archivo, proyectados_de_version
from ._sop_excel import mes_snapshot_desde_nombre


class Command(BaseCommand):
    help = (
        "Genera la plantilla de precios S&OP del mes indicado (por defecto, el siguiente a la última versión "
        "cargada): meses reales llenos con el precio del tablero y meses proyectados con los de la versión "
        "anterior, listos para editar celda por celda. No reemplaza una plantilla que ya existe (salvo --reemplazar)."
    )

    def add_arguments(self, parser):
        parser.add_argument("carpeta", help="Carpeta de salida")
        parser.add_argument("--mes", help="Mes de emisión 'Noviembre 2026'")
        parser.add_argument("--reemplazar", action="store_true", help="Reemplaza la plantilla si ya existe.")

    def handle(self, *args, **options):
        carpeta = options["carpeta"]
        if not os.path.isdir(carpeta):
            raise CommandError(f"No existe la carpeta {carpeta}")
        mes = mes_snapshot_desde_nombre(options["mes"]) if options["mes"] else q.mes_siguiente_sop()
        if mes is None:
            raise CommandError("No se reconoce el mes: escríbelo como 'Noviembre 2026'.")
        base = [v for v in q.versiones_precios_sop() if v < mes]
        if not base:
            raise CommandError("No hay una versión de precios anterior cargada: convierte primero un Excel "
                               "con 'manage.py convertir_precios_sop'.")
        salida = os.path.join(carpeta, nombre_archivo(mes))
        if os.path.exists(salida) and not options["reemplazar"]:
            raise CommandError(f"Ya existe {salida} (usa --reemplazar si quieres generarla de nuevo).")
        version = armar_version(mes, proyectados_de_version(base[-1], mes),
                                origen=f"la versión de precios de {q.formato_mes_anio(base[-1])}")
        escribir_plantilla(salida, version)
        self.stdout.write(self.style.SUCCESS(
            f"{salida}: {len(version['filas'])} materiales, {len(version['meses_proyectados'])} meses proyectados "
            f"(base: versión {q.formato_mes_anio(base[-1])})."))
