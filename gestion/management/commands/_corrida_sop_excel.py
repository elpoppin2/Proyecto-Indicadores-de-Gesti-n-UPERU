"""Lectura de la corrida S&OP que arma Planificación cada mes ('UNACEM-PROY-INVENTARIOS SOP <Mes> <Año>.xlsx'):
hojas 'Mov. Inv Atocongo SOP ...' y 'Mov. Inv Condor SOP ...'.

Cada material es un bloque: una fila con 'Nombre\\nCód. SAP' en la columna B e 'Inv. Inicial' (o 'S. Ini.') en la C;
debajo, una fila por concepto con la etiqueta en C y la unidad en D: primero las cantidades (Compra / Prod / Ing,
Consumo, Traslado, Ajuste, Inv. Final), luego los soles (S/.) y los soles por tonelada (S/ x tm, con el Precio SOP),
y al final la cobertura y el stock mínimo/máximo. Los títulos sueltos de la columna B (MATERIAS PRIMAS, COMBUSTIBLES -
ENERGÍA, ADITIVO, BOLSAS Y BIG BAGS, OTRAS MATERIAS PRIMAS) dicen a qué sección pertenecen los bloques siguientes.
La fila de meses está arriba (una fecha por columna, desde 2023)."""

import datetime
import re
import unicodedata

import openpyxl

from ._excel_utils import CalamineWorkbook, _valor_como_openpyxl

CENTROS = (("U001", "atocongo", "Materias primas"), ("U002", "condor", "Combustibles"))
SECCIONES_SOP = ("Materias primas", "Combustibles")
_CODIGO = re.compile(r"(\d{2}-\d{8})")
_INICIO = {"inv. inicial", "s. ini.", "s. ini", "stock inicial"}
_CONCEPTOS = (
    ("stock_inicial", {"inv. inicial", "s. ini.", "s. ini", "stock inicial"}),
    ("compra", {"compra", "compras", "prod / ing", "compra total", "entregas"}),
    ("consumo", {"consumo", "cons", "consumos"}),
    ("traslado", {"traslado"}),
    ("ajuste", {"ajus. inv", "ajust. inv", "ajuste de inv", "ajuste inv", "ajust inv", "ajuste de inventario"}),
    ("stock_final", {"inv. final", "s. fin", "stock final"}),
    ("precio_sop", {"precio sop"}),
)
_MINMAX = (
    ("minimo", {"stock min", "minimo", "inv. minimo", "inv. min", "inventario min", "min", "stock minimo"}),
    ("maximo", {"stock max", "maximo", "inv. maximo", "inv max", "inv. max", "inventario max", "max", "stock maximo"}),
)


def _norm(valor):
    s = unicodedata.normalize("NFKD", str(valor)).encode("ascii", "ignore").decode().strip().lower()
    return re.sub(r"\s+", " ", s)


def _buscar(etiqueta, opciones):
    e = _norm(etiqueta)
    return next((nombre for nombre, valores in opciones if e in valores), None)


def _tipo_unidad(unidad):
    u = _norm(unidad or "")
    if u in ("s/.", "s/"):
        return "valor"
    if "x" in u and "s/" in u:
        return "precio"
    return "cantidad"


def _seccion(titulo):
    t = _norm(titulo).upper()
    if "ADITIVO" in t:
        return "Aditivos"
    if "BOLSA" in t or "BIG BAG" in t or "BIG-BAG" in t:
        return "Envases"
    if "COMBUSTIBLE" in t or "ENERG" in t:
        return "Combustibles"
    if "MATERIA PRIMA" in t or "MATERIAS PRIMAS" in t or t in ("YESOS", "OXIDOS"):
        return "Materias primas"
    return None


def hojas_movimiento(archivo):
    """{centro: [hojas 'Mov. Inv' visibles de esa planta]} en el orden del libro."""
    libro = openpyxl.load_workbook(archivo, read_only=True)
    try:
        visibles = [ws.title for ws in libro.worksheets
                    if ws.sheet_state == "visible" and _norm(ws.title).startswith("mov. inv")]
    finally:
        libro.close()
    return {centro: [h for h in visibles if planta in _norm(h)] for centro, planta, _ in CENTROS}


def elegir_hojas(archivo, elegidas=None):
    """{centro: hoja} del S&OP. Si una planta tiene más de una hoja visible (escenarios, copias, versiones),
    hay que indicarla en `elegidas` ({centro: nombre}); si no, ValueError con las opciones."""
    elegidas = {c: h for c, h in (elegidas or {}).items() if h}
    candidatas = hojas_movimiento(archivo)
    salida = {}
    for centro, _, _ in CENTROS:
        if centro in elegidas:
            salida[centro] = elegidas[centro]
        elif len(candidatas[centro]) == 1:
            salida[centro] = candidatas[centro][0]
        else:
            opciones = ", ".join(f"'{h}'" for h in candidatas[centro]) or "ninguna"
            raise ValueError(f"no se sabe qué hoja usar para {centro} (hojas 'Mov. Inv' visibles: {opciones})")
    return salida


def _filas_desde_a1(archivo, hoja):
    """Filas de la hoja desde A1. El lector común (_filas_xlsx) empieza en la primera celda con dato y en la hoja de
    Condorcocha la columna A está vacía: las columnas se correrían una posición."""
    if CalamineWorkbook is None:
        libro = openpyxl.load_workbook(archivo, read_only=True, data_only=True)
        try:
            return [list(f) for f in libro[hoja].iter_rows(values_only=True)]
        finally:
            libro.close()
    libro = CalamineWorkbook.from_path(str(archivo))
    try:
        return [[_valor_como_openpyxl(v) for v in fila]
                for fila in libro.get_sheet_by_name(hoja).to_python(skip_empty_area=False)]
    finally:
        libro.close()


def leer_bloques(archivo, hoja, seccion_inicial):
    """Bloques de una hoja 'Mov. Inv': lista de dicts con codigo, nombre, unidad, seccion y datos
    {(concepto, tipo): {mes: valor}}; tipo = cantidad, valor (S/) o precio (S/ por unidad). Dentro de un bloque se
    toma la primera fila de cada concepto y tipo (más abajo vienen propuestas y precios de presupuesto)."""
    filas = _filas_desde_a1(archivo, hoja)
    fila_mes = next((i for i, f in enumerate(filas[:8]) if sum(isinstance(v, datetime.datetime) for v in f) >= 12), None)
    if fila_mes is None:
        raise ValueError(f"la hoja '{hoja}' no tiene la fila de meses arriba")
    meses = {j: v.date().replace(day=1) for j, v in enumerate(filas[fila_mes]) if isinstance(v, datetime.datetime)}
    bloques, actual, seccion, vistos = [], None, seccion_inicial, set()
    for fila in filas:
        b = fila[1] if len(fila) > 1 else None
        c = fila[2] if len(fila) > 2 else None
        d = fila[3] if len(fila) > 3 else None
        if b and not c and not _CODIGO.search(str(b)) and _seccion(b):
            seccion = _seccion(b)
        if b and c and _norm(c) in _INICIO and _CODIGO.search(str(b)):
            actual = {"codigo": _CODIGO.search(str(b)).group(1),
                      "nombre": re.sub(r"\s+", " ", str(b).split("\n")[0]).strip(" -"),
                      "unidad": str(d or "").strip(), "seccion": seccion, "datos": {}}
            bloques.append(actual)
            vistos = set()
        if actual is None:
            continue
        concepto, tipo = None, None
        if c:
            concepto = _buscar(c, _CONCEPTOS)
            if concepto:
                tipo = "precio" if concepto == "precio_sop" else _tipo_unidad(d)
            elif _buscar(c, _MINMAX):
                concepto, tipo = _buscar(c, _MINMAX), "cantidad"
        if concepto is None and b and _buscar(b, _MINMAX):
            concepto, tipo = _buscar(b, _MINMAX), "cantidad"
        if concepto is None or (concepto, tipo) in vistos:
            continue
        vistos.add((concepto, tipo))
        actual["datos"][(concepto, tipo)] = {
            mes: float(fila[j]) for j, mes in meses.items()
            if j < len(fila) and isinstance(fila[j], (int, float)) and not isinstance(fila[j], bool)}
    return bloques


def leer_corrida(archivo, elegidas=None):
    """{centro: {"hoja": nombre, "bloques": [...]}} de las dos plantas."""
    hojas = elegir_hojas(archivo, elegidas)
    return {centro: {"hoja": hojas[centro], "bloques": leer_bloques(archivo, hojas[centro], seccion)}
            for centro, _, seccion in CENTROS}
