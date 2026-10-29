from django.core.management.base import BaseCommand
from django.db import transaction

from dashboard.queries import marcar_datos_actualizados
from gestion.models import Solicitante
from ._csv_utils import leer_filas, texto


def es_gep_proyectos(*campos):
    """GEP o Proyectos: el área o la gerencia del solicitante es la de
    Ejecución de Proyectos (GEP, PMO, divisiones de proyectos, Planta
    Térmica de la Gerencia de Proyectos)."""
    return any("GEP" in c.upper() or "PROYECT" in c.upper() for c in campos)


class Command(BaseCommand):
    help = ("Importa los solicitantes de compra con su área y gerencia (USUARIOS.xlsx de "
            "'INFORMACIÓN SOLICITANTES'). Reemplaza la tabla completa.")

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Ruta al Excel de solicitantes")

    @transaction.atomic
    def handle(self, *args, **options):
        filas = {}
        for row in leer_filas(options["ruta"]):
            codigo = texto(row.get("SOLICITANTE"))
            if not codigo:
                continue
            area, gerencia, gerencia_fix = (texto(row.get(c)) for c in ("AREA", "GERENCIA", "GERENCIA FIX"))
            filas[codigo.upper()] = Solicitante(
                codigo=codigo, descripcion=texto(row.get("DESCRIPCION SOLICITANTE")), area=area, gerencia=gerencia,
                gerencia_fix=gerencia_fix, gerencia_general=texto(row.get("GERENCIA GENERAL")),
                centro=texto(row.get("CENTRO")), gep_proyectos=es_gep_proyectos(area, gerencia, gerencia_fix),
            )
        Solicitante.objects.all().delete()
        Solicitante.objects.bulk_create(filas.values())
        transaction.on_commit(marcar_datos_actualizados)
        gep = sum(f.gep_proyectos for f in filas.values())
        self.stdout.write(self.style.SUCCESS(f"Solicitantes: {len(filas)} cargados, {gep} de GEP o Proyectos."))
