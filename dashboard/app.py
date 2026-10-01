import functools

import streamlit as st

import bootstrap  # noqa: F401  (configura Django antes de usar el ORM)
import queries as q
import vista_categoria
from auth import es_admin, requerir_login

st.set_page_config(page_title="Indicadores de Gestión", page_icon="📊", layout="wide")

requerir_login()

ICONOS_CATEGORIA = {
    "CAPEX": "🏗️",
    "COMBUSTIBLES": "⛽",
    "EMPAQUES": "📦",
    "MATERIAS PRIMAS": "🪨",
    "REPUESTOS": "🔧",
    "SERVICIOS ADMINISTRATIVOS": "🗂️",
    "SERVICIOS INDUSTRIALES": "⚙️",
    "SUMINISTROS INDUSTRIALES": "🧰",
}


def _slug(categoria):
    return categoria.lower().replace(" ", "-")


anios_compras = q.anios_compras_disponibles()
categorias = q.categorias_dashboard(anios_compras[-1]) if anios_compras else []

paginas = [
    st.Page(
        functools.partial(vista_categoria.render, categoria),
        title=categoria,
        icon=ICONOS_CATEGORIA.get(categoria, "🏷️"),
        url_path=_slug(categoria),
        default=(i == 0),
    )
    for i, categoria in enumerate(categorias)
]
paginas.append(st.Page("vistas/materiales.py", title="Materiales", icon="📇"))
if es_admin():
    paginas.append(st.Page("vistas/administracion.py", title="Administración", icon="⚙️"))

st.navigation(paginas).run()
