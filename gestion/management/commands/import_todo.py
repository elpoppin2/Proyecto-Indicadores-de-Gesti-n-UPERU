import glob
import os
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Orquesta la carga completa desde la carpeta sincronizada de SAP (por ejemplo "
        "'DOCUMENTOS DESCARGA TABLAS MM'): materiales (TABLA_SQ01_ZMM60), inventario "
        "(TABLA_MB5B), compras (TABLA_SQ01_ME2N) y movimientos (TABLA_MB51). "
        "CATEGORÍAS se omite si no está presente, ya que no forma parte de la "
        "sincronización diaria y normalmente ya está cargada."
    )

    def add_arguments(self, parser):
        parser.add_argument("base_dir", help="Ruta a la carpeta con las tablas SAP")
        parser.add_argument(
            "--forzar", action="store_true",
            help="Reprocesa todos los archivos aunque no hayan cambiado desde la última "
                 "importación (por defecto solo se procesan los archivos nuevos/modificados).",
        )

    def handle(self, *args, **options):
        base = options["base_dir"]
        forzar = options["forzar"]
        if not os.path.isdir(base):
            raise CommandError(f"No se encontró la carpeta {base}")

        categorias_csv = self._un_archivo_opcional(base, "CATEGORÍAS")
        if categorias_csv:
            self.stdout.write("== Categorías ==")
            call_command("import_categorias", categorias_csv)
        else:
            self.stdout.write("== Categorías == (omitido, no se encontró la carpeta 'CATEGORÍAS')")

        inicio = time.monotonic()
        materiales_xlsx = self._un_archivo(base, "TABLA_SQ01_ZMM60")
        self._paso("Materiales", "import_materiales", materiales_xlsx, forzar=forzar)

        inventario_dir = self._carpeta(base, "TABLA_MB5B")
        self._paso("Inventario mensual", "import_inventario", inventario_dir, forzar=forzar)

        compras_dir = self._carpeta(base, "TABLA_SQ01_ME2N")
        self._paso("Compras", "import_compras", compras_dir, reemplazar=True, forzar=forzar)

        movimientos_dir = self._carpeta(base, "TABLA_MB51")
        self._paso("Movimientos (MB51)", "import_movimientos", movimientos_dir, forzar=forzar)

        plan_sop_dir = self._carpeta_opcional(base, "TABLA SOP ABASTECIMIENTO")
        if plan_sop_dir:
            self._paso("Plan de abastecimiento (S&OP)", "import_plan_abastecimiento_sop", plan_sop_dir, forzar=forzar)
        else:
            self.stdout.write("== Plan de abastecimiento (S&OP) == (omitido, no se encontró la carpeta)")

        consumo_sop_dir = self._carpeta_opcional(base, "TABLA SOP CONSUMOS")
        if consumo_sop_dir:
            self._paso("Plan de consumos (S&OP)", "import_consumo_sop", consumo_sop_dir, forzar=forzar)
        else:
            self.stdout.write("== Plan de consumos (S&OP) == (omitido, no se encontró la carpeta)")

        minmax_sop_dir = self._carpeta_opcional(base, "TABLA SOP MIN MAX")
        if minmax_sop_dir:
            self._paso("Stock mínimo/máximo (S&OP)", "import_minmax_sop", minmax_sop_dir, forzar=forzar)
        else:
            self.stdout.write("== Stock mínimo/máximo (S&OP) == (omitido, no se encontró la carpeta)")

        ingresos_sop_dir = self._carpeta_opcional(base, "TABLA_ZMM056")
        if ingresos_sop_dir:
            self._paso("Ingresos físicos (S&OP, ZMM056)", "import_ingresos_fisicos_sop", ingresos_sop_dir, forzar=forzar)
        else:
            self.stdout.write("== Ingresos físicos (S&OP, ZMM056) == (omitido, no se encontró la carpeta)")

        fbl3n_dir = self._carpeta_opcional(base, "TABLA_FBL3N")
        if fbl3n_dir:
            self._paso("Ajustes de valor de existencias (FBL3N)", "import_fbl3n", fbl3n_dir, forzar=forzar)
        else:
            self.stdout.write("== Ajustes de valor de existencias (FBL3N) == (omitido, no se encontró la carpeta)")

        self.stdout.write(self.style.SUCCESS(
            f"Carga completa finalizada en {self._duracion(time.monotonic() - inicio)}."
        ))

    def _paso(self, titulo, comando, *args, **kwargs):
        """Corre un importador mostrando cuánto tardó, para saber dónde se va
        el tiempo de la carga diaria (el log de la tarea programada)."""
        self.stdout.write(f"== {titulo} ==")
        self.stdout.flush()
        t = time.monotonic()
        call_command(comando, *args, **kwargs)
        self.stdout.write(f"   ({titulo}: {self._duracion(time.monotonic() - t)})")
        self.stdout.flush()

    @staticmethod
    def _duracion(segundos):
        minutos, seg = divmod(int(round(segundos)), 60)
        return f"{minutos} min {seg:02d} s" if minutos else f"{seg} s"

    def _carpeta(self, base, nombre):
        ruta = os.path.join(base, nombre)
        if not os.path.isdir(ruta):
            raise CommandError(f"No se encontró la carpeta '{nombre}' dentro de {base}")
        return ruta

    def _un_archivo(self, base, nombre):
        ruta = self._carpeta(base, nombre)
        archivos = glob.glob(os.path.join(ruta, "*.csv")) + glob.glob(os.path.join(ruta, "*.xlsx"))
        if not archivos:
            raise CommandError(f"No se encontró ningún CSV/Excel dentro de {ruta}")
        if len(archivos) > 1:
            raise CommandError(
                f"Se esperaba un único archivo en {ruta}, se encontraron {len(archivos)}: {archivos}"
            )
        return archivos[0]

    def _un_archivo_opcional(self, base, nombre):
        ruta = os.path.join(base, nombre)
        if not os.path.isdir(ruta):
            return None
        return self._un_archivo(base, nombre)

    def _carpeta_opcional(self, base, nombre):
        ruta = os.path.join(base, nombre)
        return ruta if os.path.isdir(ruta) else None
