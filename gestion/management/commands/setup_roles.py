from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand

from gestion.models import Categoria, InventarioMensual, LineaCompra, Material

ADMIN_GROUP = "Administradores"
COORDINADOR_GROUP = "Coordinadores"
MODELOS = [Categoria, Material, InventarioMensual, LineaCompra]


def _permisos(modelos, acciones):
    permisos = []
    for modelo in modelos:
        ct = ContentType.objects.get_for_model(modelo)
        for accion in acciones:
            codename = f"{accion}_{modelo._meta.model_name}"
            permisos.append(Permission.objects.get(content_type=ct, codename=codename))
    return permisos


class Command(BaseCommand):
    help = (
        "Crea (o actualiza) los grupos de roles 'Administradores' (acceso total) y "
        "'Coordinadores' (solo visualización) con sus permisos sobre los modelos de gestión."
    )

    def handle(self, *args, **options):
        admin_group, _ = Group.objects.get_or_create(name=ADMIN_GROUP)
        coord_group, _ = Group.objects.get_or_create(name=COORDINADOR_GROUP)

        admin_group.permissions.set(_permisos(MODELOS, ["add", "change", "delete", "view"]))
        coord_group.permissions.set(_permisos(MODELOS, ["view"]))

        self.stdout.write(self.style.SUCCESS(
            f"Grupo '{ADMIN_GROUP}': acceso total (add/change/delete/view).\n"
            f"Grupo '{COORDINADOR_GROUP}': solo visualización (view)."
        ))
