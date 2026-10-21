"""Planificación S&OP > Consumo: plan de consumos vs real y consumo por tonelada producida."""

import datetime

import pandas as pd

from .base import _cacheado, _query, _sumar_meses
from .inventario import catalogo_materiales
from .sop_base import _FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP, _FILTRO_CON_CODIGO_SOP, _MATERIALES_CONSUMO_VIA_OTRO, materiales_planificados_sop


# ---------------------------------------------------------------------------
# Evaluación de consumo (S&OP). Pedido por el usuario: el Plan de Consumos del
# S&OP (archivo mensual) contra el consumo real de MB51 (mismas clases que
# Cobertura: 2xx + Z01/Z04/353), por material/centro y mes. Carbón Terceros
# en Condorcocha se consume vía su chancado (14-00001112): real y plan se
# toman del chancado y se muestran como 06-00000005 (mismo criterio que la
# Evolución de inventario, donde el Excel repite el mismo número en las dos
# filas).
# ---------------------------------------------------------------------------

def _como_material_comprado(df):
    """Renombra las filas del material por el que realmente se consume
    (_MATERIALES_CONSUMO_VIA_OTRO) al material que se compra/planifica,
    descartando la fila propia de este último si trae las dos."""
    for (origen, centro_origen), (real, centro_real) in _MATERIALES_CONSUMO_VIA_OTRO.items():
        es_real = (df["material_codigo"] == real) & (df["centro"] == centro_real)
        if es_real.any():
            df = df[~((df["material_codigo"] == origen) & (df["centro"] == centro_origen))].copy()
            df.loc[(df["material_codigo"] == real) & (df["centro"] == centro_real), ["material_codigo", "centro"]] = [origen, centro_origen]
    return df


@_cacheado
def plan_consumo_sop():
    """Plan de Consumos S&OP de todos los archivos cargados: una fila por
    mes_snapshot (emisión), material/centro y mes planificado, en toneladas
    (positivas), con la descripción del Excel."""
    df = _query(f"""
        SELECT mes_snapshot AS snapshot, material_codigo AS material_codigo, centro_destino AS centro,
               mes_planificado AS mes, SUM(cantidad_consumo) AS plan, MAX(material_descripcion) AS descripcion
        FROM gestion_planconsumosop
        WHERE {_FILTRO_CON_CODIGO_SOP}
        GROUP BY mes_snapshot, material_codigo, centro_destino, mes_planificado
    """)
    if df.empty:
        return df
    df["snapshot"] = pd.to_datetime(df["snapshot"]).dt.date
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    df["plan"] = df["plan"].fillna(0.0).astype(float)
    partes = [_como_material_comprado(g) for _, g in df.groupby("snapshot")]
    df = pd.concat(partes, ignore_index=True)
    return df[df["plan"] > 0].reset_index(drop=True)


@_cacheado
def consumo_real_mensual_sop():
    """Consumo real (MB51) por material/centro y mes de los materiales del
    S&OP (planes de abastecimiento y de consumos), en toneladas positivas,
    con descripción y categoría (sin categoría en el maestro -> MATERIAS
    PRIMAS, como en Plan vs Real)."""
    columnas = ["material_codigo", "texto_breve", "centro", "categoria", "mes", "consumo"]
    planificados = set(materiales_planificados_sop())
    plan = plan_consumo_sop()
    if not plan.empty:
        planificados |= set(plan["material_codigo"])
    planificados |= {real for real, _centro in _MATERIALES_CONSUMO_VIA_OTRO.values()}
    if not planificados:
        return pd.DataFrame(columns=columnas)
    df = _query(f"""
        SELECT material_codigo AS material_codigo, centro AS centro,
               substr(fecha_contabilizacion, 1, 7) || '-01' AS mes, -SUM(cantidad) AS consumo
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({",".join(f"'{m}'" for m in sorted(planificados))})
              AND +centro IN ('U001', 'U002') AND {_FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP}
              AND fecha_contabilizacion IS NOT NULL
        GROUP BY material_codigo, centro, mes
    """)
    if df.empty:
        return pd.DataFrame(columns=columnas)
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    df["consumo"] = df["consumo"].astype(float)
    df = _como_material_comprado(df)
    df = df.groupby(["material_codigo", "centro", "mes"], as_index=False)["consumo"].sum()
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "categoria"]].drop_duplicates("material_codigo")
    df = df.merge(catalogo, on="material_codigo", how="left")
    df.loc[df["categoria"].isna(), "categoria"] = "MATERIAS PRIMAS"
    df["texto_breve"] = df["texto_breve"].fillna("")
    return df[columnas].sort_values(["material_codigo", "centro", "mes"]).reset_index(drop=True)


def plan_consumo_del_mes(mes):
    """Plan de consumo vigente para `mes`: el del S&OP más reciente emitido
    hasta ese mes que lo incluya (idealmente el del propio mes). Devuelve
    (DataFrame material_codigo/centro/plan/descripcion, snapshot usado)."""
    plan = plan_consumo_sop()
    if plan.empty:
        return plan, None
    candidatos = plan[(plan["mes"] == mes) & (plan["snapshot"] <= mes)]
    if candidatos.empty:
        return candidatos, None
    snapshot = max(candidatos["snapshot"])
    return candidatos[candidatos["snapshot"] == snapshot][["material_codigo", "centro", "plan", "descripcion"]], snapshot


# ---------------------------------------------------------------------------
# Consumo por tonelada producida (S&OP). Pedido por el usuario (2026-09-30):
# saber si un consumo mayor viene de producir más o de gastar más por
# tonelada. MB51 trae la producción (101/102 de órdenes de producción) y lo que
# consume cada orden (261/262), así que se sigue la cadena de cada material
# hasta el producto final de su planta: CLINKER (calizas, arcillas,
# correctivos y carbones, vía crudo o combustible sólido) o CEMENTO (yeso,
# puzolana, escoria y el propio clinker). Factor = consumo real del mes (el
# mismo de la Evaluación de consumo) / producción del mes de ese producto en
# el mismo centro.
# ---------------------------------------------------------------------------

_PRODUCTOS_FINALES = {"CLINKER": "clinker", "CEMENTO": "cemento"}
_MESES_CADENA_PRODUCCION = 12


def _producto_final(descripcion):
    texto = (descripcion or "").strip().upper()
    for prefijo, producto in _PRODUCTOS_FINALES.items():
        if texto.startswith(prefijo):
            return producto
    return None


@_cacheado
def produccion_mensual_planta():
    """Producción neta mensual (101 menos 102 de órdenes de producción) de
    clinker y cemento por centro, en toneladas: columnas centro, mes,
    producto ('clinker'/'cemento'), produccion."""
    columnas = ["centro", "mes", "producto", "produccion"]
    df = _query("""
        SELECT centro AS centro, substr(fecha_contabilizacion, 1, 7) || '-01' AS mes,
               descripcion_material AS descripcion, SUM(cantidad) AS produccion
        FROM gestion_movimientomaterial
        WHERE clase_movimiento IN ('101', '102') AND orden <> '' AND substr(material_codigo, 1, 3) = '14-'
              AND substr(upper(descripcion_material), 1, 7) IN ('CLINKER', 'CEMENTO')
              AND fecha_contabilizacion IS NOT NULL
        GROUP BY centro, mes, descripcion_material
    """)
    if df.empty:
        return pd.DataFrame(columns=columnas)
    df["producto"] = df["descripcion"].map(_producto_final)
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    df["produccion"] = df["produccion"].astype(float)
    df = df.groupby(["centro", "mes", "producto"], as_index=False)["produccion"].sum()
    # Mes con MB51 incompleto (p.ej. Feb-2026, 8.4K filas de ~80K): producción
    # menor al 30% de la mediana de ese centro/producto -> se descarta, si no
    # el factor de ese mes se dispara.
    mediana = df.groupby(["centro", "producto"])["produccion"].transform("median")
    return df[(df["produccion"] > 0) & (df["produccion"] >= 0.3 * mediana)][columnas].reset_index(drop=True)


@_cacheado
def destino_produccion_sop():
    """A qué producto final va el consumo de cada material/centro, siguiendo
    las órdenes de producción de los últimos 12 meses (cadena material ->
    producto de la orden -> ... -> clinker o cemento). Devuelve un dict
    (material, centro) -> {"clinker": fracción, "cemento": fracción}; las
    fracciones pueden sumar menos de 1 si parte del consumo va a algo que no
    termina en ninguno de los dos (p.ej. harina cruda recuperada)."""
    ultima = _query("SELECT MAX(fecha_contabilizacion) AS f FROM gestion_movimientomaterial")["f"].iloc[0]
    if ultima is None:
        return {}
    desde = _sumar_meses(datetime.date.fromisoformat(str(ultima)[:10]).replace(day=1), -_MESES_CADENA_PRODUCCION)
    salidas = _query("""
        SELECT orden AS orden, material_codigo AS material, centro AS centro,
               MAX(descripcion_material) AS descripcion, SUM(cantidad) AS cantidad
        FROM gestion_movimientomaterial
        WHERE clase_movimiento IN ('101', '102') AND orden <> '' AND fecha_contabilizacion >= %s
        GROUP BY orden, material_codigo, centro
    """, [desde.isoformat()])
    entradas = _query("""
        SELECT orden AS orden, material_codigo AS material, centro AS centro, -SUM(cantidad) AS cantidad
        FROM gestion_movimientomaterial
        WHERE clase_movimiento IN ('261', '262') AND orden <> '' AND fecha_contabilizacion >= %s
        GROUP BY orden, material_codigo, centro
    """, [desde.isoformat()])
    if salidas.empty or entradas.empty:
        return {}
    # Una orden produce un material: el de mayor cantidad neta.
    salidas = salidas.sort_values("cantidad").drop_duplicates("orden", keep="last")
    finales = {(m, c): _producto_final(d) for m, c, d in zip(salidas["material"], salidas["centro"], salidas["descripcion"])}
    aristas = entradas.merge(
        salidas[["orden", "material", "centro"]].rename(columns={"material": "salida", "centro": "centro_salida"}), on="orden"
    )
    aristas = aristas[(aristas["cantidad"] > 0) & (aristas["material"] != aristas["salida"])]
    aristas = aristas.groupby(["material", "centro", "salida", "centro_salida"], as_index=False)["cantidad"].sum()
    hacia = {}
    for r in aristas.itertuples():
        hacia.setdefault((r.material, r.centro), []).append(((r.salida, r.centro_salida), float(r.cantidad)))

    memo = {}

    def destino(nodo, visitados):
        if nodo in memo:
            return memo[nodo]
        total = sum(t for _, t in hacia.get(nodo, []))
        resultado = {"clinker": 0.0, "cemento": 0.0}
        for salida, t in hacia.get(nodo, []):
            if salida in visitados:
                continue
            final = finales.get(salida)
            parte = {final: 1.0} if final else destino(salida, visitados | {salida})
            for producto, fraccion in parte.items():
                resultado[producto] += t / total * fraccion
        memo[nodo] = resultado
        return resultado

    resultado = {nodo: destino(nodo, {nodo}) for nodo in hacia}
    # Mismo empalme que el consumo: Carbón Terceros U002 va vía su chancado.
    for origen, real in _MATERIALES_CONSUMO_VIA_OTRO.items():
        if real in resultado:
            resultado[origen] = resultado[real]
    return resultado


@_cacheado
def consumo_por_tonelada_sop():
    """Consumo por tonelada producida, mes a mes, de cada material/centro del
    S&OP: columnas material_codigo, texto_breve, centro, categoria, producto
    ('clinker'/'cemento', el que recibe la mayor parte de su consumo),
    participacion (esa fracción), mes, consumo, produccion, factor (T de
    material por T de producto). Materiales cuyo consumo no llega a clinker
    ni cemento quedan fuera."""
    columnas = ["material_codigo", "texto_breve", "centro", "categoria", "producto", "participacion",
                "mes", "consumo", "produccion", "factor"]
    real = consumo_real_mensual_sop()
    produccion = produccion_mensual_planta()
    destinos = destino_produccion_sop()
    if real.empty or produccion.empty or not destinos:
        return pd.DataFrame(columns=columnas)
    principal = {}
    for clave, partes in destinos.items():
        producto = max(partes, key=partes.get)
        if partes[producto] >= 0.5:
            principal[clave] = (producto, partes[producto])
    df = real.copy()
    claves = list(zip(df["material_codigo"], df["centro"]))
    df["producto"] = [principal.get(k, (None, None))[0] for k in claves]
    df["participacion"] = [principal.get(k, (None, None))[1] for k in claves]
    df = df[df["producto"].notna()].merge(produccion, on=["centro", "mes", "producto"], how="inner")
    df = df[(df["produccion"] > 0) & (df["consumo"] > 0)].copy()
    df["factor"] = df["consumo"] / df["produccion"]
    return df[columnas].sort_values(["material_codigo", "centro", "mes"]).reset_index(drop=True)
