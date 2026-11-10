"""Reglas y metas editables desde el tablero (pedido del usuario, 2026-11-10): metas por categoría, metas y supuestos de la Portada y el PCS fijo del costo por energía, con registro de quién cambió qué y cuándo."""

import datetime
import json
import os
from pathlib import Path

from dashboard import queries as q
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .. import ejecutivo
from .comun import _categorias_disponibles, _construir_nav

# Quién cambió qué y cuándo (fuera del repositorio, como las reglas).
HISTORIAL = Path(settings.BASE_DIR) / "datos_locales" / "reglas_historial.jsonl"
GRUPO_EDITORES = "Administradores"
_HISTORIAL_EN_PAGINA = 20

# Metas por categoría (reglas_locales.json "metas_categoria", ver
# ejecutivo._METAS_CATEGORIA): clave -> (texto, unidad, más es mejor).
METAS_CATEGORIA = {
    "contrato_pct": ("Compras bajo contrato", "%", True),
    "pmp_dias": ("Plazo medio de pago", "días", True),
    "ipu_pct": ("IPU", "%", False),
    "otif_pct": ("OTIF", "%", True),
    "inventario": ("Valor del inventario", "S/", False),
    "inventario_sin_obsoletos": ("Inventario sin obsoletos", "S/", False),
    "dias_inventario": ("Días de inventario", "días", False),
    "proveedor_unico_pct": ("Proveedor único", "%", False),
}
# Metas generales y supuestos de la Portada (metas.json): textos.
METAS_GENERALES = {
    "contrato_pct": ("Compras bajo contrato", "%"), "pmp_dias": ("Plazo medio de pago", "días"),
    "ipu_pct": ("IPU", "%"), "inventario_var_pct": ("Variación del inventario", "%"),
    "inmovilizado_pct": ("Inventario inmovilizado", "%"), "cumplimiento_sop_pct": ("Cumplimiento del plan S&OP", "%"),
    "proveedor_unico_pct": ("Proveedor único", "%"), "criticos_en_riesgo": ("Materiales críticos en riesgo", "materiales"),
    "otif_pct": ("OTIF", "%"),
}
SUPUESTOS = {
    "otif_dias_tolerancia": ("OTIF: días de tolerancia en la entrega", "días"),
    "otif_cantidad_tolerancia_pct": ("OTIF: tolerancia en la cantidad", "%"),
    "ahorro_contrato_marco_pct": ("Ahorro de un contrato marco", "%"),
    "inmovilizado_recuperable_pct": ("Parte recuperable del inventario inmovilizado", "%"),
    "monto_minimo_proveedor_plazo": ("Compras mínimas de un proveedor para revisar su plazo", "S/"),
    "meses_compra_recurrente": ("Meses de compra para considerarla recurrente", "meses"),
}


def puede_editar(usuario):
    return usuario.is_superuser or usuario.groups.filter(name=GRUPO_EDITORES).exists()


def _numero(texto):
    """Número de un campo del formulario (acepta coma de miles), None si está
    vacío; ValueError si no es un número."""
    texto = (texto or "").strip().replace(",", "")
    if not texto:
        return None
    valor = float(texto)
    return int(valor) if valor.is_integer() else valor


def _texto(valor):
    """Valor para un campo del formulario: con separador de miles, "" sin dato."""
    if valor is None:
        return ""
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return f"{valor:,}"


def _escribir(ruta, datos, compacto):
    """Escribe `datos` en `ruta` por un temporal (si algo falla, el archivo
    anterior queda intacto). `compacto`: una clave de primer nivel por línea
    (el formato de reglas_locales.json)."""
    if compacto:
        lineas = [f" {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}" for k, v in datos.items()]
        texto = "{\n" + ",\n".join(lineas) + "\n}\n"
    else:
        texto = json.dumps(datos, ensure_ascii=False, indent=1) + "\n"
    tmp = Path(str(ruta) + ".tmp")
    tmp.write_text(texto, encoding="utf-8")
    os.replace(tmp, ruta)


def _registrar(usuario, cambios):
    HISTORIAL.parent.mkdir(exist_ok=True)
    ahora = datetime.datetime.now().isoformat(timespec="seconds")
    with HISTORIAL.open("a", encoding="utf-8") as f:
        for que, antes, despues in cambios:
            f.write(json.dumps({"fecha": ahora, "usuario": usuario.username, "que": que, "antes": antes,
                                "despues": despues}, ensure_ascii=False) + "\n")


def _historial():
    if not HISTORIAL.exists():
        return []
    filas = [json.loads(linea) for linea in HISTORIAL.read_text(encoding="utf-8").splitlines() if linea.strip()]
    for f in filas:
        f["fecha"] = datetime.datetime.fromisoformat(f["fecha"])
    return list(reversed(filas))[:_HISTORIAL_EN_PAGINA]


def _guardar(request, anio):
    """Aplica el formulario: devuelve los cambios [(qué, antes, después)] o
    levanta ValueError con el campo que no es un número."""
    datos = request.POST
    cambios = []
    reglas_ruta = Path(q._ARCHIVO_REGLAS_LOCALES)
    reglas = json.loads(reglas_ruta.read_text(encoding="utf-8")) if reglas_ruta.exists() else {}
    metas_ruta = ejecutivo._RUTA_METAS
    metas = json.loads(metas_ruta.read_text(encoding="utf-8"))

    def leer(nombre, etiqueta):
        try:
            return _numero(datos.get(nombre))
        except ValueError:
            raise ValueError(f"«{etiqueta}» no es un número: {datos.get(nombre)}") from None

    # Metas por categoría del año elegido.
    por_categoria = reglas.setdefault("metas_categoria", {})
    for categoria in _categorias_disponibles():
        for clave, (texto, _, _) in METAS_CATEGORIA.items():
            nombre = f"cat|{categoria}|{clave}"
            if nombre not in datos:
                continue
            nuevo = leer(nombre, f"{texto} de {categoria.title()}")
            actual = por_categoria.get(categoria, {}).get(clave, {}).get(str(anio))
            if nuevo == actual:
                continue
            cambios.append((f"Meta {anio} de {categoria.title()}: {texto}", actual, nuevo))
            anios = por_categoria.setdefault(categoria, {}).setdefault(clave, {})
            if nuevo is None:
                anios.pop(str(anio), None)
                if not anios:
                    por_categoria[categoria].pop(clave)
                if not por_categoria[categoria]:
                    por_categoria.pop(categoria)
            else:
                anios[str(anio)] = nuevo

    # PCS fijo del costo por energía.
    for material, actual in list(reglas.get("pcs_fijo_energia", {}).items()):
        nombre = f"pcs|{material}"
        if nombre in datos:
            nuevo = leer(nombre, f"PCS fijo de {material}")
            if nuevo is not None and nuevo != actual:
                cambios.append((f"PCS fijo de {material} (kcal/kg)", actual, nuevo))
                reglas["pcs_fijo_energia"][material] = nuevo

    # Metas generales de la Portada (meta, tolerancia, confirmada).
    cambios_metas = []
    for clave, meta in metas.get("metas", {}).items():
        texto = METAS_GENERALES.get(clave, (clave, ""))[0]
        for campo in ("meta", "tolerancia"):
            nombre = f"meta|{clave}|{campo}"
            if nombre in datos:
                nuevo = leer(nombre, f"{texto} ({campo})")
                if nuevo is not None and nuevo != meta.get(campo):
                    cambios_metas.append((f"Portada: {texto}, {campo}", meta.get(campo), nuevo))
                    meta[campo] = nuevo
        if f"meta|{clave}|presente" in datos:
            confirmada = f"meta|{clave}|confirmada" in datos
            if confirmada != bool(meta.get("confirmada")):
                cambios_metas.append((f"Portada: {texto}, confirmada", meta.get("confirmada"), confirmada))
                meta["confirmada"] = confirmada
    for clave in SUPUESTOS:
        nombre = f"supuesto|{clave}"
        if nombre in datos and clave in metas.get("supuestos", {}):
            nuevo = leer(nombre, SUPUESTOS[clave][0])
            if nuevo is not None and nuevo != metas["supuestos"][clave]:
                cambios_metas.append((f"Supuesto: {SUPUESTOS[clave][0]}", metas["supuestos"][clave], nuevo))
                metas["supuestos"][clave] = nuevo

    if len(cambios) > 0:
        _escribir(reglas_ruta, reglas, compacto=True)
    if cambios_metas:
        _escribir(metas_ruta, metas, compacto=False)
    return cambios + cambios_metas


@login_required
def reglas_y_metas(request):
    """Página Reglas y metas: ver (todos) y editar (Administradores)."""
    hoy = datetime.date.today()
    try:
        anio = int(request.GET.get("anio") or request.POST.get("anio") or hoy.year)
    except ValueError:
        anio = hoy.year
    editable = puede_editar(request.user)
    if request.method == "POST":
        if not editable:
            messages.error(request, f"Solo el grupo {GRUPO_EDITORES} puede cambiar reglas y metas.")
            return redirect(f"{request.path}?anio={anio}")
        try:
            cambios = _guardar(request, anio)
        except ValueError as error:
            messages.error(request, f"No se guardó nada: {error}")
        else:
            if cambios:
                _registrar(request.user, cambios)
                messages.success(request, f"{len(cambios)} cambio{'s' if len(cambios) != 1 else ''} guardado"
                                          f"{'s' if len(cambios) != 1 else ''}: las páginas ya los usan.")
            else:
                messages.info(request, "No había cambios que guardar.")
        return redirect(f"{request.path}?anio={anio}")

    reglas = q.regla_local("metas_categoria", {})
    metas = json.loads(ejecutivo._RUTA_METAS.read_text(encoding="utf-8"))
    categorias = [{
        "nombre": c, "texto": c.title(),
        "metas": [{"clave": k, "nombre": f"cat|{c}|{k}", "valor": _texto(reglas.get(c, {}).get(k, {}).get(str(anio)))}
                  for k in METAS_CATEGORIA],
    } for c in _categorias_disponibles()]
    catalogo = q.catalogo_materiales().drop_duplicates("material_codigo").set_index("material_codigo")["texto_breve"]
    categorias_nav, _ = _construir_nav("reglas-y-metas")
    contexto = {
        "categorias_nav": categorias_nav, "editable": editable, "grupo_editores": GRUPO_EDITORES,
        "anio": anio, "anios": [hoy.year - 1, hoy.year, hoy.year + 1],
        "columnas": [{"clave": k, "texto": t, "unidad": u, "mayor": m} for k, (t, u, m) in METAS_CATEGORIA.items()],
        "categorias": categorias,
        "metas_generales": [{"clave": k, "texto": METAS_GENERALES.get(k, (k, ""))[0], "unidad": METAS_GENERALES.get(k, ("", ""))[1],
                             "meta": _texto(v.get("meta")), "tolerancia": _texto(v.get("tolerancia")),
                             "mayor": v.get("sentido") == "mayor", "confirmada": bool(v.get("confirmada"))}
                            for k, v in metas.get("metas", {}).items()],
        "supuestos": [{"clave": k, "texto": t, "unidad": u, "valor": _texto(metas.get("supuestos", {}).get(k))}
                      for k, (t, u) in SUPUESTOS.items() if k in metas.get("supuestos", {})],
        "pcs_fijo": [{"material": m, "texto": catalogo.get(m, ""), "valor": _texto(v)}
                     for m, v in q.regla_local("pcs_fijo_energia", {}).items()],
        "flete_referencia": [{"clave": k.replace("|", " · "), "valor": v} for k, v in q.regla_local("flete_referencia", {}).items()],
        "parte_acopio": [{"material": m, "texto": catalogo.get(m, ""), "embarque": e, "toneladas": t}
                         for m, e, t in q.regla_local("parte_acopio_por_barco", [])],
        "historial": _historial(),
    }
    return render(request, "gestion/reglas_y_metas.html", contexto)
