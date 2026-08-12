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


def write_config(ser: serial.Serial, frame: bytes, save: bool,
                  wait_s: float = 1.5, debug: bool = False) -> bytes:
    ser.reset_input_buffer()
    if debug:
        print(f'  -> отправляю: {frame.hex(" ")}')
    ser.write(frame)
    ser.flush()

    # Опрашиваем порт вместо одного фиксированного sleep+read — так виден
    # любой частичный/запоздавший ответ вместо полной тишины.
    deadline = time.time() + wait_s
    buf = bytearray()
    while time.time() < deadline and len(buf) < 10:
        n = ser.in_waiting
        if n:
            buf.extend(ser.read(n))
        else:
            time.sleep(0.02)
    if debug:
        print(f'  <- получено за {wait_s:.1f} с: {bytes(buf).hex(" ") if buf else "<ничего>"}')
    return bytes(buf)


def configs_match(resp: bytes, addh: int, addl: int, netid: int,
                   reg0: int, reg1: int, channel: int, reg3: int) -> bool:
    """Сравнивает 10-байтный ответ (0xC1 ...) с ожидаемыми значениями регистров."""
    if len(resp) != 10:
        return False
    return (resp[3], resp[4], resp[5], resp[6], resp[7], resp[8], resp[9]) == \
           (addh, addl, netid, reg0, reg1, channel, reg3)


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
    ap.add_argument('--wait', type=float, default=1.5,
                     help='сколько секунд ждать ответ модуля на команду записи (по умолчанию 1.5)')
    ap.add_argument('--debug', action='store_true',
                     help='печатать отправленные и полученные сырые байты')
    ap.add_argument('--try-both', action='store_true',
                     help='если постоянная запись (0xC0) не отвечает — автоматически '
                          'попробовать временную (0xC2) для диагностики')
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

        cmd_byte = 0xC2 if args.temporary else 0xC0
        frame = bytes([cmd_byte, 0x00, 0x07, addh, addl, netid, reg0, reg1, channel, reg3])

        label = 'временно (0xC2)' if args.temporary else 'постоянно, во флеш (0xC0)'
        print(f'\nЗаписываю новую конфигурацию: {label}, жду ответ до {args.wait} с...')
        resp = write_config(ser, frame, save=not args.temporary, wait_s=args.wait, debug=args.debug)

        # Подтверждением может быть 0xC0, 0xC1 или 0xC2 — на практике этот донгл
        # отвечает заголовком 0xC1 независимо от того, какую команду записи послали.
        ok = len(resp) == 10 and resp[0] in (0xC0, 0xC1, 0xC2) and \
             configs_match(resp, addh, addl, netid, reg0, reg1, channel, reg3)

        if not ok and args.try_both and cmd_byte == 0xC0:
            print('\nПостоянная запись (0xC0) не подтвердилась напрямую. Пробую временную (0xC2)...')
            frame2 = bytes([0xC2]) + frame[1:]
            resp = write_config(ser, frame2, save=False, wait_s=args.wait, debug=args.debug)
            ok = len(resp) == 10 and resp[0] in (0xC0, 0xC1, 0xC2) and \
                 configs_match(resp, addh, addl, netid, reg0, reg1, channel, reg3)
            if ok:
                print('\n0xC2 сработала. Похоже, эта прошивка не подтверждает 0xC0 напрямую '
                      '(или не поддерживает постоянное сохранение) — обычно проще и надёжнее '
                      'работать через 0xC2 (--temporary) и перезаписывать конфигурацию при '
                      'каждом включении модуля, либо один раз сохранить через официальный GUI.')

        if not ok:
            # Ответа не было или он не совпал с ожидаемым — перепроверяем явным чтением:
            # возможно, запись реально применилась, просто без подтверждения на этот запрос.
            print('\nПрямого подтверждения нет, перечитываю конфигурацию для проверки...')
            time.sleep(0.3)
            verify = read_config(ser)
            ok = configs_match(verify, addh, addl, netid, reg0, reg1, channel, reg3)
            if ok:
                resp = verify
                print('Конфигурация на самом деле применилась (подтверждено повторным чтением).')

        if not ok:
            print(f'\nЗапись не подтвердилась. Получено: {resp.hex(" ") if resp else "<пусто>"}')
            print('Проверьте:')
            print('  - светодиод всё ещё горит ПОСТОЯННЫМ красным (не мигает, не погас);')
            print('  - порт не был переоткрыт/занят другим процессом между запусками;')
            print('  - попробуйте --temporary (запись 0xC2 вместо 0xC0);')
            print('  - попробуйте заново зажать боковую кнопку на 2 сек перед записью;')
            print('  - увеличьте --wait до 3-5 секунд.')
            sys.exit(1)

        print('\nПодтверждённая конфигурация:')
        print(describe(resp))
        print('\nГотово.')


if __name__ == '__main__':
    main()
