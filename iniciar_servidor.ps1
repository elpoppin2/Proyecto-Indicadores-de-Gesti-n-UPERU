# Levanta el dashboard en modo produccion (para uso diario de gerencia), en
# vez de "manage.py runserver" (servidor de desarrollo de Django):
#  - waitress: servidor WSGI de produccion que funciona en Windows, con varios
#    hilos (varias personas / varias secciones a la vez).
#  - DJANGO_DEBUG=False: ante un error no muestra codigo ni rutas internas, y
#    Django no guarda cada consulta en memoria.
#  - collectstatic: con DEBUG=False los estilos se sirven desde "staticfiles"
#    con nombres versionados (el navegador los guarda y solo los vuelve a pedir
#    cuando cambian). Se corre en cada arranque para tomar cualquier cambio.
#
# Uso:  powershell -ExecutionPolicy Bypass -File iniciar_servidor.ps1
# Luego abrir http://127.0.0.1:8000/

$proyecto = $PSScriptRoot
$python = Join-Path $proyecto "venv\Scripts\python.exe"
Set-Location $proyecto

$env:DJANGO_DEBUG = "False"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

& $python manage.py collectstatic --noinput | Out-Null
& $python -m waitress --listen=0.0.0.0:8000 --threads=8 --channel-timeout=300 config.wsgi:application
