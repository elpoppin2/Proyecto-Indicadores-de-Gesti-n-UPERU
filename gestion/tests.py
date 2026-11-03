"""Pruebas de la lógica que no depende de los datos cargados: formatos, fechas,
semáforo contra metas, condiciones de pago, el compactado del HTML guardado, el
inventario valorizado a precio promedio y la plantilla de precios S&OP.
No usan la base de datos (corren en segundos):

    venv\\Scripts\\python.exe manage.py test gestion

Lo que sí depende de los datos se revisa con dos comandos propios:
verificar_calculos (cifras de los meses cerrados contra la foto local) y
revisar_paginas (todas las páginas, filtros, detalles y descargas). Los
valores de estas pruebas son de ejemplo, no cifras de la empresa.
"""

import datetime

from dashboard import queries as q
from django.test import SimpleTestCase

from gestion import ejecutivo
from gestion.templatetags import dashboard_extras as filtros
from gestion.vistas.comun import compactar_html


class FormatosTest(SimpleTestCase):
    def test_numero(self):
        self.assertEqual(filtros.numero(1234.567, 1), "1,234.6")
        self.assertEqual(filtros.numero(-0.04, 1), "0.0")  # sin "-0.0"
        self.assertEqual(filtros.numero(None), "")
        self.assertEqual(filtros.numero(float("nan")), "")

    def test_soles_abreviados(self):
        self.assertEqual(filtros.soles_mm(12_340_000), "S/ 12.3 MM")
        self.assertEqual(filtros.soles_mm(450_000), "S/ 450 K")
        self.assertEqual(filtros.soles_mm(-0.4), "S/ 0")  # sin "−S/ 0"
        self.assertEqual(filtros.soles_mm(None), "—")

    def test_nombre_visible(self):
        self.assertEqual(filtros.nombre_visible("MATERIAS PRIMAS"), "Materias primas")
        self.assertEqual(filtros.nombre_visible("PLANIFICACIÓN S&OP"), "Planificación S&OP")

    def test_corte_de_inventario(self):
        self.assertEqual(filtros.corte(datetime.date(2026, 9, 30)), "Septiembre - 2026")
        self.assertEqual(filtros.corte(datetime.date(2026, 10, 4)), "Octubre - 2026 (al 04/10)")

    def test_nombre_de_centro(self):
        self.assertEqual(filtros.centro("U001"), "ATOCONGO")
        self.assertEqual(filtros.centro("U004"), "U004")


class FechasTest(SimpleTestCase):
    def test_sumar_meses(self):
        self.assertEqual(q._sumar_meses(datetime.date(2026, 11, 15), 3), datetime.date(2027, 2, 1))
        self.assertEqual(q._sumar_meses(datetime.date(2026, 1, 1), -1), datetime.date(2025, 12, 1))

    def test_limites_de_mes_bisiesto(self):
        self.assertEqual(q._limites_mes(datetime.date(2028, 2, 10)),
                         (datetime.date(2028, 2, 1), datetime.date(2028, 2, 29)))

    def test_mes_de_una_fecha(self):
        self.assertEqual(q._mes_fecha("2026-10-29"), datetime.date(2026, 10, 1))
        self.assertEqual(q._mes_fecha(datetime.datetime(2026, 10, 29, 8)), datetime.date(2026, 10, 1))


class SemaforoTest(SimpleTestCase):
    def test_mayor_es_mejor_con_tolerancia(self):
        cfg = {"meta": 50, "sentido": "mayor", "tolerancia": 5}
        self.assertEqual(ejecutivo._estado(52, cfg), "ok")
        self.assertEqual(ejecutivo._estado(47, cfg), "aviso")
        self.assertEqual(ejecutivo._estado(40, cfg), "alerta")

    def test_menor_es_mejor_sin_tolerancia(self):
        cfg = {"meta": 30, "sentido": "menor"}
        self.assertEqual(ejecutivo._estado(30, cfg), "ok")
        self.assertEqual(ejecutivo._estado(31, cfg), "alerta")

    def test_sin_dato_o_sin_meta(self):
        self.assertEqual(ejecutivo._estado(None, {"meta": 1, "sentido": "mayor"}), "neutro")
        self.assertEqual(ejecutivo._estado(5, None), "neutro")

    def test_texto_de_la_meta(self):
        formato = lambda v: f"{v}%"  # noqa: E731
        self.assertEqual(ejecutivo._texto_meta({"meta": 50, "sentido": "mayor"}, formato), "Meta ≥ 50% (propuesta)")
        self.assertEqual(ejecutivo._texto_meta({"meta": 30, "sentido": "menor", "confirmada": True}, formato),
                         "Meta ≤ 30%")


class CondicionPagoTest(SimpleTestCase):
    def test_dias_de_condiciones_simples(self):
        self.assertEqual(q.dias_ponderados_condicion_pago(" f030 "), 30.0)
        self.assertEqual(q.dias_ponderados_condicion_pago("N045"), 45.0)

    def test_condiciones_sin_plazo(self):
        self.assertEqual(q.dias_ponderados_condicion_pago("C000"), 0.0)
        self.assertEqual(q.dias_ponderados_condicion_pago(""), 0.0)
        self.assertEqual(q.dias_ponderados_condicion_pago(None), 0.0)


class CompactarHtmlTest(SimpleTestCase):
    def test_tabla_sin_espacios_entre_etiquetas(self):
        html = "<table>\n  <tr>\n    <td>1</td>\n    <td>2</td>\n  </tr>\n</table>"
        self.assertEqual(compactar_html(html), "<table><tr><td>1</td><td>2</td></tr></table>")

    def test_elementos_en_linea_conservan_su_separacion(self):
        self.assertEqual(compactar_html("<p><b>a</b>\n      <i>b</i></p>"), "<p><b>a</b>\n<i>b</i></p>")

    def test_no_toca_el_texto_de_las_celdas(self):
        html = "<td>  dos  espacios  </td>"
        self.assertEqual(compactar_html(html), html)


class InventarioValorizadoTest(SimpleTestCase):
    """Simulación: inventario valorizado a precio promedio móvil (valores de ejemplo)."""

    def test_un_mes_a_precio_promedio(self):
        # 100 en stock a 10 + 100 comprados a 12 -> promedio 11; quedan 120.
        pmp, valor = q.valorizar_mes(100, 1000, 100, 1200, 120)
        self.assertAlmostEqual(pmp, 11.0)
        self.assertAlmostEqual(valor, 1320.0)

    def test_quiebre_vale_cero_y_sin_stock_no_hay_promedio(self):
        self.assertEqual(q.valorizar_mes(10, 100, 0, 0, -5), (10.0, 0.0))
        self.assertEqual(q.valorizar_mes(0, 0, 0, 0, 0), (None, 0.0))

    def test_traslado_entra_al_promedio_del_que_envia_mas_flete(self):
        import pandas as pd
        from dashboard.consultas.simulacion import _valorizar

        mes1, mes2 = datetime.date(2026, 10, 1), datetime.date(2026, 11, 1)
        base = {"texto_breve": "CARBON", "categoria": "COMBUSTIBLES", "familia": ""}
        filas = [
            # El que envía: compra 100 a 12 y manda 50 al que recibe.
            {**base, "material_codigo": "06-00000003", "centro": "U001", "mes": mes1, "stock_inicial": 100.0,
             "abastecimiento": 100.0, "consumo": 50.0, "traslado": 50.0, "traslado_recibido": 0.0,
             "stock_final": 100.0, "valor_partida": 1000.0},
            {**base, "material_codigo": "06-00000003", "centro": "U002", "mes": mes1, "stock_inicial": 0.0,
             "abastecimiento": 50.0, "consumo": 20.0, "traslado": 0.0, "traslado_recibido": 50.0,
             "stock_final": 30.0, "valor_partida": 0.0},
            # Mes 2: compra sin precio en la versión -> al promedio vigente.
            {**base, "material_codigo": "06-00000003", "centro": "U001", "mes": mes2, "stock_inicial": 100.0,
             "abastecimiento": 10.0, "consumo": 10.0, "traslado": 0.0, "traslado_recibido": 0.0,
             "stock_final": 100.0, "valor_partida": 1000.0},
        ]
        precios = {("06-00000003", "U001", mes1): 12.0, ("06-00000003", "U002", mes1): 15.0}
        r = _valorizar(pd.DataFrame(filas), precios).set_index(["centro", "mes"])
        self.assertAlmostEqual(r.loc[("U001", mes1), "pmp"], 11.0)
        self.assertAlmostEqual(r.loc[("U001", mes1), "valor_final"], 1100.0)
        # 50 recibidos a 11 (promedio del que envía) + 3 de flete (15 − 12).
        self.assertAlmostEqual(r.loc[("U002", mes1), "valor_recibido"], 700.0)
        self.assertAlmostEqual(r.loc[("U002", mes1), "valor_final"], 420.0)
        self.assertTrue(r.loc[("U001", mes2), "sin_precio"])
        self.assertAlmostEqual(r.loc[("U001", mes2), "valor_final"], 1100.0)
        fila = r.loc[("U001", mes1)]
        self.assertAlmostEqual(fila.valor_inicial + fila.valor_compras + fila.valor_recibido - fila.valor_salidas,
                               fila.valor_final)


class PlantillaPreciosTest(SimpleTestCase):
    """La plantilla 'Precios SOP' se lee igual que se escribe; el Supuesto del Excel anterior."""

    def test_ida_y_vuelta(self):
        import os
        import tempfile

        from gestion.management.commands._precios_sop_excel import leer_plantilla
        from gestion.precios_sop import escribir_plantilla

        set26, oct26, nov26 = datetime.date(2026, 9, 1), datetime.date(2026, 10, 1), datetime.date(2026, 11, 1)
        version = {"mes": oct26, "origen": "prueba", "meses_reales": [set26], "meses_proyectados": [oct26, nov26],
                   "filas": [{"material_codigo": "07-00000001", "centro": "U001", "categoria": "MATERIAS PRIMAS",
                              "descripcion": "CALIZA", "unidad": "T", "reales": {set26: 50.0},
                              "proyectados": {oct26: 51.25, nov26: None}, "supuesto": "Contrato"}]}
        ruta = os.path.join(tempfile.mkdtemp(), "Precios SOP - Octubre 2026.xlsx")
        escribir_plantilla(ruta, version)
        celdas = {c["mes"]: c for c in leer_plantilla(ruta)}
        self.assertEqual(set(celdas), {set26, oct26, nov26})
        self.assertAlmostEqual(float(celdas[oct26]["precio"]), 51.25)
        self.assertIsNone(celdas[nov26]["precio"])
        self.assertEqual((celdas[oct26]["centro"], celdas[oct26]["supuesto"]), ("U001", "Contrato"))

    def test_supuesto_deja_a_la_vista_el_ajuste(self):
        from gestion.management.commands._precios_sop_excel import _supuesto

        mar30 = datetime.date(2030, 3, 1)
        self.assertEqual(_supuesto([(datetime.date(2030, 2, 1), "=AU10", "repite"), (mar30, "=AV10*(1+2.5%)", "ajuste")]),
                         "Valor escrito; +2.5% desde Mar-30")
