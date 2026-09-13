import glob
import os

from django.core.management.base import BaseCommand, CommandError

from gestion.models import InventarioMensual
from ._csv_utils import codigo, decimal, fecha, leer_filas, texto


class Command(BaseCommand):
    help = (
        "Importa inventario mensual desde uno o varios CSV de la transacción MB5B "
        "(acepta un archivo o un directorio con varios archivos mensuales)."
    )

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Archivo CSV o carpeta con los CSV de inventario mensual")
        parser.add_argument("--batch-size", type=int, default=5000)

    def handle(self, *args, **options):
        ruta = options["ruta"]
        batch_size = options["batch_size"]

        if os.path.isdir(ruta):
            archivos = sorted(glob.glob(os.path.join(ruta, "*.csv")))
        elif os.path.isfile(ruta):
            archivos = [ruta]
        else:
            raise CommandError(f"Ruta no encontrada: {ruta}")

        if not archivos:
            raise CommandError(f"No se encontraron archivos CSV en {ruta}")

        total_general = 0
        for archivo in archivos:
            total = self._importar_archivo(archivo, batch_size)
            total_general += total
            self.stdout.write(f"  {os.path.basename(archivo)}: {total} filas")

        self.stdout.write(self.style.SUCCESS(
            f"Inventario: {total_general} filas procesadas en {len(archivos)} archivo(s)."
        ))

    def _importar_archivo(self, archivo, batch_size):
        lote = []
        total = 0

        def volcar_lote():
            nonlocal lote
            if lote:
                InventarioMensual.objects.bulk_create(lote, batch_size=batch_size, ignore_conflicts=True)
                lote = []

        for row in leer_filas(archivo):
            mat = codigo(row.get("Material"))
            fch = fecha(row.get("A fecha"))
            if not mat or not fch:
                continue

            lote.append(InventarioMensual(
                material_codigo=mat,
                texto_breve=texto(row.get("Texto breve de material")),
                stock_cierre=decimal(row.get("Stock de cierre")) or 0,
                unidad_medida=texto(row.get("Unidad medida base")),
                valor_final=decimal(row.get("Valor final")) or 0,
                denominacion_tipo_material=texto(row.get("Denominación tipo de mat.")),
                denominacion_grupo_articulos=texto(row.get("Denom.gr-artículos")),
                fecha=fch,
                area_valoracion=texto(row.get("Área de valoración")),
            ))
            total += 1
            if len(lote) >= batch_size:
                volcar_lote()

        volcar_lote()
        return total
