"""Test dello sfondo cartografico (tools/build_basemap.py e /api/basemap del collettore).

Uso:  python test_basemap.py   (dalla cartella node/tools/tests)
"""
import json
import sys
from pathlib import Path

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI.parents[1] / "python"))
sys.path.insert(0, str(QUI.parents[0]))
sys.path.insert(0, str(QUI.parents[1].parent / "collector"))

import build_basemap as bb  # noqa: E402
import fetch_osm_tiles as ft  # noqa: E402

RISULTATI = []


def check(nome, ok, info=""):
    RISULTATI.append((nome, bool(ok), info))
    print(f"  [{'OK ' if ok else 'ERR'}] {nome}" + (f"  ({info})" if info and not ok else ""))


def way(i, hw, name, pts):
    return {"type": "way", "id": i, "nodes": list(range(i * 100, i * 100 + len(pts))),
            "geometry": [{"lat": a, "lon": b} for a, b in pts], "tags": {"highway": hw, "name": name} if name else {"highway": hw}}


def main():
    osm = {"bbox": [45.0, 7.0, 45.01, 7.01], "elements": [
        way(1, "primary", "Corso Lungo", [(45.0, 7.0), (45.0, 7.004), (45.0, 7.008)]),       # ~630 m
        way(2, "residential", "Via Corta", [(45.001, 7.0), (45.001, 7.0005)]),                # ~40 m: niente nome
        way(3, "footway", "Sentiero", [(45.002, 7.0), (45.002, 7.001)]),                      # non e' una strada
        way(4, "residential", None, [(45.003, 7.0), (45.003, 7.003)]),                        # senza nome
    ]}
    b = bb.build_basemap(osm)
    check("strade stradali tenute, il sentiero no", len(b["ways"]) == 3, len(b["ways"]))
    check("classi: primary=0, residential=2", {w["c"] for w in b["ways"]} == {0, 2})
    check("prima le locali, poi i grandi assi (disegno)", b["ways"][-1]["c"] == 0)
    nomi = [l["n"] for l in b["labels"]]
    check("nome solo per i tratti abbastanza lunghi", nomi == ["Corso Lungo"], nomi)
    a = b["labels"][0]["a"]
    check("testo mai capovolto (-90..90)", -90 <= a <= 90, a)
    # tile di OpenStreetMap: conteggio e coordinate (senza rete)
    check("tile (45.0653, 7.6669) a zoom 17", ft.tile_xy(45.0653, 7.6669, 17) == (68327, 47116), ft.tile_xy(45.0653, 7.6669, 17))
    el = ft.elenco(ft.BBOX, 17)
    check("zona del progetto: poche decine di tile, non centinaia", 50 < len(el) <= 150, len(el))
    check("tile senza doppioni e dentro il limite del server pubblico", len(set(el)) == len(el) and len(el) <= ft.MAX_TILE)
    check("User-Agent che identifica il progetto", "CROSS" in ft.UA and "github.com" in ft.UA)
    reale = QUI.parents[1].parent / "collector" / "basemap.json"
    d = json.loads(reale.read_text(encoding="utf-8"))
    check("basemap.json generato: tante strade e nomi", len(d["ways"]) > 1000 and len(d["labels"]) > 100, (len(d["ways"]), len(d["labels"])))
    check("basemap.json nella zona di Torino", all(45.04 < p[0] < 45.09 and 7.63 < p[1] < 7.72 for w in d["ways"] for p in w["g"]))
    check("basemap.json leggero (< 400 KB)", reale.stat().st_size < 400_000, reale.stat().st_size)
    import collector as col
    c = col.Collector({"nodes": {}, "listen": {"udp": False}, "db": ":memory:", "basemap": str(reale),
                       "tiles": str(QUI.parents[1].parent / "collector" / "tiles"),
                       "topology": str(QUI.parents[1] / "python" / "topology.json")})
    if c is not None:
        r = c.api_basemap()
        check("collettore: /api/basemap serve strade e nomi", len(r["ways"]) > 1000 and len(r["labels"]) > 100)
    tdir = QUI.parents[1].parent / "collector" / "tiles"
    if (tdir / "index.json").exists():
        idx = json.loads((tdir / "index.json").read_text(encoding="utf-8"))
        z = idx["zoom"]
        check("indice delle tile coerente con i file", all((tdir / str(z) / str(x) / f"{y}.png").exists() for x, y in idx["tiles"]))
        check("tutte le tile della zona presenti", len(idx["tiles"]) == len(ft.elenco(ft.BBOX, z)), len(idx["tiles"]))
        check("attribuzione OpenStreetMap nell'indice", "OpenStreetMap" in idx.get("attribution", ""))
        if c is not None:
            x, y = idx["tiles"][0]
            check("collettore: serve una tile PNG", (c.tile_file(f"/tiles/{z}/{x}/{y}.png") or b"").startswith(b"PNG", 1))
            check("collettore: niente percorsi fuori da tiles/", c.tile_file("/tiles/../collector.py") is None
                  and c.tile_file(f"/tiles/{z}/{x}/..%2f..%2fcollector.png") is None)
    ok = sum(1 for r in RISULTATI if r[1])
    print(f"\n{ok}/{len(RISULTATI)} controlli OK")
    sys.exit(0 if ok == len(RISULTATI) else 1)


if __name__ == "__main__":
    main()
