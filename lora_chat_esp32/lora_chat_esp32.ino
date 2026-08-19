/* ============================================================================
 * LoRa-2026 Chat — автономный чат на ESP32-S3-N16R8 + Ebyte E22-900T22D
 *
 * Плата: ESP32-S3-N16R8 (16 МБ Flash, 8 МБ PSRAM)
 * ВАЖНО: Tools -> USB CDC On Boot -> Disabled
 *        Tools -> PSRAM -> OPI PSRAM
 *        Tools -> Partition Scheme -> см. partitions.csv в этом проекте
 *
 * Подключение E22 (как в e22_soft_repeater.ino):
 *   TXD -> GPIO17, RXD -> GPIO18, AUX -> GPIO16, M0 -> GPIO5, M1 -> GPIO6
 *   VCC -> 3V3 (отдельный LDO + конденсатор 100-470 мкФ), GND -> GND
 *
 * ==========================================================================*/

#include <WiFi.h>
#include <DNSServer.h>
#include <LittleFS.h>
#include <ESPAsyncWebServer.h>
#include <ArduinoJson.h>
#include <Adafruit_NeoPixel.h>
#include "LoRa_E22.h"

// ======================= НАСТРОЙКИ УЗЛА (менять на каждой плате) ===========
//#define NODE_ADDL      0x07     // уникальный адрес узла в сети (0x01, 0x02, 0x03 ...)
#define NODE_ADDL      0x08     // уникальный адрес узла в сети (0x01, 0x02, 0x03 ...)
//#define NODE_NICK      "node1"  // ник по умолчанию для этого узла (можно менять из веб-UI)
#define NODE_NICK      "node8"  // ник по умолчанию для этого узла (можно менять из веб-UI)
#define FLOOD_TTL      3        // сколько раз пакет может быть переретранслирован

// ======================= Wi-Fi точка доступа ================================
//#define AP_SSID        "LoRa-2026"
#define AP_SSID        "LoRa-2026-8"
#define AP_PASSWORD    "lora2026pass"     // мин. 8 символов
//IPAddress AP_IP(192, 168, 4, 1);
IPAddress AP_IP(192, 168, 2, 2);
IPAddress AP_MASK(255, 255, 255, 0);

// ======================= Пины E22 (ESP32-S3-N16R8) ==========================
#define PIN_AUX   16
#define PIN_M0    5
#define PIN_M1    6
#define PIN_RXD1  17
#define PIN_TXD1  18

// ======================= Встроенный RGB (WS2812, GPIO48) ====================
#define LED_PIN    48
#define LED_COUNT  1
Adafruit_NeoPixel pixel(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);
volatile uint32_t ledOffAt = 0;
volatile bool ledOn = false;

// ======================= Радио параметры сети ================================
#define LORA_CHANNEL   19       // 850.125 + 19*1 = 869.125 МГц (868.7-869.2, LBT обязателен)
#define NET_ID         0x00
#define BROADCAST_ADDH 0xFF
#define BROADCAST_ADDL 0xFF     // широковещательный адрес модуля E22 (см. datasheet EBYTE)

LoRa_E22 e22(&Serial1, PIN_AUX, PIN_M0, PIN_M1, UART_BPS_RATE_9600);

// ======================= Протокол чат-пакета =================================
#define NICK_LEN       10       // 9 символов + '\0'
#define TEXT_CHUNK_LEN 38       // размер одного фрагмента текста

#pragma pack(push, 1)
struct ChatPacket {
  uint8_t msgId;                // счётчик сообщений отправителя (0..255, с переполнением)
  uint8_t fromAddr;             // ADDL отправителя
  uint8_t ttl;                  // сколько раз ещё можно переретранслировать
  uint8_t chunkIndex;           // номер фрагмента (с 0)
  uint8_t chunkTotal;           // всего фрагментов в сообщении
  char    nick[NICK_LEN];
  char    text[TEXT_CHUNK_LEN];
  uint8_t crc8;                 // CRC8 по всем предыдущим полям (msgId..text)
};
#pragma pack(pop)
// Итоговый размер пакета: 5 + 10 + 38 + 1(crc8) = 54 байта полезной нагрузки.
// С учётом заголовка fixedTransmission (ADDH,ADDL,CHAN = 3 байта) на воздух
// уходит ~57 байт — проверьте по факту через RF_Settings/логи, что это
// укладывается в лимит вашей конфигурации SF/BW (см. E22-900T22D datasheet).

// ======================= CRC8 (полином 0x07, CRC-8-CCITT, старший бит вперёд) ====
uint8_t crc8(const uint8_t *data, size_t len) {
  uint8_t crc = 0x00;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t b = 0; b < 8; b++) {
      if (crc & 0x80) {
        crc = (uint8_t)((crc << 1) ^ 0x07);
      } else {
        crc = (uint8_t)(crc << 1);
      }
    }
  }
  return crc;
}

// Посчитать и проставить crc8 в пакет (перед отправкой/ретрансляцией)
void fillCrc(ChatPacket &pkt) {
  pkt.crc8 = crc8((const uint8_t *)&pkt, sizeof(ChatPacket) - sizeof(pkt.crc8));
}

// Проверить crc8 у принятого пакета
bool crcValid(const ChatPacket &pkt) {
  uint8_t calc = crc8((const uint8_t *)&pkt, sizeof(ChatPacket) - sizeof(pkt.crc8));
  return calc == pkt.crc8;
}

// ======================= Дедупликация и сборка фрагментов ===================
struct SeenEntry { uint8_t fromAddr; uint8_t msgId; bool used; uint32_t ts; };
#define SEEN_CACHE_SIZE 32
#define SEEN_CACHE_TTL_MS 120000UL   // запись в кэше "протухает" через 2 минуты
SeenEntry seenCache[SEEN_CACHE_SIZE];
uint8_t seenCacheHead = 0;

bool alreadySeen(uint8_t fromAddr, uint8_t msgId) {
  uint32_t now = millis();
  for (int i = 0; i < SEEN_CACHE_SIZE; i++) {
    if (seenCache[i].used && seenCache[i].fromAddr == fromAddr && seenCache[i].msgId == msgId) {
      // защита от переполнения millis() (~49 дней): считаем протухшей и разницу "в минус"
      uint32_t age = now - seenCache[i].ts;
      if (age < SEEN_CACHE_TTL_MS) {
        return true;
      }
      return false; // запись устарела — считаем это новым сообщением
    }
  }
  return false;
}
void rememberSeen(uint8_t fromAddr, uint8_t msgId) {
  seenCache[seenCacheHead] = { fromAddr, msgId, true, millis() };
  seenCacheHead = (seenCacheHead + 1) % SEEN_CACHE_SIZE;
}

struct ReassemblyBuf {
  bool used;
  uint8_t fromAddr, msgId, chunkTotal;
  uint8_t receivedMask;   // до 8 фрагментов (достаточно для сообщений ~300 симв.)
  char nick[NICK_LEN];
  char text[8 * TEXT_CHUNK_LEN + 1];
  uint32_t lastUpdateMs;
};
#define REASM_SLOTS 4
#define REASM_SLOT_TIMEOUT_MS 15000UL  // если сборка с этим ключом (fromAddr+msgId)
                                        // не обновлялась дольше 15с, считаем её
                                        // "протухшей" и не продолжаем её новыми чанками
ReassemblyBuf reasm[REASM_SLOTS];

// ======================= История сообщений (для новых веб-клиентов) =========
struct HistoryItem { String nick; String text; uint32_t ts; };
#define HISTORY_SIZE 50
HistoryItem history[HISTORY_SIZE];
uint8_t historyCount = 0, historyHead = 0;

void pushHistory(const String &nick, const String &text) {
  history[historyHead] = { nick, text, millis() };
  historyHead = (historyHead + 1) % HISTORY_SIZE;
  if (historyCount < HISTORY_SIZE) historyCount++;
}

// ======================= Веб-сервер / WebSocket ==============================
AsyncWebServer server(80);
AsyncWebSocket ws("/ws");
DNSServer dnsServer;
SemaphoreHandle_t wsMutex;

// Очередь исходящих сообщений: веб -> задача LoRa
struct OutgoingMsg { char nick[NICK_LEN]; char text[240]; };
QueueHandle_t outQueue;

// ======================= Индикация ============================================
void ledFlash(uint8_t r, uint8_t g, uint8_t b, uint16_t ms) {
  pixel.setPixelColor(0, pixel.Color(r, g, b));
  pixel.show();
  ledOn = true;
  ledOffAt = millis() + ms;
}
void ledUpdate() {
  if (ledOn && (int32_t)(millis() - ledOffAt) >= 0) {
    pixel.setPixelColor(0, 0);
    pixel.show();
    ledOn = false;
  }
}

// ======================= Рассылка JSON всем веб-клиентам =====================
void broadcastChatJson(const String &nick, const String &text, bool fromRadio) {
  StaticJsonDocument<512> doc;
  doc["type"] = "message";
  doc["nick"] = nick;
  doc["text"] = text;
  doc["radio"] = fromRadio;
  doc["ts"] = millis();
  String out;
  serializeJson(doc, out);

  if (xSemaphoreTake(wsMutex, pdMS_TO_TICKS(200)) == pdTRUE) {
    ws.textAll(out);
    xSemaphoreGive(wsMutex);
  }
}

void broadcastStatusJson(const String &statusText) {
  StaticJsonDocument<256> doc;
  doc["type"] = "status";
  doc["text"] = statusText;
  String out;
  serializeJson(doc, out);
  if (xSemaphoreTake(wsMutex, pdMS_TO_TICKS(200)) == pdTRUE) {
    ws.textAll(out);
    xSemaphoreGive(wsMutex);
  }
}

// ======================= Конфигурация модуля E22 =============================
bool configureE22() {
  ResponseStructContainer c = e22.getConfiguration();
  if (c.status.code != 1) {
    Serial.print(F("getConfiguration failed: "));
    Serial.println(c.status.getResponseDescription());
    c.close();
    return false;
  }
  Configuration configuration = *(Configuration *)c.data;

  configuration.ADDH = 0x00;
  configuration.ADDL = NODE_ADDL;
  configuration.NETID = NET_ID;
  configuration.CHAN = LORA_CHANNEL;

  configuration.SPED.uartBaudRate = UART_BPS_9600;
  configuration.SPED.airDataRate  = AIR_DATA_RATE_010_24;
  configuration.SPED.uartParity   = MODE_00_8N1;

  configuration.OPTION.transmissionPower = POWER_10;
  configuration.OPTION.RSSIAmbientNoise = RSSI_AMBIENT_NOISE_ENABLED;

  configuration.TRANSMISSION_MODE.fixedTransmission = FT_FIXED_TRANSMISSION;
  configuration.TRANSMISSION_MODE.enableLBT  = LBT_ENABLED;   // требование ГКРЧ 868.7-869.2
  configuration.TRANSMISSION_MODE.enableRSSI = RSSI_ENABLED;
  configuration.TRANSMISSION_MODE.enableRepeater = REPEATER_DISABLED; // ретрансляция программная

  ResponseStatus rs = e22.setConfiguration(configuration, WRITE_CFG_PWR_DWN_SAVE);
  c.close();
  return (rs.code == 1);
}

// ======================= Отправка сообщения по LoRa (с разбиением) ==========
uint8_t txMsgIdCounter = 0;

bool sendChatOverLora(const char *nick, const char *text) {
  size_t len = strlen(text);
  uint8_t chunkTotal = (len == 0) ? 1 : (uint8_t)((len + TEXT_CHUNK_LEN - 1) / TEXT_CHUNK_LEN);
  if (chunkTotal > 8) chunkTotal = 8; // ограничение размера сборочного буфера

  uint8_t msgId = txMsgIdCounter++;
  bool allOk = true;

  for (uint8_t i = 0; i < chunkTotal; i++) {
    ChatPacket pkt;
    memset(&pkt, 0, sizeof(pkt));
    pkt.msgId = msgId;
    pkt.fromAddr = NODE_ADDL;
    pkt.ttl = FLOOD_TTL;
    pkt.chunkIndex = i;
    pkt.chunkTotal = chunkTotal;
    strncpy(pkt.nick, nick, NICK_LEN - 1);

    size_t offset = (size_t)i * TEXT_CHUNK_LEN;
    size_t chunkLen = min((size_t)TEXT_CHUNK_LEN, len - offset);
    memcpy(pkt.text, text + offset, chunkLen);

    fillCrc(pkt); // считаем CRC после того, как все поля заполнены

    ResponseStatus rs = e22.sendFixedMessage(
        BROADCAST_ADDH, BROADCAST_ADDL, LORA_CHANNEL,
        (uint8_t *)&pkt, (uint8_t)sizeof(pkt));

    if (rs.code != 1) {
      allOk = false;
      Serial.printf("[TX] chunk %d/%d FAIL: %s\r\n", i + 1, chunkTotal,
                     rs.getResponseDescription().c_str());
    } else {
      Serial.printf("[TX] chunk %d/%d OK (msgId=%u)\r\n", i + 1, chunkTotal, msgId);
    }
    //delay(50); // небольшая пауза между фрагментами, чтобы не забить эфир
    delay(2520); // небольшая пауза между фрагментами, чтобы не забить эфир
  }

  ledFlash(0, 0, 60, 300); // синяя вспышка = своя отправка
  return allOk;
}

// Пересылка уже готового (чужого) пакета дальше — флуд-ретрансляция
void repeatPacket(ChatPacket &pkt) {
  pkt.ttl -= 1;
  fillCrc(pkt); // ttl изменился -> CRC нужно пересчитать заново, иначе следующий
                // узел отбросит пакет как повреждённый
  delay(random(20, 120)); // случайная задержка, чтобы соседние узлы не били в эфир одновременно
  ResponseStatus rs = e22.sendFixedMessage(
      BROADCAST_ADDH, BROADCAST_ADDL, LORA_CHANNEL,
      (uint8_t *)&pkt, (uint8_t)sizeof(pkt));
  ledFlash(0, 60, 0, 200); // зелёная вспышка = ретрансляция чужого пакета
  Serial.printf("[REPEAT] from=%u msgId=%u ttl->%u : %s\r\n",
                pkt.fromAddr, pkt.msgId, pkt.ttl, rs.getResponseDescription().c_str());
}

// ======================= Сборка фрагментированных сообщений =================
void handleIncomingPacket(ChatPacket &pkt, int rssi) {
  // Дедуп: этот конкретный пакет (fromAddr+msgId+chunkIndex) не различаем по
  // chunkIndex специально — дедуп на уровне msgId достаточен, т.к. чанки одного
  // msgId идут последовательно и обрабатываются по мере прихода.
  bool duplicate = alreadySeen(pkt.fromAddr, pkt.msgId) && pkt.chunkTotal == 1;
  // Для многочанковых сообщений дедуп делаем по слоту реассемблинга ниже.

  if (pkt.chunkTotal == 1) {
    if (duplicate) {
      // уже видели — просто ретранслируем дальше, если ttl>0, но не показываем повторно
      if (pkt.ttl > 0) repeatPacket(pkt);
      return;
    }
    rememberSeen(pkt.fromAddr, pkt.msgId);
    String nick = String(pkt.nick);
    String text = String(pkt.text);
    Serial.printf("[RX] %s: %s (RSSI %d, from=%u)\r\n", nick.c_str(), text.c_str(), rssi, pkt.fromAddr);
    pushHistory(nick, text);
    broadcastChatJson(nick, text, true);
    if (pkt.ttl > 0) repeatPacket(pkt);
    return;
  }

  // Многочанковое сообщение — ищем/создаём слот сборки
  int slot = -1;
  for (int i = 0; i < REASM_SLOTS; i++) {
    if (reasm[i].used && reasm[i].fromAddr == pkt.fromAddr && reasm[i].msgId == pkt.msgId) {
      bool stale = (millis() - reasm[i].lastUpdateMs) > REASM_SLOT_TIMEOUT_MS;
      bool totalMismatch = reasm[i].chunkTotal != pkt.chunkTotal;
      if (stale || totalMismatch) {
        // Совпадение ключа (fromAddr,msgId) со старой, уже неактуальной сборкой —
        // это НЕ продолжение той сборки, а начало нового сообщения, которому
        // "повезло" получить тот же msgId (например, после перезапуска клиента
        // на ПК, где счётчик msgId стартует заново). Сбрасываем слот, а не
        // домешиваем в него новые чанки поверх старых недо/уже собранных данных.
        Serial.printf("[REASM] слот from=%u msgId=%u сброшен (stale=%d, "
                      "totalMismatch=%d: было %u, стало %u) — считаем новым сообщением\r\n",
                      pkt.fromAddr, pkt.msgId, stale, totalMismatch,
                      reasm[i].chunkTotal, pkt.chunkTotal);
        reasm[i].used = false;
        continue; // не назначаем slot=i — пусть ниже найдётся/создастся заново
      }
      slot = i; break;
    }
  }
  if (slot == -1) {
    if (alreadySeen(pkt.fromAddr, pkt.msgId)) {
      if (pkt.ttl > 0) repeatPacket(pkt);
      return; // уже собрали и показали это сообщение раньше
    }
    // ищем свободный/самый старый слот
    for (int i = 0; i < REASM_SLOTS; i++) {
      if (!reasm[i].used) { slot = i; break; }
    }
    if (slot == -1) slot = 0; // вытесняем нулевой слот, если все заняты
    reasm[slot] = ReassemblyBuf{};
    reasm[slot].used = true;
    reasm[slot].fromAddr = pkt.fromAddr;
    reasm[slot].msgId = pkt.msgId;
    reasm[slot].chunkTotal = pkt.chunkTotal;
    reasm[slot].receivedMask = 0;
    strncpy(reasm[slot].nick, pkt.nick, NICK_LEN - 1);
    memset(reasm[slot].text, 0, sizeof(reasm[slot].text));
    reasm[slot].lastUpdateMs = millis();
  }

  if (pkt.chunkIndex < 8 && !(reasm[slot].receivedMask & (1 << pkt.chunkIndex))) {
    reasm[slot].receivedMask |= (1 << pkt.chunkIndex);
    size_t off = (size_t)pkt.chunkIndex * TEXT_CHUNK_LEN;
    memcpy(reasm[slot].text + off, pkt.text, TEXT_CHUNK_LEN);
    reasm[slot].lastUpdateMs = millis();
  }

  if (pkt.ttl > 0) repeatPacket(pkt);

  uint8_t fullMask = (1 << pkt.chunkTotal) - 1;
  if ((reasm[slot].receivedMask & fullMask) == fullMask) {
    rememberSeen(pkt.fromAddr, pkt.msgId);
    String nick = String(reasm[slot].nick);
    String text = String(reasm[slot].text);
    Serial.printf("[RX-ASSEMBLED] %s: %s (from=%u)\r\n", nick.c_str(), text.c_str(), pkt.fromAddr);
    pushHistory(nick, text);
    broadcastChatJson(nick, text, true);
    reasm[slot].used = false;
  }
}

// ======================= Задача LoRa (ядро 0) =================================
void loraTask(void *param) {
  for (;;) {
    // 1) Приём по радио.
    // ВАЖНО: receiveMessageRSSI(size_t) — это перегрузка для приёма готовой
    // бинарной структуры целиком, она возвращает ResponseStructContainer
    // (data — это void*, а не String, в отличие от receiveMessageRSSI() без
    // аргумента, которая использовалась в e22_soft_repeater.ino).
    if (e22.available() > 1) {
      ResponseStructContainer rsc = e22.receiveMessageRSSI((uint8_t)sizeof(ChatPacket));
      if (rsc.status.code == 1) {
        ChatPacket pkt;
        memcpy(&pkt, rsc.data, sizeof(ChatPacket));
        int rssi = (int)rsc.rssi - 256;
        rsc.close(); // библиотека сама free() выделенный под data буфер

        if (!crcValid(pkt)) {
          // Пакет пришёл правильного размера, но с повреждённым содержимым
          // (коллизия в эфире, помеха и т.п.) — отбрасываем, чтобы не
          // скормить мусор в reassembly-буфер и не "обрезать" сообщение.
          Serial.printf("[CRC] отброшен повреждённый пакет: from=%u msgId=%u "
                        "chunk=%u/%u (RSSI %d)\r\n",
                        pkt.fromAddr, pkt.msgId, pkt.chunkIndex + 1, pkt.chunkTotal, rssi);
        } else {
          handleIncomingPacket(pkt, rssi);
        }
      } else {
        Serial.print(F("Ошибка приёма: "));
        Serial.println(rsc.status.getResponseDescription());
        rsc.close();
      }
    }

    // 2) Исходящие сообщения от веб-интерфейса
    OutgoingMsg out;
    if (xQueueReceive(outQueue, &out, 0) == pdTRUE) {
      sendChatOverLora(out.nick, out.text);
    }

    ledUpdate();
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

// ======================= HTTP / WebSocket обработчики =========================
void onWsEvent(AsyncWebSocket *server, AsyncWebSocketClient *client,
               AwsEventType type, void *arg, uint8_t *data, size_t len) {
  if (type == WS_EVT_CONNECT) {
    Serial.printf("[WS] client #%u connected\r\n", client->id());
    // отправляем историю новому клиенту
    StaticJsonDocument<4096> doc;
    doc["type"] = "history";
    JsonArray arr = doc.createNestedArray("items");
    for (uint8_t i = 0; i < historyCount; i++) {
      uint8_t idx = (historyHead + HISTORY_SIZE - historyCount + i) % HISTORY_SIZE;
      JsonObject item = arr.createNestedObject();
      item["nick"] = history[idx].nick;
      item["text"] = history[idx].text;
      item["ts"] = history[idx].ts;
    }
    String out;
    serializeJson(doc, out);
    client->text(out);
  } else if (type == WS_EVT_DISCONNECT) {
    Serial.printf("[WS] client #%u disconnected\r\n", client->id());
  } else if (type == WS_EVT_DATA) {
    AwsFrameInfo *info = (AwsFrameInfo *)arg;
    if (info->final && info->index == 0 && info->len == len && info->opcode == WS_TEXT) {
      StaticJsonDocument<512> doc;
      DeserializationError err = deserializeJson(doc, data, len);
      if (!err && doc["type"] == "send") {
        String nick = doc["nick"] | NODE_NICK;
        String text = doc["text"] | "";
        if (text.length() > 0) {
          OutgoingMsg out;
          memset(&out, 0, sizeof(out));
          strncpy(out.nick, nick.c_str(), NICK_LEN - 1);
          strncpy(out.text, text.c_str(), sizeof(out.text) - 1);
          xQueueSend(outQueue, &out, pdMS_TO_TICKS(100));
          // сразу показываем своё сообщение в UI отправителя и остальных вкладок
          pushHistory(nick, text);
          broadcastChatJson(nick, text, false);
        }
      }
    }
  }
}

void setupCaptivePortal() {
  dnsServer.start(53, "*", AP_IP);
}

void setupRoutes() {
  server.serveStatic("/", LittleFS, "/").setDefaultFile("index.html");

  // captive-portal: типичные проверочные URL разных ОС отдают редирект на главную
  server.on("/generate_204", HTTP_GET, [](AsyncWebServerRequest *req) {
    req->redirect("/");
  });
  server.on("/hotspot-detect.html", HTTP_GET, [](AsyncWebServerRequest *req) {
    req->redirect("/");
  });

  server.on("/api/status", HTTP_GET, [](AsyncWebServerRequest *req) {
    StaticJsonDocument<256> doc;
    doc["node_addr"] = NODE_ADDL;
    doc["channel"] = LORA_CHANNEL;
    doc["netid"] = NET_ID;
    doc["clients"] = ws.count();
    String out;
    serializeJson(doc, out);
    req->send(200, "application/json", out);
  });

  server.onNotFound([](AsyncWebServerRequest *req) {
    req->redirect("/"); // всё неизвестное — на главную (упрощённый captive portal)
  });
}

// ======================= setup / loop ==========================================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println();
  Serial.println(F("=== LoRa-2026 Chat (ESP32-S3-N16R8 + E22-900T22D), v2 CRC8 ==="));

  randomSeed(esp_random());

  pixel.begin();
  pixel.setBrightness(80);
  pixel.clear();
  pixel.show();

  if (!LittleFS.begin(true)) {
    Serial.println(F("LittleFS mount FAILED"));
  } else {
    Serial.println(F("LittleFS OK"));
  }

  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(AP_IP, AP_IP, AP_MASK);
  WiFi.softAP(AP_SSID, AP_PASSWORD);
  Serial.print(F("AP запущена: "));
  Serial.println(AP_SSID);
  Serial.print(F("IP: "));
  Serial.println(WiFi.softAPIP());

  setupCaptivePortal();

  wsMutex = xSemaphoreCreateMutex();
  outQueue = xQueueCreate(10, sizeof(OutgoingMsg));

  ws.onEvent(onWsEvent);
  server.addHandler(&ws);
  setupRoutes();
  server.begin();
  Serial.println(F("Веб-сервер запущен"));

  Serial1.begin(9600, SERIAL_8N1, PIN_RXD1, PIN_TXD1);
  e22.begin();
  bool ok = configureE22();
  Serial.print(F("Конфигурация E22: "));
  Serial.println(ok ? F("OK") : F("FAIL"));
  Serial.printf("Адрес узла: 00:%02X, NETID: %02X, канал: %d\r\n", NODE_ADDL, NET_ID, LORA_CHANNEL);
  Serial.printf("Размер ChatPacket: %d байт (с учётом CRC8)\r\n", (int)sizeof(ChatPacket));

  xTaskCreatePinnedToCore(loraTask, "loraTask", 8192, NULL, 1, NULL, 0);

  Serial.println(F("Готово. Подключайтесь к точке доступа и открывайте http://192.168.4.1/"));
}

void loop() {
  dnsServer.processNextRequest();
  ws.cleanupClients();
  delay(5);
}
