import datetime
import json
import os

from django.core.management.base import BaseCommand, CommandError

from dashboard import queries as q

_GESTION = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLANTILLA = os.path.join(_GESTION, "report_templates", "planificacion_estatico.html")
ESTATICOS = os.path.join(_GESTION, "static", "gestion")


def _fecha_iso(valor):
    if hasattr(valor, "isoformat"):
        return valor.isoformat()
    return str(valor)[:10]


class Command(BaseCommand):
    help = (
        "Genera un HTML estático y autocontenido de la pestaña Planificación (mismo diseño "
        "que el localhost, con los datos embebidos) para compartirlo sin servidor ni login."
    )
    MESES_INDICADORES = 12  # meses del filtro Mes de Indicadores que trae el estático

    def add_arguments(self, parser):
        parser.add_argument("salida", help="Ruta del archivo .html a escribir (se sobrescribe)")

    def handle(self, *args, **options):
        salida = options["salida"]
        if not os.path.isfile(PLANTILLA):
            raise CommandError(f"No se encontró la plantilla {PLANTILLA}")

        meses = q.meses_planificacion_disponibles()
        if not meses:
            raise CommandError("No hay Plan de Abastecimiento cargado")

        plan_rows = []
        for m in meses:
            df = q.plan_vs_real_abastecimiento_por_material(m)
            for _, r in df.iterrows():
                plan_rows.append({
                    "mes": m.isoformat(), "material_codigo": r["material_codigo"],
                    "texto_breve": r["texto_breve"], "centro": r["centro"],
                    "categoria": r["categoria"], "familia": r["familia"],
                    "cantidad_plan": round(float(r["cantidad_plan"]), 3),
                    "cantidad_real": round(float(r["cantidad_real"]), 3),
                })

        # Ingresos por proveedor de cada material (Detalle por material).
        proveedor_rows = []
        for m in meses:
            for r in q.ingresos_fisicos_por_proveedor(m).itertuples():
                proveedor_rows.append({
                    "mes": m.isoformat(), "material_codigo": r.material_codigo, "centro": r.centro,
                    "proveedor": r.proveedor or "(sin proveedor)", "codigo": r.proveedor_codigo,
                    "tipo": "Propio" if str(r.tipo_operacion) == "5" else "Terceros",
                    "real": round(float(r.cantidad_real), 3), "carros": int(r.carros),
                })

        # Evolución de entregas por proveedor (replica views._contexto_entregas):
        # entregas por mes/material/centro/proveedor de los últimos meses.
        from gestion.views import _ENTREGAS_MESES
        entregas = q.entregas_por_proveedor_sop()
        if not entregas.empty:
            entregas = entregas[entregas["mes"].isin(sorted(entregas["mes"].unique())[-_ENTREGAS_MESES:])]
        entregas_rows = [
            {"mes": r.mes.isoformat(), "material_codigo": r.material_codigo, "texto_breve": r.texto_breve,
             "centro": r.centro, "proveedor": r.proveedor, "tipo": r.tipo,
             "toneladas": round(float(r.toneladas), 3), "carros": int(r.carros)}
            for r in entregas.itertuples()
        ]

        catalogo = q.catalogo_materiales()[["material_codigo", "categoria"]]
        daily_rows = []
        for m in meses:
            fi, ff = q._limites_mes(m)
            real = q._query(f"""
                SELECT {q.FECHA_INGRESO_EFECTIVA} AS fecha, material_codigo AS material_codigo, centro AS centro,
                       SUM(cantidad) AS cantidad_real, COUNT(*) AS carros
                FROM gestion_ingresofisicosop
                WHERE {q.FECHA_INGRESO_EFECTIVA} BETWEEN %s AND %s AND tipo_operacion IN ({q._IN_TIPOS_OPERACION_RECEPCION})
                GROUP BY {q.FECHA_INGRESO_EFECTIVA}, material_codigo, centro
            """, [fi, ff])
            if real.empty:
                continue
            real = real.merge(catalogo, on="material_codigo", how="left")
            real = q._con_categoria_efectiva_planificacion(real, q.plan_abastecimiento_por_material(m))
            real = real[real["categoria"].isin(q.CATEGORIAS_PLANIFICACION_SOP)]
            for _, r in real.iterrows():
                daily_rows.append({
                    "mes": m.isoformat(), "fecha": _fecha_iso(r["fecha"]),
                    "material_codigo": r["material_codigo"], "centro": r["centro"],
                    "cantidad_real": round(float(r["cantidad_real"]), 3), "carros": int(r["carros"]),
                })

        meses_inv = q.meses_inventario_disponibles()
        cobertura_rows = []
        cobertura_por_fecha = q.cobertura_stock_planificacion_por_mes(meses_inv.values())
        for mes_key, fecha_stock in sorted(meses_inv.items()):
            df = cobertura_por_fecha[fecha_stock]
            for _, r in df.iterrows():
                nulo = lambda v: None if v != v else v  # noqa: E731
                cobertura_rows.append({
                    "mes": mes_key.isoformat(), "fecha_stock": fecha_stock.isoformat(),
                    "material_codigo": r["material_codigo"], "texto_breve": r["texto_breve"],
                    "centro": r["centro"], "categoria": r["categoria"], "familia": r["familia"],
                    "stock_cierre": round(float(r["stock_cierre"]), 3),
                    "consumo_diario": round(float(r["consumo_diario"]), 4),
                    "consumo_mensual": round(float(r["consumo_mensual"]), 3),
                    "cobertura_dias": None if nulo(r["cobertura_dias"]) is None else round(float(r["cobertura_dias"]), 3),
                    "cobertura_meses": None if nulo(r["cobertura_meses"]) is None else round(float(r["cobertura_meses"]), 4),
                })

        # Evolución de inventario (S&OP): ya calculada por material/mes; el
        # HTML solo filtra y suma (ver sección 5 de la plantilla).
        proyeccion_rows = []
        proyeccion_inicial = {}
        diagnostico_rows = []
        for snapshot in q.snapshots_sop_disponibles():
            fecha_inicial = q.fecha_inventario_inicial_sop(snapshot)
            if fecha_inicial:
                proyeccion_inicial[snapshot.isoformat()] = fecha_inicial.isoformat()
            for r in q.proyeccion_inventario_sop(snapshot).itertuples():
                proyeccion_rows.append({
                    "snapshot": snapshot.isoformat(), "mes": r.mes.isoformat(),
                    "material_codigo": r.material_codigo, "texto_breve": r.texto_breve,
                    "centro": r.centro, "categoria": r.categoria,
                    "stock_inicial": round(float(r.stock_inicial), 3),
                    "abastecimiento": round(float(r.abastecimiento), 3),
                    "consumo": round(float(r.consumo), 3),
                    "traslado": round(float(r.traslado), 3),
                    "traslado_recibido": round(float(r.traslado_recibido), 3),
                    "stock_final": round(float(r.stock_final), 3),
                    "stock_minimo": None if r.stock_minimo != r.stock_minimo else round(float(r.stock_minimo), 3),
                    "stock_maximo": None if r.stock_maximo != r.stock_maximo else round(float(r.stock_maximo), 3),
                    "estado": r.estado,
                })
            for d in q.diagnostico_minmax_sop(snapshot).to_dict("records"):
                diagnostico_rows.append({
                    "snapshot": snapshot.isoformat(),
                    **{k: d[k] for k in ("material_codigo", "texto_breve", "centro", "diagnostico", "diagnostico_texto",
                                         "meses_bajo_minimo", "meses_en_rango", "meses_sobre_maximo", "lead_time")},
                    **{k: (None if d[k] != d[k] else round(float(d[k]), 3))
                       for k in ("stock_minimo", "stock_maximo", "stock_hoy", "exceso_maximo", "recorte_posible",
                                 "faltante", "deficit_en_plazo")},
                    "pedir_a_mas_tardar": d["pedir_a_mas_tardar"].isoformat() if d["pedir_a_mas_tardar"] else None,
                    "primer_mes_bajo_minimo": d["primer_mes_bajo_minimo"].isoformat() if d["primer_mes_bajo_minimo"] else None,
                })

        # Costo por energía de los carbones: sumas por mes/material/centro/
        # proveedor (el HTML filtra y calcula S/ por Gcal, ver sección 6 de la
        # plantilla y views._contexto_energia).
        energia_rows = []
        premios = q.premios_calidad_sop()
        if not premios.empty:
            energia = q.ingresos_con_energia(premios)
            con_cenizas = energia["cenizas"].notna()
            energia = energia.assign(
                mes=energia["fecha"].map(lambda f: f.replace(day=1).isoformat()),
                cen_t=(energia["cenizas"] * energia["toneladas"]).where(con_cenizas, 0.0),
                t_cen=energia["toneladas"].where(con_cenizas, 0.0),
            )
            claves = ["mes", "material_codigo", "texto_breve", "centro", "proveedor"]
            for k, g in energia.groupby(claves):
                energia_rows.append({
                    **dict(zip(claves, k)), "documentos": int(len(g)),
                    **{c: round(float(g[c].sum()), 3) for c in ("toneladas", "gcal", "importe", "premio", "cen_t", "t_cen")},
                })

        # Evaluación de consumo (sección 7, replica views._contexto_consumo):
        # plan de consumos de todos los S&OP y consumo real mensual de MB51.
        plan_consumo = q.plan_consumo_sop()
        real_consumo = q.consumo_real_mensual_sop()
        consumo_plan_rows = [
            {"snapshot": r.snapshot.isoformat(), "material_codigo": r.material_codigo, "centro": r.centro,
             "mes": r.mes.isoformat(), "plan": round(float(r.plan), 3), "descripcion": r.descripcion or ""}
            for r in plan_consumo.itertuples()
        ]
        consumo_real_rows = [
            {"material_codigo": r.material_codigo, "centro": r.centro, "mes": r.mes.isoformat(),
             "consumo": round(float(r.consumo), 3)}
            for r in real_consumo.itertuples()
        ]
        # Descripción y categoría como en la vista: la de MB51, si no la del
        # maestro (el HTML usa la del plan si no hay ninguna).
        catalogo_nombres = q.catalogo_materiales().drop_duplicates("material_codigo").set_index("material_codigo")
        reales = real_consumo.drop_duplicates("material_codigo").set_index("material_codigo")
        consumo_nombres = {}
        for codigo in sorted(set(plan_consumo["material_codigo"]) if not plan_consumo.empty else []):
            texto = (reales["texto_breve"].get(codigo) if codigo in reales.index else None) or catalogo_nombres["texto_breve"].get(codigo) or ""
            categoria = (reales["categoria"].get(codigo) if codigo in reales.index else None) or catalogo_nombres["categoria"].get(codigo) or "MATERIAS PRIMAS"
            consumo_nombres[codigo] = {"texto": texto, "categoria": categoria}
        fecha_corte = q._query("SELECT MAX(fecha_contabilizacion) AS f FROM gestion_movimientomaterial")["f"].iloc[0]

        # Consumo por tonelada producida (sección 8, replica
        # views._contexto_consumo_especifico).
        factor_rows = [
            {"material_codigo": r.material_codigo, "texto_breve": r.texto_breve, "centro": r.centro, "producto": r.producto,
             "participacion": round(float(r.participacion), 4), "mes": r.mes.isoformat(),
             "consumo": round(float(r.consumo), 3), "produccion": round(float(r.produccion), 3), "factor": float(r.factor)}
            for r in q.consumo_por_tonelada_sop().itertuples()
        ]
        produccion_rows = [
            {"centro": r.centro, "mes": r.mes.isoformat(), "producto": r.producto, "produccion": round(float(r.produccion), 3)}
            for r in q.produccion_mensual_planta().itertuples()
        ]

        datos = {
            "generado": datetime.date.today().isoformat(),
            "pcq": self._precios_calidad_premios(), "centro_importacion": q.CENTRO_IMPORTACION,
            "parametros_calidad": [list(p) for p in q.PARAMETROS_CALIDAD],
            "consumo_plan_rows": consumo_plan_rows, "consumo_real_rows": consumo_real_rows,
            "consumo_nombres": consumo_nombres, "fecha_corte": str(fecha_corte)[:10] if fecha_corte else None,
            "factor_rows": factor_rows, "produccion_rows": produccion_rows,
            "energia_rows": energia_rows,
            "proyeccion_rows": proyeccion_rows, "proyeccion_inicial": proyeccion_inicial,
            "diagnostico_rows": diagnostico_rows, "estados_stock": q.ESTADOS_STOCK,            "meses": [m.isoformat() for m in meses],
            "meses_inventario": {k.isoformat(): v.isoformat() for k, v in sorted(meses_inv.items())},
            "centros_desc": q.descripcion_centros_planificacion(), "nombres_centro": q.NOMBRES_CENTRO,
            "plan_rows": plan_rows, "daily_rows": daily_rows, "cobertura_rows": cobertura_rows,
            "proveedor_rows": proveedor_rows, "entregas_rows": entregas_rows,
        }

        with open(PLANTILLA, encoding="utf-8") as f:
            plantilla = f.read()
        # El estilo y las pestañas son los mismos archivos del localhost (no una
        # copia que se desactualice): se incrustan tal cual al generar.
        with open(os.path.join(ESTATICOS, "css", "dashboard.css"), encoding="utf-8") as f:
            css = f.read()
        with open(os.path.join(ESTATICOS, "js", "zonas.js"), encoding="utf-8") as f:
            zonas_js = f.read()
        # Transición de los gráficos al abrir cada zona (mismo archivo que el localhost).
        with open(os.path.join(ESTATICOS, "js", "graficos.js"), encoding="utf-8") as f:
            zonas_js += "\n" + f.read()
        # Logo de UNACEM incrustado (el archivo se abre sin servidor).
        import base64
        with open(os.path.join(ESTATICOS, "img", "unacem-logo.svg"), "rb") as f:
            logo = "data:image/svg+xml;base64," + base64.b64encode(f.read()).decode("ascii")
        # Gráficos de la pestaña Indicadores: los dibuja el mismo código del
        # localhost (vista por defecto de cada pestaña, sin filtros), así
        # quedan idénticos. El filtro Mes trae ya dibujados los últimos
        # MESES_INDICADORES meses (cada uno en un <template>; el JS del
        # estático cambia entre ellos): todos inflarían demasiado el archivo.
        import re
        from django.test import RequestFactory
        from gestion import views
        nota_web = "con los filtros de su pestaña"

        def indicadores_de(mes):
            html = views._html_indicadores(RequestFactory().get("/planificacion/"), mes_forzado=mes)
            if nota_web not in html:
                raise CommandError("Cambió el texto de la nota de Indicadores: actualizar el exportador")
            html = html.replace(nota_web, "con la vista por defecto de cada pestaña")
            return re.sub(r"\n\s+", "\n", html)  # sin la sangría de la plantilla

        indicadores = indicadores_de("")
        meses_opciones = re.findall(r'<option value="(\d{4}-\d{2}-\d{2})"', indicadores)[:self.MESES_INDICADORES]
        indicadores += "\n" + "\n".join(
            f'<template id="indicadores-mes-{m}">{indicadores_de(m)}</template>' for m in meses_opciones)
        # "</" escapado: un texto de SAP con "</script>" no puede cortar el <script> de los datos.
        datos_json = json.dumps(datos, ensure_ascii=False).replace("</", "<\\/")
        html = (plantilla.replace("/*__DASHBOARD_CSS__*/", css)
                .replace("/*__ZONAS_JS__*/", zonas_js)
                .replace("__LOGO_UNACEM__", logo)
                .replace("<!--__INDICADORES_HTML__-->", indicadores)
                .replace("__DATA_JSON__", datos_json))

        # Se escribe a un temporal y se reemplaza al final: si algo falla a
        # medias, el archivo compartido anterior queda intacto.
        tmp = salida + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(tmp, salida)
        self.stdout.write(self.style.SUCCESS(
            f"HTML generado: {salida} ({len(html) // 1024} KB, {len(plan_rows)} filas plan, "
            f"{len(daily_rows)} diarias, {len(cobertura_rows)} cobertura)"
        ))

    @staticmethod
    def _precios_calidad_premios():
        """Datos de Precios de entrada, Inflación/IPU, Calidad y Premios para
        el HTML (secciones 9 a 12 de la plantilla, que replican las vistas).
        - Precios: una fila por material/centro/mes (la inflación y el IPU se
          calculan en el navegador desde acá, igual que queries.inflacion_precios).
        - Calidad: los tickets sumados por material/centro/proveedor/número de
          muestra/fecha (32K tickets -> ~4K filas). Por parámetro guarda
          [Σ valor×T, Σ T, Σ valor, n], con lo que salen exactos el promedio
          ponderado por toneladas (tickets con T > 0) y el promedio simple de
          cada muestra (mín/máx/desviación entre muestras).
        - Premios: sumas por mes/material/centro/proveedor (el período filtra
          por mes), y los 300 documentos más recientes de cada
          material/centro/proveedor (la tabla por documento muestra los 300 más
          recientes de lo filtrado, que siempre están entre estos)."""
        campos = q._CAMPOS_CALIDAD

        precios_rows = [
            {"material_codigo": r.material_codigo, "texto_breve": r.texto_breve, "centro": r.centro,
             "categoria": r.categoria, "mes": r.mes.isoformat(), "toneladas": round(float(r.toneladas), 3),
             "importe": round(float(r.importe), 2), "precio": float(r.precio),
             "entradas": int(r.entradas), "proveedores": int(r.proveedores)}
            for r in q.precios_entrada_sop_real().itertuples()
        ]

        def sumas_calidad(df, con_simple):
            """Columnas auxiliares por parámetro para sumar por grupo."""
            extra = {}
            con_t = df["toneladas"] > 0
            for c in campos:
                dato = df[c].notna()
                extra[f"{c}__tv"] = (df[c] * df["toneladas"]).where(dato & con_t, 0.0)
                extra[f"{c}__t"] = df["toneladas"].where(dato & con_t, 0.0)
                if con_simple:
                    extra[f"{c}__v"] = df[c].where(dato, 0.0)
                    extra[f"{c}__n"] = dato.astype(int)
            return df.assign(**extra)

        # Formato compacto (el archivo pesaba ~4 MB más con claves repetidas):
        # materiales y proveedores van en listas y cada fila guarda su índice;
        # los parámetros van como [índice en PARAMETROS_CALIDAD, sumas...].
        materiales, proveedores = {}, {}

        def indice_material(codigo, texto, categoria):
            if codigo not in materiales:
                materiales[codigo] = [len(materiales), [codigo, texto, categoria]]
            return materiales[codigo][0]

        def indice_proveedor(nombre):
            return proveedores.setdefault(nombre, len(proveedores))

        def parametros_de(fila, con_simple):
            salida = []
            for i, c in enumerate(campos):
                if con_simple and fila[f"{c}__n"] > 0:
                    salida += [i, round(fila[f"{c}__tv"], 3), round(fila[f"{c}__t"], 3),
                               round(fila[f"{c}__v"], 4), int(fila[f"{c}__n"])]
                elif not con_simple and fila[f"{c}__t"] > 0:
                    salida += [i, round(fila[f"{c}__tv"], 3), round(fila[f"{c}__t"], 3)]
            return salida

        calidad_rows = []
        calidad = q.calidad_ingresos_sop()
        if not calidad.empty:
            calidad = calidad.reset_index(drop=True)
            calidad = calidad.assign(
                proveedor=calidad["proveedor"].replace("", "(sin proveedor)"),
                # Sin número de muestra, cada ticket es su propia muestra (como en resumen_calidad).
                muestra=[m if m else f"#{i}" for i, m in enumerate(calidad["numero_muestra"])],
            )
            calidad = sumas_calidad(calidad, True)
            claves = ["material_codigo", "texto_breve", "categoria", "centro", "proveedor", "muestra", "fecha"]
            sumas = [c for c in calidad.columns if "__" in c] + ["toneladas"]
            for fila in calidad.groupby(claves, as_index=False)[sumas].sum().to_dict("records"):
                calidad_rows.append([
                    indice_material(fila["material_codigo"], fila["texto_breve"], fila["categoria"]), fila["centro"],
                    indice_proveedor(fila["proveedor"]), fila["muestra"], fila["fecha"].isoformat(),
                    round(float(fila["toneladas"]), 3), parametros_de(fila, True),
                ])

        premios_rows, premios_docs = [], []
        premios = q.premios_calidad_sop()
        if not premios.empty:
            premios = premios.assign(mes=[f.replace(day=1).isoformat() for f in premios["fecha"]])
            aux = sumas_calidad(premios, False).assign(
                premios=premios["premio"].where(premios["premio"] > 0, 0.0),
                castigos=premios["premio"].where(premios["premio"] < 0, 0.0),
                n_premio=(premios["premio"] > 0).astype(int), n_castigo=(premios["premio"] < 0).astype(int),
                documentos=1,
            )
            claves = ["mes", "material_codigo", "texto_breve", "categoria", "centro", "proveedor"]
            sumas = [c for c in aux.columns if "__" in c] + ["toneladas", "importe", "premios", "castigos",
                                                             "n_premio", "n_castigo", "documentos"]
            for fila in aux.groupby(claves, as_index=False)[sumas].sum().to_dict("records"):
                premios_rows.append([
                    fila["mes"], indice_material(fila["material_codigo"], fila["texto_breve"], fila["categoria"]),
                    fila["centro"], indice_proveedor(fila["proveedor"]), int(fila["documentos"]),
                    round(float(fila["toneladas"]), 3), round(float(fila["importe"]), 2),
                    round(float(fila["premios"]), 2), round(float(fila["castigos"]), 2),
                    int(fila["n_premio"]), int(fila["n_castigo"]), parametros_de(fila, False),
                ])
            # premios ya viene ordenado del más reciente al más antiguo. Del
            # documento se guardan toneladas, importe y premio: precio
            # efectivo, premio por T y precio base se derivan igual que en
            # queries.premios_calidad_sop.
            for _, g in premios.groupby(["material_codigo", "centro", "proveedor"], sort=False):
                for r in g.head(300).to_dict("records"):
                    valores = []
                    for i, c in enumerate(campos):
                        if r[c] == r[c] and r[c] is not None:
                            valores += [i, float(r[c])]
                    premios_docs.append([
                        r["fecha"].isoformat(), indice_material(r["material_codigo"], r["texto_breve"], r["categoria"]),
                        r["centro"], indice_proveedor(r["proveedor"]), str(r["ticket"]), str(r["documento"]),
                        float(r["toneladas"]), float(r["importe"]), float(r["premio"]), valores,
                    ])
        return {
            "precios": precios_rows,
            "precios_referencia": [[m, c, d.isoformat(), h.isoformat(), p] for m, c, d, h, p in q.PRECIOS_REFERENCIA_IPU],
            "materiales": [m for _, m in sorted(materiales.values(), key=lambda x: x[0])],
            "proveedores": sorted(proveedores, key=proveedores.get),
            "calidad": calidad_rows, "premios": premios_rows, "premios_docs": premios_docs,
        }
