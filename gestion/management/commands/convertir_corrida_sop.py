import datetime
import os

from django.core.management.base import BaseCommand, CommandError
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from dashboard import queries as q
from dashboard.consultas.sop_base import _FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP
from gestion.models import PlanAbastecimientoSOP, PlanConsumoSOP, PlanTrasladoSOP
from gestion.precios_sop import _MESES_NOMBRE

from ._corrida_sop_excel import CENTROS, SECCIONES_SOP, leer_corrida
from ._sop_excel import leer_matriz_mensual, mes_snapshot_desde_nombre

_ORDEN_SECCION = {seccion: i for i, seccion in enumerate(SECCIONES_SOP)}
# Cada tipo de archivo: prefijo del nombre (el que leen import_consumo_sop, import_plan_abastecimiento_sop e
# import_traslados_sop), modelo con lo ya cargado, encabezado de las columnas 2 y 4, y signo en el Excel.
TIPOS = {
    "consumos": {"prefijo": "Consumos SOP", "modelo": PlanConsumoSOP, "campo": "cantidad_consumo",
                 "centro": "centro_destino", "columnas": ("Centro Destino", "Destino"), "signo": -1},
    "abastecimiento": {"prefijo": "Abastecimiento SOP", "modelo": PlanAbastecimientoSOP, "campo": "cantidad_plan",
                       "centro": "centro_destino", "columnas": ("Centro Destino", "Destino"), "signo": 1},
    "traslados": {"prefijo": "Traslados SOP", "modelo": PlanTrasladoSOP, "campo": "cantidad",
                  "centro": "centro_origen", "columnas": ("Centro Origen", "Centro Destino"), "signo": 1},
}


def nombre_archivo(tipo, mes):
    """'Consumos SOP - Setiembre 2026.xlsx', 'Abastecimiento SOP - …', 'Traslados SOP - …'."""
    return f"{TIPOS[tipo]['prefijo']} - {_MESES_NOMBRE[mes.month - 1]} {mes.year}.xlsx"


def _mes_anterior(mes):
    return (mes - datetime.timedelta(days=1)).replace(day=1)


class Command(BaseCommand):
    help = (
        "Arma desde la corrida de Planificación ('UNACEM-PROY-INVENTARIOS SOP <Mes> <Año>.xlsx', hojas 'Mov. Inv "
        "Atocongo/Condor SOP ...') los archivos que carga el tablero, desde el mes de emisión y solo para Materias "
        "primas y Combustibles: --consumos (fila Consumo, en TABLA SOP CONSUMOS), --abastecimiento (fila Compra / "
        "Prod-Ing; si el material no tiene compra, lo que entra por traslado; TABLA SOP ABASTECIMIENTO) y "
        "--traslados (fila Traslado de un material que también se compra: lo que sale hacia la otra planta; TABLA "
        "SOP TRASLADOS). Por defecto solo los materiales del S&OP que ya tiene el tablero (y sus códigos antiguos). "
        "Si el mes de emisión quedó con el real exacto de MB51 (la corrida se actualizó al cerrar el mes), ese mes "
        "toma lo que planificó el S&OP anterior. No modifica la corrida y no reemplaza archivos que ya existen "
        "(salvo --reemplazar)."
    )

    def add_arguments(self, parser):
        parser.add_argument("archivo", help="Corrida S&OP de Planificación")
        for tipo in TIPOS:
            parser.add_argument(f"--{tipo}", metavar="CARPETA", help=f"Carpeta donde dejar '{TIPOS[tipo]['prefijo']} - <mes> <año>.xlsx'")
        parser.add_argument("--mes", help="Mes de emisión 'Octubre 2026' (por defecto, el del nombre del archivo)")
        parser.add_argument("--hoja-atocongo", help="Hoja de Atocongo, si el libro trae varias 'Mov. Inv' (escenarios)")
        parser.add_argument("--hoja-condorcocha", help="Hoja de Condorcocha, si el libro trae varias 'Mov. Inv'")
        parser.add_argument("--todos", action="store_true",
                            help="Todas las materias primas y combustibles de la corrida, no solo las del S&OP.")
        parser.add_argument("--reemplazar", action="store_true", help="Reemplaza los archivos si ya existen.")

    def handle(self, *args, **options):
        archivo = options["archivo"]
        carpetas = {tipo: options[tipo] for tipo in TIPOS if options[tipo]}
        if not carpetas:
            raise CommandError("Indica al menos una salida: --consumos, --abastecimiento o --traslados.")
        if not os.path.isfile(archivo):
            raise CommandError(f"No se encontró {archivo}")
        mes = mes_snapshot_desde_nombre(options["mes"] or archivo)
        if mes is None:
            raise CommandError("No se reconoce el mes de emisión: indícalo con --mes 'Octubre 2026'.")
        for tipo, carpeta in carpetas.items():
            if not os.path.isdir(carpeta):
                raise CommandError(f"No existe la carpeta {carpeta}")
            salida = os.path.join(carpeta, nombre_archivo(tipo, mes))
            if os.path.exists(salida) and not options["reemplazar"]:
                raise CommandError(f"Ya existe {salida} (usa --reemplazar si quieres generarlo de nuevo).")
        try:
            corrida = leer_corrida(archivo, {"U001": options["hoja_atocongo"], "U002": options["hoja_condorcocha"]})
        except (ValueError, KeyError) as error:
            raise CommandError(f"{os.path.basename(archivo)}: {error}")
        hojas = {c: d["hoja"] for c, d in corrida.items()}
        repuestas_abastecimiento = set()
        for tipo in TIPOS:  # en orden: los traslados siguen lo que se repuso en el abastecimiento
            if tipo not in carpetas:
                continue
            carpeta = carpetas[tipo]
            universo = None if options["todos"] else _universo_sop(tipo)
            if tipo == "traslados":
                filas, fuera = _traslados(corrida, mes, universo), []
                repuestas = _reponer_traslados(filas, mes, carpeta, repuestas_abastecimiento)
            else:
                filas, fuera = _filas(corrida, mes, universo, tipo)
                repuestas = _reponer_mes_emision(filas, mes, carpeta, tipo)
                if tipo == "abastecimiento":
                    repuestas_abastecimiento = {clave for clave, _, _ in repuestas}
            salida = os.path.join(carpeta, nombre_archivo(tipo, mes))
            _escribir(salida, tipo, mes, filas, {"origen": os.path.basename(archivo), "hojas": hojas,
                                                  "repuestas": repuestas, "fuera": fuera, "todos": options["todos"]})
            self.stdout.write(self.style.SUCCESS(f"{salida}: {len(filas)} filas."))
            for (material, centro), anterior_corrida, anterior in repuestas:
                self.stdout.write(f"  {material} {centro}: {mes:%m-%Y} venía con el real ({anterior_corrida:,.2f}); "
                                  + (f"se toma el S&OP anterior ({anterior:,.2f})." if anterior is not None
                                     else "sin S&OP anterior, queda vacío."))
            if fuera:
                self.stdout.write("  Fuera del S&OP: " + ", ".join(f"{m} {c} {n}" for (m, c), n, _ in fuera))
        self.stdout.write(f"Hojas: {hojas['U001']!r} / {hojas['U002']!r}.")


def _codigos_nuevos():
    """{código antiguo: {códigos vigentes}} (CODIGOS_ANTERIORES y los que se partieron por proveedor)."""
    anteriores = {antiguo: {nuevo} for antiguo, nuevo in q.CODIGOS_ANTERIORES.items()}
    for antiguo, por_proveedor in q.CODIGOS_ANTERIORES_POR_PROVEEDOR.items():
        anteriores.setdefault(antiguo, set()).update(por_proveedor.values())
    return anteriores


def _universo_sop(tipo):
    """Materiales y centros de los planes S&OP ya cargados, con sus códigos antiguos: para consumos, los de
    abastecimiento y consumos; para abastecimiento y traslados, solo los de abastecimiento (el Carbón Terceros
    Chancado 14-00001112 se consume, pero se abastece como 06-00000005: su Prod / Ing no es una compra)."""
    pares = set()
    for modelo in (PlanAbastecimientoSOP, PlanConsumoSOP) if tipo == "consumos" else (PlanAbastecimientoSOP,):
        pares |= set(modelo.objects.values_list("material_codigo", "centro_destino").distinct())
    pares = {(m, c) for m, c in pares if not m.startswith(q.PREFIJO_SIN_CODIGO_SOP)}
    for antiguo, nuevos in _codigos_nuevos().items():
        pares |= {(antiguo, c) for m, c in list(pares) if m in nuevos}
    return pares


def _serie(bloque, tipo):
    """Cantidades del bloque para el archivo `tipo`, en positivo. Abastecimiento = la compra (Prod / Ing en
    Condorcocha); un material sin fila de compra (Caliza Roca Extraída en Atocongo) se abastece por traslado."""
    datos = bloque["datos"]
    if tipo == "consumos":
        return {m: abs(v) for m, v in datos.get(("consumo", "cantidad"), {}).items()}
    if ("compra", "cantidad") in datos:
        return {m: abs(v) for m, v in datos[("compra", "cantidad")].items()}
    return {m: abs(v) for m, v in datos.get(("traslado", "cantidad"), {}).items()}


def _descripcion(catalogo, bloque):
    texto_breve = catalogo.get(bloque["codigo"])
    return texto_breve if isinstance(texto_breve, str) and texto_breve.strip() else bloque["nombre"]


def _sin_codigos_antiguos(filas):
    """Un código antiguo solo vale mientras la corrida no trae el vigente en esa planta (después queda como un
    bloque más, con el plan que ya pasó al código nuevo)."""
    for antiguo, nuevos in _codigos_nuevos().items():
        for centro in {c for m, c in filas if m == antiguo}:
            if any((nuevo, centro) in filas for nuevo in nuevos):
                del filas[(antiguo, centro)]
    return filas


def _catalogo():
    return q.catalogo_materiales().drop_duplicates("material_codigo").set_index("material_codigo")["texto_breve"]


def _filas(corrida, mes, universo, tipo):
    """{(material, centro): {descripcion, destino, seccion, valores {mes: cantidad positiva}}} y los materiales que
    quedan fuera del universo con alguna cantidad planificada [((material, centro), nombre, total)]."""
    catalogo = _catalogo()
    filas, fuera = {}, []
    for centro, dato in corrida.items():
        for bloque in dato["bloques"]:
            if bloque["seccion"] not in SECCIONES_SOP:
                continue
            clave = (bloque["codigo"], centro)
            valores = {m: v for m, v in _serie(bloque, tipo).items() if m >= mes}
            if universo is not None and clave not in universo:
                if sum(valores.values()) >= 1:
                    fuera.append((clave, bloque["nombre"], sum(valores.values())))
                continue
            if clave in filas:  # el mismo código dos veces en la planta: vale el primero
                continue
            filas[clave] = {"descripcion": _descripcion(catalogo, bloque), "destino": q.NOMBRES_CENTRO.get(centro, centro),
                            "seccion": bloque["seccion"], "valores": valores}
    return _sin_codigos_antiguos(filas), fuera


def _traslados(corrida, mes, universo):
    """Salidas hacia la otra planta: la fila Traslado de un material que también tiene compra en ese centro
    (Carbón Importado y Carbón Trujillo en Atocongo). {(material, centro que envía): {..., destino = centro que
    recibe}}. La corrida no siempre usa el mismo signo: cuenta la cantidad. Un material que el tablero traslada
    por regla (_TRASLADOS_SOP) y que la corrida no traslada va en cero: hasta abr-2025 el Carbón Importado
    llegaba a Condorcocha directo del puerto, sin pasar por el stock de Atocongo."""
    por_regla = {(material, envia) for (material, _), envia in q._TRASLADOS_SOP.items()}
    catalogo = _catalogo()
    otro = {c: [o for o, _, _ in CENTROS if o != c][0] for c, _, _ in CENTROS}
    filas = {}
    for centro, dato in corrida.items():
        for bloque in dato["bloques"]:
            if bloque["seccion"] not in SECCIONES_SOP or ("compra", "cantidad") not in bloque["datos"]:
                continue
            clave, destino = (bloque["codigo"], centro), otro[centro]
            if universo is not None and (clave not in universo or (bloque["codigo"], destino) not in universo):
                continue
            if clave in filas:
                continue
            valores = {m: abs(v) for m, v in bloque["datos"].get(("traslado", "cantidad"), {}).items() if m >= mes}
            if not any(valores.values()):
                if clave not in por_regla:
                    continue
                valores = {m: 0.0 for m in bloque["datos"][("compra", "cantidad")] if m >= mes}
            filas[clave] = {"descripcion": _descripcion(catalogo, bloque), "destino": destino,
                            "seccion": bloque["seccion"], "valores": valores}
    return _sin_codigos_antiguos(filas)


def _real_del_mes(materiales, mes, tipo):
    """Real de MB51 del mes por (material, centro), en positivo: consumo (mismas clases que Planificación) o
    entradas por compra (101 menos 102)."""
    ids = ",".join(f"'{m}'" for m in sorted(materiales))
    if tipo == "consumos":
        cantidad, filtro = "-SUM(cantidad)", _FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP
    else:
        cantidad = "SUM(CASE WHEN clase_movimiento = '101' THEN cantidad ELSE -ABS(cantidad) END)"
        filtro = "clase_movimiento IN ('101', '102')"
    df = q._query(f"""
        SELECT material_codigo AS material, centro AS centro, {cantidad} AS cantidad
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({ids}) AND +centro IN ('U001', 'U002') AND {filtro}
              AND substr(fecha_contabilizacion, 1, 7) = %s
        GROUP BY material_codigo, centro
    """, [f"{mes:%Y-%m}"])
    return {(r.material, r.centro): float(r.cantidad or 0) for r in df.itertuples()}


def _plan_anterior(tipo, mes, carpeta):
    """Lo que el S&OP anterior planificó para `mes`, por (material, centro), en positivo: el archivo del mes
    anterior en la misma carpeta o, si no está, lo cargado en el tablero."""
    previo = os.path.join(carpeta, nombre_archivo(tipo, _mes_anterior(mes)))
    if os.path.exists(previo):
        return {(c["material_codigo"], c["centro"]): abs(float(c["valor"])) for c in leer_matriz_mensual(previo)
                if c["mes"] == mes and c["valor"] is not None}
    t = TIPOS[tipo]
    return {(getattr(p, "material_codigo"), getattr(p, t["centro"])): abs(float(getattr(p, t["campo"])))
            for p in t["modelo"].objects.filter(mes_snapshot=_mes_anterior(mes), mes_planificado=mes)
            if getattr(p, t["campo"]) is not None}


def _reponer_mes_emision(filas, mes, carpeta, tipo):
    """La corrida se sigue actualizando después del S&OP: si el mes de emisión trae exactamente el real de MB51
    (al centésimo), ya no es el plan. Ese mes toma lo que planificó el S&OP anterior. Un plan que solo se parece
    al real no se toca. Devuelve [((material, centro), valor de la corrida, valor puesto o None)]."""
    if not filas:
        return []
    real = _real_del_mes({m for m, _ in filas}, mes, tipo)
    anterior = None
    repuestas = []
    for clave, fila in filas.items():
        plan, hubo = fila["valores"].get(mes), real.get(clave)
        if plan is None or hubo is None or hubo < 1 or abs(plan - hubo) > 0.01:
            continue
        if anterior is None:
            anterior = _plan_anterior(tipo, mes, carpeta)
        nuevo = anterior.get(clave)
        if nuevo is None:
            del fila["valores"][mes]
        else:
            fila["valores"][mes] = nuevo
        repuestas.append((clave, plan, nuevo))
    return repuestas


def _reponer_traslados(filas, mes, carpeta, repuestas_abastecimiento):
    """Si el abastecimiento del centro que recibe se repuso en el mes de emisión, el traslado de ese mes también
    toma el del S&OP anterior (para que salga lo mismo que llega)."""
    if not repuestas_abastecimiento:
        return []
    anterior = _plan_anterior("traslados", mes, carpeta)
    repuestas = []
    for clave, fila in filas.items():
        if (clave[0], fila["destino"]) not in repuestas_abastecimiento or mes not in fila["valores"]:
            continue
        plan, nuevo = fila["valores"][mes], anterior.get(clave)
        if nuevo is None:
            del fila["valores"][mes]
        else:
            fila["valores"][mes] = nuevo
        repuestas.append((clave, plan, nuevo))
    return repuestas


def _escribir(destino, tipo, mes, filas, nota):
    t = TIPOS[tipo]
    letra, negrita = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True)
    meses = sorted({m for f in filas.values() for m in f["valores"]})
    wb = Workbook()
    ws = wb.active
    ws.title = t["prefijo"].split()[0]
    ws.append(["Cod. SAP", t["columnas"][0], "Material", t["columnas"][1],
               *[datetime.datetime(m.year, m.month, 1) for m in meses]])
    for j in range(1, 5 + len(meses)):
        celda = ws.cell(1, j)
        celda.font, celda.fill = negrita, PatternFill("solid", fgColor="D9D9D9")
        celda.alignment = Alignment(horizontal="center", vertical="center")
        if j > 4:
            celda.number_format = "mmm-yy"
    orden = sorted(filas, key=lambda k: (k[1], _ORDEN_SECCION.get(filas[k]["seccion"], 9), k[0]))
    for i, (material, centro) in enumerate(orden, start=2):
        fila = filas[(material, centro)]
        for j, valor in enumerate((material, centro, fila["descripcion"], fila["destino"]), start=1):
            ws.cell(i, j, valor).font = letra
        for j, m in enumerate(meses, start=5):
            if m in fila["valores"]:
                celda = ws.cell(i, j, round(t["signo"] * fila["valores"][m], 4))
                celda.font, celda.number_format = letra, "#,##0.00"
    for j, ancho in enumerate((13, 13, 40, 14), start=1):
        ws.column_dimensions[get_column_letter(j)].width = ancho
    for j in range(5, 5 + len(meses)):
        ws.column_dimensions[get_column_letter(j)].width = 11
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = f"A1:{get_column_letter(4 + len(meses))}{len(orden) + 1}"

    que = {
        "consumos": "Consumo planificado de cada material, en negativo (salida de almacén) y en la unidad de la "
                    "corrida, desde el mes de emisión. Es la fila Consumo de la corrida.",
        "abastecimiento": "Abastecimiento planificado de cada material y centro (compra o, en Condorcocha, Prod / Ing; "
                          "lo recibido por traslado también cuenta aquí), desde el mes de emisión. Un material sin fila "
                          "de compra se abastece por traslado (fila Traslado).",
        "traslados": "Lo que cada centro envía a la otra planta (fila Traslado de un material que también se compra), "
                     "desde el mes de emisión. El centro que recibe ya lo tiene en su abastecimiento.",
    }[tipo]
    lineas = [
        (f"{t['prefijo'].split()[0]} S&OP de {_MESES_NOMBRE[mes.month - 1].lower()} {mes.year}", True),
        (que, False),
        (f"Corrida: {nota['origen']} (hojas «{nota['hojas']['U001'].strip()}» y «{nota['hojas']['U002'].strip()}»).", False),
        ("Materiales: todas las materias primas y combustibles de la corrida." if nota["todos"] else
         "Materiales: los del S&OP que usa el tablero (abastecimiento y consumos ya cargados) y sus códigos antiguos.",
         False),
    ]
    for (material, centro), plan, anterior in nota["repuestas"]:
        lineas.append((f"{material} {centro}: {mes:%m-%Y} venía con el real ({plan:,.2f}); "
                       + (f"se tomó el plan del S&OP anterior ({anterior:,.2f})." if anterior is not None
                          else "sin S&OP anterior, quedó vacío."), False))
    if nota["fuera"]:
        lineas.append(("Quedaron fuera (están en la corrida, pero no en el S&OP del tablero): "
                       + "; ".join(f"{m} {c} {n}" for (m, c), n, _ in nota["fuera"]) + ".", False))
    carpeta = {"consumos": "TABLA SOP CONSUMOS", "abastecimiento": "TABLA SOP ABASTECIMIENTO",
               "traslados": "TABLA SOP TRASLADOS"}[tipo]
    lineas.append((f"Generado por el tablero el {datetime.date.today():%d/%m/%Y}. Para cargarlo, guárdalo en "
                   f"DOCUMENTOS DESCARGA TABLAS MM\\{carpeta}.", False))
    ayuda = wb.create_sheet("Cómo se armó")
    for i, (texto, es_titulo) in enumerate(lineas, start=1):
        celda = ayuda.cell(i, 1, texto)
        celda.font = Font(name="Arial", size=12 if es_titulo else 10, bold=es_titulo)
        celda.alignment = Alignment(wrap_text=True, vertical="top")
    ayuda.column_dimensions["A"].width = 110
    wb.save(destino)
