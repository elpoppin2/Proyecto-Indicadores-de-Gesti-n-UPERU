"""Pruebas de las reglas de negocio definidas por el usuario (compras, báscula
ZMM056, ajustes de precio, calidad, premios, costo por energía, alcance de
Logística). Cada prueba fija una regla con datos de ejemplo: si un cambio la
rompe, falla aquí aunque la página siga cargando. Las reglas que viven en SQL
se evalúan en una base SQLite en memoria (no la del proyecto). Corren con las
demás:

    venv\\Scripts\\python.exe manage.py test gestion

Los valores son de ejemplo, no cifras de la empresa.
"""

import datetime
import sqlite3
from unittest import mock

import pandas as pd
from django.test import SimpleTestCase

from dashboard import queries as q
from dashboard.consultas import base, calidad, importados, reglas, sop_base
from gestion.management.commands import import_ingresos_fisicos_sop as zmm056

D = datetime.date


def _sqlite(tablas):
    """Conexión SQLite en memoria con `tablas`: {nombre: [dict, ...]}."""
    con = sqlite3.connect(":memory:")
    for nombre, filas in tablas.items():
        columnas = list(filas[0])
        con.execute(f"CREATE TABLE {nombre} ({', '.join(columnas)})")
        con.executemany(f"INSERT INTO {nombre} VALUES ({', '.join('?' * len(columnas))})",
                        [tuple(f[c] for c in columnas) for f in filas])
    return con


class ComprasTest(SimpleTestCase):
    """Categoría efectiva y filtros de las líneas de compra (ME2N)."""

    LINEA = {"id": 0, "grupo_compras": "G10", "campo_b": "", "proveedor": "1000001", "cuenta_proveedor": "PROVEEDOR",
             "doc_compra": "4500000001", "pos_doc_compra": "10", "texto_breve": "MATERIAL", "contrato": ""}

    def categoria(self, linea, categoria_articulo="", categoria_servicio=""):
        con = _sqlite({"lc": [{**self.LINEA, **linea}], "c": [{"id": 0, "categoria": categoria_articulo}],
                       "cs": [{"id": 0, "categoria_servicio": categoria_servicio}]})
        return con.execute(f"SELECT {reglas.CATEGORIA_EFECTIVA_LC} FROM lc LEFT JOIN c ON c.id = lc.id "
                           "LEFT JOIN cs ON cs.id = lc.id").fetchone()[0]

    def test_categoria_del_grupo_de_articulos_o_la_del_grupo_de_compras(self):
        self.assertEqual(self.categoria({}, "REPUESTOS", "SERVICIOS INDUSTRIALES"), "REPUESTOS")
        self.assertEqual(self.categoria({}, "", "SERVICIOS INDUSTRIALES"), "SERVICIOS INDUSTRIALES")

    def test_capex_se_disuelve_en_la_categoria_del_grupo_de_compras(self):
        self.assertEqual(self.categoria({}, "CAPEX", "SUMINISTROS INDUSTRIALES"), "SUMINISTROS INDUSTRIALES")

    def test_grupo_de_compras_sin_fila_en_la_tabla(self):
        grupo, categoria = next(iter(reglas.CATEGORIA_GRUPO_COMPRAS_SIN_TABLA.items()))
        self.assertEqual(self.categoria({"grupo_compras": grupo}, "CAPEX", ""), categoria)

    def test_pedido_con_categoria_puesta_a_mano(self):
        pedido, categoria = next(iter(reglas.CATEGORIA_POR_PEDIDO_LC.items()))
        self.assertEqual(self.categoria({"doc_compra": pedido}, "MATERIAS PRIMAS"), categoria)

    def test_fletes_de_materias_primas_salvo_las_posiciones_excluidas(self):
        flete = {"grupo_compras": reglas.GRUPO_COMPRAS_FLETES_MP, "texto_breve": "Flete de yeso"}
        self.assertEqual(self.categoria(flete, "", "SERVICIOS INDUSTRIALES"), "MATERIAS PRIMAS")
        doc, pos = reglas.FLETES_MP_EXCLUIDOS[0]
        excluida = {**flete, "doc_compra": doc, "pos_doc_compra": pos}
        self.assertEqual(self.categoria(excluida, "", "SERVICIOS INDUSTRIALES"), "SERVICIOS INDUSTRIALES")

    def test_lineas_que_no_cuentan_como_compra(self):
        def valida(linea):
            con = _sqlite({"lc": [{**self.LINEA, **linea}]})
            return bool(con.execute(f"SELECT COUNT(*) FROM lc WHERE {reglas._FILTRO_COMPRAS_VALIDAS_BASE}").fetchone()[0])
        self.assertTrue(valida({}))
        self.assertFalse(valida({"grupo_compras": "G67"}))
        self.assertFalse(valida({"campo_b": "L"}))
        self.assertFalse(valida({"proveedor": "4000002"}))
        self.assertFalse(valida({"proveedor": "", "cuenta_proveedor": ""}))

    def test_bajo_contrato_por_rango_de_pedido_o_contrato(self):
        def bajo_contrato(linea):
            con = _sqlite({"lc": [{**self.LINEA, **linea}]})
            return bool(con.execute(f"SELECT COUNT(*) FROM lc WHERE {reglas.FILTRO_BAJO_CONTRATO_LC}").fetchone()[0])
        self.assertFalse(bajo_contrato({}))
        self.assertTrue(bajo_contrato({"doc_compra": "4700000005"}))
        self.assertTrue(bajo_contrato({"doc_compra": "4000000005"}))
        self.assertTrue(bajo_contrato({"contrato": "4600000001"}))


class TrasladosYFechasTest(SimpleTestCase):
    def test_lo_recibido_en_el_almacen_de_traslado_cuenta_en_su_planta_de_destino(self):
        (centro, almacen), destino = next(iter(importados.ALMACENES_TRASLADO.items()))
        con = _sqlite({"t": [{"centro": centro, "almacen": almacen}, {"centro": centro, "almacen": "OTRO"}]})
        filas = [f[0] for f in con.execute(f"SELECT {importados._sql_centro_destino()} FROM t")]
        self.assertEqual(filas, [destino, centro])

    def test_ingreso_con_otra_fecha_solo_para_ese_material_centro_y_dia(self):
        (material, centro, fecha), nueva = next(iter(sop_base.INGRESOS_CON_OTRA_FECHA.items()))
        con = _sqlite({"t": [
            {"material_codigo": material, "centro": centro, "fecha_ingreso": fecha.isoformat()},
            {"material_codigo": material, "centro": "OTRO", "fecha_ingreso": fecha.isoformat()},
        ]})
        filas = [f[0] for f in con.execute(f"SELECT {sop_base.FECHA_INGRESO_EFECTIVA} FROM t")]
        self.assertEqual(filas, [nueva.isoformat(), fecha.isoformat()])


class ZMM056Test(SimpleTestCase):
    """Lectura de los tickets de báscula (import_ingresos_fisicos_sop)."""

    def encabezado_nuevo(self):
        # Como el robot de TIC: empieza por "Ticket Registro" (el antiguo, por "Centro Salida").
        nombres = list(zmm056._NOMBRES.values()) + list(zmm056._NOMBRES_CALIDAD.values())
        nombres.remove("Ticket Registro")
        nombres.insert(0, "Ticket Registro")
        nombres.insert(nombres.index("Nombre 1") + 1, "Otra")
        return nombres + ["Nombre 1"]  # el segundo "Nombre 1" es el del transportista

    def fila(self, encabezado, **valores):
        ix, _ = zmm056._indices(encabezado)
        fila = [None] * len(encabezado)
        for campo, valor in valores.items():
            fila[ix[campo]] = valor
        return fila

    def test_formato_nuevo_por_nombre_con_el_segundo_nombre_1_para_el_transportista(self):
        encabezado = self.encabezado_nuevo()
        ix, ix_calidad = zmm056._indices(encabezado)
        self.assertEqual(ix["proveedor"], encabezado.index("Nombre 1"))
        self.assertEqual(ix["transportista"], len(encabezado) - 1)
        self.assertIn("pcs", ix_calidad.values())

    def test_formato_desconocido_avisa_las_columnas_que_faltan(self):
        with self.assertRaises(Exception) as error:
            zmm056._indices(["Ticket Registro", "Material"])
        self.assertIn("Centro Salida", str(error.exception))

    def test_kilos_a_toneladas_anulados_fuera_y_analisis_en_cero_sin_dato(self):
        encabezado = self.encabezado_nuevo()
        ix, ix_calidad = zmm056._indices(encabezado)
        fila = self.fila(encabezado, centro="U001", ticket="T1", estado="PRO", material="M1", humedo=25500,
                         seco=24000, fecha=D(2026, 1, 5))
        fila[[i for i, c in ix_calidad.items() if c == "pcs"][0]] = 6500
        fila[[i for i, c in ix_calidad.items() if c == "cenizas"][0]] = 0
        datos = zmm056._ticket(fila, ix, ix_calidad, "ATOCONGO")
        self.assertAlmostEqual(float(datos["cantidad"]), 25.5)
        self.assertAlmostEqual(datos["cantidad_seca"], 24.0)
        self.assertEqual(datos["pcs"], 6500.0)
        self.assertIsNone(datos["cenizas"])
        fila[ix["estado"]] = zmm056._ESTADO_TICKET_ANULADO
        self.assertIsNone(zmm056._ticket(fila, ix, ix_calidad, "ATOCONGO"))
        fila[ix["estado"]] = "REG"  # pesado pero sin documento en SAP: sí cuenta
        self.assertIsNotNone(zmm056._ticket(fila, ix, ix_calidad, "ATOCONGO"))


class AjustesYCalidadTest(SimpleTestCase):
    def test_concepto_de_cada_ajuste_de_la_fbl3n(self):
        self.assertEqual(q.concepto_ajuste("PR", "", ""), "Cambio de precio (MR22)")
        self.assertEqual(q.concepto_ajuste("RE", "MIRO", "Flete terrestre"), "Flete / transporte")
        self.assertEqual(q.concepto_ajuste("RE", "MIRO", "DUA importación"), "Cargos de importación")
        self.assertEqual(q.concepto_ajuste("RE", "MR8M", ""), "Anulación de factura (MR8M)")
        self.assertEqual(q.concepto_ajuste("SA", "FB50", ""), "Asiento contable manual")

    def test_calidad_ponderada_por_toneladas_y_cada_muestra_una_vez(self):
        df = pd.DataFrame({"pcs": [6000.0, 6000.0, 5000.0, None], "toneladas": [10.0, 10.0, 20.0, 5.0],
                           "numero_muestra": ["A", "A", "B", "C"]})
        self.assertAlmostEqual(q.promedio_calidad(df, "pcs"), 5500.0)
        r = q.resumen_calidad(df, "pcs")
        self.assertEqual(r["muestras"], 2)  # la muestra A cubre dos tickets
        self.assertEqual((r["minimo"], r["maximo"]), (5000.0, 6000.0))

    def test_premios_y_castigos_sobre_el_precio_base(self):
        df = pd.DataFrame({"toneladas": [10.0, 10.0], "importe": [1050.0, 980.0], "premio": [50.0, -20.0]})
        r = q.resumen_premios(df)
        self.assertEqual((r["premios"], r["castigos"], r["neto"]), (50.0, -20.0, 30.0))
        self.assertAlmostEqual(r["precio_base"], 100.0)
        self.assertAlmostEqual(r["precio_efectivo"], 101.5)


class EnergiaTest(SimpleTestCase):
    """Costo por energía de los carbones (reglas confirmadas por el usuario el 2026-11-10)."""

    TRUJILLO, CDC = next(iter(calidad.PCS_DE_OTRO_CENTRO))
    ATOCONGO = calidad.PCS_DE_OTRO_CENTRO[(TRUJILLO, CDC)]

    def ticket(self, material, centro, fecha, toneladas, importe, pcs, premio=0.0):
        return {"fecha": fecha, "material_codigo": material, "centro": centro, "proveedor": "P", "ticket": "t",
                "documento": "d", "premio": premio, "toneladas": toneladas, "importe": importe, "pcs": pcs,
                "cenizas": None, "texto_breve": material, "categoria": "COMBUSTIBLES"}

    def calcular(self, tickets, precios, analisis=None, fijos=None):
        barcos = pd.DataFrame({"material_codigo": ["IMP"], "pedido": ["P1"], "llegada": [D(2026, 1, 10)]})
        with mock.patch.object(calidad, "premios_calidad_sop", return_value=pd.DataFrame(tickets)), \
                mock.patch.object(calidad, "precios_entrada_sop_real", return_value=pd.DataFrame(precios)), \
                mock.patch.object(calidad, "calidad_ingresos_sop", return_value=pd.DataFrame(analisis or tickets)), \
                mock.patch.object(calidad, "embarques_importados", return_value=(barcos, None)), \
                mock.patch.object(calidad, "_query", return_value=pd.DataFrame({"pedido": ["P1"], "nombre": ["NAVIERA"]})), \
                mock.patch.object(calidad, "regla_local", return_value=fijos or {}):
            return calidad.ingresos_energia_sop()

    def precio(self, material, centro, mes, toneladas, importe_mb51, ajuste, entradas=1):
        return {"material_codigo": material, "centro": centro, "mes": mes, "toneladas": toneladas, "ajuste": ajuste,
                "importe_mb51": importe_mb51, "importe": importe_mb51 + ajuste, "entradas": entradas,
                "texto_breve": material, "categoria": "COMBUSTIBLES"}

    def test_gj_y_valores_agregados_repartidos_por_toneladas(self):
        enero = D(2026, 1, 1)
        df = self.calcular([self.ticket("M1", "U001", D(2026, 1, 5), 10.0, 1000.0, 6000.0)],
                           [self.precio("M1", "U001", enero, 100.0, 10000.0, 500.0)])
        fila = df.iloc[0]
        self.assertAlmostEqual(fila["gj"], 10 * 6000 * 4.1868 / 1000)
        self.assertAlmostEqual(fila["ajuste"], 50.0)  # 5 por T de los ajustes del mes
        self.assertAlmostEqual(fila["importe"], 1050.0)
        self.assertAlmostEqual(fila["costo_gj"], 1050.0 / fila["gj"])

    def test_resumen_suma_contrato_mas_valores_agregados_mas_calidad(self):
        enero = D(2026, 1, 1)
        df = self.calcular([self.ticket("M1", "U001", D(2026, 1, 5), 10.0, 1030.0, 6000.0, premio=30.0)],
                           [self.precio("M1", "U001", enero, 10.0, 1030.0, 70.0)])
        r = q.resumen_energia(df)
        self.assertAlmostEqual(r["costo_gj_base"] + r["ajuste_gj"] + r["premio_gj"], r["costo_gj"])
        self.assertAlmostEqual(r["precio_base"], 100.0)
        self.assertAlmostEqual(r["pcs"], 6000.0)

    def test_trujillo_de_condorcocha_con_el_pcs_de_atocongo_del_mes_o_de_su_historia(self):
        enero, febrero = D(2026, 1, 1), D(2026, 2, 1)
        atocongo = [self.ticket(self.TRUJILLO, self.ATOCONGO, D(2026, 1, 3), 10.0, 1.0, 6000.0),
                    self.ticket(self.TRUJILLO, self.ATOCONGO, D(2026, 1, 9), 30.0, 1.0, 6600.0)]
        cdc = [self.ticket(self.TRUJILLO, self.CDC, D(2026, 1, 20), 5.0, 500.0, 5000.0),
               self.ticket(self.TRUJILLO, self.CDC, D(2026, 3, 2), 5.0, 500.0, None)]
        df = self.calcular(atocongo + cdc, [self.precio(self.TRUJILLO, self.CDC, febrero, 1.0, 1.0, 0.0)])
        pcs = df[df["centro"] == self.CDC].set_index("fecha")["pcs"]
        self.assertAlmostEqual(pcs[D(2026, 1, 20)], 6450.0)  # promedio ponderado de Atocongo en enero
        self.assertAlmostEqual(pcs[D(2026, 3, 2)], 6450.0)  # marzo sin análisis: el de toda su historia
        self.assertNotIn(enero, list(df["fecha"]))

    def test_importado_con_pcs_fijo_y_sus_llegadas_a_planta(self):
        enero = D(2026, 1, 1)
        df = self.calcular([self.ticket("M1", "U001", D(2026, 1, 5), 10.0, 1000.0, 6000.0)],
                           [self.precio("IMP", "U002", enero, 200.0, 100000.0, 2000.0, entradas=7)],
                           fijos={"IMP": 6400})
        imp = df[df["material_codigo"] == "IMP"].iloc[0]
        self.assertEqual((imp["pcs"], imp["documentos"], imp["proveedor"]), (6400.0, 7, "NAVIERA"))
        self.assertAlmostEqual(imp["importe"], 102000.0)
        self.assertEqual(q.resumen_energia(df)["documentos"], 8)


class PlanificacionTest(SimpleTestCase):
    def test_mes_por_defecto_es_el_actual_si_esta_en_el_plan(self):
        from gestion.vistas import plan_cumplimiento
        hoy = datetime.date.today().replace(day=1)
        meses = [q._sumar_meses(hoy, i) for i in (-2, -1, 0, 1, 2)]
        with mock.patch.object(plan_cumplimiento.q, "meses_planificacion_disponibles", return_value=meses):
            self.assertEqual(plan_cumplimiento._meses_plan()[1], hoy)
            # Con una emisión del S&OP, solo sus meses desde la emisión.
            self.assertEqual(plan_cumplimiento._meses_plan(meses[3])[0], meses[3:])

    def test_alcance_de_logistica_pares_y_meses_excluidos(self):
        df = pd.DataFrame({"material_codigo": ["A", "A", "B"], "centro": ["U1", "U1", "U1"],
                           "mes": [D(2026, 8, 1), D(2026, 9, 1), D(2026, 9, 1)]})
        with base.alcance_materiales([("A", "U1")], excluir=[("A", "U1", D(2026, 9, 1))]):
            filtrado = base._en_alcance(df)
        self.assertEqual(list(filtrado["mes"]), [D(2026, 8, 1)])
        self.assertEqual(len(base._en_alcance(df)), 3)  # fuera del alcance, todo

    def test_cobertura_con_consumo_minimo_solo_meses_de_produccion(self):
        from gestion.vistas import plan_consumo as pc
        # Salidas: meses de producción (40, 60) entre meses chicos (2, 3, 1).
        flujo, finales = [40.0, 2.0, 3.0, 60.0, 1.0], [100.0, 98.0, 95.0, 35.0, 34.0]
        minimos, maximos = [25.0] * 5, [150.0] * 5
        with mock.patch.object(pc.q, "regla_local", return_value={}):
            sin = pc._fila_cobertura("M1", "CALIZA", "U1", 120.0, finales, flujo, minimos, maximos)
        with mock.patch.object(pc.q, "regla_local", return_value={"M1|U1": 10}):
            con = pc._fila_cobertura("M1", "CALIZA", "U1", 120.0, finales, flujo, minimos, maximos)
            ajena = pc._fila_cobertura("M2", "OTRO", "U1", 120.0, finales, flujo, minimos, maximos)
        self.assertIsNone(sin["consumo_minimo"])
        self.assertAlmostEqual(sin["celdas"][0]["cobertura"], 100 / ((2 + 3 + 60) / 3))
        self.assertEqual(con["consumo_minimo"], 10)
        # Ventana de los 3 meses siguientes: solo entra el de producción (60).
        self.assertAlmostEqual(con["celdas"][0]["cobertura"], 100 / 60)
        # Sin producción en los siguientes: promedio de los meses de producción del horizonte (50).
        self.assertAlmostEqual(con["celdas"][3]["cobertura"], 35 / 50)
        self.assertAlmostEqual(con["cobertura_inicial"], 120 / 40)  # primeros 3 meses: solo el 40
        self.assertAlmostEqual(con["minimo_meses"], 25 / 50)
        # Otro material, sin regla: igual que antes.
        self.assertEqual(ajena["celdas"], sin["celdas"])


class ReglasYMetasTest(SimpleTestCase):
    """Página Reglas y metas: guarda en archivos temporales (no en los reales)."""

    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        from gestion import ejecutivo
        from gestion.vistas import reglas as vista
        self.vista, self.json = vista, json
        carpeta = Path(tempfile.mkdtemp())
        self.reglas = carpeta / "reglas.json"
        self.metas = carpeta / "metas.json"
        self.reglas.write_text(json.dumps({"metas_categoria": {"REPUESTOS": {"otif_pct": {"2026": 80}}},
                                           "pcs_fijo_energia": {"M1": 6400}}), encoding="utf-8")
        self.metas.write_text(json.dumps({"metas": {"otif_pct": {"meta": 90, "sentido": "mayor", "tolerancia": 10,
                                                                 "confirmada": False}},
                                          "supuestos": {"otif_dias_tolerancia": 5}}), encoding="utf-8")
        for p in (mock.patch.object(q, "_ARCHIVO_REGLAS_LOCALES", self.reglas),
                  mock.patch.object(ejecutivo, "_RUTA_METAS", self.metas),
                  mock.patch.object(vista, "_categorias_disponibles", return_value=["REPUESTOS", "EMPAQUES"])):
            p.start()
            self.addCleanup(p.stop)

    def guardar(self, campos):
        from django.test import RequestFactory
        return self.vista._guardar(RequestFactory().post("/", campos), 2026)

    def leer(self, ruta):
        return self.json.loads(ruta.read_text(encoding="utf-8"))

    def test_numeros_con_coma_de_miles_y_vacio_sin_dato(self):
        self.assertEqual(self.vista._numero("12,345,678"), 12345678)
        self.assertEqual(self.vista._numero("2.5"), 2.5)
        self.assertIsNone(self.vista._numero(" "))

    def test_meta_por_categoria_nueva_cambiada_y_borrada(self):
        cambios = self.guardar({"cat|EMPAQUES|pmp_dias": "45", "cat|REPUESTOS|otif_pct": "",
                                "cat|REPUESTOS|ipu_pct": ""})
        metas = self.leer(self.reglas)["metas_categoria"]
        self.assertEqual(metas, {"EMPAQUES": {"pmp_dias": {"2026": 45}}})
        self.assertEqual(len(cambios), 2)
        self.assertEqual(self.leer(self.reglas)["pcs_fijo_energia"], {"M1": 6400})  # lo demás queda igual

    def test_metas_de_la_portada_y_confirmacion(self):
        self.guardar({"meta|otif_pct|meta": "92", "meta|otif_pct|presente": "1", "meta|otif_pct|confirmada": "on",
                      "supuesto|otif_dias_tolerancia": "7"})
        metas = self.leer(self.metas)
        self.assertEqual((metas["metas"]["otif_pct"]["meta"], metas["metas"]["otif_pct"]["confirmada"]), (92, True))
        self.assertEqual(metas["supuestos"]["otif_dias_tolerancia"], 7)

    def test_un_valor_invalido_no_guarda_nada(self):
        antes = self.reglas.read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            self.guardar({"cat|EMPAQUES|pmp_dias": "45", "pcs|M1": "siete mil"})
        self.assertEqual(self.reglas.read_text(encoding="utf-8"), antes)


class EscenariosSopTest(SimpleTestCase):
    """Simulación de compras y traslados (gestion/escenarios_sop.py): partición
    del abastecimiento, valores que van al archivo y escritura del Excel."""

    def setUp(self):
        from gestion import escenarios_sop
        self.e = escenarios_sop
        self.mes = D(2027, 1, 1)
        self.base = pd.DataFrame([
            # Material comprado en Atocongo que además envía a Condorcocha (regla del S&OP).
            {"material_codigo": "06-00000003", "centro": "U001", "mes": self.mes, "abastecimiento": 100.0,
             "traslado": 40.0, "traslado_recibido": 0.0, "texto_breve": "M1"},
            {"material_codigo": "06-00000003", "centro": "U002", "mes": self.mes, "abastecimiento": 40.0,
             "traslado": 0.0, "traslado_recibido": 40.0, "texto_breve": "M1"},
            {"material_codigo": "07-00000001", "centro": "U001", "mes": self.mes, "abastecimiento": 25.0,
             "traslado": 0.0, "traslado_recibido": 0.0, "texto_breve": "M2"},
        ])

    def test_compras_y_traslado_recibido_por_celda_y_pares(self):
        celdas = self.e.movimientos_por_celda(self.base)
        self.assertEqual(celdas[("06-00000003", "U002", self.mes)], {"compras": 0.0, "recibido": 40.0, "traslado": 0.0})
        self.assertEqual(celdas[("06-00000003", "U001", self.mes)]["traslado"], 40.0)
        de_quien, a_quien = self.e.pares_traslado(self.base)
        self.assertEqual(de_quien, {("06-00000003", "U002"): "U001"})
        self.assertEqual(a_quien, {("06-00000003", "U001"): "U002"})

    def test_solo_traslado_y_compra_o_traslado(self):
        # Caliza Roca Extraída de Atocongo: todo lo que llega es traslado desde U002.
        # Carbón Trujillo de Condorcocha: compra o traslado desde Atocongo, aunque el S&OP no traiga traslado.
        fila = {"mes": self.mes, "traslado": 0.0, "traslado_recibido": 0.0, "texto_breve": "M"}
        base = pd.DataFrame([
            {**fila, "material_codigo": "14-00000024", "centro": "U001", "abastecimiento": 30.0},
            {**fila, "material_codigo": "06-00001601", "centro": "U001", "abastecimiento": 1.0},
            {**fila, "material_codigo": "06-00001601", "centro": "U002", "abastecimiento": 6.0},
        ])
        celdas = self.e.movimientos_por_celda(base)
        self.assertEqual(celdas[("14-00000024", "U001", self.mes)], {"compras": 0.0, "recibido": 30.0, "traslado": 0.0})
        self.assertEqual(celdas[("06-00001601", "U002", self.mes)], {"compras": 6.0, "recibido": 0.0, "traslado": 0.0})
        de_quien, a_quien = self.e.pares_traslado(base)
        self.assertEqual(de_quien, {("14-00000024", "U001"): "U002", ("06-00001601", "U002"): "U001"})
        self.assertEqual(a_quien, {("06-00001601", "U001"): "U002"})

    def test_compra_guardada_en_lo_que_llega_solo_por_traslado(self):
        # Escenario guardado cuando la Caliza Roca Extraída de Atocongo era compra: pasa a su traslado recibido;
        # si la celda ya tenía traslado, vale ese.
        cambios = self.e._limpiar_cambios({
            "compras": {"14-00000024|U001|2027-01": {"base": 50, "valor": 30}, "14-00000024|U001|2027-02": {"base": 50, "valor": 30},
                        "07-00000001|U001|2027-01": {"base": 25, "valor": 30}},
            "traslados": {"14-00000024|U002|U001|2027-02": {"base": 50, "valor": 35}}})
        self.assertEqual(cambios, {
            "compras": {"07-00000001|U001|2027-01": {"base": 25.0, "valor": 30.0}},
            "traslados": {"14-00000024|U002|U001|2027-01": {"base": 50.0, "valor": 30.0},
                          "14-00000024|U002|U001|2027-02": {"base": 50.0, "valor": 35.0}}})

    def test_el_archivo_recibe_compras_mas_traslado(self):
        escenario = {"cambios": {
            "compras": {"07-00000001|U001|2027-01": {"base": 25.0, "valor": 30.0}},
            "traslados": {"06-00000003|U001|U002|2027-01": {"base": 40.0, "valor": 45.0}}}}
        objetivo = self.e._valores_objetivo(escenario, self.e.movimientos_por_celda(self.base))
        self.assertEqual(objetivo, {("07-00000001", "U001", self.mes): 30.0, ("06-00000003", "U002", self.mes): 45.0})
        self.assertEqual(self.e._conflictos(escenario, self.e.movimientos_por_celda(self.base)), [])
        escenario["cambios"]["compras"]["07-00000001|U001|2027-01"]["base"] = 20.0
        self.assertEqual(self.e._conflictos(escenario, self.e.movimientos_por_celda(self.base)), ["07-00000001|U001|2027-01"])

    def test_cambios_no_validos(self):
        with self.assertRaises(self.e.ErrorEscenario):
            self.e._limpiar_cambios({"compras": {"07-00000001|U001|2027-01": {"base": 1, "valor": "mucho"}}})
        with self.assertRaises(self.e.ErrorEscenario):
            self.e._limpiar_cambios({"compras": {"sin formato": {"base": 1, "valor": 2}}})

    def test_escritura_del_excel_con_fila_nueva_y_fila_fusionada(self):
        import os
        import tempfile
        import openpyxl
        ruta = os.path.join(tempfile.mkdtemp(), "Abastecimiento SOP - Enero 2027.xlsx")
        libro = openpyxl.Workbook()
        hoja = libro.active
        hoja.append(["Cod. SAP", "Centro Destino", "Material", "Destino", datetime.datetime(2027, 1, 1)])
        hoja.append(["07-00000001 ", "U001", "M2", "ATOCONGO", 25])
        # Carbón Terceros y su Chancado van a la misma fila de la proyección.
        hoja.append(["06-00000005", "U002", "M3", "CONDORCOCHA", 10])
        hoja.append(["14-00001112", "U002", "M4", "CONDORCOCHA", 4])
        libro.save(ruta)
        self.e._escribir_abastecimiento(ruta, {
            ("07-00000001", "U001", self.mes): 30.0, ("06-00000005", "U002", self.mes): 20.0,
            ("07-00000009", "U002", self.mes): 7.5}, {("07-00000009", "U002"): "M5"})
        filas = list(openpyxl.load_workbook(ruta).active.iter_rows(min_row=2, values_only=True))
        self.assertEqual(filas[0][4], 30)
        self.assertEqual((filas[1][4], filas[2][4]), (16, 4))  # 20 en total: 16 + los 4 del chancado
        self.assertEqual(filas[3], ("07-00000009", "U002", "M5", "CONDORCOCHA", 7.5))

    def test_archivo_de_traslados(self):
        # Van al archivo los traslados con los dos centros en la proyección (Carbón Importado por regla, Carbón
        # Trujillo compra o traslado); la Caliza Roca Extraída llega de un centro que el S&OP no planifica.
        import os
        import tempfile
        from gestion.management.commands._sop_excel import leer_matriz_mensual
        fila = {"mes": self.mes, "abastecimiento": 1.0, "traslado": 0.0, "traslado_recibido": 0.0, "texto_breve": "M"}
        base = pd.DataFrame([{**fila, "material_codigo": m, "centro": c} for m, c in (
            ("06-00000003", "U001"), ("06-00000003", "U002"), ("06-00001601", "U001"), ("06-00001601", "U002"),
            ("14-00000024", "U001"))])
        self.assertEqual(self.e.pares_archivo_traslados(base),
                         {("06-00000003", "U001"): "U002", ("06-00001601", "U001"): "U002"})
        # Creado con el formato de la corrida, se escribe un traslado que ya tiene fila y otro nuevo.
        febrero = D(2027, 2, 1)
        ruta = os.path.join(tempfile.mkdtemp(), "Traslados SOP - Enero 2027.xlsx")
        self.e._libro_traslados(ruta, "Traslados SOP - Enero 2027", [self.mes, febrero],
                                [("06-00000003", "U001", "M1", "U002", [40.0, 0.0])], ["nota"])
        self.e._escribir_traslados(ruta, {("06-00000003", "U001", "U002", febrero): 12.5,
                                          ("06-00001601", "U001", "U002", self.mes): 3.0}, {("06-00001601", "U001"): "M6"})
        celdas = {(c["material_codigo"], c["centro"], c["destino"], c["mes"]): float(c["valor"])
                  for c in leer_matriz_mensual(ruta) if c["valor"] is not None}
        self.assertEqual(celdas, {("06-00000003", "U001", "U002", self.mes): 40.0,
                                  ("06-00000003", "U001", "U002", febrero): 12.5,
                                  ("06-00001601", "U001", "U002", self.mes): 3.0})


class MovimientoHistoriaYCopiasTest(SimpleTestCase):
    """Meses cerrados del Movimiento de inventario por material y estado de las copias HTML al aplicar."""

    def test_historia_reparte_ingresos_y_cuadra_con_otros(self):
        from gestion.vistas import plan_consumo as pc
        m1, m2 = D(2026, 8, 1), D(2026, 9, 1)
        cierres = pd.DataFrame([{"material_codigo": "M1", "centro": "U1", "mes": m, "stock": s}
                                for m, s in ((D(2026, 7, 1), 100.0), (m1, 120.0), (m2, 90.0))])
        movs = pd.DataFrame([
            {"material_codigo": "M1", "centro": "U1", "mes": m1, "tipo": "ingreso", "contraparte": "", "cantidad": 50.0},
            {"material_codigo": "M1", "centro": "U1", "mes": m1, "tipo": "ingreso", "contraparte": "U2", "cantidad": 10.0},
            {"material_codigo": "M1", "centro": "U1", "mes": m1, "tipo": "consumo", "contraparte": "", "cantidad": 35.0},
            {"material_codigo": "M1", "centro": "U1", "mes": m2, "tipo": "envio", "contraparte": "U2", "cantidad": 30.0},
            {"material_codigo": "M9", "centro": "U1", "mes": m2, "tipo": "consumo", "contraparte": "", "cantidad": 99.0},
        ])
        h = pc._historia_movimiento(cierres, movs, [m1, m2], {("M1", "U1")}, set(cierres["mes"]))[("M1", "U1")]
        self.assertEqual((h[0]["inicial"], h[0]["compras"], h[0]["recibido"], h[0]["consumo"], h[0]["final"]),
                         (100.0, 50.0, 10.0, 35.0, 120.0))
        self.assertAlmostEqual(h[0]["otros"], -5.0)  # 100 + 50 + 10 − 35 = 125 contra 120 de cierre
        self.assertEqual((h[1]["inicial"], h[1]["traslado"], h[1]["otros"]), (120.0, 30.0, 0.0))
        self.assertEqual(pc._historia_movimiento(cierres, movs, [], {("M1", "U1")}, set()), {})

    def test_copias_estado_vencido_y_sin_destino(self):
        import os
        import tempfile
        from pathlib import Path
        from gestion import copias_html
        with tempfile.TemporaryDirectory() as carpeta:
            estado, config = Path(carpeta) / "estado.json", Path(carpeta) / "config.json"
            with mock.patch.object(copias_html, "ESTADO", estado), mock.patch.object(copias_html, "CONFIG", config):
                self.assertEqual(copias_html.regenerar("prueba")["estado"], "sin_destino")
                viejo = (datetime.datetime.now() - datetime.timedelta(hours=2)).isoformat(timespec="seconds")
                estado.write_text(f'{{"estado": "actualizando", "inicio": "{viejo}"}}', encoding="utf-8")
                self.assertEqual(copias_html.estado()["estado"], "error")
                # Lanzada a mano después de una corrida vieja: mientras corre no se da por vencida.
                config.write_text('{"planificacion": "p.html", "sitio": "s"}', encoding="utf-8")
                estado.write_text(f'{{"estado": "listo", "inicio": "{viejo}", "fin": "{viejo}"}}', encoding="utf-8")
                vistos = []
                with mock.patch.object(copias_html, "call_command", lambda *a, **k: vistos.append(copias_html.estado()["estado"])):
                    copias_html.correr()
                self.assertEqual(vistos, ["actualizando", "actualizando"])
                self.assertEqual(copias_html.estado()["estado"], "listo")
                # Lanzada desde el servidor (DJANGO_DEBUG=False): el proceso de las copias corre con DEBUG, como la
                # tarea diaria (sin eso los estáticos salen versionados y las copias no encuentran zonas.js).
                with mock.patch.object(copias_html, "REGISTRO", Path(carpeta) / "copias.log"), \
                        mock.patch.dict(os.environ, {"DJANGO_DEBUG": "False"}), \
                        mock.patch.object(copias_html.subprocess, "Popen") as popen:
                    copias_html.regenerar("prueba")
                self.assertEqual(popen.call_args.kwargs["env"]["DJANGO_DEBUG"], "True")
