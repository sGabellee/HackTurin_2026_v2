# CROSS v2 - piano dei lavori rimanenti

Riferimento tecnico: `docs/ARCHITETTURA.md`. Ogni passo si fa in una sola sessione, senza agenti paralleli:
si leggono solo i file del passo, si modifica, si eseguono i test del passo. Nessun commit finché non lo decidi tu.

Interprete per le prove: `C:\Users\Gabriele\Desktop\Hackaton\dataset\.venv\Scripts\python.exe` (3.11).
Test dei componenti: `src/node/tools/tests/test_*.py` (si lanciano da quella cartella).

## 0. Stato attuale

| Componente | File | Stato |
|---|---|---|
| Energia, MCU, segnali | `power.py`, `mcu.py`, `signals.py`, `sketch/sketch.ino` | fatto, test 59/59 e 110/110; revisione non fatta |
| Topologia | `tools/build_topology.py`, `python/topology.json` | fatto, 462 incroci, 781 archi, test 106/106; revisione non fatta |
| Radio e instradamento | `peer.py`, `route.py` | fatto, test passati; revisione non fatta |
| Collettore e dashboard | `collector/collector.py`, `dashboard.html`, `collector.json`, `api.py`, `storage.py` | fatto, test passati, provata nel browser; revisione non fatta |
| Attraversamenti e utenti | `situation.py`, `crosswalk.py`, `users.py`, `events.py` | fatto, test 49/49, 32/32, 34/34; `test_suite.py` 118/118 |
| Firmware ESP32 | `esp32_radio.ino`, `esp32_tag.ino`, `firmware/README.md` | radio compilato pulito (C3 e ESP32, core 3.x); **da fare**: compilare il tag, riscrivere il README (ancora v1), cancellare le cartelle vecchie |
| Integrazione nel nodo | `node.py`, `main.py`, `config.py`, `config.json`, `publisher.py` | da fare |
| Simulatore della città | `tools/simulate_city.py` | da fare |
| Documentazione | `README.md`, `docs/GUIDA.md` | da fare |

Compilatori trovati sul PC: `arduino-cli` dentro Arduino IDE (core esp32 3.x) e PlatformIO (core 2.0.17).
Script pronto: `scratchpad/fw_build/build.sh <sketch> <fqbn|pio_c3|pio_s3>` (va ricopiato in `src/firmware/tools/` se lo vuoi tenere).

## 1. Chiudere attraversamenti e utenti (priorità massima: sicurezza)

1. Leggere `crosswalk.py` e `users.py` e controllare che siano completi rispetto a §2.6 e §2.7.
2. `events.py`: aggiungere `crossing_conflict`, `assisted_crossing`, `emergency_corridor`, `wake_sensor_fault`.
3. Scrivere ed eseguire `test_crosswalk.py`, `test_users.py`, `test_situation_v2.py` con gli scenari di §2.7:
   camera che dorme -> WAIT/ASSIST_UNAVAILABLE; veicolo a 6 s -> attention; a 2 s -> conflict; non vedente sulle strisce;
   ambulanza; ciclabile occupata; nessun Tag vicino -> nessun messaggio; mai CROSS_OK con camera non fresca.
4. Controllo di regressione: `test_suite.py` deve passare ancora per la parte `situation`.

Verifica: i tre test passano; ricerca mirata di ogni punto in cui può uscire CROSS_OK o la luce "safe".

## 2. Chiudere il firmware

1. Compilare `esp32_radio` ed `esp32_tag` con i due core (C3 e DevKit/S3) e correggere errori e warning.
2. Il Tag deve ignorare il campo `sig` negli `adv` (gli adv sono firmati se c'è `p2p.key`).
3. Riscrivere `src/firmware/README.md` (collegamenti, Arduino IDE, prove, tabella vibrazioni di §6).
4. Solo dopo: cancellare `esp32_gateway`, `esp32_wearable`, `esp32_bracciale`, `uno_r4_incrocio`.

Verifica: 4 compilazioni pulite; prova a banco con una radio sul PC e `python src/node/python/peer.py listen`.

## 3. Revisione dei componenti fatti (una alla volta, solo lettura mirata)

| Componente | Cosa controllare |
|---|---|
| radio | taglio a 240 byte, dedup, rilancio con `h`, consegna solo ai destinatari `to`, id accorciati `~resto` |
| energia | transizioni, `hold_s`, guasto sensore; a riposo restano accese solo emergenze remote e strisce |
| collettore | cursore di export, nodo irraggiungibile, `decode_min` deve espandere gli id `~resto` |
| topologia | nessuna correzione prevista; solo controllo a vista sulla mappa della dashboard |

## 4. Integrazione nel nodo

Decisioni già prese dai risultati, da applicare qui:
- La board del prototipo è **`incrocio-124`** (Via Bricherasio / Via Vela, a 11 m). "Via Vela / Corso Stati Uniti" non esiste in OSM: correggere anche §4 della specifica.
- `poll_sensors() == None` con sensori configurati per più di qualche secondo = `sensor_fault` (il nodo resta sveglio).
- `near_rssi` letto da `config["users"]` e passato a `PeerLink` (oggi lo legge da `p2p`).
- Nel config: bordo `i` collegato al canale luce `i` (lo sketch nel riflesso locale usa quella coppia).

Lavori:
1. `config.py` e `config.json` con lo schema v2 (§2.12) e i controlli di validità.
2. `node.py` secondo §2.9: PowerManager, MCULink/FakeMCU, SignalController, CrosswalkController, UserLink,
   PeerLink v2 con `Topology`, `targets_for` per gli avvisi, evento `emergency_corridor`, risposta `rs` ai `rq`,
   `status_snapshot()` con energia, attraversamenti, Tag e peer.
3. `main.py`: `detector.stop()`/`start()` dal cambio di stato di energia; più camere.
4. `publisher.py`: togliere invio HTTP e MQTT, restano archivio, log e UI.
5. Cancellare `remote.py`, `wearable.py`, `leds.py`, `web/app.html`, `src/server/`, `src/client/`.
6. Aggiornare `health.py`, `replay.py`, `zone_editor.py` (più zone `crosswalk_*` e `waiting_area_*`), `export_data.py`.
7. `test_suite.py`: togliere le prove su `/app`, server v1 e `leds.py`; aggiungere un test del nodo intero con FakeMCU.

Verifica: tutti i test verdi; nodo avviato sul PC con FakeMCU e UDP, `/api/status` completo.

## 5. Simulatore della città

`src/node/tools/simulate_city.py`: tutti i 462 incroci come nodi leggeri (FakeMCU + SimRadioBus + Tag virtuali),
API per nodo su `/n/<id>/api/...`, `collector.json` per la simulazione.
Scenari: `blind_crossing`, `ambulance_route`, `bike_lane`, `pedestrian_night`, `demo`.

Verifica: collettore + dashboard puntati sul simulatore; gli scenari si vedono sulla mappa, i nodi lontani restano a riposo.

## 6. Documentazione

- `README.md` in italiano per la v2 (resta il riferimento a `Efesto.pdf`, al video YouTube e al terzo posto).
- `docs/GUIDA.md`: collegamenti, firmware, App Lab, topologia, simulazione, collettore e dashboard.
- Togliere `PIANO.md` e `GUIDA_DEMO.md` della v1; tenere `TUTORIAL.md` se ancora valido.
- Aggiornare `docs/ARCHITETTURA.md` con le deviazioni accettate: formato del cursore, campi in più di `topology.json`, `source`, `near_rssi`.
- `.env.example` e `.gitignore` (`collector.db`).

## 7. Prove sulla board (non si possono fare dal PC)

1. Modulino Distance su `Wire1` con la libreria installata in App Lab; staccare il Qwiic e verificare il guasto.
2. `analogWrite` su D6, D9, D10, D11 con il core Zephyr della UNO Q.
3. Accesso del container App Lab alla seriale USB della radio ESP32.
4. `detector.stop()`/`start()` del brick VideoObjectDetection al cambio di stato.
5. Consumo reale a riposo e attivo, per correggere i valori `power_w`.
6. Più sensori VL53L4CD richiedono indirizzi diversi (XSHUT o multiplexer): per ora un sensore per incrocio.

## Ordine consigliato

1 -> 4 -> 5 -> 3 -> 2 -> 6 -> 7. Prima la sicurezza e un nodo che gira davvero, poi simulazione e controlli, il firmware quando hai le schede in mano.
