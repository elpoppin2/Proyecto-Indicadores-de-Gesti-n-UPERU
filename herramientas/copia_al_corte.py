"""Copias de las categorías acumuladas a un corte (pedido del usuario, 2026-10-26: "con filtros acumulados
hasta setiembre 2026").

1. Copia la base (sqlite backup: la original no se toca) y deja solo los datos hasta el corte: se borran los
   movimientos, compras, inventarios, FBL3N e ingresos de balanza con fecha posterior, y las emisiones del S&OP
   posteriores al mes del corte.
2. Con el día de hoy fijado al día siguiente del corte (el mes del corte queda cerrado) y una caché propia,
   genera la copia estática de cada categoría (exportar_sitio_html --solo-categorias), con los filtros del año
   del corte de enero al mes del corte.

La base recortada y su caché quedan en datos_locales/corte_AAAAMMDD/ (no van al repositorio). Necesita
freezegun, instalado aparte para no tocar el venv:
    venv\\Scripts\\python.exe -m pip install --target datos_locales\\libs freezegun

uso: venv\\Scripts\\python.exe herramientas\\copia_al_corte.py "<carpeta destino>" [AAAA-MM-DD corte]
"""
import datetime
import os
import shutil
import sqlite3
import sys
import time

proyecto = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
destino = sys.argv[1]
corte = datetime.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else datetime.date(2026, 9, 30)
trabajo = os.path.join(proyecto, "datos_locales", f"corte_{corte:%Y%m%d}")
base = os.path.join(trabajo, "db.sqlite3")
cache = os.path.join(trabajo, "cache")

# La base se rehace en cada corrida (los datos del localhost pudieron recargarse).
shutil.rmtree(trabajo, ignore_errors=True)
os.makedirs(trabajo)
t = time.monotonic()
origen = sqlite3.connect(f"file:{os.path.join(proyecto, 'db.sqlite3')}?mode=ro", uri=True)
copia = sqlite3.connect(base)
origen.backup(copia)
origen.close()
c, mes_corte = corte.isoformat(), corte.replace(day=1).isoformat()
for tabla, condicion in [
    ("gestion_movimientomaterial", f"fecha_contabilizacion > '{c}'"),
    ("gestion_lineacompra", f"fecha_documento > '{c}'"),
    ("gestion_inventariomensual", f"fecha > '{c}'"),
    ("gestion_ajustepreciofbl3n", f"fecha_contabilizacion > '{c}'"),
    ("gestion_ingresofisicosop", f"fecha_ingreso > '{c}'"),
    ("gestion_planabastecimientosop", f"mes_snapshot > '{mes_corte}'"),
    ("gestion_planconsumosop", f"mes_snapshot > '{mes_corte}'"),
    ("gestion_stockminmaxsop", f"mes_snapshot > '{mes_corte}'"),
]:
    n = copia.execute(f"DELETE FROM {tabla} WHERE {condicion}").rowcount
    print(f"  {tabla}: {n:,} filas después del corte", flush=True)
copia.commit()
copia.close()
print(f"Base al {corte:%d/%m/%Y} lista en {time.monotonic() - t:.0f} s", flush=True)

# freezegun al final del path: sus dependencias no tapan las del venv.
sys.path.append(os.path.join(proyecto, "datos_locales", "libs"))
sys.path.insert(0, proyecto)
os.chdir(proyecto)
os.environ["DATABASE_URL"] = "sqlite:///" + base.replace("\\", "/")
os.environ["UPERU_DIR_CACHE"] = cache
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
# Todo se carga ANTES de fijar la fecha (importar pandas con la fecha fijada
# desborda la pila); freezegun cambia también las fechas de los módulos ya cargados.
import django  # noqa: E402
django.setup()
import pandas  # noqa: E402,F401
from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402
from gestion import views  # noqa: E402,F401
from gestion.management.commands import exportar_sitio_html  # noqa: E402,F401
import freezegun  # noqa: E402

MESES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]
with freezegun.freeze_time(datetime.datetime.combine(corte + datetime.timedelta(days=1), datetime.time(8, 0)),
                           ignore=["threading", "queue", "pandas", "numpy", "django.db"]):
    assert os.path.samefile(connection.settings_dict["NAME"], base), connection.settings_dict["NAME"]
    print("Hoy fijado en", datetime.date.today(), flush=True)
    anio, mes = corte.year, corte.month
    parametros = (f"anio_compras={anio}&mes_compras_desde=1&mes_compras_hasta={mes}"
                  f"&anio_cxp={anio}&mes_cxp_desde=1&mes_cxp_hasta={mes}"
                  f"&periodo_ipu={anio}&anio_indicadores={anio}")
    os.makedirs(destino, exist_ok=True)
    call_command("exportar_sitio_html", destino, solo_categorias=True, parametros=parametros,
                 aviso=f"Acumulado de enero a {MESES[mes]} {anio}: datos al {corte:%d/%m/%Y}. "
                       f"Vista fija para presentar; los filtros y el detalle fila por fila funcionan en el localhost.")
