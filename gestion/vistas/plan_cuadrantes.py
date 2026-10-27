"""Cuadrantes de la pestaña Indicadores con filtro Año: Planificación (IPU, plazo medio de pago, valor del inventario y rotación de Materias Primas y Combustibles) y las categorías que lo usan (Suministros Industriales, ver categoria_indicadores)."""

import datetime

import pandas as pd
from dashboard import queries as q

from .. import charts
from .comun import _MESES_ABREV_GUION, _PANEL_LINEAS, _cortes_inventario, _mes_corto, _serie_inventario, _soles_eje, _soles_mm
from .zonas import _ZONAS_PLANIFICACION

# Pedido del usuario (2026-10-23): la pestaña Indicadores de Planificación
# con solo cuatro cuadrantes, como Suministros Industriales. El IPU es el de
# la pestaña Precios e IPU (materiales del S&OP, interanual); el plazo medio
# de pago, el inventario y su rotación, de las categorías de Planificación
# (Materias Primas y Combustibles) juntas.
CATEGORIAS_CUADRANTES = q.CATEGORIAS_PLANIFICACION_SOP

# Filtro Año (pedido del usuario, 2026-10-24, primero en Planificación y
# luego en Suministros Industriales): "2025 en adelante" (por defecto, como
# el Período de Precios e IPU) o un solo año. Cada cuadrante recorta sus
# meses al período y compara contra el año anterior.
ANIO_INICIO = 2025
_IRI_MESES = 12
_MESES_IRI_HISTORIA = 48  # meses que se intentan calcular (quedan los que tienen 12 meses de datos)


def periodos_cuadrantes():
    """Opciones del filtro Año: (valor, texto). "desde" = de ANIO_INICIO al mes en curso."""
    hoy = datetime.date.today()
    return [("desde", f"{ANIO_INICIO} en adelante")] + [(str(a), f"Solo {a}") for a in range(ANIO_INICIO, hoy.year + 1)]


def periodo_valido(valor):
    return valor if valor in {v for v, _ in periodos_cuadrantes()} else "desde"


def _en_periodo(mes, periodo):
    return mes.year >= ANIO_INICIO if periodo == "desde" else mes.year == int(periodo)


def _anio_base(periodo):
    """Año contra el que se compara: el anterior al período."""
    return (ANIO_INICIO if periodo == "desde" else int(periodo)) - 1


def tono_ipu(pct):
    """(tono, estado) de la cifra del IPU."""
    return ("alerta", "Sube") if pct > 0.5 else ("ok", "Baja") if pct < -0.5 else ("neutro", "Estable")


def _en_segmentos(df, segmentos):
    """Filas de `df` (categoria + texto_breve) de los `segmentos` de Planificación."""
    return df[[q.segmento_planificacion(c, t) in segmentos for c, t in zip(df["categoria"], df["texto_breve"])]]


def _texto_segmentos(segmentos, defecto="Materias Primas y Combustibles"):
    """'Materias Primas y Combustible Sólido' (segmentos elegidos), o `defecto` sin filtro."""
    if not segmentos:
        return defecto
    nombres = [x.capitalize() for x in q.SEGMENTOS_PLANIFICACION if x in segmentos]
    return nombres[0] if len(nombres) == 1 else ", ".join(nombres[:-1]) + " y " + nombres[-1]


def _serie_inventario_categorias(cortes, categorias, segmentos=None):
    """Valor del inventario de varias categorías juntas en cada corte: (fecha,
    valor), solo los cortes que tienen todas. Con `segmentos` (Planificación:
    Materias Primas / Combustible Sólido / Líquido), material por material."""
    if segmentos:
        serie = []
        for c in cortes:
            inv = q.inventario_por_material(c)
            inv = _en_segmentos(inv[inv["categoria"].isin(categorias)], segmentos)
            serie.append((c, float(inv["valor_total"].sum())))
        return serie
    series = [dict(_serie_inventario(cortes, c)) for c in categorias]
    return [(f, sum(s[f] for s in series)) for f in cortes if all(f in s for s in series)]


def _iri_por_mes(categoria, meses_serie=_IRI_MESES + 1, segmentos=None):
    """IRI (Índice de Rotación de Inventarios) de 12 meses de `categoria` (o
    de varias juntas, p.ej. las de Planificación) al cierre de cada uno de
    los últimos `meses_serie` meses cerrados: consumo valorizado de los 12
    meses que terminan en ese mes ÷ inventario promedio (valor MB5B al cierre
    de esos 12 meses). Lista de dicts mes, iri, consumo, inventario
    (promedio), dias (365 ÷ IRI); solo los meses con los 12 meses completos
    de datos."""
    categorias = (categoria,) if isinstance(categoria, str) else tuple(categoria)
    cerrado = q._sumar_meses(datetime.date.today().replace(day=1), -1)
    if segmentos:
        consumo_df = q.consumo_valorizado_mensual_por_material()
        consumo_df = _en_segmentos(consumo_df[consumo_df["categoria"].isin(categorias)], segmentos)
    else:
        consumo_df = q.consumo_valorizado_mensual_por_categoria()
        consumo_df = consumo_df[consumo_df["categoria"].isin(categorias)]
    consumo = {m: float(v) for m, v in consumo_df.groupby("mes")["consumo_valor"].sum().items()}
    cortes = [c for c in _cortes_inventario() if c.replace(day=1) <= cerrado]
    inventario = _serie_inventario_categorias(cortes, categorias, segmentos)
    inventario = {f.replace(day=1): v for f, v in inventario}
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


def _pmp_categorias(categorias, segmentos=None):
    """Compras por mes con su peso (días de pago × valor) de varias
    categorías juntas (o de sus `segmentos` de Planificación, material por
    material), hasta el mes en curso: mes, valor, peso, dias60 (valor
    pactado a 60 días o más)."""
    hoy_mes = datetime.date.today().replace(day=1)
    if segmentos:
        df = _en_segmentos(q.compras_por_mes_condicion_pago_material(tuple(categorias)), segmentos)
    else:
        partes = [q.compras_por_mes_y_condicion_pago(c) for c in categorias]
        df = pd.concat([p for p in partes if not p.empty], ignore_index=True) if any(not p.empty for p in partes) else pd.DataFrame()
    if df.empty:
        return df
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    df = df[df["mes"] <= hoy_mes].copy()
    df["valor_total"] = df["valor_total"].astype(float)
    df["dias"] = df["condicion_pago"].map(q.dias_ponderados_condicion_pago)
    df["peso"] = df["dias"] * df["valor_total"]
    df["dias60"] = df["valor_total"].where(df["dias"] >= 60, 0.0)
    return df.groupby("mes", as_index=False).agg(valor=("valor_total", "sum"), peso=("peso", "sum"), dias60=("dias60", "sum"))


# Cada cuadrante devuelve los campos de su panel (_panel_indicador.html):
# titulo, cifra, sub, tipo, g, tono, estado, nota y vacio (sin datos en el
# período). Quien lo usa le agrega su zona ("Ver detalle").
def _vacio(titulo, texto):
    return {"titulo": titulo, "cifra": "—", "sub": "", "tipo": "", "g": None, "vacio": texto}


def cuadrante_pmp(categorias, periodo, nota, con_caja=False, segmentos=None):
    """Plazo medio de pago del período, contra los mismos meses del año
    anterior (con "desde", contra todo el año anterior al período).
    `con_caja`: suma cuánta caja vale un día más de plazo (compras del
    período ÷ sus días), como el panel de las categorías."""
    titulo = "Plazo medio de pago"
    pm = _pmp_categorias(categorias, segmentos)
    serie = pm[pm["mes"].map(lambda m: _en_periodo(m, periodo))] if not pm.empty else pm
    if serie.empty or not serie["valor"].sum():
        return _vacio(titulo, "Sin compras registradas en el período.")
    primer_mes, ultimo_mes = serie["mes"].min(), serie["mes"].max()
    base = _anio_base(periodo)
    anios = pm["mes"].map(lambda m: m.year)
    if periodo == "desde":
        previo = pm[anios == base]  # el año completo anterior al período
        tramo = f"Compras de {_mes_corto(primer_mes)} a {_mes_corto(ultimo_mes)}, contra todo {base}"
    else:
        previo = pm[(anios == base) & (pm["mes"].map(lambda m: m.month) <= ultimo_mes.month)]
        tramo = f"Compras de Ene–{_MESES_ABREV_GUION[ultimo_mes.month]} {ultimo_mes.year}, mismos meses de {base}"
    valor = serie["valor"].sum()
    pmp = float(serie["peso"].sum() / valor)
    pmp_previo = float(previo["peso"].sum() / previo["valor"].sum()) if previo["valor"].sum() else None
    pct_60 = float(serie["dias60"].sum() / valor * 100)
    sub = tramo + f" · {pct_60:z.0f}% del valor a 60 días o más"
    if con_caja:
        fin = min(datetime.date.today(), q._sumar_meses(ultimo_mes, 1) - datetime.timedelta(days=1))
        sub += f" · cada día más de plazo = {_soles_mm(valor / max((fin - primer_mes).days + 1, 1))} de caja"
    diferencia = round(pmp - pmp_previo) if pmp_previo is not None else None
    return {
        "titulo": titulo, "cifra": f"{pmp:z.0f} días", "sub": sub,
        "tipo": "lineas", "g": charts.lineas_mensuales(
            [{"nombre": "PMP", "clase": "serie-0",
              "puntos": [(f.mes, f.peso / f.valor, f"{q.formato_mes_anio(f.mes)}\nPMP: {f.peso / f.valor:z.0f} días"
                          f"\nCompras S/ {f.valor:z,.0f}") for f in serie.itertuples() if f.valor]}],
            _mes_corto, lambda v: f"{v:z.0f} d", desde_cero=True, max_etiquetas_eje=6, **_PANEL_LINEAS),
        "tono": "neutro",
        "estado": f"{diferencia:+d} día{'' if abs(diferencia) == 1 else 's'} vs {base}" if diferencia is not None else "",
        "nota": nota,
    }


def cuadrante_inventario(categorias, periodo, texto_categorias, segmentos=None):
    """Valor del inventario al cierre de cada mes del período, contra el
    cierre del año anterior (p.ej. Dic-25 para "Solo 2026")."""
    titulo = "Valor del inventario"
    todos = _serie_inventario_categorias(_cortes_inventario(), categorias, segmentos)
    valores = [(f, v) for f, v in todos if _en_periodo(f, periodo)]
    if not valores:
        return _vacio(titulo, "Sin inventario registrado en el período.")
    cierre_previo = next((x for x in reversed(todos) if x[0].year == _anio_base(periodo)), None)
    return {
        "titulo": titulo, "cifra": _soles_mm(valores[-1][1]), "sub": f"Al {valores[-1][0]:%d/%m/%Y} · {texto_categorias}",
        "tipo": "lineas", "g": charts.lineas_mensuales(
            [{"nombre": "Valor", "clase": "serie-0",
              "puntos": [(f.replace(day=1), v, f"Al {f:%d/%m/%Y}\nS/ {v:z,.0f}") for f, v in valores]}],
            _mes_corto, _soles_eje, max_etiquetas_eje=6, **_PANEL_LINEAS),
        "tono": "neutro",
        "estado": (f"{(valores[-1][1] / cierre_previo[1] - 1) * 100:+z.1f}% vs {_mes_corto(cierre_previo[0])}"
                   if cierre_previo and cierre_previo[1] else ""),
        "nota": "Valor del inventario (MB5B) al cierre de cada mes del período (el mes en curso, a su último corte), "
                "contra el cierre del año anterior.",
    }


def cuadrante_iri(categorias, periodo, segmentos=None):
    """IRI de 12 meses al cierre de cada mes del período, contra 12 meses antes."""
    titulo = "Rotación de inventarios (IRI)"
    iri = _iri_por_mes(categorias, meses_serie=_MESES_IRI_HISTORIA, segmentos=segmentos)
    serie = [f for f in iri if _en_periodo(f["mes"], periodo)]
    if not serie:
        return _vacio(titulo, "Faltan 12 meses de consumo e inventario para calcularlo.")
    ultimo = serie[-1]
    hace_un_anio = next((f for f in iri if f["mes"] == q._sumar_meses(ultimo["mes"], -12)), None)
    return {
        "titulo": titulo, "cifra": f"{ultimo['iri']:z.2f} veces",
        "sub": (f"{_mes_corto(ultimo['desde'])}–{_mes_corto(ultimo['mes'])} · consumo {_soles_mm(ultimo['consumo'])} ÷ "
                f"inventario promedio {_soles_mm(ultimo['inventario'])}"
                + (f" · {ultimo['dias']:z,.0f} días de inventario" if ultimo["dias"] else "")),
        "tipo": "lineas", "g": charts.lineas_mensuales(
            [{"nombre": "IRI", "clase": "serie-0",
              "puntos": [(f["mes"], f["iri"],
                          f"{_mes_corto(f['desde'])}–{_mes_corto(f['mes'])}\nIRI {f['iri']:z.2f} veces"
                          f"\nConsumo S/ {f['consumo']:z,.0f} ÷ inventario promedio S/ {f['inventario']:z,.0f}")
                         for f in serie]}],
            _mes_corto, lambda v: f"{v:z.2f}", desde_cero=True, max_etiquetas_eje=6, **_PANEL_LINEAS),
        "tono": "neutro",
        "estado": f"{ultimo['iri'] - hace_un_anio['iri']:+z.2f} vs {_mes_corto(hace_un_anio['mes'])}" if hace_un_anio else "",
        "nota": "Consumo valorizado (MB51) de los 12 meses que terminan en cada mes ÷ valor promedio del inventario "
                "(MB5B) al cierre de esos 12 meses. Más alto = el inventario rota más rápido.",
    }


def _paneles_cuadrantes(inf, periodo="desde", segmentos=None):
    """Los cuatro paneles de Planificación (IPU, plazo medio de pago, valor
    del inventario e IRI), con el formato de _panel_indicador.html,
    recortados al `periodo` del filtro Año ("desde" o un año). `inf`:
    contexto de la sección Inflación de precios (interanual) con ese mismo
    Período y sus demás filtros por defecto. `segmentos`: los de
    Planificación elegidos en el filtro Categoría (None = todos); `inf` ya
    viene con esas categorías."""
    texto = _texto_segmentos(segmentos)
    nombres_zona = {clave: nombre for clave, nombre, _, _ in _ZONAS_PLANIFICACION}
    paneles = []

    def agregar(zona, campos, zona_nombre=None, leyenda=()):
        paneles.append({"tono": "", "estado": "", "nota": "", "vacio": "", **campos, "zona": zona,
                        "zona_nombre": zona_nombre or nombres_zona.get(zona, ""), "leyenda": list(leyenda), "ancho": False})

    # 1. IPU: variación interanual de cada mes (columnas) y acumulado del año (línea).
    titulo = "IPU · Índice de Precios Unacem"
    r = inf.get("resumen") if inf else None
    if not r or not r.get("por_anio") or not inf.get("canasta") or inf.get("periodo_sel") != periodo:
        agregar("precios", _vacio(titulo, "Sin compras con base de comparación en el período."))
    else:
        etiquetas, canasta, ipu_fila = inf["meses"], inf["canasta"], inf["ipu_fila"]
        ipu = [None if x is None else x["pct"] for x in ipu_fila]
        linea = None
        if any(v is not None for v in ipu):
            ultimo = next(v for v in reversed(ipu) if v is not None)
            linea = {"valores": ipu, "clase": "", "etiqueta_final": f"{ultimo:+z.1f}%",
                     "cortes": [i for i in range(1, len(etiquetas)) if etiquetas[i][-2:] != etiquetas[i - 1][-2:]],
                     "detalles": ["" if x is None else x["detalle"] for x in ipu_fila]}
        if periodo == "desde":
            # Todo el período: cada mes contra el mismo mes del año anterior, ponderado.
            pct, bolsa = r["periodo_pct"], r["bolsa"]
            tramo = f"{r['desde']} a {r['hasta']} vs los mismos meses del año anterior"
        else:
            a = r["por_anio"][-1]
            pct, bolsa = a["pct"], a["bolsa"]
            tramo = f"{a['anio']} acumulado a {a['hasta']} vs el mismo tramo de {a['anio_base']}"
        tono, estado = tono_ipu(pct)
        agregar("precios", {
            "titulo": titulo, "cifra": f"{pct:+z.1f}%",
            "sub": f"{tramo} · bolsa {'+' if bolsa >= 0 else '−'}{_soles_mm(abs(bolsa))} · {inf['filas_total']:,} materiales",
            "tipo": "columnas",
            "g": charts.columnas(etiquetas, [[{"valor": x["pct"], "clase": "col-sube" if x["pct"] > 0 else "col-baja",
                                               "detalle": x["detalle"]}] for x in canasta],
                                 lambda v: f"{v:+z.0f}%", linea=linea, ancho=480, alto=200),
            "tono": tono, "estado": estado,
            "nota": "Precio real de compra (MB51 + ajustes FBL3N) de cada mes contra el mismo mes del año anterior; "
                    "la línea acumula desde enero."},
            leyenda=[("col-sube", "El mes sube"), ("col-baja", "El mes baja"), ("linea-ipu", "IPU acumulado del año")])

    # 2-4. Plazo medio de pago, valor del inventario e IRI de las categorías de Planificación juntas.
    agregar("", cuadrante_pmp(CATEGORIAS_CUADRANTES, periodo, f"Días de pago pactados en las órdenes de compra de "
                                                             f"{texto}, ponderados por valor.", segmentos=segmentos),
            zona_nombre="Cuentas por pagar")
    agregar("inventario", cuadrante_inventario(CATEGORIAS_CUADRANTES, periodo, texto, segmentos=segmentos))
    agregar("inventario", cuadrante_iri(CATEGORIAS_CUADRANTES, periodo, segmentos=segmentos))
    return paneles
