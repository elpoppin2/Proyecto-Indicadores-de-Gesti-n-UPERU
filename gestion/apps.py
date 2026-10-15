from django.apps import AppConfig
from django.db import OperationalError


def _actualizar_ultimo_ingreso(sender, user, **kwargs):
    """Como django.contrib.auth.models.update_last_login, pero si la base está
    tomada por la carga diaria ("database is locked") no impide ingresar: la
    fecha del último ingreso es un dato secundario."""
    from django.contrib.auth.models import update_last_login
    try:
        update_last_login(sender, user, **kwargs)
    except OperationalError:
        pass


class GestionConfig(AppConfig):
    name = 'gestion'

    def ready(self):
        from django.contrib.auth.models import update_last_login
        from django.contrib.auth.signals import user_logged_in
        user_logged_in.disconnect(update_last_login, dispatch_uid='update_last_login')
        user_logged_in.connect(_actualizar_ultimo_ingreso, dispatch_uid='actualizar_ultimo_ingreso')
