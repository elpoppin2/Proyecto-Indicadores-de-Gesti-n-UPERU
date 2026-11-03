import datetime
import os

from django.core.management.base import BaseCommand, CommandError
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from dashboard import queries as q
from dashboard.consultas.sop_base import _FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP
from gestion.models import PlanAbastecimientoSOP, PlanConsumoSOP
from gestion.precios_sop import _MESES_NOMBRE

from ._corrida_sop_excel import SECCIONES_SOP, leer_corrida
from ._sop_excel import leer_matriz_mensual, mes_snapshot_desde_nombre

_ORDEN_SECCION = {seccion: i for i, seccion in enumerate(SECCIONES_SOP)}


def nombre_archivo(mes):
    """'Consumos SOP - Setiembre 2026.xlsx' (el que lee import_consumo_sop)."""
    return f"Consumos SOP - {_MESES_NOMBRE[mes.month - 1]} {mes.year}.xlsx"


def _mes_anterior(mes):
    return (mes - datetime.timedelta(days=1)).replace(day=1)


class Command(BaseCommand):
    help = (
        "Arma 'Consumos SOP - <mes> <año>.xlsx' (formato de TABLA SOP CONSUMOS) desde la corrida de Planificación "
        "('UNACEM-PROY-INVENTARIOS SOP <Mes> <Año>.xlsx', hojas 'Mov. Inv Atocongo/Condor SOP ...'): la fila Consumo "
        "de cada material de Materias primas y Combustibles, desde el mes de emisión. Por defecto solo los materiales "
        "del S&OP que ya tiene el tablero (y sus códigos antiguos). Si el mes de emisión quedó con el consumo real "
        "(la corrida se actualizó al cerrar el mes), ese mes toma lo que planificó el S&OP anterior. No modifica la "
        "corrida y no reemplaza un archivo que ya existe (salvo --reemplazar)."
    )

    def add_arguments(self, parser):
        parser.add_argument("archivo", help="Corrida S&OP de Planificación")
        parser.add_argument("carpeta", help="Carpeta de salida (p.ej. ...\\DOCUMENTOS DESCARGA TABLAS MM\\TABLA SOP CONSUMOS)")
        parser.add_argument("--mes", help="Mes de emisión 'Octubre 2026' (por defecto, el del nombre del archivo)")
        parser.add_argument("--hoja-atocongo", help="Hoja de Atocongo, si el libro trae varias 'Mov. Inv' (escenarios)")
        parser.add_argument("--hoja-condorcocha", help="Hoja de Condorcocha, si el libro trae varias 'Mov. Inv'")
        parser.add_argument("--todos", action="store_true",
                            help="Todas las materias primas y combustibles de la corrida, no solo las del S&OP.")
        parser.add_argument("--reemplazar", action="store_true", help="Reemplaza el archivo si ya existe.")

    def handle(self, *args, **options):
        archivo, carpeta = options["archivo"], options["carpeta"]
        if not os.path.isfile(archivo):
            raise CommandError(f"No se encontró {archivo}")
        if not os.path.isdir(carpeta):
            raise CommandError(f"No existe la carpeta {carpeta}")
        mes = mes_snapshot_desde_nombre(options["mes"] or archivo)
        if mes is None:
            raise CommandError("No se reconoce el mes de emisión: indícalo con --mes 'Octubre 2026'.")
        salida = os.path.join(carpeta, nombre_archivo(mes))
        if os.path.exists(salida) and not options["reemplazar"]:
            raise CommandError(f"Ya existe {salida} (usa --reemplazar si quieres generarlo de nuevo).")
        try:
            corrida = leer_corrida(archivo, {"U001": options["hoja_atocongo"], "U002": options["hoja_condorcocha"]})
        except (ValueError, KeyError) as error:
            raise CommandError(f"{os.path.basename(archivo)}: {error}")
        universo = None if options["todos"] else _universo_sop()
        filas, fuera = _filas(corrida, mes, universo)
        repuestas = _reponer_mes_emision(filas, mes, carpeta)
        _escribir(salida, mes, filas, {
            "origen": os.path.basename(archivo), "hojas": {c: d["hoja"] for c, d in corrida.items()},
            "repuestas": repuestas, "fuera": fuera, "todos": options["todos"]})
        self.stdout.write(self.style.SUCCESS(
            f"{salida}: {len(filas)} materiales, hojas {corrida['U001']['hoja']!r} / {corrida['U002']['hoja']!r}."))
        for (material, centro), plan_corrida, anterior in repuestas:
            self.stdout.write(f"  {material} {centro}: {mes:%m-%Y} venía con el real ({plan_corrida:,.2f} T); "
                              + (f"se toma el S&OP anterior ({anterior:,.2f} T)." if anterior is not None
                                 else "sin S&OP anterior, queda vacío."))
        if fuera:
            self.stdout.write("  Fuera del S&OP (con consumo planificado): "
                              + ", ".join(f"{m} {c} {n}" for (m, c), n, _ in fuera))


def _codigos_nuevos():
    """{código antiguo: {códigos vigentes}} (CODIGOS_ANTERIORES y los que se partieron por proveedor)."""
    anteriores = {antiguo: {nuevo} for antiguo, nuevo in q.CODIGOS_ANTERIORES.items()}
    for antiguo, por_proveedor in q.CODIGOS_ANTERIORES_POR_PROVEEDOR.items():
        anteriores.setdefault(antiguo, set()).update(por_proveedor.values())
    return anteriores


def _universo_sop():
    """Materiales y centros de los planes S&OP ya cargados (abastecimiento y consumos), con sus códigos antiguos."""
    pares = set()
    for modelo in (PlanAbastecimientoSOP, PlanConsumoSOP):
        pares |= set(modelo.objects.values_list("material_codigo", "centro_destino").distinct())
    pares = {(m, c) for m, c in pares if not m.startswith(q.PREFIJO_SIN_CODIGO_SOP)}
    for antiguo, nuevos in _codigos_nuevos().items():
        pares |= {(antiguo, c) for m, c in list(pares) if m in nuevos}
    return pares


def _filas(corrida, mes, universo):
    """{(material, centro): {descripcion, destino, seccion, consumo {mes: negativo}}} y los materiales que quedan
    fuera del universo con algún consumo planificado [((material, centro), nombre, total)]."""
    catalogo = q.catalogo_materiales().drop_duplicates("material_codigo").set_index("material_codigo")["texto_breve"]
    filas, fuera = {}, []
    for centro, dato in corrida.items():
        for bloque in dato["bloques"]:
            if bloque["seccion"] not in SECCIONES_SOP:
                continue
            clave = (bloque["codigo"], centro)
            consumo = {m: -abs(v) for m, v in bloque["datos"].get(("consumo", "cantidad"), {}).items() if m >= mes}
            if universo is not None and clave not in universo:
                total = sum(-v for v in consumo.values())
                if total >= 1:
                    fuera.append((clave, bloque["nombre"], total))
                continue
            if clave in filas:  # el mismo código dos veces en la planta: vale el primero
                continue
            texto_breve = catalogo.get(bloque["codigo"])
            filas[clave] = {"descripcion": texto_breve if isinstance(texto_breve, str) and texto_breve.strip()
                            else bloque["nombre"],
                            "destino": q.NOMBRES_CENTRO.get(centro, centro), "seccion": bloque["seccion"],
                            "consumo": consumo}
    # Un código antiguo solo vale mientras la corrida no trae el vigente en esa planta (después queda como un
    # bloque más, con el plan que ya pasó al código nuevo).
    for antiguo, nuevos in _codigos_nuevos().items():
        for centro in {c for m, c in filas if m == antiguo}:
            if any((nuevo, centro) in filas for nuevo in nuevos):
                del filas[(antiguo, centro)]
    return filas, fuera


def _reponer_mes_emision(filas, mes, carpeta):
    """La corrida se sigue actualizando después del S&OP: si el mes de emisión trae exactamente el consumo real de
    MB51, ya no es el plan. Ese mes toma lo que planificó el S&OP anterior (archivo de la carpeta o lo cargado)."""
    if not filas:
        return []
    ids = ",".join(f"'{m}'" for m in sorted({m for m, _ in filas}))
    real = q._query(f"""
        SELECT material_codigo AS material, centro AS centro, -SUM(cantidad) AS consumo
        FROM gestion_movimientomaterial
        WHERE material_codigo IN ({ids}) AND +centro IN ('U001', 'U002') AND {_FILTRO_CLASE_MOVIMIENTO_CONSUMO_SOP}
              AND substr(fecha_contabilizacion, 1, 7) = %s
        GROUP BY material_codigo, centro
    """, [f"{mes:%Y-%m}"])
    real = {(r.material, r.centro): float(r.consumo or 0) for r in real.itertuples()}
    anterior = {}
    previo = os.path.join(carpeta, nombre_archivo(_mes_anterior(mes)))
    if os.path.exists(previo):
        anterior = {(c["material_codigo"], c["centro"]): float(c["valor"]) for c in leer_matriz_mensual(previo)
                    if c["mes"] == mes and c["valor"] is not None}
    else:
        anterior = {(p.material_codigo, p.centro_destino): -float(p.cantidad_consumo)
                    for p in PlanConsumoSOP.objects.filter(mes_snapshot=_mes_anterior(mes), mes_planificado=mes)
                    if p.cantidad_consumo is not None}
    repuestas = []
    for clave, fila in filas.items():
        plan, consumido = fila["consumo"].get(mes), real.get(clave)
        # Copia exacta del real (al centésimo): un plan que solo se parece al real no se toca.
        if plan is None or consumido is None or consumido < 1 or abs(-plan - consumido) > 0.01:
            continue
        nuevo = anterior.get(clave)
        if nuevo is None:
            del fila["consumo"][mes]
        else:
            fila["consumo"][mes] = nuevo
        repuestas.append((clave, -plan, None if nuevo is None else -nuevo))
    return repuestas


def _escribir(destino, mes, filas, nota):
    letra, negrita = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True)
    meses = sorted({m for f in filas.values() for m in f["consumo"]})
    wb = Workbook()
    ws = wb.active
    ws.title = "Consumos"
    ws.append(["Cod. SAP", "Centro Destino", "Material", "Destino",
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
            if m in fila["consumo"]:
                celda = ws.cell(i, j, round(fila["consumo"][m], 4))
                celda.font, celda.number_format = letra, "#,##0.00"
    for j, ancho in enumerate((13, 9, 40, 13), start=1):
        ws.column_dimensions[get_column_letter(j)].width = ancho
    for j in range(5, 5 + len(meses)):
        ws.column_dimensions[get_column_letter(j)].width = 11
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = f"A1:{get_column_letter(4 + len(meses))}{len(orden) + 1}"

    ayuda = wb.create_sheet("Cómo se armó")
    lineas = [
        (f"Consumos S&OP de {_MESES_NOMBRE[mes.month - 1].lower()} {mes.year}", True),
        ("Consumo planificado de cada material, en negativo (salida de almacén) y en la unidad de la corrida, "
         "desde el mes de emisión. Es la fila Consumo de la corrida de Planificación.", False),
        (f"Corrida: {nota['origen']} (hojas «{nota['hojas']['U001']}» y «{nota['hojas']['U002']}»).", False),
        ("Materiales: todas las materias primas y combustibles de la corrida." if nota["todos"] else
         "Materiales: los del S&OP que usa el tablero (abastecimiento y consumos ya cargados) y sus códigos antiguos.",
         False),
    ]
    for (material, centro), plan, anterior in nota["repuestas"]:
        lineas.append((f"{material} {centro}: {mes:%m-%Y} venía con el consumo real ({plan:,.2f}); "
                       + (f"se tomó el plan del S&OP anterior ({anterior:,.2f})." if anterior is not None
                          else "sin S&OP anterior, quedó vacío."), False))
    if nota["fuera"]:
        lineas.append(("Quedaron fuera (están en la corrida con consumo planificado, pero no en el S&OP del tablero): "
                       + "; ".join(f"{m} {c} {n}" for (m, c), n, _ in nota["fuera"]) + ".", False))
    lineas.append((f"Generado por el tablero el {datetime.date.today():%d/%m/%Y}. Para cargarlo, guárdalo en "
                   "DOCUMENTOS DESCARGA TABLAS MM\\TABLA SOP CONSUMOS.", False))
    for i, (texto, es_titulo) in enumerate(lineas, start=1):
        celda = ayuda.cell(i, 1, texto)
        celda.font = Font(name="Arial", size=12 if es_titulo else 10, bold=es_titulo)
        celda.alignment = Alignment(wrap_text=True, vertical="top")
    ayuda.column_dimensions["A"].width = 110
    wb.save(destino)
