import datetime
import glob
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from gestion.models import AjustePrecioFBL3N
from ._excel_utils import decimal, fecha, leer_filas_excel_dict, texto
from ._seguimiento_archivos import necesita_importar, registrar_importado

# Clases de documento que ya están en MB51 (entradas WE, consumos WA,
# despachos WL, inventario físico WI con su 701/702): no se guardan, sumarlas
# duplicaría lo que MB51 ya valoriza. Todo lo demás (KR/KA/S2/SA: facturas
# MIRO, XK/XS: anulaciones MR8M, PR: MR22/MR21, PM/X1: liquidación de
# órdenes...) cambia el valor del stock sin cantidad en MB51.
CLASES_EN_MB51 = {"WE", "WA", "WL", "WI"}

# Columnas que el layout de la FBL3N debe traer (definido con el usuario en
# oct-2026). Sin Centro o Código transacción no se puede asignar el ajuste a
# una planta ni saber de qué trata: el archivo se rechaza para volver a
# exportarlo con la disposición completa.
_REQUERIDAS = [
    "Cuenta de mayor", "Material", "Nº documento", "Clase de documento", "Fecha contabiliz.",
    "Importe en moneda local", "Centro", "Código transacción",
]


def _num(valor):
    numero = decimal(valor)
    return float(numero) if numero is not None else None


class Command(BaseCommand):
    help = (
        "Importa los ajustes de valor de las cuentas de existencias desde los Excel de la "
        "FBL3N ('TABLA_FBL3N'). Cada archivo reemplaza los ajustes de las fechas que cubre, "
        "así que volver a bajar un período (p.ej. un año corregido) no duplica. Por defecto "
        "se saltan los archivos que no cambiaron (usar --forzar para reprocesarlos)."
    )

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Archivo .xlsx o carpeta con los Excel de la FBL3N")
        parser.add_argument("--forzar", action="store_true")

    def handle(self, *args, **options):
        ruta = options["ruta"]
        if os.path.isdir(ruta):
            archivos = sorted(a for a in glob.glob(os.path.join(ruta, "**", "*.xlsx"), recursive=True)
                              if not os.path.basename(a).startswith("~$"))
        elif os.path.isfile(ruta):
            archivos = [ruta]
        else:
            raise CommandError(f"Ruta no encontrada: {ruta}")

        total, saltados, rechazados = 0, 0, []
        for archivo in archivos:
            if not options["forzar"] and not necesita_importar(archivo):
                saltados += 1
                continue
            resultado = self._importar_archivo(archivo)
            if isinstance(resultado, str):
                rechazados.append(resultado)
                self.stdout.write(self.style.WARNING(f"  {os.path.basename(archivo)}: NO SE CARGÓ. {resultado}"))
                continue
            total += resultado
        self.stdout.write(self.style.SUCCESS(
            f"FBL3N: {total} ajustes cargados ({saltados} archivo(s) sin cambios, {len(rechazados)} rechazado(s))."
        ))

    @transaction.atomic
    def _importar_archivo(self, archivo):
        filas = leer_filas_excel_dict(archivo)
        primera = next(filas, None)
        if primera is None:
            return "El archivo está vacío."
        faltan = [c for c in _REQUERIDAS if c not in primera]
        if faltan:
            return ("Le faltan las columnas " + ", ".join(faltan)
                    + ": vuelve a exportarlo con la disposición completa.")

        objetos, fechas, cuentas, leidas = [], [], set(), 0

        def agregar(f):
            nonlocal leidas
            fecha_cont = fecha_texto(f.get("Fecha contabiliz."))
            if fecha_cont is None:
                return
            leidas += 1
            fechas.append(fecha_cont)
            cuentas.add(texto(f.get("Cuenta de mayor")))
            clase = texto(f.get("Clase de documento"))
            material = texto(f.get("Material"))
            importe = _num(f.get("Importe en moneda local"))
            # Sin material no se puede asignar a ninguno (reclasificaciones
            # contables, existencias por recibir, compensaciones).
            if clase in CLASES_EN_MB51 or not material or importe is None:
                return
            objetos.append(AjustePrecioFBL3N(
                cuenta_mayor=texto(f.get("Cuenta de mayor")),
                material_codigo=material,
                centro=texto(f.get("Centro")),
                numero_documento=texto(f.get("Nº documento")),
                clase_documento=clase,
                codigo_transaccion=texto(f.get("Código transacción")),
                clave_contabilizacion=texto(f.get("Clave contabiliz.")),
                fecha_documento=fecha_texto(f.get("Fecha de documento")),
                fecha_contabilizacion=fecha_cont,
                importe=importe,
                importe_ml2=_num(f.get("Importe en ML2")),
                moneda_documento=texto(f.get("Moneda del documento")),
                importe_moneda_documento=_num(f.get("Importe en moneda doc.")),
                cantidad=_num(f.get("Cantidad")),
                unidad_medida_base=texto(f.get("Unidad medida base")),
                documento_compras=texto(f.get("Documento compras")),
                proveedor=texto(f.get("Proveedor")),
                referencia=texto(f.get("Referencia"))[:60],
                texto=texto(f.get("Texto"))[:255],
                texto_cabecera=texto(f.get("Texto cab.documento"))[:255],
                usuario=texto(f.get("Nombre del usuario")),
                cuenta_contrapartida=texto(f.get("Cta.contrapartida")),
            ))

        agregar(primera)
        for f in filas:
            agregar(f)
        if not fechas:
            return "No trae ninguna fila con fecha de contabilización."

        # Reemplaza lo que hubiera de las mismas fechas Y las mismas cuentas (un
        # año vuelto a bajar, o el mes en curso actualizado). Por cuenta: un
        # archivo aparte con otras cuentas (p.ej. la de la caliza de cantera
        # propia, clase 23) no borra los ajustes de las cuentas 24-28.
        desde, hasta = min(fechas), max(fechas)
        borrados, _ = AjustePrecioFBL3N.objects.filter(
            fecha_contabilizacion__gte=desde, fecha_contabilizacion__lte=hasta,
            cuenta_mayor__in=cuentas).delete()
        AjustePrecioFBL3N.objects.bulk_create(objetos, batch_size=2000)
        registrar_importado(archivo, len(objetos))
        self.stdout.write(
            f"  {os.path.basename(archivo)}: {leidas} líneas del {desde:%d/%m/%Y} al {hasta:%d/%m/%Y}, "
            f"{len(objetos)} ajustes guardados ({borrados} reemplazados)"
        )
        return len(objetos)


def fecha_texto(valor):
    """leer_filas_excel_dict entrega las fechas como texto ('2026-09-01
    00:00:00'): de vuelta a date."""
    if not valor:
        return None
    try:
        return datetime.date.fromisoformat(str(valor)[:10])
    except ValueError:
        return fecha(valor)
