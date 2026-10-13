"""Test di crosswalk.py (docs/ARCHITETTURA.md par. 2.6 e 2.8): stato degli attraversamenti e luci.

Prova gli scenari della specifica: nessuno -> idle; persona sul bordo con camera che dorme -> unknown;
camera pronta senza veicoli -> occupied e safe; veicolo a 6 s -> attention; a 2 s -> conflict (luce 3);
non vedente -> assist; soccorso -> emergency; tenuta minima con escalation immediata; mai safe senza
camera fresca; eventi crossing_conflict e assisted_crossing; piu' attraversamenti sullo stesso canale.

Uso:  python test_crosswalk.py   (dalla cartella node/tools/tests; esce con codice 1 se qualcosa fallisce)
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from crosswalk import LIGHT, CrosswalkController  # noqa: E402

RISULTATI = []


def check(nome, ok, info=""):
    RISULTATI.append((nome, bool(ok), info))
    print(f"  [{'OK ' if ok else 'ERR'}] {nome}" + (f"  ({info})" if info and not ok else ""))


CFG = {"crosswalks": [{"id": 0, "camera": "cam0", "zone": "crosswalk", "waiting": "waiting_area", "curb": 0, "light": 0},
                      {"id": 1, "camera": "cam0", "zone": "crosswalk_2", "waiting": "waiting_area_2", "curb": 1, "light": 1}],
       "users": {"min_state_s": 1.5, "safe_eta_s": 8, "conflict_eta_s": 4}}


def vista(waiting=0, crossing=0, vehicles=(), fresh=True, cyclists=0):
    etas = [v["eta_s"] for v in vehicles if v.get("eta_s") is not None]
    return {"fresh": fresh, "waiting": waiting, "crossing": crossing, "cyclists": cyclists,
            "vehicles": list(vehicles), "min_eta_s": min(etas) if etas else None}


def veicolo(eta, side="left", cls="car"):
    return {"track": 7, "class": cls, "eta_s": eta, "side": side, "speed": 8.0}


def stato(c, i=0):
    return c.states()[i]


def test_scenari_base():
    print("== scenari base")
    c = CrosswalkController(CFG, "incrocio-124")
    c.update(0.0, {}, {0: {"present": False, "ok": True}}, {}, None, False)
    s = stato(c)
    check("nessuno -> idle, luce 0", s["state"] == "idle" and s["light"] == 0)
    check("idle con camera spenta non e' safe", s["safe"] is False)

    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: None}, {0: {"present": True, "ok": True}}, {}, None, False)
    s = stato(c)
    check("bordo occupato e camera che dorme -> unknown", s["state"] == "unknown" and s["light"] == LIGHT["unknown"])
    check("unknown non e' mai safe", s["safe"] is False)

    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: vista(waiting=1)}, {0: {"present": True, "ok": True}}, {}, None, True)
    s = stato(c)
    check("camera pronta senza veicoli -> occupied safe", s["state"] == "occupied" and s["safe"] is True, s)

    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: vista(waiting=1, fresh=False)}, {0: {"present": True, "ok": True}}, {}, None, True)
    check("fotogramma non fresco -> unknown, non safe", stato(c)["state"] == "unknown" and not stato(c)["safe"])

    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: vista(waiting=1, vehicles=[veicolo(6.0)])}, {}, {}, None, True)
    s = stato(c)
    check("veicolo a 6 s -> attention, luce 2", s["state"] == "attention" and s["light"] == 2)
    check("attention: lato sinistro e non safe", s["side"] == "left" and s["safe"] is False)

    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: vista(waiting=1, vehicles=[veicolo(2.0, "right")])}, {}, {}, None, True)
    s = stato(c)
    check("veicolo a 2 s -> conflict, luce 3", s["state"] == "conflict" and s["light"] == 3)
    check("conflict: lato destro", s["side"] == "right")

    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: vista(waiting=1, vehicles=[veicolo(None)])}, {}, {}, None, True)
    check("veicolo con moto ignoto -> attention, non safe", stato(c)["state"] == "attention" and not stato(c)["safe"])

    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: vista(vehicles=[veicolo(2.0)])}, {}, {}, None, True)
    check("veicolo vicino ma nessuno sulle strisce -> idle", stato(c)["state"] == "idle")


def test_non_vedente_e_soccorso():
    print("== non vedente e soccorso")
    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: vista()}, {}, {0: {"blind": 1, "pedestrians": 0}}, None, True)
    s = stato(c)
    check("Tag blind vicino, camera fresca, nessun veicolo -> assist (luce 4) e safe",
          s["state"] == "assist" and s["light"] == 4 and s["safe"])
    ev = c.take_events()
    check("evento assisted_crossing new", any(e.get("kind") == "assisted_crossing" and e.get("action") == "new" for e in ev), ev)
    c.update(1.0, {0: vista()}, {}, {0: {"blind": 0, "pedestrians": 0}}, None, True)
    ev = c.take_events()
    check("assisted_crossing cancel quando il Tag non e' piu' vicino",
          any(e.get("kind") == "assisted_crossing" and e.get("action") == "cancel" for e in ev), ev)

    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: None}, {}, {0: {"blind": 1}}, None, False)
    check("Tag blind con camera che dorme -> unknown (mai assist)", stato(c)["state"] == "unknown" and not stato(c)["safe"])

    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: vista(waiting=1)}, {}, {}, {"eta_s": 20, "from": "incrocio-3"}, True)
    s = stato(c)
    check("soccorso in arrivo -> emergency, luce 5, non safe", s["state"] == "emergency" and s["light"] == 5 and not s["safe"])
    check("soccorso: tutti gli attraversamenti in emergency", stato(c, 1)["state"] == "emergency")

    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: vista(vehicles=[veicolo(3.0, cls="emergency")])}, {}, {}, None, True)
    check("soccorso visto dalla camera -> emergency", stato(c)["state"] == "emergency")


def test_tenuta_ed_eventi():
    print("== tenuta minima ed eventi")
    c = CrosswalkController(CFG, "n")
    bordo = {0: {"present": True, "ok": True}}
    c.update(0.0, {0: vista(waiting=1, vehicles=[veicolo(2.0)])}, bordo, {}, None, True)
    ev = c.take_events()
    check("conflict genera crossing_conflict new", any(e.get("kind") == "crossing_conflict" and e.get("action") == "new" for e in ev), ev)
    c.update(0.5, {0: vista(waiting=1)}, bordo, {}, None, True)
    s = stato(c)
    check("dopo 0,5 s resta conflict (tenuta)", s["state"] == "conflict" and s["raw"] == "occupied")
    check("durante la tenuta non e' safe", s["safe"] is False)
    c.update(2.0, {0: vista(waiting=1)}, bordo, {}, None, True)
    s = stato(c)
    check("dopo 1,5 s torna occupied safe", s["state"] == "occupied" and s["safe"])
    ev = c.take_events()
    check("crossing_conflict cancel all'uscita", any(e.get("kind") == "crossing_conflict" and e.get("action") == "cancel" for e in ev), ev)
    c.update(2.1, {0: vista(waiting=1, vehicles=[veicolo(1.0)])}, bordo, {}, None, True)
    check("escalation immediata verso conflict", stato(c)["state"] == "conflict")
    tr = c.transitions()
    check("transitions registra i passaggi", any(x.get("to") == "conflict" for x in tr), tr)


def test_robustezza_e_canali():
    print("== robustezza e canali")
    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: {"fresh": True, "vehicles": "rotto", "waiting": "x"}}, {0: {"present": True}}, {}, None, True)
    check("vista malformata: nessuna eccezione, mai conflict falso", stato(c)["state"] in ("occupied", "unknown"))
    c.update(0.0, {0: vista(waiting=1, vehicles=[{"eta_s": "abc"}])}, {}, {}, None, True)
    check("veicolo con ETA non numerico conta come moto ignoto (non safe)", stato(c)["safe"] is False)

    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: vista(waiting=1), 1: None}, {1: {"present": True, "ok": True}}, {}, None, True)
    codes = c.light_codes()
    check("luce per canale: 0 occupied, 1 unknown", codes.get(0) == 1 and codes.get(1) == 6, codes)

    cfg = {"crosswalks": [{"id": 0, "zone": "crosswalk", "light": 0, "curb": 0},
                          {"id": 1, "zone": "crosswalk_2", "light": 0, "curb": 1}]}
    c = CrosswalkController(cfg, "n")
    c.update(0.0, {0: vista(waiting=1), 1: vista(waiting=1, vehicles=[veicolo(2.0)])}, {}, {}, None, True)
    check("due attraversamenti sullo stesso canale: vince il piu' grave", c.light_codes().get(0) == 3, c.light_codes())

    c = CrosswalkController(CFG, "n")
    c.update(0.0, {0: vista()}, {0: {"present": True, "ok": False}}, {}, None, True)
    check("sensore guasto non conta come presenza", stato(c)["state"] == "idle")
    check("state() per l'API", "crosswalks" in c.state() and len(c.state()["crosswalks"]) == 2)


def main():
    for f in (test_scenari_base, test_non_vedente_e_soccorso, test_tenuta_ed_eventi, test_robustezza_e_canali):
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
