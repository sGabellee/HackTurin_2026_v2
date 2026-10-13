"""Topologia della citta' da OpenStreetMap per CROSS v2 (specifica docs/ARCHITETTURA.md, paragrafo 4).

Legge le strade scaricate da Overpass (JSON "out body geom": way con nodes, geometry e tags) e scrive
topology.json: gli incroci della zona, gli archi di strada tra incroci consecutivi e, per ogni incrocio,
i vicini con direzione (gradi bussola), strada e distanza. Lo usano route.py (instradamento dei pericoli e
del corridoio di soccorso), il simulatore della citta', il collettore e la dashboard (mappa).
Dati (c) OpenStreetMap contributors, licenza ODbL: l'attribuzione resta nel campo "source".

Regole:
  - incrocio = punto OSM dentro l'area in cui si incontrano almeno 3 bracci di strada (gli estremi di una
    way contano 1, i punti interni 2) oppure 2 strade con nome diverso (cambio di nome della strada);
  - i punti vicini dello stesso incrocio reale (corsi con controviali, carreggiate separate) si
    raggruppano: legame per distanza <= --cluster-m (25 m) purche' il gruppo non superi --max-span-m (60 m);
    una rotatoria fino a --roundabout-m (120 m) di diametro diventa un solo incrocio; due gruppi con il centro
    a meno di --min-sep-m (15 m) si uniscono comunque, e cosi' due gruppi entro --max-span-m che hanno in
    comune due strade (la stessa via che incontra i due controviali di un corso largo 40-60 m).
    Posizione = centro del gruppo, bracci = unione dei bracci;
  - arco = tratto di strada tra due incroci consecutivi: si attraversano i punti che non sono incroci (anche
    il punto in cui una strada e' spezzata in due way). Lunghezza in metri lungo la strada da centro a centro,
    strada = tag name (altrimenti il tipo, es. "raccordo"), geometria semplificata con Douglas-Peucker
    (--simplify-m, 3 m). I tratti verso vicoli ciechi o che escono dall'area si scartano;
  - senso unico: "oneway": true se il tratto si percorre solo da "a" verso "b" (gli archi a doppio senso
    hanno "a" con numero piu' basso). Il grafo per gli avvisi resta NON orientato: i vicini sono simmetrici;
  - archi doppi tra gli stessi due incroci (le carreggiate di un corso): si tiene il piu' corto, i sensi si
    sommano (due carreggiate a senso unico opposte = doppio senso);
  - un incrocio rimasto con 2 soli vicini sulla stessa strada (biforcazione tra controviale e carreggiata
    centrale, innesto di un vicolo cieco senza nome) non e' un incrocio: si toglie e i due archi diventano uno;
  - archi "scorciatoia": se tra A e C esiste un altro percorso sulla stessa strada lungo al massimo
    --bypass-ratio (1.15) volte l'arco diretto (+25 m), l'arco A-C salta incroci reali (la carreggiata
    centrale di un corso passa davanti agli incroci dei controviali): si toglie, cosi' gli avvisi lungo
    la strada toccano gli incroci in fila (0 = non togliere mai);
  - si tiene il componente connesso piu' grande (gli incroci senza vicini spariscono con gli altri pezzi);
  - id "incrocio-<n>" da nord-ovest a sud-est (lungo la diagonale), nome "Strada A / Strada B" con le due
    strade piu' importanti per tipo (primaria > secondaria > ...), "osm" = punto OSM rappresentativo.

Uso (dalla cartella src/node/tools):
  python build_topology.py
        dati in data/osm/torino_crocetta.json (area 45.055,7.655,45.072,7.685) -> ../python/topology.json
  python build_topology.py --input zona.json --bbox s,w,n,e --out topology.json
  python build_topology.py --download --bbox 45.055,7.655,45.072,7.685 --save ../../../data/osm/zona.json
        (internet SOLO con --download: piu' server Overpass di riserva, attesa crescente tra i tentativi)
  python build_topology.py --mark 45.06536,7.66689        incrocio piu' vicino a un punto (ripetibile)
  python build_topology.py --verify ../python/topology.json   solo i controlli su un file gia' generato
Esce con codice 1 se i controlli di coerenza falliscono (il file allora non viene scritto, salvo --force),
con codice 2 per un errore di input, di rete o di scrittura (messaggio su stderr, mai un traceback).
Il download scarta le risposte parziali di Overpass ("remark": "runtime error ...") e passa al server dopo.
"""
import argparse
import heapq
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

QUI = Path(__file__).resolve().parent
REPO = QUI.parents[2]
# dati OSM di partenza: l'ultimo scaricato (zona.json, quello da cui nasce topology.json), altrimenti il primo (torino_crocetta.json)
_OSM = REPO / "data" / "osm"
DEFAULT_INPUT = next((p for p in (_OSM / "zona.json", _OSM / "torino_crocetta.json") if p.exists()),
                     _OSM / "torino_crocetta.json")
DEFAULT_OUT = QUI.parent / "python" / "topology.json"
DEFAULT_BBOX = (45.055, 7.655, 45.072, 7.685)      # Crocetta / San Secondo / Centro (s, w, n, e)
VIA_VELA = (45.06536, 7.66689)                     # board del prototipo, Via Vincenzo Vela (verso Corso Stati Uniti)

SOURCE = "© OpenStreetMap contributors, ODbL"
VERSION = 2
R_TERRA = 6371008.8                                # raggio medio (m)
M_PER_DEG = math.pi * R_TERRA / 180.0              # metri per grado di latitudine

DEFAULTS = {"cluster_m": 25.0, "max_span_m": 60.0, "simplify_m": 3.0, "roundabout_m": 120.0, "min_sep_m": 15.0,
            "bypass_ratio": 1.15}

# Tipi di strada usati (stessi della query Overpass). Rango: piu' basso = piu' importante (per il nome).
HW_RANK = {"motorway": 0, "trunk": 1, "primary": 2, "secondary": 3, "tertiary": 4, "unclassified": 5,
           "residential": 6, "living_street": 7, "motorway_link": 8, "trunk_link": 8, "primary_link": 9,
           "secondary_link": 10, "tertiary_link": 11}
HIGHWAYS = frozenset(k for k in HW_RANK if not k.startswith("motorway"))
HW_LABEL = {"motorway": "autostrada", "trunk": "superstrada", "primary": "strada primaria",
            "secondary": "strada secondaria", "tertiary": "strada terziaria", "unclassified": "strada locale",
            "residential": "strada residenziale", "living_street": "zona residenziale"}

OVERPASS_SERVERS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
USER_AGENT = "CROSS-build-topology/2 (hackTurin smart crossing; offline topology builder)"


# ---------------------------------------------------------------------- geometria

def haversine_m(lat1, lon1, lat2, lon2):
    """Distanza in metri sul globo."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_TERRA * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Direzione iniziale da 1 a 2 in gradi bussola (0 = nord, 90 = est)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def polyline_m(pts):
    return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:]))


class _Proj:
    """Proiezione locale equirettangolare (metri): basta per distanze di qualche chilometro."""

    def __init__(self, lat0, lon0):
        self.lat0, self.lon0 = lat0, lon0
        self.k = math.cos(math.radians(lat0)) * M_PER_DEG

    def xy(self, lat, lon):
        return ((lon - self.lon0) * self.k, (lat - self.lat0) * M_PER_DEG)


def _dist_seg(p, a, b):
    """Distanza del punto p dal segmento a-b (coordinate in metri)."""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    l2 = dx * dx + dy * dy
    if l2 <= 1e-12:
        return math.hypot(p[0] - ax, p[1] - ay)
    u = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2))
    return math.hypot(p[0] - ax - u * dx, p[1] - ay - u * dy)


def simplify(pts, tol_m=3.0):
    """Douglas-Peucker su [(lat, lon), ...] con tolleranza in metri. Tiene sempre il primo e l'ultimo punto
    e toglie i punti ripetuti. Iterativo (niente limiti di ricorsione)."""
    pulito = []
    for p in pts:
        p = (float(p[0]), float(p[1]))
        if not pulito or p != pulito[-1]:
            pulito.append(p)
    if len(pulito) <= 2 or tol_m <= 0:
        return pulito
    pr = _Proj(pulito[0][0], pulito[0][1])
    xy = [pr.xy(lat, lon) for lat, lon in pulito]
    tieni = [False] * len(xy)
    tieni[0] = tieni[-1] = True
    pila = [(0, len(xy) - 1)]
    while pila:
        i, j = pila.pop()
        if j <= i + 1:
            continue
        dmax, kmax = -1.0, -1
        for k in range(i + 1, j):
            d = _dist_seg(xy[k], xy[i], xy[j])
            if d > dmax:
                dmax, kmax = d, k
        if dmax > tol_m:
            tieni[kmax] = True
            pila.append((i, kmax))
            pila.append((kmax, j))
    return [p for p, t in zip(pulito, tieni) if t]


def _r6(x):
    return round(float(x), 6)


def _fnum(v):
    """Numero finito come float, altrimenti None (anche per True/False, stringhe, NaN)."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _coord(lat, lon):
    """(lat, lon) se sono numeri finiti dentro i limiti del globo, altrimenti None."""
    lat, lon = _fnum(lat), _fnum(lon)
    if lat is None or lon is None or not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return (lat, lon)


def _osm_id(v):
    """Id OSM intero (anche se scritto come stringa di cifre), altrimenti None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


# ---------------------------------------------------------------------- dati OSM

def parse_bbox(text):
    """"s,w,n,e" -> (s, w, n, e) controllato."""
    try:
        s, w, n, e = (float(v) for v in str(text).replace(" ", "").split(","))
    except ValueError:
        raise ValueError(f"bbox non valida '{text}': servono 4 numeri s,w,n,e") from None
    if not (-90 <= s < n <= 90 and -180 <= w < e <= 180):
        raise ValueError(f"bbox non valida '{text}': serve s < n e w < e")
    return (s, w, n, e)


def overpass_query(bbox, highways=HIGHWAYS, timeout_s=120):
    """Query Overpass: tutte le way delle strade principali e residenziali dell'area, con nodi e geometria."""
    s, w, n, e = bbox
    tipi = "|".join(sorted(highways))
    return (f"[out:json][timeout:{int(timeout_s)}];\n"
            f'way["highway"~"^({tipi})$"]({s},{w},{n},{e});\n'
            "out body geom;")


def download_osm(bbox, servers=OVERPASS_SERVERS, rounds=2, wait_s=5.0, timeout_s=180, opener=None,
                 sleep=None, log=print):
    """Scarica i dati da Overpass provando i server in fila (rounds giri), con attesa crescente tra i
    tentativi. Ritorna il JSON di Overpass con in piu' "bbox". Solleva RuntimeError se tutti falliscono.
    opener/sleep di default (urllib.request.urlopen, time.sleep) si leggono al momento della chiamata."""
    corpo = urllib.parse.urlencode({"data": overpass_query(bbox, timeout_s=max(60, int(timeout_s) - 10))})
    corpo = corpo.encode("utf-8")
    apri = opener or urllib.request.urlopen
    sleep = sleep or time.sleep
    errori, tentativo = [], 0
    for _ in range(max(1, int(rounds))):
        for url in servers:
            if tentativo:
                attesa = min(60.0, wait_s * tentativo)
                log(f"  attendo {attesa:.0f} s prima del prossimo tentativo")
                sleep(attesa)
            tentativo += 1
            try:
                log(f"  scarico da {url} ...")
                req = urllib.request.Request(url, data=corpo, headers={
                    "User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"})
                with apri(req, timeout=timeout_s) as r:
                    raw = r.read()
                osm = json.loads(raw.decode("utf-8"))
                if not isinstance(osm, dict) or not isinstance(osm.get("elements"), list):
                    raise ValueError("risposta senza 'elements'")
                # Overpass risponde 200 anche quando la query si interrompe a meta' (tempo o memoria
                # finiti): gli elementi sono PARZIALI e "remark" contiene "runtime error". Dati monchi =
                # incroci e strade mancanti nella topologia: si prova il prossimo server.
                nota = str(osm.get("remark") or "")
                if "error" in nota.lower():
                    raise ValueError(f"risposta incompleta ({nota[:160]})")
                if not any(isinstance(e, dict) and e.get("type") == "way" for e in osm["elements"]):
                    raise ValueError(f"nessuna strada nella risposta {osm.get('remark') or ''}".strip())
                osm["bbox"] = list(bbox)
                log(f"  ok: {len(osm['elements'])} elementi ({len(raw) / 1e6:.1f} MB)")
                return osm
            except Exception as e:  # rete, HTTP 429/504, JSON rotto: si passa al prossimo server
                errori.append(f"{url}: {e}")
                log(f"  {url} non disponibile: {e}")
    raise RuntimeError("download da Overpass non riuscito:\n  " + "\n  ".join(errori))


def load_osm(path):
    with open(path, encoding="utf-8-sig") as f:        # -sig: accetta anche un file salvato con BOM
        osm = json.load(f)
    if not isinstance(osm, dict) or not isinstance(osm.get("elements"), list):
        raise ValueError(f"{path}: non e' un JSON di Overpass (manca 'elements')")
    return osm


def name_key(name):
    """Chiave per confrontare i nomi ("Piazza C.L.N." e "Piazza CLN" sono la stessa strada)."""
    return " ".join(str(name).replace(".", "").casefold().split())


def hw_label(hw):
    if hw in HW_LABEL:
        return HW_LABEL[hw]
    if str(hw).endswith("_link"):
        return "raccordo"
    return str(hw or "strada")


def _oneway_dir(tags):
    """+1 = solo nel verso della way, -1 = solo nel verso opposto, 0 = doppio senso."""
    v = str(tags.get("oneway", "")).strip().lower()
    if v in ("yes", "true", "1"):
        return 1
    if v in ("-1", "reverse"):
        return -1
    if v in ("no", "false", "0", "alternating", "reversible"):
        return 0
    if tags.get("junction") in ("roundabout", "circular") or tags.get("highway") in ("motorway",):
        return 1
    return 0


class _Way:
    __slots__ = ("id", "name", "key", "hw", "dir", "rb", "pts")

    def __init__(self, wid, tags, pts):
        self.id = wid
        nome = str(tags.get("name") or "").strip()
        self.name = nome or None
        self.key = name_key(nome) if nome else None
        self.hw = tags.get("highway")
        self.dir = _oneway_dir(tags)
        self.rb = tags.get("junction") in ("roundabout", "circular")
        self.pts = pts                      # [(id nodo OSM, lat, lon)]


def parse_ways(osm, highways=HIGHWAYS):
    """Way stradali utili. Coordinate da "geometry" (out geom) o dagli elementi node (out body + >;).
    Un punto senza coordinate valide (o con un id non numerico) spezza la way: dati sporchi non fermano
    lo strumento. Le aree (area=yes) non sono strade percorribili."""
    els = osm.get("elements") if isinstance(osm, dict) else None
    if not isinstance(els, list):
        raise ValueError("dati OSM senza 'elements'")
    node_xy = {}
    for e in els:
        if isinstance(e, dict) and e.get("type") == "node":
            nid, c = _osm_id(e.get("id")), _coord(e.get("lat"), e.get("lon"))
            if nid is not None and c is not None:
                node_xy[nid] = c
    ways = []
    for e in els:
        if not isinstance(e, dict) or e.get("type") != "way":
            continue
        tags = e.get("tags") if isinstance(e.get("tags"), dict) else {}
        hw = tags.get("highway")
        if not isinstance(hw, str) or hw not in highways or str(tags.get("area", "")).lower() == "yes":
            continue
        ids = e.get("nodes") if isinstance(e.get("nodes"), list) else []
        geom = e.get("geometry") if isinstance(e.get("geometry"), list) else []
        pezzo = []
        for i, nid in enumerate(ids):
            nid, c = _osm_id(nid), None
            if nid is not None and len(geom) == len(ids) and isinstance(geom[i], dict):
                c = _coord(geom[i].get("lat"), geom[i].get("lon"))
            if c is None and nid is not None:
                c = node_xy.get(nid)
            if c is None:
                if len(pezzo) >= 2:
                    ways.append(_Way(e.get("id"), tags, pezzo))
                pezzo = []
                continue
            pezzo.append((nid, c[0], c[1]))
        if len(pezzo) >= 2:
            ways.append(_Way(e.get("id"), tags, pezzo))
    return ways


# ---------------------------------------------------------------------- costruzione

class _UF:
    def __init__(self, items):
        self.p = {x: x for x in items}

    def find(self, x):
        r = x
        while self.p[r] != r:
            r = self.p[r]
        while self.p[x] != r:
            self.p[x], x = r, self.p[x]
        return r

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)
        return min(ra, rb)


def _coppie(pos, raggio):
    """[(distanza, a, b)] ordinate, con a < b e distanza <= raggio; pos = {id: (x, y) in metri}."""
    cella = max(1.0, float(raggio))
    griglia = defaultdict(list)
    for k, (x, y) in pos.items():
        griglia[(int(math.floor(x / cella)), int(math.floor(y / cella)))].append(k)
    out = []
    for (cx, cy), qui in griglia.items():
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for a in qui:
                    for b in griglia.get((cx + dx, cy + dy), ()):
                        if a < b:
                            d = math.dist(pos[a], pos[b])
                            if d <= raggio:
                                out.append((d, a, b))
    out.sort()
    return out


def _seg_ok(seg, a, ways):
    """Il segmento seg = (u, v, way) si puo' percorrere partendo dal suo estremo a?"""
    u, _v, wi = seg
    d = ways[wi].dir
    return d == 0 or (d == 1) == (a == u)


def build_topology(osm, bbox=None, cluster_m=DEFAULTS["cluster_m"], max_span_m=DEFAULTS["max_span_m"],
                   simplify_m=DEFAULTS["simplify_m"], roundabout_m=DEFAULTS["roundabout_m"],
                   min_sep_m=DEFAULTS["min_sep_m"], bypass_ratio=DEFAULTS["bypass_ratio"], highways=HIGHWAYS,
                   log=None):
    """Dati Overpass -> dizionario topology.json v2. log(str) riceve il riepilogo dei passaggi."""
    log = log or (lambda *_a: None)
    if not isinstance(osm, dict):
        raise ValueError("dati OSM non validi: serve il JSON di Overpass (un oggetto con 'elements')")
    if bbox is None and isinstance(osm.get("bbox"), (list, tuple)) and len(osm["bbox"]) == 4:
        bbox = tuple(float(v) for v in osm["bbox"])
    ways = parse_ways(osm, highways)
    if not ways:
        raise ValueError("nessuna strada utile nei dati")

    # ---- grafo dei punti OSM: segmenti consecutivi delle way
    xy, keys, ways_at = {}, defaultdict(set), defaultdict(set)
    adj, segs = defaultdict(list), []
    for wi, w in enumerate(ways):
        for nid, lat, lon in w.pts:
            xy.setdefault(nid, (lat, lon))
            ways_at[nid].add(wi)
            if w.key:
                keys[nid].add(w.key)
        for i in range(len(w.pts) - 1):
            u, v = w.pts[i][0], w.pts[i + 1][0]
            if u == v:
                continue
            sid = len(segs)
            segs.append((u, v, wi))
            adj[u].append((sid, v))
            adj[v].append((sid, u))
    if not adj:
        raise ValueError("nessun tratto di strada valido nei dati (way con un solo punto o punti ripetuti)")

    if bbox:
        s, w_, n, e = bbox

        def dentro(nid):
            lat, lon = xy[nid]
            return s <= lat <= n and w_ <= lon <= e
    else:
        def dentro(_nid):
            return True

    lats = [xy[k][0] for k in adj]
    lons = [xy[k][1] for k in adj]
    pr = _Proj((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)
    pxy = {nid: pr.xy(*xy[nid]) for nid in adj}

    # grado = numero di segmenti che toccano il punto (estremo di una way 1, punto interno 2)
    xset = {nid for nid in adj if dentro(nid) and (len(adj[nid]) >= 3 or len(keys[nid]) >= 2)}
    xpts = sorted(xset)
    log(f"strade: {len(ways)} way, {len(adj)} punti OSM, {len(xpts)} punti d'incrocio")
    if not xpts:
        raise ValueError("nessun incrocio nell'area")

    # ---- tratti tra punti d'incrocio consecutivi
    usati, grezzi, scartati = set(), [], 0
    for p in xpts:
        for sid, v in adj[p]:
            if sid in usati:
                continue
            usati.add(sid)
            cammino, sids, prima, cur, ok = [p], [sid], sid, v, True
            while True:
                cammino.append(cur)
                if cur in xset:
                    break
                if not dentro(cur):
                    ok = False                         # esce dall'area: non so dove arriva
                    break
                avanti = [(s2, o) for s2, o in adj[cur] if s2 != prima]
                if len(avanti) != 1 or avanti[0][0] in usati:
                    ok = False                         # vicolo cieco
                    break
                prima, cur = avanti[0]
                usati.add(prima)
                sids.append(prima)
            if not ok:
                scartati += 1
                continue
            q = cammino[-1]
            fwd = all(_seg_ok(segs[s2], a, ways) for s2, a in zip(sids, cammino))
            bwd = all(_seg_ok(segs[s2], b, ways) for s2, b in zip(sids, cammino[1:]))
            per_nome = defaultdict(float)             # strada dell'arco = quella piu' lunga nel tratto
            for s2, a, b in zip(sids, cammino, cammino[1:]):
                wy = ways[segs[s2][2]]
                per_nome[wy.key or ("~" + hw_label(wy.hw))] += math.dist(pxy[a], pxy[b])
            grezzi.append({"p": p, "q": q, "path": cammino, "fwd": fwd, "bwd": bwd, "names": dict(per_nome)})
    log(f"tratti di strada tra punti d'incrocio: {len(grezzi)} (scartati {scartati} verso vicoli ciechi o fuori area)")

    # ---- raggruppamento dei punti dello stesso incrocio reale
    uf = _UF(xpts)
    membri = {x: [x] for x in xpts}
    span = {x: 0.0 for x in xpts}
    chiavi = {x: set(keys[x]) for x in xpts}

    def unisci(ra, rb):
        r = uf.union(ra, rb)
        altro = rb if r == ra else ra
        membri[r].extend(membri.pop(altro))
        chiavi[r] |= chiavi.pop(altro)
        span.pop(altro, None)
        span[r] = max(math.dist(pxy[i], pxy[j]) for i in membri[r] for j in membri[r])
        return r

    def estensione(ra, rb):
        """Estensione del gruppo che nascerebbe unendo ra e rb (distanza massima tra i punti)."""
        return max(span[ra], span[rb],
                   max(math.dist(pxy[i], pxy[j]) for i in membri[ra] for j in membri[rb]))

    def centro(ms):
        ms = sorted(ms, key=str)   # ordine fisso: la somma in virgola mobile non dipende dall'hash dei set
        return (math.fsum(xy[m][0] for m in ms) / len(ms), math.fsum(xy[m][1] for m in ms) / len(ms))

    # rotatorie: tutti i punti d'incrocio sull'anello diventano un incrocio solo
    rb_uf = _UF([nid for nid in adj if any(ways[wi].rb for wi in ways_at[nid])])
    for u, v, wi in segs:
        if ways[wi].rb:
            rb_uf.union(u, v)
    anelli = defaultdict(list)
    for nid in rb_uf.p:
        anelli[rb_uf.find(nid)].append(nid)
    rotatorie = 0
    for anello in anelli.values():
        sull_anello = [m for m in sorted(anello) if m in xset]
        if len(sull_anello) < 2:
            continue
        if max(math.dist(pxy[a], pxy[b]) for a in anello for b in anello) <= roundabout_m:
            for m in sull_anello[1:]:
                ra, rb = uf.find(sull_anello[0]), uf.find(m)
                if ra != rb:
                    unisci(ra, rb)
            rotatorie += 1

    # legame per distanza, senza superare l'estensione massima
    for _d, a, b in _coppie({x: pxy[x] for x in xpts}, cluster_m):
        ra, rb = uf.find(a), uf.find(b)
        if ra != rb and estensione(ra, rb) <= max(max_span_m, span[ra], span[rb]):
            unisci(ra, rb)

    # gruppi vicini: centri a meno di min_sep_m (sempre) o stesse due strade (es. le due giunzioni di una
    # via con i controviali di un corso, a 30-50 m l'una dall'altra) entro l'estensione massima
    per_centri = per_nomi = 0
    cambiato = True
    while cambiato:
        cambiato = False
        cen = {r: pr.xy(*centro(ms)) for r, ms in membri.items()}
        toccati = set()
        for d, a, b in _coppie(cen, max(max_span_m, min_sep_m)):
            if a in toccati or b in toccati:
                continue                            # cambiato in questo giro: si ricontrolla al prossimo
            if d < min_sep_m:
                unisci(a, b)
                per_centri += 1
            elif len(chiavi[a] & chiavi[b]) >= 2 and estensione(a, b) <= max(max_span_m, span[a], span[b]):
                unisci(a, b)
                per_nomi += 1
            else:
                continue
            toccati.update((a, b))
            cambiato = True

    gruppi = {}
    for r, ms in membri.items():
        lat, lon = centro(ms)
        c = pr.xy(lat, lon)
        raggio = max(math.dist(pxy[m], c) for m in ms)
        rapp = sorted(ms, key=lambda m: (-len(adj[m]), math.dist(pxy[m], c), m))[0]
        gruppi[r] = {"members": sorted(ms), "lat": lat, "lon": lon, "xy": c, "r": raggio, "osm": rapp}
    multipli = sum(1 for g in gruppi.values() if len(g["members"]) > 1)
    log(f"incroci dopo il raggruppamento: {len(gruppi)} ({multipli} da piu' punti; {rotatorie} rotatorie, "
        f"{per_nomi} uniti per stesse strade, {per_centri} per centri a meno di {min_sep_m:g} m; "
        f"estensione max {max((span[r] for r in gruppi), default=0):.0f} m)")
    gruppo_di = {m: r for r, g in gruppi.items() for m in g["members"]}

    # ---- archi tra incroci (gruppi)
    def taglia(path_xy, ga, gb):
        """Toglie i punti dentro i due incroci: la linea va da centro a centro senza zig-zag."""
        ra, rb = ga["r"] + 1.0, gb["r"] + 1.0
        i = 0
        while i < len(path_xy) and math.dist(pr.xy(*path_xy[i]), ga["xy"]) <= ra:
            i += 1
        j = len(path_xy) - 1
        while j >= 0 and math.dist(pr.xy(*path_xy[j]), gb["xy"]) <= rb:
            j -= 1
        dentro_arco = path_xy[i:j + 1] if i <= j else []
        return [(ga["lat"], ga["lon"])] + dentro_arco + [(gb["lat"], gb["lon"])]

    archi = {}                                      # (gruppo a, gruppo b) con a < b -> arco orientato a -> b

    def metti(a, b, pts, fwd, bwd, skey):
        """Arco tra i gruppi a e b (punti da a verso b): tiene il piu' corto, unisce i sensi."""
        if a == b:
            return
        if a > b:
            a, b, pts, fwd, bwd = b, a, pts[::-1], bwd, fwd
        m = polyline_m(pts)
        vecchio = archi.get((a, b))
        if vecchio is None or m < vecchio["m"] - 1e-9:
            archi[(a, b)] = {"a": a, "b": b, "pts": pts, "m": m, "skey": skey,
                             "fwd": fwd or (vecchio is not None and vecchio["fwd"]),
                             "bwd": bwd or (vecchio is not None and vecchio["bwd"])}
        else:
            vecchio["fwd"] = vecchio["fwd"] or fwd
            vecchio["bwd"] = vecchio["bwd"] or bwd

    interni = 0
    for g in grezzi:
        a, b = gruppo_di[g["p"]], gruppo_di[g["q"]]
        if a == b:
            interni += 1                            # tratto interno all'incrocio
            continue
        skey = max(sorted(g["names"]), key=lambda k2: g["names"][k2])
        metti(a, b, taglia([xy[nid] for nid in g["path"]], gruppi[a], gruppi[b]), g["fwd"], g["bwd"], skey)
    log(f"archi tra incroci: {len(archi)} (tratti interni agli incroci {interni}, doppioni uniti "
        f"{len(grezzi) - interni - len(archi)})")

    # nomi visualizzati: per ogni chiave il nome OSM piu' frequente
    conta_nomi = defaultdict(lambda: defaultdict(int))
    for wy in ways:
        if wy.key:
            conta_nomi[wy.key][wy.name] += 1

    def mostra(skey):
        if skey.startswith("~"):
            return skey[1:]
        c = conta_nomi.get(skey)
        return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if c else skey

    def strade_gruppo(r):
        """Strade dell'incrocio: ({chiave con nome: rango migliore}, {"~tipo": rango} per le strade senza nome)."""
        nomi, tipi = {}, {}
        for m in gruppi[r]["members"]:
            for wi in ways_at[m]:
                wy = ways[wi]
                rk = HW_RANK.get(wy.hw, 50)
                if wy.key:
                    nomi[wy.key] = min(rk, nomi.get(wy.key, 99))
                else:
                    k2 = "~" + hw_label(wy.hw)
                    tipi[k2] = min(rk, tipi.get(k2, 99))
        return nomi, tipi

    def vicini_di():
        v = defaultdict(dict)
        for (a, b), ed in archi.items():
            v[a][b] = ed
            v[b][a] = ed
        return v

    def orienta(ed, da):
        """(punti, fwd, bwd) dell'arco visto partendo dal gruppo da."""
        if ed["a"] == da:
            return ed["pts"], ed["fwd"], ed["bwd"]
        return ed["pts"][::-1], ed["bwd"], ed["fwd"]

    def togli_biforcazioni():
        """Un incrocio con 2 soli vicini sulla stessa strada (e nessun'altra strada con nome) non e' un
        incrocio: i suoi due archi diventano uno."""
        tolti = 0
        vic = vicini_di()
        coda = deque(sorted(vic))
        while coda:
            x = coda.popleft()
            if len(vic.get(x, {})) != 2:
                continue
            (n1, e1), (n2, e2) = sorted(vic[x].items())
            nomi, _tipi = strade_gruppo(x)
            if e1["skey"] != e2["skey"] or len(nomi) > 1 or (nomi and e1["skey"] not in nomi):
                continue
            p1, f1, b1 = orienta(e1, n1)            # n1 -> x
            p2, f2, b2 = orienta(e2, x)             # x -> n2
            del archi[(min(n1, x), max(n1, x))]
            del archi[(min(n2, x), max(n2, x))]
            metti(n1, n2, p1 + p2[1:], f1 and f2, b1 and b2, e1["skey"])
            tolti += 1
            vic = vicini_di()
            coda.extend((n1, n2))                   # i vicini possono essere diventati biforcazioni
        return tolti

    def togli_scorciatoie():
        """Arco "scorciatoia": la carreggiata centrale di un corso va dritta da A a C mentre i controviali
        toccano gli incroci B in mezzo. Se esiste un altro percorso A -> ... -> C sulla stessa strada lungo
        al massimo bypass_ratio volte l'arco (+25 m), l'arco diretto salta incroci reali: si toglie (il
        grafo resta connesso, gli avvisi lungo la strada arrivano a tutti gli incroci in fila)."""
        if not bypass_ratio or bypass_ratio <= 1.0:
            return 0
        tolti = 0
        vic = vicini_di()
        for (a, c), ed in sorted(archi.items(), key=lambda kv: (-kv[1]["m"], kv[0])):
            if (a, c) not in archi:
                continue
            limite = max(bypass_ratio * ed["m"], ed["m"] + 25.0)
            dist, heap, trovato = {a: 0.0}, [(0.0, a)], False
            while heap:
                d, x = heapq.heappop(heap)
                if d > dist.get(x, math.inf):
                    continue
                if x == c:
                    trovato = True
                    break
                for y, e2 in vic[x].items():
                    if e2 is ed or e2["skey"] != ed["skey"]:
                        continue
                    nd = d + e2["m"]
                    if nd <= limite and nd < dist.get(y, math.inf):
                        dist[y] = nd
                        heapq.heappush(heap, (nd, y))
            if trovato:
                del archi[(a, c)]
                del vic[a][c]
                del vic[c][a]
                tolti += 1
        return tolti

    biforcazioni = togli_biforcazioni()
    scorciatoie = togli_scorciatoie()
    biforcazioni += togli_biforcazioni()
    log(f"biforcazioni sulla stessa strada tolte: {biforcazioni}; scorciatoie tolte: {scorciatoie}")

    # ---- componente connesso piu' grande
    vic = vicini_di()
    visti, comp = set(), []
    for x in sorted(vic):
        if x in visti:
            continue
        pezzo, coda = [], deque([x])
        visti.add(x)
        while coda:
            c = coda.popleft()
            pezzo.append(c)
            for y in vic[c]:
                if y not in visti:
                    visti.add(y)
                    coda.append(y)
        comp.append(pezzo)
    if not comp:
        raise ValueError("nessun arco tra incroci: grafo vuoto")
    comp.sort(key=lambda c: (-len(c), min(c)))
    tenuti = set(comp[0])
    log(f"componenti connessi: {len(comp)} (tengo il piu' grande, {len(tenuti)} incroci; "
        f"scartati {sum(len(c) for c in comp[1:])} incroci scollegati e "
        f"{len(gruppi) - len(visti)} senza vicini)")

    # ---- id da nord-ovest a sud-est
    ordine = sorted(tenuti, key=lambda r: (gruppi[r]["xy"][0] - gruppi[r]["xy"][1], gruppi[r]["osm"]))
    nid_di = {r: f"incrocio-{i + 1}" for i, r in enumerate(ordine)}
    numero = {r: i + 1 for i, r in enumerate(ordine)}

    nodes = {}
    for r in ordine:
        g = gruppi[r]
        nomi, tipi = strade_gruppo(r)
        bracci = defaultdict(int)
        for y, ed in vic[r].items():
            if y in tenuti:
                bracci[ed["skey"]] += 1
        ordinati = sorted(nomi, key=lambda k2: (nomi[k2], -bracci.get(k2, 0), mostra(k2)))
        strade = [mostra(k2) for k2 in ordinati]
        if len(strade) < 2:
            extra = sorted(tipi, key=lambda k2: (tipi[k2], -bracci.get(k2, 0), k2))
            strade += [mostra(k2) for k2 in extra if mostra(k2) not in strade]
        strade = strade or ["strada"]
        nodes[nid_di[r]] = {"name": " / ".join(strade[:2]), "lat": _r6(g["lat"]), "lon": _r6(g["lon"]),
                            "osm": g["osm"], "streets": strade, "neighbors": {}}

    edges = []
    for (a, b), ed in archi.items():
        if a not in tenuti or b not in tenuti:
            continue
        pts, fwd, bwd = ed["pts"], ed["fwd"], ed["bwd"]
        if fwd and not bwd:
            oneway, da, a_ = True, a, b
        elif bwd and not fwd:
            oneway, da, a_ = True, b, a
            pts = pts[::-1]
        else:
            oneway = False
            da, a_ = (a, b) if numero[a] < numero[b] else (b, a)
            if da != a:
                pts = pts[::-1]
        geom = simplify(pts, simplify_m)
        m = round(polyline_m(pts), 1)
        strada = mostra(ed["skey"])
        edges.append({"a": nid_di[da], "b": nid_di[a_], "street": strada, "m": m,
                      "geom": [[_r6(lat), _r6(lon)] for lat, lon in geom], "oneway": oneway})
        na, nb = nodes[nid_di[da]], nodes[nid_di[a_]]
        ga, gb = gruppi[da], gruppi[a_]
        na["neighbors"][nid_di[a_]] = {"bearing": round(bearing_deg(ga["lat"], ga["lon"], gb["lat"], gb["lon"]), 1),
                                       "street": strada, "m": m}
        nb["neighbors"][nid_di[da]] = {"bearing": round(bearing_deg(gb["lat"], gb["lon"], ga["lat"], ga["lon"]), 1),
                                       "street": strada, "m": m}
    edges.sort(key=lambda ed: (min(_num(ed["a"]), _num(ed["b"])), max(_num(ed["a"]), _num(ed["b"]))))
    for nd in nodes.values():
        nd["neighbors"] = dict(sorted(nd["neighbors"].items(), key=lambda kv: _num(kv[0])))

    if bbox is None:
        bbox = (min(n["lat"] for n in nodes.values()), min(n["lon"] for n in nodes.values()),
                max(n["lat"] for n in nodes.values()), max(n["lon"] for n in nodes.values()))
    topo = {"version": VERSION, "source": SOURCE, "bbox": [float(v) for v in bbox],
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "osm_base": (osm.get("osm3s") or {}).get("timestamp_osm_base") if isinstance(osm.get("osm3s"), dict) else None,
            "params": {"cluster_m": cluster_m, "max_span_m": max_span_m, "simplify_m": simplify_m,
                       "roundabout_m": roundabout_m, "min_sep_m": min_sep_m, "bypass_ratio": bypass_ratio},
            "nodes": nodes, "edges": edges}
    if topo["osm_base"] is None:
        del topo["osm_base"]
    return topo


def _num(nid):
    try:
        return int(str(nid).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 10 ** 9


# ---------------------------------------------------------------------- controlli e riepilogo

def _nodi_edges(topo):
    """(nodes, edges) di un topology.json anche malformato: dict e lista, vuoti se mancano."""
    nodes = topo.get("nodes") if isinstance(topo, dict) else None
    edges = topo.get("edges") if isinstance(topo, dict) else None
    return (nodes if isinstance(nodes, dict) else {}), (edges if isinstance(edges, list) else [])


def _vicini(n):
    """Vicini di un nodo come dict {id: dict}; {} se il nodo o il campo sono malformati."""
    nb = n.get("neighbors") if isinstance(n, dict) else None
    return {k: v for k, v in nb.items() if isinstance(v, dict)} if isinstance(nb, dict) else {}


def summary(topo):
    nodes, edges = _nodi_edges(topo)
    edges = [e for e in edges if isinstance(e, dict)]
    gradi = [len(_vicini(n)) for n in nodes.values()]
    per_grado = defaultdict(int)
    for g in gradi:
        per_grado[g] += 1
    metri = [_fnum(e.get("m")) or 0.0 for e in edges]
    return {"nodes": len(nodes), "edges": len(edges),
            "km": round(sum(metri) / 1000.0, 2),
            "avg_degree": round(sum(gradi) / len(gradi), 2) if gradi else 0.0,
            "max_degree": max(gradi, default=0),
            "oneway": sum(1 for e in edges if e.get("oneway") is True),
            "by_degree": dict(sorted(per_grado.items())),
            "avg_edge_m": round(sum(metri) / len(metri), 1) if metri else 0.0}


def nearest(topo, lat, lon):
    """(id, distanza in metri) dell'incrocio piu' vicino al punto; (None, None) se il punto non e' valido
    o non ci sono incroci con coordinate valide."""
    p = _coord(lat, lon)
    best = (None, None)
    if p is None:
        return best
    for nid, n in _nodi_edges(topo)[0].items():
        q = _coord(n.get("lat"), n.get("lon")) if isinstance(n, dict) else None
        if q is None:
            continue
        d = haversine_m(p[0], p[1], q[0], q[1])
        if best[1] is None or d < best[1]:
            best = (nid, d)
    return best


def check_topology(topo, min_sep_m=DEFAULTS["min_sep_m"], density=(10.0, 400.0)):
    """Controlli sul risultato: [(nome, ok, info, bloccante)]. I non bloccanti sono di plausibilita'.
    Non solleva mai eccezioni: un file malformato da' controlli falliti (serve anche per --verify)."""
    out = []

    def c(nome, ok, info="", hard=True):
        out.append((nome, bool(ok), info, hard))

    try:
        _controlli(topo, min_sep_m, density, c)
    except Exception as e:  # file rotto in un modo non previsto: e' un controllo fallito, non un crash
        c("controlli completati", False, f"errore durante i controlli: {e!r}")
    return out


def _controlli(topo, min_sep_m, density, c):
    nodes = topo.get("nodes") if isinstance(topo, dict) else None
    edges = topo.get("edges") if isinstance(topo, dict) else None
    c("formato v2", isinstance(nodes, dict) and isinstance(edges, list) and topo.get("version") == VERSION,
      f"version={topo.get('version') if isinstance(topo, dict) else None}")
    if not isinstance(nodes, dict) or not isinstance(edges, list):
        return
    c("attribuzione OpenStreetMap/ODbL", "OpenStreetMap" in str(topo.get("source")) and "ODbL" in str(topo.get("source")),
      str(topo.get("source")))
    c("almeno un incrocio", len(nodes) > 0)
    nums = sorted(_num(k) for k in nodes)
    c("id incrocio-1..N senza buchi", nums == list(range(1, len(nodes) + 1))
      and all(k == f"incrocio-{_num(k)}" for k in nodes), f"{nums[:5]}...")

    # nodi ben formati: coordinate valide e vicini {id: {...}}; i controlli successivi usano solo questi
    pos, campi = {}, []
    for k, n in nodes.items():
        p = _coord(n.get("lat"), n.get("lon")) if isinstance(n, dict) else None
        nb = n.get("neighbors") if isinstance(n, dict) else None
        if p is None or not all(f in n for f in ("name", "osm", "streets")) or not isinstance(nb, dict) \
                or not all(isinstance(v, dict) for v in nb.values()):
            campi.append(k)
        else:
            pos[k] = p
    c("campi dei nodi completi e validi (name, lat, lon, osm, streets, neighbors)", not campi, f"{campi[:5]}")
    vuoti = [k for k, n in nodes.items() if not isinstance(n, dict) or not str(n.get("name") or "").strip()
             or not isinstance(n.get("streets"), list) or not n["streets"]
             or not all(str(s).strip() for s in n["streets"])]
    c("nomi e strade non vuoti", not vuoti, f"{vuoti[:5]}")
    bbox = topo.get("bbox")
    bb = [_fnum(v) for v in bbox] if isinstance(bbox, list) and len(bbox) == 4 else None
    if bb is not None and None in bb:
        bb = None
    c("bbox [s, w, n, e] valida", bb is not None and bb[0] < bb[2] and bb[1] < bb[3], f"{bbox}")
    if bb is not None:
        s, w, n_, e = bb
        fuori = [k for k, (lat, lon) in pos.items()
                 if not (s - 1e-6 <= lat <= n_ + 1e-6 and w - 1e-6 <= lon <= e + 1e-6)]
        c("incroci dentro la bbox", not fuori, f"{fuori[:5]}")
    soli = [k for k in nodes if not _vicini(nodes[k])]
    c("ogni incrocio ha almeno un vicino", not soli, f"{soli[:5]}")

    # archi validi e coerenti con i vicini
    male, coppie = [], set()
    for ed in edges:
        if not isinstance(ed, dict):
            male.append(("arco non valido", str(ed)[:40]))
            continue
        a, b = ed.get("a"), ed.get("b")
        m = _fnum(ed.get("m"))
        g = ed.get("geom") if isinstance(ed.get("geom"), list) else []
        gp = [_coord(q[0], q[1]) if isinstance(q, (list, tuple)) and len(q) == 2 else None for q in g]
        k = tuple(sorted((str(a), str(b))))
        if not isinstance(a, str) or not isinstance(b, str) or a not in pos or b not in pos or a == b \
                or k in coppie or m is None or m <= 0 or len(gp) < 2 or None in gp \
                or not str(ed.get("street") or "").strip() or not isinstance(ed.get("oneway"), bool):
            male.append((a, b))
            continue
        coppie.add(k)
        pa, pb = pos[a], pos[b]
        if haversine_m(gp[0][0], gp[0][1], pa[0], pa[1]) > 1.0 or haversine_m(gp[-1][0], gp[-1][1], pb[0], pb[1]) > 1.0:
            male.append((a, b, "geom"))
        elif m + 0.5 < haversine_m(pa[0], pa[1], pb[0], pb[1]):
            male.append((a, b, "m"))
        na, nb = _vicini(nodes[a]).get(b), _vicini(nodes[b]).get(a)
        if not na or not nb or _fnum(na.get("m")) != m or _fnum(nb.get("m")) != m \
                or na.get("street") != ed["street"] or nb.get("street") != ed["street"]:
            male.append((a, b, "vicini"))
    c("archi validi (estremi, lunghezza, geometria, nessun doppione)", not male, f"{male[:5]}")
    nvic = sum(len(_vicini(n)) for n in nodes.values())
    c("vicini = archi (simmetrici)", nvic == 2 * len(coppie), f"vicini {nvic}, archi {len(coppie)}")

    # direzioni: opposte nei due sensi e davvero verso il vicino (route.py le usa per scegliere a chi
    # mandare un pericolo: una direzione sbagliata avvisa l'incrocio sbagliato e salta quello giusto)
    asim, storte = [], []
    for k in pos:
        for v, info in _vicini(nodes[k]).items():
            back = _vicini(nodes[v]).get(k) if v in pos else None
            b1 = _fnum(info.get("bearing"))
            b2 = _fnum(back.get("bearing")) if back else None
            if b1 is None or b2 is None or not (0 <= b1 < 360) or abs(((b1 - b2) % 360.0) - 180.0) > 1.0:
                asim.append((k, v))
                continue
            if haversine_m(pos[k][0], pos[k][1], pos[v][0], pos[v][1]) >= 1.0:
                atteso = bearing_deg(pos[k][0], pos[k][1], pos[v][0], pos[v][1])
                if abs((b1 - atteso + 180.0) % 360.0 - 180.0) > 3.0:
                    storte.append((k, v, round(b1, 1), round(atteso, 1)))
    c("vicini simmetrici con direzioni opposte", not asim, f"{asim[:5]}")
    c("direzioni dei vicini verso la loro posizione (entro 3 gradi)", not storte, f"{storte[:5]}")

    # connessione
    if nodes:
        primo = next(iter(nodes))
        visti, coda = {primo}, deque([primo])
        while coda:
            x = coda.popleft()
            for y in _vicini(nodes[x]):
                if y in nodes and y not in visti:
                    visti.add(y)
                    coda.append(y)
        c("grafo connesso", len(visti) == len(nodes), f"{len(visti)}/{len(nodes)} raggiungibili")

    # nessun incrocio doppio
    if pos:
        pr = _Proj(sum(p[0] for p in pos.values()) / len(pos), sum(p[1] for p in pos.values()) / len(pos))
        pxy = {k: pr.xy(*p) for k, p in pos.items()}
        cella = max(1.0, float(min_sep_m))
        gr = defaultdict(list)
        for k, p in pxy.items():
            gr[(int(p[0] // cella), int(p[1] // cella))].append(k)
        doppi = []
        for (cx, cy), qui in gr.items():
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for a in qui:
                        for b in gr.get((cx + dx, cy + dy), ()):
                            if a < b and math.dist(pxy[a], pxy[b]) < min_sep_m:
                                doppi.append((a, b, round(math.dist(pxy[a], pxy[b]), 1)))
        c(f"nessun incrocio doppio a meno di {min_sep_m:g} m", not doppi, f"{sorted(doppi)[:5]}")

    # plausibilita' (non bloccante): densita' di incroci nell'area
    if bb is not None:
        s, w, n_, e = bb
        km2 = haversine_m(s, w, n_, w) * haversine_m((s + n_) / 2, w, (s + n_) / 2, e) / 1e6
        dens = len(nodes) / km2 if km2 > 0 else 0.0
        c("densita' di incroci plausibile", density[0] <= dens <= density[1],
          f"{dens:.0f} incroci/km2 su {km2:.2f} km2", hard=False)


# ---------------------------------------------------------------------- scrittura

def dumps_topology(topo):
    """JSON leggibile e compatto: un incrocio e un arco per riga (diff piccoli quando si rigenera)."""
    sep = (",", ":")
    righe = ["{"]
    testa = [k for k in topo if k not in ("nodes", "edges")]
    for k in testa:
        righe.append(f"  {json.dumps(k)}: {json.dumps(topo[k], ensure_ascii=False, separators=sep)},")
    righe.append('  "nodes": {')
    voci = list(topo.get("nodes", {}).items())
    for i, (k, v) in enumerate(voci):
        righe.append(f"    {json.dumps(k)}: {json.dumps(v, ensure_ascii=False, separators=sep)}"
                     + ("," if i < len(voci) - 1 else ""))
    righe.append("  },")
    righe.append('  "edges": [')
    archi = topo.get("edges", [])
    for i, ed in enumerate(archi):
        righe.append(f"    {json.dumps(ed, ensure_ascii=False, separators=sep)}" + ("," if i < len(archi) - 1 else ""))
    righe.append("  ]")
    righe.append("}")
    testo = "\n".join(righe) + "\n"
    json.loads(testo)                                  # deve restare JSON valido
    return testo


def write_topology(topo, path):
    """Scrittura atomica (file temporaneo accanto e poi rename: chi legge non vede mai un file a meta').
    Se qualcosa va storto il file temporaneo non resta in giro e l'errore (OSError) passa al chiamante."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    testo = dumps_topology(topo)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(testo)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def describe(topo, nid, dist=None):
    """Una riga per l'incrocio e una per ogni vicino (anche su un file malformato: mai eccezioni)."""
    nodes = _nodi_edges(topo)[0]
    n = nodes.get(nid) if isinstance(nodes.get(nid), dict) else {}
    p = _coord(n.get("lat"), n.get("lon"))
    dove = f"({p[0]:.6f}, {p[1]:.6f})" if p else "(posizione non valida)"
    righe = [f"{nid}  {n.get('name') or '?'}  {dove}  osm {n.get('osm')}"
             + (f"  a {dist:.0f} m" if dist is not None else "")]
    for v, info in _vicini(n).items():
        b, m = _fnum(info.get("bearing")), _fnum(info.get("m"))
        altro = nodes.get(v) if isinstance(nodes.get(v), dict) else {}
        righe.append(f"    -> {str(v):14s} {f'{b:6.1f}' if b is not None else '     ?'} gradi  "
                     f"{f'{m:7.1f}' if m is not None else '      ?'} m  {info.get('street') or '?'}"
                     f"  ({altro.get('name') or 'incrocio sconosciuto'})")
    return "\n".join(righe)


def print_report(topo, checks, out=print):
    s = summary(topo)
    out(f"incroci: {s['nodes']}   archi: {s['edges']} ({s['oneway']} a senso unico)   "
        f"strade: {s['km']:.2f} km   grado medio: {s['avg_degree']:.2f} (max {s['max_degree']})   "
        f"arco medio: {s['avg_edge_m']:.0f} m")
    out("incroci per numero di vicini: " + ", ".join(f"{g}: {n}" for g, n in s["by_degree"].items()))
    for nome, ok, info, hard in checks:
        tag = "OK " if ok else ("ERR" if hard else "WARN")
        out(f"[{tag}] {nome}" + ("" if ok else f"  -> {info}") + (f"  ({info})" if ok and not hard and info else ""))


# ---------------------------------------------------------------------- riga di comando

def _latlon(text):
    try:
        lat, lon = (float(v) for v in str(text).replace(" ", "").split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"punto non valido '{text}': serve lat,lon") from None
    if _coord(lat, lon) is None:                       # nan, inf o fuori dal globo: niente risposte a caso
        raise argparse.ArgumentTypeError(f"punto non valido '{text}': lat tra -90 e 90, lon tra -180 e 180")
    return (lat, lon)


def _metri(text):
    """Parametro numerico >= 0 e finito (un nan spegnerebbe in silenzio raggruppamento o controlli)."""
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"valore non valido '{text}': serve un numero") from None
    if not math.isfinite(v) or v < 0:
        raise argparse.ArgumentTypeError(f"valore non valido '{text}': serve un numero finito >= 0")
    return v


def main(argv=None):
    ap = argparse.ArgumentParser(description="Topologia della citta' (incroci, archi, vicini) da OpenStreetMap.")
    ap.add_argument("--input", help=f"JSON di Overpass (default {DEFAULT_INPUT})")
    ap.add_argument("--download", action="store_true", help="scarica da Overpass (unico caso in cui si usa internet)")
    ap.add_argument("--bbox", help="area s,w,n,e (obbligatoria con --download)")
    ap.add_argument("--save", help="con --download: salva qui i dati grezzi scaricati")
    ap.add_argument("--out", help=f"topology.json da scrivere (default {DEFAULT_OUT}; '-' = non scrivere)")
    ap.add_argument("--verify", metavar="TOPOLOGY", help="solo i controlli su un topology.json esistente")
    ap.add_argument("--mark", type=_latlon, action="append", default=[], metavar="LAT,LON",
                    help="stampa l'incrocio piu' vicino al punto (ripetibile)")
    ap.add_argument("--cluster-m", type=_metri, default=DEFAULTS["cluster_m"])
    ap.add_argument("--max-span-m", type=_metri, default=DEFAULTS["max_span_m"])
    ap.add_argument("--simplify-m", type=_metri, default=DEFAULTS["simplify_m"])
    ap.add_argument("--roundabout-m", type=_metri, default=DEFAULTS["roundabout_m"])
    ap.add_argument("--min-sep-m", type=_metri, default=DEFAULTS["min_sep_m"])
    ap.add_argument("--bypass-ratio", type=_metri, default=DEFAULTS["bypass_ratio"],
                    help="togli gli archi scorciatoia (0 = mai)")
    ap.add_argument("--force", action="store_true", help="scrive anche se un controllo fallisce")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    for flusso in (sys.stdout, sys.stderr):
        try:                                           # console di Windows: un carattere strano non ferma tutto
            flusso.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    log = (lambda *_x: None) if a.quiet else print

    def segna(topo):
        for lat, lon in a.mark:
            nid, d = nearest(topo, lat, lon)
            if nid is None:
                print(f"piu' vicino a ({lat}, {lon}): nessun incrocio con coordinate valide")
            else:
                print(f"piu' vicino a ({lat}, {lon}):\n" + describe(topo, nid, d))

    try:
        bbox = parse_bbox(a.bbox) if a.bbox else None
    except ValueError as e:
        print(f"errore: {e}", file=sys.stderr)
        return 2

    if a.verify:
        try:
            with open(a.verify, encoding="utf-8-sig") as f:
                topo = json.load(f)
        except (OSError, ValueError) as e:
            print(f"errore: non riesco a leggere {a.verify}: {e}", file=sys.stderr)
            return 2
        checks = check_topology(topo, min_sep_m=a.min_sep_m)
        print_report(topo, checks, out=print)
        segna(topo)
        return 0 if all(ok for _n, ok, _i, hard in checks if hard) else 1

    try:
        if a.download:
            if not bbox:
                print("errore: --download richiede --bbox s,w,n,e", file=sys.stderr)
                return 2
            log(f"download da Overpass dell'area {bbox} ...")
            osm = download_osm(bbox, log=log)
            if a.save:
                Path(a.save).parent.mkdir(parents=True, exist_ok=True)
                with open(a.save, "w", encoding="utf-8") as f:
                    json.dump(osm, f, ensure_ascii=False)
                log(f"dati grezzi salvati in {a.save}")
        else:
            src = Path(a.input) if a.input else DEFAULT_INPUT
            if not a.input and bbox is None:
                bbox = DEFAULT_BBOX
            log(f"leggo {src}")
            osm = load_osm(src)
            if bbox is None and not osm.get("bbox"):
                log("attenzione: senza --bbox non taglio i bordi (incroci ai margini forse incompleti)")
        topo = build_topology(osm, bbox=bbox, cluster_m=a.cluster_m, max_span_m=a.max_span_m,
                              simplify_m=a.simplify_m, roundabout_m=a.roundabout_m, min_sep_m=a.min_sep_m,
                              bypass_ratio=a.bypass_ratio, log=log)
    except (OSError, ValueError, RuntimeError, TypeError) as e:
        print(f"errore: {e}", file=sys.stderr)
        return 2

    checks = check_topology(topo, min_sep_m=a.min_sep_m)
    print_report(topo, checks, out=print)
    segna(topo)
    ok = all(ok for _n, ok, _i, hard in checks if hard)
    if a.out == "-":
        return 0 if ok else 1
    dest = Path(a.out) if a.out else DEFAULT_OUT
    if not ok and not a.force:
        print(f"controlli falliti: {dest} NON scritto (usa --force per scriverlo comunque)", file=sys.stderr)
        return 1
    try:
        write_topology(topo, dest)
    except OSError as e:
        print(f"errore: non riesco a scrivere {dest}: {e}", file=sys.stderr)
        return 2
    print(f"scritto {dest} ({dest.stat().st_size / 1024:.0f} KB)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
