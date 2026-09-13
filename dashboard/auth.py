"""Login y control de acceso por rol para los dashboards de Streamlit.

Los roles viven en Django (grupos 'Administradores' / 'Coordinadores', ver
`gestion/management/commands/setup_roles.py`); aquí solo se valida la credencial
contra esa misma base de datos y se guarda el rol en la sesión de Streamlit.
"""
import streamlit as st
from django.contrib.auth import authenticate

ADMIN_GROUP = "Administradores"
COORDINADOR_GROUP = "Coordinadores"


def _rol_de(user):
    if user.is_superuser or user.groups.filter(name=ADMIN_GROUP).exists():
        return "admin"
    if user.groups.filter(name=COORDINADOR_GROUP).exists():
        return "coordinador"
    return None


def _formulario_login():
    st.title("📊 Indicadores de Gestión")
    st.subheader("Iniciar sesión")

    with st.form("login_form"):
        usuario = st.text_input("Usuario")
        clave = st.text_input("Contraseña", type="password")
        enviado = st.form_submit_button("Ingresar")

    if enviado:
        user = authenticate(username=usuario, password=clave)
        if user is None or not user.is_active:
            st.error("Usuario o contraseña incorrectos.")
            return
        rol = _rol_de(user)
        if rol is None:
            st.error("Este usuario no tiene un rol asignado. Contacta a un administrador.")
            return
        st.session_state["auth_usuario"] = user.username
        st.session_state["auth_rol"] = rol
        st.rerun()


def requerir_login():
    """Punto de entrada único: se llama en app.py antes de construir la navegación.
    Detiene la ejecución de la página (st.stop) si no hay sesión activa."""
    if "auth_rol" not in st.session_state:
        _formulario_login()
        st.stop()

    with st.sidebar:
        etiqueta = "Administrador" if st.session_state["auth_rol"] == "admin" else "Coordinador"
        st.caption(f"👤 {st.session_state['auth_usuario']} · {etiqueta}")
        if st.button("Cerrar sesión", width="stretch"):
            st.session_state.pop("auth_usuario", None)
            st.session_state.pop("auth_rol", None)
            st.rerun()


def es_admin():
    return st.session_state.get("auth_rol") == "admin"
