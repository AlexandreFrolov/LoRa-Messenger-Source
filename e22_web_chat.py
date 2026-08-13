#!/usr/bin/env python3
"""
e22_web_chat.py — веб-чат для обмена сообщениями через USB-донгл EBYTE E22-900T22U.

Поднимает локальный Flask-сервер с простой веб-страницей:
  - сообщения, принятые по LoRa от удалённого узла, доставляются в браузер
    мгновенно через WebSocket (без опроса сервера);
  - текст, введённый в поле на странице, отправляется в эфир через тот же
    последовательный порт, что и в e22_transceiver.py.

Логика работы с протоколом E22 (адресация в fixed-режиме, разбор RSSI-байта)
взята без изменений из e22_transceiver.py.

Режимы адресации — как в e22_transceiver.py:
  - без --peer-address: прозрачная передача "как есть";
  - с --peer-address: адресная (fixed) передача — перед данными добавляется
    3-байтный заголовок [ADDH][ADDL][CHANNEL]. Модуль должен быть заранее
    переведён в fixed-режим (см. e22_configure.py).

Если модуль сконфигурирован с включённым RSSI byte, используйте --rssi —
тогда последний байт каждого принятого пакета будет интерпретирован
как RSSI, а не как часть текста.

Установка зависимостей:
    pip install pyserial flask flask-sock

Примеры запуска:
    # список доступных портов
    python e22_web_chat.py --list

    # веб-чат без адресации, сервер слушает на 0.0.0.0:5000
    python e22_web_chat.py --port COM5 --baud 9600

    # адресный чат с узлом 0x0002 на канале 19, с отображением RSSI,
    # веб-интерфейс только на localhost:8080
    python e22_web_chat.py --port /dev/ttyUSB0 --baud 9600 \\
        --peer-address 0x0002 --channel 19 --rssi \\
        --web-host 127.0.0.1 --web-port 8080

После запуска откройте в браузере адрес, который будет выведен в консоли
(по умолчанию http://<IP-машины>:5000/).
"""

import argparse
import itertools
import json
import logging
import queue
import sys
import threading
import time
from typing import Dict, List, Optional

import serial
import serial.tools.list_ports
from flask import Flask, Response, jsonify, request
from flask_sock import Sock


# ---------------------------------------------------------------------------
# Протокол E22 — те же функции, что и в e22_transceiver.py
# ---------------------------------------------------------------------------

def list_ports() -> None:
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("Порты не найдены.")
        return
    for p in ports:
        print(f"{p.device}\t{p.description}\t(VID:PID={p.vid:04X}:{p.pid:04X})"
              if p.vid and p.pid else f"{p.device}\t{p.description}")


def auto_int(x: str) -> int:
    """Разбор адреса в hex ('0x0002') или decimal ('2')."""
    return int(x, 0)


def build_frame(payload: bytes, peer_address: Optional[int], channel: int) -> bytes:
    """Собирает кадр для отправки: с адресным заголовком (fixed-режим) или без."""
    if peer_address is None:
        return payload
    addh = (peer_address >> 8) & 0xFF
    addl = peer_address & 0xFF
    return bytes([addh, addl, channel & 0xFF]) + payload


def rssi_byte_to_dbm(b: int) -> int:
    """Байт RSSI модуля E22 -> дБм (см. документацию: dBm = -(256 - byte))."""
    return -(256 - b)


# ---------------------------------------------------------------------------
# Хранилище сообщений чата (общее для потока чтения порта и HTTP-запросов)
# ---------------------------------------------------------------------------

class ChatState:
    """Хранилище сообщений + рассылка новых сообщений всем подписанным
    WebSocket-соединениям (каждое — своя очередь queue.Queue)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.messages: List[Dict] = []
        self._id_counter = itertools.count(1)
        self._subscribers: List["queue.Queue[Dict]"] = []

    def add(self, direction: str, text: str, rssi: Optional[int] = None) -> Dict:
        with self.lock:
            msg = {
                "id": next(self._id_counter),
                "time": time.strftime("%H:%M:%S"),
                "dir": direction,  # "rx" (принято по LoRa) или "tx" (отправлено с сайта)
                "text": text,
                "rssi": rssi,
            }
            self.messages.append(msg)
            subscribers = list(self._subscribers)
        for q in subscribers:
            q.put(msg)
        return msg

    def since(self, last_id: int) -> List[Dict]:
        with self.lock:
            return [m for m in self.messages if m["id"] > last_id]

    def snapshot(self) -> List[Dict]:
        with self.lock:
            return list(self.messages)

    def subscribe(self) -> "queue.Queue[Dict]":
        q: "queue.Queue[Dict]" = queue.Queue()
        with self.lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Dict]") -> None:
        with self.lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


chat = ChatState()
serial_write_lock = threading.Lock()  # защищает ser.write от одновременных HTTP-запросов
ser: Optional[serial.Serial] = None
cfg: Optional[argparse.Namespace] = None  # аргументы командной строки, заполняются в main()
reader_thread_obj: Optional[threading.Thread] = None  # для проверки живости через /api/status


def reader_thread(stop_event: threading.Event, settle_s: float = 0.08) -> None:
    """Фоновое чтение из порта — всё принятое из эфира кладём в ChatState.

    ВАЖНО: это daemon-поток. Если тут вылетит необработанное исключение,
    поток молча умрёт и приём перестанет работать без каких-либо следов
    в веб-интерфейсе — поэтому ловим Exception целиком, а не только
    SerialException, и всегда печатаем trace в консоль/journalctl.
    """
    while not stop_event.is_set():
        try:
            if ser.in_waiting == 0:
                time.sleep(0.02)
                continue

            # даём время дочитать остаток пакета одним куском
            time.sleep(settle_s)
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                continue

            rssi_dbm = None
            payload = chunk
            if cfg.rssi and len(chunk) >= 2:
                rssi_dbm = rssi_byte_to_dbm(chunk[-1])
                payload = chunk[:-1]

            text = payload.decode("utf-8", errors="replace")
            chat.add("rx", text, rssi_dbm)
            rssi_part = f" RSSI={rssi_dbm}dBm" if rssi_dbm is not None else ""
            print(f"[RX] {text!r}{rssi_part}", flush=True)

        except serial.SerialException as e:
            print(f"[RX] порт недоступен, поток чтения остановлен: {e}", file=sys.stderr, flush=True)
            chat.add("rx", f"[ошибка чтения порта: {e}]")
            stop_event.set()
            break

        except Exception:
            # Любая другая ошибка (например, при разборе конкретного пакета)
            # не должна останавливать приём последующих сообщений.
            import traceback
            traceback.print_exc()
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# Flask-приложение
# ---------------------------------------------------------------------------

app = Flask(__name__)
sock = Sock(app)

# Werkzeug по умолчанию пишет access-log на КАЖДЫЙ HTTP-запрос. Раньше это
# было заметно из-за поллинга раз в секунду; с WebSocket запросов гораздо
# меньше, но всё равно оставляем только предупреждения/ошибки — события
# LoRa (RX/TX) логируются отдельно через print() ниже.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

PAGE_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<title>LoRa веб-чат — E22</title>
<style>
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; }
  body {
    margin: 0; padding: 0;
    font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    background: #f2f4f7; color: #1a1a1a;
    display: flex; flex-direction: column;
    /* 100vh на мобильных считает высоту под адресной строкой браузера —
       страница получается выше видимой области. 100dvh (динамический
       viewport) учитывает реальную видимую высоту; там, где dvh не
       поддерживается, остаётся запасной вариант 100vh, объявленный выше. */
    height: 100vh;
    height: 100dvh;
    overflow: hidden;
  }
  header {
    background: #2c3e50; color: #fff; padding: 10px 14px;
    padding-top: calc(10px + env(safe-area-inset-top));
    display: flex; justify-content: space-between; align-items: center;
    flex-shrink: 0;
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; }
  #status { font-size: 11px; opacity: 0.85; white-space: nowrap; }
  #status.offline { color: #ff8080; }
  #chat {
    flex: 1; min-height: 0; overflow-y: auto; -webkit-overflow-scrolling: touch;
    padding: 12px; display: flex; flex-direction: column; gap: 8px;
  }
  .msg {
    max-width: 82%; padding: 8px 12px; border-radius: 12px;
    line-height: 1.35; font-size: 14px; word-wrap: break-word;
    white-space: pre-wrap;
  }
  .msg .meta { font-size: 11px; opacity: 0.6; margin-top: 4px; }
  .rx { align-self: flex-start; background: #fff; border: 1px solid #e0e0e0; border-bottom-left-radius: 2px; }
  .tx { align-self: flex-end; background: #2c7be5; color: #fff; border-bottom-right-radius: 2px; }
  .tx .meta { opacity: 0.75; }
  #composer {
    display: flex; gap: 8px; padding: 10px 12px;
    padding-bottom: calc(10px + env(safe-area-inset-bottom));
    background: #fff; border-top: 1px solid #e0e0e0;
    flex-shrink: 0;
  }
  #text {
    flex: 1; padding: 10px 12px; border: 1px solid #ccc; border-radius: 8px;
    /* 16px — минимум, чтобы iOS Safari не зумил страницу при фокусе на поле */
    font-size: 16px; resize: none; max-height: 100px; overflow-y: auto;
  }
  #send {
    padding: 0 18px; border: none; border-radius: 8px;
    background: #2c7be5; color: #fff; font-size: 14px; font-weight: 600;
    cursor: pointer; flex-shrink: 0;
  }
  #send:disabled { opacity: 0.5; cursor: default; }
  #empty { text-align: center; color: #888; font-size: 13px; margin-top: 24px; }

  @media (max-width: 480px) {
    header { padding-left: 12px; padding-right: 12px; }
    header h1 { font-size: 14px; }
    #chat { padding: 8px; gap: 6px; }
    .msg { max-width: 88%; font-size: 13.5px; }
  }
</style>
</head>
<body>
<header>
  <h1>LoRa веб-чат (E22-900T22U)</h1>
  <span id="status">подключение…</span>
</header>
<div id="chat"><div id="empty">Сообщений пока нет</div></div>
<div id="composer">
  <textarea id="text" rows="1" placeholder="Введите сообщение и нажмите Enter…"></textarea>
  <button id="send">Отправить</button>
</div>

<script>
const chatEl = document.getElementById('chat');
const statusEl = document.getElementById('status');
const textEl = document.getElementById('text');
const sendBtn = document.getElementById('send');

let ws = null;
let reconnectDelay = 1000;  // растёт до RECONNECT_MAX при обрывах связи
const RECONNECT_MAX = 10000;

function renderMessage(m) {
  const empty = document.getElementById('empty');
  if (empty) empty.remove();

  const div = document.createElement('div');
  div.className = 'msg ' + (m.dir === 'tx' ? 'tx' : 'rx');

  const textDiv = document.createElement('div');
  textDiv.textContent = m.text;
  div.appendChild(textDiv);

  const meta = document.createElement('div');
  meta.className = 'meta';
  let metaText = (m.dir === 'tx' ? 'Вы' : 'Удалённый узел') + ' · ' + m.time;
  if (m.rssi !== null && m.rssi !== undefined) {
    metaText += ' · RSSI ' + m.rssi + ' dBm';
  }
  meta.textContent = metaText;
  div.appendChild(meta);

  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function setStatus(text, offline) {
  statusEl.textContent = text;
  statusEl.classList.toggle('offline', !!offline);
  sendBtn.disabled = !!offline;
}

function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');

  ws.addEventListener('open', () => {
    setStatus('подключено', false);
    reconnectDelay = 1000;
  });

  ws.addEventListener('message', (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (e) {
      return;
    }
    if (msg.type === 'message') {
      renderMessage(msg.data);
    }
  });

  ws.addEventListener('close', () => {
    setStatus('нет связи с сервером, переподключение…', true);
    setTimeout(connectWebSocket, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.5, RECONNECT_MAX);
  });

  ws.addEventListener('error', () => {
    ws.close();
  });
}

function sendMessage() {
  const text = textEl.value.trim();
  if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ text }));
  textEl.value = '';
  textEl.focus();
}

sendBtn.addEventListener('click', sendMessage);
textEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

setStatus('подключение…', true);
connectWebSocket();
</script>
</body>
</html>
"""


def send_over_lora(text: str) -> Dict:
    """Собирает кадр, пишет его в порт и кладёт TX-сообщение в ChatState
    (что автоматически разошлёт его всем подключённым WebSocket-клиентам).
    Бросает serial.SerialException при ошибке записи в порт."""
    if ser is None:
        raise serial.SerialException("serial port not open")
    frame = build_frame(text.encode("utf-8"), cfg.peer_address, cfg.channel)
    with serial_write_lock:
        ser.write(frame)
        ser.flush()
    msg = chat.add("tx", text)
    print(f"[TX] {text!r}", flush=True)
    return msg


@app.route("/")
def index() -> Response:
    return Response(PAGE_HTML, mimetype="text/html")


@sock.route("/ws")
def ws_chat(ws) -> None:
    """WebSocket-соединение с браузером: при подключении отдаём всю историю,
    затем в реальном времени пушим новые RX/TX-сообщения и принимаем текст
    для отправки в эфир. flask-sock синхронный, поэтому в одном потоке на
    соединение чередуем короткое неблокирующее чтение от клиента с проверкой
    очереди исходящих сообщений."""
    q = chat.subscribe()
    try:
        for m in chat.snapshot():
            ws.send(json.dumps({"type": "message", "data": m}))

        while True:
            try:
                raw = ws.receive(timeout=0.2)
            except Exception:
                break  # соединение закрыто клиентом или сетевая ошибка

            if raw:
                try:
                    data = json.loads(raw)
                    text = (data.get("text") or "").strip()
                except (TypeError, ValueError, AttributeError):
                    text = ""
                if text:
                    try:
                        send_over_lora(text)
                    except serial.SerialException as e:
                        ws.send(json.dumps({"type": "error", "message": f"serial write failed: {e}"}))

            try:
                while True:
                    msg = q.get_nowait()
                    ws.send(json.dumps({"type": "message", "data": msg}))
            except queue.Empty:
                pass
    finally:
        chat.unsubscribe(q)


@app.route("/api/messages")
def api_messages():
    since = request.args.get("since", 0, type=int)
    return jsonify(chat.since(since))


@app.route("/api/status")
def api_status():
    with chat.lock:
        last_msg = chat.messages[-1] if chat.messages else None
    return jsonify({
        "serial_port_open": ser is not None and ser.is_open,
        "reader_thread_alive": reader_thread_obj is not None and reader_thread_obj.is_alive(),
        "total_messages": len(chat.messages),
        "last_message": last_msg,
    })


@app.route("/api/send", methods=["POST"])
def api_send():
    """REST-эндпоинт оставлен для отладки/скриптов (curl и т.п.);
    сама веб-страница теперь отправляет сообщения через /ws."""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "empty text"}), 400

    try:
        msg = send_over_lora(text)
    except serial.SerialException as e:
        return jsonify({"error": f"serial write failed: {e}"}), 500

    return jsonify(msg)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main() -> None:
    global ser, cfg, reader_thread_obj

    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="показать список доступных портов и выйти")
    ap.add_argument("--port", help="имя последовательного порта, например COM5 или /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600, help="serial baud rate модуля (по умолчанию 9600)")
    ap.add_argument("--timeout", type=float, default=0.1, help="read timeout порта, сек")
    ap.add_argument("--peer-address", type=auto_int, default=None,
                     help="адрес модуля-собеседника (hex '0x0002' или decimal), "
                          "включает адресную отправку в fixed-режиме")
    ap.add_argument("--channel", type=int, default=19,
                     help="канал получателя для адресного заголовка (по умолчанию 19, "
                          "должен совпадать с настройкой обоих модулей)")
    ap.add_argument("--rssi", action="store_true",
                     help="интерпретировать последний байт каждого принятого пакета как RSSI "
                          "(модуль должен быть сконфигурирован с RSSI byte enable)")
    ap.add_argument("--web-host", default="0.0.0.0", help="адрес, на котором слушает веб-сервер (по умолчанию 0.0.0.0)")
    ap.add_argument("--web-port", type=int, default=5000, help="порт веб-сервера (по умолчанию 5000)")
    ap.add_argument("--debug", action="store_true", help="запустить Flask в режиме отладки")
    cfg = ap.parse_args()

    if cfg.list:
        list_ports()
        return

    if not cfg.port:
        ap.error("укажите --port (или используйте --list, чтобы посмотреть доступные)")

    if not (0 <= cfg.channel <= 255):
        ap.error("--channel должен быть в диапазоне 0..255")
    if cfg.peer_address is not None and not (0 <= cfg.peer_address <= 0xFFFF):
        ap.error("--peer-address должен быть в диапазоне 0..65535")

    try:
        ser = serial.Serial(cfg.port, cfg.baud, timeout=cfg.timeout)
    except serial.SerialException as e:
        print(f"Не удалось открыть {cfg.port}: {e}")
        print("Windows: проверьте номер COM-порта в диспетчере устройств и наличие драйвера моста.")
        print("Linux: проверьте, что пользователь в группе dialout (sudo usermod -aG dialout $USER).")
        sys.exit(1)

    mode = (f"адресный, peer=0x{cfg.peer_address:04X}, channel={cfg.channel}"
            if cfg.peer_address is not None else "прозрачный (без адресации)")
    print(f"LoRa-порт {cfg.port} @ {cfg.baud} baud открыт, режим: {mode}, RSSI: {'вкл' if cfg.rssi else 'выкл'}")

    stop_event = threading.Event()
    t = threading.Thread(target=reader_thread, args=(stop_event,), daemon=True)
    t.start()
    reader_thread_obj = t

    print(f"Веб-интерфейс: http://{cfg.web_host}:{cfg.web_port}/  "
          f"(если сервер запущен на удалённой машине — используйте её IP вместо 0.0.0.0)")

    try:
        app.run(host=cfg.web_host, port=cfg.web_port, debug=cfg.debug, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        t.join(timeout=1)
        if ser is not None:
            ser.close()
        print("\nПорт закрыт.")


if __name__ == "__main__":
    main()
