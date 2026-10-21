import datetime
import json
import math
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard import queries as q
from gestion import ejecutivo, views

# Fuera del repositorio (excluida de git): son cifras de la empresa.
ARCHIVO = Path(settings.BASE_DIR) / "datos_locales" / "foto_calculos.json"
# Diferencias que no cuentan: redondeos de punto flotante.
_TOLERANCIA_RELATIVA = 0.0005  # 0.05%
_TOLERANCIA_ABSOLUTA = 0.05    # p.ej. 0.05 puntos de un %
_CAMPOS_CATEGORIA = ("compras", "variacion", "contrato", "pmp", "ipu", "unico", "inventario")


def _meses_cerrados(cantidad):
    """Los últimos `cantidad` meses que ya no cambian con la carga diaria: la
    carga recarga el mes en curso y, hasta el día 10, también el anterior."""
    hoy = datetime.date.today()
    ultimo = q._sumar_meses(hoy.replace(day=1), -2 if hoy.day <= 10 else -1)
    disponibles = set(ejecutivo.meses_disponibles())
    meses = [q._sumar_meses(ultimo, -i) for i in range(cantidad)]
    return sorted(m for m in meses if m in disponibles)


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


class Command(BaseCommand):
    help = (
        "Compara las cifras de los meses cerrados (indicadores de la portada y de cada área, "
        "y la tabla por categoría: compras, contrato, PMP, IPU, proveedor único, inventario) "
        "contra la última foto guardada, para detectar si un cambio de código las movió sin "
        "querer. --guardar toma una foto nueva (después de revisar que las diferencias son "
        "correctas). Nota: una corrección de datos de SAP (ME2N modificada, ajuste FBL3N "
        "tardío) también mueve cifras de meses cerrados."
    )

    def add_arguments(self, parser):
        parser.add_argument("--guardar", action="store_true", help="Guarda la foto actual como referencia.")
        parser.add_argument("--meses", type=int, default=12, help="Meses cerrados a revisar (por defecto 12).")

    def handle(self, *args, **options):
        areas = [("Planificación", views._area_ejecutiva("planificacion"))]
        areas += [(c, views._area_ejecutiva(views._slug(c))) for c in views._categorias_disponibles()]
        meses = _meses_cerrados(options["meses"])
        if not meses:
            raise CommandError("No hay meses cerrados con datos")
        actual = {m.isoformat(): _foto_mes(m, areas) for m in meses}

        if options["guardar"] or not ARCHIVO.exists():
            ARCHIVO.parent.mkdir(exist_ok=True)
            foto = {"guardada": datetime.datetime.now().isoformat(timespec="seconds"), "meses": actual}
            ARCHIVO.write_text(json.dumps(foto, ensure_ascii=False, indent=1), encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(
                f"Foto guardada: {len(actual)} meses ({meses[0]:%Y-%m} a {meses[-1]:%Y-%m}) en {ARCHIVO}"))
            return

        foto = json.loads(ARCHIVO.read_text(encoding="utf-8"))
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

        comparados = [m for m in actual if m in foto["meses"]]
        self.stdout.write(f"Foto del {foto['guardada']}; {len(comparados)} meses comparados.")
        if not diferencias:
            self.stdout.write(self.style.SUCCESS("Sin diferencias."))
            return
        for mes, que, antes, ahora in diferencias:
            self.stdout.write(f"  {mes[:7]}  {que}: {antes} -> {ahora}")
        raise CommandError(f"{len(diferencias)} cifra(s) distintas a la foto. Si son correctas: verificar_calculos --guardar")
