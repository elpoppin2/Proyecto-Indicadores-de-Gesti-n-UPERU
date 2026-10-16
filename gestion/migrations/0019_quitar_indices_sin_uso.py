"""Quita los índices que las consultas del dashboard no usan o que repiten el
comienzo de otro índice (proyecto más ligero, pedido por el usuario el
2026-10-16). Medido con EXPLAIN QUERY PLAN de todas las páginas, desgloses,
descargas y el HTML estático con la caché vacía.

En la base solo se ejecuta DROP INDEX: un AlterField de db_index haría que
Django reconstruya en SQLite la tabla de MB51 entera (3.75 M filas)."""
from django.db import migrations, models

# (tabla, índice, columnas) de los índices de un solo campo que se quitan.
_INDICES_CAMPO = [
    ("gestion_movimientomaterial", "gestion_movimientomaterial_origen_datos_5840bd3c", "origen_datos"),
    ("gestion_movimientomaterial", "gestion_movimientomaterial_material_codigo_7ca4f5ad", "material_codigo"),
    ("gestion_movimientomaterial", "gestion_movimientomaterial_centro_4c913b20", "centro"),
    ("gestion_movimientomaterial", "gestion_movimientomaterial_proveedor_3d216695", "proveedor"),
    ("gestion_movimientomaterial", "gestion_movimientomaterial_documento_material_51c3e36c", "documento_material"),
    ("gestion_movimientomaterial", "gestion_movimientomaterial_clase_movimiento_56cc9b42", "clase_movimiento"),
    ("gestion_movimientomaterial", "gestion_movimientomaterial_fecha_documento_121967cf", "fecha_documento"),
    ("gestion_movimientomaterial", "gestion_movimientomaterial_posicion_documento_material_c8daf2ca",
     "posicion_documento_material"),
    ("gestion_inventariomensual", "gestion_inventariomensual_fecha_d239d32b", "fecha"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("gestion", "0018_ajustes_precio_fbl3n"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    f'DROP INDEX IF EXISTS "{indice}"',
                    reverse_sql=f'CREATE INDEX IF NOT EXISTS "{indice}" ON "{tabla}" ("{columna}")',
                )
                for tabla, indice, columna in _INDICES_CAMPO
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="movimientomaterial", name="origen_datos",
                    field=models.CharField(choices=[("MATERIALES", "Materiales"), ("SERVICIOS", "Servicios")], max_length=12),
                ),
                migrations.AlterField(
                    model_name="movimientomaterial", name="material_codigo",
                    field=models.CharField(blank=True, max_length=40, verbose_name="Material"),
                ),
                migrations.AlterField(
                    model_name="movimientomaterial", name="centro",
                    field=models.CharField(blank=True, max_length=20, verbose_name="Centro"),
                ),
                migrations.AlterField(
                    model_name="movimientomaterial", name="proveedor",
                    field=models.CharField(blank=True, max_length=40, verbose_name="Proveedor"),
                ),
                migrations.AlterField(
                    model_name="movimientomaterial", name="documento_material",
                    field=models.CharField(blank=True, max_length=40, verbose_name="Documento material"),
                ),
                migrations.AlterField(
                    model_name="movimientomaterial", name="clase_movimiento",
                    field=models.CharField(blank=True, max_length=20, verbose_name="Clase de movimiento"),
                ),
                migrations.AlterField(
                    model_name="movimientomaterial", name="fecha_documento",
                    field=models.DateField(blank=True, null=True, verbose_name="Fecha de documento"),
                ),
                migrations.AlterField(
                    model_name="movimientomaterial", name="posicion_documento_material",
                    field=models.CharField(blank=True, max_length=20, verbose_name="Posición doc.mat."),
                ),
                migrations.AlterField(
                    model_name="inventariomensual", name="fecha",
                    field=models.DateField(verbose_name="A fecha"),
                ),
            ],
        ),
        # Índices compuestos sin uso (DROP INDEX directo también en SQLite).
        migrations.RemoveIndex(model_name="movimientomaterial", name="gestion_mov_centro_3eaa46_idx"),
        migrations.RemoveIndex(model_name="inventariomensual", name="gestion_inv_fecha_63fa5e_idx"),
    ]
