import glob
import os

from django.db import transaction
from django.core.management.base import BaseCommand, CommandError

from gestion.models import PlanAbastecimientoSOP
from ._sop_excel import leer_matriz_mensual, mes_snapshot_desde_nombre
from ._seguimiento_archivos import necesita_importar, registrar_importado


class Command(BaseCommand):
    help = (
        "Importa el plan de abastecimiento (S&OP) desde el Excel de "
        "'TABLA SOP ABASTECIMIENTO' (una fila por material/centro, una columna por "
        "mes proyectado). El mes de emisión del plan se toma del nombre del archivo. "
        "Por defecto se saltan los archivos que no cambiaron desde la última "
        "importación (usar --forzar para reprocesarlos igual)."
    )

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Archivo .xlsx o carpeta con los Excel del plan SOP")
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
        forzar = options["forzar"]

        if os.path.isdir(ruta):
            archivos = sorted(glob.glob(os.path.join(ruta, "**", "*.xlsx"), recursive=True))
            archivos = [a for a in archivos if not os.path.basename(a).startswith("~$")]
        elif os.path.isfile(ruta):
            archivos = [ruta]
        else:
            raise CommandError(f"Ruta no encontrada: {ruta}")

        if not archivos:
            raise CommandError(f"No se encontraron archivos .xlsx en {ruta}")

        total_general = 0
        saltados = 0
        for archivo in archivos:
            if not forzar and not necesita_importar(archivo):
                saltados += 1
                continue

            mes_snapshot = mes_snapshot_desde_nombre(archivo)
            if mes_snapshot is None:
                self.stdout.write(self.style.WARNING(
                    f"  {os.path.basename(archivo)}: no se pudo determinar el mes de emisión "
                    "del nombre del archivo, se omite."
                ))
                continue

            total = self._importar_archivo(archivo, mes_snapshot)
            registrar_importado(archivo, total)
            total_general += total
            self.stdout.write(f"  {os.path.basename(archivo)} (plan {mes_snapshot}): {total} filas")

        self.stdout.write(self.style.SUCCESS(
            f"Plan de abastecimiento SOP: {total_general} filas procesadas en "
            f"{len(archivos) - saltados} archivo(s) ({saltados} sin cambios, saltados)."
        ))

    def _importar_archivo(self, archivo, mes_snapshot):
        # Cada archivo es una emisión completa del S&OP: reemplaza todo lo de
        # su mes de emisión (si el archivo corregido ya no trae un material o
        # un mes, no queda el valor anterior).
        celdas = list(leer_matriz_mensual(archivo))
        if celdas:
            PlanAbastecimientoSOP.objects.filter(mes_snapshot=mes_snapshot).delete()
        total = 0
        for celda in celdas:
            PlanAbastecimientoSOP.objects.update_or_create(
                material_codigo=celda["material_codigo"],
                centro_destino=celda["centro"],
                mes_snapshot=mes_snapshot,
                mes_planificado=celda["mes"],
                defaults={
                    "material_descripcion": celda["descripcion"],
                    "destino": celda["destino"],
                    "cantidad_plan": celda["valor"],
                },
            )
            total += 1
        return total
