"""Edge sin ventana manejado por su protocolo de depuración (solo biblioteca
estándar): abre una página, ejecuta JavaScript y devuelve lo que calcula.
Lo usa verificar_copia para leer la copia HTML como la ve el navegador."""

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request

EDGE = os.environ.get("UPERU_EDGE", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


class _WebSocket:
    def __init__(self, url):
        hostport, ruta = url[len("ws://"):].split("/", 1)
        host, puerto = hostport.split(":")
        self.s = socket.create_connection((host, int(puerto)))
        clave = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{ruta} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {clave}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        respuesta = b""
        while b"\r\n\r\n" not in respuesta:
            respuesta += self.s.recv(4096)
        self.buf = respuesta.split(b"\r\n\r\n", 1)[1]

    def enviar(self, texto):
        datos = texto.encode()
        cabecera = bytearray([0x81])
        n = len(datos)
        if n < 126:
            cabecera.append(0x80 | n)
        elif n < 65536:
            cabecera.append(0x80 | 126)
            cabecera += struct.pack(">H", n)
        else:
            cabecera.append(0x80 | 127)
            cabecera += struct.pack(">Q", n)
        mascara = os.urandom(4)
        cabecera += mascara
        self.s.sendall(bytes(cabecera) + bytes(b ^ mascara[i % 4] for i, b in enumerate(datos)))

    def _leer(self, n):
        while len(self.buf) < n:
            trozo = self.s.recv(1 << 20)
            if not trozo:
                raise ConnectionError("Edge cerró la conexión")
            self.buf += trozo
        salida, self.buf = self.buf[:n], self.buf[n:]
        return salida

    def recibir(self):
        texto = b""
        while True:
            b1, b2 = self._leer(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._leer(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._leer(8))[0]
            carga = self._leer(n)
            codigo = b1 & 0x0F
            if codigo == 8:
                raise ConnectionError("Edge cerró la conexión")
            if codigo in (9, 10):
                continue
            texto += carga
            if b1 & 0x80:
                return texto.decode()


class Navegador:
    """`with Navegador() as nav: nav.abrir(url); nav.js("...")`."""

    def __init__(self, puerto=9361, ancho=1440, alto=900):
        self.puerto, self.ancho, self.alto = puerto, ancho, alto
        self.perfil = tempfile.mkdtemp(prefix="uperu_edge_")
        self.n = 0

    def __enter__(self):
        if not os.path.exists(EDGE):
            raise RuntimeError(f"No se encontró Edge en {EDGE} (variable UPERU_EDGE para otra ruta)")
        self.proceso = subprocess.Popen(
            [EDGE, "--headless=new", f"--remote-debugging-port={self.puerto}", f"--user-data-dir={self.perfil}",
             f"--window-size={self.ancho},{self.alto}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.puerto}/json/version", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        destino = json.load(urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{self.puerto}/json/new?about:blank", method="PUT")))
        self.ws = _WebSocket(destino["webSocketDebuggerUrl"])
        self.cmd("Page.enable")
        self.cmd("Runtime.enable")
        self.cmd("Page.addScriptToEvaluateOnNewDocument", source=(
            "window.__errores=[];window.addEventListener('error',function(e){window.__errores.push(e.message)});"))
        return self

    def __exit__(self, *exc):
        self.proceso.terminate()
        try:
            self.proceso.wait(10)
        except subprocess.TimeoutExpired:
            self.proceso.kill()
        shutil.rmtree(self.perfil, ignore_errors=True)

    def cmd(self, metodo, **parametros):
        self.n += 1
        self.ws.enviar(json.dumps({"id": self.n, "method": metodo, "params": parametros}))
        while True:
            mensaje = json.loads(self.ws.recibir())
            if mensaje.get("id") == self.n:
                return mensaje.get("result", mensaje)

    def js(self, expresion):
        r = self.cmd("Runtime.evaluate", expression=expresion, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description", "error de JavaScript"))
        return r.get("result", {}).get("value")

    def abrir(self, url, espera=8):
        self.cmd("Page.navigate", url=url)
        time.sleep(espera)
