"""Planificación S&OP > Cumplimiento: plan de abastecimiento vs ingresos físicos (ZMM056), entregas por proveedor, viajes de recepción y traslados."""

import datetime

import pandas as pd

from .base import _cacheado, _en_alcance, _limites_mes, _lista_materiales, _plan_extra_alcance, _query, _sql_proveedores_alcance
from .importados import reparto_por_embarque
from .inventario import catalogo_materiales
from .sop_base import CATEGORIAS_PLANIFICACION_SOP, FECHA_INGRESO_EFECTIVA, _FILTRO_CON_CODIGO_SOP, _IN_TIPOS_OPERACION_RECEPCION, materiales_planificados_sop


# Importados que llegan por barco (pedido del usuario, 2026-10-23): en el
# cumplimiento su real no es la balanza (en Atocongo el barco entra por U004
# y se traslada sin pesarse en la báscula de recepción) sino lo que MB51 deja
# PUESTO EN DESTINO en cada planta, en toneladas secas: el reparto por
# embarque (importados.reparto_por_embarque: traslados desde U004, reenvío
# de Atocongo a Condorcocha y ajustes de humedad Z15/Z16 de cada barco).
MATERIALES_REAL_MB51 = ("06-00000003", "07-00001301")  # Carbón Importado, Escoria japonesa (Altos Hornos)

# Mes en que cuenta para el cumplimiento lo recibido de un importado en un
# mes (material, centro, mes real) -> mes del S&OP:
# - El barco de Carbón Importado de marzo 2026 llegó a Atocongo entre fines de
#   marzo y abril; el S&OP de abril lo planificó completo en abril y el OTIF
#   del usuario lo cuenta en abril (2026-10-26).
# - El traslado de Carbón Importado de Atocongo a Condorcocha del 30-set-2026
#   (641 de ese día; casi todo llegó el 1-oct) es del abastecimiento de
#   OCTUBRE: acuerdo del 2026-10-06 (INGRESOS_CON_OTRA_FECHA, en la báscula)
#   que el usuario pidió mantener cuando el real pasó a MB51 (2026-10-26).
#   Setiembre queda en 0 para Condorcocha.
MES_CUMPLIMIENTO_IMPORTADOS = {
    ("06-00000003", "U001", "2026-03-01"): "2026-04-01",
    ("06-00000003", "U002", "2026-09-01"): "2026-10-01",
}


def sops_abastecimiento_disponibles():
    """Emisiones del S&OP de abastecimiento cargadas (mes de emisión), de la
    más nueva a la más antigua."""
    df = _query("SELECT DISTINCT mes_snapshot AS mes FROM gestion_planabastecimientosop ORDER BY mes_snapshot DESC")
    return [m if isinstance(m, datetime.date) else datetime.date.fromisoformat(str(m)[:10]) for m in df["mes"]]


def _real_mb51(mes):
    """Toneladas puestas en destino (MB51, secas) de MATERIALES_REAL_MB51 en
    `mes`, por material y centro: material_codigo, centro, cantidad_real,
    embarques."""
    r = reparto_por_embarque()
    clave = mes.isoformat() if hasattr(mes, "isoformat") else str(mes)[:10]
    r = r[r["material_codigo"].isin(MATERIALES_REAL_MB51)]
    mes_cumplimiento = [MES_CUMPLIMIENTO_IMPORTADOS.get((m, c, x), x)
                        for m, c, x in zip(r["material_codigo"], r["centro"], r["mes"].astype(str).str[:10])]
    r = r[[x == clave for x in mes_cumplimiento]]
    # Lo que llegó a cada planta en el mes (pedido del usuario, 2026-10-26: el
    # barco de Sep-26 se recibió completo en Atocongo, aunque luego se reenvíe):
    # no se descuenta lo que Atocongo reenvía después a Condorcocha.
    r = r.groupby(["material_codigo", "centro"], as_index=False).agg(
        cantidad_real=("recibido", "sum"), embarques=("embarques", lambda s: ", ".join(sorted({x for x in s if x}))))
    return r[r["cantidad_real"].abs() >= 0.5].reset_index(drop=True)


def _sin_mb51(sql_material="material_codigo"):
    """Condición SQL (con su AND) que deja fuera de la balanza los materiales
    cuyo real sale de MB51."""
    return f" AND {sql_material} NOT IN ({','.join(repr(m) for m in MATERIALES_REAL_MB51)})"


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


def meses_con_sop_propio():
    """Meses que tienen su propio S&OP de abastecimiento (la emisión de ese
    mes trae ese mes): los únicos que se evalúan por defecto."""
    df = _query("SELECT DISTINCT mes_snapshot AS mes FROM gestion_planabastecimientosop "
                "WHERE mes_planificado = mes_snapshot ORDER BY mes_snapshot")
    return [m if isinstance(m, datetime.date) else datetime.date.fromisoformat(str(m)[:10]) for m in df["mes"]]


def plan_abastecimiento_por_material(mes, sop=None):
    """Plan de abastecimiento (S&OP) por material/centro para `mes`. `sop`:
    la emisión del S&OP (mes de emisión) elegida en el segmentador S&OP; sin
    ella, la del MISMO mes (regla del usuario, 2026-10-26: cada mes se evalúa
    solo contra su propio S&OP — setiembre contra el S&OP de setiembre —; un
    mes sin su S&OP no tiene plan). Solo las celdas con dato: una celda vacía
    es un material que ese S&OP no trae para ese mes."""
    df = _en_alcance(_query(f"""
        SELECT material_codigo AS material_codigo, centro_destino AS centro,
               SUM(cantidad_plan) AS cantidad_plan
        FROM gestion_planabastecimientosop
        WHERE mes_planificado = %s AND mes_snapshot = %s AND cantidad_plan IS NOT NULL
              AND {_FILTRO_CON_CODIGO_SOP}
        GROUP BY material_codigo, centro_destino
    """, [mes, sop or mes]), mes=mes)
    # Plan provisional del alcance (Logística: Puzolana Ayacucho en
    # Condorcocha, set-26), sumado al del S&OP.
    extra = _plan_extra_alcance(mes)
    if extra:
        extra = pd.DataFrame(extra, columns=["material_codigo", "centro", "cantidad_plan"])
        df = (pd.concat([df, extra], ignore_index=True)
              .groupby(["material_codigo", "centro"], as_index=False)["cantidad_plan"].sum())
    return df


def ingresos_fisicos_por_material(mes):
    """Ingresos físicos reales de Recepción (báscula, ZMM056 — solo Tipo de
    operación 1/5, ver _TIPOS_OPERACION_RECEPCION) por material/centro en el
    mes calendario `mes` (por FECHA_INGRESO_EFECTIVA). Definido por el
    usuario: no se toma de MB51 para estos materiales."""
    fecha_inicio, fecha_fin = _limites_mes(mes)
    balanza = _query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro,
               SUM(cantidad) AS cantidad_real
        FROM gestion_ingresofisicosop
        WHERE {FECHA_INGRESO_EFECTIVA} BETWEEN %s AND %s
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION}){_sql_proveedores_alcance()}{_sin_mb51()}
        GROUP BY material_codigo, centro
    """, [fecha_inicio, fecha_fin])
    mb51 = _real_mb51(mes)[["material_codigo", "centro", "cantidad_real"]]
    return _en_alcance(pd.concat([balanza, mb51], ignore_index=True), mes=mes)


def ingresos_fisicos_por_proveedor(mes):
    """Ingresos físicos de Recepción (mismas reglas que
    ingresos_fisicos_por_material) por material/centro/proveedor en el mes
    `mes`, con la cantidad de carros (tickets). En los ingresos propios (Tipo
    5: cantera o traslado entre plantas) el "proveedor" de ZMM056 es la planta
    de origen (ATOCONGO, PUCARA...). Usado por el Detalle por material de
    Cumplimiento. Pedido por el usuario (después llegará el plan por proveedor)."""
    fecha_inicio, fecha_fin = _limites_mes(mes)
    df = _query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro, proveedor_codigo AS proveedor_codigo,
               proveedor AS proveedor, MAX(tipo_operacion) AS tipo_operacion,
               SUM(cantidad) AS cantidad_real, COUNT(*) AS carros
        FROM gestion_ingresofisicosop
        WHERE {FECHA_INGRESO_EFECTIVA} BETWEEN %s AND %s
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION}){_sql_proveedores_alcance()}
        GROUP BY material_codigo, centro, proveedor_codigo, proveedor
    """, [fecha_inicio, fecha_fin])
    # Importados por barco: una sola fila con lo puesto en destino según MB51
    # (sus embarques); los carros siguen siendo los camiones de la balanza.
    mb51 = _real_mb51(mes)
    if not df.empty or not mb51.empty:
        carros = df[df["material_codigo"].isin(MATERIALES_REAL_MB51)].groupby(["material_codigo", "centro"])["carros"].sum()
        filas = [{"material_codigo": r.material_codigo, "centro": r.centro, "proveedor_codigo": "",
                  "proveedor": f"MB51 puesto en destino · embarque {r.embarques}" if r.embarques else "MB51 puesto en destino",
                  "tipo_operacion": "MB51", "cantidad_real": r.cantidad_real,
                  "carros": int(carros.get((r.material_codigo, r.centro), 0))} for r in mb51.itertuples()]
        df = pd.concat([df[~df["material_codigo"].isin(MATERIALES_REAL_MB51)], pd.DataFrame(filas, columns=df.columns)],
                       ignore_index=True)
    return _en_alcance(df, mes=mes)


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
        WHERE fecha_ingreso IS NOT NULL AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION}){_sql_proveedores_alcance()}
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
        WHERE tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION}) AND fecha_ingreso IS NOT NULL{_sql_proveedores_alcance()}
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
    """, [fecha_inicio, fecha_fin, centro]), centro=centro, mes=mes)


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
def plan_vs_real_abastecimiento_por_material(mes, categoria=None, centro=None, material=None, sop=None):
    """Detalle Plan vs Real de abastecimiento por material (toneladas), solo
    Materias Primas y Energía/Combustibles. `categoria`/`centro`/`material`:
    segmentadores propios de esta tabla; `sop`: emisión del S&OP (ver
    plan_abastecimiento_por_material). Solo los materiales que trae ese S&OP
    (regla del usuario, 2026-10-26, para cuadrar con su OTIF): lo recibido
    de un material que el S&OP del mes no planificó no entra al cumplimiento."""
    plan = plan_abastecimiento_por_material(mes, sop)
    real = ingresos_fisicos_por_material(mes)
    df = plan.merge(real, on=["material_codigo", "centro"], how="left")
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


def _traslados_recibidos_mb51(fecha_inicio, fecha_fin):
    """Toneladas que cada planta recibió por traslado (641, neto de 642) de
    MATERIALES_REAL_MB51 cada día entre las fechas: fecha, material_codigo,
    centro, toneladas."""
    df = _query(f"""
        SELECT fecha_contabilizacion AS fecha, material_codigo AS material_codigo, centro AS centro,
               SUM(cantidad) AS toneladas
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({','.join(repr(m) for m in MATERIALES_REAL_MB51)})
              AND fecha_contabilizacion BETWEEN %s AND %s
              AND ((clase_movimiento = '641' AND cantidad > 0) OR (clase_movimiento = '642' AND cantidad < 0))
        GROUP BY fecha_contabilizacion, material_codigo, centro
    """, [fecha_inicio, fecha_fin])
    df["fecha"] = pd.to_datetime(df["fecha"]).dt.date
    df["toneladas"] = df["toneladas"].astype(float)
    return df


def ingresos_diarios_abastecimiento(mes):
    """Ingresos de cada día de `mes` por material/centro: fecha,
    material_codigo, centro, cantidad_real, carros. Balanza (ZMM056,
    Recepción), salvo MATERIALES_REAL_MB51: su total del mes es lo puesto en
    destino de MB51 (secas), repartido entre los días en que la planta recibió
    el traslado (641, neto de 642) según sus toneladas; sin traslados en el
    mes, va al último día. Lo recibido en otro mes que cuenta en este
    (MES_CUMPLIMIENTO_IMPORTADOS) va al primer día. Los carros siguen siendo
    los de la balanza."""
    fecha_inicio, fecha_fin = _limites_mes(mes)
    balanza = _query(f"""
        SELECT {FECHA_INGRESO_EFECTIVA} AS fecha, material_codigo AS material_codigo, centro AS centro,
               SUM(cantidad) AS cantidad_real, COUNT(*) AS carros
        FROM gestion_ingresofisicosop
        WHERE {FECHA_INGRESO_EFECTIVA} BETWEEN %s AND %s
              AND tipo_operacion IN ({_IN_TIPOS_OPERACION_RECEPCION}){_sql_proveedores_alcance()}
        GROUP BY {FECHA_INGRESO_EFECTIVA}, material_codigo, centro
    """, [fecha_inicio, fecha_fin])
    if not balanza.empty:
        balanza["fecha"] = pd.to_datetime(balanza["fecha"]).dt.date
    mb51 = _real_mb51(mes)
    if mb51.empty:
        return balanza[~balanza["material_codigo"].isin(MATERIALES_REAL_MB51)].reset_index(drop=True)
    traslados = _traslados_recibidos_mb51(fecha_inicio, fecha_fin)
    for (material, centro, origen), destino in MES_CUMPLIMIENTO_IMPORTADOS.items():
        if destino == fecha_inicio.isoformat():
            otro = _traslados_recibidos_mb51(*_limites_mes(datetime.date.fromisoformat(origen)))
            otro = otro[(otro["material_codigo"] == material) & (otro["centro"] == centro)].assign(fecha=fecha_inicio)
            traslados = pd.concat([traslados, otro], ignore_index=True)
    filas = []
    for r in mb51.itertuples():
        dias = traslados[(traslados["material_codigo"] == r.material_codigo) & (traslados["centro"] == r.centro)] \
            if not traslados.empty else traslados
        total = float(dias["toneladas"].sum()) if not dias.empty else 0.0
        if total > 0:
            filas += [(d.fecha, r.material_codigo, r.centro, r.cantidad_real * d.toneladas / total) for d in dias.itertuples()]
        else:
            filas.append((fecha_fin, r.material_codigo, r.centro, r.cantidad_real))
    importados = pd.DataFrame(filas, columns=["fecha", "material_codigo", "centro", "cantidad_real"])
    carros = balanza[balanza["material_codigo"].isin(MATERIALES_REAL_MB51)][["fecha", "material_codigo", "centro", "carros"]]
    importados = importados.groupby(["fecha", "material_codigo", "centro"], as_index=False)["cantidad_real"].sum()
    importados = importados.merge(carros, on=["fecha", "material_codigo", "centro"], how="outer")
    importados["cantidad_real"] = importados["cantidad_real"].fillna(0.0)
    importados["carros"] = importados["carros"].fillna(0).astype(int)
    return pd.concat([balanza[~balanza["material_codigo"].isin(MATERIALES_REAL_MB51)], importados],
                     ignore_index=True)


def evaluacion_diaria_abastecimiento(mes, categoria=None, centro=None, material=None, sop=None):
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
            mes, categoria=categoria, centro=centro, material=material, sop=sop
        )["cantidad_plan"].sum()
    )

    fecha_inicio, fecha_fin = _limites_mes(mes)
    dias_mes = (fecha_fin - fecha_inicio).days + 1
    plan_diario = plan_total / dias_mes if dias_mes else 0.0

    real = ingresos_diarios_abastecimiento(mes)

    catalogo = catalogo_materiales()[["material_codigo", "categoria"]]
    plan = plan_abastecimiento_por_material(mes, sop)
    real = _en_alcance(real, mes=mes).merge(catalogo, on="material_codigo", how="left")
    real = _con_categoria_efectiva_planificacion(real, plan)
    real = real[real["categoria"].isin(CATEGORIAS_PLANIFICACION_SOP)]
    # Como la evaluación mensual: solo los materiales que trae el S&OP del mes.
    pares = set(zip(plan["material_codigo"], plan["centro"]))
    real = real[[k in pares for k in zip(real["material_codigo"], real["centro"])]]
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


def resumen_abastecimiento_por_categoria(mes, categoria=None, centro=None, sop=None):
    """Resumen Plan vs Real por categoría (Materias Primas / Energía y
    Combustibles), para la fila-resumen de Planificación. `categoria`/`centro`/
    `sop`: mismos segmentadores que plan_vs_real_abastecimiento_por_material."""
    detalle = plan_vs_real_abastecimiento_por_material(mes, categoria=categoria, centro=centro, sop=sop)
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


def cumplimiento_mensual_abastecimiento(categoria=None, centro=None, sop=None):
    """Serie mensual de Plan vs Real de Abastecimiento (todos los meses con
    snapshot cargado), para ver la tendencia de cumplimiento mes a mes en vez
    de un solo mes a la vez. Un mes sin Plan (todavía no llegó ese snapshot) o
    sin Real (mes futuro, todavía no pasa) simplemente no tiene cumplimiento
    definido — se va completando solo a medida que se cargan más archivos de
    Plan de Abastecimiento mes a mes. Pedido por el usuario."""
    hoy_mes = datetime.date.today().replace(day=1)
    filas = []
    # Sin segmentador S&OP, solo los meses con su propio S&OP (cada mes contra
    # el S&OP de ese mes, regla del usuario 2026-10-26).
    meses = meses_planificacion_disponibles() if sop else meses_con_sop_propio()
    for mes in meses:
        detalle = plan_vs_real_abastecimiento_por_material(mes, categoria=categoria, centro=centro, sop=sop)
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
