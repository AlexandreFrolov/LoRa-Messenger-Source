#!/usr/bin/env python3
"""
e22_chat_console.py — консольный LoRa-чат для Windows/Linux, совместимый
с прошивкой lora_chat_esp32.ino (ESP32-S3-N16R8 + Ebyte E22-900T22D,
проект LoRa-2026).

v2: формат ChatPacket расширен на 1 байт CRC8 (совместимо с прошивкой v2).
Раньше пакет правильного размера, но с повреждённым в эфире содержимым
(коллизия при ретрансляции, помеха) принимался как валидный и портил
сборку многочастных сообщений ("обрезание" текста). Теперь каждый пакет
несёт контрольную сумму по первым 53 байтам, и приёмник отбрасывает
пакет, если CRC не совпала, вместо того чтобы отдать в реассемблинг мусор.

ВАЖНО: эта версия скрипта НЕСОВМЕСТИМА со старой прошивкой (без CRC8) —
там был 53-байтный пакет, здесь 54 байта. Прошивка и скрипт должны быть
обновлены одновременно на обоих концах.

Скрипт реализует тот же бинарный протокол ChatPacket (msgId/fromAddr/ttl/
chunkIndex/chunkTotal/nick/text/crc8), что и прошивка, поэтому PC с USB-
донглом E22-900T22U подключается к сети LoRa-2026 как ещё один равноправный
узел: может отправлять сообщения (с разбиением на фрагменты по 38 байт) и
принимать/собирать чужие, включая ретранслированные другими узлами.

============================ Конфигурация донгла ===================================
Скрипт предполагает, что USB-донгл (в примере — COM13) уже настроен через
e22_configure.py / RF_Settings GUI так же, как показывает e22_read_config.py:

    ADDH:ADDL       0x00:0x01   (свой адрес узла — под себя, только ADDL
                                  используется как однобайтовый fromAddr,
                                  как и в прошивке ESP32)
    NetID           0x00
    Канал           19          (869.125 МГц — LORA_CHANNEL в .ino)
    Air data rate   2.4k        (AIR_DATA_RATE_010_24 в .ino)
    Режим передачи  fixed
    RSSI байт       вкл         (иначе запустите с флагом --no-rssi)
    LBT             вкл

Если конфигурация вашего донгла отличается — поменяйте параметры через
аргументы командной строки (--node-addr, --channel, --no-rssi).
======================================================================================

Протокол ChatPacket (см. lora_chat_esp32.ino, v2), 54 байта на линии, без
выравнивания (аналог #pragma pack(1)):

    uint8_t  msgId        — счётчик сообщений отправителя (0..255)
    uint8_t  fromAddr      — ADDL отправителя
    uint8_t  ttl            — сколько раз пакет ещё можно ретранслировать
    uint8_t  chunkIndex     — номер фрагмента (с 0)
    uint8_t  chunkTotal     — всего фрагментов в сообщении
    char     nick[10]       — ник, с завершающим нулём
    char     text[38]       — фрагмент текста (UTF-8)
    uint8_t  crc8           — CRC8 (полином 0x07) по всем 53 байтам выше

Если у донгла включён RSSI-байт (как в примере на COM13), на линии за
каждым 54-байтным пакетом следует ещё 1 байт RSSI.

ВАЖНО про паузу между чанками при отправке: модуль E22 может выполнять
LBT (прослушивание эфира) перед каждой передачей до 2 секунд (см. даташит
EBYTE, максимальный dwell time LBT). USB-версия донгла не даёт доступа к
пину AUX как отдельному сигналу (в отличие от TTL/SPI модулей), поэтому
аппаратного подтверждения "модуль освободился" нет — используется
фиксированная пауза между записями в порт с запасом на худший случай LBT.

Установка зависимостей:
    pip install pyserial

Примеры запуска:
    # список доступных портов
    python e22_chat_console.py --list

    # подключение к чату на COM13 конфигурацией "как в примере"
    python e22_chat_console.py --port COM13 --nick Alexandre

    # свой адрес узла/канал/без RSSI-байта
    python e22_chat_console.py --port COM13 --node-addr 0x02 --channel 19 --no-rssi
"""

import argparse
import random
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import serial
import serial.tools.list_ports

# ============================ Протокол (см. lora_chat_esp32.ino, v2) ================
NICK_LEN = 10
TEXT_CHUNK_LEN = 38

# Тело пакета без CRC (5 полей + nick + text) — 53 байта, ровно как раньше.
BODY_FMT = f"<BBBBB{NICK_LEN}s{TEXT_CHUNK_LEN}s"
BODY_LEN = struct.calcsize(BODY_FMT)
assert BODY_LEN == 53, BODY_LEN

# Полный пакет на линии: тело + 1 байт CRC8 = 54 байта.
PACKET_LEN = BODY_LEN + 1

BROADCAST_ADDH = 0xFF
BROADCAST_ADDL = 0xFF
DEFAULT_CHANNEL = 19
DEFAULT_FLOOD_TTL = 3
MAX_CHUNKS = 8  # см. receivedMask/REASM_SLOTS в .ino — сообщения длиннее
                # MAX_CHUNKS * TEXT_CHUNK_LEN байт прошивка не соберёт

# Пауза между записью чанков в serial-порт. LBT может занимать до 2с
# (см. datasheet EBYTE, "maximum dwell time of LBT is 2 seconds") — берём
# запас сверх этого максимума + время эфирной передачи пакета на 2.4kbps.
INTER_CHUNK_DELAY_SEC = 2.2


# ============================ CRC8 (полином 0x07, как в прошивке) ===================
def crc8(data: bytes) -> int:
    """CRC-8-CCITT, полином 0x07, старший бит вперёд, начальное значение 0.
    Должно побитово совпадать с crc8() в lora_chat_esp32.ino."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


def rssi_byte_to_dbm(b: int) -> int:
    """См. документацию EBYTE: dBm = -(256 - byte)."""
    return -(256 - b)


def auto_int(x: str) -> int:
    """Разбор адреса/канала в hex ('0x01') или decimal ('1')."""
    return int(x, 0)


def list_ports() -> None:
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("Порты не найдены.")
        return
    for p in ports:
        if p.vid and p.pid:
            print(f"{p.device}\t{p.description}\t(VID:PID={p.vid:04X}:{p.pid:04X})")
        else:
            print(f"{p.device}\t{p.description}")


# ============================ ChatPacket (тело + CRC8) ==============================
@dataclass
class ChatPacket:
    msg_id: int
    from_addr: int
    ttl: int
    chunk_index: int
    chunk_total: int
    nick: str
    text_bytes: bytes
    # ВАЖНО: text_bytes — это СЫРЫЕ байты фрагмента, а не декодированная строка.
    # Раньше каждый чанк резался по границе символа UTF-8 (_split_utf8) и сразу
    # декодировался в str — из-за этого непоследние чанки могли оказаться короче
    # TEXT_CHUNK_LEN, оставляя внутри общего буфера сборки ЛИШНИЙ нулевой байт-
    # паддинг посреди сообщения. Прошивка ESP32 копит все чанки в один C-буфер и
    # обрезает текст по ПЕРВОМУ нулевому байту (String(char*)) — такой "случайный"
    # нуль внутри буфера обрывал сообщение задолго до конца, хотя более поздние
    # чанки были получены и собраны верно. Теперь чанки режутся строго по
    # TEXT_CHUNK_LEN сырых байт (см. send_text), декодирование в UTF-8 происходит
    # один раз, после того как собраны ВСЕ фрагменты сообщения — так нулевой байт-
    # паддинг гарантированно может встретиться только в самом последнем чанке.

    def _body_bytes(self) -> bytes:
        # Ник — UTF-8; почти наверняка обрежется при кириллице (10 байт буфера
        # ~ 4-5 кириллических символов) — предупреждаем в send_text().
        nick_b = self.nick.encode("utf-8", errors="replace")[: NICK_LEN - 1]
        text_b = self.text_bytes[:TEXT_CHUNK_LEN]
        return struct.pack(
            BODY_FMT,
            self.msg_id & 0xFF,
            self.from_addr & 0xFF,
            self.ttl & 0xFF,
            self.chunk_index & 0xFF,
            self.chunk_total & 0xFF,
            nick_b.ljust(NICK_LEN, b"\x00"),
            text_b.ljust(TEXT_CHUNK_LEN, b"\x00"),
        )

    def pack(self) -> bytes:
        """Тело + CRC8 — итоговые 54 байта, как ждёт прошивка."""
        body = self._body_bytes()
        return body + bytes([crc8(body)])

    @staticmethod
    def unpack(raw: bytes) -> "ChatPacket":
        """Разбирает 54-байтный пакет. Бросает ValueError, если CRC не совпала —
        вызывающий код должен отловить это и отбросить пакет, не пытаясь
        скормить его в реассемблинг (см. _process_packet)."""
        if len(raw) != PACKET_LEN:
            raise struct.error(f"ожидалось {PACKET_LEN} байт, получено {len(raw)}")

        body = raw[:BODY_LEN]
        received_crc = raw[BODY_LEN]
        calc_crc = crc8(body)
        if calc_crc != received_crc:
            raise ValueError(
                f"CRC8 не совпала: получено 0x{received_crc:02X}, "
                f"посчитано 0x{calc_crc:02X} — пакет повреждён"
            )

        msg_id, from_addr, ttl, chunk_index, chunk_total, nick_b, text_b = struct.unpack(
            BODY_FMT, body
        )
        nick = nick_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace")

        # Обрезать по первому нулевому байту можно ТОЛЬКО для последнего чанка
        # сообщения — там нулевой паддинг легитимен и однозначен. Для всех
        # промежуточных чанков берём все TEXT_CHUNK_LEN байт как есть: они
        # заполнены отправителем ровно полностью (см. send_text), нулевой байт
        # внутри них не паддинг, а (крайне маловероятно, но не исключено при
        # errors='replace') часть реального содержимого.
        if chunk_index == chunk_total - 1:
            text_bytes = text_b.split(b"\x00", 1)[0]
        else:
            text_bytes = text_b

        return ChatPacket(msg_id, from_addr, ttl, chunk_index, chunk_total, nick, text_bytes)


# ============================ Сборка многочанковых сообщений ========================
class Reassembler:
    """Python-аналог ReassemblyBuf/handleIncomingPacket() из lora_chat_esp32.ino:
    копит фрагменты по (fromAddr, msgId) и отдаёт готовый (nick, text), когда
    получены все chunkTotal фрагментов. Также хранит кэш уже показанных
    сообщений (аналог seenCache) с TTL по времени, чтобы не выводить дубликаты
    от ретрансляции соседними узлами, но при этом не "залипать" навечно на
    старых msgId (например, после перезапуска этого же скрипта)."""

    SEEN_TTL_SEC = 120.0  # как SEEN_CACHE_TTL_MS в прошивке

    def __init__(self, seen_cache_size: int = 32):
        self._slots = {}          # (from_addr, msg_id) -> {"nick", "chunk_total", "chunks"}
        self._seen: dict = {}     # key -> timestamp
        self._seen_order: list = []
        self._seen_cache_size = seen_cache_size

    def _is_seen(self, key) -> bool:
        ts = self._seen.get(key)
        if ts is None:
            return False
        if time.monotonic() - ts > self.SEEN_TTL_SEC:
            return False  # запись протухла — считаем сообщение новым
        return True

    def _remember_seen(self, key) -> None:
        if key not in self._seen:
            self._seen_order.append(key)
            if len(self._seen_order) > self._seen_cache_size:
                old = self._seen_order.pop(0)
                self._seen.pop(old, None)
        self._seen[key] = time.monotonic()

    def feed(self, pkt: ChatPacket) -> Optional[tuple]:
        """Возвращает (nick, text), когда сообщение полностью собрано, иначе None."""
        key = (pkt.from_addr, pkt.msg_id)

        if pkt.chunk_total <= 1:
            if self._is_seen(key):
                return None
            self._remember_seen(key)
            text = pkt.text_bytes.decode("utf-8", errors="replace")
            return (pkt.nick, text)

        if self._is_seen(key):
            return None

        slot = self._slots.get(key)
        if slot is None:
            slot = {"nick": pkt.nick, "chunk_total": pkt.chunk_total, "chunks": {}}
            self._slots[key] = slot

        slot["chunks"][pkt.chunk_index] = pkt.text_bytes

        if len(slot["chunks"]) >= slot["chunk_total"]:
            # Конкатенируем СЫРЫЕ байты всех фрагментов и декодируем UTF-8
            # только один раз, целиком — так граница между чанками (даже если
            # она приходится на середину многобайтового символа) никак не
            # портит итоговый текст.
            full_bytes = b"".join(
                slot["chunks"].get(i, b"") for i in range(slot["chunk_total"])
            )
            del self._slots[key]
            self._remember_seen(key)
            text = full_bytes.decode("utf-8", errors="replace")
            return (slot["nick"], text)

        return None

        if len(slot["chunks"]) >= slot["chunk_total"]:
            full_text = "".join(slot["chunks"].get(i, "") for i in range(slot["chunk_total"]))
            del self._slots[key]
            self._remember_seen(key)
            return (slot["nick"], full_text)

        return None


# ============================ Клиент чата ============================================
class LoraChatClient:
    def __init__(self, ser: serial.Serial, node_addr: int, channel: int,
                 nick: str, ttl: int, rssi_enabled: bool, debug: bool = False):
        self.ser = ser
        self.node_addr = node_addr & 0xFF
        self.channel = channel & 0xFF
        self.nick = nick
        self.ttl = ttl
        self.rssi_enabled = rssi_enabled
        self.debug = debug
        self.packet_len_on_wire = PACKET_LEN + (1 if rssi_enabled else 0)
        # Случайный старт вместо 0 — снижает вероятность, что при перезапуске
        # скрипта msgId совпадёт с ещё не "протухшим" слотом сборки на ESP32
        # (см. REASM_SLOT_TIMEOUT_MS / stale-slot guard в прошивке v2).
        self.msg_id_counter = random.randint(0, 255)
        self.reasm = Reassembler()
        self._buf = bytearray()
        self._stop = threading.Event()

    def build_tx_frame(self, pkt: ChatPacket) -> bytes:
        """Fixed-режим E22: 3-байтный заголовок [ADDH][ADDL][CHANNEL] адресата
        (широковещательный, как BROADCAST_ADDH/ADDL в .ino) + сам ChatPacket."""
        payload = pkt.pack()
        return bytes([BROADCAST_ADDH, BROADCAST_ADDL, self.channel]) + payload

    @staticmethod
    def _split_bytes(data: bytes, max_bytes: int) -> list:
        """Режем СЫРЫЕ байты на куски по max_bytes, не заботясь о границах
        символов UTF-8 — резать посреди многобайтового символа безопасно,
        т.к. декодирование делается один раз, после сборки ВСЕХ чанков
        (см. Reassembler.feed). Раньше здесь была character-aware нарезка
        (_split_utf8), из-за которой непоследний чанк мог оказаться короче
        TEXT_CHUNK_LEN — это оставляло "случайный" нулевой байт-паддинг
        посреди буфера сборки на приёмнике и обрывало сообщение раньше
        времени (см. комментарий у ChatPacket.text_bytes)."""
        if not data:
            return [b""]
        return [data[i:i + max_bytes] for i in range(0, len(data), max_bytes)]

    def send_text(self, text: str) -> None:
        data = text.encode("utf-8", errors="replace")
        chunks = self._split_bytes(data, TEXT_CHUNK_LEN)
        if len(chunks) > MAX_CHUNKS:
            print(f"[!] Сообщение слишком длинное, обрезано до {MAX_CHUNKS} фрагментов "
                  f"(~{MAX_CHUNKS * TEXT_CHUNK_LEN} байт) — прошивка больше не соберёт")
            chunks = chunks[:MAX_CHUNKS]

        msg_id = self.msg_id_counter
        self.msg_id_counter = (self.msg_id_counter + 1) & 0xFF

        if self.debug:
            print(f"[DEBUG SPLIT] msgId={msg_id} всего чанков={len(chunks)}")
            for i, c in enumerate(chunks):
                # Лениво декодируем только для отображения в дебаге — на границе
                # чанка возможен символ-заменитель '\ufffd', если чанк обрывает
                # многобайтовый символ. Это ожидаемо и не влияет на итоговый
                # текст, который будет собран и раскодирован целиком на приёмнике.
                preview = c.decode("utf-8", errors="replace")
                print(f"[DEBUG SPLIT]   chunk {i}: {len(c)} байт (raw), "
                      f"превью={preview!r}")

        for i, chunk_bytes in enumerate(chunks):
            pkt = ChatPacket(
                msg_id=msg_id,
                from_addr=self.node_addr,
                ttl=self.ttl,
                chunk_index=i,
                chunk_total=len(chunks),
                nick=self.nick,
                text_bytes=chunk_bytes,
            )
            frame = self.build_tx_frame(pkt)
            self.ser.write(frame)
            self.ser.flush()
            if i < len(chunks) - 1:
                # пауза перед СЛЕДУЮЩИМ чанком — с запасом на LBT (до 2с по
                # даташиту) + airtime текущего пакета на 2.4kbps
                time.sleep(INTER_CHUNK_DELAY_SEC)

        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] Вы: {text}")

    def _process_packet(self, raw_with_rssi: bytes) -> None:
        if self.rssi_enabled:
            payload = raw_with_rssi[:-1]
            rssi_dbm: Optional[int] = rssi_byte_to_dbm(raw_with_rssi[-1])
        else:
            payload = raw_with_rssi
            rssi_dbm = None

        try:
            pkt = ChatPacket.unpack(payload)
        except ValueError as e:
            # CRC8 не совпала — пакет повреждён (коллизия в эфире, помеха и т.п.)
            if self.debug:
                print(f"[DEBUG CRC] отброшен повреждённый пакет ({len(payload)} байт): {e}")
            return
        except struct.error as e:
            print(f"[!] Ошибка разбора пакета ({len(payload)} байт): {e}")
            return

        if self.debug:
            rssi_part = f" RSSI={rssi_dbm}dBm" if rssi_dbm is not None else ""
            # Декодируем только для отображения — на границе непоследнего чанка
            # возможен '\ufffd' (символ-заменитель), если чанк обрывает
            # многобайтовый символ. Реальная сборка текста происходит в
            # Reassembler.feed() из СЫРЫХ байт всех чанков, эта строка не влияет
            # на итоговый результат.
            preview = pkt.text_bytes.decode("utf-8", errors="replace")
            print(f"[DEBUG RX] fromAddr=0x{pkt.from_addr:02X} msgId={pkt.msg_id} "
                  f"ttl={pkt.ttl} chunk={pkt.chunk_index+1}/{pkt.chunk_total} "
                  f"nick='{pkt.nick}' text='{preview}'{rssi_part}")

        if pkt.from_addr == self.node_addr:
            # свой же пакет, вернувшийся через ретрансляцию соседним узлом — не дублируем
            if self.debug:
                print(f"[DEBUG] отфильтровано: fromAddr совпадает с нашим "
                      f"(0x{self.node_addr:02X}) — это эхо своего же сообщения")
            return

        result = self.reasm.feed(pkt)
        if result is not None:
            nick, text = result
            rssi_part = f"  RSSI={rssi_dbm} dBm" if rssi_dbm is not None else ""
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] {nick}: {text}{rssi_part}")

    def reader_loop(self) -> None:
        while not self._stop.is_set():
            try:
                n = self.ser.in_waiting
                if n:
                    chunk = self.ser.read(n)
                    if self.debug:
                        print(f"[DEBUG RAW] +{len(chunk)} байт: {chunk.hex(' ')}")
                    self._buf.extend(chunk)
                while len(self._buf) >= self.packet_len_on_wire:
                    raw = bytes(self._buf[: self.packet_len_on_wire])
                    del self._buf[: self.packet_len_on_wire]
                    self._process_packet(raw)
                time.sleep(0.02)
            except serial.SerialException as e:
                print(f"\n[!] Ошибка чтения порта: {e}")
                self._stop.set()
                break

    def stop(self) -> None:
        self._stop.set()


def main() -> None:
    global INTER_CHUNK_DELAY_SEC
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="показать список доступных портов")
    ap.add_argument("--port", help="имя порта, например COM13")
    ap.add_argument("--baud", type=int, default=9600, help="serial baud rate модуля (9600)")
    ap.add_argument("--node-addr", type=auto_int, default=0x01,
                     help="ADDL этого узла (однобайтовый fromAddr в ChatPacket), "
                          "по умолчанию 0x01 — как в конфигурации COM13 из примера")
    ap.add_argument("--channel", type=int, default=DEFAULT_CHANNEL,
                     help=f"радиоканал (по умолчанию {DEFAULT_CHANNEL} — 869.125 МГц, "
                          "должен совпадать с LORA_CHANNEL в .ino)")
    ap.add_argument("--nick", default="PC", help="ник, отображаемый в чате (лучше латиницей — "
                                                   "буфер ника всего 9 значащих байт)")
    ap.add_argument("--ttl", type=int, default=DEFAULT_FLOOD_TTL,
                     help=f"TTL для флуд-ретрансляции наших сообщений (по умолчанию {DEFAULT_FLOOD_TTL})")
    ap.add_argument("--no-rssi", action="store_true",
                     help="донгл сконфигурирован БЕЗ RSSI-байта в пакете "
                          "(по умолчанию считаем, что RSSI байт включён, как в примере)")
    ap.add_argument("--chunk-delay", type=float, default=INTER_CHUNK_DELAY_SEC,
                     help=f"пауза между чанками в секундах при отправке длинных сообщений "
                          f"(по умолчанию {INTER_CHUNK_DELAY_SEC}с — с запасом на LBT до 2с "
                          "по даташиту EBYTE; уменьшайте осторожно и проверяйте на реальных "
                          "многочанковых сообщениях)")
    ap.add_argument("--debug", action="store_true",
                     help="показывать сырые байты с порта и разобранные поля пакета "
                          "ДО фильтрации own-address — полезно для диагностики "
                          "'сообщения не приходят обратно' и повреждённых по CRC пакетов")
    args = ap.parse_args()

    if args.list:
        list_ports()
        return
    if not args.port:
        ap.error("укажите --port (или используйте --list, чтобы посмотреть доступные)")
    if not (0 <= args.node_addr <= 0xFF):
        ap.error("--node-addr должен быть в диапазоне 0..255 (однобайтовый fromAddr)")
    if not (0 <= args.channel <= 255):
        ap.error("--channel должен быть в диапазоне 0..255")

    INTER_CHUNK_DELAY_SEC = args.chunk_delay

    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.1)
    except serial.SerialException as e:
        print(f"Не удалось открыть {args.port}: {e}")
        print("Windows: проверьте номер COM-порта в диспетчере устройств.")
        sys.exit(1)

    client = LoraChatClient(
        ser=ser,
        node_addr=args.node_addr,
        channel=args.channel,
        nick=args.nick,
        ttl=args.ttl,
        rssi_enabled=not args.no_rssi,
        debug=args.debug,
    )

    print(f"Открыт {args.port} @ {args.baud} baud")
    print(f"Узел: addr=0x{args.node_addr:02X}, channel={args.channel}, nick='{args.nick}', "
          f"TTL={args.ttl}, RSSI байт: {'вкл' if client.rssi_enabled else 'выкл'}")
    print(f"Размер пакета на линии: {client.packet_len_on_wire} байт "
          f"(payload {PACKET_LEN} = {BODY_LEN} тело + 1 CRC8"
          f"{' + 1 RSSI' if client.rssi_enabled else ''})")
    print(f"Пауза между чанками при отправке: {INTER_CHUNK_DELAY_SEC}с")
    print("Вводите текст и жмите Enter для отправки в чат LoRa-2026. Ctrl+C — выход.\n")

    t = threading.Thread(target=client.reader_loop, daemon=True)
    t.start()

    try:
        while True:
            line = input()
            if args.debug:
                print(f"[DEBUG INPUT] len(chars)={len(line)} "
                      f"len(utf8 bytes)={len(line.encode('utf-8'))} repr={line!r}")
            if line.strip():
                client.send_text(line)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        client.stop()
        t.join(timeout=1)
        ser.close()
        print("\nПорт закрыт.")


if __name__ == "__main__":
    main()
