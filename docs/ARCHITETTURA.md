# CROSS v2 — Architettura (specifica di riferimento)

Questo documento è il contratto tra tutte le parti del progetto: nodo (Python sulla UNO Q), MCU (sketch
STM32), radio ESP32, CROSS Tag (dispositivo personale), collettore (server secondario) e dashboard.
Se codice e documento non coincidono, va corretto il codice o va aggiornato il documento: mai lasciarli
divergere.

## 0. Principi

1. **Offline prima di tutto.** Ogni incrocio funziona da solo, senza internet, senza server e senza router.
   Gli incroci si parlano **peer-to-peer** via radio (ESP-NOW tramite un ESP32 "radio" collegato in USB);
   gli utenti della strada portano un **CROSS Tag** (ESP32) che parla con l'incrocio più vicino sulla
   stessa radio. Per sviluppo e simulazione lo stesso protocollo viaggia su UDP o su un bus radio virtuale.
2. **Poca energia.** Il nodo resta **a riposo** (camera e intelligenza artificiale ferme, LED spenti,
   radio in ascolto) finché un **sensore** non lo sveglia: il Modulino Distance sul palo delle strisce vede
   una persona ferma sul bordo. Altri risvegli sono configurabili (radio di un Tag vicino o di un mezzo di
   soccorso, pericolo diretto qui da un incrocio vicino) e per default **attivi**: arrivano dalla radio che è
   comunque accesa. Se il sensore di risveglio è guasto il nodo **non dorme** (sicurezza prima del risparmio).
3. **Gli utenti della strada al centro.** Il valore principale è parlare con chi è in strada: pedoni, pedoni
   non vedenti, ciclisti, conducenti, mezzi di soccorso. Ognuno riceve avvisi diversi (vibrazioni, suoni,
   luci) e le **luci sopra l'attraversamento** si accendono quando qualcuno sta per attraversare,
   lampeggiando più forte se un veicolo si avvicina.
4. **Mai un falso "via libera".** Il sistema dice "puoi attraversare" solo se la camera è sveglia, ha un
   fotogramma recente e nessun veicolo si avvicina entro la soglia di sicurezza su nessun accesso che taglia
   quelle strisce. Se non può saperlo dice "attenzione", mai "via libera".
5. **Server secondario.** Il collettore non riceve niente in automatico: quando serve **va a chiedere** i dati
   ai nodi (pull). Il nodo conserva tutto nel suo archivio finché qualcuno non lo scarica.
6. **360 gradi.** Un nodo può avere più camere (una per accesso, o una fisheye); le tracce delle camere sono
   fuse in una sola vista dell'incrocio. Ogni accesso è legato a una direzione reale (gradi bussola) e quindi
   agli incroci vicini.
7. **Una sola dashboard, locale.** Una pagina in una colonna, stile editoriale, angoli vivi, nessuna libreria
   esterna: funziona senza internet. Mostra la mappa di tutti gli incroci della zona (da OpenStreetMap).

## 1. Hardware

### 1.1 Incrocio
| Parte | Collegamento | Ruolo |
|---|---|---|
| Arduino UNO Q 4GB | — | Linux (QRB2210): visione, regole, rete. MCU (STM32U585): sensori e luci in tempo reale |
| Camere (1–4) | UNO Media Carrier (IMX219) o USB | 360°: una per accesso o una fisheye |
| Modulino Distance (VL53L4CD) | Qwiic (I2C, `Wire1`) | presenza sul bordo delle strisce (fino a ~1,3 m): **sensore di risveglio** |
| 4 LED di stato | D2–D5, anodo comune a 3,3 V | ruoli `hazard`, `cyclist`, `person`, `emergency` |
| Luci dell'attraversamento (1–4 canali) | D6, D9, D10, D11 (PWM) → MOSFET logic-level → strisce LED 12 V | luce sopra le strisce |
| ESP32 "radio" | USB seriale (`/dev/ttyACM0` o `/dev/ttyUSB0`) | ESP-NOW ↔ seriale, canale Wi-Fi fisso 1 |

### 1.2 CROSS Tag (dispositivo personale)
ESP32-C3 SuperMini (o ESP32 DevKit/S3): motorino vibrante via transistor NPN su GPIO 4, LED su GPIO 8
(SuperMini, attivo basso) o LED della scheda, buzzer opzionale su GPIO 5 (avvisi sonori per non vedenti),
pulsante BOOT per scegliere il ruolo (pressione lunga), alimentazione a batteria LiPo.
Ruoli: `pedestrian`, `blind`, `cyclist`, `driver`, `emergency`.

## 2. Il nodo (Python, `src/node/python/`)

Solo libreria standard di Python 3.11 (gira nel container di App Lab). Ogni modulo non deve mai far
cadere il nodo: errori loggati una volta, si continua senza quella parte.

### 2.1 Mappa dei moduli
| Modulo | Responsabilità |
|---|---|
| `main.py` | ingresso App Lab: camere, `Node`, ciclo di tick, avvio/arresto dell'inferenza secondo lo stato di energia |
| `node.py` | collega tutto (vedi §2.9) |
| `config.py` | carica e valida `config.json` (§2.12), segreti da `.env` |
| `power.py` | **nuovo** — stati di energia e risvegli (§2.2) |
| `mcu.py` | **nuovo** — dialogo con lo sketch via Bridge: sensori e comando unico delle luci (§2.3) |
| `signals.py` | **nuovo** (sostituisce `leds.py`) — 4 LED di stato + canali delle luci dell'attraversamento (§2.4) |
| `situation.py` | regole sulle tracce; **in più** più strisce per camera e la vista `crossing_view()` (§2.5) |
| `crosswalk.py` | **nuovo** — stato di ogni attraversamento e luce da accendere (§2.6) |
| `users.py` | **nuovo** — registro dei Tag vicini e avvisi per ruolo (§2.7) |
| `peer.py` | protocollo radio **SC-P2P v2** con trasporti `udp`, `serial`, `sim` (§3) |
| `route.py` | **nuovo** — topologia della città e instradamento (§4) |
| `fusion.py` | fusione delle camere (360°) |
| `events.py` | catalogo degli eventi (+ nuovi tipi, §2.8) |
| `storage.py` | archivio SQLite + **cursori di export** per il collettore (§5.1) |
| `stats.py`, `health.py`, `tracker.py`, `geometry.py`, `safety.py`, `traffic_light.py` | come prima |
| `api.py` | API HTTP locale in sola lettura + `/api/export` per il pull (§5.1) |
| `publisher.py` | solo archivio locale, log `events.jsonl` e Web UI di App Lab (niente più invio HTTP) |

Rimossi: `remote.py` (casella del server), `wearable.py` (bracciale UDP della v1), `leds.py`
(diventa `signals.py`), `web/app.html` (pagina telefono legata al server).

### 2.2 Energia — `power.py`
```python
class PowerManager:
    def __init__(self, cfg: dict, on_change=None)   # cfg = config["power"]
    state: str                    # "sleep" | "active" | "cooldown"
    awake: bool                   # active o cooldown: la camera lavora
    def trigger(self, source: str, t: float, hold_s: float | None = None, detail=None) -> bool
    def activity(self, t: float)  # qualcosa di rilevante in scena: resta sveglio
    def tick(self, t: float)      # transizioni a tempo
    def stats(self, t: float) -> dict
```
- **Sorgenti di risveglio** (`source`): `curb` (sensore sul bordo), `radio_vru` (Tag vicino con ruolo in
  `wake_roles`), `radio_emergency` (mezzo di soccorso), `radio_hazard` (pericolo diretto qui da un vicino),
  `manual`, `schedule`, `boot`, `sensor_fault`.
- **Transizioni:** `sleep --trigger--> active`; `active --(nessuna attività per active_hold_s)--> cooldown`;
  `cooldown --(cooldown_s senza attività)--> sleep`; un trigger o un'attività in `cooldown` riporta ad `active`.
  `hold_s` (es. 60 s per un mezzo di soccorso) prolunga lo stato attivo almeno fino a `t + hold_s`.
- **Risveglio programmato** (`sample_every_s` > 0): ogni N s si sveglia per `sample_s` s (statistiche).
- **Guasto del sensore:** `trigger("sensor_fault")` tiene il nodo sempre attivo finché il sensore non torna
  (`clear_fault(t)`); con `power.enabled = false` il nodo è sempre attivo.
- **Stima dell'energia:** `stats()` = `{"state", "since_ms", "wakeups", "by_source": {...}, "seconds":
  {"sleep","active","cooldown"}, "energy_wh", "always_on_wh", "saved_wh", "saved_pct"}` con i consumi di
  `power_w` (default `sleep` 2.5 W, `active` 9.0 W, `cooldown` 9.0 W).
- `on_change(old, new, source, t)` chiamato a ogni cambio di stato.

### 2.3 MCU — `mcu.py` e sketch
Funzioni esposte dallo sketch (`Bridge.provide`), chiamate dal Python con `Bridge.call` in un thread a
parte (le chiamate sono bloccanti, il nodo non deve mai fermarsi):

| Funzione | Argomenti → ritorno | Significato |
|---|---|---|
| `set_signals` | `(int leds, int xwalk, int ttl_ms)` → `0` | comando unico: 4 LED + fino a 4 canali luce strisce |
| `get_sensors` | `()` → `int` | bit 0–3: presenza sul bordo `i`; bit 4–7: sensore `i` funzionante; bit 8: sketch pronto |
| `get_distance` | `(int i)` → `int` | distanza misurata dal sensore `i` in mm, `-1` se niente / guasto |

- `leds`: 2 bit per LED (LED1 bit 0–1 … LED4 bit 6–7), modi `0` spento, `1` acceso, `2` lento (1 Hz),
  `3` veloce (4 Hz). Uguale alla v1.
- `xwalk`: **4 bit per canale** (canale 0 bit 0–3 … canale 3 bit 12–15). Codici luce:

| Codice | Nome | Resa sulla striscia | Quando |
|---|---|---|---|
| 0 | `off` | spenta | nessuno |
| 1 | `on` | accesa fissa | qualcuno aspetta o attraversa, nessun veicolo vicino |
| 2 | `attention` | lampeggio lento 1 Hz | qualcuno aspetta/attraversa e un veicolo si avvicina (ETA ≤ `safe_eta_s`) |
| 3 | `danger` | lampeggio veloce 4 Hz | conflitto: veicolo con ETA ≤ `conflict_eta_s` mentre qualcuno è sulle strisce o sul bordo |
| 4 | `assist` | "respiro" lento (PWM 20→100 %) | un utente non vedente attraversa assistito, nessun conflitto |
| 5 | `emergency` | doppio lampo ogni 1 s | un mezzo di soccorso arriva: i pedoni aspettano |
| 6 | `unknown` | lampeggio lento con duty 25 % | qualcuno sul bordo ma la camera non è ancora pronta: prudenza |

- **Riflesso locale dello sketch:** se il sensore `i` vede una presenza e il canale `i` non è comandato dal
  Python (comando scaduto), lo sketch accende **subito** il canale `i` in `unknown` (6): la luce non aspetta
  l'avvio della camera.
- **Fail-safe:** se per `ttl_ms` non arriva nessun `set_signals`, lo sketch passa in autonomia: LED di
  pericolo spenti, LED4 lampo breve ogni 2 s, canali luce guidati solo dai sensori (riflesso locale).
- **Sensori:** Modulino Distance letto a 10 Hz; presenza = distanza < `PRESENCE_MM` (900) per 3 letture di
  fila; assenza dopo 10 letture sopra soglia. Sensore assente o in errore → bit "funzionante" a 0.

`mcu.py`:
```python
class MCULink:
    def __init__(self, bridge, cfg: dict, start_thread=True)   # cfg = config["mcu"]
    def poll_sensors(self) -> dict | None   # {"ready": bool, "curbs": [{"id": i, "present": bool, "ok": bool}]}
    def distance(self, i: int) -> int | None
    def send_signals(self, leds: int, xwalk: int, ttl_ms: int)  # mai bloccante (ultimo valore vince)
    def state(self) -> dict
```
Senza Bridge (PC, test, simulazione) si usa un MCU finto con la stessa interfaccia (`FakeMCU` nei test e nel
simulatore).

### 2.4 Segnali — `signals.py`
Sostituisce `leds.py` mantenendone il comportamento sui 4 LED (ruoli `hazard`, `cyclist`, `person`,
`emergency`, `status`, ecc., spegnimento appena la causa sparisce, pericoli remoti fino a scadenza) e
aggiungendo i canali luce:
```python
class SignalController:
    def __init__(self, cfg: dict, mcu)          # cfg = config["signals"]
    def on_hazard(self, msg, local: bool, t)
    def set_presence(self, classes, t, tracks=())
    def set_crosswalks(self, codes: dict[int, int])   # canale -> codice luce (§2.3)
    def push(self, t=None)                      # invia subito se cambiato
    def tick(self, t, online)                   # keep-alive ogni refresh_s
    def state(self) -> dict                     # {"leds": {...}, "crosswalks": {canale: nome}}
```
A riposo (`sleep`) i LED di presenza e pericolo locale sono spenti; restano attivi i pericoli remoti di
categoria `emergency` e le luci delle strisce comandate da `crosswalk.py`.

### 2.5 Situazione — `situation.py`
- **Più attraversamenti per camera:** sono strisce tutte le zone il cui nome inizia per `crosswalk`
  (`crosswalk`, `crosswalk_n`, …) e aree d'attesa quelle che iniziano per `waiting_area`. Le regole che oggi
  usano la zona `crosswalk` devono valere per ognuna.
- **Nuova vista** per gli attraversamenti:
```python
def crossing_view(self, t: float, crosswalk_zone: str, waiting_zone: str | None,
                  left: str | None, right: str | None) -> dict
# {"fresh": bool,                         # fotogramma negli ultimi 1.0 s
#  "waiting": int, "crossing": int,       # pedoni (person) in attesa / sulle strisce
#  "blind_like": False,                   # (riservato: la visione non riconosce i non vedenti)
#  "cyclists": int,
#  "vehicles": [{"track": id, "class": "vehicle|emergency", "eta_s": float|None,
#                "side": "left|right|unknown", "speed": float|None}],   # veicoli diretti alle strisce
#  "min_eta_s": float|None}
```
  ETA = distanza dalla linea delle strisce / velocità verso le strisce (metri se calibrata, altrimenti
  frazioni d'immagine con la velocità in scala). Solo veicoli che si **avvicinano**. `side` dagli accessi:
  il veicolo che arriva dall'accesso `left` è a sinistra.

### 2.6 Attraversamenti — `crosswalk.py`
```python
class CrosswalkController:
    def __init__(self, cfg: dict, node_id: str)   # cfg = config completo (usa "crosswalks" e "users")
    def update(self, t, views: dict, curbs: dict, users: dict, emergency: dict | None, camera_ready: bool) -> list
    # views:  id -> risultato di crossing_view (o None se la camera dorme)
    # curbs:  id -> {"present": bool, "ok": bool}
    # users:  id -> {"blind": int, "pedestrians": int}  Tag vicini a quell'attraversamento
    # emergency: {"eta_s": float|None, "from": node|None} se un mezzo di soccorso arriva, altrimenti None
    # ritorna la lista degli stati: [{"id","state","light","safe","reason","min_eta_s","since_ms"}]
    def states(self) -> list
    def light_codes(self) -> dict[int, int]       # canale -> codice luce
```
Stati e luce (dal più grave): `emergency` (5) > `conflict` (3) > `attention` (2) > `assist` (4) >
`unknown` (6) > `occupied` (1) > `idle` (0).
- Qualcuno c'è se: presenza sul bordo **oppure** camera con `waiting + crossing > 0` **oppure** Tag
  `blind`/`pedestrian` vicino a quell'attraversamento.
- `conflict` se qualcuno c'è e `min_eta_s ≤ conflict_eta_s` (default 4 s); `attention` se `≤ safe_eta_s`
  (default 8 s); `unknown` se qualcuno c'è ma la camera non è pronta o il fotogramma non è fresco.
- `safe = True` solo se: camera pronta e fresca, nessun veicolo con ETA ≤ `safe_eta_s`, nessuna emergenza.
- Gli stati restano almeno `min_state_s` (default 1.5 s) per non sfarfallare, tranne i passaggi verso uno
  stato più grave che sono immediati.

### 2.7 Utenti della strada — `users.py`
```python
class UserLink:
    def __init__(self, cfg: dict, node_id: str, send)   # send(obj) trasmette un oggetto SC-P2P
    def on_beacon(self, msg: dict, t)       # "vru_beacon" o "emergency_beacon" normalizzati da peer.py
    def near(self, t) -> list               # Tag vicini: [{"device","role","rssi","age_s","crosswalk"}]
    def counts_by_crosswalk(self, t) -> dict
    def advise(self, t, crossings: list, hazards: list, emergency: dict | None, camera_ready: bool) -> list
    def state(self) -> dict
```
- Un Tag è **vicino** se `rssi ≥ near_rssi` (default −75 dBm) o se arriva da un trasporto senza RSSI
  (UDP/sim con `"near": true`). Scade dopo `user_timeout_s` (default 6 s).
- `advise` produce messaggi `adv` (§3.2) **solo se ci sono Tag vicini**: subito quando il consiglio cambia,
  poi ogni `adv_period_s` (default 1 s). Un messaggio per ruolo (più quelli rivolti a un singolo
  dispositivo). Il Tag tiene valido un avviso per `ttl` ms (default 3000).
- **Codici di avviso** (`c`):

| c | Nome | Per chi | Significato |
|---|---|---|---|
| 0 | `CLEAR` | tutti | niente da segnalare |
| 1 | `WAIT` | pedone, non vedente | non attraversare |
| 2 | `CROSS_OK` | pedone, non vedente | puoi attraversare (solo con `safe = True`) |
| 3 | `KEEP_GOING` | pedone, non vedente | sei sulle strisce: continua |
| 4 | `VEHICLE_LEFT` | pedone, non vedente, ciclista | veicolo in arrivo da sinistra |
| 5 | `VEHICLE_RIGHT` | pedone, non vedente, ciclista | veicolo in arrivo da destra |
| 6 | `PEDESTRIAN_AHEAD` | conducente | pedoni sulle strisce o in attesa all'incrocio |
| 7 | `CYCLIST_AHEAD` | conducente | ciclisti all'incrocio |
| 8 | `BIKE_LANE_BLOCKED` | ciclista | auto nella ciclabile o ferma in ciclabile |
| 9 | `EMERGENCY_APPROACHING` | tutti tranne `emergency` | mezzo di soccorso in arrivo: pedoni aspettano, veicoli liberano |
| 10 | `CORRIDOR_CLEAR` | soccorso | incrocio libero |
| 11 | `CORRIDOR_BUSY` | soccorso | pedoni o veicoli nell'incrocio |
| 12 | `HAZARD_AHEAD` | conducente, ciclista | pericolo segnalato qui o in arrivo (campo `k`) |
| 13 | `ASSIST_UNAVAILABLE` | non vedente | l'incrocio non può vedere (camera ferma o guasta): non affidarti al sistema |
| 14 | `WRONG_WAY` | tutti | veicolo contromano |

- **Regole per il non vedente** (le più importanti): `CROSS_OK` solo con `safe = True`; se è sulle strisce
  `KEEP_GOING`, ma `VEHICLE_LEFT/RIGHT` se un veicolo è in conflitto; se la camera non è pronta
  `ASSIST_UNAVAILABLE` (e l'attraversamento resta in `unknown`); con un mezzo di soccorso `EMERGENCY_APPROACHING`.
  Un Tag `blind` vicino sveglia il nodo e porta l'attraversamento in `assist`.
- **Mezzo di soccorso:** quando un Tag `emergency` è vicino il nodo gli manda `CORRIDOR_CLEAR` o
  `CORRIDOR_BUSY`; a tutti gli altri `EMERGENCY_APPROACHING`.

### 2.8 Nuovi eventi — `events.py`
| kind | categoria | gravità tipica | inoltro | quando |
|---|---|---|---|---|
| `crossing_conflict` | risk | 2–3 | no | un attraversamento entra in `conflict` |
| `assisted_crossing` | info | 1 | no | un utente non vedente viene assistito (inizio/fine) |
| `emergency_corridor` | emergency | 3 | sì (lungo la direzione) | un mezzo di soccorso (Tag o camera) passa: corridoio verso i prossimi incroci |
| `wake_sensor_fault` | info | 1 | no | il sensore di risveglio non risponde: il nodo resta sveglio |

### 2.9 Il nodo — `node.py`
Ciclo di vita (il `tick` gira ogni `tick_s`, default 0.25 s; `process` a ogni fotogramma):
1. `tick`: legge i sensori (`mcu.poll_sensors`, al massimo ogni `mcu.sensor_poll_s`) → presenza sul
   bordo = `power.trigger("curb")`; sensore guasto = `power.trigger("sensor_fault")` + evento
   `wake_sensor_fault` una volta.
2. `power.tick(t)`; se lo stato cambia, `main.py` avvia/ferma l'inferenza (callback `on_power`).
3. Viste degli attraversamenti (`crossing_view` per ogni attraversamento se la camera è sveglia),
   `crosswalk.update(...)`, `signals.set_crosswalks(crosswalk.light_codes())`,
   `users.advise(...)`, `signals.tick(...)`.
4. `process(detections, camera, t)`: se il nodo dorme **scarta** i fotogrammi (non li elabora); se è sveglio
   come la v1 (motore, statistiche, presenze, LED) e `power.activity(t)` se ci sono tracce confermate.
5. Pericoli locali (`hazard`): archivio, segnali, radio (`peer.send_hazard` con destinatari calcolati da
   `route.targets_for`, §4).
6. Messaggi radio in arrivo (§3): `remote_hazard` → segnali e archivio; se diretto qui e `wake_on_radio`,
   `power.trigger("radio_hazard")`; `vru_beacon`/`emergency_beacon` → `users.on_beacon` e, se previsto,
   `power.trigger(...)`; un `emergency_beacon` fa partire anche il **corridoio**: evento
   `emergency_corridor` inoltrato lungo la direzione del mezzo (rotta da `route.targets_for` con la sua
   direzione `hd`), così gli incroci successivi si svegliano prima che arrivi.
7. `summary_request` dal collettore via radio → risposta `rs` (§3.2).

### 2.10 Camere e 360°
`config["cameras"]` contiene più camere (`cam0`…`cam3`), ognuna con zone, corsie, accessi e
`approach_bearing` (accesso → gradi bussola, 0 = nord). Con più camere `fusion.py` unisce i movimenti tra
camere. `main.py` crea un `VideoObjectDetection` per camera quando App Lab lo permette; altrimenti una sola
camera (fisheye) con tutte le zone.

### 2.11 Avvio e arresto dell'inferenza — `main.py`
`on_power(old, new)`: se `new == "sleep"` prova `detector.stop()`, se `new` è sveglio prova
`detector.start()`. Se il brick non ha questi metodi, i fotogrammi arrivano comunque ma `node.process`
li scarta (si risparmia l'elaborazione Python; il risparmio pieno richiede lo stop del brick).

### 2.12 `config.json` (v2)
```jsonc
{
  "node_id": "incrocio-42",
  "position": {"lat": 45.06536, "lon": 7.66689},
  "topology": "topology.json",                       // copia locale della topologia della città
  "cameras": {"cam0": {"frame_size": [640, 480], "zones": {...}, "lanes": {...}, "approaches": {...},
               "approach_bearing": {"via": 225, "incrocio": 90}}},
  "crosswalks": [{"id": 0, "camera": "cam0", "zone": "crosswalk", "waiting": "waiting_area",
                  "curb": 0, "light": 0, "left": "via", "right": "incrocio"}],
  "power":   {"enabled": true, "active_hold_s": 20, "cooldown_s": 5, "boot_active_s": 30,
              "wake_on_radio": true, "wake_roles": ["blind", "emergency"],
              "sample_every_s": 0, "sample_s": 10, "power_w": {"sleep": 2.5, "active": 9.0, "cooldown": 9.0}},
  "mcu":     {"sensor_poll_s": 0.25, "curbs": 1, "distance_poll_s": 2.0},
  "signals": {"roles": ["hazard", "cyclist", "person", "emergency"], "channels": 1, "ttl_ms": 3000, "refresh_s": 1.0},
  "users":   {"near_rssi": -75, "user_timeout_s": 6, "adv_period_s": 1.0, "adv_ttl_ms": 3000,
              "safe_eta_s": 8, "conflict_eta_s": 4, "min_state_s": 1.5},
  "p2p":     {"enabled": true, "transports": ["serial", "udp"], "serial": {"port": "/dev/ttyACM0", "baud": 115200},
              "group": "239.255.42.99", "port": 47999, "peers": [], "key": "", "hb_sleep_s": 10, "hb_active_s": 5},
  "api":     {"enabled": true, "port": 8100},
  "data":    {"store_db": true, "retention_days": 30, "stats_interval_s": 60},
  "thresholds": {}, "rules": {}, "traffic_light": {"mode": "none"}
}
```
Rimossi rispetto alla v1: `server`, `publish.url/token/mqtt`, `wearable`, `neighbors` (ora dalla topologia),
`leds` (ora `signals`).

## 3. Protocollo radio SC-P2P v2 — `peer.py`

### 3.1 Regole generali
- Un messaggio = un oggetto JSON compatto UTF-8 di **massimo 240 byte** (sta in un frame ESP-NOW da 250).
- Campo comune: `"v": 2`, `"t"` = tipo. Firma opzionale `sig` come nella v1 (HMAC-SHA256 dei campi
  ordinati esclusi `sig` e `h`, primi 16 caratteri hex) se c'è una chiave condivisa; i Tag non firmano.
- **Trasporti** (uno o più per nodo): `serial` (ESP32 radio in USB: una riga JSON per messaggio; in
  ricezione la radio antepone `R<rssi> ` alla riga, es. `R-63 {"v":2,...}`), `udp` (multicast
  239.255.42.99:47999 TTL 1 + peer unicast), `sim` (bus radio virtuale del simulatore: consegna ai nodi entro
  la portata `range_m` con RSSI calcolato dalla distanza). Il nodo che riceve da un trasporto **rilancia**
  sugli altri (ponte).
- **Dedup:** `(id, a)` ricordati per max(scadenza, 60 s). I messaggi senza `id` (beacon) non si deduplicano.
- **Rilancio:** se `h > 0` e mai visto, il nodo ritrasmette con `h - 1`. I Tag non rilanciano mai.

### 3.2 Tipi di messaggio
| t | Da → a | Campi | Note |
|---|---|---|---|
| `hb` | nodo → nodi, collettore | `n`, `ts`, `pw` (`s`/`a`/`c` = stato di energia), `h:0` | ogni `hb_sleep_s` a riposo, `hb_active_s` sveglio |
| `hz` | nodo → nodi | `id`, `n`, `a` (`n` nuovo / `c` fine), `k`, `c`, `s`, `ts`, `e`, `hd` (direzione in gradi, opz.), `to` (lista nodi destinatari, opz.), `tg` (profili interessati, opz.), `h` | consegnato se il nodo è in `to`; senza `to` lo consegnano i vicini del mittente. `h` = lunghezza del percorso + 1 per arrivare anche a radio |
| `vru` | Tag → nodi | `d` (id dispositivo), `p` (ruolo), `q` (contatore), `b` (batteria %, opz.), `hd`/`sp` (direzione/velocità, opz.) | ogni 1 s; nessun orologio sul Tag |
| `emv` | Tag soccorso → nodi | `d`, `q`, `hd` (gradi, opz.), `nx` (prossimo nodo, opz.), `st` (`run` sirena accesa) | ogni 0,5 s |
| `adv` | nodo → Tag | `n`, `id`, `r` (lista ruoli) o `to` (id dispositivo), `c` (codice §2.7), `s` (1–3), `x` (attraversamento), `k` (tipo di pericolo, opz.), `ttl` (ms) | il Tag mostra l'avviso più grave per il suo ruolo |
| `rq` | collettore → nodi | `id`, `w` (`sum`) | richiesta di riepilogo via radio |
| `rs` | nodo → collettore | `id` (della richiesta), `n`, `p` (riepilogo compatto: `pw`, `hz` pericoli attivi, `u` Tag vicini, `ev` eventi da ultimo export, `wk` risvegli oggi) | risposta, `h:0` |

### 3.3 Messaggi normalizzati verso il nodo (`on_message`)
`remote_hazard` (come v1 + `to`, `heading`), `vru_beacon` (`device`, `role`, `rssi`, `near`, `seq`,
`battery`, `heading`, `speed`, `via`), `emergency_beacon` (`device`, `heading`, `next_node`, `siren`,
`rssi`, `near`, `via`), `summary_request` (`id`). Gli `adv` di altri nodi vengono ignorati dal nodo.

### 3.4 API
```python
class PeerLink:
    def __init__(self, cfg, node_id, on_message, start_thread=True, sim_bus=None, position=None)
    def send_hazard(self, msg, targets=None, hops=None) -> bool
    def send_obj(self, obj, keep=()) -> bool          # adv, rs, ... (già nel formato v2)
    def send_heartbeat(self, power_state="a") -> bool
    def poll_once(self, timeout=0.0) -> int
    def peers(self) -> dict                           # node_id -> {"last_seen","pw","rssi","via"}
    online: bool
    def state(self) -> dict
    def close(self)
class SimRadioBus:                                     # per il simulatore e i test
    def __init__(self, range_m=180.0)
    def attach(self, endpoint_id, position, deliver)   # deliver(bytes, rssi)
    def move(self, endpoint_id, position)
    def broadcast(self, endpoint_id, data: bytes)
```

## 4. Topologia e instradamento — `route.py` e `tools/build_topology.py`

`topology.json` (generato da OpenStreetMap, © OpenStreetMap contributors, licenza ODbL):
```jsonc
{"version": 2, "source": "OpenStreetMap (ODbL)", "bbox": [s, w, n, e], "generated": "...",
 "nodes": {"incrocio-42": {"name": "Via Vincenzo Vela / Corso Stati Uniti", "lat": 45.065, "lon": 7.667,
           "osm": 123456, "streets": ["Via Vincenzo Vela", "Corso Stati Uniti"],
           "neighbors": {"incrocio-17": {"bearing": 223.5, "street": "Corso Stati Uniti", "m": 92.0}}}},
 "edges": [{"a": "incrocio-42", "b": "incrocio-17", "street": "Corso Stati Uniti", "m": 92.0,
            "geom": [[45.065, 7.667], [45.0649, 7.6668]]}]}
```
`route.py`:
```python
class Topology:
    @classmethod
    def load(cls, path) -> "Topology"
    def ids(self) -> list
    def pos(self, nid) -> tuple | None
    def neighbors(self, nid) -> list
    def bearing(self, a, b) -> float
    def nearest(self, lat, lon) -> str | None
    def first_toward(self, src, heading_deg, max_turn=100) -> str | None
    def along(self, src, first, hops, max_turn=100) -> list
    def path(self, src, dst) -> list                     # percorso più breve in metri, estremi inclusi
    def targets_for(self, src, heading_deg=None, category="risk", speed_kmh=None, dst=None) -> list
    # direzione nota -> 2 incroci lungo la strada (3 per emergenze o veicoli > 40 km/h)
    # direzione ignota -> tutti i vicini; dst -> tutti gli incroci del percorso più breve
```
Il nodo trasforma l'accesso visto dalla camera (es. `towards: "via"`) in gradi con `approach_bearing`.

## 5. Raccolta dati — il collettore (server secondario, `src/collector/`)

### 5.1 Lato nodo: pull
`GET /api/export?cursor=<c>&limit=<n>` (default 500, max 2000) restituisce in ordine di arrivo i messaggi
salvati dopo il cursore: `{"node", "cursor": "<nuovo>", "more": bool, "items": [{"table", "rid", "ts",
"msg"}]}`. Il cursore è opaco per il collettore (es. `e120.t45.s7.u9` = ultimo rid per tabella). Il nodo
non cancella nulla per l'export: vale la `retention_days`. Più `GET /api/status` (con energia,
attraversamenti, Tag vicini, peer) e `GET /api/health`.

### 5.2 Collettore — `collector.py`
- Solo libreria standard. `python collector.py --config collector.json --port 8000`.
- `collector.json`: `{"topology": "...", "nodes": {"incrocio-42": "http://192.168.1.50:8100", ...},
  "pull": {"mode": "manual" | "interval", "interval_s": 600}, "listen": {"udp": true, "serial": ""},
  "db": "collector.db"}`.
- **Pull**: su richiesta (`POST /api/pull`, opzionale `{"nodes": [...]}`) o a intervallo, per ogni nodo:
  `/api/status` + `/api/export` finché `more`; salva in SQLite; tiene cursore e ora dell'ultimo pull.
  Un nodo irraggiungibile non blocca gli altri (timeout breve, tentativi in parallelo).
- **Ascolto passivo** (opzionale): si unisce al multicast UDP (o a una radio ESP32 sulla seriale) senza
  trasmettere: vede `hb`, `hz`, `vru`, `emv`, `adv` per la vista "in diretta". Può inviare `rq` per i
  riepiloghi via radio.
- API per la dashboard: `GET /api/overview`, `/api/map`, `/api/hazards`, `/api/users`, `/api/crosswalks`,
  `/api/energy`, `/api/events?after=`, `/api/nodes`, `POST /api/pull`, `GET /api/pull`.

### 5.3 Dashboard — `src/collector/dashboard.html`
- **Una sola pagina, una sola colonna**, blocchi uno sotto l'altro, larghezza massima ~1100 px.
- Stile editoriale (riferimento: politicometro.vercel.app): testata blu notte a tutta larghezza con i numeri
  principali; ogni sezione apre con una riga orizzontale spessa e un titolo; etichette in maiuscoletto
  spaziato; numeri grandi e leggeri; **angoli vivi ovunque** (`border-radius: 0`); niente ombre, niente
  card arrotondate, niente colonne affiancate. Font: `"Titillium Web", system-ui, sans-serif` (se il font
  non è installato si usa quello di sistema: nessun download).
- Sezioni, in ordine: testata con i numeri (incroci, svegli ora, a riposo, pericoli in corso, Tag in strada,
  energia risparmiata); **mappa della città** (SVG da `topology.json`: strade, tutti gli incroci, colore per
  stato, percorsi dei pericoli animati tratto per tratto, Tag vicini; clic su un incrocio = scheda sotto la
  mappa); pericoli in corso; attraversamenti; utenti della strada; energia; raccolta dati (pull per nodo,
  pulsante "Scarica ora"); registro eventi.
- Nessuna risorsa esterna, nessuna libreria: funziona offline.

## 6. CROSS Tag e radio (firmware, `src/firmware/`)
- `esp32_radio/`: ponte trasparente ESP-NOW ↔ seriale (dalla v1 `esp32_gateway`), canale 1, in ricezione
  antepone `R<rssi> ` alla riga.
- `esp32_tag/`: un solo firmware per tutti i ruoli. Beacon `vru` ogni 1 s (`emv` ogni 0,5 s per il ruolo
  `emergency`), riceve `adv` (anche via `to` = proprio id) e mostra l'avviso più grave valido per il suo
  ruolo con vibrazione/LED/suono:

| Codice | Vibrazione | Suono (non vedente) |
|---|---|---|
| `WAIT` | impulso lungo ogni 2 s | tono basso lento |
| `CROSS_OK` | due impulsi brevi ogni 1,5 s | tono alto ripetuto |
| `KEEP_GOING` | tic leggero ogni 1 s | tic |
| `VEHICLE_LEFT` / `VEHICLE_RIGHT` | 3 impulsi rapidi (sx) / 4 impulsi rapidi (dx) | tono che sale / scende |
| `EMERGENCY_APPROACHING` | vibrazione continua a scatti 5 Hz | sirena a due toni |
| `BIKE_LANE_BLOCKED`, `HAZARD_AHEAD`, `PEDESTRIAN_AHEAD`, `CYCLIST_AHEAD`, `WRONG_WAY` | 3 impulsi medi | — |
| `ASSIST_UNAVAILABLE` | impulso lungo + pausa + impulso lungo, una volta ogni 5 s | voce/tono di errore |
| `CORRIDOR_CLEAR` / `CORRIDOR_BUSY` (soccorso) | LED verde / rosso | — |

  Ruolo scelto con pressione lunga di BOOT (salvato in memoria), comandi dal monitor seriale per le prove.

## 7. Simulazione — `src/node/tools/simulate_city.py`
Fa girare in un solo processo **tutti** i nodi della topologia con il codice vero (`Node`), un `FakeMCU`
per ciascuno, il bus radio virtuale (portata realistica, più salti) e Tag virtuali in movimento (pedone,
non vedente, ciclista, conducente, ambulanza). Gli scenari generano detection finte solo per i nodi
svegli. Espone l'API locale di ogni nodo su `http://127.0.0.1:<porta>/n/<node_id>/api/...` così il
collettore li scarica come nodi veri. Scenari: `blind_crossing`, `ambulance_route`, `bike_lane`,
`pedestrian_night` (risveglio dal bordo), `demo` (tutti in sequenza).

## 8. Prove
`src/node/tools/test_suite.py` copre: energia (stati, risvegli, guasto sensore), segnali (LED + codici
luce), attraversamenti (stati, `safe`, mai via libera con camera ferma), utenti (avvisi per ruolo, ritmo,
non vedente, soccorso), protocollo v2 (dimensioni, firma, dedup, rilancio, ponte tra trasporti, bus
virtuale a più salti), instradamento, nodo intero, export/pull del collettore, simulazione breve di città.
