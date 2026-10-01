import glob
import os

from django.db import transaction
from django.core.management.base import BaseCommand, CommandError

from gestion.models import IngresoFisicoSOP
from ._excel_utils import decimal, fecha, leer_filas_excel, texto
from ._seguimiento_archivos import fuera_de_ventana, necesita_importar, registrar_importado

# El Pesaje Neto (báscula) viene en kilogramos aunque la Unidad de medida base
# del material sea toneladas: verificado contra "Cantidad de Pedido" (que sí
# viene ya en toneladas) — coincide exactamente con Pesaje Neto Húmedo / 1000
# en todos los tickets revisados. Se convierte acá para que `cantidad` quede
# en la misma unidad que PlanAbastecimientoSOP.cantidad_plan (toneladas).
_KG_A_TONELADAS = 1000

# Posiciones (0-based) dentro de las filas del Excel de ZMM056. El encabezado
# trae columnas repetidas (p.ej. "Documento material" cinco veces, "Nombre 1"
# dos veces), así que se lee por posición y no por nombre. Verificado contra
# los archivos reales de Atocongo y Condorcocha (mismo layout, 107 columnas).
_IDX_CENTRO_SALIDA = 0
_IDX_TICKET_REGISTRO = 1
_IDX_ESTADO_TICKET = 2
_IDX_FECHA_INGRESO = 3
_IDX_PROVEEDOR_CODIGO = 67
_IDX_PROVEEDOR_NOMBRE = 86
_IDX_MATERIAL = 78
_IDX_DESCRIPCION_MATERIAL = 79
_IDX_PESAJE_NETO_HUMEDO = 82
_IDX_UNIDAD_MEDIDA_BASE = 87
_IDX_DOCUMENTO_MATERIAL = 103
_IDX_POSICION = 104
_IDX_TIPO_OPERACION = 105
_IDX_NUMERO_MUESTRA = 59
_IDX_PREMIO = 66          # "Premio o Castigo", S/ por ticket (la de % viene siempre en 0)
_IDX_PESAJE_NETO_SECO = 71  # kg, igual que el húmedo

# Calidad: columnas "Factor ..." del análisis químico (posición -> campo del
# modelo). 0.0 en el Excel = no analizado, se guarda como None.
_IDX_CALIDAD = {
    12: "humedad", 13: "cao", 14: "sio2", 15: "so3", 16: "mgo", 17: "fe2o3", 18: "al2o3",
    19: "insolubles", 20: "cenizas", 21: "volatiles", 22: "azufre", 23: "pcs", 24: "hgi",
    25: "yeso", 26: "humedad_total", 27: "azufre_elt", 28: "sialfe",
}

# Al recargar un archivo (p.ej. el del mes en curso, cada día), un ticket que
# ya estaba se ACTUALIZA con lo que trae el Excel: el análisis de laboratorio
# o el Documento material pueden llegar días después del pesaje. Antes se
# ignoraba el ticket repetido y se quedaba con la primera versión.
_CAMPOS_ACTUALIZABLES = [
    "planta_origen", "material_codigo", "descripcion_material", "fecha_ingreso", "cantidad",
    "unidad_medida_base", "proveedor_codigo", "proveedor", "documento_material", "posicion",
    "tipo_operacion", "numero_muestra", *_IDX_CALIDAD.values(), "premio", "cantidad_seca",
]

# Se excluyen solo los tickets anulados: un ticket 'REG'/'NAC'/'REV'/'ATN' ya
# se pesó en báscula (siempre trae Pesaje Neto), aunque su Documento material
# en SAP pueda estar pendiente — solo 'ANU' significa que el ticket no cuenta.
# Definido por el usuario.
_ESTADO_TICKET_ANULADO = "ANU"


def _analisis(valor):
    """Valor de un análisis químico: float, o None si no se analizó (0 o vacío)."""
    numero = decimal(valor)
    return float(numero) if numero else None


class Command(BaseCommand):
    help = (
        "Importa los ingresos físicos de Materias Primas y Energía/Combustibles "
        "(tickets de báscula, ZMM056) desde 'TABLA_ZMM056' (recorre las subcarpetas "
        "por planta, p.ej. ATOCONGO/CONDORCOCHA). Se importan todos los tickets con "
        "Estado Ticket distinto de 'ANU' (anulado). Por defecto se saltan los archivos "
        "que no cambiaron desde la última importación (usar --forzar para reprocesarlos)."
    )

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Archivo .xlsx o carpeta con los Excel de ZMM056")
        parser.add_argument("--batch-size", type=int, default=2000)
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

            planta_origen = self._planta_desde_ruta(archivo)
            total = self._importar_archivo(archivo, planta_origen, batch_size)
            registrar_importado(archivo, total)
            total_general += total
            self.stdout.write(f"  {os.path.basename(archivo)} ({planta_origen}): {total} filas")

        self.stdout.write(self.style.SUCCESS(
            f"Ingresos físicos SOP: {total_general} filas procesadas en "
            f"{len(archivos) - saltados - cerrados} archivo(s) ({saltados} sin cambios, "
            f"{cerrados} de meses cerrados, saltados)."
        ))

    def _planta_desde_ruta(self, archivo):
        for parte in os.path.normpath(archivo).split(os.sep):
            if "ATOCONGO" in parte.upper():
                return "ATOCONGO"
            if "CONDORCOCHA" in parte.upper():
                return "CONDORCOCHA"
        return ""

    def _importar_archivo(self, archivo, planta_origen, batch_size):
        lote = []
        total = 0
        vistos = set()

        def volcar_lote():
            nonlocal lote
            if lote:
                IngresoFisicoSOP.objects.bulk_create(
                    lote, batch_size=batch_size, update_conflicts=True,
                    unique_fields=["centro", "ticket_registro"], update_fields=_CAMPOS_ACTUALIZABLES,
                )
                lote = []

        for fila in leer_filas_excel(archivo):
            if texto(fila[_IDX_ESTADO_TICKET]) == _ESTADO_TICKET_ANULADO:
                continue

            ticket_registro = texto(fila[_IDX_TICKET_REGISTRO])
            if not ticket_registro:
                continue
            # Un mismo ticket puede venir en más de una fila (columna "Sec."):
            # se queda la primera, igual que antes.
            clave = (texto(fila[_IDX_CENTRO_SALIDA]), ticket_registro)
            if clave in vistos:
                continue
            vistos.add(clave)

            peso_neto_humedo = decimal(fila[_IDX_PESAJE_NETO_HUMEDO])
            cantidad_toneladas = peso_neto_humedo / _KG_A_TONELADAS if peso_neto_humedo is not None else None

            lote.append(IngresoFisicoSOP(
                planta_origen=planta_origen,
                ticket_registro=ticket_registro,
                centro=texto(fila[_IDX_CENTRO_SALIDA]),
                material_codigo=texto(fila[_IDX_MATERIAL]),
                descripcion_material=texto(fila[_IDX_DESCRIPCION_MATERIAL]),
                fecha_ingreso=fecha(fila[_IDX_FECHA_INGRESO]),
                cantidad=cantidad_toneladas,
                unidad_medida_base=texto(fila[_IDX_UNIDAD_MEDIDA_BASE]),
                proveedor_codigo=texto(fila[_IDX_PROVEEDOR_CODIGO]),
                proveedor=texto(fila[_IDX_PROVEEDOR_NOMBRE]),
                documento_material=texto(fila[_IDX_DOCUMENTO_MATERIAL]),
                posicion=texto(fila[_IDX_POSICION]),
                tipo_operacion=texto(fila[_IDX_TIPO_OPERACION]),
                numero_muestra=texto(fila[_IDX_NUMERO_MUESTRA]),
                **{campo: _analisis(fila[indice]) for indice, campo in _IDX_CALIDAD.items()},
                premio=float(premio) if (premio := decimal(fila[_IDX_PREMIO])) else None,
                cantidad_seca=float(seco) / _KG_A_TONELADAS if (seco := decimal(fila[_IDX_PESAJE_NETO_SECO])) else None,
            ))
            total += 1
            if len(lote) >= batch_size:
                volcar_lote()

        volcar_lote()
        return total
