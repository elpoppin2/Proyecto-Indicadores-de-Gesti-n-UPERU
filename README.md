# Indicadores de Gestión

Dashboard Django de indicadores de gestión de Supply Chain (compras, inventario,
precios/IPU, cuentas por pagar y Planificación S&OP), alimentado desde extracciones de
SAP (MB51, MB5B, ME2N, ZMM060, ZMM056, FBL3N) y un catálogo de categorías.

## Arquitectura

- `config/`: configuración de Django.
- `gestion/`: modelos, vistas (`gestion/vistas/`), plantillas, gráficos SVG
  (`charts.py`), resumen ejecutivo (`ejecutivo.py`) y comandos de carga y exportación.
- `dashboard/queries.py`: consultas y cálculos (SQL directo + pandas) con caché versionada.
  (La antigua app de Streamlit se retiró el 2026-10-21; el dashboard web la reemplazó.)

## Instalación

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe manage.py migrate
```

## Carga de datos

Cada CSV se importa con un comando de Django. También existe un orquestador
`import_todo` que corre los cuatro pasos en orden a partir de la carpeta
`CSVs_Convertidos`.

```powershell
# Todo de una vez (recomendado)
.\venv\Scripts\python.exe manage.py import_todo "E:\Exceles Control de Gestión\CSVs_Convertidos"

# O paso a paso:
.\venv\Scripts\python.exe manage.py import_categorias "<ruta>\CATEGORÍAS\CATEGORÍAS OFICIAL.csv"
.\venv\Scripts\python.exe manage.py import_materiales "<ruta>\TABLA_SQ01_ZMM060\MM60_SUPPLY.csv"
.\venv\Scripts\python.exe manage.py import_inventario "<ruta>\TABLA_MB5B"
.\venv\Scripts\python.exe manage.py import_compras "<ruta>\TABLA_ME2N"
```

`import_inventario` e `import_compras` aceptan tanto un archivo individual como una
carpeta completa (importan todos los `.csv` que encuentren dentro).

Los reimportes son seguros: `import_categorias` y `import_materiales` actualizan por
código; `import_inventario` ignora duplicados (mismo material+fecha+área); si necesitas
recargar compras de un año, borra antes esas filas (`LineaCompra.objects.filter(anio_origen=2024).delete()`)
para no duplicar líneas.

## Roles y autenticación

Todas las páginas piden iniciar sesión. Hay dos roles, implementados como grupos de Django:

- **admin**: además del dashboard, entra al panel de Django (`/admin/`).
- **coordinador**: solo el dashboard.

Todavía no hay permisos por área: cualquier usuario ve todas las páginas.

Primero, crea los grupos de roles (una sola vez):

```powershell
.\venv\Scripts\python.exe manage.py setup_roles
```

Luego crea usuarios asignándoles un rol:

```powershell
.\venv\Scripts\python.exe manage.py crear_usuario juan --password "clave-segura" --rol admin
.\venv\Scripts\python.exe manage.py crear_usuario maria --password "clave-segura" --rol coordinador
```

`crear_usuario` es idempotente: si el usuario ya existe, actualiza su contraseña y su rol.
Las contraseñas no se escriben en este repositorio.

## Uso

```powershell
# Desarrollo (el uso diario es con waitress, ver abajo)
.\venv\Scripts\python.exe manage.py runserver

# Prueba de humo después de un cambio: todas las páginas, filtros, detalles y descargas
.\venv\Scripts\python.exe manage.py revisar_paginas

# Cifras de meses cerrados contra la foto guardada (IPU, PMP, compras, S&OP...)
.\venv\Scripts\python.exe manage.py verificar_calculos
```

## Operación diaria (dashboard web para gerencia)

### Levantar el dashboard

```powershell
powershell -ExecutionPolicy Bypass -File iniciar_servidor.ps1
# abrir http://127.0.0.1:8000/
```

Corre en modo producción: `waitress` (servidor WSGI con varios hilos) y
`DJANGO_DEBUG=False` (un error no muestra código). `runserver` queda solo para
desarrollo.

No hace falta levantarlo a mano. La tarea programada `UPERU_Dashboard` corre
`iniciar_servidor.ps1` al iniciar sesión (con 1 minuto de retraso, sin límite de
tiempo y hasta 3 reintentos si se cae). Para reiniciarlo después de cambiar
código: detener los procesos `python.exe` de waitress y correr
`Start-ScheduledTask UPERU_Dashboard`.

### Carga diaria automática

La tarea programada `UPERU_ActualizarTablasMM` (al iniciar sesión y a las 4 AM)
ejecuta `C:\Users\BLAZO\Scripts\actualizar_tablas_mm.ps1`:

1. Copia las tablas SAP desde OneDrive.
2. `import_todo`: importa solo lo nuevo o modificado, con el tiempo de cada paso
   en el log. Un log por corrida en `C:\Users\BLAZO\Scripts\logs\`
   (`actualizar_tablas_mm_AAAA-MM-DD_HHMM.log`, se guardan 30 días;
   `actualizar_tablas_mm.log` es siempre la última).
   - MB51, MB5B y ZMM056 solo recargan el mes en curso, más el anterior hasta el
     día 10 (cierre contable). Los meses cerrados no se tocan salvo con `--forzar`.
   - ME2N se recarga completo cuando cambia.
   - Cada importador corre en una sola transacción: si falla, no deja datos a medias.
   - Si una tabla falla, se sigue con las demás y termina con código 2 (carga
     parcial); la tabla que falló conserva los datos de la carga anterior.
3. `exportar_planificacion_html`: regenera el HTML compartido de Planificación.
4. `calentar_cache`: abre todas las páginas para dejarlas calculadas.
5. `exportar_sitio_html`: copia estática por área en la carpeta de SharePoint.

### Por qué es rápido

- **Caché de consultas en disco** (`.cache_consultas/`, ver `dashboard/queries.py`).
  - La comparten el servidor y la tarea programada.
  - En memoria, cada proceso guarda lo más usado hasta 1 GB (`UPERU_CACHE_MEMORIA_MB`).
  - No vence por tiempo: cada importación que carga datos cambia la versión y
    todo se recalcula con los datos nuevos, sin reiniciar el servidor.
  - Se puede borrar sin riesgo: se regenera sola.
- **Lectura de Excel con `python-calamine`**, 4 a 5 veces más rápida que
  openpyxl. `UPERU_LECTOR_EXCEL=openpyxl` fuerza el lector anterior.
- **SQLite en modo WAL**, con más memoria (ver `config/settings.py`). El
  dashboard lee mientras la carga escribe.
- **Índices y planes de consulta revisados.** Donde SQLite elegía mal el índice,
  la consulta lo corrige con `+columna` (comentado en cada caso).

| | Antes | Ahora |
|---|---|---|
| Primera apertura de Overview | ~136 s | ~0.3 s |
| Primera apertura de las demás páginas | 2 a 10 s | 0.1 a 0.2 s |
| Carga diaria (mes en curso y ME2N completo) | ~21 min | ~2 min |

## Despliegue en Azure

La app ya está preparada para producción: `config/settings.py` lee `SECRET_KEY`, `DEBUG`,
`ALLOWED_HOSTS`, `DATABASE_URL`, etc. desde variables de entorno (con valores por defecto
que mantienen el desarrollo local igual que hasta ahora, usando SQLite). En producción se
usa **Azure Database for PostgreSQL** en vez de SQLite (una Web App no conserva un
archivo `db.sqlite3` local de forma segura entre reinicios y despliegues).

Esto requiere tener [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
instalado y haber hecho `az login` con tu cuenta. Los comandos son PowerShell.

### 1. Recursos base

```powershell
$RG = "rg-indicadores-gestion"
$LOCATION = "eastus2"                     # o la región más cercana a ti
$PLAN = "plan-indicadores"
$DJANGO_APP = "indicadores-admin"         # define la URL: <nombre>.azurewebsites.net
$PG_SERVER = "pg-indicadores"
$PG_ADMIN = "pgadmin"
$PG_PASSWORD = "<clave-fuerte-aqui>"
$DB_NAME = "indicadores"

az group create --name $RG --location $LOCATION

# Plan Linux (B1 ~ USD 13/mes; usa F1 si solo quieres probar gratis)
az appservice plan create --resource-group $RG --name $PLAN --is-linux --sku B1
```

### 2. Base de datos (Postgres Flexible Server, capa económica ~USD 12-25/mes)

```powershell
az postgres flexible-server create `
  --resource-group $RG --name $PG_SERVER --location $LOCATION `
  --admin-user $PG_ADMIN --admin-password $PG_PASSWORD `
  --sku-name Standard_B1ms --tier Burstable --storage-size 32 --version 16 `
  --public-access 0.0.0.0-255.255.255.255   # acceso abierto temporal para poder migrar datos desde tu PC

az postgres flexible-server db create --resource-group $RG --server-name $PG_SERVER --database-name $DB_NAME
```

Después de cargar los datos (paso 5) puedes restringir `--public-access` solo a las IPs
salientes de la Web App (`az webapp show --query outboundIpAddresses`).

### 3. La Web App

```powershell
az webapp create --resource-group $RG --plan $PLAN --name $DJANGO_APP --runtime "PYTHON:3.12"
az webapp config set --resource-group $RG --name $DJANGO_APP `
  --startup-file "python manage.py collectstatic --noinput && gunicorn config.wsgi --bind=0.0.0.0:8000 --timeout 600"

$DB_URL = "postgres://${PG_ADMIN}:${PG_PASSWORD}@${PG_SERVER}.postgres.database.azure.com:5432/${DB_NAME}?sslmode=require"

az webapp config appsettings set --resource-group $RG --name $DJANGO_APP --settings `
  DATABASE_URL=$DB_URL `
  DJANGO_SECRET_KEY="<genera-una-clave-larga-y-distinta-a-la-de-local>" `
  DJANGO_DEBUG="False" `
  DJANGO_ALLOWED_HOSTS="$DJANGO_APP.azurewebsites.net" `
  DJANGO_CSRF_TRUSTED_ORIGINS="https://$DJANGO_APP.azurewebsites.net" `
  SCM_DO_BUILD_DURING_DEPLOYMENT="1" `
  WEBSITES_PORT="8000"
```

Genera `DJANGO_SECRET_KEY` con, por ejemplo:
`.\venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`

### 4. Desplegar el código

Deploy por zip (no requiere GitHub):

```powershell
Compress-Archive -Path * -DestinationPath ..\deploy.zip -Force -Exclude venv,db.sqlite3,staticfiles
az webapp deploy --resource-group $RG --name $DJANGO_APP --src-path ..\deploy.zip --type zip
```

### 5. Migrar el esquema y cargar los datos

Se hace desde tu PC apuntando temporalmente a la base de Azure (no hace falta subir los
CSV a ningún lado):

```powershell
$env:DATABASE_URL = $DB_URL
.\venv\Scripts\python.exe manage.py migrate
.\venv\Scripts\python.exe manage.py setup_roles
.\venv\Scripts\python.exe manage.py crear_usuario admin --password "clave-fuerte" --rol admin
.\venv\Scripts\python.exe manage.py import_todo "E:\Exceles Control de Gestión\CSVs_Convertidos"
Remove-Item Env:\DATABASE_URL   # tu entorno local vuelve a usar SQLite
```

Al terminar: `https://indicadores-admin.azurewebsites.net` (dashboard, pide login) y
`/admin/` (panel de Django, solo admins).

### Notas

- Costo aproximado con esta configuración: Plan B1 (~USD 13/mes) + Postgres B1ms
  (~USD 12-25/mes según región/almacenamiento). Para solo probar, cambia `--sku B1` por
  `F1` (gratis) en el App Service Plan — con límite de CPU/hora y sin *always on*.
- La reimportación de compras (`import_compras`) no tiene *unique constraint*, así que si
  vuelves a correr `import_todo` sobre la BD de Azure, primero borra `LineaCompra` para no
  duplicar filas (igual que en local).
- Si prefieres no depender de Azure CLI, todos estos mismos recursos se pueden crear desde
  el Portal de Azure (Resource Group → App Services → Postgres Flexible Server) usando los
  mismos nombres y variables de entorno de arriba.

## Modelo de datos (`gestion/models.py`)

- `Categoria`: catálogo de grupos de artículos (familia/categoría/tipo de material).
- `Material`: maestro de materiales (ZMM060), enlazado a `Categoria`.
- `InventarioMensual`: stock y valorización de cierre por material y mes (MB5B).
- `LineaCompra`: líneas de pedido/solicitud de compra (ME2N), con año de origen para
  facilitar reimportes incrementales.
