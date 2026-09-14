from django.db import migrations

DATOS = [
    ("G02", "", "ENERGÍA ELÉCTRICA"),
    ("G03", "C.Villafán", "MATERIAS PRIMAS"),
    ("G08", "D.Villalobos", "MRO"),
    ("G09", "Serv.Indust.", "SERVICIOS INDUSTRIALES"),
    ("G11", "L.Albatrino", "MATERIAS PRIMAS"),
    ("G12", "D.Alfaro", "SERVICIOS ADMINISTRATIVOS"),
    ("G13", "Miguel Dasso", "SUMINISTROS INDUSTRIALES"),
    ("G30", "Traslado MP,PT,PP", "SERVICIOS INDUSTRIALES"),
    ("G32", "OP DMPRA", "SERVICIOS INDUSTRIALES"),
    ("G33", "OP DPRA", "SERVICIOS INDUSTRIALES"),
    ("G35", "OP DPRC", "SERVICIOS INDUSTRIALES"),
    ("G42", "M.Egusquiza", "SUMINISTROS INDUSTRIALES"),
    ("G45", "D.Alfaro", "SERVICIOS ADMINISTRATIVOS"),
    ("G46", "Serv.Indust.", "SERVICIOS INDUSTRIALES"),
    ("G55", "C.Fernandez", "MRO"),
    ("G56", "E.Bohl F.E.", "SERVICIOS INDUSTRIALES"),
    ("G57", "S.Honorio", "SUMINISTROS INDUSTRIALES"),
    ("G58", "M.Aguirre", "MRO"),
    ("G61", "D.Alfaro F.E.", "SERVICIOS ADMINISTRATIVOS"),
    ("G64", "D.Villalobos F.E.", "MRO"),
    ("G65", "Erika Correa", "SUMINISTROS INDUSTRIALES"),
    ("G66", "Katherine Loa", "EMPAQUES"),
    ("G62", "", "SUMINISTROS INDUSTRIALES"),
    ("G68", "", "SUMINISTROS INDUSTRIALES"),
]


def cargar_datos(apps, schema_editor):
    CategoriaServicio = apps.get_model("gestion", "CategoriaServicio")
    for grupo_compras, responsable, categoria_servicio in DATOS:
        CategoriaServicio.objects.update_or_create(
            grupo_compras=grupo_compras,
            defaults={"responsable": responsable, "categoria_servicio": categoria_servicio},
        )


def revertir_datos(apps, schema_editor):
    CategoriaServicio = apps.get_model("gestion", "CategoriaServicio")
    CategoriaServicio.objects.filter(grupo_compras__in=[d[0] for d in DATOS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("gestion", "0003_categoriaservicio"),
    ]

    operations = [
        migrations.RunPython(cargar_datos, revertir_datos),
    ]
