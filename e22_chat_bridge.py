#!/usr/bin/env python3
"""
e22_chat_bridge.py — консольный "мост" для общения с прошивкой LoRa-2026 Chat
на ESP32-S3 (см. LoRa-2026 Chat / e22_web_chat.ino) через USB-донгл
EBYTE E22-900T22U на компьютере.

В отличие от e22_transceiver.py, этот скрипт формирует и разбирает
БИНАРНУЮ структуру ChatPacket (53 байта, #pragma pack(1)), точно такую же,
как использует прошивка ESP32:

    struct ChatPacket {
      uint8_t msgId;
      uint8_t fromAddr;
      uint8_t ttl;
      uint8_t chunkIndex;
      uint8_t chunkTotal;
      char    nick[10];
      char    text[38];
    };

ВАЖНО про адресацию (fixed-режим E22):
  - На передачу модуль (и библиотека Ebyte на ESP32) ожидает от хоста
    3-байтный заголовок [ADDH][ADDL][CHAN] ПЕРЕД полезной нагрузкой —
    это адрес/канал ПОЛУЧАТЕЛЯ. Прошивка ESP32 шлёт на широковещательный
    адрес 0xFFFF, поэтому и этот скрипт по умолчанию шлёт на 0xFFFF.
  - На приём, при включённом fixed-режиме и RSSI (RSSI_ENABLED), модуль
    выдаёт в UART: [ADDH][ADDL][CHAN] + данные + [RSSI]. Это НЕ то же
    самое, что "3 байта заголовка получателя" при отправке — тут это
    адрес/канал ОТПРАВИТЕЛЯ, добавленные автоматически модулем.
    Библиотека на ESP32 эту обвязку снимает сама внутри receiveMessageRSSI().
    Здесь её приходится снимать вручную — см. parse_incoming_frame().

ПЕРЕД ИСПОЛЬЗОВАНИЕМ обязательно настройте COM-порт компьютера (донгл)
через RF_Settings GUI или e22_configure.py ТАК ЖЕ, как настроена ESP32
в configureE22():
    ADDH=0x00, ADDL=<свой уникальный адрес, НЕ 0x07 — он занят ESP32>
    NETID=0x02
    CHAN=19
    Air data rate = 2.4 kbps (AIR_DATA_RATE_010_24)
    UART = 9600 8N1
    Fixed transmission = включено
    LBT = включено
    RSSI byte = включено
Если хотя бы один параметр не совпадёт с ESP32 — пакеты не пройдут молча,
без явной ошибки (радио просто не "услышит" друг друга).

Установка зависимостей:
    pip install pyserial

Примеры запуска:
    # список портов
    python e22_chat_bridge.py --list

    # интерактивный чат: слушаем эфир и отправляем свои сообщения
    python e22_chat_bridge.py --port COM13 --baud 9600 --address 0x01 --nick pc-user

    # разовая отправка одного сообщения и выход
    python e22_chat_bridge.py --port COM13 --address 0x01 --nick pc-user --send "привет из python"
"""

import argparse
import struct
import sys
import threading
import time
from typing import Optional, Tuple

import serial
import serial.tools.list_ports

# ==== Константы протокола — должны совпадать с прошивкой ESP32 ====
NICK_LEN = 10          # 9 символов + '\0'
TEXT_CHUNK_LEN = 38    # размер одного фрагмента текста
PACKET_FMT = f"<BBBBB{NICK_LEN}s{TEXT_CHUNK_LEN}s"
PACKET_SIZE = struct.calcsize(PACKET_FMT)  # должно быть 53
assert PACKET_SIZE == 53, f"неожиданный размер структуры: {PACKET_SIZE}"

BROADCAST_ADDH = 0xFF
BROADCAST_ADDL = 0xFF
DEFAULT_CHANNEL = 19
DEFAULT_TTL = 3
MAX_CHUNKS = 8


def list_ports() -> None:
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("Порты не найдены.")
        return
    for p in ports:
        print(f"{p.device}\t{p.description}")


def auto_int(x: str) -> int:
    return int(x, 0)


# ======================= Сборка/разбор ChatPacket ============================

def build_chat_packets(nick: str, text: str, msg_id: int, from_addr: int,
                        ttl: int = DEFAULT_TTL) -> list:
    """Разбивает текст на фрагменты по TEXT_CHUNK_LEN и упаковывает их
    в список бинарных ChatPacket (bytes), как это делает sendChatOverLora()
    в прошивке ESP32."""
    text_bytes = text.encode("utf-8")
    chunk_total = 1 if len(text_bytes) == 0 else (
        (len(text_bytes) + TEXT_CHUNK_LEN - 1) // TEXT_CHUNK_LEN
    )
    chunk_total = min(chunk_total, MAX_CHUNKS)

    nick_bytes = nick.encode("utf-8")[: NICK_LEN - 1]

    packets = []
    for i in range(chunk_total):
        offset = i * TEXT_CHUNK_LEN
        chunk = text_bytes[offset: offset + TEXT_CHUNK_LEN]
        pkt = struct.pack(
            PACKET_FMT,
            msg_id & 0xFF,
            from_addr & 0xFF,
            ttl & 0xFF,
            i,
            chunk_total,
            nick_bytes,
            chunk,
        )
        packets.append(pkt)
    return packets


def build_frame_for_tx(payload: bytes, dest_addr: int, channel: int) -> bytes:
    """Добавляет 3-байтный заголовок получателя для fixed-режима
    (аналог того, что делает sendFixedMessage() в библиотеке ESP32)."""
    addh = (dest_addr >> 8) & 0xFF
    addl = dest_addr & 0xFF
    return bytes([addh, addl, channel & 0xFF]) + payload


def rssi_byte_to_dbm(b: int) -> int:
    return -(256 - b)


def try_unpack_chat_packet(data: bytes) -> Optional[Tuple]:
    """Пытается распарсить ровно PACKET_SIZE байт как ChatPacket.
    Возвращает (msg_id, from_addr, ttl, chunk_idx, chunk_total, nick, text)
    либо None, если данные не похожи на валидный пакет."""
    if len(data) != PACKET_SIZE:
        return None
    try:
        msg_id, from_addr, ttl, chunk_idx, chunk_total, nick_raw, text_raw = struct.unpack(
            PACKET_FMT, data
        )
    except struct.error:
        return None

    # простая проверка правдоподобности, чтобы не путать мусор с пакетом
    if chunk_total == 0 or chunk_total > MAX_CHUNKS or chunk_idx >= chunk_total:
        return None

    nick = nick_raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    text = text_raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    return msg_id, from_addr, ttl, chunk_idx, chunk_total, nick, text


def parse_incoming_frame(raw: bytes, rssi_enabled: bool):
    """Пытается вычленить ChatPacket из "сырого" куска, прочитанного из
    порта. Модуль в fixed-режиме с RSSI обычно отдаёт:
        [ADDH][ADDL][CHAN] + <PACKET_SIZE байт данных> + [RSSI]
    Но на практике куски могут приходить без адресного заголовка (в
    зависимости от прошивки/версии модуля) — поэтому пробуем несколько
    вариантов длины и берём тот, что даёт правдоподобный пакет.
    Возвращает (parsed_packet_or_None, rssi_dbm_or_None)."""

    n = len(raw)

    candidates = []
    # Вариант 1: заголовок(3) + пакет(53) + rssi(1) = 57
    if rssi_enabled and n >= 3 + PACKET_SIZE + 1:
        candidates.append((raw[3:3 + PACKET_SIZE], raw[3 + PACKET_SIZE]))
    # Вариант 2: заголовок(3) + пакет(53), без rssi = 56
    if n >= 3 + PACKET_SIZE:
        candidates.append((raw[3:3 + PACKET_SIZE], None))
    # Вариант 3: пакет(53) + rssi(1) = 54, без адресного заголовка
    if rssi_enabled and n >= PACKET_SIZE + 1:
        candidates.append((raw[0:PACKET_SIZE], raw[PACKET_SIZE]))
    # Вариант 4: голый пакет(53), без заголовка и без rssi
    if n >= PACKET_SIZE:
        candidates.append((raw[0:PACKET_SIZE], None))

    for payload, rssi_raw in candidates:
        parsed = try_unpack_chat_packet(payload)
        if parsed is not None:
            rssi_dbm = rssi_byte_to_dbm(rssi_raw) if rssi_raw is not None else None
            return parsed, rssi_dbm

    return None, None


# ======================= Поток чтения из порта ================================

def reader_thread(ser: serial.Serial, stop_event: threading.Event,
                   rssi_enabled: bool, own_addr: int, settle_s: float = 0.08) -> None:
    seen = set()  # (from_addr, msg_id) — простая защита от повторного вывода
    reasm = {}    # (from_addr, msg_id) -> {"nick":..., "chunks": {idx: text}, "total":...}

    while not stop_event.is_set():
        try:
            if ser.in_waiting == 0:
                time.sleep(0.02)
                continue

            time.sleep(settle_s)  # даём дочитать пакет одним куском
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                continue

            parsed, rssi_dbm = parse_incoming_frame(chunk, rssi_enabled)
            if parsed is None:
                print(f"\n[!] Получены нераспознанные байты ({len(chunk)}): {chunk.hex()}")
                continue

            msg_id, from_addr, ttl, chunk_idx, chunk_total, nick, text = parsed

            if from_addr == own_addr:
                continue  # своё же эхо, если оно вдруг долетает

            ts = time.strftime("%H:%M:%S")
            rssi_part = f"  RSSI={rssi_dbm} dBm" if rssi_dbm is not None else ""

            key = (from_addr, msg_id)
            if chunk_total == 1:
                if key in seen:
                    continue
                seen.add(key)
                print(f"\n[{ts}] {nick}: {text}{rssi_part}  (from=0x{from_addr:02X})")
                continue

            # многочанковое сообщение — собираем по частям
            slot = reasm.setdefault(key, {"nick": nick, "chunks": {}, "total": chunk_total})
            slot["chunks"][chunk_idx] = text
            if len(slot["chunks"]) == slot["total"]:
                full_text = "".join(slot["chunks"][i] for i in range(slot["total"]))
                if key not in seen:
                    seen.add(key)
                    print(f"\n[{ts}] {slot['nick']}: {full_text}{rssi_part}  (from=0x{from_addr:02X}, {slot['total']} частей)")
                del reasm[key]

        except serial.SerialException as e:
            print(f"\nОшибка чтения порта: {e}")
            stop_event.set()
            break


# ======================= main ================================================

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="показать список доступных портов")
    ap.add_argument("--port", help="имя порта, например COM13")
    ap.add_argument("--baud", type=int, default=9600, help="serial baud rate модуля")
    ap.add_argument("--address", type=auto_int, default=0x01,
                     help="собственный адрес узла (ADDL), должен быть настроен "
                          "в модуле через RF_Settings/e22_configure.py и НЕ совпадать "
                          "с адресом ESP32 (у неё 0x07 по умолчанию в скетче)")
    ap.add_argument("--channel", type=int, default=DEFAULT_CHANNEL,
                     help=f"канал (по умолчанию {DEFAULT_CHANNEL}, должен совпадать с ESP32)")
    ap.add_argument("--nick", default="pc-user", help="ник, который увидят в чате")
    ap.add_argument("--ttl", type=int, default=DEFAULT_TTL, help="TTL ретрансляции пакета")
    ap.add_argument("--no-rssi", action="store_true",
                     help="указать, если RSSI byte выключен в конфигурации модуля")
    ap.add_argument("--send", help="отправить одно сообщение и выйти (без интерактивного режима)")
    ap.add_argument("--timeout", type=float, default=0.1, help="read timeout, сек")
    args = ap.parse_args()

    if args.list:
        list_ports()
        return
    if not args.port:
        ap.error("укажите --port (или --list, чтобы посмотреть доступные)")
    if not (0 <= args.channel <= 255):
        ap.error("--channel должен быть в диапазоне 0..255")
    if not (0 <= args.address <= 0xFF):
        ap.error("--address должен быть в диапазоне 0..255 (это ADDL, ADDH считается 0x00)")

    rssi_enabled = not args.no_rssi

    try:
        ser = serial.Serial(args.port, args.baud, timeout=args.timeout)
    except serial.SerialException as e:
        print(f"Не удалось открыть {args.port}: {e}")
        sys.exit(1)

    print(f"Открыт {args.port} @ {args.baud} baud, свой адрес=0x{args.address:02X}, "
          f"канал={args.channel}, RSSI={'вкл' if rssi_enabled else 'выкл'}, ник='{args.nick}'")
    print(f"Размер ChatPacket: {PACKET_SIZE} байт (должен совпадать с прошивкой ESP32)")

    msg_id_counter = 0

    def send_message(text: str) -> None:
        nonlocal msg_id_counter
        packets = build_chat_packets(args.nick, text, msg_id_counter, args.address, args.ttl)
        msg_id_counter = (msg_id_counter + 1) & 0xFF
        for i, pkt in enumerate(packets):
            frame = build_frame_for_tx(pkt, (BROADCAST_ADDH << 8) | BROADCAST_ADDL, args.channel)
            ser.write(frame)
            ser.flush()
            print(f"[TX] chunk {i + 1}/{len(packets)} отправлен ({len(frame)} байт на порт)")
            time.sleep(0.05)

    if args.send is not None:
        send_message(args.send)
        ser.close()
        return

    stop_event = threading.Event()
    t = threading.Thread(target=reader_thread,
                          args=(ser, stop_event, rssi_enabled, args.address),
                          daemon=True)
    t.start()

    print("Интерактивный режим. Вводите текст и жмите Enter для отправки. Ctrl+C для выхода.\n")
    try:
        while True:
            line = input()
            if line:
                send_message(line)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop_event.set()
        t.join(timeout=1)
        ser.close()
        print("\nПорт закрыт.")


if __name__ == "__main__":
    main()
