/* Menú lateral, accesos directos y botones flotantes (pedido del usuario,
   2026-11-09). El mismo archivo corre en el localhost y en las copias HTML
   (exportar_sitio_html lo incrusta como los demás scripts y
   exportar_planificacion_html lo agrega a zonas.js).
   - Menú lateral: la barra de páginas y la de pestañas salen de la cabecera
     (dashboard.css las oculta con .js-menu) y pasan a un panel que sale del
     borde izquierdo cuando el puntero se queda ahí medio segundo, y se
     retrae al salir. En el borde queda una franja con un tramo por pestaña
     (la abierta en rojo); mientras corre la espera, una línea roja baja por
     el borde. El botón con la pestaña abierta, junto al título, lo abre con
     clic o con el teclado (y en pantallas táctiles).
   - Accesos directos: al pasar por una pestaña del menú se listan sus
     apartados (los títulos h2 de la pestaña; data-atajo en el h2 cambia el
     texto) y el clic lleva a cada uno. Para no saltar de pestaña al cruzar
     en diagonal hacia la lista, se sigue el rumbo del puntero (la idea de
     jQuery-menu-aim, github.com/kamens/jQuery-menu-aim).
   - Botones flotantes, abajo a la derecha: tema claro/oscuro (el ojo) y
     filtros (las rayitas). El panel de filtros junta los de todas las
     secciones de la pestaña abierta en un orden fijo (Período, Alcance,
     Vista): cada uno cambia todas las secciones que lo usan, por el mismo
     camino que si se cambiara en cada sección. Los filtros de cada sección
     siguen en su lugar para ajustar una sola. */
(function () {
  'use strict';
  var doc = document, raiz = doc.documentElement;

  function iniciar() {
    var cabecera = doc.querySelector('header.pagina');
    if (!cabecera || doc.getElementById('menu-lateral')) return;

    var reducido = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
    var conMouse = window.matchMedia ? window.matchMedia('(hover: hover) and (pointer: fine)') : { matches: true };
    var GRACIA_SALIDA = 220;  // ms fuera del menú antes de retraerlo (un desliz no lo cierra)
    var ICONO_ZONA = {
      indicadores: 'gauge', cumplimiento: 'target', consumo: 'factory', inventario: 'warehouse', precios: 'tag',
      calidad: 'flask-conical', simulacion: 'chart-spline', resumen: 'clipboard-list', informe: 'file-text',
      compras: 'shopping-cart', abastecimiento: 'package-check', cuentas: 'receipt', transporte: 'truck',
      fletes: 'route', detalle: 'layers'
    };
    var ICONO_PAGINA = {
      portada: 'house', overview: 'layout-grid', planificacion: 'calendar-range', logistica: 'truck',
      combustibles: 'fuel', empaques: 'package', 'materias-primas': 'mountain', repuestos: 'cog',
      'servicios-administrativos': 'briefcase', 'servicios-industriales': 'wrench', 'suministros-industriales': 'boxes'
    };

    function crear(etiqueta, clase, texto) {
      var n = doc.createElement(etiqueta);
      if (clase) n.className = clase;
      if (texto != null) n.textContent = texto;
      return n;
    }
    function icono(nombre, clase) {
      var s = crear('span', 'icono' + (clase ? ' ' + clase : ''));
      s.setAttribute('aria-hidden', 'true');
      s.style.setProperty('--icono', 'var(--i-' + nombre + ')');
      return s;
    }
    function boton(clase) {
      var b = crear('button', clase);
      b.type = 'button';
      return b;
    }
    function ms(variable, defecto) {
      var v = parseFloat(getComputedStyle(raiz).getPropertyValue(variable));
      return isFinite(v) ? v : defecto;
    }
    function normalizar(t) {
      return (t || '').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/\s+/g, ' ').trim().toLowerCase();
    }
    // Texto de un título sin el botón de reiniciar ni el período que llevan algunos ("Fill Rate · Ene–Oct 2026").
    function textoTitulo(h) {
      var propio = h.getAttribute('data-atajo');
      if (propio !== null) return propio.trim();
      var texto = '';
      h.childNodes.forEach(function (n) {
        if (n.nodeType === 3) texto += n.textContent;
        else if (n.nodeType === 1 && !n.matches('button, [aria-hidden="true"]')) texto += n.textContent;
      });
      texto = texto.replace(/\s+/g, ' ').trim();
      var corte = texto.indexOf(' · ');
      return corte > 0 ? texto.slice(0, corte) : texto;
    }

    // --- Lo que ya trae la página ------------------------------------------
    var h1 = cabecera.querySelector('h1');
    var tituloPagina = h1 ? h1.textContent.trim() : doc.title;
    var tabs = Array.prototype.slice.call(doc.querySelectorAll('.zonas .zona-tab[data-zona]'));
    var enlaces = Array.prototype.slice.call(doc.querySelectorAll('.nav-categorias a.nav-link'));

    function zonaActiva() {
      for (var i = 0; i < tabs.length; i++) if (tabs[i].getAttribute('aria-selected') === 'true') return tabs[i];
      return tabs[0] || null;
    }
    function panelDe(tab) { return tab ? doc.getElementById(tab.getAttribute('aria-controls')) : null; }
    function nombreZona(tab) {
      var n = tab.querySelector('.zona-nombre');
      return (n ? n.textContent : tab.textContent).trim();
    }
    function descZona(tab) {
      var d = tab.querySelector('.zona-desc');
      return d ? d.textContent.trim() : '';
    }

    // --- Menú lateral --------------------------------------------------------
    var menu = crear('nav', 'menu-lateral');
    menu.id = 'menu-lateral';
    menu.setAttribute('aria-label', 'Páginas y pestañas');
    var cuerpo = crear('div', 'menu-cuerpo');
    menu.appendChild(cuerpo);
    var filas = [];  // una por pestaña: { tab, boton, li }

    function listaZonas() {
      var ul = crear('ul', 'menu-sublista');
      ul.setAttribute('aria-label', 'Pestañas de ' + tituloPagina);
      tabs.forEach(function (tab) {
        var li = crear('li', 'menu-fila-zona');
        var b = boton('menu-item menu-zona');
        b.dataset.zona = tab.dataset.zona;
        b.appendChild(icono(ICONO_ZONA[tab.dataset.zona] || 'layers'));
        b.appendChild(crear('span', 'menu-texto', nombreZona(tab)));
        var flecha = crear('span', 'menu-flecha');
        flecha.setAttribute('aria-hidden', 'true');
        b.appendChild(flecha);
        var expandir = boton('menu-expandir');
        expandir.setAttribute('aria-expanded', 'false');
        expandir.setAttribute('aria-label', 'Apartados de ' + nombreZona(tab));
        li.appendChild(b);
        li.appendChild(expandir);
        ul.appendChild(li);
        filas.push({ tab: tab, boton: b, expandir: expandir, li: li });
      });
      return ul;
    }

    if (enlaces.length) {
      cuerpo.appendChild(crear('p', 'menu-grupo', 'Páginas'));
      var lista = crear('ul', 'menu-lista');
      var conZonas = false;
      enlaces.forEach(function (a) {
        var li = crear('li');
        var item = crear('a', 'menu-item menu-pagina');
        item.href = a.getAttribute('href');
        item.appendChild(icono(ICONO_PAGINA[a.dataset.pagina] || 'folder'));
        item.appendChild(crear('span', 'menu-texto', a.textContent.trim()));
        li.appendChild(item);
        if (a.classList.contains('activo')) {
          item.setAttribute('aria-current', 'page');
          if (tabs.length) { li.appendChild(listaZonas()); conZonas = true; }
        }
        lista.appendChild(li);
      });
      cuerpo.appendChild(lista);
      if (tabs.length && !conZonas) {
        cuerpo.appendChild(crear('p', 'menu-grupo', tituloPagina));
        cuerpo.appendChild(listaZonas());
      }
    } else if (tabs.length) {
      // Copia de Planificación (sin barra de páginas): el título y sus pestañas.
      cuerpo.appendChild(crear('p', 'menu-grupo', tituloPagina));
      cuerpo.appendChild(listaZonas());
    }

    // Borde izquierdo con la franja de tramos.
    var borde = crear('div', 'menu-borde');
    borde.setAttribute('aria-hidden', 'true');
    var estratos = crear('span', 'menu-estratos');
    (tabs.length ? tabs : enlaces).forEach(function () { estratos.appendChild(crear('i')); });
    borde.appendChild(estratos);

    // Botón con la pestaña abierta, junto al título.
    var ubicacion = boton('menu-ubicacion');
    ubicacion.setAttribute('aria-controls', 'menu-lateral');
    ubicacion.setAttribute('aria-expanded', 'false');
    ubicacion.title = 'Menú: también se abre dejando el puntero medio segundo en el borde izquierdo';
    var ubIcono = icono('panel-left'), ubTexto = crear('span', 'menu-ubicacion-texto', '');
    ubicacion.appendChild(ubIcono);
    ubicacion.appendChild(ubTexto);
    if (h1) {
      var filaTitulo = crear('div', 'titulo-fila');
      h1.parentNode.insertBefore(filaTitulo, h1);
      filaTitulo.appendChild(h1);
      filaTitulo.appendChild(ubicacion);
    } else {
      cabecera.appendChild(ubicacion);
    }

    doc.body.appendChild(borde);
    doc.body.appendChild(menu);

    function marcar(animar) {
      var activa = zonaActiva();
      filas.forEach(function (f) {
        if (f.tab === activa) f.boton.setAttribute('aria-current', 'true');
        else f.boton.removeAttribute('aria-current');
      });
      var indice = tabs.length ? tabs.indexOf(activa) : enlaces.findIndex(function (a) { return a.classList.contains('activo'); });
      Array.prototype.forEach.call(estratos.children, function (t, i) { t.classList.toggle('activo', i === indice); });
      var texto = activa ? nombreZona(activa) : 'Páginas';
      ubicacion.setAttribute('aria-label', (activa ? 'Pestaña ' + texto + '. ' : '') + 'Abrir el menú de páginas y pestañas');
      if (ubTexto.textContent === texto) return;
      ubTexto.textContent = texto;
      ubIcono.style.setProperty('--icono', 'var(--i-' + (activa ? ICONO_ZONA[activa.dataset.zona] || 'layers' : 'panel-left') + ')');
      if (animar && !reducido.matches) {
        ubicacion.classList.remove('is-swapping');
        void ubicacion.offsetWidth;  // reinicia la animación del texto
        ubicacion.classList.add('is-swapping');
      }
    }
    marcar(false);
    ubicacion.addEventListener('animationend', function () { ubicacion.classList.remove('is-swapping'); });

    // --- Abrir y retraer -----------------------------------------------------
    var abierto = false, entroAlMenu = false, tEspera = 0, tCierre = 0;

    function abrir(opciones) {
      clearTimeout(tEspera);
      clearTimeout(tCierre);
      borde.classList.remove('esperando');
      if (abierto) return;
      abierto = true;
      entroAlMenu = !!opciones.puntero;  // desde el borde, el puntero ya está encima
      menu.classList.add('is-open');
      raiz.classList.add('menu-abierto');
      ubicacion.setAttribute('aria-expanded', 'true');
      if (opciones.foco) {
        var destino = menu.querySelector('.menu-zona[aria-current]') || menu.querySelector('.menu-item[aria-current]') ||
                      menu.querySelector('.menu-item');
        if (destino) destino.focus({ preventScroll: true });
      }
    }
    function cerrar(devolverFoco) {
      clearTimeout(tCierre);
      if (!abierto) return;
      abierto = false;
      ocultarAtajos(true);
      menu.classList.remove('is-open');
      raiz.classList.remove('menu-abierto');
      ubicacion.setAttribute('aria-expanded', 'false');
      if (devolverFoco) ubicacion.focus({ preventScroll: true });
      else if (menu.contains(doc.activeElement)) doc.activeElement.blur();
    }

    // Medio segundo con el puntero en el borde (pedido del usuario): si sale antes, no pasa nada.
    borde.addEventListener('pointerenter', function (e) {
      if (e.pointerType !== 'mouse' || abierto) return;
      borde.classList.add('esperando');
      clearTimeout(tEspera);
      tEspera = setTimeout(function () { abrir({ puntero: true }); }, ms('--menu-espera', 500));
    });
    borde.addEventListener('pointerleave', function () {
      clearTimeout(tEspera);
      borde.classList.remove('esperando');
    });
    borde.addEventListener('click', function () { abrir({ puntero: true }); });

    menu.addEventListener('pointerenter', function (e) {
      if (e.pointerType !== 'mouse') return;
      entroAlMenu = true;
      clearTimeout(tCierre);
    });
    menu.addEventListener('pointerleave', function (e) {
      if (e.pointerType !== 'mouse' || !abierto || !entroAlMenu) return;
      clearTimeout(tCierre);
      tCierre = setTimeout(function () { cerrar(false); }, GRACIA_SALIDA);
    });
    ubicacion.addEventListener('click', function (e) {
      if (abierto) cerrar(false);
      else abrir({ puntero: false, foco: e.detail === 0 });  // detail 0: Enter o espacio
    });
    doc.addEventListener('pointerdown', function (e) {
      if (abierto && !menu.contains(e.target) && !ubicacion.contains(e.target) && !borde.contains(e.target)) cerrar(false);
    });
    menu.addEventListener('focusout', function (e) {
      var hacia = e.relatedTarget;
      if (abierto && hacia && !menu.contains(hacia) && hacia !== ubicacion) cerrar(false);
    });
    doc.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && abierto && !e.defaultPrevented) cerrar(menu.contains(doc.activeElement));
    });

    // --- Accesos directos de cada pestaña ------------------------------------
    var atajos = crear('div', 'menu-atajos');
    atajos.id = 'menu-atajos';
    var caja = crear('div', 'menu-atajos-caja t-dropdown');
    atajos.appendChild(caja);
    var filaVista = null;   // la pestaña cuyos apartados se ven
    var filaBajo = null;    // la pestaña bajo el puntero
    var tCambio = 0, huella = [];

    // Dónde están los títulos de una pestaña: en la página, o en su <template> (copia HTML, todavía sin dibujar).
    function contenidoDe(tab) {
      var panel = panelDe(tab);
      if (!panel) return null;
      var plantilla = doc.getElementById('plantilla-zona-' + tab.dataset.zona);
      if (panel.hasAttribute('data-pendiente') && plantilla && plantilla.content) return { raiz: plantilla.content };
      if (panel.hasAttribute('data-pendiente') && !panel.querySelector('h2')) return { pendiente: true };
      return { raiz: panel };
    }
    function titulos(contenedor) {
      var lista = [];
      contenedor.querySelectorAll('h2').forEach(function (h) {
        if (h.classList.contains('segmentadores-titulo') || h.classList.contains('subtitulo') || h.closest('details')) return;
        var oculto = h.closest('[hidden]');
        if (oculto && oculto !== contenedor) return;  // vistas alternativas de un filtro, ocultas
        var texto = textoTitulo(h);
        if (texto) lista.push({ texto: texto, h: h });
      });
      return lista;
    }
    function destino(h) { return h.closest('section, article') || h; }
    // El apartado a la vista: el último cuyo comienzo ya pasó el tercio de arriba de la pantalla.
    function apartadoActual(lista) {
      var actual = 0, limite = Math.min(220, window.innerHeight / 3);
      lista.forEach(function (t, i) {
        var r = destino(t.h).getBoundingClientRect();
        if (r.height && r.top <= limite) actual = i;
      });
      return actual;
    }

    function pintarAtajos(fila) {
      var tab = fila.tab;
      caja.replaceChildren();
      caja.setAttribute('aria-label', 'Apartados de ' + nombreZona(tab));
      var cab = crear('div', 'atajos-cabecera');
      cab.appendChild(crear('strong', 'atajos-titulo', nombreZona(tab)));
      if (descZona(tab)) cab.appendChild(crear('span', 'atajos-desc', descZona(tab)));
      caja.appendChild(cab);
      var info = contenidoDe(tab);
      if (!info) return;
      if (info.pendiente) {
        // Un pedido por pasada: si la pestaña no llega (sin servidor), se avisa en vez de reintentar.
        if (fila.cargando || !window.uperuZonas) {
          caja.appendChild(crear('p', 'atajos-vacio', 'No se pudieron cargar los apartados de esta pestaña.'));
          return;
        }
        caja.appendChild(crear('p', 'atajos-vacio', 'Cargando apartados…'));
        fila.cargando = true;
        window.uperuZonas.cargar(tab.dataset.zona).then(function () {
          if (filaVista === fila) { pintarAtajos(fila); ubicarAtajos(fila); }
          fila.cargando = false;
        });
        return;
      }
      var lista = titulos(info.raiz);
      if (!lista.length) {
        caja.appendChild(crear('p', 'atajos-vacio', 'Esta pestaña no tiene apartados.'));
        return;
      }
      var actual = tab === zonaActiva() && info.raiz.nodeType === 1 ? apartadoActual(lista) : -1;
      var ul = crear('ul', 'atajos-lista');
      lista.forEach(function (t, i) {
        var li = crear('li');
        var b = boton('atajo');
        b.textContent = t.texto;
        b.dataset.indice = i;
        if (i === actual) b.setAttribute('aria-current', 'location');
        li.appendChild(b);
        ul.appendChild(li);
      });
      caja.appendChild(ul);
    }
    function enLinea() { return !conMouse.matches || window.innerWidth <= 760; }
    function ubicarAtajos(fila) {
      if (enLinea()) { atajos.style.top = ''; atajos.style.left = ''; return; }
      var r = fila.boton.getBoundingClientRect(), m = menu.getBoundingClientRect();
      var alto = atajos.offsetHeight;
      var arriba = Math.max(12, Math.min(r.top - 12, window.innerHeight - alto - 12));
      atajos.style.left = (m.right - 1) + 'px';
      atajos.style.top = arriba + 'px';
      // Crece desde la altura de la pestaña.
      caja.style.transformOrigin = 'left ' + Math.round(r.top + r.height / 2 - arriba) + 'px';
    }
    function mostrarAtajos(fila) {
      clearTimeout(tCambio);
      if (filaVista === fila) return;
      var yaVisible = !!filaVista;
      if (filaVista) {
        filaVista.boton.classList.remove('is-hover');
        filaVista.expandir.setAttribute('aria-expanded', 'false');
      }
      filaVista = fila;
      fila.boton.classList.add('is-hover');
      fila.expandir.setAttribute('aria-expanded', 'true');
      pintarAtajos(fila);
      fila.li.appendChild(atajos);
      if (!yaVisible) atajos.style.transition = 'none';  // la primera vez aparece en su lugar, sin deslizarse
      ubicarAtajos(fila);
      if (!yaVisible) {
        void atajos.offsetWidth;
        atajos.style.transition = '';
        caja.classList.remove('is-closing');
        caja.classList.add('is-open');
      }
    }
    function ocultarAtajos(inmediato) {
      clearTimeout(tCambio);
      if (!filaVista) return;
      filaVista.boton.classList.remove('is-hover');
      filaVista.expandir.setAttribute('aria-expanded', 'false');
      filaVista = null;
      caja.classList.remove('is-open');
      if (inmediato) { atajos.remove(); return; }
      caja.classList.add('is-closing');
      setTimeout(function () {
        if (filaVista) return;
        atajos.remove();
        caja.classList.remove('is-closing');
      }, ms('--dropdown-close-dur', 150));
    }

    function filaDe(nodo) {
      var b = nodo && nodo.closest ? nodo.closest('.menu-zona, .menu-expandir') : null;
      for (var i = 0; b && i < filas.length; i++) if (filas[i].boton === b || filas[i].expandir === b) return filas[i];
      return null;
    }
    // ¿El puntero va hacia la lista de apartados? Si las pendientes hacia sus esquinas izquierdas se abren, sí.
    function rumboALaLista() {
      if (!filaVista || huella.length < 2) return false;
      var r = caja.getBoundingClientRect(), actual = huella[huella.length - 1], previo = huella[0];
      var arriba = { x: r.left, y: r.top - 30 }, abajo = { x: r.left, y: r.bottom + 30 };
      function pendiente(a, b) { return (b.y - a.y) / ((b.x - a.x) || 0.001); }
      return pendiente(actual, arriba) < pendiente(previo, arriba) && pendiente(actual, abajo) > pendiente(previo, abajo);
    }
    function programarCambio(fila) {
      clearTimeout(tCambio);
      var espera = !filaVista ? 90 : rumboALaLista() ? 300 : 60;
      tCambio = setTimeout(function () {
        if (filaBajo !== fila) return;  // el puntero siguió de largo
        if (fila) mostrarAtajos(fila);
        else ocultarAtajos(false);
      }, espera);
    }
    menu.addEventListener('pointermove', function (e) {
      huella.push({ x: e.clientX, y: e.clientY });
      if (huella.length > 4) huella.shift();
    }, { passive: true });
    menu.addEventListener('pointerover', function (e) {
      if (e.pointerType !== 'mouse' || enLinea()) return;
      if (atajos.contains(e.target)) { filaBajo = filaVista; clearTimeout(tCambio); return; }
      var fila = filaDe(e.target);
      filaBajo = fila;
      if (fila === filaVista && fila) { clearTimeout(tCambio); return; }
      programarCambio(fila);
    });
    menu.addEventListener('focusin', function (e) {
      if (atajos.contains(e.target) || enLinea()) return;
      var fila = filaDe(e.target);
      if (fila) mostrarAtajos(fila);
      else ocultarAtajos(false);
    });

    function irA(el) {
      var suave = !reducido.matches;
      el.scrollIntoView({ behavior: suave ? 'smooth' : 'auto', block: 'start' });
      if (!suave) return;
      // Con content-visibility las secciones de arriba recién toman su alto al pasar: si el destino quedó corrido, se
      // ajusta al final. No, si mientras tanto la persona movió la página.
      var movio = false, eventos = ['wheel', 'touchstart', 'keydown', 'pointerdown'];
      function marcarMovio() { movio = true; }
      eventos.forEach(function (ev) { window.addEventListener(ev, marcarMovio, { passive: true }); });
      setTimeout(function () {
        eventos.forEach(function (ev) { window.removeEventListener(ev, marcarMovio, { passive: true }); });
        var margen = parseFloat(getComputedStyle(el).scrollMarginTop) || 0;
        if (!movio && Math.abs(el.getBoundingClientRect().top - margen) > 4) el.scrollIntoView({ behavior: 'auto', block: 'start' });
      }, 900);
    }
    function irAtajo(b) {
      var fila = filaVista;
      if (!fila) return;
      var indice = parseInt(b.dataset.indice, 10), zona = fila.tab.dataset.zona, api = window.uperuZonas;
      var teclado = b === doc.activeElement && b.matches(':focus-visible');
      cerrar(false);
      if (fila.tab !== zonaActiva()) {
        if (api) api.activar(zona);
        else fila.tab.click();
      }
      (api ? api.cargar(zona) : Promise.resolve()).then(function () {
        requestAnimationFrame(function () {
          var t = titulos(panelDe(fila.tab))[indice];
          if (!t) return;
          irA(destino(t.h));
          if (teclado) {
            var objetivo = destino(t.h);
            if (!objetivo.hasAttribute('tabindex')) objetivo.setAttribute('tabindex', '-1');
            objetivo.focus({ preventScroll: true });
          }
        });
      });
    }

    menu.addEventListener('click', function (e) {
      var atajo = e.target.closest('.atajo');
      if (atajo) { irAtajo(atajo); return; }
      var fila = filaDe(e.target);
      if (!fila) return;
      if (e.target.closest('.menu-expandir')) {
        if (filaVista === fila) ocultarAtajos(true);
        else { ocultarAtajos(true); mostrarAtajos(fila); }
        return;
      }
      var teclado = e.detail === 0;
      fila.tab.click();  // zonas.js abre la pestaña
      cerrar(teclado);
    });

    // La pestaña cambió (clic en el menú, "Ver detalle", un enlace a una sección).
    doc.addEventListener('uperu:zona-activada', function () {
      marcar(true);
      if (panelFiltros && !panelFiltros.hidden) pintarFiltros();
      programarInsignia();
    });
    window.addEventListener('resize', function () { if (filaVista) ubicarAtajos(filaVista); });

    // --- Tema claro / oscuro ---------------------------------------------------
    var dock = crear('div', 'dock-flotante');
    dock.setAttribute('role', 'toolbar');
    dock.setAttribute('aria-label', 'Tema y filtros');
    dock.setAttribute('aria-orientation', 'vertical');
    var botonTema = boton('dock-boton dock-tema');
    botonTema.id = 'tema-alternar';
    var ojo = crear('span', 't-icon-swap');
    var ojoAbierto = icono('eye', 't-icon'), ojoCerrado = icono('eye-closed', 't-icon');
    ojoAbierto.dataset.icon = 'a';
    ojoCerrado.dataset.icon = 'b';
    ojo.appendChild(ojoAbierto);
    ojo.appendChild(ojoCerrado);
    botonTema.appendChild(ojo);

    function temaActual() {
      var t = raiz.getAttribute('data-theme');
      if (t === 'light' || t === 'dark') return t;
      return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    function rotularTema() {
      var oscuro = temaActual() === 'dark';
      ojo.dataset.state = oscuro ? 'b' : 'a';
      botonTema.dataset.etiqueta = oscuro ? 'Tema claro' : 'Tema oscuro';
      botonTema.setAttribute('aria-label', oscuro ? 'Cambiar al tema claro' : 'Cambiar al tema oscuro');
    }
    function fijarTema(nuevo) {
      raiz.setAttribute('data-theme', nuevo);
      try { localStorage.setItem('uperu_tema', nuevo); } catch (e) { /* sin almacenamiento: vale para esta visita */ }
      rotularTema();
    }
    rotularTema();
    if (window.matchMedia) {
      var sistema = window.matchMedia('(prefers-color-scheme: dark)');
      if (sistema.addEventListener) sistema.addEventListener('change', rotularTema);
    }
    // El fondo del tema nuevo crece en círculo desde el ojo, el tema cambia
    // cuando cubre la pantalla y el círculo se desvanece. Desde el 2026-11-10
    // (el usuario lo veía "con pocos fps"): antes era un recorte en círculo de
    // la página nueva (View Transitions), que la volvía a pintar entera en
    // cada cuadro; ahora solo se escala y se desvanece un círculo de color
    // (transform y opacity, los hace la tarjeta de video), y el cambio de
    // tema, que sí es pesado, pasa una sola vez y tapado.
    var cambiandoTema = false;
    botonTema.addEventListener('click', function () {
      var nuevo = temaActual() === 'dark' ? 'light' : 'dark';
      if (!Element.prototype.animate || reducido.matches) { fijarTema(nuevo); return; }
      if (cambiandoTema) return;
      cambiandoTema = true;
      ojo.dataset.state = nuevo === 'dark' ? 'b' : 'a';
      var r = botonTema.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
      var radio = Math.ceil(Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y))) + 2;
      var tinta = crear('div', 'tema-tinta');
      tinta.setAttribute('aria-hidden', 'true');
      tinta.style.left = (x - radio) + 'px';
      tinta.style.top = (y - radio) + 'px';
      tinta.style.width = tinta.style.height = (2 * radio) + 'px';
      tinta.style.background = getComputedStyle(raiz).getPropertyValue('--fondo-tema-' + nuevo).trim() || (nuevo === 'dark' ? '#17191b' : '#ffffff');
      doc.body.appendChild(tinta);
      function fin() { tinta.remove(); cambiandoTema = false; }
      tinta.animate([{ transform: 'scale(0)' }, { transform: 'scale(1)' }],
                    { duration: 460, easing: 'cubic-bezier(.6, 0, .3, 1)', fill: 'forwards' }).finished.then(function () {
        // Sin las transiciones de color de cada botón y celda: el tema cambia de una vez.
        raiz.classList.add('tema-cambiando');
        fijarTema(nuevo);
        requestAnimationFrame(function () {
          requestAnimationFrame(function () {
            raiz.classList.remove('tema-cambiando');
            tinta.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 280, easing: 'ease-out', fill: 'forwards' }).finished.then(fin, fin);
          });
        });
      }, function () { fijarTema(nuevo); fin(); });
    });

    // --- Estado de los datos (cabecera): se cierra al tocar afuera o con Esc ---
    function cerrarEstado(excepto) {
      doc.querySelectorAll('details.estado-datos[open]').forEach(function (d) {
        if (!excepto || !d.contains(excepto)) d.open = false;
      });
    }
    doc.addEventListener('pointerdown', function (e) { cerrarEstado(e.target); });
    doc.addEventListener('keydown', function (e) { if (e.key === 'Escape') cerrarEstado(null); });

    // --- Filtros -------------------------------------------------------------
    var botonFiltros = boton('dock-boton dock-filtros');
    botonFiltros.id = 'filtros-abrir';
    botonFiltros.dataset.etiqueta = 'Filtros';
    botonFiltros.setAttribute('aria-label', 'Filtros');
    botonFiltros.setAttribute('aria-haspopup', 'dialog');
    botonFiltros.setAttribute('aria-controls', 'panel-filtros');
    botonFiltros.setAttribute('aria-expanded', 'false');
    botonFiltros.appendChild(icono('list-filter'));
    var insignia = crear('span', 'dock-insignia');
    insignia.hidden = true;
    insignia.setAttribute('aria-hidden', 'true');
    botonFiltros.appendChild(insignia);
    dock.appendChild(botonTema);
    dock.appendChild(botonFiltros);
    doc.body.appendChild(dock);

    // Orden fijo de los filtros en todas las páginas: [clave, grupo, etiqueta normalizada].
    var GRUPOS = ['Período', 'Alcance', 'Vista'];
    var DIMENSIONES = [
      ['sop', 0, /^s&op$/], ['version', 0, /^version de precios$/], ['mes', 0, /^mes$/], ['corte', 0, /^corte/],
      ['anio', 0, /^ano$/], ['desde', 0, /^desde$/], ['hasta', 0, /^hasta$/], ['periodo', 0, /^periodo$/],
      ['mescomp', 0, /^mes comparativo$/],
      ['categoria', 1, /^categoria$/], ['familia', 1, /^familia$/], ['centro', 1, /^centro$/], ['material', 1, /^material$/],
      ['proveedor', 1, /^proveedor$/], ['producto', 1, /^se consume en$/], ['solicitante', 1, /^solicitante$/]
    ];
    var VARIOS = '__varios__';
    var AVISO_COPIA = 'En esta copia los filtros muestran la vista por defecto: cámbialos en el localhost.';

    // Los formularios que mandan en la pestaña abierta: los de la pestaña y los de toda la página.
    function formularios() {
      var panel = panelDe(zonaActiva());
      return Array.prototype.filter.call(doc.querySelectorAll('form.filtros'), function (f) {
        if (f.closest('.panel-filtros')) return false;
        var zona = f.closest('.zona-panel');
        if (zona && zona !== panel) return false;
        var oculto = f.closest('[hidden]');
        return !oculto || oculto === panel;
      });
    }
    function controles(form) {
      var lista = [];
      form.querySelectorAll('fieldset.campo-fichas').forEach(function (fs) {
        var leyenda = fs.querySelector('legend');
        lista.push({ tipo: 'fichas', etiqueta: leyenda ? leyenda.textContent.trim() : '', el: fs, form: form });
      });
      form.querySelectorAll('select').forEach(function (s) {
        if (s.closest('fieldset.campo-fichas')) return;
        var campo = s.closest('.campo'), label = campo && campo.querySelector('label');
        lista.push({ tipo: s.hasAttribute('data-multi') ? 'multi' : 'select', el: s, form: form,
                     etiqueta: label ? label.textContent.trim() : (s.getAttribute('aria-label') || s.name || s.id) });
      });
      return lista;
    }
    function modelo() {
      var dims = {}, orden = [];
      formularios().forEach(function (form) {
        controles(form).forEach(function (c) {
          var n = normalizar(c.etiqueta), def = null;
          for (var i = 0; i < DIMENSIONES.length && !def; i++) if (DIMENSIONES[i][2].test(n)) def = DIMENSIONES[i];
          var clave = (def ? def[0] : 'v-' + n.replace(/[^a-z0-9]+/g, '-')) + '-' + c.tipo;
          var d = dims[clave];
          if (!d) {
            d = dims[clave] = { clave: clave, tipo: c.tipo, etiqueta: c.etiqueta, grupo: def ? def[1] : 2,
                                orden: def ? DIMENSIONES.indexOf(def) : 100 + orden.length, controles: [] };
            orden.push(d);
          }
          d.controles.push(c);
        });
      });
      return orden.sort(function (a, b) { return a.grupo - b.grupo || a.orden - b.orden; });
    }
    function elegidos(select) {
      return Array.prototype.filter.call(select.options, function (o) { return o.selected; }).map(function (o) { return o.value; });
    }
    function unicos(lista) {
      return lista.filter(function (v, i) { return lista.indexOf(v) === i; });
    }
    function tieneOpcion(select, valor) {
      return Array.prototype.some.call(select.options, function (o) { return o.value === valor; });
    }
    // Las secciones que usan un filtro (para decir dónde rige).
    function secciones(d) {
      var nombres = [];
      d.controles.forEach(function (c) {
        // data-target: la sección (o el bloque de secciones) que el formulario vuelve a pedir; sin él (copia de
        // Planificación), la sección que lo contiene.
        var objetivo = c.form.dataset.target && doc.getElementById(c.form.dataset.target);
        var ts = objetivo ? titulos(objetivo) : [];
        if (!objetivo) {
          var contenedora = c.form.closest('section, article');
          ts = contenedora ? titulos(contenedora).slice(0, 1) : [];
        }
        ts = ts.map(function (t) { return t.texto; });
        (ts.length ? ts : [c.form.closest('.zona-panel') ? 'esta pestaña' : 'toda la página']).forEach(function (t) {
          if (nombres.indexOf(t) === -1) nombres.push(t);
        });
      });
      return nombres;
    }
    function rotuloAlcance(d, distinto) {
      var nombres = secciones(d);
      var texto = distinto ? 'Distinto en cada sección: elige uno para todas.' :
                  nombres.length === 1 ? 'Rige en ' + nombres[0] + '.' : 'Rige en ' + nombres.length + ' secciones.';
      var p = crear('span', 'pf-alcance' + (distinto ? ' pf-distinto' : ''), texto);
      if (nombres.length > 1) p.title = nombres.join('\n');
      return p;
    }
    function idDe(d) { return 'pf-' + d.clave; }

    function campoSelect(d) {
      var campo = crear('div', 'campo pf-campo');
      var sel = crear('select');
      sel.id = idDe(d);
      sel.dataset.pf = d.clave;
      var label = crear('label', null, d.etiqueta);
      label.htmlFor = sel.id;
      // Opciones: las de la sección que más tiene y, al final, las que solo tienen otras.
      var base = d.controles.slice().sort(function (a, b) { return b.el.options.length - a.el.options.length; })[0].el;
      var vistas = {};
      [base].concat(d.controles.map(function (c) { return c.el; })).forEach(function (s) {
        Array.prototype.forEach.call(s.options, function (o) {
          if (vistas[o.value]) return;
          vistas[o.value] = true;
          var n = crear('option', null, o.textContent);
          n.value = o.value;
          sel.appendChild(n);
        });
      });
      var valores = unicos(d.controles.map(function (c) { return c.el.value; }));
      if (valores.length === 1) sel.value = valores[0];
      else {
        var varios = crear('option', null, 'Varios valores');
        varios.value = VARIOS;
        varios.disabled = varios.hidden = true;
        sel.insertBefore(varios, sel.firstChild);
        sel.value = VARIOS;
      }
      sel.disabled = d.controles.every(function (c) { return c.el.disabled; });
      if (sel.disabled) sel.title = AVISO_COPIA;
      campo.appendChild(label);
      campo.appendChild(sel);
      campo.appendChild(rotuloAlcance(d, valores.length > 1));
      return campo;
    }
    function campoMulti(d) {
      var campo = crear('div', 'campo campo-multi pf-campo');
      var cont = crear('div', 'filtro-multi');
      var sel = crear('select');
      sel.id = idDe(d);
      sel.dataset.pf = d.clave;
      sel.multiple = sel.hidden = true;
      sel.setAttribute('data-multi', '');
      sel.setAttribute('aria-hidden', 'true');
      sel.tabIndex = -1;
      var label = crear('label', null, d.etiqueta);
      label.htmlFor = sel.id + '-boton';  // el botón lo arma zonas.js
      var vistas = {};
      d.controles.forEach(function (c) {
        Array.prototype.forEach.call(c.el.options, function (o) {
          if (vistas[o.value]) return;
          vistas[o.value] = true;
          var n = crear('option', null, o.textContent);
          n.value = o.value;
          sel.appendChild(n);
        });
      });
      var claves = unicos(d.controles.map(function (c) { return elegidos(c.el).sort().join(','); }));
      if (claves.length === 1 && claves[0]) {
        var marcados = {};
        claves[0].split(',').forEach(function (k) { marcados[k] = true; });
        Array.prototype.forEach.call(sel.options, function (o) { o.selected = !!marcados[o.value]; });
      }
      cont.appendChild(sel);
      campo.appendChild(label);
      campo.appendChild(cont);
      campo.appendChild(rotuloAlcance(d, claves.length > 1));
      if (d.controles.every(bloqueado)) campo.dataset.bloqueado = '1';
      return campo;
    }
    // Un filtro de sección que no se puede cambiar (copia del sitio): el select o el botón de Material deshabilitado.
    function bloqueado(c) {
      if (c.el.disabled) return true;
      if (c.tipo === 'multi') return !!c.el.closest('.filtro-multi').querySelector('.filtro-multi-boton:disabled');
      if (c.tipo === 'fichas') return !c.el.querySelector('input:not(:disabled)');
      return false;
    }
    function campoFichas(d) {
      var fs = crear('fieldset', 'campo campo-fichas pf-campo');
      fs.dataset.pf = d.clave;
      fs.setAttribute('data-al-menos-uno', '');  // como en la sección: al menos una (graficos.js)
      fs.appendChild(crear('legend', null, d.etiqueta));
      var cont = crear('div', 'fichas-filtro');
      var vistas = {};
      d.controles.forEach(function (c) {
        c.el.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
          if (vistas[cb.value]) return;
          vistas[cb.value] = true;
          var lab = crear('label', 'ficha-filtro');
          var inp = crear('input');
          inp.type = 'checkbox';
          inp.value = cb.value;
          inp.checked = d.controles.every(function (c2) {
            var otro = c2.el.querySelector('input[type="checkbox"][value="' + CSS.escape(cb.value) + '"]');
            return !otro || otro.checked;
          });
          var nombre = cb.parentNode.querySelector('span');
          lab.appendChild(inp);
          lab.appendChild(crear('span', null, (nombre || cb.parentNode).textContent.trim()));
          cont.appendChild(lab);
        });
      });
      fs.appendChild(cont);
      fs.appendChild(rotuloAlcance(d, false));
      return fs;
    }

    var panelFiltros = null, pfTitulo = null, pfCuerpo = null, pfPie = null, pfRestablecer = null;
    function crearPanel() {
      panelFiltros = crear('section', 'panel-filtros t-dropdown');
      panelFiltros.id = 'panel-filtros';
      panelFiltros.hidden = true;
      panelFiltros.dataset.origin = 'bottom-right';
      panelFiltros.setAttribute('role', 'dialog');
      panelFiltros.setAttribute('aria-labelledby', 'panel-filtros-titulo');
      var cab = crear('div', 'pf-cabecera');
      pfTitulo = crear('h2', 'pf-titulo');
      pfTitulo.id = 'panel-filtros-titulo';
      var cerrarBoton = boton('pf-cerrar');
      cerrarBoton.setAttribute('aria-label', 'Cerrar los filtros');
      cerrarBoton.appendChild(icono('x'));
      cerrarBoton.addEventListener('click', function () { cerrarFiltros(true); });
      cab.appendChild(pfTitulo);
      cab.appendChild(cerrarBoton);
      panelFiltros.appendChild(cab);
      panelFiltros.appendChild(crear('p', 'pf-nota', 'Cada filtro cambia todas las secciones de esta pestaña que lo usan.'));
      pfCuerpo = crear('div', 'pf-cuerpo');
      panelFiltros.appendChild(pfCuerpo);
      pfPie = crear('div', 'pf-pie');
      pfRestablecer = boton('btn-link');
      pfRestablecer.textContent = 'Restablecer filtros';
      pfRestablecer.addEventListener('click', restablecer);
      pfPie.appendChild(pfRestablecer);
      panelFiltros.appendChild(pfPie);
      panelFiltros.addEventListener('change', alCambiar);
      panelFiltros.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && !e.defaultPrevented) { e.preventDefault(); cerrarFiltros(true); }
      });
      doc.body.appendChild(panelFiltros);
    }
    // Los botones de reinicio de las secciones de la pestaña (los agrega base.html; en las copias no se ven).
    function reinicios() {
      return formularios().map(function (f) {
        return f.dataset.target ? doc.querySelector('.btn-reiniciar[data-reiniciar="' + CSS.escape(f.dataset.target) + '"]') : null;
      }).filter(function (b) { return b && b.getClientRects().length; });
    }
    function pintarFiltros() {
      var activo = doc.activeElement && panelFiltros.contains(doc.activeElement) ? doc.activeElement.id : null;
      var zona = zonaActiva();
      pfTitulo.textContent = 'Filtros de ' + (zona ? nombreZona(zona) : tituloPagina);
      pfCuerpo.replaceChildren();
      var dims = modelo();
      if (!dims.length) pfCuerpo.appendChild(crear('p', 'pf-vacio', 'Esta pestaña no tiene filtros.'));
      else if (dims.every(function (d) { return d.controles.every(bloqueado); })) pfCuerpo.appendChild(crear('p', 'pf-vacio', AVISO_COPIA));
      GRUPOS.forEach(function (nombre, g) {
        var deGrupo = dims.filter(function (d) { return d.grupo === g; });
        if (!deGrupo.length) return;
        var grupo = crear('div', 'pf-grupo');
        grupo.setAttribute('role', 'group');
        var titulo = crear('p', 'pf-grupo-titulo', nombre);
        titulo.id = 'pf-grupo-' + g;
        grupo.setAttribute('aria-labelledby', titulo.id);
        grupo.appendChild(titulo);
        deGrupo.forEach(function (d) {
          grupo.appendChild(d.tipo === 'select' ? campoSelect(d) : d.tipo === 'multi' ? campoMulti(d) : campoFichas(d));
        });
        pfCuerpo.appendChild(grupo);
      });
      pfPie.hidden = !reinicios().length;
      if (activo) {
        var mismo = doc.getElementById(activo);
        if (mismo) mismo.focus({ preventScroll: true });
      }
      // En la copia del sitio los filtros de las secciones están bloqueados: el del panel también. Dos cuadros:
      // zonas.js arma el botón de Material en el cuadro siguiente al de este panel.
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          panelFiltros.querySelectorAll('[data-bloqueado] .filtro-multi-boton').forEach(function (b) { b.disabled = true; b.title = AVISO_COPIA; });
        });
      });
    }

    var filtrosAbiertos = false;
    function abrirFiltros() {
      if (!panelFiltros) crearPanel();
      pintarFiltros();
      filtrosAbiertos = true;
      panelFiltros.hidden = false;
      panelFiltros.classList.remove('is-closing');
      void panelFiltros.offsetWidth;  // parte de la escala previa (transitions-dev 05)
      panelFiltros.classList.add('is-open');
      botonFiltros.setAttribute('aria-expanded', 'true');
      var primero = panelFiltros.querySelector('.pf-cuerpo select:not([disabled]):not([data-multi]), .pf-cuerpo input') ||
                    panelFiltros.querySelector('.pf-cerrar');
      primero.focus({ preventScroll: true });
    }
    function cerrarFiltros(devolverFoco) {
      if (!filtrosAbiertos) return;
      filtrosAbiertos = false;
      panelFiltros.classList.remove('is-open');
      panelFiltros.classList.add('is-closing');
      botonFiltros.setAttribute('aria-expanded', 'false');
      setTimeout(function () {
        if (filtrosAbiertos) return;
        panelFiltros.hidden = true;
        panelFiltros.classList.remove('is-closing');
      }, ms('--dropdown-close-dur', 150));
      if (devolverFoco) botonFiltros.focus({ preventScroll: true });
    }
    botonFiltros.addEventListener('click', function () {
      if (filtrosAbiertos) cerrarFiltros(false);
      else abrirFiltros();
    });
    doc.addEventListener('pointerdown', function (e) {
      if (!filtrosAbiertos) return;
      var t = e.target;
      if (panelFiltros.contains(t) || botonFiltros.contains(t) || (t.closest && t.closest('.filtro-multi-panel'))) return;
      cerrarFiltros(false);
    });

    // --- Aplicar un filtro del panel a las secciones -------------------------
    function dimension(clave) {
      var dims = modelo();
      for (var i = 0; i < dims.length; i++) if (dims[i].clave === clave) return dims[i];
      return null;
    }
    var estatico = function () { return !!window.uperuCargarZona; };
    // En el localhost cada sección se pide con la URL actual más su formulario: todos los valores
    // nuevos van a la URL antes de pedir la primera (si no, la última respuesta borraba los de las demás).
    function ponerEnUrl(pares) {
      if (estatico() || !pares.length) return;
      var params = new URLSearchParams(window.location.search);
      pares.forEach(function (p) { if (p[0]) params.set(p[0], p[1]); });
      var qs = params.toString();
      window.history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : '') + window.location.hash);
    }
    function avisar(el) { el.dispatchEvent(new Event('change', { bubbles: true })); }

    function aplicarSelect(d, valor) {
      if (valor === VARIOS) return;
      var objetivos = d.controles.map(function (c) { return c.el; }).filter(function (s) {
        return !s.disabled && s.value !== valor && tieneOpcion(s, valor);
      });
      if (!objetivos.length) return;
      objetivos.forEach(function (s) { s.value = valor; });
      ponerEnUrl(objetivos.map(function (s) { return [s.name, valor]; }));
      // Los meses de las otras pestañas se alinean como si se hubiera cambiado en una sección (zonas.js).
      if (window.uperuAlinearDesde) window.uperuAlinearDesde(objetivos[0]);
      objetivos.forEach(avisar);
    }
    function aplicarMulti(d, codigos) {
      if (!window.uperuFijarMateriales) return;
      var pedidos = codigos.reduce(function (m, c) { m[c] = true; return m; }, {});
      var cambios = [];
      d.controles.forEach(function (c) {
        if (c.el.disabled) return;
        // Cada sección recibe los elegidos que tiene; si no tiene ninguno, queda como está.
        var propios = codigos.length ? Array.prototype.filter.call(c.el.options, function (o) { return pedidos[o.value]; })
          .map(function (o) { return o.value; }) : [];
        if (codigos.length && !propios.length) return;
        if (elegidos(c.el).sort().join(',') === propios.slice().sort().join(',')) return;
        var oculto = c.el.closest('.filtro-multi').querySelector('input[data-multi-valor]');
        var todos = propios.length === c.el.options.length;
        cambios.push({ id: c.el.id, codigos: propios, nombre: oculto && oculto.name, valor: todos ? '' : propios.join(',') });
      });
      ponerEnUrl(cambios.map(function (x) { return [x.nombre, x.valor]; }));
      cambios.forEach(function (x) { window.uperuFijarMateriales(x.id, x.codigos); });
    }
    function aplicarFichas(d, fs) {
      var marcados = {};
      fs.querySelectorAll('input[type="checkbox"]').forEach(function (i) { marcados[i.value] = i.checked; });
      d.controles.forEach(function (c) {
        var ultimo = null;
        c.el.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
          if (cb.value in marcados && cb.checked !== marcados[cb.value]) { cb.checked = marcados[cb.value]; ultimo = cb; }
        });
        if (ultimo) avisar(ultimo);  // un solo pedido por sección
      });
    }
    function alCambiar(e) {
      var control = e.target.closest('[data-pf]');
      if (!control) return;
      var d = dimension(control.dataset.pf);  // se vuelve a leer: las secciones se reemplazan al filtrar
      if (!d) return;
      if (d.tipo === 'select') aplicarSelect(d, control.value);
      else if (d.tipo === 'multi') aplicarMulti(d, elegidos(control));
      else aplicarFichas(d, control);
      programarRepintado(estatico() ? 0 : 400);
      programarInsignia();
    }
    function restablecer() {
      reinicios().forEach(function (b) { b.click(); });
      programarRepintado(400);
    }

    // El panel abierto se pone al día cuando las secciones llegan con sus filtros nuevos.
    var tRepintado = 0;
    function programarRepintado(espera) {
      clearTimeout(tRepintado);
      tRepintado = setTimeout(function () { if (filtrosAbiertos) pintarFiltros(); }, espera);
    }
    doc.addEventListener('uperu:seccion-actualizada', function () {
      programarRepintado(120);
      programarInsignia();
    });

    // Insignia: cuántos filtros de alcance no están en "Todos" (o tienen materiales elegidos).
    var tInsignia = 0;
    function programarInsignia() {
      clearTimeout(tInsignia);
      tInsignia = setTimeout(actualizarInsignia, 150);
    }
    function actualizarInsignia() {
      var n = modelo().filter(function (d) {
        return d.grupo === 1 && d.controles.some(function (c) {
          if (c.tipo === 'multi') return elegidos(c.el).length > 0;
          if (c.tipo !== 'select') return false;
          var primera = c.el.options[0];
          return !!primera && /^tod/i.test(primera.textContent.trim()) && c.el.value !== primera.value;
        });
      }).length;
      insignia.hidden = !n;
      insignia.textContent = n || '';
      botonFiltros.setAttribute('aria-label', n ? 'Filtros: ' + n + (n === 1 ? ' aplicado' : ' aplicados') : 'Filtros');
    }
    doc.addEventListener('uperu:zona-entrando', programarInsignia);
    programarInsignia();
  }

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', iniciar);
  else iniciar();
})();
