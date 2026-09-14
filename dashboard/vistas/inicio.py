import sys
from pathlib import Path

import plotly.express as px
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

if not fechas_inv:
    st.warning("Todavía no hay datos de inventario cargados. Ejecuta `python manage.py import_todo <carpeta>`.")
if not anios_compras:
    st.warning("Todavía no hay datos de compras cargados.")

st.divider()

tab_inventario, tab_compras, tab_materiales = st.tabs(["📦 Inventario", "🛒 Compras", "🗂️ Materiales"])

with tab_inventario:
    if not fechas_inv:
        st.info("No hay datos de inventario cargados todavía.")
    else:
        serie = q.valor_inventario_mensual()
        st.subheader("Valor de inventario en el tiempo")
        fig = px.line(serie, x="fecha", y="valor_total", markers=True, labels={"fecha": "Mes", "valor_total": "Valor (S/)"})
        st.plotly_chart(fig, width="stretch", key="inicio_inv_serie")

        fecha_sel = st.selectbox("Mes a analizar", options=list(reversed(fechas_inv)), index=0, key="inicio_inv_mes")

        resumen_inv = q.resumen_inventario(fecha_sel)
        c1, c2, c3 = st.columns(3)
        c1.metric("Materiales con stock", f"{int(resumen_inv['materiales']):,}")
        c2.metric("Stock total (unidades)", f"{resumen_inv['stock_total']:,.0f}")
        c3.metric("Valor total", f"S/ {resumen_inv['valor_total']:,.2f}")

        col_izq, col_der = st.columns(2)
        with col_izq:
            st.subheader("Valor por grupo de artículos")
            df_grupo = q.valor_inventario_por_grupo(fecha_sel)
            fig2 = px.bar(df_grupo, x="valor_total", y="grupo", orientation="h", labels={"valor_total": "Valor (S/)", "grupo": ""})
            fig2.update_layout(yaxis={"categoryorder": "total ascending"})
            st.plotly_chart(fig2, width="stretch", key="inicio_inv_grupo")

        with col_der:
            st.subheader("Valor por familia")
            df_familia = q.valor_inventario_por_familia(fecha_sel)
            fig3 = px.bar(df_familia, x="valor_total", y="familia", orientation="h", labels={"valor_total": "Valor (S/)", "familia": ""})
            fig3.update_layout(yaxis={"categoryorder": "total ascending"})
            st.plotly_chart(fig3, width="stretch", key="inicio_inv_familia")

        st.subheader("Top materiales por valor de inventario")
        df_top = q.top_materiales_inventario(fecha_sel)
        st.dataframe(df_top, width="stretch", hide_index=True)

with tab_compras:
    if not anios_compras:
        st.info("No hay datos de compras cargados todavía.")
    else:
        anio_sel = st.selectbox("Año", options=list(reversed(anios_compras)), index=0, key="inicio_compras_anio")

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
        st.plotly_chart(fig_pie, width="stretch", key="inicio_compras_categoria")

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
        st.plotly_chart(fig_contrato, width="stretch", key="inicio_compras_contrato")

        st.subheader("PMP por O.C creada por categoría")
        df_pmp_cat = q.pmp_por_categoria(anio_sel)
        fig_pmp = px.bar(
            df_pmp_cat, x="categoria", y="pmp",
            labels={"pmp": "PMP (días)", "categoria": ""},
            category_orders={"categoria": df_pmp_cat["categoria"].tolist()},
        )
        st.plotly_chart(fig_pmp, width="stretch", key="inicio_compras_pmp_categoria")

with tab_materiales:
    col_izq, col_der = st.columns(2)
    with col_izq:
        st.subheader("Materiales por familia")
        df_familia_mat = q.materiales_por_familia()
        fig = px.bar(df_familia_mat, x="materiales", y="familia", orientation="h", labels={"materiales": "Materiales", "familia": ""})
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, width="stretch", key="inicio_mat_familia")

    with col_der:
        st.subheader("Materiales por fabricante (top 20)")
        df_fab = q.materiales_por_fabricante()
        fig2 = px.bar(df_fab, x="materiales", y="fabricante", orientation="h", labels={"materiales": "Materiales", "fabricante": ""})
        fig2.update_layout(yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig2, width="stretch", key="inicio_mat_fab")
