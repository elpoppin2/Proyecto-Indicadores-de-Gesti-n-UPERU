"""Regenera las copias HTML (Planificación y sitio) y deja su estado para la barra de la simulación. Lo lanza el
servidor al aplicar un escenario al S&OP (gestion/copias_html.py); también sirve a mano."""
from django.core.management.base import BaseCommand

from gestion import copias_html


class Command(BaseCommand):
    help = "Regenera las copias HTML de Planificación y del sitio (rutas en datos_locales/copias_html.json)."

    def handle(self, *args, **opciones):
        copias_html.correr(stdout=self.stdout)
