#!/usr/bin/env python3
"""
e22_transceiver.py — приём/передача данных через USB-донгл EBYTE E22-900T22U.

Работает одинаково на Windows 11 и Linux: модуль виден как обычный
последовательный порт (COMx в Windows, /dev/ttyUSB* в Linux) и в
прозрачном (transmission) режиме просто ретранслирует байты в эфир и
обратно — конфигурировать ничего не нужно, режим по умолчанию.

Установка зависимостей:
    pip install pyserial

Примеры запуска:
    # список доступных портов
    python e22_transceiver.py --list

    # интерактивный режим: что печатаете — уходит в эфир,
    # что принято из эфира — печатается в консоль
    python e22_transceiver.py --port COM5 --baud 9600

    # разовая отправка одной строки и выход
    python e22_transceiver.py --port /dev/ttyUSB0 --baud 9600 --send "hello lora"

Важно:
    --baud должен совпадать с "serial baud rate", который выставлен в
    конфигурации модуля (по умолчанию у E22-900T22U обычно 9600,
    проверить/поменять можно официальной утилитой EBYTE RF_Settings
    в режиме конфигурации — зажать боковую кнопку на 2 сек, загорится
    красный светодиод). Скорость радиоэфира (air rate) на приёмнике и
    передатчике тоже должна совпадать — это отдельный параметр,
    настраивается там же.
"""

import argparse
import sys
import threading
import time

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


def reader_thread(ser: serial.Serial, stop_event: threading.Event) -> None:
    """Фоновое чтение из порта — печатает всё, что прилетает из эфира."""
    buf = bytearray()
    while not stop_event.is_set():
        try:
            n = ser.in_waiting
            if n:
                buf.extend(ser.read(n))
                # пытаемся показать как текст, если не получается — как hex
                try:
                    text = buf.decode("utf-8")
                    ts = time.strftime("%H:%M:%S")
                    print(f"\n[{ts}] RX: {text}")
                    buf.clear()
                except UnicodeDecodeError:
                    if len(buf) > 256:  # защита от мусора без валидного конца
                        ts = time.strftime("%H:%M:%S")
                        print(f"\n[{ts}] RX (hex): {buf.hex()}")
                        buf.clear()
            else:
                time.sleep(0.02)
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
    args = ap.parse_args()

    if args.list:
        list_ports()
        return

    if not args.port:
        ap.error("укажите --port (или используйте --list, чтобы посмотреть доступные)")

    try:
        ser = serial.Serial(args.port, args.baud, timeout=args.timeout)
    except serial.SerialException as e:
        print(f"Не удалось открыть {args.port}: {e}")
        print("Windows: проверьте номер COM-порта в диспетчере устройств и наличие драйвера моста.")
        print("Linux: проверьте, что пользователь в группе dialout (sudo usermod -aG dialout $USER).")
        sys.exit(1)

    print(f"Открыт {args.port} @ {args.baud} baud")

    if args.send is not None:
        ser.write(args.send.encode("utf-8"))
        ser.flush()
        print(f"Отправлено: {args.send!r}")
        ser.close()
        return

    stop_event = threading.Event()
    t = threading.Thread(target=reader_thread, args=(ser, stop_event), daemon=True)
    t.start()

    print("Интерактивный режим. Вводите строки и жмите Enter для отправки. Ctrl+C для выхода.\n")
    try:
        while True:
            line = input()
            if line:
                ser.write(line.encode("utf-8"))
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
