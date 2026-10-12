/*
  CROSS v2 - radio dell'incrocio: ponte ESP-NOW <-> seriale (protocollo SC-P2P v2)

  Collegata via USB alla UNO Q (115200 baud, un oggetto JSON per riga terminata da "\n"):
    - riga ricevuta dalla seriale  -> trasmessa in broadcast ESP-NOW (FF:FF:FF:FF:FF:FF)
    - frame ESP-NOW ricevuto       -> stampato sulla seriale come una riga, preceduto dall'RSSI:
                                        R-63 {"v":2,"t":"vru","d":"tag-a1b2c3","p":"blind","q":12}
  La radio e' "trasparente": non interpreta il JSON, non fa rilancio e non controlla le firme
  (dedup, rilancio con "h", firma HMAC, Tag vicini ecc. li gestisce peer.py sul nodo).
  L'RSSI (dBm) serve al nodo per capire se un CROSS Tag e' davvero vicino all'incrocio.

  RSSI secondo il core Arduino-ESP32:
    - core 3.x (ESP-IDF 5.x): letto dal frame stesso (info->rx_ctrl->rssi);
    - core 2.x (ESP-IDF 4.4): la callback ESP-NOW non lo fornisce; con RSSI_PROMISCUOUS 1 la radio
      ascolta anche in modalita' promiscua (solo frame di gestione) e prende l'RSSI dell'ultimo
      frame ESP-NOW dello stesso mittente. Se non lo trova (o con RSSI_PROMISCUOUS 0) scrive "R0".
    "R0" significa sempre "RSSI non disponibile": il nodo lo tratta come un trasporto senza RSSI.

  Convenzione seriale: le righe che iniziano con "#" sono commenti/stato della radio e vanno ignorate
  dal lato Python (che comunque scarta ogni riga non JSON). Anche le righe "#" inviate dal Python
  alla radio vengono ignorate. Le righe trasmesse NON hanno il prefisso "R": solo quelle ricevute.

  Energia: la radio e' alimentata dalla UNO Q e deve restare sempre in ascolto (e' lei che sveglia il
  nodo quando arriva un Tag o un mezzo di soccorso), quindi niente risparmio sul Wi-Fi; si abbassa
  solo la frequenza della CPU (RADIO_CPU_MHZ, 80 MHz e' il minimo che regge il Wi-Fi).

  Compatibile con core Arduino-ESP32 2.x (ESP-IDF 4.4) e 3.x (ESP-IDF 5.x, anche 5.5).
  Nessuna libreria da installare.
*/
#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#if __has_include(<esp_arduino_version.h>)
#include <esp_arduino_version.h>
#endif
#if __has_include(<esp_idf_version.h>)
#include <esp_idf_version.h>
#endif

// ======================= configurazione =======================
#define WIFI_CHANNEL 1          // canale fisso, UGUALE su tutte le radio e su tutti i CROSS Tag (1-13)
#define SERIAL_BAUD 115200
#define LINE_BUF 256            // buffer riga seriale: righe piu' lunghe vengono scartate
#define MAX_PAYLOAD 240         // limite del protocollo SC-P2P v2
#define MAX_FRAME 250           // massimo di un frame ESP-NOW v1: frame piu' lunghi ignorati
#define RX_QUEUE_LEN 16         // frame ricevuti in attesa di essere stampati
#define STATUS_EVERY_MS 60000UL // riga di stato "#" periodica (0 = mai)
#define RADIO_CPU_MHZ 80        // frequenza CPU (80 = risparmio, minimo per il Wi-Fi; 0 = default)
#define RSSI_PROMISCUOUS 1      // solo core 2.x: RSSI dalla modalita' promiscua (0 = sempre "R0")
#define RSSI_UNKNOWN 0          // valore stampato quando l'RSSI non e' disponibile ("R0")
#define RSSI_SLOTS 16           // core 2.x: mittenti ricordati per l'RSSI
#define RSSI_MAX_AGE_MS 2000    // core 2.x: RSSI piu' vecchio di cosi' = non disponibile

// LED di attivita': lampo breve a ogni messaggio trasmesso o ricevuto.
// ESP32-C3 (SuperMini): LED blu su GPIO 8, acceso con LOW. Altre schede: LED_BUILTIN o GPIO 2.
// Per forzare un altro pin definite qui LED_PIN (e LED_ACTIVE_LOW); -1 = nessun LED.
#ifndef LED_PIN
#if defined(CONFIG_IDF_TARGET_ESP32C3)
#define LED_PIN 8
#define LED_ACTIVE_LOW 1
#elif defined(LED_BUILTIN)
#define LED_PIN LED_BUILTIN
#else
#define LED_PIN 2               // LED blu delle DevKit ESP32 classiche
#endif
#endif
#ifndef LED_ACTIVE_LOW
#define LED_ACTIVE_LOW 0        // 1 se il LED della scheda si accende con LOW
#endif

// core 1.x non definisce la versione: assumiamo la vecchia API
#ifndef ESP_ARDUINO_VERSION_MAJOR
#define ESP_ARDUINO_VERSION_MAJOR 1
#define ESP_ARDUINO_VERSION_MINOR 0
#define ESP_ARDUINO_VERSION_PATCH 0
#endif

// La callback di invio ha cambiato firma con ESP-IDF 5.5 (core Arduino 3.3):
// prima (const uint8_t *mac, status), poi (const esp_now_send_info_t *info, status).
#if defined(ESP_IDF_VERSION) && defined(ESP_IDF_VERSION_VAL)
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 0)
#define SC_SEND_INFO_CB 1
#endif
#endif
#ifndef SC_SEND_INFO_CB
#define SC_SEND_INFO_CB 0
#endif

// Firme delle callback ESP-NOW. Sono macro (e non due definizioni dentro #if) perche' alcuni
// strumenti (es. PlatformIO) generano i prototipi delle funzioni del .ino senza guardare gli #if.
#if ESP_ARDUINO_VERSION_MAJOR >= 3
#define SC_RECV_CB_ARGS const esp_now_recv_info_t *info, const uint8_t *data, int len
#else
#define SC_RECV_CB_ARGS const uint8_t *mac, const uint8_t *data, int len
#endif
#if SC_SEND_INFO_CB
#define SC_SEND_CB_ARGS const esp_now_send_info_t *who, esp_now_send_status_t status
#else
#define SC_SEND_CB_ARGS const uint8_t *who, esp_now_send_status_t status
#endif

// core 2.x: l'RSSI arriva dalla modalita' promiscua
#if ESP_ARDUINO_VERSION_MAJOR < 3 && RSSI_PROMISCUOUS
#define SC_RSSI_PROMISC 1
#else
#define SC_RSSI_PROMISC 0
#endif

// ======================= stato =======================
struct Frame {
  uint16_t len;
  int16_t rssi;     // dBm dal frame (core 3.x) oppure RSSI_UNKNOWN
  uint8_t mac[6];   // mittente: serve al core 2.x per cercare l'RSSI
  uint8_t data[MAX_FRAME];
};

struct RssiSlot {   // core 2.x: ultimo RSSI visto per mittente (modalita' promiscua)
  uint8_t mac[6];
  int8_t rssi;
  uint32_t at;      // millis() della ricezione
};

static const uint8_t BCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static const int PIN_LED = LED_PIN;
static QueueHandle_t rxQueue = nullptr;

// contatori (quelli "volatile" sono scritti dal task Wi-Fi nelle callback)
static volatile uint32_t rxDropped = 0, rxTooLong = 0, txFail = 0;
static uint32_t rxCount = 0, rxBad = 0, rxNoRssi = 0, txCount = 0, txErr = 0, lineErr = 0;

static char line[LINE_BUF];
static size_t lineLen = 0;
static bool lineOverflow = false;

static uint32_t ledOffAt = 0, lastStatus = 0;
static bool ledOn = false;
static bool promiscOn = false;

static RssiSlot rssiTab[RSSI_SLOTS];
static uint8_t rssiNext = 0;
static portMUX_TYPE rssiMux = portMUX_INITIALIZER_UNLOCKED;

// ======================= LED attivita' =======================
static void ledWrite(bool on) {
  ledOn = on;
  if (PIN_LED < 0) return;
  digitalWrite(PIN_LED, (on != (LED_ACTIVE_LOW != 0)) ? HIGH : LOW);
}

static void ledPulse() {
  ledWrite(true);
  ledOffAt = millis() + 40;
}

static void ledTick() {
  if (ledOn && (int32_t)(millis() - ledOffAt) >= 0) ledWrite(false);
}

// ======================= RSSI (core 2.x, modalita' promiscua) =======================
// Gira nel task Wi-Fi per ogni frame di gestione: deve essere brevissima.
// Un frame ESP-NOW e' un "action frame" (0xD0) di categoria "vendor specific" (127) con l'OUI di
// Espressif 18:FE:34; il mittente (addr2) e' ai byte 10-15 dell'intestazione 802.11.
static void onPromisc(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (buf == nullptr || type != WIFI_PKT_MGMT) return;
  const wifi_promiscuous_pkt_t *p = (const wifi_promiscuous_pkt_t *)buf;
  const uint8_t *f = p->payload;
  if ((int)p->rx_ctrl.sig_len < 39) return;
  if (f[0] != 0xD0 || f[24] != 127 || f[25] != 0x18 || f[26] != 0xFE || f[27] != 0x34) return;
  int8_t rssi = (int8_t)p->rx_ctrl.rssi;
  uint32_t now = millis();
  portENTER_CRITICAL(&rssiMux);
  int k = -1;
  for (int i = 0; i < RSSI_SLOTS; i++) {
    if (memcmp(rssiTab[i].mac, f + 10, 6) == 0) {
      k = i;
      break;
    }
  }
  if (k < 0) {  // mittente nuovo: prende lo slot successivo a rotazione
    k = rssiNext;
    rssiNext = (uint8_t)((rssiNext + 1) % RSSI_SLOTS);
    memcpy(rssiTab[k].mac, f + 10, 6);
  }
  rssiTab[k].rssi = rssi;
  rssiTab[k].at = now;
  portEXIT_CRITICAL(&rssiMux);
}

// RSSI recente del mittente, RSSI_UNKNOWN se non c'e'. Chiamata dal loop: a quel punto la
// callback promiscua dello stesso frame e' gia' passata, in qualunque ordine il driver le chiami.
static int rssiLookup(const uint8_t *mac) {
  int r = RSSI_UNKNOWN;
  uint32_t now = millis();
  portENTER_CRITICAL(&rssiMux);
  for (int i = 0; i < RSSI_SLOTS; i++) {
    if (memcmp(rssiTab[i].mac, mac, 6) == 0) {
      if ((int32_t)(now - rssiTab[i].at) <= (int32_t)RSSI_MAX_AGE_MS) r = rssiTab[i].rssi;
      break;
    }
  }
  portEXIT_CRITICAL(&rssiMux);
  return r;
}

// ======================= callback ESP-NOW =======================
// Girano nel task Wi-Fi: solo copia in coda, niente Serial qui dentro.
static void queueFrame(const uint8_t *mac, int rssi, const uint8_t *data, int len) {
  static Frame f;  // la callback e' sempre chiamata dallo stesso task: basta un buffer statico
  if (data == nullptr || len <= 0 || len > MAX_FRAME) {
    rxTooLong = rxTooLong + 1;  // (niente ++ su volatile: deprecato in C++20)
    return;
  }
  f.len = (uint16_t)len;
  f.rssi = (int16_t)rssi;
  if (mac != nullptr) memcpy(f.mac, mac, 6);
  else memset(f.mac, 0, 6);
  memcpy(f.data, data, len);
  if (xQueueSend(rxQueue, &f, 0) != pdTRUE) rxDropped = rxDropped + 1;
}

static void onRecv(SC_RECV_CB_ARGS) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  const uint8_t *mac = (info != nullptr) ? info->src_addr : nullptr;
  int rssi = (info != nullptr && info->rx_ctrl != nullptr) ? (int)info->rx_ctrl->rssi : RSSI_UNKNOWN;
#else
  int rssi = RSSI_UNKNOWN;  // core 2.x: cercato nel loop con rssiLookup()
#endif
  queueFrame(mac, rssi, data, len);
}

static void onSent(SC_SEND_CB_ARGS) {
  (void)who;
  if (status != ESP_NOW_SEND_SUCCESS) txFail = txFail + 1;
}

// ======================= avvio radio =======================
static bool startEspNow() {
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();     // nessun AP: il canale resta quello fissato qui sotto
  WiFi.setSleep(false);  // ricezione continua: la radio e' l'orecchio sempre acceso del nodo

  esp_err_t err = ESP_FAIL;
  for (int i = 0; i < 20 && err != ESP_OK; i++) {
    err = esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
    if (err != ESP_OK) delay(50);
  }
  if (err != ESP_OK) Serial.printf("# errore: canale %d non impostato (%s)\n", WIFI_CHANNEL, esp_err_to_name(err));

  if (esp_now_init() != ESP_OK) return false;
  esp_now_register_recv_cb(onRecv);
  esp_now_register_send_cb(onSent);

  esp_now_peer_info_t peer;
  memset(&peer, 0, sizeof(peer));
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = 0;  // 0 = canale corrente (quello impostato sopra)
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;  // il broadcast ESP-NOW non puo' essere cifrato
  esp_err_t perr = esp_now_add_peer(&peer);
  if (perr != ESP_OK && perr != ESP_ERR_ESPNOW_EXIST) return false;

#if SC_RSSI_PROMISC
  // core 2.x: solo frame di gestione (gli ESP-NOW sono "action frame"), callback leggerissima
  wifi_promiscuous_filter_t filt;
  memset(&filt, 0, sizeof(filt));
  filt.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT;
  esp_wifi_set_promiscuous_filter(&filt);
  esp_wifi_set_promiscuous_rx_cb(onPromisc);
  promiscOn = esp_wifi_set_promiscuous(true) == ESP_OK;
  if (!promiscOn) Serial.println("# attenzione: modalita' promiscua non attiva, RSSI non disponibile (R0)");
#else
  (void)onPromisc;  // core 3.x: l'RSSI arriva con il frame, la modalita' promiscua non serve
  (void)rssiLookup;
#endif
  return true;
}

static const char *rssiMode() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  (void)promiscOn;
  return "frame";
#else
  return promiscOn ? "promiscuo" : "no(R0)";
#endif
}

static void printStatus(const char *what) {
  uint8_t ch = 0;
  wifi_second_chan_t sc;
  esp_wifi_get_channel(&ch, &sc);
  Serial.printf("# SC-P2P v2 radio %s: mac=%s canale=%u core=%d.%d.%d cpu=%luMHz rssi=%s tx=%lu txerr=%lu "
                "txfail=%lu rx=%lu rxbad=%lu senza_rssi=%lu persi=%lu lunghi=%lu righe_err=%lu\n",
                what, WiFi.macAddress().c_str(), (unsigned)ch, ESP_ARDUINO_VERSION_MAJOR,
                ESP_ARDUINO_VERSION_MINOR, ESP_ARDUINO_VERSION_PATCH, (unsigned long)getCpuFrequencyMhz(),
                rssiMode(), (unsigned long)txCount, (unsigned long)txErr, (unsigned long)txFail,
                (unsigned long)rxCount, (unsigned long)rxBad, (unsigned long)rxNoRssi,
                (unsigned long)rxDropped, (unsigned long)rxTooLong, (unsigned long)lineErr);
}

// ======================= controllo testo =======================
// true se i byte sono testo UTF-8 senza caratteri di controllo e sembrano un oggetto JSON
static bool isJsonText(const uint8_t *s, int n) {
  if (n < 2 || s[0] != '{' || s[n - 1] != '}') return false;
  for (int i = 0; i < n;) {
    uint8_t c = s[i];
    if (c < 0x80) {
      if (c < 0x20 || c == 0x7F) return false;  // controllo, \n, \r, \0 ...
      i++;
      continue;
    }
    int extra = (c & 0xE0) == 0xC0 ? 1 : (c & 0xF0) == 0xE0 ? 2 : (c & 0xF8) == 0xF0 ? 3 : -1;
    if (extra < 0 || c == 0xC0 || c == 0xC1 || c > 0xF4 || i + extra >= n) return false;
    for (int k = 1; k <= extra; k++)
      if ((s[i + k] & 0xC0) != 0x80) return false;
    i += extra + 1;
  }
  return true;
}

// ======================= ESP-NOW -> seriale =======================
static void printFrame(const Frame &f) {
  static char out[8 + MAX_FRAME + 2];  // "R-100 " + JSON + "\n"
  int n = f.len;
  // toglie eventuali terminatori/spazi finali (es. mittente che invia anche lo '\0')
  while (n > 0 && (f.data[n - 1] == 0 || f.data[n - 1] == ' ' || f.data[n - 1] == '\r' || f.data[n - 1] == '\n')) n--;
  if (!isJsonText(f.data, n)) {
    rxBad++;
    return;
  }
  int rssi = f.rssi;
#if SC_RSSI_PROMISC
  if (promiscOn) rssi = rssiLookup(f.mac);
#endif
  if (rssi == RSSI_UNKNOWN) rxNoRssi++;
  int k = snprintf(out, 9, "R%d ", rssi);  // al massimo "R-128 " (6 caratteri)
  if (k <= 0 || k > 8) k = snprintf(out, 9, "R%d ", RSSI_UNKNOWN);
  memcpy(out + k, f.data, n);
  out[k + n] = '\n';
  Serial.write((const uint8_t *)out, k + n + 1);  // una sola scrittura: la riga resta intera
  rxCount++;
  ledPulse();
}

// ======================= seriale -> ESP-NOW =======================
static void handleLine(char *s, size_t len) {
  while (len > 0 && (s[len - 1] == ' ' || s[len - 1] == '\t')) s[--len] = '\0';
  while (len > 0 && (*s == ' ' || *s == '\t')) {
    s++;
    len--;
  }
  if (len == 0 || s[0] == '#') return;  // riga vuota o commento
  if (s[0] != '{') {
    lineErr++;
    Serial.println("# ignorata: la riga non e' un oggetto JSON");
    return;
  }
  if (len > MAX_PAYLOAD) {
    lineErr++;
    Serial.printf("# errore: messaggio di %u byte > %d, non inviato\n", (unsigned)len, MAX_PAYLOAD);
    return;
  }
  esp_err_t err = esp_now_send(BCAST, (const uint8_t *)s, len);
  for (int i = 0; err == ESP_ERR_ESPNOW_NO_MEM && i < 3; i++) {  // coda TX piena: piccolo ritardo e riprova
    delay(5);
    err = esp_now_send(BCAST, (const uint8_t *)s, len);
  }
  if (err == ESP_OK) {
    txCount++;
    ledPulse();
  } else {
    txErr++;
    Serial.printf("# errore esp_now_send: %s\n", esp_err_to_name(err));
  }
}

static void readSerial() {
  int budget = 512;  // non monopolizza il loop
  while (budget-- > 0 && Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\n') {
      if (lineOverflow) {
        lineErr++;
        Serial.printf("# errore: riga oltre %d byte, scartata\n", LINE_BUF - 1);
      } else {
        line[lineLen] = '\0';
        handleLine(line, lineLen);
      }
      lineLen = 0;
      lineOverflow = false;
    } else if (c == '\r' || lineOverflow) {
      // CR ignorato (monitor seriali che inviano CRLF); resto della riga troppo lunga scartato
    } else if (lineLen >= LINE_BUF - 1) {
      lineOverflow = true;
    } else {
      line[lineLen++] = (char)c;
    }
  }
}

// ======================= setup / loop =======================
void setup() {
#if RADIO_CPU_MHZ > 0
  setCpuFrequencyMhz(RADIO_CPU_MHZ);  // prima di Serial.begin: la seriale parte gia' alla frequenza giusta
#endif
#if !ARDUINO_USB_CDC_ON_BOOT
  Serial.setRxBufferSize(1024);  // va chiamato prima di begin()
#endif
  Serial.begin(SERIAL_BAUD);
  if (PIN_LED >= 0) pinMode(PIN_LED, OUTPUT);
  ledWrite(false);
  delay(200);

  rxQueue = xQueueCreate(RX_QUEUE_LEN, sizeof(Frame));
  if (rxQueue == nullptr || !startEspNow()) {
    Serial.println("# errore: ESP-NOW non avviato, riavvio tra 5 s");
    delay(5000);
    ESP.restart();
  }
  printStatus("pronto");
  lastStatus = millis();
}

void loop() {
  static Frame f;
  readSerial();
  for (int i = 0; i < 8 && xQueueReceive(rxQueue, &f, 0) == pdTRUE; i++) printFrame(f);
  ledTick();
  if (STATUS_EVERY_MS > 0 && millis() - lastStatus >= STATUS_EVERY_MS) {
    lastStatus = millis();
    printStatus("stato");
  }
  delay(1);
}
