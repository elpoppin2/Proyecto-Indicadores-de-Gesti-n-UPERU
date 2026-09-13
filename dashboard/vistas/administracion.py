import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from django.contrib.auth.models import User  # noqa: E402

st.title("⚙️ Administración")
st.caption("Esta sección solo es visible para el rol Administrador.")

st.subheader("Usuarios y roles")
filas = []
for u in User.objects.all().order_by("username"):
    if u.is_superuser or u.groups.filter(name="Administradores").exists():
        rol = "Administrador"
    elif u.groups.filter(name="Coordinadores").exists():
        rol = "Coordinador"
    else:
        rol = "Sin rol"
    filas.append({
        "usuario": u.username,
        "rol": rol,
        "activo": u.is_active,
        "último ingreso (Django admin)": u.last_login,
    })
st.dataframe(filas, width="stretch", hide_index=True)

st.subheader("Gestión de usuarios")
st.markdown(
    """
    Por seguridad, los usuarios y roles se crean desde la línea de comandos, no desde
    este panel:

    ```powershell
    # Rol admin: acceso total (dashboards + panel de Django)
    python manage.py crear_usuario juan --password "clave-segura" --rol admin

    # Rol coordinador: solo visualización de los dashboards
    python manage.py crear_usuario maria --password "clave-segura" --rol coordinador
    ```

    Si es la primera vez, corre antes `python manage.py setup_roles` para crear los
    grupos **Administradores** y **Coordinadores** con sus permisos.
    """
)

st.subheader("Panel de Django (CRUD completo)")
st.markdown(
    "Para editar datos directamente: `python manage.py runserver` y abre "
    "[http://localhost:8000/admin/](http://localhost:8000/admin/) (solo entran los usuarios con rol admin)."
)
