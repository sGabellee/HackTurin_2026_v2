# CROSS v2 - guida pratica

Come provare tutto sul PC (senza hardware), poi come montare e avviare un incrocio vero.
Il riferimento tecnico è [ARCHITETTURA.md](ARCHITETTURA.md); i passi ancora da fare sono in [PIANO_V2.md](PIANO_V2.md).

Interprete: Python 3.11 con la sola libreria standard (nessun `pip install` per il nodo, il collettore e il simulatore).
Negli esempi `python` è quello che usate voi (sul PC del progetto: `C:\Users\Gabriele\Desktop\Hackaton\dataset\.venv\Scripts\python.exe`).

## 1. Provare tutto sul PC (5 minuti)

### 1.1 Test
```bash
python src/node/tools/run_tests.py      # tutti i test (13 script, circa 2 minuti)
```
Singoli: `test_suite.py` (regole, configurazione, radio UDP, API) in `src/node/tools`; in `src/node/tools/tests`: `test_node.py`
(il nodo intero: energia, strisce, Tag, corridoio di emergenza), `test_users.py` (avvisi ai Tag per ruolo), `test_crosswalk.py`
(stati delle strisce e luci: mai via libera senza vedere), `test_city.py` (simulatore), e gli altri per modulo.
Ogni script stampa `[OK ]`/`[ERR]` e termina con codice 1 se qualcosa fallisce.

### 1.2 La città simulata con la dashboard
Terminale 1, il simulatore (tutti i 462 incroci della mappa, MCU finti, radio virtuale, Tag virtuali):
```bash
cd src/node/tools
python simulate_city.py --scenario demo --loop --udp
```
Terminale 2, il collettore (server secondario) con la dashboard:
```bash
cd src/collector
python collector.py --config collector_sim.json --port 8000 --pull
```
Aprite <http://127.0.0.1:8000>. `collector_sim.json` viene scritto dal simulatore all'avvio.
Sulla mappa vedete gli incroci a riposo (grigi) che si svegliano al passaggio degli attori: il non vedente alla
board, l'ambulanza lungo un percorso di 2 km, l'auto che invade la ciclabile, il pedone di notte.
Con `--fast` il tempo è accelerato (per i test), senza `--loop` lo scenario gira una volta sola.
Scenari: `blind_crossing`, `ambulance_route`, `bike_lane`, `pedestrian_night`, `demo`, `idle`.
Con `--nodes 60` si simulano solo i 60 incroci più vicini alla board (più leggero).

### 1.3 Un video vero sul nodo (serve `opencv-python` e `ultralytics`)
```bash
python replay.py video.mp4 --snapshot foto.png        # primo fotogramma per disegnare le zone
python zone_editor.py foto.png ..\python\config.json  # disegna strisce, aree d'attesa, ciclabile
python replay.py video.mp4                            # il nodo completo sul video, con LED e luce delle strisce
```

## 2. Costruire un incrocio

### 2.1 Cosa serve
| Parte | Note |
|---|---|
| Arduino UNO Q 4GB | con App Lab; modello di visione già installato (vedi [TUTORIAL.md](TUTORIAL.md), fasi 1-3) |
| Camera USB o UNO Media Carrier | una per accesso, oppure una fisheye |
| Modulino Distance + cavo Qwiic | sensore di risveglio, sul palo delle strisce, a circa 1 m da terra |
| 4 LED (rosso, verde, giallo, blu) + 4 resistenze 220-330 ohm | stato dell'incrocio, D2-D5 |
| Strisce LED 12 V + MOSFET logic level + alimentatore 12 V | luce sopra le strisce, D6 (poi D9, D10, D11 per altre strisce) |
| ESP32 (C3 SuperMini, S3 o DevKit) con cavo USB | radio dell'incrocio, sulla USB della UNO Q |
| Un ESP32 per ogni persona che vuole il Tag | più motorino vibrante, NPN, diodo, buzzer (non vedenti) |

Lo schema completo dei collegamenti dei LED, del MOSFET e del sensore è in testa a `src/node/sketch/sketch.ino`.

### 2.2 Firmware ESP32
Radio dell'incrocio e Tag: vedi [src/firmware/README.md](../src/firmware/README.md) (Arduino IDE, schede, collegamenti,
tabella delle vibrazioni). Compilano con il core Arduino-ESP32 2.x e 3.x. Provate prima la radio da sola e il Tag da solo
dal monitor seriale (comandi `t`, `i`, `1`..`5`), poi insieme.

### 2.3 Sketch dell'MCU e nodo Python con App Lab
1. Creare un'app in App Lab e copiare: `src/node/python/*` (con `topology.json` e `config.json`) nella cartella `python/` dell'app,
   `src/node/sketch/sketch.ino` nella cartella `sketch/`.
2. Se App Lab mostra "No apps yet", manca `sketch/sketch.yaml`: copiarlo dagli esempi della board
   (`/var/lib/arduino-app-cli/examples/core-and-foundational/03-bridge-basics/01-call-sketch-function-from-python/sketch/sketch.yaml`).
3. Nello sketch servono le librerie `Arduino_RouterBridge` e `Arduino_Modulino`; il bus del sensore è `Wire1` (`CURB_WIRE`).
4. Nell'app (`app.yaml`) dichiarare la porta dell'API locale: `ports: [8100]`.
5. Modificare `config.json`: `node_id` (l'incrocio giusto della mappa), `position`, `cameras` (zone), `crosswalks`, `p2p.serial.port`.
   Per trovare l'incrocio giusto: `python src/node/tools/build_topology.py --verify src/node/python/topology.json --mark 45.0653,7.6669`.
   Il prototipo (Via Bricherasio / Via Vela) è `incrocio-130` con la mappa attuale (493 incroci); gli id cambiano se si rigenera la mappa.
6. Facoltativo: `.env` accanto a `config.json` con `SC_P2P_KEY` (stessa chiave su tutti i nodi), vedi `.env.example`.
7. Avviare l'app: al boot il nodo resta sveglio `boot_active_s` (30 s), poi va a riposo; il sensore sul bordo, un Tag vicino
   o un incrocio vicino lo risvegliano. `http://<ip-board>:8100/api/status` mostra energia, strisce, Tag e radio.

Cose da verificare sulla board (non provabili dal PC): il Modulino su `Wire1`, `analogWrite` su D6/D9/D10/D11, l'accesso del
container alla seriale USB della radio (`/dev/ttyACM0`), `detector.stop()`/`start()` del brick di visione, i consumi reali.

### 2.4 Il collettore (server secondario)
Non è necessario al funzionamento: gli incroci si parlano da soli. Serve per vedere la città intera.
1. Copiare `src/collector/collector.json` e inserire in `nodes` l'indirizzo dell'API di ogni nodo (`http://<ip>:8100`).
2. `python src/collector/collector.py --config collector.json --port 8000` e aprire la pagina.
3. "Scarica ora" scarica i dati nuovi da tutti i nodi (o da uno solo); `pull.mode: "interval"` lo fa da solo. I cursori
   garantiscono che si scarichi solo ciò che manca. Con `listen.udp` o `listen.serial` (una radio ESP32 sul PC) si vede
   anche la radio in diretta; il collettore ascolta soltanto, non trasmette mai.

## 3. Ricostruire la mappa della città
```bash
cd src/node/tools
python build_topology.py --verify ../python/topology.json              # controlli sul file attuale
python build_topology.py --input ../../../data/osm/torino_crocetta.json  # rigenera dai dati OpenStreetMap
python build_topology.py --download --bbox 45.055,7.655,45.072,7.685 --save ../../../data/osm/zona.json  # nuova zona (rete)
```
Gli id degli incroci dipendono dai dati: dopo aver rigenerato, controllate `node_id` nei `config.json` e nel `collector.json`.
Dati cartografici: © OpenStreetMap contributors, ODbL.

### 3.1 L'immagine della mappa sotto i nodi
La dashboard disegna i nodi sopra le **tile di OpenStreetMap** salvate in `src/collector/tiles/` (120 tile a zoom 17, circa 3 MB):
è la mappa com'era quando le abbiamo scaricate, non si aggiorna da sola e funziona offline.
```bash
python src/node/tools/fetch_osm_tiles.py --dry-run          # quante tile servono
python src/node/tools/fetch_osm_tiles.py                    # scarica le mancanti (una al secondo, solo per la zona del progetto)
python src/node/tools/fetch_osm_tiles.py --bbox s,w,n,e --zoom 16   # un'altra zona
```
Si scarica una volta sola e solo per aree piccole (lo script si rifiuta oltre 400 tile: il server pubblico di OSM non è per il
download di massa). Senza tile la dashboard usa uno sfondo disegnato con le strade OSM (`python src/node/tools/build_basemap.py`
genera `src/collector/basemap.json`). L'attribuzione «© OpenStreetMap contributors» resta visibile in basso a destra sulla mappa.

## 4. Problemi frequenti
| Sintomo | Causa probabile |
|---|---|
| `[mcu] lettura dei sensori fallita` | sketch v2 non caricato (o Bridge giù): il nodo resta sveglio dopo 5 s |
| Il nodo non va mai a riposo | sensore guasto (`wake_sensor_fault` negli eventi) o Tag vicino con ruolo che sveglia (`power.wake_roles`) |
| LED4 fisso all'avvio | il router Linux della UNO Q non risponde ancora (attendere) |
| Strisce sempre "incerta" (luce 6) | camera non pronta o zona `crosswalk` assente nella camera (guardare gli avvisi di `[config]`) |
| Nessun avviso sul Tag | Tag fuori portata (`users.near_rssi`), canale Wi-Fi diverso da 1, o radio dell'incrocio non collegata |
| Il collettore non vede un nodo | porta 8100 non dichiarata in `app.yaml`, o `api.enabled` falso |
