import glob
import os
import re

from django.db import transaction
from django.core.management.base import BaseCommand, CommandError

from gestion.models import LineaCompra
from ._csv_utils import codigo, decimal, fecha, leer_filas, texto
from ._seguimiento_archivos import necesita_importar, registrar_importado

AÑO_RE = re.compile(r"(20\d{2})")


class Command(BaseCommand):
    help = (
        "Importa líneas de pedido/solicitud de compra desde uno o varios CSV de la "
        "transacción ME2N (acepta un archivo o un directorio con archivos por año)."
    )

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Archivo CSV/Excel o carpeta con los archivos de compras")
        parser.add_argument("--batch-size", type=int, default=5000)
        parser.add_argument("--anio", type=int, help="Forzar año de origen si el nombre del archivo no lo indica")
        parser.add_argument(
            "--reemplazar", action="store_true",
            help="Borra las líneas existentes de LineaCompra para el año antes de insertar "
                 "(necesario al reimportar el mismo año, ya que este comando no deduplica).",
        )
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
        anio_forzado = options.get("anio")
        reemplazar = options["reemplazar"]
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
        for archivo in archivos:
            match = AÑO_RE.search(os.path.basename(archivo))
            anio = anio_forzado or (int(match.group(1)) if match else None)
            if not anio:
                raise CommandError(f"No se pudo determinar el año de origen para {archivo}; usa --anio")

            # Un año (un archivo entero) se reemplaza solo si cambió desde la última
            # importación: este comando no deduplica por línea, así que reimportar un
            # archivo sin cambios significaría borrar y volver a insertar lo mismo.
            if not forzar and not necesita_importar(archivo):
                saltados += 1
                continue

            if reemplazar:
                borrados, _ = LineaCompra.objects.filter(anio_origen=anio).delete()
                if borrados:
                    self.stdout.write(f"  ({anio}) borradas {borrados} filas existentes antes de reimportar")

            total = self._importar_archivo(archivo, anio, batch_size)
            registrar_importado(archivo, total)
            total_general += total
            self.stdout.write(f"  {os.path.basename(archivo)} ({anio}): {total} filas")

        self.stdout.write(self.style.SUCCESS(
            f"Compras: {total_general} filas procesadas en {len(archivos) - saltados} archivo(s)"
            f" ({saltados} sin cambios, saltados)."
        ))

    def _importar_archivo(self, archivo, anio, batch_size):
        lote = []
        total = 0

        def volcar_lote():
            nonlocal lote
            if lote:
                LineaCompra.objects.bulk_create(lote, batch_size=batch_size)
                lote = []

        for row in leer_filas(archivo):
            lote.append(LineaCompra(
                autor=texto(row.get("Autor")),
                prioridad_orden=texto(row.get("Prioridad de la orden")),
                texto_prioridad=texto(row.get("Texto para prioridad")),
                condicion_pago=texto(row.get("CPag")),
                incoterm=texto(row.get("Incot")),
                incoterm_lugar1=texto(row.get("Inc.Lugar1")),
                incoterm_lugar2=texto(row.get("Inc.Lugar2")),
                tipo_cambio=decimal(row.get("Tp.cambio")),
                clase_valoracion=texto(row.get("Cl.valor.")),
                fecha_liberacion_pedido=fecha(row.get("Fecha liberación Pedido")),
                fecha_documento=fecha(row.get("Fecha doc.")),
                fecha_entrega=fecha(row.get("Fe.entrega")),
                fecha_entrega_estimada=fecha(row.get("FeEntEst")),
                doc_compra=codigo(row.get("Doc.compr.")),
                pos_doc_compra=codigo(row.get("Pos.")),
                clase_documento=texto(row.get("Cl.")),
                tipo=texto(row.get("T")),
                grupo_compras=texto(row.get("GCp")),
                proveedor=codigo(row.get("Proveedor")),
                cuenta_proveedor=codigo(row.get("Número de cuenta del proveedor")),
                material_codigo=codigo(row.get("Material")),
                texto_breve=texto(row.get("Texto breve")),
                grupo_articulo=texto(row.get("Grupo art.")),
                campo_b=texto(row.get("B")),
                campo_p=texto(row.get("P")),
                campo_i=texto(row.get("I")),
                centro=texto(row.get("Ce.")),
                almacen=texto(row.get("Alm.")),
                cantidad_pedido=decimal(row.get("Ctd.pedido")),
                unidad_medida_pedido=texto(row.get("UMP")),
                precio_neto=decimal(row.get("Prc.neto")),
                moneda=texto(row.get("Mon.")),
                campo_por=texto(row.get("por")),
                solicitud_pedido=codigo(row.get("Sol.pedido")),
                pos_solicitud=codigo(row.get("Pos..1")),
                solicitante=texto(row.get("Solicitante")),
                numero_necesidad=codigo(row.get("Nº nec.")),
                unidad_medida_solicitud=texto(row.get("UMP.1")),
                organizacion_compras=texto(row.get("OrgC")),
                numero_jerarquia=codigo(row.get("Número de jerarquía")),
                inicio_periodo_validez=fecha(row.get("IniPerVa")),
                fin_periodo_validez=fecha(row.get("FinPerVal")),
                contrato=codigo(row.get("Contr.")),
                pos_contrato=codigo(row.get("Pos..2")),
                valor_previsto_cabecera=decimal(row.get("Val.prev.(cab.)")),
                moneda_valor_previsto=texto(row.get("Mon..1")),
                cantidad_por_entregar=decimal(row.get("Por Entregar (Cantidad)")),
                unidad_medida_entrega=texto(row.get("UMP.2")),
                valor_neto=decimal(row.get("Valor neto")),
                valor_por_calcular=decimal(row.get("Por Calcular (Valor)")),
                valor_por_entregar=decimal(row.get("Por Entregar (Valor)")),
                en_contrato=texto(row.get("EnC")),
                status_tratamiento=texto(row.get("Status tratamiento doc.compras")),
                cantidad_entrada=decimal(row.get("Cantidad entrada")),
                anio_origen=anio,
            ))
            total += 1
            if len(lote) >= batch_size:
                volcar_lote()

        volcar_lote()
        return total
