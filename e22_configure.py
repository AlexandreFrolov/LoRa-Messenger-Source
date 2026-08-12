#!/usr/bin/env python3
"""
e22_configure.py — запись конфигурации в EBYTE E22-900T22U:
    - минимально возможная мощность передачи (10dBm)
    - канал 19 (частота = 850.125 + 19 = 869.125 МГц)
    - LBT (Listen Before Talk) включён
    - fixed-режим передачи включён (нужен, чтобы адресация вообще работала)
    - RSSI байт добавляется к каждому принятому пакету
    - адрес модуля — задаётся параметром

ПЕРЕД ЗАПУСКОМ: переведите модуль в режим конфигурации — зажмите боковую
кнопку на 2 секунды, пока светодиод не загорится ПОСТОЯННЫМ красным.

Использование:
    pip install pyserial

    # постоянная запись (сохраняется во флеш, переживёт перезагрузку)
    python e22_configure.py --port COM5 --address 0x0001

    # временная запись (сбросится при следующем включении модуля)
    python e22_configure.py --port COM5 --address 1 --temporary

    # заодно поменять NetID (по умолчанию NetID не трогается)
    python e22_configure.py --port COM5 --address 1 --netid 0x00

Регистры (см. User Manual серии E22, стр. 13-16):
    REG1 (байт 7), биты 1:0 — TX power:  00=22dBm 01=17dBm 10=13dBm 11=10dBm
    REG3 (байт 9):
        бит 7 — RSSI byte enable (добавлять RSSI к принятым данным)
        бит 6 — transmission mode (0=transparent, 1=fixed)
        бит 5 — repeater (не трогаем)
        бит 4 — LBT enable
        бит 3 — WOR control (не трогаем)
        биты 2:0 — WOR cycle (не трогаем)
"""

import argparse
import sys
import time

import serial

MIN_TXPOWER_BITS = 0b11        # 10dBm — минимальная мощность в линейке E22-900T22U
CHANNEL = 19
BASE_FREQ_MHZ = 850.125


def auto_int(x: str) -> int:
    """Разбор адреса/NetID в hex ('0x12AB') или decimal ('4779')."""
    return int(x, 0)


def read_config(ser: serial.Serial) -> bytes:
    ser.reset_input_buffer()
    ser.write(bytes([0xC1, 0x00, 0x07]))
    ser.flush()
    time.sleep(0.05)
    return ser.read(10)


def write_config(ser: serial.Serial, frame: bytes, save: bool) -> bytes:
    ser.reset_input_buffer()
    ser.write(frame)
    ser.flush()
    time.sleep(0.25 if save else 0.05)  # запись во флеш (0xC0) требует больше времени
    return ser.read(10)


def describe(resp: bytes) -> str:
    if len(resp) != 10:
        return f'НЕПОЛНЫЙ ОТВЕТ ({len(resp)} байт): {resp.hex()}'
    addr = (resp[3] << 8) | resp[4]
    netid = resp[5]
    reg1 = resp[7]
    channel = resp[8]
    reg3 = resp[9]

    txpower_map = {0b00: '22dBm', 0b01: '17dBm', 0b10: '13dBm', 0b11: '10dBm'}
    txpower = txpower_map[reg1 & 0b11]

    rssi_en = bool(reg3 & 0b10000000)
    fixed = bool(reg3 & 0b01000000)
    lbt = bool(reg3 & 0b00010000)

    freq = BASE_FREQ_MHZ + channel

    lines = [
        f'  Адрес:            0x{addr:04X}',
        f'  NetID:            0x{netid:02X}',
        f'  Канал:            {channel} (~{freq:.3f} МГц)',
        f'  TX power:         {txpower}',
        f'  Режим передачи:   {"fixed" if fixed else "transparent"}',
        f'  LBT:              {"вкл" if lbt else "выкл"}',
        f'  RSSI байт:        {"вкл" if rssi_en else "выкл"}',
        f'  Сырой ответ:      {resp.hex(" ")}',
    ]
    return '\n'.join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', required=True, help='COM-порт (Windows) или /dev/ttyUSB* (Linux)')
    ap.add_argument('--baud', type=int, default=9600, help='baud rate конфигурационного канала')
    ap.add_argument('--address', required=True, type=auto_int,
                     help='адрес модуля, 0..65535, hex ("0x0001") или decimal ("1")')
    ap.add_argument('--netid', type=auto_int, default=None,
                     help='NetID (0..255), если не указан — не изменяется')
    ap.add_argument('--temporary', action='store_true',
                     help='записать во временную память (0xC2) вместо постоянной (0xC0 по умолчанию)')
    args = ap.parse_args()

    if not (0 <= args.address <= 0xFFFF):
        sys.exit('Адрес должен быть в диапазоне 0..65535 (0x0000..0xFFFF).')
    if args.netid is not None and not (0 <= args.netid <= 0xFF):
        sys.exit('NetID должен быть в диапазоне 0..255.')

    try:
        ser = serial.Serial(args.port, args.baud, timeout=1)
    except serial.SerialException as e:
        sys.exit(f'Не удалось открыть {args.port}: {e}')

    with ser:
        print(f'Читаю текущую конфигурацию с {args.port}...')
        cur = read_config(ser)
        if len(cur) != 10 or cur[0] != 0xC1:
            sys.exit(
                'Не удалось прочитать текущую конфигурацию.\n'
                'Убедитесь, что модуль в режиме конфигурации '
                '(зажать боковую кнопку 2 сек до постоянного красного светодиода).'
            )
        print('Текущая конфигурация:')
        print(describe(cur))

        addh, addl = (args.address >> 8) & 0xFF, args.address & 0xFF
        netid = args.netid if args.netid is not None else cur[5]
        reg0 = cur[6]  # baud/parity/air rate — не трогаем
        reg1 = (cur[7] & 0b11111100) | MIN_TXPOWER_BITS  # только биты TX power
        channel = CHANNEL
        reg3 = cur[9]
        reg3 |= 0b10000000  # RSSI byte enable
        reg3 |= 0b01000000  # fixed transmission mode
        reg3 |= 0b00010000  # LBT enable

        frame = bytes([0xC0 if not args.temporary else 0xC2,
                        0x00, 0x07,
                        addh, addl, netid,
                        reg0, reg1, channel, reg3])

        print(f'\nЗаписываю новую конфигурацию ({"временно" if args.temporary else "постоянно, во флеш"})...')
        resp = write_config(ser, frame, save=not args.temporary)
        if len(resp) != 10 or resp[0] not in (0xC0, 0xC2):
            sys.exit(f'Модуль не подтвердил запись. Ответ: {resp.hex() if resp else "<пусто>"}')

        print('Подтверждённая конфигурация:')
        print(describe(resp))
        print('\nГотово.')


if __name__ == '__main__':
    main()
