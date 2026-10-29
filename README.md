# Indicadores de Gestión — Supply Chain

Dashboard Django de indicadores de gestión de Supply Chain: compras, proveedores,
inventario, precios e IPU, cuentas por pagar, OTIF y Planificación S&OP. Se alimenta de
extracciones de SAP (MB51, MB5B, ME2N, ZMM060, ZMM056, FBL3N), de los Excel del S&OP y
de un catálogo de categorías. La **Portada** es la vista gerencial (indicadores contra su
meta, con semáforo por categoría). Cada área tiene su página con pestañas, y cada número
puede abrirse fila por fila.

## Cómo está armado

```
SAP y Excel del S&OP ──► comandos import_* ──► base de datos (gestion/models.py)
                                                    │
        dashboard/consultas/  SQL + pandas, con caché versionada (memoria y disco)
                                                    │
        gestion/vistas/       arma cada pestaña y guarda su HTML ya dibujado
                                                    │
        gestion/templates/    ──► navegador (localhost) y copias estáticas (SharePoint)
```

| Carpeta / archivo | Qué hay |
|---|---|
| `config/` | Configuración de Django. Todo lo sensible sale de variables de entorno o de `secretos/` (fuera de git). |
| `dashboard/consultas/` | Consultas y cálculos, un módulo por tema: `base` (caché, alcance, fechas), `reglas` (reglas de negocio en SQL que comparten todas las consultas), `inventario`, `compras`, `precios` (precio real, IPU), `detalle` (filas del detalle fila por fila) y, para el S&OP, `sop_base`, `sop_abastecimiento`, `sop_inventario`, `consumo_sop`, `importados`, `calidad`. |
| `dashboard/queries.py` | Fachada: re-exporta todo, así el resto del código usa `from dashboard import queries as q`. |
| `gestion/vistas/` | Un módulo por página: `portada`, `overview`, `categoria` (+ `categoria_pestanas`, `categoria_indicadores`), `planificacion` (+ `plan_*` por sección), `detalle`. `zonas.py` define las pestañas y `comun.py` lo compartido (filtros, navegación, caché del HTML). `gestion/views.py` es la fachada para `urls.py` y los comandos. |
| `gestion/charts.py` | Gráficos en SVG generados en el servidor, sin librerías de JavaScript. |
| `gestion/ejecutivo.py` | Portada, semáforo contra metas, cartera de oportunidades e informe mensual. |
| `gestion/desglose.py` | Detalle fila por fila y descarga a Excel de cada indicador. |
| `gestion/asistente.py` | Asistente de IA de Planificación (solo lectura; inactivo sin clave de API). |
| `gestion/metas.json` | Metas propuestas de la Portada (las confirmadas por categoría van en `reglas_locales.json`). |
| `gestion/management/commands/` | Carga de datos (`import_*`), caché (`calentar_cache`), copias estáticas (`exportar_*`) y verificación (`verificar_calculos`, `revisar_paginas`). |
| `datos_locales/` | Fuera de git: `reglas_locales.json` (cantidades y metas de la empresa) y la foto de `verificar_calculos`. |
| `herramientas/` | Scripts de uso puntual (copia del sitio a una fecha de corte). |

## Criterios del proyecto

Lo que hace que el tablero sea confiable, rápido y fácil de mantener. Sirven para cualquier
tablero nuevo del área.

1. **Una regla de negocio vive en un solo lugar.** Qué compras cuentan, el valor de
   compra, la categoría efectiva o el plazo de pago están en `dashboard/consultas/reglas.py`;
   todas las consultas y páginas usan esa misma definición. Un indicador nunca se calcula
   de dos formas.
2. **Cada número se puede auditar.** Cada indicador tiene su "Cómo se calcula" y su
   detalle fila por fila (`desglose.py`), con los mismos filtros que la cifra agregada: la
   suma de las filas cuadra con el número mostrado.
3. **El repositorio es público: ninguna cifra de la empresa en el código.** Ni montos,
   toneladas, porcentajes ni metas en código, comentarios o mensajes de commit. Las
   cantidades que la lógica necesita se leen de `datos_locales/reglas_locales.json`
   (`q.regla_local`), que no se sube. Antes de cada push se revisa `git diff origin/main`.
4. **La caché nunca muestra datos viejos.** No vence por tiempo: se invalida cuando entran
   datos nuevos (`marcar_datos_actualizados`) o cambia el código (huella de las consultas y
   de las vistas). Lo ya dibujado lleva además la fecha, el alcance y la fecha de
   modificación de `metas.json` y `reglas_locales.json`.
5. **Lo que no depende del usuario se dibuja una sola vez.** Cada pestaña se guarda ya
   dibujada por sus filtros (`html_guardado` en `gestion/vistas/comun.py`), y
   `calentar_cache` deja listas las vistas por defecto después de cada carga.
6. **Las cargas no dejan datos a medias.** Cada importador corre en una transacción, solo
   procesa archivos nuevos o modificados y se puede repetir sin duplicar.
7. **Se verifica antes de publicar.** Pruebas unitarias, cifras de los meses cerrados
   contra una foto (`verificar_calculos`) y todas las páginas y filtros (`revisar_paginas`);
   ver [Verificación](#verificación).
8. **Las copias compartidas salen del mismo código.** La copia de SharePoint y el HTML de
   Planificación se generan desde las mismas vistas y estilos del localhost; nunca se
   mantiene una copia aparte.
9. **Seguro por defecto.** Todas las páginas piden sesión, las claves están fuera del
   código y el servidor escucha solo en `127.0.0.1`.

## Agregar un indicador

1. **Consulta** en el módulo de `dashboard/consultas/` del tema. Usa las reglas de
   `reglas.py` (por ejemplo `FILTRO_COMPRAS_VALIDAS_LC`) en vez de repetir condiciones, y
   `_query` (SQL) o `@_cacheado` (cálculo en pandas) para que quede en la caché. Exponla en
   `dashboard/queries.py`.
2. **Detalle fila por fila** en `consultas/detalle.py` y `gestion/desglose.py`, con los
   mismos filtros que la consulta agregada.
3. **Vista**: el contexto de la pestaña en `gestion/vistas/` y el gráfico con
   `gestion/charts.py`. Si la pestaña es nueva, regístrala en `zonas.py`.
4. **Plantilla** en `gestion/templates/gestion/`, con su bloque "Cómo se calcula".
5. **Meta** (si tiene): propuesta en `metas.json`; la confirmada, en
   `reglas_locales.json`.
6. **Verificar**: `manage.py test gestion`, `verificar_calculos --guardar` (para incluirlo
   en la foto) y `revisar_paginas`.

## Instalación

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe manage.py migrate
```

## Carga de datos

`import_todo` carga todo desde la carpeta sincronizada de SAP, en este orden:
categorías (si está la carpeta), materiales (ZMM060), inventario (MB5B), compras (ME2N),
movimientos (MB51), plan de abastecimiento, plan de consumos y stock mínimo/máximo del
S&OP, ingresos físicos (ZMM056), solicitantes y ajustes de valor (FBL3N).

```powershell
.\venv\Scripts\python.exe manage.py import_todo "<carpeta DOCUMENTOS DESCARGA TABLAS MM>"
```

Cada tabla tiene también su comando (`import_materiales`, `import_compras`,
`import_movimientos`...), que acepta un archivo o una carpeta. Los reimportes son seguros:
solo se procesan archivos nuevos o modificados (`--forzar` reprocesa todo) y cada uno
reemplaza lo que cubre.

## Roles y autenticación

Todas las páginas piden iniciar sesión. Hay dos roles, implementados como grupos de Django:

- **admin**: además del dashboard, entra al panel de Django (`/admin/`).
- **coordinador**: solo el dashboard.

Todavía no hay permisos por área: cualquier usuario ve todas las páginas.

```powershell
.\venv\Scripts\python.exe manage.py setup_roles     # una sola vez: crea los grupos
.\venv\Scripts\python.exe manage.py crear_usuario juan --password "clave-segura" --rol admin
.\venv\Scripts\python.exe manage.py crear_usuario maria --password "clave-segura" --rol coordinador
```

`crear_usuario` es idempotente: si el usuario ya existe, actualiza su contraseña y su rol.
Las contraseñas no se escriben en este repositorio.

## Verificación

Después de cada cambio, en este orden:

```powershell
# 1. Pruebas unitarias (segundos, sin base de datos): formatos, fechas, semáforo, reglas
.\venv\Scripts\python.exe manage.py test gestion

# 2. Cifras de los 12 meses cerrados contra la foto guardada (IPU, PMP, compras, S&OP...)
#    --guardar toma una foto nueva cuando la diferencia es esperada
.\venv\Scripts\python.exe manage.py verificar_calculos

# 3. Prueba de humo: todas las páginas, pestañas, filtros, detalles y descargas
.\venv\Scripts\python.exe manage.py revisar_paginas
```

Luego se reinicia el servidor, se corre `calentar_cache` y se regeneran las copias
estáticas (`exportar_sitio_html` y `exportar_planificacion_html`).

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

- **Caché de consultas en disco** (`.cache_consultas/`, ver `dashboard/consultas/base.py`).
  - La comparten el servidor y la tarea programada.
  - En memoria, cada proceso guarda lo más usado hasta 1 GB (`UPERU_CACHE_MEMORIA_MB`).
  - No vence por tiempo: cada importación que carga datos cambia la versión y
    todo se recalcula con los datos nuevos, sin reiniciar el servidor.
  - Se puede borrar sin riesgo: se regenera sola.
- **Cada pestaña se guarda ya dibujada** (Planificación, categorías, Overview), por sus
  filtros: la segunda apertura no recalcula nada. El HTML se guarda sin la sangría de las
  plantillas (entre 15 y 30 % menos que enviar y que armar en el navegador).
- **Lectura de Excel con `python-calamine`**, 4 a 5 veces más rápida que
  openpyxl. `UPERU_LECTOR_EXCEL=openpyxl` fuerza el lector anterior.
- **SQLite en modo WAL**, con más memoria (ver `config/settings.py`). El
  dashboard lee mientras la carga escribe.
- **Índices y planes de consulta revisados.** Donde SQLite elegía mal el índice,
  la consulta lo corrige con `+columna` (comentado en cada caso).
- **Respuestas comprimidas** (gzip) y estáticos con nombre versionado (whitenoise): el
  navegador guarda los estilos y solo los vuelve a pedir cuando cambian.

| | Antes | Ahora |
|---|---|---|
| Primera apertura de Overview | ~136 s | ~0.03 s |
| Primera apertura de cada pestaña de categoría | 0.1 a 1 s | ~0.01 s |
| Las 63 páginas y pestañas, desde la caché en disco | ~11 s | ~1 s |
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

| Modelo | Origen | Qué guarda |
|---|---|---|
| `Categoria` | Catálogo de categorías | Grupo de artículos → familia, categoría y tipo de material. |
| `CategoriaServicio` | Catálogo de categorías | Categoría por grupo de compras, para las compras sin material (servicios). |
| `Material` | ZMM060 | Maestro de materiales, enlazado a su categoría. |
| `InventarioMensual` | MB5B | Stock y valor de cierre mensual por material. |
| `LineaCompra` | ME2N | Líneas de pedido y solicitud de compra, con su año de origen. |
| `MovimientoMaterial` | MB51 | Movimientos de materiales y servicios (entradas, consumos, traslados). |
| `Solicitante` | Información de solicitantes | Solicitante de compra con su área y gerencia (filtro GEP y Proyectos). |
| `PlanAbastecimientoSOP`, `PlanConsumoSOP`, `StockMinMaxSOP` | Excel del S&OP | Plan de abastecimiento, plan de consumos y stock mínimo/máximo por material, centro y mes. |
| `IngresoFisicoSOP` | ZMM056 | Tickets de báscula de Materias Primas y Combustibles. |
| `AjustePrecioFBL3N` | FBL3N | Ajustes al valor del stock que no pasan por MB51 (facturas, cambios de precio): precio real. |
| `ArchivoImportado` | Cargas | Qué archivo se importó y cuándo, para procesar solo lo nuevo. |
| `ConsultaAsistente` | Asistente de IA | Registro de cada pregunta: herramientas usadas, tokens y duración. |
