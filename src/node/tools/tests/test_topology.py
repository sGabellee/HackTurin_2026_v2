"""Prove della topologia della citta' (tools/build_topology.py) e del file generato python/topology.json.

Copre: semplificazione delle geometrie, un piccolo dataset sintetico (croce, T, angoli con cambio di nome,
vicolo cieco con e senza nome, way spezzata, strada curva con punti intermedi, corso a doppia carreggiata
da raggruppare, sensi unici, isola scollegata, strada che esce dall'area, rotatoria), id e nomi, simmetria
dei vicini, download da Overpass con server finti (niente rete) e i controlli sul topology.json generato.

Uso:  python test_topology.py      (dalla cartella src/node/tools/tests; esce con codice 1 se qualcosa fallisce)
"""
import contextlib
import copy
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI.parents[1] / "python"))      # ../../python (moduli del nodo)
sys.path.insert(0, str(QUI.parent))                     # ../ (tools: build_topology)

import build_topology as bt  # noqa: E402

TOPOLOGY = QUI.parents[1] / "python" / "topology.json"
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


# ---------------------------------------------------------------------- dati sintetici

LAT0, LON0 = 45.0, 7.0
K_LON = bt.M_PER_DEG * math.cos(math.radians(LAT0))


def pt(x, y):
    """Metri (x verso est, y verso nord) -> (lat, lon) attorno a (45.0, 7.0)."""
    return (LAT0 + y / bt.M_PER_DEG, LON0 + x / K_LON)


def xy(lat, lon):
    return ((lon - LON0) * K_LON, (lat - LAT0) * bt.M_PER_DEG)


class FintoOSM:
    """Costruisce un JSON come quello di Overpass "out body geom"; i punti uguali condividono l'id OSM."""

    def __init__(self):
        self.ids, self.ways = {}, []

    def nodo(self, x, y):
        k = (round(x, 3) + 0.0, round(y, 3) + 0.0)
        if k not in self.ids:
            self.ids[k] = 1000 + len(self.ids)
        return self.ids[k]

    def way(self, nome, tipo, punti, **tag):
        tags = {"highway": tipo}
        if nome:
            tags["name"] = nome
        tags.update(tag)
        geom = [{"lat": pt(x, y)[0], "lon": pt(x, y)[1]} for x, y in punti]
        self.ways.append({"type": "way", "id": 1 + len(self.ways), "nodes": [self.nodo(x, y) for x, y in punti],
                          "geometry": geom, "tags": tags})

    def dati(self, solo_nodi=False):
        if not solo_nodi:
            return {"version": 0.6, "osm3s": {"timestamp_osm_base": "2026-01-01T00:00:00Z"},
                    "elements": copy.deepcopy(self.ways)}
        nodi = [{"type": "node", "id": i, "lat": pt(*k)[0], "lon": pt(*k)[1]} for k, i in self.ids.items()]
        return {"elements": nodi + [{k: v for k, v in w.items() if k != "geometry"} for w in self.ways]}


ARCO = [(float(x), 200.0 + (30.0 * math.sin(math.pi * x / 100.0) if 0 < x < 100 else 0.0)) for x in range(0, 101, 5)]


def citta():
    o = FintoOSM()
    o.way("Via Nord", "residential", ARCO + [(150, 200), (200, 200), (300, 200), (400, 200)])   # curva a ovest
    o.way("Via Centro", "secondary", [(0, 100), (50, 100), (100, 100), (150, 100), (200, 100)])
    o.way("Via Sud", "residential", [(0, 0), (50, 0), (100, 0), (150, 0), (200, 0), (300, 0), (400, 0)])
    o.way("Via Ovest", "residential", [(0, 0), (0, 50), (0, 100), (0, 150), (0, 200)])
    o.way("Via Mezzo", "primary", [(100, 0), (100, 50), (100, 100), (100, 150), (100, 200), (100, 250),
                                   (100, 350), (100, 400)])                 # esce dall'area a nord
    o.way("Via Est", "residential", [(200, 0), (200, 50)])                  # strada spezzata in due way
    o.way("Via Est", "residential", [(200, 50), (200, 100), (200, 150), (200, 200)])
    o.way("Vicolo Cieco", "residential", [(50, 100), (50, 130), (50, 160)])
    o.way(None, "residential", [(150, 0), (150, -30)])                      # vicolo senza nome
    # corso a due carreggiate a senso unico opposto, a 12 m l'una dall'altra
    o.way("Corso Doppio", "secondary", [(200, 100), (210, 106), (300, 106), (400, 106)], oneway="yes")
    o.way("Corso Doppio", "secondary", [(400, 94), (300, 94), (210, 94), (200, 100)], oneway="yes")
    o.way("Via Uno", "residential", [(300, 0), (300, 50), (300, 94), (300, 106), (300, 150), (300, 200)],
          oneway="yes")                                                     # verso nord
    o.way("Via Due", "residential", [(400, 0), (400, 94), (400, 106), (400, 200)], oneway="-1")  # verso sud
    o.way("Piazza Inutile", "residential", [(250, 250), (260, 250), (260, 260), (250, 250)], area="yes")
    # isola scollegata (componente piu' piccolo)
    o.way("Via Isola A", "residential", [(1000, 0), (1100, 0)])
    o.way("Via Isola B", "residential", [(1000, 100), (1100, 100)])
    o.way("Via Isola C", "residential", [(1000, 0), (1000, 100)])
    o.way("Via Isola D", "residential", [(1100, 0), (1100, 100)])
    return o


BBOX = pt(-50, -60) + pt(1200, 300)          # (s, w, n, e): Via Mezzo oltre y = 300 e' fuori


def rotatoria():
    o = FintoOSM()
    anello = [(30 * math.cos(math.radians(a)), 30 * math.sin(math.radians(a))) for a in range(0, 360, 30)]
    o.way("Piazza Tonda", "residential", anello + [anello[0]], junction="roundabout")
    o.way("Via Alfa", "tertiary", [(0, 30), (0, 90), (0, 150)])
    o.way("Via Beta", "tertiary", [(30, 0), (90, 0), (150, 0)])
    o.way("Via Gamma", "tertiary", [(0, -30), (0, -90), (0, -150)])
    o.way("Via Delta", "tertiary", [(-30, 0), (-90, 0), (-150, 0)])
    o.way("Via Alta", "residential", [(-100, 150), (0, 150), (100, 150)])
    o.way("Via Destra", "residential", [(150, -100), (150, 0), (150, 100)])
    o.way("Via Bassa", "residential", [(-100, -150), (0, -150), (100, -150)])
    o.way("Via Sinistra", "residential", [(-150, -100), (-150, 0), (-150, 100)])
    return o


def costruisci(osm, **kw):
    kw.setdefault("bbox", BBOX)
    return bt.build_topology(osm, **kw)


def vicino_a(topo, x, y, tol=3.0):
    """Id dell'incrocio entro tol metri dal punto (x, y), altrimenti None."""
    for nid, n in topo["nodes"].items():
        px, py = xy(n["lat"], n["lon"])
        if math.hypot(px - x, py - y) <= tol:
            return nid
    return None


def arco(topo, a, b):
    for ed in topo["edges"]:
        if {ed["a"], ed["b"]} == {a, b}:
            return ed
    return None


def dist_polilinea(p, poly):
    return min(bt._dist_seg(p, a, b) for a, b in zip(poly, poly[1:]))


def hard_ok(topo):
    falliti = [(n, i) for n, ok, i, hard in bt.check_topology(topo) if hard and not ok]
    return not falliti, falliti


# ---------------------------------------------------------------------- geometria

@test
def test_geometria():
    d = bt.haversine_m(*pt(0, 0), *pt(0, 100))
    check("haversine: 100 m verso nord", abs(d - 100.0) < 0.2, f"{d:.2f}")
    check("direzione verso nord = 0 gradi", abs(bt.bearing_deg(*pt(0, 0), *pt(0, 100))) < 0.1)
    check("direzione verso est = 90 gradi", abs(bt.bearing_deg(*pt(0, 0), *pt(100, 0)) - 90.0) < 0.1)
    check("direzione verso ovest = 270 gradi", abs(bt.bearing_deg(*pt(0, 0), *pt(-100, 0)) - 270.0) < 0.1)

    dritta = [pt(x, (1.0 if x % 20 else -1.0) if 0 < x < 200 else 0.0) for x in range(0, 201, 10)]
    s = bt.simplify(dritta, 3.0)
    check("semplificazione: rumore di 1 m su una retta -> 2 punti", len(s) == 2, f"{len(s)} punti")
    check("semplificazione: estremi conservati", s[0] == dritta[0] and s[-1] == dritta[-1])
    elle = [pt(0, 0), pt(50, 0), pt(100, 0), pt(100, 50), pt(100, 100)]
    s = bt.simplify(elle, 3.0)
    check("semplificazione: una L resta di 3 punti con lo spigolo", len(s) == 3 and s[1] == pt(100, 0), f"{s}")
    check("semplificazione: tolleranza 0 non cambia nulla", bt.simplify(elle, 0) == elle)
    doppi = [pt(0, 0), pt(0, 0), pt(10, 0), pt(10, 0)]
    check("semplificazione: punti ripetuti tolti", bt.simplify(doppi, 3.0) == [pt(0, 0), pt(10, 0)])
    curva = [pt(*p) for p in ARCO]
    s = bt.simplify(curva, 3.0)
    scarto = max(dist_polilinea(xy(*p), [xy(*q) for q in s]) for p in curva)
    check("semplificazione di una curva: meno punti, scarto <= 3 m",
          2 < len(s) < len(curva) and scarto <= 3.0 + 1e-6, f"{len(s)}/{len(curva)} punti, scarto {scarto:.2f} m")
    check("nomi: 'Piazza C.L.N.' e 'Piazza CLN' sono la stessa strada",
          bt.name_key("Piazza C.L.N.") == bt.name_key("piazza  CLN"))
    check("bbox: lettura e controllo", bt.parse_bbox("45.055, 7.655,45.072,7.685") == (45.055, 7.655, 45.072, 7.685))
    try:
        bt.parse_bbox("45.07,7.6,45.05,7.7")
        check("bbox rovesciata rifiutata", False, "nessun errore")
    except ValueError:
        check("bbox rovesciata rifiutata", True)


# ---------------------------------------------------------------------- dataset sintetico

@test
def test_citta_sintetica():
    righe = []
    topo = costruisci(citta().dati(), log=righe.append)
    nodes = topo["nodes"]
    ok, falliti = hard_ok(topo)
    check("citta' sintetica: tutti i controlli di coerenza", ok, f"{falliti}")
    check("citta' sintetica: 16 incroci", len(nodes) == 16, f"{len(nodes)}: {[n['name'] for n in nodes.values()]}")
    check("citta' sintetica: 23 archi", len(topo["edges"]) == 23, f"{len(topo['edges'])}")
    check("citta' sintetica: riepilogo dei passaggi nel log", any("incroci dopo il raggruppamento" in r for r in righe))

    croce = vicino_a(topo, 100, 100)
    check("croce: incrocio con 4 vicini", croce and len(nodes[croce]["neighbors"]) == 4,
          f"{croce} {nodes.get(croce)}")
    check("croce: nome con la strada principale per tipo prima (primaria, poi secondaria)",
          croce and nodes[croce]["name"] == "Via Mezzo / Via Centro", f"{croce and nodes[croce]['name']}")
    t = vicino_a(topo, 100, 0)
    check("T: incrocio con 3 vicini", t and len(nodes[t]["neighbors"]) == 3, f"{t and nodes[t]}")
    angolo = vicino_a(topo, 0, 0)
    check("angolo tra due strade con nome diverso = incrocio (2 vicini)",
          angolo and len(nodes[angolo]["neighbors"]) == 2 and set(nodes[angolo]["streets"]) == {"Via Sud", "Via Ovest"},
          f"{angolo and nodes[angolo]}")
    vc = vicino_a(topo, 50, 100)
    check("vicolo cieco con nome: l'innesto resta un incrocio, il vicolo non diventa un arco",
          vc and "Vicolo Cieco" in nodes[vc]["name"] and len(nodes[vc]["neighbors"]) == 2
          and vicino_a(topo, 50, 160, tol=20) is None, f"{vc and nodes[vc]}")
    check("vicolo senza nome sulla stessa strada: non e' un incrocio (archi uniti)",
          vicino_a(topo, 150, 0, tol=10) is None and arco(topo, t, vicino_a(topo, 200, 0)) is not None)
    ed = arco(topo, t, vicino_a(topo, 200, 0))
    check("arco unito attraverso il vicolo senza nome: 100 m", ed and abs(ed["m"] - 100.0) < 0.5, f"{ed}")
    est_s, est_c = vicino_a(topo, 200, 0), vicino_a(topo, 200, 100)
    ed = arco(topo, est_s, est_c)
    check("strada spezzata in due way: arco unico attraverso il punto di giunzione",
          ed is not None and vicino_a(topo, 200, 50, tol=10) is None and abs(ed["m"] - 100.0) < 0.5, f"{ed}")

    # strada curva con punti intermedi
    a, b = vicino_a(topo, 0, 200), vicino_a(topo, 100, 200)
    ed = arco(topo, a, b)
    lung = bt.polyline_m([pt(*p) for p in ARCO])
    check("strada curva: lunghezza lungo la strada (non in linea d'aria)",
          ed and abs(ed["m"] - lung) < 0.5 and ed["m"] > 110, f"{ed and ed['m']} contro {lung:.1f}")
    if ed:
        geom = [xy(*p) for p in ed["geom"]]
        scarto = max(dist_polilinea(xy(*pt(*p)), geom) for p in ARCO)
        check("strada curva: geometria semplificata (meno punti, scarto <= 3 m)",
              2 < len(geom) < len(ARCO) and scarto <= 3.0 + 1e-6, f"{len(geom)} punti, scarto {scarto:.2f}")
        check("geometria dell'arco da centro a centro degli incroci",
              ed["geom"][0] == [nodes[ed["a"]]["lat"], nodes[ed["a"]]["lon"]]
              and ed["geom"][-1] == [nodes[ed["b"]]["lat"], nodes[ed["b"]]["lon"]])
    check("strada: nome della strada sull'arco", ed and ed["street"] == "Via Nord", f"{ed and ed['street']}")

    # doppia carreggiata
    c300 = vicino_a(topo, 300, 100)
    check("doppia carreggiata: i due punti a 12 m diventano un incrocio solo, al centro",
          c300 is not None and vicino_a(topo, 300, 94, tol=2) is None, f"{c300}")
    check("doppia carreggiata: nessun incrocio doppio entro 15 m",
          c300 and sum(1 for n in nodes.values() if math.dist(xy(n["lat"], n["lon"]), (300, 100)) < 15) == 1)
    check("doppia carreggiata: nome 'Corso Doppio / Via Uno'", c300 and nodes[c300]["name"] == "Corso Doppio / Via Uno",
          f"{c300 and nodes[c300]['name']}")
    ed = arco(topo, est_c, c300)
    check("doppia carreggiata: un arco solo tra due incroci, il piu' corto (~100 m)",
          ed and abs(ed["m"] - 100.0) < 2.0 and sum(1 for e in topo["edges"] if {e["a"], e["b"]} == {est_c, c300}) == 1,
          f"{ed}")
    check("doppia carreggiata a sensi opposti: arco a doppio senso", ed and ed["oneway"] is False, f"{ed}")
    check("archi a doppio senso: 'a' e' l'incrocio con il numero piu' basso",
          all(bt._num(e["a"]) < bt._num(e["b"]) for e in topo["edges"] if not e["oneway"]))

    # sensi unici
    s300 = vicino_a(topo, 300, 0)
    ed = arco(topo, s300, c300)
    check("senso unico (oneway=yes): registrato, 'a' -> 'b' nel verso di marcia",
          ed and ed["oneway"] is True and ed["a"] == s300 and ed["b"] == c300, f"{ed}")
    n400, c400 = vicino_a(topo, 400, 200), vicino_a(topo, 400, 100)
    ed = arco(topo, n400, c400)
    check("senso unico al contrario (oneway=-1): 'a' e' l'incrocio a nord",
          ed and ed["oneway"] is True and ed["a"] == n400, f"{ed}")
    check("il grafo resta non orientato: vicini in entrambi i sensi anche sui sensi unici",
          ed and c400 in nodes[n400]["neighbors"] and n400 in nodes[c400]["neighbors"])

    # isola, area, bordi
    check("isola scollegata scartata (si tiene il componente piu' grande)",
          not any(xy(n["lat"], n["lon"])[0] > 900 for n in nodes.values()))
    check("aree (area=yes) ignorate", not any("Piazza Inutile" in n["streets"] for n in nodes.values()))
    check("niente incroci fuori dall'area", all(xy(n["lat"], n["lon"])[1] <= 300 for n in nodes.values()))
    nord = vicino_a(topo, 100, 200)
    check("strada che esce dall'area: il tratto fuori non diventa un arco",
          nord and len(nodes[nord]["neighbors"]) == 3, f"{nord and nodes[nord]['neighbors']}")

    # vicini
    v = nodes[croce]["neighbors"] if croce else {}
    su, dx = vicino_a(topo, 100, 200), vicino_a(topo, 200, 100)
    check("vicini: direzione verso nord ~0 gradi, verso est ~90 gradi",
          su in v and dx in v and min(v[su]["bearing"], 360 - v[su]["bearing"]) < 1 and abs(v[dx]["bearing"] - 90) < 1,
          f"{v}")
    check("vicini: campi bearing, street, m", all(set(i) == {"bearing", "street", "m"}
                                                   for n in nodes.values() for i in n["neighbors"].values()))
    simm = all(k in nodes[o]["neighbors"] and nodes[o]["neighbors"][k]["m"] == i["m"]
               and abs(((i["bearing"] - nodes[o]["neighbors"][k]["bearing"]) % 360) - 180) < 1
               for k, n in nodes.items() for o, i in n["neighbors"].items())
    check("vicini simmetrici con stessa distanza e direzioni opposte", simm)


@test
def test_id_e_nomi():
    topo = costruisci(citta().dati())
    nodes = topo["nodes"]
    check("id 'incrocio-<n>' da 1 a N", list(nodes) == [f"incrocio-{i}" for i in range(1, len(nodes) + 1)], f"{list(nodes)}")
    chiave = [xy(n["lat"], n["lon"])[0] - xy(n["lat"], n["lon"])[1] for n in nodes.values()]
    # pari merito (stessa diagonale) entro l'arrotondamento delle coordinate: tolleranza 0.5 m
    check("id ordinati da nord-ovest a sud-est", all(a <= b + 0.5 for a, b in zip(chiave, chiave[1:])),
          f"{[round(c, 1) for c in chiave]}")
    check("incrocio-1 e' l'angolo a nord-ovest", vicino_a(topo, 0, 200) == "incrocio-1")
    check(f"l'ultimo e' l'angolo a sud-est (incrocio-{len(nodes)})", vicino_a(topo, 400, 0) == f"incrocio-{len(nodes)}")
    check("nomi 'Strada A / Strada B' non vuoti",
          all(n["name"] and n["name"] == " / ".join(n["streets"][:2]) for n in nodes.values()))
    check("campo osm = id di un punto OSM dell'incrocio", all(isinstance(n["osm"], int) and n["osm"] >= 1000
                                                             for n in nodes.values()))
    again = costruisci(citta().dati())
    check("risultato stabile: stessa topologia a ogni esecuzione",
          again["nodes"] == topo["nodes"] and again["edges"] == topo["edges"])
    solo_nodi = costruisci(citta().dati(solo_nodi=True))
    check("dati 'out body' con elementi node (senza geometry): stesso risultato",
          solo_nodi["nodes"] == topo["nodes"] and solo_nodi["edges"] == topo["edges"])
    check("attribuzione OpenStreetMap contributors, ODbL nel campo source",
          "OpenStreetMap contributors" in topo["source"] and "ODbL" in topo["source"])
    check("formato v2: version, bbox, generated", topo["version"] == 2 and len(topo["bbox"]) == 4 and topo["generated"])
    testo = bt.dumps_topology(topo)
    check("scrittura: JSON valido, un incrocio per riga", json.loads(testo)["nodes"] == topo["nodes"]
          and testo.count('"incrocio-1": {') == 1)


@test
def test_parametri():
    o = citta()
    sep = costruisci(o.dati(), cluster_m=5.0, max_span_m=5.0, min_sep_m=5.0)
    check("soglia di raggruppamento configurabile: con 5 m le due carreggiate restano 2 incroci",
          vicino_a(sep, 300, 94, tol=1) is not None and vicino_a(sep, 300, 106, tol=1) is not None)
    gruppo = costruisci(o.dati())
    check("con la soglia di default (25 m) un incrocio solo", vicino_a(gruppo, 300, 94, tol=1) is None)

    r = costruisci(rotatoria().dati(), bbox=pt(-300, -300) + pt(300, 300))
    centro = vicino_a(r, 0, 0, tol=2)
    check("rotatoria (diametro 60 m): un incrocio solo al centro con 4 vicini",
          len(r["nodes"]) == 5 and centro and len(r["nodes"][centro]["neighbors"]) == 4,
          f"{len(r['nodes'])} incroci, {r['nodes'].get(centro)}")
    check("rotatoria: nome con le strade principali per tipo",
          centro and r["nodes"][centro]["streets"][0].startswith("Via ") and "Piazza Tonda" in r["nodes"][centro]["streets"])
    ed = arco(r, centro, vicino_a(r, 0, 150))
    check("rotatoria: braccio da centro a centro (150 m)", ed and abs(ed["m"] - 150.0) < 1.0, f"{ed}")
    piccola = costruisci(rotatoria().dati(), bbox=pt(-300, -300) + pt(300, 300), roundabout_m=10)
    check("rotatoria con --roundabout-m piu' piccolo del diametro: 4 incroci sull'anello",
          vicino_a(piccola, 0, 0, tol=5) is None and vicino_a(piccola, 0, 30, tol=1) is not None)
    ok, falliti = hard_ok(piccola)
    check("rotatoria non raggruppata: controlli di coerenza ok", ok, f"{falliti}")


@test
def test_scorciatoie():
    # corso con carreggiata centrale dritta (A -> C) e controviale che tocca l'incrocio B in mezzo
    o = FintoOSM()
    o.way("Corso Lungo", "primary", [(0, 0), (200, 0)])                         # centrale, nessun incrocio in mezzo
    o.way("Corso Lungo", "residential", [(0, 0), (10, 20), (100, 20), (190, 20), (200, 0)])   # controviale
    o.way("Via Traversa", "residential", [(100, 20), (100, 120)])
    o.way("Via Alta", "residential", [(0, 120), (100, 120), (200, 120)])        # chiusura a nord: tutto connesso
    o.way("Via Sinistra", "residential", [(0, 0), (0, 120)])
    o.way("Via Destra", "residential", [(200, 0), (200, 120)])
    topo = costruisci(o.dati(), bbox=pt(-50, -50) + pt(250, 200))
    a, b, c = vicino_a(topo, 0, 0, 12), vicino_a(topo, 100, 20, 2), vicino_a(topo, 200, 0, 12)
    check("scorciatoia della carreggiata centrale tolta: A-C passa per B",
          a and b and c and arco(topo, a, c) is None and arco(topo, a, b) and arco(topo, b, c),
          f"{a} {b} {c} {[(e['a'], e['b'], e['m']) for e in topo['edges']]}")
    tieni = costruisci(o.dati(), bbox=pt(-50, -50) + pt(250, 200), bypass_ratio=0)
    a2, c2 = vicino_a(tieni, 0, 0, 12), vicino_a(tieni, 200, 0, 12)
    check("con --bypass-ratio 0 la scorciatoia resta", arco(tieni, a2, c2) is not None)
    ok, falliti = hard_ok(topo)
    check("dopo aver tolto le scorciatoie il grafo resta coerente e connesso", ok, f"{falliti}")


# ---------------------------------------------------------------------- controlli: devono anche fallire

def fallito(topo, parola):
    """True se fallisce un controllo bloccante il cui nome contiene parola."""
    return any(parola in n and hard and not ok for n, ok, _i, hard in bt.check_topology(topo))


@test
def test_controlli_negativi():
    """Ogni controllo sul file deve accorgersi del difetto che cerca (un controllo che non fallisce mai
    non protegge nessuno) e un file malformato deve dare errori, mai un'eccezione."""
    base = costruisci(citta().dati())
    check("controlli: la citta' sintetica li passa tutti", hard_ok(base)[0], f"{hard_ok(base)[1]}")
    ids = list(base["nodes"])
    a = ids[0]
    b = next(iter(base["nodes"][a]["neighbors"]))

    t = copy.deepcopy(base)
    t["source"] = "dati miei"
    check("controlli: attribuzione OpenStreetMap/ODbL mancante -> errore", fallito(t, "attribuzione"))
    t = copy.deepcopy(base)
    t["nodes"][a]["name"] = "  "
    check("controlli: nome vuoto -> errore", fallito(t, "nomi e strade"))
    t = copy.deepcopy(base)
    t["nodes"][a]["streets"] = []
    check("controlli: elenco delle strade vuoto -> errore", fallito(t, "nomi e strade"))
    t = copy.deepcopy(base)
    t["nodes"]["incrocio-99"] = t["nodes"].pop(ids[-1])
    check("controlli: id con buchi -> errore", fallito(t, "id incrocio-1..N"))

    t = copy.deepcopy(base)
    t["nodes"][ids[0]]["lat"] = round(t["nodes"][ids[1]]["lat"] + 5 / bt.M_PER_DEG, 6)
    t["nodes"][ids[0]]["lon"] = t["nodes"][ids[1]]["lon"]
    check("controlli: due incroci a 5 m -> errore 'incrocio doppio'", fallito(t, "incrocio doppio"))

    # una coppia di incroci lontani collegati solo tra loro, per il resto coerente: solo la connessione fallisce
    t = copy.deepcopy(base)
    n = len(t["nodes"])
    k1, k2 = f"incrocio-{n + 1}", f"incrocio-{n + 2}"
    p, q = pt(700, 0), pt(800, 0)
    m = round(bt.haversine_m(*p, *q), 1)
    for k, qui, la, altro in ((k1, p, q, k2), (k2, q, p, k1)):
        t["nodes"][k] = {"name": "Via Lontana / Via Sola", "lat": round(qui[0], 6), "lon": round(qui[1], 6), "osm": 1,
                         "streets": ["Via Lontana", "Via Sola"],
                         "neighbors": {altro: {"bearing": round(bt.bearing_deg(*qui, *la), 1), "street": "Via Lontana",
                                               "m": m}}}
    t["edges"].append({"a": k1, "b": k2, "street": "Via Lontana", "m": m, "oneway": False,
                       "geom": [[round(p[0], 6), round(p[1], 6)], [round(q[0], 6), round(q[1], 6)]]})
    check("controlli: componente scollegato -> errore 'grafo connesso' (archi e vicini coerenti)",
          fallito(t, "grafo connesso") and not fallito(t, "archi validi") and not fallito(t, "vicini simmetrici"),
          f"{[x for x in bt.check_topology(t) if not x[1]]}")

    # direzione sbagliata ma simmetrica: la vede solo il controllo direzione <-> posizione
    t = copy.deepcopy(base)
    for u, v in ((a, b), (b, a)):
        t["nodes"][u]["neighbors"][v]["bearing"] = round((t["nodes"][u]["neighbors"][v]["bearing"] + 90) % 360, 1)
    check("controlli: direzione ruotata di 90 gradi nei due sensi -> errore 'direzioni verso il vicino'",
          fallito(t, "direzioni dei vicini verso") and not fallito(t, "vicini simmetrici"))
    t = copy.deepcopy(base)
    t["nodes"][a]["neighbors"][b]["bearing"] = round((t["nodes"][a]["neighbors"][b]["bearing"] + 20) % 360, 1)
    check("controlli: direzioni non opposte nei due sensi -> errore", fallito(t, "vicini simmetrici"))

    t = copy.deepcopy(base)
    ed = t["edges"][0]
    for x in (ed, t["nodes"][ed["a"]]["neighbors"][ed["b"]], t["nodes"][ed["b"]]["neighbors"][ed["a"]]):
        x["m"] = 1.0
    check("controlli: arco piu' corto della linea d'aria -> errore", fallito(t, "archi validi"))
    t = copy.deepcopy(base)
    t["edges"][0]["geom"][0] = [t["edges"][0]["geom"][0][0] + 0.001, t["edges"][0]["geom"][0][1]]
    check("controlli: geometria che non parte dall'incrocio -> errore", fallito(t, "archi validi"))
    t = copy.deepcopy(base)
    t["edges"].append(copy.deepcopy(t["edges"][0]))
    check("controlli: arco doppio -> errore", fallito(t, "archi validi"))
    t = copy.deepcopy(base)
    t["edges"][0]["oneway"] = "si"
    check("controlli: oneway non booleano -> errore", fallito(t, "archi validi"))
    t = copy.deepcopy(base)
    del t["edges"][0]
    check("controlli: vicino senza arco -> errore", fallito(t, "vicini = archi"))
    t = copy.deepcopy(base)
    t["nodes"][a]["lat"] = t["bbox"][2] + 0.01
    check("controlli: incrocio fuori dalla bbox -> errore", fallito(t, "dentro la bbox"))

    # file malformati: errori segnalati, nessuna eccezione da check_topology/summary/nearest/describe
    rotto = copy.deepcopy(base)
    rotto["nodes"][ids[0]].pop("lat")
    rotto["nodes"][ids[1]]["neighbors"] = ["x"]
    rotto["nodes"][ids[2]]["lat"] = "nord"
    rotto["nodes"][ids[3]] = "non un nodo"
    rotto["nodes"][ids[4]]["neighbors"][next(iter(rotto["nodes"][ids[4]]["neighbors"]))] = {"bearing": "x", "m": None}
    rotto["edges"][0]["m"] = "abc"
    rotto["edges"][1]["geom"] = [["a", "b"], None]
    rotto["edges"][2]["a"] = ["lista"]
    rotto["edges"].append("non un arco")
    rotto["bbox"] = ["s", "w", "n", "e"]
    casi = {"non un oggetto": [1, 2], "nodes come lista": {"version": 2, "nodes": [], "edges": {}},
            "campi sbagliati ovunque": rotto}
    for nome, topo in casi.items():
        try:
            ris = bt.check_topology(topo)
            bt.summary(topo)
            bt.nearest(topo, 45.0, 7.0)
            nodi = topo.get("nodes") if isinstance(topo, dict) else None
            for k in list(nodi)[:6] if isinstance(nodi, dict) else []:
                bt.describe(topo, k)
            check(f"controlli su un file malformato ({nome}): errori segnalati, nessuna eccezione",
                  any(hard and not ok for _n, ok, _i, hard in ris)
                  and not any(n == "controlli completati" for n, *_r in ris), f"{ris}")
        except Exception as e:
            check(f"controlli su un file malformato ({nome}): errori segnalati, nessuna eccezione", False,
                  f"{e!r}\n{traceback.format_exc(limit=3)}")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "rotto.json"
        f.write_text(json.dumps(rotto), encoding="utf-8")
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = bt.main(["--verify", str(f), "--mark", "45,7"])
        except Exception as e:
            rc = repr(e)
        check("riga di comando: --verify su un file malformato -> codice 1, nessun traceback", rc == 1, f"rc={rc}")
    check("nearest: punto non valido -> (None, None)", bt.nearest(base, float("nan"), 7.0) == (None, None))


# ---------------------------------------------------------------------- dati OSM sporchi

@test
def test_dati_sporchi():
    o = citta()
    base = costruisci(o.dati())
    d = o.dati()
    for w in d["elements"]:
        w["nodes"] = [str(n) for n in w["nodes"]]
    t = costruisci(d)
    check("dati sporchi: id OSM scritti come stringhe -> stessa topologia",
          t["nodes"] == base["nodes"] and t["edges"] == base["edges"])

    d = o.dati()
    d["elements"] += [None, "x", {"type": "way"}, {"type": "way", "id": 9001, "tags": ["highway"], "nodes": [1000, 1001]},
                      {"type": "way", "id": 9002, "tags": {"highway": ["residential"]}, "nodes": [1000, 1001]},
                      {"type": "way", "id": 9003, "tags": {"highway": "residential"}, "nodes": None, "geometry": 5},
                      {"type": "way", "id": 9004, "tags": {"highway": "residential"}, "nodes": [[1], {"a": 1}]},
                      {"type": "node", "id": "abc", "lat": "x", "lon": None}]
    t = costruisci(d)
    check("dati sporchi: elementi malformati ignorati -> stessa topologia",
          t["nodes"] == base["nodes"] and t["edges"] == base["edges"])

    d = o.dati()
    w = next(w for w in d["elements"] if w["tags"].get("name") == "Via Sud")
    w["geometry"][5] = {"lat": "nan", "lon": 7.0}                         # punto (300, 0) senza coordinate valide
    t = costruisci(d)
    check("dati sporchi: coordinata non valida spezza la way, topologia ancora coerente e senza punti a caso",
          hard_ok(t)[0] and all(abs(n["lat"] - LAT0) < 0.01 and abs(n["lon"] - LON0) < 0.02 for n in t["nodes"].values()),
          f"{hard_ok(t)[1]}")

    for nome, dati in (("way con un punto ripetuto", {"elements": [{"type": "way", "id": 1, "nodes": [5, 5],
                                                                    "tags": {"highway": "residential"},
                                                                    "geometry": [{"lat": 45, "lon": 7}] * 2}]}),
                       ("nessuna strada", {"elements": []}),
                       ("non un oggetto", [1, 2]),
                       ("tipo di strada non testuale", {"elements": [{"type": "way", "id": 1, "nodes": [1, 2],
                                                                      "tags": {"highway": {"x": 1}}}]})):
        try:
            bt.build_topology(dati)
            check(f"dati sporchi: {nome} -> ValueError con messaggio", False, "nessun errore")
        except ValueError:
            check(f"dati sporchi: {nome} -> ValueError con messaggio", True)
        except Exception as e:
            check(f"dati sporchi: {nome} -> ValueError con messaggio", False, repr(e))


@test
def test_id_stabili_tra_esecuzioni():
    """Gli id 'incrocio-<n>' finiscono in config.json delle board: due processi diversi (ordine dei set di
    stringhe diverso, PYTHONHASHSEED) devono dare gli stessi id e gli stessi archi."""
    with tempfile.TemporaryDirectory() as tmp:
        lavori = []
        src = Path(tmp) / "zona.json"
        src.write_text(json.dumps(citta().dati()), encoding="utf-8")
        lavori.append(("citta' sintetica", ["--input", str(src), "--bbox", ",".join(f"{v:.7f}" for v in BBOX)]))
        if bt.DEFAULT_INPUT.exists():
            lavori.append(("dati OSM di Torino", []))
        for nome, argv in lavori:
            uscite = []
            for seme in ("1", "4242"):
                dst = Path(tmp) / f"t_{len(uscite)}.json"
                r = subprocess.run([sys.executable, str(QUI.parent / "build_topology.py"), *argv, "--out", str(dst),
                                    "--quiet"], env=dict(os.environ, PYTHONHASHSEED=seme), capture_output=True,
                                   timeout=300)
                uscite.append(json.loads(dst.read_text(encoding="utf-8")) if r.returncode == 0 and dst.exists() else None)
            ok = all(uscite) and uscite[0]["nodes"] == uscite[1]["nodes"] and uscite[0]["edges"] == uscite[1]["edges"]
            check(f"id e archi identici tra processi con PYTHONHASHSEED diversi ({nome})", ok)
            if nome.startswith("dati OSM") and ok and TOPOLOGY.exists():
                salvato = json.loads(TOPOLOGY.read_text(encoding="utf-8"))
                check("topology.json uguale a quello rigenerato in un altro processo",
                      salvato["nodes"] == uscite[0]["nodes"] and salvato["edges"] == uscite[0]["edges"])


# ---------------------------------------------------------------------- download e riga di comando

def apri_finto(risposte):
    chiamate = []

    def apri(req, timeout=None):
        chiamate.append(req)
        r = risposte.pop(0)
        if isinstance(r, Exception):
            raise r
        return io.BytesIO(r)
    return apri, chiamate


@test
def test_download_finto():
    q = bt.overpass_query((45.055, 7.655, 45.072, 7.685))
    check("query Overpass: way highway nell'area con nodi e geometria",
          "(45.055,7.655,45.072,7.685)" in q and "out body geom" in q and "residential" in q and "primary" in q, q)
    attese = []
    buono = json.dumps(citta().dati()).encode("utf-8")
    apri, chiamate = apri_finto([urllib.error.URLError("server giu'"), b"<html>troppo traffico</html>", buono])
    osm = bt.download_osm((45.0, 7.0, 45.1, 7.1), servers=("http://a", "http://b", "http://c"), opener=apri,
                          sleep=attese.append, log=lambda *_x: None)
    check("download: prova i server di riserva finche' uno risponde", len(chiamate) == 3
          and [c.full_url for c in chiamate] == ["http://a", "http://b", "http://c"])
    check("download: attesa crescente tra i tentativi", attese == [5.0, 10.0], f"{attese}")
    corpo = urllib.parse.parse_qs(chiamate[0].data.decode("utf-8"))["data"][0]
    check("download: invia la query dell'area", "(45.0,7.0,45.1,7.1)" in corpo, corpo)
    check("download: dati con la bbox usata", osm.get("bbox") == [45.0, 7.0, 45.1, 7.1] and osm["elements"])
    apri, chiamate = apri_finto([urllib.error.URLError("no")] * 4)
    try:
        bt.download_osm((45.0, 7.0, 45.1, 7.1), servers=("http://a", "http://b"), rounds=2, opener=apri,
                        sleep=lambda _s: None, log=lambda *_x: None)
        check("download: errore chiaro se tutti i server falliscono", False, "nessun errore")
    except RuntimeError as e:
        check("download: errore chiaro se tutti i server falliscono", "http://a" in str(e) and len(chiamate) == 4)

    # Overpass risponde 200 con dati PARZIALI quando la query va in timeout: non vanno usati
    parziale = citta().dati()
    parziale["elements"] = parziale["elements"][:3]
    parziale["remark"] = 'runtime error: Query timed out in "query" at line 2 after 121 seconds.'
    apri, chiamate = apri_finto([json.dumps(parziale).encode("utf-8"), buono])
    osm = bt.download_osm((45.0, 7.0, 45.1, 7.1), servers=("http://a", "http://b"), opener=apri,
                          sleep=lambda _s: None, log=lambda *_x: None)
    check("download: risposta parziale ('remark: runtime error') scartata, si passa al server dopo",
          len(chiamate) == 2 and len(osm["elements"]) == len(citta().dati()["elements"]), f"{len(chiamate)} chiamate")
    apri, chiamate = apri_finto([json.dumps(parziale).encode("utf-8")] * 2)
    try:
        bt.download_osm((45.0, 7.0, 45.1, 7.1), servers=("http://a",), rounds=2, opener=apri,
                        sleep=lambda _s: None, log=lambda *_x: None)
        check("download: solo risposte parziali -> errore (mai una topologia monca)", False, "nessun errore")
    except RuntimeError as e:
        check("download: solo risposte parziali -> errore (mai una topologia monca)", "incompleta" in str(e), str(e))


@test
def test_riga_di_comando_senza_rete():
    vero = urllib.request.urlopen

    def vietato(*_a, **_k):
        raise AssertionError("rete usata senza --download")
    urllib.request.urlopen = vietato
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "zona.json", Path(tmp) / "out" / "topology.json"
            src.write_text(json.dumps(citta().dati()), encoding="utf-8")
            bbox = ",".join(f"{v:.7f}" for v in BBOX)
            uscita = io.StringIO()
            with contextlib.redirect_stdout(uscita):
                rc = bt.main(["--input", str(src), "--bbox", bbox, "--out", str(dst), "--quiet",
                              "--mark", f"{pt(100, 100)[0]},{pt(100, 100)[1]}"])
            testo = uscita.getvalue()
            check("riga di comando: costruisce e scrive senza rete", rc == 0 and dst.exists(), f"rc={rc}\n{testo}")
            check("riga di comando: riepilogo (incroci, archi, lunghezza, grado medio)",
                  "incroci: 16" in testo and "archi: 23" in testo and "km" in testo and "grado medio" in testo, testo)
            check("riga di comando: --mark stampa l'incrocio piu' vicino", "Via Mezzo / Via Centro" in testo, testo)
            scritto = json.loads(dst.read_text(encoding="utf-8"))
            check("riga di comando: file scritto valido", len(scritto["nodes"]) == 16 and hard_ok(scritto)[0])
            with contextlib.redirect_stdout(io.StringIO()):
                rc = bt.main(["--verify", str(dst)])
            check("riga di comando: --verify su un file buono -> 0", rc == 0)
            rotto = copy.deepcopy(scritto)
            primo = next(iter(rotto["nodes"]))
            altro = next(iter(rotto["nodes"][primo]["neighbors"]))
            del rotto["nodes"][altro]["neighbors"][primo]                      # vicino non simmetrico
            (Path(tmp) / "rotto.json").write_text(json.dumps(rotto), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = bt.main(["--verify", str(Path(tmp) / "rotto.json")])
            check("riga di comando: --verify trova i vicini non simmetrici -> 1", rc == 1)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = bt.main(["--download"])
            check("riga di comando: --download senza --bbox rifiutato (nessun accesso alla rete)", rc == 2)

            # file con BOM (salvato da un editor di Windows): si legge lo stesso
            bom = Path(tmp) / "bom.json"
            bom.write_text("﻿" + json.dumps(citta().dati()), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                rc = bt.main(["--input", str(bom), "--bbox", bbox, "--out", "-", "--quiet"])
            check("riga di comando: dati OSM con BOM letti", rc == 0, f"rc={rc}")

            # parametri e punti assurdi rifiutati da argparse (un nan spegnerebbe i controlli in silenzio)
            for argv, nome in ((["--mark", "nan,nan"], "--mark nan,nan"), (["--mark", "95,7"], "--mark fuori dal globo"),
                               (["--min-sep-m", "nan"], "--min-sep-m nan"), (["--cluster-m", "-5"], "--cluster-m -5")):
                try:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        rc = bt.main(["--input", str(src), "--bbox", bbox, "--out", "-", "--quiet"] + argv)
                except SystemExit as e:
                    rc = e.code
                check(f"riga di comando: {nome} rifiutato (codice 2)", rc == 2, f"rc={rc}")

            # destinazione non scrivibile (e' una cartella): codice 2, nessun traceback, nessun .tmp lasciato
            cartella = Path(tmp) / "cartella.json"
            cartella.mkdir()
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = bt.main(["--input", str(src), "--bbox", bbox, "--out", str(cartella), "--quiet"])
            check("riga di comando: errore di scrittura -> codice 2 con messaggio",
                  rc == 2 and "non riesco a scrivere" in err.getvalue(), f"rc={rc} {err.getvalue()!r}")
            check("riga di comando: nessun file temporaneo lasciato dopo un errore di scrittura",
                  not (Path(tmp) / "cartella.json.tmp").exists())

            # file di input senza nessuna strada valida: codice 2 con messaggio chiaro
            vuoto = Path(tmp) / "vuoto.json"
            vuoto.write_text(json.dumps({"elements": [{"type": "way", "id": 1, "nodes": [5, 5], "tags": {
                "highway": "residential"}, "geometry": [{"lat": 45, "lon": 7}, {"lat": 45, "lon": 7}]}]}), encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = bt.main(["--input", str(vuoto), "--out", "-", "--quiet"])
            check("riga di comando: dati senza strade valide -> codice 2", rc == 2 and "errore" in err.getvalue(),
                  f"rc={rc} {err.getvalue()!r}")

        # --download dalla riga di comando con un server finto: salva i dati grezzi e scrive la topologia
        risposte = [b'{"elements": [], "remark": "runtime error: out of memory"}', json.dumps(citta().dati()).encode()]
        chiamate = []

        def finto_urlopen(req, timeout=None):
            chiamate.append(req.full_url)
            return io.BytesIO(risposte.pop(0))
        urllib.request.urlopen = finto_urlopen
        vero_sleep = bt.time.sleep
        bt.time.sleep = lambda _s: None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                grezzi, dst = Path(tmp) / "osm" / "zona.json", Path(tmp) / "topology.json"
                bbox = ",".join(f"{v:.7f}" for v in BBOX)
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = bt.main(["--download", "--bbox", bbox, "--save", str(grezzi), "--out", str(dst), "--quiet"])
                salvato = json.loads(grezzi.read_text(encoding="utf-8")) if grezzi.exists() else {}
                check("riga di comando: --download (server finti) salva i dati e scrive la topologia",
                      rc == 0 and dst.exists() and len(chiamate) == 2 and len(salvato.get("bbox", [])) == 4,
                      f"rc={rc} chiamate={chiamate}")
                if dst.exists():
                    check("riga di comando: topologia da --download uguale a quella da --input",
                          json.loads(dst.read_text(encoding="utf-8"))["nodes"] == costruisci(citta().dati())["nodes"])
        finally:
            bt.time.sleep = vero_sleep
    finally:
        urllib.request.urlopen = vero


# ---------------------------------------------------------------------- file generato

@test
def test_file_generato():
    check("topology.json presente in src/node/python", TOPOLOGY.exists(), str(TOPOLOGY))
    if not TOPOLOGY.exists():
        return
    topo = json.loads(TOPOLOGY.read_text(encoding="utf-8"))
    nodes, edges = topo["nodes"], topo["edges"]
    for nome, ok, info, hard in bt.check_topology(topo):
        check(f"topology.json: {nome}", ok, info)
    check("topology.json: numero di incroci plausibile per la zona (qualche centinaio)", 150 <= len(nodes) <= 1000,
          f"{len(nodes)}")
    check("topology.json: attribuzione 'OpenStreetMap contributors, ODbL'",
          "OpenStreetMap contributors" in topo["source"] and "ODbL" in topo["source"], topo["source"])
    check("topology.json: area Crocetta / San Secondo / Centro", topo["bbox"] == list(bt.DEFAULT_BBOX), f"{topo['bbox']}")
    check("topology.json: campi dei nodi come la specifica",
          all({"name", "lat", "lon", "osm", "streets", "neighbors"} <= set(n) for n in nodes.values()))
    check("topology.json: campi degli archi come la specifica",
          all({"a", "b", "street", "m", "geom", "oneway"} <= set(e) for e in edges))
    s = bt.summary(topo)
    check("topology.json: grado medio plausibile (2.5-4.5)", 2.5 <= s["avg_degree"] <= 4.5, f"{s}")
    check("topology.json: arco medio plausibile (40-250 m)", 40 <= s["avg_edge_m"] <= 250, f"{s}")
    check("topology.json: nessun incrocio con piu' di 8 vicini", s["max_degree"] <= 8, f"{s}")
    nomi = {n["name"] for n in nodes.values()}
    for atteso in ("Corso Vittorio Emanuele II / Corso Re Umberto", "Corso Galileo Ferraris / Via Magenta"):
        check(f"topology.json: c'e' l'incrocio '{atteso}'", atteso in nomi)
    lat, lon = bt.VIA_VELA
    nid, d = bt.nearest(topo, lat, lon)
    check("topology.json: incrocio vicino alla board del prototipo (Via Vincenzo Vela) entro 50 m",
          nid and d <= 50 and "Via Vincenzo Vela" in nodes[nid]["streets"], f"{nid} a {d}")
    print(f"      board del prototipo ({lat}, {lon}) -> {bt.describe(topo, nid, d)}")
    csu = min((bt.haversine_m(lat, lon, n["lat"], n["lon"]), k) for k, n in nodes.items()
              if "Corso Stati Uniti" in n["streets"])
    print(f"      incrocio su Corso Stati Uniti piu' vicino: {csu[1]} {nodes[csu[1]]['name']} a {csu[0]:.0f} m")

    # integrazione con chi usa il file sul nodo: route.Topology (solo lettura del modulo di un'altra parte)
    try:
        import route
    except Exception as e:  # route.py e' di un'altra parte del progetto: se manca si salta, non si fallisce
        route = None
        print(f"      (route.py non importabile, prova di integrazione saltata: {e!r})")
    if route is not None:
        with contextlib.redirect_stdout(io.StringIO()):
            rt = route.Topology.load(TOPOLOGY)
        check("route.py: carica tutti gli incroci di topology.json", sorted(rt.ids()) == sorted(nodes),
              f"{len(rt.ids())} contro {len(nodes)}")
        check("route.py: stessi vicini per ogni incrocio",
              all(set(rt.neighbors(k)) == set(n["neighbors"]) for k, n in nodes.items()))
        check("route.py: stesso incrocio piu' vicino alla board del prototipo", rt.nearest(lat, lon) == nid,
              f"{rt.nearest(lat, lon)} contro {nid}")
        storti = [(k, v, rt.targets_for(k, heading_deg=i["bearing"])[:1]) for k, n in nodes.items()
                  for v, i in n["neighbors"].items() if rt.targets_for(k, heading_deg=i["bearing"])[:1] != [v]]
        check("route.py: un pericolo diretto verso un vicino avvisa per primo quel vicino (tutti gli incroci)",
              not storti, f"{len(storti)} casi, es. {storti[:3]}")
        ultimo = f"incrocio-{len(nodes)}"
        p = rt.path("incrocio-1", ultimo)
        check("route.py: percorso da nord-ovest a sud-est attraverso la citta'",
              len(p) >= 3 and p[0] == "incrocio-1" and p[-1] == ultimo
              and all(y in nodes[x]["neighbors"] for x, y in zip(p, p[1:])), f"{p}")

    if bt.DEFAULT_INPUT.exists():
        p = topo.get("params") or {}
        nuovo = bt.build_topology(bt.load_osm(bt.DEFAULT_INPUT), bbox=tuple(topo["bbox"]),
                                  **{k: p[k] for k in bt.DEFAULTS if k in p})
        check("topology.json allineato ai dati OSM e al generatore (rigenerato uguale)",
              nuovo["nodes"] == nodes and nuovo["edges"] == edges,
              "rigenerare con: python build_topology.py")
    else:
        print(f"      (dati OSM {bt.DEFAULT_INPUT} assenti: confronto con la rigenerazione saltato)")


def main():
    for t in (test_geometria, test_citta_sintetica, test_id_e_nomi, test_parametri, test_scorciatoie,
              test_controlli_negativi, test_dati_sporchi, test_id_stabili_tra_esecuzioni,
              test_download_finto, test_riga_di_comando_senza_rete, test_file_generato):
        print(f"--- {t.__name__}")
        t()
    falliti = [r for r in RISULTATI if not r[1]]
    print(f"\n{len(RISULTATI) - len(falliti)}/{len(RISULTATI)} controlli superati")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
