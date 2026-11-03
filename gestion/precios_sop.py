"""Versión mensual de precios S&OP (pedido del usuario, 2026-11-03): armado y escritura de la plantilla
'Precios SOP - <mes> <año>.xlsx' que Planificación llena cada mes y deja en 'TABLA SOP PRECIOS'.

Hoja 'Precios' (fila 1 = encabezado, como las demás tablas S&OP):
  Cod. SAP | Centro | Material | Unidad | un mes por columna (fecha) ... | Supuesto
- Los meses anteriores a la emisión traen el precio real de compra del tablero (MB51 + FBL3N), en gris y
  bloqueados: son referencia y no se cargan.
- Desde el mes de emisión, el precio proyectado celda por celda (valor escrito, en azul): es lo que se carga.
- Supuesto: de dónde sale el precio de cada fila (contrato, cálculo por proveedor, % de ajuste...).
Lo usan los comandos convertir_precios_sop (desde el Excel anterior), plantilla_precios_sop (la del mes
siguiente) y la descarga de la pestaña Simulación."""

import datetime

from dashboard import queries as q
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Protection
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.protection import SheetProtection

HOJA = "Precios"
HOJA_AYUDA = "Cómo llenar"
CARPETA = "TABLA SOP PRECIOS"
ENCABEZADO_FIJO = ("Cod. SAP", "Centro", "Material", "Unidad")
COLUMNA_SUPUESTO = "Supuesto"
MESES_REALES = 12          # meses cerrados de referencia antes de la emisión
MESES_HORIZONTE = 18       # sin plan S&OP cargado: emisión + 17 meses ("Proyección 18 meses")
_MESES_NOMBRE = ("Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Setiembre",
                 "Octubre", "Noviembre", "Diciembre")
_ORDEN_CATEGORIA = {"MATERIAS PRIMAS": 0, "COMBUSTIBLES": 1}


def nombre_archivo(mes):
    """'Precios SOP - Octubre 2026.xlsx' (mismo estilo que 'Abastecimiento SOP - Setiembre 2026.xlsx')."""
    return f"Precios SOP - {_MESES_NOMBRE[mes.month - 1]} {mes.year}.xlsx"


def _meses(desde, cantidad):
    return [q._sumar_meses(desde, i) for i in range(cantidad)]


def armar_version(mes, proyectados, origen):
    """Contenido de la plantilla del S&OP `mes`.
    `proyectados`: {(material, centro): {"precios": {mes: S/}, "supuesto", "descripcion", "unidad"}} (lo que
    trae el Excel anterior o la versión previa). Filas: los materiales del S&OP de ese mes (si su plan está
    cargado) más los de `proyectados` que son de Materias Primas o Combustibles (alcance pedido por el usuario
    para empezar). Meses: desde la emisión hasta el último mes del plan S&OP (o 18 meses sin plan).
    Devuelve dict con mes, origen, meses_reales, meses_proyectados y filas."""
    catalogo = q.catalogo_materiales().drop_duplicates("material_codigo").set_index("material_codigo")
    pares, meses_plan = set(), []
    if mes in q.snapshots_sop_disponibles():
        plan = q.proyeccion_inventario_sop(mes)
        pares = {(m, c) for m, c in zip(plan["material_codigo"], plan["centro"])
                 if not m.startswith(q.PREFIJO_SIN_CODIGO_SOP)}
        meses_plan = sorted(set(plan["mes"]))
    if meses_plan:
        meses_proyectados = meses_plan
    else:
        fin = max((m for d in proyectados.values() for m in d["precios"] if m >= mes), default=None)
        meses_proyectados = [m for m in _meses(mes, MESES_HORIZONTE) if fin is None or m <= fin]
    # Fuera del S&OP, solo lo de Materias Primas o Combustibles con algún precio proyectado.
    for (material, centro), dato in proyectados.items():
        categoria = catalogo["categoria"].get(material) if material in catalogo.index else None
        if categoria in _ORDEN_CATEGORIA and any(dato["precios"].get(m) for m in meses_proyectados):
            pares.add((material, centro))
    meses_reales = _meses(q._sumar_meses(mes, -MESES_REALES), MESES_REALES)

    real = q.precios_entrada_sop_real()
    real = real[real["mes"].isin(meses_reales) & (real["precio"] > 0)]
    precio_real = {(m, c, mes_r): float(p) for m, c, mes_r, p in zip(real["material_codigo"], real["centro"], real["mes"], real["precio"])}

    filas = []
    for material, centro in pares:
        dato = proyectados.get((material, centro), {})
        en_catalogo = material in catalogo.index
        categoria = (catalogo["categoria"].get(material) if en_catalogo else None) or "MATERIAS PRIMAS"
        filas.append({
            "material_codigo": material, "centro": centro, "categoria": categoria,
            "descripcion": (catalogo["texto_breve"].get(material) if en_catalogo else None) or dato.get("descripcion", ""),
            "unidad": (catalogo["unidad_medida_base"].get(material) if en_catalogo else None) or dato.get("unidad", ""),
            "reales": {m: precio_real.get((material, centro, m)) for m in meses_reales},
            "proyectados": {m: dato.get("precios", {}).get(m) for m in meses_proyectados},
            "supuesto": dato.get("supuesto", ""),
        })
    filas.sort(key=lambda f: (f["centro"], _ORDEN_CATEGORIA.get(f["categoria"], 9), f["material_codigo"]))
    return {"mes": mes, "origen": origen, "meses_reales": meses_reales, "meses_proyectados": meses_proyectados,
            "filas": filas}


def proyectados_de_version(mes_base, mes_nuevo):
    """Precios de la versión `mes_base` para armar la del S&OP `mes_nuevo`:
    cada mes toma el de la versión anterior; los meses que esa versión no
    alcanza repiten su último precio. Base de la plantilla del mes siguiente."""
    df = q.precios_sop(mes_base)
    salida = {}
    for (material, centro), g in df.groupby(["material_codigo", "centro"]):
        g = g.sort_values("mes")
        precios = {m: float(p) for m, p in zip(g["mes"], g["precio"]) if p == p}
        ultimo = precios[max(precios)] if precios else None
        horizonte = _meses(mes_nuevo, MESES_HORIZONTE)
        supuestos = [s for s in g["supuesto"] if s]
        salida[(material, centro)] = {
            "precios": {m: precios.get(m, ultimo if m > max(precios, default=m) else None) for m in horizonte},
            "supuesto": max(set(supuestos), key=supuestos.count) if supuestos else "",
            "descripcion": g["descripcion"].iloc[0], "unidad": g["unidad"].iloc[0],
        }
    return salida


def escribir_plantilla(destino, version):
    """Escribe la plantilla (`version` de armar_version) en `destino` (ruta o archivo abierto)."""
    mes, meses_reales, meses_proy = version["mes"], version["meses_reales"], version["meses_proyectados"]
    letra = Font(name="Arial", size=10)
    negrita = Font(name="Arial", size=10, bold=True)
    gris = Font(name="Arial", size=10, color="808080")
    azul = Font(name="Arial", size=10, color="0000FF")
    fondo_cab, fondo_cab_proy = PatternFill("solid", fgColor="D9D9D9"), PatternFill("solid", fgColor="BDD7EE")
    fondo_real, fondo_proy = PatternFill("solid", fgColor="F2F2F2"), PatternFill("solid", fgColor="DDEBF7")
    editable = Protection(locked=False)

    wb = Workbook()
    ws = wb.active
    ws.title = HOJA
    encabezado = [*ENCABEZADO_FIJO, *[datetime.datetime(m.year, m.month, 1) for m in meses_reales + meses_proy],
                  COLUMNA_SUPUESTO]
    ws.append(encabezado)
    col_proy0 = len(ENCABEZADO_FIJO) + len(meses_reales) + 1
    col_sup = len(encabezado)
    for j, _ in enumerate(encabezado, start=1):
        celda = ws.cell(1, j)
        celda.font = negrita
        celda.fill = fondo_cab_proy if col_proy0 <= j < col_sup else fondo_cab
        celda.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        if len(ENCABEZADO_FIJO) < j < col_sup:
            celda.number_format = "mmm-yy"
    for i, f in enumerate(version["filas"], start=2):
        fijos = (f["material_codigo"], f["centro"], f["descripcion"], f["unidad"])
        for j, valor in enumerate(fijos, start=1):
            ws.cell(i, j, valor).font = letra
        for j, m in enumerate(meses_reales, start=len(ENCABEZADO_FIJO) + 1):
            celda = ws.cell(i, j, f["reales"].get(m))
            celda.font, celda.fill, celda.number_format = gris, fondo_real, "#,##0.00"
        for j, m in enumerate(meses_proy, start=col_proy0):
            valor = f["proyectados"].get(m)
            celda = ws.cell(i, j, valor)
            celda.font, celda.fill, celda.number_format, celda.protection = azul, fondo_proy, "#,##0.00", editable
        celda = ws.cell(i, col_sup, f["supuesto"] or None)
        celda.font, celda.protection = letra, editable
        celda.alignment = Alignment(wrap_text=False)
    ultima = max(len(version["filas"]) + 1, 2)
    if meses_proy:
        validacion = DataValidation(type="decimal", operator="greaterThanOrEqual", formula1="0", allow_blank=True,
                                    showErrorMessage=True, errorTitle="Precio no válido",
                                    error="Escribe el precio (un número mayor o igual a 0) o deja la celda vacía.")
        validacion.add(f"{get_column_letter(col_proy0)}2:{get_column_letter(col_sup - 1)}{ultima + 200}")
        ws.add_data_validation(validacion)
    for j, ancho in enumerate((13, 9, 42, 8), start=1):
        ws.column_dimensions[get_column_letter(j)].width = ancho
    for j in range(len(ENCABEZADO_FIJO) + 1, col_sup):
        ws.column_dimensions[get_column_letter(j)].width = 10.5
    ws.column_dimensions[get_column_letter(col_sup)].width = 60
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = ws.cell(2, len(ENCABEZADO_FIJO) + 1)
    ws.auto_filter.ref = f"A1:{get_column_letter(col_sup)}{ultima}"
    # Hoja protegida SIN clave: solo se escriben las celdas azules y el supuesto
    # (Revisar > Desproteger hoja para agregar un material).
    ws.protection = SheetProtection(sheet=True, formatCells=False, formatColumns=False, formatRows=False,
                                    insertRows=False, deleteRows=False, sort=False, autoFilter=False)

    ayuda = wb.create_sheet(HOJA_AYUDA)
    nombre = nombre_archivo(mes)
    lineas = [
        (f"Precios S&OP de {_MESES_NOMBRE[mes.month - 1].lower()} {mes.year}", True),
        ("", False),
        ("Celdas azules (desde el mes de emisión): el precio de compra proyectado de cada material y mes, en soles "
         "por la unidad de la columna Unidad. Escríbelo celda por celda; una celda vacía es un mes sin precio (el "
         "inventario de ese mes se valoriza a su precio promedio).", False),
        ("Celdas grises: precio real de compra de los meses cerrados (MB51 + FBL3N del tablero). Son solo "
         "referencia: no se cargan.", False),
        ("Supuesto: de dónde sale el precio de la fila (contrato, cálculo por proveedor, % de ajuste...). Se ve en "
         "el tablero junto a la versión.", False),
        (f"Guarda el archivo con el nombre «{nombre}» en DOCUMENTOS DESCARGA TABLAS MM\\{CARPETA}. "
         "Cada S&OP usa solo su propia versión de precios: un mes sin archivo queda sin valorizar.", False),
        ("La hoja Precios está protegida sin clave para no mover las columnas. Para agregar un material: Revisar > "
         "Desproteger hoja, agrega la fila con su Cod. SAP y Centro (U001 Atocongo, U002 Condorcocha).", False),
        ("", False),
        (f"Generada por el tablero el {datetime.date.today():%d/%m/%Y} desde {version['origen']}.", False),
    ]
    for i, (texto, es_titulo) in enumerate(lineas, start=1):
        celda = ayuda.cell(i, 1, texto or None)
        celda.font = Font(name="Arial", size=12 if es_titulo else 10, bold=es_titulo)
        celda.alignment = Alignment(wrap_text=True, vertical="top")
    ayuda.column_dimensions["A"].width = 110
    wb.save(destino)
