"""Lectura de los Excel de precios S&OP: la plantilla mensual 'Precios SOP - <mes> <año>.xlsx' (hoja 'Precios',
ver gestion/precios_sop.py) y el Excel anterior '1. Precios S&OP Proyección 18 meses <Mes> <Año>.xlsx' (hojas
'Tar. Atocongo <Mes>' y 'Tar. Condorcocha <Mes>'), que se convierte a la plantilla."""

import datetime
import re
from collections import Counter

import openpyxl

from dashboard import queries as q
from gestion.precios_sop import COLUMNA_SUPUESTO, ENCABEZADO_FIJO, HOJA

from ._excel_utils import _filas_xlsx, decimal, texto

_CENTRO_POR_NOMBRE = {nombre.upper(): codigo for codigo, nombre in q.NOMBRES_CENTRO.items()}
_CODIGO_SAP = re.compile(r"^\d{2}-\d{8}$")


def _centro(valor):
    valor = texto(valor).upper()
    return _CENTRO_POR_NOMBRE.get(valor, valor)


def hojas_del_archivo(archivo):
    libro = openpyxl.load_workbook(archivo, read_only=True)
    try:
        return list(libro.sheetnames)
    finally:
        libro.close()


def leer_plantilla(archivo):
    """Una fila por celda de mes de la hoja 'Precios': material_codigo, centro,
    descripcion, unidad, mes (día 1), precio (Decimal; None si está vacía o es
    0) y supuesto. Incluye los meses reales (grises): el importador se queda
    con los desde la emisión."""
    filas = _filas_xlsx(archivo, HOJA)
    encabezado = next(filas, None) or ()
    nombres = [texto(v) if isinstance(v, str) else v for v in encabezado]
    if tuple(nombres[:len(ENCABEZADO_FIJO)]) != ENCABEZADO_FIJO:
        raise ValueError(f"la hoja '{HOJA}' debe empezar con las columnas {', '.join(ENCABEZADO_FIJO)}")
    meses = [(i, v.date().replace(day=1) if isinstance(v, datetime.datetime) else v.replace(day=1))
             for i, v in enumerate(encabezado) if isinstance(v, (datetime.datetime, datetime.date))]
    col_supuesto = next((i for i, v in enumerate(nombres) if v == COLUMNA_SUPUESTO), None)
    for fila in filas:
        if not fila or not any(v is not None and v != "" for v in fila):
            continue
        codigo = texto(str(fila[0])) if fila[0] is not None else ""
        if not codigo:
            continue
        supuesto = texto(str(fila[col_supuesto])) if col_supuesto is not None and col_supuesto < len(fila) and fila[col_supuesto] is not None else ""
        for indice, mes in meses:
            precio = decimal(fila[indice]) if indice < len(fila) else None
            yield {
                "material_codigo": codigo, "centro": _centro(fila[1]), "descripcion": texto(str(fila[2] or "")),
                "unidad": texto(str(fila[3] or "")), "mes": mes,
                "precio": precio if precio is not None and precio > 0 else None, "supuesto": supuesto[:255],
            }


# --- Excel anterior (hojas "Tar. Atocongo <Mes>" / "Tar. Condorcocha <Mes>") ---
_HOJAS_LEGADO = (("Tar. Atocongo", "U001"), ("Tar. Condorcocha", "U002"))
_PCT = re.compile(r"\(1\+([\d.]+%?)\)")


def _tipo_formula(formula, columna_anterior, fila):
    """Cómo se arma un precio del Excel anterior (para el Supuesto de la fila)."""
    if not (isinstance(formula, str) and formula.startswith("=")):
        return "valor"
    f = formula.replace("$", "").replace(" ", "")
    if re.fullmatch(rf"=\+?{columna_anterior}{fila}", f):
        return "repite"
    if "CÁLCULO" in f.upper() or "CALCULO" in f.upper():
        return "calculo"
    if "[" in f:
        return "externo"
    if "!" in f:
        return "otra_planta"
    if _PCT.search(f):
        return "ajuste"
    return "valor"


def _mes_corto(mes):
    return f"{('Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Set', 'Oct', 'Nov', 'Dic')[mes.month - 1]}-{mes.year % 100:02d}"


def _supuesto(formulas):
    """Texto del Supuesto de una fila a partir de las fórmulas de sus meses
    proyectados [(mes, fórmula, tipo)]: de dónde sale y los % de ajuste que
    estaban escritos dentro de las fórmulas (ahora a la vista)."""
    tipos = Counter(t for _, _, t in formulas)
    partes = []
    if tipos["calculo"]:
        partes.append("Cálculo por proveedor (hoja CÁLCULO: contrato, bono, premio, flete, humedad, descarga)")
    for mes, f, t in formulas:
        if t == "otra_planta":
            planta = "Condorcocha" if "Condorcocha" in f else "Atocongo"
            resto = re.sub(r"^=\+?'[^']+'!\$?[A-Z]+\$?\d+", "", f.replace(" ", ""))
            partes.append(f"Precio de {planta}{(' ' + resto.replace('-', '− ').replace('+', '+ ')) if resto else ''}")
            break
    if tipos["externo"]:
        partes.append("Vínculo a otro libro")
    if not partes:
        partes.append("Valor escrito")
    vistos = []
    for mes, f, _ in formulas:
        for pct in _PCT.findall(f.replace(" ", "")) if isinstance(f, str) else ():
            pct = pct if pct.endswith("%") else f"{float(pct) * 100:g}%"
            if pct not in [p for p, _ in vistos]:
                vistos.append((pct, mes))
    partes += [f"+{pct} desde {_mes_corto(mes)}" for pct, mes in vistos]
    return "; ".join(partes)[:255]


def leer_legado(archivo, desde):
    """Precios del Excel anterior por (material, centro): {"precios": {mes:
    S/}, "supuesto", "descripcion", "unidad"}. Toma los valores que Excel dejó
    calculados (el archivo tiene que haberse guardado en Excel) y arma el
    Supuesto con las fórmulas de los meses desde `desde` (la emisión). Un
    precio 0 o vacío = sin precio."""
    valores = openpyxl.load_workbook(archivo, data_only=True)
    formulas = openpyxl.load_workbook(archivo, data_only=False)
    salida = {}
    for prefijo, centro in _HOJAS_LEGADO:
        nombre = next((h for h in formulas.sheetnames if h.startswith(prefijo)), None)
        if nombre is None:
            raise ValueError(f"no tiene la hoja '{prefijo} <Mes>'")
        wf, wv = formulas[nombre], valores[nombre]
        # Fila de meses: la que más fechas tiene antes del primer Cod. SAP (la más
        # cercana a los materiales, debajo de las tarifas de servicios).
        primera = next((r for r in range(1, wf.max_row + 1) if _CODIGO_SAP.match(str(wf.cell(r, 3).value or "").strip())), None)
        if primera is None:
            raise ValueError(f"la hoja '{nombre}' no tiene materiales con Cod. SAP en la columna C")
        fila_mes, mejor = None, 0
        for r in range(1, primera):
            n = sum(isinstance(wf.cell(r, c).value, datetime.datetime) for c in range(1, wf.max_column + 1))
            if n and n >= mejor:
                fila_mes, mejor = r, n
        meses = {c: wf.cell(fila_mes, c).value.date().replace(day=1) for c in range(1, wf.max_column + 1)
                 if isinstance(wf.cell(fila_mes, c).value, datetime.datetime)}
        for r in range(fila_mes + 1, wf.max_row + 1):
            codigo = str(wf.cell(r, 3).value or "").strip()
            if not _CODIGO_SAP.match(codigo) or (codigo, centro) in salida:
                continue
            unidad = next((str(wf.cell(r, c).value).strip() for c in range(5, 10)
                           if isinstance(wf.cell(r, c).value, str) and wf.cell(r, c).value.strip().upper().startswith("S/")), "")
            precios, proyectadas = {}, []
            for c, mes in meses.items():
                v = wv.cell(r, c).value
                if isinstance(v, (int, float)) and v > 0:
                    precios[mes] = float(v)
                if mes >= desde and isinstance(v, (int, float)) and v > 0:
                    f = wf.cell(r, c).value
                    proyectadas.append((mes, f, _tipo_formula(f, openpyxl.utils.get_column_letter(c - 1), r)))
            salida[(codigo, centro)] = {
                "precios": precios, "supuesto": _supuesto(proyectadas) if proyectadas else "",
                "descripcion": str(wf.cell(r, 4).value or "").strip(),
                "unidad": unidad.replace("S/", "").strip(" /.") or unidad,
            }
    # Un material que en el Excel sigue con su código antiguo (Pelets Chips como
    # 07-00000008 Fierro de Terceros) pasa al código vigente, si el vigente no tiene fila propia.
    for (codigo, centro) in list(salida):
        nuevo = q.CODIGOS_ANTERIORES.get(codigo)
        if nuevo and (nuevo, centro) not in salida:
            dato = salida.pop((codigo, centro))
            dato["supuesto"] = f"Del código anterior {codigo} ({dato['descripcion']})" + (
                f"; {dato['supuesto']}" if dato["supuesto"] else "")
            salida[(nuevo, centro)] = dato
    return salida
