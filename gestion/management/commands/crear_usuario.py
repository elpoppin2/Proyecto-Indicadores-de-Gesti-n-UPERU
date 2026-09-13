from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError

from .setup_roles import ADMIN_GROUP, COORDINADOR_GROUP


class Command(BaseCommand):
    help = (
        "Crea o actualiza un usuario y lo asigna a un rol: 'admin' (acceso total, incluye "
        "el panel de Django) o 'coordinador' (solo visualización en los dashboards). "
        "Requiere haber corrido antes 'python manage.py setup_roles'."
    )

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--password", required=True, help="Contraseña del usuario")
        parser.add_argument("--rol", required=True, choices=["admin", "coordinador"])
        parser.add_argument("--email", default="")

    def handle(self, *args, **options):
        try:
            admin_group = Group.objects.get(name=ADMIN_GROUP)
            coord_group = Group.objects.get(name=COORDINADOR_GROUP)
        except Group.DoesNotExist:
            raise CommandError("Los roles no existen todavía. Corre primero: python manage.py setup_roles")

        username = options["username"]
        rol = options["rol"]

        user, created = User.objects.get_or_create(username=username)
        user.email = options["email"] or user.email
        user.set_password(options["password"])

        if rol == "admin":
            user.is_staff = True
            user.is_superuser = True
            user.groups.set([admin_group])
        else:
            user.is_staff = False
            user.is_superuser = False
            user.groups.set([coord_group])

        user.save()

        accion = "creado" if created else "actualizado"
        self.stdout.write(self.style.SUCCESS(f"Usuario '{username}' {accion} con rol '{rol}'."))
