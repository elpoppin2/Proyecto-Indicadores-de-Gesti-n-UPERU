from django.contrib import admin

from .models import Categoria, CategoriaServicio, Material, InventarioMensual, LineaCompra


@admin.register(Categoria)
class CategoriaAdmin(admin.ModelAdmin):
    list_display = ("grupo_articulo", "descripcion", "familia", "categoria", "tipo_material")
    search_fields = ("grupo_articulo", "descripcion", "familia", "categoria")
    list_filter = ("familia", "categoria", "tipo_material")


@admin.register(CategoriaServicio)
class CategoriaServicioAdmin(admin.ModelAdmin):
    list_display = ("grupo_compras", "responsable", "categoria_servicio")
    search_fields = ("grupo_compras", "responsable", "categoria_servicio")
    list_filter = ("categoria_servicio",)


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = ("codigo", "descripcion", "categoria", "centro", "fabricante", "material_critico")
    search_fields = ("codigo", "descripcion", "fabricante")
    list_filter = ("centro", "material_critico", "estado_material")
    autocomplete_fields = ("categoria",)


@admin.register(InventarioMensual)
class InventarioMensualAdmin(admin.ModelAdmin):
    list_display = ("material_codigo", "fecha", "area_valoracion", "stock_cierre", "valor_final")
    search_fields = ("material_codigo", "texto_breve")
    list_filter = ("fecha", "area_valoracion")
    date_hierarchy = "fecha"


@admin.register(LineaCompra)
class LineaCompraAdmin(admin.ModelAdmin):
    list_display = ("doc_compra", "pos_doc_compra", "material_codigo", "proveedor", "fecha_documento", "valor_neto", "anio_origen")
    search_fields = ("doc_compra", "material_codigo", "proveedor", "solicitud_pedido")
    list_filter = ("anio_origen", "grupo_compras", "moneda")
    date_hierarchy = "fecha_documento"
