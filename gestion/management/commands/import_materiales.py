from django.db import transaction
from django.core.management.base import BaseCommand

from gestion.models import Categoria, Material
from ._csv_utils import codigo, fecha, leer_filas, texto
from ._seguimiento_archivos import necesita_importar, registrar_importado


class Command(BaseCommand):
    help = (
        "Importa el maestro de materiales desde el CSV de la transacción SQ01/ZMM060 "
        "(MM60_SUPPLY.csv). Se salta si el archivo no cambió desde la última importación "
        "(usar --forzar para reprocesarlo igual)."
    )

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Ruta al archivo CSV de materiales")
        parser.add_argument("--batch-size", type=int, default=2000)
        parser.add_argument(
            "--forzar", action="store_true",
            help="Reprocesa el archivo aunque no haya cambiado desde la última importación.",
        )

    # Todo el comando en una sola transacción: mucho más rápido en SQLite que
    # confirmar lote por lote, y si algo falla a medias no quedan datos
    # parciales (p.ej. Compras sin el año que se estaba reemplazando).
    @transaction.atomic
    def handle(self, *args, **options):
        path = options["csv_path"]
        batch_size = options["batch_size"]
        forzar = options["forzar"]

        if not forzar and not necesita_importar(path):
            self.stdout.write("Materiales: sin cambios desde la última importación, saltado.")
            return

        categorias_validas = set(Categoria.objects.values_list("grupo_articulo", flat=True))

        lote = []
        vistos = set()
        total = 0

        def volcar_lote():
            nonlocal lote
            if lote:
                Material.objects.bulk_create(lote, batch_size=batch_size, ignore_conflicts=True)
                lote = []

        for row in leer_filas(path):
            cod = codigo(row.get("Material"))
            if not cod or cod in vistos:
                continue
            vistos.add(cod)

            grupo = texto(row.get("Grupo art."))
            categoria_id = grupo if grupo in categorias_validas else None

            lote.append(Material(
                codigo=cod,
                descripcion=texto(row.get("Número de material")),
                categoria_id=categoria_id,
                jerarquia_productos=codigo(row.get("Jquía. Productos")),
                unidad_medida_base=texto(row.get("UMB")),
                tipo_repuesto=texto(row.get("Tipo de Repuesto")),
                material_cancerigeno=texto(row.get("Mat. Cancerígeno")),
                perf=texto(row.get("Perf")),
                centro=texto(row.get("Ce.")),
                grupo_compras=texto(row.get("GCp")),
                campo_c=texto(row.get("C")),
                numero_fabricante=texto(row.get("NºFb")),
                fabricante=codigo(row.get("Fabricante")),
                material_critico=texto(row.get("Mat. Critico")),
                estado_material=texto(row.get("Estado mat.vál.p.tod.ce.")),
                sm=texto(row.get("SM")),
                campo_mat=texto(row.get("Mat")),
                campo_ce=texto(row.get("Ce")),
                fecha_creacion=fecha(row.get("Creado")),
            ))
            total += 1
            if len(lote) >= batch_size:
                volcar_lote()

        volcar_lote()
        registrar_importado(path, total)
        self.stdout.write(self.style.SUCCESS(f"Materiales: {total} filas procesadas."))
