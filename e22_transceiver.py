#!/usr/bin/env python3
"""
e22_transceiver.py — приём/передача данных через USB-донгл EBYTE E22-900T22U.

Работает одинаково на Windows 11 и Linux: модуль виден как обычный
последовательный порт (COMx в Windows, /dev/ttyUSB* в Linux).

Два режима работы:

1. Без --peer-address — прозрачная передача "как есть" (модуль должен
   быть в transparent-режиме, либо оба узла на одном канале/адресе).

2. С --peer-address — адресная (fixed) передача: перед данными
   отправляется 3-байтный заголовок [ADDH][ADDL][CHANNEL] получателя,
   как того требует fixed-режим передачи E22. Модуль ДОЛЖЕН быть
   предварительно переведён в fixed-режим (см. e22_configure.py).

Если модуль сконфигурирован с включённым RSSI byte (см. e22_configure.py,
поле "RSSI байт: вкл"), используйте --rssi — тогда последний байт
каждого принятого пакета будет интерпретирован как RSSI, а не как
часть текста.

Установка зависимостей:
    pip install pyserial

Примеры запуска:
    # список доступных портов
    python e22_transceiver.py --list

    # интерактивный обмен без адресации, без RSSI
    python e22_transceiver.py --port COM5 --baud 9600

    # адресный обмен с узлом 0x0002 на канале 19, с отображением RSSI
    python e22_transceiver.py --port COM5 --baud 9600 --peer-address 0x0002 --channel 19 --rssi

    # разовая отправка одной строки и выход
    python e22_transceiver.py --port COM5 --baud 9600 --peer-address 0x0002 --channel 19 --send "hello lora"
"""

import argparse
import sys
import threading
import time
from typing import Optional

import serial
import serial.tools.list_ports


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


def reader_thread(ser: serial.Serial, stop_event: threading.Event,
                   show_rssi: bool, settle_s: float = 0.08) -> None:
    """Фоновое чтение из порта — печатает всё, что прилетает из эфира."""
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

            ts = time.strftime("%H:%M:%S")

            rssi_dbm = None
            payload = chunk
            if show_rssi and len(chunk) >= 2:
                rssi_dbm = rssi_byte_to_dbm(chunk[-1])
                payload = chunk[:-1]

            text = payload.decode("utf-8", errors="replace")
            rssi_part = f"  RSSI={rssi_dbm} dBm" if rssi_dbm is not None else ""
            print(f"\n[{ts}] RX: {text}{rssi_part}")

        except serial.SerialException as e:
            print(f"\nОшибка чтения порта: {e}")
            stop_event.set()
            break


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="показать список доступных портов")
    ap.add_argument("--port", help="имя порта, например COM5 или /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600, help="serial baud rate модуля (по умолчанию 9600)")
    ap.add_argument("--send", help="отправить одну строку и выйти (без интерактивного режима)")
    ap.add_argument("--timeout", type=float, default=0.1, help="read timeout, сек")
    ap.add_argument("--peer-address", type=auto_int, default=None,
                     help="адрес модуля-собеседника (hex '0x0002' или decimal), "
                          "включает адресную отправку в fixed-режиме")
    ap.add_argument("--channel", type=int, default=19,
                     help="канал получателя для адресного заголовка (по умолчанию 19, "
                          "должен совпадать с настройкой обоих модулей)")
    ap.add_argument("--rssi", action="store_true",
                     help="интерпретировать последний байт каждого принятого пакета как RSSI "
                          "(модуль должен быть сконфигурирован с RSSI byte enable)")
    args = ap.parse_args()

    if args.list:
        list_ports()
        return

    if not args.port:
        ap.error("укажите --port (или используйте --list, чтобы посмотреть доступные)")

    if not (0 <= args.channel <= 255):
        ap.error("--channel должен быть в диапазоне 0..255")
    if args.peer_address is not None and not (0 <= args.peer_address <= 0xFFFF):
        ap.error("--peer-address должен быть в диапазоне 0..65535")

    try:
        ser = serial.Serial(args.port, args.baud, timeout=args.timeout)
    except serial.SerialException as e:
        print(f"Не удалось открыть {args.port}: {e}")
        print("Windows: проверьте номер COM-порта в диспетчере устройств и наличие драйвера моста.")
        print("Linux: проверьте, что пользователь в группе dialout (sudo usermod -aG dialout $USER).")
        sys.exit(1)

    mode = (f"адресный, peer=0x{args.peer_address:04X}, channel={args.channel}"
            if args.peer_address is not None else "прозрачный (без адресации)")
    print(f"Открыт {args.port} @ {args.baud} baud, режим: {mode}, RSSI: {'вкл' if args.rssi else 'выкл'}")

    if args.send is not None:
        frame = build_frame(args.send.encode("utf-8"), args.peer_address, args.channel)
        ser.write(frame)
        ser.flush()
        print(f"Отправлено: {args.send!r}")
        ser.close()
        return

    stop_event = threading.Event()
    t = threading.Thread(target=reader_thread, args=(ser, stop_event, args.rssi), daemon=True)
    t.start()

    print("Интерактивный режим. Вводите строки и жмите Enter для отправки. Ctrl+C для выхода.\n")
    try:
        while True:
            line = input()
            if line:
                frame = build_frame(line.encode("utf-8"), args.peer_address, args.channel)
                ser.write(frame)
                ser.flush()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        stop_event.set()
        t.join(timeout=1)
        ser.close()
        print("\nПорт закрыт.")


if __name__ == "__main__":
    main()
