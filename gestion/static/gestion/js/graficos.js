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
