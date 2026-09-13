import sys
from pathlib import Path

import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import queries as q  # noqa: E402

st.title("🛒 Compras y solicitudes (ME2N)")

anios = q.anios_compras_disponibles()
if not anios:
    st.warning("No hay datos de compras cargados todavía.")
    st.stop()

anio_sel = st.selectbox("Año", options=list(reversed(anios)), index=0)

cumplimiento = q.cumplimiento_entrega(anio_sel)
total = int(cumplimiento["total"] or 0)
a_tiempo = int(cumplimiento["a_tiempo"] or 0)
pct = (a_tiempo / total * 100) if total else 0

c1, c2, c3 = st.columns(3)
c1.metric("Líneas de pedido", f"{total:,}")
c2.metric("Entregadas a tiempo", f"{a_tiempo:,}")
c3.metric("% cumplimiento de entrega", f"{pct:.1f}%")

st.subheader("Monto de compras por mes")
df_mes = q.compras_por_mes(anio_sel)
fig = px.bar(df_mes, x="mes", y="valor_total", labels={"mes": "Mes", "valor_total": "Valor neto (S/)"})
st.plotly_chart(fig, width="stretch")

col_izq, col_der = st.columns(2)

with col_izq:
    st.subheader("Top proveedores")
    df_prov = q.top_proveedores(anio_sel)
    fig2 = px.bar(df_prov, x="valor_total", y="proveedor", orientation="h", labels={"valor_total": "Valor (S/)", "proveedor": ""})
    fig2.update_layout(yaxis={"categoryorder": "total ascending"})
    st.plotly_chart(fig2, width="stretch")

with col_der:
    st.subheader("Materiales más comprados")
    df_mat = q.top_materiales_comprados(anio_sel)
    fig3 = px.bar(df_mat, x="valor_total", y="texto_breve", orientation="h", labels={"valor_total": "Valor (S/)", "texto_breve": ""})
    fig3.update_layout(yaxis={"categoryorder": "total ascending"})
    st.plotly_chart(fig3, width="stretch")

st.subheader("Pendientes por entregar")
df_pend = q.pendientes_por_entregar(anio_sel)
st.dataframe(df_pend, width="stretch", hide_index=True)
