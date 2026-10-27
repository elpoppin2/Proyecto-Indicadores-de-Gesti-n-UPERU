"""Origen del precio de compra: producción propia, materiales importados por embarque (FIFO por barco, humedad, acopio) y códigos de material antiguos."""

import datetime
import re

import pandas as pd

from .base import _cacheado, _query, _texto_sql, formato_mes_anio_abrev
from .reglas import CLASES_LIQUIDACION_ORDENES, regla_local


# ---------------------------------------------------------------------------
# Precios de entrada (S&OP): precio de compra por tonelada de los materiales
# del Plan de Abastecimiento, desde MB51. Pedido por el usuario. Reglas
# (descubiertas inspeccionando MB51):
#   - Solo clases 101 (entrada de mercancía por pedido) y 102 (su anulación)
#     CON proveedor: esa es la compra a un tercero, valorizada al precio del
#     pedido. Un 101 sin proveedor es la recepción de un traslado entre
#     centros (entra con importe 0, el valor viaja en el 641) o de una orden
#     de producción (calizas de cantera propia, Clinker: costo estándar, no
#     un precio de compra) — no cuentan.
#   - Los importados (Carbón Importado, Escoria japonesa) se compran en el
#     centro U004 (importación) y de ahí se trasladan a planta: su precio es
#     el del 101 en U004, un barco por mes de llegada.
#   - Precio del mes = importe total ÷ toneladas totales (promedio ponderado
#     por cantidad), en soles (importe en moneda local).
# ---------------------------------------------------------------------------
_CLASES_ENTRADA_COMPRA = "('101', '102')"
CENTRO_IMPORTACION = "U004"
_MINIMO_TRASLADO = 0.02

# Almacenes de traslado (regla del usuario, 2026-10-23): lo que se compra para
# Condorcocha entra primero al almacén A200 de Atocongo (U001) y a los ~5 días
# se traslada a U002 (641: casi todo el valor de 2024-2026, sobre todo Repuestos
# y Suministros Industriales). Cuenta solo puesto en destino: la
# entrada (cantidad, valor y mes de la compra) va a la planta de destino y no a
# Atocongo. Se usa el valor de la compra y no el del traslado: el 641 sale al
# precio promedio de U001, que se mezcla con el stock de otros almacenes.
# Los ajustes de la FBL3N siguen la regla de siempre (con_precio_real): los de
# un material que ya no tiene compras en U001 pasan a su planta de compra.
ALMACENES_TRASLADO = {("U001", "A200"): "U002"}


def _sql_centro_destino(centro="centro", almacen="almacen"):
    """Expresión SQL con la planta de destino de una entrada o pedido: la de
    ALMACENES_TRASLADO si se recibió en un almacén de traslado, si no su centro."""
    casos = " ".join(f"WHEN {centro} = {_texto_sql(c)} AND {almacen} = {_texto_sql(a)} THEN {_texto_sql(d)}"
                     for (c, a), d in ALMACENES_TRASLADO.items())
    return f"(CASE {casos} ELSE {centro} END)" if casos else centro


# Materiales de producción propia que el usuario mide en el IPU/TCO como uno
# comprado (pedido del usuario, oct-2026: la "Caliza CC" de su reporte):
# (material, centro que la recibe) -> centro que la envía. La caliza extraída
# entra a Atocongo por traslado desde Condorcocha (al valor estándar de U002) y
# su costo real llega con la liquidación mensual de la orden CAL_ROCA_AT (KO88/
# CO88, clase PP; X2 = anulación) a la cuenta 2310002000 (FBL3N). Además, en
# cada cierre del Material Ledger el centro que envía le pasa sus diferencias
# de precio (FBL3N clase ML, el mismo documento en los dos centros con signo
# contrario: "ZUPRV: Diferencias de precio de material entrada"), que la
# CKM3N muestra en Entradas > Traslado (pedido del usuario 2026-10-19: ene-26,
# S/ 16,646.38). Precio = (valor del traslado + liquidación + esas
# diferencias) ÷ toneladas de entrada (traslado neto + ajustes de
# humedad/traslado Z15/Z16/Z31/Z32): el mismo valor y las mismas toneladas de
# la fila Entradas de la CKM3N (verificado: ene-26 S/ 2,341,741.71 y
# 36,647.83 T; set-26 39,624.01 T).
PRODUCCION_PROPIA = {("14-00000024", "U001"): "U002"}
CLASES_LIQUIDACION_PRODUCCION = ("PP", "X2")
# Anulaciones de traslado (642) que fueron un error de SAP y no se descuentan
# (pedido del usuario, 2026-10-20): (material, fecha). El 26-jun-26 se
# anularon 137 traslados de la Caliza Roca Extraída (4,607 T) que sí llegaron;
# sin esto jun-26 quedaba con 18,401 T en vez de ~23,000 (precio 83.75 en vez
# de ~67). Las otras anulaciones de junio (un camión cada una) sí cuentan.
ANULACIONES_ERRONEAS = [("14-00000024", datetime.date(2026, 6, 26))]


def _sql_sin_anulaciones_erroneas(material):
    """Condición SQL que deja fuera las 642 de ANULACIONES_ERRONEAS de `material`."""
    fechas = [f"'{f.isoformat()}'" for m, f in ANULACIONES_ERRONEAS if m == material]
    if not fechas:
        return "1"
    return f"NOT (clase_movimiento = '642' AND fecha_contabilizacion IN ({', '.join(fechas)}))"


def _produccion_propia(df, materiales=None):
    """`df` (salida del SQL de precios) más una fila por mes de cada material
    de PRODUCCION_PROPIA (si `materiales` lo incluye): toneladas de entrada e
    importe = valor del traslado (la liquidación entra como ajuste en
    con_precio_real). Un mes con el traslado sin valor en MB51 (pasó en
    jul-25) toma el valor por tonelada del mes anterior."""
    filas = []
    for (material, centro), origen in PRODUCCION_PROPIA.items():
        if materiales is not None and material not in materiales:
            continue
        m = _query(f"""
            SELECT substr(fecha_contabilizacion, 1, 7) || '-01' AS mes, centro, clase_movimiento AS cl,
                   SUM(CASE WHEN cantidad > 0 THEN cantidad ELSE 0 END) AS t_pos,
                   SUM(CASE WHEN cantidad < 0 THEN cantidad ELSE 0 END) AS t_neg,
                   SUM(cantidad) AS t, SUM(importe_moneda_local) AS imp,
                   SUM(CASE WHEN cantidad > 0 THEN 1 ELSE 0 END) AS n_pos
            FROM gestion_movimientomaterial
            WHERE material_codigo = {_texto_sql(material)} AND centro IN ({_texto_sql(centro)}, {_texto_sql(origen)})
                  AND clase_movimiento IN ('641', '642', 'Z15', 'Z16', 'Z31', 'Z32') AND fecha_contabilizacion IS NOT NULL
                  AND {_sql_sin_anulaciones_erroneas(material)}
            GROUP BY 1, 2, 3
        """)
        if m.empty:
            continue
        for c in ("t_pos", "t_neg", "t", "imp"):
            m[c] = m[c].astype(float)
        valor_anterior = None
        for mes, g in sorted(m.groupby("mes"), key=lambda x: x[0]):
            d, o = g[g["centro"] == centro], g[g["centro"] == origen]
            traslado = d.loc[d["cl"] == "641", "t_pos"].sum() + d.loc[d["cl"] == "642", "t_neg"].sum()
            toneladas = traslado + d.loc[d["cl"].isin(["Z15", "Z16", "Z31", "Z32"]), "t"].sum()
            valor = -o.loc[o["cl"].isin(["641", "642"]), "imp"].sum()
            if traslado <= 0.5:
                continue
            if valor <= 0 and valor_anterior:
                valor = valor_anterior * traslado
            valor_anterior = valor / traslado if valor > 0 else valor_anterior
            filas.append({"material_codigo": material, "centro": centro, "mes": mes, "toneladas": toneladas,
                          "importe": valor, "entradas": int(d.loc[d["cl"] == "641", "n_pos"].sum()), "proveedores": 0})
    if not filas:
        return df
    return pd.concat([df, pd.DataFrame(filas)[df.columns]], ignore_index=True)


def liquidaciones_produccion_propia():
    """Costo real de los materiales de PRODUCCION_PROPIA en su centro, por
    mes (FBL3N): la liquidación de sus órdenes (clases PP/X2) más las
    diferencias de precio que le pasa el centro que envía en el cierre del
    Material Ledger (clase ML, el mismo documento en el centro de origen con
    importe contrario). Columnas: material_codigo, centro, mes, liquidacion."""
    claves = " OR ".join(
        f"""(d.material_codigo = {_texto_sql(m)} AND d.centro = {_texto_sql(c)} AND (
                d.clase_documento IN {CLASES_LIQUIDACION_PRODUCCION}
                OR (d.clase_documento = 'ML' AND EXISTS (
                    SELECT 1 FROM gestion_ajustepreciofbl3n o
                    WHERE o.material_codigo = d.material_codigo AND o.centro = {_texto_sql(origen)}
                          AND o.numero_documento = d.numero_documento
                          AND o.fecha_contabilizacion = d.fecha_contabilizacion
                          AND ABS(o.importe + d.importe) < 0.01))))"""
        for (m, c), origen in PRODUCCION_PROPIA.items())
    df = _query(f"""
        SELECT d.material_codigo AS material_codigo, d.centro AS centro,
               substr(d.fecha_contabilizacion, 1, 7) || '-01' AS mes, SUM(d.importe) AS liquidacion
        FROM gestion_ajustepreciofbl3n d
        WHERE {claves}
        GROUP BY 1, 2, 3
    """)
    if not df.empty:
        df["mes"] = pd.to_datetime(df["mes"]).dt.date
    return df


@_cacheado
def materiales_importados():
    """Materiales comprados en el centro de importación (101 con proveedor en
    U004) y trasladados a planta: su precio se mide puesto en destino."""
    # Agrupado en una sola pasada (~1 s; con EXISTS correlacionado eran ~60 s).
    df = _query(f"""
        SELECT material_codigo FROM gestion_movimientomaterial
        WHERE +centro = '{CENTRO_IMPORTACION}' AND clase_movimiento IN ('101', '641')
        GROUP BY material_codigo
        HAVING SUM(clase_movimiento = '101' AND proveedor <> '') > 0 AND SUM(clase_movimiento = '641') > 0
    """)
    return set(df["material_codigo"])


def _traslados_desde_importacion(materiales):
    """Lo que cada planta recibe de U004 por traslado (641, neto de
    anulaciones 642, lado receptor; solo el primer destino), por
    material/centro/mes: toneladas, importe (valor del traslado), entradas.
    Sin los traslados de corrección (ver _MINIMO_TRASLADO)."""
    destino = _query(f"""
        SELECT r.material_codigo AS material_codigo, r.centro AS centro,
               substr(r.fecha_contabilizacion, 1, 7) || '-01' AS mes,
               SUM(r.cantidad) AS toneladas, SUM(r.importe_moneda_local) AS importe,
               SUM(CASE WHEN r.clase_movimiento = '641' THEN 1 ELSE 0 END) AS entradas
        FROM gestion_movimientomaterial r
        WHERE r.material_codigo IN ({",".join(_texto_sql(m) for m in materiales)})
              AND r.centro <> '{CENTRO_IMPORTACION}'
              AND ((r.clase_movimiento = '641' AND r.cantidad > 0) OR (r.clase_movimiento = '642' AND r.cantidad < 0))
              AND EXISTS (SELECT 1 FROM gestion_movimientomaterial o
                          WHERE o.documento_material = r.documento_material
                                AND o.ejercicio_documento_material = r.ejercicio_documento_material
                                AND o.material_codigo = r.material_codigo AND o.centro = '{CENTRO_IMPORTACION}')
        GROUP BY r.material_codigo, r.centro, mes
    """).copy()
    # Traslados de corrección (unas decenas de T con valor revaluado, p.ej.
    # 31 T a S/ 5,000 por T) no son un barco llegando a planta: fuera los
    # meses con menos del _MINIMO_TRASLADO del mayor traslado mensual del material.
    destino["toneladas"] = destino["toneladas"].astype(float)
    destino["importe"] = destino["importe"].astype(float)
    mayor = destino.groupby("material_codigo")["toneladas"].transform("max")
    return destino[destino["toneladas"] >= mayor * _MINIMO_TRASLADO]


@_cacheado
def valores_traslado(materiales):
    """Valor de salida y de llegada de los traslados (641 neto de 642, mismo
    documento) de `materiales` por material/origen/destino/mes: toneladas
    (lado receptor), valor_llega, valor_sale. La diferencia es el flete que
    SAP suma al recibir (de Atocongo a Condorcocha y, mucho menor, de U004 a
    Atocongo)."""
    lista = ",".join(_texto_sql(m) for m in materiales)
    df = _query(f"""
        SELECT r.material_codigo AS material_codigo, o.centro AS origen, r.centro AS destino,
               substr(r.fecha_contabilizacion, 1, 7) || '-01' AS mes,
               SUM(r.cantidad) AS toneladas, SUM(r.importe_moneda_local) AS valor_llega,
               -SUM(o.importe_moneda_local) AS valor_sale
        FROM gestion_movimientomaterial r
        JOIN gestion_movimientomaterial o
          ON o.documento_material = r.documento_material AND o.ejercicio_documento_material = r.ejercicio_documento_material
         AND o.material_codigo = r.material_codigo AND o.clase_movimiento = r.clase_movimiento
         AND o.centro <> r.centro AND o.posicion_documento_material <> r.posicion_documento_material
        WHERE r.material_codigo IN ({lista})
              AND ((r.clase_movimiento = '641' AND r.cantidad > 0) OR (r.clase_movimiento = '642' AND r.cantidad < 0))
        GROUP BY r.material_codigo, o.centro, r.centro, mes
    """)
    for c in ("toneladas", "valor_llega", "valor_sale"):
        df[c] = df[c].astype(float)
    return df


@_cacheado
def ajustes_traslado_destino(materiales):
    """Ajustes por traslado (Z31 entrada / Z32 salida) que la planta de
    destino registra al recibir, ticket por ticket (solo cantidad, sin
    valor), por material/centro/mes: toneladas. Forman parte de lo recibido
    (regla del usuario, 2026-10-26: Condorcocha ene-26 = traslado − Z32 +
    Z31, como su Excel). Sin U004."""
    lista = ",".join(_texto_sql(m) for m in materiales)
    df = _query(f"""
        SELECT material_codigo, centro, substr(fecha_contabilizacion, 1, 7) || '-01' AS mes, SUM(cantidad) AS toneladas
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({lista}) AND clase_movimiento IN ('Z31', 'Z32') AND centro <> '{CENTRO_IMPORTACION}'
        GROUP BY material_codigo, centro, mes
    """)
    df["toneladas"] = df["toneladas"].astype(float)
    return df


_AJUSTE_TRASLADO_MAXIMO = 0.10  # lo normal es 0.1%-2% de lo recibido en el mes


def _descontar_ajustes_traslado(material, lotes, ajustes):
    """Suma a lo recibido por cada planta en un mes sus ajustes por traslado
    (ajustes_traslado_destino): las toneladas de esos lotes cambian en esa
    proporción y su valor no (el precio por tonelada se recalcula). Un
    ajuste de un mes sin llegadas va al último mes anterior con llegadas.
    Uno que movería más del _AJUSTE_TRASLADO_MAXIMO de lo recibido no es un
    ajuste de recepción (abr-25: casi todo lo recibido ese mes; el Excel
    del usuario tampoco lo descuenta) y no se aplica."""
    for a in ajustes[ajustes["material_codigo"] == material].itertuples():
        meses = sorted({x["mes"] for x in lotes if x["centro"] == a.centro and x["mes"] <= a.mes})
        if not meses or abs(a.toneladas) < 1e-6:
            continue
        del_mes = [x for x in lotes if x["centro"] == a.centro and x["mes"] == meses[-1]]
        total = sum(x["cantidad"] for x in del_mes)
        if total <= 1e-6 or abs(a.toneladas) > total * _AJUSTE_TRASLADO_MAXIMO:
            continue
        factor = (total + a.toneladas) / total
        for x in del_mes:
            x["cantidad"] *= factor
            x["recibido"] = x.get("recibido", 0.0) * factor if "recibido" in x else x["cantidad"]
            for c in ("mb51", "ajuste", "ordenes", "flete"):
                x[c] = (x.get(c) or 0.0) / factor


def _por_tonelada(df, valor):
    """{(material, origen, destino, mes): valor ÷ toneladas} de valores_traslado."""
    df = df[df["toneladas"].abs() > 1e-6]
    return {(r.material_codigo, r.origen, r.destino, r.mes): getattr(r, valor) / r.toneladas for r in df.itertuples()}


# ---------------------------------------------------------------------------
# Importados por embarque (pedido del usuario, 2026-10-15). Los cargos de un
# barco (flete, desestiba, demurrage, aduanas, diferencias de factura) llegan
# a la FBL3N semanas o meses después del ingreso a U004: asignados a su mes
# de contabilización distorsionaban el precio de otro mes y, como revalúan el
# stock de U004, terminaban viajando en el traslado de OTRO barco. Ahora:
# - Embarque = ingresos (101/102 con proveedor en U004) de un mismo pedido
#   separados por menos de _DIAS_ENTRE_BARCOS días (un pedido marco trae
#   varios barcos: 4500011752 = Sep-24, Mar-25 y Jul-25).
# - Cada partida de la FBL3N del material (en U004, U001 o U002) va a su
#   embarque: por el pedido 45xxxxxxxx que trae en el pedido o en el texto
#   ("PD4500011752-DESESTIBA", "45/10996 DEMURRAGE"), al barco de ese pedido
#   llegado más recientemente antes de la partida; sin pedido, al último
#   barco llegado antes de la partida. Lo anterior al primer barco cargado
#   (embarques de 2023) queda fuera.
# - Costo del embarque = (valor del ingreso + sus partidas) ÷ toneladas
#   recibidas. Lo que cada planta recibe de U004 se valoriza por FIFO con el
#   costo de los barcos de donde salió (un barco solo abastece desde su
#   llegada). Toneladas sin barco cargado que las cubra (falta la MB51 de
#   ese ingreso) quedan al valor del traslado, sin ajustes.
# - Ajustes por humedad (pedido del usuario, 2026-10-19): MB51 clases Z15
#   (entrada) / Z16 (salida), solo cantidad y sin valor ("Ajuste Humedad
#   SET-25", "M/V EVA BRIGHT"). Van al último barco llegado antes del ajuste,
#   en cualquier centro, y bajan sus toneladas: el costo del barco se reparte
#   en las toneladas secas (Escoria ~6-8% por barco). Cada tonelada recibida
#   lleva la parte de humedad de su barco (FIFO).
# Solo para importados que se compran únicamente en U004 (Carbón Importado,
# Escoria japonesa...); los demás siguen con el reparto por mes.
# ---------------------------------------------------------------------------
_PEDIDO_EN_TEXTO = re.compile(r"(?<!\d)45(\d{8})(?!\d)|(?<!\d)45/(\d{4,8})(?!\d)")
_DIAS_ENTRE_BARCOS = 45
CLASES_AJUSTE_HUMEDAD = ("Z15", "Z16")


def _pedido_de_partida(*campos):
    """Pedido de importación (45xxxxxxxx) citado en una partida, o None."""
    for campo in campos:
        for m in _PEDIDO_EN_TEXTO.finditer(campo or ""):
            return "45" + (m.group(1) or m.group(2).zfill(8))
    return None


def _embarque_de_partidas(partidas, barcos):
    """Embarque de cada partida de la FBL3N (columnas material_codigo,
    fecha, documento_compras, texto, referencia) según los `barcos` de
    embarques_importados: (embarques, criterios), en el orden de `partidas`.
    Criterio: "pedido" (cita el pedido del barco), "fecha" (último barco
    llegado antes) o "anterior" (antes del primer barco cargado: fuera)."""
    # Listas simples por material (ordenadas por llegada): miles de partidas.
    por_material = {}
    for b in barcos.sort_values("llegada").itertuples():
        por_material.setdefault(b.material_codigo, []).append((b.llegada, b.pedido, b.embarque))
    asignados, criterios = [], []
    for material, fecha, doc, texto, ref in zip(partidas["material_codigo"], partidas["fecha"],
                                                partidas["documento_compras"], partidas["texto"], partidas["referencia"]):
        b = por_material.get(material)
        if b is None:
            asignados.append(None); criterios.append("")
            continue
        pedido = _pedido_de_partida(doc, texto, ref)
        del_pedido = [x for x in b if x[1] == pedido] if pedido else []
        llegados = [x for x in b if x[0] <= fecha]
        if del_pedido:
            antes = [x for x in del_pedido if x[0] <= fecha]
            asignados.append((antes[-1] if antes else del_pedido[0])[2]); criterios.append("pedido")
        elif llegados:
            asignados.append(llegados[-1][2]); criterios.append("fecha")
        else:
            asignados.append(None); criterios.append("anterior")
    return asignados, criterios


@_cacheado
def embarques_importados():
    """(embarques, partidas) de los importados comprados solo en U004.
    embarques: material_codigo, embarque, pedido, llegada, toneladas (las
    del ingreso), importe (MB51), ajuste (FBL3N al precio), ajuste_ordenes,
    partidas, humedad (T de sus ajustes Z15/Z16, negativo = se secó).
    partidas: material_codigo, numero_documento, fecha, centro, importe,
    embarque (None = fuera) y criterio (pedido / fecha / anterior)."""
    vacio = (pd.DataFrame(columns=["material_codigo", "embarque", "pedido", "llegada", "toneladas", "importe",
                                   "ajuste", "ajuste_ordenes", "partidas", "humedad"]),
             pd.DataFrame(columns=["material_codigo", "numero_documento", "fecha", "centro", "importe",
                                   "embarque", "criterio"]))
    importados = sorted(materiales_importados())
    if not importados:
        return vacio
    lista = ",".join(_texto_sql(m) for m in importados)
    compras = _query(f"""
        SELECT material_codigo, centro, pedido, fecha_contabilizacion AS fecha,
               SUM(cantidad) AS toneladas, SUM(importe_moneda_local) AS importe
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({lista}) AND +clase_movimiento IN {_CLASES_ENTRADA_COMPRA} AND proveedor <> ''
              AND fecha_contabilizacion IS NOT NULL
        GROUP BY material_codigo, centro, pedido, fecha_contabilizacion
    """)
    if compras.empty:
        return vacio
    solo_u004 = compras.groupby("material_codigo")["centro"].agg(lambda s: set(s) == {CENTRO_IMPORTACION})
    compras = compras[compras["material_codigo"].isin(set(solo_u004[solo_u004].index))].copy()
    compras["fecha"] = pd.to_datetime(compras["fecha"]).dt.date
    compras["toneladas"] = compras["toneladas"].astype(float)
    compras["importe"] = compras["importe"].astype(float)

    barcos = []
    for (material, pedido), g in compras.sort_values("fecha").groupby(["material_codigo", "pedido"]):
        actual, anterior = None, None
        for r in g.itertuples():
            if actual is None or (r.fecha - anterior).days > _DIAS_ENTRE_BARCOS:
                actual = {"material_codigo": material, "pedido": pedido, "llegada": r.fecha, "toneladas": 0.0, "importe": 0.0}
                barcos.append(actual)
            actual["toneladas"] += r.toneladas
            actual["importe"] += r.importe
            anterior = r.fecha
    barcos = pd.DataFrame([b for b in barcos if b["toneladas"] > 0.5])
    if barcos.empty:
        return vacio
    barcos = barcos.sort_values(["material_codigo", "llegada"]).reset_index(drop=True)
    barcos["embarque"] = [f"{p} ({formato_mes_anio_abrev(f)})" for p, f in zip(barcos["pedido"], barcos["llegada"])]

    partidas = _query(f"""
        SELECT material_codigo, numero_documento, fecha_contabilizacion AS fecha, centro, clase_documento,
               documento_compras, texto, referencia, importe
        FROM gestion_ajustepreciofbl3n
        WHERE material_codigo IN ({",".join(_texto_sql(m) for m in barcos["material_codigo"].unique())}) AND importe <> 0
    """)
    partidas["fecha"] = pd.to_datetime(partidas["fecha"]).dt.date
    partidas["importe"] = partidas["importe"].astype(float)
    partidas["embarque"], partidas["criterio"] = _embarque_de_partidas(partidas, barcos)
    ordenes = partidas["clase_documento"].isin(CLASES_LIQUIDACION_ORDENES)
    suma = partidas[partidas["embarque"].notna()].assign(
        ajuste=partidas["importe"].where(~ordenes, 0.0), ajuste_ordenes=partidas["importe"].where(ordenes, 0.0)
    ).groupby("embarque").agg(ajuste=("ajuste", "sum"), ajuste_ordenes=("ajuste_ordenes", "sum"),
                              partidas=("importe", "size"))
    barcos = barcos.merge(suma, left_on="embarque", right_index=True, how="left")
    for c in ("ajuste", "ajuste_ordenes"):
        barcos[c] = barcos[c].fillna(0.0)
    barcos["partidas"] = barcos["partidas"].fillna(0).astype(int)

    # Ajustes por humedad: al último barco llegado antes (sin pedido ni texto
    # que lo cite, criterio "fecha"); los anteriores al primer barco, fuera.
    humedad = _query(f"""
        SELECT material_codigo, fecha_contabilizacion AS fecha, SUM(cantidad) AS toneladas
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({",".join(_texto_sql(m) for m in barcos["material_codigo"].unique())})
              AND clase_movimiento IN {CLASES_AJUSTE_HUMEDAD} AND fecha_contabilizacion IS NOT NULL
        GROUP BY material_codigo, fecha_contabilizacion
    """)
    barcos["humedad"] = 0.0
    if not humedad.empty:
        humedad["fecha"] = pd.to_datetime(humedad["fecha"]).dt.date
        humedad["toneladas"] = humedad["toneladas"].astype(float)
        humedad = humedad.assign(documento_compras="", texto="", referencia="")
        humedad["embarque"], _ = _embarque_de_partidas(humedad, barcos)
        por_barco = humedad[humedad["embarque"].notna()].groupby("embarque")["toneladas"].sum()
        barcos["humedad"] = barcos["embarque"].map(por_barco).fillna(0.0)
    return (barcos[vacio[0].columns].reset_index(drop=True),
            partidas[vacio[1].columns].reset_index(drop=True))


# Centro de acopio (regla del usuario, 2026-10-20): Atocongo recibe Carbón
# Importado de U004 y reenvía una parte a Condorcocha. Para no contar dos
# veces ni cargarle a Atocongo lo que no consume, Atocongo se queda solo con lo
# que no reenvía y Condorcocha cuenta lo que recibe de Atocongo (641 desde
# U001, neto de anulaciones 642), cada tonelada con el costo del barco del
# que salió: lo reenviado en un mes se resta del último barco llegado a
# Atocongo (y si no alcanza, del anterior). centro de acopio -> centro al que
# reenvía.
ACOPIO_IMPORTADOS = {"U001": "U002"}

# Parte de un barco que es del centro de acopio (regla del usuario,
# 2026-10-26), en T secas: el resto del barco es de Condorcocha y cuenta
# cuando llega allá (lo que todavía está en Atocongo esperando su traslado no
# cuenta en ninguna planta). Solo para el PRECIO (IPU, matriz TCO); el
# cumplimiento S&OP sigue con lo que queda físicamente en cada planta (el
# plan pone el barco entero en Atocongo). Sin un barco acá, la regla de
# siempre: Atocongo se queda con lo que no reenvía.
#   - Mar-26: lo que llegó a Atocongo en marzo (neto de humedad) es de
#     Atocongo; lo demás, de Condorcocha.
#   - Ago-26: de lo recibido en septiembre solo una parte es de Atocongo; lo
#     demás se trasladará a Condorcocha en los meses siguientes.
# (material, embarque) -> T secas de Atocongo; las cantidades están en
# datos_locales/reglas_locales.json ("parte_acopio_por_barco").
PARTE_ACOPIO_POR_BARCO = {(m, e): float(t) for m, e, t in regla_local("parte_acopio_por_barco", [])}


def _reenvios_acopio(materiales):
    """Lo que cada centro de ACOPIO_IMPORTADOS reenvía a su destino (641 con
    cantidad > 0 en el destino, neto de 642), por material/mes: origen,
    centro (destino), mes, toneladas, entradas."""
    columnas = ["material_codigo", "origen", "centro", "mes", "toneladas", "entradas"]
    partes = []
    for origen, destino in ACOPIO_IMPORTADOS.items():
        partes.append(_query(f"""
            SELECT r.material_codigo AS material_codigo, '{origen}' AS origen, r.centro AS centro,
                   substr(r.fecha_contabilizacion, 1, 7) || '-01' AS mes, SUM(r.cantidad) AS toneladas,
                   SUM(CASE WHEN r.clase_movimiento = '641' THEN 1 ELSE 0 END) AS entradas
            FROM gestion_movimientomaterial r
            WHERE r.material_codigo IN ({",".join(_texto_sql(m) for m in materiales)}) AND r.centro = '{destino}'
                  AND ((r.clase_movimiento = '641' AND r.cantidad > 0) OR (r.clase_movimiento = '642' AND r.cantidad < 0))
                  AND EXISTS (SELECT 1 FROM gestion_movimientomaterial o
                              WHERE o.documento_material = r.documento_material
                                    AND o.ejercicio_documento_material = r.ejercicio_documento_material
                                    AND o.material_codigo = r.material_codigo AND o.centro = '{origen}')
            GROUP BY r.material_codigo, r.centro, mes
        """))
    df = pd.concat(partes, ignore_index=True) if partes else pd.DataFrame(columns=columnas)
    df["toneladas"] = df["toneladas"].astype(float)
    return df[columnas]


_COLUMNAS_LOTES = ["material_codigo", "centro", "mes", "embarque", "cantidad", "mb51", "ajuste", "ordenes",
                   "entradas", "reenviado", "por_trasladar", "recibido", "flete"]


@_cacheado
def _lotes_por_embarque(parte_acopio=False):
    """Lo que entra a cada planta desde U004, un lote por material/centro/mes
    y barco (embarque None = sin barco cargado, al valor del traslado):
    cantidad (T secas que se quedan en esa planta), costos por tonelada seca
    (mb51, ajuste, ordenes), entradas, reenviado (T llegadas desde el centro
    de acopio) y por_trasladar (T en el acopio que son de la otra planta y
    aún no se trasladan), recibido (T que llegaron a la planta ese mes, sin
    descontar lo que después se reenvía: el real del cumplimiento) y flete
    (S/ por T incluido en mb51). `parte_acopio`: el precio, con
    PARTE_ACOPIO_POR_BARCO y el costo puesto en planta (TCO, pedido del
    usuario 2026-10-26): lo que llega de U004, al costo de su barco más el
    flete del traslado; lo que Condorcocha recibe de Atocongo, al valor con
    que llega según SAP (la valorización del inventario de Atocongo más el
    flete). Sin él, lo físico. Ver reparto_por_embarque."""
    barcos, _ = embarques_importados()
    if barcos.empty:
        return pd.DataFrame(columns=_COLUMNAS_LOTES)
    materiales = sorted(barcos["material_codigo"].unique())
    traslados = _traslados_desde_importacion(materiales)
    reenvios = _reenvios_acopio(materiales)
    valores = valores_traslado(materiales)
    flete_de = {k: v for k, v in _por_tonelada(valores.assign(flete=valores["valor_llega"] - valores["valor_sale"]),
                                                 "flete").items()} if parte_acopio else {}
    llegada_de = _por_tonelada(valores, "valor_llega") if parte_acopio else {}
    ajustes = ajustes_traslado_destino(materiales)
    filas = []
    for material, t in traslados.groupby("material_codigo"):
        # Costos por tonelada SECA de cada barco (la humedad baja sus toneladas).
        cola = []
        for b in barcos[barcos["material_codigo"] == material].itertuples():
            secas = b.toneladas + b.humedad
            cola.append({"embarque": b.embarque, "llegada": b.llegada.replace(day=1), "quedan": b.toneladas,
                         "seca": secas / b.toneladas, "mb51": b.importe / secas, "ajuste": b.ajuste / secas,
                         "ordenes": b.ajuste_ordenes / secas})
        # 1) Lo que cada planta recibe de U004, por FIFO de barcos: un lote por
        #    barco (o sin barco) y mes, con sus costos por tonelada seca.
        lotes = []
        for r in t.sort_values(["mes", "centro"]).itertuples():
            mes = datetime.date.fromisoformat(r.mes)
            falta, primero = r.toneladas, True
            for b in cola:
                if falta <= 1e-6:
                    break
                if b["quedan"] <= 1e-6 or b["llegada"] > mes:
                    continue
                toma = min(falta, b["quedan"])
                flete = flete_de.get((material, CENTRO_IMPORTACION, r.centro, r.mes), 0.0)
                lotes.append({"centro": r.centro, "mes": r.mes, "embarque": b["embarque"], "cantidad": toma * b["seca"],
                              "mb51": b["mb51"] + flete, "ajuste": b["ajuste"], "ordenes": b["ordenes"], "sale": 0.0,
                              "entradas": r.entradas if primero else 0, "flete": flete})
                b["quedan"] -= toma; falta -= toma; primero = False
            if falta > 1e-6:  # sin barco cargado: al valor del traslado, sin ajustes
                lotes.append({"centro": r.centro, "mes": r.mes, "embarque": None, "cantidad": falta,
                              "mb51": r.importe / r.toneladas, "ajuste": 0.0, "ordenes": 0.0, "sale": 0.0,
                              "entradas": r.entradas if primero else 0,
                              "flete": flete_de.get((material, CENTRO_IMPORTACION, r.centro, r.mes), 0.0)})
        # Parte del acopio de cada barco de PARTE_ACOPIO_POR_BARCO: se reserva
        # en sus primeros lotes en el acopio y no se reenvía; lo demás del
        # barco se puede reenviar desde su primer mes en el acopio (un barco
        # llegado a fin de mes se reenvía con lo que entra el mes siguiente).
        for x in lotes:
            x["reserva"], x["desde"], x["con_parte"] = 0.0, x["mes"], False
        for (m, embarque), parte in (PARTE_ACOPIO_POR_BARCO.items() if parte_acopio else ()):
            if m != material:
                continue
            suyos = sorted((x for x in lotes if x["embarque"] == embarque and x["centro"] in ACOPIO_IMPORTADOS),
                           key=lambda x: x["mes"])
            if not suyos:
                continue
            # Lo que llegó al acopio sin barco cargado esos meses (unas
            # centenas de T más que el barco) va con el barco: no es del acopio.
            meses = {x["mes"] for x in suyos}
            suyos += [x for x in lotes if x["embarque"] is None and x["centro"] in ACOPIO_IMPORTADOS and x["mes"] in meses]
            for x in suyos:
                x["reserva"] = min(parte, x["cantidad"]) if x["embarque"] is not None else 0.0
                parte -= x["reserva"]
                # La parte del acopio cuenta toda en el primer mes del barco.
                x["desde"], x["con_parte"] = suyos[0]["mes"], True
        # 2) Reenvíos del centro de acopio: lo reenviado en un mes sale del
        #    último barco llegado al acopio hasta ese mes (y si no alcanza, del
        #    anterior) y entra al destino ese mes, con el costo de su barco. No
        #    FIFO: el acopio también consume lo suyo, así que lo de un barco
        #    viejo ya no está para reenviarse meses después.
        recibidos = []
        for f in reenvios[reenvios["material_codigo"] == material].sort_values("mes").itertuples():
            falta = f.toneladas
            # Precio: lo que Condorcocha recibe de Atocongo entra al valor de
            # la llegada en SAP (inventario de Atocongo + flete), no al de su barco.
            llega = llegada_de.get((material, f.origen, f.centro, f.mes))
            valor = ({"mb51": llega, "ajuste": 0.0, "ordenes": 0.0,
                      "flete": flete_de.get((material, f.origen, f.centro, f.mes), 0.0)} if llega is not None else {})
            disponibles = sorted((x for x in lotes if x["centro"] == f.origen and x["desde"] <= f.mes),
                                 key=lambda x: (x["desde"], x["mes"]), reverse=True)
            primero = True
            for x in disponibles:
                if falta <= 1e-6:
                    break
                libre = x["cantidad"] - x["sale"] - x["reserva"]
                if libre <= 1e-6:
                    continue
                toma = min(falta, libre)
                x["sale"] += toma; falta -= toma
                recibidos.append({**x, **valor, "centro": f.centro, "mes": f.mes, "cantidad": toma, "sale": 0.0,
                                  "entradas": f.entradas if primero else 0, "reenviado": toma, "por_trasladar": 0.0,
                                  "recibido": toma})
                primero = False
            if falta > 1e-6 and disponibles:  # más de lo recibido: al costo del último lote
                ultimo = disponibles[0]
                recibidos.append({**ultimo, **valor, "centro": f.centro, "mes": f.mes, "cantidad": falta, "sale": 0.0,
                                  "entradas": f.entradas if primero else 0, "reenviado": falta, "por_trasladar": 0.0,
                                  "recibido": falta})
        # Ajustes por traslado de la planta que recibe (Z31/Z32): parte de lo recibido.
        _descontar_ajustes_traslado(material, [x for x in lotes if x["centro"] not in ACOPIO_IMPORTADOS] + recibidos, ajustes)
        # 3) Lo que se queda en cada planta (cantidad − reenviado; en un barco
        #    con parte del acopio, solo esa parte: lo demás está por
        #    trasladarse a la otra planta) y lo recibido.
        filas += [{**x, "material_codigo": material, "reenviado": 0.0, "mes": x["desde"], "recibido": x["cantidad"],
                   "cantidad": x["reserva"] if x["con_parte"] else x["cantidad"] - x["sale"],
                   "por_trasladar": x["cantidad"] - x["sale"] - x["reserva"] if x["con_parte"] else 0.0} for x in lotes]
        filas += [{**x, "material_codigo": material} for x in recibidos]
    return pd.DataFrame(filas, columns=_COLUMNAS_LOTES)


def _mes_principal_por_barco(lotes):
    """Mes en que cuenta cada barco en cada planta para el precio (regla del
    usuario, 2026-10-25): un barco se traslada a fin de mes y su llegada a
    planta se partía en dos meses (el de Ene-25 de la Escoria: 15.8 mil T en
    enero y 18.2 mil T en febrero). Todo lo de un barco en una planta va al
    primer mes en que llegó a esa planta (el usuario lo eligió sobre el mes
    con más toneladas); lo que no tiene barco cargado queda en su mes. Solo
    los materiales que no pasan por el centro de acopio (la
    Escoria): el Carbón Importado llega a Condorcocha por reenvíos de
    Atocongo a lo largo de 3 a 5 meses, que no son un traslado partido a fin
    de mes. Devuelve los lotes con ese mes."""
    con_acopio = set(lotes.loc[lotes["reenviado"] > 0, "material_codigo"])
    con_barco = lotes["embarque"].notna() & ~lotes["material_codigo"].isin(con_acopio)
    if not con_barco.any():
        return lotes
    por_mes = lotes[con_barco].groupby(["material_codigo", "centro", "embarque", "mes"])["cantidad"].sum().reset_index()
    # Primer mes con toneladas de verdad (no un redondeo de la cola FIFO).
    principal = por_mes[por_mes["cantidad"] >= 0.5].groupby(["material_codigo", "centro", "embarque"])["mes"].min()
    claves = list(zip(lotes.loc[con_barco, "material_codigo"], lotes.loc[con_barco, "centro"], lotes.loc[con_barco, "embarque"]))
    nuevos = [principal.get(c, m) for c, m in zip(claves, lotes.loc[con_barco, "mes"])]
    # Lo que llegó sin barco cargado un mes en que también llegó un barco va
    # con ese barco (si no, quedaba solo, p.ej. 77 T en feb-25 al valor del
    # traslado, y pasaba a ser la base del IPU de mar-26).
    mover = {}
    for (material, centro, _), mes, nuevo in zip(claves, lotes.loc[con_barco, "mes"], nuevos):
        mover.setdefault((material, centro, mes), nuevo)
    sin_barco = lotes["embarque"].isna() & ~lotes["material_codigo"].isin(con_acopio)
    lotes = lotes.copy()
    lotes.loc[con_barco, "mes"] = nuevos
    lotes.loc[sin_barco, "mes"] = [mover.get((a, c, m), m) for a, c, m in
                                   zip(lotes.loc[sin_barco, "material_codigo"], lotes.loc[sin_barco, "centro"], lotes.loc[sin_barco, "mes"])]
    return lotes


@_cacheado
def reparto_por_embarque(para_precio=False):
    """Lo que entra a cada planta desde U004 (material/centro/mes, mismos
    traslados que _puesto_en_destino) valorizado por FIFO con sus embarques,
    en toneladas secas (menos la humedad de sus barcos) y con el reenvío del
    centro de acopio (ACOPIO_IMPORTADOS): toneladas, importe (MB51 de los
    barcos + valor del traslado de lo que no tiene barco cargado), ajuste,
    ajuste_ordenes, entradas, reenviado (T que llegaron desde el centro de
    acopio), sin_embarque (T), embarques (texto), por_trasladar, recibido (T
    llegadas a la planta en el mes, sin descontar reenvíos: el real del
    cumplimiento) y flete (S/ incluidos en importe).
    `para_precio`: cada barco cuenta en un solo mes por planta
    (_mes_principal_por_barco) y el acopio solo con su parte de cada barco
    (PARTE_ACOPIO_POR_BARCO); sin él, cada mes con lo que llegó y quedó
    físicamente (el real del cumplimiento S&OP)."""
    columnas = ["material_codigo", "centro", "mes", "toneladas", "importe", "ajuste", "ajuste_ordenes",
                "entradas", "reenviado", "sin_embarque", "embarques", "por_trasladar", "recibido", "flete"]
    lotes = _lotes_por_embarque(parte_acopio=para_precio)
    if lotes.empty:
        return pd.DataFrame(columns=columnas)
    if para_precio:
        lotes = _mes_principal_por_barco(lotes)
    filas = []
    for (material, centro, mes), g in lotes.groupby(["material_codigo", "centro", "mes"], sort=True):
        cantidad = g["cantidad"]
        filas.append({
            "material_codigo": material, "centro": centro, "mes": mes, "toneladas": float(cantidad.sum()),
            "importe": float((cantidad * g["mb51"]).sum()), "ajuste": float((cantidad * g["ajuste"]).sum()),
            "ajuste_ordenes": float((cantidad * g["ordenes"]).sum()), "entradas": int(g["entradas"].sum()),
            "reenviado": float(g["reenviado"].sum()), "sin_embarque": float(cantidad[g["embarque"].isna()].sum()),
            # En el orden en que entraron (FIFO), sin repetir.
            "embarques": ", ".join(dict.fromkeys(e for e, c in zip(g["embarque"], cantidad) if pd.notna(e) and c > 1e-6)),
            "por_trasladar": float(g["por_trasladar"].sum()),
            "recibido": float(g["recibido"].sum()), "flete": float((cantidad * g["flete"].fillna(0.0)).sum()),
        })
    return pd.DataFrame(filas, columns=columnas).sort_values(["material_codigo", "centro", "mes"]).reset_index(drop=True)


def _puesto_en_destino(df):
    """Regla del usuario (2026-10-14): el IPU no considera el centro de
    importación U004, sino el valor PUESTO EN DESTINO (U001/U002). Las
    compras de `df` en U004 se reemplazan por lo que cada planta recibe de
    U004 por traslado (641, neto de anulaciones 642, lado receptor): misma
    cantidad, al valor con que entra a la planta. Lo que el centro de acopio
    (Atocongo) reenvía a Condorcocha cuenta en Condorcocha y no en Atocongo
    (ACOPIO_IMPORTADOS, importados por embarque). Lo comprado en U004 que
    nunca se traslada (repuestos y suministros del muelle, ~S/ 8 MM) queda
    fuera del análisis.
    `df`: material_codigo, centro, mes (texto 'AAAA-MM-01'), toneladas,
    importe, entradas, proveedores (salida del SQL de los precios)."""
    if df.empty or not (df["centro"] == CENTRO_IMPORTACION).any():
        return df
    materiales = sorted(df.loc[df["centro"] == CENTRO_IMPORTACION, "material_codigo"].unique())
    destino = _traslados_desde_importacion(materiales).copy()
    # Importados por embarque: lo recibido sale de reparto_por_embarque (el
    # valor MB51 del barco de donde salió por FIFO, no el valor del traslado
    # que trae revaluaciones de U004 de otros barcos; toneladas secas; el
    # reenvío del centro de acopio ya repartido entre las plantas).
    reparto = reparto_por_embarque(para_precio=True)  # el precio: un mes por barco
    if not reparto.empty:
        # Un mes en que el acopio reenvió todo lo recibido queda sin toneladas.
        propios = reparto[reparto["material_codigo"].isin(materiales) & (reparto["toneladas"] >= 0.5)]
        destino = pd.concat([destino[~destino["material_codigo"].isin(set(reparto["material_codigo"]))],
                             propios[["material_codigo", "centro", "mes", "toneladas", "importe", "entradas"]]],
                            ignore_index=True)
    proveedores = df[df["centro"] == CENTRO_IMPORTACION].groupby("material_codigo")["proveedores"].max()
    destino["proveedores"] = destino["material_codigo"].map(proveedores).fillna(1).astype(int)
    resto = df[df["centro"] != CENTRO_IMPORTACION]
    # Un material con compra local y traslado desde U004 en el mismo centro y
    # mes: se suman (entra a la planta por las dos vías).
    junto = pd.concat([resto, destino[resto.columns]], ignore_index=True)
    junto["toneladas"] = junto["toneladas"].astype(float)
    junto["importe"] = junto["importe"].astype(float)
    return junto.groupby(["material_codigo", "centro", "mes"], as_index=False).agg(
        toneladas=("toneladas", "sum"), importe=("importe", "sum"),
        entradas=("entradas", "sum"), proveedores=("proveedores", "max"))


# Cambios de código (definidos por el usuario, 2026-10-19): para los precios y
# el IPU, las compras del código antiguo cuentan como del código nuevo, así la
# serie de precios arranca en 2024 y no en el cambio de código.
# - 07-00000005 (Caliza Terceros, Atocongo) se separó en jul-2025 por
#   proveedor: Roca Fuerte (1000717) -> 07-00003137 y Andina (10006751) ->
#   07-00003138. Sus ajustes de la FBL3N (casi todos cambios de precio MR22,
#   sin proveedor) van todos a Roca Fuerte (AJUSTES_CODIGOS_ANTERIORES).
# - 07-00000008 Pelets Chips Recuperados (Atocongo) -> 07-00003147.
CODIGOS_ANTERIORES_POR_PROVEEDOR = {"07-00000005": {"1000717": "07-00003137", "10006751": "07-00003138"}}
CODIGOS_ANTERIORES = {"07-00000008": "07-00003147"}
# Código al que van los ajustes de la FBL3N de cada código antiguo.
AJUSTES_CODIGOS_ANTERIORES = {**CODIGOS_ANTERIORES, "07-00000005": "07-00003137"}


def _sql_material_vigente(material="material_codigo", proveedor="proveedor"):
    """Expresión SQL con el código vigente de cada línea (ver CODIGOS_ANTERIORES)."""
    casos = [f"WHEN {material} = '{a}' AND {proveedor} = '{p}' THEN '{n}'"
             for a, por in CODIGOS_ANTERIORES_POR_PROVEEDOR.items() for p, n in por.items()]
    casos += [f"WHEN {material} = '{a}' THEN '{n}'" for a, n in CODIGOS_ANTERIORES.items()]
    return f"(CASE {' '.join(casos)} ELSE {material} END)" if casos else material


def _con_codigos_anteriores(materiales):
    """`materiales` más los códigos antiguos que pasan a alguno de ellos."""
    nuevos = set(materiales)
    anteriores = {a for a, n in CODIGOS_ANTERIORES.items() if n in nuevos}
    anteriores |= {a for a, por in CODIGOS_ANTERIORES_POR_PROVEEDOR.items() if nuevos & set(por.values())}
    return sorted(nuevos | anteriores)


def _sql_traspasos_cambio_codigo():
    """Condición SQL de las entradas por traspaso entre materiales (309) con
    que el stock de un código antiguo pasó a su código nuevo: cuentan como
    compra del código nuevo, con su cantidad y valor (pedido del usuario,
    2026-10-20; así lo mide su Excel de IPU). Jul-25 "CAMBIO COD. COORD
    COSTOS": 07-00000005 -> Roca Fuerte 5,778 T y Andina 8,667 T, a S/ 55.75.
    Solo el lado que entra (cantidad > 0) y solo documentos donde sale un
    código antiguo; "0" si no hay ninguno."""
    antiguos = sorted(set(CODIGOS_ANTERIORES) | set(CODIGOS_ANTERIORES_POR_PROVEEDOR))
    if not antiguos:
        return "0"
    docs = _query(f"""
        SELECT DISTINCT documento_material AS doc FROM gestion_movimientomaterial
        WHERE material_codigo IN ({",".join(_texto_sql(m) for m in antiguos)}) AND clase_movimiento = '309' AND cantidad < 0
    """)
    if docs.empty:
        return "0"
    lista = ",".join(_texto_sql(d) for d in docs["doc"])
    return (f"(clase_movimiento = '309' AND cantidad > 0 AND documento_material IN ({lista}) "
            f"AND material_codigo NOT IN ({','.join(_texto_sql(m) for m in antiguos)}))")
