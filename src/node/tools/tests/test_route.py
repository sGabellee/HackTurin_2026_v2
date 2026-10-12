"""Test della topologia e dell'instradamento (route.py, ARCHITETTURA.md §4).

Prova su una piccola citta' sintetica (griglia 3 x 4 con un tratto lungo e una strada curva):
vicini in entrambi i sensi, direzioni (scritte, dalla geometria, dalle coordinate), distanze,
incrocio piu' vicino, primo incrocio verso una direzione, catena "dritta" lungo la strada,
percorso piu' breve in metri, destinatari per direzione / senza direzione / verso una meta,
conversione dei punti cardinali, caricamento da file e robustezza. Se c'e' gia'
src/node/python/topology.json v2 (generato da OpenStreetMap) lo prova anche su quello.

Uso:  python test_route.py      (dalla cartella node/tools/tests; esce con codice 1 se qualcosa fallisce)
"""
import json
import math
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

QUI = Path(__file__).resolve().parent
PY = QUI.parents[1] / "python"
sys.path.insert(0, str(PY))

import route  # noqa: E402
from route import Topology, compass_deg, compass_name  # noqa: E402

RISULTATI = []


def check(nome, ok, info=""):
    RISULTATI.append((nome, bool(ok), info))
    print(f"[{'OK ' if ok else 'ERR'}] {nome}" + ("" if ok else f"  -> {info}"))


def test(fn):
    """Decoratore: un test che solleva un'eccezione conta come fallito, gli altri continuano."""
    def wrapper():
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, f"eccezione {e!r}\n{traceback.format_exc(limit=4)}")
    wrapper.__name__ = fn.__name__
    return wrapper


# ---------------------------------------------------------------------- citta' sintetica

LAT0, LON0 = 45.0650, 7.6650
DLAT = 100 / 111320                                        # 100 m verso nord
DLON = 100 / (111320 * math.cos(math.radians(LAT0)))       # 100 m verso est


def p(r, c):
    """Riga r (0 = la piu' a nord), colonna c (0 = la piu' a ovest)."""
    return (LAT0 - r * DLAT, LON0 + c * DLON)


def citta():
    """Griglia 3 x 4 (n<r><c>, 100 m), vicini scritti SOLO verso est e verso sud; tratto lungo
    n00 - n23 ("Tangenziale", 2000 m) solo in "edges"; strada curva n03 - x con la geometria."""
    nodes = {}
    for r in range(3):
        for c in range(4):
            nid = f"n{r}{c}"
            lat, lon = p(r, c)
            nb = {}
            if c < 3:   # verso est: direzione scritta
                nb[f"n{r}{c + 1}"] = {"bearing": 90.0, "street": f"Via R{r}", "m": 100.0}
            if r < 2:   # verso sud: direzione dalle coordinate, distanza dalle coordinate
                nb[f"n{r + 1}{c}"] = {"street": f"Corso C{c}"}
            nodes[nid] = {"name": f"Via R{r} / Corso C{c}", "lat": lat, "lon": lon,
                          "streets": [f"Via R{r}", f"Corso C{c}"], "neighbors": nb}
    xl = (LAT0 + DLAT, LON0 + 4 * DLON)                    # a nord-est di n03
    nodes["x"] = {"name": "Via Curva", "lat": xl[0], "lon": xl[1], "neighbors": {}}
    edges = [{"a": "n00", "b": "n23", "street": "Tangenziale", "m": 2000.0},
             # la strada esce da n03 verso NORD e poi gira a est verso x
             {"a": "x", "b": "n03", "street": "Via Curva", "m": 200.0,
              "geom": [list(xl), [LAT0 + DLAT, LON0 + 3 * DLON], list(p(0, 3))]}]
    return {"version": 2, "source": "sintetica", "nodes": nodes, "edges": edges}


@test
def test_struttura():
    t = Topology.from_dict(citta())
    check("ids ordinati (12 + x)", t.ids() == sorted([f"n{r}{c}" for r in range(3) for c in range(4)] + ["x"]),
          t.ids())
    check("len e contains", len(t) == 13 and "n12" in t and "zz" not in t)
    check("pos", t.pos("n12") == p(1, 2) and t.pos("zz") is None)
    check("nome", t.name("n11") == "Via R1 / Corso C1" and t.name("zz") is None)
    check("vicini in entrambi i sensi (scritti solo est/sud)", t.neighbors("n11") == ["n01", "n10", "n12", "n21"],
          t.neighbors("n11"))
    check("vicini da edges (tratto lungo)", "n23" in t.neighbors("n00") and "n00" in t.neighbors("n23"))
    check("vicini di x (solo da edges)", t.neighbors("x") == ["n03"], t.neighbors("x"))
    check("incrocio sconosciuto: nessun vicino", t.neighbors("zz") == [])
    check("strada nei due sensi", t.street("n11", "n12") == t.street("n12", "n11") == "Via R1")
    check("strada del corso", t.street("n01", "n11") == "Corso C1")
    check("metri scritti", t.meters("n10", "n11") == 100.0 and t.meters("n11", "n10") == 100.0)
    check("metri dal tratto lungo", t.meters("n00", "n23") == 2000.0)
    check("metri dalle coordinate (~100)", abs(t.meters("n01", "n11") - 100) < 1, t.meters("n01", "n11"))
    ed = t.edges()
    coppie = {(e["a"], e["b"]) for e in ed}
    check("un tratto per coppia", len(ed) == len(coppie) == 8 + 9 + 2, len(ed))
    check("geometria nel tratto curvo", any(e["street"] == "Via Curva" and len(e["geom"]) == 3 for e in ed))
    s = t.summary()
    check("riepilogo", s["nodes"] == 13 and s["edges"] == 19 and s["with_position"] == 13, s)


@test
def test_direzioni():
    t = Topology.from_dict(citta())
    check("bearing scritto (90)", t.bearing("n10", "n11") == 90.0)
    check("bearing al contrario dalle coordinate (~270)", abs(t.bearing("n11", "n10") - 270) < 0.5,
          t.bearing("n11", "n10"))
    check("bearing verso sud dalle coordinate (~180)", abs(t.bearing("n01", "n11") - 180) < 0.5)
    check("bearing verso nord (~0)", min(t.bearing("n11", "n01"), 360 - t.bearing("n11", "n01")) < 0.5)
    check("bearing dalla geometria: n03 -> x esce verso nord", min(t.bearing("n03", "x"), 360 - t.bearing("n03", "x")) < 1,
          t.bearing("n03", "x"))
    check("bearing dalla geometria: x -> n03 esce verso ovest", abs(t.bearing("x", "n03") - 270) < 1,
          t.bearing("x", "n03"))
    check("bearing tra non vicini: retta (~135)", abs(t.bearing("n00", "n11") - 135) < 1, t.bearing("n00", "n11"))
    check("bearing con incrocio sconosciuto: None", t.bearing("n00", "zz") is None)


@test
def test_nearest_first_along():
    t = Topology.from_dict(citta())
    lat, lon = p(1, 2)
    check("nearest", t.nearest(lat + 0.0001, lon - 0.0001) == "n12")
    check("nearest su topologia vuota: None", Topology.from_dict({}).nearest(lat, lon) is None)
    check("nearest con coordinate non valide: None", t.nearest("a", None) is None)
    check("first_toward est", t.first_toward("n11", 90) == "n12")
    check("first_toward nord", t.first_toward("n11", 0) == "n01")
    check("first_toward 200 gradi -> sud", t.first_toward("n11", 200) == "n21")
    check("first_toward con nome italiano", t.first_toward("n11", "ovest") == "n10")
    check("first_toward con nome inglese", t.first_toward("n11", "South") == "n21")
    check("first_toward senza direzione: None", t.first_toward("n11", None) is None)
    check("first_toward oltre la curva massima: None", t.first_toward("n11", 45, max_turn=30) is None)
    check("first_toward incrocio sconosciuto: None", t.first_toward("zz", 90) is None)
    check("along dritto lungo la via", t.along("n10", "n11", 3) == ["n11", "n12", "n13"], t.along("n10", "n11", 3))
    check("along lungo il corso", t.along("n01", "n11", 2) == ["n11", "n21"])
    check("along 1 = solo il primo", t.along("n10", "n11", 1) == ["n11"])
    check("along 0 = nessuno", t.along("n10", "n11", 0) == [])
    check("along con curva massima 45: si ferma a fine via", t.along("n10", "n11", 6, max_turn=45) == ["n11", "n12", "n13"],
          t.along("n10", "n11", 6, max_turn=45))
    lungo = t.along("n10", "n11", 6)
    check("along: mai indietro, mai ripetuti", "n10" not in lungo and len(set(lungo)) == len(lungo) <= 6, lungo)
    check("along: la catena e' fatta di vicini", all(b in t.neighbors(a) for a, b in zip(["n10"] + lungo, lungo)))


@test
def test_stessa_strada():
    """A un bivio: un ramo resta sulla stessa strada con una curva di 20 gradi, l'altro cambia
    strada con 10 gradi: si resta sulla stessa strada."""
    def pt(dx_m, dy_m):
        return [LAT0 + dy_m / 111320, LON0 + dx_m / (111320 * math.cos(math.radians(LAT0)))]

    def verso(deg, m=100):
        return pt(m * math.sin(math.radians(deg)), m * math.cos(math.radians(deg)))
    a, b = pt(-100, 0), pt(0, 0)
    stessa = [b[0] + (verso(110)[0] - LAT0), b[1] + (verso(110)[1] - LON0)]   # 90 + 20
    altra = [b[0] + (verso(80)[0] - LAT0), b[1] + (verso(80)[1] - LON0)]      # 90 - 10
    data = {"nodes": {"a": {"lat": a[0], "lon": a[1], "neighbors": {"b": {"street": "Corso Re"}}},
                      "b": {"lat": b[0], "lon": b[1], "neighbors": {"s": {"street": "Corso Re"},
                                                                    "o": {"street": "Via Altra"}}},
                      "s": {"lat": stessa[0], "lon": stessa[1]}, "o": {"lat": altra[0], "lon": altra[1]}}}
    t = Topology.from_dict(data)
    check("bivio: resta sulla stessa strada", t.along("a", "b", 2) == ["b", "s"], t.along("a", "b", 2))
    data["nodes"]["b"]["neighbors"]["o"]["street"] = "Corso Re"
    data["nodes"]["b"]["neighbors"]["s"]["street"] = "Via Altra"
    t = Topology.from_dict(data)
    check("bivio: senza la stessa strada vince la curva minore", t.along("a", "b", 2) == ["b", "o"], t.along("a", "b", 2))


@test
def test_percorsi():
    t = Topology.from_dict(citta())
    pr = t.path("n00", "n23")
    check("percorso piu' breve in metri (evita i 2000 m)", len(pr) == 6 and pr[0] == "n00" and pr[-1] == "n23"
          and "n23" not in pr[1:-1], pr)
    check("lunghezza del percorso ~500 m", abs(t.path_m(pr) - 500) < 2, t.path_m(pr))
    check("percorso: passi tra vicini", all(b in t.neighbors(a) for a, b in zip(pr, pr[1:])))
    check("percorso verso se stesso", t.path("n00", "n00") == ["n00"])
    check("percorso verso sconosciuto: []", t.path("n00", "zz") == [] and t.path("zz", "n00") == [])
    iso = citta()
    iso["nodes"]["solo"] = {"lat": LAT0, "lon": LON0 + 0.01}
    check("percorso verso un incrocio isolato: []", Topology.from_dict(iso).path("n00", "solo") == [])
    # con il tratto lungo corto diventa la scelta migliore
    corto = citta()
    corto["edges"][0]["m"] = 50.0
    check("percorso con il tratto lungo accorciato", Topology.from_dict(corto).path("n00", "n23") == ["n00", "n23"])


@test
def test_destinatari():
    t = Topology.from_dict(citta())
    check("direzione nota: 2 incroci lungo la via", t.targets_for("n10", 90) == ["n11", "n12"], t.targets_for("n10", 90))
    check("emergenza: 3 incroci", t.targets_for("n10", 90, category="emergency") == ["n11", "n12", "n13"])
    check("veloce (50 km/h): 3 incroci", t.targets_for("n10", 90, speed_kmh=50) == ["n11", "n12", "n13"])
    check("40 km/h non e' 'oltre 40': 2 incroci", t.targets_for("n10", 90, speed_kmh=40) == ["n11", "n12"])
    check("velocita' non numerica: 2 incroci", t.targets_for("n10", 90, speed_kmh="boh") == ["n11", "n12"])
    check("direzione come nome", t.targets_for("n10", "est") == ["n11", "n12"])
    check("direzione come stringa numerica", t.targets_for("n10", "90") == ["n11", "n12"])
    check("direzione ignota: tutti i vicini", t.targets_for("n11") == ["n01", "n10", "n12", "n21"])
    check("direzione non riconosciuta ('via'): tutti i vicini", t.targets_for("n11", "via") == ["n01", "n10", "n12", "n21"])
    tg = t.targets_for("n00", 300)   # nord-ovest: nessun vicino entro 100 gradi
    check("nessun vicino in quella direzione: tutti i vicini", tg == ["n01", "n10", "n23"], tg)
    check("meta nota: tutto il percorso", t.targets_for("n00", dst="n23") == t.path("n00", "n23")[1:])
    check("meta vince sulla direzione", t.targets_for("n00", 90, dst="n20") == t.path("n00", "n20")[1:])
    check("meta = se stesso: nessuno", t.targets_for("n00", dst="n00") == [])
    check("meta sconosciuta: solo lei", t.targets_for("n00", dst="board-99") == ["board-99"])
    check("sorgente sconosciuta senza direzione: nessuno", t.targets_for("zz") == [])
    check("sorgente sconosciuta con meta: la meta", t.targets_for("zz", dst="n00") == ["n00"])
    for src in t.ids():
        for hd in (None, 0, 45, 90, 135, 180, 225, 270, 315):
            for cat in ("risk", "emergency"):
                out = t.targets_for(src, hd, cat)
                if src in out or len(set(out)) != len(out) or len(out) > max(3, len(t.neighbors(src))):
                    check(f"destinatari coerenti {src} {hd} {cat}", False, out)
                    return
    check("destinatari coerenti per ogni incrocio e direzione (mai se stesso, mai doppi)", True)
    no_fw = citta()
    no_fw["forward_unknown_direction"] = False
    check("forward_unknown_direction false: direzione ignota -> nessuno",
          Topology.from_dict(no_fw).targets_for("n11") == [])


@test
def test_punti_cardinali():
    casi = {"nord": 0, "Nord-Est": 45, "nord est": 45, "nordest": 45, "north_east": 45, "NE": 45, "est": 90,
            "E": 90, "sud-est": 135, "SE": 135, "sud": 180, "south": 180, "s": 180, "sud-ovest": 225, "SO": 225,
            "sw": 225, "ovest": 270, "West": 270, "o": 270, "w": 270, "nord-ovest": 315, "northwest": 315,
            "NO": 315, "nw": 315, "N": 0, 90: 90, 450: 90, -90: 270, "135": 135, " 22.5 ": 22.5}
    sbagliati = {k: compass_deg(k) for k, v in casi.items() if compass_deg(k) is None or abs(compass_deg(k) - v) > 1e-9}
    check("compass_deg su nomi, sigle e numeri", not sbagliati, sbagliati)
    nulli = [v for v in (None, "", "via", "incrocio", [], {}, True, float("nan"), "nord-nord") if compass_deg(v) is not None]
    check("compass_deg: non direzioni -> None", not nulli, nulli)
    check("compass_name", compass_name(225) == "sud-ovest" and compass_name(359) == "nord" and compass_name(None) is None)
    check("turn_deg", route.turn_deg(350, 10) == 20 and route.turn_deg(90, 270) == 180)


@test
def test_file_e_ricarica():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "topology.json"
        f.write_text(json.dumps(citta()), encoding="utf-8")
        t = Topology.load(f)
        check("load da file", len(t) == 13 and t.neighbors("n11") == ["n01", "n10", "n12", "n21"])
        check("niente da ricaricare se il file non cambia", t.maybe_reload() is False)
        f.write_text("{ non valido", encoding="utf-8")
        os.utime(f, (time.time() + 5, time.time() + 5))
        check("file rovinato: tiene la versione precedente", t.maybe_reload() is False and len(t) == 13)
        nuova = citta()
        nuova["nodes"]["n99"] = {"lat": LAT0, "lon": LON0 - DLON, "neighbors": {"n00": {"street": "Via Nuova"}}}
        f.write_text(json.dumps(nuova), encoding="utf-8")
        os.utime(f, (time.time() + 10, time.time() + 10))
        check("file aggiornato: ricaricato", t.maybe_reload() is True and "n99" in t.neighbors("n00"))
        vuota = Topology.load(Path(tmp) / "manca.json")
        check("file mancante: topologia vuota", len(vuota) == 0 and vuota.targets_for("n00", 90) == []
              and vuota.path("a", "b") == [])
        rotto = Path(tmp) / "rotto.json"
        rotto.write_text("[1, 2, 3]", encoding="utf-8")
        check("file non oggetto: topologia vuota", len(Topology.load(rotto)) == 0)
        profondo = Path(tmp) / "profondo.json"
        profondo.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
        check("JSON annidatissimo: topologia vuota", len(Topology.load(profondo)) == 0)


@test
def test_robustezza():
    strani = [None, [], "x", {"nodes": []}, {"nodes": {"a": None, "b": 5, "c": {"lat": "x", "lon": 7}}},
              {"nodes": {"a": {"lat": 45, "lon": 7, "neighbors": {"b": 5, "c": None, "d": {"bearing": "x", "m": -3}}}},
               "edges": [None, 5, {"a": "a"}, {"a": "a", "b": "a"}, {"a": "a", "b": "e", "geom": 7},
                         {"a": "a", "b": "f", "geom": [[1], "x", [45.001, 7.0]]}]},
              {"nodes": {"a": {"lat": 999, "lon": 7}}}]
    for i, d in enumerate(strani):
        t = Topology(d)
        for nid in t.ids() + ["zz"]:
            t.neighbors(nid)
            t.targets_for(nid, 90)
            t.targets_for(nid)
            t.targets_for(nid, dst="a")
            t.nearest(45, 7)
    check("dati strani: nessuna eccezione", True)
    t = Topology(strani[5])
    check("vicino scritto come v1/dizionario ignorato se non valido", sorted(t.neighbors("a")) == ["d", "e", "f"],
          t.neighbors("a"))
    check("metri negativi ignorati", t.meters("a", "d") == route.DEFAULT_EDGE_M)
    check("coordinate fuori scala ignorate", Topology(strani[6]).pos("a") is None)


@test
def test_formato_v1():
    v1 = {"forward_unknown_direction": True, "nodes": {
        "incrocio-1": {"lat": 45.06536, "lon": 7.66689, "neighbors": {"sud-ovest": "incrocio-2", "est": "incrocio-8"}},
        "incrocio-2": {"lat": 45.06462, "lon": 7.666421, "neighbors": {"nord-est": "incrocio-1"}},
        "incrocio-8": {"lat": 45.06477, "lon": 7.66830, "neighbors": {}},
        "incrocio-9": {"neighbors": {"nord": "incrocio-8"}}}}
    t = Topology.from_dict(v1)
    check("v1: vicini", t.neighbors("incrocio-1") == ["incrocio-2", "incrocio-8"] and t.neighbors("incrocio-8")
          == ["incrocio-1", "incrocio-9"], (t.neighbors("incrocio-1"), t.neighbors("incrocio-8")))
    check("v1: direzione dalle coordinate", t.first_toward("incrocio-1", "sud-ovest") == "incrocio-2")
    check("v1: senza coordinate, direzione dal nome della via", t.bearing("incrocio-9", "incrocio-8") == 0.0)
    server = PY.parents[1] / "server" / "topology.json"
    if server.exists():
        ts = Topology.load(server)
        check("v1: topology.json del server", len(ts) >= 2 and all(ts.neighbors(n) for n in ts.ids()))


@test
def test_topologia_vera():
    f = PY / "topology.json"
    try:
        raw = json.loads(f.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        print("     (src/node/python/topology.json non c'e' ancora: salto)")
        return
    if not isinstance(raw, dict) or raw.get("version") != 2:
        print("     (src/node/python/topology.json non e' v2: salto)")
        return
    t = Topology.load(f)
    ids = t.ids()
    check(f"topologia vera: {len(ids)} incroci caricati", len(ids) == len(raw.get("nodes") or {}) > 0)
    asim = [(a, b) for a in ids for b in t.neighbors(a) if a not in t.neighbors(b)]
    check("topologia vera: vicini simmetrici", not asim, asim[:5])
    check("topologia vera: tutti con coordinate", all(t.pos(n) for n in ids), [n for n in ids if not t.pos(n)][:5])
    senza_dir = [(a, b) for a in ids for b in t.neighbors(a) if t.bearing(a, b) is None]
    check("topologia vera: tutti i tratti con direzione", not senza_dir, senza_dir[:5])
    check("topologia vera: metri positivi", all((e["m"] or 0) > 0 for e in t.edges()))
    if len(ids) >= 2:
        pr = t.path(ids[0], ids[-1])
        check("topologia vera: percorso tra il primo e l'ultimo incrocio",
              pr and pr[0] == ids[0] and pr[-1] == ids[-1] and all(b in t.neighbors(a) for a, b in zip(pr, pr[1:])),
              pr)
    errori = []
    t0 = time.time()
    for src in ids:
        for hd in (None, 0, 90, 180, 270):
            for cat in ("risk", "emergency"):
                out = t.targets_for(src, hd, cat)
                if src in out or len(set(out)) != len(out) or any(x not in t for x in out):
                    errori.append((src, hd, cat, out))
                if hd is not None and out and out[0] not in t.neighbors(src):
                    errori.append((src, hd, "primo non vicino", out))
    check(f"topologia vera: destinatari coerenti per tutti ({time.time() - t0:.2f} s)", not errori, errori[:5])
    vela = t.nearest(45.06536, 7.66689)
    check("topologia vera: incrocio piu' vicino a Via Vela", vela is not None, vela)


def main():
    for fn in (test_struttura, test_direzioni, test_nearest_first_along, test_stessa_strada, test_percorsi,
               test_destinatari, test_punti_cardinali, test_file_e_ricarica, test_robustezza, test_formato_v1,
               test_topologia_vera):
        print(f"\n== {fn.__name__}")
        fn()
    falliti = [r for r in RISULTATI if not r[1]]
    print(f"\n{len(RISULTATI) - len(falliti)}/{len(RISULTATI)} controlli OK")
    for nome, _, info in falliti:
        print(f"  FALLITO: {nome} -> {str(info)[:300]}")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
