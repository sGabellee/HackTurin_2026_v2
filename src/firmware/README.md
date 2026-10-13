# Firmware ESP32 di CROSS v2

Due firmware, tutti e due senza Wi-Fi di casa e senza librerie da installare. Comunicano con ESP-NOW
sul canale Wi-Fi fisso 1, con il protocollo SC-P2P v2 (`docs/ARCHITETTURA.md` §3).

```
 CROSS Tag (pedone, non vedente,           radio dell'incrocio              UNO Q dell'incrocio
 ciclista, conducente, soccorso)           (ESP32 su USB)                   (nodo Python, peer.py)
 ┌──────────────┐  beacon vru/emv  ))) ┌──────────────┐  R-63 {...}\n  ┌──────────────┐
 │ esp32_tag    │ ───────────────────▶ │ esp32_radio  │ ─────────────▶ │ node.py      │
 │ vibra, LED,  │ ◀─────────────────── │ ponte        │ ◀───────────── │ users.py     │
 │ suono        │      avvisi adv  ((( │ trasparente  │   {...}\n      │ crosswalk.py │
 └──────────────┘                      └──────────────┘                └──────────────┘
                                              ▲  ESP-NOW broadcast
                                              ▼  (hz, hb, rq, rs tra incroci vicini)
                                       radio degli altri incroci
```

| Cartella | Cosa fa |
|---|---|
| `esp32_radio/` | Ponte trasparente tra seriale USB (115200, una riga JSON per messaggio) ed ESP-NOW broadcast. Ogni frame ricevuto viene stampato con davanti l'RSSI: `R-63 {...}`. Non interpreta il JSON: dedup, rilancio, firme e Tag vicini li gestisce `peer.py`. |
| `esp32_tag/` | Il dispositivo personale, un solo firmware per i cinque ruoli. Manda il beacon di presenza e trasforma gli avvisi dell'incrocio in vibrazioni, LED e (per i non vedenti) suoni. |

## Schede supportate

| Scheda | Radio | Tag | Note |
|---|---|---|---|
| ESP32-C3 SuperMini | si | si (consigliata) | LED blu su GPIO 8 acceso con LOW, BOOT su GPIO 9 |
| ESP32-S3 DevKitC | si | si | LED RGB della scheda: verde/rosso veri per il soccorso, BOOT su GPIO 0 |
| ESP32 DevKit (classico) | si | si | LED su GPIO 2, BOOT su GPIO 0 |

Core Arduino-ESP32 2.x e 3.x (anche ESP-IDF 5.5). Con il core 2.x la radio ricava l'RSSI in modalità
promiscua (`RSSI_PROMISCUOUS 1`); se non ci riesce scrive `R0`, che il nodo tratta come "RSSI non disponibile".

## Collegamenti del Tag

| Parte | Pin | Collegamento |
|---|---|---|
| Motorino vibrante | GPIO 4 (`VIBRO_PIN`) | GPIO 4 → resistenza 1 kΩ → base di un NPN (es. 2N2222 o BC337); motorino tra 3,3 V e collettore; emettitore a GND; diodo 1N4148 in parallelo al motorino (catodo verso 3,3 V) |
| Buzzer (facoltativo, per non vedenti) | GPIO 5 (`BUZZER_PIN`) | buzzer passivo/piezo tra GPIO 5 e GND (con un buzzer attivo: `BUZZER_ACTIVE 1`) |
| LED verde e rosso (facoltativi, soccorso) | `LED_GREEN_PIN`, `LED_RED_PIN` | LED + resistenza 330 Ω verso GND; senza questi LED si usa il LED della scheda |
| Batteria (facoltativa) | `BATTERY_ADC_PIN` | partitore con due resistenze uguali verso un pin ADC (`BATTERY_DIVIDER_X10 20`) |
| Pulsante ruolo | BOOT della scheda | nessun collegamento |

Per una demo veloce il motorino si può sostituire con un LED e una resistenza su GPIO 4.
Tutti i `#define` in testa al file si cambiano lì o con i build flag.

## Caricare il firmware (Arduino IDE)

1. Arduino IDE 2: in Gestore schede installa "esp32" di Espressif.
2. Apri `esp32_radio/esp32_radio.ino` oppure `esp32_tag/esp32_tag.ino`.
3. Scheda: "ESP32C3 Dev Module" per la SuperMini (con "USB CDC On Boot: Enabled"), "ESP32S3 Dev Module" o "ESP32 Dev Module".
4. Scegli la porta e premi Carica. Monitor seriale a 115200 baud.

Da riga di comando (arduino-cli incluso in Arduino IDE):

```bash
arduino-cli compile --fqbn esp32:esp32:esp32c3 src/firmware/esp32_tag
```

## Il Tag

- **Beacon**: `{"v":2,"t":"vru","d":"tag-a1b2c3","p":"blind","q":12}` ogni 1 s;
  per il soccorso `{"v":2,"t":"emv","d":"tag-a1b2c3","q":40,"st":"run"}` ogni 0,5 s. L'id viene dal MAC.
- **Avvisi**: tiene solo gli `adv` con il proprio ruolo in `r` o il proprio id in `to`. Ogni avviso vale
  `ttl` ms dall'arrivo (l'incrocio lo ripete ogni secondo): se l'incrocio tace, l'avviso si spegne da solo.
  Tra più incroci vince il più grave; un "via libera" non copre mai un pericolo ancora valido.
  Il campo `sig` (firma degli incroci) viene ignorato.
- **Ruolo**: pressione lunga (1,5 s) di BOOT = ruolo successivo, salvato in memoria. Conferma con N
  vibrazioni: 1 pedone, 2 non vedente, 3 ciclista, 4 conducente, 5 soccorso. Pressione breve = ripete.
- **Energia**: CPU a 80 MHz e LED quasi sempre spento. `TAG_RADIO_SAVE 1` (solo core 3.x) fa dormire la
  radio tra le finestre di ascolto, mai per i ruoli non vedente e soccorso.

### Tabella degli avvisi (§6)

| Codice | Nome | Vibrazione | Suono (non vedente) |
|---|---|---|---|
| 1 | `WAIT` | impulso lungo ogni 2 s | tono basso lento |
| 2 | `CROSS_OK` | due impulsi brevi ogni 1,5 s | tono alto ripetuto |
| 3 | `KEEP_GOING` | tic leggero ogni 1 s | tic |
| 4 / 5 | `VEHICLE_LEFT` / `VEHICLE_RIGHT` | 3 impulsi rapidi (sx) / 4 impulsi rapidi (dx) | tono che sale / scende |
| 9 | `EMERGENCY_APPROACHING` | vibrazione continua a scatti 5 Hz | sirena a due toni |
| 6, 7, 8, 12, 14 | `PEDESTRIAN_AHEAD`, `CYCLIST_AHEAD`, `BIKE_LANE_BLOCKED`, `HAZARD_AHEAD`, `WRONG_WAY` | 3 impulsi medi | - |
| 13 | `ASSIST_UNAVAILABLE` | impulso lungo, pausa, impulso lungo, ogni 5 s | tono di errore |
| 10 / 11 | `CORRIDOR_CLEAR` / `CORRIDOR_BUSY` (soccorso) | LED verde / rosso (un solo LED: lampeggio lento / veloce) | - |
| 0 | `CLEAR` | niente | niente |

### Comandi dal monitor seriale

`h` aiuto, `i` stato, `1`..`5` ruolo, `t` avviso di prova, `t4` avviso di prova con codice 4, `s` sirena
(soccorso), `d90` direzione, `v` log dettagliato, `x` autotest del lettore JSON. Una riga `{...}` viene
trattata come un messaggio ricevuto via radio, per provare senza incrocio:

```
{"v":2,"t":"adv","n":"incrocio-124","id":"a1","r":["blind"],"c":4,"s":3,"x":0,"ttl":3000}
```

## Prove passo passo

1. **Radio da sola**: carica `esp32_radio`, apri il monitor seriale. All'avvio stampa righe `#` con
   MAC e canale. Scrivi una riga JSON e premi invio: viene trasmessa.
2. **Tag da solo**: carica `esp32_tag`, monitor seriale, `i` per lo stato, `t` per sentire la vibrazione
   di prova, `2` per passare a non vedente e `t4` per il suono "veicolo da sinistra".
3. **Tag + radio**: con il Tag acceso, il monitor della radio mostra ogni secondo
   `R-45 {"v":2,"t":"vru",...}`. Incolla nel monitor della radio un `adv` come quello sopra con
   `"r":["pedestrian"]`: il Tag vibra.
4. **Con il nodo** (UNO Q, Linux): collega la radio via USB e lancia
   `python src/node/python/peer.py listen --transports serial --serial /dev/ttyACM0` per vedere il traffico, oppure
   `python src/node/python/peer.py tag --role blind` per un finto Tag via UDP senza hardware.
   Su Windows la seriale della radio non è supportata da `peer.py`: usare il monitor di Arduino IDE.

Le cartelle della v1 (`esp32_gateway`, `esp32_wearable`, `esp32_bracciale`, `uno_r4_incrocio`) sono state
sostituite da questi due firmware.
