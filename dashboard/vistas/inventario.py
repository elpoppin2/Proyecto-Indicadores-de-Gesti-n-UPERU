import sys
from pathlib import Path

import plotly.express as px
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import queries as q  # noqa: E402

st.title("📦 Inventario mensual (MB5B)")

fechas = q.fechas_inventario_disponibles()
if not fechas:
    st.warning("No hay datos de inventario cargados todavía.")
    st.stop()

serie = q.valor_inventario_mensual()
st.subheader("Valor de inventario en el tiempo")
fig = px.line(serie, x="fecha", y="valor_total", markers=True, labels={"fecha": "Mes", "valor_total": "Valor (S/)"})
st.plotly_chart(fig, width="stretch")

fecha_sel = st.selectbox("Mes a analizar", options=list(reversed(fechas)), index=0)

resumen = q.resumen_inventario(fecha_sel)
c1, c2, c3 = st.columns(3)
c1.metric("Materiales con stock", f"{int(resumen['materiales']):,}")
c2.metric("Stock total (unidades)", f"{resumen['stock_total']:,.0f}")
c3.metric("Valor total", f"S/ {resumen['valor_total']:,.2f}")

col_izq, col_der = st.columns(2)

with col_izq:
    st.subheader("Valor por grupo de artículos")
    df_grupo = q.valor_inventario_por_grupo(fecha_sel)
    fig2 = px.bar(df_grupo, x="valor_total", y="grupo", orientation="h", labels={"valor_total": "Valor (S/)", "grupo": ""})
    fig2.update_layout(yaxis={"categoryorder": "total ascending"})
    st.plotly_chart(fig2, width="stretch")

with col_der:
    st.subheader("Valor por familia")
    df_familia = q.valor_inventario_por_familia(fecha_sel)
    fig3 = px.bar(df_familia, x="valor_total", y="familia", orientation="h", labels={"valor_total": "Valor (S/)", "familia": ""})
    fig3.update_layout(yaxis={"categoryorder": "total ascending"})
    st.plotly_chart(fig3, width="stretch")

st.subheader("Top materiales por valor de inventario")
df_top = q.top_materiales_inventario(fecha_sel)
st.dataframe(df_top, width="stretch", hide_index=True)
