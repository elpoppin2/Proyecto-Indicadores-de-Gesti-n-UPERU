import glob
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from gestion.models import PlanTrasladoSOP
from ._seguimiento_archivos import necesita_importar, registrar_importado
from ._sop_excel import leer_matriz_mensual, mes_snapshot_desde_nombre


class Command(BaseCommand):
    help = (
        "Importa los traslados entre plantas del S&OP desde 'TABLA SOP TRASLADOS' ('Traslados SOP - <mes> <año>.xlsx': "
        "Cod. SAP | Centro Origen | Material | Centro Destino | una columna por mes). El mes de emisión se toma del "
        "nombre del archivo y cada archivo reemplaza lo de su mes. Por defecto se saltan los archivos sin cambios."
    )

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Archivo .xlsx o carpeta con los Excel de traslados SOP")
        parser.add_argument("--forzar", action="store_true",
                            help="Reprocesa todos los archivos aunque no hayan cambiado desde la última importación.")

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
            celdas = [c for c in leer_matriz_mensual(archivo) if c["destino"]]
            PlanTrasladoSOP.objects.filter(mes_snapshot=mes_snapshot).delete()
            unicas = {(c["material_codigo"], c["centro"], c["destino"], c["mes"]): c for c in celdas}
            PlanTrasladoSOP.objects.bulk_create([
                PlanTrasladoSOP(material_codigo=m, material_descripcion=c["descripcion"][:255], centro_origen=origen,
                                centro_destino=destino, mes_snapshot=mes_snapshot, mes_planificado=mes,
                                cantidad=c["valor"])
                for (m, origen, destino, mes), c in unicas.items()
            ])
            registrar_importado(archivo, len(unicas))
            total_general += len(unicas)
            procesados += 1
            self.stdout.write(f"  {os.path.basename(archivo)} (traslados {mes_snapshot}): {len(unicas)} filas")
        self.stdout.write(self.style.SUCCESS(
            f"Traslados SOP: {total_general} filas procesadas en {procesados} archivo(s)."))
