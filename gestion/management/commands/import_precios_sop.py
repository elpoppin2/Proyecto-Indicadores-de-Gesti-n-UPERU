import glob
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from gestion.models import PrecioSOP
from gestion.precios_sop import HOJA
from ._precios_sop_excel import hojas_del_archivo, leer_plantilla
from ._seguimiento_archivos import necesita_importar, registrar_importado
from ._sop_excel import mes_snapshot_desde_nombre


class Command(BaseCommand):
    help = (
        "Importa las versiones de precios S&OP desde 'TABLA SOP PRECIOS' ('Precios SOP - <mes> <año>.xlsx', "
        "hoja 'Precios': una fila por material/centro y una columna por mes). Solo se cargan los meses desde la "
        "emisión (los anteriores son el precio real, de referencia). Un archivo sin mes en el nombre se salta. "
        "Por defecto se saltan los archivos sin cambios."
    )

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Archivo .xlsx o carpeta con las versiones de precios")
        parser.add_argument(
            "--forzar", action="store_true",
            help="Reprocesa todos los archivos aunque no hayan cambiado desde la última importación.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        ruta = options["ruta"]
        if os.path.isdir(ruta):
            archivos = sorted(glob.glob(os.path.join(ruta, "**", "*.xlsx"), recursive=True))
            archivos = [a for a in archivos if not os.path.basename(a).startswith("~$")]
        elif os.path.isfile(ruta):
            archivos = [ruta]
        else:
            raise CommandError(f"Ruta no encontrada: {ruta}")

        total_general = procesados = 0
        for archivo in archivos:
            mes_snapshot = mes_snapshot_desde_nombre(archivo)
            if mes_snapshot is None:
                continue
            if not options["forzar"] and not necesita_importar(archivo):
                continue
            total = self._importar_archivo(archivo, mes_snapshot)
            registrar_importado(archivo, total)
            total_general += total
            procesados += 1
            self.stdout.write(f"  {os.path.basename(archivo)} (precios S&OP {mes_snapshot}): {total} filas")
        self.stdout.write(self.style.SUCCESS(
            f"Precios S&OP: {total_general} filas procesadas en {procesados} archivo(s)."))

    def _importar_archivo(self, archivo, mes_snapshot):
        nombre = os.path.basename(archivo)
        hojas = hojas_del_archivo(archivo)
        if HOJA not in hojas:
            if any(h.startswith("Tar. ") for h in hojas):
                raise CommandError(f"{nombre}: es el Excel anterior; conviértelo primero con "
                                   "'manage.py convertir_precios_sop'.")
            raise CommandError(f"{nombre}: no tiene la hoja '{HOJA}'.")
        try:
            celdas = [c for c in leer_plantilla(archivo) if c["mes"] >= mes_snapshot]
        except ValueError as error:
            raise CommandError(f"{nombre}: {error}")
        # El archivo reemplaza por completo lo cargado para ese mes de emisión.
        PrecioSOP.objects.filter(mes_snapshot=mes_snapshot).delete()
        unicas = {(c["material_codigo"], c["centro"], c["mes"]): c for c in celdas}
        PrecioSOP.objects.bulk_create([
            PrecioSOP(material_codigo=m, centro=centro, mes=mes, mes_snapshot=mes_snapshot, precio=c["precio"],
                      material_descripcion=c["descripcion"][:255], unidad=c["unidad"][:20], supuesto=c["supuesto"])
            for (m, centro, mes), c in unicas.items()
        ])
        return len(unicas)
