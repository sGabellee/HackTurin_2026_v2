/*
  CROSS v2 - CROSS Tag: il dispositivo personale degli utenti della strada (protocollo SC-P2P v2)

  Un solo firmware per tutti i ruoli (docs/ARCHITETTURA.md §1.2, §3, §6):
    pedestrian (pedone), blind (pedone non vedente), cyclist (ciclista), driver (conducente),
    emergency (mezzo di soccorso).

  Cosa fa:
    - trasmette in broadcast ESP-NOW (canale Wi-Fi fisso 1, senza collegarsi a nessuna rete) il
      beacon di presenza, che la radio dell'incrocio passa al nodo con l'RSSI:
        {"v":2,"t":"vru","d":"tag-a1b2c3","p":"blind","q":12}         ogni 1 s
        {"v":2,"t":"emv","d":"tag-a1b2c3","q":40,"st":"run"}          ogni 0,5 s (ruolo emergency)
      Nessun orologio: "q" e' solo un contatore. Il Tag non firma e non rilancia mai nulla.
    - riceve gli avvisi "adv" degli incroci vicini, per esempio
        {"v":2,"t":"adv","n":"incrocio-42","id":"a7","r":["blind","pedestrian"],"c":1,"s":2,"x":0,"ttl":3000}
      e tiene solo quelli per il suo ruolo ("r" contiene il ruolo) o per lui ("to" = id del Tag).
      Un avviso vale "ttl" ms dal momento in cui arriva (l'incrocio lo ripete ogni secondo): se
      l'incrocio tace l'avviso si spegne da solo.
    - mostra l'avviso piu' grave ancora valido con vibrazione, LED e (solo per i non vedenti) suono,
      come nella tabella di §6. Per il soccorso: LED verde = corridoio libero, rosso = occupato
      (con un solo LED: lampeggio lento = libero, veloce = occupato).
    Piu' incroci: per ogni incrocio (e attraversamento) vale l'ultimo avviso ricevuto; tra incroci
    diversi vince il piu' grave. Un "via libera" (CROSS_OK, KEEP_GOING, CORRIDOR_CLEAR) non copre mai
    un avviso di pericolo ancora valido.

  Ruolo: pressione lunga (1,5 s) del tasto BOOT = ruolo successivo, salvato in memoria (Preferences).
  La conferma sono N vibrazioni/lampi: 1 pedone, 2 non vedente, 3 ciclista, 4 conducente, 5 soccorso.
  Pressione breve = ripete le N vibrazioni del ruolo attuale. Non tenere premuto BOOT all'accensione.

  Monitor seriale (115200, qualsiasi fine riga): "h" aiuto, "i" stato, "1".."5" ruolo, "t" avviso di
  prova, "t4" avviso di prova con codice 4, "s" sirena, "d90" direzione, "v" log dettagliato,
  "x" autotest del lettore JSON, una riga {...} = messaggio ricevuto (prove senza radio).

  Schede (preset automatici):
    - ESP32-C3 SuperMini: LED blu su GPIO 8 acceso con LOW, BOOT su GPIO 9;
    - ESP32-S3 DevKitC: LED RGB della scheda (colori veri per il corridoio), BOOT su GPIO 0;
    - ESP32 DevKit: LED su GPIO 2, BOOT su GPIO 0.
  Motorino vibrante via transistor NPN su GPIO 4, buzzer (facoltativo) su GPIO 5: vedi README.md.

  Energia: CPU a 80 MHz (TAG_CPU_MHZ) e LED quasi sempre spento. La radio resta in ascolto continuo
  perche' gli avvisi arrivano in broadcast in qualunque momento; TAG_RADIO_SAVE 1 (solo core 3.x)
  la fa dormire tra una finestra di ascolto e l'altra, mai per i ruoli blind ed emergency.

  Compatibile con core Arduino-ESP32 2.x (ESP-IDF 4.4) e 3.x (ESP-IDF 5.x, anche 5.5).
  Nessuna libreria da installare. Ogni #define qui sotto si puo' cambiare anche dai build flag.
*/
#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#if __has_include(<esp_arduino_version.h>)
#include <esp_arduino_version.h>
#endif

// ======================= configurazione =======================
#ifndef WIFI_CHANNEL
#define WIFI_CHANNEL 1          // canale fisso, UGUALE su tutte le radio degli incroci e su tutti i Tag
#endif
#ifndef DEFAULT_ROLE
#define DEFAULT_ROLE 0          // ruolo al primo avvio: 0 pedone, 1 non vedente, 2 ciclista, 3 conducente, 4 soccorso
#endif
#ifndef VIBRO_PIN
#define VIBRO_PIN 4             // base del transistor NPN del motorino (o LED + resistenza per la demo); -1 = nessuno
#endif
#ifndef BUZZER_PIN
#define BUZZER_PIN 5            // buzzer per i non vedenti; -1 = nessun buzzer
#endif
#ifndef BUZZER_ACTIVE
#define BUZZER_ACTIVE 0         // 0 = buzzer passivo / piezo (toni veri), 1 = buzzer attivo (solo acceso/spento)
#endif
#ifndef LED_GREEN_PIN
#define LED_GREEN_PIN -1        // soccorso: LED verde esterno (CORRIDOR_CLEAR); -1 = nessuno
#endif
#ifndef LED_RED_PIN
#define LED_RED_PIN -1          // soccorso: LED rosso esterno (CORRIDOR_BUSY); -1 = nessuno
#endif
#ifndef EMERGENCY_HEADING
#define EMERGENCY_HEADING -1    // soccorso: direzione di marcia in gradi 0-359 nel campo "hd"; -1 = non inviata
#endif
#ifndef EMERGENCY_SIREN
#define EMERGENCY_SIREN 1       // soccorso: 1 = sirena accesa all'avvio ("st":"run"), 0 = spenta ("st":"off")
#endif
#ifndef BATTERY_ADC_PIN
#define BATTERY_ADC_PIN -1      // pin ADC del partitore della batteria; -1 = campo "b" non inviato
#endif
#ifndef BATTERY_DIVIDER_X10
#define BATTERY_DIVIDER_X10 20  // rapporto del partitore x10 (20 = due resistenze uguali)
#endif
#ifndef TAG_CPU_MHZ
#define TAG_CPU_MHZ 80          // frequenza CPU (80 = risparmio, minimo che regge il Wi-Fi; 0 = default)
#endif
#ifndef TAG_RADIO_SAVE
#define TAG_RADIO_SAVE 0        // 1 = la radio dorme tra le finestre di ascolto (solo core 3.x, mai blind/emergency)
#endif
#ifndef SAVE_WAKE_INTERVAL_MS
#define SAVE_WAKE_INTERVAL_MS 130  // TAG_RADIO_SAVE: periodo di ascolto (non divide 1000: gli adv ogni 1 s
#endif                             // non cadono sempre nella stessa pausa)
#ifndef SAVE_WAKE_WINDOW_MS
#define SAVE_WAKE_WINDOW_MS 100    // TAG_RADIO_SAVE: ms di ascolto in ogni periodo
#endif
#ifndef ROLE_CODE_FILTER
#define ROLE_CODE_FILTER 1      // 1 = accetta solo i codici previsti per il ruolo (tabella §2.7)
#endif
#ifndef LONG_PRESS_MS
#define LONG_PRESS_MS 1500      // pressione lunga di BOOT per cambiare ruolo
#endif
#ifndef STATUS_EVERY_MS
#define STATUS_EVERY_MS 60000UL // riga di stato periodica sul monitor seriale (0 = mai)
#endif

#define DEFAULT_TTL_MS 3000     // validita' di un adv senza "ttl"
#define MIN_TTL_MS 200          // "ttl" ricevuti fuori da questi limiti vengono riportati dentro
#define MAX_TTL_MS 30000
#define LINK_TIMEOUT_MS 5000    // un incrocio ci parla se ha mandato un adv per noi negli ultimi 5 s
#define BEACON_MS 1000          // periodo del beacon "vru"
#define EMV_MS 500              // periodo del beacon "emv" (soccorso)
#define BEACON_JITTER_MS 30     // +- ms casuali: Tag accesi insieme non trasmettono sempre insieme
#define TEST_TTL_MS 5000        // durata dell'avviso di prova ("t")
#define MAX_FRAME 250           // massimo di un frame ESP-NOW v1
#define RX_QUEUE_LEN 8
#define LINE_BUF 256
#define ADV_SLOTS 8             // avvisi ricordati (uno per incrocio, attraversamento e tipo)
#define NODE_LEN 32
#define KIND_LEN 32
#define SERIAL_IDLE_MS 300      // riga senza "a capo" (monitor con "Nessun fine riga"): eseguita dopo 300 ms
#define RGB_LEVEL 40            // luminosita' del LED RGB (0-255)

// Scheda: preset del LED e del tasto BOOT (si possono forzare definendo LED_PIN / BUTTON_PIN)
#ifndef LED_PIN
#if defined(CONFIG_IDF_TARGET_ESP32C3)
#define LED_PIN 8               // ESP32-C3 SuperMini: LED blu su GPIO 8, acceso con LOW
#define LED_ACTIVE_LOW 1
#elif defined(RGB_BUILTIN)
#define LED_PIN RGB_BUILTIN     // ESP32-S3 DevKitC e simili: LED RGB della scheda
#define LED_RGB 1
#elif defined(LED_BUILTIN)
#define LED_PIN LED_BUILTIN
#else
#define LED_PIN 2               // LED blu delle DevKit ESP32 classiche
#endif
#endif
#ifndef LED_ACTIVE_LOW
#define LED_ACTIVE_LOW 0        // 1 se il LED della scheda si accende con LOW
#endif
#ifndef LED_RGB
#define LED_RGB 0
#endif
#ifndef BUTTON_PIN
#if defined(CONFIG_IDF_TARGET_ESP32C3)
#define BUTTON_PIN 9            // BOOT sulle ESP32-C3
#else
#define BUTTON_PIN 0            // BOOT su ESP32 e ESP32-S3
#endif
#endif

// colori veri per il corridoio del soccorso (LED RGB o due LED esterni)
#if LED_RGB || (LED_GREEN_PIN >= 0 && LED_RED_PIN >= 0)
#define HAVE_CORRIDOR_COLORS 1
#else
#define HAVE_CORRIDOR_COLORS 0
#endif

// core 1.x non definisce la versione: assumiamo la vecchia API
#ifndef ESP_ARDUINO_VERSION_MAJOR
#define ESP_ARDUINO_VERSION_MAJOR 1
#define ESP_ARDUINO_VERSION_MINOR 0
#define ESP_ARDUINO_VERSION_PATCH 0
#endif

// Firma della callback di ricezione ESP-NOW: macro (e non due definizioni dentro #if) perche' alcuni
// strumenti (es. PlatformIO) generano i prototipi delle funzioni del .ino senza guardare gli #if.
#if ESP_ARDUINO_VERSION_MAJOR >= 3
#define SC_RECV_CB_ARGS const esp_now_recv_info_t *info, const uint8_t *data, int len
#else
#define SC_RECV_CB_ARGS const uint8_t *mac, const uint8_t *data, int len
#endif

#define RSSI_NONE 0             // RSSI non disponibile (core 2.x, messaggi dalla seriale)

// ======================= ruoli e codici (ARCHITETTURA.md §1.2 e §2.7) =======================
enum { ROLE_PEDESTRIAN = 0, ROLE_BLIND, ROLE_CYCLIST, ROLE_DRIVER, ROLE_EMERGENCY, ROLE_COUNT };
static const char *const ROLE_NAMES[ROLE_COUNT] = {"pedestrian", "blind", "cyclist", "driver", "emergency"};
static const char *const ROLE_IT[ROLE_COUNT] = {"pedone", "non vedente", "ciclista", "conducente", "soccorso"};

enum {
  C_CLEAR = 0,
  C_WAIT = 1,
  C_CROSS_OK = 2,
  C_KEEP_GOING = 3,
  C_VEHICLE_LEFT = 4,
  C_VEHICLE_RIGHT = 5,
  C_PEDESTRIAN_AHEAD = 6,
  C_CYCLIST_AHEAD = 7,
  C_BIKE_LANE_BLOCKED = 8,
  C_EMERGENCY_APPROACHING = 9,
  C_CORRIDOR_CLEAR = 10,
  C_CORRIDOR_BUSY = 11,
  C_HAZARD_AHEAD = 12,
  C_ASSIST_UNAVAILABLE = 13,
  C_WRONG_WAY = 14,
  CODE_COUNT = 15
};
#define NO_CODE 255             // nessun avviso valido

static const char *const CODE_NAMES[CODE_COUNT] = {
  "CLEAR", "WAIT", "CROSS_OK", "KEEP_GOING", "VEHICLE_LEFT", "VEHICLE_RIGHT", "PEDESTRIAN_AHEAD",
  "CYCLIST_AHEAD", "BIKE_LANE_BLOCKED", "EMERGENCY_APPROACHING", "CORRIDOR_CLEAR", "CORRIDOR_BUSY",
  "HAZARD_AHEAD", "ASSIST_UNAVAILABLE", "WRONG_WAY"};

// Per chi e' ogni codice (colonna "Per chi" di §2.7): un bit per ruolo
#define RB(r) (1u << (r))
#define R_ALL 0x1Fu
static const uint8_t CODE_ROLES[CODE_COUNT] = {
  /* 0 CLEAR                 */ R_ALL,
  /* 1 WAIT                  */ RB(ROLE_PEDESTRIAN) | RB(ROLE_BLIND),
  /* 2 CROSS_OK              */ RB(ROLE_PEDESTRIAN) | RB(ROLE_BLIND),
  /* 3 KEEP_GOING            */ RB(ROLE_PEDESTRIAN) | RB(ROLE_BLIND),
  /* 4 VEHICLE_LEFT          */ RB(ROLE_PEDESTRIAN) | RB(ROLE_BLIND) | RB(ROLE_CYCLIST),
  /* 5 VEHICLE_RIGHT         */ RB(ROLE_PEDESTRIAN) | RB(ROLE_BLIND) | RB(ROLE_CYCLIST),
  /* 6 PEDESTRIAN_AHEAD      */ RB(ROLE_DRIVER),
  /* 7 CYCLIST_AHEAD         */ RB(ROLE_DRIVER),
  /* 8 BIKE_LANE_BLOCKED     */ RB(ROLE_CYCLIST),
  /* 9 EMERGENCY_APPROACHING */ R_ALL & ~RB(ROLE_EMERGENCY),
  /* 10 CORRIDOR_CLEAR       */ RB(ROLE_EMERGENCY),
  /* 11 CORRIDOR_BUSY        */ RB(ROLE_EMERGENCY),
  /* 12 HAZARD_AHEAD         */ RB(ROLE_DRIVER) | RB(ROLE_CYCLIST),
  /* 13 ASSIST_UNAVAILABLE   */ RB(ROLE_BLIND),
  /* 14 WRONG_WAY            */ R_ALL,
};

// Urgenza del codice, a parita' di gravita' "s" (piu' alto = piu' urgente)
static const uint8_t CODE_PRIO[CODE_COUNT] = {0, 5, 2, 3, 9, 9, 4, 4, 7, 10, 2, 6, 7, 6, 8};

// ======================= tabelle di §6: vibrazione e suono =======================
// Vibrazione: durate alternate acceso, spento, acceso, ... (ms) dall'inizio del periodo, poi spento
// fino alla fine del periodo; il disegno si ripete finche' l'avviso resta valido. period 0 = niente.
struct VibPattern {
  uint16_t period;
  uint8_t n;
  uint16_t d[8];
};
static const VibPattern VIB[CODE_COUNT] = {
  /* 0 CLEAR                 */ {0, 0, {0}},
  /* 1 WAIT                  */ {2000, 1, {800}},                              // impulso lungo ogni 2 s
  /* 2 CROSS_OK              */ {1500, 3, {120, 150, 120}},                    // due impulsi brevi ogni 1,5 s
  /* 3 KEEP_GOING            */ {1000, 1, {40}},                               // tic leggero ogni 1 s
  /* 4 VEHICLE_LEFT          */ {1200, 5, {90, 90, 90, 90, 90}},               // 3 impulsi rapidi
  /* 5 VEHICLE_RIGHT         */ {1200, 7, {90, 90, 90, 90, 90, 90, 90}},       // 4 impulsi rapidi
  /* 6 PEDESTRIAN_AHEAD      */ {3000, 5, {250, 200, 250, 200, 250}},          // 3 impulsi medi
  /* 7 CYCLIST_AHEAD         */ {3000, 5, {250, 200, 250, 200, 250}},          // 3 impulsi medi
  /* 8 BIKE_LANE_BLOCKED     */ {3000, 5, {250, 200, 250, 200, 250}},          // 3 impulsi medi
  /* 9 EMERGENCY_APPROACHING */ {200, 1, {100}},                               // continua a scatti 5 Hz
  /* 10 CORRIDOR_CLEAR       */ {0, 0, {0}},                                   // solo LED verde
  /* 11 CORRIDOR_BUSY        */ {0, 0, {0}},                                   // solo LED rosso
  /* 12 HAZARD_AHEAD         */ {3000, 5, {250, 200, 250, 200, 250}},          // 3 impulsi medi
  /* 13 ASSIST_UNAVAILABLE   */ {5000, 3, {600, 400, 600}},                    // lungo + pausa + lungo ogni 5 s
  /* 14 WRONG_WAY            */ {3000, 5, {250, 200, 250, 200, 250}},          // 3 impulsi medi
};

// Suono (solo ruolo blind): tratti di tono dall'istante "at" del periodo per "dur" ms, con la
// frequenza che va da f0 a f1 Hz (f0 = f1: tono fisso). period 0 = silenzio.
struct ToneSeg {
  uint16_t at, dur, f0, f1;
};
struct SoundPattern {
  uint16_t period;
  uint8_t n;
  ToneSeg seg[2];
};
static const SoundPattern SOUND[CODE_COUNT] = {
  /* 0 CLEAR                 */ {0, 0, {{0, 0, 0, 0}, {0, 0, 0, 0}}},
  /* 1 WAIT                  */ {2000, 1, {{0, 600, 392, 392}, {0, 0, 0, 0}}},       // tono basso lento
  /* 2 CROSS_OK              */ {500, 1, {{0, 90, 1760, 1760}, {0, 0, 0, 0}}},       // tono alto ripetuto
  /* 3 KEEP_GOING            */ {1000, 1, {{0, 15, 3000, 3000}, {0, 0, 0, 0}}},      // tic
  /* 4 VEHICLE_LEFT          */ {1200, 1, {{0, 600, 600, 1800}, {0, 0, 0, 0}}},      // tono che sale
  /* 5 VEHICLE_RIGHT         */ {1200, 1, {{0, 600, 1800, 600}, {0, 0, 0, 0}}},      // tono che scende
  /* 6 PEDESTRIAN_AHEAD      */ {0, 0, {{0, 0, 0, 0}, {0, 0, 0, 0}}},
  /* 7 CYCLIST_AHEAD         */ {0, 0, {{0, 0, 0, 0}, {0, 0, 0, 0}}},
  /* 8 BIKE_LANE_BLOCKED     */ {0, 0, {{0, 0, 0, 0}, {0, 0, 0, 0}}},
  /* 9 EMERGENCY_APPROACHING */ {800, 2, {{0, 400, 960, 960}, {400, 400, 640, 640}}}, // sirena a due toni
  /* 10 CORRIDOR_CLEAR       */ {0, 0, {{0, 0, 0, 0}, {0, 0, 0, 0}}},
  /* 11 CORRIDOR_BUSY        */ {0, 0, {{0, 0, 0, 0}, {0, 0, 0, 0}}},
  /* 12 HAZARD_AHEAD         */ {0, 0, {{0, 0, 0, 0}, {0, 0, 0, 0}}},
  /* 13 ASSIST_UNAVAILABLE   */ {5000, 2, {{0, 300, 330, 330}, {350, 600, 220, 220}}}, // tono di errore
  /* 14 WRONG_WAY            */ {0, 0, {{0, 0, 0, 0}, {0, 0, 0, 0}}},
};

// ======================= tipi =======================
struct Frame {
  uint16_t len;
  int16_t rssi;
  uint8_t data[MAX_FRAME];
};

// Un adv letto e accettato da parseAdv()
struct AdvMsg {
  bool isAdv;          // e' un "adv" SC-P2P v2 (anche se non per questo Tag)
  bool direct;         // rivolto a questo dispositivo ("to")
  uint8_t code, sev;
  int16_t x;           // attraversamento (-1 = non indicato)
  uint32_t ttl;        // ms
  char node[NODE_LEN];
  char kind[KIND_LEN];
  const char *why;     // perche' e' stato scartato (per il log)
};

// Avviso ricordato: uno per (incrocio, attraversamento, diretto o per ruolo)
struct AdvSlot {
  bool used, direct;
  uint8_t code, sev;
  int16_t x;
  uint32_t at, ttl;    // millis() della ricezione e validita' in ms
  char node[NODE_LEN];
  char kind[KIND_LEN];
};

// Casi di prova del lettore JSON (comando "x" sul Tag; li controlla anche test_firmware.py)
struct ParseCase {
  const char *json;
  uint8_t role;
  const char *dev;
  int16_t code;        // codice atteso, -1 = messaggio scartato
  uint16_t ttl;        // ttl atteso (ms) se accettato
};
static const ParseCase PARSE_CASES[] = {
  // PARSE_CASES_INIZIO
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"incrocio-42\",\"id\":\"a1\",\"r\":[\"blind\",\"pedestrian\"],\"c\":1,\"s\":2,\"x\":0,\"ttl\":3000}", ROLE_BLIND, "tag-a1b2c3", 1, 3000},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"incrocio-42\",\"id\":\"a1\",\"r\":[\"blind\",\"pedestrian\"],\"c\":1,\"s\":2,\"x\":0,\"ttl\":3000}", ROLE_PEDESTRIAN, "tag-a1b2c3", 1, 3000},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"incrocio-42\",\"id\":\"a1\",\"r\":[\"blind\",\"pedestrian\"],\"c\":1,\"s\":2,\"x\":0,\"ttl\":3000}", ROLE_CYCLIST, "tag-a1b2c3", -1, 0},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"incrocio-42\",\"id\":\"a2\",\"to\":\"tag-a1b2c3\",\"c\":2,\"s\":1,\"x\":0,\"ttl\":2500}", ROLE_BLIND, "tag-a1b2c3", 2, 2500},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"incrocio-42\",\"id\":\"a2\",\"to\":\"tag-a1b2c3\",\"c\":2,\"s\":1,\"x\":0,\"ttl\":2500}", ROLE_BLIND, "tag-0000ff", -1, 0},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"incrocio-42\",\"id\":\"a2\",\"to\":\"tag-a1b2c3x\",\"c\":2,\"s\":1,\"ttl\":2500}", ROLE_BLIND, "tag-a1b2c3", -1, 0},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"x\\\"r\\\":[\\\"cyclist\\\"],\\\"c\\\":8\",\"r\":[\"driver\"],\"c\":6,\"s\":1,\"ttl\":3000}", ROLE_CYCLIST, "tag-a1b2c3", -1, 0},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"x\\\"r\\\":[\\\"cyclist\\\"],\\\"c\\\":8\",\"r\":[\"driver\"],\"c\":6,\"s\":1,\"ttl\":3000}", ROLE_DRIVER, "tag-a1b2c3", 6, 3000},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"i-1\",\"p\":{\"c\":9,\"r\":[\"cyclist\"]},\"r\":[\"cyclist\"],\"c\":8,\"s\":2,\"ttl\":2000}", ROLE_CYCLIST, "tag-a1b2c3", 8, 2000},
  {"{ \"v\" : 2 , \"t\" : \"adv\" , \"r\" : [ \"cyclist\" , \"driver\" ] , \"c\" : 12 , \"s\" : 3 , \"k\" : \"wrong_way\" , \"ttl\" : 1500 }", ROLE_DRIVER, "tag-a1b2c3", 12, 1500},
  {"{\"v\":1,\"t\":\"adv\",\"r\":[\"blind\"],\"c\":1,\"ttl\":3000}", ROLE_BLIND, "tag-a1b2c3", -1, 0},
  {"{\"v\":2,\"t\":\"vru\",\"d\":\"tag-123456\",\"p\":\"blind\",\"q\":3}", ROLE_BLIND, "tag-a1b2c3", -1, 0},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"i-1\",\"r\":[\"pedestrian\"],\"c\":6,\"s\":1,\"ttl\":3000}", ROLE_PEDESTRIAN, "tag-a1b2c3", -1, 0},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"i-1\",\"r\":[\"blind\"],\"c\":1", ROLE_BLIND, "tag-a1b2c3", -1, 0},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"i-1\",\"r\":[\"blind\"],\"c\":15,\"ttl\":3000}", ROLE_BLIND, "tag-a1b2c3", -1, 0},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"i-1\",\"r\":[\"blind\"],\"c\":13,\"s\":2}", ROLE_BLIND, "tag-a1b2c3", 13, 3000},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"i-1\",\"r\":[\"blind\"],\"c\":3,\"ttl\":99999}", ROLE_BLIND, "tag-a1b2c3", 3, 30000},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"i-1\",\"r\":[\"blind\"],\"c\":3,\"ttl\":10}", ROLE_BLIND, "tag-a1b2c3", 3, 200},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"incrocio-1\",\"id\":\"incrocio-1-adv-7\",\"r\":[\"emergency\"],\"c\":11,\"s\":2,\"x\":1,\"ttl\":3000,\"sig\":\"0123456789abcdef\"}", ROLE_EMERGENCY, "tag-a1b2c3", 11, 3000},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"i-1\",\"r\":[\"blindx\",\"pedestrian2\"],\"c\":1,\"ttl\":3000}", ROLE_BLIND, "tag-a1b2c3", -1, 0},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"i-1\",\"r\":[\"driver\",\"cyclist\",\"pedestrian\",\"blind\"],\"c\":9,\"s\":3,\"ttl\":3000}", ROLE_DRIVER, "tag-a1b2c3", 9, 3000},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"i-1\",\"r\":[\"emergency\"],\"c\":9,\"s\":3,\"ttl\":3000}", ROLE_EMERGENCY, "tag-a1b2c3", -1, 0},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"i-1\",\"c\":14,\"s\":3,\"ttl\":3000}", ROLE_CYCLIST, "tag-a1b2c3", 14, 3000},
  {"{\"v\":2,\"t\":\"adv\",\"n\":\"i-1\",\"r\":\"cyclist\",\"c\":4,\"s\":2,\"ttl\":3000.0}", ROLE_CYCLIST, "tag-a1b2c3", 4, 3000},
  {"[\"v\",2,\"t\",\"adv\"]", ROLE_BLIND, "tag-a1b2c3", -1, 0},
  // PARSE_CASES_FINE
};

// ======================= formato dei beacon =======================
// Il quarto %s e' il campo facoltativo ("b" batteria per vru, "hd" direzione per emv) o "".
#define FMT_VRU "{\"v\":2,\"t\":\"vru\",\"d\":\"%s\",\"p\":\"%s\",\"q\":%lu%s}"
#define FMT_EMV "{\"v\":2,\"t\":\"emv\",\"d\":\"%s\",\"q\":%lu%s,\"st\":\"%s\"}"

// ======================= stato =======================
static const uint8_t BCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
static const int PIN_LED = LED_PIN;

static uint8_t role = DEFAULT_ROLE;
static char devId[16] = "tag-000000";
static Preferences prefs;
static QueueHandle_t rxQueue = nullptr;

static AdvSlot slots[ADV_SLOTS];
static uint8_t shownCode = NO_CODE;   // avviso mostrato adesso
static int shownSlot = -1;
static uint32_t patStart = 0;         // inizio del disegno di vibrazione/suono dell'avviso mostrato
static uint32_t lastAdvAt = 0;        // ultimo adv per noi (0 = mai)

static uint32_t beaconSeq = 0, nextBeacon = 0;
static bool siren = EMERGENCY_SIREN != 0;
static int heading = EMERGENCY_HEADING;
static int batteryPct = -1;
static uint32_t lastBatteryRead = 0;
static bool radioSaving = false;
static bool verbose = false;

static uint8_t fbPulses = 0;          // conferma del ruolo: N impulsi brevi
static uint32_t fbStart = 0;

static bool vibOn = false;
static uint16_t buzzFreq = 0;
static uint8_t ledState = 255;

// contatori ("volatile" quelli scritti dal task Wi-Fi nella callback)
static volatile uint32_t rxDropped = 0, rxTooLong = 0;
static uint32_t rxAdv = 0, rxNotMine = 0, rxOther = 0, rxBad = 0, beaconOk = 0, beaconErr = 0;
static uint32_t lastStatus = 0;

static char serLine[LINE_BUF];
static int serLen = 0;
static bool serOverflow = false;
static uint32_t serLastByte = 0;

enum { LED_OFF = 0, LED_STATUS, LED_ALERT, LED_GREEN, LED_RED };

// ======================= lettura del JSON (senza librerie) =======================
// Basta per i messaggi SC-P2P: un oggetto con stringhe, numeri e liste. Si guardano solo le chiavi
// di primo livello, quindi un testo dentro una stringa o un oggetto annidato non viene mai scambiato
// per un campo (un semplice strstr di "c": lo farebbe). Nessuna allocazione, nessuna eccezione.

static int jsSkipWs(const char *s, int n, int i) {
  while (i < n && (s[i] == ' ' || s[i] == '\t' || s[i] == '\r' || s[i] == '\n')) i++;
  return i;
}

// s[i] e' la virgoletta di apertura: indice subito dopo quella di chiusura, -1 se manca
static int jsSkipString(const char *s, int n, int i) {
  for (i++; i < n; i++) {
    if (s[i] == '\\') i++;  // carattere protetto (\" \\ ...): saltato
    else if (s[i] == '"') return i + 1;
  }
  return -1;
}

// indice subito dopo il valore che inizia in s[i] (stringa, numero, lista, oggetto...), -1 se rotto
static int jsSkipValue(const char *s, int n, int i) {
  if (i >= n) return -1;
  if (s[i] == '"') return jsSkipString(s, n, i);
  if (s[i] == '{' || s[i] == '[') {
    int depth = 0;
    while (i < n) {
      char c = s[i];
      if (c == '"') {
        i = jsSkipString(s, n, i);
        if (i < 0) return -1;
        continue;
      }
      if (c == '{' || c == '[') depth++;
      else if (c == '}' || c == ']') {
        depth--;
        if (depth == 0) return i + 1;
      }
      i++;
    }
    return -1;
  }
  int start = i;
  while (i < n && s[i] != ',' && s[i] != '}' && s[i] != ']' && s[i] != ' ' && s[i] != '\t' && s[i] != '\r' &&
         s[i] != '\n')
    i++;
  return i > start ? i : -1;
}

// Cerca la chiave di primo livello key: true con il valore in s[*vs .. *vs + *vl)
static bool jsonGet(const char *s, int n, const char *key, int *vs, int *vl) {
  int klen = (int)strlen(key);
  int i = jsSkipWs(s, n, 0);
  if (i >= n || s[i] != '{') return false;
  i++;
  for (int guard = 0; guard < 64; guard++) {  // al massimo 64 campi
    i = jsSkipWs(s, n, i);
    if (i >= n || s[i] != '"') return false;  // '}' = oggetto finito senza la chiave, o testo rotto
    int ks = i + 1;
    int ke = jsSkipString(s, n, i);
    if (ke < 0) return false;
    int kl = ke - 1 - ks;
    i = jsSkipWs(s, n, ke);
    if (i >= n || s[i] != ':') return false;
    i = jsSkipWs(s, n, i + 1);
    int ve = jsSkipValue(s, n, i);
    if (ve < 0) return false;
    if (kl == klen && memcmp(s + ks, key, klen) == 0) {
      *vs = i;
      *vl = ve - i;
      return true;
    }
    i = jsSkipWs(s, n, ve);
    if (i >= n || s[i] != ',') return false;
    i++;
  }
  return false;
}

// true se s e' un solo oggetto {...} chiuso bene (parentesi e virgolette): scarta i messaggi troncati
static bool jsonObjectOk(const char *s, int n) {
  int i = jsSkipWs(s, n, 0);
  if (i >= n || s[i] != '{') return false;
  int e = jsSkipValue(s, n, i);
  return e > 0 && jsSkipWs(s, n, e) == n;
}

static bool jsonHas(const char *s, int n, const char *key) {
  int vs, vl;
  return jsonGet(s, n, key, &vs, &vl);
}

// true se il valore di key e' esattamente la stringa str
static bool jsonStrEq(const char *s, int n, const char *key, const char *str) {
  int vs, vl;
  if (!jsonGet(s, n, key, &vs, &vl) || vl < 2 || s[vs] != '"') return false;
  int sl = (int)strlen(str);
  return vl - 2 == sl && memcmp(s + vs + 1, str, sl) == 0;
}

// Valore stringa (senza virgolette, tolte le barre di protezione) in out; false se manca
static bool jsonStr(const char *s, int n, const char *key, char *out, int outSize) {
  int vs, vl;
  if (outSize <= 0) return false;
  out[0] = '\0';
  if (!jsonGet(s, n, key, &vs, &vl) || vl < 2 || s[vs] != '"') return false;
  int o = 0, end = vs + vl - 1;
  for (int i = vs + 1; i < end && o < outSize - 1; i++) {
    char c = s[i];
    if (c == '\\' && i + 1 < end) c = s[++i];
    out[o++] = c;
  }
  out[o] = '\0';
  return true;
}

// Valore intero (accetta anche 3000.0 e "12"); def se manca o non e' un numero
static long jsonInt(const char *s, int n, const char *key, long def) {
  int vs, vl;
  if (!jsonGet(s, n, key, &vs, &vl)) return def;
  int a = vs, b = vs + vl;
  if (vl >= 2 && s[a] == '"') {
    a++;
    b--;
  }
  char tmp[24];
  int len = b - a;
  if (len <= 0 || len >= (int)sizeof(tmp)) return def;
  memcpy(tmp, s + a, len);
  tmp[len] = '\0';
  char *end = nullptr;
  long v = strtol(tmp, &end, 10);
  if (end == tmp || (*end != '\0' && *end != '.')) return def;
  return v;
}

// true se la lista key contiene la stringa word: "r":["blind","pedestrian"] (anche "r":"blind")
static bool jsonListHas(const char *s, int n, const char *key, const char *word) {
  int vs, vl;
  if (!jsonGet(s, n, key, &vs, &vl)) return false;
  int wl = (int)strlen(word);
  if (s[vs] == '"') return vl - 2 == wl && memcmp(s + vs + 1, word, wl) == 0;
  if (s[vs] != '[') return false;
  int end = vs + vl - 1;  // la ']' finale
  int i = vs + 1;
  while (i < end) {
    i = jsSkipWs(s, end, i);
    if (i >= end) break;
    int e = jsSkipValue(s, end, i);
    if (e < 0) return false;
    if (s[i] == '"' && e - i - 2 == wl && memcmp(s + i + 1, word, wl) == 0) return true;
    i = jsSkipWs(s, end, e);
    if (i >= end || s[i] != ',') break;
    i++;
  }
  return false;
}

// ======================= avvisi =======================
static bool codeIsWarning(uint8_t c) {
  return c != C_CLEAR && c != C_CROSS_OK && c != C_KEEP_GOING && c != C_CORRIDOR_CLEAR;
}

// Gravita' per scegliere tra avvisi di incroci diversi: prima qualunque pericolo, poi "s", poi il codice
static int adviceRank(uint8_t code, uint8_t sev) {
  if (code >= CODE_COUNT) return -1;
  return (codeIsWarning(code) ? 1000 : 0) + sev * 100 + CODE_PRIO[code];
}

// Legge un messaggio ricevuto: true se e' un adv valido per questo ruolo / dispositivo
static bool parseAdv(const char *s, int n, uint8_t r, const char *dev, AdvMsg *m) {
  memset(m, 0, sizeof(*m));
  m->x = -1;
  m->ttl = DEFAULT_TTL_MS;
  m->why = "";
  if (r >= ROLE_COUNT) r = ROLE_PEDESTRIAN;
  if (!jsonObjectOk(s, n)) {
    m->why = "JSON non valido o troncato";
    return false;
  }
  if (jsonInt(s, n, "v", -1) != 2) {
    m->why = "non e' un messaggio SC-P2P v2";
    return false;
  }
  if (!jsonStrEq(s, n, "t", "adv")) {
    m->why = "non e' un adv";
    return false;
  }
  m->isAdv = true;
  if (!jsonStr(s, n, "n", m->node, sizeof(m->node))) strcpy(m->node, "?");
  jsonStr(s, n, "k", m->kind, sizeof(m->kind));
  if (jsonHas(s, n, "to")) {
    if (!jsonStrEq(s, n, "to", dev)) {
      m->why = "per un altro Tag";
      return false;
    }
    m->direct = true;
  } else if (jsonHas(s, n, "r") && !jsonListHas(s, n, "r", ROLE_NAMES[r])) {
    m->why = "per altri ruoli";
    return false;
  }
  long c = jsonInt(s, n, "c", -1);
  if (c < 0 || c >= CODE_COUNT) {
    m->why = "codice sconosciuto";
    return false;
  }
#if ROLE_CODE_FILTER
  if ((CODE_ROLES[c] & RB(r)) == 0) {
    m->why = "codice non previsto per il ruolo";
    return false;
  }
#endif
  m->code = (uint8_t)c;
  long sv = jsonInt(s, n, "s", 1);
  m->sev = (uint8_t)(sv < 0 ? 0 : sv > 3 ? 3 : sv);
  long x = jsonInt(s, n, "x", -1);
  m->x = (int16_t)(x < -1 ? -1 : x > 999 ? 999 : x);
  long ttl = jsonInt(s, n, "ttl", DEFAULT_TTL_MS);
  m->ttl = (uint32_t)(ttl < MIN_TTL_MS ? MIN_TTL_MS : ttl > MAX_TTL_MS ? MAX_TTL_MS : ttl);
  return true;
}

static bool slotValid(const AdvSlot &a, uint32_t now) {
  return a.used && (uint32_t)(now - a.at) < a.ttl;
}

// Ricorda un avviso: sostituisce l'ultimo dello stesso incrocio/attraversamento/tipo (anche se meno
// grave: l'incrocio sa com'e' adesso la situazione), altrimenti prende uno slot libero o il piu' vecchio.
static void storeAdvice(const AdvMsg &m, uint32_t now) {
  int k = -1, freeK = -1, oldK = 0;
  uint32_t oldLeft = 0xFFFFFFFFUL;
  for (int i = 0; i < ADV_SLOTS; i++) {
    const AdvSlot &a = slots[i];
    if (a.used && a.direct == m.direct && a.x == m.x && strcmp(a.node, m.node) == 0) {
      k = i;
      break;
    }
    if (!slotValid(a, now)) {
      if (freeK < 0) freeK = i;
      continue;
    }
    uint32_t left = a.ttl - (uint32_t)(now - a.at);
    if (left < oldLeft) {
      oldLeft = left;
      oldK = i;
    }
  }
  if (k < 0) k = freeK >= 0 ? freeK : oldK;
  AdvSlot &a = slots[k];
  a.used = true;
  a.direct = m.direct;
  a.code = m.code;
  a.sev = m.sev;
  a.x = m.x;
  a.at = now;
  a.ttl = m.ttl;
  memcpy(a.node, m.node, sizeof(a.node));
  memcpy(a.kind, m.kind, sizeof(a.kind));
}

// Slot dell'avviso piu' grave ancora valido, -1 se nessuno
static int bestSlot(uint32_t now) {
  int best = -1, bestRank = -1;
  for (int i = 0; i < ADV_SLOTS; i++) {
    if (!slotValid(slots[i], now)) continue;
    int r = adviceRank(slots[i].code, slots[i].sev);
    if (r > bestRank) {
      bestRank = r;
      best = i;
    }
  }
  return best;
}

static void clearAdvice() {
  memset(slots, 0, sizeof(slots));
  shownCode = NO_CODE;
  shownSlot = -1;
}

// ======================= disegni di vibrazione e suono =======================
// Motorino acceso all'istante t (ms dall'inizio dell'avviso) per il codice code
static bool vibAt(uint8_t code, uint32_t t) {
  if (code >= CODE_COUNT) return false;
  const VibPattern &p = VIB[code];
  if (p.period == 0) return false;
  uint32_t pos = t % p.period, acc = 0;
  for (uint8_t i = 0; i < p.n && i < 8; i++) {
    acc += p.d[i];
    if (pos < acc) return (i % 2) == 0;
  }
  return false;
}

// Frequenza del buzzer (Hz, 0 = silenzio) all'istante t per il codice code
static uint16_t toneAt(uint8_t code, uint32_t t) {
  if (code >= CODE_COUNT) return 0;
  const SoundPattern &p = SOUND[code];
  if (p.period == 0) return 0;
  uint32_t pos = t % p.period;
  for (uint8_t i = 0; i < p.n && i < 2; i++) {
    const ToneSeg &g = p.seg[i];
    if (g.dur == 0 || pos < (uint32_t)g.at || pos >= (uint32_t)g.at + g.dur) continue;
    int32_t f = (int32_t)g.f0 + ((int32_t)g.f1 - (int32_t)g.f0) * (int32_t)(pos - g.at) / (int32_t)g.dur;
    f = (f + 10) / 20 * 20;  // passi di 20 Hz: il tono cambia solo quando serve
    return (uint16_t)(f < 20 ? 20 : f);
  }
  return 0;
}

// ======================= uscite: motorino, buzzer, LED =======================
static void vibWrite(bool on) {
  if (on == vibOn) return;
  vibOn = on;
#if VIBRO_PIN >= 0
  digitalWrite(VIBRO_PIN, on ? HIGH : LOW);
#endif
}

static void buzzWrite(uint16_t f) {
#if BUZZER_PIN >= 0
  if (f == buzzFreq) return;
#if BUZZER_ACTIVE
  digitalWrite(BUZZER_PIN, f ? HIGH : LOW);
#else
  if (f == 0) noTone(BUZZER_PIN);
  else tone(BUZZER_PIN, f);  // core 2.x e 3.x: LEDC tramite il task del tono, non blocca
#endif
  buzzFreq = f;
#else
  (void)f;
#endif
}

// sempre definita (i prototipi generati dall'IDE la dichiarano comunque); fa qualcosa solo con LED_RGB
static inline void rgbWrite(uint8_t r, uint8_t g, uint8_t b) {
#if LED_RGB
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  rgbLedWrite(PIN_LED, r, g, b);
#else
  neopixelWrite(PIN_LED, r, g, b);
#endif
#else
  (void)r; (void)g; (void)b;
#endif
}

// LED_OFF, LED_STATUS (blu sul LED RGB), LED_ALERT (bianco), LED_GREEN, LED_RED; scrive solo se cambia
static void ledWrite(uint8_t st) {
  if (st == ledState) return;
  ledState = st;
#if LED_RGB
  uint8_t r = 0, g = 0, b = 0;
  if (st == LED_STATUS) b = RGB_LEVEL;
  else if (st == LED_ALERT) r = g = b = RGB_LEVEL;
  else if (st == LED_GREEN) g = RGB_LEVEL;
  else if (st == LED_RED) r = RGB_LEVEL;
  rgbWrite(r, g, b);
#else
  bool on = st != LED_OFF && !(HAVE_CORRIDOR_COLORS && (st == LED_GREEN || st == LED_RED));
  if (PIN_LED >= 0) digitalWrite(PIN_LED, (on != (LED_ACTIVE_LOW != 0)) ? HIGH : LOW);
#endif
#if LED_GREEN_PIN >= 0
  digitalWrite(LED_GREEN_PIN, st == LED_GREEN ? HIGH : LOW);
#endif
#if LED_RED_PIN >= 0
  digitalWrite(LED_RED_PIN, st == LED_RED ? HIGH : LOW);
#endif
}

// Conferma del ruolo (N impulsi brevi): true finche' dura, *on = motorino/LED accesi adesso
static bool feedbackAt(uint32_t now, bool *on) {
  if (fbPulses == 0) return false;
  uint32_t t = now - fbStart;
  if (t / 340 >= fbPulses) {
    fbPulses = 0;
    return false;
  }
  *on = (t % 340) < 120;
  return true;
}

static void feedback(uint8_t pulses) {
  fbPulses = pulses;
  fbStart = millis();
}

static bool linked(uint32_t now) {
  return lastAdvAt != 0 && (uint32_t)(now - lastAdvAt) < LINK_TIMEOUT_MS;
}

static void outputsTick(uint32_t now) {
  bool vib = false, fb = false;
  uint16_t freq = 0;
  uint8_t led = LED_OFF;
  if (feedbackAt(now, &fb)) {  // conferma del ruolo: ha la precedenza (dura meno di 2 s)
    vib = fb;
    led = fb ? LED_STATUS : LED_OFF;
  } else if (shownCode < CODE_COUNT && shownCode != C_CLEAR) {
    uint32_t t = now - patStart;
    if (shownCode == C_CORRIDOR_CLEAR || shownCode == C_CORRIDOR_BUSY) {
      bool busy = shownCode == C_CORRIDOR_BUSY;
#if HAVE_CORRIDOR_COLORS
      (void)t;
      led = busy ? LED_RED : LED_GREEN;
#else
      bool on = busy ? (t % 250) < 125 : (t % 1000) < 500;  // un solo LED: veloce = occupato, lento = libero
      led = on ? LED_ALERT : LED_OFF;
#endif
    } else {
      vib = vibAt(shownCode, t);
      led = vib ? LED_ALERT : LED_OFF;
      if (role == ROLE_BLIND) freq = toneAt(shownCode, t);
    }
  } else {
    // nessun avviso: lampo breve ogni 3 s, doppio se un incrocio ci sta parlando
    uint32_t p = now % 3000;
    bool on = p < 30 || (linked(now) && p >= 200 && p < 230);
    led = on ? LED_STATUS : LED_OFF;
  }
  vibWrite(vib);
  buzzWrite(freq);
  ledWrite(led);
}

// ======================= ruolo e tasto BOOT =======================
static void saveRole() {
  if (prefs.begin("cross-tag", false)) {
    prefs.putUChar("role", role);
    prefs.end();
  } else {
    Serial.println("[errore] memoria Preferences non disponibile: ruolo non salvato");
  }
}

static void loadRole() {
  role = DEFAULT_ROLE;
  if (prefs.begin("cross-tag", true)) {  // sola lettura: al primo avvio lo spazio non esiste ancora
    role = prefs.getUChar("role", DEFAULT_ROLE);
    prefs.end();
  }
  if (role >= ROLE_COUNT) role = ROLE_PEDESTRIAN;
}

static void applyRadioPower();

static void showRole() {
  Serial.printf("[ruolo] %s (%s): %u vibrazioni\n", ROLE_NAMES[role], ROLE_IT[role], (unsigned)(role + 1));
  feedback(role + 1);
}

static void setRole(uint8_t r) {
  if (r >= ROLE_COUNT) return;
  role = r;
  saveRole();
  clearAdvice();  // gli avvisi ricevuti valevano per il ruolo di prima
  applyRadioPower();
  nextBeacon = millis();  // subito un beacon con il ruolo nuovo
  Serial.print("[ruolo] cambiato -> ");
  showRole();
}

// Antirimbalzo 30 ms; pressione lunga = ruolo successivo, breve = mostra il ruolo
static void buttonTick(uint32_t now) {
#if BUTTON_PIN >= 0
  static bool raw = false, stable = false, longDone = false;
  static uint32_t changedAt = 0, pressedAt = 0;
  bool down = digitalRead(BUTTON_PIN) == LOW;
  if (down != raw) {
    raw = down;
    changedAt = now;
  }
  if (raw != stable && now - changedAt >= 30) {
    stable = raw;
    if (stable) {
      pressedAt = now;
      longDone = false;
    } else if (!longDone) {
      showRole();
    }
  }
  if (stable && !longDone && now - pressedAt >= LONG_PRESS_MS) {
    longDone = true;
    setRole((uint8_t)((role + 1) % ROLE_COUNT));
  }
#else
  (void)now;
#endif
}

// ======================= radio ESP-NOW =======================
// Callback nel task Wi-Fi: solo copia in coda, l'elaborazione avviene nel loop().
static void onRecv(SC_RECV_CB_ARGS) {
  static Frame f;  // chiamata sempre dallo stesso task: basta un buffer statico
  if (data == nullptr || len <= 0 || len > MAX_FRAME) {
    rxTooLong = rxTooLong + 1;  // (niente ++ su volatile: deprecato in C++20)
    return;
  }
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  f.rssi = (info != nullptr && info->rx_ctrl != nullptr) ? (int16_t)info->rx_ctrl->rssi : (int16_t)RSSI_NONE;
#else
  (void)mac;
  f.rssi = RSSI_NONE;
#endif
  f.len = (uint16_t)len;
  memcpy(f.data, data, len);
  if (rxQueue == nullptr || xQueueSend(rxQueue, &f, 0) != pdTRUE) rxDropped = rxDropped + 1;
}

// Ascolto continuo (default) o, con TAG_RADIO_SAVE 1 e core 3.x, finestre di ascolto
// SAVE_WAKE_WINDOW_MS ogni SAVE_WAKE_INTERVAL_MS. Mai per blind ed emergency: per loro un avviso
// perso conta troppo.
static void applyRadioPower() {
#if TAG_RADIO_SAVE && ESP_ARDUINO_VERSION_MAJOR >= 3
  bool save = role != ROLE_BLIND && role != ROLE_EMERGENCY;
  esp_err_t err;
  if (save) {
    err = esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
    if (err == ESP_OK) err = esp_now_set_wake_window(SAVE_WAKE_WINDOW_MS);
    if (err == ESP_OK) err = esp_wifi_connectionless_module_set_wake_interval(SAVE_WAKE_INTERVAL_MS);
  } else {
    err = esp_wifi_set_ps(WIFI_PS_NONE);
  }
  radioSaving = save && err == ESP_OK;
  if (err != ESP_OK) {
    esp_wifi_set_ps(WIFI_PS_NONE);
    Serial.printf("[radio] risparmio non attivo (%s): ascolto continuo\n", esp_err_to_name(err));
  }
#else
  esp_wifi_set_ps(WIFI_PS_NONE);  // ascolto continuo: nessun avviso perso
  radioSaving = false;
#endif
}

static bool startRadio() {
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();     // nessuna rete: il canale resta quello fissato qui sotto
  WiFi.setSleep(false);  // ascolto continuo (vedi applyRadioPower)

  esp_err_t err = ESP_FAIL;
  for (int i = 0; i < 20 && err != ESP_OK; i++) {
    err = esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
    if (err != ESP_OK) delay(50);
  }
  if (err != ESP_OK) Serial.printf("[errore] canale %d non impostato: %s\n", WIFI_CHANNEL, esp_err_to_name(err));

  if (esp_now_init() != ESP_OK) return false;
  esp_now_register_recv_cb(onRecv);

  esp_now_peer_info_t peer;
  memset(&peer, 0, sizeof(peer));
  memcpy(peer.peer_addr, BCAST, 6);
  peer.channel = 0;  // 0 = canale corrente
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;  // il broadcast ESP-NOW non puo' essere cifrato
  esp_err_t perr = esp_now_add_peer(&peer);
  if (perr != ESP_OK && perr != ESP_ERR_ESPNOW_EXIST) return false;
  applyRadioPower();
  return true;
}

static void readBattery(uint32_t now) {
#if BATTERY_ADC_PIN >= 0
  if (lastBatteryRead != 0 && now - lastBatteryRead < 10000) return;
  lastBatteryRead = now;
  uint32_t mv = analogReadMilliVolts(BATTERY_ADC_PIN) * BATTERY_DIVIDER_X10 / 10;
  int pct = ((int)mv - 3300) * 100 / (4200 - 3300);  // LiPo: 3,3 V vuota, 4,2 V carica
  batteryPct = pct < 0 ? 0 : pct > 100 ? 100 : pct;
#else
  (void)now;
  (void)lastBatteryRead;
#endif
}

static int buildBeacon(char *out, int size) {
  char extra[16] = "";
  int n;
  if (role == ROLE_EMERGENCY) {
    if (heading >= 0 && heading < 360) snprintf(extra, sizeof(extra), ",\"hd\":%d", heading);
    n = snprintf(out, size, FMT_EMV, devId, (unsigned long)beaconSeq, extra, siren ? "run" : "off");
  } else {
    if (batteryPct >= 0) snprintf(extra, sizeof(extra), ",\"b\":%d", batteryPct);
    n = snprintf(out, size, FMT_VRU, devId, ROLE_NAMES[role], (unsigned long)beaconSeq, extra);
  }
  return (n > 0 && n < size) ? n : -1;
}

static void beaconTick(uint32_t now) {
  if ((int32_t)(now - nextBeacon) < 0) return;
  uint32_t period = role == ROLE_EMERGENCY ? EMV_MS : BEACON_MS;
  nextBeacon = now + period - BEACON_JITTER_MS + (uint32_t)random(2 * BEACON_JITTER_MS + 1);
  readBattery(now);
  beaconSeq++;
  char buf[128];
  int n = buildBeacon(buf, sizeof(buf));
  if (n <= 0) return;
  esp_err_t err = esp_now_send(BCAST, (const uint8_t *)buf, n);
  if (err == ESP_OK) beaconOk++;
  else beaconErr++;
  if (verbose || (err != ESP_OK && beaconErr <= 3))
    Serial.printf("[beacon] %s%s%s\n", buf, err == ESP_OK ? "" : " -> errore ", err == ESP_OK ? "" : esp_err_to_name(err));
}

// ======================= messaggi ricevuti =======================
static void showAdviceChange(uint32_t now) {
  int k = bestSlot(now);
  uint8_t code = k >= 0 ? slots[k].code : NO_CODE;
  if (code == shownCode) {
    shownSlot = k;
    return;
  }
  shownCode = code;
  shownSlot = k;
  patStart = now;
  if (k < 0) {
    Serial.println("[avviso] nessuno (scaduto o finito)");
    return;
  }
  const AdvSlot &a = slots[k];
  Serial.printf("[avviso] %s (c=%u s=%u) da %s%s x=%d%s%s valido %lu ms\n", CODE_NAMES[a.code], (unsigned)a.code,
                (unsigned)a.sev, a.node, a.direct ? " solo per me" : "", (int)a.x, a.kind[0] ? " k=" : "", a.kind,
                (unsigned long)a.ttl);
}

static void handleMessage(const char *s, int n, int rssi, const char *via) {
  while (n > 0 && (s[n - 1] == '\0' || s[n - 1] == ' ' || s[n - 1] == '\r' || s[n - 1] == '\n')) n--;
  if (n < 2 || s[0] != '{') {
    rxBad++;
    return;
  }
  if (verbose) {
    if (rssi != RSSI_NONE) Serial.printf("[rx %s %d dBm] %.*s\n", via, rssi, n, s);
    else Serial.printf("[rx %s] %.*s\n", via, n, s);
  }
  AdvMsg m;
  uint32_t now = millis();
  if (!parseAdv(s, n, role, devId, &m)) {
    if (m.isAdv) rxNotMine++;
    else rxOther++;  // beacon di altri Tag, hb e hz degli incroci, testo non valido...
    if (verbose || strcmp(via, "seriale") == 0) Serial.printf("[rx %s] ignorato: %s\n", via, m.why);
    return;
  }
  rxAdv++;
  lastAdvAt = now;
  storeAdvice(m, now);
  showAdviceChange(now);
}

// ======================= monitor seriale =======================
static void printHelp() {
  Serial.println("[aiuto] comandi dal Monitor seriale (115200, qualsiasi fine riga):");
  Serial.println("  1..5      ruolo: 1 pedone, 2 non vedente, 3 ciclista, 4 conducente, 5 soccorso");
  Serial.println("  t         avviso di prova per il ruolo (5 s); t<n> = codice n, es. t4 = VEHICLE_LEFT");
  Serial.println("  i oppure ? stato del Tag");
  Serial.println("  s         sirena accesa/spenta (soccorso: campo \"st\" del beacon emv)");
  Serial.println("  d<gradi>  direzione del soccorso (campo \"hd\"), d-1 = non inviata");
  Serial.println("  v         log dettagliato (beacon inviati e messaggi ricevuti) acceso/spento");
  Serial.println("  x         autotest del lettore JSON e dei disegni di vibrazione");
  Serial.println("  {...}     riga JSON trattata come messaggio ricevuto via radio (es. un adv)");
}

static void printStatus() {
  uint32_t now = millis();
  uint8_t ch = 0;
  wifi_second_chan_t sc;
  esp_wifi_get_channel(&ch, &sc);
  Serial.printf("[stato] id=%s ruolo=%s (%s) mac=%s canale=%u core=%d.%d.%d cpu=%luMHz radio=%s\n", devId,
                ROLE_NAMES[role], ROLE_IT[role], WiFi.macAddress().c_str(), (unsigned)ch, ESP_ARDUINO_VERSION_MAJOR,
                ESP_ARDUINO_VERSION_MINOR, ESP_ARDUINO_VERSION_PATCH, (unsigned long)getCpuFrequencyMhz(),
                radioSaving ? "risparmio" : "ascolto continuo");
  Serial.printf("[stato] beacon=%lu errori=%lu adv_per_me=%lu adv_per_altri=%lu altri=%lu non_json=%lu persi=%lu "
                "lunghi=%lu incrocio=%s\n",
                (unsigned long)beaconOk, (unsigned long)beaconErr, (unsigned long)rxAdv, (unsigned long)rxNotMine,
                (unsigned long)rxOther, (unsigned long)rxBad, (unsigned long)rxDropped, (unsigned long)rxTooLong,
                linked(now) ? "collegato" : "nessuno");
  int k = bestSlot(now);
  if (k >= 0) {
    const AdvSlot &a = slots[k];
    Serial.printf("[stato] avviso=%s da %s x=%d s=%u resta %lu ms\n", CODE_NAMES[a.code], a.node, (int)a.x,
                  (unsigned)a.sev, (unsigned long)(a.ttl - (uint32_t)(now - a.at)));
  } else {
    Serial.println("[stato] avviso=nessuno");
  }
  if (role == ROLE_EMERGENCY) Serial.printf("[stato] sirena=%s direzione=%d\n", siren ? "run" : "off", heading);
  Serial.printf("[stato] motorino=GPIO%d buzzer=GPIO%d%s batteria=%d%%\n", (int)VIBRO_PIN, (int)BUZZER_PIN,
                BUZZER_ACTIVE ? " (attivo)" : "", batteryPct);
}

static void selfTest() {
  int fails = 0, total = 0;
  for (size_t i = 0; i < sizeof(PARSE_CASES) / sizeof(PARSE_CASES[0]); i++) {
    const ParseCase &tc = PARSE_CASES[i];
    AdvMsg m;
    bool ok = parseAdv(tc.json, (int)strlen(tc.json), tc.role, tc.dev, &m);
    bool pass = ok ? (tc.code == (int16_t)m.code && m.ttl == (uint32_t)tc.ttl) : (tc.code < 0);
    total++;
    if (!pass) fails++;
    Serial.printf("[%s] caso %u (%s): %s%s%s\n", pass ? "OK " : "ERR", (unsigned)(i + 1), ROLE_NAMES[tc.role],
                  ok ? "accettato " : "scartato: ", ok ? CODE_NAMES[m.code] : m.why, pass ? "" : " <- atteso diverso");
  }
  struct {
    bool got, want;
    const char *what;
  } checks[] = {
    {vibAt(C_WAIT, 10), true, "WAIT acceso all'inizio"},
    {vibAt(C_WAIT, 1500), false, "WAIT spento a 1,5 s"},
    {vibAt(C_WAIT, 2010), true, "WAIT ripete dopo 2 s"},
    {vibAt(C_EMERGENCY_APPROACHING, 150), false, "EMERGENCY spento a 150 ms (5 Hz)"},
    {vibAt(C_EMERGENCY_APPROACHING, 250), true, "EMERGENCY acceso a 250 ms"},
    {vibAt(C_CORRIDOR_BUSY, 10), false, "CORRIDOR_BUSY senza vibrazione"},
    {toneAt(C_VEHICLE_LEFT, 100) < toneAt(C_VEHICLE_LEFT, 500), true, "VEHICLE_LEFT tono che sale"},
    {toneAt(C_VEHICLE_RIGHT, 100) > toneAt(C_VEHICLE_RIGHT, 500), true, "VEHICLE_RIGHT tono che scende"},
    {toneAt(C_EMERGENCY_APPROACHING, 100) != toneAt(C_EMERGENCY_APPROACHING, 500), true, "sirena a due toni"},
    {toneAt(C_HAZARD_AHEAD, 100) == 0, true, "HAZARD_AHEAD senza suono"},
    {adviceRank(C_WAIT, 1) > adviceRank(C_CROSS_OK, 3), true, "WAIT batte sempre CROSS_OK"},
  };
  for (size_t i = 0; i < sizeof(checks) / sizeof(checks[0]); i++) {
    bool pass = checks[i].got == checks[i].want;
    total++;
    if (!pass) fails++;
    Serial.printf("[%s] %s\n", pass ? "OK " : "ERR", checks[i].what);
  }
  Serial.printf("[autotest] %d/%d superati%s\n", total - fails, total, fails ? " - QUALCOSA NON VA" : "");
}

// Avviso di prova: passa dallo stesso percorso di un adv vero (slot "prova")
static void testAdvice(int code) {
  if (code < 0) {
    static const uint8_t DEF[ROLE_COUNT] = {C_WAIT, C_WAIT, C_BIKE_LANE_BLOCKED, C_PEDESTRIAN_AHEAD, C_CORRIDOR_BUSY};
    code = DEF[role];
  }
  if (code >= CODE_COUNT) {
    Serial.printf("[prova] codice %d sconosciuto (0-%d)\n", code, CODE_COUNT - 1);
    return;
  }
  AdvMsg m;
  memset(&m, 0, sizeof(m));
  m.code = (uint8_t)code;
  m.sev = 2;
  m.x = -1;
  m.ttl = TEST_TTL_MS;
  strcpy(m.node, "prova");
  uint32_t now = millis();
  Serial.printf("[prova] avviso %s per %d s (ruolo %s)\n", CODE_NAMES[code], TEST_TTL_MS / 1000, ROLE_NAMES[role]);
  storeAdvice(m, now);
  showAdviceChange(now);
}

static void handleCommand(char *s, int len) {
  while (len > 0 && (s[len - 1] == ' ' || s[len - 1] == '\t')) s[--len] = '\0';
  while (len > 0 && (*s == ' ' || *s == '\t')) {
    s++;
    len--;
  }
  if (len == 0) return;
  if (s[0] == 'R') {  // riga copiata dall'uscita della radio: "R-63 {...}"
    char *b = strchr(s, '{');
    if (b != nullptr) {
      len -= (int)(b - s);
      s = b;
    }
  }
  if (s[0] == '{') {
    handleMessage(s, len, RSSI_NONE, "seriale");
    return;
  }
  char c = s[0];
  if (c >= '1' && c <= '5' && len == 1) setRole((uint8_t)(c - '1'));
  else if (c == 't') testAdvice(len > 1 ? atoi(s + 1) : -1);
  else if (c == 'i' || c == '?') printStatus();
  else if (c == 's') {
    siren = !siren;
    Serial.printf("[soccorso] sirena %s\n", siren ? "accesa (st=run)" : "spenta (st=off)");
  } else if (c == 'd') {
    heading = len > 1 ? atoi(s + 1) : -1;
    if (heading < 0 || heading >= 360) heading = -1;
    Serial.printf("[soccorso] direzione %d%s\n", heading, heading < 0 ? " (non inviata)" : " gradi");
  } else if (c == 'v') {
    verbose = !verbose;
    Serial.printf("[log] dettagliato %s\n", verbose ? "acceso" : "spento");
  } else if (c == 'x') selfTest();
  else if (c == 'h') printHelp();
  else Serial.println("[?] comando sconosciuto: h = aiuto");
}

static void serialTick(uint32_t now) {
  int budget = 256;  // non monopolizza il loop
  while (budget-- > 0 && Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    serLastByte = now;
    if (c == '\n' || c == '\r') {
      if (!serOverflow && serLen > 0) {
        serLine[serLen] = '\0';
        handleCommand(serLine, serLen);
      } else if (serOverflow) {
        Serial.printf("[errore] riga oltre %d caratteri, scartata\n", LINE_BUF - 1);
      }
      serLen = 0;
      serOverflow = false;
    } else if (serOverflow) {
      // resto della riga troppo lunga: scartato
    } else if (serLen >= LINE_BUF - 1) {
      serOverflow = true;
    } else {
      serLine[serLen++] = (char)c;
    }
  }
  // monitor senza "a capo": la riga si esegue dopo una breve pausa
  if (serLen > 0 && now - serLastByte >= SERIAL_IDLE_MS) {
    if (!serOverflow) {
      serLine[serLen] = '\0';
      handleCommand(serLine, serLen);
    }
    serLen = 0;
    serOverflow = false;
  }
}

// ======================= setup / loop =======================
void setup() {
#if VIBRO_PIN >= 0
  pinMode(VIBRO_PIN, OUTPUT);  // subito: motorino spento
  digitalWrite(VIBRO_PIN, LOW);
#endif
#if BUZZER_PIN >= 0
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);
#endif
#if LED_GREEN_PIN >= 0
  pinMode(LED_GREEN_PIN, OUTPUT);
#endif
#if LED_RED_PIN >= 0
  pinMode(LED_RED_PIN, OUTPUT);
#endif
#if !LED_RGB
  if (PIN_LED >= 0) pinMode(PIN_LED, OUTPUT);
#endif
  ledWrite(LED_OFF);
#if BUTTON_PIN >= 0
  pinMode(BUTTON_PIN, INPUT_PULLUP);
#endif
#if TAG_CPU_MHZ > 0
  setCpuFrequencyMhz(TAG_CPU_MHZ);  // prima di Serial.begin: la seriale parte gia' alla frequenza giusta
#endif
  Serial.begin(115200);
  delay(200);

  loadRole();
  rxQueue = xQueueCreate(RX_QUEUE_LEN, sizeof(Frame));
  if (rxQueue == nullptr || !startRadio()) {
    Serial.println("[errore] ESP-NOW non avviato: riavvio tra 5 s");
    delay(5000);
    ESP.restart();
  }
  // id del dispositivo dal MAC (ultimi 3 byte): stabile, lo stesso a ogni accensione
  uint8_t mac[6] = {0, 0, 0, 0, 0, 0};
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  snprintf(devId, sizeof(devId), "tag-%02x%02x%02x", mac[3], mac[4], mac[5]);

  Serial.printf("\n[avvio] CROSS Tag SC-P2P v2 id=%s ruolo=%s (%s) canale=%d core=%d.%d.%d\n", devId,
                ROLE_NAMES[role], ROLE_IT[role], WIFI_CHANNEL, ESP_ARDUINO_VERSION_MAJOR, ESP_ARDUINO_VERSION_MINOR,
                ESP_ARDUINO_VERSION_PATCH);
  Serial.println("[avvio] BOOT lungo = cambia ruolo, BOOT breve = mostra ruolo, h = aiuto");
  showRole();  // N vibrazioni = ruolo attuale
  nextBeacon = millis() + (uint32_t)random(200);
  lastStatus = millis();
}

void loop() {
  static Frame f;
  for (int i = 0; i < 4 && rxQueue != nullptr && xQueueReceive(rxQueue, &f, 0) == pdTRUE; i++)
    handleMessage((const char *)f.data, f.len, f.rssi, "radio");
  uint32_t now = millis();
  serialTick(now);
  buttonTick(now);
  beaconTick(now);
  showAdviceChange(now);  // avvisi scaduti: si passa al successivo o si spegne tutto
  outputsTick(now);
  if (STATUS_EVERY_MS > 0 && now - lastStatus >= STATUS_EVERY_MS) {
    lastStatus = now;
    printStatus();
  }
  delay(4);  // la CPU riposa: il disegno delle vibrazioni ha comunque una risoluzione di pochi ms
}
