/* Simulación de compras y traslados en «Movimiento de inventario por material»
   (Planificación > Inventario). Pedido del usuario, 2026-11-11: "simular o tener
   estas celdas solamente con abastecimiento (compras) y traslados, dado que los
   consumos lo hace producción"; guardar escenarios y aplicarlos al archivo del S&OP.

   Trabaja sobre la tabla ya dibujada (table[data-simulable], del localhost o de
   las copias): cada celda trae su valor sin redondear (data-v) y cada bloque su
   material, centro y de/a qué centro traslada (data-envia / data-recibe). Al
   cambiar una celda se recalcula el inventario de ese mes y de los siguientes,
   el estado frente al mín./máx. y el primer quiebre. Un traslado es un solo
   número: cambiarlo en quien recibe mueve también la salida de quien envía.

   Escenarios: en el localhost se guardan en el servidor (gestion/escenarios_sop.py)
   y un administrador los aplica al archivo de abastecimiento del S&OP vigente; en
   las copias se ven los guardados al exportar y los nuevos quedan en este navegador. */
(function () {
  'use strict';

  var TOLERANCIA = 0.5;
  var CLAVE_LOCAL = 'uperu-escenarios-sop';
  var formato = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

  // Estado de la simulación: sobrevive a que la sección se vuelva a dibujar (filtros).
  var sim = {
    sop: '', activa: false, id: '', nombre: '', cambios: vacio(), guardado: '',
    escenarios: [], servidor: null, url: '', pidiendo: null
  };

  function vacio() { return { compras: {}, traslados: {} }; }
  function firma(cambios) { return JSON.stringify([ordenado(cambios.compras), ordenado(cambios.traslados)]); }
  function ordenado(obj) { return Object.keys(obj).sort().map(function (k) { return [k, obj[k].valor]; }); }
  function sinGuardar() { return firma(sim.cambios) !== sim.guardado; }
  function nCambios() { return Object.keys(sim.cambios.compras).length + Object.keys(sim.cambios.traslados).length; }

  function enCopia() { return !!(window.uperuBloquearFiltros || window.uperuSimulacionCopia); }
  function numeroTexto(v) { var r = Math.round(v); return formato.format(r === 0 ? 0 : r); }
  function leerNumero(texto) {
    var t = String(texto == null ? '' : texto).trim().toLowerCase().replace(/[\s,]/g, '').replace(/^[−+-]/, '');
    if (!t) return 0;
    var mil = /k$/.test(t);
    if (mil) t = t.slice(0, -1);
    if (!/^\d*\.?\d+$/.test(t)) return null;
    return parseFloat(t) * (mil ? 1000 : 1);
  }
  function escapar(t) {
    return String(t).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; });
  }

  // --- Modelo de la tabla ------------------------------------------------------
  function leerTabla(tabla) {
    var meses = [].map.call(tabla.querySelectorAll('thead th[data-mes]'), function (th) {
      return { mes: th.dataset.mes, corto: th.textContent.trim() };
    });
    var bloques = [].map.call(tabla.querySelectorAll('tbody[data-mat]'), function (tb) {
      var filas = {}, base = {};
      tb.querySelectorAll('tr[data-concepto]').forEach(function (tr) {
        var celdas = [].slice.call(tr.querySelectorAll('td[data-v]'));
        filas[tr.dataset.concepto] = { tr: tr, celdas: celdas };
        base[tr.dataset.concepto] = celdas.map(function (td) { return parseFloat(td.dataset.v) || 0; });
      });
      var finales = filas.final ? filas.final.celdas : [];
      var limite = function (td, k) { return td.dataset[k] === '' || td.dataset[k] == null ? null : parseFloat(td.dataset[k]); };
      var b = {
        tbody: tb, mat: tb.dataset.mat, centro: tb.dataset.centro, envia: tb.dataset.envia || '', recibe: tb.dataset.recibe || '',
        filas: filas, base: base, minimos: finales.map(function (td) { return limite(td, 'min'); }),
        maximos: finales.map(function (td) { return limite(td, 'max'); }),
        quiebre: tb.querySelector('.mov-quiebre'), cabeza: tb.querySelector('th.mov-f1')
      };
      b.quiebreBase = primerQuiebre(base.final || [], meses);
      return b;
    });
    return { tabla: tabla, meses: meses, bloques: bloques };
  }

  function primerQuiebre(finales, meses) {
    for (var i = 0; i < finales.length; i++) if (finales[i] < 0) return meses[i] ? meses[i].corto : '';
    return '';
  }
  function estadoStock(v, mi, ma) {
    if (v < 0) return 'quiebre';
    if (mi !== null && v < mi) return 'bajo_minimo';
    if (ma !== null && v > ma) return 'sobre_maximo';
    return (mi !== null || ma !== null) ? 'en_rango' : 'sin_parametro';
  }
  function claveCompra(b, mes) { return b.mat + '|' + b.centro + '|' + mes; }
  function claveRecibido(b, mes) { return b.envia ? b.mat + '|' + b.envia + '|' + b.centro + '|' + mes : ''; }
  function claveEnvio(b, mes) { return b.recibe ? b.mat + '|' + b.centro + '|' + b.recibe + '|' + mes : ''; }

  // Valores de un bloque con los cambios del escenario (o los del S&OP si la simulación está apagada).
  function calcular(modelo, b, conCambios) {
    var n = modelo.meses.length, compras = [], recibido = [], traslado = [], inicial = [], finales = [];
    var saldo = (b.base.inicial || [0])[0] || 0;
    for (var i = 0; i < n; i++) {
      var mes = modelo.meses[i].mes;
      var c = (b.base.compras || [])[i] || 0, r = (b.base.recibido || [])[i] || 0, t = (b.base.traslado || [])[i] || 0;
      var consumo = (b.base.consumo || [])[i] || 0;
      if (conCambios) {
        var cc = sim.cambios.compras[claveCompra(b, mes)];
        if (cc) c = cc.valor;
        var cr = sim.cambios.traslados[claveRecibido(b, mes)];
        if (cr) r = cr.valor;
        var ct = sim.cambios.traslados[claveEnvio(b, mes)];
        if (ct) t = t + ct.valor - ct.base;
      }
      inicial.push(saldo);
      saldo = saldo + c + r - consumo - t;
      compras.push(c); recibido.push(r); traslado.push(t); finales.push(saldo);
    }
    return { compras: compras, recibido: recibido, traslado: traslado, inicial: inicial, final: finales };
  }

  function pintar(modelo) {
    var conCambios = sim.activa;
    // Sin simular nunca, la tabla queda tal como vino (mismos textos que el servidor).
    if (!conCambios && !modelo.tocada) return;
    modelo.tocada = true;
    modelo.tabla.classList.toggle('sim-activa', conCambios);
    var quiebresBase = 0, quiebresSim = 0;
    modelo.bloques.forEach(function (b) {
      var v = calcular(modelo, b, conCambios);
      ['compras', 'recibido', 'traslado', 'inicial'].forEach(function (k) {
        var fila = b.filas[k];
        if (!fila) return;
        fila.celdas.forEach(function (td, i) {
          var valor = v[k][i], base = b.base[k][i], cambiado = Math.abs(valor - base) >= TOLERANCIA;
          var signo = k === 'traslado' ? -1 : 1;
          td.textContent = (k !== 'inicial' && Math.abs(valor) < TOLERANCIA) ? '—' : numeroTexto(signo * valor);
          var editable = conCambios && editableFila(b, k);
          td.classList.toggle('sim-editable', editable);
          td.classList.toggle('sim-cambiado', conCambios && cambiado && k !== 'inicial');
          td.classList.toggle('sim-afectado', conCambios && cambiado && k === 'inicial');
          if (editable) { td.tabIndex = -1; td.dataset.simFila = k; td.dataset.simCol = i; }
          else { td.removeAttribute('tabindex'); }
          if (conCambios && cambiado) td.title = 'S&OP: ' + numeroTexto(signo * base);
          else td.removeAttribute('title');
        });
      });
      if (b.filas.final) {
        b.filas.final.celdas.forEach(function (td, i) {
          var valor = v.final[i], cambiado = Math.abs(valor - b.base.final[i]) >= TOLERANCIA;
          td.textContent = numeroTexto(valor);
          td.className = td.className.replace(/\bestado-\S+/g, '').trim() + ' estado-' + estadoStock(valor, b.minimos[i], b.maximos[i]);
          td.classList.toggle('sim-afectado', conCambios && cambiado);
          if (conCambios && cambiado) td.title = 'S&OP: ' + numeroTexto(b.base.final[i]); else td.removeAttribute('title');
        });
      }
      var quiebre = primerQuiebre(v.final, modelo.meses);
      if (b.quiebreBase) quiebresBase++;
      if (quiebre) quiebresSim++;
      if (!b.quiebre && quiebre && b.cabeza) {
        b.quiebre = document.createElement('span');
        b.quiebre.className = 'mov-quiebre';
        b.cabeza.appendChild(b.quiebre);
      }
      if (b.quiebre) {
        b.quiebre.hidden = !quiebre && !(conCambios && b.quiebreBase);
        b.quiebre.textContent = quiebre ? 'Quiebre: ' + quiebre + (conCambios && quiebre !== b.quiebreBase ? (b.quiebreBase ? ' (S&OP: ' + b.quiebreBase + ')' : ' (S&OP: sin quiebre)') : '')
          : 'Sin quiebre (S&OP: ' + b.quiebreBase + ')';
        b.quiebre.classList.toggle('sim-resuelto', !quiebre);
      }
    });
    modelo.quiebres = [quiebresBase, quiebresSim];
    // Una sola parada de Tab en la tabla; dentro, las flechas.
    var primera = modelo.tabla.querySelector('td.sim-editable');
    if (primera && !modelo.tabla.querySelector('td.sim-editable[tabindex="0"]')) primera.tabIndex = 0;
  }

  // Compras en todo bloque con fila Abastecimiento; traslados solo si se sabe el par (quién envía o recibe).
  function editableFila(b, k) {
    if (k === 'compras') return true;
    if (k === 'recibido') return !!b.envia;
    if (k === 'traslado') return !!b.recibe;
    return false;
  }

  // --- Edición de celdas ---------------------------------------------------------
  function abrirEditor(modelo, td) {
    if (!sim.activa || td.querySelector('input')) return;
    var b = modelo.bloques.find(function (x) { return x.tbody === td.closest('tbody'); });
    var k = td.dataset.simFila, i = +td.dataset.simCol, mes = modelo.meses[i].mes;
    var actual = calcular(modelo, b, true)[k][i];
    var input = document.createElement('input');
    input.type = 'text';
    input.inputMode = 'decimal';
    input.className = 'sim-input';
    input.value = Math.abs(actual) < TOLERANCIA ? '' : String(Math.round(actual * 100) / 100);
    input.setAttribute('aria-label', (k === 'compras' ? 'Abastecimiento' : 'Traslado') + ' de ' + b.mat + ' en ' + modelo.meses[i].corto + ' (toneladas)');
    var texto = td.textContent;
    td.textContent = '';
    td.appendChild(input);
    input.focus();
    input.select();
    var cerrado = false;
    function cerrar(guardar, mover) {
      if (cerrado) return;
      if (guardar) {
        var valor = leerNumero(input.value);
        if (valor === null) {
          input.setAttribute('aria-invalid', 'true');
          input.title = 'Escribe un número en toneladas (p. ej. 5000 o 5k)';
          input.focus();
          return;
        }
        fijarCambio(b, k, mes, i, valor);
      }
      cerrado = true;
      input.remove();
      td.textContent = texto;
      repintar();
      var destino = mover ? vecina(modelo, td, mover) : td;
      if (destino) { destino.focus(); if (mover) abrirEditorEn(destino); }
    }
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); cerrar(true, null); }
      else if (e.key === 'Escape') { e.preventDefault(); cerrar(false, null); }
      else if (e.key === 'Tab') { e.preventDefault(); cerrar(true, e.shiftKey ? 'izquierda' : 'derecha'); }
    });
    // Al salir con un valor que no es número, queda el anterior.
    input.addEventListener('blur', function () { cerrar(leerNumero(input.value) !== null, null); });
  }
  function abrirEditorEn(td) {
    var modelo = td.closest('table')._simModelo;
    if (modelo) abrirEditor(modelo, td);
  }

  function fijarCambio(b, k, mes, i, valor) {
    var tipo = k === 'compras' ? 'compras' : 'traslados';
    var clave = k === 'compras' ? claveCompra(b, mes) : k === 'recibido' ? claveRecibido(b, mes) : claveEnvio(b, mes);
    var base = k === 'compras' ? b.base.compras[i] : k === 'recibido' ? b.base.recibido[i] : null;
    if (k === 'traslado') {
      // Lo que envía este centro es la suma de lo que recibe el otro: el cambio vale para el par.
      var anterior = sim.cambios.traslados[clave];
      base = anterior ? anterior.base : b.base.traslado[i];
    }
    if (Math.abs(valor - base) < TOLERANCIA) delete sim.cambios[tipo][clave];
    else sim.cambios[tipo][clave] = { base: Math.round(base * 10000) / 10000, valor: Math.round(valor * 10000) / 10000 };
  }

  function vecina(modelo, td, direccion) {
    var celdas = [].slice.call(modelo.tabla.querySelectorAll('td.sim-editable'));
    if (direccion === 'derecha' || direccion === 'izquierda') {
      var fila = [].slice.call(td.parentNode.querySelectorAll('td.sim-editable'));
      var j = fila.indexOf(td) + (direccion === 'derecha' ? 1 : -1);
      return fila[j] || null;
    }
    var col = td.dataset.simCol, filas = celdas.filter(function (c) { return c.dataset.simCol === col; });
    var p = filas.indexOf(td) + (direccion === 'abajo' ? 1 : -1);
    return filas[p] || null;
  }

  // --- Barra de la simulación -----------------------------------------------------
  function barraHtml() {
    return '<div class="sim-barra" data-sim-barra>' +
      '<button type="button" class="sim-alternar" aria-pressed="false" data-sim="alternar">Simular compras y traslados</button>' +
      '<div class="sim-controles" hidden>' +
        '<label class="sim-campo">Escenario<select data-sim="escenario"></select></label>' +
        '<label class="sim-campo sim-campo-nombre">Nombre<input type="text" maxlength="80" data-sim="nombre" placeholder="Ej.: más compras desde junio"></label>' +
        '<button type="button" class="sim-boton" data-sim="guardar">Guardar escenario</button>' +
        '<button type="button" class="sim-boton" data-sim="restablecer">Volver al S&amp;OP</button>' +
        '<button type="button" class="sim-boton" data-sim="eliminar">Eliminar</button>' +
        '<button type="button" class="sim-boton sim-boton-aplicar" data-sim="aplicar">Aplicar al archivo del S&amp;OP</button>' +
      '</div>' +
      '<p class="sim-estado" aria-live="polite" data-sim="estado"></p>' +
      '<p class="sim-estado sim-copias" role="status" aria-live="polite" data-sim="copias" hidden></p>' +
    '</div>';
  }

  function pintarBarra(modelo) {
    var barra = modelo.barra;
    if (!barra) return;
    var controles = barra.querySelector('.sim-controles'), alternar = barra.querySelector('[data-sim="alternar"]');
    alternar.setAttribute('aria-pressed', String(sim.activa));
    alternar.textContent = sim.activa ? 'Terminar simulación' : 'Simular compras y traslados';
    controles.hidden = !sim.activa;
    var select = barra.querySelector('[data-sim="escenario"]');
    var opciones = '<option value="">Sin escenario (S&amp;OP ' + escapar(sim.sopTexto || '') + ')</option>' +
      sim.escenarios.map(function (e) {
        var marca = e.local ? ' · este navegador' : (e.aplicado && e.aplicado.length ? ' · aplicado' : '');
        return '<option value="' + escapar(e.id) + '">' + escapar(e.nombre) + marca + '</option>';
      }).join('');
    if (select.innerHTML !== opciones) select.innerHTML = opciones;
    select.value = sim.id;
    var nombre = barra.querySelector('[data-sim="nombre"]');
    if (document.activeElement !== nombre) nombre.value = sim.nombre;
    var actual = escenarioActual();
    barra.querySelector('[data-sim="eliminar"]').hidden = !actual || (!actual.local && enCopia());
    var aplicar = barra.querySelector('[data-sim="aplicar"]');
    aplicar.hidden = enCopia() || !sim.servidor;
    aplicar.disabled = !(sim.servidor && sim.servidor.puede_aplicar) || !actual || actual.local;
    aplicar.title = !sim.servidor ? '' : !sim.servidor.puede_aplicar ? sim.servidor.motivo_no_aplicar
      : !actual ? 'Guarda el escenario para aplicarlo.' : 'Reemplaza «' + sim.servidor.archivo + '» con este escenario (queda un respaldo).';

    var estado = barra.querySelector('[data-sim="estado"]');
    if (!sim.activa) {
      estado.textContent = nCambios() ? 'Hay un escenario abierto con ' + nCambios() + ' celda(s) cambiada(s): la tabla muestra el S&OP hasta que vuelvas a simular.' : '';
      return;
    }
    var partes = [];
    var n = nCambios();
    partes.push(n ? n + (n === 1 ? ' celda cambiada' : ' celdas cambiadas') : 'Escribe en las celdas de Abastecimiento y de traslados (en toneladas; «5k» = 5,000)');
    if (modelo.quiebres && n) partes.push('materiales con quiebre: ' + modelo.quiebres[0] + ' en el S&OP, ' + modelo.quiebres[1] + ' con el escenario');
    if (n && sinGuardar()) partes.push('sin guardar');
    if (actual && actual.aplicado && actual.aplicado.length) {
      var ap = actual.aplicado[actual.aplicado.length - 1];
      partes.push('aplicado al archivo el ' + ap.fecha.slice(8, 10) + '/' + ap.fecha.slice(5, 7) + '/' + ap.fecha.slice(0, 4) + ' ' + ap.fecha.slice(11, 16));
    }
    var nota = enCopia() ? ' En esta copia, los escenarios nuevos quedan solo en este navegador; para aplicarlos al archivo del S&OP, ármalos en el localhost.'
      : ' Solo cambia esta tabla: los gráficos y el resto de la pestaña siguen con el S&OP.';
    estado.textContent = partes.join(' · ') + '.' + nota;
  }

  function escenarioActual() {
    return sim.id ? sim.escenarios.find(function (e) { return e.id === sim.id; }) || null : null;
  }

  function mensaje(modelo, texto, error) {
    var estado = modelo.barra && modelo.barra.querySelector('[data-sim="estado"]');
    if (!estado) return;
    estado.textContent = texto;
    estado.classList.toggle('sim-error', !!error);
    clearTimeout(modelo._mensaje);
    modelo._mensaje = setTimeout(function () { estado.classList.remove('sim-error'); pintarBarra(modelo); }, error ? 9000 : 5000);
  }

  // --- Escenarios: servidor o este navegador -----------------------------------
  function locales() {
    try {
      var todos = JSON.parse(localStorage.getItem(CLAVE_LOCAL) || '[]');
      return Array.isArray(todos) ? todos : [];
    } catch (e) { return []; }
  }
  function guardarLocales(lista) {
    try { localStorage.setItem(CLAVE_LOCAL, JSON.stringify(lista)); return true; } catch (e) { return false; }
  }

  function csrf() {
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }
  function pedir(url, datos) {
    var opciones = datos ? { method: 'POST', credentials: 'same-origin', body: JSON.stringify(datos),
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() } } : { credentials: 'same-origin' };
    return fetch(url, opciones).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || 'El servidor respondió con un error (' + r.status + ').');
        return j;
      });
    });
  }

  function cargarEscenarios(modelo) {
    if (enCopia()) {
      var exportados = (window.uperuEscenariosSop || []).filter(function (e) { return e.sop === sim.sop; });
      var propios = locales().filter(function (e) { return e.sop === sim.sop; }).map(function (e) { e.local = true; return e; });
      sim.escenarios = propios.concat(exportados);
      sim.cargado = sim.sop;
      repintar();
      return Promise.resolve();
    }
    if (!sim.url) return Promise.resolve();
    var sop = sim.sop;
    sim.cargado = sop;
    sim.pidiendo = pedir(sim.url + '?sop=' + encodeURIComponent(sop)).then(function (j) {
      if (sim.sop !== sop) return;
      sim.servidor = j;
      sim.escenarios = j.escenarios || [];
      sim.sopTexto = j.sop_texto || '';
      pintarBarra(modelo);
    }).catch(function (e) { sim.cargado = ''; mensaje(modelo, 'No se pudieron leer los escenarios: ' + e.message, true); });
    return sim.pidiendo;
  }

  function abrirEscenario(modelo, id) {
    var e = sim.escenarios.find(function (x) { return x.id === id; });
    sim.id = e ? e.id : '';
    sim.nombre = e ? e.nombre : '';
    sim.cambios = vacio();
    if (e) {
      // La base es el S&OP de hoy: un cambio que ya coincide con el S&OP (p. ej. tras aplicarlo) no cuenta.
      Object.keys(e.cambios.compras || {}).forEach(function (k) { sim.cambios.compras[k] = { base: e.cambios.compras[k].base, valor: e.cambios.compras[k].valor }; });
      Object.keys(e.cambios.traslados || {}).forEach(function (k) { sim.cambios.traslados[k] = { base: e.cambios.traslados[k].base, valor: e.cambios.traslados[k].valor }; });
      rebasar(modelo);
    }
    sim.guardado = e ? firma(e.cambios) : firma(vacio());
    repintar();
  }

  // Una compra guardada en lo que ahora llega solo por traslado (bloque sin fila Abastecimiento, p. ej. la Caliza
  // Roca Extraída de Atocongo desde 2026-11-11) pasa a ser su traslado recibido, como escenarios_sop._normalizar.
  function compraATraslado(b, mes) {
    var k = claveCompra(b, mes), c = sim.cambios.compras[k];
    if (!c || b.filas.compras || !b.filas.recibido || !b.envia) return;
    delete sim.cambios.compras[k];
    if (!sim.cambios.traslados[claveRecibido(b, mes)]) sim.cambios.traslados[claveRecibido(b, mes)] = c;
  }

  // Actualiza la base de cada cambio con el S&OP que muestra la tabla (los bloques que estén a la vista).
  function rebasar(modelo) {
    modelo.bloques.forEach(function (b) {
      modelo.meses.forEach(function (m, i) {
        compraATraslado(b, m.mes);
        var pares = [['compras', claveCompra(b, m.mes), b.base.compras], ['traslados', claveRecibido(b, m.mes), b.base.recibido],
                     ['traslados', claveEnvio(b, m.mes), b.base.traslado]];
        pares.forEach(function (p) {
          var c = p[1] && sim.cambios[p[0]][p[1]];
          if (!c || !p[2]) return;
          c.base = Math.round(p[2][i] * 10000) / 10000;
          if (Math.abs(c.valor - c.base) < TOLERANCIA) delete sim.cambios[p[0]][p[1]];
        });
      });
    });
  }

  function guardarEscenario(modelo) {
    var nombre = modelo.barra.querySelector('[data-sim="nombre"]').value.trim();
    if (!nombre) { mensaje(modelo, 'Escribe un nombre para el escenario.', true); modelo.barra.querySelector('[data-sim="nombre"]').focus(); return; }
    if (!nCambios()) { mensaje(modelo, 'El escenario no cambia ninguna celda.', true); return; }
    var actual = escenarioActual();
    // Con otro nombre se guarda como uno nuevo (el abierto queda como estaba).
    var mismo = actual && actual.nombre.toLowerCase() === nombre.toLowerCase() ? actual : null;
    if (mismo && enCopia() && !mismo.local) mismo = null;  // uno del tablero no se pisa desde la copia
    var datos = { id: mismo ? mismo.id : '', nombre: nombre, sop: sim.sop, cambios: sim.cambios };
    if (enCopia() || (mismo && mismo.local)) {
      var lista = locales(), ahora = new Date().toISOString().slice(0, 19);
      if (!mismo && lista.concat(sim.escenarios).some(function (e) { return e.sop === sim.sop && e.nombre.toLowerCase() === nombre.toLowerCase(); })) {
        mensaje(modelo, 'Ya hay un escenario «' + nombre + '»: ábrelo para guardarlo encima o usa otro nombre.', true); return;
      }
      var e = { id: mismo ? mismo.id : 'local-' + Date.now().toString(36), nombre: nombre, sop: sim.sop,
                cambios: JSON.parse(JSON.stringify(sim.cambios)), modificado: ahora, local: true };
      lista = lista.filter(function (x) { return x.id !== e.id; }).concat([e]);
      if (!guardarLocales(lista)) { mensaje(modelo, 'Este navegador no permite guardar datos de la página.', true); return; }
      sim.escenarios = sim.escenarios.filter(function (x) { return x.id !== e.id; });
      sim.escenarios.unshift(e);
      despuesDeGuardar(modelo, e, 'Guardado en este navegador.');
      return;
    }
    pedir(sim.url + 'guardar/', datos).then(function (j) {
      sim.escenarios = sim.escenarios.filter(function (x) { return x.id !== j.escenario.id; });
      sim.escenarios.unshift(j.escenario);
      despuesDeGuardar(modelo, j.escenario, 'Escenario guardado.');
    }).catch(function (err) { mensaje(modelo, err.message, true); });
  }
  function despuesDeGuardar(modelo, e, texto) {
    sim.id = e.id; sim.nombre = e.nombre; sim.guardado = firma(e.cambios);
    repintar();
    mensaje(modelo, texto + ' «' + e.nombre + '», ' + nCambios() + ' celda(s).');
  }

  function eliminarEscenario(modelo) {
    var actual = escenarioActual();
    if (!actual || !window.confirm('¿Eliminar el escenario «' + actual.nombre + '»? No cambia el archivo del S&OP.')) return;
    var listo = function () {
      sim.escenarios = sim.escenarios.filter(function (x) { return x.id !== actual.id; });
      sim.id = ''; sim.nombre = ''; sim.guardado = firma(vacio());
      if (!nCambios()) sim.guardado = firma(sim.cambios);
      repintar();
      mensaje(modelo, 'Escenario «' + actual.nombre + '» eliminado. Los cambios siguen en la tabla hasta que vuelvas al S&OP.');
    };
    if (actual.local) { guardarLocales(locales().filter(function (x) { return x.id !== actual.id; })); listo(); return; }
    pedir(sim.url + 'eliminar/', { id: actual.id }).then(listo).catch(function (err) { mensaje(modelo, err.message, true); });
  }

  function aplicarEscenario(modelo, boton) {
    var actual = escenarioActual();
    if (!actual || !sim.servidor || !sim.servidor.puede_aplicar) return;
    if (sinGuardar()) { mensaje(modelo, 'Guarda los cambios del escenario antes de aplicarlo.', true); return; }
    var n = Object.keys(actual.cambios.compras).length + Object.keys(actual.cambios.traslados).length;
    // Los traslados entre Atocongo y Condorcocha van además al archivo de traslados del S&OP.
    var traslados = Object.keys(actual.cambios.traslados).length && sim.servidor.archivo_traslados
      ? '\n\nSus traslados entre centros van también a «' + sim.servidor.archivo_traslados + '»' +
        (sim.servidor.traslados_existe ? '' : ' (se crea con los traslados que ya tiene el S&OP)') + '.' : '';
    if (!window.confirm('Se reemplazará «' + sim.servidor.archivo + '» con las ' + n + ' celda(s) del escenario «' + actual.nombre +
        '» y se volverá a cargar el S&OP.' + traslados + '\n\nLos archivos actuales quedan respaldados en _RETIRADOS. ¿Continuar?')) return;
    boton.disabled = true;
    boton.classList.add('ocupado');
    mensaje(modelo, 'Aplicando el escenario: respaldo, archivo y recarga del S&OP… (puede tardar un minuto)');
    pedir(sim.url + 'aplicar/', { id: actual.id }).then(function (j) {
      var nota = j.copias && j.copias.estado === 'actualizando' ? ' Las copias HTML se actualizan en segundo plano.' : '';
      var archivos = (j.archivos || [j.archivo]).map(function (a) { return '«' + a + '»'; });
      mensaje(modelo, 'Listo: ' + archivos.join(' y ') + (archivos.length > 1 ? ' quedaron' : ' quedó') + ' con el escenario (' +
        j.celdas + ' celdas). Respaldo en ' + j.respaldo + '.' + nota + ' Recargando la página…');
      sim.activa = false;
      setTimeout(function () { location.reload(); }, 1800);
    }).catch(function (err) {
      boton.disabled = false;
      boton.classList.remove('ocupado');
      mensaje(modelo, err.message, true);
    });
  }

  // --- Copias HTML ------------------------------------------------------------------
  // Al aplicar un escenario, el servidor regenera en segundo plano la copia de
  // Planificación y la del sitio (gestion/copias_html.py; pedido del usuario,
  // 2026-11-11): la barra dice cómo va, consultando cada 5 s mientras dura.
  var copias = { datos: null, espera: null };
  function reciente(iso) { var t = Date.parse(iso || ''); return isFinite(t) && Date.now() - t < 60 * 60 * 1000; }
  function pintarCopias() {
    var d = copias.datos || {}, texto = '', error = false;
    if (d.estado === 'actualizando') texto = 'Actualizando las copias HTML (Planificación y sitio)… toma unos minutos.';
    else if (d.estado === 'listo' && reciente(d.fin)) texto = 'Copias HTML actualizadas a las ' + d.fin.slice(11, 16) + '.';
    else if (d.estado === 'error' && reciente(d.fin)) {
      texto = (d.mensaje || 'No se pudieron actualizar las copias HTML.') + ' Se actualizarán con la tarea diaria.';
      error = true;
    }
    modelos.forEach(function (m) {
      var p = m.barra && m.barra.querySelector('[data-sim="copias"]');
      if (!p) return;
      p.hidden = !texto;
      p.textContent = texto;
      p.classList.toggle('sim-error', error);
    });
  }
  function revisarCopias() {
    if (enCopia() || !sim.url) return;
    clearTimeout(copias.espera);
    pedir(sim.url + 'copias/').then(function (d) {
      copias.datos = d;
      pintarCopias();
      if (d.estado === 'actualizando') copias.espera = setTimeout(revisarCopias, 5000);
    }).catch(function () { /* sin estado, la barra no dice nada de las copias */ });
  }

  // --- Montaje ---------------------------------------------------------------------
  var modelos = [];
  function repintar() {
    modelos = modelos.filter(function (m) { return document.contains(m.tabla); });
    modelos.forEach(function (m) { pintar(m); pintarBarra(m); });
  }

  function preparar(tabla) {
    tabla.dataset.simListo = '1';
    var modelo = leerTabla(tabla);
    if (!modelo.bloques.length || !modelo.meses.length) return;
    tabla._simModelo = modelo;
    var sop = tabla.dataset.sop || '';
    if (sop !== sim.sop) {
      sim.sop = sop; sim.id = ''; sim.nombre = ''; sim.cambios = vacio(); sim.guardado = firma(vacio());
      sim.escenarios = []; sim.servidor = null; sim.activa = false; sim.cargado = '';
      sim.sopTexto = tabla.dataset.sopTexto || '';
    }
    sim.url = tabla.dataset.escenariosUrl || '';
    if (sim.url && !/\/$/.test(sim.url)) sim.url += '/';
    var contenedor = tabla.closest('.tabla-movimiento') || tabla;
    contenedor.insertAdjacentHTML('beforebegin', barraHtml());
    modelo.barra = contenedor.previousElementSibling;
    modelos.push(modelo);
    modelo.barra.addEventListener('click', function (e) {
      var b = e.target.closest('button[data-sim]');
      if (!b) return;
      var accion = b.dataset.sim;
      if (accion === 'alternar') {
        sim.activa = !sim.activa;
        if (sim.activa && sim.cargado !== sim.sop) cargarEscenarios(modelo);
        repintar();
      } else if (accion === 'guardar') guardarEscenario(modelo);
      else if (accion === 'restablecer') {
        if (nCambios() && sinGuardar() && !window.confirm('Hay cambios sin guardar. ¿Volver a los valores del S&OP?')) return;
        sim.id = ''; sim.nombre = ''; sim.cambios = vacio(); sim.guardado = firma(vacio());
        repintar();
      } else if (accion === 'eliminar') eliminarEscenario(modelo);
      else if (accion === 'aplicar') aplicarEscenario(modelo, b);
    });
    modelo.barra.addEventListener('change', function (e) {
      if (e.target.dataset.sim !== 'escenario') return;
      if (nCambios() && sinGuardar() && !window.confirm('Hay cambios sin guardar. ¿Descartarlos y abrir otro escenario?')) {
        e.target.value = sim.id; return;
      }
      abrirEscenario(modelo, e.target.value);
    });
    modelo.barra.addEventListener('input', function (e) {
      if (e.target.dataset.sim === 'nombre') sim.nombre = e.target.value;
    });
    tabla.addEventListener('click', function (e) {
      var td = e.target.closest('td.sim-editable');
      if (td && !td.querySelector('input')) abrirEditor(modelo, td);
    });
    tabla.addEventListener('keydown', function (e) {
      var td = e.target.closest && e.target.closest('td.sim-editable');
      if (!td || e.target !== td) return;
      var dir = { ArrowRight: 'derecha', ArrowLeft: 'izquierda', ArrowDown: 'abajo', ArrowUp: 'arriba' }[e.key];
      if (dir) { var v = vecina(modelo, td, dir); if (v) { e.preventDefault(); v.focus(); } return; }
      if (e.key === 'Enter' || e.key === 'F2' || /^[\d.k]$/i.test(e.key)) {
        e.preventDefault();
        abrirEditor(modelo, td);
        var input = td.querySelector('input');
        if (input && /^[\d.]$/.test(e.key)) input.value = e.key;
      } else if (e.key === 'Delete' || e.key === 'Backspace') {
        e.preventDefault();
        var b = modelo.bloques.find(function (x) { return x.tbody === td.closest('tbody'); });
        var i = +td.dataset.simCol;
        fijarCambio(b, td.dataset.simFila, modelo.meses[i].mes, i, 0);
        repintar();
        td.focus();
      }
    });
    if (sim.activa || nCambios()) rebasar(modelo);
    pintar(modelo);
    pintarBarra(modelo);
    if (sim.activa && sim.cargado !== sim.sop) cargarEscenarios(modelo);
    revisarCopias();
  }

  function buscar(raiz) {
    (raiz || document).querySelectorAll('table[data-simulable]:not([data-sim-listo])').forEach(preparar);
  }

  var pendiente = false;
  new MutationObserver(function () {
    if (pendiente) return;
    pendiente = true;
    requestAnimationFrame(function () { pendiente = false; buscar(document); });
  }).observe(document.documentElement, { childList: true, subtree: true });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { buscar(document); });
  else buscar(document);
  window.uperuSimulacionInventario = { buscar: buscar };
})();
