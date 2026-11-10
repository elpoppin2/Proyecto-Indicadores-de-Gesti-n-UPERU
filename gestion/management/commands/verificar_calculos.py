import datetime
import hashlib
import json
import math
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard import queries as q
from dashboard.consultas import base
from gestion import ejecutivo, views
from gestion.vistas import comun

# Fuera del repositorio (excluida de git): son cifras de la empresa.
ARCHIVO = Path(settings.BASE_DIR) / "datos_locales" / "foto_calculos.json"
# Diferencias que no cuentan: redondeos de punto flotante.
_TOLERANCIA_RELATIVA = 0.0005  # 0.05%
_TOLERANCIA_ABSOLUTA = 0.05    # p.ej. 0.05 puntos de un %
_CAMPOS_CATEGORIA = ("compras", "variacion", "contrato", "pmp", "ipu", "unico", "inventario")
# Diferencias que se listan cuando las cifras cambiaron por datos nuevos (el resto se cuenta).
_MAXIMO_LISTADO_DATOS = 10


def _meses_cerrados(cantidad):
    """Los últimos `cantidad` meses que ya no cambian con la carga diaria: la
    carga recarga el mes en curso y, hasta el día 10, también el anterior."""
    hoy = datetime.date.today()
    ultimo = q._sumar_meses(hoy.replace(day=1), -2 if hoy.day <= 10 else -1)
    disponibles = set(ejecutivo.meses_disponibles())
    meses = [q._sumar_meses(ultimo, -i) for i in range(cantidad)]
    return sorted(m for m in meses if m in disponibles)


def huella_codigo():
    """Huella de todo lo que calcula las cifras sin ser un dato cargado: las
    consultas, las vistas, el resumen ejecutivo, las plantillas y las reglas
    y metas editables (reglas_locales.json, metas.json)."""
    huella = hashlib.sha1(f"{base._HUELLA_CODIGO}|{comun._HUELLA_VISTAS}".encode())
    for ruta in (Path(q._ARCHIVO_REGLAS_LOCALES), ejecutivo._RUTA_METAS):
        try:
            huella.update(ruta.read_bytes())
        except OSError:
            pass
    return huella.hexdigest()[:12]


def _numero(v):
    if v is None:
        return None
    v = float(v)
    return None if math.isnan(v) else round(v, 6)


def _foto_mes(mes, areas):
    """Indicadores de la portada (texto mostrado, por área) y la tabla por
    categoría (números) de un mes."""
    general = ejecutivo.contexto(mes)
    foto = {
        "kpis": {"General": {k["clave"]: k["cifra"] for k in general["kpis"]}},
        "categorias": {c["categoria"]: {campo: _numero(c.get(campo)) for campo in _CAMPOS_CATEGORIA}
                       for c in general["categorias"]},
    }
    for nombre, area in areas:
        foto["kpis"][nombre] = {k["clave"]: k["cifra"] for k in ejecutivo.contexto(mes, area)["kpis"]}
    return foto


def _distinto(antes, ahora):
    if isinstance(antes, (int, float)) and isinstance(ahora, (int, float)):
        return abs(antes - ahora) > max(_TOLERANCIA_ABSOLUTA, abs(antes) * _TOLERANCIA_RELATIVA)
    return antes != ahora


def _diferencias(foto, actual):
    diferencias = []
    for mes, ahora in actual.items():
        antes = foto["meses"].get(mes)
        if antes is None:
            continue  # mes nuevo en la ventana: entra en la próxima foto
        for area, kpis in ahora["kpis"].items():
            previos = antes["kpis"].get(area, {})
            for clave in sorted(set(kpis) | set(previos)):
                if _distinto(previos.get(clave), kpis.get(clave)):
                    diferencias.append((mes, f"{area} · {clave}", previos.get(clave), kpis.get(clave)))
        for categoria in sorted(set(ahora["categorias"]) | set(antes["categorias"])):
            a, b = antes["categorias"].get(categoria, {}), ahora["categorias"].get(categoria, {})
            for campo in _CAMPOS_CATEGORIA:
                if _distinto(a.get(campo), b.get(campo)):
                    diferencias.append((mes, f"{categoria} · {campo}", a.get(campo), b.get(campo)))
    return diferencias


class Command(BaseCommand):
    help = (
        "Compara las cifras de los meses cerrados (indicadores de la portada y de cada área, "
        "y la tabla por categoría: compras, contrato, PMP, IPU, proveedor único, inventario) "
        "contra la última foto guardada, para detectar si un cambio de código las movió sin "
        "querer. La foto guarda la huella del código (consultas, vistas, plantillas, reglas y "
        "metas): si el código es el mismo de la foto, lo que cambió vino de datos nuevos (una "
        "ME2N corregida, un ajuste FBL3N tardío) y la foto se renueva sola; si el código "
        "cambió, las diferencias se reportan como error. --renovar (lo llama calentar_cache "
        "después de cada carga) renueva la foto solo si el código no cambió. --guardar toma una "
        "foto nueva después de revisar que las diferencias son correctas."
    )

    def add_arguments(self, parser):
        parser.add_argument("--guardar", action="store_true", help="Guarda la foto actual como referencia.")
        parser.add_argument("--renovar", action="store_true",
                            help="Renueva la foto si el código es el mismo de la foto (después de una carga).")
        parser.add_argument("--meses", type=int, default=12, help="Meses cerrados a revisar (por defecto 12).")

    def _guardar(self, actual, huella, motivo):
        ARCHIVO.parent.mkdir(exist_ok=True)
        foto = {"guardada": datetime.datetime.now().isoformat(timespec="seconds"), "huella_codigo": huella,
                "version_datos": base._version_datos(), "motivo": motivo, "meses": actual}
        ARCHIVO.write_text(json.dumps(foto, ensure_ascii=False, indent=1), encoding="utf-8")
        meses = sorted(actual)
        self.stdout.write(self.style.SUCCESS(
            f"Foto guardada ({motivo}): {len(actual)} meses ({meses[0][:7]} a {meses[-1][:7]}) en {ARCHIVO}"))

    def handle(self, *args, **options):
        huella = huella_codigo()
        foto = json.loads(ARCHIVO.read_text(encoding="utf-8")) if ARCHIVO.exists() else None
        if options["renovar"] and foto and foto.get("huella_codigo") != huella:
            # No se calcula nada: renovar ahora escondería lo que movió el código.
            self.stdout.write(self.style.WARNING(
                "Foto de cifras sin renovar: el código, las reglas o las metas cambiaron desde la última foto. "
                "Correr verificar_calculos y revisar las diferencias."))
            return

        areas = [("Planificación", views._area_ejecutiva("planificacion"))]
        areas += [(c, views._area_ejecutiva(views._slug(c))) for c in views._categorias_disponibles()]
        meses = _meses_cerrados(options["meses"])
        if not meses:
            raise CommandError("No hay meses cerrados con datos")
        actual = {m.isoformat(): _foto_mes(m, areas) for m in meses}

        if options["guardar"] or foto is None:
            self._guardar(actual, huella, "pedida" if options["guardar"] else "primera")
            return

        diferencias = _diferencias(foto, actual)
        comparados = [m for m in actual if m in foto["meses"]]
        mismo_codigo = foto.get("huella_codigo") == huella
        datos_nuevos = foto.get("version_datos") != base._version_datos()
        self.stdout.write(f"Foto del {foto['guardada']}; {len(comparados)} meses comparados; "
                          f"código {'igual' if mismo_codigo else 'distinto'} al de la foto; "
                          f"{'entraron datos nuevos' if datos_nuevos else 'mismos datos'} desde entonces.")
        if mismo_codigo:
            # Sin cambios de código, una diferencia solo puede venir de los datos.
            if diferencias:
                self.stdout.write(f"{len(diferencias)} cifra(s) cambiaron por datos nuevos (el código es el mismo):")
                for mes, que, antes, ahora in diferencias[:_MAXIMO_LISTADO_DATOS]:
                    self.stdout.write(f"  {mes[:7]}  {que}: {antes} -> {ahora}")
                if len(diferencias) > _MAXIMO_LISTADO_DATOS:
                    self.stdout.write(f"  y {len(diferencias) - _MAXIMO_LISTADO_DATOS} más.")
            else:
                self.stdout.write(self.style.SUCCESS("Sin diferencias."))
            if diferencias or datos_nuevos or len(comparados) < len(actual):
                self._guardar(actual, huella, "datos nuevos")
            return

        if not diferencias:
            self.stdout.write(self.style.SUCCESS("Sin diferencias: el cambio de código no movió ninguna cifra."))
            self._guardar(actual, huella, "código revisado sin diferencias")
            return
        for mes, que, antes, ahora in diferencias:
            self.stdout.write(f"  {mes[:7]}  {que}: {antes} -> {ahora}")
        aviso = (" También entraron datos nuevos desde la foto: parte de las diferencias puede venir de ellos."
                 if datos_nuevos else "")
        raise CommandError(f"{len(diferencias)} cifra(s) distintas a la foto con el código cambiado.{aviso} "
                           "Si son correctas: verificar_calculos --guardar")
