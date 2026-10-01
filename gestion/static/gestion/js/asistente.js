/* Asistente de IA de Planificación: panel lateral de chat (ver
   gestion/asistente.py y views.asistente_planificacion). Pedido por el
   usuario.
   - La conversación visible se guarda en sessionStorage (sobrevive a recargar
     la página o cambiar un filtro, y se borra al cerrar la pestaña); el
     servidor guarda el mismo historial en la sesión para las repreguntas.
   - Las respuestas vienen en markdown simple: se convierten a HTML ESCAPANDO
     primero todo el texto (solo se generan las etiquetas propias: párrafos,
     listas, tablas, negritas), así una respuesta nunca puede inyectar HTML. */
(function () {
  var panel = document.getElementById('asistente-panel');
  var abrir = document.getElementById('asistente-abrir');
  if (!panel || !abrir) return;
  var cerrar = document.getElementById('asistente-cerrar');
  var nueva = document.getElementById('asistente-nueva');
  var mensajes = document.getElementById('asistente-mensajes');
  var bienvenida = document.getElementById('asistente-bienvenida');
  var formulario = document.getElementById('asistente-formulario');
  var campo = document.getElementById('asistente-pregunta');
  var enviar = document.getElementById('asistente-enviar');
  var url = panel.dataset.url;
  var activo = panel.dataset.activo === '1';
  var CLAVE = 'uperu_asistente_planificacion';
  var ocupado = false;

  /* ---------- markdown seguro ---------- */
  function escapar(texto) {
    return texto.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function enLinea(texto) {
    var s = escapar(texto);
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[\s(])\*(?!\s)([^*]+?)\*(?=[\s).,;:]|$)/g, '$1<em>$2</em>');
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    return s;
  }
  function celdas(fila) {
    return fila.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (c) { return c.trim(); });
  }
  function esSeparador(fila) {
    return celdas(fila).every(function (c) { return /^:?-{2,}:?$/.test(c); });
  }
  function esNumero(texto) {
    return /^[+\-−]?\s?(S\/\s?)?[\d.,]+\s?(%|T|K|M|Gcal)?$/.test(texto.replace(/\*\*/g, ''));
  }
  function tabla(filas) {
    var cabecera = null;
    if (filas.length > 1 && esSeparador(filas[1])) {
      cabecera = celdas(filas[0]);
      filas = filas.slice(2);
    }
    var html = '<div class="asistente-tabla"><table>';
    if (cabecera) {
      html += '<thead><tr>' + cabecera.map(function (c) { return '<th>' + enLinea(c) + '</th>'; }).join('') + '</tr></thead>';
    }
    html += '<tbody>' + filas.filter(function (f) { return !esSeparador(f); }).map(function (f) {
      return '<tr>' + celdas(f).map(function (c) {
        return '<td' + (esNumero(c) ? ' class="num"' : '') + '>' + enLinea(c) + '</td>';
      }).join('') + '</tr>';
    }).join('') + '</tbody></table></div>';
    return html;
  }
  var ES_TABLA = /^\s*\|/, ES_VINETA = /^\s*[-*•]\s+/, ES_NUMERADA = /^\s*\d+[.)]\s+/, ES_TITULO = /^\s*#{1,6}\s+/;
  function markdown(texto) {
    var lineas = String(texto || '').replace(/\r/g, '').split('\n');
    var html = '', i = 0, bloque;
    while (i < lineas.length) {
      var linea = lineas[i];
      if (!linea.trim()) { i++; continue; }
      if (ES_TABLA.test(linea)) {
        bloque = [];
        while (i < lineas.length && ES_TABLA.test(lineas[i])) bloque.push(lineas[i++]);
        html += tabla(bloque);
      } else if (ES_VINETA.test(linea) || ES_NUMERADA.test(linea)) {
        var numerada = ES_NUMERADA.test(linea), patron = numerada ? ES_NUMERADA : ES_VINETA;
        bloque = [];
        while (i < lineas.length && patron.test(lineas[i])) bloque.push('<li>' + enLinea(lineas[i++].replace(patron, '')) + '</li>');
        html += (numerada ? '<ol>' : '<ul>') + bloque.join('') + (numerada ? '</ol>' : '</ul>');
      } else if (ES_TITULO.test(linea)) {
        html += '<p class="asistente-subtitulo">' + enLinea(linea.replace(ES_TITULO, '')) + '</p>';
        i++;
      } else {
        bloque = [];
        while (i < lineas.length && lineas[i].trim() && !ES_TABLA.test(lineas[i]) && !ES_VINETA.test(lineas[i])
               && !ES_NUMERADA.test(lineas[i]) && !ES_TITULO.test(lineas[i])) {
          bloque.push(enLinea(lineas[i++]));
        }
        html += '<p>' + bloque.join('<br>') + '</p>';
      }
    }
    return html;
  }

  /* ---------- conversación ---------- */
  var conversacion = [];
  function guardar() {
    try { sessionStorage.setItem(CLAVE, JSON.stringify(conversacion.slice(-30))); } catch (e) {}
  }
  function desplazar() { mensajes.scrollTop = mensajes.scrollHeight; }

  function agregarMensaje(m, sinGuardar) {
    var burbuja = document.createElement('div');
    burbuja.className = 'asistente-mensaje asistente-' + m.rol + (m.error ? ' asistente-error' : '');
    if (m.rol === 'usuario') {
      burbuja.textContent = m.texto;
    } else {
      burbuja.innerHTML = markdown(m.texto);
      if (m.secciones && m.secciones.length) {
        var fuentes = document.createElement('div');
        fuentes.className = 'asistente-fuentes';
        fuentes.textContent = 'Datos consultados: ' + m.secciones.join(' · ') + (m.duracion ? ' · ' + m.duracion + ' s' : '');
        burbuja.appendChild(fuentes);
      }
    }
    mensajes.appendChild(burbuja);
    if (bienvenida) bienvenida.hidden = true;
    if (!sinGuardar) { conversacion.push(m); guardar(); }
    desplazar();
    return burbuja;
  }

  function mostrarPensando() {
    var burbuja = document.createElement('div');
    burbuja.className = 'asistente-mensaje asistente-ia asistente-pensando';
    burbuja.innerHTML = '<span class="asistente-puntos" aria-hidden="true"><i></i><i></i><i></i></span><span class="asistente-estado">Consultando los datos…</span>';
    mensajes.appendChild(burbuja);
    desplazar();
    // Una respuesta con varias consultas tarda 10-40 s: el texto va cambiando
    // para que se note que sigue trabajando.
    var etapas = ['Consultando los datos…', 'Cruzando la información…', 'Calculando…', 'Redactando la respuesta…'];
    var n = 0;
    var reloj = setInterval(function () {
      n = Math.min(n + 1, etapas.length - 1);
      var estado = burbuja.querySelector('.asistente-estado');
      if (estado) estado.textContent = etapas[n];
    }, 6000);
    return function () { clearInterval(reloj); burbuja.remove(); };
  }

  function csrf() {
    var token = formulario.querySelector('input[name="csrfmiddlewaretoken"]');
    return token ? token.value : '';
  }

  function preguntar(texto) {
    texto = (texto || '').trim();
    if (!texto || ocupado || !activo) return;
    ocupado = true;
    enviar.disabled = true;
    campo.value = '';
    agregarMensaje({ rol: 'usuario', texto: texto });
    var quitarPensando = mostrarPensando();
    fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      body: JSON.stringify({ pregunta: texto })
    }).then(function (r) {
      if (r.status === 403) throw new Error('La sesión expiró: recarga la página e inicia sesión de nuevo.');
      if (!r.ok) throw new Error('El servidor respondió con un error (' + r.status + ').');
      return r.json();
    }).then(function (datos) {
      quitarPensando();
      agregarMensaje({ rol: 'ia', texto: datos.respuesta || 'Sin respuesta.', secciones: datos.secciones || [],
                       duracion: datos.duracion, error: !!datos.error });
    }).catch(function (error) {
      quitarPensando();
      agregarMensaje({ rol: 'ia', texto: error.message || 'No se pudo conectar con el servidor.', error: true });
    }).then(function () {
      ocupado = false;
      enviar.disabled = false;
      campo.focus();
    });
  }

  function abrirPanel() {
    panel.hidden = false;
    abrir.setAttribute('aria-expanded', 'true');
    document.body.classList.add('asistente-abierto');
    desplazar();
    if (activo) campo.focus();
  }
  function cerrarPanel() {
    panel.hidden = true;
    abrir.setAttribute('aria-expanded', 'false');
    document.body.classList.remove('asistente-abierto');
    abrir.focus();
  }

  abrir.addEventListener('click', function () { panel.hidden ? abrirPanel() : cerrarPanel(); });
  cerrar.addEventListener('click', cerrarPanel);
  document.addEventListener('keydown', function (evento) {
    if (evento.key === 'Escape' && !panel.hidden) cerrarPanel();
  });
  formulario.addEventListener('submit', function (evento) {
    evento.preventDefault();
    preguntar(campo.value);
  });
  campo.addEventListener('keydown', function (evento) {
    if (evento.key === 'Enter' && !evento.shiftKey) {
      evento.preventDefault();
      preguntar(campo.value);
    }
  });
  panel.addEventListener('click', function (evento) {
    var sugerencia = evento.target.closest('.asistente-sugerencia');
    if (sugerencia && !sugerencia.disabled) preguntar(sugerencia.textContent);
  });
  nueva.addEventListener('click', function () {
    if (ocupado) return;
    conversacion = [];
    guardar();
    Array.prototype.slice.call(mensajes.querySelectorAll('.asistente-mensaje')).forEach(function (m) { m.remove(); });
    if (bienvenida) bienvenida.hidden = false;
    if (activo) {
      fetch(url, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
        body: JSON.stringify({ reiniciar: true })
      }).catch(function () {});
      campo.focus();
    }
  });

  // Restaura la conversación de esta pestaña.
  try {
    var guardada = JSON.parse(sessionStorage.getItem(CLAVE) || '[]');
    if (Array.isArray(guardada)) {
      guardada.forEach(function (m) { agregarMensaje(m, true); });
      conversacion = guardada;
    }
  } catch (e) {}
})();
