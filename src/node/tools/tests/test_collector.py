"""Test del collettore (src/collector/collector.py) e della dashboard (dashboard.html), ARCHITETTURA.md par. 5.

Prova: pull da due nodi finti (due LocalAPI vere con archivio in memoria) e da un nodo irraggiungibile,
in parallelo e con timeout breve; cursori incrementali, pagine, nessun duplicato, sfasamento
dell'orologio di un nodo; tutte le API della dashboard (/api/overview, /api/map, /api/nodes,
/api/hazards, /api/users, /api/crosswalks, /api/energy, /api/events, GET e POST /api/pull) anche via
HTTP; pull a intervallo; ripartenza dall'archivio; topologia v1 del server convertita al volo, vuota
e rovinata; ascolto radio passivo (UDP in loopback: hz con id corto, vru v2 e v1, emv, hb, adv; firma
con la chiave; nessuna trasmissione); dashboard: sintassi del JavaScript con node, regole di stile
(una colonna, angoli vivi, nessuna risorsa esterna) e una prova completa dello script in node con un
DOM finto contro le API vere del collettore (nomi dei campi coerenti, nessun errore di disegno).

Uso:  python test_collector.py      (dalla cartella node/tools/tests; esce con codice 1 se qualcosa fallisce)
"""
import contextlib
import inspect
import io
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

QUI = Path(__file__).resolve().parent
SRC = QUI.parents[2]
sys.path.insert(0, str(QUI.parents[1] / "python"))
sys.path.insert(0, str(SRC / "collector"))

import collector as C  # noqa: E402
import peer  # noqa: E402
from api import LocalAPI  # noqa: E402
from storage import Storage  # noqa: E402

RISULTATI = []
CTX = {}
DASHBOARD = SRC / "collector" / "dashboard.html"
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
        RISULTATI.append((fn.__name__, False, f"eccezione {e!r}\n{traceback.format_exc(limit=5)}"))
    for nome, ok, info in RISULTATI[prima:]:
        print(f"[{'OK ' if ok else 'ERR'}] {nome}" + ("" if ok else f"  -> {str(info)[:600]}"))


def now_ms():
    return int(time.time() * 1000)


def porta_libera(tipo=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, tipo)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def aspetta(cond, secondi=4.0):
    fine = time.time() + secondi
    while time.time() < fine:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


# ---------------------------------------------------------------------- nodi finti

class NodoFinto:
    """Quello che LocalAPI legge da un nodo: id, archivio, status_snapshot()."""

    def __init__(self, node_id, snapshot):
        self.node_id = node_id
        self.storage = Storage(":memory:")
        self.cfg = {"node_id": node_id}
        self.started = time.time()
        self.latest_scene = []
        self.snapshot = snapshot

    def status_snapshot(self):
        return dict(self.snapshot)


class APISfasata(LocalAPI):
    """API di un nodo con l'orologio 60 s indietro (offline senza NTP)."""

    def _status(self):
        s = super()._status()
        s["ts"] -= 60_000
        return s


TOPO = {
    "version": 2, "source": "OpenStreetMap (ODbL)", "bbox": [45.069, 7.659, 45.073, 7.665],
    "nodes": {
        "n1": {"name": "Via Uno / Corso Due", "lat": 45.0700, "lon": 7.6600, "streets": ["Via Uno"],
               "neighbors": {"n2": {"bearing": 90, "street": "Via Uno", "m": 157}}},
        "n2": {"name": "Via Uno / Via Tre", "lat": 45.0700, "lon": 7.6620, "streets": ["Via Uno", "Via Tre"],
               "neighbors": {"n1": {"bearing": 270, "street": "Via Uno", "m": 157},
                             "n3": {"bearing": 90, "street": "Via Uno", "m": 157},
                             "n4": {"bearing": 0, "street": "Via Tre", "m": 222}}},
        "n3": {"name": "Via Uno / Via Quattro", "lat": 45.0700, "lon": 7.6640, "streets": ["Via Uno"], "neighbors": {}},
        "n4": {"name": "Via Tre / Via Cinque", "lat": 45.0720, "lon": 7.6620, "streets": ["Via Tre"], "neighbors": {}},
    },
    "edges": [
        {"a": "n1", "b": "n2", "street": "Via Uno", "m": 157, "geom": [[45.0700, 7.6600], [45.07005, 7.6610], [45.0700, 7.6620]]},
        {"a": "n2", "b": "n3", "street": "Via Uno", "m": 157, "geom": [[45.0700, 7.6620], [45.0700, 7.6640]]},
        {"a": "n2", "b": "n4", "street": "Via Tre", "m": 222, "geom": [[45.0700, 7.6620], [45.0720, 7.6620]]},
    ],
}


def prepara():
    """Due nodi veri (LocalAPI + Storage) con dati, uno irraggiungibile, topologia e collettore."""
    tmp = Path(tempfile.mkdtemp(prefix="cross-collettore-"))
    (tmp / "topology.json").write_text(json.dumps(TOPO), encoding="utf-8")
    t = now_ms()
    p1 = {"state": "sleep", "since_ms": t - 30_000, "wakeups": 3, "by_source": {"curb": 2, "radio_vru": 1},
          "seconds": {"sleep": 900.0, "active": 80.0, "cooldown": 20.0}, "energy_wh": 0.875,
          "always_on_wh": 2.5, "saved_wh": 1.625, "saved_pct": 65.0, "fault": False}
    p2 = {"state": "active", "since_ms": t - 5_000, "wakeups": 1, "by_source": {"curb": 1},
          "seconds": {"sleep": 100.0, "active": 100.0, "cooldown": 0.0}, "energy_wh": 0.32,
          "always_on_wh": 0.5, "saved_wh": 0.18, "saved_pct": 36.0, "fault": False}
    n1 = NodoFinto("n1", {"node": "n1", "power": p1,
                          "crosswalks": [{"id": 0, "state": "assist", "light": 4, "safe": True, "reason": "non vedente",
                                          "min_eta_s": None, "since_ms": t - 2000}],
                          "users": [{"device": "tag-1", "role": "blind", "rssi": -60, "age_s": 1.0, "crosswalk": 0}],
                          "peers": {"n2": {"last_seen": t, "pw": "a", "rssi": -70, "via": "udp"}}})
    n2 = NodoFinto("n2", {"node": "n2", "power": p2,
                          "crosswalks": [{"id": 0, "state": "conflict", "light": 3, "safe": False, "reason": "veicolo",
                                          "min_eta_s": 2.5, "since_ms": t - 1000}],
                          "users": [], "peers": {}})
    s1, s2 = n1.storage, n2.storage
    s1.save({"type": "hazard", "id": "n1-cam0-000007", "node": "n1", "kind": "speeding", "category": "risk",
             "severity": 2, "action": "new", "ts": t - 5000, "expires": t + 600_000, "to": ["n2"]})
    for i in range(3):
        s1.save({"type": "track", "id": f"tr{i}", "class": "person", "ts": t - 4000 + i, "crosswalk_crossing": True})
    s1.save({"type": "track", "id": "tr9", "class": "vehicle", "ts": t - 3000})
    s1.save({"type": "status", "ts": t - 2000, "power": p1})
    s1.save({"type": "stats", "ts": t - 1500, "cameras": {}})
    tn2 = t - 60_000  # orologio di n2
    s2.save({"type": "remote_hazard", "id": "n1-cam0-000007", "from_node": "n1", "kind": "speeding",
             "category": "risk", "severity": 2, "action": "new", "ts": tn2 - 4000, "expires": tn2 + 600_000})
    s2.save({"type": "hazard", "id": "n2-x-1", "node": "n2", "kind": "crossing_conflict", "category": "risk",
             "severity": 2, "action": "new", "ts": tn2 - 3000, "expires": tn2 - 1000, "reason": "auto veloce"})
    s2.save({"type": "hazard", "id": "n2-x-2", "node": "n2", "kind": "assisted_crossing", "category": "info",
             "severity": 1, "action": "new", "ts": tn2 - 2000, "expires": tn2 - 500})
    s2.save({"type": "status", "ts": tn2 - 1000, "power": p2})
    a1 = LocalAPI({"host": "127.0.0.1", "port": 0}, n1)
    a2 = APISfasata({"host": "127.0.0.1", "port": 0}, n2)
    a1.start()
    a2.start()
    morto = porta_libera()
    cfg = {"title": "CROSS Prova", "topology": str(tmp / "topology.json"), "db": str(tmp / "collector.db"),
           "nodes": {"n1": f"http://127.0.0.1:{a1.port}", "n2": {"url": f"http://127.0.0.1:{a2.port}", "name": "Due"},
                     "n3": f"http://127.0.0.1:{morto}", "rotto": "ftp://x"},
           "pull": {"mode": "manual", "timeout_s": 1.5, "page_limit": 3},
           "listen": {"udp": True, "port": porta_libera(socket.SOCK_DGRAM), "serial": ""}}
    col = C.Collector(cfg)
    CTX.update(tmp=tmp, n1=n1, n2=n2, a1=a1, a2=a2, cfg=cfg, col=col, t=t)
    return col


def h(col, path, method="GET", body=b""):
    path, _, query = path.partition("?")
    code, obj, ctype = col.handle(method, path, query, body)
    return code, obj


# ---------------------------------------------------------------------- pull

def test_pull():
    col = prepara()
    check("config: nodo con indirizzo non valido scartato", set(col.targets) == {"n1", "n2", "n3"}, col.targets)
    t0 = time.time()
    r = col.pull(wait=True)
    dt = time.time() - t0
    res = r["results"]
    check("pull: n1 riuscito con tutti gli elementi", res["n1"]["ok"] and res["n1"]["items"] == 7, res["n1"])
    check("pull: n1 in 3 pagine da 3 (page_limit)", res["n1"]["pages"] == 3, res["n1"])
    check("pull: n2 riuscito", res["n2"]["ok"] and res["n2"]["items"] == 4 and res["n2"]["pages"] == 2, res["n2"])
    check("pull: n3 irraggiungibile -> errore in italiano", not res["n3"]["ok"] and res["n3"]["error"], res["n3"])
    check("pull: esito complessivo False per il nodo irraggiungibile", r["ok"] is False)
    check("pull: in parallelo, l'irraggiungibile non blocca gli altri (timeout breve)",
          dt < 6 and res["n1"]["duration_ms"] < 1500, (dt, res["n1"]["duration_ms"], res["n3"]["duration_ms"]))
    nodi = col.store.nodes()
    check("archivio: cursore salvato per i nodi riusciti, vuoto per l'irraggiungibile",
          nodi["n1"]["cursor"].startswith("d") and nodi["n2"]["cursor"] and nodi["n3"]["cursor"] == "", nodi["n3"])
    check("archivio: ultimo pull, errori ed elementi per nodo",
          nodi["n1"]["items"] == 7 and nodi["n3"]["errors"] == 1 and nodi["n3"]["last_error"]
          and nodi["n1"]["last_ok_ms"] == nodi["n1"]["last_pull_ms"])
    off = col.info["n2"]["offset"]
    check("orologio di n2 indietro di 60 s: sfasamento misurato", 58_000 <= off <= 62_000, off)
    check("orologio di n1 giusto: sfasamento 0", col.info["n1"]["offset"] == 0, col.info["n1"]["offset"])
    r = col.pull(wait=True, nodes=["n1", "n2"])
    check("secondo pull: nessun elemento nuovo (cursori incrementali)",
          r["results"]["n1"]["items"] == 0 and r["results"]["n2"]["items"] == 0 and r["results"]["n1"]["pages"] == 1,
          r["results"])
    CTX["n1"].storage.save({"type": "track", "id": "nuovo1", "class": "cyclist", "ts": now_ms()})
    CTX["n1"].storage.save({"type": "status", "ts": now_ms(), "power": {"state": "sleep", "wakeups": 3}})
    r = col.pull(wait=True, nodes="n1")
    check("pull di un solo nodo: solo i 2 elementi nuovi", r["results"]["n1"]["items"] == 2 and "n2" not in r["results"],
          r["results"])
    col.store._db("reset", lambda c: c.execute("UPDATE nodes SET cursor = '' WHERE id = 'n1'"))
    r = col.pull(wait=True, nodes=["n1"])
    check("cursore perso: si riscarica tutto ma senza duplicati", r["results"]["n1"]["ok"]
          and r["results"]["n1"]["items"] == 0 and col.store.nodes()["n1"]["cursor"], r["results"]["n1"])
    n = col.store._db("conta", lambda c: c.execute("SELECT COUNT(*) FROM items WHERE node='n1'").fetchone()[0])
    check("archivio: 9 elementi di n1, una volta sola", n == 9, n)
    r = col.pull(wait=True, nodes=["n1", "sconosciuto"])
    check("pull con un nodo sconosciuto: segnalato in unknown", r["unknown"] == ["sconosciuto"], r)


# ---------------------------------------------------------------------- API della dashboard

def test_api_dashboard():
    col = CTX["col"]
    code, o = h(col, "/api/overview")
    c = o["counts"]
    check("/api/overview: struttura", code == 200 and set(o) >= {"ts", "title", "counts", "energy", "listen", "pull",
                                                                  "topology"} and o["title"] == "CROSS Prova", o.keys())
    check("/api/overview: conteggi (4 incroci, 3 configurati, 1 sveglio, 1 a riposo, 2 senza dati)",
          (c["nodes"], c["configured"], c["awake"], c["asleep"], c["nodata"]) == (4, 3, 1, 1, 2), c)
    check("/api/overview: pericoli in corso e Tag in strada", c["hazards"] == 1 and c["tags"] == 1, c)
    check("/api/overview: energia risparmiata sommata", abs(o["energy"]["saved_wh"] - 1.805) < 1e-6
          and o["energy"]["nodes"] == 2, o["energy"])
    check("/api/overview: topologia v2 caricata", o["topology"]["format"] == "v2" and o["topology"]["nodes"] == 4
          and o["topology"]["edges"] == 3, o["topology"])
    code, m = h(col, "/api/map")
    st = {n["id"]: n["state"] for n in m["nodes"]}
    check("/api/map: nodi con lat/lon/nome/stato", code == 200 and len(m["nodes"]) == 4 and all(
        {"id", "name", "lat", "lon", "state", "stale", "tags", "hazard"} <= set(n) for n in m["nodes"]), m["nodes"])
    check("/api/map: stati (pericolo, sveglio, nessun dato)",
          st == {"n1": "hazard", "n2": "awake", "n3": "nodata", "n4": "nodata"}, st)
    check("/api/map: archi con geometria", len(m["edges"]) == 3 and m["edges"][0]["g"][1] == [45.07005, 7.661], m["edges"])
    check("/api/map: Tag vicini per incrocio", next(n for n in m["nodes"] if n["id"] == "n1")["tags"] == 1)
    code, m0 = h(col, "/api/map?geom=0")
    check("/api/map?geom=0: senza archi, stessa revisione", "edges" not in m0 and m0["rev"] == m["rev"])
    code, nd = h(col, "/api/nodes")
    by = {n["id"]: n for n in nd["nodes"]}
    check("/api/nodes: raccolta dati per nodo", by["n1"]["pull"]["ok"] is True and by["n1"]["pull"]["items_total"] == 9
          and by["n3"]["pull"]["ok"] is False and by["n3"]["pull"]["errors"] >= 1 and by["n3"]["pull"]["last_error"]
          and by["n4"]["pull"] is None, by["n3"]["pull"])
    check("/api/nodes: energia, attraversamenti, Tag, peer dallo status", by["n1"]["power"]["wakeups"] == 3
          and by["n1"]["crosswalks"][0]["light"] == "assist" and by["n1"]["users"][0]["device"] == "tag-1"
          and by["n1"]["peers"][0]["id"] == "n2" and by["n2"]["name"] == "Via Uno / Via Tre", by["n1"])
    check("/api/nodes: configurato e sorgente", by["n1"]["configured"] and not by["n4"]["configured"]
          and by["n1"]["source"] == "pull" and by["n4"]["source"] == "none")
    code, kn = h(col, "/api/nodes?known=1")
    check("/api/nodes?known=1: solo configurati o con dati", sorted(n["id"] for n in kn["nodes"]) == ["n1", "n2", "n3"],
          [n["id"] for n in kn["nodes"]])
    code, hz = h(col, "/api/hazards")
    it = hz["items"][0] if hz["items"] else {}
    check("/api/hazards: il pericolo di n1 con il percorso verso n2", len(hz["items"]) == 1 and it["node"] == "n1"
          and it["kind"] == "speeding" and it["path"] == ["n1", "n2"] and it["receivers"] == ["n2"], hz)
    check("/api/hazards: tratti con geometria orientata", it.get("segments") and it["segments"][0]["a"] == "n1"
          and it["segments"][0]["g"][0] == [45.07, 7.66] and len(it["segments"][0]["g"]) == 3, it.get("segments"))
    check("/api/hazards: scadenza corretta", it.get("expires_ms", 0) > now_ms() + 500_000 and it.get("danger") is True)
    code, ev = h(col, "/api/events")
    tipi = [(e["node"], e["type"], e["kind"]) for e in ev["items"]]
    check("/api/events: registro dal pull", ("n1", "hazard", "speeding") in tipi and ("n2", "remote_hazard", "speeding")
          in tipi and ("n2", "hazard", "crossing_conflict") in tipi, tipi)
    rem = next(e for e in ev["items"] if e["type"] == "remote_hazard")
    loc = next(e for e in ev["items"] if e["type"] == "hazard" and e["node"] == "n1")
    check("/api/events: tempi di n2 riportati sull'orologio del collettore", abs(rem["ts"] - (loc["ts"] + 1000)) < 3000
          and rem["from"] == "n1", (rem["ts"], loc["ts"]))
    check("/api/events: campi stabili", all({"seq", "ts", "node", "name", "src", "type", "kind", "category", "severity",
                                             "action", "id", "from", "to", "detail"} <= set(e) for e in ev["items"]))
    code, ev2 = h(col, f"/api/events?after={ev['last']}")
    check("/api/events?after=ultimo: vuoto", code == 200 and ev2["items"] == [] and ev2["last"] == ev["last"])
    code, ev3 = h(col, "/api/events?node=n2&limit=1")
    check("/api/events?node=&limit=: filtro per nodo", len(ev3["items"]) == 1 and ev3["items"][0]["node"] == "n2")
    code, _ = h(col, "/api/events?after=abc")
    check("/api/events?after=abc: 400", code == 400)
    code, cw = h(col, "/api/crosswalks")
    check("/api/crosswalks: stati delle strisce dal piu' grave", [x["state"] for x in cw["items"]] == ["conflict", "assist"]
          and cw["items"][0]["light"] == "danger", cw["items"])
    check("/api/crosswalks: conteggi", cw["counts"] == {"lit_now": 2, "unsafe_now": 1, "conflicts_24h": 1,
                                                         "assisted_24h": 1, "crossings_24h": 3}, cw["counts"])
    code, us = h(col, "/api/users")
    check("/api/users: Tag dal pull con ruolo e incrocio", us["by_role"]["blind"] == 1 and us["tags"][0]["node"] == "n1"
          and us["tags"][0]["src"] == "pull", us)
    check("/api/users: visti dalle camere in 24 h", us["seen_24h"] == {"person": 3, "vehicle": 1, "cyclist": 1},
          us["seen_24h"])
    code, en = h(col, "/api/energy")
    tot = en["total"]
    check("/api/energy: totali (risvegli, sorgenti, tempo a riposo)", tot["nodes"] == 2 and tot["wakeups"] == 4
          and tot["by_source"] == {"curb": 3, "radio_vru": 1} and tot["rest_pct"] == 83.3
          and abs(tot["saved_wh"] - 1.805) < 1e-6, tot)
    check("/api/energy: per incrocio, dal piu' risparmioso", [n["id"] for n in en["nodes"]] == ["n1", "n2"]
          and en["nodes"][0]["rest_pct"] == 90.0, en["nodes"])
    code, pl = h(col, "/api/pull")
    check("GET /api/pull: stato, risultati e storico", code == 200 and pl["mode"] == "manual"
          and set(pl["results"]) == {"n1", "n2", "n3"} and len(pl["history"]) >= 5, pl.keys())
    code, r = h(col, "/api/pull", "POST", b'{"nodes": ["n2", "boh"], "wait": true}')
    check("POST /api/pull con wait: 200 con i risultati", code == 200 and r["results"]["n2"]["ok"]
          and r["unknown"] == ["boh"], r)
    code, r = h(col, "/api/pull", "POST", b"")
    check("POST /api/pull senza corpo: 202, partono tutti", code == 202 and set(r["started"]) | set(r["already"])
          == {"n1", "n2", "n3"}, r)
    aspetta(lambda: not col.api_pull_state()["running"], 5)
    code, _ = h(col, "/api/pull", "POST", b"non json")
    code2, _ = h(col, "/api/pull", "POST", b'{"nodes": 5}')
    check("POST /api/pull con corpo sbagliato: 400", code == 400 and code2 == 400, (code, code2))
    code, _ = h(col, "/api/overview", "POST")
    code2, _ = h(col, "/api/nulla")
    check("POST altrove 405, risorsa sconosciuta 404", code == 405 and code2 == 404)
    code, html = h(col, "/")
    check("GET /: la dashboard", code == 200 and isinstance(html, bytes) and b"<!doctype html>" in html[:50].lower())


def test_http():
    col = CTX["col"]
    srv = C.serve(col, "127.0.0.1", 0)
    CTX["srv"] = srv
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    with _NO_PROXY.open(base + "/api/overview", timeout=5) as r:
        o = json.loads(r.read())
        ctype = r.headers.get("Content-Type", "")
    check("HTTP GET /api/overview: JSON", o["counts"]["configured"] == 3 and ctype.startswith("application/json"))
    req = urllib.request.Request(base + "/api/pull", data=json.dumps({"nodes": ["n1"], "wait": True}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with _NO_PROXY.open(req, timeout=10) as r:
        p = json.loads(r.read())
    check("HTTP POST /api/pull (pulsante 'Scarica ora' di un nodo)", r.status == 200 and p["results"]["n1"]["ok"], p)
    with _NO_PROXY.open(base + "/", timeout=5) as r:
        body = r.read()
        ctype = r.headers.get("Content-Type", "")
    check("HTTP GET /: dashboard in text/html", ctype.startswith("text/html") and b"CROSS" in body)
    try:
        _NO_PROXY.open(base + "/api/boh", timeout=5)
        code = 200
    except urllib.error.HTTPError as e:
        code = e.code
    check("HTTP risorsa sconosciuta: 404", code == 404)


# ---------------------------------------------------------------------- radio passiva

def manda(port, data):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(data if isinstance(data, bytes) else json.dumps(data).encode(), ("127.0.0.1", port))
    finally:
        s.close()


def test_radio_udp():
    col = CTX["col"]
    col.start(listen=True, interval=False)
    port = CTX["cfg"]["listen"]["port"]
    check("ascolto UDP aperto", aspetta(lambda: col.radio.state()["udp"], 2), col.radio.state())
    check("decodifica di peer.py in uso", col.radio.state()["codec"] == "peer.py")
    t = now_ms()
    frames = [
        {"v": 2, "t": "hz", "id": "~cam0-000001", "n": "n3", "a": "n", "k": "emergency_vehicle", "c": "emergency",
         "s": 3, "ts": t, "e": t + 60_000, "hd": 270, "to": ["n2"], "h": 2},
        {"v": 2, "t": "vru", "d": "tag-9", "p": "cyclist", "q": 1, "b": 80},
        {"v": 1, "t": "vru", "d": "band-1", "p": "person"},
        {"v": 2, "t": "emv", "d": "amb-1", "q": 5, "hd": 270, "nx": "n2", "st": "run"},
        {"v": 2, "t": "hb", "n": "n3", "ts": t, "pw": "s", "h": 0},
        {"v": 2, "t": "adv", "n": "n2", "id": "a1", "to": "tag-9", "c": 4, "s": 2, "x": 0, "ttl": 3000},
        {"v": 2, "t": "adv", "n": "n1", "id": "a2", "r": ["blind", "pedestrian"], "c": 1, "s": 2, "x": 0, "ttl": 3000},
    ]
    manda(port, b"rumore che non e' JSON")
    manda(port, {"v": 2, "t": "rq", "id": "q1", "w": "sum"})        # richieste: ignorate
    manda(port, b"R-63 " + json.dumps(frames[0]).encode())           # con RSSI come la radio seriale
    for f in frames[1:]:
        manda(port, f)
    ok = aspetta(lambda: col.radio.state()["frames"] >= 7, 4)
    s = col.radio.state()
    check("UDP: ricevuti hz, vru (v2 e v1), emv, hb, adv in loopback", ok and s["by_type"] == {
        "hz": 1, "vru": 2, "emv": 1, "hb": 1, "adv": 2}, s)
    hz = {i["id"]: i for i in col.api_hazards()["items"]}
    e = hz.get("n3-cam0-000001")
    check("UDP: hz con id corto ricostruito e mostrato come pericolo radio",
          e is not None and e["src"] == "radio" and e["category"] == "emergency" and e["path"] == ["n3", "n2"], hz.keys())
    tags = {x["device"]: x for x in col.api_users()["tags"]}
    check("UDP: Tag ciclista, pedone (beacon v1) e soccorso", tags.get("tag-9", {}).get("role") == "cyclist"
          and tags.get("band-1", {}).get("role") == "pedestrian" and tags.get("amb-1", {}).get("role") == "emergency"
          and tags["amb-1"]["next_node"] == "n2" and tags["amb-1"]["siren"] is True, tags)
    check("UDP: avviso al Tag -> Tag vicino a quell'incrocio", tags["tag-9"]["node"] == "n2"
          and tags["tag-9"]["advice"]["code"] == "VEHICLE_LEFT", tags["tag-9"])
    u = col.api_users()
    check("UDP: avvisi in corso e conteggio per codice", len(u["advice"]) == 2
          and u["advice_counts"] == {"VEHICLE_LEFT": 1, "WAIT": 1}, u["advice"])
    st = {n["id"]: n["state"] for n in col.api_map(geom=False)["nodes"]}
    check("UDP: mezzo di soccorso da n3 diretto a n2 -> entrambi in emergenza sulla mappa",
          st["n3"] == "emergency" and st["n2"] == "emergency", st)
    nd = {n["id"]: n for n in col.api_nodes()["nodes"]}
    check("UDP: stato di energia di n3 dalla radio", nd["n3"]["power_state"] == "sleep" and nd["n3"]["source"] == "radio"
          and nd["n3"]["radio"]["pw"] == "sleep", nd["n3"])
    ev = [x for x in col.api_events()["items"] if x["src"] == "radio"]
    check("UDP: il pericolo radio e' nel registro", len(ev) == 1 and ev[0]["id"] == "n3-cam0-000001", ev)
    o = col.api_overview()
    check("UDP: testata aggiornata (emergenze, Tag)", o["counts"]["emergencies"] == 1 and o["counts"]["tags"] >= 4
          and o["listen"]["frames"] >= 7, o["counts"])
    manda(port, frames[0])  # stesso hz di nuovo: niente doppioni nel registro
    aspetta(lambda: col.radio.state()["frames"] >= 8, 2)
    ev = [x for x in col.api_events()["items"] if x["src"] == "radio"]
    check("UDP: hz ripetuto non duplica il registro", len(ev) == 1, ev)
    sorgente = inspect.getsource(C.RadioListener) + inspect.getsource(C._open_serial_reader)
    check("ascolto passivo: nessuna chiamata di trasmissione nel codice",
          not re.search(r"\.sendto\(|\.send\(|\.sendall\(|\.write\(", sorgente))


def test_radio_firma():
    ricevuti = []
    rl = C.RadioListener({"udp": False, "serial": "", "key": "segreta"}, lambda o, r, v: ricevuti.append(o))
    t = now_ms()
    hz = {"v": 2, "t": "hz", "id": "n1-c-1", "n": "n1", "a": "n", "k": "speeding", "c": "risk", "s": 2, "ts": t, "e": t + 9000, "h": 1}
    check("firma: hz senza firma scartato", rl.handle(json.dumps(hz).encode(), "udp") is None and rl.errors == 1)
    firmato = peer.encode(hz, key="segreta")
    check("firma: hz firmato (peer.encode) accettato", rl.handle(firmato, "udp") is not None)
    check("firma: hz con h cambiato dal rilancio ancora valido",
          rl.handle(json.dumps(dict(json.loads(firmato), h=0)).encode(), "udp") is not None)
    vru = {"v": 2, "t": "vru", "d": "tag-1", "p": "blind", "q": 1}
    check("firma: beacon di un Tag senza firma accettato", rl.handle(json.dumps(vru).encode(), "udp") is not None)
    check("firma: beacon con firma sbagliata scartato",
          rl.handle(json.dumps(dict(vru, sig="0" * 16)).encode(), "udp") is None)
    rl2 = C.RadioListener({"udp": False}, lambda o, r, v: ricevuti.append(o))
    rl2._peer_decode = None
    rl2._expand_id, rl2._role_of = C.expand_id_min, C.role_min
    o = rl2.handle(b'R-70 {"v":2,"t":"hz","id":"~a-1","n":"n9","a":"n","k":"x","c":"risk","s":1,"ts":1,"e":2,"h":0}', "serial")
    check("decodifica minima senza peer.py: hz con id ricostruito", o is not None and o["id"] == "n9-a-1")
    o = rl2.handle(b'{"v":1,"t":"vru","d":"b1","p":"vehicle"}', "serial")
    check("decodifica minima: beacon v1 con ruolo convertito", o is not None and o["p"] == "driver")
    check("decodifica minima: messaggi non validi ignorati", rl2.handle(b"[1,2]", "udp") is None
          and rl2.handle(b'{"v":3,"t":"hz"}', "udp") is None and rl2.handle(b"", "udp") is None)

    def esplode(o, r, v):
        raise RuntimeError("callback rotta")
    rl3 = C.RadioListener({"udp": False}, esplode)
    check("callback in errore: nessuna eccezione verso il ricevitore",
          rl3.handle(json.dumps(vru).encode(), "udp") is not None and rl3.errors == 1)


# ---------------------------------------------------------------------- dashboard

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const base = process.argv[2], html = fs.readFileSync(process.argv[3], 'utf8'), pickId = process.argv[4] || '';
const code = /<script>([\s\S]*?)<\/script>/.exec(html)[1];
const errors = [], warns = [], timers = [], frames = [];
function El(id, tag) {
  this.id = id || ''; this.tagName = tag || 'div'; this.attrs = {}; this.children = []; this.innerHTML = '';
  this._text = ''; this.dataset = {}; this.hidden = false; this.style = {}; this.value = ''; this.disabled = false;
  this.parentNode = null; this.ls = {}; this.classList = { set: new Set(), add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); } };
}
Object.defineProperty(El.prototype, 'textContent', { get() { return this._text; }, set(v) { this._text = String(v); this.children = []; } });
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.appendChild = function (c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; };
El.prototype.removeChild = function (c) { this.children = this.children.filter(function (x) { return x !== c; }); c.parentNode = null; return c; };
El.prototype.addEventListener = function (t, f) { (this.ls[t] = this.ls[t] || []).push(f); };
El.prototype.dispatch = function (t, ev) { var self = this; (this.ls[t] || []).forEach(function (f) { f.call(self, ev || {}); }); };
El.prototype.querySelectorAll = function () { return []; };
El.prototype.getBoundingClientRect = function () { return { left: 0, top: 0, width: 1000, height: 600 }; };
El.prototype.scrollIntoView = function () {};
El.prototype.setPointerCapture = function () {};
El.prototype.closest = function () { return null; };
const els = {};
const document = { hidden: false, title: '', ls: {},
  getElementById(id) { return els[id] || (els[id] = new El(id)); },
  createElementNS(ns, tag) { return new El('', tag); },
  addEventListener(t, f) { (this.ls[t] = this.ls[t] || []).push(f); } };
const requests = [];
const sandbox = { document: document,
  console: { log() {}, info() {}, warn: function () { warns.push([].map.call(arguments, String).join(' ')); },
             error: function () { errors.push([].map.call(arguments, function (x) { return (x && x.stack) || String(x); }).join(' ')); } },
  fetch: function (u, o) { requests.push(u); var opt = Object.assign({}, o || {}); delete opt.cache; return fetch(base + u, opt); },
  setTimeout: function (f, ms) { timers.push({ f: f, ms: ms }); return timers.length; }, clearTimeout: function () {},
  requestAnimationFrame: function (f) { frames.push(f); return frames.length; },
  matchMedia: function () { return { matches: false }; }, addEventListener: function () {} };
sandbox.window = sandbox;
vm.createContext(sandbox);
function giri() { return timers.filter(function (t) { return t.ms === 2000; }); }
async function attendi(n) {
  for (let i = 0; i < 400; i++) { if (giri().length >= n) return true; await new Promise(function (r) { setTimeout(r, 25); }); }
  return false;
}
(async function () {
  vm.runInContext(code, sandbox, { filename: 'dashboard.js' });
  const ok1 = await attendi(1);
  giri()[0].f();                       // secondo giro: geometria gia' presa, eventi incrementali
  const ok2 = await attendi(2);
  frames.splice(0).forEach(function (f) { f(1000); });   // un fotogramma dell'animazione dei percorsi
  els.pick.value = pickId; els.pick.dispatch('change');  // scheda di un incrocio dall'elenco
  await new Promise(function (r) { setTimeout(r, 500); });
  const out = { ok1: ok1, ok2: ok2, errors: errors, warns: warns, requests: requests, html: {}, map: {} };
  ['meta', 'f-nodes', 'f-awake', 'f-asleep', 'f-haz', 'f-tags', 'f-saved', 'haz', 'cross', 'cross-figs', 'user-figs',
   'tags-list', 'adv-list', 'adv-counts', 'seen', 'energy-figs', 'wake', 'energy-nodes', 'pull-mode', 'pull-table',
   'radio', 'pull-hist', 'log', 'card', 'pick'].forEach(function (id) { out.html[id] = (els[id] || {}).innerHTML || ''; });
  const g = function (id) { return els[id] || { children: [], attrs: {} }; };
  out.map.nodes = g('nodes').children.map(function (x) { return (x.attrs['data-id'] || '') + ':' + (x.attrs['class'] || ''); });
  out.map.streets = (g('streets').attrs.d || '').length;
  out.map.routes = g('routes').children.length;
  out.map.routeD = g('routes').children.map(function (r) { return (r.children[1] && r.children[1].attrs.d) || ''; });
  out.map.tags = g('tags').children.length;
  out.map.viewBox = g('map').attrs.viewBox || '';
  out.card_hidden = g('card').hidden;
  out.title = g('title')._text;
  process.stdout.write(JSON.stringify(out));
})().catch(function (e) { process.stdout.write(JSON.stringify({ fatal: String((e && e.stack) || e) })); });
"""


def _node():
    return shutil.which("node") or shutil.which("node.exe")


def test_dashboard_statica():
    html = DASHBOARD.read_text(encoding="utf-8")
    script = re.search(r"<script>([\s\S]*?)</script>", html)
    node = _node()
    if node is None:
        check("JavaScript della dashboard: node non installato, controllo sintattico saltato", True)
    else:
        f = CTX["tmp"] / "dashboard_check.js"
        f.write_text(script.group(1), encoding="utf-8")
        p = subprocess.run([node, "--check", str(f)], capture_output=True, text=True, timeout=30)
        check("JavaScript della dashboard: sintassi valida (node --check)", p.returncode == 0, p.stderr[-800:])
    check("una sola pagina con un solo script in linea", html.count("<script") == 1 and "<script src" not in html)
    esterni = re.findall(r"""(?:src|href)\s*=\s*["']?(?:https?:)?//[^"'\s>]+|@import|<link\b|url\(\s*["']?(?:https?:)?//""", html)
    check("nessuna risorsa esterna (script, fogli di stile, font, immagini)", not esterni, esterni)
    check("fetch solo verso le API del collettore", all(u.startswith("/api/") for u in
                                                         re.findall(r"getJSON\('([^']+)'", html)))
    css = re.search(r"<style>([\s\S]*?)</style>", html).group(1)
    raggi = re.findall(r"border-radius\s*:\s*([^;}]+)", css)
    check("angoli vivi ovunque (border-radius: 0)", raggi and all(r.strip().startswith("0") for r in raggi), raggi)
    ombre = re.findall(r"(?:box|text)-shadow\s*:\s*([^;}]+)", css)
    check("niente ombre", all(o.strip().startswith("none") for o in ombre), ombre)
    check("nessuna colonna affiancata (niente grid, columns, float)",
          not re.search(r"grid-template-columns|display\s*:\s*grid|(?<![-\w])columns\s*:|column-count|float\s*:\s*(left|right)",
                        css))
    check("font Titillium Web con ripiego di sistema", '"Titillium Web", system-ui, sans-serif' in css)
    check("testo blu notte #131a33 su bianco", "--ink: #131a33" in css and "--paper: #ffffff" in css)
    check("larghezza massima ~1100 px", "--wrap: 1100px" in css)
    check("numeri grandi e leggeri (font-weight 300)", re.search(r"\.num\s*\{[^}]*font-weight:\s*300", css) is not None)
    check("etichette in maiuscoletto spaziato", re.search(r"\.lab\s*\{[^}]*letter-spacing[^}]*text-transform:\s*uppercase",
                                                          css) is not None)
    check("sezioni aperte da una riga spessa blu con segno bicolore",
          re.search(r"\.sec\s*\{[^}]*border-top:\s*6px solid var\(--blue\)", css) is not None and ".sec .mark" in css)
    ordine = [html.find(f'id="{s}"') for s in ("mappa", "pericoli", "attraversamenti", "utenti", "energia", "raccolta",
                                                 "registro")]
    check("sezioni nell'ordine di par. 5.3", all(i > 0 for i in ordine) and ordine == sorted(ordine), ordine)
    testata = html[html.find('<header class="mast">'):html.find("</header>")]
    check("testata con i sei numeri", all(x in testata for x in ("incroci", "svegli ora", "a riposo", "pericoli in corso",
                                                                 "Tag in strada", "energia risparmiata")))
    usate = sorted(set(re.findall(r"'(/api/[a-z]+)", html)))
    col = CTX["col"]
    mancanti = [u for u in usate if h(col, u)[0] == 404]
    check("la dashboard usa solo API che il collettore espone", usate and not mancanti, (usate, mancanti))
    check("aggiornamento ogni 2 s", "setTimeout(loop, 2000)" in html)


def test_dashboard_in_node():
    node = _node()
    if node is None:
        check("prova della dashboard in node: node non installato, saltata", True)
        return
    srv = CTX.get("srv") or C.serve(CTX["col"], "127.0.0.1", 0)
    CTX["srv"] = srv
    f = CTX["tmp"] / "harness.js"
    f.write_text(HARNESS, encoding="utf-8")
    p = subprocess.run([node, str(f), f"http://127.0.0.1:{srv.server_address[1]}", str(DASHBOARD), "n1"],
                       capture_output=True, text=True, timeout=60, encoding="utf-8")
    try:
        out = json.loads(p.stdout)
    except ValueError:
        check("dashboard in node: risultato leggibile", False, (p.stdout[-500:], p.stderr[-800:]))
        return
    check("dashboard in node: nessun errore fatale", "fatal" not in out, out.get("fatal"))
    if "fatal" in out:
        return
    H = out["html"]
    check("dashboard in node: due giri di aggiornamento completati", out["ok1"] and out["ok2"])
    check("dashboard in node: nessun errore nel disegno delle sezioni", not out["errors"], out["errors"][:3])
    check("dashboard in node: nessuna richiesta fallita", not out["warns"], out["warns"][:3])
    check("dashboard in node: secondo giro senza geometria e con eventi incrementali",
          "/api/map?geom=0" in out["requests"] and any(r.startswith("/api/events?after=") for r in out["requests"]),
          out["requests"])
    check("testata: numeri e titolo", H["f-nodes"] == "4" and H["f-tags"] not in ("", "–") and out["title"] == "CROSS Prova"
          and "Wh" in H["f-saved"], (H["f-nodes"], H["f-tags"], H["f-saved"], out["title"]))
    check("testata: riga di stato", "Aggiornato alle" in H["meta"] and "radio in ascolto" in H["meta"], H["meta"])
    nodi = dict(x.split(":", 1) for x in out["map"]["nodes"])
    check("mappa: un punto per incrocio con la classe dello stato",
          len(nodi) == 4 and "hazard" in nodi["n1"] and "emergency" in nodi["n3"] and "nodata" in nodi["n4"]
          and " sel" in nodi["n1"], nodi)
    check("mappa: strade disegnate e vista impostata", out["map"]["streets"] > 50 and len(out["map"]["viewBox"].split()) == 4,
          out["map"])
    check("mappa: percorsi dei pericoli animati", out["map"]["routes"] == 2 and all(d.startswith("M") for d in out["map"]["routeD"]),
          out["map"]["routeD"])
    check("mappa: Tag vicini come piccoli segni", out["map"]["tags"] >= 2, out["map"]["tags"])
    check("scheda dell'incrocio sotto la mappa", not out["card_hidden"] and "Scheda dell" in H["card"]
          and "Via Uno / Corso Due" in H["card"] and "Ultimi eventi" in H["card"] and "Eccesso di velocit" in H["card"],
          H["card"][:400])
    check("pericoli in corso: tabella", "Eccesso di velocit" in H["haz"] and "Mezzo di soccorso" in H["haz"], H["haz"][:300])
    check("attraversamenti: numeri e tabella", "Conflitto" in H["cross"] and "Assistito" in H["cross"]
          and "attraversamenti in 24 h" in H["cross-figs"], H["cross"][:300])
    check("utenti della strada: Tag e avvisi", "tag-9" in H["tags-list"] and "tag-1" in H["tags-list"]
          and "Veicolo in arrivo da sinistra" in H["adv-list"] and "Ciclisti" in H["user-figs"], H["tags-list"][:300])
    check("energia: tempo a riposo, risvegli per sorgente, barre", "83" in H["energy-figs"]
          and "Persona sul bordo delle strisce" in H["wake"] and 'class="bar' in H["energy-nodes"], H["energy-figs"])
    check("raccolta dati: tabella con 'Scarica ora' ed errori", H["pull-table"].count("Scarica ora") == 3
          and "non riuscito" in H["pull-table"] and "a richiesta" in H["pull-mode"], H["pull-table"][:300])
    check("raccolta dati: ascolto radio e storico", "senza trasmettere" in H["radio"] and "<li>" in H["pull-hist"])
    check("registro eventi", "Eccesso di velocit" in H["log"] and "ricevuto da" in H["log"], H["log"][:300])
    check("elenco degli incroci per aprire la scheda", H["pick"].count("<option") == 5)


# ---------------------------------------------------------------------- topologia, intervallo, ripartenza

def test_topologia():
    tmp = CTX["tmp"]
    v1 = C.V1_TOPOLOGY
    cm = C.CityMap(tmp / "manca.json", fallbacks=[v1])
    d = cm.get()
    if v1.is_file():
        check("topologia v1 del server convertita al volo", d["format"] == "v1" and len(d["nodes"]) > 0
              and len(d["edges"]) > 0 and all(len(e["geom"]) >= 2 for e in d["edges"]),
              (d["format"], len(d["nodes"]), len(d["edges"])))
    else:
        check("topologia v1 del server assente: mappa vuota", d["format"] == "vuota")
    d = C.CityMap(tmp / "manca.json", fallbacks=[]).get()
    check("nessuna topologia: mappa vuota senza errori", d["format"] == "vuota" and d["nodes"] == {} and d["bbox"] is None)
    f = tmp / "t2.json"
    f.write_text(json.dumps(TOPO), encoding="utf-8")
    cm = C.CityMap(f)
    rev = cm.get()["rev"]
    f.write_text("{rovinato", encoding="utf-8")
    cm._checked = 0
    check("topologia rovinata: resta la versione precedente", cm.get()["rev"] == rev and len(cm.get()["nodes"]) == 4)
    topo = dict(TOPO, edges=[], nodes={k: dict(v) for k, v in TOPO["nodes"].items()})
    f.write_text(json.dumps(topo), encoding="utf-8")
    cm._checked = 0
    d = cm.get()
    check("topologia cambiata: ricaricata (archi dai vicini se mancano gli edges)", d["rev"] != rev
          and len(d["edges"]) == 3, len(d["edges"]))
    check("percorso piu' breve sulla topologia", C.CityMap.path(d, "n1", "n4") == ["n1", "n2", "n4"])
    vuota = C.Collector({"nodes": {}, "db": ":memory:", "topology": str(tmp / "manca.json"),
                         "listen": {"udp": False}})
    vuota.city = C.CityMap(tmp / "manca.json", fallbacks=[])
    o, m = vuota.api_overview(), vuota.api_map()
    check("collettore senza nodi ne' topologia: risposte vuote valide", o["counts"]["nodes"] == 0 and m["nodes"] == []
          and m["edges"] == [] and vuota.api_hazards()["items"] == [] and vuota.api_energy()["total"]["rest_pct"] is None)
    vuota.close()


def test_intervallo():
    a1 = CTX["a1"]
    col = C.Collector({"nodes": {"n1": f"http://127.0.0.1:{a1.port}"}, "db": ":memory:", "listen": {"udp": False},
                       "pull": {"mode": "interval", "interval_s": 10, "timeout_s": 2}})
    try:
        col.city = C.CityMap(None, fallbacks=[])
        col.start(listen=False)
        ok = aspetta(lambda: "n1" in col.api_pull_state()["results"], 6)
        st = col.api_pull_state()
        check("pull a intervallo: primo pull da solo dopo l'avvio", ok and st["results"]["n1"]["ok"]
              and st["mode"] == "interval" and st["next_ms"] > now_ms(), st)
    finally:
        col.close()


def test_ripartenza():
    col = CTX["col"]
    srv = CTX.pop("srv", None)
    if srv is not None:
        srv.shutdown()
        srv.server_close()
    cursore = col.store.cursor("n1")
    col.close()
    col2 = C.Collector(CTX["cfg"])
    try:
        check("ripartenza: cursori conservati", col2.store.cursor("n1") == cursore and cursore)
        check("ripartenza: ultimo stato dei nodi dall'archivio", col2.info["n1"]["status"]["power"]["state"] == "sleep")
        hz = col2.api_hazards()["items"]
        check("ripartenza: pericoli in corso ricostruiti dal registro", any(x["id"] == "n1-cam0-000007" for x in hz),
              [x["id"] for x in hz])
        r = col2.pull(wait=True, nodes=["n1", "n2"])
        check("ripartenza: il pull riprende dal cursore", r["results"]["n1"]["items"] == 0
              and r["results"]["n2"]["items"] == 0, r["results"])
    finally:
        col2.close()


def chiudi():
    for k in ("a1", "a2"):
        if CTX.get(k) is not None:
            CTX[k].stop()
    if CTX.get("tmp"):
        shutil.rmtree(CTX["tmp"], ignore_errors=True)


def main():
    try:
        for fn in (test_pull, test_api_dashboard, test_http, test_radio_udp, test_radio_firma, test_dashboard_statica,
                   test_dashboard_in_node, test_topologia, test_intervallo, test_ripartenza):
            print(f"\n== {fn.__name__}")
            esegui(fn)
    finally:
        with contextlib.redirect_stdout(io.StringIO()):
            chiudi()
    falliti = [r for r in RISULTATI if not r[1]]
    print(f"\n{len(RISULTATI) - len(falliti)}/{len(RISULTATI)} controlli OK")
    for nome, _, info in falliti:
        print(f"  FALLITO: {nome} -> {str(info)[:300]}")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
