import streamlit as st

import bootstrap  # noqa: F401  (configura Django antes de usar el ORM)
from auth import es_admin, requerir_login

st.set_page_config(page_title="Indicadores de Gestión", page_icon="📊", layout="wide")

requerir_login()

paginas = [
    st.Page("vistas/inicio.py", title="Inicio", icon="🏠", default=True),
    st.Page("vistas/inventario.py", title="Inventario", icon="📦"),
    st.Page("vistas/compras.py", title="Compras", icon="🛒"),
    st.Page("vistas/materiales.py", title="Materiales", icon="🗂️"),
]
if es_admin():
    paginas.append(st.Page("vistas/administracion.py", title="Administración", icon="⚙️"))

st.navigation(paginas).run()
