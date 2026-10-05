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
    path("planificacion/asistente/", views.asistente_planificacion, name="asistente_planificacion"),
    path("<slug:slug>/exportar/", views.exportar_excel, name="exportar_excel_categoria"),
    path("<slug:slug>/rotacion-materiales/", views.rotacion_materiales_pagina, name="rotacion_materiales_pagina"),
    path("<slug:slug>/detalle/<slug:clave>/", views.desglose_filas, name="desglose_filas"),
    path("<slug:slug>/", views.categoria_detalle, name="categoria_detalle"),
]
