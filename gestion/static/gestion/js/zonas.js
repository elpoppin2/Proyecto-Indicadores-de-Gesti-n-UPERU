/* Pestañas (zonas) de Planificación: Indicadores, Cumplimiento, Consumo,
   Inventario, Precios e IPU, Calidad y energía. Pedido por el usuario
   ("lavado de cara" por zonas, y que todo sea lo más fluido posible).

   - La página trae solo Indicadores. Cada otra zona se pide al servidor al
     abrirla (?_zona=consumo) y, apenas la página queda tranquila, se precargan
     todas en segundo plano, una por una: al hacer clic ya están.
   - La zona activa queda en el hash de la URL (#consumo): un enlace
     compartido o un F5 vuelve a la misma pestaña, y los filtros de cada
     sección (base.html) la conservan. Un enlace a una sección (#sec-consumo)
     abre la zona que la contiene.
   - Si cambia un filtro en cualquier zona, Indicadores (gráficos de todas)
     se vuelve a pedir la próxima vez que se abre, y los números clave de
     arriba de esa zona (?_kpis=zona) se actualizan al momento.
   - Animaciones sutiles: la barra de la pestaña activa se desliza, la zona
     entra desde el lado hacia el que se movió (desde un estado ya visible,
     sin pantallazo en blanco), los paneles de Indicadores aparecen en
     cascada, las barras crecen, y al ordenar una tabla las filas
     se asientan. Todo se apaga con "reducir movimiento" del sistema. */

/* Filtro Material con varios materiales y grupos (Calizas, Puzolanas...),
   en todas las secciones de Planificación (web y HTML estático). Pedido por
   el usuario. Cada filtro es un <div class="filtro-multi"> con un <select
   multiple data-multi> oculto (las opciones y lo elegido) y, en la web, un
   <input data-multi-valor> con los códigos separados por coma, que es lo que
   viaja al servidor. Acá se arma el botón con el resumen ("Todos", "Calizas
   (5)", "3 materiales") y el panel con buscador, botones de grupo y casillas.
   El grupo sale de la descripción del material (primera palabra). */
(function () {
  var GRUPOS = [
    [/^CALIZA/, 'Calizas'], [/^PUZOLANA/, 'Puzolanas'], [/^CARB[OÓ]N/, 'Carbones'], [/^YESO/, 'Yesos'],
    [/^ARCILLA/, 'Arcillas'], [/^S[IÍ]LICE/, 'Sílices'], [/^ESCORIA/, 'Escorias'], [/^[OÓ]XIDO/, 'Óxido de fierro'],
    [/^CL[IÍ]NKER/, 'Clínker'], [/^PELET/, 'Pelets'], [/^MINERAL/, 'Minerales'], [/^(COQUE|PET ?COKE)/, 'Coque']
  ];
  function descripcion(opcion) {
    var t = opcion.textContent, i = t.indexOf(' — ');
    return (i >= 0 ? t.slice(i + 3) : t).trim();
  }
  function grupoDe(opcion) {
    var d = descripcion(opcion).toUpperCase();
    for (var i = 0; i < GRUPOS.length; i++) if (GRUPOS[i][0].test(d)) return GRUPOS[i][1];
    var palabra = d.split(/\s+/)[0] || 'Otros';
    return palabra.charAt(0) + palabra.slice(1).toLowerCase();
  }
  function enTitulo(s) { return s.toLowerCase().replace(/(^|[\s(-])\S/g, function (c) { return c.toUpperCase(); }); }
  function gruposDe(select) {
    var mapa = {};
    Array.prototype.forEach.call(select.options, function (o) { var g = grupoDe(o); (mapa[g] = mapa[g] || []).push(o); });
    return Object.keys(mapa).sort(function (a, b) { return a.localeCompare(b, 'es'); })
      .map(function (g) { return { nombre: g, opciones: mapa[g] }; });
  }
  function elegidas(select) { return Array.prototype.filter.call(select.options, function (o) { return o.selected; }); }
  function resumen(select) {
    var sel = elegidas(select);
    if (!sel.length) return 'Todos';
    if (sel.length === 1) return enTitulo(descripcion(sel[0]));
    var grupos = gruposDe(select);
    for (var i = 0; i < grupos.length; i++) {
      var g = grupos[i];
      if (g.opciones.length === sel.length && g.opciones.every(function (o) { return o.selected; })) return g.nombre + ' (' + sel.length + ')';
    }
    return sel.length + ' materiales';
  }

  function montar(c) {
    var select = c.querySelector('select[data-multi]');
    if (!select) return;
    if (!c._boton) {
      var boton = document.createElement('button');
      boton.type = 'button';
      boton.className = 'filtro-multi-boton';
      boton.id = select.id + '-boton';
      boton.setAttribute('aria-haspopup', 'dialog');
      boton.setAttribute('aria-expanded', 'false');
      c.appendChild(boton);
      c._boton = boton;
    }
    var texto = resumen(select);
    if (c._boton.textContent !== texto) c._boton.textContent = texto;  // sin cambios, sin mutación (observador)
    var titulo = elegidas(select).map(descripcion).join(', ');
    if (c._boton.title !== titulo) c._boton.title = titulo;
  }
  function montarTodos() { document.querySelectorAll('.filtro-multi').forEach(montar); }

  // Fija los materiales elegidos y avisa como cualquier filtro: en la web
  // cambia el campo oculto (base.html pide la sección); en el estático, el
  // propio <select> (sus funciones escuchan su "change").
  function fijar(select, codigos) {
    var conjunto = {};
    codigos.forEach(function (c) { conjunto[c] = true; });
    codigos.forEach(function (c) {
      if (!Array.prototype.some.call(select.options, function (o) { return o.value === c; })) {
        var o = document.createElement('option'); o.value = c; o.textContent = c; select.appendChild(o);
      }
    });
    var todos = select.options.length > 1 && Array.prototype.every.call(select.options, function (o) { return conjunto[o.value]; });
    Array.prototype.forEach.call(select.options, function (o) { o.selected = !todos && !!conjunto[o.value]; });
    var contenedor = select.closest('.filtro-multi');
    var oculto = contenedor && contenedor.querySelector('input[data-multi-valor]');
    if (contenedor) montar(contenedor);
    if (oculto) {
      oculto.value = elegidas(select).map(function (o) { return o.value; }).join(',');
      oculto.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }
  window.uperuFijarMateriales = function (idSelect, codigos) {
    var select = document.getElementById(idSelect);
    if (select) fijar(select, codigos);
  };

  // --- Panel (uno solo, en <body>: dentro de una sección con
  // content-visibility quedaba recortado) ---------------------------------
  var panel = null, actual = null;
  function cerrar(devolverFoco) {
    if (!panel) return;
    panel.remove();
    panel = null;
    if (actual) {
      actual._boton.setAttribute('aria-expanded', 'false');
      if (devolverFoco) actual._boton.focus();
    }
    actual = null;
  }
  function crear(etiqueta, clase, texto) {
    var n = document.createElement(etiqueta);
    if (clase) n.className = clase;
    if (texto != null) n.textContent = texto;
    return n;
  }
  function abrir(c) {
    cerrar();
    actual = c;
    var select = c.querySelector('select[data-multi]');
    var grupos = gruposDe(select);
    panel = crear('div', 'filtro-multi-panel');
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'Elegir materiales');

    var buscar = crear('input', 'filtro-multi-buscar');
    buscar.type = 'search';
    buscar.placeholder = 'Buscar material o código';
    buscar.setAttribute('aria-label', 'Buscar material o código');
    panel.appendChild(buscar);

    var chips = crear('div', 'filtro-multi-grupos');
    var lista = crear('div', 'filtro-multi-lista');
    grupos.forEach(function (g) {
      var chip = crear('button', 'chip-grupo', g.nombre + ' ');
      chip.type = 'button';
      chip.dataset.grupo = g.nombre;
      chip.appendChild(crear('span', 'chip-cuenta', String(g.opciones.length)));
      chips.appendChild(chip);

      var bloque = crear('div', 'grupo-bloque');
      bloque.dataset.grupo = g.nombre;
      var cabecera = crear('label', 'grupo-titulo');
      var checkGrupo = crear('input');
      checkGrupo.type = 'checkbox';
      checkGrupo.dataset.grupoCheck = g.nombre;
      cabecera.appendChild(checkGrupo);
      cabecera.appendChild(crear('span', null, g.nombre));
      bloque.appendChild(cabecera);
      g.opciones.forEach(function (o) {
        var fila = crear('label', 'opcion-material');
        var check = crear('input');
        check.type = 'checkbox';
        check.value = o.value;
        check.checked = o.selected;
        check.dataset.grupo = g.nombre;
        fila.appendChild(check);
        fila.appendChild(crear('span', 'opcion-nombre', enTitulo(descripcion(o))));
        fila.appendChild(crear('span', 'opcion-codigo', o.value));
        fila.dataset.buscar = (o.value + ' ' + descripcion(o)).toLowerCase();
        bloque.appendChild(fila);
      });
      lista.appendChild(bloque);
    });
    panel.appendChild(chips);
    panel.appendChild(lista);
    var pie = crear('div', 'filtro-multi-pie');
    var botonTodos = crear('button', 'btn-link', 'Todos (sin filtro)');
    botonTodos.type = 'button';
    botonTodos.dataset.accion = 'todos';
    var aplicar = crear('button', 'filtro-multi-aplicar', 'Aplicar');
    aplicar.type = 'button';
    aplicar.dataset.accion = 'aplicar';
    pie.appendChild(botonTodos);
    pie.appendChild(aplicar);
    panel.appendChild(pie);

    function checks(grupo) {
      return Array.prototype.slice.call(lista.querySelectorAll('input[type="checkbox"][value]' + (grupo ? '[data-grupo="' + grupo + '"]' : '')));
    }
    function estados() {
      grupos.forEach(function (g) {
        var cs = checks(g.nombre), n = cs.filter(function (x) { return x.checked; }).length;
        var cg = lista.querySelector('[data-grupo-check="' + g.nombre + '"]');
        cg.checked = n === cs.length;
        cg.indeterminate = n > 0 && n < cs.length;
        chips.querySelector('[data-grupo="' + g.nombre + '"]').setAttribute('aria-pressed', n === cs.length ? 'true' : 'false');
      });
      var total = checks().filter(function (x) { return x.checked; }).length;
      aplicar.textContent = total ? 'Aplicar (' + total + ')' : 'Aplicar (todos)';
    }
    function aplicarSeleccion() {
      fijar(select, checks().filter(function (x) { return x.checked; }).map(function (x) { return x.value; }));
      cerrar(true);
    }
    panel.addEventListener('change', function (e) {
      var grupo = e.target.dataset.grupoCheck;
      if (grupo) checks(grupo).forEach(function (x) { x.checked = e.target.checked; });
      estados();
    });
    panel.addEventListener('click', function (e) {
      var chip = e.target.closest('.chip-grupo');
      if (chip) {
        var cs = checks(chip.dataset.grupo), todosMarcados = cs.every(function (x) { return x.checked; });
        cs.forEach(function (x) { x.checked = !todosMarcados; });
        estados();
        return;
      }
      var accion = e.target.closest('[data-accion]');
      if (!accion) return;
      if (accion.dataset.accion === 'todos') checks().forEach(function (x) { x.checked = false; });
      aplicarSeleccion();
    });
    buscar.addEventListener('input', function () {
      var texto = buscar.value.trim().toLowerCase();
      lista.querySelectorAll('.grupo-bloque').forEach(function (bloque) {
        var visibles = 0;
        bloque.querySelectorAll('.opcion-material').forEach(function (fila) {
          var ver = !texto || fila.dataset.buscar.indexOf(texto) !== -1 || bloque.dataset.grupo.toLowerCase().indexOf(texto) !== -1;
          fila.hidden = !ver;
          if (ver) visibles++;
        });
        bloque.hidden = !visibles;
      });
    });
    panel.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { e.preventDefault(); cerrar(true); }
      else if (e.key === 'Enter' && e.target === buscar) { e.preventDefault(); aplicarSeleccion(); }
    });
    estados();
    document.body.appendChild(panel);
    ubicar();
    c._boton.setAttribute('aria-expanded', 'true');
    buscar.focus();
  }
  function ubicar() {
    if (!panel || !actual) return;
    var r = actual._boton.getBoundingClientRect();
    var ancho = Math.min(360, window.innerWidth - 16);
    panel.style.width = ancho + 'px';
    panel.style.left = Math.max(8, Math.min(r.left, window.innerWidth - ancho - 8)) + 'px';
    var abajo = window.innerHeight - r.bottom - 12;
    if (abajo >= 300 || abajo >= r.top) {
      panel.style.top = (r.bottom + 4) + 'px'; panel.style.bottom = '';
      panel.style.maxHeight = Math.max(abajo, 220) + 'px';
    } else {
      panel.style.bottom = (window.innerHeight - r.top + 4) + 'px'; panel.style.top = '';
      panel.style.maxHeight = (r.top - 12) + 'px';
    }
  }

  document.addEventListener('click', function (e) {
    var boton = e.target.closest && e.target.closest('.filtro-multi-boton');
    if (boton) {
      var c = boton.closest('.filtro-multi');
      if (actual === c) cerrar(); else abrir(c);
      return;
    }
    if (panel && !panel.contains(e.target)) cerrar();
  });
  window.addEventListener('resize', ubicar);
  window.addEventListener('scroll', function (e) { if (panel && !panel.contains(e.target)) ubicar(); }, true);

  // Filtros que llegan después (zonas pedidas al servidor, secciones
  // reemplazadas al filtrar) u opciones que cambian (estático): se montan o
  // actualizan en el siguiente cuadro.
  var pendiente = false;
  function tocaFiltro(m) {
    if (m.target.closest && m.target.closest('.filtro-multi')) return true;
    return Array.prototype.some.call(m.addedNodes, function (n) {
      return n.nodeType === 1 && (n.matches('.filtro-multi') || !!n.querySelector('.filtro-multi'));
    });
  }
  new MutationObserver(function (mutaciones) {
    if (pendiente || !mutaciones.some(tocaFiltro)) return;
    pendiente = true;
    requestAnimationFrame(function () { pendiente = false; montarTodos(); });
  }).observe(document.body, { childList: true, subtree: true });
  montarTodos();
})();

(function () {
  var barra = document.querySelector('.zonas');
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.zona-tab[data-zona]'));
  if (!barra || !tabs.length) return;
  var indicador = barra.querySelector('.zona-indicador');
  var reducido = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var indicadoresDesactualizado = false;

  function panelDe(tab) { return document.getElementById(tab.getAttribute('aria-controls')); }
  function tabDeZona(zona) {
    for (var i = 0; i < tabs.length; i++) if (tabs[i].dataset.zona === zona) return tabs[i];
    return null;
  }
  function tabActiva() {
    for (var i = 0; i < tabs.length; i++) if (tabs[i].getAttribute('aria-selected') === 'true') return tabs[i];
    return tabs[0];
  }

  // --- Barra deslizante bajo la pestaña activa -----------------------------
  function moverIndicador(tab) {
    if (!indicador) return;
    indicador.style.width = tab.offsetWidth + 'px';
    indicador.style.transform = 'translateX(' + tab.offsetLeft + 'px)';
  }
  if (indicador) {
    barra.classList.add('zonas-con-indicador');
    moverIndicador(tabActiva());
    // Recién ahora se habilita la transición (si no, la barra "viaja" al cargar).
    requestAnimationFrame(function () { requestAnimationFrame(function () { barra.classList.add('zonas-animadas'); }); });
    window.addEventListener('resize', function () { moverIndicador(tabActiva()); });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { moverIndicador(tabActiva()); });
  }

  // --- Entrada de una zona ---------------------------------------------------
  // La clase de entrada se quita cuando terminó todo (las tarjetas de
  // Indicadores entran en cascada y duran más que el panel); el contador
  // evita quitarle la clase a una entrada más nueva si se cambia rápido.
  function animarEntrada(panel, direccion) {
    if (reducido || !panel) return;
    panel.classList.remove('entrando', 'entrando-der', 'entrando-izq');
    void panel.offsetWidth;  // reinicia la animación si se repite
    panel.classList.add(direccion > 0 ? 'entrando-der' : direccion < 0 ? 'entrando-izq' : 'entrando');
    var turno = panel._turnoEntrada = (panel._turnoEntrada || 0) + 1;
    setTimeout(function () {
      if (panel._turnoEntrada === turno) panel.classList.remove('entrando', 'entrando-der', 'entrando-izq');
    }, 700);
  }
  document.addEventListener('animationend', function (evento) {
    var nodo = evento.target;
    if (!nodo.classList) return;
    if (nodo.classList.contains('seccion-actualizada')) nodo.classList.remove('seccion-actualizada');
    else if (nodo.tagName === 'TBODY') nodo.classList.remove('tabla-ordenada');
  });

  // --- Carga diferida de cada zona ------------------------------------------
  function urlZona(zona) {
    var qs = window.location.search;
    return window.location.pathname + (qs ? qs + '&' : '?') + '_zona=' + encodeURIComponent(zona);
  }

  function cargarZona(panel, forzar) {
    if (!panel || (!forzar && !panel.hasAttribute('data-pendiente'))) return Promise.resolve();
    if (panel._cargando) return panel._cargando;
    var zona = panel.id.replace(/^zona-/, '');
    // El HTML estático de OneDrive (sin servidor) trae todo el contenido y
    // define window.uperuCargarZona para dibujar cada zona la primera vez.
    if (window.uperuCargarZona) {
      window.uperuCargarZona(zona);
      panel.removeAttribute('data-pendiente');
      return Promise.resolve();
    }
    panel.setAttribute('aria-busy', 'true');
    if (forzar) panel.classList.add('zona-refrescando');
    panel._cargando = fetch(urlZona(zona), { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (respuesta) {
        if (!respuesta.ok) throw new Error('HTTP ' + respuesta.status);
        return respuesta.text();
      })
      .then(function (html) {
        panel.innerHTML = html;
        panel.removeAttribute('data-pendiente');
        if (window.uperuInicializarContenido) window.uperuInicializarContenido(panel);
        if (!panel.hidden) animarEntrada(panel, 0);
      })
      .catch(function () {
        panel.innerHTML = '<div class="zona-error"><p>No se pudo cargar esta zona.</p>' +
          '<button type="button" class="btn-link" data-reintentar-zona="' + zona + '">Reintentar</button></div>';
      })
      .then(function () {
        panel.removeAttribute('aria-busy');
        panel.classList.remove('zona-refrescando');
        panel._cargando = null;
      });
    return panel._cargando;
  }

  // Precarga en segundo plano, una zona por vez, cuando la página ya cargó.
  function precargar() {
    if (navigator.connection && navigator.connection.saveData) return;
    var pendientes = tabs.map(panelDe).filter(function (p) { return p && p.hasAttribute('data-pendiente'); });
    (function siguiente() {
      var panel = pendientes.shift();
      if (!panel) return;
      cargarZona(panel).then(function () { setTimeout(siguiente, 80); });
    })();
  }
  function cuandoTranquilo(fn) {
    if (window.requestIdleCallback) window.requestIdleCallback(fn, { timeout: 2500 });
    else setTimeout(fn, 700);
  }
  if (document.readyState === 'complete') cuandoTranquilo(precargar);
  else window.addEventListener('load', function () { cuandoTranquilo(precargar); });

  // --- Activar una zona -------------------------------------------------------
  function activar(tab, opciones) {
    opciones = opciones || {};
    var anterior = tabActiva();
    var direccion = tabs.indexOf(tab) - tabs.indexOf(anterior);
    tabs.forEach(function (t) {
      var activa = t === tab;
      t.setAttribute('aria-selected', activa ? 'true' : 'false');
      t.tabIndex = activa ? 0 : -1;
      var panel = panelDe(t);
      if (panel) panel.hidden = !activa;
    });
    moverIndicador(tab);
    // Barra más ancha que la pantalla (celular): se desplaza solo en
    // horizontal y solo si la pestaña quedó fuera de la vista. Antes era
    // scrollIntoView, que también movía la barra en vertical y la página.
    if (barra.scrollWidth > barra.clientWidth + 1) {
      var izq = tab.offsetLeft, der = izq + tab.offsetWidth;
      var destinoX = izq < barra.scrollLeft ? izq : (der > barra.scrollLeft + barra.clientWidth ? der - barra.clientWidth : null);
      if (destinoX !== null) barra.scrollTo({ left: destinoX, behavior: reducido ? 'auto' : 'smooth' });
    }
    if (opciones.foco) tab.focus();
    if (opciones.hash !== false) {
      var hash = tab === tabs[0] ? '' : '#' + tab.dataset.zona;
      window.history.replaceState(null, '', window.location.pathname + window.location.search + hash);
    }
    var panel = panelDe(tab);
    if (tab !== anterior) animarEntrada(panel, direccion);
    if (tab === tabs[0] && indicadoresDesactualizado) {
      indicadoresDesactualizado = false;
      cargarZona(panel, true);
    } else {
      cargarZona(panel);
    }
    // Al cambiar de zona desde abajo de la página, volver al inicio de la zona
    // (justo debajo de la barra de pestañas, que queda fija arriba).
    if (opciones.subir && panel) {
      var arriba = panel.getBoundingClientRect().top + window.scrollY - barra.offsetHeight - 14;
      if (window.scrollY > arriba) window.scrollTo({ top: Math.max(arriba, 0), behavior: 'auto' });
    }
    return panel;
  }

  // Hash -> zona: "#consumo" es una zona; "#sec-consumo" es una sección de
  // alguna zona (aunque todavía no se haya cargado: data-secciones).
  function desdeHash() {
    var id = decodeURIComponent(window.location.hash.slice(1));
    if (!id) return false;
    var tab = tabDeZona(id);
    if (tab) { activar(tab, { hash: false }); return true; }
    var panel = null;
    tabs.forEach(function (t) {
      var p = panelDe(t);
      if (p && (' ' + (p.dataset.secciones || '') + ' ').indexOf(' ' + id + ' ') !== -1) panel = p;
    });
    if (!panel) return false;
    tab = document.querySelector('.zona-tab[aria-controls="' + panel.id + '"]');
    activar(tab, { hash: false });
    cargarZona(panel).then(function () {
      var destino = document.getElementById(id);
      if (destino) destino.scrollIntoView({ block: 'start' });
    });
    return true;
  }

  // --- Eventos ----------------------------------------------------------------
  document.addEventListener('click', function (evento) {
    var tab = evento.target.closest('.zona-tab[data-zona]');
    if (tab) { activar(tab, { subir: true }); return; }

    var reintentar = evento.target.closest('[data-reintentar-zona]');
    if (reintentar) { cargarZona(document.getElementById('zona-' + reintentar.dataset.reintentarZona), true); return; }

    // "Ver detalle" de un gráfico de Indicadores.
    var ir = evento.target.closest('[data-ir-zona]');
    if (ir) {
      var destino = tabDeZona(ir.dataset.irZona);
      if (destino) activar(destino, { subir: true, foco: true });
      return;
    }

    // Proveedores de un material (Detalle por material): abrir / cerrar uno, o todos.
    var btnProveedores = evento.target.closest('.btn-proveedores');
    if (btnProveedores) {
      var abrir = btnProveedores.getAttribute('aria-expanded') !== 'true';
      btnProveedores.closest('table').querySelectorAll('.fila-hija[data-grupo="' + btnProveedores.dataset.grupo + '"]')
        .forEach(function (fila) { fila.hidden = !abrir; });
      btnProveedores.setAttribute('aria-expanded', abrir ? 'true' : 'false');
      return;
    }
    var todosProveedores = evento.target.closest('[data-proveedores-todos]');
    if (todosProveedores) {
      var seccionProv = todosProveedores.closest('section');
      var abrirTodos = todosProveedores.getAttribute('aria-pressed') !== 'true';
      seccionProv.querySelectorAll('.tabla-con-hijas .fila-hija').forEach(function (fila) { fila.hidden = !abrirTodos; });
      seccionProv.querySelectorAll('.btn-proveedores').forEach(function (b) { b.setAttribute('aria-expanded', abrirTodos ? 'true' : 'false'); });
      todosProveedores.setAttribute('aria-pressed', abrirTodos ? 'true' : 'false');
      todosProveedores.textContent = abrirTodos ? 'Ocultar proveedores' : 'Ver proveedores de todos';
      return;
    }

    var enlace = evento.target.closest('a[href^="#"]');
    if (enlace && enlace.getAttribute('href').length > 1) window.setTimeout(desdeHash, 0);

    // Tabla ordenada (base.html ya reordenó las filas): se asientan con un fundido.
    var th = evento.target.closest('.zona-panel table thead th');
    if (th && !reducido && !th.closest('table[data-paginado]')) {
      var tbody = th.closest('table').querySelector('tbody');
      if (tbody) { tbody.classList.remove('tabla-ordenada'); void tbody.offsetWidth; tbody.classList.add('tabla-ordenada'); }
    }

    // Tarjeta de precio / de calidad -> elige su centro y material en los
    // filtros de su sección y la recarga (mismo camino que cambiar un filtro).
    var tarjeta = evento.target.closest('[data-material-precios], [data-material-calidad]');
    if (tarjeta) {
      var tipo = tarjeta.hasAttribute('data-material-precios') ? 'precios' : 'calidad';
      var centro = document.getElementById('centro_' + tipo);
      var material = document.getElementById('material_' + tipo);
      if (!centro || !material) return;
      centro.value = tipo === 'precios' ? tarjeta.dataset.centroPrecios : tarjeta.dataset.centroCalidad;
      window.uperuFijarMateriales('material_' + tipo, [tipo === 'precios' ? tarjeta.dataset.materialPrecios : tarjeta.dataset.materialCalidad]);
    }
  });

  // Teclado (patrón WAI-ARIA de pestañas): flechas, Inicio y Fin.
  document.addEventListener('keydown', function (evento) {
    var tab = evento.target.closest && evento.target.closest('.zona-tab[data-zona]');
    if (!tab) return;
    var i = tabs.indexOf(tab), siguiente = null;
    if (evento.key === 'ArrowRight') siguiente = tabs[(i + 1) % tabs.length];
    else if (evento.key === 'ArrowLeft') siguiente = tabs[(i - 1 + tabs.length) % tabs.length];
    else if (evento.key === 'Home') siguiente = tabs[0];
    else if (evento.key === 'End') siguiente = tabs[tabs.length - 1];
    if (!siguiente) return;
    evento.preventDefault();
    activar(siguiente, { foco: true });
  });

  // Cambió un filtro: Indicadores se vuelve a pedir al abrirla y los números
  // clave de arriba de la zona de esa sección se piden de nuevo ya mismo.
  var pedidosKpis = {};
  function refrescarKpis(zona) {
    var franja = document.querySelector('.franja-kpis[data-kpis-zona="' + zona + '"]');
    if (!franja || window.uperuCargarZona) return;
    if (pedidosKpis[zona]) pedidosKpis[zona].abort();
    var control = pedidosKpis[zona] = new AbortController();
    var qs = window.location.search;
    fetch(window.location.pathname + (qs ? qs + '&' : '?') + '_kpis=' + encodeURIComponent(zona),
          { headers: { 'X-Requested-With': 'XMLHttpRequest' }, signal: control.signal })
      .then(function (respuesta) { if (!respuesta.ok) throw new Error('HTTP ' + respuesta.status); return respuesta.text(); })
      .then(function (html) {
        var nueva = new DOMParser().parseFromString(html, 'text/html').querySelector('.franja-kpis');
        if (!nueva || nueva.outerHTML === franja.outerHTML) return;
        if (!reducido) nueva.classList.add('seccion-actualizada');
        franja.replaceWith(nueva);
      })
      .catch(function () {});
  }
  document.addEventListener('uperu:seccion-actualizada', function (evento) {
    if (evento.detail && evento.detail.id === 'sec-indicadores') return;  // su propio filtro Mes
    indicadoresDesactualizado = true;
    var seccion = evento.detail && document.getElementById(evento.detail.id);
    var panel = seccion && seccion.closest('.zona-panel');
    if (panel) refrescarKpis(panel.id.replace(/^zona-/, ''));
  });
  window.addEventListener('hashchange', desdeHash);

  if (!desdeHash()) {
    cargarZona(panelDe(tabs[0]));
    animarEntrada(panelDe(tabs[0]), 0);
  }
})();
