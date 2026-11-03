import glob
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from gestion.models import PlanConsumoSOP
from ._seguimiento_archivos import necesita_importar, registrar_importado
from ._sop_excel import leer_matriz_mensual, mes_snapshot_desde_nombre


class Command(BaseCommand):
    help = (
        "Importa el plan de consumos (S&OP) desde el Excel de 'TABLA SOP CONSUMOS' "
        "(una fila por material/centro, una columna por mes proyectado; mismo formato "
        "que el de Abastecimiento). El mes de emisión se toma del nombre del archivo. "
        "El consumo viene negativo en el Excel y se guarda en positivo. Por defecto se "
        "saltan los archivos que no cambiaron (usar --forzar para reprocesarlos)."
    )

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Archivo .xlsx o carpeta con los Excel de consumos SOP")
        parser.add_argument(
            "--forzar", action="store_true",
            help="Reprocesa todos los archivos aunque no hayan cambiado desde la última importación.",
        )

    # Todo el comando en una sola transacción (ver import_movimientos).
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

            total, positivos = self._importar_archivo(archivo, mes_snapshot)
            registrar_importado(archivo, total)
            total_general += total
            self.stdout.write(f"  {os.path.basename(archivo)} (consumo {mes_snapshot}): {total} filas")
            if positivos:
                # Un consumo positivo en el Excel (entrada en vez de salida) es
                # raro: se guarda igual, con el signo invertido, pero se avisa.
                self.stdout.write(self.style.WARNING(
                    f"    {positivos} celda(s) con consumo positivo en el Excel (se esperaba negativo)."
                ))

        self.stdout.write(self.style.SUCCESS(
            f"Consumos SOP: {total_general} filas procesadas en "
            f"{len(archivos) - saltados} archivo(s) ({saltados} sin cambios, saltados)."
        ))

    def _importar_archivo(self, archivo, mes_snapshot):
        # Cada archivo es una emisión completa del S&OP: reemplaza todo lo de su
        # mes de emisión (como el de abastecimiento), así un mes o un material
        # que el archivo corregido ya no trae no queda con el valor anterior.
        celdas = list(leer_matriz_mensual(archivo))
        positivos = sum(1 for c in celdas if c["valor"] is not None and c["valor"] > 0)
        if celdas:
            PlanConsumoSOP.objects.filter(mes_snapshot=mes_snapshot).delete()
        unicas = {(c["material_codigo"], c["centro"], c["mes"]): c for c in celdas}
        PlanConsumoSOP.objects.bulk_create([
            PlanConsumoSOP(material_codigo=m, centro_destino=centro, mes_snapshot=mes_snapshot, mes_planificado=mes,
                           material_descripcion=c["descripcion"], destino=c["destino"],
                           cantidad_consumo=-c["valor"] if c["valor"] is not None else None)
            for (m, centro, mes), c in unicas.items()
        ])
        return len(unicas), positivos
