"""Helpers para armar gráficos a mano (SVG/CSS) para las páginas HTML del
dashboard, sin depender de una librería JS externa (la red corporativa puede
bloquear CDNs y esta es una herramienta interna).

Importante: todas las coordenadas se devuelven ya formateadas como texto
(con punto decimal). Django, con LANGUAGE_CODE='es', renderiza los números
de una plantilla con coma decimal (p. ej. "8,0"), lo que invalidaría los
atributos numéricos de un <svg>; por eso no se pasan floats crudos al template."""

import math


def serie_area(puntos, ancho=640, alto=220, pad_x=8, pad_y=24, formato_valor=None):
    """puntos: lista de (etiqueta, valor) ya ordenada cronológicamente.
    `formato_valor`: función valor -> texto para la etiqueta de cada punto;
    por defecto son millones de soles (uso original: Evolución de Inventario).
    Para otras unidades (p.ej. PMP en días) se pasa un formateador distinto."""
    if formato_valor is None:
        formato_valor = lambda v: f"{v / 1_000_000:,.1f}"  # noqa: E731
    if len(puntos) < 2:
        return None

    valores = [v for _, v in puntos]
    minimo, maximo = min(valores), max(valores)
    rango = (maximo - minimo) or 1
    paso = (ancho - 2 * pad_x) / (len(puntos) - 1)
    base_y = alto - pad_y

    crudos = []
    for i, (etiqueta, valor) in enumerate(puntos):
        x = pad_x + i * paso
        y = pad_y + (alto - 2 * pad_y) * (1 - (valor - minimo) / rango)
        crudos.append((x, y, etiqueta, valor))

    linea = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y, _, _) in enumerate(crudos))
    area = linea + f" L{crudos[-1][0]:.1f} {base_y:.1f} L{crudos[0][0]:.1f} {base_y:.1f} Z"

    # Etiquetas del eje: como mucho ~7 para que no se amontonen.
    paso_etiqueta = max(1, len(crudos) // 7)
    puntos_out = []
    for i, (x, y, etiqueta, valor) in enumerate(crudos):
        puntos_out.append({
            # Coordenadas en unidades del viewBox: para los <circle>/<path> del SVG.
            "x": f"{x:.1f}", "y": f"{y:.1f}",
            # Las mismas coordenadas como % del contenedor: para las etiquetas de
            # texto en HTML superpuesto (ver nota de módulo). Un <text> dentro del
            # SVG se escala junto con el gráfico y se ve más grande de lo previsto
            # cuando el SVG se estira más que su viewBox; en HTML normal, la
            # tipografía siempre respeta el tamaño real definido en el CSS.
            "x_pct": f"{x / ancho * 100:.2f}", "y_pct": f"{y / alto * 100:.2f}",
            "etiqueta": etiqueta,
            "etiqueta_valor": formato_valor(valor),
            "mostrar_etiqueta": (i % paso_etiqueta == 0) or (i == len(crudos) - 1),
        })

    return {
        "ancho": ancho, "alto": alto, "linea": linea, "area": area, "puntos": puntos_out,
    }


def curva_doble(puntos_a, puntos_b, ancho=640, alto=220, pad_x=8, pad_y=24, formato_valor=None):
    """Dos series sobre el mismo eje X (p.ej. Objetivo acumulado vs Real
    acumulado por día del mes) — curva S de cumplimiento. `puntos_a` es la
    serie completa (Objetivo: todos los días del mes, hasta fin de mes).
    `puntos_b` (Real) comparte el mismo eje X/escala que `puntos_a` pero puede
    ser más corta — se corta donde termine el dato real (hoy), sin seguir
    hasta fin de mes como el objetivo; debe tener como mínimo 1 punto y como
    máximo los mismos que `puntos_a`. A diferencia de serie_area, no dibuja
    área; y como serían ~30 puntos (uno por día), la etiqueta de valor de cada
    serie solo se muestra cada tantos puntos (mismo paso que la etiqueta de
    eje X) más el último punto de cada línea, no en los 30 — se amontonarían."""
    if formato_valor is None:
        formato_valor = lambda v: f"{v:,.0f}"  # noqa: E731
    if len(puntos_a) < 2 or not puntos_b or len(puntos_b) > len(puntos_a):
        return None

    valores = [v for _, v in puntos_a] + [v for _, v in puntos_b]
    minimo, maximo = min(0.0, min(valores)), max(valores)
    rango = (maximo - minimo) or 1
    paso = (ancho - 2 * pad_x) / (len(puntos_a) - 1)
    base_y = alto - pad_y

    def _coords(puntos):
        crudos = []
        for i, (_, valor) in enumerate(puntos):
            x = pad_x + i * paso
            y = base_y - (alto - 2 * pad_y) * ((valor - minimo) / rango)
            crudos.append((x, y))
        return crudos

    coords_a, coords_b = _coords(puntos_a), _coords(puntos_b)
    linea_a = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(coords_a))
    linea_b = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(coords_b))

    # Qué puntos llevan etiqueta de valor: un paso fijo (cada 1/6) dejaba dos
    # etiquetas de LA MISMA línea pegadas cuando un punto obligatorio (fin de
    # mes, corte del Real en "hoy") caía justo al lado de una marca periódica
    # — ej. día 21 (periódico) y día 22 (corte de hoy) uno al lado del otro.
    # Ahora se arma la lista con una distancia mínima en píxeles entre
    # cualquier par de etiquetas mostradas: se agregan primero los puntos
    # obligatorios (primer día, fin de mes, corte del Real) y luego los
    # periódicos, pero solo si no quedan pegados a uno ya elegido. Pedido por
    # el usuario.
    n = len(puntos_a)
    ultimo_b = len(puntos_b) - 1
    _MIN_GAP_PX = 58
    min_gap_idx = max(1, math.ceil(_MIN_GAP_PX / paso)) if paso else 1
    mostrar_idx = {0, n - 1, ultimo_b}
    for i in range(0, n, min_gap_idx):
        if all(abs(i - j) >= min_gap_idx for j in mostrar_idx):
            mostrar_idx.add(i)

    etiquetas = []
    for i, (etiqueta, valor_a) in enumerate(puntos_a):
        x = pad_x + i * paso
        xa, ya = coords_a[i]
        mostrar = i in mostrar_idx
        entrada = {
            "x_pct": f"{x / ancho * 100:.2f}",
            "etiqueta": etiqueta,
            "mostrar": mostrar,
            "a": {
                "x": f"{xa:.1f}", "y": f"{ya:.1f}",
                "x_pct": f"{xa / ancho * 100:.2f}", "y_pct": f"{ya / alto * 100:.2f}",
                "valor": formato_valor(valor_a),
                "pos": "arriba",
            },
            "b": None,
        }
        if i <= ultimo_b:
            xb, yb = coords_b[i]
            # Antes: "Objetivo" siempre arriba de SU punto y "Real" siempre
            # abajo del suyo, sin importar cuál línea va arriba en ese punto.
            # Cuando el Real superaba al Objetivo (su punto quedaba más arriba
            # en pantalla que el del Objetivo) las dos etiquetas terminaban
            # empujadas hacia la misma franja intermedia y chocaban. Ahora se
            # decide dinámicamente cuál punto está arriba en CADA columna y esa
            # etiqueta se separa hacia arriba, la del punto de abajo hacia
            # abajo — así siempre quedan a los dos lados, nunca superpuestas,
            # incluso cuando los dos puntos casi coinciden. Pedido por el usuario.
            entrada["b"] = {
                "x": f"{xb:.1f}", "y": f"{yb:.1f}",
                "x_pct": f"{xb / ancho * 100:.2f}", "y_pct": f"{yb / alto * 100:.2f}",
                "valor": formato_valor(puntos_b[i][1]),
                "pos": "abajo",
            }
            if yb < ya:
                entrada["a"]["pos"] = "abajo"
                entrada["b"]["pos"] = "arriba"
        etiquetas.append(entrada)

    ultimo_x_a, ultimo_y_a = coords_a[-1]
    ultimo_x_b, ultimo_y_b = coords_b[-1]

    return {
        "ancho": ancho, "alto": alto, "linea_a": linea_a, "linea_b": linea_b,
        "etiquetas": etiquetas,
        "ultimo_a": {"x": f"{ultimo_x_a:.1f}", "y": f"{ultimo_y_a:.1f}", "valor": formato_valor(puntos_a[-1][1])},
        "ultimo_b": {"x": f"{ultimo_x_b:.1f}", "y": f"{ultimo_y_b:.1f}", "valor": formato_valor(puntos_b[-1][1])},
    }


def barras_mensuales(puntos, ancho=640, alto=240, pad_x=10, pad_y=28, meta=100, formato_valor=None):
    """Barras verticales de una sola métrica por mes (pensado para % de
    Cumplimiento), con una línea de referencia horizontal en `meta`. `puntos`:
    lista de (etiqueta, valor) en orden cronológico — `valor` puede ser None
    (mes sin dato: no dibuja barra, solo deja el espacio y, si corresponde, la
    etiqueta de mes). Cada barra trae su propia clase CSS según quede por
    encima/cerca/por debajo de la meta (verde/ámbar/rojo), calculada acá
    porque una plantilla no puede hacer ese condicional con umbrales."""
    if formato_valor is None:
        formato_valor = lambda v: f"{v:.1f}%"  # noqa: E731
    if not puntos:
        return None

    valores = [v for _, v in puntos if v is not None]
    maximo = max([meta] + valores) * 1.15 if valores else meta * 1.15
    n = len(puntos)
    plot_ancho = ancho - 2 * pad_x
    plot_alto = alto - 2 * pad_y
    paso = plot_ancho / n
    ancho_barra = max(4.0, min(28.0, paso * 0.55))
    base_y = alto - pad_y
    y_meta = base_y - plot_alto * (meta / maximo)

    # Mismo criterio que curva_doble: con muchos meses, una etiqueta por barra
    # se amontona — se muestra cada tantas (según el ancho real de cada
    # columna) más siempre la última.
    paso_etiqueta = max(1, math.ceil(46 / paso))

    barras_out = []
    for i, (etiqueta, valor) in enumerate(puntos):
        cx = pad_x + paso * i + paso / 2
        entrada = {
            "x_pct": f"{cx / ancho * 100:.2f}",
            "etiqueta": etiqueta,
            "mostrar_etiqueta": (i % paso_etiqueta == 0) or (i == n - 1),
            "valor": None,
        }
        if valor is not None:
            alto_barra = max(2.0, plot_alto * (valor / maximo))
            y = base_y - alto_barra
            if valor >= meta:
                clase = "barra-buena"
            elif valor < 80:
                clase = "barra-mala"
            else:
                clase = "barra-regular"
            entrada["valor"] = {
                "x": f"{cx - ancho_barra / 2:.1f}", "y": f"{y:.1f}",
                "ancho": f"{ancho_barra:.1f}", "alto": f"{alto_barra:.1f}",
                "clase": clase,
                "x_pct": entrada["x_pct"], "y_pct": f"{y / alto * 100:.2f}",
                "etiqueta_valor": formato_valor(valor),
            }
        barras_out.append(entrada)

    return {
        "ancho": ancho, "alto": alto, "barras": barras_out,
        "y_meta": f"{y_meta:.1f}", "meta_texto": f"{meta:.0f}%",
    }


def soles_corto(valor):
    """Soles con signo y abreviados (+S/ 1.23M, −S/ 45K) para etiquetas."""
    return ("+" if valor > 0 else ("−" if valor < 0 else "")) + "S/ " + _toneladas_corto(abs(valor))


def evolucion_ipu(puntos, etiqueta_mes, formato=None, ancho=960, alto=300, pad_x=26, pad_y=30):
    """IPU (Índice de Precios Unacem) mes a mes: línea con el valor
    ACUMULADO del año (se corta en cada enero, porque el acumulado se
    reinicia) y un punto por mes con el valor INDIVIDUAL de ese mes (rojo si
    el precio subió, verde si bajó), sobre un eje de meses continuo (un mes
    sin compra queda como hueco) con la línea de 0. De cada mes, la
    etiqueta del valor más alto va arriba y la del más bajo abajo, para que
    no se pisen. Pedido por el usuario.
    `puntos`: lista ordenada de dicts con mes, individual, acumulado,
    detalle_individual y detalle_acumulado (texto del recuadro al pasar el
    mouse). `formato`: valor -> texto (por defecto, fracción como %)."""
    if formato is None:
        formato = lambda v: f"{v * 100:+.1f}%"  # noqa: E731
    if not puntos:
        return None
    meses = _meses_continuos(puntos[0]["mes"], puntos[-1]["mes"])
    if len(meses) < 2:
        meses = [meses[0], meses[0].replace(year=meses[0].year + (meses[0].month == 12), month=meses[0].month % 12 + 1)]
    indice = {m: i for i, m in enumerate(meses)}
    valores = [p["individual"] for p in puntos] + [p["acumulado"] for p in puntos] + [0.0]
    arriba, abajo = max(valores), min(valores)
    rango = (arriba - abajo) or 0.01
    arriba, abajo = arriba + rango * 0.12, abajo - rango * 0.12
    tope, base = pad_y, alto - 26
    paso = (ancho - 2 * pad_x) / (len(meses) - 1)

    def x_de(mes):
        return pad_x + indice[mes] * paso

    def y_de(valor):
        return tope + (base - tope) * (arriba - valor) / (arriba - abajo)

    segmentos, linea, marcas = [], [], []
    anio_anterior = None
    for p in puntos:
        x, y_acum, y_ind = x_de(p["mes"]), y_de(p["acumulado"]), y_de(p["individual"])
        if p["mes"].year != anio_anterior:
            segmentos.append([])
            anio_anterior = p["mes"].year
        segmentos[-1].append((x, y_acum))
        juntos = abs(y_acum - y_ind) < 6  # enero: acumulado = individual
        acumulado_arriba = y_acum <= y_ind
        linea.append({
            "x": f"{x:.1f}", "y": f"{y_acum:.1f}", "x_pct": f"{x / ancho * 100:.2f}", "y_pct": f"{y_acum / alto * 100:.2f}",
            "etiqueta": formato(p["acumulado"]), "pos": "arriba" if acumulado_arriba else "abajo",
            "detalle": p["detalle_acumulado"],
        })
        marcas.append({
            "x": f"{x:.1f}", "y": f"{y_ind:.1f}", "x_pct": f"{x / ancho * 100:.2f}", "y_pct": f"{y_ind / alto * 100:.2f}",
            "etiqueta": "" if juntos else formato(p["individual"]), "pos": "abajo" if acumulado_arriba else "arriba",
            "sube": p["individual"] > 0, "detalle": p["detalle_individual"],
        })
    trazos = [" ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(s)) for s in segmentos]
    # Separador y rótulo de cada año (en el primer mes de cada año del eje).
    anios = []
    for m in meses:
        if m == meses[0] or m.month == 1:
            x = x_de(m) - (paso / 2 if m != meses[0] else 0)
            anios.append({"x": f"{x:.1f}", "x_pct": f"{max(x, 0) / ancho * 100:.2f}", "texto": str(m.year),
                          "separador": m != meses[0]})
    return {
        "ancho": ancho, "alto": alto, "trazos": trazos, "linea": linea, "marcas": marcas, "anios": anios,
        "y_cero": f"{y_de(0.0):.1f}", "tope": f"{tope - 18:.1f}", "base": f"{base:.1f}",
        "eje": [{"x_pct": f"{x_de(m) / ancho * 100:.2f}", "texto": etiqueta_mes(m)} for m in meses],
    }


def barras(items, campo_valor="valor"):
    """items: lista de dicts ya ordenada de mayor a menor. Agrega 'pct' (0-100,
    ya formateado como texto) para pintar cada barra como un div con width en
    CSS, sin SVG. Debe ser texto con punto decimal: en español, Django
    renderiza los floats de una plantilla con coma decimal, lo que invalidaría
    un "width: 45,3%" en CSS."""
    if not items:
        return []
    maximo = max(i[campo_valor] for i in items) or 1
    return [{**i, "pct": f"{i[campo_valor] / maximo * 100:.1f}"} for i in items]


def _toneladas_corto(valor):
    """523,400 -> '523K'; 1,234,000 -> '1.23M'; negativos con signo. Corto a
    propósito: con 17 meses en 640 px de ancho, un número completo por punto
    se pisaba con el del mes vecino."""
    absoluto = abs(valor)
    if absoluto >= 1_000_000:
        texto = f"{absoluto / 1_000_000:.2f}M"
    elif absoluto >= 10_000:
        texto = f"{absoluto / 1_000:.0f}K"
    elif absoluto >= 1_000:
        # Con un decimal por debajo de 10 mil: en materiales de poco volumen
        # (p.ej. carbón), 4,662 y 5,333 se verían los dos como "5K".
        texto = f"{absoluto / 1_000:.1f}K"
    else:
        texto = f"{absoluto:.0f}"
    return f"-{texto}" if valor < 0 else texto


def evolucion_stock(puntos, barras=None, banda=None, detalles=None, ancho=640, alto=300, pad_x=16, pad_y=30):
    """Evolución de stock proyectado (toneladas), en dos franjas que comparten
    los meses del eje X:
    - arriba, la línea de stock (roja, como el objetivo de la Curva S) con la
      línea de 0 siempre dentro de la escala, para que un quiebre (stock
      negativo) se vea de inmediato, con su punto en color de alerta;
    - abajo, barritas del abastecimiento (entra) y el consumo (sale)
      planificados de cada mes, en su propia escala, para ver qué movió el
      stock. Pedido por el usuario.
    `puntos`: lista de (etiqueta_mes, stock). `barras`: lista paralela a
    `puntos` de (abastecimiento, consumo), o None en los puntos sin barras
    (p.ej. el primero, que es el stock de partida). `banda`: lista paralela a
    `puntos` de (mínimo, máximo) —None donde no hay— para dibujar la banda de
    stock objetivo detrás de la línea (solo tiene sentido para UN material).
    `detalles`: lista paralela a `puntos` con el texto (varias líneas) del
    recuadro que aparece al pasar el mouse por el punto o su etiqueta.
    Todos los meses llevan su etiqueta en el eje (antes, con más de 9 meses,
    se mostraba uno de cada dos). Pedido por el usuario."""
    if len(puntos) < 2:
        return None
    valores = [v for _, v in puntos]
    limites = [v for par in (banda or []) if par for v in par if v is not None and v == v]
    minimo, maximo = min(min(valores), 0.0), max(max(valores + limites), 0.0)
    rango = (maximo - minimo) or 1.0
    paso = (ancho - 2 * pad_x) / (len(puntos) - 1)

    # Franjas: la línea usa ~la mitad de arriba; las barras, la de abajo (sobre
    # las etiquetas de los meses, que van pegadas al borde inferior).
    base_linea = alto * 0.56 if barras else alto - pad_y
    tope_barras, base_barras = alto * 0.66, alto - 22

    def y_de(valor):
        return pad_y + (base_linea - pad_y) * (1 - (valor - minimo) / rango)

    crudos = [(pad_x + i * paso, y_de(v), etiqueta, v) for i, (etiqueta, v) in enumerate(puntos)]
    linea = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y, _, _) in enumerate(crudos))
    y_cero = y_de(0.0)
    area = linea + f" L{crudos[-1][0]:.1f} {y_cero:.1f} L{crudos[0][0]:.1f} {y_cero:.1f} Z"

    puntos_out = []
    for i, (x, y, etiqueta, valor) in enumerate(crudos):
        puntos_out.append({
            "x": f"{x:.1f}", "y": f"{y:.1f}",
            "x_pct": f"{x / ancho * 100:.2f}", "y_pct": f"{y / alto * 100:.2f}",
            "etiqueta": etiqueta,
            "etiqueta_valor": _toneladas_corto(valor),
            "negativo": valor < 0,
            "detalle": detalles[i] if detalles else f"{etiqueta}: {valor:,.0f} T",
        })

    barras_out = []
    if barras:
        mayor = max((max(b) for b in barras if b), default=0.0) or 1.0
        ancho_barra = min(paso * 0.36, 14.0)
        for (x, _, etiqueta, _), par in zip(crudos, barras):
            if not par:
                continue
            for valor, clase, desplazamiento, nombre in (
                (par[0], "barra-ingreso", -ancho_barra - 1, "Abastecimiento planificado"),
                (par[1], "barra-consumo", 1, "Consumo planificado"),
            ):
                alto_barra = max(valor, 0.0) / mayor * (base_barras - tope_barras)
                x_barra = x + desplazamiento
                # Valor dentro de la barra, en vertical (en horizontal no entra
                # en barras de ~14 px); si la barra es muy baja para el
                # número, va justo encima de ella.
                dentro = alto_barra >= 24
                y_etiqueta = base_barras - alto_barra / 2 if dentro else base_barras - alto_barra - 2
                barras_out.append({
                    "x": f"{x_barra:.1f}", "y": f"{base_barras - alto_barra:.1f}",
                    "ancho": f"{ancho_barra:.1f}", "alto": f"{alto_barra:.1f}", "clase": clase,
                    "titulo": f"{nombre} {etiqueta}: {valor:,.0f} T",
                    "etiqueta": _toneladas_corto(valor) if valor > 0 else "",
                    "etiqueta_dentro": dentro,
                    "x_pct": f"{(x_barra + ancho_barra / 2) / ancho * 100:.2f}",
                    "y_pct": f"{y_etiqueta / alto * 100:.2f}",
                })
    # Banda mínimo–máximo: escalonada (el valor vale para todo el mes, no se
    # interpola entre meses), como polígono + sus dos bordes punteados.
    banda_out = None
    if banda and any(par and par[0] is not None and par[1] is not None for par in banda):
        tramos_min, tramos_max = [], []
        for i, (x, _, _, _) in enumerate(crudos):
            par = banda[i] if i < len(banda) else None
            if not par or par[0] is None or par[1] is None or par[0] != par[0] or par[1] != par[1]:
                continue
            x0, x1 = max(x - paso / 2, pad_x), min(x + paso / 2, ancho - pad_x)
            tramos_min += [(x0, y_de(par[0])), (x1, y_de(par[0]))]
            tramos_max += [(x0, y_de(par[1])), (x1, y_de(par[1]))]
        if tramos_min:
            def trazo(puntos_linea):
                return " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(puntos_linea))
            poligono = trazo(tramos_max) + " " + " ".join(f"L{x:.1f} {y:.1f}" for x, y in reversed(tramos_min)) + " Z"
            ultimo = next(par for par in reversed(banda) if par and par[0] is not None and par[1] is not None)
            banda_out = {
                "area": poligono, "linea_min": trazo(tramos_min), "linea_max": trazo(tramos_max),
                "texto_min": _toneladas_corto(ultimo[0]), "texto_max": _toneladas_corto(ultimo[1]),
            }
    return {
        "ancho": ancho, "alto": alto, "linea": linea, "area": area, "puntos": puntos_out,
        "y_cero": f"{y_cero:.1f}", "hay_negativos": any(v < 0 for v in valores),
        "barras": barras_out, "base_barras": f"{base_barras:.1f}", "banda": banda_out,
    }


def _meses_continuos(desde, hasta):
    """Lista de meses (día 1) de `desde` a `hasta`, inclusive."""
    meses, actual = [], desde.replace(day=1)
    while actual <= hasta:
        meses.append(actual)
        actual = actual.replace(year=actual.year + (actual.month == 12), month=actual.month % 12 + 1)
    return meses


def evolucion_precio(serie, etiqueta_mes, base=None, ancho=640, alto=280, pad_x=16, pad_y=28):
    """Precio de entrada mensual (S/ por T) de UN material, en dos franjas con
    el mismo eje de meses (continuo: un mes sin compra queda como hueco, así
    se ve cada cuánto se compra — p.ej. un barco cada 5 meses):
    - arriba, la línea del precio (escala ajustada al rango del precio, no
      desde 0, para que una variación de 2-3% se vea), con etiqueta solo en
      el primer y último punto y en el precio mínimo y máximo (con 30+ meses,
      un número por punto se pisaba);
    - abajo, barritas grises con las toneladas compradas cada mes.
    `serie`: lista de (mes, precio, toneladas) ordenada. `etiqueta_mes`:
    función mes -> texto corto del eje. `base`: lista opcional de (mes,
    precio) con el precio contra el que se compara cada mes (Inflación de
    precios), dibujada como línea punteada en la misma escala."""
    if not serie:
        return None
    meses = _meses_continuos(serie[0][0], serie[-1][0])
    if len(meses) < 2:
        meses = [meses[0], meses[0].replace(year=meses[0].year + (meses[0].month == 12), month=meses[0].month % 12 + 1)]
    indice = {m: i for i, m in enumerate(meses)}
    precios = [p for _, p, _ in serie] + [p for _, p in (base or [])]
    minimo, maximo = min(precios), max(precios)
    margen = (maximo - minimo) * 0.15 or maximo * 0.05 or 1.0
    piso, techo = minimo - margen, maximo + margen
    # Rango mínimo de ±10% del precio promedio: sin esto, una variación de
    # 2-3% ocupaba todo el alto y parecía un salto enorme.
    centro, medio_rango = (piso + techo) / 2, max((techo - piso) / 2, 0.10 * sum(precios) / len(precios))
    piso, techo = centro - medio_rango, centro + medio_rango
    paso = (ancho - 2 * pad_x) / (len(meses) - 1)
    base_linea = alto * 0.60
    tope_barras, base_barras = alto * 0.70, alto - 22

    def y_de(valor):
        return pad_y + (base_linea - pad_y) * (1 - (valor - piso) / (techo - piso))

    crudos = [(pad_x + indice[m] * paso, y_de(p), m, p, t) for m, p, t in serie]
    linea = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y, _, _, _) in enumerate(crudos))
    i_min = min(range(len(crudos)), key=lambda i: crudos[i][3])
    i_max = max(range(len(crudos)), key=lambda i: crudos[i][3])
    con_etiqueta = {0, len(crudos) - 1, i_min, i_max}
    puntos = [{
        "x": f"{x:.1f}", "y": f"{y:.1f}", "x_pct": f"{x / ancho * 100:.2f}", "y_pct": f"{y / alto * 100:.2f}",
        "titulo": f"{etiqueta_mes(m)}: S/ {p:,.2f} por T · {t:,.0f} T",
        "etiqueta": f"{p:,.1f}" if i in con_etiqueta else "",
    } for i, (x, y, m, p, t) in enumerate(crudos)]

    mayor = max(t for _, _, t in serie) or 1.0
    ancho_barra = max(min(paso * 0.6, 14.0), 2.0)
    barras = []
    for x, _, m, p, t in crudos:
        alto_barra = max(t, 0.0) / mayor * (base_barras - tope_barras)
        barras.append({
            "x": f"{x - ancho_barra / 2:.1f}", "y": f"{base_barras - alto_barra:.1f}",
            "ancho": f"{ancho_barra:.1f}", "alto": f"{alto_barra:.1f}",
            "titulo": f"{etiqueta_mes(m)}: {t:,.0f} T compradas",
        })
    paso_eje = max(1, math.ceil(len(meses) / 10))
    eje = [
        {"x_pct": f"{(pad_x + i * paso) / ancho * 100:.2f}", "texto": etiqueta_mes(m)}
        for i, m in enumerate(meses) if i % paso_eje == 0 or i == len(meses) - 1
    ]
    linea_base = " ".join(
        f"{'M' if i == 0 else 'L'}{pad_x + indice[m] * paso:.1f} {y_de(p):.1f}" for i, (m, p) in enumerate(base or [])
    )
    return {
        "ancho": ancho, "alto": alto, "linea": linea, "puntos": puntos, "barras": barras, "eje": eje,
        "base_barras": f"{base_barras:.1f}", "tonelaje_maximo": _toneladas_corto(mayor), "linea_base": linea_base,
    }


def minigrafico_precio(serie, ancho=180, alto=46, pad=5):
    """Minigráfico (sparkline) del precio de entrada para la grilla de
    materiales: solo la línea, con el último punto marcado. `serie`: lista de
    (mes, precio). Mismo eje de meses continuo que evolucion_precio."""
    if not serie:
        return None
    meses = _meses_continuos(serie[0][0], serie[-1][0])
    total = max(len(meses) - 1, 1)
    indice = {m: i for i, m in enumerate(meses)}
    precios = [p for _, p in serie]
    minimo, maximo = min(precios), max(precios)
    rango = (maximo - minimo) or (maximo * 0.1) or 1.0
    if maximo == minimo:
        minimo -= rango / 2
    crudos = [
        (pad + indice[m] / total * (ancho - 2 * pad), pad + (alto - 2 * pad) * (1 - (p - minimo) / rango))
        for m, p in serie
    ]
    if len(crudos) == 1:
        crudos = [(pad, crudos[0][1]), (ancho - pad, crudos[0][1])]
    return {
        "ancho": ancho, "alto": alto,
        "linea": " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(crudos)),
        "fin_x": f"{crudos[-1][0]:.1f}", "fin_y": f"{crudos[-1][1]:.1f}",
    }


def evolucion_calidad(serie, promedio, formato, etiqueta_fecha, referencia=None, ancho=360, alto=150, pad_x=12, pad_y=22):
    """Evolución de UN parámetro de calidad (p.ej. CaO %) en el tiempo, para
    la grilla de gráficos de un material: línea con un punto por semana
    (promedio ponderado por toneladas), ubicados según su fecha real, más una
    línea punteada con el promedio del período. Etiquetas de valor solo en el
    primer y último punto y en el mínimo y máximo. `serie`: lista de (fecha,
    valor) ordenada; `formato`: valor -> texto; `etiqueta_fecha`: fecha ->
    texto corto del eje. `referencia`: otro promedio (p.ej. el de todos los
    proveedores, con uno elegido) que se dibuja como segunda línea."""
    if not serie:
        return None
    valores = [v for _, v in serie]
    fijos = [promedio] + ([referencia] if referencia is not None else [])
    minimo, maximo = min(valores + fijos), max(valores + fijos)
    margen = (maximo - minimo) * 0.18 or abs(maximo) * 0.05 or 1.0
    piso, techo = minimo - margen, maximo + margen
    # Rango mínimo de ±3% del promedio: sin esto, un parámetro casi constante
    # (p.ej. HGI 55 todas las semanas) mostraba ruido de decimales como saltos.
    medio_rango = max((techo - piso) / 2, abs(promedio) * 0.03)
    centro = (piso + techo) / 2
    piso, techo = centro - medio_rango, centro + medio_rango
    base = alto - 20
    primero, ultimo = serie[0][0], serie[-1][0]
    dias = max((ultimo - primero).days, 1)

    def x_de(fecha):
        return pad_x + (ancho - 2 * pad_x) * ((fecha - primero).days / dias if len(serie) > 1 else 0.5)

    def y_de(valor):
        return pad_y + (base - pad_y) * (1 - (valor - piso) / (techo - piso))

    crudos = [(x_de(f), y_de(v), f, v) for f, v in serie]
    i_min = min(range(len(crudos)), key=lambda i: crudos[i][3])
    i_max = max(range(len(crudos)), key=lambda i: crudos[i][3])
    con_etiqueta = {0, len(crudos) - 1, i_min, i_max}
    y_promedio = y_de(promedio)
    return {
        "ancho": ancho, "alto": alto,
        "linea": " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y, _, _) in enumerate(crudos)),
        "y_promedio": f"{y_promedio:.1f}", "y_promedio_pct": f"{y_promedio / alto * 100:.2f}",
        "y_referencia": f"{y_de(referencia):.1f}" if referencia is not None else None,
        # "alineacion": las etiquetas del primer y último punto (y las fechas
        # del eje) se alinean hacia adentro, si no quedaban cortadas en el
        # borde del gráfico.
        "puntos": [{
            "x": f"{x:.1f}", "y": f"{y:.1f}", "x_pct": f"{x / ancho * 100:.2f}", "y_pct": f"{y / alto * 100:.2f}",
            "titulo": f"Semana del {etiqueta_fecha(f)}: {formato(v)}",
            "etiqueta": formato(v) if i in con_etiqueta else "",
            "alineacion": "inicio" if i == 0 and len(crudos) > 1 else ("fin" if i == len(crudos) - 1 and len(crudos) > 1 else ""),
        } for i, (x, y, f, v) in enumerate(crudos)],
        "eje": [{"x_pct": f"{x_de(f) / ancho * 100:.2f}", "texto": etiqueta_fecha(f),
                 "alineacion": ("inicio" if f == primero else "fin") if len(serie) > 1 else ""}
                for f in ([primero, ultimo] if len(serie) > 1 else [primero])],
    }


def evolucion_consumo(real, plan, etiqueta_mes, mes_parcial=None, ancho=960, alto=300, pad_x=22, pad_y=26):
    """Consumo mes a mes (toneladas): barras con el consumo REAL (MB51) y una
    línea con puntos con el PLAN de consumos del S&OP, sobre un eje de meses
    continuo. `real` y `plan`: listas de (mes, toneladas). `mes_parcial`: mes
    en curso (su barra va más clara: todavía no cierra). Pedido por el
    usuario."""
    if not real and not plan:
        return None
    todos = [m for m, _ in real] + [m for m, _ in plan]
    meses = _meses_continuos(min(todos), max(todos))
    if len(meses) < 2:
        meses = [meses[0], meses[0].replace(year=meses[0].year + (meses[0].month == 12), month=meses[0].month % 12 + 1)]
    indice = {m: i for i, m in enumerate(meses)}
    mayor = max([v for _, v in real] + [v for _, v in plan] + [1.0]) * 1.15
    base = alto - 24
    paso = (ancho - 2 * pad_x) / (len(meses) - 1)
    ancho_barra = max(min(paso * 0.62, 26.0), 3.0)

    def x_de(mes):
        return pad_x + indice[mes] * paso

    def y_de(valor):
        return base - (base - pad_y) * max(valor, 0.0) / mayor

    plan_por_mes = dict(plan)
    barras = []
    for mes, valor in real:
        x, y = x_de(mes), y_de(valor)
        alto_barra = base - y
        dentro = alto_barra >= 30
        parcial = mes == mes_parcial
        texto_plan = f" · plan {plan_por_mes[mes]:,.0f} T ({valor / plan_por_mes[mes] * 100:.0f}%)" if plan_por_mes.get(mes) else ""
        barras.append({
            "x": f"{x - ancho_barra / 2:.1f}", "y": f"{y:.1f}", "ancho": f"{ancho_barra:.1f}", "alto": f"{alto_barra:.1f}",
            "parcial": parcial, "x_pct": f"{x / ancho * 100:.2f}",
            "y_pct": f"{(y + alto_barra / 2 if dentro else y - 2) / alto * 100:.2f}", "dentro": dentro,
            "etiqueta": _toneladas_corto(valor) if valor > 0 else "",
            "detalle": f"{etiqueta_mes(mes)}: consumo real {valor:,.0f} T{' (mes en curso, parcial)' if parcial else ''}{texto_plan}",
        })
    puntos = [{
        "x": f"{x_de(m):.1f}", "y": f"{y_de(v):.1f}", "x_pct": f"{x_de(m) / ancho * 100:.2f}", "y_pct": f"{y_de(v) / alto * 100:.2f}",
        "etiqueta": _toneladas_corto(v), "detalle": f"{etiqueta_mes(m)}: plan de consumo S&OP {v:,.0f} T",
    } for m, v in sorted(plan)]
    linea = " ".join(f"{'M' if i == 0 else 'L'}{p['x']} {p['y']}" for i, p in enumerate(puntos))
    paso_eje = max(1, math.ceil(len(meses) / 20))
    return {
        "ancho": ancho, "alto": alto, "base": f"{base:.1f}", "barras": barras, "puntos": puntos, "linea": linea,
        "eje": [{"x_pct": f"{x_de(m) / ancho * 100:.2f}", "texto": etiqueta_mes(m)}
                for i, m in enumerate(meses) if i % paso_eje == 0 or i == len(meses) - 1],
    }


def lineas_mensuales(series, etiqueta_mes, formato, referencias=None, desde_cero=False,
                     ancho=960, alto=280, pad_izq=52, pad_der=46, pad_arr=22, pad_abj=24, max_etiquetas_eje=18):
    """Una o dos series mensuales en la MISMA unidad (un solo eje) sobre un eje
    de meses continuo: la línea se corta donde falta un mes, cada punto tiene
    su recuadro de detalle al pasar el mouse. Etiquetas de valor: con una
    serie, primer y último punto y el mínimo y máximo; con dos, solo el último
    de cada una (más se pisaban). `series`: lista de dicts {"nombre", "clase",
    "puntos": [(mes, valor, detalle)]} con los puntos ordenados; `formato`:
    valor -> texto; `referencias`: lista opcional de {"valor", "texto",
    "clase"} dibujadas como líneas horizontales punteadas (p.ej. el promedio).
    Usado por Consumo por tonelada producida."""
    series = [s for s in series if s["puntos"]]
    if not series:
        return None
    todos = [m for s in series for m, _, _ in s["puntos"]]
    meses = _meses_continuos(min(todos), max(todos))
    if len(meses) < 2:
        meses = [meses[0], meses[0].replace(year=meses[0].year + (meses[0].month == 12), month=meses[0].month % 12 + 1)]
    indice = {m: i for i, m in enumerate(meses)}
    series = [{**s, "puntos": [(indice[m], v, d) for m, v, d in s["puntos"]]} for s in series]
    return lineas_indice(series, [etiqueta_mes(m) for m in meses], formato, referencias=referencias,
                         desde_cero=desde_cero, ancho=ancho, alto=alto, pad_izq=pad_izq, pad_der=pad_der,
                         pad_arr=pad_arr, pad_abj=pad_abj, max_etiquetas_eje=max_etiquetas_eje)


def lineas_indice(series, etiquetas, formato, referencias=None, desde_cero=False, ancho=960, alto=280,
                  pad_izq=52, pad_der=46, pad_arr=22, pad_abj=24, max_etiquetas_eje=18, piso=None, techo=None):
    """Lo mismo que lineas_mensuales sobre un eje de posiciones 0..n-1 con
    `etiquetas[i]` como texto de cada una (meses, días del mes...). Los
    puntos de cada serie son (posición, valor, detalle); la línea se corta
    donde falta una posición, salvo en las series con "continua": True (p.ej.
    el plan lineal de la curva S, de solo dos puntos). `piso`/`techo`:
    escala fija opcional. Usado también por la curva S de la pestaña
    Indicadores."""
    series = [s for s in series if s["puntos"]]
    if not series:
        return None
    referencias = referencias or []
    n_pos = max(len(etiquetas), 2)
    valores = [v for s in series for _, v, _ in s["puntos"]] + [r["valor"] for r in referencias]
    minimo, maximo = min(valores), max(valores)
    margen = (maximo - minimo) * 0.12 or abs(maximo) * 0.1 or 1.0
    if piso is None:
        piso = 0.0 if desde_cero else minimo - margen
    if techo is None:
        techo = maximo + margen
    base = alto - pad_abj
    paso = (ancho - pad_izq - pad_der) / (n_pos - 1)

    def x_de(posicion):
        return pad_izq + posicion * paso

    def y_de(valor):
        return pad_arr + (base - pad_arr) * (1 - (valor - piso) / (techo - piso))

    salida = []
    for s in series:
        tramos, actual, previo = [], [], None
        for posicion, valor, _ in s["puntos"]:
            if previo is not None and posicion - previo > 1 and not s.get("continua"):
                tramos.append(actual)
                actual = []
            actual.append(f"{'M' if not actual else 'L'}{x_de(posicion):.1f} {y_de(valor):.1f}")
            previo = posicion
        tramos.append(actual)
        n = len(s["puntos"])
        if len(series) == 1:
            i_min = min(range(n), key=lambda i: s["puntos"][i][1])
            i_max = max(range(n), key=lambda i: s["puntos"][i][1])
            con_etiqueta = {0, n - 1, i_min, i_max}
        else:
            con_etiqueta = {n - 1}
        salida.append({
            "nombre": s["nombre"], "clase": s["clase"],
            "tramos": [" ".join(t) for t in tramos if len(t) > 1],
            "puntos": [{
                "x": f"{x_de(m):.1f}", "y": f"{y_de(v):.1f}",
                "x_pct": f"{x_de(m) / ancho * 100:.2f}", "y_pct": f"{(y_de(v) - 12) / alto * 100:.2f}",
                "detalle": d, "etiqueta": formato(v) if i in con_etiqueta else "",
            } for i, (m, v, d) in enumerate(s["puntos"])],
        })
    # Con dos series, la etiqueta del último punto va a la derecha de ese punto
    # (arriba se pisaba con la otra serie); si las dos quedan casi a la misma
    # altura, se separan al menos una línea de texto.
    if len(salida) > 1:
        finales = sorted((s["puntos"][-1] for s in salida), key=lambda p: float(p["y"]))
        previo = None
        for p in finales:
            y = float(p["y"])
            if previo is not None and y - previo < 13:
                y = previo + 13
            p["y_pct"] = f"{y / alto * 100:.2f}"
            p["derecha"] = True
            previo = y
    marcas = [v for v in _marcas_eje(piso, techo, 4) if piso <= v <= techo]
    paso_eje = max(1, math.ceil(len(etiquetas) / max_etiquetas_eje))
    return {
        "ancho": ancho, "alto": alto, "base": f"{base:.1f}", "x0": pad_izq, "x1": ancho - pad_der,
        "series": salida,
        "grilla": [{"y": f"{y_de(v):.1f}", "y_pct": f"{y_de(v) / alto * 100:.2f}", "texto": formato(v)} for v in marcas],
        "referencias": [{"y": f"{y_de(r['valor']):.1f}", "y_pct": f"{(y_de(r['valor']) - 12) / alto * 100:.2f}",
                         "texto": r["texto"], "clase": r.get("clase", "")} for r in referencias],
        "eje": [{"x_pct": f"{x_de(i) / ancho * 100:.2f}", "texto": e}
                for i, e in enumerate(etiquetas) if i % paso_eje == 0 or i == len(etiquetas) - 1],
    }


def pilas_horizontales(filas):
    """Barras horizontales apiladas al 100% (HTML/CSS), una por fila: cada
    segmento ocupa su parte del total de la fila. `filas`: dicts con
    "segmentos" ({"valor", "clase", "detalle"}); se agrega "ancho" (% como
    texto con punto) y "texto" (el valor, solo si el segmento es lo bastante
    ancho para que se lea). Usado por Salud del inventario (Indicadores)."""
    if not filas:
        return None
    salida = []
    for f in filas:
        total = sum(s["valor"] for s in f["segmentos"]) or 1
        segmentos = [{**s, "ancho": f"{s['valor'] / total * 100:.2f}",
                      "texto": f"{s['valor']:,.0f}" if s["valor"] / total >= 0.07 else ""}
                     for s in f["segmentos"] if s["valor"] > 0]
        salida.append({**f, "segmentos": segmentos})
    return {"filas": salida}


def barras_escala(filas, minimo, maximo, referencias=(), banda=None):
    """Barras horizontales en HTML/CSS (sin SVG) sobre una escala fija
    [minimo, maximo] que parte de 0: % de una meta (0–130%, con la meta en
    100%), desviaciones con signo (banda de tolerancia alrededor de 0) o días
    de cobertura (con el umbral de 30 días). `filas`: dicts con "valor"; se
    les agrega la posición de la barra en % del ancho (izq, ancho) y
    "recortada" si el valor se sale de la escala. `referencias`: (valor,
    texto) marcados con una línea vertical; `banda`: (desde, hasta)
    sombreado. Usado por la pestaña Indicadores de Planificación."""
    if not filas:
        return None
    rango = (maximo - minimo) or 1.0

    def pos(valor):
        return (min(max(valor, minimo), maximo) - minimo) / rango * 100

    cero = pos(0.0)
    salida = []
    for f in filas:
        x = pos(f["valor"])
        salida.append({**f, "izq": f"{min(cero, x):.2f}", "ancho": f"{abs(x - cero):.2f}",
                       "negativa": f["valor"] < 0, "recortada": not minimo <= f["valor"] <= maximo})
    return {
        "filas": salida,
        "referencias": [{"pos": f"{pos(v):.2f}", "texto": t} for v, t in referencias],
        "banda": {"izq": f"{pos(banda[0]):.2f}", "ancho": f"{pos(banda[1]) - pos(banda[0]):.2f}"} if banda else None,
    }


def columnas(etiquetas, pilas, formato, linea=None, textos=None, ancho=960, alto=250,
             pad_izq=44, pad_der=16, pad_arr=20, pad_abj=24):
    """Columnas por mes (SVG) apiladas y con signo, en un solo eje: los
    segmentos positivos se apilan hacia arriba desde 0 y los negativos hacia
    abajo (p.ej. premios arriba y castigos abajo, o materiales por estado de
    stock). `etiquetas`: texto de cada columna en el eje; `pilas`: lista
    paralela de listas de segmentos {"valor", "clase"}; `formato`: valor ->
    texto de la grilla. `linea`: opcional, {"valores" (paralela, None = sin
    punto), "cortes" (índices donde empieza un tramo nuevo, p.ej. cada
    enero), "clase", "detalles", "etiqueta_final"} en la MISMA unidad que las
    columnas. `textos`: lista paralela de textos cortos sobre cada columna
    (vacío = sin texto). Cada columna trae su recuadro de detalle
    (`pilas[i]` puede traer "detalle" en el primer segmento o se arma con
    los valores). Usado por la pestaña Indicadores de Planificación."""
    n = len(etiquetas)
    if not n:
        return None
    if linea and linea.get("etiqueta_final"):
        pad_der = max(pad_der, 12 + 6.2 * len(linea["etiqueta_final"]))  # la etiqueta final va a la derecha del último punto
    arriba = [sum(s["valor"] for s in p if s["valor"] > 0) for p in pilas]
    abajo = [sum(s["valor"] for s in p if s["valor"] < 0) for p in pilas]
    valores_linea = [v for v in (linea or {}).get("valores", []) if v is not None]
    techo, piso = max(arriba + valores_linea + [0.0]), min(abajo + valores_linea + [0.0])
    margen = (techo - piso) * 0.12 or 1.0
    techo = techo + margen if techo > 0 else 0.0
    piso = piso - margen if piso < 0 else 0.0
    base = alto - pad_abj
    paso = (ancho - pad_izq - pad_der) / n
    ancho_col = max(min(paso * 0.62, 34.0), 3.0)

    def y_de(valor):
        return pad_arr + (base - pad_arr) * (techo - valor) / (techo - piso)

    def x_de(i):
        return pad_izq + paso * (i + 0.5)

    columnas_out = []
    for i, pila in enumerate(pilas):
        x = x_de(i)
        segmentos, sube, baja = [], 0.0, 0.0
        for s in pila:
            if not s["valor"]:
                continue
            if s["valor"] > 0:
                y0, y1 = y_de(sube + s["valor"]), y_de(sube)
                sube += s["valor"]
            else:
                y0, y1 = y_de(baja), y_de(baja + s["valor"])
                baja += s["valor"]
            segmentos.append({"x": f"{x - ancho_col / 2:.1f}", "y": f"{y0:.1f}", "ancho": f"{ancho_col:.1f}",
                              "alto": f"{max(y1 - y0, 0.8):.1f}", "clase": s["clase"]})
        texto = (textos[i] if textos else "") or ""
        negativa = sube == 0 and baja < 0
        columnas_out.append({
            "segmentos": segmentos, "detalle": pila[0].get("detalle", "") if pila else "",
            "filtrar": pila[0].get("filtrar", "") if pila else "",
            "zona_x": f"{x - paso / 2:.1f}", "zona_ancho": f"{paso:.1f}",
            "texto": texto, "texto_abajo": negativa, "x_pct": f"{x / ancho * 100:.2f}",
            "y_pct": f"{(y_de(baja) if negativa else y_de(sube)) / alto * 100:.2f}",
        })

    linea_out = None
    if linea and valores_linea:
        tramos, actual = [], []
        puntos = []
        cortes = set(linea.get("cortes", ()))
        for i, v in enumerate(linea["valores"]):
            if v is None or i in cortes:
                if len(actual) > 1:
                    tramos.append(" ".join(actual))
                actual = []
            if v is None:
                continue
            actual.append(f"{'M' if not actual else 'L'}{x_de(i):.1f} {y_de(v):.1f}")
            puntos.append({"x": f"{x_de(i):.1f}", "y": f"{y_de(v):.1f}",
                           "detalle": (linea.get("detalles") or [""] * n)[i]})
        if len(actual) > 1:
            tramos.append(" ".join(actual))
        ultimo = max(i for i, v in enumerate(linea["valores"]) if v is not None)
        linea_out = {
            "clase": linea.get("clase", ""), "tramos": tramos, "puntos": puntos,
            "etiqueta_final": linea.get("etiqueta_final", ""),
            "x_pct": f"{x_de(ultimo) / ancho * 100:.2f}",
            "y_pct": f"{y_de(linea['valores'][ultimo]) / alto * 100:.2f}",
        }

    marcas = [v for v in _marcas_eje(piso, techo, 4) if piso <= v <= techo]
    # Etiquetas del eje: una cada tantas columnas para que no se pisen (~52 px
    # por etiqueta de mes), siempre la última.
    paso_eje = max(1, math.ceil(52 / paso))
    return {
        "ancho": ancho, "alto": alto, "x0": pad_izq, "x1": ancho - pad_der, "y_cero": f"{y_de(0.0):.1f}",
        "columnas": columnas_out, "linea": linea_out, "arriba": f"{pad_arr:.1f}", "alto_util": f"{base - pad_arr:.1f}",
        "grilla": [{"y": f"{y_de(v):.1f}", "y_pct": f"{y_de(v) / alto * 100:.2f}", "texto": formato(v)} for v in marcas],
        "eje": [{"x_pct": f"{x_de(i) / ancho * 100:.2f}", "texto": e}
                for i, e in enumerate(etiquetas) if (n - 1 - i) % paso_eje == 0],
    }


def _marcas_eje(minimo, maximo, cantidad=5):
    """Valores redondos (1, 2, 2.5 o 5 × 10^n) entre `minimo` y `maximo`."""
    crudo = (maximo - minimo) / cantidad or abs(maximo) or 1.0
    potencia = 10 ** math.floor(math.log10(crudo))
    paso = next(m * potencia for m in (1, 2, 2.5, 5, 10) if m * potencia >= crudo)
    marcas, valor = [], math.ceil(minimo / paso) * paso
    while valor <= maximo + paso * 1e-6:
        marcas.append(valor)
        valor += paso
    return marcas


def _separar_etiquetas(etiquetas, alto_linea=13, ancho_letra=5.6):
    """Corre verticalmente (±1, ±2 líneas) las etiquetas que se pisarían con
    otra ya ubicada. `etiquetas`: dicts con x, y (centro vertical, unidades
    del viewBox), texto y alineacion ("fin" = el texto termina en x); agrega
    `y_final`."""
    ubicadas = []
    for e in etiquetas:
        ancho = len(e["texto"]) * ancho_letra
        x0 = e["x"] - ancho if e["alineacion"] == "fin" else e["x"]
        for dy in (0, alto_linea, -alto_linea, 2 * alto_linea, -2 * alto_linea):
            y = e["y"] + dy
            if not any(x0 < ux1 and ux0 < x0 + ancho and abs(y - uy) < alto_linea for ux0, ux1, uy in ubicadas):
                break
        e["y_final"] = y
        ubicadas.append((x0, x0 + ancho, y))
    return etiquetas


def dispersion_pago(puntos, referencia, formato_x, ancho=960, alto=380, pad_izq=48, pad_der=40, pad_arr=16,
                    pad_abj=26, etiquetados=8):
    """Monto comprado (eje x, escala logarítmica: los montos van de miles a
    decenas de millones) vs días de pago (eje y) de cada proveedor, con la
    línea punteada del PMP de la categoría (`referencia`): un punto por
    debajo se paga antes que el promedio, uno por encima después. Solo los
    `etiquetados` proveedores de más monto llevan su nombre. `puntos`: dicts
    con nombre (texto de la etiqueta), monto, dias y detalle; `formato_x`:
    monto -> texto de las marcas del eje x. Usado por Cuentas por pagar."""
    puntos = [p for p in puntos if p["monto"] > 0]
    if not puntos:
        return None
    logs = [math.log10(p["monto"]) for p in puntos]
    x_lo, x_hi = math.floor(min(logs)), math.ceil(max(logs))
    if x_hi - x_lo < 2:
        x_lo, x_hi = x_lo - 1, x_hi + (1 if x_hi - x_lo < 1 else 0)
    y_hi = max([p["dias"] for p in puntos] + [referencia, 30.0]) * 1.12
    izq, der, arr, abj = pad_izq, ancho - pad_der, pad_arr, alto - pad_abj

    def x_de(monto):
        return izq + (der - izq) * (math.log10(monto) - x_lo) / (x_hi - x_lo)

    def y_de(dias):
        return abj - (abj - arr) * dias / y_hi

    mas_grandes = {id(p) for p in sorted(puntos, key=lambda p: -p["monto"])[:etiquetados]}
    marcas, etiquetas = [], []
    for p in sorted(puntos, key=lambda p: p["monto"]):
        x, y = x_de(p["monto"]), y_de(p["dias"])
        sentido = "antes" if p["dias"] < referencia - 0.5 else ("despues" if p["dias"] > referencia + 0.5 else "igual")
        marca = {"x": f"{x:.1f}", "y": f"{y:.1f}", "sentido": sentido, "detalle": p["detalle"],
                 "grande": id(p) in mas_grandes, "filtrar": p.get("filtrar", "")}
        marcas.append(marca)
        if marca["grande"]:
            alineacion = "fin" if x > izq + (der - izq) * 0.7 else ""
            etiqueta = {"x": x, "y": y, "texto": p["nombre"], "alineacion": alineacion}
            etiquetas.append(etiqueta)
            marca["_etiqueta"] = etiqueta
    _separar_etiquetas(etiquetas)
    for m in marcas:
        e = m.pop("_etiqueta", None)
        if e:
            m.update({"nombre": e["texto"], "alineacion": e["alineacion"],
                      "x_pct": f"{e['x'] / ancho * 100:.2f}", "y_pct": f"{e['y_final'] / alto * 100:.2f}"})
    return {
        "ancho": ancho, "alto": alto, "marcas": marcas,
        "referencia": {"y": f"{y_de(referencia):.1f}", "y_pct": f"{y_de(referencia) / alto * 100:.2f}"},
        "grilla_y": [{"y": f"{y_de(v):.1f}", "y_pct": f"{y_de(v) / alto * 100:.2f}", "texto": f"{v:,.0f} d"}
                     for v in _marcas_eje(0, y_hi) if v <= y_hi],
        "grilla_x": [{"x": f"{x_de(10 ** k):.1f}", "x_pct": f"{x_de(10 ** k) / ancho * 100:.2f}", "texto": formato_x(10 ** k)}
                     for k in range(x_lo, x_hi + 1)],
        "base": f"{abj:.1f}", "arriba": f"{arr:.1f}", "izq": f"{izq:.1f}", "der": f"{der:.1f}",
        "izq_pct": f"{izq / ancho * 100:.2f}",
    }


def dispersion_energia(puntos, costo_referencia, ancho=960, alto=360, pad_izq=48, pad_der=18, pad_arr=14, pad_abj=24):
    """PCS (eje x, kcal/kg) vs precio efectivo (eje y, S/ por T) de cada
    proveedor o material de carbón, con una burbuja proporcional a las
    toneladas. La línea punteada une todos los (PCS, precio) que cuestan lo
    mismo por Gcal que `costo_referencia` (precio = costo × PCS ÷ 1,000): una
    burbuja por debajo compra la energía más barata que el promedio, una por
    encima más cara. `puntos`: dicts con nombre, pcs, precio, toneladas y
    costo_gcal."""
    if not puntos:
        return None
    pcs = [p["pcs"] for p in puntos]
    precios = [p["precio"] for p in puntos]
    margen_x = max((max(pcs) - min(pcs)) * 0.12, 150.0)
    margen_y = max((max(precios) - min(precios)) * 0.15, max(precios) * 0.04)
    x_lo, x_hi = min(pcs) - margen_x, max(pcs) + margen_x
    y_lo, y_hi = min(precios) - margen_y, max(precios) + margen_y
    izq, der, arr, abj = pad_izq, ancho - pad_der, pad_arr, alto - pad_abj

    def x_de(v):
        return izq + (der - izq) * (v - x_lo) / (x_hi - x_lo)

    def y_de(v):
        return abj - (abj - arr) * (v - y_lo) / (y_hi - y_lo)

    # Tramo visible de la línea de costo igual al promedio (se recorta al área
    # del gráfico con un clipPath en la plantilla).
    linea = f"M{x_de(x_lo):.1f} {y_de(costo_referencia * x_lo / 1000):.1f} L{x_de(x_hi):.1f} {y_de(costo_referencia * x_hi / 1000):.1f}"
    mayor = max(p["toneladas"] for p in puntos) or 1.0
    burbujas = []
    for p in sorted(puntos, key=lambda p: -p["toneladas"]):
        x, y = x_de(p["pcs"]), y_de(p["precio"])
        diferencia = p["costo_gcal"] / costo_referencia - 1 if costo_referencia else 0.0
        radio = 5 + 13 * math.sqrt(max(p["toneladas"], 0) / mayor)
        # Las del tercio derecho llevan el nombre a la izquierda de la
        # burbuja, si no se salía del gráfico.
        alineacion = "fin" if x > izq + (der - izq) * 0.7 else ""
        burbujas.append({
            "x": f"{x:.1f}", "y": f"{y:.1f}", "r": f"{radio:.1f}",
            "x_pct": f"{x / ancho * 100:.2f}", "desplazamiento": f"{radio * 1.2 + 3:.0f}",
            "sentido": "barato" if diferencia < -0.005 else ("caro" if diferencia > 0.005 else "igual"),
            "nombre": p["nombre"], "alineacion": alineacion,
            "titulo": (f"{p['nombre']}: PCS {p['pcs']:,.0f} kcal/kg · S/ {p['precio']:,.2f} por T · "
                       f"S/ {p['costo_gcal']:,.2f} por Gcal ({diferencia * 100:+.1f}% vs promedio) · {p['toneladas']:,.0f} T"),
            "_etiqueta": {"x": x - radio - 3 if alineacion else x + radio + 3, "y": y,
                          "texto": p["nombre"], "alineacion": alineacion},
        })
    _separar_etiquetas([b["_etiqueta"] for b in burbujas])
    for b in burbujas:
        b["y_pct"] = f"{b.pop('_etiqueta')['y_final'] / alto * 100:.2f}"
    return {
        "ancho": ancho, "alto": alto, "linea": linea, "burbujas": burbujas,
        "area": {"x": izq, "y": arr, "ancho": der - izq, "alto": abj - arr},
        "grilla_y": [{"y": f"{y_de(v):.1f}", "y_pct": f"{y_de(v) / alto * 100:.2f}", "texto": f"{v:,.0f}"}
                     for v in _marcas_eje(y_lo, y_hi)],
        "grilla_x": [{"x": f"{x_de(v):.1f}", "x_pct": f"{x_de(v) / ancho * 100:.2f}", "texto": f"{v:,.0f}"}
                     for v in _marcas_eje(x_lo, x_hi, 6)],
        "base": f"{abj:.1f}", "izq": f"{izq:.1f}", "izq_pct": f"{izq / ancho * 100:.2f}",
    }
