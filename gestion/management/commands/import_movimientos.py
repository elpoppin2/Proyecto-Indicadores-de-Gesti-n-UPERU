import glob
import os

from django.db import transaction
from django.core.management.base import BaseCommand, CommandError

from gestion.models import MovimientoMaterial
from ._excel_utils import decimal, fecha, hora, leer_filas_excel, texto
from ._seguimiento_archivos import fuera_de_ventana, necesita_importar, registrar_importado

# Orden exacto de columnas del Excel de MB51 (72 columnas). El encabezado trae
# "Pos.pedido cliente" duplicado, por eso se lee por posición y no por nombre.
COLUMNAS = [
    "material_codigo", "sociedad", "centro", "nombre_centro", "proveedor", "pedido",
    "posicion", "descripcion_material", "documento_material", "fecha_contabilizacion",
    "cantidad", "cantidad_um_entrada", "unidad_medida_entrada", "clase_transaccion",
    "clase_movimiento", "usuario", "destino_mercancia", "importe_moneda_local", "moneda",
    "referencia", "ejercicio_documento_material", "fecha_documento", "fecha_entrada",
    "texto_clase_movimiento", "cuenta_mayor", "indicador_debe_haber", "orden",
    "centro_coste", "almacen", "motivo_movimiento", "cliente", "pedido_de_cliente",
    "stock_especial", "posicion_documento_material", "activo_fijo", "subnumero",
    "contador", "hoja_ruta_operacion", "texto_cabecera_documento", "cantidad_umpp",
    "um_precio_pedido", "unidad_medida_pedido", "cantidad_um_pedido", "clase_valoracion",
    "lote", "hora_entrada", "documento_configurable", "documento_almacen",
    "numero_almacen", "importe_ext_moneda_local", "valor_venta", "pedido_cliente",
    "reparto_pedido_cliente", "pos_pedido_cliente_1", "indicador_movimiento", "consumo",
    "indicador_entrada", "pos_pedido_cliente_2", "unidad_medida_base", "grafo",
    "operacion", "elemento_pep", "reserva", "pos_reserva_traslado", "segmento_stock",
    "valor_pv_con_iva", "vale_acompanamiento_mercancias", "posicion_creada_automaticamente",
    "linea_original", "imputacion_multiple", "contrato_marco", "pos_contrato_sup",
]

CAMPOS_FECHA = {"fecha_contabilizacion", "fecha_documento", "fecha_entrada"}
CAMPOS_HORA = {"hora_entrada"}
CAMPOS_DECIMAL = {
    "cantidad", "cantidad_um_entrada", "importe_moneda_local", "cantidad_umpp",
    "cantidad_um_pedido", "importe_ext_moneda_local", "valor_venta", "valor_pv_con_iva",
}


class Command(BaseCommand):
    help = (
        "Importa movimientos de materiales/servicios desde los Excel de la transacción "
        "MB51 (acepta un archivo .xlsx, o una carpeta que se recorre recursivamente). "
        "Por defecto se saltan los archivos que no cambiaron desde la última importación "
        "(usar --forzar para reprocesarlos igual)."
    )

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Archivo .xlsx o carpeta con los Excel de MB51")
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
        cerrados = 0
        for archivo in archivos:
            if not forzar and fuera_de_ventana(archivo):
                cerrados += 1
                continue
            if not forzar and not necesita_importar(archivo):
                saltados += 1
                continue
            origen = (
                MovimientoMaterial.ORIGEN_SERVICIOS
                if "SERVICIOS" in archivo.upper()
                else MovimientoMaterial.ORIGEN_MATERIALES
            )
            total = self._importar_archivo(archivo, origen, batch_size)
            registrar_importado(archivo, total)
            total_general += total
            self.stdout.write(f"  {os.path.basename(archivo)} ({origen}): {total} filas")

        self.stdout.write(self.style.SUCCESS(
            f"Movimientos: {total_general} filas procesadas en {len(archivos) - saltados - cerrados} archivo(s)"
            f" ({saltados} sin cambios, {cerrados} de meses cerrados, saltados)."
        ))

    def _importar_archivo(self, archivo, origen, batch_size):
        lote = []
        total = 0

        def volcar_lote():
            nonlocal lote
            if lote:
                MovimientoMaterial.objects.bulk_create(lote, batch_size=batch_size, ignore_conflicts=True)
                lote = []

        for fila in leer_filas_excel(archivo):
            valores = {"origen_datos": origen}
            for nombre_campo, valor in zip(COLUMNAS, fila):
                if nombre_campo in CAMPOS_FECHA:
                    valores[nombre_campo] = fecha(valor)
                elif nombre_campo in CAMPOS_HORA:
                    valores[nombre_campo] = hora(valor)
                elif nombre_campo in CAMPOS_DECIMAL:
                    valores[nombre_campo] = decimal(valor)
                else:
                    valores[nombre_campo] = texto(valor)

            if not valores.get("documento_material"):
                continue

            lote.append(MovimientoMaterial(**valores))
            total += 1
            if len(lote) >= batch_size:
                volcar_lote()

        volcar_lote()
        return total
