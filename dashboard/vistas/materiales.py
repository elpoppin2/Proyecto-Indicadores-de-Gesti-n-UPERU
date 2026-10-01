import sys
from pathlib import Path

import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import queries as q  # noqa: E402
from theme import estilo_barras  # noqa: E402

st.title("🗂️ Catálogo de materiales y categorías")

resumen = q.resumen_materiales()
c1, c2, c3 = st.columns(3)
c1.metric("Materiales", f"{int(resumen['total_materiales']):,}")
c2.metric("Materiales críticos", f"{int(resumen['criticos']):,}")
c3.metric("Categorías", f"{int(resumen['total_categorias']):,}")

col_izq, col_der = st.columns(2)

with col_izq:
    st.subheader("Materiales por familia")
    df_familia = q.materiales_por_familia()
    fig = px.bar(df_familia, x="materiales", y="familia", orientation="h", labels={"materiales": "Materiales", "familia": ""})
    fig.update_layout(yaxis={"categoryorder": "total ascending"})
    estilo_barras(fig)
    st.plotly_chart(fig, width="stretch")

with col_der:
    st.subheader("Materiales por fabricante (top 20)")
    df_fab = q.materiales_por_fabricante()
    fig2 = px.bar(df_fab, x="materiales", y="fabricante", orientation="h", labels={"materiales": "Materiales", "fabricante": ""})
    fig2.update_layout(yaxis={"categoryorder": "total ascending"})
    estilo_barras(fig2)
    st.plotly_chart(fig2, width="stretch")
