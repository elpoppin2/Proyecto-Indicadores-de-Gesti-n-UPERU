"""Inventario (MB5B) y consumo (MB51): valor por mes y categoría, variación, materiales sin movimiento, ABC, críticos, consumos y rotación."""

import calendar
import datetime

import pandas as pd

from .base import _cacheado, _query
from .reglas import CATEGORIAS_EXCLUIDAS_INVENTARIO, CATEGORIA_INVENTARIO_EFECTIVA, CATEGORIA_MATERIAL_EFECTIVA, CONSOLIDACION_CATEGORIAS_INVENTARIO, FAMILIA_MATERIAL_EFECTIVA, FILTRO_CATEGORIA_INVENTARIO_VALIDA, FILTRO_INVENTARIO_CON_CATEGORIA


@_cacheado
def catalogo_materiales():
    """Maestro de materiales (ZMM060) con su categoría y familia efectivas. Es la
    fuente única para clasificar materiales al cruzar datasets en pandas."""
    return _query(f"""
        SELECT m.codigo AS material_codigo,
               m.descripcion AS texto_breve,
               m.unidad_medida_base AS unidad_medida_base,
               m.centro AS centro,
               {CATEGORIA_MATERIAL_EFECTIVA} AS categoria,
               {FAMILIA_MATERIAL_EFECTIVA} AS familia
        FROM gestion_material m
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
    """)


@_cacheado
def inventario_por_material(fecha):
    """Stock y valor de cierre por material a una fecha, ya clasificado por categoría
    y familia. Es el dataset base del que se derivan (en pandas) el resumen, el top de
    materiales y el valor por familia, evitando repetir agregaciones pesadas."""
    inv = _query(f"""
        SELECT i.material_codigo AS material_codigo,
               SUM(i.stock_cierre) AS stock_cierre,
               SUM(i.valor_final) AS valor_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE i.fecha = %s AND {FILTRO_INVENTARIO_CON_CATEGORIA}
              AND {FILTRO_CATEGORIA_INVENTARIO_VALIDA}
        GROUP BY i.material_codigo
    """, [fecha])
    catalogo = catalogo_materiales()
    df = inv.merge(catalogo, on="material_codigo", how="left")
    df["texto_breve"] = df["texto_breve"].fillna("")
    df["unidad_medida_base"] = df["unidad_medida_base"].fillna("")
    df["centro"] = df["centro"].fillna("")
    return df


def fechas_inventario_disponibles():
    # Salta de fecha en fecha por el índice (MIN(fecha) > la anterior) en vez
    # de un DISTINCT que lee ~1.5 M filas: son ~40 fechas, así que son ~40
    # búsquedas puntuales. Con el disco en frío (primera apertura del día) el
    # DISTINCT tardaba ~2.6 s. La CTE devuelve texto, de ahí la conversión.
    df = _query("""
        WITH RECURSIVE f(fecha) AS (
            SELECT MIN(fecha) FROM gestion_inventariomensual
            UNION ALL
            SELECT (SELECT MIN(fecha) FROM gestion_inventariomensual WHERE fecha > f.fecha)
            FROM f WHERE f.fecha IS NOT NULL
        )
        SELECT fecha FROM f WHERE fecha IS NOT NULL
    """)
    return [datetime.date.fromisoformat(str(f)[:10]) for f in df["fecha"]]


def valor_inventario_mensual():
    return _query(f"""
        SELECT i.fecha AS fecha, SUM(i.valor_final) AS valor_total, SUM(i.stock_cierre) AS stock_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE {FILTRO_INVENTARIO_CON_CATEGORIA}
        GROUP BY i.fecha
        ORDER BY i.fecha
    """)


def valor_inventario_mensual_por_categoria(fecha_desde=None):
    """Serie mensual de Valor Final desagregada por categoría (misma clasificación
    que el segmentador de % Variación de Inventario: excepción -> MATERIAS PRIMAS,
    MROE/MROM -> REPUESTOS, CONSUMIBLES/SUMINISTROS -> SUMINISTROS INDUSTRIALES,
    NO APLICA y SERVICIOS ADMINISTRATIVOS excluidos).

    `fecha_desde` acota el rango leído: recorrer el histórico completo (1.26M filas)
    tarda ~20 s en SQLite, así que conviene pedir solo los años que se van a graficar."""
    filtro_fecha = "AND i.fecha >= %s" if fecha_desde else ""
    params = [fecha_desde] if fecha_desde else []
    return _query(f"""
        SELECT i.fecha AS fecha,
               {CATEGORIA_INVENTARIO_EFECTIVA} AS categoria,
               SUM(i.valor_final) AS valor_total
        FROM gestion_inventariomensual i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE {FILTRO_INVENTARIO_CON_CATEGORIA}
              AND {FILTRO_CATEGORIA_INVENTARIO_VALIDA}
              {filtro_fecha}
        GROUP BY i.fecha, {CATEGORIA_INVENTARIO_EFECTIVA}
        ORDER BY i.fecha
    """, params)


def categorias_inventario_disponibles():
    """Categorías disponibles para el segmentador de % Variación de Inventario. Usa
    CATEGORIA_INVENTARIO_EFECTIVA: los materiales de la excepción van a MATERIAS
    PRIMAS, CONSUMIBLES/SUMINISTROS se consolidan en SUMINISTROS INDUSTRIALES, y
    NO APLICA queda excluido.

    Se usa en las pestañas de TODAS las páginas: la categoría solo depende de
    (material, centro), así que se cruzan con el maestro los ~pares distintos
    en vez de las ~1.5 M líneas de inventario (11.8 s -> ~1 s en frío)."""
    df = _query(f"""
        SELECT DISTINCT {CATEGORIA_INVENTARIO_EFECTIVA} AS categoria
        FROM (SELECT DISTINCT material_codigo, area_valoracion FROM gestion_inventariomensual) i
        LEFT JOIN gestion_material m ON m.codigo = i.material_codigo
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        WHERE {FILTRO_INVENTARIO_CON_CATEGORIA}
              AND {FILTRO_CATEGORIA_INVENTARIO_VALIDA}
        ORDER BY categoria
    """)
    return df["categoria"].tolist()


@_cacheado
def variacion_inventario_por_material(fecha_inicial, fecha_final, categoria=None, familia=None, centro=None):
    """Variación de Valor Final y Stock de Cierre (MB5B) por código de material entre
    dos meses. fecha_final debe ser posterior a fecha_inicial. Respeta el mismo filtro
    de categoría que el resto del modelo de Inventario (FILTRO_INVENTARIO_CON_CATEGORIA),
    excluye NO APLICA y, si se indica `categoria`, filtra además por esa categoría
    (según CATEGORIA_INVENTARIO_EFECTIVA: excepción -> MATERIAS PRIMAS,
    CONSUMIBLES/SUMINISTROS consolidados en SUMINISTROS INDUSTRIALES). La descripción
    prioriza ZMM060 (gestion_material.descripcion) sobre el texto_breve de MB5B, que
    viene vacío en ~9% de las filas; la UMB también viene de ZMM060. `familia` filtra
    además por FAMILIA_INVENTARIO_EFECTIVA."""
    mes1 = inventario_por_material(fecha_inicial)
    mes2 = inventario_por_material(fecha_final)
    df = mes1.merge(mes2, on="material_codigo", how="outer", suffixes=("_ini", "_fin"))

    # Los datos descriptivos se toman del mes final y, si el material ya no existe ahí,
    # del inicial.
    for columna in ("texto_breve", "unidad_medida_base", "categoria", "familia", "centro"):
        df[columna] = df[f"{columna}_fin"].fillna(df[f"{columna}_ini"])

    df["stock_mes_inicial"] = df["stock_cierre_ini"].fillna(0.0).astype(float)
    df["stock_mes_final"] = df["stock_cierre_fin"].fillna(0.0).astype(float)
    df["variacion_stock"] = df["stock_mes_final"] - df["stock_mes_inicial"]
    df["valor_mes_inicial"] = df["valor_total_ini"].fillna(0.0).astype(float)
    df["valor_mes_final"] = df["valor_total_fin"].fillna(0.0).astype(float)
    df["variacion_valor"] = df["valor_mes_final"] - df["valor_mes_inicial"]
    df["variacion_pct"] = (
        df["variacion_valor"] / df["valor_mes_inicial"] * 100
    ).where(df["valor_mes_inicial"] != 0)

    if categoria:
        df = df[df["categoria"] == categoria]
    if familia:
        df = df[df["familia"] == familia]
    if centro:
        df = df[df["centro"] == centro]

    columnas = [
        "material_codigo", "texto_breve", "unidad_medida_base", "categoria", "familia", "centro",
        "stock_mes_inicial", "stock_mes_final", "variacion_stock",
        "valor_mes_inicial", "valor_mes_final", "variacion_valor", "variacion_pct",
    ]
    return df[columnas].sort_values("variacion_valor", ascending=False).reset_index(drop=True)


# "Materiales sin Movimiento" (antes "Obsoletos"): sin ningún consumo en los
# últimos 3 años, no 6 meses como el resto de indicadores de consumo — es un
# umbral mucho más exigente pensado para detectar capital realmente
# inmovilizado, no solo baja rotación reciente. Definido por el usuario.
MATERIALES_SIN_MOVIMIENTO_ANIOS = 3


@_cacheado
def materiales_obsoletos_por_categoria(fecha, familia=None, centro=None):
    """Materiales con stock de cierre > 0 pero sin ningún consumo (MB51,
    FILTRO_CLASE_MOVIMIENTO_CONSUMO) en los últimos MATERIALES_SIN_MOVIMIENTO_ANIOS
    años (3) que terminan en `fecha` — "Materiales sin Movimiento": capital
    inmovilizado por categoría. Se compara cantidad consumida con 0 (no
    cantidad contra cantidad de distinta UMB), así que no hay problema de
    unidades; el valor sí se puede sumar entre materiales sin problema.
    `familia`/`centro`: segmentadores propios de esta tabla (no afectan otros
    indicadores)."""
    fecha_inicio, fecha_fin = _ventana_anios(fecha, MATERIALES_SIN_MOVIMIENTO_ANIOS)
    inv = inventario_por_material(fecha)
    consumo = consumo_periodo_por_material(fecha_inicio, fecha_fin)
    df = inv.merge(consumo, on="material_codigo", how="left")
    df["consumo_total"] = df["consumo_total"].fillna(0.0)
    if familia:
        df = df[df["familia"] == familia]
    if centro:
        df = df[df["centro"] == centro]
    obsoletos = df[(df["valor_total"] > 0) & (df["consumo_total"] <= 0)]
    if obsoletos.empty:
        return []
    resumen = obsoletos.groupby("categoria", as_index=False).agg(
        materiales_obsoletos=("material_codigo", "count"),
        valor_obsoleto=("valor_total", "sum"),
    )
    return resumen.to_dict("records")


@_cacheado
def clasificacion_abc_por_categoria(fecha, familia=None, centro=None):
    """Clasificación ABC (Pareto de valor) de los materiales con stock de cada
    categoría: A = acumulan hasta 80% del valor de la categoría, B = hasta 95%,
    C = el resto. Ayuda a priorizar qué materiales vigilar de cerca.
    `familia`/`centro`: segmentadores propios de esta tabla (no afectan otros
    indicadores)."""
    inv = inventario_por_material(fecha)
    inv = inv[inv["valor_total"] > 0]
    if familia:
        inv = inv[inv["familia"] == familia]
    if centro:
        inv = inv[inv["centro"] == centro]
    resultado = []
    for categoria, grupo in inv.groupby("categoria"):
        grupo = grupo.sort_values("valor_total", ascending=False)
        total = grupo["valor_total"].sum()
        acumulado = grupo["valor_total"].cumsum() / total * 100
        clase = pd.cut(acumulado, bins=[-0.01, 80, 95, 100.01], labels=["A", "B", "C"])
        tabla = pd.DataFrame({"clase_abc": clase.values, "valor_total": grupo["valor_total"].values})
        resumen = (
            tabla.groupby("clase_abc", observed=True)
            .agg(materiales=("valor_total", "count"), valor=("valor_total", "sum"))
            .reindex(["A", "B", "C"]).fillna(0)
        )
        resultado.append({
            "categoria": categoria,
            "valor_total": float(total),
            "clases": [
                {"clase": c, "materiales": int(resumen.loc[c, "materiales"]), "valor": float(resumen.loc[c, "valor"])}
                for c in ["A", "B", "C"]
            ],
        })
    return resultado


@_cacheado
def cobertura_materiales_criticos_por_categoria(fecha, familia=None, centro=None):
    """Cobertura (meses) de los materiales marcados como críticos (ZMM060,
    campo 'Mat. Critico' = 'X'), por categoría, y cuántos están en riesgo
    (cobertura menor a 1 mes al ritmo de consumo actual). `familia`/`centro`:
    segmentadores propios de esta tabla (no afectan otros indicadores)."""
    criticos = _query("SELECT codigo AS material_codigo FROM gestion_material WHERE material_critico = 'X'")
    rot = rotacion_inventario_por_material(fecha)
    rot_criticos = rot[rot["material_codigo"].isin(set(criticos["material_codigo"]))].dropna(subset=["cobertura_meses"])
    if familia:
        rot_criticos = rot_criticos[rot_criticos["familia"] == familia]
    if centro:
        rot_criticos = rot_criticos[rot_criticos["centro"] == centro]
    if rot_criticos.empty:
        return []
    resultado = []
    for categoria, grupo in rot_criticos.groupby("categoria"):
        resultado.append({
            "categoria": categoria,
            "materiales_criticos": int(len(grupo)),
            "cobertura_promedio": round(float(grupo["cobertura_meses"].mean()), 2),
            "en_riesgo": int((grupo["cobertura_meses"] < 1).sum()),
        })
    return sorted(resultado, key=lambda r: r["en_riesgo"], reverse=True)


def resumen_materiales():
    df = _query("""
        SELECT
            (SELECT COUNT(*) FROM gestion_material) AS total_materiales,
            (SELECT COUNT(*) FROM gestion_material WHERE material_critico != '') AS criticos,
            (SELECT COUNT(*) FROM gestion_categoria) AS total_categorias
    """)
    return df.iloc[0]


def materiales_por_familia(top=20):
    return _query("""
        SELECT COALESCE(c.familia, 'Sin categoría') AS familia, COUNT(*) AS materiales
        FROM gestion_material m
        LEFT JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id
        GROUP BY familia
        ORDER BY materiales DESC
        LIMIT %s
    """, [top])


def materiales_por_fabricante(top=20):
    return _query("""
        SELECT fabricante, COUNT(*) AS materiales
        FROM gestion_material
        WHERE fabricante != ''
        GROUP BY fabricante
        ORDER BY materiales DESC
        LIMIT %s
    """, [top])


# Clases de movimiento de MB51 que cuentan como "consumo" en TODAS las pestañas
# (Rotación, Materiales sin Movimiento, Críticos, Planificación): Z01 (SM
# Vta.Relacionadas), Z04 (EM AnulTransf.Gratui), 353 (aún sin datos) y todas
# las que empiezan con "2", de la 200 a la 299 (201/202 a centro de costo,
# 221/222 a proyecto, 261/262 a orden, 281/282 a red...). Las anulaciones
# (202, 262...) vienen con el signo contrario y se restan solas. Definido por
# el usuario el 2026-10-04: antes solo Z01/Z04/353 fuera de Planificación, y
# la 261 (1.5 M movimientos, el grueso de los repuestos) no contaba.
CLASES_MOVIMIENTO_CONSUMO = ("Z01", "Z04", "353")
_IN_CLASES_MOVIMIENTO_CONSUMO = ",".join(f"'{c}'" for c in CLASES_MOVIMIENTO_CONSUMO)
# BETWEEN (no substr/LIKE) para que SQLite use el índice idx_mov_consumo_rotacion;
# todas las clases "2xx" cargadas son numéricas.
FILTRO_CLASE_MOVIMIENTO_CONSUMO = (
    f"(clase_movimiento BETWEEN '200' AND '299' OR clase_movimiento IN ({_IN_CLASES_MOVIMIENTO_CONSUMO}))"
)

# Para REPUESTOS (categorías origen MROE/MROM), los movimientos con Lote "USADO"
# son repuestos reacondicionados que reingresan a almacén, no consumo real: se
# excluyen de los indicadores de consumo. El resto de categorías sí cuenta todos
# sus lotes. Definido por el usuario.
_CATEGORIAS_ORIGEN_REPUESTOS = tuple(
    origen for origen, destino in CONSOLIDACION_CATEGORIAS_INVENTARIO.items() if destino == "REPUESTOS"
)
_IN_CATEGORIAS_ORIGEN_REPUESTOS = ",".join(f"'{c}'" for c in _CATEGORIAS_ORIGEN_REPUESTOS)
FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS = (
    "NOT (lote = 'USADO' AND material_codigo IN ("
    "SELECT m.codigo FROM gestion_material m"
    " INNER JOIN gestion_categoria c ON c.grupo_articulo = m.categoria_id"
    f" WHERE c.categoria IN ({_IN_CATEGORIAS_ORIGEN_REPUESTOS})))"
)


def _ventana_6_meses(fecha_objetivo):
    """Rango de fechas (inicio, fin) de los 6 meses calendario que terminan en el
    mes de `fecha_objetivo` (inclusive)."""
    anio, mes = fecha_objetivo.year, fecha_objetivo.month
    fecha_fin = datetime.date(anio, mes, calendar.monthrange(anio, mes)[1])
    total_meses = (anio * 12 + (mes - 1)) - 5
    inicio_anio, inicio_mes = divmod(total_meses, 12)
    fecha_inicio = datetime.date(inicio_anio, inicio_mes + 1, 1)
    return fecha_inicio, fecha_fin


def consumo_6_meses_por_material(fecha):
    """Consumo (MB51) por material en los 6 meses calendario que terminan en `fecha`.
    Solo lee columnas del índice de cobertura idx_mov_consumo_rotacion, por eso no
    trae descripción ni UMB (esas salen del maestro de materiales)."""
    fecha_inicio, fecha_fin = _ventana_6_meses(fecha)
    return _query(f"""
        SELECT material_codigo AS material_codigo,
               -SUM(cantidad) AS consumo_total
        FROM gestion_movimientomaterial
        WHERE {FILTRO_CLASE_MOVIMIENTO_CONSUMO}
              AND fecha_contabilizacion BETWEEN %s AND %s
              AND material_codigo != ''
              AND {FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS}
        GROUP BY material_codigo
    """, [fecha_inicio, fecha_fin])


def _ventana_anios(fecha_objetivo, anios):
    """Rango de fechas (inicio, fin) de los últimos `anios` años exactos (día a
    día, no por mes calendario como _ventana_6_meses) que terminan en
    `fecha_objetivo` (inclusive). Si esa historia todavía no está cargada (los
    datos de MB51 arrancan en 2024), la ventana simplemente no encuentra
    movimientos en la parte que falta — no hace falta ningún caso especial:
    en cuanto se cargue esa data (p.ej. el año 2023), el mismo cálculo la
    toma en cuenta sin cambiar el código."""
    try:
        fecha_inicio = fecha_objetivo.replace(year=fecha_objetivo.year - anios)
    except ValueError:
        # 29 de febrero en un año no bisiesto tres años atrás: cae al 28.
        fecha_inicio = fecha_objetivo.replace(year=fecha_objetivo.year - anios, day=28)
    return fecha_inicio, fecha_objetivo


def consumo_periodo_por_material(fecha_inicio, fecha_fin):
    """Consumo (MB51) por material entre `fecha_inicio` y `fecha_fin` (ambas
    inclusive). Generalización de consumo_6_meses_por_material para ventanas
    de cualquier tamaño (p.ej. los 3 años de Materiales sin Movimiento)."""
    return _query(f"""
        SELECT material_codigo AS material_codigo,
               -SUM(cantidad) AS consumo_total
        FROM gestion_movimientomaterial
        WHERE {FILTRO_CLASE_MOVIMIENTO_CONSUMO}
              AND fecha_contabilizacion BETWEEN %s AND %s
              AND material_codigo != ''
              AND {FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS}
        GROUP BY material_codigo
    """, [fecha_inicio, fecha_fin])


def consumo_valorizado_6_meses_por_material(fecha):
    """Igual que consumo_6_meses_por_material, pero en valor (S/., campo
    'Importe en moneda local' de MB51, mismo signo que cantidad: negativo en
    salidas H, positivo en sus anulativos S) en vez de cantidad. Sirve para
    agregar consumo entre materiales con distinta unidad de medida (p.ej. la
    rotación de inventario a nivel de categoría en el OVERVIEW, donde sumar
    cantidades de materiales en KG, UND, M, etc. no sería válido)."""
    fecha_inicio, fecha_fin = _ventana_6_meses(fecha)
    return _query(f"""
        SELECT material_codigo AS material_codigo,
               -SUM(importe_moneda_local) AS consumo_valor_6meses
        FROM gestion_movimientomaterial
        WHERE {FILTRO_CLASE_MOVIMIENTO_CONSUMO}
              AND fecha_contabilizacion BETWEEN %s AND %s
              AND material_codigo != ''
              AND {FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS}
        GROUP BY material_codigo
    """, [fecha_inicio, fecha_fin])


@_cacheado
def consumo_valorizado_mensual_por_categoria():
    """Consumo en valor (S/, mismas reglas que consumo_valorizado_6_meses_por_material)
    por categoría y mes: insumo del IRI de 12 meses (pestaña Indicadores de las
    categorías). Columnas: categoria, mes (date, día 1), consumo_valor."""
    df = _query(f"""
        SELECT material_codigo AS material_codigo, substr(fecha_contabilizacion, 1, 7) || '-01' AS mes,
               -SUM(importe_moneda_local) AS consumo_valor
        FROM gestion_movimientomaterial
        WHERE {FILTRO_CLASE_MOVIMIENTO_CONSUMO}
              AND fecha_contabilizacion IS NOT NULL
              AND material_codigo != ''
              AND {FILTRO_EXCLUYE_LOTE_USADO_REPUESTOS}
        GROUP BY material_codigo, mes
    """)
    if df.empty:
        return pd.DataFrame(columns=["categoria", "mes", "consumo_valor"])
    df = df.merge(catalogo_materiales()[["material_codigo", "categoria"]].drop_duplicates("material_codigo"),
                  on="material_codigo", how="left")
    df["mes"] = pd.to_datetime(df["mes"]).dt.date
    df["consumo_valor"] = df["consumo_valor"].astype(float)
    return df.groupby(["categoria", "mes"], as_index=False)["consumo_valor"].sum()


@_cacheado
def rotacion_inventario_por_material(fecha):
    """Índice de Rotación de Inventario por material (veces) = Consumo promedio (MB51,
    6 últimos meses calendario que terminan en `fecha`) ÷ Inventario de cierre (MB5B,
    a `fecha`); y su inverso, Cobertura (meses) = Inventario de cierre ÷ Consumo
    promedio. Ambos en cantidad (unidades), no en valor. Incluye la categoría/familia
    de cada material para poder filtrar sin repetir la consulta.

    El cruce inventario-consumo se hace en pandas: en SQL, unir ambas agregaciones
    como CTEs obligaba a SQLite a un join anidado sin índice (~20 s); así baja a ~1 s."""
    inv = inventario_por_material(fecha)
    cons = consumo_6_meses_por_material(fecha)
    catalogo = catalogo_materiales()

    # Materiales con consumo pero sin stock: se completan desde el maestro.
    cons = cons.merge(catalogo, on="material_codigo", how="left")
    df = inv.merge(cons[["material_codigo", "consumo_total"]], on="material_codigo", how="outer")

    solo_consumo = df["stock_cierre"].isna()
    if solo_consumo.any():
        faltantes = df.loc[solo_consumo, "material_codigo"]
        relleno = catalogo.set_index("material_codigo").reindex(faltantes)
        for columna in ("texto_breve", "unidad_medida_base", "centro", "categoria", "familia"):
            df.loc[solo_consumo, columna] = relleno[columna].values

    df["stock_cierre"] = df["stock_cierre"].fillna(0.0).astype(float)
    df["valor_total"] = df["valor_total"].fillna(0.0).astype(float)
    df["consumo_promedio"] = df["consumo_total"].fillna(0.0).astype(float) / 6.0
    df["indice_rotacion"] = (df["consumo_promedio"] / df["stock_cierre"]).where(df["stock_cierre"] != 0)
    df["cobertura_meses"] = (df["stock_cierre"] / df["consumo_promedio"]).where(df["consumo_promedio"] != 0)

    # Los materiales con consumo pero sin stock traen su categoría del maestro, que no
    # pasa por el filtro de inventario: se excluyen aquí las categorías descartadas.
    df = df[~df["categoria"].isin(CATEGORIAS_EXCLUIDAS_INVENTARIO)]

    columnas = [
        "material_codigo", "texto_breve", "unidad_medida_base", "centro", "categoria", "familia",
        "stock_cierre", "consumo_promedio", "indice_rotacion", "cobertura_meses",
    ]
    return df[columnas].sort_values("indice_rotacion", ascending=False, na_position="last").reset_index(drop=True)
