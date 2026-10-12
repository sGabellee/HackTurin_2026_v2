/*
  CROSS v2 - sketch MCU (STM32U585 della Arduino UNO Q, core Zephyr)
  Specifica: docs/ARCHITETTURA.md, par. 2.3. Lato Python: src/node/python/mcu.py e signals.py.

  Cosa fa:
    - accende i 4 LED di stato (D2..D5) e fino a 4 canali luce sopra le strisce (D6, D9, D10, D11, PWM);
    - legge il Modulino Distance (VL53L4CD) sul bordo delle strisce a 10 Hz: e' il SENSORE DI RISVEGLIO
      del nodo (il Python dorme finche' qualcuno non si ferma sul bordo);
    - riflesso locale: se il sensore i vede una persona e il canale i non e' comandato dal Python
      (comando scaduto), accende SUBITO il canale i in "unknown" (lampeggio lento 25 %): la luce non
      aspetta l'avvio della camera;
    - fail-safe "cervello offline": se per ttl_ms non arriva nessun set_signals, LED di pericolo spenti,
      LED4 lampo breve ogni 2 s, canali luce guidati solo dai sensori (riflesso locale).

  Funzioni per il Python (Bridge.provide, chiamate con Bridge.call da un thread a parte):
    set_signals(int leds, int xwalk, int ttl_ms) -> 0
        leds:  2 bit per LED -> LED1 bit 0-1, LED2 bit 2-3, LED3 bit 4-5, LED4 bit 6-7
               modo 0 spento, 1 acceso, 2 lampeggio lento (1 Hz), 3 lampeggio veloce (4 Hz)
        xwalk: 4 bit per canale -> canale 0 bit 0-3, canale 1 bit 4-7, canale 2 bit 8-11, canale 3 bit 12-15
               codici luce: 0 off            spenta
                            1 on             accesa fissa
                            2 attention      lampeggio lento 1 Hz
                            3 danger         lampeggio veloce 4 Hz
                            4 assist         "respiro" lento (PWM 20 -> 100 %, periodo 2 s)
                            5 emergency      doppio lampo ogni 1 s
                            6 unknown        lampeggio lento con duty 25 %
               un codice 7..15 (non valido) viene mostrato come unknown: nel dubbio, prudenza
        ttl_ms: validita' del comando (massimo 60 s). Il Python lo rimanda ogni ~1 s anche se non cambia.
    get_sensors() -> int
        bit 0-3: presenza sul bordo i; bit 4-7: sensore i funzionante; bit 8: sketch pronto
    get_distance(int i) -> int
        distanza misurata dal sensore i in mm, -1 se niente davanti, sensore guasto o indice sbagliato

  Ruoli dei LED (config.json -> signals.roles = ["hazard", "cyclist", "person", "emergency"]):
    LED1 (D2) ROSSO   pericolo (lento se da un vicino)   LED2 (D3) VERDE  ciclista davanti alla camera
    LED3 (D4) GIALLO  persona davanti alla camera        LED4 (D5) BLU    emergenza (lampeggio veloce)
  Per spostare un colore su un altro pin basta cambiare l'ordine di signals.roles, non lo sketch.

  All'avvio: test dei LED uno dopo l'altro (200 ms ciascuno) e dei canali luce (150 ms ciascuno),
  poi LED 4 fisso finche' il Bridge non e' pronto (se resta fisso, il router Linux non risponde).
  Il riflesso locale parte quando loop() gira, cioe' dopo Bridge.begin(): Bridge.begin() aspetta che il
  router di Linux risponda, quindi all'accensione (Linux che si avvia) per qualche decina di secondi
  il riflesso non c'e'. Dopo, se Linux o il Python si fermano, loop() continua e il fail-safe lavora.

  =====================================================================================================
  SCHEMA DEI COLLEGAMENTI
  =====================================================================================================
  LED DI STATO - "ANODO COMUNE" (COMMON_ANODE true, quello della nostra breadboard)
    gamba lunga (anodo) di tutti i LED -> binario comune -> pin 3.3V della board
    gamba corta (catodo) -> resistenza 220-330 ohm -> pin D2 / D3 / D4 / D5
    Il LED si accende quando il pin va a GND (LOW). Da spento il pin resta "scollegato" (INPUT).
    Meglio 3.3V che 5V sul binario comune: i pin dell'MCU lavorano a 3,3 V. Con il 5V i LED rossi/gialli
    possono restare leggermente accesi da spenti e il pin riceve una tensione piu' alta della sua.
  LED DI STATO - "CATODO COMUNE" (COMMON_ANODE false)
    pin D2..D5 -> resistenza -> gamba lunga; gamba corta -> GND.
  ATTENZIONE: i pin dell'MCU sono a 3,3 V e reggono poca corrente: stare sotto ~8 mA per pin.
    LED rosso/giallo (~2 V) con 220-330 ohm -> circa 4-6 mA: ok. MAI sotto ~150 ohm.
    LED blu/bianchi (~3 V) a 3,3 V restano comunque deboli: meglio rossi, gialli o verdi.

  LUCI SOPRA LE STRISCE (1-4 canali, strisce LED a 12 V) - un MOSFET N "logic level" per canale
    pin D6 / D9 / D10 / D11 -> resistenza 100-220 ohm -> GATE del MOSFET
    GATE -> resistenza 100 kohm -> GND        (pull-down: striscia spenta mentre l'MCU si avvia)
    SOURCE -> GND                             (GND dell'alimentatore 12 V IN COMUNE con GND della board)
    DRAIN  -> negativo (-) della striscia LED
    +12 V dell'alimentatore -> positivo (+) della striscia LED
    MOSFET che conducono bene con 3,3 V sul gate: es. IRLB8721, AO3400 (SMD), IRLML6344 (SMD) o un
    modulo "MOSFET per Arduino" logic level. NON un IRF540/IRFZ44 normale (serve 10 V sul gate).
    Il pin alto (HIGH) accende la striscia; se il vostro modulo e' invertito: XWALK_INVERT true.

  SENSORE DI BORDO - Modulino Distance (VL53L4CD, fino a ~1,3 m)
    cavo Qwiic -> connettore Qwiic della UNO Q (bus I2C Wire1, 3,3 V). Montato sul palo delle strisce,
    rivolto verso il punto in cui il pedone aspetta (altezza ~1 m, in orizzontale).
    Presenza = distanza < PRESENCE_MM (900 mm) per 3 letture di fila; assenza dopo 10 letture sopra soglia.
    Sensore assente o guasto -> bit "funzionante" a 0 e presenza a 0: il Python NON va a riposo
    (sicurezza prima del risparmio). Se il sensore viene ricollegato lo sketch lo reinizializza da solo.
    Per usare i pin SDA/SCL della testata invece del Qwiic: CURB_WIRE Wire.

  PIU' SENSORI (N_CURBS > 1, struttura gia' pronta: un elemento degli array per bordo, bordo i -> canale i)
    Tutti i VL53L4CD si accendono all'indirizzo I2C 0x29: due sensori sullo stesso bus vanno in conflitto.
    Servono indirizzi diversi: o si cambia l'indirizzo a ogni accensione (VL53L4CD_SetI2CAddress, tenendo
    spenti gli altri con il loro pin XSHUT; con la libreria Modulino tutti i ModulinoDistance usano 0x29,
    quindi per questo serve la classe VL53L4CD di STM32duino), o si usa un multiplexer I2C (es. TCA9548A),
    o si mette ogni sensore su un bus diverso. Poi basta scrivere gli indirizzi in CURB_ADDR.
  =====================================================================================================
*/
#include <Arduino_RouterBridge.h>
#include <Arduino_Modulino.h>   // libreria Arduino_Modulino (nelle versioni vecchie si chiamava <Modulino.h>)

// ---------------------------------------------------------------------- configurazione
#define CURB_WIRE Wire1         // bus I2C dei sensori: Wire1 = connettore Qwiic della UNO Q
#define N_CURBS 1               // sensori di bordo collegati (1..4); il sensore i guida il canale luce i
static_assert(N_CURBS >= 1 && N_CURBS <= 4, "N_CURBS deve essere tra 1 e 4");
const uint8_t CURB_ADDR[N_CURBS] = {0x29};   // indirizzo I2C di ogni sensore (vedi PIU' SENSORI)

const int LED_PINS[4] = {2, 3, 4, 5};
const int XWALK_PINS[4] = {6, 9, 10, 11};    // PWM
// true: anodi sul binario comune (3.3V), catodi -> resistenza -> pin. false: pin -> LED -> GND
const bool COMMON_ANODE = true;
const bool XWALK_INVERT = false;             // true se il modulo MOSFET accende con il pin BASSO

const uint32_t DEFAULT_TTL_MS = 3000;
const uint32_t MAX_TTL_MS = 60000;           // un comando non vale mai piu' di 60 s (come mcu.py MAX_TTL_MS)
const uint32_t OFFLINE_PERIOD_MS = 2000;     // fail-safe: un lampo del LED 4 ogni 2 s...
const uint32_t OFFLINE_FLASH_MS = 150;       // ...lungo 150 ms

const int32_t PRESENCE_MM = 900;             // presenza: piu' vicino di 90 cm...
const uint8_t PRESENCE_ON_READS = 3;         // ...per 3 letture di fila (0,3 s)
const uint8_t PRESENCE_OFF_READS = 10;       // assenza dopo 10 letture sopra soglia (1 s)
const uint32_t SENSOR_PERIOD_MS = 100;       // 10 Hz
const uint32_t PROBE_PERIOD_MS = 1000;       // ogni secondo controllo che il sensore risponda sul bus
const uint32_t RETRY_MAX_MS = 60000;         // sensore che risponde ma non si inizializza: riprovo fino a ogni 60 s

// codici luce (tenere allineati a mcu.py LIGHT_CODES)
enum LightCode { XW_OFF = 0, XW_ON = 1, XW_ATTENTION = 2, XW_DANGER = 3, XW_ASSIST = 4, XW_EMERGENCY = 5, XW_UNKNOWN = 6 };

// ---------------------------------------------------------------------- stato condiviso con il Bridge
// Scritti dal thread del Bridge, letti da loop(): volatile e solo valori a 32 bit.
// cmd_seq e' dispari mentre set_signals scrive: loop() copia il comando solo quando e' pari e uguale
// prima e dopo la copia, cosi' non mescola mai i LED di un comando con le luci di un altro.
volatile uint32_t cmd_seq = 0;
volatile uint32_t cmd_leds = 0;
volatile uint32_t cmd_xwalk = 0;
volatile uint32_t cmd_ttl_ms = DEFAULT_TTL_MS;
volatile uint32_t cmd_at_ms = 0;
volatile uint32_t cmd_received = 0;

// Scritti da loop(), letti dal thread del Bridge (get_sensors / get_distance)
volatile uint32_t sensors_word = 0;          // bit 8 (pronto) a 0 finche' loop() non ha letto i sensori
volatile int32_t distance_mm[4] = {-1, -1, -1, -1};

// ---------------------------------------------------------------------- stato di loop()
struct Command { uint32_t leds, xwalk, ttl_ms, at_ms, received; };
Command cmd = {0, 0, DEFAULT_TTL_MS, 0, 0};  // ultima copia coerente del comando

struct Curb {
  bool ok;               // sensore presente e inizializzato
  bool present;          // presenza (con isteresi)
  uint8_t near_reads;    // letture di fila sotto soglia
  uint8_t far_reads;     // letture di fila sopra soglia (o senza nulla davanti)
  int32_t mm;            // ultima distanza valida, -1 se niente
  uint32_t next_probe_ms;
  uint32_t retry_ms;
};
ModulinoDistance curb_sensor[N_CURBS];
Curb curb[N_CURBS];

bool led_level[4] = {false, false, false, false};  // ultimo stato scritto sui pin dei LED
int xwalk_duty[4] = {-1, -1, -1, -1};              // ultimo duty scritto sui canali (-1 = mai)
uint32_t next_sensor_ms = 0;

// ---------------------------------------------------------------------- funzioni per il Python
// Con Bridge.provide girano nel thread del Bridge (stack piccolo): solo salvare o leggere valori,
// niente stampe, niente delay, niente Bridge.call, niente I2C.
int set_signals(int leds, int xwalk, int ttl_ms) {
  cmd_seq = cmd_seq + 1;                       // dispari: scrittura in corso
  cmd_leds = (uint32_t)leds & 0xFF;
  cmd_xwalk = (uint32_t)xwalk & 0xFFFF;
  // ttl fuori misura (0, negativo, enorme) non deve mai spegnere il fail-safe
  cmd_ttl_ms = ttl_ms <= 0 ? DEFAULT_TTL_MS : ((uint32_t)ttl_ms > MAX_TTL_MS ? MAX_TTL_MS : (uint32_t)ttl_ms);
  cmd_at_ms = millis();
  cmd_received = 1;
  cmd_seq = cmd_seq + 1;                       // pari: comando completo
  return 0;
}

int get_sensors() {
  return (int)sensors_word;
}

int get_distance(int i) {
  if (i < 0 || i >= 4) return -1;
  return (int)distance_mm[i];
}

// ---------------------------------------------------------------------- uscite
// Livello del LED per un modo, calcolato da millis(): stessi modi lampeggiano in fase
bool mode_level(uint32_t mode, uint32_t now) {
  switch (mode) {
    case 1: return true;
    case 2: return (now % 1000) < 500;  // lento, 1 Hz
    case 3: return (now % 250) < 125;   // veloce, 4 Hz
    default: return false;
  }
}

// Duty PWM (0..255) di un canale luce per un codice (uguale a light_duty() di mcu.py)
int light_duty(uint32_t code, uint32_t now) {
  switch (code) {
    case XW_OFF: return 0;
    case XW_ON: return 255;
    case XW_ATTENTION: return (now % 1000) < 500 ? 255 : 0;
    case XW_DANGER: return (now % 250) < 125 ? 255 : 0;
    case XW_ASSIST: {                           // respiro: 20 % -> 100 % -> 20 % in 2 s
      uint32_t f = now % 2000;
      uint32_t tri = f < 1000 ? f : 2000 - f;   // 0..1000
      return 51 + (int)((204 * tri) / 1000);
    }
    case XW_EMERGENCY: {                        // doppio lampo ogni secondo
      uint32_t f = now % 1000;
      return (f < 100 || (f >= 200 && f < 300)) ? 255 : 0;
    }
    default:                                    // XW_UNKNOWN e codici non validi: prudenza
      return (now % 1000) < 250 ? 255 : 0;
  }
}

// Accende/spegne il LED sul pin, qualunque sia il collegamento
void drive(int pin, bool on) {
  if (COMMON_ANODE) {
    if (on) {
      pinMode(pin, OUTPUT);
      digitalWrite(pin, LOW);   // il pin porta il catodo a GND: passa corrente
    } else {
      pinMode(pin, INPUT);      // pin "scollegato": nessuna corrente, anche con il binario a 5V
    }
  } else {
    digitalWrite(pin, on ? HIGH : LOW);
  }
}

void write_led(int i, bool on) {
  if (led_level[i] != on) {
    led_level[i] = on;
    drive(LED_PINS[i], on);
  }
}

void write_xwalk(int ch, int duty) {
  if (xwalk_duty[ch] != duty) {
    xwalk_duty[ch] = duty;
    analogWrite(XWALK_PINS[ch], XWALK_INVERT ? 255 - duty : duty);
  }
}

// ---------------------------------------------------------------------- sensori di bordo
bool i2c_present(uint8_t addr) {
  CURB_WIRE.beginTransmission(addr);
  return CURB_WIRE.endTransmission() == 0;
}

void curb_reset(int i) {
  curb[i].present = false;
  curb[i].near_reads = 0;
  curb[i].far_reads = 0;
  curb[i].mm = -1;
}

// Inizializza il sensore i solo se risponde sul bus. begin() della libreria crea ogni volta un nuovo
// oggetto per il sensore senza liberare il vecchio: non la chiamo a vuoto ogni secondo quando il sensore
// non c'e', solo quando ricompare sul bus (ogni ricollegamento costa qualche centinaio di byte di RAM).
void curb_init(int i, uint32_t now) {
  curb_reset(i);
  bool there = i2c_present(CURB_ADDR[i]);
  curb[i].ok = there && curb_sensor[i].begin();
  if (curb[i].ok || !there) {
    curb[i].retry_ms = PROBE_PERIOD_MS;     // ok, oppure assente: controllo il bus ogni secondo (costa poco)
  } else {
    // risponde ma non parte: riprovo sempre piu' di rado
    curb[i].retry_ms = curb[i].retry_ms * 2 > RETRY_MAX_MS ? RETRY_MAX_MS : curb[i].retry_ms * 2;
  }
  curb[i].next_probe_ms = now + curb[i].retry_ms;
}

// Una lettura (10 Hz) con l'isteresi: presenza dopo 3 letture sotto soglia, assenza dopo 10 sopra.
// available() della libreria e' true se l'ultima misura e' valida (il sensore misura da solo ogni 20 ms,
// quindi a 10 Hz c'e' sempre una misura nuova); false = niente davanti entro la portata.
// Se il sensore si stacca, available() ripete l'ultima misura: per questo read_sensors() controlla
// ogni secondo che risponda ancora sul bus.
void curb_read(int i) {
  Curb &c = curb[i];
  int32_t mm = -1;
  if (curb_sensor[i].available()) {
    float d = curb_sensor[i].get();
    // d == d: non e' NaN (NaN = niente davanti). d > 0: prima della prima misura la libreria ha il
    // risultato tutto a zero (stato "valido", 0 mm): non e' una persona attaccata al sensore.
    if (d == d && d > 0) mm = (int32_t)d;
  }
  c.mm = mm;
  if (mm >= 0 && mm < PRESENCE_MM) {
    c.far_reads = 0;
    if (c.near_reads < 255) c.near_reads++;
    if (c.near_reads >= PRESENCE_ON_READS) c.present = true;
  } else {
    c.near_reads = 0;
    if (c.far_reads < 255) c.far_reads++;
    if (c.far_reads >= PRESENCE_OFF_READS) c.present = false;
  }
}

void read_sensors(uint32_t now) {
  uint32_t bits = 0;
  for (int i = 0; i < N_CURBS; i++) {
    Curb &c = curb[i];
    if ((int32_t)(now - c.next_probe_ms) >= 0) {
      if (c.ok) {
        c.next_probe_ms = now + PROBE_PERIOD_MS;
        if (!i2c_present(CURB_ADDR[i])) {     // staccato o guasto: niente presenza, il Python resta sveglio
          c.ok = false;
          c.retry_ms = PROBE_PERIOD_MS;
          curb_reset(i);
        }
      } else {
        curb_init(i, now);                    // assente: provo a ritrovarlo (anche se collegato dopo l'avvio)
      }
    }
    if (c.ok) {
      curb_read(i);
      bits |= 1u << (4 + i);
      if (c.present) bits |= 1u << i;
    } else {
      curb_reset(i);
    }
    distance_mm[i] = c.mm;
  }
  bits |= 1u << 8;                            // sketch pronto: i sensori sono stati letti almeno una volta
  sensors_word = bits;
}

// ---------------------------------------------------------------------- comando del Python
// Copia coerente del comando (vedi cmd_seq). Se set_signals sta scrivendo proprio adesso tengo la
// copia precedente: al prossimo giro di loop() sara' completa.
void read_command() {
  for (int tries = 0; tries < 3; tries++) {
    uint32_t s1 = cmd_seq;
    if (s1 & 1u) continue;
    Command c;
    c.leds = cmd_leds;
    c.xwalk = cmd_xwalk;
    c.ttl_ms = cmd_ttl_ms;
    c.at_ms = cmd_at_ms;
    c.received = cmd_received;
    if (cmd_seq == s1) {
      cmd = c;
      return;
    }
  }
}

// ---------------------------------------------------------------------- setup e loop
void setup() {
  for (int i = 0; i < 4; i++) {
    if (!COMMON_ANODE) pinMode(LED_PINS[i], OUTPUT);
    drive(LED_PINS[i], false);
    pinMode(XWALK_PINS[i], OUTPUT);
    write_xwalk(i, 0);
  }
  // Test del cablaggio: ogni LED 200 ms, poi ogni canale luce 150 ms, uno dopo l'altro
  for (int i = 0; i < 4; i++) {
    drive(LED_PINS[i], true);
    delay(200);
    drive(LED_PINS[i], false);
  }
  for (int i = 0; i < 4; i++) {
    write_xwalk(i, 255);
    delay(150);
    write_xwalk(i, 0);
  }
  // Sensori prima del Bridge: sono pronti appena il Python li chiede
  Modulino.begin(CURB_WIRE);
  for (int i = 0; i < N_CURBS; i++) {
    curb[i].retry_ms = PROBE_PERIOD_MS;
    curb_init(i, millis());
  }
  // LED 4 fisso mentre aspetto il router Linux (Bridge.begin aspetta la sua risposta)
  drive(LED_PINS[3], true);
  led_level[3] = true;
  Bridge.begin();
  Bridge.provide("set_signals", set_signals);
  Bridge.provide("get_sensors", get_sensors);
  Bridge.provide("get_distance", get_distance);
  next_sensor_ms = millis();
}

void loop() {
  uint32_t now = millis();
  if ((int32_t)(now - next_sensor_ms) >= 0) {
    next_sensor_ms = now + SENSOR_PERIOD_MS;
    read_sensors(now);
  }

  // Prima copio il comando, poi leggo millis(): cosi' cmd.at_ms non e' mai "nel futuro"
  read_command();
  now = millis();
  // differenza senza segno: ok anche quando millis() riparte da 0
  bool alive = cmd.received && (now - cmd.at_ms) < cmd.ttl_ms;

  for (int i = 0; i < 4; i++) {
    bool on;
    if (alive) {
      on = mode_level((cmd.leds >> (2 * i)) & 0x3, now);
    } else {
      on = (i == 3) && (now % OFFLINE_PERIOD_MS) < OFFLINE_FLASH_MS;  // cervello offline
    }
    write_led(i, on);
  }

  for (int ch = 0; ch < 4; ch++) {
    uint32_t code;
    if (alive) {
      code = (cmd.xwalk >> (4 * ch)) & 0xF;        // decide il Python (crosswalk.py)
    } else {
      // riflesso locale: qualcuno sul bordo e nessun comando valido -> prudenza subito
      bool present = ch < N_CURBS && curb[ch].ok && curb[ch].present;
      code = present ? XW_UNKNOWN : XW_OFF;
    }
    write_xwalk(ch, light_duty(code, now));
  }
  // niente delay: dopo ogni loop() il core chiama il Bridge (__loopHook) che cede la CPU
}
