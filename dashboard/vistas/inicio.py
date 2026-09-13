import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import queries as q  # noqa: E402

st.title("📊 Indicadores de Gestión")
st.caption("Datos ingestados desde SAP (MB5B, ME2N, ZMM060) hacia una base de datos Django, visualizados con Streamlit.")

resumen_mat = q.resumen_materiales()
fechas_inv = q.fechas_inventario_disponibles()
anios_compras = q.anios_compras_disponibles()

col1, col2, col3, col4 = st.columns(4)
col1.metric("Materiales en catálogo", f"{int(resumen_mat['total_materiales']):,}")
col2.metric("Materiales críticos", f"{int(resumen_mat['criticos']):,}")
col3.metric("Categorías", f"{int(resumen_mat['total_categorias']):,}")
col4.metric("Meses de inventario cargados", f"{len(fechas_inv)}")

st.divider()
st.markdown(
    """
    Usa el menú de la izquierda para navegar:

    - **Inventario** — evolución del stock y su valorización mensual (MB5B).
    - **Compras** — pedidos y solicitudes de compra, proveedores y cumplimiento de entrega (ME2N).
    - **Materiales** — catálogo de materiales y categorías (ZMM060 / CATEGORÍAS).
    """
)

if not fechas_inv:
    st.warning("Todavía no hay datos de inventario cargados. Ejecuta `python manage.py import_todo <carpeta>`.")
if not anios_compras:
    st.warning("Todavía no hay datos de compras cargados.")
