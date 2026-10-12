"""Topologia della citta' e instradamento dei pericoli tra incroci (CROSS v2, ARCHITETTURA.md §4).

Ogni nodo ha una copia locale di topology.json (generato da OpenStreetMap, © OpenStreetMap
contributors, licenza ODbL, vedi tools/build_topology.py) e decide DA SOLO a quali incroci mandare
un pericolo: nessun server in mezzo. Formato v2:
    {"version": 2, "source": "OpenStreetMap (ODbL)", "bbox": [s, w, n, e], "generated": "...",
     "nodes": {"incrocio-42": {"name": "...", "lat": 45.065, "lon": 7.667, "osm": 123456,
               "streets": ["Via Vincenzo Vela", "Corso Stati Uniti"],
               "neighbors": {"incrocio-17": {"bearing": 223.5, "street": "Corso Stati Uniti", "m": 92.0}}}},
     "edges": [{"a": "incrocio-42", "b": "incrocio-17", "street": "Corso Stati Uniti", "m": 92.0,
                "geom": [[45.065, 7.667], [45.0649, 7.6668]]}]}
I vicini valgono in entrambi i sensi anche se sono scritti su uno solo dei due incroci (o solo in
"edges"). Si legge anche il formato v1 del server ("neighbors": {"sud-ovest": "incrocio-2"}).

Regole di instradamento (portate dal server v1, ora sul nodo):
  - direzione nota (gradi bussola, 0 = nord, 90 = est): primo incrocio verso la direzione (curva
    massima 100 gradi) e poi quelli che proseguono "dritti" lungo la strada: 2 incroci, 3 per le
    emergenze o i veicoli oltre 40 km/h;
  - direzione ignota: tutti i vicini;
  - destinazione nota (dst): tutti gli incroci del percorso piu' breve in metri (Dijkstra).

Direzione di un tratto (bearing a -> b), dalla fonte piu' precisa: "bearing" scritto sul vicino,
poi la geometria della strada in "edges" (primo tratto in uscita da a), poi la retta tra le
coordinate, infine il contrario (+180) del bearing scritto sull'altro incrocio.
Lunghezza di un tratto: "m" del vicino o dell'arco, altrimenti la distanza in linea d'aria,
altrimenti 100 m.

Prova da riga di comando:
  python route.py topology.json                      riepilogo
  python route.py topology.json incrocio-42 --heading nord-est [--category emergency]
  python route.py topology.json incrocio-42 --dst incrocio-17
"""
import heapq
import json
import math
from pathlib import Path

MAX_TURN_DEG = 100            # curve fino a 100 gradi: la catena segue la strada ma non torna indietro
HOPS_DIRECTION = 2            # incroci avvisati lungo la direzione del veicolo
HOPS_FAST = 3                 # ... se e' un mezzo di soccorso o va veloce
FAST_KMH = 40                 # "veloce": oltre 40 km/h
SAME_STREET_BONUS_DEG = 15    # a parita' (quasi) di curva si resta sulla stessa strada
DEFAULT_EDGE_M = 100.0        # lunghezza di un tratto senza misure ne' coordinate

# punti cardinali italiani e inglesi (nomi completi e sigle) -> gradi bussola
COMPASS = {"nord": 0, "north": 0, "n": 0,
           "nord-est": 45, "north-east": 45, "ne": 45,
           "est": 90, "east": 90, "e": 90,
           "sud-est": 135, "south-east": 135, "se": 135,
           "sud": 180, "south": 180, "s": 180,
           "sud-ovest": 225, "south-west": 225, "sw": 225, "so": 225,
           "ovest": 270, "west": 270, "w": 270, "o": 270,
           "nord-ovest": 315, "north-west": 315, "nw": 315, "no": 315}
_COMPASS_FLAT = {k.replace("-", ""): v for k, v in COMPASS.items()}


def _log(text):
    print("[route]", text, flush=True)


def _num(v):
    """Numero finito o None (niente bool, niente NaN)."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def compass_deg(value):
    """Direzione -> gradi bussola in [0, 360), oppure None se non e' una direzione.

    Accetta numeri (anche come stringa: "135"), nomi italiani o inglesi senza distinzione di
    maiuscole e con trattino, spazio, trattino basso o niente in mezzo ("nord-est", "Nord Est",
    "nordest", "north_east", "NE"), e le sigle (n, ne, e, se, s, so/sw, o/w, no/nw).
    """
    n = _num(value)
    if n is not None:
        return n % 360
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    for sep in (" ", "_", "-"):
        key = key.replace(sep, "")
    deg = _COMPASS_FLAT.get(key)
    return float(deg) if deg is not None else None


def compass_name(deg):
    """Gradi -> nome italiano del punto cardinale piu' vicino (per i log e la dashboard)."""
    d = _num(deg)
    if d is None:
        return None
    names = ("nord", "nord-est", "est", "sud-est", "sud", "sud-ovest", "ovest", "nord-ovest")
    return names[int(((d % 360) + 22.5) // 45) % 8]


def dist_m(a, b):
    """Distanza approssimata in metri tra due (lat, lon) vicini (equirettangolare)."""
    dy = (b[0] - a[0]) * 111320
    dx = (b[1] - a[1]) * 111320 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def bearing_deg(a, b):
    """Direzione da a verso b in gradi: 0 nord, 90 est, 180 sud, 270 ovest."""
    dy = b[0] - a[0]
    dx = (b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.degrees(math.atan2(dx, dy)) % 360


def turn_deg(a, b):
    """Angolo (0..180) tra due direzioni."""
    return abs((a - b + 180) % 360 - 180)


def _latlon(p):
    """[lat, lon] / (lat, lon) / {"lat", "lon"} -> (lat, lon) o None."""
    if isinstance(p, dict):
        lat, lon = _num(p.get("lat")), _num(p.get("lon"))
    elif isinstance(p, (list, tuple)) and len(p) >= 2:
        lat, lon = _num(p[0]), _num(p[1])
    else:
        return None
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return (lat, lon)


class _Graph:
    """Dati pronti per l'uso (sostituiti in blocco quando si ricarica il file)."""

    def __init__(self):
        self.nodes = {}          # id -> dict originale (pulito)
        self.pos = {}            # id -> (lat, lon)
        self.adj = {}            # id -> set di vicini
        self.bearing = {}        # (a, b) -> gradi
        self.meters = {}         # (a, b) -> metri (simmetrico)
        self.street = {}         # (a, b) -> nome della strada (simmetrico)
        self.edges = []          # [{"a", "b", "street", "m", "geom"}] uno per coppia
        self.meta = {}
        self.forward_unknown = True


class Topology:
    """Topologia della citta'. Nessun metodo solleva eccezioni per dati mancanti o sbagliati:
    un incrocio sconosciuto non ha vicini, una direzione sconosciuta da' None."""

    def __init__(self, data=None, path=None):
        self.file = Path(path) if path else None   # (non "path": e' il metodo del percorso)
        self._mtime = None
        self._loaded = False
        try:
            self._g = self._build(data if isinstance(data, dict) else {})
        except Exception as e:   # dati strani: meglio una topologia vuota che un nodo fermo
            _log(f"topologia non valida ({e!r}): nessun incrocio noto")
            self._g = _Graph()

    # ------------------------------------------------------------------ caricamento

    @classmethod
    def load(cls, path) -> "Topology":
        """Legge topology.json. File mancante o non valido -> topologia vuota (nessun vicino)."""
        topo = cls(None, path)
        topo.maybe_reload(force=True)
        return topo

    @classmethod
    def from_dict(cls, data) -> "Topology":
        return cls(data)

    def maybe_reload(self, force=False) -> bool:
        """Ricarica il file se e' cambiato (es. aggiornato dal collettore). Se il nuovo file non e'
        valido tiene la versione precedente. Ritorna True se ha caricato qualcosa di nuovo."""
        if self.file is None:
            return False
        try:
            mtime = self.file.stat().st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return False
        self._mtime = mtime
        if mtime is None:
            _log(f"{self.file} non trovato: nessun incrocio vicino noto (inoltro a tutti quelli in portata)")
            if force:
                self._g = self._build({})
            return force
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("serve un oggetto JSON")
            g = self._build(raw)
        except Exception as e:   # OSError, ValueError, RecursionError, dati strani...
            _log(f"{self.file} non valido ({e!r}): "
                 + ("tengo la versione precedente" if self._loaded else "nessun incrocio vicino noto"))
            return False
        self._g = g
        self._loaded = True
        _log(f"{len(g.nodes)} incroci, {len(g.edges)} tratti di strada da {self.file.name}")
        return True

    def _build(self, raw):
        g = _Graph()
        g.meta = {k: raw.get(k) for k in ("version", "source", "bbox", "generated") if k in raw}
        g.forward_unknown = bool(raw.get("forward_unknown_direction", True))
        nodes = raw.get("nodes") if isinstance(raw.get("nodes"), dict) else {}
        explicit_bearing = {}
        bad = 0

        def link(a, b, street=None, m=None):
            if not a or not b or a == b:
                return
            g.adj.setdefault(a, set()).add(b)
            g.adj.setdefault(b, set()).add(a)
            key = (a, b) if a < b else (b, a)
            if m is not None and m > 0:
                g.meters[key] = min(m, g.meters.get(key, math.inf))
            if street and key not in g.street:
                g.street[key] = street

        for nid, n in nodes.items():
            nid = str(nid)
            n = n if isinstance(n, dict) else {}
            g.nodes[nid] = n
            g.adj.setdefault(nid, set())
            p = _latlon(n)
            if p:
                g.pos[nid] = p
        for nid, n in nodes.items():
            nid = str(nid)
            nb = (n or {}).get("neighbors") if isinstance(n, dict) else None
            if not isinstance(nb, dict):
                continue
            for k, v in nb.items():
                if isinstance(v, dict):                  # v2: {id: {bearing, street, m}}
                    other = str(k)
                    b = _num(v.get("bearing"))
                    if b is not None:
                        explicit_bearing[(nid, other)] = b % 360
                    street = v.get("street") if isinstance(v.get("street"), str) else None
                    link(nid, other, street, _num(v.get("m")))
                elif isinstance(v, str) and v:           # v1: {"sud-ovest": "incrocio-2"}
                    link(nid, v)
                    b = compass_deg(k)
                    if b is not None and (nid, v) not in explicit_bearing:
                        explicit_bearing[(nid, v, "v1")] = b
                elif v is not None:
                    bad += 1
        geoms = {}               # (a, b) con a < b -> punti della strada da a verso b
        for e in raw.get("edges") if isinstance(raw.get("edges"), list) else []:
            if not isinstance(e, dict) or not e.get("a") or not e.get("b"):
                bad += 1
                continue
            a, b = str(e["a"]), str(e["b"])
            if a == b:
                continue
            street = e.get("street") if isinstance(e.get("street"), str) else None
            link(a, b, street, _num(e.get("m")))
            raw_geom = e.get("geom") if isinstance(e.get("geom"), list) else []
            geom = [q for q in (_latlon(x) for x in raw_geom) if q]
            key = (a, b) if a < b else (b, a)
            if len(geom) >= 2 and key not in geoms:
                if a != key[0]:
                    geom = geom[::-1]
                p0 = g.pos.get(key[0])     # verso giusto anche se "geom" e' scritta al contrario
                if p0 and dist_m(p0, geom[-1]) < dist_m(p0, geom[0]):
                    geom = geom[::-1]
                geoms[key] = geom
        if bad:
            _log(f"{bad} voci di vicini/tratti non valide ignorate")

        # una voce per coppia (per la mappa della dashboard)
        for a in sorted(g.adj):
            for b in sorted(g.adj[a]):
                if a < b:
                    key = (a, b)
                    m = g.meters.get(key)
                    if m is None and a in g.pos and b in g.pos:
                        m = round(dist_m(g.pos[a], g.pos[b]), 1)
                    g.edges.append({"a": a, "b": b, "street": g.street.get(key), "m": m,
                                    "geom": [list(q) for q in geoms.get(key, [])]})

        # direzioni di ogni tratto, nei due sensi
        for a, nbs in g.adj.items():
            for b in nbs:
                deg = explicit_bearing.get((a, b))
                if deg is None:
                    key = (a, b) if a < b else (b, a)
                    geom = geoms.get(key)
                    if geom:
                        pts = geom if a == key[0] else geom[::-1]
                        # primo punto ad almeno 10 m: i punti OSM vicinissimi danno direzioni a caso
                        far = next((q for q in pts[1:] if dist_m(pts[0], q) >= 10), pts[-1])
                        if dist_m(pts[0], far) > 0.5:
                            deg = bearing_deg(pts[0], far)
                if deg is None and a in g.pos and b in g.pos and g.pos[a] != g.pos[b]:
                    deg = bearing_deg(g.pos[a], g.pos[b])
                if deg is None and (b, a) in explicit_bearing:
                    deg = (explicit_bearing[(b, a)] + 180) % 360
                if deg is None:
                    deg = explicit_bearing.get((a, b, "v1"))
                if deg is not None:
                    g.bearing[(a, b)] = round(deg % 360, 2)
        unknown = sorted(n for n in g.adj if n not in g.nodes)
        if unknown:   # restano raggiungibili (es. una board vera non ancora in topologia)
            _log(f"vicini non descritti in 'nodes' (senza coordinate): {', '.join(unknown[:10])}"
                 + (" ..." if len(unknown) > 10 else ""))
        return g

    # ------------------------------------------------------------------ interrogazioni

    def ids(self) -> list:
        return sorted(self._g.nodes)

    def __len__(self):
        return len(self._g.nodes)

    def __contains__(self, nid):
        return str(nid) in self._g.nodes

    def pos(self, nid) -> tuple | None:
        """(lat, lon) dell'incrocio, o None."""
        return self._g.pos.get(str(nid))

    def name(self, nid) -> str | None:
        n = self._g.nodes.get(str(nid)) or {}
        return n.get("name") if isinstance(n.get("name"), str) else None

    def neighbors(self, nid) -> list:
        """Vicini diretti (in entrambi i sensi), ordinati."""
        return sorted(self._g.adj.get(str(nid), ()))

    def bearing(self, a, b) -> float | None:
        """Direzione del tratto a -> b in gradi (vedi in testa); se a e b non sono vicini, la retta
        tra le coordinate; None se non si puo' sapere."""
        a, b = str(a), str(b)
        deg = self._g.bearing.get((a, b))
        if deg is None and a in self._g.pos and b in self._g.pos and self._g.pos[a] != self._g.pos[b]:
            deg = round(bearing_deg(self._g.pos[a], self._g.pos[b]), 2)
        return deg

    def street(self, a, b) -> str | None:
        a, b = str(a), str(b)
        return self._g.street.get((a, b) if a < b else (b, a))

    def meters(self, a, b) -> float:
        """Lunghezza del tratto a - b (vedi in testa)."""
        a, b = str(a), str(b)
        m = self._g.meters.get((a, b) if a < b else (b, a))
        if m is not None:
            return m
        if a in self._g.pos and b in self._g.pos:
            return dist_m(self._g.pos[a], self._g.pos[b])
        return DEFAULT_EDGE_M

    def edges(self) -> list:
        """Un tratto per coppia di vicini: [{"a", "b", "street", "m", "geom"}]."""
        return [dict(e) for e in self._g.edges]

    def meta(self) -> dict:
        return dict(self._g.meta)

    def nearest(self, lat, lon) -> str | None:
        """Incrocio piu' vicino al punto (solo quelli con coordinate)."""
        p = _latlon((lat, lon))
        if p is None or not self._g.pos:
            return None
        return min(self._g.pos, key=lambda nid: (dist_m(p, self._g.pos[nid]), nid))

    def _arrival(self, prev, cur):
        """Direzione di marcia arrivando in cur da prev (conta la curva della strada vicino a cur)."""
        back = self._g.bearing.get((cur, prev))
        if back is not None:
            return (back + 180) % 360
        return self._g.bearing.get((prev, cur))

    def first_toward(self, src, heading_deg, max_turn=MAX_TURN_DEG) -> str | None:
        """Il vicino di src nella direzione piu' simile a heading_deg (curva <= max_turn), o None."""
        src = str(src)
        hd = compass_deg(heading_deg)
        if hd is None:
            return None
        cand = []
        for n in self._g.adj.get(src, ()):
            b = self._g.bearing.get((src, n))
            if b is not None:
                t = turn_deg(hd, b)
                if t <= max_turn:
                    cand.append((t, n))
        return min(cand)[1] if cand else None

    def along(self, src, first, hops, max_turn=MAX_TURN_DEG) -> list:
        """first e poi gli incroci che proseguono "dritti" nella direzione src -> first, fino a hops
        incroci in tutto. A parita' (quasi) di curva resta sulla stessa strada; non torna mai indietro."""
        src, first = str(src), str(first)
        try:
            hops = int(hops)
        except (TypeError, ValueError):
            hops = HOPS_DIRECTION
        if hops <= 0 or first == src:
            return []
        chain, prev, cur = [first], src, first
        while len(chain) < hops:
            heading = self._arrival(prev, cur)
            if heading is None:
                break
            street = self.street(prev, cur)
            cand = []
            for n in self._g.adj.get(cur, ()):
                if n in (prev, src) or n in chain:
                    continue
                b = self._g.bearing.get((cur, n))
                if b is None:
                    continue
                t = turn_deg(heading, b)
                if t > max_turn:
                    continue
                bonus = SAME_STREET_BONUS_DEG if street and self.street(cur, n) == street else 0
                cand.append((t - bonus, t, n))
            if not cand:
                break
            prev, cur = cur, min(cand)[2]
            chain.append(cur)
        return chain

    def path(self, src, dst) -> list:
        """Percorso piu' breve in metri da src a dst, estremi inclusi; [] se non collegati."""
        src, dst = str(src), str(dst)
        adj = self._g.adj
        if src not in adj or dst not in adj:
            return []
        if src == dst:
            return [src]
        best, prev, heap = {src: 0.0}, {}, [(0.0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == dst:
                break
            if d > best.get(u, math.inf):
                continue
            for v in sorted(adj.get(u, ())):
                nd = d + self.meters(u, v)
                if nd < best.get(v, math.inf) - 1e-9:
                    best[v], prev[v] = nd, u
                    heapq.heappush(heap, (nd, v))
        if dst not in prev:
            return []
        out = [dst]
        while out[-1] != src:
            out.append(prev[out[-1]])
        return out[::-1]

    def path_m(self, nodes) -> float:
        """Lunghezza in metri di un percorso (lista di incroci consecutivi)."""
        return sum(self.meters(a, b) for a, b in zip(nodes, nodes[1:]))

    def targets_for(self, src, heading_deg=None, category="risk", speed_kmh=None, dst=None) -> list:
        """Incroci da avvisare per un pericolo visto a src (src escluso), nell'ordine in cui il
        veicolo li incontra.
        - dst: tutti gli incroci del percorso piu' breve verso dst (se non c'e' un percorso: [dst]);
        - direzione nota (gradi o nome del punto cardinale): 2 incroci lungo la strada, 3 se
          category == "emergency" o speed_kmh > 40; se nessun vicino e' in quella direzione si
          avvisano tutti i vicini (meglio un avviso in piu' che uno in meno);
        - direzione ignota: tutti i vicini (se la topologia non lo vieta con
          "forward_unknown_direction": false)."""
        src = str(src)
        if dst not in (None, ""):
            dst = str(dst)
            if dst == src:
                return []
            p = self.path(src, dst)
            return p[1:] if len(p) >= 2 else [dst]
        hd = compass_deg(heading_deg)
        if hd is not None:
            speed = _num(speed_kmh)
            fast = category == "emergency" or (speed is not None and speed > FAST_KMH)
            first = self.first_toward(src, hd)
            if first is not None:
                return self.along(src, first, HOPS_FAST if fast else HOPS_DIRECTION)
        if not self._g.forward_unknown:
            return []
        return self.neighbors(src)

    def summary(self) -> dict:
        """Riepilogo per lo stato del nodo e per i log."""
        g = self._g
        return {"nodes": len(g.nodes), "edges": len(g.edges), "with_position": len(g.pos),
                "file": str(self.file) if self.file else None, **g.meta}


# ---------------------------------------------------------------------------- riga di comando

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Prova dell'instradamento CROSS v2")
    ap.add_argument("topology", nargs="?", default=str(Path(__file__).with_name("topology.json")))
    ap.add_argument("src", nargs="?")
    ap.add_argument("--heading", help="gradi o punto cardinale (nord, sud-est, ...)")
    ap.add_argument("--category", default="risk")
    ap.add_argument("--speed", type=float, default=None, help="km/h")
    ap.add_argument("--dst")
    args = ap.parse_args()

    topo = Topology.load(args.topology)
    print(json.dumps(topo.summary()))
    if args.src:
        print("vicini:", {n: (topo.bearing(args.src, n), topo.street(args.src, n), round(topo.meters(args.src, n)))
                          for n in topo.neighbors(args.src)})
        tg = topo.targets_for(args.src, args.heading, args.category, args.speed, args.dst)
        print("destinatari:", tg, f"({round(topo.path_m([args.src] + tg))} m)" if tg and args.dst else "")
