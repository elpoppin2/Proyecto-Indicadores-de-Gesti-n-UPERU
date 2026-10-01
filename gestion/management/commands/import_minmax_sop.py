import glob
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from gestion.models import StockMinMaxSOP
from ._seguimiento_archivos import necesita_importar, registrar_importado
from ._sop_excel import leer_matriz_mensual, mes_snapshot_desde_nombre

HOJA_MINIMO = "Mínimo"
HOJA_MAXIMO = "Máximo"


class Command(BaseCommand):
    help = (
        "Importa el stock mínimo y máximo (S&OP) desde 'TABLA SOP MIN MAX' "
        "('MinMax SOP - <mes> <año>.xlsx', hojas 'Mínimo' y 'Máximo': una fila por "
        "material/centro, una columna por mes). Un archivo sin mes en el nombre (p.ej. "
        "la plantilla vacía) se salta. Por defecto se saltan los archivos sin cambios."
    )

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Archivo .xlsx o carpeta con los Excel de mínimos/máximos")
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

        total_general = 0
        procesados = 0
        for archivo in archivos:
            mes_snapshot = mes_snapshot_desde_nombre(archivo)
            if mes_snapshot is None:
                continue  # la plantilla (sin mes en el nombre) no se carga
            if not forzar and not necesita_importar(archivo):
                continue

            total = self._importar_archivo(archivo, mes_snapshot)
            registrar_importado(archivo, total)
            total_general += total
            procesados += 1
            self.stdout.write(f"  {os.path.basename(archivo)} (min/max {mes_snapshot}): {total} filas")

        self.stdout.write(self.style.SUCCESS(
            f"Stock mínimo/máximo SOP: {total_general} filas procesadas en {procesados} archivo(s)."
        ))

    def _importar_archivo(self, archivo, mes_snapshot):
        valores = {}
        for hoja, campo in ((HOJA_MINIMO, "stock_minimo"), (HOJA_MAXIMO, "stock_maximo")):
            try:
                celdas = list(leer_matriz_mensual(archivo, hoja))
            except Exception as error:  # noqa: BLE001 (openpyxl y calamine avisan distinto)
                raise CommandError(f"{os.path.basename(archivo)}: no tiene la hoja '{hoja}' ({error})")
            for celda in celdas:
                clave = (celda["material_codigo"], celda["centro"], celda["mes"])
                fila = valores.setdefault(clave, {"material_descripcion": celda["descripcion"]})
                fila[campo] = celda["valor"]

        # El archivo reemplaza por completo lo cargado para ese mes de emisión
        # (si se borra un material del Excel, no queda un valor viejo colgado).
        StockMinMaxSOP.objects.filter(mes_snapshot=mes_snapshot).delete()
        StockMinMaxSOP.objects.bulk_create([
            StockMinMaxSOP(
                material_codigo=material, centro_destino=centro, mes_snapshot=mes_snapshot,
                mes_planificado=mes, material_descripcion=datos["material_descripcion"],
                stock_minimo=datos.get("stock_minimo"), stock_maximo=datos.get("stock_maximo"),
            )
            for (material, centro, mes), datos in valores.items()
        ])
        return len(valores)
