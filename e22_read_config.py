#!/usr/bin/env python3
"""
e22_read_config.py — чтение текущей конфигурации EBYTE E22-900T22U.

ПЕРЕД ЗАПУСКОМ: переведите модуль в режим конфигурации — зажмите боковую
кнопку на 2 секунды, пока светодиод не загорится ПОСТОЯННЫМ красным.
В обычном (transmission) режиме модуль не отвечает на эти команды, а
ретранслирует любые присланные байты в эфир.

Использование:
    pip install pyserial
    python e22_read_config.py --port COM5           # Windows
    python e22_read_config.py --port /dev/ttyUSB0    # Linux

Протокол (общий для серии E22, регистры описаны в User Manual, стр. 13-16):
    команда чтения конфигурации: C1 00 07
    ответ модуля, 10 байт:
        [0]    эхо команды (0xC1)
        [1]    начальный адрес регистра (0x00)
        [2]    длина блока (0x07)
        [3:5]  ADDH, ADDL — адрес модуля
        [5]    NETID
        [6]    REG0 — baud rate(3б) | parity(2б) | air data rate(3б)
        [7]    REG1 — sub-packet(2б) | amb.noise en.(1б) | resv(3б) | tx power(2б)
        [8]    REG2 — канал (частота = 850.125 + канал, МГц, для 900 MHz модели)
        [9]    REG3 — rssi en.(1б) | fixed mode(1б) | repeater(1б) | lbt(1б) |
                       wor ctrl(1б) | wor cycle(3б)
"""

import argparse
import sys

import serial

BAUDRATE = {'000': 1200, '001': 2400, '010': 4800, '011': 9600,
            '100': 19200, '101': 38400, '110': 57600, '111': 115200}
PARITY = {'00': '8N1', '01': '8O1', '10': '8E1', '11': '8N1(resv)'}
AIR_RATE = {'000': '0.3k', '001': '1.2k', '010': '2.4k', '011': '4.8k',
            '100': '9.6k', '101': '19.2k', '110': '38.4k', '111': '62.5k'}
SUBPACKET = {'00': '240B', '01': '128B', '10': '64B', '11': '32B'}
TXPOWER = {'00': '22dBm', '01': '17dBm', '10': '13dBm', '11': '10dBm'}
WOR_CYCLE_MS = {'000': 500, '001': 1000, '010': 1500, '011': 2000,
                '100': 2500, '101': 3000, '110': 3500, '111': 4000}

BASE_FREQ_MHZ = 850.125  # для 900 MHz модели (850.125 + channel)


def bits(byte: int) -> str:
    return f'{byte:08b}'


def decode_config(resp: bytes) -> dict:
    if len(resp) != 10:
        raise ValueError(f'ожидалось 10 байт ответа, получено {len(resp)}: {resp.hex()}')
    if resp[0] != 0xC1:
        raise ValueError(
            f'неожиданный первый байт 0x{resp[0]:02X} (ожидался 0xC1). '
            'Похоже, модуль не в режиме конфигурации — зажмите боковую '
            'кнопку на 2 сек до постоянного красного светодиода.'
        )

    addr = (resp[3] << 8) | resp[4]
    netid = resp[5]

    reg0 = bits(resp[6])
    baud = BAUDRATE[reg0[0:3]]
    parity = PARITY[reg0[3:5]]
    air_rate = AIR_RATE[reg0[5:8]]

    reg1 = bits(resp[7])
    subpacket = SUBPACKET[reg1[0:2]]
    amb_noise = bool(int(reg1[2]))
    txpower = TXPOWER[reg1[6:8]]

    channel = resp[8]
    freq = BASE_FREQ_MHZ + channel

    reg3 = bits(resp[9])
    rssi_enabled = bool(int(reg3[0]))
    fixed_mode = bool(int(reg3[1]))
    repeater = bool(int(reg3[2]))
    lbt = bool(int(reg3[3]))
    wor_transmitter = bool(int(reg3[4]))
    wor_cycle_ms = WOR_CYCLE_MS[reg3[5:8]]

    return {
        'address': f'0x{addr:04X}',
        'netid': f'0x{netid:02X}',
        'serial_baudrate': baud,
        'serial_parity': parity,
        'air_data_rate': air_rate,
        'sub_packet_size': subpacket,
        'ambient_noise_rssi': amb_noise,
        'tx_power': txpower,
        'channel': channel,
        'frequency_mhz': round(freq, 3),
        'rssi_byte_enabled': rssi_enabled,
        'transmission_mode': 'fixed' if fixed_mode else 'transparent',
        'repeater_mode': repeater,
        'lbt_enabled': lbt,
        'wor_role': 'transmitter' if wor_transmitter else 'receiver',
        'wor_cycle_ms': wor_cycle_ms,
        'raw_hex': resp.hex(' '),
    }


def read_config(port: str, baud: int, timeout: float) -> bytes:
    with serial.Serial(port, baud, timeout=timeout) as ser:
        ser.reset_input_buffer()
        ser.write(bytes([0xC1, 0x00, 0x07]))
        ser.flush()
        resp = ser.read(10)
    return resp


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', required=True, help='COM-порт (Windows) или /dev/ttyUSB* (Linux)')
    ap.add_argument('--baud', type=int, default=9600,
                     help='serial baud rate, которым сейчас настроен модуль (по умолчанию 9600)')
    ap.add_argument('--timeout', type=float, default=0.5, help='таймаут чтения ответа, сек')
    args = ap.parse_args()

    print(f'Открываю {args.port} @ {args.baud} baud...')
    try:
        resp = read_config(args.port, args.baud, args.timeout)
    except serial.SerialException as e:
        print(f'Не удалось открыть порт: {e}')
        sys.exit(1)

    if not resp:
        print('Нет ответа от модуля.')
        print('Проверьте: модуль в режиме конфигурации (постоянный красный светодиод),')
        print('порт не занят другой программой, baud rate указан верно.')
        sys.exit(1)

    try:
        cfg = decode_config(resp)
    except ValueError as e:
        print(f'Ошибка разбора ответа: {e}')
        sys.exit(1)

    print('\n=================== КОНФИГУРАЦИЯ E22-900T22U ===================')
    print(f"Адрес (ADDH:ADDL)     {cfg['address']}")
    print(f"NetID                 {cfg['netid']}")
    print(f"Serial baud rate      {cfg['serial_baudrate']} bps")
    print(f"Serial parity         {cfg['serial_parity']}")
    print(f"Air data rate         {cfg['air_data_rate']}")
    print(f"Sub-packet size       {cfg['sub_packet_size']}")
    print(f"Ambient noise RSSI    {'вкл' if cfg['ambient_noise_rssi'] else 'выкл'}")
    print(f"TX power              {cfg['tx_power']}")
    print(f"Канал                 {cfg['channel']} (частота ≈ {cfg['frequency_mhz']} МГц)")
    print(f"RSSI байт в пакете    {'вкл' if cfg['rssi_byte_enabled'] else 'выкл'}")
    print(f"Режим передачи        {cfg['transmission_mode']}")
    print(f"Repeater              {'вкл' if cfg['repeater_mode'] else 'выкл'}")
    print(f"LBT                   {'вкл' if cfg['lbt_enabled'] else 'выкл'}")
    print(f"WOR роль              {cfg['wor_role']}")
    print(f"WOR cycle             {cfg['wor_cycle_ms']} мс")
    print(f"Сырой ответ (hex)     {cfg['raw_hex']}")
    print('==================================================================')


if __name__ == '__main__':
    main()
