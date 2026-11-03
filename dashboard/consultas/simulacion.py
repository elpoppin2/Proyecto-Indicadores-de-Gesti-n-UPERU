"""Simulación S&OP (pedido del usuario, 2026-11-03): versiones de precios S&OP (una por S&OP, cargadas de 'Precios SOP - <mes> <año>.xlsx'), inventario valorizado proyectado a precio promedio móvil, comparación entre versiones y precisión del pronóstico de precios."""

import datetime

import pandas as pd

from .base import _cacheado, _query, _sumar_meses
from .precios import precios_entrada_sop_real
from .sop_inventario import _TRASLADOS_SOP, proyeccion_inventario_sop, snapshots_sop_disponibles


def versiones_precios_sop():
    """Meses de emisión con versión de precios S&OP cargada, del más antiguo al más reciente."""
    df = _query("SELECT DISTINCT mes_snapshot AS mes FROM gestion_preciosop ORDER BY mes")
    return [datetime.date.fromisoformat(str(m)[:10]) for m in df["mes"]]


@_cacheado
def precios_sop(mes_snapshot):
    """Precios de la versión `mes_snapshot`: una fila por material/centro/mes
    (precio en S/ por unidad; NaN = celda vacía) con su descripción, unidad y
    supuesto."""
    df = _query("""
        SELECT material_codigo AS material_codigo, centro AS centro, mes AS mes, precio AS precio,
               material_descripcion AS descripcion, unidad AS unidad, supuesto AS supuesto
        FROM gestion_preciosop WHERE mes_snapshot = %s
    """, [mes_snapshot])
    if df.empty:
        return df
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    df["precio"] = pd.to_numeric(df["precio"], errors="coerce")
    return df.sort_values(["centro", "material_codigo", "mes"]).reset_index(drop=True)


def valorizar_mes(stock_inicial, valor_inicial, entradas, valor_entradas, stock_final):
    """Un mes a precio promedio ponderado móvil (como el precio V de SAP), con
    todas las entradas del mes antes de sus salidas:
    PMP = (valor inicial + valor de las entradas) ÷ (stock inicial + entradas)
    y valor final = stock final × PMP (lo que sale, sale a ese PMP). Un stock
    negativo (quiebre del plan) es 0 físico. Devuelve (pmp, valor_final); pmp
    es None si no hay stock ni entradas."""
    stock_fisico = max(stock_inicial, 0.0)
    disponible = stock_fisico + entradas
    if disponible <= 0:
        return None, 0.0
    pmp = ((valor_inicial if stock_fisico > 0 else 0.0) + valor_entradas) / disponible
    return pmp, max(stock_final, 0.0) * pmp


def _valorizar(proyeccion, precio_de):
    """Inventario valorizado mes a mes de `proyeccion` (proyeccion_inventario_sop)
    con los precios `precio_de` {(material, centro, mes): S/ por unidad}:
    - las compras (abastecimiento que no es traslado) entran al precio S&OP del
      mes; sin precio, al PMP vigente (no lo mueven) y la fila queda marcada;
    - lo recibido por traslado (Carbón Importado en Condorcocha) entra al PMP
      con que salió del centro que envía ese mes más el flete, que es la
      diferencia entre el precio S&OP de los dos centros (así lo valoriza SAP:
      valor de salida + flete al recibir);
    - el stock de partida va a su valor del MB5B."""
    columnas_salida = [
        "material_codigo", "texto_breve", "centro", "categoria", "familia", "mes",
        "stock_inicial", "valor_inicial", "compras", "precio_sop", "valor_compras", "recibido", "valor_recibido",
        "salidas", "valor_salidas", "pmp", "stock_final", "valor_final", "sin_precio",
    ]
    if proyeccion.empty:
        return pd.DataFrame(columns=columnas_salida)
    estado = {}       # (material, centro) -> (stock, valor, pmp)
    pmp_salida = {}   # (material, centro, mes) -> PMP con que salió del centro ese mes
    filas = []
    for mes, grupo in proyeccion.sort_values("mes").groupby("mes", sort=True):
        # Primero los centros que envían: el que recibe necesita su PMP del mes.
        orden = sorted(grupo.itertuples(index=False), key=lambda r: float(r.traslado_recibido) > 0)
        for r in orden:
            clave = (r.material_codigo, r.centro)
            if clave not in estado:
                stock0 = float(r.stock_inicial)
                valor0 = float(r.valor_partida) if stock0 > 0 else 0.0
                estado[clave] = (stock0, valor0, valor0 / stock0 if stock0 > 0 else None)
            stock, valor, pmp_previo = estado[clave]
            recibido = float(r.traslado_recibido)
            compras = float(r.abastecimiento) - recibido
            precio = precio_de.get((r.material_codigo, r.centro, mes))
            sin_precio = compras > 0 and precio is None
            precio_compra = precio if precio is not None else (pmp_previo or 0.0)
            valor_compras = compras * precio_compra
            valor_recibido = 0.0
            if recibido:
                # Solo hay dos plantas: lo recibido sale de la otra (Carbón Importado y Trujillo, de Atocongo).
                envia = _TRASLADOS_SOP.get(clave) or {"U001": "U002", "U002": "U001"}.get(r.centro)
                precio_envia = precio_de.get((r.material_codigo, envia, mes))
                flete = max(precio - precio_envia, 0.0) if precio is not None and precio_envia is not None else 0.0
                valor_recibido = recibido * (pmp_salida.get((r.material_codigo, envia, mes), precio_compra) + flete)
            pmp, valor_final = valorizar_mes(stock, valor, compras + recibido, valor_compras + valor_recibido,
                                             float(r.stock_final))
            if pmp is None:
                pmp = pmp_previo
            pmp_salida[(r.material_codigo, r.centro, mes)] = pmp
            valor_inicial = valor if stock > 0 else 0.0
            filas.append({
                "material_codigo": r.material_codigo, "texto_breve": r.texto_breve, "centro": r.centro,
                "categoria": r.categoria, "familia": r.familia, "mes": mes,
                "stock_inicial": stock, "valor_inicial": valor_inicial,
                "compras": compras, "precio_sop": precio, "valor_compras": valor_compras,
                "recibido": recibido, "valor_recibido": valor_recibido,
                "salidas": float(r.consumo) + float(r.traslado),
                "valor_salidas": valor_inicial + valor_compras + valor_recibido - valor_final,
                "pmp": pmp, "stock_final": float(r.stock_final), "valor_final": valor_final, "sin_precio": sin_precio,
            })
            estado[clave] = (float(r.stock_final), valor_final, pmp)
    return pd.DataFrame(filas, columns=columnas_salida)


def _precios_por_clave(mes_precios):
    df = precios_sop(mes_precios)
    if df.empty:
        return {}
    df = df[df["precio"].notna() & (df["precio"] > 0)]
    return {(m, c, mes): float(p) for m, c, mes, p in zip(df["material_codigo"], df["centro"], df["mes"], df["precio"])}


@_cacheado
def inventario_valorizado_sop(mes_snapshot, mes_precios=None):
    """Inventario valorizado proyectado del S&OP `mes_snapshot`: cantidades del
    S&OP (proyeccion_inventario_sop) valorizadas con la versión de precios
    `mes_precios` (por defecto, la del mismo S&OP: cada S&OP tiene la suya,
    regla del usuario). Una fila por material/centro/mes (ver _valorizar).
    Vacío si esa versión de precios no está cargada."""
    precios = _precios_por_clave(mes_precios or mes_snapshot)
    if not precios:
        return _valorizar(pd.DataFrame(), {})
    return _valorizar(proyeccion_inventario_sop(mes_snapshot), precios)


def sop_valorizables():
    """S&OP que se pueden valorizar (con Plan de Consumos) y si tienen su versión de precios."""
    con_precios = set(versiones_precios_sop())
    return [(s, s in con_precios) for s in snapshots_sop_disponibles()]


def sop_anterior_con_precios(mes_snapshot):
    """El S&OP valorizable anterior a `mes_snapshot` que tiene versión de precios, o None."""
    anteriores = [s for s, con_precio in sop_valorizables() if con_precio and s < mes_snapshot]
    return anteriores[-1] if anteriores else None


@_cacheado
def comparativo_valorizado_sop(mes_snapshot, anterior):
    """Cambio del inventario valorizado entre el S&OP `anterior` y `mes_snapshot`
    en los meses que tienen los dos, por material/centro/mes:
    - valor_anterior: cantidades y precios del S&OP anterior;
    - valor_cruzado: cantidades del S&OP nuevo con los precios del anterior;
    - efecto_cantidad = cruzado − anterior (plan e inventario de partida);
    - efecto_precio = nuevo − cruzado (la versión de precios)."""
    claves = ["material_codigo", "centro", "mes"]
    nuevo = inventario_valorizado_sop(mes_snapshot)
    viejo = inventario_valorizado_sop(anterior)
    cruzado = _valorizar(proyeccion_inventario_sop(mes_snapshot), _precios_por_clave(anterior))
    if nuevo.empty or viejo.empty:
        return pd.DataFrame()
    meses = sorted(set(nuevo["mes"]) & set(viejo["mes"]))
    base = nuevo[nuevo["mes"].isin(meses)][claves + ["texto_breve", "categoria", "familia", "valor_final"]]
    df = base.merge(cruzado[claves + ["valor_final"]].rename(columns={"valor_final": "valor_cruzado"}), on=claves, how="left")
    otro = viejo[viejo["mes"].isin(meses)][claves + ["texto_breve", "categoria", "familia", "valor_final"]].rename(
        columns={"valor_final": "valor_anterior"})
    df = df.merge(otro, on=claves, how="outer", suffixes=("", "_ant"))
    for col in ("texto_breve", "categoria", "familia"):
        df[col] = df[col].fillna(df[f"{col}_ant"])
    df = df.drop(columns=["texto_breve_ant", "categoria_ant", "familia_ant"])
    for col in ("valor_final", "valor_cruzado", "valor_anterior"):
        df[col] = df[col].fillna(0.0)
    df["efecto_cantidad"] = df["valor_cruzado"] - df["valor_anterior"]
    df["efecto_precio"] = df["valor_final"] - df["valor_cruzado"]
    return df.sort_values(claves).reset_index(drop=True)


@_cacheado
def precision_precios_sop(hasta=None):
    """Lo que proyectó cada versión de precios contra el precio real de compra
    (MB51 + FBL3N, precios_entrada_sop_real) en los meses cerrados (antes de
    `hasta`, por defecto el mes en curso): una fila por versión/material/centro/
    mes con horizonte (meses entre la emisión y el mes), error = proyectado ÷
    real − 1 e importe real (para ponderar)."""
    columnas = ["version", "material_codigo", "texto_breve", "centro", "categoria", "mes", "horizonte",
                "proyectado", "real", "importe", "error"]
    hasta = hasta or datetime.date.today().replace(day=1)
    versiones = versiones_precios_sop()
    if not versiones:
        return pd.DataFrame(columns=columnas)
    real = precios_entrada_sop_real()
    real = real[(real["precio"] > 0) & (real["mes"] < hasta)][
        ["material_codigo", "texto_breve", "centro", "categoria", "mes", "precio", "importe"]].rename(columns={"precio": "real"})
    partes = []
    for version in versiones:
        p = precios_sop(version)
        p = p[p["precio"].notna() & (p["precio"] > 0) & (p["mes"] < hasta)]
        if p.empty:
            continue
        partes.append(p[["material_codigo", "centro", "mes", "precio"]].rename(columns={"precio": "proyectado"}).assign(version=version))
    if not partes:
        return pd.DataFrame(columns=columnas)
    df = pd.concat(partes).merge(real, on=["material_codigo", "centro", "mes"], how="inner")
    df["horizonte"] = [(m.year - v.year) * 12 + m.month - v.month for m, v in zip(df["mes"], df["version"])]
    df["error"] = df["proyectado"] / df["real"] - 1
    return df[columnas].sort_values(["version", "centro", "material_codigo", "mes"]).reset_index(drop=True)


def mes_siguiente_sop():
    """Mes de emisión de la próxima versión de precios: el siguiente a la última cargada (o el mes en curso)."""
    versiones = versiones_precios_sop()
    actual = datetime.date.today().replace(day=1)
    return max(_sumar_meses(versiones[-1], 1), actual) if versiones else actual
