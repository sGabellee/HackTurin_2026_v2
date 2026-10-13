"""Test di users.py (docs/ARCHITETTURA.md par. 2.7): Tag vicini e avvisi "adv" per ruolo.

Scenari: pedone con camera che dorme -> WAIT, non vedente -> ASSIST_UNAVAILABLE; camera pronta e nessun
veicolo -> CROSS_OK; veicolo da sinistra -> VEHICLE_LEFT; non vedente sulle strisce con conflitto ->
VEHICLE_LEFT, senza conflitto KEEP_GOING; ambulanza -> EMERGENCY_APPROACHING ai pedoni e CORRIDOR_* al
soccorso; ciclabile occupata -> BIKE_LANE_BLOCKED; conducente -> PEDESTRIAN_AHEAD; contromano -> WRONG_WAY;
Tag lontano o nessun Tag -> nessun messaggio; ritmo di invio; formato e 240 byte; mai CROSS_OK senza
camera fresca. Gli stati degli attraversamenti vengono dal vero CrosswalkController.

Uso:  python test_users.py   (dalla cartella node/tools/tests; esce con codice 1 se qualcosa fallisce)
"""
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from crosswalk import CrosswalkController  # noqa: E402
from users import CODES, UserLink  # noqa: E402

RISULTATI = []
CFG = {"crosswalks": [{"id": 0, "camera": "cam0", "zone": "crosswalk", "waiting": "waiting_area", "curb": 0, "light": 0}],
       "users": {"near_rssi": -75, "user_timeout_s": 6, "adv_period_s": 1, "min_state_s": 1.5}}


def check(nome, ok, info=""):
    RISULTATI.append((nome, bool(ok), info))
    print(f"  [{'OK ' if ok else 'ERR'}] {nome}" + (f"  ({info})" if info and not ok else ""))


class Banco:
    """Nodo minimo: CrosswalkController + UserLink con invio registrato."""

    def __init__(self, cfg=CFG):
        self.inviati = []
        self.cw = CrosswalkController(cfg, "incrocio-124")
        self.ul = UserLink(cfg, "incrocio-124", self.inviati.append)

    def tag(self, dev, ruolo, t, rssi=-60, **extra):
        tipo = "emergency_beacon" if ruolo == "emergency" else "vru_beacon"
        return self.ul.on_beacon({"type": tipo, "device": dev, "role": ruolo, "rssi": rssi, **extra}, t)

    def giro(self, t, vista=None, ready=True, emergency=None, hazards=(), bordo=False):
        users = self.ul.counts_by_crosswalk(t)
        sts = self.cw.update(t, {0: vista}, {0: {"present": bordo, "ok": True}}, users, emergency, ready)
        return self.ul.advise(t, sts, list(hazards), emergency, ready)


def vista(waiting=1, crossing=0, vehicles=(), fresh=True, bike=0):
    etas = [v["eta_s"] for v in vehicles if v.get("eta_s") is not None]
    return {"fresh": fresh, "waiting": waiting, "crossing": crossing, "cyclists": 0, "bike_lane_cyclists": bike,
            "vehicles": list(vehicles), "min_eta_s": min(etas) if etas else None}


def auto(eta, side="left"):
    return {"track": 3, "class": "car", "eta_s": eta, "side": side, "speed": 9.0}


def codici(msgs, ruolo=None, dev=None):
    out = []
    for m in msgs:
        if (ruolo and ruolo in (m.get("r") or [])) or (dev and m.get("to") == dev):
            out.append(m["c"])
    return out


def test_pedone_e_non_vedente():
    print("== pedone e non vedente in attesa")
    b = Banco()
    b.tag("p1", "pedestrian", 0.0)
    b.tag("b1", "blind", 0.0)
    m = b.giro(0.0, vista=None, ready=False, bordo=True)
    check("camera che dorme: pedone WAIT", CODES["WAIT"] in codici(m, "pedestrian"), m)
    check("camera che dorme: non vedente ASSIST_UNAVAILABLE", CODES["ASSIST_UNAVAILABLE"] in codici(m, "blind"), m)
    check("mai CROSS_OK senza camera", all(x["c"] != CODES["CROSS_OK"] for x in m))

    b = Banco()
    b.tag("p1", "pedestrian", 0.0)
    m = b.giro(0.0, vista=vista(fresh=False))
    check("fotogramma vecchio: niente CROSS_OK", all(x["c"] != CODES["CROSS_OK"] for x in m), m)

    b = Banco()
    b.tag("p1", "pedestrian", 0.0)
    m = b.giro(0.0, vista=vista())
    check("camera pronta, nessun veicolo: CROSS_OK", codici(m, "pedestrian") == [CODES["CROSS_OK"]], m)

    b = Banco()
    b.tag("p1", "pedestrian", 0.0)
    m = b.giro(0.0, vista=vista(vehicles=[auto(6.0, "left")]))
    check("veicolo da sinistra a 6 s: VEHICLE_LEFT", codici(m, "pedestrian") == [CODES["VEHICLE_LEFT"]], m)
    b = Banco()
    b.tag("p1", "pedestrian", 0.0)
    m = b.giro(0.0, vista=vista(vehicles=[auto(2.0, "right")]))
    check("veicolo da destra a 2 s: VEHICLE_RIGHT gravita' 3",
          any(x["c"] == CODES["VEHICLE_RIGHT"] and x["s"] == 3 for x in m), m)

    b = Banco()
    b.tag("b1", "blind", 0.0)
    m = b.giro(0.0, vista=vista())
    check("non vedente, camera pronta, nessun veicolo: CROSS_OK", codici(m, "blind") == [CODES["CROSS_OK"]], m)
    check("stato assist sulla luce", b.cw.light_codes().get(0) == 4, b.cw.light_codes())


def test_non_vedente_sulle_strisce():
    print("== non vedente sulle strisce")
    b = Banco()
    b.tag("b1", "blind", 0.0)
    b.giro(0.0, vista=vista())                      # CROSS_OK
    b.tag("b1", "blind", 1.0, speed=1.0)            # si muove: sta attraversando
    m = b.giro(1.0, vista=vista(waiting=0, crossing=1))
    check("sulle strisce senza conflitto: KEEP_GOING", codici(m, "blind") == [CODES["KEEP_GOING"]], m)
    b.tag("b1", "blind", 1.5, speed=1.0)
    m = b.giro(1.5, vista=vista(waiting=0, crossing=1, vehicles=[auto(2.0, "left")]))
    check("sulle strisce con veicolo in conflitto: VEHICLE_LEFT", codici(m, "blind") == [CODES["VEHICLE_LEFT"]], m)


def test_soccorso():
    print("== soccorso")
    b = Banco()
    b.tag("p1", "pedestrian", 0.0)
    b.tag("amb", "emergency", 0.0)
    m = b.giro(0.0, vista=vista(waiting=1), emergency={"eta_s": 15, "from": "incrocio-3"})
    check("pedone: EMERGENCY_APPROACHING", codici(m, "pedestrian") == [CODES["EMERGENCY_APPROACHING"]], m)
    check("luce emergency sulle strisce", b.cw.light_codes().get(0) == 5)

    b = Banco()
    b.tag("amb", "emergency", 0.0)
    m = b.giro(0.0, vista=vista(waiting=0))
    check("soccorso, strisce libere e camera fresca: CORRIDOR_CLEAR", codici(m, "emergency") == [CODES["CORRIDOR_CLEAR"]], m)
    b = Banco()
    b.tag("amb", "emergency", 0.0)
    m = b.giro(0.0, vista=vista(waiting=0, crossing=1))
    check("soccorso, qualcuno sulle strisce: CORRIDOR_BUSY", codici(m, "emergency") == [CODES["CORRIDOR_BUSY"]], m)
    b = Banco()
    b.tag("amb", "emergency", 0.0)
    m = b.giro(0.0, vista=None, ready=False)
    check("soccorso, camera che dorme: CORRIDOR_BUSY (mai libero alla cieca)",
          codici(m, "emergency") == [CODES["CORRIDOR_BUSY"]], m)


def test_ciclista_conducente_contromano():
    print("== ciclista, conducente, contromano")
    inv = {"type": "hazard", "kind": "bike_lane_invasion", "category": "violation", "severity": 2,
           "node": "incrocio-124", "ts": 0, "expires": 60_000, "where": {"zone": "bike_lane"}}
    b = Banco()
    b.tag("c1", "cyclist", 0.0)
    m = b.giro(0.0, vista=vista(waiting=0), hazards=[inv])
    check("ciclabile invasa: BIKE_LANE_BLOCKED", codici(m, "cyclist") == [CODES["BIKE_LANE_BLOCKED"]], m)
    check("k = tipo di pericolo", any(x.get("k") == "bike_lane_invasion" for x in m), m)

    b = Banco()
    b.tag("d1", "driver", 0.0)
    m = b.giro(0.0, vista=vista(waiting=0, crossing=1))
    check("conducente con persona sulle strisce: PEDESTRIAN_AHEAD", codici(m, "driver") == [CODES["PEDESTRIAN_AHEAD"]], m)
    b = Banco()
    b.tag("d1", "driver", 0.0)
    b.tag("c1", "cyclist", 0.0)
    m = b.giro(0.0, vista=vista(waiting=0))
    check("conducente con un ciclista vicino: CYCLIST_AHEAD", codici(m, "driver") == [CODES["CYCLIST_AHEAD"]], m)

    cm = {"type": "remote_hazard", "kind": "wrong_way", "category": "risk", "severity": 3, "node": "incrocio-9",
          "ts": 0, "expires": 60_000}
    b = Banco()
    b.tag("p1", "pedestrian", 0.0)
    b.tag("d1", "driver", 0.0)
    m = b.giro(0.0, vista=vista(waiting=1), hazards=[cm])
    check("contromano: WRONG_WAY a pedone e conducente",
          codici(m, "pedestrian") == [CODES["WRONG_WAY"]] and codici(m, "driver") == [CODES["WRONG_WAY"]], m)
    scaduto = dict(cm, expires=500)
    b = Banco()
    b.tag("d1", "driver", 2.0)
    m = b.giro(2.0, vista=vista(waiting=0), hazards=[scaduto])
    check("pericolo scaduto ignorato", codici(m, "driver") == [CODES["CLEAR"]], m)


def test_vicinanza_ritmo_formato():
    print("== vicinanza, ritmo e formato")
    b = Banco()
    check("nessun Tag: nessun messaggio", b.giro(0.0, vista=vista()) == [])
    b.tag("p1", "pedestrian", 0.0, rssi=-90)
    check("Tag lontano (rssi -90): nessun messaggio", b.giro(0.0, vista=vista()) == [])

    b = Banco()
    b.tag("p1", "pedestrian", 0.0)
    m1 = b.giro(0.0, vista=vista())
    m2 = b.giro(0.3, vista=vista())
    m3 = b.giro(1.05, vista=vista())
    check("subito al primo giro", len(m1) == 1)
    check("stesso avviso entro 1 s: niente ripetizione", m2 == [])
    check("ripetuto dopo adv_period_s", len(m3) == 1)
    m4 = b.giro(1.2, vista=vista(vehicles=[auto(2.0)]))
    check("avviso cambiato: inviato subito", len(m4) == 1 and m4[0]["c"] == CODES["VEHICLE_LEFT"], m4)
    m5 = b.giro(10.0, vista=vista())
    check("Tag scaduto dopo user_timeout_s: silenzio", m5 == [])

    b = Banco()
    b.tag("p1", "pedestrian", 0.0)
    m = b.giro(0.0, vista=vista())[0]
    campi = {"v", "t", "n", "id", "c", "s", "x", "ttl"}
    check("formato adv (§3.2)", campi <= set(m) and m["v"] == 2 and m["t"] == "adv" and ("r" in m or "to" in m), m)
    check("dentro 240 byte", len(json.dumps(m, separators=(",", ":")).encode()) <= 240)

    b = Banco()
    b.tag("p1", "pedestrian", 0.0)
    b.tag("p2", "pedestrian", 0.0)
    m = b.giro(0.0, vista=vista())
    check("stesso avviso a due pedoni: un messaggio per ruolo", len(m) == 1 and m[0].get("r") == ["pedestrian"], m)
    check("state() per l'API", b.ul.state(0.0)["near"] == 2)
    check("beacon non valido ignorato", b.ul.on_beacon({"type": "vru_beacon"}, 0.0) is None)


def main():
    for f in (test_pedone_e_non_vedente, test_non_vedente_sulle_strisce, test_soccorso,
              test_ciclista_conducente_contromano, test_vicinanza_ritmo_formato):
        try:
            f()
        except Exception as e:
            traceback.print_exc()
            check(f"{f.__name__} senza eccezioni", False, repr(e))
    ok = sum(1 for r in RISULTATI if r[1])
    print(f"\n{ok}/{len(RISULTATI)} controlli OK")
    for nome, esito, info in RISULTATI:
        if not esito:
            print("  FALLITO:", nome, info)
    sys.exit(0 if ok == len(RISULTATI) else 1)


if __name__ == "__main__":
    main()
