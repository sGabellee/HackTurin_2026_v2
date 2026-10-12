#!/usr/bin/env python3
"""CROSS v2 - collettore (server SECONDARIO) e dashboard locale (ARCHITETTURA.md §5).

Gli incroci funzionano da soli, offline e peer-to-peer: il collettore non riceve niente in
automatico e nessun nodo dipende da lui. Quando serve VA A CHIEDERE i dati ai nodi (pull):
    per ogni nodo  GET <url>/api/status  +  GET <url>/api/export?cursor=..  finche' "more"
e li conserva in SQLite (collector.db) con il cursore di ogni nodo, cosi' il pull successivo
scarica solo il nuovo. Opzionale: ascolto PASSIVO della radio SC-P2P v2 (multicast UDP o un
ESP32 radio sulla seriale) per la vista in diretta: riceve hb, hz, vru, emv, adv (e rs) e NON
trasmette mai niente.

Solo libreria standard:
    python collector.py --config collector.json --port 8000 [--host 0.0.0.0] [--pull]
e poi http://127.0.0.1:8000/  (dashboard.html: una pagina, nessuna risorsa esterna).

collector.json (percorsi relativi alla cartella del file):
    {"topology": "../node/python/topology.json",           mappa della citta' (v2, o v1 del server)
     "nodes": {"incrocio-42": "http://192.168.1.50:8100"},  oppure {"id": {"url": "...", "name": "..."}}
     "pull": {"mode": "manual" | "interval", "interval_s": 600, "timeout_s": 4,
              "page_limit": 1000, "max_pages": 200, "workers": 8},
     "listen": {"udp": true, "serial": "", "baud": 115200, "group": "239.255.42.99", "port": 47999, "key": ""},
     "db": "collector.db", "stale_s": 120, "tag_timeout_s": 10, "retention_days": 0}
Se il file della topologia non esiste si prova ../server/topology.json (formato v1, convertito al
volo: archi dritti tra i vicini); se manca anche quello la mappa e' vuota (solo elenco dei nodi).

API (JSON, campi stabili; ts / *_ms = millisecondi epoch dell'orologio del collettore: i tempi dei
nodi sono corretti con lo sfasamento misurato a ogni pull, quelli radio come fa peer.py):

  GET  /api/health      {"ok": true, "ts"}
  GET  /api/overview    {"ts", "title", "counts": {"nodes", "configured", "awake", "asleep", "nodata" (mai
                         arrivato niente), "stale" (dato vecchio), "hazards", "emergencies", "tags"},
                         "energy": {"saved_wh", "saved_pct", "energy_wh", "always_on_wh", "rest_pct", "nodes"},
                         "listen": {"udp", "multicast", "serial", "port", "frames", "by_type", "last_rx_ms",
                                    "errors", "codec": "peer.py" | "minima"},
                         "pull": {"mode", "interval_s", "running", "last_ms", "next_ms"},
                         "topology": {"format", "file", "rev", "nodes", "edges", "source"}}
  GET  /api/map[?geom=0] {"rev", "format", "source", "bbox": [s, w, n, e] | null,
                         "nodes": [{"id", "name", "lat", "lon", "state", "stale", "age_s", "configured",
                                    "in_topology", "tags", "hazard"}],
                         "edges": [{"a", "b", "street", "g": [[lat, lon], ...]}]}   (edges solo con geom=1)
                         state: "sleep" | "awake" | "hazard" | "emergency" | "nodata"
  GET  /api/nodes[?known=1]  (known=1: solo i nodi configurati o da cui e' arrivato qualcosa)
                        {"ts", "nodes": [{"id", "name", "url", "in_topology", "configured", "lat", "lon", "state",
                         "power_state", "stale", "age_s", "source": "radio" | "pull" | "none", "last_seen_ms",
                         "power" (stats() di power.py o null), "crosswalks": [{"id", "state", "light", "safe",
                         "reason", "min_eta_s", "since_ms"}], "users": [{"device", "role", "rssi", "age_s",
                         "crosswalk", "battery"}], "peers": [{"id", ...}], "warnings",
                         "radio": {"pw", "last_ms", "rssi", "via", "summary"} | null, "clock_offset_ms", "status_ms",
                         "pull": {"last_ms", "last_ok_ms", "ok", "items_total", "items_last", "pages",
                                  "errors", "last_error", "last_error_ms", "cursor", "duration_ms",
                                  "running", "more"}}]}
  GET  /api/hazards     {"ts", "items": [{"key", "id", "node", "name", "kind", "category", "severity",
                         "danger", "since_ms", "expires_ms", "heading", "to", "receivers", "src",
                         "path": [id...], "segments": [{"a", "b", "g": [[lat, lon], ...]}]}]}
  GET  /api/users       {"ts", "by_role": {ruolo: n}, "tags": [{"device", "role", "node", "rssi", "age_s",
                         "battery", "heading", "speed", "crosswalk", "next_node", "siren", "src", "advice"}],
                         "advice": [{"node", "x", "to", "roles", "c", "code", "s", "k", "age_s"}],
                         "advice_counts": {code: n}, "seen_24h": {classe: n}}
  GET  /api/crosswalks  {"ts", "items": [{"node", "name", "id", "state", "light", "safe", "reason",
                         "min_eta_s", "since_ms", "age_s", "stale"}],
                         "counts": {"lit_now", "conflicts_24h", "assisted_24h", "crossings_24h", "unsafe_now"}}
  GET  /api/energy      {"ts", "total": {"nodes", "seconds": {"sleep", "active", "cooldown"}, "rest_pct",
                         "wakeups", "by_source": {...}, "energy_wh", "always_on_wh", "saved_wh", "saved_pct"},
                         "nodes": [{"id", "name", "state", "rest_pct", "wakeups", "by_source", "energy_wh",
                                    "saved_wh", "saved_pct", "age_s"}]}
  GET  /api/events?after=<seq>&limit=<n>&node=<id>
                        {"last": seq massimo, "items": [{"seq", "ts", "node", "name", "src", "type", "kind",
                         "category", "severity", "action", "id", "from", "to", "detail"}]}
                        senza after: gli ultimi `limit` (default 100, max 500), in ordine di seq
  GET  /api/pull        {"mode", "interval_s", "running": [id], "last_ms", "next_ms",
                         "results": {id: risultato}, "history": [{"ts", "node", "ok", "items", "pages",
                         "duration_ms", "error"}]}
  POST /api/pull        corpo opzionale {"nodes": [id, ...], "wait": false}
                        -> 202 {"ok", "started", "already", "unknown"}
                        con "wait": true -> 200 {"ok", "results": {id: risultato}, "unknown"}
                        risultato = {"node", "ok", "items", "pages", "more", "error", "duration_ms", "ts"}
  GET  /                dashboard.html
"""
import argparse
import hashlib
import heapq
import hmac
import http.client
import json
import math
import os
import select
import socket
import sqlite3
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

HERE = Path(__file__).resolve().parent
NODE_PY = HERE.parent / "node" / "python"
DASHBOARD = HERE / "dashboard.html"
V1_TOPOLOGY = HERE.parent / "server" / "topology.json"

DEFAULTS = {
    "topology": "../node/python/topology.json",
    "nodes": {},
    "pull": {"mode": "manual", "interval_s": 600, "timeout_s": 4.0, "page_limit": 1000,
             "max_pages": 200, "workers": 8},
    "listen": {"udp": False, "serial": "", "baud": 115200, "group": "239.255.42.99", "port": 47999, "key": ""},
    "db": "collector.db",
    "stale_s": 120,          # oltre: il dato di un nodo non vale piu' come "adesso"
    "tag_timeout_s": 10,     # un Tag sentito via radio resta "in strada" per 10 s
    "live_timeout_s": 30,    # heartbeat radio valido per 30 s (3 x hb_sleep_s)
    "retention_days": 0,     # 0 = tieni tutto
}
LISTEN_TYPES = ("hb", "hz", "vru", "emv", "adv", "rs")
LIGHTS = ["off", "on", "attention", "danger", "assist", "emergency", "unknown"]
ADVICE = ["CLEAR", "WAIT", "CROSS_OK", "KEEP_GOING", "VEHICLE_LEFT", "VEHICLE_RIGHT", "PEDESTRIAN_AHEAD",
          "CYCLIST_AHEAD", "BIKE_LANE_BLOCKED", "EMERGENCY_APPROACHING", "CORRIDOR_CLEAR", "CORRIDOR_BUSY",
          "HAZARD_AHEAD", "ASSIST_UNAVAILABLE", "WRONG_WAY"]
ROLES = ["pedestrian", "blind", "cyclist", "driver", "emergency"]
PW_CODES = {"s": "sleep", "a": "active", "c": "cooldown", "sleep": "sleep", "active": "active",
            "cooldown": "cooldown"}
DANGER_CATEGORIES = ("risk", "violation", "emergency")
SKEW_MS = 5_000           # tempi radio sfasati oltre 5 s: uso l'ora di ricezione (come peer.py)
OFFSET_MIN_MS = 2_000     # sfasamento dell'orologio di un nodo sotto i 2 s: ignorato
MAX_RESPONSE = 64 * 1024 * 1024
MAX_BODY = 64 * 1024
HAZARD_KEEP_MS = 2 * 3600 * 1000
PULL_HISTORY = 2000
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # nodi in LAN: niente proxy


def log(text):
    print(f"[collettore] {text}", flush=True)


def now_ms():
    return int(time.time() * 1000)


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _num(v, default=None):
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(text, default=None):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def dist_m(a, b):
    """Distanza approssimata in metri tra due (lat, lon) vicini."""
    dy = (b[0] - a[0]) * 111320.0
    dx = (b[1] - a[1]) * 111320.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


# ---------------------------------------------------------------------------- configurazione

def load_config(path=None, overrides=None):
    """collector.json + default. Mai eccezioni: file mancante o rotto = default con un avviso."""
    raw = {}
    base = HERE
    if path:
        p = Path(path)
        base = p.resolve().parent
        try:
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("serve un oggetto JSON")
        except FileNotFoundError:
            log(f"{p} non trovato: uso la configurazione di default")
            raw = {}
        except (OSError, ValueError) as e:
            log(f"{p} non valido ({e}): uso la configurazione di default")
            raw = {}
    raw = {**raw, **(overrides or {})}
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in raw.items():
        if isinstance(DEFAULTS.get(k), dict) and k != "nodes":
            cfg[k] = {**DEFAULTS[k], **(v if isinstance(v, dict) else {})}
        else:
            cfg[k] = v
    if isinstance(cfg["listen"].get("serial"), dict):  # {"port": ..., "baud": ...}
        ser = cfg["listen"]["serial"]
        cfg["listen"]["baud"] = ser.get("baud", cfg["listen"]["baud"])
        cfg["listen"]["serial"] = ser.get("port") or ""
    cfg["_base"] = str(base)
    cfg["_nodes"] = _norm_nodes(cfg.get("nodes"))
    return cfg


def _norm_nodes(raw):
    """{"id": "http://..."} | {"id": {"url", "name"}} | [{"id", "url", "name"}] -> {id: {"url", "name"}}."""
    items = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        items = [(n.get("id"), n) for n in raw if isinstance(n, dict)]
    out = {}
    for nid, v in items:
        url = v if isinstance(v, str) else (v.get("url") if isinstance(v, dict) else None)
        name = v.get("name") if isinstance(v, dict) else None
        if not nid or not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
            log(f"nodo {nid!r}: indirizzo {url!r} non valido (serve http://ip:porta): ignorato")
            continue
        out[str(nid)] = {"url": url.strip().rstrip("/"), "name": name}
    return out


def _resolve(base, p):
    if not p:
        return None
    p = Path(str(p))
    return p if p.is_absolute() else Path(base) / p


# ---------------------------------------------------------------------------- topologia

class CityMap:
    """Topologia della citta' (topology.json v2 di build_topology.py, o v1 del server convertita),
    ricaricata da sola quando il file cambia. get() -> dict normalizzato:
    {"rev", "format": "v2" | "v1" | "vuota", "file", "source", "bbox", "nodes": {id: {"name", "lat",
    "lon", "streets"}}, "edges": [{"a", "b", "street", "m", "geom"}], "adj": {id: [(vicino, m)]},
    "by_pair": {(a, b): indice arco}}."""

    def __init__(self, path=None, fallbacks=()):
        self.candidates = [Path(p) for p in ([path] if path else []) + list(fallbacks) if p]
        self._lock = threading.Lock()
        self._data = self._empty()
        self._sig = "mai"
        self._checked = 0.0

    @staticmethod
    def _empty():
        return {"rev": "vuota", "format": "vuota", "file": None, "source": None, "bbox": None,
                "nodes": {}, "edges": [], "adj": {}, "by_pair": {}}

    def get(self):
        with self._lock:
            t = time.time()
            if t - self._checked >= 2.0:
                self._checked = t
                self._maybe_reload()
            return self._data

    def _maybe_reload(self):
        file = next((p for p in self.candidates if p.is_file()), None)
        try:
            sig = (str(file), file.stat().st_mtime, file.stat().st_size) if file else None
        except OSError:
            sig = None
        if sig == self._sig:
            return
        self._sig = sig
        if file is None:
            if self._data["format"] != "vuota" or self._data["file"] is None:
                log("nessuna topologia trovata (" + ", ".join(str(p) for p in self.candidates)
                    + "): mappa vuota, restano l'elenco dei nodi e i dati")
            self._data = self._empty()
            return
        try:
            raw_bytes = file.read_bytes()
            data = self._normalize(json.loads(raw_bytes.decode("utf-8-sig")), file,
                                   hashlib.sha1(raw_bytes).hexdigest()[:10])
        except Exception as e:  # file a meta' scrittura o rovinato: tengo la versione precedente
            log(f"topologia {file} non leggibile ({e!r}): tengo la versione precedente")
            return
        self._data = data
        log(f"topologia {file.name} ({data['format']}): {len(data['nodes'])} incroci, {len(data['edges'])} tratti")

    def _normalize(self, raw, file, rev):
        if not isinstance(raw, dict):
            raise ValueError("serve un oggetto JSON")
        nodes = {}
        for nid, n in (raw.get("nodes") or {}).items():
            if not isinstance(n, dict):
                continue
            lat, lon = _num(n.get("lat")), _num(n.get("lon"))
            if lat is None or lon is None:
                continue  # senza coordinate non si puo' disegnare (es. pos schematica della v1)
            streets = n.get("streets") if isinstance(n.get("streets"), list) else []
            nodes[str(nid)] = {"name": str(n.get("name") or nid), "lat": lat, "lon": lon,
                               "streets": [str(s) for s in streets if s]}
        is_v2 = raw.get("version") == 2 or isinstance(raw.get("edges"), list)
        edges, seen = [], set()

        def add(a, b, street=None, m=None, geom=None):
            if a == b or a not in nodes and b not in nodes:
                return
            key = tuple(sorted((a, b)))
            if key in seen:
                return
            pts = []
            for p in geom if isinstance(geom, list) else []:
                if isinstance(p, (list, tuple)) and len(p) >= 2 and _num(p[0]) is not None and _num(p[1]) is not None:
                    pts.append([float(p[0]), float(p[1])])
            if len(pts) < 2:
                if a not in nodes or b not in nodes:
                    return  # arco verso un incrocio sconosciuto e senza geometria: non disegnabile
                pts = [[nodes[a]["lat"], nodes[a]["lon"]], [nodes[b]["lat"], nodes[b]["lon"]]]
            seen.add(key)
            lung = _num(m)
            if lung is None or lung <= 0:
                lung = sum(dist_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1)) or 100.0
            edges.append({"a": a, "b": b, "street": str(street or ""), "m": round(lung, 1), "geom": pts})

        if is_v2:
            for e in raw.get("edges") or []:
                if isinstance(e, dict) and e.get("a") and e.get("b"):
                    add(str(e["a"]), str(e["b"]), e.get("street"), e.get("m"), e.get("geom"))
            for nid, n in (raw.get("nodes") or {}).items():
                nb = n.get("neighbors") if isinstance(n, dict) else None
                for other, info in (nb.items() if isinstance(nb, dict) else []):
                    info = info if isinstance(info, dict) else {}
                    if str(other) in nodes:
                        add(str(nid), str(other), info.get("street"), info.get("m"))
        else:  # v1: "neighbors": {"sud-ovest": "incrocio-2"}
            for nid, n in (raw.get("nodes") or {}).items():
                nb = n.get("neighbors") if isinstance(n, dict) else None
                for _, other in (nb.items() if isinstance(nb, dict) else []):
                    if other and str(other) in nodes:
                        add(str(nid), str(other), None, None)
        adj, by_pair = {}, {}
        for i, e in enumerate(edges):
            if e["a"] in nodes and e["b"] in nodes:
                adj.setdefault(e["a"], []).append((e["b"], e["m"]))
                adj.setdefault(e["b"], []).append((e["a"], e["m"]))
            by_pair[(e["a"], e["b"])] = i
            by_pair[(e["b"], e["a"])] = i
        bbox = raw.get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4 and all(_num(x) is not None for x in bbox)
                and bbox[0] < bbox[2] and bbox[1] < bbox[3]):
            pts = [(n["lat"], n["lon"]) for n in nodes.values()] + [tuple(p) for e in edges for p in e["geom"]]
            bbox = ([min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)]
                    if pts else None)
        return {"rev": rev, "format": "v2" if is_v2 else "v1", "file": str(file),
                "source": raw.get("source") or ("OpenStreetMap (ODbL)" if is_v2 else "topologia v1 del server"),
                "bbox": [float(x) for x in bbox] if bbox else None, "nodes": nodes, "edges": edges,
                "adj": adj, "by_pair": by_pair}

    @staticmethod
    def path(data, src, dst):
        """Percorso piu' breve in metri (Dijkstra), estremi inclusi; [] se non collegati."""
        if src == dst:
            return [src] if src in data["nodes"] else []
        if src not in data["adj"] or dst not in data["adj"]:
            return []
        best, prev, heap = {src: 0.0}, {}, [(0.0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == dst:
                break
            if d > best.get(u, math.inf):
                continue
            for v, m in data["adj"].get(u, ()):
                nd = d + m
                if nd < best.get(v, math.inf):
                    best[v], prev[v] = nd, u
                    heapq.heappush(heap, (nd, v))
        if dst not in best:
            return []
        out = [dst]
        while out[-1] != src:
            out.append(prev[out[-1]])
        return out[::-1]

    @staticmethod
    def segment(data, a, b):
        """Geometria del tratto a -> b ([[lat, lon], ...] orientata da a verso b) o None."""
        i = data["by_pair"].get((a, b))
        if i is None:
            return None
        e = data["edges"][i]
        g = e["geom"]
        return [list(p) for p in (g if e["a"] == a else g[::-1])]


# ---------------------------------------------------------------------------- archivio SQLite

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, url TEXT, cursor TEXT NOT NULL DEFAULT '',
    last_pull_ms INTEGER, last_ok_ms INTEGER, last_error TEXT, last_error_ms INTEGER,
    errors INTEGER NOT NULL DEFAULT 0, pulls INTEGER NOT NULL DEFAULT 0,
    items INTEGER NOT NULL DEFAULT 0, last_items INTEGER NOT NULL DEFAULT 0, last_pages INTEGER,
    last_duration_ms INTEGER, last_more INTEGER NOT NULL DEFAULT 0,
    clock_offset_ms INTEGER NOT NULL DEFAULT 0, status_json TEXT, status_ms INTEGER);
CREATE TABLE IF NOT EXISTS items (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, node TEXT NOT NULL, tbl TEXT NOT NULL, rid INTEGER NOT NULL,
    ts INTEGER, lts INTEGER, type TEXT, kind TEXT, flag TEXT, rx_ms INTEGER, json TEXT NOT NULL,
    UNIQUE (node, tbl, rid, ts));
CREATE INDEX IF NOT EXISTS items_lts ON items(lts);
CREATE INDEX IF NOT EXISTS items_tbl_kind_lts ON items(tbl, kind, lts);
CREATE INDEX IF NOT EXISTS items_rx ON items(rx_ms);
CREATE TABLE IF NOT EXISTS feed (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, rx_ms INTEGER, node TEXT NOT NULL, src TEXT,
    type TEXT NOT NULL, kind TEXT, category TEXT, severity INTEGER, action TEXT NOT NULL,
    hid TEXT NOT NULL, json TEXT, UNIQUE (node, type, hid, action));
CREATE INDEX IF NOT EXISTS feed_node ON feed(node, seq);
CREATE INDEX IF NOT EXISTS feed_rx ON feed(rx_ms);
CREATE TABLE IF NOT EXISTS pulls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, node TEXT, ok INTEGER, items INTEGER,
    pages INTEGER, duration_ms INTEGER, error TEXT);
"""


class Store:
    """collector.db: nodi (cursore, ultimo pull, errori, ultimo stato), elementi scaricati,
    registro eventi (feed) e storico dei pull. Una connessione con lock; i metodi non sollevano
    eccezioni a runtime (log e valore vuoto)."""

    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._errors = {}
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def _err(self, where, e):
        n = self._errors[where] = self._errors.get(where, 0) + 1
        if n <= 3 or n % 100 == 0:
            log(f"archivio: errore in {where} (x{n}): {e!r}")

    def _db(self, where, fn, default=None):
        with self._lock:
            if self._conn is None:
                return default
            try:
                with self._conn:
                    return fn(self._conn)
            except Exception as e:
                self._err(where, e)
                return default

    def close(self):
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ---------------------------------------------------------------- nodi

    def ensure_node(self, nid, url):
        self._db("ensure_node", lambda c: c.execute(
            "INSERT INTO nodes(id, url) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET url = excluded.url",
            (nid, url)))

    def nodes(self):
        cols = ("id", "url", "cursor", "last_pull_ms", "last_ok_ms", "last_error", "last_error_ms", "errors",
                "pulls", "items", "last_items", "last_pages", "last_duration_ms", "last_more",
                "clock_offset_ms", "status_json", "status_ms")
        rows = self._db("nodes", lambda c: c.execute(f"SELECT {', '.join(cols)} FROM nodes").fetchall(), [])
        return {r[0]: dict(zip(cols, r)) for r in rows}

    def cursor(self, nid):
        row = self._db("cursor", lambda c: c.execute("SELECT cursor FROM nodes WHERE id = ?", (nid,)).fetchone())
        return (row[0] or "") if row else ""

    def set_status(self, nid, status, rx, offset):
        self._db("set_status", lambda c: c.execute(
            "UPDATE nodes SET status_json = ?, status_ms = ?, clock_offset_ms = ? WHERE id = ?",
            (_dumps(status), rx, int(offset), nid)))

    def save_page(self, nid, items, cursor, rx, offset):
        """Salva una pagina di export e il nuovo cursore nella STESSA transazione (un'interruzione
        non perde e non duplica niente). Ritorna la lista degli elementi nuovi (gia' visti = ignorati)."""
        def op(c):
            nuovi = []
            for it in items:
                if not isinstance(it, dict) or not isinstance(it.get("msg"), dict):
                    continue
                tbl, rid = str(it.get("table") or "?"), _int(it.get("rid"))
                if rid is None:
                    continue
                msg = it["msg"]
                ts = _int(it.get("ts"), _int(msg.get("ts"), 0)) or 0   # orologio del nodo (chiave)
                kind = msg.get("kind") if tbl == "events" else (msg.get("class") if tbl == "tracks" else None)
                flag = "crossing" if tbl == "tracks" and msg.get("crosswalk_crossing") else None
                cur = c.execute("INSERT OR IGNORE INTO items(node, tbl, rid, ts, lts, type, kind, flag, rx_ms, json)"
                                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (nid, tbl, rid, ts, ts + offset, str(msg.get("type") or ""), kind, flag, rx,
                                 _dumps(msg)))
                if cur.rowcount > 0:
                    nuovi.append({"table": tbl, "rid": rid, "ts": ts, "lts": ts + offset, "msg": msg})
            c.execute("UPDATE nodes SET cursor = ?, items = items + ? WHERE id = ?", (cursor, len(nuovi), nid))
            return nuovi

        return self._db("save_page", op, None)

    def pull_done(self, nid, res):
        def op(c):
            if res["ok"]:
                c.execute("UPDATE nodes SET last_pull_ms = ?, last_ok_ms = ?, pulls = pulls + 1, last_items = ?,"
                          " last_pages = ?, last_duration_ms = ?, last_more = ? WHERE id = ?",
                          (res["ts"], res["ts"], res["items"], res["pages"], res["duration_ms"],
                           1 if res["more"] else 0, nid))
            else:
                c.execute("UPDATE nodes SET last_pull_ms = ?, pulls = pulls + 1, last_items = ?, last_pages = ?,"
                          " last_duration_ms = ?, last_more = ?, errors = errors + 1, last_error = ?,"
                          " last_error_ms = ? WHERE id = ?",
                          (res["ts"], res["items"], res["pages"], res["duration_ms"], 1 if res["more"] else 0,
                           res["error"], res["ts"], nid))
            c.execute("INSERT INTO pulls(ts, node, ok, items, pages, duration_ms, error) VALUES (?,?,?,?,?,?,?)",
                      (res["ts"], nid, 1 if res["ok"] else 0, res["items"], res["pages"], res["duration_ms"],
                       res["error"]))
            c.execute("DELETE FROM pulls WHERE id <= (SELECT MAX(id) FROM pulls) - ?", (PULL_HISTORY,))

        self._db("pull_done", op)

    def pull_history(self, limit=30):
        rows = self._db("pull_history", lambda c: c.execute(
            "SELECT ts, node, ok, items, pages, duration_ms, error FROM pulls ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall(), [])
        return [{"ts": r[0], "node": r[1], "ok": bool(r[2]), "items": r[3], "pages": r[4], "duration_ms": r[5],
                 "error": r[6]} for r in rows]

    # ---------------------------------------------------------------- registro eventi

    def feed_add(self, e):
        """Voce del registro; duplicati (stesso nodo, tipo, id, azione) ignorati. Ritorna seq o None."""
        def op(c):
            cur = c.execute("INSERT OR IGNORE INTO feed(ts, rx_ms, node, src, type, kind, category, severity,"
                            " action, hid, json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (e["ts"], e["rx_ms"], e["node"], e["src"], e["type"], e.get("kind"), e.get("category"),
                             e.get("severity"), e["action"], e["id"], _dumps(e.get("extra") or {})))
            return cur.lastrowid if cur.rowcount > 0 else None

        return self._db("feed_add", op)

    def feed(self, after=None, limit=100, node=None, since_rx=None):
        cols = "seq, ts, rx_ms, node, src, type, kind, category, severity, action, hid, json"
        where, args = [], []
        if node:
            where.append("node = ?")
            args.append(node)
        if since_rx is not None:
            where.append("rx_ms >= ?")
            args.append(since_rx)
        if after is not None:
            where.append("seq > ?")
            args.append(after)
            sql = f"SELECT {cols} FROM feed{' WHERE ' + ' AND '.join(where) if where else ''} ORDER BY seq LIMIT ?"
        else:
            sql = (f"SELECT * FROM (SELECT {cols} FROM feed{' WHERE ' + ' AND '.join(where) if where else ''}"
                   f" ORDER BY seq DESC LIMIT ?) ORDER BY seq")
        rows = self._db("feed", lambda c: c.execute(sql, args + [limit]).fetchall(), [])
        out = []
        for r in rows:
            extra = _loads(r[11], {}) or {}
            out.append({"seq": r[0], "ts": r[1], "rx_ms": r[2], "node": r[3], "src": r[4], "type": r[5],
                        "kind": r[6], "category": r[7], "severity": r[8], "action": r[9], "id": r[10],
                        "extra": extra})
        return out

    def feed_last(self):
        row = self._db("feed_last", lambda c: c.execute("SELECT MAX(seq) FROM feed").fetchone())
        return (row[0] or 0) if row else 0

    # ---------------------------------------------------------------- conteggi

    def counts_since(self, since_ts):
        """Conteggi delle ultime ore per le sezioni attraversamenti e utenti della strada."""
        def op(c):
            out = {"kinds": {}, "classes": {}, "crossings": 0}
            for kind, n in c.execute("SELECT kind, COUNT(*) FROM items WHERE tbl = 'events' AND type = 'hazard'"
                                     " AND lts >= ? AND json NOT LIKE '%\"action\":\"cancel\"%' GROUP BY kind",
                                     (since_ts,)):
                out["kinds"][kind or "?"] = n
            for cls, n in c.execute("SELECT kind, COUNT(*) FROM items WHERE tbl = 'tracks' AND lts >= ?"
                                    " GROUP BY kind", (since_ts,)):
                out["classes"][cls or "?"] = n
            out["crossings"] = c.execute("SELECT COUNT(*) FROM items WHERE tbl = 'tracks' AND flag = 'crossing'"
                                         " AND lts >= ?", (since_ts,)).fetchone()[0]
            return out

        return self._db("counts", op, {"kinds": {}, "classes": {}, "crossings": 0})

    def latest_power(self):
        """Ultimo blocco "power" trovato nei messaggi status scaricati, per nodo: {id: (ts, power)}."""
        def op(c):
            out = {}
            for node, ts, js in c.execute(
                    "SELECT i.node, i.lts, i.json FROM items i JOIN (SELECT node, MAX(rid) AS rid FROM items"
                    " WHERE tbl = 'status' AND json LIKE '%\"power\"%' GROUP BY node) m"
                    " ON i.node = m.node AND i.rid = m.rid AND i.tbl = 'status'"):
                p = (_loads(js, {}) or {}).get("power")
                if isinstance(p, dict):
                    out[node] = (ts, p)
            return out

        return self._db("latest_power", op, {})

    def cleanup(self, days):
        if not days or days <= 0:
            return 0
        cutoff = now_ms() - int(days * 86400 * 1000)

        def op(c):
            return sum(max(c.execute(f"DELETE FROM {t} WHERE rx_ms < ?", (cutoff,)).rowcount, 0)
                       for t in ("items", "feed"))

        n = self._db("cleanup", op, 0)
        if n:
            log(f"pulizia: cancellate {n} righe piu' vecchie di {days:g} giorni")
        return n


# ---------------------------------------------------------------------------- normalizzazione

def norm_power(p):
    return dict(p) if isinstance(p, dict) and p else None


def norm_crosswalks(x):
    if isinstance(x, dict):
        x = [{"id": k, **v} if isinstance(v, dict) else {"id": k, "state": v} for k, v in x.items()]
    out = []
    for c in x if isinstance(x, list) else []:
        if not isinstance(c, dict):
            continue
        light = c.get("light")
        if isinstance(light, int) and 0 <= light < len(LIGHTS):
            light = LIGHTS[light]
        out.append({"id": c.get("id"), "state": c.get("state"), "light": light, "safe": bool(c.get("safe")),
                    "reason": c.get("reason"), "min_eta_s": _num(c.get("min_eta_s")),
                    "since_ms": _int(c.get("since_ms"))})
    return out


def norm_users(x):
    if isinstance(x, dict):
        x = x.get("near") if isinstance(x.get("near"), list) else (x.get("tags") or [])
    out = []
    for u in x if isinstance(x, list) else []:
        if isinstance(u, dict) and u.get("device"):
            out.append({"device": str(u["device"]), "role": u.get("role"), "rssi": _num(u.get("rssi")),
                        "age_s": _num(u.get("age_s"), 0.0), "crosswalk": u.get("crosswalk"),
                        "battery": _num(u.get("battery"))})
    return out


def norm_peers(x):
    if isinstance(x, dict):
        return [{"id": str(k), **(v if isinstance(v, dict) else {})} for k, v in x.items()]
    if isinstance(x, list):
        return [{"id": str(p)} if not isinstance(p, dict) else p for p in x]
    return []


def power_state(status):
    """Stato di energia dal /api/status di un nodo; un nodo senza gestione dell'energia e' sempre sveglio."""
    if not isinstance(status, dict):
        return None
    p = status.get("power")
    if isinstance(p, dict):
        return PW_CODES.get(str(p.get("state")), "active")
    return "active"


def _sign(obj, key):
    body = {k: v for k, v in obj.items() if k not in ("sig", "h")}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(key.encode("utf-8"), canon, hashlib.sha256).hexdigest()[:16]


# ---------------------------------------------------------------------------- radio (solo ascolto)

def split_frame(data):
    """Riga della radio ESP32 ("R-63 {...}") o datagramma UDP -> (bytes JSON, rssi o None)."""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    data = data.strip()
    rssi = None
    if data[:1] == b"R":
        head, _, rest = data.partition(b" ")
        r = _int(head[1:].decode("ascii", "replace"))
        if r is not None:
            rssi, data = r, rest.strip()
    i = data.find(b"{")
    return (data[i:] if i >= 0 else b""), rssi


ROLE_ALIASES = {"person": "pedestrian", "pedone": "pedestrian", "vehicle": "driver", "car": "driver",
                "conducente": "driver", "ciclista": "cyclist", "bike": "cyclist", "cieco": "blind",
                "non_vedente": "blind", "ambulance": "emergency", "soccorso": "emergency"}


def decode_min(data):
    """Decodifica minima SC-P2P v2 (equivalente a peer.decode): dict con v=2 e t noto, altrimenti None.
    Come peer.decode accetta anche i beacon "vru" v1 dei vecchi braccialetti."""
    try:
        o = json.loads(data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data)
    except Exception:
        return None
    if not isinstance(o, dict):
        return None
    if o.get("v") == 2 and o.get("t") in LISTEN_TYPES:
        return o
    if o.get("v") == 1 and o.get("t") == "vru":
        return {**o, "v": 2, "v1": True}
    return None


def role_min(p):
    """Ruolo del Tag (equivalente a peer.role_of: accetta i profili v1 person, vehicle)."""
    if not isinstance(p, str) or not p:
        return "pedestrian"
    p = p.strip().lower()
    return ROLE_ALIASES.get(p, p)


def expand_id_min(o):
    """Id completo di un hz (equivalente a peer.expand_id): "~cam0-000123" con n = "incrocio-42"
    -> "incrocio-42-cam0-000123" (il nodo accorcia l'id quando il messaggio supera i 240 byte)."""
    mid, n = o.get("id"), o.get("n")
    if o.get("t") == "hz" and isinstance(mid, str) and mid.startswith("~") and isinstance(n, str) and n:
        return f"{n}-{mid[1:]}"
    return mid


class RadioListener:
    """Ascolto passivo della radio: multicast UDP e/o ESP32 radio sulla seriale. Non trasmette MAI
    (nessuna chiamata di invio: il socket serve solo a ricevere, la seriale e' aperta in lettura).
    on_frame(obj, rssi, via) per ogni messaggio v2 valido."""

    def __init__(self, cfg, on_frame):
        self.cfg = cfg
        self.on_frame = on_frame
        self.key = str(cfg.get("key") or "")
        self.group = str(cfg.get("group") or DEFAULTS["listen"]["group"])
        self.port = _int(cfg.get("port"), 47999)
        self._stop = threading.Event()
        self._threads = []
        self._sock = None
        self._ser_close = None
        self.multicast = False
        self.serial_open = False
        self.frames = 0
        self.errors = 0
        self.by_type = {}
        self.last_rx_ms = None
        self._logged = set()
        codec = self._load_peer_codec()
        self._peer_decode, self._peer_verify = codec.get("decode"), codec.get("verify")
        self._expand_id = codec.get("expand_id") or expand_id_min
        self._role_of = codec.get("role_of") or role_min
        self.codec = "peer.py" if self._peer_decode is not None else "minima"

    @staticmethod
    def _load_peer_codec():
        """decode/verify/expand_id/role_of di src/node/python/peer.py (lo stesso codice del nodo), se
        importabile; altrimenti {} e si usano le funzioni minime equivalenti qui sopra."""
        try:
            if str(NODE_PY) not in sys.path:
                sys.path.append(str(NODE_PY))
            import peer  # noqa: E402
            return {k: getattr(peer, k, None) for k in ("decode", "verify", "expand_id", "role_of")
                    if callable(getattr(peer, k, None))}
        except Exception as e:
            log(f"peer.py non importabile ({e!r}): uso la decodifica minima del collettore")
            return {}

    def _log_once(self, key, text):
        if key not in self._logged:
            self._logged.add(key)
            log(text)

    # ---------------------------------------------------------------- avvio

    def start(self):
        if self.cfg.get("udp"):
            self._open_udp()
            if self._sock is not None:
                self._spawn(self._run_udp, "radio-udp")
        port = str(self.cfg.get("serial") or "")
        if port:
            self._spawn(lambda: self._run_serial(port), "radio-seriale")

    def _spawn(self, fn, name):
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def close(self):
        self._stop.set()
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        if self._ser_close:
            try:
                self._ser_close()
            except Exception:
                pass
        for t in self._threads:
            t.join(timeout=1.5)

    def state(self):
        return {"udp": self._sock is not None, "multicast": self.multicast, "serial": self.serial_open,
                "port": self.port if self.cfg.get("udp") else None, "frames": self.frames,
                "by_type": dict(self.by_type), "last_rx_ms": self.last_rx_ms, "errors": self.errors,
                "codec": self.codec}

    # ---------------------------------------------------------------- decodifica

    def handle(self, data, via, rssi=None):
        """Un datagramma / una riga: decodifica, firma, callback. Ritorna l'oggetto o None."""
        line, r = split_frame(data)
        rssi = r if r is not None else rssi
        if not line:
            return None
        o = None
        if self._peer_decode is not None:
            try:
                o = self._peer_decode(line)
            except Exception:
                o = None
        if not (isinstance(o, dict) and o.get("v") == 2 and o.get("t") in LISTEN_TYPES):
            o = decode_min(line)
        if o is None:
            return None
        # con la chiave: i messaggi dei nodi devono essere firmati; i Tag non firmano, ma se un
        # beacon e' firmato la firma deve essere giusta (come peer.py)
        if self.key and (o["t"] not in ("vru", "emv") or "sig" in o) and not self._verify(o):
            self.errors += 1
            self._log_once("sig:" + str(o.get("n") or o.get("d")),
                           f"firma mancante o errata da {o.get('n') or o.get('d')!r}: scartato")
            return None
        try:  # dopo la firma (calcolata sull'id corto): id completo dell'hz, ruolo v2 del Tag
            if o["t"] == "hz" and isinstance(o.get("id"), str) and o["id"].startswith("~"):
                o = dict(o, id=self._expand_id(o))
            elif o["t"] == "vru":
                o = dict(o, p=self._role_of(o.get("p")))
        except Exception as e:
            self._log_once("norm", f"normalizzazione non riuscita ({e!r}): uso il messaggio com'e'")
        self.frames += 1
        self.by_type[o["t"]] = self.by_type.get(o["t"], 0) + 1
        self.last_rx_ms = now_ms()
        try:
            self.on_frame(o, rssi, via)
        except Exception as e:
            self.errors += 1
            self._log_once("cb", f"messaggio radio non gestito ({e!r}): {line[:80]!r}")
        return o

    def _verify(self, o):
        if self._peer_verify is not None:
            try:
                return bool(self._peer_verify(o, self.key))
            except Exception:
                pass
        sig = o.get("sig")
        return isinstance(sig, str) and hmac.compare_digest(sig, _sign(o, self.key))

    # ---------------------------------------------------------------- UDP

    def _open_udp(self):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            s.bind(("", self.port))
            s.setblocking(False)
            self._sock = s
        except OSError as e:
            if s is not None:
                s.close()
            log(f"ascolto UDP sulla porta {self.port} non disponibile ({e}): niente vista radio via UDP")
            return
        self._join()
        log(f"ascolto radio passivo su UDP {self.group}:{self.port}" + ("" if self.multicast else " (solo unicast)"))

    def _join(self):
        try:
            mreq = struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton("0.0.0.0"))
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self.multicast = True
        except OSError as e:
            self._log_once("join", f"multicast {self.group} non disponibile ({e}): riprovo ogni 30 s")

    def _run_udp(self):
        last_join = time.time()
        while not self._stop.is_set():
            s = self._sock
            if s is None:
                return
            try:
                ready, _, _ = select.select([s], [], [], 0.5)
            except (OSError, ValueError):
                return  # socket chiuso
            if ready:
                for _ in range(500):
                    try:
                        data, addr = s.recvfrom(4096)
                    except (BlockingIOError, InterruptedError):
                        break
                    except ConnectionResetError:
                        continue
                    except OSError:
                        return
                    self.handle(data, "udp")
            if not self.multicast and time.time() - last_join > 30 and self._sock is not None:
                last_join = time.time()
                self._join()

    # ---------------------------------------------------------------- seriale (ESP32 radio)

    def _run_serial(self, port):
        baud = _int(self.cfg.get("baud"), 115200)
        while not self._stop.is_set():
            try:
                read, close = _open_serial_reader(port, baud)
            except Exception as e:
                self._log_once("ser_open", f"radio seriale {port} non apribile ({e}): riprovo ogni 10 s")
                self._stop.wait(10)
                continue
            self._logged.discard("ser_open")
            self._ser_close, self.serial_open = close, True
            log(f"ascolto radio passivo sulla seriale {port} a {baud} baud")
            buf = b""
            try:
                while not self._stop.is_set():
                    chunk = read()
                    if chunk is None:
                        raise OSError("radio scollegata")
                    if not chunk:
                        continue
                    *lines, buf = (buf + chunk).split(b"\n")
                    if len(buf) > 4096:
                        buf = b""
                    for line in lines:
                        if line.strip() and not line.lstrip().startswith(b"#"):  # "#..." = stato della radio
                            self.handle(line, "serial")
            except Exception as e:
                if not self._stop.is_set():
                    log(f"radio seriale {port}: {e}: riprovo tra 10 s")
            finally:
                self.serial_open = False
                try:
                    close()
                except Exception:
                    pass
            self._stop.wait(10)


def _open_serial_reader(port, baud):
    """Apre la seriale SOLO in lettura. Ritorna (read() -> bytes | b"" a vuoto | None se chiusa, close()).
    pyserial se installato; altrimenti termios (Linux/macOS) o API di Windows via ctypes."""
    try:
        import serial  # pyserial, facoltativo
        s = serial.Serial(port, baud, timeout=0.5)
        return (lambda: s.read(512)), s.close
    except ImportError:
        pass
    if os.name == "posix":
        import termios
        import tty
        fd = os.open(port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            speed = getattr(termios, f"B{baud}", termios.B115200)
            attrs[2] |= termios.CLOCAL | termios.CREAD
            attrs[4] = attrs[5] = speed
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            os.close(fd)
            raise

        def read():
            r, _, _ = select.select([fd], [], [], 0.5)
            if not r:
                return b""
            try:
                data = os.read(fd, 4096)
            except BlockingIOError:
                return b""
            return data or None

        return read, (lambda: os.close(fd))
    # Windows: CreateFile su \\.\COMx, velocita' con BuildCommDCB/SetCommState, lettura con timeout
    import ctypes
    import msvcrt
    from ctypes import wintypes
    name = port if port.startswith("\\\\.\\") else "\\\\.\\" + port
    f = open(name, "rb", buffering=0)
    try:
        k32 = ctypes.windll.kernel32
        h = msvcrt.get_osfhandle(f.fileno())
        dcb = (ctypes.c_ubyte * 28)()
        struct.pack_into("<I", dcb, 0, 28)
        if not k32.GetCommState(h, dcb) or not k32.BuildCommDCBW(f"baud={baud} parity=N data=8 stop=1", dcb) \
                or not k32.SetCommState(h, dcb):
            raise OSError(f"impostazione della porta {port} non riuscita")

        class COMMTIMEOUTS(ctypes.Structure):
            _fields_ = [(n, wintypes.DWORD) for n in ("ReadIntervalTimeout", "ReadTotalTimeoutMultiplier",
                                                       "ReadTotalTimeoutConstant", "WriteTotalTimeoutMultiplier",
                                                       "WriteTotalTimeoutConstant")]
        # ritorna subito con i byte disponibili, oppure aspetta al massimo 500 ms il primo byte
        k32.SetCommTimeouts(h, ctypes.byref(COMMTIMEOUTS(0xFFFFFFFF, 0xFFFFFFFF, 500, 0, 0)))
    except Exception:
        f.close()
        raise
    return (lambda: f.read(4096) or b""), f.close


# ---------------------------------------------------------------------------- pull

class PullError(Exception):
    pass


def http_json(url, timeout):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "CROSS-collettore/2"})
    with _OPENER.open(req, timeout=timeout) as r:
        body = r.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise PullError("risposta troppo grande")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise PullError(f"risposta non JSON da {url}: {e}") from None


def _why(e):
    """Errore di rete -> frase breve in italiano."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code} {e.reason}"
    if isinstance(e, urllib.error.URLError):
        e = e.reason if isinstance(e.reason, BaseException) else e
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "nessuna risposta (timeout)"
    if isinstance(e, ConnectionRefusedError):
        return "connessione rifiutata (API del nodo spenta?)"
    if isinstance(e, OSError) and getattr(e, "strerror", None):
        return f"irraggiungibile: {e.strerror}"
    return str(e) or e.__class__.__name__


# ---------------------------------------------------------------------------- collettore

class Collector:
    def __init__(self, cfg, start=False):
        self.cfg = cfg if "_nodes" in cfg else load_config(None, cfg)
        base = self.cfg["_base"]
        self.base = Path(base)
        self.title = str(self.cfg.get("title") or "CROSS")
        self.targets = dict(self.cfg["_nodes"])  # id -> {"url", "name"}
        pc = self.cfg["pull"]
        self.mode = "interval" if str(pc.get("mode")) == "interval" else "manual"
        self.interval_s = max(10.0, _num(pc.get("interval_s"), 600.0))
        self.timeout_s = min(max(_num(pc.get("timeout_s"), 4.0), 0.3), 60.0)
        self.page_limit = min(max(_int(pc.get("page_limit"), 1000) or 1000, 1), 2000)
        self.max_pages = max(1, _int(pc.get("max_pages"), 200) or 200)
        self.stale_s = max(5.0, _num(self.cfg.get("stale_s"), 120.0))
        self.tag_timeout_s = max(1.0, _num(self.cfg.get("tag_timeout_s"), 10.0))
        self.live_timeout_s = max(2.0, _num(self.cfg.get("live_timeout_s"), 30.0))
        topo = _resolve(base, self.cfg.get("topology"))
        self.city = CityMap(topo, fallbacks=[V1_TOPOLOGY])
        db = self.cfg.get("db") or "collector.db"
        self.store = Store(db if db == ":memory:" else _resolve(base, db))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=max(1, min(_int(pc.get("workers"), 8) or 8, 32)),
                                        thread_name_prefix="pull")
        self._running = {}           # id -> future
        self._results = {}           # id -> ultimo risultato
        self._last_pull_ms = None
        self._next_pull_ms = None
        self._threads = []
        # stato in memoria (diretta radio + ultimo stato scaricato)
        self.info = {}               # id -> {"status", "status_ms", "offset"}
        self.radio_nodes = {}        # id -> {"pw", "last_ms", "rssi", "via", "summary"}
        self.tags = {}               # device -> {...}
        self.advice = {}             # (nodo, x, destinatario) -> {...}
        self.advice_log = deque(maxlen=5000)  # (ms, codice) a ogni cambio di consiglio
        self.hazards = {}            # (origine, id) -> {...}
        self.pulled_power = {}       # id -> (ts, power) dagli status scaricati
        for nid, t in self.targets.items():
            self.store.ensure_node(nid, t["url"])
        self._load_state()
        self.radio = RadioListener(self.cfg["listen"], self.on_radio)
        if start:
            self.start()

    # ---------------------------------------------------------------- ciclo di vita

    def start(self, listen=True, interval=True):
        if listen:
            self.radio.start()
        if interval and self.mode == "interval":
            self._spawn(self._interval_loop, "pull-intervallo")
        if (_num(self.cfg.get("retention_days"), 0) or 0) > 0:
            self._spawn(self._cleanup_loop, "pulizia")

    def _spawn(self, fn, name):
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def close(self):
        self._stop.set()
        self.radio.close()
        self._pool.shutdown(wait=False, cancel_futures=True)
        for t in self._threads:
            t.join(timeout=1.0)
        self.store.close()

    def _interval_loop(self):
        self._next_pull_ms = now_ms() + 2000
        log(f"pull automatico ogni {self.interval_s:g} s")
        while not self._stop.wait(0.5):
            if now_ms() >= (self._next_pull_ms or 0):
                self._next_pull_ms = now_ms() + int(self.interval_s * 1000)
                self.pull()

    def _cleanup_loop(self):
        while not self._stop.wait(3600):
            self.store.cleanup(_num(self.cfg.get("retention_days"), 0))

    def _load_state(self):
        """Riparte dall'archivio: ultimo stato scaricato di ogni nodo e pericoli delle ultime 2 ore."""
        for nid, row in self.store.nodes().items():
            st = _loads(row.get("status_json"))
            if isinstance(st, dict):
                self.info[nid] = {"status": st, "status_ms": row.get("status_ms"),
                                  "offset": row.get("clock_offset_ms") or 0}
            else:
                self.info.setdefault(nid, {"status": None, "status_ms": None, "offset": row.get("clock_offset_ms") or 0})
        self.pulled_power = self.store.latest_power()
        for e in self.store.feed(since_rx=now_ms() - HAZARD_KEEP_MS, limit=5000):
            hz = (e.get("extra") or {}).get("hz")
            if isinstance(hz, dict):
                self._hazard_update(hz)

    # ---------------------------------------------------------------- pull

    def pull(self, nodes=None, wait=False, timeout=None):
        """Scarica dai nodi indicati (default tutti), in parallelo. Un nodo gia' in corso non riparte."""
        if nodes is None:
            ids = list(self.targets)
        else:
            ids = [nodes] if isinstance(nodes, str) else [str(n) for n in nodes if n]
        unknown = [n for n in ids if n not in self.targets]
        started, already, futs = [], [], {}
        with self._lock:
            for nid in ids:
                if nid not in self.targets:
                    continue
                f = self._running.get(nid)
                if f is not None and not f.done():
                    already.append(nid)
                    futs[nid] = f
                    continue
                try:
                    f = self._pool.submit(self._pull_node, nid, self.targets[nid]["url"])
                except RuntimeError:  # collettore in chiusura
                    continue
                self._running[nid] = futs[nid] = f
                started.append(nid)
            if started:
                self._last_pull_ms = now_ms()
        if not wait:
            return {"ok": True, "started": started, "already": already, "unknown": unknown}
        limit = timeout if timeout is not None else self.timeout_s * (self.max_pages + 2) + 5
        futures_wait(list(futs.values()), timeout=limit)
        results = {}
        for nid, f in futs.items():
            if f.done() and not f.cancelled() and f.exception() is None:
                results[nid] = f.result()
            else:
                results[nid] = {"node": nid, "ok": False, "items": 0, "pages": 0, "more": True,
                                "error": "pull ancora in corso", "duration_ms": None, "ts": now_ms()}
        return {"ok": all(r["ok"] for r in results.values()) and not unknown, "results": results,
                "unknown": unknown}

    def _pull_node(self, nid, url):
        t0 = time.time()
        res = {"node": nid, "ok": False, "items": 0, "pages": 0, "more": False, "error": None,
               "duration_ms": None, "ts": now_ms()}
        try:
            self._pull_status(nid, url)
            cursor = self.store.cursor(nid)
            for _ in range(self.max_pages):
                if self._stop.is_set():
                    raise PullError("collettore in chiusura")
                q = urlencode({"cursor": cursor, "limit": self.page_limit})
                page = http_json(f"{url}/api/export?{q}", self.timeout_s)
                if not isinstance(page, dict) or not isinstance(page.get("items"), list) \
                        or not isinstance(page.get("cursor"), str):
                    raise PullError("risposta di /api/export non valida")
                if page.get("node") not in (None, nid):
                    raise PullError(f"a {url} risponde il nodo {page.get('node')!r}, non {nid!r}: controlla collector.json")
                if page.get("error"):
                    raise PullError(f"il nodo dice: {page['error']}")
                offset = (self.info.get(nid) or {}).get("offset") or 0
                rx = now_ms()
                nuovi = self.store.save_page(nid, page["items"], page["cursor"], rx, offset)
                if nuovi is None:
                    raise PullError("archivio del collettore non scrivibile")
                res["pages"] += 1
                res["items"] += len(nuovi)
                self._ingest(nid, nuovi, rx, offset)
                more = bool(page.get("more"))
                if not more or (page["cursor"] == cursor and not page["items"]):
                    res["more"] = False
                    break
                cursor = page["cursor"]
            else:
                res["more"] = True  # troppe pagine per un pull: il resto al prossimo
            res["ok"] = True
        except PullError as e:
            res["error"] = str(e)
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            res["error"] = _why(e)
        except Exception as e:  # mai far cadere il collettore per un nodo
            res["error"] = f"errore inatteso: {e!r}"
        res["duration_ms"] = int((time.time() - t0) * 1000)
        self.store.pull_done(nid, res)
        with self._lock:
            self._results[nid] = res
        if res["ok"]:
            log(f"pull {nid}: {res['items']} elementi nuovi in {res['pages']} pagine ({res['duration_ms']} ms)"
                + (", ne restano altri" if res["more"] else ""))
        else:
            log(f"pull {nid} non riuscito: {res['error']}")
        return res

    def _pull_status(self, nid, url):
        t_a = time.time()
        st = http_json(f"{url}/api/status", self.timeout_s)  # nodo irraggiungibile: eccezione qui
        t_b = time.time()
        if not isinstance(st, dict):
            raise PullError("risposta di /api/status non valida")
        rx = int((t_a + t_b) / 2 * 1000)
        node_ts = _int(st.get("ts"))
        offset = rx - node_ts if node_ts else 0
        if abs(offset) < OFFSET_MIN_MS:
            offset = 0
        elif nid not in self.info or abs(offset - ((self.info.get(nid) or {}).get("offset") or 0)) > 60_000:
            log(f"orologio di {nid} sfasato di {offset / 1000:+.1f} s: correggo i suoi tempi")
        with self._lock:
            self.info[nid] = {"status": st, "status_ms": rx, "offset": offset}
            if isinstance(st.get("power"), dict):
                self.pulled_power[nid] = (rx, st["power"])
        self.store.set_status(nid, st, rx, offset)

    def _ingest(self, nid, items, rx, offset):
        """Elementi nuovi di un pull -> registro eventi, pericoli in corso, energia."""
        for it in items:
            msg, tbl = it["msg"], it["table"]
            try:
                if tbl == "events":
                    self._event_from_pull(nid, it, msg, rx, offset)
                elif tbl == "status" and isinstance(msg.get("power"), dict):
                    with self._lock:
                        old = self.pulled_power.get(nid)
                        if old is None or (old[0] or 0) <= it["lts"]:
                            self.pulled_power[nid] = (it["lts"], msg["power"])
            except Exception as e:
                log(f"elemento {tbl}:{it.get('rid')} di {nid} non interpretato: {e!r}")

    def _event_from_pull(self, nid, it, msg, rx, offset):
        tipo = str(msg.get("type") or "event")
        hid = str(msg.get("id") or f"{it['table']}:{it['rid']}")
        action = "cancel" if msg.get("action") == "cancel" else "new"
        ts = (_int(msg.get("ts"), it["ts"]) or 0) + offset   # ora del collettore
        exp = _int(msg.get("expires"))
        exp = exp + offset if exp else None
        to = msg.get("to") or msg.get("next_nodes") or []
        to = [str(x) for x in to] if isinstance(to, list) else []
        heading = _num(msg.get("heading"), _num((msg.get("actor") or {}).get("heading_deg")))
        hz = None
        if tipo == "hazard":
            origin = str(msg.get("node") or nid)
            hz = {"origin": origin, "id": hid, "action": action, "kind": msg.get("kind"),
                  "category": msg.get("category"), "severity": _int(msg.get("severity"), 1), "ts": ts,
                  "expires": exp, "to": to, "heading": heading, "src": "pull", "receiver": None}
            node = origin
        elif tipo == "remote_hazard":
            origin = str(msg.get("from_node") or "?")
            hz = {"origin": origin, "id": hid, "action": action, "kind": msg.get("kind"),
                  "category": msg.get("category"), "severity": _int(msg.get("severity"), 1), "ts": ts,
                  "expires": exp, "to": to, "heading": heading, "src": "pull", "receiver": nid}
            node = nid
        else:
            node = nid
        detail = {k: msg[k] for k in ("reason", "crosswalk", "cause_txt", "camera", "via", "from", "class")
                  if k in msg and isinstance(msg[k], (str, int, float))}
        if isinstance(msg.get("details"), dict):
            detail.update({k: v for k, v in msg["details"].items() if isinstance(v, (str, int, float, bool))})
        extra = {"hz": hz, "to": to, "from": msg.get("from_node"), "detail": detail}
        self.store.feed_add({"ts": ts, "rx_ms": rx, "node": node, "src": "pull", "type": tipo,
                             "kind": msg.get("kind"), "category": msg.get("category"),
                             "severity": _int(msg.get("severity")), "action": action, "id": hid, "extra": extra})
        if hz is not None:
            self._hazard_update(hz)

    # ---------------------------------------------------------------- diretta radio

    def on_radio(self, o, rssi, via):
        t = o["t"]
        now = now_ms()
        if t == "hb":
            n = o.get("n")
            if isinstance(n, str) and n:
                with self._lock:
                    r = self.radio_nodes.setdefault(n, {})
                    r.update({"pw": PW_CODES.get(str(o.get("pw")), "active"), "last_ms": now, "rssi": rssi,
                              "via": via})
        elif t == "rs":
            n, p = o.get("n"), o.get("p") if isinstance(o.get("p"), dict) else {}
            if isinstance(n, str) and n:
                with self._lock:
                    r = self.radio_nodes.setdefault(n, {})
                    r.update({"last_ms": now, "rssi": rssi, "via": via, "summary": p})
                    if p.get("pw") is not None:
                        r["pw"] = PW_CODES.get(str(p.get("pw")), "active")
        elif t == "hz":
            self._hz(o, now, via)
        elif t in ("vru", "emv"):
            d = o.get("d")
            if isinstance(d, str) and d:
                with self._lock:
                    tag = self.tags.setdefault(d, {"device": d})
                    tag.update({"role": "emergency" if t == "emv" else (o.get("p") or tag.get("role")),
                                "last_ms": now, "rssi": rssi, "via": via, "seq": _int(o.get("q")),
                                "battery": _num(o.get("b"), tag.get("battery")), "heading": _num(o.get("hd")),
                                "speed": _num(o.get("sp")), "kind": t})
                    if t == "emv":
                        tag["next_node"] = o.get("nx")
                        tag["siren"] = o.get("st") == "run"
        elif t == "adv":
            self._adv(o, now)

    def _hz(self, o, now, via):
        mid, n = o.get("id"), o.get("n")
        if not isinstance(mid, str) or not isinstance(n, str) or o.get("a") not in ("n", "c"):
            return
        ts = _int(o.get("ts"), now)
        exp = _int(o.get("e"), ts + 30_000)
        if abs(now - ts) > SKEW_MS:  # orologio del nodo sfasato (offline senza NTP): riporto sul mio
            ts, exp = now, now + max(0, exp - ts)
        to = [str(x) for x in o.get("to")] if isinstance(o.get("to"), list) else []
        hz = {"origin": n, "id": mid, "action": "cancel" if o["a"] == "c" else "new", "kind": o.get("k"),
              "category": o.get("c"), "severity": _int(o.get("s"), 1), "ts": ts, "expires": exp, "to": to,
              "heading": _num(o.get("hd")), "src": "radio", "receiver": None}
        self._hazard_update(hz)
        self.store.feed_add({"ts": ts, "rx_ms": now, "node": n, "src": "radio", "type": "hazard",
                             "kind": o.get("k"), "category": o.get("c"), "severity": _int(o.get("s")),
                             "action": hz["action"], "id": mid,
                             "extra": {"hz": hz, "to": to, "from": None, "detail": {"via": via}}})

    def _adv(self, o, now):
        n = o.get("n")
        c = _int(o.get("c"))
        if not isinstance(n, str) or c is None:
            return
        to = o.get("to") if isinstance(o.get("to"), str) else None
        roles = [str(r) for r in o.get("r")] if isinstance(o.get("r"), list) else []
        key = (n, _int(o.get("x")), to or ",".join(sorted(roles)))
        rec = {"node": n, "x": _int(o.get("x")), "to": to, "roles": roles, "c": c,
               "code": ADVICE[c] if 0 <= c < len(ADVICE) else str(c), "s": _int(o.get("s"), 1),
               "k": o.get("k"), "ttl": _int(o.get("ttl"), 3000), "last_ms": now}
        with self._lock:
            old = self.advice.get(key)
            if old is None or old["c"] != c or now - old["last_ms"] > 10_000:
                self.advice_log.append((now, rec["code"]))
            self.advice[key] = rec
            if to:  # avviso per un Tag preciso: il Tag e' vicino a quel nodo
                tag = self.tags.setdefault(to, {"device": to, "role": None, "last_ms": now})
                tag["node"], tag["node_ms"] = n, now
                tag["advice"] = {"c": c, "code": rec["code"], "s": rec["s"]}

    # ---------------------------------------------------------------- pericoli

    def _hazard_update(self, hz):
        key = (hz["origin"], hz["id"])
        with self._lock:
            h = self.hazards.get(key)
            if h is None:
                h = self.hazards[key] = {"origin": hz["origin"], "id": hz["id"], "kind": hz.get("kind"),
                                         "category": hz.get("category"), "severity": hz.get("severity") or 1,
                                         "since": hz.get("ts"), "expires": None, "to": [], "heading": None,
                                         "receivers": [], "src": hz.get("src"), "ended": None, "local": False}
            if hz.get("receiver"):
                if hz["receiver"] not in h["receivers"]:
                    h["receivers"].append(hz["receiver"])
            else:
                h["local"] = True  # visto dall'origine (pull dell'origine o radio): dati di prima mano
            if hz["action"] == "cancel":
                h["ended"] = hz.get("ts") or now_ms()
                return
            if hz.get("receiver") and h["local"] and h["expires"] is not None:
                return  # copia ricevuta da un altro nodo: non cambia i dati dell'origine
            for k in ("kind", "category"):
                if hz.get(k):
                    h[k] = hz[k]
            h["severity"] = max(h["severity"] or 1, hz.get("severity") or 1)
            if hz.get("ts") and (h["since"] is None or hz["ts"] < h["since"]):
                h["since"] = hz["ts"]
            exp = hz.get("expires") or ((hz.get("ts") or now_ms()) + 60_000)
            h["expires"] = max(h["expires"] or 0, exp)
            if hz.get("to"):
                h["to"] = list(hz["to"])
            if hz.get("heading") is not None:
                h["heading"] = hz["heading"]
            if hz.get("src") == "radio":
                h["src"] = "radio"
            # un nuovo inizio dopo una fine (stesso id) riapre il pericolo
            if h["ended"] is not None and (hz.get("ts") or 0) > h["ended"]:
                h["ended"] = None

    def active_hazards(self, now=None):
        now = now or now_ms()
        with self._lock:
            for k in [k for k, h in self.hazards.items()
                      if (h["ended"] or h["expires"] or 0) < now - HAZARD_KEEP_MS]:
                self.hazards.pop(k, None)
            return [dict(h, to=list(h["to"]), receivers=list(h["receivers"])) for h in self.hazards.values()
                    if h["ended"] is None and (h["expires"] or 0) > now]

    @staticmethod
    def is_danger(h):
        return h.get("category") in DANGER_CATEGORIES or (h.get("severity") or 1) >= 2

    def hazard_route(self, h, city):
        """Percorso del pericolo sulla mappa: origine -> destinatari "to" (nell'ordine) lungo le strade,
        poi i nodi che l'hanno ricevuto. Ritorna (nodi, tratti [{"a", "b", "g"}])."""
        origin = h["origin"]
        if origin not in city["nodes"]:
            return [], []
        nodes, segs, seen = [origin], [], set()

        def walk(a, b):
            p = CityMap.path(city, a, b)
            for x, y in zip(p, p[1:]):
                if (x, y) in seen or len(segs) >= 80:
                    continue
                seen.add((x, y))
                g = CityMap.segment(city, x, y)
                if g:
                    segs.append({"a": x, "b": y, "g": [[round(q[0], 6), round(q[1], 6)] for q in g]})
                if y not in nodes:
                    nodes.append(y)
            return bool(p)

        cur = origin
        for target in h["to"]:
            if target != cur and target in city["nodes"] and walk(cur, target):
                cur = target
        for r in h["receivers"]:
            if r not in nodes and r in city["nodes"]:
                walk(origin, r)
        return nodes, segs

    # ---------------------------------------------------------------- viste per la dashboard

    def _node_view(self, nid, city, now, hazards_by_node=None, emergency_nodes=()):
        """Stato completo di un nodo da tutte le fonti (radio, ultimo pull)."""
        with self._lock:
            inf = dict(self.info.get(nid) or {})
            radio = dict(self.radio_nodes.get(nid) or {}) or None
        st = inf.get("status") if isinstance(inf.get("status"), dict) else None
        cands = []
        if radio and radio.get("last_ms") and radio.get("pw"):
            cands.append((radio["last_ms"], radio["pw"], "radio"))
        if st is not None and inf.get("status_ms"):
            cands.append((inf["status_ms"], power_state(st), "pull"))
        last_ms, pstate, source = max(cands) if cands else (None, None, "none")
        age = round((now - last_ms) / 1000, 1) if last_ms else None
        radio_fresh = bool(radio and radio.get("last_ms") and now - radio["last_ms"] <= self.live_timeout_s * 1000)
        stale = last_ms is None or (age > self.stale_s and not radio_fresh)
        hz = (hazards_by_node or {}).get(nid) or []
        if nid in emergency_nodes or any(h.get("category") == "emergency" for h in hz):
            state = "emergency"
        elif any(self.is_danger(h) for h in hz):
            state = "hazard"
        elif pstate is None:
            state = "nodata"
        else:
            state = "sleep" if pstate == "sleep" else "awake"
        tn = city["nodes"].get(nid) or {}
        lat, lon = tn.get("lat"), tn.get("lon")
        cfg_pos = ((st or {}).get("config") or {}).get("position") if st else None
        if lat is None and isinstance(cfg_pos, dict):
            lat, lon = _num(cfg_pos.get("lat")), _num(cfg_pos.get("lon"))
        target = self.targets.get(nid) or {}
        name = tn.get("name") or target.get("name") or ((st or {}).get("config") or {}).get("name") or nid
        power = norm_power((st or {}).get("power"))
        pp = self.pulled_power.get(nid)
        if pp and (power is None or (pp[0] or 0) > (inf.get("status_ms") or 0)):
            power = norm_power(pp[1])
        peers = norm_peers((st or {}).get("peers"))
        if not peers and isinstance((st or {}).get("p2p"), dict):
            peers = norm_peers(st["p2p"].get("peers"))
        warnings = ((st or {}).get("status") or {}).get("warnings") if isinstance((st or {}).get("status"), dict) else []
        return {"id": nid, "name": name, "url": target.get("url"), "in_topology": nid in city["nodes"],
                "configured": nid in self.targets, "lat": lat, "lon": lon, "state": state, "power_state": pstate,
                "stale": stale, "age_s": age, "source": source, "last_seen_ms": last_ms, "power": power,
                "crosswalks": norm_crosswalks((st or {}).get("crosswalks")),
                "users": norm_users((st or {}).get("users")), "peers": peers,
                "warnings": warnings if isinstance(warnings, list) else [],
                "radio": ({"pw": radio.get("pw"), "last_ms": radio.get("last_ms"), "rssi": radio.get("rssi"),
                           "via": radio.get("via"), "summary": radio.get("summary")} if radio else None),
                "clock_offset_ms": inf.get("offset") or 0, "status_ms": inf.get("status_ms")}

    def _known_ids(self, city):
        """Nodi della topologia + configurati + sentiti via radio o scaricati."""
        with self._lock:
            ids = set(self.targets) | set(self.radio_nodes) | {k for k, v in self.info.items() if v.get("status")}
            ids |= {h["origin"] for h in self.hazards.values() if h["origin"] and h["origin"] != "?"}
        return list(city["nodes"]) + sorted(ids - set(city["nodes"]))

    def _context(self, now):
        city = self.city.get()
        hz = self.active_hazards(now)
        by_node, emergency_nodes = {}, set()
        for h in hz:
            by_node.setdefault(h["origin"], []).append(h)
            if h.get("category") == "emergency":
                emergency_nodes.add(h["origin"])
                emergency_nodes.update(n for n in h["to"] if n)
        return city, hz, by_node, emergency_nodes

    def all_nodes(self, now=None):
        now = now or now_ms()
        city, _, by_node, emer = self._context(now)
        return [self._node_view(n, city, now, by_node, emer) for n in self._known_ids(city)]

    def tags_now(self, now=None):
        now = now or now_ms()
        out = {}
        with self._lock:
            for d, t in list(self.tags.items()):
                if now - (t.get("last_ms") or 0) > max(self.tag_timeout_s, 60) * 1000 * 10:
                    self.tags.pop(d, None)  # dimenticato dopo molto tempo
                    continue
                if now - (t.get("last_ms") or 0) > self.tag_timeout_s * 1000:
                    continue
                node = t.get("node") if now - (t.get("node_ms") or 0) <= self.tag_timeout_s * 1000 else None
                out[d] = {"device": d, "role": t.get("role"), "node": node, "rssi": t.get("rssi"),
                          "age_s": round((now - t["last_ms"]) / 1000, 1), "battery": t.get("battery"),
                          "heading": t.get("heading"), "speed": t.get("speed"), "crosswalk": None,
                          "next_node": t.get("next_node"), "siren": t.get("siren"), "src": "radio",
                          "advice": t.get("advice")}
            infos = {k: dict(v) for k, v in self.info.items()}
        for nid, inf in infos.items():
            st, ms = inf.get("status"), inf.get("status_ms")
            if not isinstance(st, dict) or not ms or now - ms > self.stale_s * 1000:
                continue
            for u in norm_users(st.get("users")):
                age = round((now - ms) / 1000 + (u.get("age_s") or 0), 1)
                cur = out.get(u["device"])
                if cur is None or (cur.get("node") is None and cur["src"] == "radio"):
                    base = cur or {"device": u["device"], "heading": None, "speed": None, "next_node": None,
                                   "siren": None, "advice": None, "src": "pull", "age_s": age}
                    base.update({"role": cur.get("role") if cur and cur.get("role") else u.get("role"),
                                 "node": nid, "rssi": u.get("rssi") if u.get("rssi") is not None else base.get("rssi"),
                                 "crosswalk": u.get("crosswalk"),
                                 "battery": base.get("battery") if base.get("battery") is not None else u.get("battery")})
                    out[u["device"]] = base
        return list(out.values())

    # ---------------------------------------------------------------- risposte API

    def api_overview(self):
        now = now_ms()
        nodes = self.all_nodes(now)
        hz = self.active_hazards(now)
        tags = self.tags_now(now)
        city = self.city.get()
        fresh = [n for n in nodes if not n["stale"] and n["power_state"]]
        en = self.api_energy(now, nodes)["total"]
        with self._lock:
            running = sorted(k for k, f in self._running.items() if not f.done())
        return {"ts": now, "title": self.title,
                "counts": {"nodes": len(nodes), "configured": len(self.targets),
                           "awake": sum(1 for n in fresh if n["power_state"] != "sleep"),
                           "asleep": sum(1 for n in fresh if n["power_state"] == "sleep"),
                           "nodata": sum(1 for n in nodes if n["last_seen_ms"] is None),
                           "stale": sum(1 for n in nodes if n["stale"] and n["last_seen_ms"] is not None),
                           "hazards": sum(1 for h in hz if self.is_danger(h)),
                           "emergencies": sum(1 for h in hz if h.get("category") == "emergency"),
                           "tags": len(tags)},
                "energy": {k: en[k] for k in ("saved_wh", "saved_pct", "energy_wh", "always_on_wh", "rest_pct",
                                              "nodes")},
                "listen": self.radio.state(),
                "pull": {"mode": self.mode, "interval_s": self.interval_s, "running": running,
                         "last_ms": self._last_pull_ms, "next_ms": self._next_pull_ms},
                "topology": {"format": city["format"], "file": city["file"], "rev": city["rev"],
                             "nodes": len(city["nodes"]), "edges": len(city["edges"]), "source": city["source"]}}

    def api_map(self, geom=True):
        now = now_ms()
        city, hz, by_node, emer = self._context(now)
        tags = self.tags_now(now)
        tag_count = {}
        for t in tags:
            if t.get("node"):
                tag_count[t["node"]] = tag_count.get(t["node"], 0) + 1
        out_nodes = []
        for nid in self._known_ids(city):
            v = self._node_view(nid, city, now, by_node, emer)
            if v["lat"] is None or v["lon"] is None:
                continue
            top = max(by_node.get(nid) or [{}], key=lambda h: h.get("severity") or 0)
            out_nodes.append({"id": nid, "name": v["name"], "lat": round(v["lat"], 6), "lon": round(v["lon"], 6),
                              "state": v["state"], "stale": v["stale"], "age_s": v["age_s"],
                              "configured": v["configured"], "in_topology": v["in_topology"],
                              "tags": tag_count.get(nid, 0), "hazard": top.get("kind")})
        res = {"rev": city["rev"], "format": city["format"], "source": city["source"], "bbox": city["bbox"],
               "nodes": out_nodes}
        if geom:
            res["edges"] = [{"a": e["a"], "b": e["b"], "street": e["street"],
                             "g": [[round(p[0], 6), round(p[1], 6)] for p in e["geom"]]} for e in city["edges"]]
        return res

    def api_nodes(self, known=False):
        """Tutti i nodi (topologia + configurati + sentiti); known=True solo quelli configurati o da cui
        e' arrivato qualcosa (la dashboard lo chiede ogni 2 s: con centinaia di incroci pesa meno)."""
        now = now_ms()
        nodes = self.all_nodes(now)
        if known:
            nodes = [n for n in nodes if n["configured"] or n["source"] != "none"]
        db = self.store.nodes()
        with self._lock:
            running = {k for k, f in self._running.items() if not f.done()}
        for n in nodes:
            r = db.get(n["id"])
            n["pull"] = None if r is None else {
                "last_ms": r["last_pull_ms"], "last_ok_ms": r["last_ok_ms"],
                "ok": None if r["last_pull_ms"] is None else (r["last_ok_ms"] == r["last_pull_ms"]),
                "items_total": r["items"], "items_last": r["last_items"], "pages": r["last_pages"],
                "errors": r["errors"], "last_error": r["last_error"], "last_error_ms": r["last_error_ms"],
                "cursor": r["cursor"], "duration_ms": r["last_duration_ms"], "running": n["id"] in running,
                "more": bool(r["last_more"])}
        return {"ts": now, "nodes": nodes}

    def api_hazards(self):
        now = now_ms()
        city = self.city.get()
        items = []
        for h in sorted(self.active_hazards(now), key=lambda h: (-(h.get("severity") or 1), -(h.get("since") or 0))):
            path, segs = self.hazard_route(h, city)
            items.append({"key": f"{h['origin']}|{h['id']}", "id": h["id"], "node": h["origin"],
                          "name": (city["nodes"].get(h["origin"]) or {}).get("name") or h["origin"],
                          "kind": h.get("kind"), "category": h.get("category"), "severity": h.get("severity"),
                          "danger": self.is_danger(h), "since_ms": h.get("since"), "expires_ms": h.get("expires"),
                          "heading": h.get("heading"), "to": h["to"], "receivers": h["receivers"],
                          "src": h.get("src"), "path": path, "segments": segs})
        return {"ts": now, "items": items}

    def api_users(self):
        now = now_ms()
        tags = self.tags_now(now)
        by_role = {r: 0 for r in ROLES}
        for t in tags:
            r = t.get("role") if t.get("role") in by_role else None
            if r:
                by_role[r] += 1
            else:
                by_role["sconosciuto"] = by_role.get("sconosciuto", 0) + 1
        with self._lock:
            adv = [dict(a) for a in self.advice.values() if now - a["last_ms"] <= max(a["ttl"] or 3000, 3000) + 7000]
            counts = {}
            for ms, code in self.advice_log:
                if now - ms <= 3600_000:
                    counts[code] = counts.get(code, 0) + 1
            for k in [k for k, a in self.advice.items() if now - a["last_ms"] > 600_000]:
                self.advice.pop(k, None)
        for a in adv:
            a["age_s"] = round((now - a.pop("last_ms")) / 1000, 1)
        adv.sort(key=lambda a: (-(a.get("s") or 0), a["age_s"]))
        seen = self.store.counts_since(now - 86400_000)["classes"]
        return {"ts": now, "by_role": by_role, "tags": sorted(tags, key=lambda t: (t.get("node") or "~", t["device"])),
                "advice": adv[:100], "advice_counts": counts, "seen_24h": seen}

    def api_crosswalks(self):
        now = now_ms()
        items = []
        for n in self.all_nodes(now):
            for c in n["crosswalks"]:
                items.append({"node": n["id"], "name": n["name"], **c, "age_s": n["age_s"],
                              "stale": n["age_s"] is None or n["age_s"] > self.stale_s})
        order = ["emergency", "conflict", "attention", "assist", "unknown", "occupied", "idle"]
        items.sort(key=lambda c: (c["stale"], order.index(c["state"]) if c["state"] in order else 9, c["node"]))
        cnt = self.store.counts_since(now - 86400_000)
        fresh = [c for c in items if not c["stale"]]
        return {"ts": now, "items": items,
                "counts": {"lit_now": sum(1 for c in fresh if c["light"] not in (None, "off")),
                           "unsafe_now": sum(1 for c in fresh if c["state"] in ("conflict", "attention", "emergency")),
                           "conflicts_24h": cnt["kinds"].get("crossing_conflict", 0),
                           "assisted_24h": cnt["kinds"].get("assisted_crossing", 0),
                           "crossings_24h": cnt["crossings"]}}

    def api_energy(self, now=None, nodes=None):
        now = now or now_ms()
        nodes = nodes if nodes is not None else self.all_nodes(now)
        tot = {"nodes": 0, "seconds": {"sleep": 0.0, "active": 0.0, "cooldown": 0.0}, "wakeups": 0,
               "by_source": {}, "energy_wh": 0.0, "always_on_wh": 0.0, "saved_wh": 0.0}
        rows = []
        for n in nodes:
            p = n.get("power")
            if not p:
                continue
            sec = p.get("seconds") if isinstance(p.get("seconds"), dict) else {}
            s_tot = sum(_num(v, 0.0) for v in sec.values())
            rest = round(100.0 * _num(sec.get("sleep"), 0.0) / s_tot, 1) if s_tot > 0 else None
            tot["nodes"] += 1
            for k in tot["seconds"]:
                tot["seconds"][k] += _num(sec.get(k), 0.0)
            tot["wakeups"] += _int(p.get("wakeups"), 0) or 0
            for src, v in (p.get("by_source") or {}).items():
                tot["by_source"][src] = tot["by_source"].get(src, 0) + (_int(v, 0) or 0)
            for k in ("energy_wh", "always_on_wh", "saved_wh"):
                tot[k] += _num(p.get(k), 0.0)
            rows.append({"id": n["id"], "name": n["name"], "state": n["power_state"], "rest_pct": rest,
                         "wakeups": _int(p.get("wakeups"), 0), "by_source": p.get("by_source") or {},
                         "energy_wh": _num(p.get("energy_wh")), "saved_wh": _num(p.get("saved_wh")),
                         "saved_pct": _num(p.get("saved_pct")), "age_s": n["age_s"]})
        s_all = sum(tot["seconds"].values())
        tot["rest_pct"] = round(100.0 * tot["seconds"]["sleep"] / s_all, 1) if s_all > 0 else None
        tot["saved_pct"] = (round(100.0 * tot["saved_wh"] / tot["always_on_wh"], 1)
                            if tot["always_on_wh"] > 0 else None)
        for k in ("energy_wh", "always_on_wh", "saved_wh"):
            tot[k] = round(tot[k], 3)
        tot["seconds"] = {k: round(v, 1) for k, v in tot["seconds"].items()}
        rows.sort(key=lambda r: -(r["saved_wh"] or 0))
        return {"ts": now, "total": tot, "nodes": rows}

    def api_events(self, after=None, limit=100, node=None):
        city = self.city.get()
        limit = min(max(limit or 100, 1), 500)
        items = []
        for e in self.store.feed(after=after, limit=limit, node=node):
            ex = e.pop("extra") or {}
            e.pop("rx_ms", None)
            e.update({"name": (city["nodes"].get(e["node"]) or {}).get("name") or e["node"],
                      "from": ex.get("from"), "to": ex.get("to") or [], "detail": ex.get("detail") or {}})
            items.append(e)
        return {"last": self.store.feed_last(), "items": items}

    def api_pull_state(self):
        with self._lock:
            running = sorted(k for k, f in self._running.items() if not f.done())
            results = {k: dict(v) for k, v in self._results.items()}
        return {"mode": self.mode, "interval_s": self.interval_s, "running": running,
                "last_ms": self._last_pull_ms, "next_ms": self._next_pull_ms, "results": results,
                "history": self.store.pull_history(30)}

    # ---------------------------------------------------------------- HTTP

    def handle(self, method, path, query="", body=b""):
        """(codice, oggetto JSON | bytes, content-type). Mai eccezioni."""
        path = path.rstrip("/") or "/"
        try:
            q = {k: v[0] for k, v in parse_qs(query or "").items() if v and v[0].strip()}
            if method == "POST":
                if path != "/api/pull":
                    return 405, {"error": "solo POST /api/pull"}, None
                data = {}
                if body and body.strip():
                    data = _loads(body.decode("utf-8", "replace"))
                    if not isinstance(data, dict):
                        return 400, {"error": "corpo non valido: serve un oggetto JSON"}, None
                nodes = data.get("nodes")
                if nodes is not None and not isinstance(nodes, (list, str)):
                    return 400, {"error": "'nodes' deve essere una lista di id"}, None
                res = self.pull(nodes, wait=bool(data.get("wait")))
                return (200 if data.get("wait") else 202), res, None
            if method != "GET":
                return 405, {"error": "metodo non ammesso"}, None
            if path in ("/", "/index.html", "/dashboard", "/dashboard.html"):
                try:
                    return 200, DASHBOARD.read_bytes(), "text/html; charset=utf-8"
                except OSError:
                    return 404, b"dashboard.html mancante accanto a collector.py", "text/plain; charset=utf-8"
            if path == "/favicon.ico":
                return 204, b"", "image/x-icon"
            if path == "/api/health":
                return 200, {"ok": True, "ts": now_ms()}, None
            if path == "/api/overview":
                return 200, self.api_overview(), None
            if path == "/api/map":
                return 200, self.api_map(geom=q.get("geom", "1") not in ("0", "false", "no")), None
            if path == "/api/nodes":
                return 200, self.api_nodes(known=q.get("known", "0") not in ("0", "false", "no")), None
            if path == "/api/hazards":
                return 200, self.api_hazards(), None
            if path == "/api/users":
                return 200, self.api_users(), None
            if path == "/api/crosswalks":
                return 200, self.api_crosswalks(), None
            if path == "/api/energy":
                return 200, self.api_energy(), None
            if path == "/api/events":
                after = _int(q.get("after")) if "after" in q else None
                if "after" in q and after is None:
                    return 400, {"error": "parametro 'after' non valido (serve un numero)"}, None
                return 200, self.api_events(after, _int(q.get("limit"), 100), q.get("node")), None
            if path == "/api/pull":
                return 200, self.api_pull_state(), None
            return 404, {"error": "risorsa non trovata", "path": path}, None
        except Exception as e:  # mai far cadere il server per una richiesta
            log(f"errore su {method} {path}: {e!r}")
            return 500, {"error": "errore interno", "detail": repr(e)}, None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"


class _Handler(BaseHTTPRequestHandler):
    server_version = "CROSSCollettore/2"
    timeout = 15

    def do_GET(self):
        self._go("GET")

    def do_HEAD(self):
        self._go("GET", head=True)

    def do_POST(self):
        self._go("POST")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _go(self, method, head=False):
        parts = urlsplit(self.path)
        body = b""
        if method == "POST":
            n = _int(self.headers.get("Content-Length"), 0) or 0
            if n > MAX_BODY:
                return self._send(413, _dumps({"error": "corpo troppo grande"}).encode(), "application/json")
            body = self.rfile.read(n) if n > 0 else b""
        code, payload, ctype = self.server.collector.handle(method, parts.path, parts.query, body)
        if not isinstance(payload, (bytes, bytearray)):
            payload = _dumps(payload).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        self._send(code, payload, ctype, head)

    def _send(self, code, body, ctype, head=False):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head and body:
                self.wfile.write(body)
        except (ConnectionError, OSError):
            pass

    def log_message(self, fmt, *args):
        pass


def serve(collector, host="0.0.0.0", port=8000):
    """Avvia il server HTTP in un thread. Ritorna il server (porta reale in server_address[1])."""
    srv = _Server((host, port), _Handler)
    srv.collector = collector
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, name="http", daemon=True).start()
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description="CROSS v2 - collettore (pull dai nodi) e dashboard locale")
    ap.add_argument("--config", default=str(HERE / "collector.json"))
    ap.add_argument("--host", default="0.0.0.0", help="127.0.0.1 = solo da questo computer")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", help="archivio SQLite (sostituisce 'db' del config)")
    ap.add_argument("--pull", action="store_true", help="scarica subito da tutti i nodi all'avvio")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.db:
        cfg["db"] = str(Path(args.db).resolve())
    col = Collector(cfg)
    try:
        srv = serve(col, args.host, args.port)
    except OSError as e:
        log(f"non riesco ad aprire {args.host}:{args.port} ({e})")
        col.close()
        return 1
    col.start()
    log(f"{len(col.targets)} nodi configurati, pull {col.mode}"
        + (f" ogni {col.interval_s:g} s" if col.mode == "interval" else " (pulsante 'Scarica ora' o POST /api/pull)"))
    log(f"dashboard: http://127.0.0.1:{srv.server_address[1]}/")
    if args.pull:
        col.pull()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("chiusura")
    finally:
        srv.shutdown()
        srv.server_close()
        col.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
