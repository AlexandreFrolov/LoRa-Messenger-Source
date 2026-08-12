#!/usr/bin/env python3
"""
e22_start_session.py — одна команда на весь цикл "включил донгл -> готов к работе".

Поскольку на этом донгле конфигурация НЕ переживает отключение USB
(проверено эмпирически: 0xC0 не отвечает, 0xC2 отвечает, но volatile),
конфигурировать модуль приходится заново при каждом включении. Этот
скрипт совмещает два шага в один:

    1. запускает e22_configure.py (адрес, канал 19, минимальная мощность,
       LBT, fixed-режим, RSSI byte);
    2. если конфигурация прошла успешно — сразу запускает
       e22_transceiver.py с адресом собеседника и включённым --rssi.

ПЕРЕД ЗАПУСКОМ: зажмите боковую кнопку модуля на 2 сек до постоянного
красного светодиода — так же, как для обычного e22_configure.py.
После того как конфигурация применится, модуль сам продолжит работать
в рабочем режиме (config-mode сессия не мешает transceiver'у).

Использование:
    python e22_start_session.py --port COM5 --address 0x0001 --peer-address 0x0002
    python e22_start_session.py --port COM7 --address 0x0002 --peer-address 0x0001

Любые дополнительные флаги после "--" передаются напрямую в
e22_transceiver.py, например:
    python e22_start_session.py --port COM5 --address 1 --peer-address 2 -- --send "hi"
"""

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def auto_int(x: str) -> int:
    return int(x, 0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', required=True, help='COM-порт (Windows) или /dev/ttyUSB* (Linux)')
    ap.add_argument('--baud', type=int, default=9600)
    ap.add_argument('--address', required=True, type=auto_int, help='адрес этого модуля')
    ap.add_argument('--peer-address', required=True, type=auto_int, help='адрес собеседника')
    ap.add_argument('--channel', type=int, default=19)
    ap.add_argument('--wait', type=float, default=3.0, help='таймаут ответа при конфигурации')
    ap.add_argument('extra', nargs=argparse.REMAINDER,
                     help='доп. аргументы для e22_transceiver.py после "--"')
    args = ap.parse_args()

    extra = args.extra
    if extra and extra[0] == '--':
        extra = extra[1:]

    print('=== Шаг 1/2: конфигурирую модуль ===')
    cfg_cmd = [
        sys.executable, str(HERE / 'e22_configure.py'),
        '--port', args.port,
        '--baud', str(args.baud),
        '--address', hex(args.address),
        '--wait', str(args.wait),
    ]
    result = subprocess.run(cfg_cmd)
    if result.returncode != 0:
        print('\nКонфигурация не прошла — приём/передачу не запускаю.')
        print('Проверьте, что модуль в режиме конфигурации (постоянный красный светодиод), '
              'и попробуйте снова.')
        sys.exit(result.returncode)

    print('\n=== Шаг 2/2: запускаю приём/передачу ===')
    print('(Если светодиод ещё красный — можно коротко нажать кнопку, '
          'чтобы выйти из режима конфигурации; на части донглов это не требуется.)\n')
    tx_cmd = [
        sys.executable, str(HERE / 'e22_transceiver.py'),
        '--port', args.port,
        '--baud', str(args.baud),
        '--peer-address', hex(args.peer_address),
        '--channel', str(args.channel),
        '--rssi',
        *extra,
    ]
    subprocess.run(tx_cmd)


if __name__ == '__main__':
    main()
