"""Estado de los datos: hasta qué fecha llega cada fuente de SAP, cuándo se cargó y qué falta (cabecera de las páginas)."""

import datetime

import pandas as pd

from .base import _cacheado, _query, _sumar_meses, formato_mes_anio

# Fuentes que se muestran (pedido del usuario, 2026-11-10: "que se vea si una
# cifra todavía no tiene todos sus datos"): (clave, nombre, para qué se usa,
# texto de la carpeta en la ruta del archivo cargado, consulta de la fecha de
# corte, cómo se carga). "diaria": llega con la carga de cada día;
# "mensual": se carga a mano tras el cierre (la FBL3N); "sop": una versión por
# mes de emisión del S&OP.
FUENTES = (
    ("mb51", "Movimientos (MB51)", "Ingresos, consumos, traslados y precios de entrada", "TABLA_MB51",
     "SELECT MAX(fecha_contabilizacion) FROM gestion_movimientomaterial", "diaria"),
    ("me2n", "Compras (ME2N)", "Compras, plazo de pago y OTIF", "TABLA_SQ01_ME2N",
     "SELECT MAX(fecha_documento) FROM gestion_lineacompra", "diaria"),
    ("zmm056", "Báscula (ZMM056)", "Ingresos físicos, calidad, premios y transporte", "TABLA_ZMM056",
     "SELECT MAX(fecha_ingreso) FROM gestion_ingresofisicosop", "diaria"),
    ("mb5b", "Inventario (MB5B)", "Stock y valor del inventario", "TABLA_MB5B",
     "SELECT MAX(fecha) FROM gestion_inventariomensual", "diaria"),
    ("fbl3n", "Ajustes de precio (FBL3N)", "Precio real, IPU y valores agregados del costo por GJ", "TABLA_FBL3N",
     "SELECT MAX(fecha_contabilizacion) FROM gestion_ajustepreciofbl3n", "mensual"),
    ("sop", "Plan S&OP", "Plan de abastecimiento y de consumos", "TABLA SOP ABASTECIMIENTO",
     "SELECT MAX(mes_snapshot) FROM gestion_planabastecimientosop", "sop"),
    ("precios_sop", "Precios S&OP", "Simulación del inventario valorizado", "TABLA SOP PRECIOS",
     "SELECT MAX(mes_snapshot) FROM gestion_preciosop", "sop"),
)
# Una fuente diaria sin datos nuevos en más de estos días va con aviso.
DIAS_SIN_DATOS = 3


def _fecha(valor):
    if valor is None or (isinstance(valor, float) and pd.isna(valor)):
        return None
    return pd.Timestamp(valor).date()


def _meses_texto(desde, hasta):
    """"Octubre - 2026" o "De Agosto - 2026 a Octubre - 2026"."""
    return formato_mes_anio(desde) if desde == hasta else f"De {formato_mes_anio(desde)} a {formato_mes_anio(hasta)}"


def evaluar_fuente(tipo, corte, hoy, referencia=None):
    """(estado, nota) de una fuente: "ok" o "aviso" y el porqué. `corte`: la
    última fecha (o mes de emisión) que trae; `referencia`: para Precios
    S&OP, el mes del último Plan S&OP."""
    if corte is None:
        return "aviso", "Todavía no tiene datos cargados."
    if tipo == "diaria":
        dias = (hoy - corte).days
        if dias > DIAS_SIN_DATOS:
            return "aviso", f"Sin datos nuevos desde el {corte:%d/%m/%Y} ({dias} días)."
        return "ok", ""
    mes_anterior = _sumar_meses(hoy.replace(day=1), -1)
    if tipo == "mensual":
        ultimo = corte.replace(day=1)
        if ultimo < mes_anterior:
            faltan = _meses_texto(_sumar_meses(ultimo, 1), mes_anterior)
            return "aviso", (f"{faltan} todavía sin ajustes: en {'ese mes' if faltan[:3] != 'De ' else 'esos meses'} "
                             "el precio real, el IPU y los valores agregados del costo por GJ van sin ellos.")
        return "ok", ""
    if referencia is not None and corte < referencia:
        return "aviso", (f"La última versión es la de {formato_mes_anio(corte)}; el S&OP de "
                         f"{formato_mes_anio(referencia)} todavía no tiene la suya.")
    if corte < mes_anterior:
        return "aviso", f"El último cargado es el de {formato_mes_anio(corte)}."
    return "ok", ""


@_cacheado
def estado_datos():
    """Estado de cada fuente: nombre, uso, hasta (fecha de corte o mes de
    emisión), hasta_texto, cargada (última carga de un archivo de esa
    fuente), estado ("ok"/"aviso") y nota; más datos_al (corte de los
    movimientos), ultima_carga y avisos (cuántas fuentes con aviso)."""
    hoy = datetime.date.today()
    cargas = _query("SELECT ruta, fecha_importado FROM gestion_archivoimportado")
    fuentes, cortes = [], {}
    for clave, nombre, uso, carpeta, consulta, tipo in FUENTES:
        corte = _fecha(_query(consulta).iloc[0, 0])
        cortes[clave] = corte
        de_la_fuente = cargas[cargas["ruta"].str.contains(carpeta, regex=False)] if not cargas.empty else cargas
        cargada = pd.Timestamp(de_la_fuente["fecha_importado"].max()).to_pydatetime() if not de_la_fuente.empty else None
        estado, nota = evaluar_fuente(tipo, corte, hoy, cortes.get("sop") if clave == "precios_sop" else None)
        if corte is None:
            hasta_texto = "—"
        elif tipo == "diaria":
            hasta_texto = f"{corte:%d/%m/%Y}"
        else:
            hasta_texto = (f"S&OP {formato_mes_anio(corte)}" if tipo == "sop" else formato_mes_anio(corte))
        fuentes.append({"clave": clave, "nombre": nombre, "uso": uso, "hasta": corte, "hasta_texto": hasta_texto,
                        "cargada": cargada, "estado": estado, "nota": nota})
    cargadas = [f["cargada"] for f in fuentes if f["cargada"]]
    return {"fuentes": fuentes, "datos_al": cortes.get("mb51"), "ultima_carga": max(cargadas) if cargadas else None,
            "avisos": sum(f["estado"] == "aviso" for f in fuentes)}
