import os

from django.core.management.base import BaseCommand, CommandError

from gestion.precios_sop import armar_version, escribir_plantilla, nombre_archivo
from ._precios_sop_excel import leer_legado
from ._sop_excel import mes_snapshot_desde_nombre


class Command(BaseCommand):
    help = (
        "Convierte el Excel anterior de precios ('1. Precios S&OP Proyección 18 meses <Mes> <Año>.xlsx', hojas "
        "'Tar. Atocongo' y 'Tar. Condorcocha') a la plantilla mensual 'Precios SOP - <mes> <año>.xlsx' en la "
        "carpeta indicada. Toma los valores que Excel dejó calculados; el Supuesto de cada fila sale de sus "
        "fórmulas. No modifica el archivo original y no reemplaza una plantilla que ya existe (salvo --reemplazar)."
    )

    def add_arguments(self, parser):
        parser.add_argument("archivo", help="Excel anterior de precios S&OP")
        parser.add_argument("carpeta", help="Carpeta de salida (p.ej. ...\\DOCUMENTOS DESCARGA TABLAS MM\\TABLA SOP PRECIOS)")
        parser.add_argument("--mes", help="Mes de emisión 'Octubre 2026' (por defecto, el del nombre del archivo)")
        parser.add_argument("--reemplazar", action="store_true", help="Reemplaza la plantilla si ya existe.")

    def handle(self, *args, **options):
        archivo, carpeta = options["archivo"], options["carpeta"]
        if not os.path.isfile(archivo):
            raise CommandError(f"No se encontró {archivo}")
        if not os.path.isdir(carpeta):
            raise CommandError(f"No existe la carpeta {carpeta}")
        mes = mes_snapshot_desde_nombre(options["mes"] or archivo)
        if mes is None:
            raise CommandError("No se reconoce el mes de emisión: indícalo con --mes 'Octubre 2026'.")
        salida = os.path.join(carpeta, nombre_archivo(mes))
        if os.path.exists(salida) and not options["reemplazar"]:
            raise CommandError(f"Ya existe {salida} (usa --reemplazar si quieres generarla de nuevo).")
        try:
            proyectados = leer_legado(archivo, mes)
        except ValueError as error:
            raise CommandError(f"{os.path.basename(archivo)}: {error}")
        version = armar_version(mes, proyectados, origen=os.path.basename(archivo))
        escribir_plantilla(salida, version)
        con_precio = sum(any(v is not None for v in f["proyectados"].values()) for f in version["filas"])
        self.stdout.write(self.style.SUCCESS(
            f"{salida}: {len(version['filas'])} materiales ({con_precio} con precio), "
            f"{len(version['meses_proyectados'])} meses proyectados."))
