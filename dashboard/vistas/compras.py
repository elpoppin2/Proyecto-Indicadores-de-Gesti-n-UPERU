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

resumen_contrato = q.resumen_compras_contrato(anio_sel)
valor_bajo_contrato = float(resumen_contrato["valor_bajo_contrato"] or 0)
valor_total_compras = float(resumen_contrato["valor_total"] or 0)
pct_contrato = (valor_bajo_contrato / valor_total_compras * 100) if valor_total_compras else 0

pmp = q.calcular_pmp(anio_sel)

with st.container(border=True):
    st.subheader("Valor de compras bajo contrato")
    cc1, cc2, cc3 = st.columns(3)
    cc1.metric("Valor Compras (S/ miles)", f"{valor_total_compras / 1000:,.0f}")
    cc2.metric("Compras bajo contrato", f"{pct_contrato:.1f}%")
    cc3.metric("PMP por O.C", f"{pmp:.0f}")

st.divider()

st.subheader("Valor de compras por categoría")
df_categoria_c = q.compras_por_categoria(anio_sel)
fig_pie = px.pie(df_categoria_c, values="valor_total", names="categoria")
st.plotly_chart(fig_pie, width="stretch")

st.subheader("Compras bajo contrato por categoría")
df_contrato_c = q.compras_por_categoria_contrato(anio_sel)
df_contrato_largo = df_contrato_c.melt(
    id_vars="categoria",
    value_vars=["valor_total", "valor_bajo_contrato"],
    var_name="tipo",
    value_name="valor",
)
df_contrato_largo["tipo"] = df_contrato_largo["tipo"].map({
    "valor_total": "Compra total",
    "valor_bajo_contrato": "Bajo contrato",
})
fig_contrato = px.bar(
    df_contrato_largo, x="categoria", y="valor", color="tipo", barmode="group",
    labels={"valor": "Valor (S/)", "categoria": "", "tipo": ""},
    color_discrete_map={"Compra total": "red", "Bajo contrato": "white"},
    category_orders={"categoria": df_contrato_c["categoria"].tolist()},
)
st.plotly_chart(fig_contrato, width="stretch")

st.subheader("PMP por O.C creada por categoría")
df_pmp_cat = q.pmp_por_categoria(anio_sel)
fig_pmp = px.bar(
    df_pmp_cat, x="categoria", y="pmp",
    labels={"pmp": "PMP (días)", "categoria": ""},
    category_orders={"categoria": df_pmp_cat["categoria"].tolist()},
)
st.plotly_chart(fig_pmp, width="stretch")
