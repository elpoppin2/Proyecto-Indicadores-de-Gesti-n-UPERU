"""Contenido de cada página de categoría del menú lateral (una por categoría de
CATEGORÍAS OFICIAL). No es una página de Streamlit por sí sola: app.py registra una
st.Page por categoría, todas apuntando a la función render() de este módulo."""

import io

import plotly.express as px
import streamlit as st

import queries as q
from theme import estilo_barras, estilo_lineas


def render(categoria):
    st.title(f"📁 {categoria}")

    fechas_inv = q.fechas_inventario_disponibles()
    anios_compras = q.anios_compras_disponibles()

    if not fechas_inv and not anios_compras:
        st.warning("Todavía no hay datos cargados. Ejecuta `python manage.py import_todo <carpeta>`.")
        return

    # --- Compras ---
    col_anio, _, _ = st.columns(3)
    with col_anio:
        anio_sel = None
        if anios_compras:
            anio_sel = st.selectbox(
                "Año (compras)", options=list(reversed(anios_compras)), index=0, key=f"anio_compras_{categoria}"
            )

    st.divider()
    st.subheader(f"🛒 Compras {anio_sel}" if anio_sel else "🛒 Compras")
    if anio_sel is None:
        st.info("Sin años de compras disponibles.")
    else:
        compras_cat = q.compras_por_categoria_contrato(anio_sel)
        fila = compras_cat[compras_cat["categoria"] == categoria]
        if fila.empty:
            st.info("Sin compras registradas en esta categoría para el año seleccionado.")
        else:
            pmp_cat = q.pmp_por_categoria(anio_sel)
            valor_total = float(fila["valor_total"].iloc[0] or 0)
            valor_bajo_contrato = float(fila["valor_bajo_contrato"].iloc[0] or 0)
            pct_contrato = (valor_bajo_contrato / valor_total * 100) if valor_total else 0
            fila_pmp = pmp_cat[pmp_cat["categoria"] == categoria]
            pmp = float(fila_pmp["pmp"].iloc[0]) if not fila_pmp.empty else 0
            with st.container(border=True):
                c1, c2, c3 = st.columns(3)
                c1.metric("Valor Compras (S/. miles)", f"{valor_total / 1000:,.0f}")
                c2.metric("Compras bajo contrato", f"{pct_contrato:.1f}%")
                c3.metric("PMP por O.C", f"{pmp:.0f}")

    if not fechas_inv:
        st.divider()
        st.info("No hay datos de inventario cargados.")
        return

    # --- Evolución de Inventario ---
    st.divider()
    st.subheader("📈 Evolución de Inventario")
    anio_max_inv = fechas_inv[-1].year
    fechas_anio_max_inv = [f for f in fechas_inv if f.year == anio_max_inv]
    idx_inicio_evo = fechas_inv.index(fechas_anio_max_inv[0]) if fechas_anio_max_inv else 0

    col_ini_evo, col_fin_evo = st.columns(2)
    with col_ini_evo:
        mes_inicio_evo = st.selectbox(
            "Mes de inicio", options=fechas_inv, index=idx_inicio_evo,
            format_func=q.formato_mes_anio, key=f"mes_inicio_evolucion_{categoria}",
        )
    with col_fin_evo:
        opciones_fin_evo = [f for f in fechas_inv if f >= mes_inicio_evo]
        mes_fin_evo = st.selectbox(
            "Mes de fin", options=opciones_fin_evo, index=len(opciones_fin_evo) - 1,
            format_func=q.formato_mes_anio, key=f"mes_fin_evolucion_{categoria}_{mes_inicio_evo}",
        )

    serie_cat = q.valor_inventario_mensual_por_categoria(fecha_desde=mes_inicio_evo)
    serie = serie_cat[
        (serie_cat["categoria"] == categoria)
        & (serie_cat["fecha"] >= mes_inicio_evo)
        & (serie_cat["fecha"] <= mes_fin_evo)
    ]
    if serie.empty:
        st.info("Esta categoría no tiene inventario registrado en ese rango.")
    else:
        serie = serie.sort_values("fecha").copy()
        serie["etiqueta"] = (serie["valor_total"] / 1_000_000).map(lambda v: f"{v:,.1f}M")
        serie["etiqueta_mes"] = serie["fecha"].map(q.formato_mes_anio_abrev)
        fig = px.line(
            serie, x="etiqueta_mes", y="valor_total", markers=True, text="etiqueta",
            labels={"etiqueta_mes": "Mes", "valor_total": "Valor (S/.)"},
        )
        fig.update_xaxes(categoryorder="array", categoryarray=serie["etiqueta_mes"].tolist())
        fig.update_traces(textposition="top center")
        estilo_lineas(fig)
        st.plotly_chart(fig, width="stretch", key=f"serie_{categoria}")

    # --- Inventario (foto del mes: métricas, familias, top materiales) ---
    st.divider()
    st.subheader("📦 Inventario")
    fecha_cierre = st.selectbox(
        "Mes de cierre", options=list(reversed(fechas_inv)), index=0,
        format_func=q.formato_mes_anio, key=f"mes_cierre_{categoria}",
    )
    inv_mat = q.inventario_por_material(fecha_cierre)
    inv = inv_mat[inv_mat["categoria"] == categoria]
    if inv.empty:
        st.info("Esta categoría no tiene inventario (solo compras de servicios o CAPEX).")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Materiales con stock", f"{len(inv):,}")
        c2.metric("Stock total (unidades)", f"{inv['stock_cierre'].sum():,.0f}")
        c3.metric("Valor total", q.formato_soles(inv["valor_total"].sum()))

        familias_df = (
            inv.groupby("familia", as_index=False)["valor_total"].sum()
            .sort_values("valor_total", ascending=False).head(15)
        )
        fig_fam = px.bar(
            familias_df, x="valor_total", y="familia", orientation="h",
            labels={"valor_total": "Valor (S/.)", "familia": ""},
        )
        fig_fam.update_layout(yaxis={"categoryorder": "total ascending"})
        estilo_barras(fig_fam)
        st.plotly_chart(fig_fam, width="stretch", key=f"familia_{categoria}")

        st.markdown("**Top materiales por valor de inventario**")
        centros_top_opts = ["Todos"] + sorted(c for c in inv["centro"].unique().tolist() if c)
        centro_top_sel = st.selectbox("Centro", options=centros_top_opts, index=0, key=f"centro_top_{categoria}")
        inv_top = inv if centro_top_sel == "Todos" else inv[inv["centro"] == centro_top_sel]

        top = inv_top.sort_values("valor_total", ascending=False).head(20).copy()
        top["stock_cierre"] = top["stock_cierre"].map(lambda v: f"{v:,.0f}")
        top["valor_total"] = top["valor_total"].map(q.formato_soles)
        st.dataframe(
            top[[
                "material_codigo", "texto_breve", "familia", "centro",
                "unidad_medida_base", "stock_cierre", "valor_total",
            ]],
            width="stretch", hide_index=True,
            column_config={
                "material_codigo": "Material", "texto_breve": "Descripción", "familia": "Familia",
                "centro": "Centro", "unidad_medida_base": "UMB", "stock_cierre": "Stock", "valor_total": "Valor",
            },
        )

    # --- Índice de Rotación ---
    st.divider()
    st.subheader("🔄 Índice de Rotación de Inventario")
    fecha_rotacion = st.selectbox(
        "Mes de referencia (rotación)", options=list(reversed(fechas_inv)), index=0,
        format_func=q.formato_mes_anio, key=f"mes_rotacion_{categoria}",
    )
    rotacion = q.rotacion_inventario_por_material(fecha_rotacion)
    rot = rotacion[rotacion["categoria"] == categoria]
    if rot.empty:
        st.info("Sin datos de rotación para esta categoría en el mes seleccionado.")
    else:
        centros_rot_opts = ["Todos"] + sorted(c for c in rot["centro"].unique().tolist() if c)
        centro_rot_sel = st.selectbox("Centro", options=centros_rot_opts, index=0, key=f"centro_rotacion_{categoria}")
        rot = rot if centro_rot_sel == "Todos" else rot[rot["centro"] == centro_rot_sel]

        st.caption(
            "Consumo promedio (MB51, últimos 6 meses calendario: clases de movimiento "
            "201/202, 221/222, 261/262, 281/282) ÷ Stock de cierre (MB5B), en cantidad."
        )
        rot = rot.copy()
        rot["stock_cierre"] = rot["stock_cierre"].map(lambda v: f"{v:,.0f}")
        rot["consumo_promedio"] = rot["consumo_promedio"].map(lambda v: f"{v:,.1f}")
        rot["indice_rotacion"] = rot["indice_rotacion"].map(lambda v: f"{v:,.2f}" if v == v else "")
        rot["cobertura_meses"] = rot["cobertura_meses"].map(lambda v: f"{v:,.2f}" if v == v else "")
        st.dataframe(
            rot[[
                "material_codigo", "texto_breve", "unidad_medida_base", "centro",
                "stock_cierre", "consumo_promedio", "indice_rotacion", "cobertura_meses",
            ]],
            width="stretch", hide_index=True,
            column_config={
                "material_codigo": "Material", "texto_breve": "Descripción", "unidad_medida_base": "UMB",
                "centro": "Centro", "stock_cierre": "Stock de cierre", "consumo_promedio": "Consumo promedio (6 meses)",
                "indice_rotacion": "Índice de Rotación (veces)", "cobertura_meses": "Cobertura (meses)",
            },
        )

    # --- % Variación de Inventario ---
    st.divider()
    st.subheader("📊 % Variación de Inventario")
    col_final, col_inicial, col_fam = st.columns(3)
    with col_final:
        fecha_var_final = st.selectbox(
            "Mes de inventario", options=list(reversed(fechas_inv)), index=0,
            format_func=q.formato_mes_anio, key=f"mes_var_final_{categoria}",
        )
    with col_inicial:
        opciones_comparativo = [f for f in fechas_inv if f < fecha_var_final]
        fecha_var_inicial = None
        if opciones_comparativo:
            fecha_var_inicial = st.selectbox(
                "Mes comparativo (variación)", options=list(reversed(opciones_comparativo)), index=0,
                format_func=q.formato_mes_anio, key=f"mes_var_inicial_{categoria}_{fecha_var_final}",
            )
        else:
            st.info("No hay un mes anterior para comparar.")

    if fecha_var_inicial:
        variacion = q.variacion_inventario_por_material(fecha_var_inicial, fecha_var_final)
        var = variacion[variacion["categoria"] == categoria]
        if var.empty:
            st.info("Sin datos de variación para esta categoría en el rango seleccionado.")
        else:
            with col_fam:
                familias_opts = ["Todas"] + sorted(var["familia"].dropna().unique().tolist())
                familia_sel = st.selectbox("Familia", options=familias_opts, index=0, key=f"familia_var_{categoria}")
            if familia_sel != "Todas":
                var = var[var["familia"] == familia_sel]

            var_fmt = var.copy()
            var_fmt["stock_mes_inicial"] = var_fmt["stock_mes_inicial"].map(lambda v: f"{v:,.0f}")
            var_fmt["stock_mes_final"] = var_fmt["stock_mes_final"].map(lambda v: f"{v:,.0f}")
            var_fmt["variacion_stock"] = var_fmt["variacion_stock"].map(lambda v: f"{v:,.0f}")
            var_fmt["valor_mes_inicial"] = var_fmt["valor_mes_inicial"].map(q.formato_soles)
            var_fmt["valor_mes_final"] = var_fmt["valor_mes_final"].map(q.formato_soles)
            var_fmt["variacion_valor"] = var_fmt["variacion_valor"].map(q.formato_soles)
            var_fmt["variacion_pct"] = var_fmt["variacion_pct"].map(lambda v: f"{v:.1f}%" if v == v else "")
            st.dataframe(
                var_fmt[[
                    "material_codigo", "texto_breve", "unidad_medida_base",
                    "stock_mes_inicial", "stock_mes_final", "variacion_stock",
                    "valor_mes_inicial", "valor_mes_final", "variacion_valor", "variacion_pct",
                ]],
                width="stretch", hide_index=True,
                column_config={
                    "material_codigo": "Material", "texto_breve": "Descripción", "unidad_medida_base": "UMB",
                    "stock_mes_inicial": f"Stock {q.formato_mes_anio(fecha_var_inicial)}",
                    "stock_mes_final": f"Stock {q.formato_mes_anio(fecha_var_final)}",
                    "variacion_stock": "Variación (cantidad)",
                    "valor_mes_inicial": f"Valor {q.formato_mes_anio(fecha_var_inicial)}",
                    "valor_mes_final": f"Valor {q.formato_mes_anio(fecha_var_final)}",
                    "variacion_valor": "Variación (S/.)", "variacion_pct": "Variación (%)",
                },
            )

            # El Excel se arma solo si se pide, para no generarlo en cada carga.
            if st.checkbox("Preparar descarga en Excel", key=f"excel_{categoria}"):
                buffer = io.BytesIO()
                var.to_excel(buffer, index=False, sheet_name="Variacion Inventario", engine="openpyxl")
                st.download_button(
                    "⬇️ Descargar en Excel",
                    data=buffer.getvalue(),
                    file_name=f"variacion_{categoria}_{fecha_var_inicial}_{fecha_var_final}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"descarga_{categoria}",
                )
