# CROSS v2 - stato e cosa resta

Riferimento tecnico: [ARCHITETTURA.md](ARCHITETTURA.md) (§9: scelte prese in implementazione). Come provarlo: [GUIDA.md](GUIDA.md).
Nessun commit è stato fatto: la decisione è vostra.

## Fatto (tutto provato sul PC: `python src/node/tools/run_tests.py`, 13 script su 13)

| Parte | Dove | Note |
|---|---|---|
| Energia, MCU, segnali | `power.py`, `mcu.py`, `signals.py`, `sketch/sketch.ino` | riposo/attivo/raffreddamento, riflesso locale e fail-safe, luci con 7 codici |
| Attraversamenti e utenti | `crosswalk.py`, `users.py`, `situation.py`, `events.py` | mai "via libera" senza camera sveglia e fotogramma recente |
| Radio SC-P2P v2 e instradamento | `peer.py`, `route.py` | seriale/UDP/bus virtuale, firma, dedup, più salti |
| Mappa della città | `tools/build_topology.py`, `topology.json` | mappa OSM; l'id della board dipende dalla mappa (oggi `incrocio-124`), il nodo lo ritrova dalla posizione se cambia |
| Nodo integrato | `node.py`, `main.py`, `config.py`, `config.json`, `publisher.py` | senza server; sensori → energia → strisce → Tag → radio |
| Collettore e dashboard | `src/collector/` | pull con cursore, ascolto passivo, una pagina a colonna unica, nodi sopra le tile OSM salvate |
| Simulatore della città | `tools/simulate_city.py` | 462 nodi, MCU finti, Tag virtuali, 5 scenari; provato anche sulla dashboard |
| Firmware ESP32 | `esp32_radio`, `esp32_tag` | compilano senza errori né avvisi con core 2.0.17 (C3) e 3.x (C3 ed ESP32) |
| Strumenti | `replay.py`, `zone_editor.py`, `export_data.py` | aggiornati alla v2 (più strisce, MCU finto, niente server) |
| Documentazione | `README.md`, `GUIDA.md`, firmware `README.md` | v1 rimossa (server, client, bracciale UDP, pagina telefono) |

## Da fare sulla board (non si può dal PC)

1. Caricare lo sketch (`Arduino_RouterBridge`, `Arduino_Modulino`): finora controllato solo nella sintassi con header finti.
2. Modulino Distance su `Wire1`; staccare il cavo Qwiic e verificare che il nodo resti sveglio (`wake_sensor_fault`).
3. `analogWrite` su D6, D9, D10, D11 con il core Zephyr; luci con MOSFET e alimentatore 12 V.
4. Accesso del container di App Lab alla seriale USB della radio (`/dev/ttyACM0`) e porta 8100 in `app.yaml`.
5. `detector.stop()`/`start()` del brick di visione al cambio di energia (se non esistono, i fotogrammi vengono scartati ma il consumo non scende).
6. Più camere: se il brick non accetta la scelta della camera, `main.py` ne usa una sola (fisheye con tutte le zone).
7. Consumi reali a riposo e attivo per correggere `power.power_w` (ora 2,5 / 9 / 9 W, stime).
8. Più sensori VL53L4CD sullo stesso bus richiedono indirizzi diversi (XSHUT o multiplexer): per ora un sensore per incrocio.
9. Prova a banco di Tag e radio con le schede vere (RSSI reale, portata, canale 1) e taratura di `users.near_rssi`.
10. Calibrare le camere (`zone_editor.py`) e le soglie `safe_eta_s` / `conflict_eta_s` sul campo.

## Limiti noti
- `simulate_city.py --fast`: i pericoli inoltrati tra incroci possono scadere subito (orologi sfasati); usare il tempo reale.
- La prima apertura della dashboard con 462 nodi simulati sullo stesso PC può metterci una decina di secondi.
- Gli id degli incroci cambiano se si rigenera la mappa con dati OSM diversi: riallineare `node_id`.
- Il collettore non trasmette mai (nemmeno `rq` via radio): i riepiloghi radio `rs` arrivano solo se li chiede qualcun altro.
- `leds`, `server` e `wearable` della v1 in un vecchio `config.json` vengono ignorati con un avviso.
