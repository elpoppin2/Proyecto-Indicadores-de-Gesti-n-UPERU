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
            # Índice "cubriente" de las series de Valor de Inventario (histórico
            # completo, ~1.5 M filas): trae todo lo que esas sumas necesitan,
            # así SQLite las resuelve leyendo solo el índice en vez de ir fila
            # por fila a la tabla (que es ancha: textos de descripción).
            models.Index(
                fields=["fecha", "material_codigo", "area_valoracion", "valor_final", "stock_cierre"],
                name="idx_inv_valor_cubriente",
            ),
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


class MovimientoMaterial(models.Model):
    """Movimientos de materiales/servicios (transacción MB51), archivos Excel en
    TABLA_MB51/TABLA_MB51_MATERIALES y TABLA_MB51/TABLA_MB51_SERVICIOS."""

    ORIGEN_MATERIALES = "MATERIALES"
    ORIGEN_SERVICIOS = "SERVICIOS"
    ORIGEN_OPCIONES = [
        (ORIGEN_MATERIALES, "Materiales"),
        (ORIGEN_SERVICIOS, "Servicios"),
    ]

    origen_datos = models.CharField(max_length=12, choices=ORIGEN_OPCIONES, db_index=True)

    material_codigo = models.CharField(max_length=40, blank=True, db_index=True, verbose_name="Material")
    sociedad = models.CharField(max_length=20, blank=True, verbose_name="Sociedad")
    centro = models.CharField(max_length=20, blank=True, db_index=True, verbose_name="Centro")
    nombre_centro = models.CharField(max_length=100, blank=True, verbose_name="Nombre 1")
    proveedor = models.CharField(max_length=40, blank=True, db_index=True, verbose_name="Proveedor")
    pedido = models.CharField(max_length=40, blank=True, verbose_name="Pedido")
    posicion = models.CharField(max_length=20, blank=True, verbose_name="Posición")
    descripcion_material = models.CharField(max_length=255, blank=True, verbose_name="Descripción material")
    documento_material = models.CharField(max_length=40, blank=True, db_index=True, verbose_name="Documento material")
    fecha_contabilizacion = models.DateField(null=True, blank=True, db_index=True, verbose_name="Fecha contabiliz.")
    cantidad = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Cantidad")
    cantidad_um_entrada = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Ctd.en UM entrada")
    unidad_medida_entrada = models.CharField(max_length=20, blank=True, verbose_name="Un.medida de entrada")
    clase_transaccion = models.CharField(max_length=20, blank=True, verbose_name="Clase de trans./eve.")
    clase_movimiento = models.CharField(max_length=20, blank=True, db_index=True, verbose_name="Clase de movimiento")
    usuario = models.CharField(max_length=60, blank=True, verbose_name="Nombre del usuario")
    destino_mercancia = models.CharField(max_length=100, blank=True, verbose_name="Dest.mercancía")
    importe_moneda_local = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Impte.mon.local")
    moneda = models.CharField(max_length=10, blank=True, verbose_name="Moneda")
    referencia = models.CharField(max_length=100, blank=True, verbose_name="Referencia")
    ejercicio_documento_material = models.CharField(max_length=20, blank=True, verbose_name="Ejerc.documento mat.")
    fecha_documento = models.DateField(null=True, blank=True, db_index=True, verbose_name="Fecha de documento")
    fecha_entrada = models.DateField(null=True, blank=True, verbose_name="Fecha de entrada")
    texto_clase_movimiento = models.CharField(max_length=100, blank=True, verbose_name="Texto clase de mov.")
    cuenta_mayor = models.CharField(max_length=40, blank=True, verbose_name="Cuenta de mayor")
    indicador_debe_haber = models.CharField(max_length=10, blank=True, verbose_name="Indicador Debe/Haber")
    orden = models.CharField(max_length=40, blank=True, verbose_name="Orden")
    centro_coste = models.CharField(max_length=40, blank=True, verbose_name="Centro de coste")
    almacen = models.CharField(max_length=20, blank=True, verbose_name="Almacén")
    motivo_movimiento = models.CharField(max_length=40, blank=True, verbose_name="Motivo movimiento")
    cliente = models.CharField(max_length=40, blank=True, verbose_name="Cliente")
    pedido_de_cliente = models.CharField(max_length=40, blank=True, verbose_name="Pedido de cliente")
    stock_especial = models.CharField(max_length=20, blank=True, verbose_name="Stock especial")
    posicion_documento_material = models.CharField(max_length=20, blank=True, db_index=True, verbose_name="Posición doc.mat.")
    activo_fijo = models.CharField(max_length=40, blank=True, verbose_name="Activo fijo")
    subnumero = models.CharField(max_length=20, blank=True, verbose_name="Subnúmero")
    contador = models.CharField(max_length=20, blank=True, verbose_name="Contador")
    hoja_ruta_operacion = models.CharField(max_length=40, blank=True, verbose_name="Nº hoja ruta oper.")
    texto_cabecera_documento = models.CharField(max_length=255, blank=True, verbose_name="Texto cab.documento")
    cantidad_umpp = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Cantidad en UMPP")
    um_precio_pedido = models.CharField(max_length=20, blank=True, verbose_name="UM precio pedido")
    unidad_medida_pedido = models.CharField(max_length=20, blank=True, verbose_name="Unidad medida pedido")
    cantidad_um_pedido = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Cantidad en UM ped.")
    clase_valoracion = models.CharField(max_length=20, blank=True, verbose_name="Clase de valoración")
    lote = models.CharField(max_length=40, blank=True, verbose_name="Lote")
    hora_entrada = models.TimeField(null=True, blank=True, verbose_name="Hora de entrada")
    documento_configurable = models.CharField(max_length=40, blank=True, verbose_name="Nº documento configurable")
    documento_almacen = models.CharField(max_length=40, blank=True, verbose_name="Documento de almacén")
    numero_almacen = models.CharField(max_length=20, blank=True, verbose_name="Nº almacén")
    importe_ext_moneda_local = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Impte.ext.mon.local")
    valor_venta = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Valor de venta")
    pedido_cliente = models.CharField(max_length=40, blank=True, verbose_name="Pedido cliente")
    reparto_pedido_cliente = models.CharField(max_length=20, blank=True, verbose_name="Reparto pedido clte.")
    pos_pedido_cliente_1 = models.CharField(max_length=20, blank=True, verbose_name="Pos.pedido cliente (1)")
    indicador_movimiento = models.CharField(max_length=20, blank=True, verbose_name="Ind.movimiento")
    consumo = models.CharField(max_length=20, blank=True, verbose_name="Consumo")
    indicador_entrada = models.CharField(max_length=20, blank=True, verbose_name="Indicador de entrada")
    pos_pedido_cliente_2 = models.CharField(max_length=20, blank=True, verbose_name="Pos.pedido cliente (2)")
    unidad_medida_base = models.CharField(max_length=20, blank=True, verbose_name="Unidad medida base")
    grafo = models.CharField(max_length=40, blank=True, verbose_name="Grafo")
    operacion = models.CharField(max_length=20, blank=True, verbose_name="Operación")
    elemento_pep = models.CharField(max_length=40, blank=True, verbose_name="Elemento PEP")
    reserva = models.CharField(max_length=40, blank=True, verbose_name="Reserva")
    pos_reserva_traslado = models.CharField(max_length=20, blank=True, verbose_name="Nº pos.reserva traslado")
    segmento_stock = models.CharField(max_length=40, blank=True, verbose_name="Segmento de stock")
    valor_pv_con_iva = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True, verbose_name="Valor PV con IVA")
    vale_acompanamiento_mercancias = models.CharField(max_length=40, blank=True, verbose_name="Vale acomp.mercancías")
    posicion_creada_automaticamente = models.CharField(max_length=20, blank=True, verbose_name="Posición creada automáticamente")
    linea_original = models.CharField(max_length=20, blank=True, verbose_name="Línea original")
    imputacion_multiple = models.CharField(max_length=20, blank=True, verbose_name="Imputación múltiple")
    contrato_marco = models.CharField(max_length=40, blank=True, verbose_name="Contrato marco")
    pos_contrato_sup = models.CharField(max_length=20, blank=True, verbose_name="Pos.contrato sup.")

    class Meta:
        verbose_name = "Movimiento de material"
        verbose_name_plural = "Movimientos de material"
        constraints = [
            models.UniqueConstraint(
                fields=["documento_material", "ejercicio_documento_material", "posicion_documento_material"],
                name="uniq_movimiento_documento_posicion",
            )
        ]
        indexes = [
            models.Index(fields=["material_codigo", "fecha_contabilizacion"]),
            models.Index(fields=["centro", "fecha_contabilizacion"]),
            # Índice de cobertura para el cálculo de consumo del Índice de Rotación:
            # filtra por clase de movimiento + rango de fechas y agrupa por material,
            # todo desde el índice sin tocar la tabla (3.5M filas).
            models.Index(
                fields=["clase_movimiento", "fecha_contabilizacion", "material_codigo", "cantidad"],
                name="idx_mov_consumo_rotacion",
            ),
            # Índice de cobertura para el cruce con LineaCompra (Lead Time de
            # proveedores): permite ubicar la Entrada de Mercancía real de un
            # pedido/posición sin escanear las ~1.3M filas de la tabla.
            models.Index(
                fields=["pedido", "posicion", "clase_movimiento", "fecha_contabilizacion"],
                name="idx_mov_pedido_posicion",
            ),
        ]

    def __str__(self):
        return f"{self.documento_material}/{self.posicion_documento_material} - {self.material_codigo or self.descripcion_material}"


class ArchivoImportado(models.Model):
    """Registro de qué archivo fuente SAP (MB5B/MB51/ME2N/ZMM060) se importó por
    última vez, con qué tamaño y fecha de modificación. import_todo corre a
    diario y de los ~70 archivos SAP normalmente solo cambia el del mes en
    curso; los comandos import_* usan esta tabla para saltarse los que no
    cambiaron desde la última carga en vez de reprocesarlos todos cada vez."""

    ruta = models.CharField(max_length=500, unique=True)
    tamano_bytes = models.BigIntegerField()
    fecha_modificacion = models.DateTimeField()
    filas_importadas = models.IntegerField(default=0)
    fecha_importado = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Archivo importado"
        verbose_name_plural = "Archivos importados"

    def __str__(self):
        return self.ruta


class PlanAbastecimientoSOP(models.Model):
    """Plan mensual de abastecimiento (proceso S&OP), cargado a mano por
    Planificación en un Excel con una fila por material/centro y una columna
    por mes proyectado (archivo 'Abastecimiento SOP - <mes> <año>.xlsx').
    Se guarda tanto el mes en que se emitió el plan (mes_snapshot, tomado del
    nombre del archivo) como el mes proyectado (mes_planificado, tomado de la
    columna): así, si se vuelve a cargar el plan al mes siguiente, no se pierde
    la proyección anterior y se puede comparar cómo cambió. Solo aplica a
    Materias Primas y Energía/Combustibles. Definido por el usuario."""

    material_codigo = models.CharField(max_length=40, db_index=True, verbose_name="Material")
    material_descripcion = models.CharField(max_length=255, blank=True)
    centro_destino = models.CharField(max_length=20, db_index=True, verbose_name="Centro destino")
    destino = models.CharField(max_length=100, blank=True)
    mes_snapshot = models.DateField(db_index=True, verbose_name="Mes de emisión del plan")
    mes_planificado = models.DateField(db_index=True, verbose_name="Mes proyectado")
    cantidad_plan = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)

    class Meta:
        verbose_name = "Plan de abastecimiento (S&OP)"
        verbose_name_plural = "Planes de abastecimiento (S&OP)"
        constraints = [
            models.UniqueConstraint(
                fields=["material_codigo", "centro_destino", "mes_snapshot", "mes_planificado"],
                name="uniq_plan_abastecimiento_sop",
            )
        ]
        indexes = [
            models.Index(fields=["mes_planificado", "material_codigo"]),
        ]

    def __str__(self):
        return f"{self.material_codigo} @ {self.centro_destino} - plan {self.mes_planificado}"


class PlanConsumoSOP(models.Model):
    """Plan mensual de consumo (proceso S&OP), mismo formato que
    PlanAbastecimientoSOP ('Consumos SOP - <mes> <año>.xlsx', carpeta 'TABLA
    SOP CONSUMOS'). Con los dos planes y el inventario de cierre del mes
    anterior se proyecta la evolución del inventario:
    Stock fin de mes = Stock inicial + Abastecimiento − Consumo.
    El Excel trae el consumo con signo negativo (salida de almacén); acá se
    guarda en positivo (cantidad consumida), para que la fórmula se lea tal
    cual. Definido por el usuario."""

    material_codigo = models.CharField(max_length=40, db_index=True, verbose_name="Material")
    material_descripcion = models.CharField(max_length=255, blank=True)
    centro_destino = models.CharField(max_length=20, db_index=True, verbose_name="Centro destino")
    destino = models.CharField(max_length=100, blank=True)
    mes_snapshot = models.DateField(db_index=True, verbose_name="Mes de emisión del plan")
    mes_planificado = models.DateField(db_index=True, verbose_name="Mes proyectado")
    cantidad_consumo = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)

    class Meta:
        verbose_name = "Plan de consumo (S&OP)"
        verbose_name_plural = "Planes de consumo (S&OP)"
        constraints = [
            models.UniqueConstraint(
                fields=["material_codigo", "centro_destino", "mes_snapshot", "mes_planificado"],
                name="uniq_plan_consumo_sop",
            )
        ]

    def __str__(self):
        return f"{self.material_codigo} @ {self.centro_destino} - consumo {self.mes_planificado}"


class StockMinMaxSOP(models.Model):
    """Stock mínimo y máximo por material/centro y mes (proceso S&OP), cargado
    por Planificación en 'MinMax SOP - <mes> <año>.xlsx' (carpeta 'TABLA SOP
    MIN MAX'; hojas 'Mínimo' y 'Máximo', mismo formato que Abastecimiento).
    Se guarda tal cual viene (una celda vacía = None); al consultar, un mes
    vacío repite el último valor definido del material (ver
    queries.minmax_sop), así solo hay que escribir el mes en que cambia.
    Definido por el usuario."""

    material_codigo = models.CharField(max_length=40, db_index=True, verbose_name="Material")
    material_descripcion = models.CharField(max_length=255, blank=True)
    centro_destino = models.CharField(max_length=20, db_index=True, verbose_name="Centro destino")
    mes_snapshot = models.DateField(db_index=True, verbose_name="Mes de emisión")
    mes_planificado = models.DateField(db_index=True, verbose_name="Mes")
    stock_minimo = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    stock_maximo = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)

    class Meta:
        verbose_name = "Stock mínimo/máximo (S&OP)"
        verbose_name_plural = "Stocks mínimos/máximos (S&OP)"
        constraints = [
            models.UniqueConstraint(
                fields=["material_codigo", "centro_destino", "mes_snapshot", "mes_planificado"],
                name="uniq_stock_minmax_sop",
            )
        ]

    def __str__(self):
        return f"{self.material_codigo} @ {self.centro_destino} - min/max {self.mes_planificado}"


class IngresoFisicoSOP(models.Model):
    """Ingresos físicos reales de Materias Primas y Energía/Combustibles
    (tickets de báscula, transacción ZMM056), usados para medir el
    abastecimiento real contra PlanAbastecimientoSOP. No se toma de MB51: la
    báscula es la fuente de verdad para estos materiales. Se importan todos
    los tickets con Estado Ticket != 'ANU' (anulado) — no solo 'PRO': un
    ticket 'REG'/'NAC'/'REV'/'ATN' ya se pesó en báscula (siempre trae Pesaje
    Neto), solo que su Documento material en SAP puede estar pendiente. El
    'Centro' es 'Centro Salida' (columna A), no el 'Centro' destino (columna
    BX): Centro Salida es constante por archivo — U001 en todo el Excel de
    Atocongo, U002 en todo el de Condorcocha, verificado en varios meses — o
    sea, identifica la planta que originó el ticket, que es el nivel al que
    compara PlanAbastecimientoSOP.centro_destino (también U001/U002, no las
    ~10 subdestinos que trae el 'Centro' destino). Definido por el usuario."""

    planta_origen = models.CharField(max_length=20, blank=True, verbose_name="Planta (carpeta origen)")
    ticket_registro = models.CharField(max_length=40, blank=True, db_index=True)
    centro = models.CharField(max_length=20, db_index=True, verbose_name="Centro Salida (planta)")
    material_codigo = models.CharField(max_length=40, db_index=True)
    descripcion_material = models.CharField(max_length=255, blank=True)
    fecha_ingreso = models.DateField(null=True, blank=True, db_index=True)
    cantidad = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True,
        verbose_name="Pesaje neto húmedo (toneladas)",
    )
    unidad_medida_base = models.CharField(max_length=20, blank=True)
    proveedor_codigo = models.CharField(max_length=40, blank=True)
    proveedor = models.CharField(max_length=255, blank=True, db_index=True)
    documento_material = models.CharField(max_length=40, blank=True)
    posicion = models.CharField(max_length=20, blank=True)
    # Tipo de operación del ticket de báscula: 1 = Recepción de terceros, 5 =
    # Recepción propia (cantera/traslado entre plantas) — ambos son ingresos
    # reales; 4 = Despacho/salida (p.ej. clínker o cemento saliendo hacia otra
    # planta o destino) y 6 = otro despacho (harina cruda recuperada) — no son
    # ingresos, se guardan igual por trazabilidad pero se excluyen al filtrar
    # por Recepción (ver ingresos_fisicos_por_material en dashboard/queries.py).
    # Deducido cruzando materiales/centros de los Excel reales de ambas
    # plantas: los tickets Tipo 1/5 siempre van a un centro propio de esa
    # planta; los Tipo 4/6 van a centros/destinos variados de despacho.
    # Definido por el usuario.
    tipo_operacion = models.CharField(max_length=10, blank=True, db_index=True)

    # Calidad (análisis químico de laboratorio, columnas "Factor ..." de
    # ZMM056). Un mismo Número de muestra cubre varios tickets (un lote). En el
    # Excel, 0.0 = no analizado (ningún material tiene de verdad 0% de CaO o
    # 0 kcal/kg): se guarda como None para no promediar ceros. Qué parámetros
    # tiene cada material depende del material (calizas: CaO/MgO/SO3; carbones:
    # cenizas/volátiles/PCS/HGI; etc.). Pedido por el usuario.
    numero_muestra = models.CharField(max_length=40, blank=True)
    humedad = models.FloatField(null=True, blank=True, verbose_name="Humedad %")
    cao = models.FloatField(null=True, blank=True, verbose_name="CaO %")
    sio2 = models.FloatField(null=True, blank=True, verbose_name="SiO2 %")
    so3 = models.FloatField(null=True, blank=True, verbose_name="SO3 %")
    mgo = models.FloatField(null=True, blank=True, verbose_name="MgO %")
    fe2o3 = models.FloatField(null=True, blank=True, verbose_name="Fe2O3 %")
    al2o3 = models.FloatField(null=True, blank=True, verbose_name="Al2O3 %")
    insolubles = models.FloatField(null=True, blank=True, verbose_name="Insolubles %")
    cenizas = models.FloatField(null=True, blank=True, verbose_name="Cenizas %")
    volatiles = models.FloatField(null=True, blank=True, verbose_name="Volátiles %")
    azufre = models.FloatField(null=True, blank=True, verbose_name="Azufre %")
    pcs = models.FloatField(null=True, blank=True, verbose_name="PCS (kcal/kg)")
    hgi = models.FloatField(null=True, blank=True, verbose_name="HGI")
    yeso = models.FloatField(null=True, blank=True, verbose_name="Gypsum %")
    humedad_total = models.FloatField(null=True, blank=True, verbose_name="Humedad total %")
    azufre_elt = models.FloatField(null=True, blank=True, verbose_name="S (Elt) %")
    sialfe = models.FloatField(null=True, blank=True, verbose_name="SiO2+Al2O3+Fe2O3 %")

    # Premio o castigo por calidad (columna "Premio o Castigo" de ZMM056): monto
    # en soles del ticket, positivo = premio, negativo = castigo. Ya viene
    # sumado en el importe de la entrada de MB51 (importe = precio base ×
    # toneladas secas + premio), y la cantidad de esa entrada es el pesaje
    # neto SECO. Pedido por el usuario.
    premio = models.FloatField(null=True, blank=True, verbose_name="Premio o castigo (S/)")
    cantidad_seca = models.FloatField(null=True, blank=True, verbose_name="Pesaje neto seco (toneladas)")

    class Meta:
        verbose_name = "Ingreso físico (S&OP)"
        verbose_name_plural = "Ingresos físicos (S&OP)"
        constraints = [
            # (centro, ticket_registro) y no (documento_material, posicion):
            # varios estados válidos (REG/NAC/REV/ATN) no tienen Documento
            # material todavía, pero ticket_registro es único dentro de cada
            # planta (centro = Centro Salida) — verificado sin duplicados en
            # varios meses de ambas plantas.
            models.UniqueConstraint(
                fields=["centro", "ticket_registro"],
                name="uniq_ingreso_fisico_sop_centro_ticket",
            )
        ]
        indexes = [
            models.Index(fields=["fecha_ingreso", "material_codigo"]),
            models.Index(fields=["centro", "fecha_ingreso"]),
        ]

    def __str__(self):
        return f"{self.material_codigo} @ {self.centro} - {self.fecha_ingreso}"


class ConsultaAsistente(models.Model):
    """Registro de cada pregunta al asistente de IA (ver gestion/asistente.py):
    quién preguntó, qué respondió, qué datos consultó y cuántos tokens usó.
    Sirve de auditoría (qué información salió hacia el servicio de IA) y para
    estimar el costo. Pedido por el usuario."""

    usuario = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)
    fecha = models.DateTimeField(auto_now_add=True, db_index=True)
    seccion = models.CharField(max_length=40, default="planificacion")
    pregunta = models.TextField()
    respuesta = models.TextField(blank=True)
    herramientas = models.JSONField(default=list, blank=True, verbose_name="Datos consultados")
    modelo = models.CharField(max_length=60, blank=True)
    tokens_entrada = models.PositiveIntegerField(default=0)
    tokens_salida = models.PositiveIntegerField(default=0)
    duracion_segundos = models.FloatField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        verbose_name = "Consulta al asistente IA"
        verbose_name_plural = "Consultas al asistente IA"
        ordering = ["-fecha"]

    def __str__(self):
        return f"{self.fecha:%Y-%m-%d %H:%M} · {self.usuario} · {self.pregunta[:60]}"
