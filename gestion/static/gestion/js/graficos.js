/* Transición de los gráficos (rediseño de octubre 2026, pedido del usuario:
   "transiciones entre gráficos y diseño"). Cuando se carga la página, se
   abre una pestaña o un filtro trae una sección nueva, cada gráfico visible
   "se arma" una sola vez:
   - líneas y áreas se revelan de izquierda a derecha (el sentido del tiempo);
   - barras y columnas crecen desde su base (hacia abajo las negativas);
   - puntos, burbujas y etiquetas de valor aparecen cuando la línea llega a
     ellos.
   El estado final es el gráfico de siempre: no se deja nada oculto si algo
   falla, y con "reducir movimiento" del sistema no se anima nada.
   Disparadores: carga de la página, evento 'uperu:zona-entrando' (zonas.js)
   y 'uperu:seccion-actualizada' (filtros de base.html). */
(function () {
  if (!Element.prototype.animate) return;
  var reducido = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  var CURVA = 'cubic-bezier(.25, .1, .25, 1)';
  var BARRIDO = 650;     // ms que tarda la línea en cruzar el gráfico
  var LINEAS = 'path:not(.zona-detalle):not([class*="grilla"]):not([class*="eje"])';
  var ETIQUETAS = '.etiqueta-valor, .etiqueta-barra, .etiqueta-barra-t, .etiqueta-burbuja';

  function visible(nodo) {
    var r = nodo.getBoundingClientRect();
    return r.width > 0 && r.bottom > -40 && r.top < (window.innerHeight || 800) + 120;
  }

  // Fracción horizontal (0 a 1) de un elemento dentro de su gráfico.
  function fraccion(x, x0, ancho) {
    return ancho > 0 ? Math.min(1, Math.max(0, (x - x0) / ancho)) : 0;
  }

  function animarSvg(svg) {
    var caja = svg.viewBox && svg.viewBox.baseVal;
    var x0 = caja ? caja.x : 0, ancho = caja && caja.width ? caja.width : svg.getBoundingClientRect().width;

    svg.querySelectorAll(LINEAS).forEach(function (el) {
      // Las líneas punteadas de referencia (mínimo, máximo, plan lineal, promedio) quedan quietas.
      var trazo = getComputedStyle(el).strokeDasharray;
      if (trazo && trazo !== 'none') return;
      el.animate([{ clipPath: 'inset(-6px 100% -6px -6px)' }, { clipPath: 'inset(-6px -6px -6px -6px)' }],
        { duration: BARRIDO, easing: CURVA });
    });

    var cero = svg.querySelector('.linea-cero');
    var yCero = cero ? parseFloat(cero.getAttribute('y1')) : null;
    svg.querySelectorAll('rect:not(.zona-detalle):not([class*="banda"])').forEach(function (el) {
      var x = parseFloat(el.getAttribute('x')) || 0, y = parseFloat(el.getAttribute('y')) || 0;
      if (!(parseFloat(el.getAttribute('height')) > 0)) return;
      var haciaAbajo = yCero !== null && y >= yCero - 0.5;
      var origen = haciaAbajo ? 'center top' : 'center bottom';
      el.animate([
        { transformBox: 'fill-box', transformOrigin: origen, transform: 'scaleY(0)' },
        { transformBox: 'fill-box', transformOrigin: origen, transform: 'scaleY(1)' }
      ], { duration: 480, delay: fraccion(x, x0, ancho) * 260, easing: 'cubic-bezier(.2, .8, .2, 1)', fill: 'backwards' });
    });

    svg.querySelectorAll('circle:not(.zona-detalle)').forEach(function (el) {
      var cx = parseFloat(el.getAttribute('cx')) || 0;
      var esBurbuja = el.classList.contains('burbuja') || el.classList.contains('punto-pago');
      el.animate(esBurbuja ? [
        { transformBox: 'fill-box', transformOrigin: 'center', transform: 'scale(0)', opacity: 0 },
        { transformBox: 'fill-box', transformOrigin: 'center', transform: 'scale(1)', opacity: 1 }
      ] : [{ opacity: 0 }, { opacity: 1 }], {
        duration: esBurbuja ? 360 : 200, delay: 80 + fraccion(cx, x0, ancho) * (BARRIDO - 120),
        easing: 'ease-out', fill: 'backwards'
      });
    });
  }

  function animarGrafico(envoltura) {
    var svg = envoltura.tagName === 'svg' ? envoltura : envoltura.querySelector('svg');
    if (svg) animarSvg(svg);
    if (envoltura.tagName === 'svg') return;
    envoltura.querySelectorAll(ETIQUETAS).forEach(function (el) {
      var izq = parseFloat(el.style.left);
      var f = isNaN(izq) ? 1 : Math.min(1, Math.max(0, izq / 100));
      el.animate([{ opacity: 0 }, { opacity: 1 }],
        { duration: 220, delay: 140 + f * (BARRIDO - 80), easing: 'ease-out', fill: 'backwards' });
    });
  }

  function animar(raiz) {
    if (reducido.matches || !raiz || !raiz.querySelectorAll) return;
    raiz.querySelectorAll('.chart-wrap, svg.minigrafico').forEach(function (g) {
      if (g.closest('[hidden]') || !visible(g)) return;
      try { animarGrafico(g); } catch (e) { /* un gráfico raro no frena al resto */ }
    });
  }
  window.uperuAnimarGraficos = animar;

  document.addEventListener('uperu:zona-entrando', function (evento) { animar(evento.detail && evento.detail.panel); });
  document.addEventListener('uperu:seccion-actualizada', function (evento) {
    animar(evento.detail && document.getElementById(evento.detail.id));
  });
  function alCargar() { animar(document.body); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', alCargar);
  else alCargar();
})();

/* Pulido de la interfaz (pedido del usuario, 2026-10-23), sin depender de
   que el navegador pueda animar:
   - Las etiquetas de un gráfico (valores, meses, ejes) nunca se salen de su
     recuadro: la primera y la última se cortaban en los bordes.
   - Las tablas de más de 18 filas se desplazan dentro de su recuadro con el
     encabezado fijo (.tabla-larga, ver dashboard.css).
   - Las cifras clave, las métricas y los totales que cambian por un filtro
     o por el buscador entran dígito a dígito ("number pop-in" de
     transitions-dev). Al cargar la página no se animan: en una pantalla de
     trabajo nada hace una entrada. */
(function () {
  var ETIQUETAS = '.etiqueta-valor, .etiqueta-mes, .etiqueta-barra, .etiqueta-barra-t, .etiqueta-burbuja, .etiqueta-grilla, .etiqueta-eje-y';
  var FILAS_TABLA_LARGA = 18;
  var CIFRAS = '.kpi-valor, .metrica .valor, .panel-cifra';

  function ajustarEtiquetas(raiz) {
    if (!raiz || !raiz.querySelectorAll) return;
    raiz.querySelectorAll('.chart-wrap').forEach(function (envoltura) {
      if (envoltura.closest('[hidden]')) return;
      var etiquetas = Array.prototype.slice.call(envoltura.querySelectorAll(ETIQUETAS));
      if (!etiquetas.length) return;
      // Primero se leen todas las posiciones y después se corrigen (sin
      // alternar lecturas y escrituras de la página).
      etiquetas.forEach(function (e) { e.style.marginLeft = ''; e.style.marginTop = ''; });
      var caja = envoltura.getBoundingClientRect();
      if (!caja.width) return;
      etiquetas.map(function (e) {
        var r = e.getBoundingClientRect(), dx = 0, dy = 0;
        if (r.width) {
          if (r.left < caja.left) dx = caja.left - r.left;
          else if (r.right > caja.right) dx = caja.right - r.right;
        }
        if (r.height && r.top < caja.top) dy = caja.top - r.top;
        return [e, dx, dy];
      }).forEach(function (c) {
        if (c[1]) c[0].style.marginLeft = c[1].toFixed(1) + 'px';
        if (c[2]) c[0].style.marginTop = c[2].toFixed(1) + 'px';
      });
    });
  }

  function marcarTablasLargas(raiz) {
    if (!raiz || !raiz.querySelectorAll) return;
    raiz.querySelectorAll('.tabla-scroll').forEach(function (c) {
      if (c.classList.contains('tabla-alto-fijo') || c.closest('.desglose-cuerpo')) return;
      var tabla = c.querySelector(':scope > table');
      if (!tabla || tabla.hasAttribute('data-paginado') || !tabla.tHead) return;
      var filas = tabla.tBodies.length ? tabla.tBodies[0].rows.length : 0;
      c.classList.toggle('tabla-larga', filas > FILAS_TABLA_LARGA);
    });
  }

  var reducido = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };

  // transitions-dev 02: el texto pasa a un grupo de dígitos; los dos últimos
  // caracteres entran 1x y 2x --digit-stagger después. Solo cifras sin
  // marcado propio (un <small> adentro, como "IPU" en una métrica, queda igual).
  function animarCifra(el) {
    if (!el || reducido.matches || el.children.length && !el.classList.contains('t-digit-group')) return;
    var texto = el.textContent.trim();
    if (!texto || texto.length > 24) return;
    el.classList.add('t-digit-group');
    el.classList.remove('is-animating');
    el.setAttribute('aria-label', texto);
    el.replaceChildren();
    var caracteres = texto.split('');
    caracteres.forEach(function (ch, i) {
      var span = document.createElement('span');
      span.className = 't-digit' + (ch === ' ' ? ' t-digit-espacio' : '');
      span.setAttribute('aria-hidden', 'true');
      span.textContent = ch;
      if (i === caracteres.length - 2) span.dataset.stagger = '1';
      else if (i === caracteres.length - 1) span.dataset.stagger = '2';
      el.appendChild(span);
    });
    void el.offsetHeight; // fuerza el reflujo para que la animación se repita
    el.classList.add('is-animating');
  }
  window.uperuAnimarCifra = animarCifra;
  window.uperuAnimarCifras = function (raiz) {
    if (raiz && raiz.querySelectorAll) raiz.querySelectorAll(CIFRAS).forEach(animarCifra);
  };

  function preparar(raiz) { marcarTablasLargas(raiz); ajustarEtiquetas(raiz); }
  window.uperuAjustarEtiquetas = ajustarEtiquetas;

  // La zona termina de dibujarse después del evento (en el estático, al abrirla): se espera un cuadro.
  document.addEventListener('uperu:zona-entrando', function (evento) {
    var panel = evento.detail && evento.detail.panel;
    requestAnimationFrame(function () { setTimeout(function () { preparar(panel); }, 0); });
  });
  document.addEventListener('uperu:seccion-actualizada', function (evento) {
    var seccion = evento.detail && document.getElementById(evento.detail.id);
    preparar(seccion);
    window.uperuAnimarCifras(seccion);
  });
  // Un grupo de casillas con data-al-menos-uno (Categoría de Indicadores) no
  // queda vacío: si se desmarca la última, vuelve a marcarse. En captura,
  // antes de que el filtro pida la sección.
  document.addEventListener('change', function (evento) {
    var c = evento.target, grupo = c.closest && c.closest('[data-al-menos-uno]');
    if (!grupo || c.type !== 'checkbox' || c.checked) return;
    if (!grupo.querySelector('input[type="checkbox"]:checked')) {
      c.checked = true;
      evento.stopImmediatePropagation();
    }
  }, true);

  // Clave de lo elegido en un formulario: nombres en orden y sus valores en
  // orden unidos por coma (la misma que arma el servidor para cada vista).
  function claveFormulario(form) {
    var partes = {};
    Array.prototype.forEach.call(form.elements, function (c) {
      if (!c.name || ((c.type === 'checkbox' || c.type === 'radio') && !c.checked)) return;
      (partes[c.name] = partes[c.name] || []).push(c.value);
    });
    return Object.keys(partes).sort().map(function (k) { return k + '=' + partes[k].sort().join(','); }).join('&');
  }

  // Filtros de Indicadores en las copias sin servidor (form[data-estatico]):
  // todas las combinaciones vienen dibujadas (data-vista) y se muestra la
  // elegida. Delegado en document: la pestaña se dibuja desde un <template>,
  // donde un <script> propio no se ejecutaría.
  document.addEventListener('change', function (evento) {
    var form = evento.target.closest && evento.target.closest('form[data-estatico]');
    var seccion = form && document.getElementById(form.dataset.target);
    if (!seccion) return;
    var clave = claveFormulario(form), visible = null;
    seccion.querySelectorAll('[data-vista]').forEach(function (vista) {
      vista.hidden = vista.getAttribute('data-vista') !== clave;
      if (!vista.hidden) visible = vista;
    });
    if (!visible) return;
    preparar(visible);
    if (window.uperuAnimarGraficos) window.uperuAnimarGraficos(visible);
    window.uperuAnimarCifras(visible);
  });

  // Tablas que se desplazan juntas a lo ancho (data-scroll-grupo): la matriz
  // de precios TCO y la de sus volúmenes, con las mismas columnas. El evento
  // scroll no burbujea: se escucha en captura.
  var sincronizando = false;
  document.addEventListener('scroll', function (evento) {
    var origen = evento.target;
    if (sincronizando || !origen.getAttribute || !origen.hasAttribute('data-scroll-grupo')) return;
    var grupo = origen.getAttribute('data-scroll-grupo');
    var raiz = origen.closest('details, section') || document;
    sincronizando = true;
    raiz.querySelectorAll('[data-scroll-grupo="' + grupo + '"]').forEach(function (otra) {
      if (otra !== origen && otra.scrollLeft !== origen.scrollLeft) otra.scrollLeft = origen.scrollLeft;
    });
    requestAnimationFrame(function () { sincronizando = false; });
  }, true);

  var espera = null;
  window.addEventListener('resize', function () {
    clearTimeout(espera);
    espera = setTimeout(function () { ajustarEtiquetas(document.body); }, 150);
  });
  function alCargar() {
    preparar(document.body);
    // Con la letra ya cargada cambian los anchos: se vuelve a ajustar.
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { ajustarEtiquetas(document.body); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', alCargar);
  else alCargar();
})();
