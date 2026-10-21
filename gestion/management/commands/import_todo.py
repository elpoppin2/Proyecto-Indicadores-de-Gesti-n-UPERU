import glob
import os
import time
import traceback

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

        # Si una tabla falla, se sigue con las demás (cada importador es una
        # transacción: la que falla queda con sus datos anteriores, completos)
        # y al final se informa cuáles fallaron. Antes, un error en MB51
        # dejaba sin cargar también ZMM056 y FBL3N.
        self._fallidos = []
        inicio = time.monotonic()
        categorias_csv = self._paso("Categorías", lambda: self._un_archivo_opcional(base, "CATEGORÍAS"))
        if categorias_csv:
            self._paso("Categorías", "import_categorias", categorias_csv)
        elif categorias_csv is None and "Categorías" not in self._fallidos:
            self.stdout.write("== Categorías == (omitido, no se encontró la carpeta 'CATEGORÍAS')")

        materiales_xlsx = self._paso("Materiales", lambda: self._mas_reciente(base, "TABLA_SQ01_ZMM60"))
        if materiales_xlsx:
            self._paso("Materiales", "import_materiales", materiales_xlsx, forzar=forzar)

        for titulo, comando, carpeta, extra in [
            ("Inventario mensual", "import_inventario", "TABLA_MB5B", {}),
            ("Compras", "import_compras", "TABLA_SQ01_ME2N", {"reemplazar": True}),
            ("Movimientos (MB51)", "import_movimientos", "TABLA_MB51", {}),
        ]:
            ruta = os.path.join(base, carpeta)
            if not os.path.isdir(ruta):
                self.stderr.write(f"== {titulo} == ERROR: no se encontró la carpeta '{carpeta}' dentro de {base}")
                self._fallidos.append(titulo)
                continue
            self._paso(titulo, comando, ruta, forzar=forzar, **extra)

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

        duracion = self._duracion(time.monotonic() - inicio)
        if self._fallidos:
            # Código 2 = carga parcial: el resto quedó cargado y la tarea diaria
            # sigue con el HTML y la caché (ver actualizar_tablas_mm.ps1).
            raise CommandError(
                f"Carga finalizada en {duracion} con errores en: {', '.join(self._fallidos)}. "
                "Esas tablas conservan los datos de la carga anterior.", returncode=2)
        self.stdout.write(self.style.SUCCESS(f"Carga completa finalizada en {duracion}."))

    def _paso(self, titulo, comando, *args, **kwargs):
        """Corre un importador (o una función, si `comando` no es un nombre)
        mostrando cuánto tardó, para saber dónde se va el tiempo de la carga
        diaria (el log de la tarea programada). Si falla, anota el error y
        devuelve None para que la carga siga con las demás tablas."""
        silencioso = callable(comando)
        if not silencioso:
            self.stdout.write(f"== {titulo} ==")
            self.stdout.flush()
        t = time.monotonic()
        try:
            resultado = comando() if silencioso else call_command(comando, *args, **kwargs)
        except Exception as error:  # noqa: BLE001 — se informa y se sigue con la tabla siguiente
            self.stderr.write(f"== {titulo} == ERROR: {type(error).__name__}: {error}")
            if not isinstance(error, CommandError):  # un CommandError ya dice qué pasó
                self.stderr.write("".join(traceback.format_exception(error)[-4:]))
            self._fallidos.append(titulo)
            return None
        if not silencioso:
            self.stdout.write(f"   ({titulo}: {self._duracion(time.monotonic() - t)})")
            self.stdout.flush()
        return resultado if silencioso else True

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

    def _mas_reciente(self, base, nombre):
        """El maestro de materiales es una foto completa: desde oct-2026 el
        robot le pone el mes al nombre (MM60_SUPPLY_OCTUBRE_2026), así que en
        la carpeta copia conviven el nuevo y los anteriores (la copia no
        borra). Se usa el más reciente en vez de detener toda la carga."""
        ruta = self._carpeta(base, nombre)
        archivos = [a for a in glob.glob(os.path.join(ruta, "*.xlsx")) + glob.glob(os.path.join(ruta, "*.csv"))
                    if not os.path.basename(a).startswith("~$")]
        if not archivos:
            raise CommandError(f"No se encontró ningún CSV/Excel dentro de {ruta}")
        elegido = max(archivos, key=os.path.getmtime)
        if len(archivos) > 1:
            otros = ", ".join(os.path.basename(a) for a in archivos if a != elegido)
            self.stdout.write(f"   ({nombre}: {len(archivos)} archivos, se usa el más reciente "
                              f"{os.path.basename(elegido)}; se ignoran: {otros})")
        return elegido

    def _un_archivo_opcional(self, base, nombre):
        ruta = os.path.join(base, nombre)
        if not os.path.isdir(ruta):
            return None
        return self._un_archivo(base, nombre)

    def _carpeta_opcional(self, base, nombre):
        ruta = os.path.join(base, nombre)
        return ruta if os.path.isdir(ruta) else None
