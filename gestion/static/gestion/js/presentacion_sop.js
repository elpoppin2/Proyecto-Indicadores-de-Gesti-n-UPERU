/* Presentación «Evolución de inventarios» (PowerPoint) de Planificación >
   Inventario (pedido del usuario, 2026-11-11): el selector de materiales
   (filtro-multi de zonas.js, agrupado por centro) y el botón que pide el
   archivo al servidor (vistas/presentacion.py) y lo descarga. La elección
   queda en este navegador para la próxima vez. La sección se vuelve a dibujar
   al filtrar: todo va por delegación en el documento. */
(function () {
  var CLAVE = 'uperu-presentacion-materiales';

  function leer() {
    try { return JSON.parse(localStorage.getItem(CLAVE) || 'null'); } catch (e) { return null; }
  }
  function guardar(valores) {
    try { localStorage.setItem(CLAVE, JSON.stringify(valores)); } catch (e) { /* sin almacenamiento: no se recuerda */ }
  }
  function elegidos(select) {
    return Array.prototype.filter.call(select.options, function (o) { return o.selected; }).map(function (o) { return o.value; });
  }

  // Al abrir el panel, la última elección de este navegador (solo los materiales que sigan en el S&OP).
  document.addEventListener('toggle', function (e) {
    var panel = e.target;
    if (!panel.matches || !panel.matches('details[data-presentacion]') || !panel.open || panel._restaurado) return;
    panel._restaurado = true;
    var guardados = leer(), select = panel.querySelector('select[data-multi]');
    if (!Array.isArray(guardados) || !select || !window.uperuFijarMateriales) return;
    var existe = {};
    Array.prototype.forEach.call(select.options, function (o) { existe[o.value] = true; });
    var validos = guardados.filter(function (v) { return existe[v]; });
    if (validos.length || !guardados.length) window.uperuFijarMateriales(select.id, validos);
  }, true);

  document.addEventListener('change', function (e) {
    var select = e.target;
    if (select.matches && select.matches('details[data-presentacion] select[data-multi]')) guardar(elegidos(select));
  });

  function nombreArchivo(cabecera) {
    var m = /filename\*=UTF-8''([^;]+)/i.exec(cabecera || '');
    if (m) { try { return decodeURIComponent(m[1]); } catch (e) { /* sigue con filename= */ } }
    m = /filename="?([^";]+)"?/i.exec(cabecera || '');
    return m ? m[1] : 'Evolución de inventarios.pptx';
  }
  function descargar(blob, nombre) {
    var url = URL.createObjectURL(blob), a = document.createElement('a');
    a.href = url;
    a.download = nombre;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
  }

  document.addEventListener('click', function (e) {
    var boton = e.target.closest && e.target.closest('[data-presentacion-descargar]');
    if (!boton || boton.disabled) return;
    var panel = boton.closest('[data-presentacion]');
    var select = panel.querySelector('select[data-multi]'), estado = panel.querySelector('[data-presentacion-estado]');
    var materiales = elegidos(select);
    var url = panel.dataset.url + '?sop=' + encodeURIComponent(panel.dataset.sop) +
      '&materiales=' + encodeURIComponent(materiales.join(','));
    var texto = boton.textContent;
    boton.disabled = true;
    boton.textContent = 'Armando la presentación…';
    estado.classList.remove('presentacion-error');
    estado.textContent = (materiales.length || select.options.length) + ' materiales: tarda unos segundos.';
    fetch(url, { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) {
          throw new Error(r.status === 400 && t ? t : 'No se pudo armar la presentación (error ' + r.status + '). Vuelve a intentarlo.');
        });
      }
      // Con la sesión vencida el servidor responde la página de ingreso, no el archivo.
      if ((r.headers.get('Content-Type') || '').indexOf('presentationml') < 0) {
        throw new Error('La sesión venció: vuelve a ingresar y descarga de nuevo.');
      }
      var nombre = nombreArchivo(r.headers.get('Content-Disposition'));
      return r.blob().then(function (b) {
        descargar(b, nombre);
        estado.textContent = 'Descargada: ' + nombre;
      });
    }).catch(function (error) {
      estado.classList.add('presentacion-error');
      estado.textContent = error.message || 'No se pudo armar la presentación. Revisa la conexión con el servidor.';
    }).then(function () {
      boton.disabled = false;
      boton.textContent = texto;
    });
  });
})();
