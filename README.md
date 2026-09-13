# Indicadores de Gestión

Aplicación Django + Streamlit para indicadores de gestión de materiales, inventario y
compras, alimentada desde extracciones CSV de SAP (MB5B, ME2N, SQ01/ZMM060) y un
catálogo de categorías.

## Arquitectura

- **Django** (`config/`, app `gestion/`): modelos, base de datos (SQLite en local,
  PostgreSQL en Azure — ver más abajo) y comandos de management para ingestar los CSV.
- **Streamlit** (`dashboard/`): dashboards que leen la misma base de datos (vía el
  ORM/conexión de Django) para mostrar indicadores. Es una app independiente que
  comparte la base de datos con Django, no un módulo de la misma.

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

Hay dos roles, implementados como grupos de Django y validados en el login de Streamlit:

- **admin**: acceso total (CRUD completo vía el panel de Django + todos los dashboards,
  incluida la sección "Administración").
- **coordinador**: solo visualización (dashboards de Streamlit; sin acceso al panel de
  Django, sin permisos de escritura).

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
Ya se crearon dos cuentas de ejemplo (**cámbiales la contraseña o bórralas antes de
usar esto en producción**):

| Usuario       | Contraseña       | Rol         |
|---------------|------------------|-------------|
| `admin`       | `CambiaEsta123!` | admin       |
| `coordinador` | `CambiaEsta123!` | coordinador |

El login de Streamlit valida usuario/contraseña contra la tabla `auth_user` de Django
(`dashboard/auth.py`, función `requerir_login`), y el rol determina qué páginas aparecen
en la barra de navegación (`dashboard/app.py`) — un coordinador nunca ve ni puede
navegar a la página "Administración".

## Uso

```powershell
# Panel de administración de Django (solo entran usuarios con rol admin)
.\venv\Scripts\python.exe manage.py runserver
# abrir http://localhost:8000/admin/

# Dashboards de Streamlit (pide usuario/contraseña al entrar)
.\venv\Scripts\python.exe -m streamlit run dashboard\app.py
```

## Despliegue en Azure

La app ya está preparada para producción: `config/settings.py` lee `SECRET_KEY`, `DEBUG`,
`ALLOWED_HOSTS`, `DATABASE_URL`, etc. desde variables de entorno (con valores por defecto
que mantienen el desarrollo local igual que hasta ahora, usando SQLite). En producción se
usa **Azure Database for PostgreSQL** en vez de SQLite: dos Web Apps (Django y Streamlit)
no pueden compartir un archivo `db.sqlite3` local entre sí de forma segura, pero sí una
base de datos gestionada.

Esto requiere tener [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
instalado y haber hecho `az login` con tu cuenta. Los comandos son PowerShell.

### 1. Recursos base

```powershell
$RG = "rg-indicadores-gestion"
$LOCATION = "eastus2"                     # o la región más cercana a ti
$PLAN = "plan-indicadores"
$DJANGO_APP = "indicadores-admin"         # define la URL: <nombre>.azurewebsites.net
$STREAMLIT_APP = "indicadores-dashboard"
$PG_SERVER = "pg-indicadores"
$PG_ADMIN = "pgadmin"
$PG_PASSWORD = "<clave-fuerte-aqui>"
$DB_NAME = "indicadores"

az group create --name $RG --location $LOCATION

# Plan Linux compartido por las dos apps (B1 ~ USD 13/mes; usa F1 si solo quieres probar gratis)
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
salientes de tus dos Web Apps (`az webapp show --query outboundIpAddresses`).

### 3. Las dos Web Apps

```powershell
az webapp create --resource-group $RG --plan $PLAN --name $DJANGO_APP --runtime "PYTHON:3.12"
az webapp config set --resource-group $RG --name $DJANGO_APP `
  --startup-file "python manage.py collectstatic --noinput && gunicorn config.wsgi --bind=0.0.0.0:8000 --timeout 600"

az webapp create --resource-group $RG --plan $PLAN --name $STREAMLIT_APP --runtime "PYTHON:3.12"
az webapp config set --resource-group $RG --name $STREAMLIT_APP `
  --startup-file "python -m streamlit run dashboard/app.py --server.port 8000 --server.address 0.0.0.0 --server.headless true"

$DB_URL = "postgres://${PG_ADMIN}:${PG_PASSWORD}@${PG_SERVER}.postgres.database.azure.com:5432/${DB_NAME}?sslmode=require"

foreach ($app in @($DJANGO_APP, $STREAMLIT_APP)) {
  az webapp config appsettings set --resource-group $RG --name $app --settings `
    DATABASE_URL=$DB_URL `
    DJANGO_SECRET_KEY="<genera-una-clave-larga-y-distinta-a-la-de-local>" `
    DJANGO_DEBUG="False" `
    DJANGO_ALLOWED_HOSTS="$DJANGO_APP.azurewebsites.net,$STREAMLIT_APP.azurewebsites.net" `
    DJANGO_CSRF_TRUSTED_ORIGINS="https://$DJANGO_APP.azurewebsites.net" `
    SCM_DO_BUILD_DURING_DEPLOYMENT="1" `
    WEBSITES_PORT="8000"
}
```

Genera `DJANGO_SECRET_KEY` con, por ejemplo:
`.\venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`

### 4. Desplegar el código

Deploy por zip (no requiere GitHub); el mismo zip sirve para las dos apps porque solo
cambia el *startup command*:

```powershell
Compress-Archive -Path * -DestinationPath ..\deploy.zip -Force -Exclude venv,db.sqlite3,staticfiles
az webapp deploy --resource-group $RG --name $DJANGO_APP --src-path ..\deploy.zip --type zip
az webapp deploy --resource-group $RG --name $STREAMLIT_APP --src-path ..\deploy.zip --type zip
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

Al terminar: `https://indicadores-admin.azurewebsites.net/admin/` (panel de Django, solo
admins) y `https://indicadores-dashboard.azurewebsites.net` (dashboards, pide login).

### Notas

- Costo aproximado con esta configuración: Plan B1 (~USD 13/mes) + Postgres B1ms
  (~USD 12-25/mes según región/almacenamiento). Para solo probar, cambia `--sku B1` por
  `F1` (gratis) en el App Service Plan — con límite de CPU/hora y sin *always on*.
  El dashboard de Streamlit necesita WebSocket, que sí funciona en F1.
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
