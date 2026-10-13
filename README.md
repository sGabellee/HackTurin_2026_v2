# CROSS – Cognitive Road Operations & Smart Signaling (team Efesto, HackTurin 2026)

> Terzo posto HackTurin 2026 – **versione 2**

**La documentazione del progetto presentato all'hackathon si trova nel file [Efesto.pdf](Efesto.pdf).**

**Video di presentazione del progetto: [guardalo su YouTube](https://www.youtube.com/watch?v=bz3QYN31SZ0).**

## Cosa cambia nella versione 2

La v2 riparte dal progetto vincitore con un'altra direzione: una rete di incroci che **funziona offline**, spreca poca
energia e **aiuta tutti gli utenti della strada**, a partire da chi fa più fatica ad attraversare.

- **Offline e peer-to-peer.** Ogni incrocio decide da solo. Gli incroci si parlano via radio (ESP-NOW, con un ESP32 sulla
  USB di ogni Arduino UNO Q), senza internet, router o server. Il server è solo un **collettore secondario**: quando serve
  va a chiedere i dati ai nodi (pull) e li mostra in una dashboard locale.
- **Poca energia.** Il nodo resta a riposo (camera e intelligenza artificiale ferme) e si sveglia solo se un sensore sul
  palo delle strisce vede qualcuno, se passa un CROSS Tag, o se un incrocio vicino segnala un'emergenza.
- **Utenti della strada al centro.** Pedoni, non vedenti, ciclisti, conducenti e mezzi di soccorso portano un **CROSS Tag**
  (un ESP32 con vibrazione, LED e, per i non vedenti, suono). L'incrocio manda a ognuno l'avviso giusto per il suo ruolo.
- **Luci sopra le strisce.** Quando qualcuno sta per attraversare e un veicolo si avvicina, la luce sopra le strisce
  cambia (attenzione, pericolo, assistenza al non vedente, emergenza). Se l'incrocio non vede bene, non dà mai il via libera.
- **Corridoio di emergenza.** Un'ambulanza sveglia gli incroci lungo il suo percorso prima che arrivi, mette in sicurezza le
  strisce e dice al conducente se il corridoio è libero.
- **Una mappa sola.** Gli incroci sono quelli reali di OpenStreetMap (462 nella zona Crocetta / San Secondo); la dashboard
  mostra tutta la città in una pagina a colonna unica.

## Struttura

| Cartella | Contenuto |
|---|---|
| `src/node/python/` | il nodo (App Lab sulla UNO Q): regole, energia, attraversamenti, Tag, radio, API locale |
| `src/node/sketch/` | sketch dell'MCU: LED, luci sopra le strisce, sensore di distanza, riflesso locale e fail-safe |
| `src/node/tools/` | simulatore della città, costruzione della mappa, replay su video, test |
| `src/firmware/` | firmware ESP32: radio dell'incrocio e CROSS Tag |
| `src/collector/` | collettore secondario e dashboard locale |
| `src/dataset/` | dataset e addestramento del modello di visione |
| `data/osm/` | dati cartografici di OpenStreetMap |
| `docs/` | [ARCHITETTURA.md](docs/ARCHITETTURA.md) (specifica), [GUIDA.md](docs/GUIDA.md) (come provarlo e montarlo), [PIANO_V2.md](docs/PIANO_V2.md) (cosa resta), [TUTORIAL.md](docs/TUTORIAL.md) (addestramento del modello) |

## Provarlo sul PC

```bash
cd src/node/tools && python simulate_city.py --scenario demo --loop --udp   # la città simulata
cd src/collector && python collector.py --config collector_sim.json --port 8000 --pull   # dashboard su http://127.0.0.1:8000
```

Solo libreria standard di Python 3.11. Tutti i dettagli (test, montaggio, firmware, App Lab) sono in [docs/GUIDA.md](docs/GUIDA.md).

Dati cartografici: © OpenStreetMap contributors, ODbL.
