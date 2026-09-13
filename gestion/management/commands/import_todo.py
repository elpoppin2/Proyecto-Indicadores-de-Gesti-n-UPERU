import glob
import os

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Orquesta la carga completa: busca CATEGORÍAS, TABLA_SQ01_ZMM060, TABLA_MB5B y "
        "TABLA_ME2N dentro de la carpeta base 'CSVs_Convertidos' y ejecuta los cuatro "
        "importadores en el orden correcto (categorías -> materiales -> inventario -> compras)."
    )

    def add_arguments(self, parser):
        parser.add_argument("base_dir", help="Ruta a la carpeta CSVs_Convertidos")

    def handle(self, *args, **options):
        base = options["base_dir"]

        categorias_csv = self._un_archivo(base, "CATEGORÍAS")
        materiales_csv = self._un_archivo(base, "TABLA_SQ01_ZMM060")
        inventario_dir = self._carpeta(base, "TABLA_MB5B")
        compras_dir = self._carpeta(base, "TABLA_ME2N")

        self.stdout.write("== Categorías ==")
        call_command("import_categorias", categorias_csv)

        self.stdout.write("== Materiales ==")
        call_command("import_materiales", materiales_csv)

        self.stdout.write("== Inventario mensual ==")
        call_command("import_inventario", inventario_dir)

        self.stdout.write("== Compras ==")
        call_command("import_compras", compras_dir)

        self.stdout.write(self.style.SUCCESS("Carga completa finalizada."))

    def _carpeta(self, base, nombre):
        ruta = os.path.join(base, nombre)
        if not os.path.isdir(ruta):
            raise CommandError(f"No se encontró la carpeta '{nombre}' dentro de {base}")
        return ruta

    def _un_archivo(self, base, nombre):
        ruta = self._carpeta(base, nombre)
        csvs = glob.glob(os.path.join(ruta, "*.csv"))
        if not csvs:
            raise CommandError(f"No se encontró ningún CSV dentro de {ruta}")
        if len(csvs) > 1:
            raise CommandError(
                f"Se esperaba un único CSV en {ruta}, se encontraron {len(csvs)}: {csvs}"
            )
        return csvs[0]
