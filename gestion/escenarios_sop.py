"""Escenarios de simulación del movimiento de inventario S&OP (pedido del
usuario, 2026-11-11: "simular o tener estas celdas solamente con
abastecimiento (compras) y traslados, dado que los consumos lo hace
producción"; "guardar escenarios e inclusive tener un botón que reemplace el
archivo original, una vez validado").

Un escenario guarda solo las celdas cambiadas, cada una con el valor del S&OP
cuando se cambió ("base") y el nuevo ("valor"), en toneladas:
- compras: "material|centro|AAAA-MM" -> lo que se compra (fila Abastecimiento).
- traslados: "material|centro que envía|centro que recibe|AAAA-MM" -> lo que se
  traslada (fila Traslado recibido de quien recibe y Traslados a otro centro de
  quien envía: es el mismo número).
El recálculo del inventario lo hace el navegador (js/simulacion_inventario.js);
aquí se guardan los escenarios (datos_locales/escenarios_sop.json, fuera del
repositorio) y se aplican a los archivos del S&OP vigente ('Abastecimiento SOP -
<mes> <año>.xlsx' y, con traslados entre sus dos centros, 'Traslados SOP - <mes>
<año>.xlsx', que se crea si no está), con respaldo previo en _RETIRADOS, recarga
y verificación.
"""

import datetime
import io
import json
import math
import os
import re
import shutil
import threading
import uuid
from copy import copy as copiar_estilo
from pathlib import Path

import openpyxl
from django.conf import settings
from django.core.management import call_command
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from dashboard import queries as q
from gestion.management.commands._excel_utils import texto
from gestion.models import ArchivoImportado, PlanTrasladoSOP

ARCHIVO = Path(settings.BASE_DIR) / "datos_locales" / "escenarios_sop.json"
CARPETA_ABASTECIMIENTO = "TABLA SOP ABASTECIMIENTO"
CARPETA_TRASLADOS = "TABLA SOP TRASLADOS"
CARPETA_RESPALDOS = "_RETIRADOS"
# Menos de media tonelada no es un cambio (mismo umbral que la tabla, que muestra "—").
TOLERANCIA = 0.5
MAX_CELDAS = 5000
_CANDADO = threading.Lock()
_CANDADO_APLICAR = threading.Lock()
_CLAVE_COMPRAS = re.compile(r"^[^|]{1,40}\|U\d{3}\|\d{4}-\d{2}$")
_CLAVE_TRASLADO = re.compile(r"^[^|]{1,40}\|U\d{3}\|U\d{3}\|\d{4}-\d{2}$")


class ErrorEscenario(Exception):
    """Algo que el usuario puede corregir: el mensaje se le muestra tal cual."""


# --- Reglas de la tabla Movimiento de inventario ------------------------------
# Lo que un centro recibe solo por traslado: todo su abastecimiento del S&OP es
# traslado recibido, no compra. {(material, centro que recibe): centro que envía}.
# Clinker y Carbón Importado de Condorcocha (desde Atocongo) y la Caliza Roca
# Extraída de Atocongo, que llega toda por 641 desde U002 (pedido del usuario,
# 2026-11-11: "es netamente traslado entre centros").
SOLO_TRASLADO = {
    **{(m, q.CENTRO_CONDORCOCHA): q.CENTRO_ATOCONGO for m in q.MATERIALES_TRASLADO_ATOCONGO_CONDORCOCHA},
    ("14-00000024", q.CENTRO_ATOCONGO): q.CENTRO_CONDORCOCHA,
}
# Lo que un centro recibe por compra o por traslado (pedido del usuario,
# 2026-11-11: el Carbón Trujillo de Condorcocha "puede ser abastecimiento y
# traslado entre centros"): van las dos filas, y la de quien envía, aunque el
# S&OP no traiga el traslado, y el traslado se puede simular.
COMPRA_O_TRASLADO = {
    ("06-00001601", q.CENTRO_CONDORCOCHA): q.CENTRO_ATOCONGO,
}


def solo_traslado(material, centro):
    return (material, centro) in SOLO_TRASLADO


def compra_o_traslado(material, centro):
    return (material, centro) in COMPRA_O_TRASLADO


def movimientos_por_celda(base):
    """{(material, centro, mes): {"compras", "recibido", "traslado"}} de una
    proyección (q.proyeccion_inventario_sop): el abastecimiento partido en
    compras y traslado recibido, con la misma regla que la tabla."""
    celdas = {}
    for r in base.itertuples():
        abastecimiento = float(r.abastecimiento)
        recibido = abastecimiento if solo_traslado(r.material_codigo, r.centro) else float(r.traslado_recibido)
        celdas[(r.material_codigo, r.centro, r.mes)] = {
            "compras": abastecimiento - recibido, "recibido": recibido, "traslado": float(r.traslado)}
    return celdas


def pares_traslado(base):
    """({(material, centro que recibe): centro que envía},
        {(material, centro que envía): centro que recibe}) de una proyección.
    Quien recibe sabe de dónde le llega por la regla del S&OP
    (q._TRASLADOS_SOP), por SOLO_TRASLADO / COMPRA_O_TRASLADO o por el único
    otro centro que traslada ese material. Sin par claro queda vacío y esa
    fila no se edita. Quien envía según COMPRA_O_TRASLADO tiene su par aunque
    el S&OP no le planifique traslados."""
    origen_fijo = {**COMPRA_O_TRASLADO, **SOLO_TRASLADO}
    claves = set(zip(base["material_codigo"], base["centro"]))
    recibe, envia = set(), set()
    for r in base.itertuples():
        if r.traslado >= TOLERANCIA:
            envia.add((r.material_codigo, r.centro))
        if r.traslado_recibido >= TOLERANCIA or (r.material_codigo, r.centro) in origen_fijo:
            recibe.add((r.material_codigo, r.centro))
    envia |= {(m, origen) for (m, c), origen in COMPRA_O_TRASLADO.items() if (m, c) in claves and (m, origen) in claves}
    de_quien = {}
    for material, centro in recibe:
        otros = {c for m, c in envia if m == material and c != centro}
        de_quien[(material, centro)] = (q._TRASLADOS_SOP.get((material, centro))
                                        or origen_fijo.get((material, centro))
                                        or (next(iter(otros)) if len(otros) == 1 else ""))
    a_quien = {}
    for material, centro in envia:
        destinos = {c for (m, c), origen in de_quien.items() if m == material and origen == centro}
        a_quien[(material, centro)] = next(iter(destinos)) if len(destinos) == 1 else ""
    return de_quien, a_quien


# --- Guardado -------------------------------------------------------------------
def _normalizar(cambios):
    """Una compra en lo que ahora llega solo por traslado (SOLO_TRASLADO: la
    Caliza Roca Extraída de Atocongo pasó ahí el 2026-11-11, con escenarios ya
    guardados como compra) pasa a ser su traslado recibido: al archivo va el
    mismo número, el abastecimiento de quien recibe. Si la celda ya tiene su
    traslado, vale ese: es la fila que se ve en la tabla."""
    for clave in list(cambios["compras"]):
        material, centro, mes = clave.split("|")
        envia = SOLO_TRASLADO.get((material, centro))
        if envia:
            cambios["traslados"].setdefault(f"{material}|{envia}|{centro}|{mes}", cambios["compras"].pop(clave))
    return cambios


def _leer():
    try:
        escenarios = json.loads(ARCHIVO.read_text(encoding="utf-8")).get("escenarios", [])
    except FileNotFoundError:
        return []
    for e in escenarios:
        _normalizar(e["cambios"])
    return escenarios


def _escribir(escenarios):
    ARCHIVO.parent.mkdir(exist_ok=True)
    tmp = Path(str(ARCHIVO) + ".tmp")
    tmp.write_text(json.dumps({"escenarios": escenarios}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, ARCHIVO)


def listar(sop=None):
    """Escenarios guardados (de un S&OP, AAAA-MM-DD), el más reciente primero."""
    with _CANDADO:
        escenarios = _leer()
    if sop:
        escenarios = [e for e in escenarios if e["sop"] == sop]
    return sorted(escenarios, key=lambda e: e["modificado"], reverse=True)


def _numero(valor):
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(valor) or abs(valor) > 1e9:
        raise ErrorEscenario("El escenario trae un valor que no es un número.")
    return round(float(valor), 4)


def _limpiar_cambios(cambios):
    """Cambios válidos {compras: {...}, traslados: {...}}; ErrorEscenario si no."""
    if not isinstance(cambios, dict):
        raise ErrorEscenario("El escenario no trae cambios.")
    limpio = {"compras": {}, "traslados": {}}
    for tipo, patron in (("compras", _CLAVE_COMPRAS), ("traslados", _CLAVE_TRASLADO)):
        for clave, celda in (cambios.get(tipo) or {}).items():
            if not patron.match(str(clave)) or not isinstance(celda, dict):
                raise ErrorEscenario(f"Celda no válida en el escenario: {clave}")
            limpio[tipo][clave] = {"base": _numero(celda.get("base")), "valor": _numero(celda.get("valor"))}
    if sum(len(v) for v in limpio.values()) > MAX_CELDAS:
        raise ErrorEscenario(f"El escenario cambia más de {MAX_CELDAS:,} celdas.")
    return _normalizar(limpio)


def guardar(datos, usuario):
    """Crea o reemplaza un escenario: {id (opcional), nombre, sop, cambios}."""
    nombre = " ".join(str(datos.get("nombre") or "").split())[:80]
    if not nombre:
        raise ErrorEscenario("Escribe un nombre para el escenario.")
    try:
        sop = datetime.date.fromisoformat(str(datos.get("sop"))).isoformat()
    except ValueError:
        raise ErrorEscenario("El escenario no indica su S&OP.")
    cambios = _limpiar_cambios(datos.get("cambios"))
    ahora = datetime.datetime.now().isoformat(timespec="seconds")
    with _CANDADO:
        escenarios = _leer()
        actual = next((e for e in escenarios if e["id"] == datos.get("id")), None) if datos.get("id") else None
        if actual is None and any(e["sop"] == sop and e["nombre"].lower() == nombre.lower() for e in escenarios):
            raise ErrorEscenario(f"Ya hay un escenario «{nombre}» en este S&OP: elígelo para guardarlo encima o usa otro nombre.")
        if actual is None:
            actual = {"id": uuid.uuid4().hex[:12], "sop": sop, "autor": usuario.username, "creado": ahora}
            escenarios.append(actual)
        elif actual["sop"] != sop:
            raise ErrorEscenario("Ese escenario es de otro S&OP.")
        actual.update({"nombre": nombre, "cambios": cambios, "modificado": ahora, "modificado_por": usuario.username})
        _escribir(escenarios)
    return actual


def eliminar(id_escenario, usuario, puede_editar):
    with _CANDADO:
        escenarios = _leer()
        actual = next((e for e in escenarios if e["id"] == id_escenario), None)
        if actual is None:
            raise ErrorEscenario("Ese escenario ya no existe.")
        if actual["autor"] != usuario.username and not puede_editar:
            raise ErrorEscenario("Solo quien lo creó o un administrador puede eliminar este escenario.")
        _escribir([e for e in escenarios if e["id"] != id_escenario])


# --- Aplicar al archivo del S&OP -----------------------------------------------
def _archivo_cargado(sop, carpeta):
    from gestion.management.commands._sop_excel import mes_snapshot_desde_nombre
    for registro in ArchivoImportado.objects.filter(ruta__icontains=carpeta).order_by("-fecha_importado"):
        if mes_snapshot_desde_nombre(registro.ruta) == sop and os.path.isfile(registro.ruta):
            return registro.ruta
    return None


def archivo_abastecimiento(sop):
    """Ruta del 'Abastecimiento SOP - <mes> <año>.xlsx' cargado para ese S&OP
    (el registro de cargas guarda dónde está), o None."""
    return _archivo_cargado(sop, CARPETA_ABASTECIMIENTO)


def archivo_traslados(sop):
    """Ruta del 'Traslados SOP - <mes> <año>.xlsx' cargado para ese S&OP, o None."""
    return _archivo_cargado(sop, CARPETA_TRASLADOS)


def ruta_traslados(sop):
    """Dónde está o va el archivo de traslados del S&OP: el cargado o, si no
    tiene, TABLA SOP TRASLADOS junto a la carpeta de su abastecimiento."""
    from gestion.management.commands.convertir_corrida_sop import nombre_archivo
    cargado = archivo_traslados(sop)
    if cargado:
        return cargado
    abastecimiento = archivo_abastecimiento(sop)
    if not abastecimiento:
        return None
    return os.path.join(os.path.dirname(os.path.dirname(abastecimiento)), CARPETA_TRASLADOS,
                        nombre_archivo("traslados", sop))


def pares_archivo_traslados(base):
    """Traslados que van en el archivo de traslados del S&OP, como los de la
    corrida: los de q._TRASLADOS_SOP y COMPRA_O_TRASLADO con los dos centros en
    la proyección (Carbón Importado y Carbón Trujillo, de Atocongo a
    Condorcocha). {(material, centro que envía): centro que recibe}. Lo que
    llega solo por traslado desde un centro que el S&OP no planifica (Clinker
    desde Atocongo, Caliza Roca Extraída desde U002) no va: su envío sumaría a
    la proyección el stock de ese centro; queda en el abastecimiento de quien
    recibe (SOLO_TRASLADO)."""
    claves = set(zip(base["material_codigo"], base["centro"]))
    return {(m, envia): recibe for (m, recibe), envia in {**q._TRASLADOS_SOP, **COMPRA_O_TRASLADO}.items()
            if (m, recibe) in claves and (m, envia) in claves}


def motivo_no_aplicable(sop):
    """Por qué no se puede aplicar un escenario a ese S&OP ("" si se puede).
    Solo el S&OP vigente: los anteriores son la historia del cumplimiento."""
    disponibles = q.snapshots_sop_disponibles()
    if not disponibles or sop != disponibles[-1]:
        return "Solo se aplica al S&OP vigente: los anteriores quedan como historia del cumplimiento."
    if not archivo_abastecimiento(sop):
        return "No se encontró el archivo de abastecimiento cargado de este S&OP."
    return ""


def _mes(texto_mes):
    return datetime.date.fromisoformat(texto_mes + "-01")


def _valores_objetivo(escenario, celdas):
    """{(material, centro, mes): abastecimiento} que debe quedar en el archivo:
    compras + traslado recibido de cada celda que el escenario toca."""
    compras = {}
    for clave, c in escenario["cambios"]["compras"].items():
        material, centro, mes = clave.split("|")
        compras[(material, centro, _mes(mes))] = c["valor"]
    recibidos = {}
    for clave, c in escenario["cambios"]["traslados"].items():
        material, envia, recibe, mes = clave.split("|")
        recibidos[(material, recibe, _mes(mes))] = c["valor"]
    objetivo = {}
    for clave in set(compras) | set(recibidos):
        actual = celdas.get(clave, {"compras": 0.0, "recibido": 0.0})
        objetivo[clave] = compras.get(clave, actual["compras"]) + recibidos.get(clave, actual["recibido"])
    return objetivo


def _conflictos(escenario, celdas):
    """Celdas cuyo valor del S&OP ya no es el que había al armar el escenario."""
    conflictos = []
    for clave, c in escenario["cambios"]["compras"].items():
        material, centro, mes = clave.split("|")
        actual = celdas.get((material, centro, _mes(mes)), {}).get("compras", 0.0)
        if abs(actual - c["base"]) >= TOLERANCIA:
            conflictos.append(clave)
    for clave, c in escenario["cambios"]["traslados"].items():
        material, envia, recibe, mes = clave.split("|")
        receptor = celdas.get((material, recibe, _mes(mes)))
        actual = receptor["recibido"] if receptor else celdas.get((material, envia, _mes(mes)), {}).get("traslado", 0.0)
        if abs(actual - c["base"]) >= TOLERANCIA:
            conflictos.append(clave)
    return conflictos


def _sin_codigo(descripcion):
    return (q.PREFIJO_SIN_CODIGO_SOP + descripcion.upper())[:40]


def _columnas_mes(hoja):
    """{mes: columna} del encabezado de un Excel del S&OP (un mes por columna desde la quinta)."""
    columnas = {}
    for celda in hoja[1][4:]:
        if isinstance(celda.value, (datetime.datetime, datetime.date)):
            fecha = celda.value.date() if isinstance(celda.value, datetime.datetime) else celda.value
            columnas[fecha.replace(day=1)] = celda.column
    return columnas


def _sin_columna(objetivo, columnas, ruta):
    faltan = sorted({clave[-1] for clave in objetivo if clave[-1] not in columnas})
    if faltan:
        raise ErrorEscenario(f"«{os.path.basename(ruta)}» no trae la columna de {q.formato_mes_anio(faltan[0])}.")


def _fila_nueva(hoja, fijas):
    """Agrega al final una fila con el formato de la última y sus cuatro columnas fijas; devuelve su número."""
    nueva = hoja.max_row + 1
    for columna in range(1, hoja.max_column + 1):
        origen, destino = hoja.cell(nueva - 1, columna), hoja.cell(nueva, columna)
        if origen.has_style:
            destino._style = copiar_estilo(origen._style)
    for columna, valor in enumerate(fijas, start=1):
        hoja.cell(nueva, columna).value = valor
    return nueva


def _cifra(valor):
    valor = round(valor, 2)
    return int(valor) if float(valor).is_integer() else valor


def _guardar(libro, ruta):
    tmp = ruta + ".escenario.tmp"
    libro.save(tmp)
    try:
        os.replace(tmp, ruta)
    except PermissionError:
        os.remove(tmp)
        raise ErrorEscenario(f"No se pudo reemplazar «{os.path.basename(ruta)}»: ciérralo en Excel y vuelve a intentarlo.")


def _escribir_abastecimiento(ruta, objetivo, textos):
    """Escribe en el Excel de abastecimiento (Cod. SAP | Centro Destino |
    Material | Destino | un mes por columna) el abastecimiento de cada celda.
    Si dos filas del archivo van a la misma fila de la proyección (Carbón
    Terceros y su Chancado, q._MATERIALES_CONSUMO_VIA_OTRO), el valor va en
    la del material y se descuenta lo de la otra. Un material sin fila en el
    archivo se agrega al final con el formato de la última fila."""
    libro = openpyxl.load_workbook(ruta)
    hoja = libro.worksheets[0]
    columnas = _columnas_mes(hoja)
    fusion = {real: origen for origen, real in q._MATERIALES_CONSUMO_VIA_OTRO.items()}
    filas = {}
    for fila in range(2, hoja.max_row + 1):
        codigo, centro = texto(hoja.cell(fila, 1).value), texto(hoja.cell(fila, 2).value)
        if not codigo:
            descripcion = texto(hoja.cell(fila, 3).value)
            if not descripcion:
                continue
            codigo = _sin_codigo(descripcion)
        clave = fusion.get((codigo, centro), (codigo, centro))
        filas.setdefault(clave, []).append((fila, codigo))

    _sin_columna(objetivo, columnas, ruta)
    for (material, centro, mes), valor in sorted(objetivo.items()):
        if (material, centro) not in filas:
            es_sin_codigo = material.startswith(q.PREFIJO_SIN_CODIGO_SOP)
            nueva = _fila_nueva(hoja, [None if es_sin_codigo else material, centro,
                                       textos.get((material, centro)) or material, q.nombre_centro(centro)])
            filas[(material, centro)] = [(nueva, material)]
        propias = filas[(material, centro)]
        principal = next((f for f, codigo in propias if codigo == material), propias[0][0])
        columna = columnas[mes]
        otras = sum(float(hoja.cell(f, columna).value or 0) for f, _ in propias if f != principal)
        hoja.cell(principal, columna).value = _cifra(valor - otras)
    _guardar(libro, ruta)


def _escribir_traslados(ruta, objetivo, textos):
    """Escribe en el Excel de traslados (Cod. SAP | Centro Origen | Material |
    Centro Destino | un mes por columna) lo que se traslada en cada celda
    {(material, centro que envía, centro que recibe, mes): valor}. Un traslado
    sin fila en el archivo se agrega al final."""
    libro = openpyxl.load_workbook(ruta)
    hoja = libro.worksheets[0]
    columnas = _columnas_mes(hoja)
    _sin_columna(objetivo, columnas, ruta)
    filas = {(texto(hoja.cell(f, 1).value), texto(hoja.cell(f, 2).value), texto(hoja.cell(f, 4).value)): f
             for f in range(2, hoja.max_row + 1) if texto(hoja.cell(f, 1).value)}
    for (material, envia, recibe, mes), valor in sorted(objetivo.items()):
        if (material, envia, recibe) not in filas:
            filas[(material, envia, recibe)] = _fila_nueva(
                hoja, [material, envia, textos.get((material, envia)) or material, recibe])
        hoja.cell(filas[(material, envia, recibe)], columnas[mes]).value = _cifra(valor)
    _guardar(libro, ruta)


def _libro_traslados(ruta, titulo, meses, filas, notas):
    """Excel de traslados nuevo con el formato de los de la corrida
    (convertir_corrida_sop): filas [(material, envía, texto, recibe, [valor por
    mes])] y una hoja «Cómo se armó» con `notas`."""
    letra, negrita = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True)
    libro = openpyxl.Workbook()
    hoja = libro.active
    hoja.title = "Traslados"
    hoja.append(["Cod. SAP", "Centro Origen", "Material", "Centro Destino",
                 *[datetime.datetime(m.year, m.month, 1) for m in meses]])
    for j in range(1, 5 + len(meses)):
        celda = hoja.cell(1, j)
        celda.font, celda.fill = negrita, PatternFill("solid", fgColor="D9D9D9")
        celda.alignment = Alignment(horizontal="center", vertical="center")
        if j > 4:
            celda.number_format = "mmm-yy"
    for i, (material, envia, nombre, recibe, valores) in enumerate(filas, start=2):
        for j, valor in enumerate((material, envia, nombre, recibe), start=1):
            hoja.cell(i, j, valor).font = letra
        for j, valor in enumerate(valores, start=5):
            celda = hoja.cell(i, j, _cifra(valor))
            celda.font, celda.number_format = letra, "#,##0.00"
    for j, ancho in enumerate((13, 13, 40, 14), start=1):
        hoja.column_dimensions[get_column_letter(j)].width = ancho
    for j in range(5, 5 + len(meses)):
        hoja.column_dimensions[get_column_letter(j)].width = 11
    hoja.freeze_panes = "E2"
    hoja.auto_filter.ref = f"A1:{get_column_letter(4 + len(meses))}{len(filas) + 1}"
    ayuda = libro.create_sheet("Cómo se armó")
    for i, nota in enumerate([titulo, *notas], start=1):
        celda = ayuda.cell(i, 1, nota)
        celda.font = Font(name="Arial", size=12 if i == 1 else 10, bold=i == 1)
        celda.alignment = Alignment(wrap_text=True, vertical="top")
    ayuda.column_dimensions["A"].width = 110
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    libro.save(ruta)


def _recargar(ruta):
    """Vuelve a cargar el archivo (reemplaza su S&OP en la base e invalida la caché)."""
    call_command("import_plan_abastecimiento_sop", ruta, forzar=True, stdout=io.StringIO())


def _recargar_traslados(ruta):
    call_command("import_traslados_sop", ruta, forzar=True, stdout=io.StringIO())


def _misma_proyeccion(antes, despues):
    return set(antes) == set(despues) and all(abs(despues[k][c] - v) < 0.01 for k, celda in antes.items()
                                              for c, v in celda.items())


def crear_archivo_traslados(sop):
    """Crea y carga el 'Traslados SOP - <mes> <año>.xlsx' de un S&OP que no lo
    tiene (pedido del usuario, 2026-11-12: compras y traslados "son dos fuentes
    de suministro distintas, uno viene por proveedores y otro por traslado
    entre centros"), con los traslados que ya tiene su proyección
    (pares_archivo_traslados). Como los de la corrida: lo que envía cada
    centro; el que recibe lo sigue teniendo en su abastecimiento (todo lo que
    llega a la planta, contra lo que se miden el cumplimiento y el Plan vs
    Real). No cambia ninguna cifra: si cambiara, lo retira. Devuelve la ruta."""
    ruta = ruta_traslados(sop)
    if ruta is None:
        raise ErrorEscenario("No se encontró el archivo de abastecimiento cargado de este S&OP.")
    if os.path.exists(ruta):
        raise ErrorEscenario(f"Ya existe «{os.path.basename(ruta)}»: no se reemplaza.")
    base = q.proyeccion_inventario_sop(sop)
    antes = movimientos_por_celda(base)
    pares = pares_archivo_traslados(base)
    if not pares:
        raise ErrorEscenario("Este S&OP no tiene materiales que se trasladen entre sus dos centros.")
    meses = sorted(set(base["mes"]))
    textos = {(r.material_codigo, r.centro): r.texto_breve for r in base.itertuples()}
    filas = [(m, envia, textos.get((m, envia)) or m, recibe,
              [antes.get((m, recibe, mes), {}).get("recibido", 0.0) for mes in meses])
             for (m, envia), recibe in sorted(pares.items())]
    titulo = os.path.splitext(os.path.basename(ruta))[0]
    _libro_traslados(ruta, titulo, meses, filas, [
        "Lo que cada centro envía a la otra planta, en toneladas y desde el mes de emisión. El centro que recibe lo "
        "sigue teniendo en su abastecimiento (todo lo que llega a la planta): lo que no le llega por traslado es "
        "compra a proveedores.",
        f"Creado por el tablero el {datetime.date.today():%d/%m/%Y} con los traslados que ya tenía el S&OP: "
        + "; ".join(f"{(nombre or m).title()} de {q.nombre_centro(envia).title()} a {q.nombre_centro(recibe).title()}, "
                    f"{sum(valores):,.0f} T en el horizonte" for m, envia, nombre, recibe, valores in filas) + ".",
        "Al aplicar un escenario con traslados, el tablero escribe aquí y en el abastecimiento (con respaldo en "
        "_RETIRADOS).",
        "Lo que llega solo por traslado desde un centro que el S&OP no planifica (Clinker, Caliza Roca Extraída) "
        "sigue solo en el abastecimiento del centro que recibe.",
    ])
    try:
        _recargar_traslados(ruta)
        if not _misma_proyeccion(antes, movimientos_por_celda(q.proyeccion_inventario_sop(sop))):
            raise ErrorEscenario("El archivo de traslados nuevo cambiaba la proyección del S&OP: no se creó.")
    except Exception:
        os.remove(ruta)
        PlanTrasladoSOP.objects.filter(mes_snapshot=sop).delete()
        ArchivoImportado.objects.filter(ruta=ruta).delete()
        q.marcar_datos_actualizados()
        raise
    return ruta


def _diferencias(escenario, celdas):
    """Celdas del escenario que, después de cargar el archivo, no quedaron con su valor."""
    diferencias = []
    for clave, c in escenario["cambios"]["compras"].items():
        material, centro, mes = clave.split("|")
        if abs(celdas.get((material, centro, _mes(mes)), {}).get("compras", 0.0) - c["valor"]) >= TOLERANCIA:
            diferencias.append(clave)
    for clave, c in escenario["cambios"]["traslados"].items():
        material, envia, recibe, mes = clave.split("|")
        if abs(celdas.get((material, recibe, _mes(mes)), {}).get("recibido", 0.0) - c["valor"]) >= TOLERANCIA:
            diferencias.append(clave)
    return diferencias


def aplicar(id_escenario, usuario):
    """Pasa el escenario a los archivos del S&OP vigente: el de abastecimiento
    (compras más lo que llega por traslado a cada centro) y, si trae traslados
    entre sus dos centros (pares_archivo_traslados), el de traslados, que se
    crea si el S&OP no lo tiene (crear_archivo_traslados). Respalda los
    archivos en _RETIRADOS, escribe las celdas, los vuelve a cargar y comprueba
    que la proyección quedó con los valores del escenario. Si algo falla,
    devuelve los archivos respaldados y los vuelve a cargar."""
    with _CANDADO_APLICAR:
        escenario = next((e for e in listar() if e["id"] == id_escenario), None)
        if escenario is None:
            raise ErrorEscenario("Ese escenario ya no existe.")
        sop = datetime.date.fromisoformat(escenario["sop"])
        motivo = motivo_no_aplicable(sop)
        if motivo:
            raise ErrorEscenario(motivo)
        n_celdas = sum(len(v) for v in escenario["cambios"].values())
        if not n_celdas:
            raise ErrorEscenario("El escenario no cambia ninguna celda.")

        base = q.proyeccion_inventario_sop(sop)
        celdas = movimientos_por_celda(base)
        conflictos = _conflictos(escenario, celdas)
        if conflictos:
            raise ErrorEscenario(
                f"El S&OP cambió desde que se guardó el escenario ({len(conflictos)} celda(s), p. ej. "
                f"{conflictos[0].replace('|', ' · ')}). Ábrelo, revísalo y guárdalo otra vez antes de aplicarlo.")
        de_quien, _ = pares_traslado(base)
        textos = {(r.material_codigo, r.centro): r.texto_breve for r in base.itertuples()}
        en_archivo = pares_archivo_traslados(base)
        traslados = {}  # los que van al archivo de traslados: {(material, envía, recibe, mes): valor}
        for clave, c in escenario["cambios"]["traslados"].items():
            material, envia, recibe, mes = clave.split("|")
            if de_quien.get((material, recibe)) != envia and q._TRASLADOS_SOP.get((material, recibe)) != envia:
                raise ErrorEscenario(f"El traslado {clave.replace('|', ' · ')} no sigue la regla del S&OP.")
            if en_archivo.get((material, envia)) == recibe:
                traslados[(material, envia, recibe, _mes(mes))] = c["valor"]
            elif not solo_traslado(material, recibe):
                nombre = (textos.get((material, recibe)) or material).title()
                raise ErrorEscenario(
                    f"El traslado de {nombre} de {q.nombre_centro(envia).title()} a {q.nombre_centro(recibe).title()} "
                    "no se puede aplicar: el S&OP no planifica ese material en el centro que envía.")

        objetivo = _valores_objetivo(escenario, celdas)
        ruta = archivo_abastecimiento(sop)
        ruta_traslados_sop = (archivo_traslados(sop) or crear_archivo_traslados(sop)) if traslados else None
        archivos = [ruta] + ([ruta_traslados_sop] if ruta_traslados_sop else [])
        ahora = datetime.datetime.now()
        carpeta_base = os.path.dirname(os.path.dirname(ruta))
        nombre_carpeta = re.sub(r'[<>:"/\\|?*]', "", f"{ahora:%Y-%m-%d %H%M} antes del escenario {escenario['nombre']}").strip()
        carpeta_respaldo = os.path.join(carpeta_base, CARPETA_RESPALDOS, nombre_carpeta)
        os.makedirs(carpeta_respaldo, exist_ok=True)
        for archivo in archivos:
            shutil.copy2(archivo, os.path.join(carpeta_respaldo, os.path.basename(archivo)))

        def recargar():
            _recargar(ruta)
            if ruta_traslados_sop:
                _recargar_traslados(ruta_traslados_sop)
        try:
            _escribir_abastecimiento(ruta, objetivo, textos)
            if ruta_traslados_sop:
                _escribir_traslados(ruta_traslados_sop, traslados, textos)
            recargar()
            nuevas = movimientos_por_celda(q.proyeccion_inventario_sop(sop))
            # Además de lo que recibe cada centro, lo que sale del que envía.
            diferencias = _diferencias(escenario, nuevas) + [
                f"{m}|{envia}|{recibe}|{mes:%Y-%m}" for (m, envia, recibe, mes), valor in traslados.items()
                if abs(nuevas.get((m, envia, mes), {}).get("traslado", 0.0) - valor) >= TOLERANCIA]
            if diferencias:
                raise ErrorEscenario(f"Después de cargar los archivos, {len(diferencias)} celda(s) no quedaron con el "
                                     f"valor del escenario (p. ej. {diferencias[0].replace('|', ' · ')}). Se devolvieron "
                                     "los archivos anteriores.")
        except Exception:
            for archivo in archivos:
                shutil.copy2(os.path.join(carpeta_respaldo, os.path.basename(archivo)), archivo)
            recargar()
            raise

        nombres = [os.path.basename(a) for a in archivos]
        respaldo = os.path.relpath(carpeta_respaldo, carpeta_base)
        with _CANDADO:
            escenarios = _leer()
            for e in escenarios:
                if e["id"] == id_escenario:
                    e.setdefault("aplicado", []).append({
                        "fecha": ahora.isoformat(timespec="seconds"), "usuario": usuario.username,
                        "archivo": nombres[0], "archivos": nombres, "celdas": n_celdas, "respaldo": respaldo})
            _escribir(escenarios)
        return {"archivo": nombres[0], "archivos": nombres, "celdas": n_celdas, "respaldo": respaldo}
