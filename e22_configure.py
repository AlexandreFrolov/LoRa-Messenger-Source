#!/usr/bin/env python3
"""
e22_configure.py — запись конфигурации в энергонезависимую память (EEPROM/Flash)
модуля EBYTE E22-900T22U:
    - минимально возможная мощность передачи (10dBm)
    - канал 19 (частота = 850.125 + 19 = 869.125 МГц)
    - LBT (Listen Before Talk) включён
    - fixed-режим передачи включён (нужен для адресации)
    - RSSI байт добавляется к каждому принятому пакету
    - адрес модуля — задаётся параметром

Изменения, записанные этим скриптом, СОХРАНЯЮТСЯ после отключения питания / USB.

ПЕРЕД ЗАПУСКОМ:
Переведите модуль в режим конфигурации (зажмите боковую кнопку на 2 секунды,
пока светодиод не загорится ПОСТОЯННЫМ красным цветом, либо убедитесь, что M0=1, M1=1).

Использование:
    pip install pyserial

    python e22_configure.py --port COM5 --address 0x0001
    python e22_configure.py --port /dev/ttyUSB0 --address 1 --debug
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
    """Чтение текущих 7 регистров конфигурации (команда 0xC1 0x00 0x07)."""
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    
    # Запрос: [0xC1, Start_Addr=0x00, Length=0x07]
    ser.write(bytes([0xC1, 0x00, 0x07]))
    ser.flush()
    time.sleep(0.1)
    
    return ser.read(10)


def write_config_eeprom(ser: serial.Serial, frame: bytes, wait_s: float = 1.5, debug: bool = False) -> bytes:
    """
    Запись конфигурации в Flash/EEPROM (команда 0xC0).
    Возвращает подтверждающий ответ модуля.
    """
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    
    if debug:
        print(f'  -> отправляю в модуль: {frame.hex(" ")}')
        
    ser.write(frame)
    ser.flush()

    # Памяти Flash требуется время на физическую запись байт (~100-150 мс)
    time.sleep(0.15)

    # Опрашиваем порт на наличие ответа
    deadline = time.time() + wait_s
    buf = bytearray()
    while time.time() < deadline and len(buf) < 10:
        n = ser.in_waiting
        if n:
            buf.extend(ser.read(n))
        else:
            time.sleep(0.02)

    if debug:
        print(f'  <- получено ответных байт: {bytes(buf).hex(" ") if buf else "<ничего>"}')
        
    return bytes(buf)


def configs_match(resp: bytes, addh: int, addl: int, netid: int,
                  reg0: int, reg1: int, channel: int, reg3: int) -> bool:
    """Сравнивает 10-байтный ответ с ожидаемыми значениями регистров."""
    if len(resp) != 10:
        return False
    # Проверяем полезные данные (байты с index 3 по 9)
    return (resp[3], resp[4], resp[5], resp[6], resp[7], resp[8], resp[9]) == \
           (addh, addl, netid, reg0, reg1, channel, reg3)


def describe(resp: bytes) -> str:
    """Декодирует 10-байтный массив ответа в человекочитаемый вид."""
    if len(resp) != 10:
        return f'НЕПОЛНЫЙ ИЛИ НЕКОРРЕКТНЫЙ ОТВЕТ ({len(resp)} байт): {resp.hex()}'
    
    addr = (resp[3] << 8) | resp[4]
    netid = resp[5]
    reg1 = resp[7]
    channel = resp[8]
    reg3 = resp[9]

    txpower_map = {0b00: '22dBm', 0b01: '17dBm', 0b10: '13dBm', 0b11: '10dBm'}
    txpower = txpower_map.get(reg1 & 0b11, 'неизвестно')

    rssi_en = bool(reg3 & 0b10000000)
    fixed = bool(reg3 & 0b01000000)
    lbt = bool(reg3 & 0b00010000)

    freq = BASE_FREQ_MHZ + channel

    lines = [
        f'  Адрес:             0x{addr:04X} ({addr})',
        f'  NetID:             0x{netid:02X}',
        f'  Канал:             {channel} (~{freq:.3f} МГц)',
        f'  TX power:          {txpower}',
        f'  Режим передачи:    {"fixed" if fixed else "transparent"}',
        f'  LBT:               {"вкл" if lbt else "выкл"}',
        f'  RSSI байт:         {"вкл" if rssi_en else "выкл"}',
        f'  Сырой ответ:       {resp.hex(" ")}',
    ]
    return '\n'.join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', required=True, help='COM-порт (например COM5 или /dev/ttyUSB0)')
    ap.add_argument('--baud', type=int, default=9600, help='Baudrate конфигурационного канала (по умолчанию 9600)')
    ap.add_argument('--address', required=True, type=auto_int,
                    help='Адрес модуля: 0..65535, hex ("0x0001") или decimal ("1")')
    ap.add_argument('--netid', type=auto_int, default=None,
                    help='NetID (0..255). Если не указан — сохраняется текущий')
    ap.add_argument('--wait', type=float, default=2.0,
                    help='Время ожидания ответа от модуля в секундах (по умолчанию 2.0)')
    ap.add_argument('--debug', action='store_true',
                    help='Выводить отладочную информацию о сырых байтах')
    args = ap.parse_args()

    if not (0 <= args.address <= 0xFFFF):
        sys.exit('Ошибка: Адрес должен быть в диапазоне 0..65535 (0x0000..0xFFFF).')
    if args.netid is not None and not (0 <= args.netid <= 0xFF):
        sys.exit('Ошибка: NetID должен быть в диапазоне 0..255.')

    try:
        # Важно: При открытии порта принудительно выставляем DTR и RTS в True.
        # В USB-донглах EBYTE они привязаны к пинам M0/M1. High (1,1) переводит модуль в Config Mode.
        ser = serial.Serial(args.port, args.baud, timeout=1)
        ser.dtr = True
        ser.rts = True
        time.sleep(0.1)  # Даем время на стабилизацию аппаратных линий
    except serial.SerialException as e:
        sys.exit(f'Не удалось открыть порт {args.port}: {e}')

    with ser:
        print(f'Читаю текущую конфигурацию с {args.port}...')
        cur = read_config(ser)
        
        if len(cur) != 10 or cur[0] not in (0xC1, 0xC0, 0xC2):
            sys.exit(
                'Ошибка: Не удалось прочитать конфигурацию.\n'
                'Убедитесь, что модуль переведён в режим конфигурации:\n'
                '  - зажмите боковую кнопку на 2 сек (светодиод горит ПОСТОЯННЫМ красным)\n'
                '  - или проверьте правильность вывода COM-порта.'
            )

        print('Текущая конфигурация модуля:')
        print(describe(cur))

        # Формируем новые значения регистров
        addh, addl = (args.address >> 8) & 0xFF, args.address & 0xFF
        netid = args.netid if args.netid is not None else cur[5]
        reg0 = cur[6]  # Baudrate / AirDataRate — оставляем без изменений
        reg1 = (cur[7] & 0b11111100) | MIN_TXPOWER_BITS  # Устанавливаем минимальную мощность (10dBm)
        channel = CHANNEL
        
        # Настройка REG3 (биты функционала)
        reg3 = cur[9]
        reg3 |= 0b10000000  # RSSI byte enable
        reg3 |= 0b01000000  # Fixed transmission mode
        reg3 |= 0b00010000  # LBT enable

        # Формируем кадр сохранения во FLASH: 
        # [0xC0 (Save Flash), 0x00 (Start Reg), 0x07 (Length), Payload (7 байт)]
        frame = bytes([0xC0, 0x00, 0x07, addh, addl, netid, reg0, reg1, channel, reg3])

        print(f'\nЗаписываю новую конфигурацию во Flash-память (0xC0)...')
        resp = write_config_eeprom(ser, frame, wait_s=args.wait, debug=args.debug)

        # Проверяем успешность
        ok = len(resp) == 10 and resp[0] in (0xC0, 0xC1) and \
             configs_match(resp, addh, addl, netid, reg0, reg1, channel, reg3)

        if not ok:
            # Если прямой ответ задерживается, пробуем перечитать
            print('Прямое подтверждение не получено. Выполняю контрольное чтение...')
            time.sleep(0.2)
            verify = read_config(ser)
            ok = configs_match(verify, addh, addl, netid, reg0, reg1, channel, reg3)
            if ok:
                resp = verify

        if not ok:
            print(f'\n[ОШИБКА] Не удалось сохранить параметры.')
            print(f'Полученный ответ: {resp.hex(" ") if resp else "<нет ответа>"}')
            print('\nПроверьте:')
            print('  1. Красный светодиод горит постоянно (режим CONFIG).')
            print('  2. Порт не заблокирован другими программами.')
            sys.exit(1)

        print('\n' + '='*50)
        print('ПОДТВЕРЖДЕННАЯ И СОХРАНЕННАЯ КОНФИГУРАЦИЯ:')
        print('='*50)
        print(describe(resp))
        print('='*50)
        print('\nУспешно! Изменения сохранены во Flash-памяти модуля.')
        print('Параметры сохранятся при отключении питания и переподключении USB.')


if __name__ == '__main__':
    main()