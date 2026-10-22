"""Pestaña Indicadores de cada categoría: un panel por pestaña con su número clave y su gráfico."""

import copy
import datetime

from dashboard import queries as q
from django.http import QueryDict

from .. import charts
from .categoria_pestanas import _COMPRAS_MESES, _cat_abastecimiento, _cat_compras, _cat_cuentas, _cat_inventario, _cat_precios
from .comun import _MESES_ABREV_GUION, _cortes_inventario, _mes_corto, _nombre_corto, _serie_inventario, _soles_eje, _soles_mm
from .zonas import _zonas_categoria


# Paneles de la pestaña Indicadores de cada categoría, en orden. Por defecto
# todos (los que apliquen a la categoría); Suministros Industriales solo IPU,
# Cuentas por pagar, Inventario e IRI de 12 meses (pedido por el usuario,
# 2026-10-16).
_PANELES_CATEGORIA_DEFECTO = ("compras", "proveedores", "inventario", "salud", "abastecimiento", "ipu", "pmp")
_PANELES_CATEGORIA = {
    "SUMINISTROS INDUSTRIALES": ("ipu", "pmp", "inventario", "iri"),
}
_IRI_MESES = 12


def _iri_por_mes(categoria, meses_serie=_IRI_MESES + 1):
    """IRI (Índice de Rotación de Inventarios) de 12 meses de `categoria` al
    cierre de cada uno de los últimos `meses_serie` meses cerrados: consumo
    valorizado de los 12 meses que terminan en ese mes ÷ inventario promedio
    (valor MB5B al cierre de esos 12 meses). Lista de dicts mes, iri,
    consumo, inventario (promedio), dias (365 ÷ IRI); solo los meses con los
    12 meses completos de datos."""
    cerrado = q._sumar_meses(datetime.date.today().replace(day=1), -1)
    consumo = q.consumo_valorizado_mensual_por_categoria()
    consumo = {m: v for m, v in zip(consumo.loc[consumo["categoria"] == categoria, "mes"],
                                     consumo.loc[consumo["categoria"] == categoria, "consumo_valor"])}
    cortes = [c for c in _cortes_inventario() if c.replace(day=1) <= cerrado]
    inventario = {f.replace(day=1): v for f, v in _serie_inventario(cortes, categoria)}
    filas = []
    for i in range(meses_serie - 1, -1, -1):
        mes = q._sumar_meses(cerrado, -i)
        ventana = [q._sumar_meses(mes, -k) for k in range(_IRI_MESES)]
        if not all(m in inventario and m in consumo for m in ventana):
            continue
        total = sum(consumo[m] for m in ventana)
        promedio = sum(inventario[m] for m in ventana) / _IRI_MESES
        if promedio <= 0:
            continue
        iri = total / promedio
        filas.append({"mes": mes, "desde": ventana[-1], "iri": iri, "consumo": total, "inventario": promedio,
                      "dias": 365 / iri if iri > 0 else None})
    return filas


def _cat_indicadores(request, categoria, tiene_inventario):
    """Pestaña Indicadores de una categoría: un panel por pestaña con su
    número clave y el gráfico que lo explica, siempre al último dato
    disponible (con los filtros por defecto de cada pestaña, no los que se
    hayan cambiado). Qué paneles y en qué orden: _PANELES_CATEGORIA. Devuelve
    los paneles y cuántos materiales críticos están en riesgo (el aviso rojo
    de arriba de la página)."""
    nombres_zona = {z["clave"]: z["nombre"] for z in _zonas_categoria(categoria, tiene_inventario)}
    claves = _PANELES_CATEGORIA.get(categoria, _PANELES_CATEGORIA_DEFECTO)
    sin_filtros = copy.copy(request)
    sin_filtros.GET = QueryDict()
    por_clave = {}

    def panel(clave, zona, titulo, cifra, sub, tipo, g, tono="", estado="", leyenda=(), nota="", ancho=False, vacio=""):
        por_clave[clave] = {
            "zona": zona, "zona_nombre": nombres_zona[zona], "titulo": titulo, "cifra": cifra, "sub": sub,
            "tipo": tipo, "g": g, "tono": tono, "estado": estado, "leyenda": list(leyenda), "nota": nota,
            "ancho": ancho, "vacio": vacio,
        }

    # 1. Compras del año a la fecha, contra los mismos meses del año anterior.
    co = _cat_compras(sin_filtros, categoria, tiene_inventario) if {"compras", "proveedores"} & set(claves) else None
    por_mes = co["compras_por_mes"] if co else None
    titulo = "Compras del año"
    if "compras" not in claves:
        pass
    elif not por_mes:
        panel("compras", "compras", titulo, "—", "", "", None, vacio="Sin compras registradas en esta categoría.", ancho=True)
    else:
        ultimo_mes = por_mes[-1][0]
        anio = ultimo_mes.year
        actual = sum(v for m, v in por_mes if m.year == anio)
        previo = sum(v for m, v in por_mes if m.year == anio - 1 and m.month <= ultimo_mes.month)
        fila_anio = next((f for f in co["compras"] if f["anio"] == anio), None)
        contrato = f" · {fila_anio['pct_contrato']:z.1f}% bajo contrato" if fila_anio else ""
        estado = f"{(actual / previo - 1) * 100:+z.1f}% vs {anio - 1}" if previo else ""
        panel("compras", "compras", titulo, _soles_mm(actual),
              f"Ene–{_MESES_ABREV_GUION[ultimo_mes.month]} {anio}{contrato}"
              + (f" · mismo tramo de {anio - 1}: {_soles_mm(previo)}" if previo else ""),
              "columnas", co["compras_mensual"]["grafico"] if co["compras_mensual"] else None, "neutro", estado,
              nota=f"Valor de las órdenes de compra por mes del documento, últimos {_COMPRAS_MESES} meses.", ancho=True)

    # 2. Concentración de proveedores (Top 5) del año, con Lead Time y Fill Rate.
    titulo = "Concentración de proveedores"
    pareto = co["pareto"] if co else None
    if "proveedores" not in claves:
        pass
    elif not pareto or not pareto["top_proveedores"]:
        panel("proveedores", "compras", titulo, "—", "", "", None, vacio="Sin compras en lo que va del año.")
    else:
        pct = float(pareto["pct_concentracion_top"])
        tono, estado = (("alerta", "Alta dependencia") if pct >= 80 else
                        ("aviso", "Concentrada") if pct >= 60 else ("ok", "Diversificada"))
        extras = []
        if co["lead_time"]:
            extras.append(f"lead time {co['lead_time']['lead_time_promedio']:z.0f} días")
        if co["fill_rate"] and co["fill_rate"].get("fill_rate_pct") is not None:
            extras.append(f"fill rate {co['fill_rate']['fill_rate_pct']:z.1f}%")
        if co["otif"]:
            extras.append(f"OTIF {co['otif']['otif']:z.0f}%")
        top = pareto["top_proveedores"]
        tope = max(float(p["pct"]) for p in top) * 1.1 or 100
        filas = [{"nombre": _nombre_corto(str(p["proveedor"])), "sub": "", "valor": float(p["pct"]),
                  "texto": f"{float(p['pct']):z.1f}%", "clase": "mal",
                  "detalle": f"{p['proveedor']}\nS/ {float(p['valor']):z,.0f} · {float(p['pct']):z.1f}% de las compras"}
                 for p in top]
        panel("proveedores", "compras", titulo, f"{pct:z.1f}%",
              f"De las compras de {co['sc']['texto']} en los {co['sc']['top']} proveedores más grandes"
              + (" · " + " · ".join(extras) if extras else ""),
              "escala", charts.barras_escala(filas, 0, tope), tono, estado)

    criticos_en_riesgo = 0
    if tiene_inventario:
        inv = _cat_inventario(sin_filtros, categoria, tiene_inventario)
        criticos_en_riesgo = inv["criticos"]["en_riesgo"] if inv["criticos"] else 0
        # 3. Valor del inventario: el último corte de cada uno de los últimos
        # 13 meses (desde Set-26 hay cortes diarios: va el último de cada mes).
        valores = _serie_inventario(_cortes_inventario()[-13:], categoria)
        titulo = "Valor del inventario"
        if "inventario" not in claves:
            pass
        elif not valores:
            panel("inventario", "inventario", titulo, "—", "", "", None, vacio="Sin inventario registrado.")
        else:
            rot = inv["rotacion"]
            partes = [f"Al {valores[-1][0]:%d/%m/%Y}"]
            # Con el panel del IRI (12 meses) no se repite la rotación por consumo de 12 meses ÷ stock del corte: serían dos cifras distintas.
            if rot["rotacion_anual"] is not None and "iri" not in claves:
                partes.append(f"rotación {rot['rotacion_anual']:z.2f} veces al año")
            if rot["dias_inventario"] is not None and "iri" not in claves:
                partes.append(f"{rot['dias_inventario']:z,.0f} días de inventario")
            estado = (f"{(valores[-1][1] / valores[-2][1] - 1) * 100:+z.1f}% vs mes anterior"
                      if len(valores) > 1 and valores[-2][1] else "")
            panel("inventario", "inventario", titulo, _soles_mm(valores[-1][1]), " · ".join(partes), "lineas",
                  charts.lineas_mensuales(
                      [{"nombre": "Valor", "clase": "serie-0",
                        "puntos": [(f.replace(day=1), v, f"Al {f:%d/%m/%Y}\nS/ {v:z,.0f}") for f, v in valores]}],
                      _mes_corto, _soles_eje, max_etiquetas_eje=7),
                  "neutro", estado, nota="Valor del inventario (MB5B) al cierre de cada mes, últimos 13 meses "
                                         "(el mes en curso, a su último corte).")

        # IRI: rotación de inventarios de 12 meses, al cierre de cada uno de los últimos 12 meses.
        iri = _iri_por_mes(categoria) if "iri" in claves else []
        titulo = "Rotación de inventarios (IRI)"
        if "iri" not in claves:
            pass
        elif not iri:
            panel("iri", "inventario", titulo, "—", "", "", None, vacio="Faltan 12 meses de consumo e inventario para calcularlo.")
        else:
            ultimo, serie = iri[-1], iri[-_IRI_MESES:]
            hace_un_anio = next((f for f in iri if f["mes"] == q._sumar_meses(ultimo["mes"], -12)), None)
            estado = f"{ultimo['iri'] - hace_un_anio['iri']:+z.2f} vs {_mes_corto(hace_un_anio['mes'])}" if hace_un_anio else ""
            panel("iri", "inventario", titulo, f"{ultimo['iri']:z.2f} veces",
                  f"{_mes_corto(ultimo['desde'])}–{_mes_corto(ultimo['mes'])} · consumo {_soles_mm(ultimo['consumo'])} ÷ "
                  f"inventario promedio {_soles_mm(ultimo['inventario'])}"
                  + (f" · {ultimo['dias']:z,.0f} días de inventario" if ultimo["dias"] else ""),
                  "lineas", charts.lineas_mensuales(
                      [{"nombre": "IRI", "clase": "serie-0",
                        "puntos": [(f["mes"], f["iri"],
                                    f"{_mes_corto(f['desde'])}–{_mes_corto(f['mes'])}\nIRI {f['iri']:z.2f} veces"
                                    f"\nConsumo S/ {f['consumo']:z,.0f} ÷ inventario promedio S/ {f['inventario']:z,.0f}")
                                   for f in serie]}],
                      _mes_corto, lambda v: f"{v:z.2f}", desde_cero=True, max_etiquetas_eje=7),
                  "neutro", estado,
                  nota="Consumo valorizado (MB51) de los 12 meses que terminan en cada mes ÷ valor promedio del "
                       "inventario (MB5B) al cierre de esos 12 meses. Más alto = el inventario rota más rápido.")

    # 4. Salud del inventario: críticos en riesgo, sin movimiento y ABC.
    if tiene_inventario and "salud" in claves:
        titulo = "Salud del inventario"
        criticos, obsoletos, abc = inv["criticos"], inv["obsoletos"], inv["abc"]
        partes = []
        if criticos:
            partes.append(f"de {criticos['materiales_criticos']:,} materiales críticos")
        if obsoletos:
            partes.append(f"sin movimiento en 3 años: {_soles_mm(float(obsoletos['valor_obsoleto']))} "
                          f"({obsoletos['materiales_obsoletos']:,} materiales)")
        g = None
        if abc and abc["clases"]:
            total_valor = sum(float(c["valor"]) for c in abc["clases"]) or 1
            total_mat = sum(int(c["materiales"]) for c in abc["clases"]) or 1
            g = charts.pilas_horizontales([
                {"nombre": "Valor", "sub": "", "texto": "",
                 "segmentos": [{"valor": float(c["valor"]), "clase": f"col-abc-{c['clase'].lower()}",
                                "detalle": f"Clase {c['clase']}: S/ {float(c['valor']):z,.0f} "
                                           f"({float(c['valor']) / total_valor * 100:z.1f}% del valor)"}
                               for c in abc["clases"]]},
                {"nombre": "Materiales", "sub": "", "texto": "",
                 "segmentos": [{"valor": int(c["materiales"]), "clase": f"col-abc-{c['clase'].lower()}",
                                "detalle": f"Clase {c['clase']}: {int(c['materiales']):,} materiales "
                                           f"({int(c['materiales']) / total_mat * 100:z.1f}%)"}
                               for c in abc["clases"]]},
            ])
            # En la barra de valor, el texto de cada segmento es su % del valor (no los soles).
            for s in g["filas"][0]["segmentos"]:
                s["texto"] = f"{s['valor'] / total_valor * 100:z.0f}%" if s["texto"] else ""
        leyenda_abc = [("col-abc-a", "Clase A"), ("col-abc-b", "Clase B"), ("col-abc-c", "Clase C")]
        nota_abc = "Barras: clasificación ABC del inventario (A = 80% del valor, B = hasta 95%, C = el resto)."
        if criticos:
            panel("salud", "inventario", titulo, f"{criticos_en_riesgo:,}", "Materiales críticos con menos de 1 mes de cobertura, "
                  + " · ".join(partes), "pilas", g, "alerta" if criticos_en_riesgo else "ok",
                  "En riesgo" if criticos_en_riesgo else "Sin riesgo", leyenda=leyenda_abc, nota=nota_abc)
        else:
            # Sin materiales críticos marcados en el maestro: la cifra pasa a
            # ser el capital inmovilizado (sin movimiento en 3 años).
            panel("salud", "inventario", titulo, _soles_mm(float(obsoletos["valor_obsoleto"])) if obsoletos else "—",
                  (f"Sin movimiento en 3 años ({obsoletos['materiales_obsoletos']:,} materiales)" if obsoletos
                   else "Sin materiales sin movimiento") + " · sin materiales críticos marcados en el maestro",
                  "pilas", g, "aviso" if obsoletos else "ok", "Inmovilizado" if obsoletos else "Sin inmovilizado",
                  leyenda=leyenda_abc, nota=nota_abc)

    # 5. Abastecimiento S&OP: cumplimiento del último mes cerrado, por centro.
    if "abastecimiento" in nombres_zona and "abastecimiento" in claves:
        titulo = "Cumplimiento del plan de abastecimiento"
        ab = _cat_abastecimiento(sin_filtros, categoria, tiene_inventario)["ab"]
        if not ab or not ab["centros"] or ab["cumplimiento_pct"] is None:
            panel("abastecimiento", "abastecimiento", titulo, "—", "", "", None, vacio="Sin plan ni ingresos del último mes cerrado.")
        else:
            pct = ab["cumplimiento_pct"]
            filas = [{"nombre": q.nombre_centro(f["centro"]).title(), "sub": f"{f['real']:z,.0f} de {f['plan']:z,.0f} T",
                      "valor": f["pct"] or 0.0, "texto": f"{f['pct']:z.0f}%" if f["pct"] is not None else "—",
                      "clase": "bien" if (f["pct"] or 0) >= 100 else "regular" if (f["pct"] or 0) >= 80 else "mal",
                      "detalle": f"{q.nombre_centro(f['centro'])}: {f['real']:z,.0f} T ingresadas de {f['plan']:z,.0f} T planificadas"}
                     for f in ab["centros"]]
            tope = min(max(120, max(f["valor"] for f in filas) * 1.08), 200)
            tono, estado = (("ok", "Cumplido") if pct >= 100 else ("aviso", "Por debajo") if pct >= 80 else ("alerta", "Bajo el plan"))
            panel("abastecimiento", "abastecimiento", titulo, f"{pct:z.1f}%",
                  f"{q.formato_mes_anio(ab['mes'])} · {ab['total_real']:z,.0f} T ingresadas de {ab['total_plan']:z,.0f} T planificadas",
                  "escala", charts.barras_escala(filas, 0, tope, referencias=[(100, "100%")]), tono, estado)

    # 6. Precios e IPU: acumulado del año (interanual) y cada mes.
    pr = _cat_precios(sin_filtros, categoria, tiene_inventario)["pr"] if "ipu" in claves else None
    titulo = "IPU · Índice de Precios Unacem"
    if pr is None:
        pass
    elif not pr["resumen"] or not pr["puntos"]:
        panel("ipu", "precios", titulo, "—", "", "", None, vacio=pr["aviso"] or "Sin compras con base de comparación.")
    else:
        a = pr["resumen"]["por_anio"][-1]
        tono, estado = (("alerta", "Sube") if a["pct"] > 0.5 else ("ok", "Baja") if a["pct"] < -0.5 else ("neutro", "Estable"))
        puntos = pr["puntos"][-_COMPRAS_MESES:]
        cortes = [i for i, p in enumerate(puntos) if i and p["mes"].year != puntos[i - 1]["mes"].year]
        g = charts.columnas(
            [_mes_corto(p["mes"]) for p in puntos],
            [[{"valor": p["individual"] * 100, "clase": "col-sube" if p["individual"] > 0 else "col-baja",
               "detalle": p["detalle_individual"]}] for p in puntos],
            lambda v: f"{v:+z.0f}%",
            linea={"valores": [p["acumulado"] * 100 for p in puntos], "cortes": cortes, "clase": "",
                   "detalles": [p["detalle_acumulado"] for p in puntos],
                   "etiqueta_final": f"{puntos[-1]['acumulado'] * 100:+z.1f}%"},
            ancho=480, alto=200,  # panel de media página: menos etiquetas de mes, sin que se pisen
        )
        panel("ipu", "precios", titulo, f"{a['pct']:+z.1f}%",
              f"{a['anio']} acumulado a {a['hasta']} vs el mismo tramo de {a['anio'] - 1} · bolsa "
              f"{'+' if a['bolsa'] >= 0 else '−'}{_soles_mm(abs(a['bolsa']))} · {pr['resumen']['materiales']:,} materiales",
              "columnas", g, tono, estado,
              leyenda=[("col-sube", "El mes sube"), ("col-baja", "El mes baja"), ("linea-ipu", "IPU acumulado del año")],
              nota="Precio real de compra (MB51 + ajustes FBL3N) de cada mes contra el mismo mes del año anterior; la línea acumula desde enero.")

    # 7. Cuentas por pagar: PMP del año y su evolución.
    cx = _cat_cuentas(sin_filtros, categoria, tiene_inventario)["cx"] if "pmp" in claves else None
    titulo = "Plazo medio de pago"
    if cx is None:
        pass
    elif not cx["pmp_anios"]:
        panel("pmp", "cuentas", titulo, "—", "", "", None, vacio="Sin compras registradas en esta categoría.")
    else:
        ultimo = cx["pmp_anios"][-1]
        estado = f"{ultimo['diferencia']:+z.0f} días vs {ultimo['anio'] - 1}" if ultimo["diferencia"] is not None else ""
        serie = cx["pmp_serie"][-_COMPRAS_MESES:]
        panel("pmp", "cuentas", titulo, f"{ultimo['pmp']:z.0f} días",
              f"Compras de {ultimo['periodo']}, mismos meses que el año anterior"
              + (f" · {cx['pct_60']:z.0f}% del valor a 60 días o más" if cx.get("pct_60") is not None else "")
              + (f" · cada día más de plazo = {_soles_mm(cx['caja_por_dia'])} de caja" if cx.get("caja_por_dia") else ""),
              "lineas", charts.lineas_mensuales(
                  [{"nombre": "PMP", "clase": "serie-0",
                    "puntos": [(m, v, f"{q.formato_mes_anio(m)}\nPMP: {v:z.0f} días") for m, v in serie]}],
                  _mes_corto, lambda v: f"{v:z.0f} d", desde_cero=True, max_etiquetas_eje=8),
              "neutro", estado, nota="Días de pago pactados en las órdenes de compra, ponderados por valor (mes a mes).")

    paneles = [por_clave[c] for c in claves if c in por_clave]
    # Paneles de media página en número impar: el último ocupa el ancho
    # completo (si no, queda solo en su fila con un hueco al lado).
    medios = [p for p in paneles if not p["ancho"]]
    if len(medios) % 2:
        medios[-1]["ancho"] = True
    return {"paneles": paneles, "criticos_en_riesgo": criticos_en_riesgo}
