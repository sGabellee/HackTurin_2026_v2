"""Sfondo cartografico per la dashboard: tutte le strade OSM con i nomi, in un file leggero.

La mappa degli incroci (topology.json) ha solo gli incroci e i tratti tra uno e l'altro. Per far
sembrare la dashboard una mappa vera, il collettore disegna sotto di essa anche tutte le strade
(comprese quelle senza incroci con altri nodi) e i nomi, leggendo basemap.json. Il file nasce dagli
stessi dati OpenStreetMap di build_topology.py, senza scaricare niente: la dashboard resta offline.

Uso:  python build_basemap.py [--input ../../../data/osm/torino_crocetta.json] [--out ../../collector/basemap.json]

basemap.json: {"version": 1, "source", "bbox": [s, w, n, e],
               "ways":   [{"c": 0|1|2, "n": nome o null, "g": [[lat, lon], ...]}],     c: 0 grandi assi, 1 collettrici, 2 locali
               "labels": [{"n": nome, "lat", "lon", "a": gradi di rotazione del testo, "c": classe}]}
"""
import argparse
import json
import math
import sys
from pathlib import Path

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI))

import build_topology as bt  # noqa: E402

CLASSE = {"trunk": 0, "primary": 0, "secondary": 0, "primary_link": 1, "secondary_link": 1, "trunk_link": 1,
          "tertiary": 1, "tertiary_link": 1, "unclassified": 2, "residential": 2, "living_street": 2}
MIN_LABEL_M = {0: 120.0, 1: 140.0, 2: 160.0}   # tratti piu' corti di cosi' non hanno il nome


def lunghezza(g):
    return sum(bt.haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(g, g[1:]))


def punto_a_meta(g):
    """(lat, lon, gradi del testo) a meta' della lunghezza; il testo non va mai capovolto."""
    meta, acc = lunghezza(g) / 2, 0.0
    for a, b in zip(g, g[1:]):
        d = bt.haversine_m(a[0], a[1], b[0], b[1])
        if acc + d >= meta and d > 0:
            f = (meta - acc) / d
            lat, lon = a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
            dy = (b[0] - a[0]) * 111320
            dx = (b[1] - a[1]) * 111320 * math.cos(math.radians(lat))
            ang = math.degrees(math.atan2(-dy, dx))        # SVG: y verso il basso
            if ang > 90:
                ang -= 180
            elif ang < -90:
                ang += 180
            return lat, lon, round(ang, 1)
        acc += d
    return g[0][0], g[0][1], 0.0


def build_basemap(osm, bbox=None, simplify_m=4.0):
    ways, labels = [], []
    per_nome = {}
    for w in bt.parse_ways(osm):
        classe = CLASSE.get(w.hw)
        if classe is None:
            continue
        g = bt.simplify([(p[1], p[2]) for p in w.pts], simplify_m)
        if len(g) < 2:
            continue
        g = [[round(p[0], 5), round(p[1], 5)] for p in g]
        ways.append({"c": classe, "n": w.name, "g": g})
        if w.name:
            L = lunghezza(g)
            if L >= MIN_LABEL_M[classe]:
                per_nome.setdefault(w.key, []).append((L, classe, w.name, g))
    for key, lista in per_nome.items():
        lista.sort(key=lambda x: -x[0])
        # una scritta per ogni tratto lungo, al massimo 4 per strada (i corsi lunghi si leggono in piu' punti)
        for L, classe, nome, g in lista[:4]:
            lat, lon, ang = punto_a_meta(g)
            labels.append({"n": nome, "lat": round(lat, 5), "lon": round(lon, 5), "a": ang, "c": classe})
    ways.sort(key=lambda x: -x["c"])    # prima le locali (sotto), poi i grandi assi
    return {"version": 1, "source": "© OpenStreetMap contributors, ODbL", "bbox": list(bbox) if bbox else osm.get("bbox"),
            "ways": ways, "labels": labels}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", default=str(bt.DEFAULT_INPUT))
    ap.add_argument("--out", default=str(QUI.parents[1] / "collector" / "basemap.json"))
    ap.add_argument("--simplify-m", type=float, default=4.0)
    args = ap.parse_args(argv)
    osm = bt.load_osm(args.input)
    bbox = osm.get("bbox") or bt.DEFAULT_BBOX
    base = build_basemap(osm, bbox, args.simplify_m)
    out = Path(args.out)
    out.write_text(json.dumps(base, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(base['ways'])} strade, {len(base['labels'])} nomi, {out.stat().st_size // 1024} KB -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
