"""Planificación S&OP > Cumplimiento: plan de abastecimiento vs ingresos físicos (ZMM056), entregas por proveedor, viajes de recepción y traslados."""

import datetime

import pandas as pd

from .base import _cacheado, _en_alcance, _limites_mes, _lista_materiales, _query
from .inventario import catalogo_materiales
from .sop_base import CATEGORIAS_PLANIFICACION_SOP, FECHA_INGRESO_EFECTIVA, _FILTRO_CON_CODIGO_SOP, _IN_TIPOS_OPERACION_RECEPCION, materiales_planificados_sop


def meses_planificacion_disponibles():
    """Unión de meses con Plan de Abastecimiento y/o Ingresos Físicos
    cargados, para el segmentador de Mes de Planificación."""
    plan = _query("SELECT DISTINCT mes_planificado AS mes FROM gestion_planabastecimientosop")
    meses_plan = plan["mes"].tolist()

    ingresos = _query(
        "SELECT DISTINCT strftime('%Y-%m-01', fecha_ingreso) AS mes FROM gestion_ingresofisicosop "
        "WHERE fecha_ingreso IS NOT NULL"
    )
    meses_ingresos = pd.to_datetime(ingresos["mes"]).dt.date.tolist() if not ingresos.empty else []

    return sorted(set(meses_plan) | set(meses_ingresos))


def descripcion_centros_planificacion():
    """Mapea centro_destino -> nombre de planta (columna 'Destino' del Excel
    de Plan de Abastecimiento SOP, p.ej. U001 -> ATOCONGO), para mostrar un
    título legible en Detalle por material/Evaluación diaria."""
    df = _query("SELECT DISTINCT centro_destino AS centro, destino FROM gestion_planabastecimientosop")
    return dict(zip(df["centro"], df["destino"]))


def plan_abastecimiento_por_material(mes):
    """Plan de abastecimiento (S&OP) por material/centro para `mes`, siempre
    con la versión más reciente del plan cargado (el snapshot más nuevo): si
    se recarga el plan al mes siguiente, la proyección se actualiza sola, no
    queda una foto vieja congelada."""
    return _en_alcance(_query(f"""
        SELECT material_codigo AS material_codigo, centro_destino AS centro,
               SUM(cantidad_plan) AS cantidad_plan
        FROM gestion_planabastecimientosop
        WHERE mes_planificado = %s AND {_FILTRO_CON_CODIGO_SOP}
              AND mes_snapshot = (SELECT MAX(mes_snapshot) FROM gestion_planabastecimientosop)
        GROUP BY material_codigo, centro_destino
    """, [mes]))


def ingresos_fisicos_por_material(mes):
    """Ingresos físicos reales de Recepción (báscula, ZMM056 — solo Tipo de
    operación 1/5, ver _TIPOS_OPERACION_RECEPCION) por material/centro en el
    mes calendario `mes` (por FECHA_INGRESO_EFECTIVA). Definido por el
    usuario: no se toma de MB51 para estos materiales."""
    fecha_inicio, fecha_fin = _limites_mes(mes)
    return _en_alcance(_query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro,
               SUM(cantidad) AS cantidad_real
        FROM gestion_ingresofisicosop
        WHERE {FECHA_INGRESO_EFECTIVA} BETWEEN %s AND %s
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION})
        GROUP BY material_codigo, centro
    """, [fecha_inicio, fecha_fin]))


def ingresos_fisicos_por_proveedor(mes):
    """Ingresos físicos de Recepción (mismas reglas que
    ingresos_fisicos_por_material) por material/centro/proveedor en el mes
    `mes`, con la cantidad de carros (tickets). En los ingresos propios (Tipo
    5: cantera o traslado entre plantas) el "proveedor" de ZMM056 es la planta
    de origen (ATOCONGO, PUCARA...). Usado por el Detalle por material de
    Cumplimiento. Pedido por el usuario (después llegará el plan por proveedor)."""
    fecha_inicio, fecha_fin = _limites_mes(mes)
    return _en_alcance(_query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro, proveedor_codigo AS proveedor_codigo,
               proveedor AS proveedor, MAX(tipo_operacion) AS tipo_operacion,
               SUM(cantidad) AS cantidad_real, COUNT(*) AS carros
        FROM gestion_ingresofisicosop
        WHERE {FECHA_INGRESO_EFECTIVA} BETWEEN %s AND %s
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION})
        GROUP BY material_codigo, centro, proveedor_codigo, proveedor
    """, [fecha_inicio, fecha_fin]))


@_cacheado
def entregas_por_proveedor_sop():
    """Entregas mes a mes por material/centro/proveedor (ZMM056, mismas reglas
    de Recepción que ingresos_fisicos_por_material: Tipo 1/5), en toneladas y
    carros, de los materiales de Planificación (misma categoría efectiva que
    Plan vs Real). En los Tipo 5 el "proveedor" es la planta o cantera de
    origen. Para Evolución de entregas por proveedor (pestaña Cumplimiento).
    Pedido por el usuario."""
    columnas = ["material_codigo", "texto_breve", "centro", "proveedor_codigo", "proveedor", "tipo",
                "mes", "toneladas", "carros"]
    df = _query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro, proveedor_codigo AS proveedor_codigo,
               proveedor AS proveedor, MAX(tipo_operacion) AS tipo_operacion,
               MAX(descripcion_material) AS descripcion,
               substr({FECHA_INGRESO_EFECTIVA}, 1, 7) || '-01' AS mes, SUM(cantidad) AS toneladas, COUNT(*) AS carros
        FROM gestion_ingresofisicosop
        WHERE fecha_ingreso IS NOT NULL AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION})
        GROUP BY material_codigo, centro, proveedor_codigo, proveedor, mes
    """)
    if df.empty:
        return pd.DataFrame(columns=columnas)
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    df["toneladas"] = df["toneladas"].fillna(0.0).astype(float)
    df["carros"] = df["carros"].astype(int)
    df["proveedor"] = df["proveedor"].fillna("").str.strip().replace("", "(sin proveedor)")
    df["tipo"] = df["tipo_operacion"].map(lambda t: "Propio" if t == "5" else "Terceros")
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "categoria"]].drop_duplicates("material_codigo")
    df = df.merge(catalogo, on="material_codigo", how="left")
    df["texto_breve"] = df["texto_breve"].where(df["texto_breve"].notna() & (df["texto_breve"] != ""), df["descripcion"]).fillna("")
    sin_categoria_en_plan = df["categoria"].isna() & df["material_codigo"].isin(materiales_planificados_sop())
    df.loc[sin_categoria_en_plan, "categoria"] = "MATERIAS PRIMAS"
    df = _en_alcance(df[df["categoria"].isin(CATEGORIAS_PLANIFICACION_SOP)])
    return df[columnas].sort_values(["mes", "material_codigo", "centro", "proveedor"]).reset_index(drop=True)


# Materiales que Condorcocha recibe SOLO por traslado desde Atocongo: todo su
# plan de abastecimiento en U002 es traslado (el clinker se produce en
# Atocongo; el Carbón Importado llega por barco a Atocongo, ver
# _TRASLADOS_SOP). El Carbón Trujillo también se traslada a veces, pero su plan
# en U002 mezcla compras directas: no se puede medir como traslado. Para el
# indicador de Traslados de la pestaña Indicadores. Visto en ZMM056 (Tipo 5,
# "proveedor" = planta de origen).
CENTRO_ATOCONGO, CENTRO_CONDORCOCHA = "U001", "U002"
MATERIALES_TRASLADO_ATOCONGO_CONDORCOCHA = ("14-00000037", "06-00000003")

# Transporte (indicadores de Logística de Materias Primas, pedido del usuario
# 2026-10-19). Menos de este tiempo entre el pesaje lleno y el vacío = el
# camión se pesó una sola vez con su tara registrada (en Atocongo, la Caliza
# Pucará, Andina y Roca Fuerte): no mide el tiempo en planta. Más de un día =
# dato de báscula erróneo.
MINUTOS_TARA_REGISTRADA = 2
MINUTOS_MAXIMO_EN_PLANTA = 24 * 60


@_cacheado
def viajes_recepcion():
    """Un ticket de recepción (tipo 1/5) por fila, de los (material, centro)
    del alcance, con lo del transporte: centro, material_codigo,
    descripcion, fecha (efectiva), mes, toneladas (húmedas), toneladas_secas
    (None si el ticket no trae peso seco), proveedor, transportista_codigo,
    transportista (nombre del ticket; si no trae, el del maestro de
    proveedores de ME2N o de la ZMM056), placa, llegada (pesaje lleno, hora
    local), minutos (entre el pesaje lleno y el vacío; None si no se puede
    medir) y tara_registrada (se pesó una sola vez)."""
    df = _query(f"""
        SELECT centro AS centro, material_codigo AS material_codigo, descripcion_material AS descripcion,
               {FECHA_INGRESO_EFECTIVA} AS fecha, cantidad AS toneladas, cantidad_seca AS toneladas_secas,
               proveedor AS proveedor, transportista_codigo AS transportista_codigo, transportista AS transportista,
               placa AS placa, fecha_pesaje_lleno AS f_lleno, hora_pesaje_lleno AS h_lleno,
               fecha_pesaje_vacio AS f_vacio, hora_pesaje_vacio AS h_vacio
        FROM gestion_ingresofisicosop
        WHERE tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION}) AND fecha_ingreso IS NOT NULL
    """)
    df = _en_alcance(df)
    if df.empty:
        return df
    nombres = _query("""
        SELECT proveedor_codigo AS codigo, MAX(proveedor) AS nombre FROM gestion_ingresofisicosop
        WHERE proveedor_codigo <> '' AND proveedor <> '' GROUP BY proveedor_codigo
        UNION ALL
        SELECT proveedor AS codigo, MAX(cuenta_proveedor) AS nombre FROM gestion_lineacompra
        WHERE proveedor <> '' AND cuenta_proveedor <> '' GROUP BY proveedor
    """).drop_duplicates("codigo").set_index("codigo")["nombre"]
    sin_nombre = df["transportista"].fillna("") == ""
    df.loc[sin_nombre, "transportista"] = [nombres.get(c) or (f"Transportista {c}" if c else "Sin transportista")
                                          for c in df.loc[sin_nombre, "transportista_codigo"]]
    df["fecha"] = pd.to_datetime(df["fecha"]).dt.date
    df["mes"] = [f.replace(day=1) for f in df["fecha"]]
    df["toneladas"] = df["toneladas"].astype(float)
    df["toneladas_secas"] = pd.to_numeric(df["toneladas_secas"], errors="coerce").where(lambda s: s > 0)
    llegada = pd.to_datetime(df["f_lleno"].astype(str) + " " + df["h_lleno"].astype(str), errors="coerce")
    salida = pd.to_datetime(df["f_vacio"].astype(str) + " " + df["h_vacio"].astype(str), errors="coerce")
    minutos = (salida - llegada).dt.total_seconds().abs() / 60
    df["llegada"] = llegada
    df["tara_registrada"] = minutos < MINUTOS_TARA_REGISTRADA
    df["minutos"] = minutos.where((minutos >= MINUTOS_TARA_REGISTRADA) & (minutos <= MINUTOS_MAXIMO_EN_PLANTA))
    return df.drop(columns=["f_lleno", "h_lleno", "f_vacio", "h_vacio"]).reset_index(drop=True)


def traslados_recibidos(mes, centro):
    """Ingresos por traslado (ZMM056 Tipo 5) que recibió `centro` en el mes
    `mes`, por material y planta de origen (el "proveedor" del ticket), con
    toneladas y carros."""
    fecha_inicio, fecha_fin = _limites_mes(mes)
    return _en_alcance(_query(f"""
        SELECT material_codigo AS material_codigo, MAX(descripcion_material) AS descripcion,
               proveedor AS origen, SUM(cantidad) AS toneladas, COUNT(*) AS carros
        FROM gestion_ingresofisicosop
        WHERE {FECHA_INGRESO_EFECTIVA} BETWEEN %s AND %s AND centro = %s AND tipo_operacion = '5'
        GROUP BY material_codigo, proveedor
    """, [fecha_inicio, fecha_fin, centro]), centro=centro)


def _con_categoria_efectiva_planificacion(df, plan):
    """Completa `categoria` (columna ya mergeada desde catalogo_materiales) en
    `df` para los materiales del Plan de Abastecimiento SOP que no tienen
    categoría propia en el maestro (p.ej. 14-00000037 CLINKER I ATO: es un
    intermedio de producción, no tiene grupo de artículo/categoría de compras
    asignado) aunque el Plan sí los incluya como parte del abastecimiento de
    Materias Primas. El alcance de Planificación no es solo "categoría =
    Materias Primas/Combustibles": también entra cualquier material que el
    propio Plan ya trae (siempre es de este dominio, lo define el equipo de
    S&OP), etiquetado MATERIAS PRIMAS si no tiene categoría propia. Usada por
    plan_vs_real_abastecimiento_por_material y evaluacion_diaria_abastecimiento
    — antes vivía duplicada en las dos y una de las copias quedó desactualizada,
    dejando sin Ingresos Físicos a estos materiales en la evaluación diaria/
    curva S aunque sí aparecían en la tabla mensual. Definido por el usuario."""
    materiales_en_plan = set(plan["material_codigo"].unique())
    sin_categoria_pero_en_plan = df["categoria"].isna() & df["material_codigo"].isin(materiales_en_plan)
    df.loc[sin_categoria_pero_en_plan, "categoria"] = "MATERIAS PRIMAS"
    return df


@_cacheado
def plan_vs_real_abastecimiento_por_material(mes, categoria=None, centro=None, material=None):
    """Detalle Plan vs Real de abastecimiento por material (toneladas), solo
    Materias Primas y Energía/Combustibles. `categoria`/`centro`/`material`:
    segmentadores propios de esta tabla."""
    plan = plan_abastecimiento_por_material(mes)
    real = ingresos_fisicos_por_material(mes)
    df = plan.merge(real, on=["material_codigo", "centro"], how="outer")
    df["cantidad_plan"] = df["cantidad_plan"].fillna(0.0).astype(float)
    df["cantidad_real"] = df["cantidad_real"].fillna(0.0).astype(float)

    # Solo texto_breve/categoria/familia del catálogo: "centro" ya viene del
    # propio plan/ingreso (destino real), no del centro por defecto del maestro
    # de materiales, así que no se trae esa columna para evitar pisarla.
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "categoria", "familia"]]
    df = df.merge(catalogo, on="material_codigo", how="left")
    df["texto_breve"] = df["texto_breve"].fillna("")
    df["familia"] = df["familia"].fillna("")

    df = _con_categoria_efectiva_planificacion(df, plan)
    df = df[df["categoria"].isin(CATEGORIAS_PLANIFICACION_SOP)]
    if categoria:
        df = df[df["categoria"] == categoria]
    if centro:
        df = df[df["centro"] == centro]
    if material:
        df = df[df["material_codigo"].isin(_lista_materiales(material))]

    df["diferencia"] = df["cantidad_real"] - df["cantidad_plan"]
    df["cumplimiento_pct"] = (df["cantidad_real"] / df["cantidad_plan"] * 100).where(df["cantidad_plan"] != 0)

    columnas = [
        "material_codigo", "texto_breve", "centro", "categoria", "familia",
        "cantidad_plan", "cantidad_real", "diferencia", "cumplimiento_pct",
    ]
    return df[columnas].sort_values("cantidad_plan", ascending=False).reset_index(drop=True)


def evaluacion_diaria_abastecimiento(mes, categoria=None, centro=None, material=None):
    """Evaluación diaria de abastecimiento: el Plan mensual (ya filtrado por
    categoría/centro/material, misma versión más reciente que la evaluación
    mensual) repartido en partes iguales entre los días calendario de `mes`
    (estimado, no un plan diario real) vs los Ingresos Físicos reales de cada
    día, con ambos acumulados, más la cantidad de carros (tickets de báscula)
    recibidos ese día — insumo de la tabla diaria y de la curva S de
    cumplimiento. `carros` usa las mismas reglas de Recepción que `cantidad`
    (Tipo de operación 1/5, Estado Ticket != 'ANU', Centro = Centro Salida):
    cada fila de IngresoFisicoSOP ya es un ticket/camión único (constraint
    (centro, ticket_registro)), así que "carros" es simplemente cuántas filas
    entran ese día bajo el mismo filtro que ya usa `cantidad`. Definido por
    el usuario."""
    plan_total = float(
        plan_vs_real_abastecimiento_por_material(
            mes, categoria=categoria, centro=centro, material=material
        )["cantidad_plan"].sum()
    )

    fecha_inicio, fecha_fin = _limites_mes(mes)
    dias_mes = (fecha_fin - fecha_inicio).days + 1
    plan_diario = plan_total / dias_mes if dias_mes else 0.0

    real = _query(f"""
        SELECT {FECHA_INGRESO_EFECTIVA} AS fecha, material_codigo AS material_codigo, centro AS centro,
               SUM(cantidad) AS cantidad_real, COUNT(*) AS carros
        FROM gestion_ingresofisicosop
        WHERE {FECHA_INGRESO_EFECTIVA} BETWEEN %s AND %s
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION})
        GROUP BY {FECHA_INGRESO_EFECTIVA}, material_codigo, centro
    """, [fecha_inicio, fecha_fin])

    catalogo = catalogo_materiales()[["material_codigo", "categoria"]]
    real = _en_alcance(real).merge(catalogo, on="material_codigo", how="left")
    real = _con_categoria_efectiva_planificacion(real, plan_abastecimiento_por_material(mes))
    real = real[real["categoria"].isin(CATEGORIAS_PLANIFICACION_SOP)]
    if categoria:
        real = real[real["categoria"] == categoria]
    if centro:
        real = real[real["centro"] == centro]
    if material:
        real = real[real["material_codigo"].isin(_lista_materiales(material))]

    real_por_dia = (
        real.groupby("fecha", as_index=False).agg(cantidad_real=("cantidad_real", "sum"), carros=("carros", "sum"))
        if not real.empty else real
    )
    mapa_real = {}
    mapa_carros = {}
    if not real_por_dia.empty:
        fechas_idx = pd.to_datetime(real_por_dia["fecha"]).dt.date
        mapa_real = dict(zip(fechas_idx, real_por_dia["cantidad_real"]))
        mapa_carros = dict(zip(fechas_idx, real_por_dia["carros"]))

    filas = []
    acumulado_plan = acumulado_real = 0.0
    fecha = fecha_inicio
    dia_n = 1
    while fecha <= fecha_fin:
        real_dia = float(mapa_real.get(fecha, 0.0))
        carros_dia = int(mapa_carros.get(fecha, 0))
        acumulado_plan += plan_diario
        acumulado_real += real_dia
        filas.append({
            "dia": dia_n, "fecha": fecha,
            "plan_diario": plan_diario, "real_diario": real_dia, "carros": carros_dia,
            "acumulado_plan": acumulado_plan, "acumulado_real": acumulado_real,
            "cumplimiento_pct": round(acumulado_real / acumulado_plan * 100, 1) if acumulado_plan else None,
        })
        fecha += datetime.timedelta(days=1)
        dia_n += 1

    return filas


def resumen_abastecimiento_por_categoria(mes, categoria=None, centro=None):
    """Resumen Plan vs Real por categoría (Materias Primas / Energía y
    Combustibles), para la fila-resumen de Planificación. `categoria`/`centro`:
    mismos segmentadores que plan_vs_real_abastecimiento_por_material."""
    detalle = plan_vs_real_abastecimiento_por_material(mes, categoria=categoria, centro=centro)
    if detalle.empty:
        return []
    resumen = detalle.groupby("categoria", as_index=False).agg(
        cantidad_plan=("cantidad_plan", "sum"),
        cantidad_real=("cantidad_real", "sum"),
    )
    filas = []
    for _, fila in resumen.iterrows():
        plan_total = float(fila["cantidad_plan"])
        real_total = float(fila["cantidad_real"])
        filas.append({
            "categoria": fila["categoria"],
            "cantidad_plan": plan_total,
            "cantidad_real": real_total,
            "diferencia": real_total - plan_total,
            "cumplimiento_pct": round(real_total / plan_total * 100, 1) if plan_total else None,
        })
    return filas


def cumplimiento_mensual_abastecimiento(categoria=None, centro=None):
    """Serie mensual de Plan vs Real de Abastecimiento (todos los meses con
    snapshot cargado), para ver la tendencia de cumplimiento mes a mes en vez
    de un solo mes a la vez. Un mes sin Plan (todavía no llegó ese snapshot) o
    sin Real (mes futuro, todavía no pasa) simplemente no tiene cumplimiento
    definido — se va completando solo a medida que se cargan más archivos de
    Plan de Abastecimiento mes a mes. Pedido por el usuario."""
    hoy_mes = datetime.date.today().replace(day=1)
    filas = []
    for mes in meses_planificacion_disponibles():
        detalle = plan_vs_real_abastecimiento_por_material(mes, categoria=categoria, centro=centro)
        plan_total = float(detalle["cantidad_plan"].sum()) if not detalle.empty else 0.0
        real_total = float(detalle["cantidad_real"].sum()) if not detalle.empty else 0.0
        # Un mes futuro (todavía no llega ni empieza) no tiene 0% de
        # cumplimiento, tiene "todavía no aplica" — 0% sugeriría que ya falló.
        # Un mes sin Plan (pasado, sin snapshot retroactivo) tampoco tiene con
        # qué comparar: Real - 0 no es una "diferencia positiva" real.
        sin_comparacion = mes > hoy_mes or not plan_total
        filas.append({
            "mes": mes,
            "cantidad_plan": plan_total,
            "cantidad_real": real_total,
            "diferencia": None if sin_comparacion else real_total - plan_total,
            "cumplimiento_pct": None if sin_comparacion else round(real_total / plan_total * 100, 1),
        })
    # La serie arranca en el primer mes con Plan: la ZMM056 trae ingresos desde
    # ene-2025 (cargada el 2026-10-20) y el Plan recién desde set-2026; sin
    # esto el gráfico abría con 20 meses vacíos.
    primero = next((i for i, f in enumerate(filas) if f["cantidad_plan"]), len(filas))
    return filas[primero:]
