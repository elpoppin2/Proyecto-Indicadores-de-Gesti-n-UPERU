"""Planificación > Precios: precio de entrada por material, matriz TCO e IPU / inflación de precios."""

import datetime

from dashboard import queries as q

from .. import charts
from .comun import _mes_corto, _opciones_categoria_centro, _opciones_material, _por_materiales, _selector_material


# Período del gráfico de precios de entrada: meses hacia atrás desde el
# último mes con compras (None = toda la historia de MB51).
_PERIODOS_PRECIOS = {"12": ("Últimos 12 meses", 12), "24": ("Últimos 24 meses", 24), "todo": ("Toda la historia", None)}
_TIPOS_PRECIO = {"real": "Real (MB51 + ajustes FBL3N)", "mb51": "Solo MB51 (precio del pedido)"}
# Partidas de la FBL3N listadas con un material elegido (las más recientes).
_MAX_AJUSTES_LISTA = 300


def _contexto_precios_entrada(request):
    """Evolución de precios de entrada (S/ por T) de los materiales del S&OP,
    desde MB51 (ver queries.precios_entrada_sop). Filtros propios en cascada
    (Categoría/Centro/Material) y Período. Con varios materiales: una tarjeta
    por material con su minigráfico; con uno: gráfico grande con las
    toneladas compradas y su detalle mensual. Pedido por el usuario.
    "Precio": real (MB51 + ajustes de valor de la FBL3N, ver
    queries.con_precio_real; por defecto) o solo MB51."""
    base = q.precios_entrada_sop_real()
    if base.empty:
        return None
    tipo_sel = request.GET.get("tipo_precios", "real")
    if tipo_sel not in _TIPOS_PRECIO:
        tipo_sel = "real"
    historia = base  # la matriz TCO usa toda la historia (año base anterior al período)
    if tipo_sel == "mb51":
        base = base.assign(importe=base["importe_mb51"], precio=base["precio_mb51"])
    periodo_sel = request.GET.get("periodo_precios", "24")
    if periodo_sel not in _PERIODOS_PRECIOS:
        periodo_sel = "24"
    meses_atras = _PERIODOS_PRECIOS[periodo_sel][1]
    if meses_atras:
        desde = q._sumar_meses(max(base["mes"]), -(meses_atras - 1))
        base = base[base["mes"] >= desde]

    categorias_op, _ = _opciones_categoria_centro(base)
    categoria_sel = request.GET.get("categoria_precios", "Todas")
    if categoria_sel not in categorias_op:
        categoria_sel = "Todas"
    por_categoria = base if categoria_sel == "Todas" else base[base["categoria"] == categoria_sel]
    _, centros_op = _opciones_categoria_centro(por_categoria)
    centro_sel = request.GET.get("centro_precios", "Todos")
    if centro_sel not in centros_op:
        centro_sel = "Todos"
    por_centro = por_categoria if centro_sel == "Todos" else por_categoria[por_categoria["centro"] == centro_sel]
    materiales_op = _opciones_material(por_centro)
    material_sel = _selector_material(request, "precios", materiales_op)
    df = _por_materiales(por_centro, material_sel)

    tarjetas = q.resumen_precios_entrada(df)
    umb = dict(zip(base["material_codigo"], base["unidad_medida_base"]))
    for t in tarjetas:
        t["unidad"] = umb.get(t["material_codigo"]) or "T"  # el Petróleo Diesel, en galones
        t["mes_ultimo_corto"] = _mes_corto(t["mes_ultimo"])
        t["mes_base_anual_corto"] = _mes_corto(t["mes_base_anual"]) if t["mes_base_anual"] else ""
        t["variacion_anual_pct"] = None if t["variacion_anual"] is None else t["variacion_anual"] * 100
        t["variacion_anterior_pct"] = None if t["variacion_anterior"] is None else t["variacion_anterior"] * 100
        t["minigrafico"] = charts.minigrafico_precio(t["serie"])
        t["importado"] = t["centro"] == q.CENTRO_IMPORTACION
    # Primero los que más subieron en el año (lo que más le importa a
    # gerencia); los que no tienen base anual, al final.
    tarjetas.sort(key=lambda t: (t["variacion_anual"] is None, -(t["variacion_anual"] or 0)))

    detalle, grafico, ajustes, ajustes_resumen, embarques = [], None, [], [], []
    if len(tarjetas) == 1:
        g = df.sort_values("mes")
        anterior = None
        # Importados por embarque (queries.embarques_importados): de qué
        # barcos salió lo recibido cada mes y la composición de cada barco.
        material_uno = tarjetas[0]["material_codigo"]
        reparto = q.reparto_por_embarque(para_precio=True)
        reparto = reparto[reparto["material_codigo"] == material_uno]
        de_barco = {(r.centro, datetime.date.fromisoformat(r.mes)): r for r in reparto.itertuples()}
        barcos, _ = q.embarques_importados()
        for b in barcos[barcos["material_codigo"] == material_uno].itertuples():
            # Precios por tonelada seca (menos los ajustes por humedad del barco).
            secas = b.toneladas + b.humedad
            embarques.append({
                "embarque": b.embarque, "pedido": b.pedido, "llegada": b.llegada, "toneladas": b.toneladas,
                "humedad": b.humedad, "secas": secas,
                "importe": b.importe, "ajuste": b.ajuste, "partidas": b.partidas,
                "precio_mb51": b.importe / secas, "precio_real": (b.importe + b.ajuste) / secas,
                "ajuste_por_t": b.ajuste / secas,
            })
        for r in g.itertuples():
            barco = de_barco.get((r.centro, r.mes))
            detalle.append({
                "mes": r.mes, "toneladas": r.toneladas, "importe": r.importe, "precio": r.precio,
                # Céntimos sueltos (líneas de flete en 0,00/−0,02) no son un ajuste.
                "importe_mb51": r.importe_mb51, "precio_mb51": r.precio_mb51,
                "ajuste": r.ajuste if abs(r.ajuste) >= 1 else 0.0,
                "ajuste_trasladado": r.ajuste_trasladado if abs(r.ajuste_trasladado) >= 1 else 0.0,
                "ajuste_ordenes": r.ajuste_ordenes if abs(r.ajuste_ordenes) >= 1 else 0.0,
                "precio_real": (r.importe_mb51 + r.ajuste) / r.toneladas, "ajuste_por_t": r.ajuste / r.toneladas,
                # Mismo criterio que la inflación (q._RAZON_ATIPICA): el ajuste
                # más que duplica el precio o lo baja a menos de la mitad.
                "atipico": (r.material_codigo, r.centro) not in q.PRODUCCION_PROPIA and not (
                    1 / q._RAZON_ATIPICA <= (r.importe_mb51 + r.ajuste) / r.importe_mb51 <= q._RAZON_ATIPICA),
                "con_fbl3n": r.con_fbl3n,
                "embarques": barco.embarques if barco is not None else "",
                # Restos chicos (stock de inicio de 2024, redondeos) no se marcan.
                "sin_embarque": barco.sin_embarque if barco is not None and barco.sin_embarque > 0.02 * r.toneladas else 0.0,
                "entradas": r.entradas, "proveedores": r.proveedores,
                "variacion_pct": None if anterior is None else (r.precio / anterior - 1) * 100,
            })
            anterior = r.precio
        grafico = charts.evolucion_precio(
            [(r.mes, float(r.precio), float(r.toneladas)) for r in g.itertuples()], _mes_corto,
        )
        detalle.reverse()  # lo más reciente arriba
        lista = q.ajustes_fbl3n_detalle([tarjetas[0]["material_codigo"]], min(g["mes"]), max(g["mes"]))
        if not lista.empty:
            ajustes = lista.head(_MAX_AJUSTES_LISTA).to_dict("records")
            ajustes_resumen = (lista.groupby(["concepto", "en_precio"])
                               .agg(lineas=("importe", "size"), importe=("importe", "sum"))
                               .reset_index().sort_values("importe", key=abs, ascending=False).to_dict("records"))

    meses_fbl3n = q.meses_con_fbl3n()
    meses_vista = set(df["mes"]) if not df.empty else set()
    sin_fbl3n = sorted(m for m in meses_vista if m not in meses_fbl3n)
    return {
        "matriz_tco": _matriz_tco(historia, df),
        "tipos": [{"valor": k, "texto": v} for k, v in _TIPOS_PRECIO.items()], "tipo_sel": tipo_sel,
        "es_real": tipo_sel == "real",
        "meses_sin_fbl3n": sin_fbl3n, "tramos_sin_fbl3n": _tramos_de_meses(sin_fbl3n),
        "fbl3n_desde": min(meses_fbl3n) if meses_fbl3n else None,
        "fbl3n_hasta": max(meses_fbl3n) if meses_fbl3n else None,
        "ajustes": ajustes, "ajustes_resumen": ajustes_resumen, "embarques": embarques,
        "ajustes_total": len(lista) if len(tarjetas) == 1 else 0,
        "max_ajustes": _MAX_AJUSTES_LISTA,
        "periodos": [{"valor": k, "texto": v[0]} for k, v in _PERIODOS_PRECIOS.items()], "periodo_sel": periodo_sel,
        "categorias": categorias_op, "categoria_sel": categoria_sel,
        "centros": centros_op, "centro_sel": centro_sel, "centro_importacion": q.CENTRO_IMPORTACION,
        "materiales": materiales_op, "material_sel": material_sel,
        "tarjetas": tarjetas, "un_material": len(tarjetas) == 1,
        "grafico": grafico, "detalle": detalle,
        "desde": min(df["mes"]) if not df.empty else None, "hasta": max(df["mes"]) if not df.empty else None,
    }


_CENTRO_CORTO = {"U001": "AT", "U002": "CC"}
# Primer año de la matriz de precios TCO (pedido del usuario: desde 2024).
_ANIO_INICIO_TCO = 2024


def _ipu_por_anio(historia, anios):
    """IPU interanual acumulado de cada año de `anios` para las filas de
    `historia` (precio real, toda la historia: la base de enero sale del año
    anterior): {año: variación} con el acumulado del último mes comparado del
    año (q.ipu_por_mes). Un año sin comparaciones no aparece."""
    inflacion = q.inflacion_precios(historia, 12)
    if inflacion.empty:
        return {}
    ipu = q.ipu_por_mes(inflacion, 12)
    resultado = {}
    for anio in anios:
        meses = [m for m in ipu if m.year == anio]
        if meses:
            resultado[anio] = ipu[max(meses)]["acumulado"] * 100
    return resultado


def _matriz_tco(historia, df):
    """Detalle de precios TCO (precio real: MB51 + ajustes FBL3N, importados
    puestos en planta) en el formato que pidió el usuario: una fila por
    material y centro de lo filtrado y, de izquierda a derecha, el precio de
    cada mes desde enero de _ANIO_INICIO_TCO hasta el último (p.ej. Ene-24 …
    Oct-26), con el IPU acumulado de cada año al cierre de sus meses; abajo,
    el IPU de todo lo filtrado (pedido del usuario). Mes sin compra = el
    último precio, marcado como repetido. `historia` sin recortar por período (el IPU de enero compara
    contra el año anterior)."""
    if df.empty:
        return None
    pares = df[["material_codigo", "centro"]].drop_duplicates()
    h = historia.merge(pares, on=["material_codigo", "centro"])
    h = h.assign(importe_tco=h["importe_mb51"] + h["ajuste"])
    ultimo = max(h["mes"])
    anios = list(range(min(_ANIO_INICIO_TCO, ultimo.year), ultimo.year + 1))
    meses_de = {a: [datetime.date(a, mes, 1) for mes in range(1, 13 if a < ultimo.year else ultimo.month + 1)]
                for a in anios}
    columnas = []
    for a in anios:
        columnas += [{"tipo": "mes", "texto": _mes_corto(m), "mes": m} for m in meses_de[a]]
        columnas.append({"tipo": "ipu", "texto": f"IPU {a}", "anio": a})
    filas = []
    primer_mes = meses_de[anios[0]][0]
    # Importados por embarque: de qué barco salió lo recibido cada mes.
    reparto = q.reparto_por_embarque(para_precio=True)
    de_barco = {(r.material_codigo, r.centro, datetime.date.fromisoformat(r.mes)): r.embarques
                for r in reparto.itertuples() if r.embarques}
    # Lo que está en el acopio esperando su traslado (PARTE_ACOPIO_POR_BARCO): no cuenta todavía.
    por_trasladar = {(r.material_codigo, r.centro, datetime.date.fromisoformat(r.mes)): r.por_trasladar
                     for r in reparto.itertuples() if r.por_trasladar > 0.5}
    fuera_ipu = {}  # año -> materiales sin compra el año anterior (no entran a su IPU)
    for (material, centro), g in h.groupby(["material_codigo", "centro"]):
        por_mes = {r.mes: r for r in g.itertuples()}
        if not any(m in por_mes for a in anios for m in meses_de[a]):
            continue
        ipu = _ipu_por_anio(g, anios)
        # En producción propia la liquidación ES el costo (no un ajuste sobre
        # MB51): no se marca como atípica.
        propio = (material, centro) in q.PRODUCCION_PROPIA
        # Mes sin compra = se repite el último precio (pedido del usuario),
        # también el de antes de enero del año anterior. No cambia el IPU:
        # ese mes no tiene toneladas y la base ya es el último precio.
        previos = [m for m in por_mes if m < primer_mes]
        vigente = (max(previos), por_mes[max(previos)].importe_tco / por_mes[max(previos)].toneladas) if previos else None
        # Meses sin compra con precio de referencia del usuario (base del IPU,
        # queries.PRECIOS_REFERENCIA_IPU): se muestra ese precio.
        referencia = q.bases_referencia_ipu(g)
        referencia = dict(zip(referencia["mes"], referencia["precio"]))
        # Regla del usuario (2026-10-20): el IPU de un año solo cuenta los
        # materiales con compra (o precio de referencia) el año anterior; un
        # material que se compró por primera vez ese año no tiene contra qué
        # compararse y queda fuera (en la tabla, "Sin base" y en la nota).
        anios_con_precio = {m.year for m in por_mes} | {m.year for m in referencia}
        celdas = []
        for c in columnas:
            if c["tipo"] == "ipu":
                a = c["anio"]
                sin_base = (a not in ipu and a in anios_con_precio and a - 1 not in anios_con_precio
                            and a > anios[0])
                if sin_base:
                    fuera_ipu.setdefault(a, []).append(f"{g['texto_breve'].iloc[0].title()} ({_CENTRO_CORTO.get(centro, centro)})")
                celdas.append({"ipu": ipu.get(a), "sin_base": sin_base,
                               "titulo": f"Sin compra en {a - 1}: no entra al IPU {a}" if sin_base else ""})
                continue
            r = por_mes.get(c["mes"])
            if r is None and c["mes"] in referencia:
                vigente = (c["mes"], referencia[c["mes"]])
                celdas.append({"precio": vigente[1], "repetido": True,
                               "titulo": f"{c['texto']}: sin compra, precio de referencia S/ {vigente[1]:z,.2f} por T (base del IPU)"})
                continue
            if r is None:
                celdas.append({"precio": vigente[1], "repetido": True,
                               "titulo": f"{c['texto']}: sin compra, se repite el precio de {_mes_corto(vigente[0])}"}
                              if vigente else None)
                continue
            vigente = (c["mes"], r.importe_tco / r.toneladas)
            celdas.append({"precio": r.importe_tco / r.toneladas,
                           "atipico": not propio and not (
                               1 / q._RAZON_ATIPICA <= r.importe_tco / r.importe_mb51 <= q._RAZON_ATIPICA),
                           "titulo": f"{c['texto']}: {r.toneladas:z,.0f} T · "
                                     + (f"traslado S/ {r.importe_mb51:z,.0f} + liquidación de la orden y diferencias del centro de origen S/ {r.ajuste:z,.0f}"
                                        if propio else f"MB51 S/ {r.importe_mb51:z,.0f} + ajustes S/ {r.ajuste:z,.0f}")
                                     + f" = S/ {r.importe_tco:z,.0f}"
                                     + (f" · embarque {de_barco[(material, centro, c['mes'])]}"
                                        if (material, centro, c["mes"]) in de_barco else "")})
        # Volúmenes con que pesa cada mes en el IPU (pedido del usuario,
        # 2026-10-26: para verificar la separación entre plantas), con el
        # total de cada año en la columna de su IPU.
        volumenes = []
        for c in columnas:
            if c["tipo"] == "ipu":
                volumenes.append({"total": sum(r.toneladas for m, r in por_mes.items() if m.year == c["anio"]), "anio": True})
                continue
            r, clave = por_mes.get(c["mes"]), (material, centro, c["mes"])
            pendiente = por_trasladar.get(clave, 0.0)
            titulo = f"{c['texto']}: {r.toneladas:z,.0f} T" if r is not None else ""
            if clave in de_barco:
                titulo += f" · embarque {de_barco[clave]}"
            if pendiente:
                titulo += (f" · además {pendiente:z,.0f} T del barco siguen en {q.nombre_centro(centro).title()} por "
                           f"trasladarse a la otra planta: cuentan cuando lleguen")
            volumenes.append({"t": r.toneladas if r is not None else None, "titulo": titulo, "pendiente": bool(pendiente)})
        filas.append({"material": material, "texto": g["texto_breve"].iloc[0],
                      "centro": _CENTRO_CORTO.get(centro, centro), "orden_centro": centro, "celdas": celdas,
                      "volumenes": volumenes})
    filas.sort(key=lambda f: (f["orden_centro"], f["material"]))
    total = _ipu_por_anio(h, anios)
    total_volumen = []
    for i, c in enumerate(columnas):
        valores = [f["volumenes"][i].get("total" if c["tipo"] == "ipu" else "t") for f in filas]
        total_volumen.append(sum(v for v in valores if v))
    return {"anios": anios, "columnas": columnas, "filas": filas, "total_volumen": total_volumen,
            "indices_ipu": [i for i, c in enumerate(columnas) if c["tipo"] == "ipu"],
            "hay_pendiente": any(v.get("pendiente") for f in filas for v in f["volumenes"]),
            "total": [total.get(c["anio"]) if c["tipo"] == "ipu" else "" for c in columnas],
            "fuera_ipu": [{"anio": a, "anio_base": a - 1, "materiales": ", ".join(sorted(m))}
                          for a, m in sorted(fuera_ipu.items())]}


def _tramos_de_meses(meses):
    """Meses ordenados -> texto con los tramos consecutivos ('Ene-25 a Dic-25,
    Oct-26')."""
    tramos = []
    for m in meses:
        if tramos and q._sumar_meses(tramos[-1][1], 1) == m:
            tramos[-1][1] = m
        else:
            tramos.append([m, m])
    return ", ".join(_mes_corto(a) if a == b else f"{_mes_corto(a)} a {_mes_corto(b)}" for a, b in tramos)


def _nota_base(r):
    """Aclaración de la base de una comparación de q.inflacion_precios (fila
    como dict), o "" si es exactamente el mes de referencia."""
    if r["base_referencia"]:
        return "precio de referencia, sin compra"
    if not r["base_sustituida"]:
        return ""
    sin = f"{_mes_corto(r['mes_referencia'])} sin compra"
    # Interanual: si no hubo precio del año anterior hasta T−12, la base es la
    # primera compra de ese año después de T−12 (queries.inflacion_precios).
    if r["mes_base"] > r["mes_referencia"]:
        return f"primera compra de {r['mes_base'].year}; {sin}"
    return f"último registrado; {sin}"


# Las dos evaluaciones de precios de Planificación (ver
# queries.inflacion_precios): interanual (T vs T−12) y mensual (T vs T−1).
# Cada una es una sección con sus propios filtros (sufijo de los parámetros
# GET). Pedido por el usuario.
_EVALUACIONES_PRECIOS = {
    "inflacion": {
        "desfase": 12, "id": "sec-inflacion", "titulo": "Inflación de precios (interanual)",
        "contra": "el mismo mes del año anterior", "ejemplo": "Ene-26 vs Ene-25",
        "sustituto": "Dic-24, Nov-24…", "base_titulo": "un año antes",
    },
    "mensual": {
        "desfase": 1, "id": "sec-inflacion-mensual", "titulo": "Evaluación mensual de precios",
        "contra": "el mes anterior", "ejemplo": "Mar-26 vs Feb-26",
        "sustituto": "Ene-26, Dic-25…", "base_titulo": "el mes anterior",
    },
}
# Por defecto se evalúa desde este año (2025 contra 2024 en adelante).
_ANIO_INICIO_INFLACION = 2025


def _periodos_inflacion(meses):
    """Opciones de Período según los meses T con comparación: desde 2025
    (por defecto), cada año, los últimos 12 meses y, si hay meses
    anteriores a 2025, toda la historia."""
    anios = sorted({m.year for m in meses if m.year >= _ANIO_INICIO_INFLACION})
    opciones = [("desde", f"{_ANIO_INICIO_INFLACION} en adelante")]
    opciones += [(str(a), f"Solo {a}") for a in anios]
    opciones.append(("12", "Últimos 12 meses"))
    if meses and min(meses).year < _ANIO_INICIO_INFLACION:
        opciones.append(("todo", "Toda la historia"))
    return opciones


def _filtrar_periodo_inflacion(inflacion, periodo):
    if periodo == "desde":
        return inflacion[inflacion["mes"] >= datetime.date(_ANIO_INICIO_INFLACION, 1, 1)]
    if periodo == "12":
        return inflacion[inflacion["mes"] >= q._sumar_meses(max(inflacion["mes"]), -11)]
    if periodo.isdigit():
        return inflacion[[m.year == int(periodo) for m in inflacion["mes"]]]
    return inflacion


def _contexto_inflacion(request, sufijo="inflacion"):
    """Evaluación de precios de compra (ver queries.inflacion_precios), en
    variación % o en bolsa (S/ = diferencia de precio × toneladas del mes
    evaluado). Filtros propios (Período/Categoría/Centro/Material/Ver). Una
    matriz material × mes con la bolsa por año, la selección ponderada mes a
    mes y, con un material, su detalle. Pedido por el usuario. Precio real
    (MB51 + FBL3N) por defecto desde que la FBL3N quedó completa (2024 a
    set-26); "Solo MB51" deja la evaluación como antes."""
    return _contexto_evaluacion_precios(request, q.precios_entrada_sop_real(), _EVALUACIONES_PRECIOS[sufijo], sufijo,
                                        unidad="auto")


_FAMILIAS_IPU_FILAS = 15


def _participacion_familias_ipu(inflacion_total, base, anio, meses):
    """Participación de cada familia en el IPU acumulado del año `anio` (el
    último del período, dict de por_anio) hasta su último mes con
    comparación: lo pagado y lo que habría costado al precio base de cada
    familia. Aporte (puntos) = bolsa de la familia ÷ base de todas × 100:
    los aportes suman el IPU del año. Pedido del usuario (2026-10-22)."""
    hasta = max(m for m in meses if m.year == anio["anio"])
    filas = inflacion_total[[m.year == anio["anio"] and m <= hasta for m in inflacion_total["mes"]]]
    filas = filas[~filas["atipica"]]
    if filas.empty:
        return None
    familia = dict(zip(base["material_codigo"], base["familia"]))
    filas = filas.assign(familia=[familia.get(m) or "Sin familia" for m in filas["material_codigo"]],
                         base_importe=filas["precio_base"] * filas["toneladas"],
                         clave=list(zip(filas["material_codigo"], filas["centro"])))
    g = filas.groupby("familia").agg(importe=("importe", "sum"), base=("base_importe", "sum"),
                                     materiales=("clave", "nunique")).reset_index()
    base_total, importe_total = float(g["base"].sum()), float(g["importe"].sum())
    if not base_total:
        return None
    g["bolsa"] = g["importe"] - g["base"]
    g["aporte"] = g["bolsa"] / base_total * 100
    g = g.reindex(g["aporte"].abs().sort_values(ascending=False).index)
    mayor = float(g["aporte"].abs().max()) or 1.0

    def fila(nombre, r_importe, r_base, materiales):
        bolsa, aporte = r_importe - r_base, (r_importe - r_base) / base_total * 100
        return {"familia": nombre, "materiales": int(materiales), "importe": r_importe, "base": r_base, "bolsa": bolsa,
                "pct": (r_importe / r_base - 1) * 100 if r_base else None, "aporte": aporte,
                "peso": r_importe / importe_total * 100 if importe_total else 0.0,
                "barra_sentido": "caro" if aporte > 0 else "barato" if aporte < 0 else "",
                "barra_pct": f"{min(abs(aporte) / mayor, 1) * 50:z.1f}"}

    primeras, resto = g.head(_FAMILIAS_IPU_FILAS), g.iloc[_FAMILIAS_IPU_FILAS:]
    resultado = [fila(r.familia, r.importe, r.base, r.materiales) for r in primeras.itertuples()]
    if not resto.empty:
        resultado.append({**fila(f"Otras {len(resto)} familias", float(resto["importe"].sum()), float(resto["base"].sum()),
                                 resto["materiales"].sum()), "resto": True})
    return {"anio": anio["anio"], "anio_base": anio["anio"] - 1, "hasta": _mes_corto(hasta), "ipu": anio["pct"],
            "bolsa": importe_total - base_total, "familias": int(len(g)), "filas": resultado}


def _contexto_evaluacion_precios(request, base, evaluacion, sufijo, campo="categoria", campo_titulo="Categoría",
                                 unidad="T", max_filas=None, por_familia=False):
    """Cuerpo de _contexto_inflacion para cualquier conjunto de compras
    (`base`, salida de precios_entrada_*_real): Planificación filtra por
    Categoría y mide en toneladas; la pestaña Precios de una categoría
    (pedido del usuario, 2026-10-22: el mismo esquema en Suministros
    Industriales) filtra por Familia, con unidades mixtas (`unidad`=None:
    "S/ por UMB" de cada material), muestra en la matriz solo los
    `max_filas` materiales de mayor impacto (|bolsa|; la Selección y el IPU
    siguen siendo de todos) y, con `por_familia`, la participación de cada
    familia en el IPU acumulado del año."""
    if base.empty:
        return None
    tipo_sel = request.GET.get(f"tipo_{sufijo}", "real")
    if tipo_sel not in _TIPOS_PRECIO:
        tipo_sel = "real"
    if tipo_sel == "mb51":
        base = base.assign(importe=base["importe_mb51"], precio=base["precio_mb51"])
    umb = dict(zip(base["material_codigo"], base["unidad_medida_base"])) if "unidad_medida_base" in base else {}

    def por_unidad(material=None):
        """"T", o la UMB del material (unidades mixtas)."""
        return unidad or umb.get(material, "UMB")

    def cantidad(valor, material=None):
        return f"{valor:z,.0f} {por_unidad(material)}"

    categorias_op = ["Todas"] + sorted(v for v in base[campo].dropna().unique().tolist() if v)
    categoria_sel = request.GET.get(f"{campo}_{sufijo}", "Todas")
    if categoria_sel not in categorias_op:
        categoria_sel = "Todas"
    por_categoria = base if categoria_sel == "Todas" else base[base[campo] == categoria_sel]
    _, centros_op = _opciones_categoria_centro(por_categoria)
    centro_sel = request.GET.get(f"centro_{sufijo}", "Todos")
    if centro_sel not in centros_op:
        centro_sel = "Todos"
    por_centro = por_categoria if centro_sel == "Todos" else por_categoria[por_categoria["centro"] == centro_sel]
    materiales_op = _opciones_material(por_centro)
    material_sel = _selector_material(request, sufijo, materiales_op)
    df = _por_materiales(por_centro, material_sel)
    if unidad == "auto":
        # Planificación: en toneladas si todo lo elegido se compra en T; con el
        # Combustible Líquido (diésel en galones), cada material en su unidad.
        unidad = "T" if {umb.get(m, "T") for m in df["material_codigo"].unique()} <= {"T"} else None
    ver_sel = request.GET.get(f"ver_{sufijo}", "pct")
    if ver_sel not in ("pct", "soles", "precio"):
        ver_sel = "pct"
    en_soles = ver_sel == "soles"
    en_precio = ver_sel == "precio"
    # Análisis de la tabla: cada mes por sí solo (individual) o el IPU
    # acumulado del año hasta ese mes. Pedido por el usuario.
    analisis_sel = request.GET.get(f"analisis_{sufijo}", "individual")
    if analisis_sel not in ("individual", "acumulado"):
        analisis_sel = "individual"
    acumulado = analisis_sel == "acumulado"

    # Sobre TODA la historia (la base de un mes puede ser anterior al período
    # mostrado, y el IPU acumulado arranca en enero aunque el período empiece
    # después); el período solo recorta los meses T que se ven.
    inflacion = q.inflacion_precios(df, evaluacion["desfase"])
    inflacion_total = inflacion
    periodos = _periodos_inflacion(sorted(inflacion["mes"].unique()) if not inflacion.empty else [])
    periodo_sel = request.GET.get(f"periodo_{sufijo}", "desde")
    if periodo_sel not in {v for v, _ in periodos}:
        periodo_sel = "desde"
    contexto = {
        **evaluacion, "sufijo": sufijo,
        "tipos": [{"valor": k, "texto": v} for k, v in _TIPOS_PRECIO.items()], "tipo_sel": tipo_sel,
        "es_real": tipo_sel == "real",
        "periodos": [{"valor": v, "texto": t} for v, t in periodos], "periodo_sel": periodo_sel,
        "categorias": categorias_op, "categoria_sel": categoria_sel,
        "filtro_campo": campo, "filtro_titulo": campo_titulo, "unidad": unidad or "UMB", "unidad_fija": bool(unidad),
        "centros": centros_op, "centro_sel": centro_sel,
        "materiales": materiales_op, "material_sel": material_sel,
        "ver": [{"valor": "pct", "texto": "Variación %"}, {"valor": "soles", "texto": "Bolsa (S/)"},
                {"valor": "precio", "texto": f"Precio (S/ por {unidad or 'UMB'})"}],
        "ver_sel": ver_sel, "en_soles": en_soles, "en_precio": en_precio,
        "analisis": [{"valor": "individual", "texto": "Individual (cada mes)"},
                     {"valor": "acumulado", "texto": "Acumulado del año (IPU)"}],
        "analisis_sel": analisis_sel, "acumulado": acumulado,
        "filas": [], "meses": [], "anios": [], "canasta": [], "ipu_fila": [], "grafico": None, "detalle": [],
        "resumen": None,
    }
    if not inflacion.empty:
        inflacion = _filtrar_periodo_inflacion(inflacion, periodo_sel)
    if inflacion.empty:
        return contexto
    inflacion = inflacion.assign(anio=[m.year for m in inflacion["mes"]])
    meses = sorted(inflacion["mes"].unique())
    anios = sorted(inflacion["anio"].unique())
    mayor_bolsa = float(inflacion.loc[~inflacion["atipica"], "bolsa"].abs().max() or 0) or 1.0

    def celda(pct, bolsa, extra, precio=None):
        valor = bolsa if en_soles else pct
        intensidad = abs(bolsa) / mayor_bolsa if en_soles else abs(pct) / 30
        # Ver precio: el precio del mes y, debajo, su variación (el color
        # sigue la variación). La fila Selección no tiene precio (sumar
        # precios de materiales distintos no tiene sentido): muestra el %.
        if en_precio and precio is not None:
            texto, subtexto = f"{precio:z,.2f}", f"{pct:+z.1f}%"
        else:
            texto, subtexto = (charts.soles_corto(bolsa) if en_soles else f"{pct:+z.1f}%"), ""
        return {
            "pct": pct, "bolsa": bolsa, "sube": valor > 0, "intensidad": f"{min(intensidad, 1):z.2f}",
            "texto": texto, "subtexto": subtexto, **extra,
        }

    def detalle_celda(r):
        base_txt = _mes_corto(r["mes_base"])
        nota = _nota_base(r)
        if nota:
            base_txt += f" ({nota})"
        signo = "+" if r["bolsa"] >= 0 else "−"
        u = por_unidad(r["material_codigo"])
        texto = (f"{r['texto_breve']} ({q.nombre_centro(r['centro'])}) · {_mes_corto(r['mes'])}: {r['variacion'] * 100:+z.1f}%\n"
                 f"{_mes_corto(r['mes'])}: S/ {r['precio']:z,.2f} por {u}\n"
                 f"vs {base_txt}: S/ {r['precio_base']:z,.2f} por {u}\n"
                 f"Bolsa: (S/ {r['precio']:z,.2f} − S/ {r['precio_base']:z,.2f}) × {cantidad(r['toneladas'], r['material_codigo'])} = "
                 f"{signo}S/ {abs(r['bolsa']):z,.0f}")
        if r["atipica"]:
            texto += "\nATÍPICA: el precio más que se duplica o cae a menos de la mitad (dato a revisar en MB51 o en los ajustes de la FBL3N); no suma en la bolsa ni en la selección."
        return texto

    def detalle_acumulado(mes, datos, ipu, nombre="", material=None):
        """Texto del recuadro de un valor acumulado (IPU) del mes `mes`. Con
        unidades mixtas, la cantidad solo va si es de un material."""
        inicio = min(m for m in ipu if m.year == mes.year)
        tramo = f"{_mes_corto(inicio)}–{_mes_corto(mes)}" if inicio != mes else _mes_corto(mes)
        signo = "+" if datos["bolsa_acumulada"] >= 0 else "−"
        if evaluacion["desfase"] == 12:
            texto = (f"{nombre}IPU acumulado {tramo} vs el mismo tramo del año anterior: {datos['acumulado'] * 100:+z.1f}%\n"
                     f"Pagado: S/ {datos['importe_acumulado']:z,.0f} · al precio del año anterior: "
                     f"S/ {datos['base_acumulada']:z,.0f}\n")
        else:
            texto = f"{nombre}IPU acumulado {tramo} (variaciones de cada mes encadenadas): {datos['acumulado'] * 100:+z.1f}%\n"
        texto += f"Bolsa acumulada: {signo}S/ {abs(datos['bolsa_acumulada']):z,.0f}"
        if unidad or material:
            texto += f" ({cantidad(datos['toneladas_acumuladas'], material)})"
        return texto

    # Con muchos materiales (una categoría: cientos), la matriz muestra los
    # max_filas de mayor impacto en soles; el resto entra igual en la
    # Selección, el IPU y los totales.
    grupos_inflacion = inflacion.groupby(["material_codigo", "centro"])
    total_materiales = grupos_inflacion.ngroups
    mostrar = None
    if max_filas and total_materiales > max_filas:
        impacto = inflacion[~inflacion["atipica"]].groupby(["material_codigo", "centro"])["bolsa"].sum().abs()
        mostrar = set(impacto.nlargest(max_filas).index)
    ultimas = [g["variacion"].iloc[-1] for _, g in grupos_inflacion]
    filas = []
    for (material, centro), g in grupos_inflacion:
        if mostrar is not None and (material, centro) not in mostrar:
            continue
        por_mes = {r["mes"]: r for r in g.to_dict("records")}
        ultimo = g.iloc[-1]
        if acumulado:
            ipu_material = q.ipu_por_mes(
                inflacion_total[(inflacion_total["material_codigo"] == material) & (inflacion_total["centro"] == centro)],
                evaluacion["desfase"],
            )
        celdas = []
        for m in meses:
            r = por_mes.get(m)
            if r is None:
                celdas.append(None)
            elif acumulado and m in ipu_material:
                d = ipu_material[m]
                celdas.append(celda(
                    d["acumulado"] * 100, d["bolsa_acumulada"],
                    {"sustituida": False, "atipica": False,
                     "detalle": detalle_acumulado(m, d, ipu_material, f"{r['texto_breve']} ({q.nombre_centro(centro)}) · ",
                                                  material)},
                    precio=d["importe_acumulado"] / d["toneladas_acumuladas"] if d["toneladas_acumuladas"] else None))
            else:
                celdas.append(celda(
                    r["variacion"] * 100, r["bolsa"],
                    {"sustituida": bool(r["base_sustituida"]), "atipica": bool(r["atipica"]), "detalle": detalle_celda(r)},
                    precio=r["precio"]))
        total = q.inflacion_canasta(g)
        validas = g[~g["atipica"]]
        filas.append({
            "material_codigo": material, "texto_breve": ultimo["texto_breve"], "centro": centro, "celdas": celdas,
            "ultima_pct": ultimo["variacion"] * 100, "ultimo_mes": _mes_corto(ultimo["mes"]),
            "periodo_pct": (total["variacion"] or 0) * 100, "bolsa": total["bolsa"],
            "bolsa_anios": [float(validas.loc[validas["anio"] == a, "bolsa"].sum()) if (g["anio"] == a).any() else None
                            for a in anios],
        })
    # Primero lo que más costó (bolsa más alta), al final lo que más ahorró.
    filas.sort(key=lambda f: -f["bolsa"])

    base_txt = "mismo mes del año anterior" if evaluacion["desfase"] == 12 else "mes anterior"
    ipu = q.ipu_por_mes(inflacion_total, evaluacion["desfase"])
    canasta, ipu_fila, puntos = [], [], []
    for m in meses:
        c = q.inflacion_canasta(inflacion[inflacion["mes"] == m])
        c["variacion"] = c["variacion"] or 0.0  # mes con solo comparaciones atípicas
        signo = "+" if c["bolsa"] >= 0 else "−"
        detalle = (f"{_mes_corto(m)} vs {base_txt}: {c['variacion'] * 100:+z.1f}%\n"
                   f"Bolsa: {signo}S/ {abs(c['bolsa']):z,.0f}\n"
                   f"{c['materiales']} material{'es' if c['materiales'] != 1 else ''} comprado{'s' if c['materiales'] != 1 else ''}"
                   + (f" · {c['toneladas']:z,.0f} {unidad}" if unidad else "") + f" · S/ {c['importe']:z,.0f}")
        canasta.append(celda(c["variacion"] * 100, c["bolsa"], {"detalle": detalle}))
        d = ipu.get(m)
        if d is None:
            ipu_fila.append(None)
            continue
        texto_acumulado = detalle_acumulado(m, d, ipu)
        ipu_fila.append(celda(d["acumulado"] * 100, d["bolsa_acumulada"], {"detalle": texto_acumulado}))
        puntos.append({
            "mes": m, "individual": d["bolsa"] if en_soles else d["individual"],
            "acumulado": d["bolsa_acumulada"] if en_soles else d["acumulado"],
            "detalle_individual": detalle, "detalle_acumulado": texto_acumulado,
        })

    # IPU de cada año: el acumulado al último mes de ese año dentro del
    # período (año completo = Ene–Dic).
    por_anio = []
    for a in anios:
        meses_anio = [m for m in meses if m.year == a and m in ipu]
        if not meses_anio:
            continue
        d = ipu[meses_anio[-1]]
        por_anio.append({"anio": a, "anio_base": a - 1 if evaluacion["desfase"] == 12 else None,
                         "hasta": _mes_corto(meses_anio[-1]), "anio_completo": meses_anio[-1].month == 12,
                         "pct": d["acumulado"] * 100, "bolsa": d["bolsa_acumulada"], "toneladas": d["toneladas_acumuladas"]})
    total = q.inflacion_canasta(inflacion)
    total["variacion"] = total["variacion"] or 0.0
    contexto.update({
        "meses": [_mes_corto(m) for m in meses], "meses_fecha": meses, "anios": anios, "filas": filas, "canasta": canasta,
        "ipu_fila": ipu_fila,
        "grafico": charts.evolucion_ipu(puntos, _mes_corto, formato=charts.soles_corto if en_soles else None),
        # Ver precio con UN material: evolución del precio del mes (línea) y
        # del precio base contra el que se compara (punteada).
        "grafico_precio": charts.evolucion_precio(
            [(r.mes, float(r.precio), float(r.toneladas)) for r in inflacion.itertuples()], _mes_corto,
            base=[(r.mes, float(r.precio_base)) for r in inflacion.itertuples()], ancho_minimo=960,
        ) if en_precio and total_materiales == 1 else None,
        "resumen": {
            "ultimo_mes": _mes_corto(meses[-1]), "ultima_pct": canasta[-1]["pct"], "ultima_bolsa": canasta[-1]["bolsa"],
            "periodo_pct": total["variacion"] * 100, "bolsa": total["bolsa"], "toneladas": total["toneladas"],
            "desde": _mes_corto(meses[0]), "hasta": _mes_corto(meses[-1]), "por_anio": por_anio,
            "suben": sum(1 for v in ultimas if v > 0), "bajan": sum(1 for v in ultimas if v < 0),
            "sustituidas": int(inflacion["base_sustituida"].sum()), "comparaciones": int(len(inflacion)),
            "atipicas": int(inflacion["atipica"].sum()),
        },
        # Bolsa de cada año DENTRO del período (columnas de la tabla).
        "bolsa_anios_total": [q.inflacion_canasta(inflacion[inflacion["anio"] == a])["bolsa"] for a in anios],
        "un_material": total_materiales == 1,
        "filas_total": total_materiales, "filas_recortadas": mostrar is not None,
    })
    if por_familia and por_anio:
        contexto["familias_ipu"] = _participacion_familias_ipu(inflacion_total, base, por_anio[-1], meses)
    if total_materiales == 1:
        contexto["detalle"] = [
            {**r, "mes_corto": _mes_corto(r["mes"]), "mes_base_corto": _mes_corto(r["mes_base"]),
             "mes_referencia_corto": _mes_corto(r["mes_referencia"]), "nota_base": _nota_base(r), "pct": r["variacion"] * 100}
            for r in reversed(inflacion.to_dict("records"))
        ]
    return contexto
