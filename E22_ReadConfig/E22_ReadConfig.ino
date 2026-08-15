/*
 * Чтение текущей конфигурации модуля EBYTE E22-900T22D
 * Плата: ESP32-S3-N16R8
 *
 * Подключение:
 *   E22-900T22D      ESP32-S3
 *   VCC       ->     3V3 (модуль потребляет пиковый ток до ~120 мА на TX,
 *                     при просадке питания рекомендуется отдельный LDO
 *                     на 3.3В с конденсатором 100-470 мкФ у самого модуля)
 *   GND       ->     GND
 *   TXD       ->     GPIO17 (RX для ESP32)
 *   RXD       ->     GPIO18 (TX для ESP32)
 *   AUX       ->     GPIO16
 *   M0        ->     GPIO5
 *   M1        ->     GPIO6
 *
 * Библиотека: "LoRa_E22" (Renzo Mischianti, EByte_LoRa_E22)
 * Установка: Arduino IDE -> Библиотеки -> поиск "LoRa_E22"
 *
 * ВАЖНО для этой платы: Tools -> USB CDC On Boot -> Disabled.
 * На этой конкретной ESP32-S3-N16R8 плате Serial физически работает
 * через UART0 (тот же порт, что и ROM-лог при загрузке), а не через
 * native USB CDC. При Enabled весь вывод Serial.print() пропадает
 * без каких-либо ошибок компиляции или явных зависаний.
 */

#include "LoRa_E22.h"

// Пины управления модулем
#define PIN_AUX   16
#define PIN_M0    5
#define PIN_M1    6

// UART для связи с модулем (аппаратный Serial1 ESP32-S3)
#define PIN_RXD1  17   // ESP32 RX <- E22 TXD
#define PIN_TXD1  18   // ESP32 TX -> E22 RXD

LoRa_E22 e22(&Serial1, PIN_AUX, PIN_M0, PIN_M1, UART_BPS_RATE_9600);

void printConfiguration(struct Configuration configuration);
float getChannelFrequency900(byte chan);

void setup() {
  Serial.begin(115200);
  delay(500);

  Serial.println();
  Serial.println(F("=== Чтение конфигурации E22-900T22D ==="));

  Serial1.begin(9600, SERIAL_8N1, PIN_RXD1, PIN_TXD1);

  // Перевод модуля в режим конфигурации (M1=1, M0=1 -> Mode 3, Sleep/Config)
  e22.begin();

  delay(500); // модулю нужно время после смены режима

  ResponseStructContainer c;
  c = e22.getConfiguration();

  if (c.status.code != 1) {
    Serial.print(F("Ошибка чтения конфигурации: "));
    Serial.println(c.status.getResponseDescription());
    c.close();
    return;
  }

  Configuration configuration = *(Configuration*) c.data;
  printConfiguration(configuration);

  c.close();

  // По желанию: вернуть модуль в нормальный режим работы (M1=0, M0=0)
  // e22.setMode(MODE_0_NORMAL);
}

void loop() {
  // ничего не делаем, конфигурация читается один раз в setup()
}

void printConfiguration(struct Configuration configuration) {
  Serial.println(F("----------------------------------------"));
  Serial.print(F("HEAD (bin): "));
  Serial.print(configuration.COMMAND, BIN);
  Serial.print(F(" "));
  Serial.print(configuration.STARTING_ADDRESS, BIN);
  Serial.print(F(" "));
  Serial.println(configuration.LENGHT, BIN);

  Serial.print(F("Адрес модуля (ADDH+ADDL): "));
  Serial.print(configuration.ADDH, HEX);
  Serial.print(configuration.ADDL, HEX);
  Serial.println();

  Serial.print(F("NET ID: "));
  Serial.println(configuration.NETID, HEX);

  Serial.println(F("--- SPEED ---"));
  Serial.print(F("Air data rate: "));
  Serial.print(configuration.SPED.airDataRate, DEC);
  Serial.print(F(" -> "));
  Serial.println(configuration.SPED.getAirDataRateDescription());
  Serial.print(F("UART parity: "));
  Serial.print(configuration.SPED.uartParity, DEC);
  Serial.print(F(" -> "));
  Serial.println(configuration.SPED.getUARTParityDescription());
  Serial.print(F("UART baud rate: "));
  Serial.print(configuration.SPED.uartBaudRate, DEC);
  Serial.print(F(" -> "));
  Serial.println(configuration.SPED.getUARTBaudRateDescription());

  Serial.println(F("--- OPTION ---"));
  Serial.print(F("Transmission power: "));
  Serial.print(configuration.OPTION.transmissionPower, DEC);
  Serial.print(F(" -> "));
  Serial.println(configuration.OPTION.getTransmissionPowerDescription());
  Serial.print(F("RSSI ambient noise enable: "));
  Serial.print(configuration.OPTION.RSSIAmbientNoise, DEC);
  Serial.print(F(" -> "));
  Serial.println(configuration.OPTION.getRSSIAmbientNoiseEnable());
  Serial.print(F("Subpacket setting: "));
  Serial.print(configuration.OPTION.subPacketSetting, DEC);
  Serial.print(F(" -> "));
  Serial.println(configuration.OPTION.getSubPacketSetting());

  Serial.print(F("Канал (CHAN): "));
  Serial.print(configuration.CHAN, DEC);
  Serial.print(F(" -> "));
  Serial.print(getChannelFrequency900(configuration.CHAN), 3);
  Serial.println(F(" МГц"));

  Serial.println(F("--- TRANSMISSION_MODE ---"));
  Serial.print(F("WOR period: "));
  Serial.print(configuration.TRANSMISSION_MODE.WORPeriod, DEC);
  Serial.print(F(" -> "));
  Serial.println(configuration.TRANSMISSION_MODE.getWORPeriodByParamsDescription());
  Serial.print(F("WOR transceiver control: "));
  Serial.print(configuration.TRANSMISSION_MODE.WORTransceiverControl, DEC);
  Serial.print(F(" -> "));
  Serial.println(configuration.TRANSMISSION_MODE.getWORTransceiverControlDescription());
  Serial.print(F("Enable LBT: "));
  Serial.print(configuration.TRANSMISSION_MODE.enableLBT, DEC);
  Serial.print(F(" -> "));
  Serial.println(configuration.TRANSMISSION_MODE.getLBTEnableByteDescription());
  Serial.print(F("Enable repeater: "));
  Serial.print(configuration.TRANSMISSION_MODE.enableRepeater, DEC);
  Serial.print(F(" -> "));
  Serial.println(configuration.TRANSMISSION_MODE.getRepeaterModeEnableByteDescription());
  Serial.print(F("Fixed transmission (адресный режим): "));
  Serial.print(configuration.TRANSMISSION_MODE.fixedTransmission, DEC);
  Serial.print(F(" -> "));
  Serial.println(configuration.TRANSMISSION_MODE.getFixedTransmissionDescription());
  Serial.print(F("Enable RSSI: "));
  Serial.print(configuration.TRANSMISSION_MODE.enableRSSI, DEC);
  Serial.print(F(" -> "));
  Serial.println(configuration.TRANSMISSION_MODE.getRSSIEnableByteDescription());

  Serial.print(F("Crypt (KEY): "));
  Serial.print(configuration.CRYPT.CRYPT_H, HEX);
  Serial.println(configuration.CRYPT.CRYPT_L, HEX);
  Serial.println(F("----------------------------------------"));
}

// Расчёт рабочей частоты канала для модулей серии E22-900T (SX1262, 900 МГц).
// Библиотечный configuration.getChannelDescription() использует базовую
// частоту 410.125 МГц, что верно для 400-й серии (E22-400T), но НЕ для
// 900-й — там правильная база 850.125 МГц. Для E22-900T22D номер канала 19
// соответствует 869.125 МГц (внутри российской подполосы ГКРЧ 868.7-869.2).
float getChannelFrequency900(byte chan) {
  const float BASE_FREQUENCY_900_MHZ = 850.125;
  return BASE_FREQUENCY_900_MHZ + chan;
}
