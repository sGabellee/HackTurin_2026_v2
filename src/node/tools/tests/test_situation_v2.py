"""Test di situation.py v2 (docs/ARCHITETTURA.md par. 2.5 e 2.8): piu' attraversamenti per camera e
crossing_view().

Prova:
  - zone "crosswalk*" e "waiting_area*": ordine, divieto di sosta per famiglia, e tutte le regole sulle
    strisce su ogni attraversamento (slow_pedestrian, crowd_on_crosswalk, pedestrian_not_yielded,
    pedestrian_red_crossing, illegal_stop, pedestrian_waiting, pedestrian_outside_crosswalk) e i
    riepiloghi degli attraversamenti per zona;
  - compatibilita' v1: con la sola zona "crosswalk" gli scenari base di simulate.py restano identici;
  - crossing_view(): fresco/non fresco, pedoni in attesa e sulle strisce, ciclisti, veicoli che si
    avvicinano con ETA (metri e scala immagine), lato sinistra/destra, veicoli fermi o che si allontanano
    esclusi, zona assente, errori interni (mai via libera);
  - catalogo eventi: i 4 tipi nuovi esistono con categoria/inoltro della specifica e le regole di
    situation non li generano mai.

Uso:  python test_situation_v2.py   (dalla cartella node/tools/tests; esce con codice 1 se qualcosa fallisce)
"""
import contextlib
import copy
import io
import sys
import traceback
from pathlib import Path

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI.parents[1] / "python"))
sys.path.insert(0, str(QUI.parents[0]))

import simulate as sim  # noqa: E402  (generatori di detection finte)
from events import KINDS  # noqa: E402
from situation import RULES, SituationEngine  # noqa: E402

RISULTATI = []
FPS = sim.FPS
NUOVI = ("crossing_conflict", "assisted_crossing", "emergency_corridor", "wake_sensor_fault")


def check(nome, ok, info=""):
    RISULTATI.append((nome, bool(ok), info))


def esegui(fn):
    """Esegue un test con i log dei moduli nascosti, poi stampa [OK ]/[ERR] per ogni controllo.
    Un test che solleva un'eccezione conta come fallito, gli altri continuano."""
    prima = len(RISULTATI)
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log):
            fn()
    except Exception as e:
        RISULTATI.append((fn.__name__, False, f"eccezione {e!r}\n{traceback.format_exc(limit=4)}"))
    for nome, ok, info in RISULTATI[prima:]:
        print(f"[{'OK ' if ok else 'ERR'}] {nome}" + ("" if ok else f"  -> {info}"))


# ---------------------------------------------------------------------- scena di prova
# Come simulate.CFG (strada 10 x 30 m vista dall'alto, strisce "crosswalk" a y 9..12 m) piu' un secondo
# attraversamento "crosswalk_nord" a y 19,5..22,5 m con la sua area d'attesa.

CFG = copy.deepcopy(sim.CFG)
CFG["node_id"] = "incrocio-1"
CFG["zones"].update({
    "sidewalk": [[0.88, 0.0], [1.0, 0.0], [1.0, 1.0], [0.88, 1.0]],
    "waiting_area": [[0.88, 0.5], [1.0, 0.5], [1.0, 0.6], [0.88, 0.6]],
    "crosswalk_nord": [[0.10, 0.25], [0.90, 0.25], [0.90, 0.35], [0.10, 0.35]],
    "waiting_area_nord": [[0.88, 0.15], [1.0, 0.15], [1.0, 0.25], [0.88, 0.25]],
})
CFG_IMG = copy.deepcopy(CFG)
CFG_IMG["calibration"] = {"image_points": None, "world_points_m": None}
DRITTA = sim.DRITTA
Y_NORD = 21.0      # centro di crosswalk_nord (m)
Y_V1 = 10.5        # centro di crosswalk (m)


def detections(gen, tau):
    det = {}
    for label, x1, y1, x2, y2 in gen(tau):
        det.setdefault(label, []).append({"confidence": 0.9, "bounding_box_xyxy": (x1, y1, x2, y2)})
    return det


def run(gen, cfg=None, luce=None, durata=16.0, t0=1000.0):
    msgs = []
    eng = SituationEngine(copy.deepcopy(cfg or CFG), on_event=msgs.append)
    eng.light.set_state(luce or "unknown", t0 - 5)
    for k in range(int(durata * FPS)):
        t = t0 + k / FPS
        eng.update(detections(gen, t - t0), t)
    return eng, msgs


def hz(msgs, kind=None, action=None):
    return [m for m in msgs if m.get("type") == "hazard" and (kind is None or m["kind"] == kind)
            and (action is None or m["action"] == action)]


def zone_di(msgs, kind):
    return sorted({(m.get("where") or {}).get("zone") for m in hz(msgs, kind, "new")})


def views(gen, cfg=None, durata=6.0, t0=1000.0, args=("crosswalk", "waiting_area", "via", "incrocio")):
    """Esegue lo scenario e raccoglie crossing_view a ogni fotogramma: [(tau, vista)]."""
    eng = SituationEngine(copy.deepcopy(cfg or CFG), on_event=lambda m: None)
    out = []
    for k in range(int(durata * FPS)):
        t = t0 + k / FPS
        eng.update(detections(gen, t - t0), t)
        out.append((round(t - t0, 2), eng.crossing_view(t, *args)))
    return eng, out


def a(out, tau):
    return next(v for tt, v in out if abs(tt - tau) < 1e-6)


# ---------------------------------------------------------------------- 1. piu' attraversamenti

def test_zone():
    eng = SituationEngine(copy.deepcopy(CFG), on_event=lambda m: None)
    check("zone strisce in ordine (crosswalk per prima)", eng.crosswalk_zones == ["crosswalk", "crosswalk_nord"],
          eng.crosswalk_zones)
    check("aree d'attesa in ordine", eng.waiting_zones == ["waiting_area", "waiting_area_nord"], eng.waiting_zones)
    check("divieto di sosta: 'crosswalk' vale per tutta la famiglia",
          "crosswalk" in eng.no_stop_zones and "crosswalk_nord" in eng.no_stop_zones
          and "bike_lane" in eng.no_stop_zones, eng.no_stop_zones)
    v1 = SituationEngine(copy.deepcopy(sim.CFG), on_event=lambda m: None)
    check("config v1: una sola zona strisce, divieto di sosta come prima",
          v1.crosswalk_zones == ["crosswalk"] and v1.no_stop_zones == ["crosswalk", "bike_lane"], v1.no_stop_zones)
    cfg = copy.deepcopy(CFG)
    cfg["no_stop_zones"] = ["crosswalk_nord"]
    eng = SituationEngine(cfg, on_event=lambda m: None)
    check("divieto di sosta su una sola striscia", eng.no_stop_zones == ["crosswalk_nord"], eng.no_stop_zones)


def test_regole_su_ogni_striscia():
    _, m = run(sim.pedone(lambda t: (1.5 + 0.5 * t, Y_NORD), 10))
    check("slow_pedestrian su crosswalk_nord", zone_di(m, "slow_pedestrian") == ["crosswalk_nord"],
          zone_di(m, "slow_pedestrian"))
    check("pedone su crosswalk_nord non e' fuori dalle strisce", not hz(m, "pedestrian_outside_crosswalk"),
          [x["kind"] for x in hz(m)])
    _, m = run(sim.pedone(lambda t: (1.5 + 0.5 * t, Y_V1), 10))
    check("slow_pedestrian su crosswalk (v1) come prima", zone_di(m, "slow_pedestrian") == ["crosswalk"],
          zone_di(m, "slow_pedestrian"))

    folla = sim.insieme(*[sim.pedone((lambda i: lambda t: (1.5 + i + 1.2 * t, Y_NORD))(i), 4) for i in range(6)])
    _, m = run(folla)
    check("crowd_on_crosswalk su crosswalk_nord", zone_di(m, "crowd_on_crosswalk") == ["crosswalk_nord"],
          zone_di(m, "crowd_on_crosswalk"))
    folla2 = sim.insieme(folla, *[sim.pedone((lambda i: lambda t: (1.5 + i + 1.2 * t, Y_V1))(i), 4) for i in range(6)])
    _, m = run(folla2)
    check("crowd_on_crosswalk: una per attraversamento (pause separate)",
          zone_di(m, "crowd_on_crosswalk") == ["crosswalk", "crosswalk_nord"]
          and len(hz(m, "crowd_on_crosswalk")) == 2, zone_di(m, "crowd_on_crosswalk"))

    gen = sim.insieme(sim.pedone(lambda t: (1.5 + 1.2 * t, Y_NORD), 6.25), sim.auto(DRITTA(8.33), 3.6))
    _, m = run(gen)
    check("pedestrian_not_yielded su crosswalk_nord", zone_di(m, "pedestrian_not_yielded") == ["crosswalk_nord"],
          zone_di(m, "pedestrian_not_yielded"))
    due = sim.insieme(sim.pedone(lambda t: (1.5 + 1.2 * t, Y_NORD), 6.25),
                      sim.pedone(lambda t: (1.5 + 1.2 * t, Y_V1), 6.25), sim.auto(DRITTA(8.33), 3.6))
    _, m = run(due)
    check("pedestrian_not_yielded: una volta per veicolo e per attraversamento",
          zone_di(m, "pedestrian_not_yielded") == ["crosswalk", "crosswalk_nord"]
          and len(hz(m, "pedestrian_not_yielded")) == 2, [(x["kind"], x.get("where")) for x in hz(m)])

    _, m = run(sim.pedone(lambda t: (9.5 - 1.2 * t, Y_NORD), 7), luce="green")
    check("pedestrian_red_crossing su crosswalk_nord", zone_di(m, "pedestrian_red_crossing") == ["crosswalk_nord"],
          zone_di(m, "pedestrian_red_crossing"))

    def sosta(t):
        if t < 2:
            return 6.4, 27 - 2.78 * t
        if t < 10:
            return 6.4, 27 - 5.56
        return 6.4, 27 - 5.56 - 3 * (t - 10)
    _, m = run(sim.auto(sosta, 13))
    check("illegal_stop su crosswalk_nord: new e cancel",
          zone_di(m, "illegal_stop") == ["crosswalk_nord"] and hz(m, "illegal_stop", "cancel"),
          [(x["kind"], x["action"], x.get("where")) for x in hz(m)])

    _, m = run(sim.pedone(lambda t: (9.4, 24.0), 25), durata=30)
    check("pedestrian_waiting in waiting_area_nord (new e cancel)",
          zone_di(m, "pedestrian_waiting") == ["waiting_area_nord"] and hz(m, "pedestrian_waiting", "cancel"),
          [(x["kind"], x["action"], x.get("where")) for x in hz(m)])


def test_riepiloghi_per_attraversamento():
    _, m = run(sim.pedone(lambda t: (1.5 + 1.2 * t, Y_NORD), 6.5))
    p = [x for x in m if x.get("type") == "track" and x["class"] == "person"]
    cc = p[0].get("crosswalk_crossing") if p else None
    check("riepilogo: attraversamento di crosswalk_nord con il nome della zona",
          cc and cc["direction"] == "crosswalk_nord:A->B" and cc["zone"] == "crosswalk_nord", p[:1])
    _, m = run(sim.pedone(lambda t: (8.5 - 1.2 * t, Y_V1), 6.5))
    p = [x for x in m if x.get("type") == "track" and x["class"] == "person"]
    cc = p[0].get("crosswalk_crossing") if p else None
    check("riepilogo: crosswalk v1 con la direzione di sempre", cc and cc["direction"] == "B->A", cc)

    def giro(t):  # attraversa crosswalk_nord, scende sul marciapiede, riattraversa crosswalk
        if t < 6.5:
            return 1.5 + 1.2 * t, Y_NORD
        if t < 11:
            return 9.3, Y_NORD - (Y_NORD - Y_V1) * (t - 6.5) / 4.5
        return 9.3 - 1.2 * (t - 11), Y_V1
    _, m = run(sim.pedone(giro, 17.5), durata=22)
    p = [x for x in m if x.get("type") == "track" and x["class"] == "person"]
    tutti = p[0].get("crosswalk_crossings") if p else None
    check("riepilogo: due attraversamenti nella stessa traccia, in ordine di tempo",
          tutti and [c["zone"] for c in tutti] == ["crosswalk_nord", "crosswalk"]
          and p[0]["crosswalk_crossing"]["zone"] == "crosswalk_nord", p[0].get("crosswalk_crossings") if p else p)


def test_compatibilita_v1():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ok = sim.main() == 0
    check("simulate.py: i 15 scenari base con la sola zona 'crosswalk' restano identici", ok,
          out.getvalue()[-800:])


# ---------------------------------------------------------------------- 2. crossing_view

def test_vista_base():
    eng = SituationEngine(copy.deepcopy(CFG), on_event=lambda m: None)
    v = eng.crossing_view(1000.0, "crosswalk", "waiting_area", "via", "incrocio")
    chiavi = {"fresh", "waiting", "crossing", "blind_like", "cyclists", "vehicles", "min_eta_s"}
    check("vista: tutte le chiavi della specifica", chiavi <= set(v), sorted(v))
    check("vista: nessun fotogramma -> non fresca", v["fresh"] is False and v["vehicles"] == [] and v["min_eta_s"] is None, v)

    gen = sim.insieme(sim.auto(DRITTA(8.33), 3.6), sim.pedone(lambda t: (9.4, 13.0), 6))
    eng, out = views(gen, durata=4.0)
    v = a(out, 0.5)
    veh = v["vehicles"][0] if v["vehicles"] else {}
    atteso = (30 - 8.33 * 0.5 - 12.0) / 8.33   # distanza dal bordo delle strisce / velocita'
    check("vista: veicolo da 'via' a sinistra, ETA in secondi (metri / m/s)",
          veh.get("side") == "left" and abs(veh.get("eta_s", 99) - atteso) < 0.15 and v["unit"] == "m"
          and abs(veh.get("speed", 0) - 8.33) < 0.3 and v["min_eta_s"] == veh.get("eta_s"), v)
    check("vista: campi del veicolo", {"track", "class", "eta_s", "side", "speed"} <= set(veh) and veh["class"] == "vehicle", veh)
    check("vista: fresca, pedone in attesa contato", v["fresh"] is True and v["waiting"] == 1 and v["crossing"] == 0, v)
    check("vista: blind_like sempre False", v["blind_like"] is False)
    v = a(out, 1.5)
    check("vista: l'ETA scende mentre il veicolo arriva", v["min_eta_s"] is not None and v["min_eta_s"] < a(out, 0.5)["min_eta_s"],
          (a(out, 0.5)["min_eta_s"], v["min_eta_s"]))
    v = a(out, 2.25)
    check("vista: veicolo sulle strisce -> ETA 0", v["min_eta_s"] == 0.0, v["vehicles"])
    v = a(out, 3.0)
    check("vista: veicolo oltre le strisce che si allontana -> escluso", v["vehicles"] == [], v["vehicles"])
    t_last = 1000.0 + (len(out) - 1) / FPS
    v = eng.crossing_view(t_last + 2.0, "crosswalk", "waiting_area", "via", "incrocio")
    check("vista: ultimo fotogramma vecchio di 2 s -> non fresca", v["fresh"] is False and v["age_s"] >= 1.9, v)


def test_vista_lati_e_esclusioni():
    _, out = views(sim.auto(lambda t: (3.0, 0.0 + 8.33 * t), 3.6), durata=2.0)
    v = a(out, 0.5)
    veh = v["vehicles"][0] if v["vehicles"] else {}
    check("vista: veicolo da 'incrocio' (dal basso) -> destra", veh.get("side") == "right" and veh.get("eta_s") is not None, v)

    _, out = views(sim.auto(lambda t: (3.0, 15.0 + 8.33 * t), 1.5), durata=1.5)
    check("vista: veicolo oltre le strisce che si allontana -> mai nella lista",
          all(v["vehicles"] == [] for _, v in out), [v["vehicles"] for _, v in out])

    _, out = views(sim.auto(lambda t: (6.4, 16.0 - 2.78 * min(t, 1.0)), 8.0), durata=8.0)
    v = a(out, 6.0)
    check("vista: veicolo fermo prima delle strisce -> non si avvicina", v["vehicles"] == [], v["vehicles"])

    _, out = views(sim.auto(lambda t: (6.4, 30 - 3.0 * t), 8.0), durata=3.0)
    v = a(out, 0.5)
    check("vista: veicolo lento a 16,5 m -> ETA ~5,5 s", v["min_eta_s"] is not None and 4.8 <= v["min_eta_s"] <= 6.2,
          v["min_eta_s"])

    gen = sim.insieme(sim.pedone(lambda t: (2.0 + 1.2 * t, Y_V1), 4), sim.ciclista(lambda t: (5.0, Y_V1 + 0.3), 4),
                      sim.ciclista(lambda t: (8.3, 25 - 3 * t), 4))
    _, out = views(gen, durata=2.0)
    v = a(out, 1.0)
    check("vista: pedone sulle strisce e ciclisti (strisce / ciclabile)",
          v["crossing"] == 1 and v["cyclists"] == 1 and v["bike_lane_cyclists"] == 1, v)

    _, out = views(sim.auto(DRITTA(8.33), 3.6), durata=2.0,
                   args=("crosswalk_nord", "waiting_area_nord", "via", "incrocio"))
    v = a(out, 0.5)
    atteso = (30 - 8.33 * 0.5 - 22.5) / 8.33
    check("vista: seconda striscia (crosswalk_nord) con il suo ETA",
          v["min_eta_s"] is not None and abs(v["min_eta_s"] - atteso) < 0.15 and v["zone"] == "crosswalk_nord",
          (v["min_eta_s"], atteso))

    _, out = views(sim.pedone(lambda t: (9.4, 24.0), 3), durata=2.0,
                   args=("crosswalk_nord", ["waiting_area", "waiting_area_nord"], "via", "incrocio"))
    check("vista: area d'attesa come lista (una per lato)", a(out, 1.0)["waiting"] == 1, a(out, 1.0))


def test_vista_senza_calibrazione():
    gen = sim.insieme(sim.auto(DRITTA(8.33), 3.6))
    _, out = views(gen, cfg=CFG_IMG, durata=2.0)
    v = a(out, 0.5)
    atteso = (30 - 8.33 * 0.5 - 12.0) / 8.33
    check("vista in scala immagine: unita' 'img' e stesso ETA",
          v["unit"] == "img" and v["min_eta_s"] is not None and abs(v["min_eta_s"] - atteso) < 0.2
          and v["vehicles"][0]["side"] == "left", v)


def test_vista_robusta():
    eng, out = views(sim.auto(DRITTA(8.33), 3.6), durata=1.0)
    t = 1000.0 + (len(out) - 1) / FPS
    v = eng.crossing_view(t, "crosswalk_inesistente", None, "via", "incrocio")
    check("vista: zona assente -> non fresca, errore, nessuna eccezione",
          v["fresh"] is False and v.get("error") and v["vehicles"] == [], v)
    v = eng.crossing_view(t, "crosswalk", "area_che_non_ce", None, None)
    check("vista: area d'attesa assente e accessi ignoti -> lato 'unknown', ma vista valida",
          v["fresh"] is True and all(x["side"] == "unknown" for x in v["vehicles"]), v)
    eng._crossing_view = lambda *a: 1 / 0
    v = eng.crossing_view(t, "crosswalk", "waiting_area", "via", "incrocio")
    check("vista: errore interno -> non fresca (mai via libera)", v["fresh"] is False and v.get("error"), v)
    eng = SituationEngine(copy.deepcopy(CFG), on_event=lambda m: None)
    eng.update({"car": "x"}, 1000.0)
    v = eng.crossing_view(1000.1, "crosswalk", None, "via", "incrocio")
    check("vista: detection malformate -> vista fresca e vuota", v["fresh"] is True and v["vehicles"] == [], v)


# ---------------------------------------------------------------------- 3. catalogo eventi

def test_catalogo():
    attesi = {"crossing_conflict": ("risk", False), "assisted_crossing": ("info", False),
              "emergency_corridor": ("emergency", True), "wake_sensor_fault": ("info", False)}
    for kind, (cat, inoltro) in attesi.items():
        k = KINDS.get(kind)
        check(f"catalogo: {kind} categoria {cat}, inoltro {inoltro}",
              k is not None and k[6] == cat and k[3] is inoltro and isinstance(k[0], int)
              and isinstance(k[2], list) and k[4] > 0, k)
    check("catalogo: crossing_conflict e' un collisionRisk ETSI (97)", KINDS["crossing_conflict"][0] == 97)
    check("catalogo: emergency_corridor e' emergencyVehicleApproaching (95)", KINDS["emergency_corridor"][0] == 95)
    check("catalogo: i tipi nuovi non sono regole di situation", not set(NUOVI) & set(RULES))
    gen = sim.insieme(sim.pedone(lambda t: (1.5 + 1.2 * t, Y_NORD), 6.25), sim.auto(DRITTA(8.33), 3.6),
                      sim.auto(DRITTA(8.33), 3.6, label="ambulance"), sim.pedone(lambda t: (9.4, 13.0), 6))
    _, m = run(gen)
    check("situation non genera mai i tipi nuovi", not [x for x in hz(m) if x["kind"] in NUOVI],
          sorted({x["kind"] for x in hz(m)}))


def main():
    for fn in (test_zone, test_regole_su_ogni_striscia, test_riepiloghi_per_attraversamento, test_compatibilita_v1,
               test_vista_base, test_vista_lati_e_esclusioni, test_vista_senza_calibrazione, test_vista_robusta,
               test_catalogo):
        print(f"\n== {fn.__name__}")
        esegui(fn)
    falliti = [r for r in RISULTATI if not r[1]]
    print(f"\n{len(RISULTATI) - len(falliti)}/{len(RISULTATI)} controlli OK")
    for nome, _, info in falliti:
        print(f"  FALLITO: {nome} -> {str(info)[:300]}")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
