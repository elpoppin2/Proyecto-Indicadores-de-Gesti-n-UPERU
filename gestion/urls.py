from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.inicio, name="inicio"),
    path("login/", auth_views.LoginView.as_view(template_name="gestion/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("portada/", views.portada, name="portada"),
    path("informe-mensual/", views.informe_mensual, name="informe_mensual"),
    path("overview/", views.overview, name="overview"),
    path("overview/exportar/", views.exportar_excel, name="exportar_excel_overview"),
    path("planificacion/", views.planificacion, name="planificacion"),
    path("planificacion/premios/exportar/", views.exportar_premios, name="exportar_premios"),
    path("planificacion/energia/exportar/", views.exportar_energia, name="exportar_energia"),
    path("planificacion/precios-sop/exportar/", views.descargar_precios_sop, name="descargar_precios_sop"),
    path("planificacion/precios-sop/plantilla/exportar/", views.plantilla_precios_sop, name="plantilla_precios_sop"),
    path("planificacion/asistente/", views.asistente_planificacion, name="asistente_planificacion"),
    path("planificacion/escenarios/", views.escenarios_lista, name="escenarios_lista"),
    path("planificacion/escenarios/guardar/", views.escenarios_guardar, name="escenarios_guardar"),
    path("planificacion/escenarios/eliminar/", views.escenarios_eliminar, name="escenarios_eliminar"),
    path("planificacion/escenarios/aplicar/", views.escenarios_aplicar, name="escenarios_aplicar"),
    path("planificacion/escenarios/copias/", views.escenarios_copias, name="escenarios_copias"),
    path("planificacion/presentacion/", views.presentacion_inventarios, name="presentacion_inventarios"),
    path("logistica-materias-primas/", views.logistica_materias_primas, name="logistica_materias_primas"),
    path("reglas-y-metas/", views.reglas_y_metas, name="reglas_y_metas"),
    path("<slug:slug>/exportar/", views.exportar_excel, name="exportar_excel_categoria"),
    path("<slug:slug>/rotacion-materiales/", views.rotacion_materiales_pagina, name="rotacion_materiales_pagina"),
    path("<slug:slug>/detalle/<slug:clave>/", views.desglose_filas, name="desglose_filas"),
    path("<slug:slug>/", views.categoria_detalle, name="categoria_detalle"),
]
