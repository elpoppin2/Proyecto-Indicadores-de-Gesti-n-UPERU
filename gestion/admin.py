from django.contrib import admin

from .models import (
    Categoria, CategoriaServicio, ConsultaAsistente, Material, InventarioMensual, LineaCompra, MovimientoMaterial,
)


@admin.register(ConsultaAsistente)
class ConsultaAsistenteAdmin(admin.ModelAdmin):
    """Auditoría del asistente de IA: qué se preguntó, qué datos consultó y
    cuánto costó. Solo lectura (es un registro, no se edita)."""
    list_display = ("fecha", "usuario", "pregunta_corta", "modelo", "tokens_entrada", "tokens_salida", "duracion_segundos", "con_error")
    list_filter = ("seccion", "modelo", "usuario")
    search_fields = ("pregunta", "respuesta")
    date_hierarchy = "fecha"

    @admin.display(description="Pregunta")
    def pregunta_corta(self, obj):
        return obj.pregunta[:90]

    @admin.display(boolean=True, description="Error")
    def con_error(self, obj):
        return bool(obj.error)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


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


@admin.register(MovimientoMaterial)
class MovimientoMaterialAdmin(admin.ModelAdmin):
    list_display = ("documento_material", "posicion_documento_material", "material_codigo", "centro", "clase_movimiento", "fecha_contabilizacion", "importe_moneda_local", "origen_datos")
    search_fields = ("documento_material", "material_codigo", "descripcion_material", "proveedor")
    list_filter = ("origen_datos", "centro", "clase_movimiento", "moneda")
    date_hierarchy = "fecha_contabilizacion"
