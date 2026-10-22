"""Planificación S&OP > Inventario: cobertura de stock, proyección de inventario y diagnóstico mín–máx."""

import datetime

import pandas as pd

from .base import _cacheado, _lista_materiales, _query, _sumar_meses
from .importados import CODIGOS_ANTERIORES
from .inventario import _ventana_6_meses, catalogo_materiales, fechas_inventario_disponibles
from .sop_base import (
    _FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP, _MATERIALES_CONSUMO_VIA_OTRO, materiales_planificados_sop,
    pares_planificados_sop,
)

# Cobertura de stock (pedidos del usuario, 2026-10-21):
# - el Clinker de Atocongo no se muestra (lo produce la planta; el S&OP solo
#   planifica el que se traslada a Condorcocha);
# - el stock y el consumo de un código antiguo (CODIGOS_ANTERIORES, p.ej.
#   07-00000008 -> Pelets Chips Recuperados 07-00003147) se suman al nuevo;
# - un material sin consumo en los 6 meses se muestra igual si el S&OP lo
#   planifica en ese centro (p.ej. Puzolana Junín en Condorcocha, recién
#   comprada): con su stock y sin cobertura.
_COBERTURA_OCULTOS = {("14-00000037", "U001")}


def _con_anteriores(materiales):
    """`materiales` más sus códigos antiguos (CODIGOS_ANTERIORES)."""
    nuevos = set(materiales)
    return sorted(nuevos | {a for a, n in CODIGOS_ANTERIORES.items() if n in nuevos})


def _a_codigo_vigente(df, columna):
    """Pasa los códigos antiguos al nuevo y suma `columna` por material/centro."""
    if df.empty or not df["material_codigo"].isin(list(CODIGOS_ANTERIORES)).any():
        return df
    df = df.assign(material_codigo=df["material_codigo"].replace(CODIGOS_ANTERIORES))
    return df.groupby(["material_codigo", "centro"], as_index=False)[columna].sum()


def meses_inventario_disponibles():
    """Un dato por mes calendario para el selector de Cobertura de Stock: el
    mes en curso a veces tiene varias fechas de corte de MB5B (recargas
    intermedias), así que se queda con la más reciente de cada mes. Devuelve
    dict {mes (día 1): fecha real de esa foto de inventario}."""
    fechas = fechas_inventario_disponibles()
    por_mes = {}
    for f in fechas:
        clave = f.replace(day=1)
        if clave not in por_mes or f > por_mes[clave]:
            por_mes[clave] = f
    return por_mes


@_cacheado
def cobertura_stock_planificacion(fecha_stock=None, centro=None, material=None):
    """Cobertura de stock de los materiales del Plan de Abastecimiento SOP, por
    material y centro (Atocongo/Condorcocha), en dos esquemas a la vez (días y
    meses). Cobertura = Stock de cierre (a `fecha_stock`, o la más reciente
    cargada si no se indica) ÷ Consumo promedio de los 6 meses calendario que
    terminan en `fecha_stock` (ver _FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP)."""
    columnas = [
        "material_codigo", "texto_breve", "centro", "categoria", "familia",
        "stock_cierre", "consumo_diario", "consumo_mensual", "cobertura_dias", "cobertura_meses",
    ]
    if fecha_stock is None:
        fecha_df = _query("SELECT MAX(fecha) AS fecha FROM gestion_inventariomensual")
        fecha_stock = fecha_df["fecha"].iloc[0] if not fecha_df.empty else None
    if not fecha_stock:
        return pd.DataFrame(columns=columnas)

    catalogo = catalogo_materiales()
    materiales_sop = materiales_planificados_sop()
    if not materiales_sop:
        return pd.DataFrame(columns=columnas)
    ids = ",".join(f"'{m}'" for m in _con_anteriores(materiales_sop))

    fecha_inicio, fecha_fin = _ventana_6_meses(
        fecha_stock if isinstance(fecha_stock, datetime.date) else datetime.date.fromisoformat(str(fecha_stock)[:10])
    )
    dias_ventana = (fecha_fin - fecha_inicio).days + 1

    stock = _query(f"""
        SELECT material_codigo AS material_codigo, area_valoracion AS centro,
               SUM(stock_cierre) AS stock_cierre
        FROM gestion_inventariomensual
        WHERE material_codigo IN ({ids}) AND area_valoracion IN ('U001', 'U002') AND fecha = %s
        GROUP BY material_codigo, area_valoracion
    """, [fecha_stock])

    # "+centro" (y no "centro") en las dos consultas de consumo de acá abajo: el
    # "+" solo le quita a SQLite la opción de usar el índice por centro, que
    # elegía por su cuenta y lo hacía recorrer casi todo MB51 de U001/U002
    # (~3.5 M filas); así usa el de (material, fecha), que con ~23 materiales
    # del Plan SOP es muchísimo más selectivo (99 s -> 5 s para 3 años). Mismo
    # resultado.
    consumo = _query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro,
               -SUM(cantidad) AS consumo_total
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({ids}) AND +centro IN ('U001', 'U002')
              AND {_FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP}
              AND fecha_contabilizacion BETWEEN %s AND %s
        GROUP BY material_codigo, centro
    """, [fecha_inicio, fecha_fin])

    extras = {}
    for origen, (mat_real, centro_real) in _MATERIALES_CONSUMO_VIA_OTRO.items():
        extra = _query(f"""
            SELECT -SUM(cantidad) AS consumo_total
            FROM gestion_movimientomaterial
            WHERE material_codigo = %s AND +centro = %s
                  AND {_FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP}
                  AND fecha_contabilizacion BETWEEN %s AND %s
        """, [mat_real, centro_real, fecha_inicio, fecha_fin])
        extras[origen] = float(extra["consumo_total"].iloc[0] or 0.0) if not extra.empty else 0.0
    consumo = _sumar_consumo_via_otro(consumo, extras)

    df = _armar_cobertura(stock, consumo, dias_ventana, catalogo)
    if centro:
        df = df[df["centro"] == centro]
    if material:
        df = df[df["material_codigo"].isin(_lista_materiales(material))]
    return df.reset_index(drop=True)


def cobertura_stock_planificacion_por_mes(fechas_stock):
    """Lo mismo que cobertura_stock_planificacion(fecha_stock=f) para varias
    fechas a la vez, devuelto como {fecha_stock: DataFrame}. Para el HTML
    estático (exportar_planificacion_html), que necesita la cobertura de todos
    los meses de inventario: llamando a la función de un mes por cada uno, cada
    llamada volvía a recorrer 6 meses de MB51 (~33 meses x 6 = el mismo
    movimiento leído hasta 6 veces, ~5 min). Acá el consumo se agrupa por mes
    calendario en UNA consulta y cada ventana de 6 meses se suma en memoria —
    da el mismo resultado porque la ventana siempre abarca meses completos.
    Ver el comentario de "+centro" en cobertura_stock_planificacion."""
    fechas = sorted({
        f if isinstance(f, datetime.date) else datetime.date.fromisoformat(str(f)[:10]) for f in fechas_stock
    })
    if not fechas:
        return {}
    catalogo = catalogo_materiales()
    materiales_sop = materiales_planificados_sop()
    if not materiales_sop:
        return {}
    sop = set(_con_anteriores(materiales_sop))
    ids_sop = ",".join(f"'{m}'" for m in sorted(sop))
    reales = {mat for mat, _centro in _MATERIALES_CONSUMO_VIA_OTRO.values()}
    ids_consumo = ",".join(f"'{m}'" for m in sorted(sop | reales))

    stock_todos = _query(f"""
        SELECT fecha AS fecha, material_codigo AS material_codigo, area_valoracion AS centro,
               SUM(stock_cierre) AS stock_cierre
        FROM gestion_inventariomensual
        WHERE material_codigo IN ({ids_sop}) AND area_valoracion IN ('U001', 'U002')
              AND fecha IN ({",".join(["%s"] * len(fechas))})
        GROUP BY fecha, material_codigo, area_valoracion
    """, fechas)
    stock_todos["fecha"] = stock_todos["fecha"].astype(str).str[:10]

    consumo_mes = _query(f"""
        SELECT substr(fecha_contabilizacion, 1, 7) AS mes, material_codigo AS material_codigo,
               centro AS centro, -SUM(cantidad) AS consumo_total
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({ids_consumo}) AND +centro IN ('U001', 'U002')
              AND {_FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP}
              AND fecha_contabilizacion BETWEEN %s AND %s
        GROUP BY mes, material_codigo, centro
    """, [_ventana_6_meses(fechas[0])[0], _ventana_6_meses(fechas[-1])[1]])

    resultado = {}
    for fecha_stock in fechas:
        fecha_inicio, fecha_fin = _ventana_6_meses(fecha_stock)
        meses_ventana = {
            f"{(fecha_inicio.year * 12 + fecha_inicio.month - 1 + i) // 12:04d}-"
            f"{(fecha_inicio.month - 1 + i) % 12 + 1:02d}"
            for i in range(6)
        }
        en_ventana = consumo_mes[consumo_mes["mes"].isin(meses_ventana)]
        consumo = (
            en_ventana[en_ventana["material_codigo"].isin(sop)]
            .groupby(["material_codigo", "centro"], as_index=False)["consumo_total"].sum()
        )
        extras = {
            origen: float(en_ventana.loc[
                (en_ventana["material_codigo"] == mat_real) & (en_ventana["centro"] == centro_real),
                "consumo_total",
            ].sum())
            for origen, (mat_real, centro_real) in _MATERIALES_CONSUMO_VIA_OTRO.items()
        }
        consumo = _sumar_consumo_via_otro(consumo, extras)
        stock = stock_todos[stock_todos["fecha"] == fecha_stock.isoformat()].drop(columns="fecha")
        resultado[fecha_stock] = _armar_cobertura(
            stock, consumo, (fecha_fin - fecha_inicio).days + 1, catalogo
        ).reset_index(drop=True)
    return resultado


def _sumar_consumo_via_otro(consumo, extras):
    """Suma a cada material de _MATERIALES_CONSUMO_VIA_OTRO el consumo del
    material por el que realmente sale al proceso (`extras`: {(material,
    centro): consumo}), agregando la fila si no tenía consumo propio."""
    for (mat_origen, centro_origen), valor_extra in extras.items():
        if valor_extra == 0.0:
            continue
        mascara = (consumo["material_codigo"] == mat_origen) & (consumo["centro"] == centro_origen)
        if mascara.any():
            consumo.loc[mascara, "consumo_total"] += valor_extra
        else:
            fila_nueva = pd.DataFrame([{"material_codigo": mat_origen, "centro": centro_origen, "consumo_total": valor_extra}])
            consumo = pd.concat([consumo, fila_nueva], ignore_index=True)
    return consumo


def _armar_cobertura(stock, consumo, dias_ventana, catalogo):
    """Parte común de cobertura_stock_planificacion y su versión por mes: con
    el stock y el consumo total de la ventana por material/centro, calcula las
    coberturas y agrega descripción/categoría/familia del maestro."""
    columnas = [
        "material_codigo", "texto_breve", "centro", "categoria", "familia",
        "stock_cierre", "consumo_diario", "consumo_mensual", "cobertura_dias", "cobertura_meses",
    ]
    stock = _a_codigo_vigente(stock, "stock_cierre")
    consumo = _a_codigo_vigente(consumo, "consumo_total")
    df = stock.merge(consumo, on=["material_codigo", "centro"], how="outer")
    claves = list(zip(df["material_codigo"], df["centro"]))
    df = df[[k not in _COBERTURA_OCULTOS for k in claves]]
    planificados = pares_planificados_sop()
    df = df.assign(_planificado=[k in planificados for k in zip(df["material_codigo"], df["centro"])])
    df["stock_cierre"] = df["stock_cierre"].fillna(0.0).astype(float)
    df["consumo_total"] = df["consumo_total"].fillna(0.0).astype(float)
    df["consumo_diario"] = df["consumo_total"] / dias_ventana
    df["cobertura_dias"] = (df["stock_cierre"] / df["consumo_diario"]).where(df["consumo_diario"] > 0)
    # Mismo cálculo en meses (igual criterio que rotacion_inventario_por_material:
    # promedio de los 6 meses calendario), para tener las dos lecturas juntas
    # en la tabla. Pedido por el usuario.
    df["consumo_mensual"] = df["consumo_total"] / 6.0
    df["cobertura_meses"] = (df["stock_cierre"] / df["consumo_mensual"]).where(df["consumo_mensual"] > 0)

    df = df.merge(catalogo[["material_codigo", "texto_breve", "categoria", "familia"]], on="material_codigo", how="left")
    # El material_codigo ya viene acotado al Plan de Abastecimiento SOP (arriba),
    # así que no hace falta filtrar por categoría — de hecho no se puede: el
    # Clinker (14-00000037) no tiene categoría en el maestro (es un intermedio
    # de producción, no se compra con grupo de artículo) y un filtro por
    # categoría lo excluiría en silencio, igual que pasó una vez en Plan vs
    # Real. Mismo fallback que allá. Definido por el usuario.
    df.loc[df["categoria"].isna(), "categoria"] = "MATERIAS PRIMAS"
    # Mismo caso: sin familia en el maestro. A diferencia de categoría, acá no
    # hay un valor de reemplazo razonable — se deja en blanco (NaN se vería
    # como "nan" literal en la plantilla si no se limpia).
    df["familia"] = df["familia"].fillna("")
    df["texto_breve"] = df["texto_breve"].fillna("")
    # Sin consumo en la ventana de 6 meses = sin señal real para calcular
    # cobertura (aunque tenga stock): no interesa en esta tabla. Pedido por el
    # usuario (ej. Puzolana de Terceros/Escoria de Altos Hornos en U002). Salvo
    # que el S&OP lo planifique en ese centro (ver _COBERTURA_OCULTOS arriba).
    df = df[(df["consumo_total"] != 0) | (df["_planificado"] & (df["stock_cierre"] != 0))]
    return df[columnas].sort_values("cobertura_dias", ascending=True, na_position="last")


# ---------------------------------------------------------------------------
# Evolución de inventario proyectada (S&OP). Para el S&OP de un mes (p.ej.
# Setiembre 2026), el punto de partida es el inventario real de cierre del mes
# anterior (MB5B de Agosto 2026), y cada mes proyectado se calcula como:
#     Stock final = Stock inicial + Abastecimiento S&OP − Consumo S&OP
# encadenando el stock final de un mes como inicial del siguiente, hasta el
# último mes que traigan los planes (hoy, Diciembre 2027). Definido por el
# usuario.
# ---------------------------------------------------------------------------

def snapshots_sop_disponibles():
    """Meses de emisión del S&OP con Plan de Abastecimiento y/o de Consumos."""
    df = _query("""
        SELECT mes_snapshot AS mes FROM gestion_planabastecimientosop
        UNION
        SELECT mes_snapshot AS mes FROM gestion_planconsumosop
        ORDER BY mes
    """)
    return [datetime.date.fromisoformat(str(m)[:10]) for m in df["mes"]]


def fecha_inventario_inicial_sop(mes_snapshot):
    """Fecha del inventario (MB5B) de cierre del mes anterior a `mes_snapshot`,
    o None si ese cierre todavía no está cargado."""
    mes_anterior = (mes_snapshot - datetime.timedelta(days=1)).replace(day=1)
    return meses_inventario_disponibles().get(mes_anterior)


def _plan_sop_por_mes(tabla, columna, mes_snapshot):
    return _query(f"""
        SELECT material_codigo AS material_codigo, centro_destino AS centro,
               mes_planificado AS mes, SUM({columna}) AS cantidad,
               MAX(material_descripcion) AS descripcion
        FROM {tabla}
        WHERE mes_snapshot = %s
        GROUP BY material_codigo, centro_destino, mes_planificado
    """, [mes_snapshot])


# Traslados entre centros: el Carbón Importado llega por barco a Atocongo y lo
# que el S&OP planifica "abastecer" a Condorcocha no es una compra aparte, es
# un traslado desde el stock de Atocongo. Sin esto, cada embarque se contaba
# dos veces (en Atocongo y otra vez en Condorcocha) y Atocongo se veía
# acumulando carbón sin fin. {(material, centro que recibe): centro que envía}.
# Definido por el usuario.
_TRASLADOS_SOP = {
    ("06-00000003", "U002"): "U001",
}

# Lead time (meses) para cumplir la cuota de abastecimiento de un mes: lo que
# está dentro de ese plazo ya está comprometido (en tránsito / en ejecución) y
# no se puede cambiar. Todos los materiales tienen 1 mes; los importados más.
# Definido por el usuario.
LEAD_TIME_SOP_DEFECTO = 1
LEAD_TIME_SOP = {
    "06-00000003": 3,  # Carbón importado
    "07-00001301": 4,  # Escoria japonesa (altos hornos)
}


def lead_time_sop(material):
    return LEAD_TIME_SOP.get(material, LEAD_TIME_SOP_DEFECTO)


@_cacheado
def proyeccion_inventario_sop(mes_snapshot):
    """Una fila por material/centro/mes proyectado: stock_inicial,
    abastecimiento, consumo y stock_final (toneladas). Materiales: los que
    trae el Plan de Abastecimiento y/o el de Consumos de ese S&OP, con el
    inventario inicial en 0 si no tenían stock de cierre (y siempre en 0 para
    los que no tienen Cod. SAP, p.ej. CALCEM: ver PREFIJO_SIN_CODIGO_SOP)."""
    columnas = [
        "material_codigo", "texto_breve", "centro", "categoria", "familia",
        "mes", "stock_inicial", "abastecimiento", "consumo", "traslado", "traslado_recibido", "stock_final",
        "stock_minimo", "stock_maximo", "estado",
    ]
    abast = _plan_sop_por_mes("gestion_planabastecimientosop", "cantidad_plan", mes_snapshot)
    cons = _plan_sop_por_mes("gestion_planconsumosop", "cantidad_consumo", mes_snapshot)
    if abast.empty and cons.empty:
        return pd.DataFrame(columns=columnas)

    for df in (abast, cons):
        df["mes"] = pd.to_datetime(df["mes"]).dt.date
        df["cantidad"] = df["cantidad"].fillna(0.0).astype(float)

    # Carbón Terceros (06-00000005/U002) y su Chancado (14-00001112/U002, ver
    # _MATERIALES_CONSUMO_VIA_OTRO) son el mismo carbón: se proyectan como UNA
    # sola fila, la del material que se compra (06-00000005), con:
    #   - stock inicial = stock de los dos,
    #   - abastecimiento = el del 06-00000005,
    #   - consumo = SOLO el del chancado (el que realmente va al proceso; el
    #     Excel de Consumos trae el mismo número en las dos filas, y sumar o
    #     mezclar ambos confunde).
    # Definido por el usuario.
    def _es(df, clave):
        return (df["material_codigo"] == clave[0]) & (df["centro"] == clave[1])

    fusiones = {}
    for origen, real in _MATERIALES_CONSUMO_VIA_OTRO.items():
        aparece_real = _es(cons, real).any() or _es(abast, real).any()
        aparece_origen = _es(cons, origen).any() or _es(abast, origen).any()
        if not (aparece_real and aparece_origen):
            continue
        fusiones[real] = origen
        if _es(cons, real).any():
            cons = cons[~_es(cons, origen)]
        for df in (abast, cons):
            df.loc[_es(df, real), "material_codigo"] = origen[0]
            df.loc[_es(df, (origen[0], real[1])), "centro"] = origen[1]
    abast = abast.groupby(["material_codigo", "centro", "mes"], as_index=False).agg(
        cantidad=("cantidad", "sum"), descripcion=("descripcion", "last"))
    cons = cons.groupby(["material_codigo", "centro", "mes"], as_index=False).agg(
        cantidad=("cantidad", "sum"), descripcion=("descripcion", "last"))
    meses = sorted(m for m in set(abast["mes"]) | set(cons["mes"]) if m >= mes_snapshot)

    claves = sorted(
        set(zip(abast["material_codigo"], abast["centro"])) | set(zip(cons["material_codigo"], cons["centro"]))
    )
    codigos_stock = {m for m, _ in claves} | {real[0] for real in fusiones}
    ids = ",".join(f"'{m}'" for m in _con_anteriores(codigos_stock))  # con los códigos antiguos (Pelets)
    fecha_inicial = fecha_inventario_inicial_sop(mes_snapshot)
    stock = {}
    if fecha_inicial:
        df_stock = _query(f"""
            SELECT material_codigo AS material_codigo, area_valoracion AS centro,
                   SUM(stock_cierre) AS stock
            FROM gestion_inventariomensual
            WHERE fecha = %s AND material_codigo IN ({ids}) AND area_valoracion IN ('U001', 'U002')
            GROUP BY material_codigo, area_valoracion
        """, [fecha_inicial])
        df_stock = _a_codigo_vigente(df_stock.fillna({"stock": 0.0}), "stock")
        stock = {(r.material_codigo, r.centro): float(r.stock or 0.0) for r in df_stock.itertuples()}
        for real, origen in fusiones.items():
            stock[origen] = stock.get(origen, 0.0) + stock.pop(real, 0.0)

    a = {(r.material_codigo, r.centro, r.mes): r.cantidad for r in abast.itertuples()}
    c = {(r.material_codigo, r.centro, r.mes): r.cantidad for r in cons.itertuples()}
    descripcion_plan = {}
    for df in (cons, abast):  # la de Abastecimiento pisa a la de Consumos
        descripcion_plan.update({(r.material_codigo, r.centro): r.descripcion for r in df.itertuples()})

    # Salidas por traslado: lo que recibe un centro por traslado sale del stock
    # del centro que lo envía, en el mismo mes (ver _TRASLADOS_SOP).
    t = {}
    for (material, recibe), envia in _TRASLADOS_SOP.items():
        for (mat, centro, mes), cantidad in a.items():
            if mat == material and centro == recibe:
                t[(material, envia, mes)] = t.get((material, envia, mes), 0.0) + cantidad
                claves = sorted(set(claves) | {(material, envia)})

    filas = []
    for material, centro in claves:
        saldo = stock.get((material, centro), 0.0)
        for mes in meses:
            entra = a.get((material, centro, mes), 0.0)
            sale = c.get((material, centro, mes), 0.0)
            traslado = t.get((material, centro, mes), 0.0)
            final = saldo + entra - sale - traslado
            filas.append({
                "material_codigo": material, "centro": centro, "mes": mes,
                "stock_inicial": saldo, "abastecimiento": entra, "consumo": sale, "traslado": traslado,
                # Parte del abastecimiento que en realidad es un traslado desde
                # otro centro: al sumar los dos centros juntos, se descuenta de
                # abastecimiento y de traslados (es un movimiento interno).
                "traslado_recibido": entra if (material, centro) in _TRASLADOS_SOP else 0.0,
                "stock_final": final, "descripcion_plan": descripcion_plan.get((material, centro), ""),
            })
            saldo = final

    df = pd.DataFrame(filas)
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "categoria", "familia"]]
    df = df.merge(catalogo, on="material_codigo", how="left")
    df["texto_breve"] = df["texto_breve"].where(df["texto_breve"].notna() & (df["texto_breve"] != ""), df["descripcion_plan"])
    # Mismo criterio que el resto de Planificación: todo lo que trae el S&OP
    # es de este dominio; sin categoría en el maestro (Clinker) = MATERIAS PRIMAS.
    df.loc[df["categoria"].isna(), "categoria"] = "MATERIAS PRIMAS"
    df["familia"] = df["familia"].fillna("")

    minmax = minmax_sop(mes_snapshot)
    if minmax.empty:
        df["stock_minimo"] = float("nan")
        df["stock_maximo"] = float("nan")
    else:
        df = df.merge(minmax, on=["material_codigo", "centro", "mes"], how="left")
    df["estado"] = [
        _estado_stock(final, minimo, maximo)
        for final, minimo, maximo in zip(df["stock_final"], df["stock_minimo"], df["stock_maximo"])
    ]
    return df[columnas].sort_values(["centro", "material_codigo", "mes"]).reset_index(drop=True)


ESTADOS_STOCK = {
    "quiebre": "Quiebre (stock negativo)",
    "bajo_minimo": "Bajo mínimo",
    "en_rango": "En rango",
    "sobre_maximo": "Sobre máximo",
    "sin_parametro": "Sin mín/máx",
}


def _estado_stock(final, minimo, maximo):
    if final < 0:
        return "quiebre"
    if minimo == minimo and minimo is not None and final < minimo:
        return "bajo_minimo"
    if maximo == maximo and maximo is not None and final > maximo:
        return "sobre_maximo"
    if (minimo == minimo and minimo is not None) or (maximo == maximo and maximo is not None):
        return "en_rango"
    return "sin_parametro"


def minmax_sop(mes_snapshot):
    """Stock mínimo/máximo por material/centro/mes para el S&OP de
    `mes_snapshot`: el archivo de ese mes de emisión, o el más reciente
    anterior si todavía no se cargó uno nuevo. Un mes vacío repite el último
    valor definido del material (solo se escribe el mes en que cambia).
    Definido por el usuario."""
    df = _query("""
        SELECT material_codigo AS material_codigo, centro_destino AS centro, mes_planificado AS mes,
               stock_minimo AS stock_minimo, stock_maximo AS stock_maximo
        FROM gestion_stockminmaxsop
        WHERE mes_snapshot = (
            SELECT COALESCE(
                (SELECT MAX(mes_snapshot) FROM gestion_stockminmaxsop WHERE mes_snapshot <= %s),
                (SELECT MIN(mes_snapshot) FROM gestion_stockminmaxsop)
            )
        )
        ORDER BY material_codigo, centro_destino, mes_planificado
    """, [mes_snapshot])
    if df.empty:
        return df
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    for columna in ("stock_minimo", "stock_maximo"):
        df[columna] = pd.to_numeric(df[columna], errors="coerce").astype(float)
        df[columna] = df.groupby(["material_codigo", "centro"])[columna].ffill()
    return df


# Clasificación del diagnóstico de mínimos/máximos, de más a menos urgente.
DIAGNOSTICOS_MINMAX = {
    "quiebre": "Riesgo de quiebre",
    "bajo_minimo": "Cae bajo el mínimo",
    "sobrecubierto": "Sobrecubierto",
    "sobre_maximo_puntual": "Sobre máximo en algunos meses",
    "equilibrado": "Equilibrado",
    "sin_parametro": "Sin mín/máx",
}
# Sobrecubierto = sobre el máximo en al menos la mitad de los meses proyectados.
_UMBRAL_SOBRECUBIERTO = 0.5


@_cacheado
def diagnostico_minmax_sop(mes_snapshot):
    """Una fila por material/centro con el diagnóstico de su stock proyectado
    frente a su mínimo/máximo en todo el horizonte del S&OP. Considera el lead
    time (lead_time_sop): la cuota de los meses dentro de ese plazo ya está
    comprometida y no se puede cambiar, así que:
    - primer_mes_modificable = mes del S&OP + lead time;
    - faltante: abastecimiento adicional (a más tardar en el primer mes
      modificable) para no bajar del mínimo desde ahí = max(mínimo − stock);
    - deficit_en_plazo: lo que falta bajo el mínimo en meses que ya no alcanza
      el lead time (solo se puede mitigar con medidas extraordinarias);
    - pedir_a_mas_tardar: para el primer mes bajo el mínimo que sí alcanza a
      corregirse, mes límite para colocar el pedido (ese mes − lead time);
    - recorte_posible: abastecimiento que se podría dejar de traer desde el
      primer mes modificable sin bajar del mínimo en ningún mes, y sin
      recortar más de lo que hay planificado desde ese mes;
    - exceso_maximo: el mayor exceso sobre el máximo (el máximo es un
      objetivo, no un límite de almacén: sobre el máximo = capital
      inmovilizado). Pedido por el usuario."""
    df = proyeccion_inventario_sop(mes_snapshot)
    if df.empty:
        return pd.DataFrame()
    filas = []
    for (material, centro), g in df.groupby(["material_codigo", "centro"]):
        g = g.sort_values("mes").reset_index(drop=True)
        meses = len(g)
        conteo = g["estado"].value_counts()
        tiene_min = g["stock_minimo"].notna().any()
        tiene_max = g["stock_maximo"].notna().any()
        lead_time = lead_time_sop(material)
        primer_modificable = _sumar_meses(mes_snapshot, lead_time)
        modificable = g["mes"] >= primer_modificable
        holgura_min = (g["stock_final"] - g["stock_minimo"]) if tiene_min else None
        exceso_max = (g["stock_final"] - g["stock_maximo"]) if tiene_max else None
        bajo = g[g["estado"].isin(["bajo_minimo", "quiebre"])]
        meses_sobre = int(conteo.get("sobre_maximo", 0))

        faltante = deficit_en_plazo = recorte = 0.0
        pedir = None
        if tiene_min:
            h_mod, h_plazo = holgura_min[modificable], holgura_min[~modificable]
            if not h_plazo.empty and h_plazo.min() < 0:
                deficit_en_plazo = float(-h_plazo.min())
            if not h_mod.empty and h_mod.min() < 0:
                faltante = float(-h_mod.min())
                primer_bajo = g.loc[modificable & (holgura_min < 0), "mes"].iloc[0]
                pedir = _sumar_meses(primer_bajo, -lead_time)
            if not h_mod.empty and h_mod.min() > 0:
                recorte = min(float(h_mod.min()), float(g.loc[modificable, "abastecimiento"].sum()))

        if not (tiene_min or tiene_max):
            clave = "sin_parametro"
        elif conteo.get("quiebre", 0):
            clave = "quiebre"
        elif conteo.get("bajo_minimo", 0):
            clave = "bajo_minimo"
        elif meses_sobre >= meses * _UMBRAL_SOBRECUBIERTO:
            clave = "sobrecubierto"
        elif meses_sobre:
            clave = "sobre_maximo_puntual"
        else:
            clave = "equilibrado"

        filas.append({
            "material_codigo": material, "texto_breve": g["texto_breve"].iloc[0], "centro": centro,
            "categoria": g["categoria"].iloc[0],
            "stock_minimo": g["stock_minimo"].iloc[0], "stock_maximo": g["stock_maximo"].iloc[0],
            "stock_hoy": float(g["stock_inicial"].iloc[0]),
            "stock_promedio": float(g["stock_final"].mean()),
            "stock_final": float(g["stock_final"].iloc[-1]),
            "meses": meses,
            "meses_bajo_minimo": int(len(bajo)),
            "meses_en_rango": int(conteo.get("en_rango", 0)),
            "meses_sobre_maximo": meses_sobre,
            "primer_mes_bajo_minimo": bajo["mes"].iloc[0] if not bajo.empty else None,
            "lead_time": lead_time,
            "primer_mes_modificable": primer_modificable,
            "pedir_a_mas_tardar": pedir,
            "exceso_maximo": float(exceso_max.max()) if tiene_max and exceso_max.max() > 0 else 0.0,
            "recorte_posible": recorte,
            "faltante": faltante,
            "deficit_en_plazo": deficit_en_plazo,
            "diagnostico": clave,
            "diagnostico_texto": DIAGNOSTICOS_MINMAX[clave],
        })
    orden = {clave: i for i, clave in enumerate(DIAGNOSTICOS_MINMAX)}
    resultado = pd.DataFrame(filas)
    resultado["_orden"] = resultado["diagnostico"].map(orden)
    return resultado.sort_values(["_orden", "exceso_maximo", "faltante"], ascending=[True, False, False]).drop(columns="_orden").reset_index(drop=True)
