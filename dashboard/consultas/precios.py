"""Precios de entrada y precio real (MB51 + ajustes FBL3N), e IPU / inflación de precios."""

import datetime

import pandas as pd

from .base import _cacheado, _query, _query_directo, _sumar_meses, _texto_sql
from .importados import AJUSTES_CODIGOS_ANTERIORES, CENTRO_IMPORTACION, PRODUCCION_PROPIA, _CLASES_ENTRADA_COMPRA, _con_codigos_anteriores, _embarque_de_partidas, _produccion_propia, _puesto_en_destino, _sql_centro_destino, _sql_material_vigente, _sql_traspasos_cambio_codigo, embarques_importados, liquidaciones_produccion_propia, materiales_importados, reparto_por_embarque, valores_traslado
from .inventario import catalogo_materiales
from .reglas import CLASES_LIQUIDACION_ORDENES, regla_local
from .sop_base import materiales_planificados_sop


# IPU de Planificación en tres categorías (pedido del usuario, 2026-10-26):
# Materias Primas, Combustible Sólido (los combustibles del S&OP, carbones) y
# Combustible Líquido (los petróleos, que no están en el S&OP; como la hoja
# "Energ Líq" del Excel IPU del usuario: solo Atocongo y Condorcocha).
CATEGORIA_COMBUSTIBLE_SOLIDO = "COMBUSTIBLE SÓLIDO"
CATEGORIA_COMBUSTIBLE_LIQUIDO = "COMBUSTIBLE LÍQUIDO"
# material -> (centros, categoría del IPU)
MATERIALES_IPU_ADICIONALES = {
    "06-00000023": (("U001", "U002"), CATEGORIA_COMBUSTIBLE_LIQUIDO),  # Petróleo Diesel B5 S-50 (galones)
    "06-00000026": (("U001", "U002"), CATEGORIA_COMBUSTIBLE_LIQUIDO),  # Petróleo Industrial N°6
}


@_cacheado
def precios_entrada_sop():
    """Una fila por material/centro/mes con compras: toneladas (la cantidad,
    en la unidad_medida_base del material: el diésel en galones), importe
    (S/), precio (S/ por unidad), entradas y proveedores distintos, más
    texto_breve y categoría del IPU. Materiales: los del Plan de
    Abastecimiento S&OP y MATERIALES_IPU_ADICIONALES (en sus centros)."""
    columnas = ["material_codigo", "texto_breve", "unidad_medida_base", "centro", "categoria", "mes",
                "toneladas", "importe", "precio", "entradas", "proveedores"]
    materiales = sorted(set(materiales_planificados_sop()) | set(MATERIALES_IPU_ADICIONALES))
    if not materiales:
        return pd.DataFrame(columns=columnas)
    # "+clase_movimiento": que SQLite use el índice por material (muy
    # selectivo, ~25 materiales) y no el de clase de movimiento (millones de
    # 101 de todos los materiales). Mismo resultado. Los códigos antiguos
    # (CODIGOS_ANTERIORES) entran con su código nuevo.
    vigente = _sql_material_vigente()
    destino = _sql_centro_destino()  # almacén de traslado (A200) -> planta de destino
    df = _query(f"""
        SELECT {vigente} AS material_codigo, {destino} AS centro,
               substr(fecha_contabilizacion, 1, 7) || '-01' AS mes,
               SUM(cantidad) AS toneladas, SUM(importe_moneda_local) AS importe,
               SUM(CASE WHEN clase_movimiento = '101' THEN 1 ELSE 0 END) AS entradas,
               COUNT(DISTINCT NULLIF(proveedor, '')) AS proveedores
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({",".join(f"'{m}'" for m in _con_codigos_anteriores(materiales))})
              AND ((+clase_movimiento IN {_CLASES_ENTRADA_COMPRA} AND proveedor <> '') OR {_sql_traspasos_cambio_codigo()})
              AND fecha_contabilizacion IS NOT NULL
        GROUP BY {vigente}, {destino}, mes
    """)
    df = _produccion_propia(_puesto_en_destino(df), set(materiales))
    if df.empty:
        return pd.DataFrame(columns=columnas)
    df["toneladas"] = df["toneladas"].astype(float)
    df["importe"] = df["importe"].astype(float)
    # Un mes con la entrada anulada por completo (101 + 102) no tiene precio.
    df = df[(df["toneladas"] > 0.5) & (df["importe"] > 0)].copy()
    df["precio"] = df["importe"] / df["toneladas"]
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    # Los adicionales, solo en sus centros (el diésel también se compra en U031 y U004).
    fuera = pd.Series([m in MATERIALES_IPU_ADICIONALES and c not in MATERIALES_IPU_ADICIONALES[m][0]
                       for m, c in zip(df["material_codigo"], df["centro"])], index=df.index)
    df = df[~fuera]
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "unidad_medida_base", "categoria"]]
    df = df.merge(catalogo, on="material_codigo", how="left")
    df.loc[df["categoria"].isna(), "categoria"] = "MATERIAS PRIMAS"
    df.loc[df["categoria"] == "COMBUSTIBLES", "categoria"] = CATEGORIA_COMBUSTIBLE_SOLIDO
    for material, (_, categoria) in MATERIALES_IPU_ADICIONALES.items():
        df.loc[df["material_codigo"] == material, "categoria"] = categoria
    df["texto_breve"] = df["texto_breve"].fillna("")
    df["unidad_medida_base"] = df["unidad_medida_base"].fillna("T")
    return df[columnas].sort_values(["material_codigo", "centro", "mes"]).reset_index(drop=True)


@_cacheado
def precios_entrada_materiales():
    """Lo mismo que precios_entrada_sop, pero de TODOS los materiales con
    compra (101/102 con proveedor), para la pestaña Precios e IPU de cada
    categoría. Pedido por el usuario. La columna "toneladas" es la cantidad en
    la UMB del material (KG, UND, M...; en T solo para materias primas y
    combustibles), con el mismo nombre para reusar inflacion_precios e
    ipu_por_mes; "precio" queda en S/ por UMB. Sin filtro de material, la
    consulta recorre todos los 101/102 (~11 s en frío), por eso va en caché."""
    columnas = ["material_codigo", "texto_breve", "unidad_medida_base", "centro", "categoria", "familia", "mes",
                "toneladas", "importe", "precio", "entradas", "proveedores"]
    vigente = _sql_material_vigente()  # códigos antiguos con su código nuevo
    destino = _sql_centro_destino()  # almacén de traslado (A200) -> planta de destino
    df = _query(f"""
        SELECT {vigente} AS material_codigo, {destino} AS centro,
               substr(fecha_contabilizacion, 1, 7) || '-01' AS mes,
               SUM(cantidad) AS toneladas, SUM(importe_moneda_local) AS importe,
               SUM(CASE WHEN clase_movimiento = '101' THEN 1 ELSE 0 END) AS entradas,
               COUNT(DISTINCT NULLIF(proveedor, '')) AS proveedores
        FROM gestion_movimientomaterial
        WHERE ((clase_movimiento IN {_CLASES_ENTRADA_COMPRA} AND proveedor <> '') OR {_sql_traspasos_cambio_codigo()})
              AND material_codigo <> '' AND fecha_contabilizacion IS NOT NULL
        GROUP BY {vigente}, {destino}, mes
    """)
    df = _produccion_propia(_puesto_en_destino(df))
    if df.empty:
        return pd.DataFrame(columns=columnas)
    df["toneladas"] = df["toneladas"].astype(float)
    df["importe"] = df["importe"].astype(float)
    df = df[(df["toneladas"] > 0) & (df["importe"] > 0)].copy()
    df["precio"] = df["importe"] / df["toneladas"]
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    catalogo = catalogo_materiales()[["material_codigo", "texto_breve", "unidad_medida_base", "categoria", "familia"]]
    df = df.merge(catalogo, on="material_codigo", how="inner")
    df["texto_breve"] = df["texto_breve"].fillna("")
    df["unidad_medida_base"] = df["unidad_medida_base"].fillna("")
    df["familia"] = df["familia"].fillna("")
    return df[columnas].sort_values(["material_codigo", "centro", "mes"]).reset_index(drop=True)


def concepto_ajuste(clase, transaccion, texto):
    """De qué trata un ajuste de la FBL3N, según su clase de documento, código
    de transacción y texto (reglas vistas en las extracciones 2024-2026)."""
    texto = (texto or "").upper()
    if clase == "PR":
        return "Cambio de precio (MR22)"
    if clase in CLASES_LIQUIDACION_ORDENES:
        return "Liquidación de órdenes (KO88)"
    if transaccion == "MR8M" or clase in ("XK", "XS"):
        return "Anulación de factura (MR8M)"
    if transaccion == "MIRO":
        if "ADUAN" in texto or "DUA" in texto:
            return "Cargos de importación"
        if "FLETE" in texto or "TRANSP" in texto:
            return "Flete / transporte"
        if "DESESTIBA" in texto or "DESCARGA" in texto:
            return "Desestiba / descarga"
        if "ANTICIPO" in texto:
            return "Liquidación de anticipo"
        return "Factura de proveedor (MIRO)"
    if transaccion.startswith("FB"):
        return "Asiento contable manual"
    return "Otros"


# Partidas de la FBL3N que NO entran al precio real (pedido del usuario,
# 2026-10-20), solo para ese material: (material, número de documento). En la
# CKM3N de mar-26 de la Puzolana de Terceros (U001) aparecen como "Otras
# entradas/consumos > Debe/haber" (cargo/descargo de material, MR22), fuera
# de Entradas (855.517 T, S/ 110,573.92): sumarlas bajaba el precio de 129.25
# a ~112.
AJUSTES_EXCLUIDOS = [("07-00000055", "3100005265"), ("07-00000055", "3100005280")]


def _sql_ajuste_excluido(material="material_codigo", documento="numero_documento"):
    """Condición SQL verdadera para las partidas de AJUSTES_EXCLUIDOS."""
    if not AJUSTES_EXCLUIDOS:
        return "0"
    return "(" + " OR ".join(f"({material} = {_texto_sql(m)} AND {documento} = {_texto_sql(d)})"
                             for m, d in AJUSTES_EXCLUIDOS) + ")"


@_cacheado
def ajustes_precio_fbl3n():
    """Ajustes de la FBL3N sumados por material/centro/mes: ajuste (S/, entra
    al precio real), ajuste_ordenes (S/, liquidaciones de órdenes, aparte) y
    lineas. Vacío si todavía no se cargó la FBL3N. Los códigos antiguos
    pasan al código nuevo de AJUSTES_CODIGOS_ANTERIORES. Sin las partidas de
    AJUSTES_EXCLUIDOS."""
    df = _query(f"""
        SELECT material_codigo, centro, substr(fecha_contabilizacion, 1, 7) || '-01' AS mes,
               SUM(CASE WHEN clase_documento IN {CLASES_LIQUIDACION_ORDENES} THEN 0 ELSE importe END) AS ajuste,
               SUM(CASE WHEN clase_documento IN {CLASES_LIQUIDACION_ORDENES} THEN importe ELSE 0 END) AS ajuste_ordenes,
               COUNT(*) AS lineas
        FROM gestion_ajustepreciofbl3n
        WHERE NOT {_sql_ajuste_excluido()}
        GROUP BY material_codigo, centro, mes
    """)
    if not df.empty:
        df["mes"] = pd.to_datetime(df["mes"]).dt.date
        df["material_codigo"] = df["material_codigo"].replace(AJUSTES_CODIGOS_ANTERIORES)
        df = df.groupby(["material_codigo", "centro", "mes"], as_index=False).agg(
            ajuste=("ajuste", "sum"), ajuste_ordenes=("ajuste_ordenes", "sum"), lineas=("lineas", "sum"))
    return df


def meses_con_fbl3n():
    """Meses cubiertos por alguna extracción de la FBL3N (para no mostrar como
    "precio real" un mes que no tiene sus ajustes cargados)."""
    df = _query("SELECT DISTINCT substr(fecha_contabilizacion, 1, 7) || '-01' AS mes FROM gestion_ajustepreciofbl3n")
    return {datetime.date.fromisoformat(m) for m in df["mes"]}


def con_precio_real(base):
    """`base` (salida de precios_entrada_sop / precios_entrada_materiales) con
    el precio real: importe_mb51 y precio_mb51 (lo de antes), ajuste,
    ajuste_trasladado (la parte que vino de meses sin compra o de otro
    centro), ajuste_ordenes (aparte), con_fbl3n (el mes tiene la FBL3N
    cargada), y "importe"/"precio" pasan a ser los reales, para que el resto
    (resumen, gráficos, inflación) funcione igual con cualquiera de los dos."""
    df = base.copy()
    df["importe_mb51"] = df["importe"]
    df["precio_mb51"] = df["precio"]
    for c in ("ajuste", "ajuste_trasladado", "ajuste_ordenes"):
        df[c] = 0.0
    meses = meses_con_fbl3n()
    df["con_fbl3n"] = df["mes"].isin(meses)
    # Producción propia sin la FBL3N del mes (la liquidación de la orden se
    # registra en el cierre): sin la liquidación su precio sería solo el
    # traslado estándar (~S/ 7 por T), así que ese mes todavía no tiene precio.
    propio = pd.Series([(m, c) in PRODUCCION_PROPIA for m, c in zip(df["material_codigo"], df["centro"])], index=df.index)
    df = df[~(propio & ~df["con_fbl3n"])]
    aj = ajustes_precio_fbl3n()
    if df.empty or aj.empty:
        return df
    aj = aj[aj["material_codigo"].isin(set(df["material_codigo"]))].copy()
    # Lo ajustado en U004 ya viaja en el valor del traslado a la planta
    # (puesto en destino, ver _puesto_en_destino): no se suma otra vez.
    aj = aj[aj["centro"] != CENTRO_IMPORTACION]
    # Producción propia: solo la liquidación de su orden y las diferencias
    # que le pasa el centro de origen (abajo, liquidaciones_produccion_propia),
    # no el resto del cierre del Material Ledger ni lo demás de la cuenta 2310002000.
    propios = {m for m, _ in PRODUCCION_PROPIA}
    aj = aj[~aj["material_codigo"].isin(propios)]
    importados = materiales_importados()
    # Importados por embarque: sus ajustes vienen del FIFO de embarques
    # (abajo), no del mes de contabilización.
    reparto = reparto_por_embarque(para_precio=True)  # los mismos meses que el precio
    por_embarque = set(reparto["material_codigo"])
    aj = aj[~aj["material_codigo"].isin(por_embarque)]
    columnas = ["ajuste", "ajuste_ordenes", "ajuste_trasladado"]

    # Materiales locales: al mismo centro (si nunca se compra en el centro del
    # ajuste, al único centro de compra) y al mes de contabilización (si no
    # hubo compra, al último mes anterior con compra).
    local = aj[~aj["material_codigo"].isin(importados)].copy()
    centros_compra = df.groupby("material_codigo")["centro"].agg(lambda s: set(s)).to_dict()

    def centro_destino(material, centro):
        compra = centros_compra.get(material, set())
        if centro in compra:
            return centro
        return next(iter(compra)) if len(compra) == 1 else None

    local["centro_destino"] = [centro_destino(m, c) for m, c in zip(local["material_codigo"], local["centro"])]
    local = local[local["centro_destino"].notna()]
    local = _prorratear_por_contrato(local)
    compras = df[["material_codigo", "centro", "mes"]].rename(columns={"centro": "centro_destino", "mes": "mes_destino"})
    compras["_clave"] = pd.to_datetime(compras["mes_destino"])
    local["_clave"] = pd.to_datetime(local["mes"])
    local = pd.merge_asof(local.sort_values("_clave"), compras.sort_values("_clave"), on="_clave",
                          by=["material_codigo", "centro_destino"], direction="backward")
    local = local[local["mes_destino"].notna()]
    trasladado = (local["mes_destino"] != local["mes_origen"]) | (local["centro_destino"] != local["centro"])
    local["ajuste_trasladado"] = local["ajuste"].where(trasladado, 0.0)
    partes = [local.groupby(["material_codigo", "centro_destino", "mes_destino"])[columnas].sum().reset_index()
              .rename(columns={"centro_destino": "centro", "mes_destino": "mes"})]

    # Importados (puestos en destino): el carbón que ajusta Condorcocha llegó
    # por Atocongo, así que el ajuste no se ata a la planta donde se registró:
    # va al material y mes (o al último mes anterior con llegada a planta) y se
    # reparte entre las plantas que recibieron ese mes según sus toneladas.
    imp = aj[aj["material_codigo"].isin(importados)]
    if not imp.empty:
        imp = imp.groupby(["material_codigo", "mes"], as_index=False)[["ajuste", "ajuste_ordenes"]].sum()
        llegadas = df[df["material_codigo"].isin(importados)].groupby(["material_codigo", "mes"], as_index=False)["toneladas"].sum()
        meses_llegada = llegadas[["material_codigo", "mes"]].rename(columns={"mes": "mes_destino"})
        meses_llegada["_clave"] = pd.to_datetime(meses_llegada["mes_destino"])
        imp["_clave"] = pd.to_datetime(imp["mes"])
        imp = pd.merge_asof(imp.sort_values("_clave"), meses_llegada.sort_values("_clave"), on="_clave",
                            by="material_codigo", direction="backward")
        imp = imp[imp["mes_destino"].notna()]
        imp["ajuste_trasladado"] = imp["ajuste"].where(imp["mes_destino"] != imp["mes"], 0.0)
        imp = imp.groupby(["material_codigo", "mes_destino"], as_index=False)[columnas].sum()
        filas = df.loc[df["material_codigo"].isin(importados), ["material_codigo", "centro", "mes", "toneladas"]]
        filas = filas.merge(imp.rename(columns={"mes_destino": "mes"}), on=["material_codigo", "mes"])
        participacion = filas["toneladas"] / filas.groupby(["material_codigo", "mes"])["toneladas"].transform("sum")
        for c in columnas:
            filas[c] = filas[c] * participacion
        partes.append(filas[["material_codigo", "centro", "mes", *columnas]])

    if not reparto.empty:
        r = reparto.assign(mes=pd.to_datetime(reparto["mes"]).dt.date)
        r = r.merge(df[["material_codigo", "centro", "mes"]], on=["material_codigo", "centro", "mes"])
        partes.append(r.assign(ajuste_trasladado=0.0)[["material_codigo", "centro", "mes", *columnas]])

    # Producción propia: la liquidación de la orden del mes (o, si ese mes no
    # hubo traslado, del último mes anterior con traslado).
    liq = liquidaciones_produccion_propia()
    liq = liq.merge(df[["material_codigo", "centro"]].drop_duplicates(), on=["material_codigo", "centro"]) if not liq.empty else liq
    if not liq.empty:
        destinos = df[["material_codigo", "centro", "mes"]].rename(columns={"mes": "mes_destino"})
        destinos["_clave"] = pd.to_datetime(destinos["mes_destino"])
        liq = liq.assign(_clave=pd.to_datetime(liq["mes"]))
        liq = pd.merge_asof(liq.sort_values("_clave"), destinos.sort_values("_clave"), on="_clave",
                            by=["material_codigo", "centro"], direction="backward")
        liq = liq[liq["mes_destino"].notna()]
        partes.append(pd.DataFrame({
            "material_codigo": liq["material_codigo"], "centro": liq["centro"], "mes": liq["mes_destino"],
            "ajuste": liq["liquidacion"].astype(float), "ajuste_ordenes": 0.0,
            "ajuste_trasladado": liq["liquidacion"].astype(float).where(liq["mes_destino"] != liq["mes"], 0.0)}))
    suma = pd.concat(partes, ignore_index=True).groupby(["material_codigo", "centro", "mes"], as_index=False)[columnas].sum()
    df = df.drop(columns=["ajuste", "ajuste_trasladado", "ajuste_ordenes"]).merge(
        suma, on=["material_codigo", "centro", "mes"], how="left")
    for c in ("ajuste", "ajuste_trasladado", "ajuste_ordenes"):
        df[c] = df[c].fillna(0.0)
    df["importe"] = df["importe_mb51"] + df["ajuste"]
    df["precio"] = df["importe"] / df["toneladas"]
    return df


# Ajustes de la FBL3N que se reparten entre lo recibido con un contrato marco
# (definido por el usuario, 2026-10-19): (material, mes del ajuste) ->
# contrato. El cambio de precio MR22 de mar-2026 de los Pelets ("Traslado de
# valor", S/ 726 mil, sin pedido) es de lo recibido con el contrato
# 4600000955 (nov-25 a ene-26): va a esos meses según sus toneladas, no todo
# al último mes con compra.
AJUSTES_POR_CONTRATO = {("07-00003147", datetime.date(2026, 3, 1)): "4600000955"}


def _prorratear_por_contrato(local):
    """`local` (ajustes por material/centro/mes, con centro_destino) con los
    de AJUSTES_POR_CONTRATO repartidos entre los meses en que se recibió con
    ese contrato (MB51, según toneladas). Agrega mes_origen: el mes del ajuste."""
    local = local.assign(mes_origen=local["mes"])
    regla = pd.Series([(m, mes) in AJUSTES_POR_CONTRATO for m, mes in zip(local["material_codigo"], local["mes"])],
                      index=local.index, dtype=bool)
    if not regla.any():
        return local
    vigente = _sql_material_vigente()
    destino = _sql_centro_destino()  # almacén de traslado (A200) -> planta de destino
    materiales = _con_codigos_anteriores({m for m, _ in AJUSTES_POR_CONTRATO})
    recibido = _query(f"""
        SELECT {vigente} AS material_codigo, {destino} AS centro_destino, contrato_marco AS contrato,
               substr(fecha_contabilizacion, 1, 7) || '-01' AS mes_recibido, SUM(cantidad) AS toneladas
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({",".join(_texto_sql(m) for m in materiales)})
              AND contrato_marco IN ({",".join(_texto_sql(c) for c in set(AJUSTES_POR_CONTRATO.values()))})
              AND +clase_movimiento IN {_CLASES_ENTRADA_COMPRA} AND proveedor <> '' AND fecha_contabilizacion IS NOT NULL
        GROUP BY {vigente}, {destino}, contrato_marco, mes_recibido
    """)
    recibido = recibido[recibido["toneladas"] > 0].assign(mes_recibido=lambda r: pd.to_datetime(r["mes_recibido"]).dt.date)
    recibido["parte"] = recibido["toneladas"] / recibido.groupby(["material_codigo", "centro_destino", "contrato"])["toneladas"].transform("sum")
    partes = local[regla].assign(contrato=[AJUSTES_POR_CONTRATO[(m, mes)] for m, mes in
                                           zip(local.loc[regla, "material_codigo"], local.loc[regla, "mes"])])
    partes = partes.merge(recibido[["material_codigo", "centro_destino", "contrato", "mes_recibido", "parte"]],
                          on=["material_codigo", "centro_destino", "contrato"])
    if partes.empty:  # sin recepciones de ese contrato: el ajuste queda como estaba
        return local
    for c in ("ajuste", "ajuste_ordenes"):
        partes[c] = partes[c] * partes["parte"]
    partes["mes"] = partes["mes_recibido"]
    return pd.concat([local[~regla], partes[local.columns]], ignore_index=True)


@_cacheado
def precios_entrada_sop_real():
    """precios_entrada_sop con el precio real (ver con_precio_real)."""
    return con_precio_real(precios_entrada_sop())


# Categorías fuera del IPU (pedido del usuario, 2026-10-28): Servicios
# Administrativos solo tenía equipos de oficina y TI con código, no sus
# servicios, y se quitó de todos los IPU (Overview, su página, resumen).
CATEGORIAS_SIN_IPU = ("SERVICIOS ADMINISTRATIVOS",)


@_cacheado
def precios_entrada_materiales_real():
    """precios_entrada_materiales (todos los materiales, pestañas de cada
    categoría y resumen ejecutivo) con el precio real. Desde 2026-10-14 todo
    el IPU usa el precio real (pedido del usuario). Sin CATEGORIAS_SIN_IPU."""
    df = con_precio_real(precios_entrada_materiales())
    return df[~df["categoria"].isin(CATEGORIAS_SIN_IPU)].reset_index(drop=True)


def ajustes_fbl3n_detalle(materiales, desde=None, hasta=None):
    """Las partidas de la FBL3N de `materiales` (todas sus plantas) entre dos
    meses, con su concepto, para ver de qué trata cada importe. Incluye las
    del código antiguo que van a alguno de ellos (AJUSTES_CODIGOS_ANTERIORES),
    con ese código al inicio del texto, como en el precio."""
    if not materiales:
        return pd.DataFrame()
    anteriores = [a for a, n in AJUSTES_CODIGOS_ANTERIORES.items() if n in set(materiales)]
    filtros = [f"material_codigo IN ({','.join(_texto_sql(m) for m in sorted(set(materiales) | set(anteriores)))})"]
    if desde:
        filtros.append(f"fecha_contabilizacion >= '{desde.isoformat()}'")
    if hasta:
        filtros.append(f"fecha_contabilizacion < '{_sumar_meses(hasta, 1).isoformat()}'")
    df = _query_directo(f"""
        SELECT fecha_contabilizacion, material_codigo, centro, numero_documento, clase_documento,
               codigo_transaccion, importe, moneda_documento, importe_moneda_documento, cantidad,
               unidad_medida_base, documento_compras, referencia, texto, usuario, cuenta_mayor
        FROM gestion_ajustepreciofbl3n WHERE {' AND '.join(filtros)}
        ORDER BY fecha_contabilizacion DESC, numero_documento
    """)
    if not df.empty:
        df["concepto"] = [concepto_ajuste(c, t, x) for c, t, x in
                          zip(df["clase_documento"], df["codigo_transaccion"], df["texto"])]
        df["fecha_contabilizacion"] = pd.to_datetime(df["fecha_contabilizacion"]).dt.date
        antiguo = df["material_codigo"].isin(anteriores)
        df.loc[antiguo, "texto"] = ["[" + m + "] " + (t or "") for m, t in zip(df.loc[antiguo, "material_codigo"], df.loc[antiguo, "texto"])]
        excluidos = set(AJUSTES_EXCLUIDOS)
        df["en_precio"] = ~df["clase_documento"].isin(CLASES_LIQUIDACION_ORDENES) & pd.Series(
            [(m, str(d)) not in excluidos for m, d in zip(df["material_codigo"], df["numero_documento"])], index=df.index)
        # Importados por embarque: a qué barco se asignó cada partida.
        barcos, _ = embarques_importados()
        barcos = barcos[barcos["material_codigo"].isin(materiales)]
        if not barcos.empty:
            df["embarque"], df["criterio_embarque"] = _embarque_de_partidas(
                df.rename(columns={"fecha_contabilizacion": "fecha"}), barcos)
    return df


def resumen_precios_entrada(df):
    """Una fila por material/centro de `df` (salida de precios_entrada_sop,
    ya filtrada): último precio y su mes, variación contra la compra anterior
    y contra el mismo mes del año anterior (o la compra más cercana antes de
    esa fecha), precio mínimo/máximo/promedio ponderado del período y
    toneladas compradas."""
    filas = []
    for (material, centro), g in df.groupby(["material_codigo", "centro"]):
        g = g.sort_values("mes")
        ultimo = g.iloc[-1]
        anterior = g.iloc[-2] if len(g) > 1 else None
        hace_un_anio = g[g["mes"] <= _sumar_meses(ultimo["mes"], -12)]
        base_anual = hace_un_anio.iloc[-1] if not hace_un_anio.empty else None
        filas.append({
            "material_codigo": material, "texto_breve": ultimo["texto_breve"], "centro": centro,
            "categoria": ultimo["categoria"],
            "mes_ultimo": ultimo["mes"], "precio_ultimo": float(ultimo["precio"]),
            "mes_anterior": anterior["mes"] if anterior is not None else None,
            "variacion_anterior": (float(ultimo["precio"] / anterior["precio"] - 1) if anterior is not None else None),
            "mes_base_anual": base_anual["mes"] if base_anual is not None else None,
            "variacion_anual": (float(ultimo["precio"] / base_anual["precio"] - 1) if base_anual is not None else None),
            "precio_minimo": float(g["precio"].min()), "precio_maximo": float(g["precio"].max()),
            "precio_promedio": float(g["importe"].sum() / g["toneladas"].sum()),
            "toneladas": float(g["toneladas"].sum()), "importe": float(g["importe"].sum()),
            "meses_con_compra": int(len(g)),
            "serie": [(r.mes, float(r.precio)) for r in g.itertuples()],
        })
    return filas


# ---------------------------------------------------------------------------
# Inflación de precios (S&OP). Pedido por el usuario: precio de compra del
# mes T contra el de un mes de referencia — interanual: el mismo mes un año
# antes (T−12); mensual: el mes anterior (T−1). Si el mes de referencia no
# tuvo compra, contra el último precio registrado ANTES de esa fecha (T−13,
# T−14... / T−2, T−3...). Sin ninguna compra hasta ese mes, T no se compara.
# Interanual (regla del usuario, 2026-10-20): la base siempre es un precio
# del año anterior; si el último precio hasta T−12 es más antiguo, se usa el
# primero del año anterior después de T−12, y sin compra ese año no hay
# comparación (Puzolana Ica, Caliza HCIM y Puzolana Junín en 2026; Pelets
# en 2025).
# Bolsa = (precio T − precio base) × toneladas de T: el impacto en soles de
# la variación, ponderado siempre con la cantidad del período más reciente
# de la comparación (2025 vs 2024 con las toneladas de 2025, 2026 vs 2025
# con las de 2026).
# ---------------------------------------------------------------------------

# Una comparación con el precio más que duplicado o reducido a menos de la
# mitad no es inflación sino un dato anómalo de MB51 (p.ej. Ene-24: Sílices
# a S/ 16.60 y Puzolana de Terceros a S/ 17.50 por T, cuando su precio de
# todo el año es ~70 y ~130): se muestra marcada pero no suma en la bolsa ni
# en la selección ponderada.
_RAZON_ATIPICA = 2.0

# Precio de referencia (S/ por T) de los meses SIN compra de un material,
# definido por el usuario (2026-10-19; su referencia es el Excel "IPU Final
# 2025 P.2026 O1 ACTUALIZABLE V2", hoja MP Ext): solo sirve de base de comparación del
# IPU, no es una compra (no aparece en la evolución del precio ni suma
# toneladas). Se muestra en la matriz de precios TCO. (material, centro o
# None = todos, primer mes, último mes, precio).
PRECIOS_REFERENCIA_IPU = [
    ("07-00000002", None, datetime.date(2024, 1, 1), datetime.date(2025, 10, 1), 42.0),  # Arcilla Terceros, hasta su ingreso de nov-2025
    ("07-00001677", None, datetime.date(2024, 1, 1), datetime.date(2024, 12, 1), 69.0),  # Arcilla Ferrosa, meses de 2024 sin compra
    ("07-00001181", None, datetime.date(2024, 1, 1), datetime.date(2024, 3, 1), 103.5),  # Yeso Terceros ATO-NAC
    ("07-00000026", None, datetime.date(2024, 1, 1), datetime.date(2024, 1, 1), 55.5),  # Yeso Terceros CDC
    ("07-00000028", None, datetime.date(2024, 1, 1), datetime.date(2024, 1, 1), 76.0),  # Óxido de Fierro Terceros
    # Puzolana Ayacucho, Condorcocha: hasta may-25 (su primera compra de 2025 es
    # jun-25), como la hoja "MP Ext" del Excel IPU del usuario (2026-10-20).
    ("07-00000027", "U002", datetime.date(2024, 1, 1), datetime.date(2025, 5, 1), 93.0),
    ("07-00000027", "U001", datetime.date(2024, 1, 1), datetime.date(2025, 8, 1), 103.6),  # Puzolana Ayacucho, Atocongo
]


# Precio de referencia tomado de otro centro (regla del usuario, 2026-10-25):
# hasta 2025 el Carbón Importado iba de U004 directo a Condorcocha y Atocongo
# no lo recibía (después Atocongo pasó a ser centro de acopio, ver
# importados.ACOPIO_IMPORTADOS). En los meses sin compra en Atocongo su
# referencia es el precio de Condorcocha de ese mes menos el flete
# Atocongo–Condorcocha: como si se hubiera recibido en Atocongo. Igual que
# PRECIOS_REFERENCIA_IPU: solo base del IPU y precio de la matriz TCO.
# Desde el 2026-10-26 el precio de Condorcocha es puesto en planta (TCO: con
# el flete que SAP suma al recibir, ver importados._lotes_por_embarque), así
# que se le resta el flete real de lo que llegó ese mes; el valor fijo
# (datos_locales/reglas_locales.json, "flete_referencia") solo si ese mes no
# tiene traslados, y sin él el flete del último mes anterior con traslados.
# (material, centro sin compra) -> (centro del que se toma el precio, flete S/ por T si no hay dato).
REFERENCIA_DESDE_OTRO_CENTRO = {("06-00000003", "U001"): ("U002", regla_local("flete_referencia", {}).get("06-00000003|U001"))}


def _flete_llegadas(material, centro):
    """{mes: flete S/ por T} de lo que `centro` recibió por traslado cada mes
    (valor de llegada − valor de salida ÷ toneladas, de todos los orígenes)."""
    v = valores_traslado((material,))
    v = v[v["destino"] == centro].groupby("mes")[["toneladas", "valor_llega", "valor_sale"]].sum()
    v = v[v["toneladas"].abs() > 1e-6]
    return {datetime.date.fromisoformat(m): (r.valor_llega - r.valor_sale) / r.toneladas for m, r in v.iterrows()}


def referencias_desde_otro_centro(df=None):
    """Precios de REFERENCIA_DESDE_OTRO_CENTRO de cada mes en que el centro de
    origen tiene compra: material_codigo, centro, mes, precio (sin descontar
    todavía los meses con compra propia). Toma los precios de `df` si trae el
    centro de origen; si no (un filtro de centro o material), los del precio
    real del S&OP."""
    filas = []
    for (codigo, centro), (origen, flete_fijo) in REFERENCIA_DESDE_OTRO_CENTRO.items():
        fuente = df[(df["material_codigo"] == codigo) & (df["centro"] == origen)] if df is not None else df
        if fuente is None or fuente.empty:
            todo = precios_entrada_sop_real()
            fuente = todo[(todo["material_codigo"] == codigo) & (todo["centro"] == origen)]
        fletes = _flete_llegadas(codigo, origen)
        for r in fuente[fuente["toneladas"] > 0].itertuples():
            flete = fletes.get(r.mes, flete_fijo)
            if flete is None:
                anteriores = [m for m in fletes if m < r.mes]
                flete = fletes[max(anteriores)] if anteriores else 0.0
            filas.append({"material_codigo": codigo, "centro": centro, "mes": r.mes,
                          "precio": float(r.precio) - flete})
    return pd.DataFrame(filas, columns=["material_codigo", "centro", "mes", "precio"])


def bases_referencia_ipu(df):
    """Precios de referencia (PRECIOS_REFERENCIA_IPU y
    REFERENCIA_DESDE_OTRO_CENTRO) de los material/centro de `df` en los
    meses que `df` no tiene compra: material_codigo, centro, mes, precio."""
    filas = []
    con_compra = set(zip(df["material_codigo"], df["centro"], df["mes"]))
    pares = set(df[["material_codigo", "centro"]].drop_duplicates().itertuples(index=False, name=None))
    for material, centro in pares:
        for codigo, centro_regla, desde, hasta, precio in PRECIOS_REFERENCIA_IPU:
            if codigo != material or (centro_regla and centro_regla != centro):
                continue
            mes = desde
            while mes <= hasta:
                if (material, centro, mes) not in con_compra:
                    filas.append({"material_codigo": material, "centro": centro, "mes": mes, "precio": precio})
                mes = _sumar_meses(mes, 1)
    if pares & set(REFERENCIA_DESDE_OTRO_CENTRO):
        ya = {(f["material_codigo"], f["centro"], f["mes"]) for f in filas}
        for r in referencias_desde_otro_centro(df).itertuples(index=False):
            clave = (r.material_codigo, r.centro, r.mes)
            if (r.material_codigo, r.centro) in pares and clave not in con_compra and clave not in ya:
                filas.append(r._asdict())
    return pd.DataFrame(filas, columns=["material_codigo", "centro", "mes", "precio"])


def inflacion_precios(df, desfase=12):
    """Una fila por material/centro/mes con compra de `df` (salida de
    precios_entrada_sop, ya filtrada) que tenga base de comparación: precio,
    toneladas e importe del mes, mes_referencia (T − `desfase` meses),
    mes_base y precio_base usados, base_sustituida (True si el mes de
    referencia no tuvo compra y se tomó el último precio anterior),
    base_referencia (True si la base es un precio de referencia del usuario,
    PRECIOS_REFERENCIA_IPU), variacion (precio ÷ precio_base − 1), bolsa (S/)
    y atipica (ver _RAZON_ATIPICA)."""
    columnas = ["material_codigo", "texto_breve", "centro", "categoria", "mes", "precio", "toneladas", "importe",
                "mes_referencia", "mes_base", "precio_base", "base_sustituida", "base_referencia",
                "variacion", "bolsa", "atipica"]
    if df.empty:
        return pd.DataFrame(columns=columnas)
    actual = df.assign(mes_referencia=[_sumar_meses(m, -desfase) for m in df["mes"]])
    actual["_clave"] = pd.to_datetime(actual["mes_referencia"])
    bases = df[["material_codigo", "centro", "mes", "precio"]].assign(base_referencia=False)
    referencia = bases_referencia_ipu(df)
    if not referencia.empty:
        referencia = referencia.astype({"material_codigo": df["material_codigo"].dtype, "centro": df["centro"].dtype})
        bases = pd.concat([bases, referencia.assign(base_referencia=True)], ignore_index=True)
    bases = bases.rename(columns={"mes": "mes_base", "precio": "precio_base"})
    bases["_clave"] = pd.to_datetime(bases["mes_base"])
    # merge_asof "backward": el último mes_base <= mes_referencia del mismo
    # material y centro (exacto si T−12 tuvo compra).
    unido = pd.merge_asof(actual.sort_values("_clave"), bases.sort_values("_clave"), on="_clave",
                          by=["material_codigo", "centro"], direction="backward")
    if desfase == 12:
        # Regla del usuario (2026-10-20): el IPU de un año compara contra
        # precios del año anterior; un material sin compra (ni precio de
        # referencia) ese año no tiene comparación. Si el último precio hasta
        # T−12 es de antes del año anterior (o no hay), la base es el primer
        # precio del año anterior después de T−12 (Pelets: ene-26 contra
        # may-25, su primera compra); si tampoco hay, no se compara.
        anio_previo = [m.year - 1 for m in unido["mes"]]
        fuera = [b is None or pd.isna(b) or b.year != a for b, a in zip(unido["mes_base"], anio_previo)]
        fuera = pd.Series(fuera, index=unido.index)
        if fuera.any():
            adelante = pd.merge_asof(unido.loc[fuera, actual.columns].sort_values("_clave"), bases.sort_values("_clave"),
                                     on="_clave", by=["material_codigo", "centro"], direction="forward")
            adelante = adelante[[b is not None and not pd.isna(b) and b.year == m.year - 1
                                 for b, m in zip(adelante["mes_base"], adelante["mes"])]]
            unido = pd.concat([unido[~fuera], adelante], ignore_index=True)
    unido = unido[unido["precio_base"].notna()].copy()
    unido["base_sustituida"] = unido["mes_base"] != unido["mes_referencia"]
    unido["variacion"] = unido["precio"] / unido["precio_base"] - 1
    unido["bolsa"] = (unido["precio"] - unido["precio_base"]) * unido["toneladas"]
    razon = unido["precio"] / unido["precio_base"]
    unido["atipica"] = (razon > _RAZON_ATIPICA) | (razon < 1 / _RAZON_ATIPICA)
    return unido[columnas].sort_values(["material_codigo", "centro", "mes"]).reset_index(drop=True)


def inflacion_canasta(df):
    """Inflación ponderada de un conjunto de materiales/meses (`df`: salida
    de inflacion_precios): lo pagado ÷ lo que habrían costado esas mismas
    toneladas al precio base − 1 (ponderado por las toneladas compradas en
    cada mes T). Devuelve variacion, bolsa (S/: lo pagado − lo que habría
    costado al precio base), toneladas, importe y materiales. Las
    comparaciones atípicas no cuentan."""
    df = df[~df["atipica"]]
    base = float((df["precio_base"] * df["toneladas"]).sum())
    importe = float(df["importe"].sum())
    return {
        "variacion": importe / base - 1 if base else None, "bolsa": importe - base,
        "toneladas": float(df["toneladas"].sum()),
        "importe": importe, "materiales": int(df[["material_codigo", "centro"]].drop_duplicates().shape[0]),
    }


def ipu_por_mes(inflacion, desfase=12):
    """IPU (Índice de Precios Unacem), nombre definido por el usuario: por
    cada mes T con comparación de `inflacion` (salida de inflacion_precios,
    uno o varios materiales, SIN recortar por período: el acumulado arranca
    en enero), un dict mes -> {individual, acumulado, bolsa,
    bolsa_acumulada, importe_acumulado, base_acumulada, toneladas_acumuladas}.
    - individual: variación ponderada del mes (inflacion_canasta).
    - acumulado, interanual (desfase 12): enero..T de este año contra
      enero..T del anterior, ponderado con las toneladas de este año: lo
      pagado ÷ lo que habría costado al precio base − 1.
    - acumulado, mensual (desfase 1): variaciones del mes encadenadas desde
      enero, (1 + v1) × (1 + v2) × ... − 1 (inflación acumulada del año).
    Se reinicia cada enero; las comparaciones atípicas no cuentan."""
    resultado = {}
    validas = inflacion[~inflacion["atipica"]]
    for anio, g in validas.groupby([m.year for m in validas["mes"]]):
        factor, bolsa, importe, base, toneladas = 1.0, 0.0, 0.0, 0.0, 0.0
        for mes, gm in sorted(g.groupby("mes"), key=lambda x: x[0]):
            c = inflacion_canasta(gm)
            if c["variacion"] is None:
                continue
            bolsa += c["bolsa"]
            importe += c["importe"]
            base += c["importe"] - c["bolsa"]
            toneladas += c["toneladas"]
            factor *= 1 + c["variacion"]
            resultado[mes] = {
                "individual": c["variacion"], "bolsa": c["bolsa"],
                "acumulado": (importe / base - 1 if base else 0.0) if desfase == 12 else factor - 1,
                "bolsa_acumulada": bolsa, "importe_acumulado": importe, "base_acumulada": base,
                "toneladas_acumuladas": toneladas,
            }
    return resultado
