"""Scarica UNA VOLTA le tile di OpenStreetMap della zona e le salva in src/collector/tiles/.

Sono l'immagine della mappa che la dashboard mette sotto i nodi: dopo il download funziona tutto offline
(la mappa resta quella di oggi, anche se la citta' cambia). Per rispettare la policy di OpenStreetMap
(https://operations.osmfoundation.org/policies/tiles/) lo script e' pensato per poche decine di tile, una
richiesta ogni --delay secondi, con un User-Agent che identifica il progetto, e non riscarica quelle che ci sono.
NON usarlo per aree grandi o per aggiornamenti frequenti.

Uso:  python fetch_osm_tiles.py [--bbox s,w,n,e] [--zoom 17] [--delay 1.0] [--out ../../collector/tiles]
      python fetch_osm_tiles.py --dry-run          (conta solo le tile)

Scrive tiles/<z>/<x>/<y>.png e tiles/index.json: {"zoom", "tiles": [[x, y], ...], "attribution", "bbox"}.
Mappa: (c) OpenStreetMap contributors, ODbL - tile con licenza ODbL/CC-BY-SA: tenere l'attribuzione visibile.
"""
import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

QUI = Path(__file__).resolve().parent
BBOX = (45.055, 7.655, 45.072, 7.685)        # Crocetta / San Secondo / Centro, come build_topology.py
UA = "CROSS-HackTurin/2.0 (progetto hackathon; https://github.com/sGabellee/HackTurin_2026_v2)"
URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
ATTRIBUZIONE = "© OpenStreetMap contributors"
MAX_TILE = 400                                # oltre non e' piu' "poche tile": servirebbe un altro metodo


def tile_xy(lat, lon, z):
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def elenco(bbox, z):
    s, w, n, e = bbox
    x0, y0 = tile_xy(n, w, z)
    x1, y1 = tile_xy(s, e, z)
    return [(x, y) for x in range(min(x0, x1), max(x0, x1) + 1) for y in range(min(y0, y1), max(y0, y1) + 1)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bbox", help="s,w,n,e (default Crocetta/San Secondo)")
    ap.add_argument("--zoom", type=int, default=17)
    ap.add_argument("--delay", type=float, default=1.0, help="secondi tra una richiesta e l'altra (minimo 0.5)")
    ap.add_argument("--out", default=str(QUI.parents[1] / "collector" / "tiles"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    bbox = tuple(float(v) for v in args.bbox.split(",")) if args.bbox else BBOX
    if len(bbox) != 4 or not (14 <= args.zoom <= 18):
        sys.exit("serve --bbox s,w,n,e e --zoom tra 14 e 18")
    tiles = elenco(bbox, args.zoom)
    out = Path(args.out)
    mancanti = [t for t in tiles if not (out / str(args.zoom) / str(t[0]) / f"{t[1]}.png").exists()]
    print(f"zoom {args.zoom}: {len(tiles)} tile, {len(mancanti)} da scaricare")
    if len(tiles) > MAX_TILE:
        sys.exit(f"{len(tiles)} tile sono troppe per il server pubblico di OpenStreetMap (massimo {MAX_TILE}): "
                 f"riduci l'area o lo zoom")
    if args.dry_run:
        return 0
    delay = max(0.5, args.delay)
    fatte = 0
    for x, y in mancanti:
        dest = out / str(args.zoom) / str(x) / f"{y}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(URL.format(z=args.zoom, x=x, y=y), headers={"User-Agent": UA})
        for tentativo in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    dati = r.read()
                if not dati.startswith(b"\x89PNG"):
                    raise ValueError("risposta non PNG")
                dest.write_bytes(dati)
                fatte += 1
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 503):       # il server chiede di rallentare: si rispetta
                    time.sleep(10 * (tentativo + 1))
                    continue
                print(f"  tile {x}/{y}: HTTP {e.code}, salto")
                break
            except Exception as e:
                print(f"  tile {x}/{y}: {e!r}, riprovo")
                time.sleep(3)
        if fatte and fatte % 20 == 0:
            print(f"  {fatte}/{len(mancanti)}")
        time.sleep(delay)
    presenti = [t for t in tiles if (out / str(args.zoom) / str(t[0]) / f"{t[1]}.png").exists()]
    (out / "index.json").write_text(json.dumps({"zoom": args.zoom, "tiles": [list(t) for t in presenti],
                                                "attribution": ATTRIBUZIONE, "bbox": list(bbox)}), encoding="utf-8")
    kb = sum((out / str(args.zoom) / str(x) / f"{y}.png").stat().st_size for x, y in presenti) // 1024
    print(f"{len(presenti)}/{len(tiles)} tile in {out} ({kb} KB)")
    return 0 if len(presenti) == len(tiles) else 1


if __name__ == "__main__":
    sys.exit(main())
