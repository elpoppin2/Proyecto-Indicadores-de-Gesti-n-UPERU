from django.db import models


class Categoria(models.Model):
    """Catálogo de grupos de artículos (archivo CATEGORÍAS OFICIAL.csv)."""

    grupo_articulo = models.CharField(max_length=40, primary_key=True, verbose_name="Grupo art.")
    descripcion = models.CharField(max_length=255, blank=True, verbose_name="Descripcion Grupo art.")
    familia = models.CharField(max_length=255, blank=True, db_index=True)
    categoria = models.CharField(max_length=255, blank=True, db_index=True)
    tipo_material = models.CharField(max_length=255, blank=True, db_index=True)

    class Meta:
        verbose_name = "Categoría"
        verbose_name_plural = "Categorías"
        ordering = ["grupo_articulo"]

    def __str__(self):
        return f"{self.grupo_articulo} - {self.descripcion}"


class Material(models.Model):
    """Maestro de materiales (archivo MM60_SUPPLY.csv, transacción SQ01/ZMM060)."""

    codigo = models.CharField(max_length=40, primary_key=True, verbose_name="Material")
    descripcion = models.CharField(max_length=255, blank=True, verbose_name="Número de material")
    categoria = models.ForeignKey(
        Categoria, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="materiales",
    )
    jerarquia_productos = models.CharField(max_length=60, blank=True, verbose_name="Jquía. Productos")
    unidad_medida_base = models.CharField(max_length=20, blank=True, verbose_name="UMB")
    tipo_repuesto = models.CharField(max_length=60, blank=True)
    material_cancerigeno = models.CharField(max_length=20, blank=True, verbose_name="Mat. Cancerígeno")
    perf = models.CharField(max_length=20, blank=True, help_text="Campo SAP 'Perf' (origen sin documentar).")
    centro = models.CharField(max_length=20, blank=True, verbose_name="Ce.")
    grupo_compras = models.CharField(max_length=20, blank=True, verbose_name="GCp")
    campo_c = models.CharField(max_length=20, blank=True, verbose_name="C", help_text="Campo SAP 'C' (origen sin documentar).")
    numero_fabricante = models.CharField(max_length=60, blank=True, verbose_name="NºFb")
    fabricante = models.CharField(max_length=255, blank=True, db_index=True)
    material_critico = models.CharField(max_length=20, blank=True, verbose_name="Mat. Critico")
    estado_material = models.CharField(max_length=60, blank=True, verbose_name="Estado mat.vál.p.tod.ce.")
    sm = models.CharField(max_length=20, blank=True, verbose_name="SM")
    campo_mat = models.CharField(max_length=20, blank=True, verbose_name="Mat", help_text="Campo SAP 'Mat' (origen sin documentar).")
    campo_ce = models.CharField(max_length=20, blank=True, verbose_name="Ce", help_text="Campo SAP 'Ce' (distinto de Centro).")
    fecha_creacion = models.DateField(null=True, blank=True, verbose_name="Creado")

    class Meta:
        verbose_name = "Material"
        verbose_name_plural = "Materiales"
        ordering = ["codigo"]

    def __str__(self):
        return f"{self.codigo} - {self.descripcion}"


class InventarioMensual(models.Model):
    """Stock de cierre mensual por material (archivos TX. MB5B - INVENTARIO TOTAL <mes> <año>.csv)."""

    material_codigo = models.CharField(max_length=40, db_index=True, verbose_name="Material")
    texto_breve = models.CharField(max_length=255, blank=True)
    stock_cierre = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    unidad_medida = models.CharField(max_length=20, blank=True)
    valor_final = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    denominacion_tipo_material = models.CharField(max_length=255, blank=True)
    denominacion_grupo_articulos = models.CharField(max_length=255, blank=True)
    fecha = models.DateField(db_index=True, verbose_name="A fecha")
    area_valoracion = models.CharField(max_length=20, blank=True)

    class Meta:
        verbose_name = "Inventario mensual"
        verbose_name_plural = "Inventario mensual"
        constraints = [
            models.UniqueConstraint(
                fields=["material_codigo", "fecha", "area_valoracion"],
                name="uniq_inventario_material_fecha_area",
            )
        ]
        indexes = [
            models.Index(fields=["fecha", "material_codigo"]),
        ]

    def __str__(self):
        return f"{self.material_codigo} @ {self.fecha}"


class CategoriaServicio(models.Model):
    """Categoría de servicio por grupo de compras (GCp). Se usa como respaldo para
    clasificar líneas de compra cuyo grupo de artículos no tiene categoría en
    CATEGORÍAS OFICIAL (compras de servicios, que no pasan por un material)."""

    grupo_compras = models.CharField(max_length=20, primary_key=True, verbose_name="GCp")
    responsable = models.CharField(max_length=100, blank=True, verbose_name="Denominación")
    categoria_servicio = models.CharField(max_length=100, verbose_name="Categorías Servicios")

    class Meta:
        verbose_name = "Categoría de servicio"
        verbose_name_plural = "Categorías de servicio"
        ordering = ["grupo_compras"]

    def __str__(self):
        return f"{self.grupo_compras} - {self.categoria_servicio}"


class LineaCompra(models.Model):
    """Líneas de pedido/solicitud de compra (archivos ME2N_SUPPLY_<año>.csv)."""

    autor = models.CharField(max_length=60, blank=True)
    prioridad_orden = models.CharField(max_length=20, blank=True, verbose_name="Prioridad de la orden")
    texto_prioridad = models.CharField(max_length=100, blank=True, verbose_name="Texto para prioridad")
    condicion_pago = models.CharField(max_length=20, blank=True, verbose_name="CPag")
    incoterm = models.CharField(max_length=20, blank=True, verbose_name="Incot")
    incoterm_lugar1 = models.CharField(max_length=100, blank=True, verbose_name="Inc.Lugar1")
    incoterm_lugar2 = models.CharField(max_length=100, blank=True, verbose_name="Inc.Lugar2")
    tipo_cambio = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True, verbose_name="Tp.cambio")
    clase_valoracion = models.CharField(max_length=20, blank=True, verbose_name="Cl.valor.")
    fecha_liberacion_pedido = models.DateField(null=True, blank=True, verbose_name="Fecha liberación Pedido")
    fecha_documento = models.DateField(null=True, blank=True, db_index=True, verbose_name="Fecha doc.")
    fecha_entrega = models.DateField(null=True, blank=True, db_index=True, verbose_name="Fe.entrega")
    fecha_entrega_estimada = models.DateField(null=True, blank=True, verbose_name="FeEntEst")
    doc_compra = models.CharField(max_length=40, blank=True, db_index=True, verbose_name="Doc.compr.")
    pos_doc_compra = models.CharField(max_length=20, blank=True, verbose_name="Pos.")
    clase_documento = models.CharField(max_length=20, blank=True, verbose_name="Cl.")
    tipo = models.CharField(max_length=20, blank=True, verbose_name="T")
    grupo_compras = models.CharField(max_length=20, blank=True, verbose_name="GCp")
    proveedor = models.CharField(max_length=255, blank=True, db_index=True)
    cuenta_proveedor = models.CharField(max_length=40, blank=True, verbose_name="Número de cuenta del proveedor")
    material_codigo = models.CharField(max_length=40, blank=True, db_index=True, verbose_name="Material")
    texto_breve = models.CharField(max_length=255, blank=True, verbose_name="Texto breve")
    grupo_articulo = models.CharField(max_length=40, blank=True, db_index=True, verbose_name="Grupo art.")
    campo_b = models.CharField(max_length=20, blank=True, verbose_name="B", help_text="Campo SAP 'B' (origen sin documentar).")
    campo_p = models.CharField(max_length=20, blank=True, verbose_name="P", help_text="Campo SAP 'P' (origen sin documentar).")
    campo_i = models.CharField(max_length=20, blank=True, verbose_name="I", help_text="Campo SAP 'I' (origen sin documentar).")
    centro = models.CharField(max_length=20, blank=True, verbose_name="Ce.")
    almacen = models.CharField(max_length=20, blank=True, verbose_name="Alm.")
    cantidad_pedido = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Ctd.pedido")
    unidad_medida_pedido = models.CharField(max_length=20, blank=True, verbose_name="UMP")
    precio_neto = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Prc.neto")
    moneda = models.CharField(max_length=10, blank=True, verbose_name="Mon.")
    campo_por = models.CharField(max_length=20, blank=True, verbose_name="por", help_text="Campo SAP 'por' (origen sin documentar).")
    solicitud_pedido = models.CharField(max_length=40, blank=True, db_index=True, verbose_name="Sol.pedido")
    pos_solicitud = models.CharField(max_length=20, blank=True, verbose_name="Pos. (solicitud)")
    solicitante = models.CharField(max_length=100, blank=True)
    numero_necesidad = models.CharField(max_length=40, blank=True, verbose_name="Nº nec.")
    unidad_medida_solicitud = models.CharField(max_length=20, blank=True, verbose_name="UMP (solicitud)")
    organizacion_compras = models.CharField(max_length=20, blank=True, verbose_name="OrgC")
    numero_jerarquia = models.CharField(max_length=60, blank=True)
    inicio_periodo_validez = models.DateField(null=True, blank=True, verbose_name="IniPerVa")
    fin_periodo_validez = models.DateField(null=True, blank=True, verbose_name="FinPerVal")
    contrato = models.CharField(max_length=40, blank=True, verbose_name="Contr.")
    pos_contrato = models.CharField(max_length=20, blank=True, verbose_name="Pos. (contrato)")
    valor_previsto_cabecera = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Val.prev.(cab.)")
    moneda_valor_previsto = models.CharField(max_length=10, blank=True, verbose_name="Mon. (val. previsto)")
    cantidad_por_entregar = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Por Entregar (Cantidad)")
    unidad_medida_entrega = models.CharField(max_length=20, blank=True, verbose_name="UMP (entrega)")
    valor_neto = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    valor_por_calcular = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Por Calcular (Valor)")
    valor_por_entregar = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Por Entregar (Valor)")
    en_contrato = models.CharField(max_length=20, blank=True, verbose_name="EnC")
    status_tratamiento = models.CharField(max_length=100, blank=True, verbose_name="Status tratamiento doc.compras")
    cantidad_entrada = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)

    # Año de origen del archivo (2024, 2025, 2026...), útil para reingestas incrementales.
    anio_origen = models.PositiveSmallIntegerField(db_index=True)

    class Meta:
        verbose_name = "Línea de compra"
        verbose_name_plural = "Líneas de compra"
        indexes = [
            models.Index(fields=["material_codigo", "fecha_documento"]),
            models.Index(fields=["proveedor", "fecha_documento"]),
        ]

    def __str__(self):
        return f"{self.doc_compra}/{self.pos_doc_compra} - {self.material_codigo}"
