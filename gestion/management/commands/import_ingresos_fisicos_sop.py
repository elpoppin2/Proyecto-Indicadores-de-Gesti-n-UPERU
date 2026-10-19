import datetime
import glob
import os

from django.db import transaction
from django.core.management.base import BaseCommand, CommandError

from gestion.models import IngresoFisicoSOP
from ._excel_utils import _filas_xlsx, decimal, fecha, texto
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

# Desde el 14-oct-2026 el robot de TIC baja la ZMM056 con otro layout (98
# columnas, otro orden, empieza por "Ticket Registro"). Ese formato se lee por
# NOMBRE de columna, tomando la PRIMERA aparición de cada nombre repetido:
# verificado contra los tickets ya cargados que la primera "Documento
# material" es la de la entrada (coincidió en 1,310 de 1,310), la primera
# "Nombre 1" es el proveedor del material y la primera "Número Muestra" la
# muestra. El layout antiguo (107 columnas, empieza por "Centro Salida") se
# sigue leyendo por posición, como antes.
_NOMBRES = {
    "centro": "Centro Salida", "ticket": "Ticket Registro", "estado": "Estado Ticket",
    "fecha": "Fecha de Ingreso", "proveedor_codigo": "Proveedor Material", "proveedor": "Nombre 1",
    "material": "Material", "descripcion": "Descripción material", "humedo": "Pesaje Neto Humedo",
    "umb": "Unidad medida base", "documento": "Documento material", "posicion": "Posición",
    "tipo": "Tipo de operacion", "muestra": "Número Muestra", "premio": "Premio o Castigo",
    "seco": "Pesaje Neto Seco",
    # Transporte (indicadores de Logística, 2026-10-19).
    "placa": "Placa Camion", "transportista_codigo": "Proveedor transportista",
    "f_lleno": "Fecha pesaje lleno", "h_lleno": "Hora pesaje lleno",
    "f_vacio": "Fecha pesaje vacio", "h_vacio": "Hora pesaje vacio",
}
# Nombres repetidos que se leen en su SEGUNDA aparición: el segundo "Nombre 1"
# es el del transportista (el primero, el del proveedor del material).
_NOMBRES_SEGUNDA = {"transportista": "Nombre 1"}
_POSICIONES_ANTIGUAS = {
    "centro": _IDX_CENTRO_SALIDA, "ticket": _IDX_TICKET_REGISTRO, "estado": _IDX_ESTADO_TICKET,
    "fecha": _IDX_FECHA_INGRESO, "proveedor_codigo": _IDX_PROVEEDOR_CODIGO, "proveedor": _IDX_PROVEEDOR_NOMBRE,
    "material": _IDX_MATERIAL, "descripcion": _IDX_DESCRIPCION_MATERIAL, "humedo": _IDX_PESAJE_NETO_HUMEDO,
    "umb": _IDX_UNIDAD_MEDIDA_BASE, "documento": _IDX_DOCUMENTO_MATERIAL, "posicion": _IDX_POSICION,
    "tipo": _IDX_TIPO_OPERACION, "muestra": _IDX_NUMERO_MUESTRA, "premio": _IDX_PREMIO, "seco": _IDX_PESAJE_NETO_SECO,
    "placa": 76, "transportista_codigo": 96, "transportista": 97,
    "f_lleno": 37, "h_lleno": 93, "f_vacio": 39, "h_vacio": 94,
}


def _indices(encabezado):
    """(posición de cada campo, posición -> campo de calidad) según el layout
    del archivo (ver _NOMBRES)."""
    encabezado = ["" if e is None else str(e).strip() for e in encabezado]
    if encabezado and encabezado[0] == "Centro Salida":
        return _POSICIONES_ANTIGUAS, _IDX_CALIDAD
    primera, segunda = {}, {}
    for i, nombre in enumerate(encabezado):
        if nombre in primera:
            segunda.setdefault(nombre, i)
        primera.setdefault(nombre, i)
    faltan = [n for n in [*_NOMBRES.values(), *_NOMBRES_CALIDAD.values()] if n not in primera]
    faltan += [f"{n} (segunda)" for n in _NOMBRES_SEGUNDA.values() if n not in segunda]
    if faltan:
        raise CommandError("La ZMM056 trae un formato desconocido; faltan las columnas: " + ", ".join(faltan))
    return ({**{campo: primera[nombre] for campo, nombre in _NOMBRES.items()},
             **{campo: segunda[nombre] for campo, nombre in _NOMBRES_SEGUNDA.items()}},
            {primera[nombre]: campo for campo, nombre in _NOMBRES_CALIDAD.items()})


_NOMBRES_CALIDAD = {
    "humedad": "Factor humedad %", "cao": "Factor CaO", "sio2": "Factor Si02", "so3": "Factor S03",
    "mgo": "Factor Mg0", "fe2o3": "Factor Fe2O3", "al2o3": "Factor Al203", "insolubles": "Factor Insolu",
    "cenizas": "Factor Cenizas %", "volatiles": "Factor Volatiles %", "azufre": "Factor Azufre %",
    "pcs": "Factor  PCS", "hgi": "Factor  HGI", "yeso": "Gypsum", "humedad_total": "Factor humedad Total",
    "azufre_elt": "Factor S(Elt)", "sialfe": "Factor SiAlFe",
}

# Al recargar un archivo (p.ej. el del mes en curso, cada día), un ticket que
# ya estaba se ACTUALIZA con lo que trae el Excel: el análisis de laboratorio
# o el Documento material pueden llegar días después del pesaje. Antes se
# ignoraba el ticket repetido y se quedaba con la primera versión.
_CAMPOS_ACTUALIZABLES = [
    "planta_origen", "material_codigo", "descripcion_material", "fecha_ingreso", "cantidad",
    "unidad_medida_base", "proveedor_codigo", "proveedor", "documento_material", "posicion",
    "tipo_operacion", "numero_muestra", *_IDX_CALIDAD.values(), "premio", "cantidad_seca",
    "transportista_codigo", "transportista", "placa",
    "fecha_pesaje_lleno", "hora_pesaje_lleno", "fecha_pesaje_vacio", "hora_pesaje_vacio",
]

# Se excluyen solo los tickets anulados: un ticket 'REG'/'NAC'/'REV'/'ATN' ya
# se pesó en báscula (siempre trae Pesaje Neto), aunque su Documento material
# en SAP pueda estar pendiente — solo 'ANU' significa que el ticket no cuenta.
# Definido por el usuario.
_ESTADO_TICKET_ANULADO = "ANU"


def _fecha(valor):
    """Fecha de una celda: date/datetime o texto 'AAAA-MM-DD'."""
    if isinstance(valor, str):
        try:
            return datetime.date.fromisoformat(valor.strip()[:10])
        except ValueError:
            return None
    return fecha(valor)


def _hora(valor):
    """Hora de una celda: time/datetime, texto 'HH:MM:SS' o fracción de día
    de Excel. 00:00:00 = sin pesaje registrado -> None."""
    if isinstance(valor, datetime.datetime):
        valor = valor.time()
    elif isinstance(valor, str):
        try:
            valor = datetime.time.fromisoformat(valor.strip()[:8])
        except ValueError:
            return None
    elif isinstance(valor, (int, float)) and 0 <= valor < 1:
        segundos = round(valor * 86400)
        valor = datetime.time(segundos // 3600 % 24, segundos // 60 % 60, segundos % 60)
    if not isinstance(valor, datetime.time) or valor == datetime.time(0, 0):
        return None
    return valor.replace(microsecond=0)


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

        filas = _filas_xlsx(archivo)
        encabezado = next(filas, None)
        if not encabezado:
            return 0
        ix, ix_calidad = _indices(encabezado)
        for fila in filas:
            if not fila or not any(v is not None and v != "" for v in fila):
                continue
            if texto(fila[ix["estado"]]) == _ESTADO_TICKET_ANULADO:
                continue

            ticket_registro = texto(fila[ix["ticket"]])
            if not ticket_registro:
                continue
            # Un mismo ticket puede venir en más de una fila (columna "Sec."):
            # se queda la primera, igual que antes.
            clave = (texto(fila[ix["centro"]]), ticket_registro)
            if clave in vistos:
                continue
            vistos.add(clave)

            peso_neto_humedo = decimal(fila[ix["humedo"]])
            cantidad_toneladas = peso_neto_humedo / _KG_A_TONELADAS if peso_neto_humedo is not None else None

            lote.append(IngresoFisicoSOP(
                planta_origen=planta_origen,
                ticket_registro=ticket_registro,
                centro=texto(fila[ix["centro"]]),
                material_codigo=texto(fila[ix["material"]]),
                descripcion_material=texto(fila[ix["descripcion"]]),
                fecha_ingreso=fecha(fila[ix["fecha"]]),
                cantidad=cantidad_toneladas,
                unidad_medida_base=texto(fila[ix["umb"]]),
                proveedor_codigo=texto(fila[ix["proveedor_codigo"]]),
                proveedor=texto(fila[ix["proveedor"]]),
                documento_material=texto(fila[ix["documento"]]),
                posicion=texto(fila[ix["posicion"]]),
                tipo_operacion=texto(fila[ix["tipo"]]),
                numero_muestra=texto(fila[ix["muestra"]]),
                **{campo: _analisis(fila[indice]) for indice, campo in ix_calidad.items()},
                premio=float(premio) if (premio := decimal(fila[ix["premio"]])) else None,
                cantidad_seca=float(seco) / _KG_A_TONELADAS if (seco := decimal(fila[ix["seco"]])) else None,
                transportista_codigo=texto(fila[ix["transportista_codigo"]]),
                transportista=texto(fila[ix["transportista"]]),
                placa=texto(fila[ix["placa"]]),
                fecha_pesaje_lleno=_fecha(fila[ix["f_lleno"]]),
                hora_pesaje_lleno=_hora(fila[ix["h_lleno"]]),
                fecha_pesaje_vacio=_fecha(fila[ix["f_vacio"]]),
                hora_pesaje_vacio=_hora(fila[ix["h_vacio"]]),
            ))
            total += 1
            if len(lote) >= batch_size:
                volcar_lote()

        volcar_lote()
        return total
