from django.core.management.base import BaseCommand

from gestion.models import Categoria
from ._csv_utils import leer_filas, texto


class Command(BaseCommand):
    help = "Importa el catálogo de categorías desde el CSV 'CATEGORÍAS OFICIAL.csv'."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Ruta al archivo CSV de categorías")

    def handle(self, *args, **options):
        path = options["csv_path"]
        creados, actualizados = 0, 0

        for row in leer_filas(path):
            grupo = texto(row.get("Grupo art."))
            if not grupo:
                continue
            _, created = Categoria.objects.update_or_create(
                grupo_articulo=grupo,
                defaults={
                    "descripcion": texto(row.get("Descripcion Grupo art.")),
                    "familia": texto(row.get("Familia")),
                    "categoria": texto(row.get("Categoria")),
                    "tipo_material": texto(row.get("Tipo de Material")),
                },
            )
            if created:
                creados += 1
            else:
                actualizados += 1

        self.stdout.write(self.style.SUCCESS(
            f"Categorías: {creados} creadas, {actualizados} actualizadas."
        ))
