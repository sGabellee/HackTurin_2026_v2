"""Test dell'export con cursore (storage.py) e dell'API locale del nodo (api.py), ARCHITETTURA.md par. 5.1.

Prova: ordine di arrivo tra le tabelle, paginazione con "more", cursore malformato (= dall'inizio),
cursore di un altro archivio, tabella svuotata, ts fuori ordine, limiti, pagina piena per byte,
archivio chiuso, riapertura dello stesso file; metodi esistenti (query, latest, heatmap, coda,
pulizia) ancora funzionanti; GET /api/export (pagine, limit, 400, nodo senza archivio),
/api/status con e senza i componenti v2, sola lettura, niente pagina /app.

Uso:  python test_export.py      (dalla cartella node/tools/tests; esce con codice 1 se qualcosa fallisce)
"""
import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI.parents[1] / "python"))

from api import LocalAPI  # noqa: E402
from storage import EXPORT_LIMIT_MAX, Storage  # noqa: E402

RISULTATI = []
T0 = 1_760_000_000_000  # ms
CURSORE = re.compile(r"d\d+\.e\d+\.t\d+\.s\d+\.u\d+\.m\d+")
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
        RISULTATI.append((fn.__name__, False, f"eccezione {e!r}\n{traceback.format_exc(limit=4)}"))
    for nome, ok, info in RISULTATI[prima:]:
        print(f"[{'OK ' if ok else 'ERR'}] {nome}" + ("" if ok else f"  -> {info}"))


# ---------------------------------------------------------------------- dati di prova

def riempi(st):
    """Un po' di tutto, salvato in quest'ordine (ts non sempre crescente tra le tabelle).
    Ritorna l'elenco atteso (tabella, ts) in ordine di arrivo per l'export."""
    st.save({"type": "hazard", "id": "n-1", "kind": "speeding", "action": "new", "severity": 2, "ts": T0 + 100})
    st.save({"type": "track", "id": "tr-1", "class": "vehicle", "ts": T0 + 50, "ts_start": T0, "ts_end": T0 + 50})
    st.save({"type": "stats", "ts": T0 + 300, "cameras": {}})
    st.save({"type": "status", "ts": T0 + 200, "power": {"state": "sleep"}})
    st.save({"type": "scene", "ts": T0 + 210, "objects": []})            # non si esporta
    st.save({"type": "movement", "id": "mv-1", "kind": "movement", "ts": T0 + 400})
    st.save({"type": "wake_note", "ts": T0 + 150})                       # tipo futuro -> messages
    st.save({"type": "hazard", "id": "n-1", "kind": "speeding", "action": "cancel", "ts": T0 + 500})
    return [("tracks", T0 + 50), ("events", T0 + 100), ("messages", T0 + 150), ("status", T0 + 200),
            ("stats", T0 + 300), ("events", T0 + 400), ("events", T0 + 500)]


def tutto(st, limit, cursor=None, max_pagine=100):
    """Scarica tutto a pagine. Ritorna (elementi, pagine, ultimo cursore)."""
    items, pagine = [], 0
    for _ in range(max_pagine):
        r = st.export(cursor, limit)
        pagine += 1
        items += r["items"]
        cursor = r["cursor"]
        if not r["more"]:
            break
    return items, pagine, cursor


# ---------------------------------------------------------------------- export (storage.py)

def test_ordine_e_formato():
    st = Storage(":memory:")
    atteso = riempi(st)
    r = st.export(None, 500)
    got = [(it["table"], it["ts"]) for it in r["items"]]
    check("export: ordine di arrivo per ts tra tutte le tabelle", got == atteso, got)
    check("export: le scene non si esportano", all(it["msg"].get("type") != "scene" for it in r["items"]))
    check("export: elemento {table, rid, ts, msg} con il messaggio originale",
          all(set(it) == {"table", "rid", "ts", "msg"} for it in r["items"])
          and r["items"][1]["msg"]["kind"] == "speeding" and isinstance(r["items"][1]["rid"], int))
    check("export: more = False quando c'e' tutto", r["more"] is False and r["count"] == len(atteso))
    check("export: cursore opaco nel formato d.e.t.s.u.m", CURSORE.fullmatch(r["cursor"]) is not None, r["cursor"])
    pos, db = Storage.parse_cursor(r["cursor"])
    check("export: il cursore contiene l'ultimo rid per tabella",
          pos == {"e": 3, "t": 1, "s": 1, "u": 1, "m": 2} and db == st.db_id, (pos, db))
    r2 = st.export(r["cursor"], 500)
    check("export: dal cursore finale niente di nuovo (stesso cursore)",
          r2["items"] == [] and r2["more"] is False and r2["cursor"] == r["cursor"], r2)
    st.save({"type": "hazard", "id": "n-2", "kind": "wrong_way", "ts": T0 + 50})   # ts vecchio, salvato dopo
    st.save({"type": "status", "ts": T0 + 900})
    r3 = st.export(r["cursor"], 500)
    check("export: dopo il cursore solo i nuovi, anche con ts piu' vecchio",
          [(it["table"], it["msg"].get("id")) for it in r3["items"]] == [("events", "n-2"), ("status", None)],
          r3["items"])
    st.close()


def test_paginazione():
    st = Storage(":memory:")
    atteso = riempi(st)
    for i in range(30):
        st.save({"type": "track", "id": f"x{i}", "class": "person", "ts": T0 + 1000 + i})
    totale = len(atteso) + 30
    r = st.export("", 4)
    check("pagine: la prima ha 'limit' elementi e more = True", r["count"] == 4 and r["more"] is True, r["count"])
    items, pagine, _ = tutto(st, 4)
    check("pagine: tutti gli elementi una volta sola", len(items) == totale
          and len({(i["table"], i["rid"]) for i in items}) == totale, len(items))
    check("pagine: numero di pagine giusto", pagine == (totale + 3) // 4, pagine)
    ts = [i["ts"] for i in items]
    check("pagine: in ordine di arrivo anche a cavallo delle pagine", ts == sorted(ts))
    pagina = st.export(None, 0)
    check("limit 0 = 1 elemento", pagina["count"] == 1 and st.export(None, -5)["count"] == 1)
    check("limit non numerico = default 500", st.export(None, "x")["count"] == totale)
    for i in range(EXPORT_LIMIT_MAX + 50):
        st.save({"type": "status", "ts": T0 + 5000 + i})
    big = st.export(None, 99999)
    check(f"limit oltre il massimo = {EXPORT_LIMIT_MAX}", big["count"] == EXPORT_LIMIT_MAX and big["more"], big["count"])
    piccola = st.export(None, 100, max_bytes=10)
    check("pagina piena per byte: almeno un elemento e more = True", piccola["count"] == 1 and piccola["more"])
    st.close()


def test_cursori_malformati():
    st = Storage(":memory:")
    riempi(st)
    base = st.export(None, 500)
    for c in ("pippo", "e1.x", "e-1", "e1..t2", "E3.T1", "e99999999999999999999", "x" * 300, 123, ["e1"], {"e": 1}):
        r = st.export(c, 500)
        check(f"cursore malformato {str(c)[:20]!r}: dall'inizio", r["items"] == base["items"], (r["count"], base["count"]))
    r = st.export(" e1 . t0 ", 500)
    check("cursore con spazi: letto lo stesso", [i["rid"] for i in r["items"] if i["table"] == "events"] == [2, 3])
    r = st.export("e3.z9", 500)
    check("lettera sconosciuta (tabella futura): ignorata, mancanti da 0",
          r["count"] == base["count"] - 3 and all(i["table"] != "events" for i in r["items"]), r["count"])
    altro = f"d{(st.db_id % 999_999_999) + 1}.e3.t1.s1.u1.m2"
    r = st.export(altro, 500)
    check("cursore di un altro archivio: tutto dall'inizio con reset", r["count"] == base["count"]
          and set(r["reset"]) == {"events", "tracks", "stats", "status", "messages"}, r["reset"])
    r = st.export("e500.t1.s1.u1.m2", 500)
    check("rid oltre la fine (tabella svuotata): quella tabella da capo",
          r["reset"] == ["events"] and [i["table"] for i in r["items"]] == ["events"] * 3, r)
    st.close()
    r = st.export(base["cursor"], 10)
    check("archivio chiuso: nessuna eccezione, stesso cursore ed errore",
          r["items"] == [] and r["cursor"] == base["cursor"] and r["more"] is False and "error" in r, r)


def test_file_e_pulizia():
    tmp = Path(tempfile.mkdtemp(prefix="cross-export-"))
    try:
        st = Storage(tmp / "n.db", retention_days=1)
        riempi(st)
        r = st.export(None, 500)
        db_id = st.db_id
        st.close()
        st = Storage(tmp / "n.db", retention_days=1)
        check("riapertura: stessa identita' dell'archivio", st.db_id == db_id and db_id is not None)
        st.save({"type": "track", "id": "dopo", "class": "cyclist", "ts": T0 + 9000})
        r2 = st.export(r["cursor"], 500)
        check("riapertura: il cursore continua da dove era", [i["msg"].get("id") for i in r2["items"]] == ["dopo"],
              r2["items"])
        # pulizia: tutto vecchio (ts del 2025) -> resta l'ultima riga di ogni tabella
        tolti = st.cleanup(time.time())
        check("pulizia: cancella le righe vecchie", tolti > 0, tolti)
        st.save({"type": "hazard", "id": "nuovo", "kind": "speeding", "ts": int(time.time() * 1000)})
        r3 = st.export(r2["cursor"], 500)
        check("dopo la pulizia i rid non ripartono: il cursore resta valido",
              [i["msg"].get("id") for i in r3["items"]] == ["nuovo"] and r3["reset"] == [], r3)
        # metodi esistenti
        check("query() funziona ancora", [m["id"] for m in st.query("events", kind="speeding")] == ["n-1", "nuovo"],
              st.query("events", kind="speeding"))
        check("latest() funziona ancora", (st.latest("tracks") or {}).get("id") == "dopo")
        st.save({"type": "stats", "ts": T0, "cameras": {"cam0": {"heatmap": {"person": {"1,2": 3}},
                                                                 "heatmap_cell": 0.5, "heatmap_unit": "m"}}})
        check("heatmap() funziona ancora", st.heatmap(camera="cam0")[0]["count"] == 3)
        check("coda di invio v1 funziona ancora", st.outbox_put({"type": "x", "ts": T0}) and st.outbox_size() == 1)
        st.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------- API locale (api.py)

class NodoFinto:
    def __init__(self, node_id="incrocio-1", storage=None, snapshot=None, componenti=False, snapshot_rotto=False):
        self.node_id = node_id
        self.storage = storage
        self.cfg = {"node_id": node_id, "p2p": {"key": "segreta"}}
        self.started = time.time()
        self.latest_scene = []
        self._snap = snapshot or {}
        self._rotto = snapshot_rotto
        if componenti:
            self.power = type("P", (), {"stats": lambda s, t: {"state": "sleep", "saved_wh": 1.5}})()
            self.crosswalk = type("C", (), {"states": lambda s: [{"id": 0, "state": "idle", "light": 0}]})()
            self.users = type("U", (), {"near": lambda s, t: [{"device": "tag-1", "role": "blind"}]})()
            self.peer = type("R", (), {"peers": lambda s: {"incrocio-2": {"pw": "s"}}})()

    def status_snapshot(self):
        if self._rotto:
            raise RuntimeError("snapshot rotto")
        return dict(self._snap)


def get(api, path):
    try:
        with _NO_PROXY.open(f"http://127.0.0.1:{api.port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def test_api_export():
    st = Storage(":memory:")
    atteso = riempi(st)
    api = LocalAPI({"host": "127.0.0.1", "port": 0}, NodoFinto(storage=st))
    api.start()
    try:
        check("api: server avviato su una porta libera", api.running and api.port > 0, api.port)
        code, r = get(api, "/api/export")
        check("GET /api/export: 200 con node, cursor, more, items",
              code == 200 and r["node"] == "incrocio-1" and r["more"] is False and len(r["items"]) == len(atteso)
              and CURSORE.fullmatch(r["cursor"]) is not None, (code, r))
        cursor, viste, pagine = "", [], 0
        while True:
            code, p = get(api, f"/api/export?cursor={cursor}&limit=3")
            pagine += 1
            viste += [(i["table"], i["ts"]) for i in p["items"]]
            cursor = p["cursor"]
            if not p["more"] or pagine > 10:
                break
        check("GET /api/export a pagine di 3: tutto in ordine", viste == atteso and pagine == 3, (pagine, viste))
        code, p = get(api, "/api/export?cursor=rotto!!&limit=2")
        check("GET /api/export con cursore malformato: dall'inizio", code == 200 and [(i["table"], i["ts"])
              for i in p["items"]] == atteso[:2] and p["more"] is True, p)
        code, p = get(api, "/api/export?limit=abc")
        check("GET /api/export?limit=abc: 400", code == 400 and "limit" in p.get("error", ""), (code, p))
        code, p = get(api, "/api/export?limit=0")
        check("GET /api/export?limit=0: almeno 1", code == 200 and len(p["items"]) == 1)
        code, p = get(api, f"/api/export?cursor={r['cursor']}")
        check("GET /api/export dal cursore finale: vuoto", code == 200 and p["items"] == [] and not p["more"])
        code, _ = get(api, "/app")
        code2, idx = get(api, "/")
        check("niente pagina /app (404) e non e' tra le risorse", code == 404 and code2 == 200
              and not any("app" in e.split("/")[-1] for e in idx["endpoints"] if e != "/api/export"), idx)
        check("/api/export e' tra le risorse", "/api/export" in idx["endpoints"])
        req = urllib.request.Request(f"http://127.0.0.1:{api.port}/api/export", data=b"{}", method="POST")
        try:
            _NO_PROXY.open(req, timeout=5)
            codice = 200
        except urllib.error.HTTPError as e:
            codice = e.code
        check("API in sola lettura: POST = 405", codice == 405, codice)
    finally:
        api.stop()
        st.close()
    code, p = LocalAPI({}, NodoFinto(storage=None)).handle("/api/export", "cursor=e1")
    check("nodo senza archivio: export vuoto, stesso cursore", code == 200 and p["items"] == [] and p["cursor"] == "e1"
          and p["storage"] is False, p)


def test_api_status():
    api = LocalAPI({}, NodoFinto(snapshot={"node": "incrocio-1", "status": {"x": 1}}))
    code, s = api.handle("/api/status", "")
    check("/api/status senza componenti v2: chiavi presenti con valori vuoti",
          code == 200 and s["power"] is None and s["crosswalks"] == [] and s["users"] == [] and s["peers"] == {}
          and isinstance(s["ts"], int), s)
    api = LocalAPI({}, NodoFinto(componenti=True))
    code, s = api.handle("/api/status", "")
    check("/api/status letto dai componenti (energia, attraversamenti, Tag, peer)",
          code == 200 and s["power"]["state"] == "sleep" and s["crosswalks"][0]["state"] == "idle"
          and s["users"][0]["role"] == "blind" and "incrocio-2" in s["peers"], s)
    api = LocalAPI({}, NodoFinto(componenti=True, snapshot={"power": {"state": "active"}}))
    code, s = api.handle("/api/status", "")
    check("/api/status: lo snapshot del nodo vince sui componenti", s["power"]["state"] == "active", s["power"])
    st = Storage(":memory:")
    st.save({"type": "status", "ts": T0, "uptime_s": 5})
    api = LocalAPI({}, NodoFinto(storage=st, snapshot_rotto=True))
    code, s = api.handle("/api/status", "")
    check("/api/status con snapshot in errore: 200 con l'ultimo stato salvato",
          code == 200 and (s.get("status") or {}).get("uptime_s") == 5 and s["node"] == "incrocio-1", s)
    code, c = api.handle("/api/config", "")
    check("/api/config senza segreti", code == 200 and c["p2p"]["key"] == "***", c)
    code, h = api.handle("/api/health", "")
    check("/api/health", code == 200 and h["ok"] is True and h["storage"] is True)
    st.close()


def main():
    for fn in (test_ordine_e_formato, test_paginazione, test_cursori_malformati, test_file_e_pulizia,
               test_api_export, test_api_status):
        print(f"\n== {fn.__name__}")
        esegui(fn)
    falliti = [r for r in RISULTATI if not r[1]]
    print(f"\n{len(RISULTATI) - len(falliti)}/{len(RISULTATI)} controlli OK")
    for nome, _, info in falliti:
        print(f"  FALLITO: {nome} -> {str(info)[:300]}")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
