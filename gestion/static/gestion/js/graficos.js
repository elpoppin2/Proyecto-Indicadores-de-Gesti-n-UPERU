/* Transición de los gráficos (rediseño de octubre 2026, pedido del usuario:
   "transiciones entre gráficos y diseño"). Cuando se carga la página, se
   abre una pestaña o un filtro trae una sección nueva, cada gráfico visible
   se descubre de izquierda a derecha (el sentido del tiempo).
   El estado final es el gráfico de siempre: no se deja nada oculto si algo
   falla, y con "reducir movimiento" del sistema no se anima nada.
   Disparadores: carga de la página, evento 'uperu:zona-entrando' (zonas.js)
   y 'uperu:seccion-actualizada' (filtros de base.html).
   Desde el 2026-11-10 (pedido del usuario: las animaciones de los gráficos de
   inventario iban "con pocos fps"): antes se animaba cada línea, barra y
   punto dentro del SVG (recorte, escala, opacidad), y el navegador volvía a
   pintar el gráfico entero en cada cuadro; con las tramas y máscaras del de
   inventario no llegaba a 60 cuadros por segundo. Ahora el gráfico se pinta
   una sola vez y lo que se mueve es una cortina del color del fondo que se
   corre hacia la derecha (solo transform, lo hace la tarjeta de video).
   Cada gráfico dibujado se descubre una sola vez: volver a una pestaña ya
   vista no lo repite (un filtro trae un gráfico nuevo, y ese sí). */
(function () {
  if (!Element.prototype.animate) return;
  var reducido = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  var CURVA = 'cubic-bezier(.33, .1, .25, 1)';
  var BARRIDO = 700;     // ms que tarda la cortina en cruzar el gráfico
  var armados = new WeakSet();  // los <svg> que ya se descubrieron

  function visible(r) {
    return r.width > 0 && r.bottom > -40 && r.top < (window.innerHeight || 800) + 120;
  }

  // Color del fondo que se ve detrás de un elemento (el de la tarjeta, o el de la página).
  function fondoDe(el) {
    for (var n = el; n && n.nodeType === 1; n = n.parentElement) {
      var c = getComputedStyle(n).backgroundColor;
      var m = c && c.match(/rgba?\(([^)]+)\)/);
      if (m && (m[1].split(',').length < 4 || parseFloat(m[1].split(',')[3]) > 0.98)) return c;
    }
    return getComputedStyle(document.documentElement).getPropertyValue('--bg') || '#fff';
  }

  function cortina(envoltura, fondo) {
    var caja = document.createElement('div');
    caja.className = 'cortina-grafico';
    caja.setAttribute('aria-hidden', 'true');
    var tela = document.createElement('i');
    tela.style.setProperty('--fondo-cortina', fondo);
    caja.appendChild(tela);
    envoltura.appendChild(caja);
    var a = tela.animate([{ transform: 'translateX(0)' }, { transform: 'translateX(100%)' }],
                         { duration: BARRIDO, easing: CURVA });
    a.onfinish = a.oncancel = function () { caja.remove(); };
  }

  // Lo pedido en un mismo cuadro (zona que entra + sección que se actualiza)
  // se resuelve junto: primero se leen todas las medidas y colores, después
  // se ponen las cortinas (sin alternar lecturas y escrituras).
  var pendientes = [], programado = false;
  function procesar() {
    programado = false;
    var envolturas = [], enLote = new Set();
    pendientes.splice(0).forEach(function (raiz) {
      if (!raiz.isConnected) return;
      raiz.querySelectorAll('.chart-wrap, svg.minigrafico').forEach(function (g) {
        var svg = g.tagName === 'svg' ? g : g.querySelector('svg');
        var clave = svg || g;
        if (enLote.has(clave) || (svg && armados.has(svg)) || g.closest('[hidden]')) return;
        enLote.add(clave);
        envolturas.push([g, svg]);
      });
    });
    var planes = [];
    envolturas.forEach(function (par) {
      if (!visible(par[0].getBoundingClientRect())) return;
      try {
        planes.push({ envoltura: par[0], svg: par[1], mini: par[0] === par[1], fondo: par[0] === par[1] ? null : fondoDe(par[0]) });
      } catch (e) { /* un gráfico raro no frena al resto */ }
    });
    planes.forEach(function (plan) {
      if (plan.svg) armados.add(plan.svg);
      try {
        // Los minigráficos de las tablas aparecen (opacidad: tampoco se vuelve a pintar).
        if (plan.mini) plan.svg.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 360, easing: 'ease-out' });
        else cortina(plan.envoltura, plan.fondo);
      } catch (e) { /* idem */ }
    });
  }

  function animar(raiz) {
    if (reducido.matches || !raiz || !raiz.querySelectorAll) return;
    pendientes.push(raiz);
    if (programado) return;
    programado = true;
    requestAnimationFrame(procesar);
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
