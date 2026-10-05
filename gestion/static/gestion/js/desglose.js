/* Detalle fila por fila (gestion/desglose.py). Cada <details class="desglose">
   al pie de una sección pide sus filas de origen al abrirse, con los
   segmentadores que tiene la página en la URL. Además:
   - Un clic en un elemento de la misma sección con data-filtrar (una fila de
     proveedor, una barra, un punto, una columna del gráfico) abre el detalle
     ya filtrado ("f_proveedor=..."): de lo general a lo específico.
   - Buscador, orden por columna, "Cargar más" (y al bajar el scroll) y quitar
     filtros piden al servidor: el detalle puede tener decenas de miles de filas.
   Todo por delegación en el documento: sigue funcionando cuando una sección
   se reemplaza al cambiar un filtro o llega después (pestañas). */
(function () {
  var PARAMETROS_PROPIOS = ['_zona', '_seccion', 'q', 'orden', 'dir', 'offset', '_parte', 'formato'];

  function estado(det) {
    if (!det._desglose) det._desglose = { filtros: new URLSearchParams(), q: '', orden: '', dir: '' };
    return det._desglose;
  }

  function construirUrl(det, extra) {
    var params = new URLSearchParams(window.location.search);
    PARAMETROS_PROPIOS.forEach(function (p) { params.delete(p); });
    Array.from(params.keys()).forEach(function (k) { if (k.indexOf('f_') === 0) params.delete(k); });
    var e = estado(det);
    e.filtros.forEach(function (v, k) { params.set(k, v); });
    if (e.q) params.set('q', e.q);
    if (e.orden) { params.set('orden', e.orden); params.set('dir', e.dir || 'desc'); }
    Object.keys(extra || {}).forEach(function (k) { params.set(k, extra[k]); });
    return det.dataset.url + '?' + params.toString();
  }

  async function cargar(det, conFoco) {
    var cuerpo = det.querySelector('.desglose-cuerpo');
    if (det._pedido) det._pedido.abort();
    var control = new AbortController();
    det._pedido = control;
    if (!cuerpo.children.length) cuerpo.innerHTML = '<p class="desglose-cargando">Cargando las filas de origen…</p>';
    cuerpo.classList.add('cargando');
    try {
      var resp = await fetch(construirUrl(det), { headers: { 'X-Requested-With': 'XMLHttpRequest' }, signal: control.signal });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      cuerpo.innerHTML = await resp.text();
      det.dataset.cargado = 'true';
      if (conFoco) {
        var campo = cuerpo.querySelector('.desglose-buscar');
        if (campo) { campo.focus(); campo.setSelectionRange(campo.value.length, campo.value.length); }
      }
    } catch (err) {
      if (err.name === 'AbortError') return;
      cuerpo.innerHTML = '<p class="aviso">No se pudo cargar el detalle. Vuelve a abrirlo para reintentar.</p>';
      det.dataset.cargado = '';
    } finally {
      if (det._pedido === control) { det._pedido = null; cuerpo.classList.remove('cargando'); }
    }
  }

  async function cargarMas(det) {
    var pie = det.querySelector('.desglose-pie');
    if (!pie || pie.dataset.hayMas !== 'true' || det._cargandoMas) return;
    det._cargandoMas = true;
    var boton = pie.querySelector('.desglose-mas');
    if (boton) boton.disabled = true;
    try {
      var offset = parseInt(pie.dataset.mostradas || '0', 10);
      var resp = await fetch(construirUrl(det, { offset: offset, _parte: 'filas' }), { headers: { 'X-Requested-With': 'XMLHttpRequest' } });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      var html = await resp.text();
      det.querySelector('.tabla-desglose tbody').insertAdjacentHTML('beforeend', html);
      var nuevas = (html.match(/<tr\b/g) || []).length;
      var total = parseInt(resp.headers.get('X-Total') || '0', 10);
      pie.dataset.mostradas = offset + nuevas;
      pie.dataset.hayMas = resp.headers.get('X-Hay-Mas') === 'true' ? 'true' : 'false';
      var texto = pie.querySelector('[data-mostradas-texto]');
      if (texto) texto.textContent = Math.min(offset + nuevas, total).toLocaleString('en-US');
      if (pie.dataset.hayMas !== 'true' && boton) boton.remove();
    } catch (err) {
      /* se deja como está: el usuario puede volver a intentar */
    } finally {
      det._cargandoMas = false;
      if (boton && boton.isConnected) boton.disabled = false;
    }
  }

  // Abrir: la primera vez pide las filas.
  document.addEventListener('toggle', function (evento) {
    var det = evento.target;
    if (!det.matches || !det.matches('details.desglose') || !det.open || det.dataset.cargado) return;
    cargar(det);
  }, true);

  document.addEventListener('click', function (evento) {
    var objetivo = evento.target;

    // Orden por columna (en el servidor).
    var th = objetivo.closest('table[data-desglose] thead th[data-col]');
    if (th) {
      var det = th.closest('details.desglose');
      var e = estado(det);
      var actual = th.getAttribute('data-orden');
      e.orden = th.dataset.col;
      e.dir = actual === 'desc' ? 'asc' : (actual === 'asc' ? 'desc' : (th.classList.contains('num') ? 'desc' : 'asc'));
      cargar(det);
      return;
    }

    var mas = objetivo.closest('.desglose-mas');
    if (mas) { cargarMas(mas.closest('details.desglose')); return; }

    // Quitar un filtro (chip) o todos.
    var quitar = objetivo.closest('[data-quitar]');
    if (quitar && quitar.closest('details.desglose')) {
      var detQ = quitar.closest('details.desglose');
      if (quitar.dataset.quitar === '*') estado(detQ).filtros = new URLSearchParams();
      else estado(detQ).filtros.delete(quitar.dataset.quitar);
      cargar(detQ);
      return;
    }

    // Clic en un elemento de la sección con data-filtrar: abre su detalle filtrado.
    var filtrable = objetivo.closest('[data-filtrar]');
    if (!filtrable || objetivo.closest('a, button, select, input, summary')) return;
    var seccion = filtrable.closest('section.bloque');
    if (!seccion) return;
    var selector = 'details.desglose' + (filtrable.dataset.filtrarClave ? '[data-clave="' + filtrable.dataset.filtrarClave + '"]' : '');
    var detF = seccion.querySelector(selector);
    if (!detF) return;
    var eF = estado(detF);
    eF.filtros = new URLSearchParams(filtrable.dataset.filtrar);
    eF.q = '';
    detF.open = true;
    detF.dataset.cargado = 'true';  // el 'toggle' no vuelve a pedir: se pide aquí con el filtro
    cargar(detF);
    detF.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
  });

  // Teclado: Enter en un elemento filtrable enfocado.
  document.addEventListener('keydown', function (evento) {
    if (evento.key !== 'Enter') return;
    var filtrable = evento.target.closest && evento.target.closest('[data-filtrar][tabindex]');
    if (filtrable) { evento.preventDefault(); filtrable.click(); }
  });

  // Buscador (en el servidor, con espera corta entre teclas).
  document.addEventListener('input', function (evento) {
    var campo = evento.target.closest('.desglose-buscar');
    if (!campo) return;
    var det = campo.closest('details.desglose');
    clearTimeout(det._buscar);
    det._buscar = setTimeout(function () {
      estado(det).q = campo.value.trim();
      cargar(det, true);
    }, 300);
  });

  // Scroll al fondo de la tabla: más filas.
  document.addEventListener('scroll', function (evento) {
    var cont = evento.target;
    if (!cont.classList || !cont.classList.contains('desglose-scroll')) return;
    if (cont.scrollHeight - cont.scrollTop - cont.clientHeight < 160) cargarMas(cont.closest('details.desglose'));
  }, true);
})();
