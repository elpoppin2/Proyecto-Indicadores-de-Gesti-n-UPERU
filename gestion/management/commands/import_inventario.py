import glob
import os

from django.db import transaction
from django.core.management.base import BaseCommand, CommandError

from gestion.models import InventarioMensual
from ._csv_utils import codigo, decimal, fecha, leer_filas, texto
from ._seguimiento_archivos import fuera_de_ventana, necesita_importar, registrar_importado


class Command(BaseCommand):
    help = (
        "Importa inventario mensual desde uno o varios CSV de la transacción MB5B "
        "(acepta un archivo o un directorio con varios archivos mensuales). Por "
        "defecto se saltan los archivos que no cambiaron desde la última importación "
        "(usar --forzar para reprocesarlos igual)."
    )

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Archivo CSV o carpeta con los CSV de inventario mensual")
        parser.add_argument("--batch-size", type=int, default=5000)
        parser.add_argument(
            "--forzar", action="store_true",
            help="Reprocesa todos los archivos aunque no hayan cambiado desde la última importación.",
        )

    # Todo el comando en una sola transacción: mucho más rápido en SQLite que
    # confirmar lote por lote, y si algo falla a medias no quedan datos
    # parciales (p.ej. Compras sin el año que se estaba reemplazando).
    @transaction.atomic
    def handle(self, *args, **options):
        ruta = options["ruta"]
        batch_size = options["batch_size"]
        forzar = options["forzar"]

        if os.path.isdir(ruta):
            archivos = sorted(
                glob.glob(os.path.join(ruta, "*.csv")) + glob.glob(os.path.join(ruta, "*.xlsx"))
            )
        elif os.path.isfile(ruta):
            archivos = [ruta]
        else:
            raise CommandError(f"Ruta no encontrada: {ruta}")

        if not archivos:
            raise CommandError(f"No se encontraron archivos CSV/Excel en {ruta}")

        total_general = 0
        saltados = 0
        cerrados = 0
        for archivo in archivos:
            if not forzar and fuera_de_ventana(archivo):
                cerrados += 1
                continue
            if not forzar and not necesita_importar(archivo):
                saltados += 1
                continue
            total = self._importar_archivo(archivo, batch_size)
            registrar_importado(archivo, total)
            total_general += total
            self.stdout.write(f"  {os.path.basename(archivo)}: {total} filas")

        self.stdout.write(self.style.SUCCESS(
            f"Inventario: {total_general} filas procesadas en {len(archivos) - saltados - cerrados} archivo(s)"
            f" ({saltados} sin cambios, {cerrados} de meses cerrados, saltados)."
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
