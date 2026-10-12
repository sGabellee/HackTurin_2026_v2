"""Archivio locale SQLite del nodo: eventi, tracce, statistiche, stato, heatmap (+ coda di invio v1).

Un solo file (smartcrossing.db) in modalita' WAL: il nodo scrive mentre l'API locale o gli
strumenti di export leggono, senza bloccarsi a vicenda. Ogni messaggio e' salvato come JSON
integrale (query() restituisce i dict originali) piu' poche colonne indicizzate per filtrare.
Nella v2 i dati restano qui finche' il collettore non viene a prenderli (export, sotto); la
tabella outbox (coda di invio push della v1) resta solo per compatibilita'.

I metodi usati a runtime non sollevano mai eccezioni: scrivono un avviso e restituiscono un
valore vuoto (solo il costruttore puo' fallire, e il nodo allora va avanti senza archivio).

Export per il collettore (pull, ARCHITETTURA.md §5.1): export(cursor, limit) restituisce in ordine
di arrivo gli elementi salvati dopo il cursore. Il cursore e' opaco per il collettore, es.
"d48213977.e120.t45.s7.u9.m3" = identita' dell'archivio + ultimo rid letto per tabella (events,
tracks, stats, status, messages senza "scene"). Il nodo non cancella nulla per l'export (vale
retention_days). Cursore malformato o di un altro archivio (file ricreato) = si riparte dall'inizio;
rid oltre la fine di una tabella (tabella svuotata dalla pulizia) = quella tabella riparte da capo.
"""
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

OUTBOX_MAX = 50000  # righe massime in coda di invio: oltre, si scartano le piu' vecchie
EXPORT_LIMIT_MAX = 2000              # elementi massimi per pagina di export
EXPORT_MAX_BYTES = 4_000_000         # JSON massimo per pagina (le stats con la heatmap pesano)
# tabella esportata -> lettera nel cursore (l'ordine decide chi viene prima a parita' di ts)
EXPORT_TABLES = (("events", "e"), ("tracks", "t"), ("stats", "s"), ("status", "u"), ("messages", "m"))
EXPORT_SKIP_TYPES = ("scene",)       # tipi della tabella messages che non si esportano (1 al secondo)
_CURSOR_TOKEN = re.compile(r"([a-z])(\d{1,18})")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    rid INTEGER PRIMARY KEY, id TEXT, ts INTEGER, type TEXT, kind TEXT,
    action TEXT, severity INTEGER, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events(kind, ts);
CREATE INDEX IF NOT EXISTS events_id ON events(id);

CREATE TABLE IF NOT EXISTS tracks (
    rid INTEGER PRIMARY KEY, id TEXT, ts INTEGER, ts_start INTEGER, ts_end INTEGER,
    camera TEXT, class TEXT, from_ap TEXT, to_ap TEXT, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS tracks_ts ON tracks(ts);
CREATE INDEX IF NOT EXISTS tracks_class_ts ON tracks(class, ts);

CREATE TABLE IF NOT EXISTS stats (rid INTEGER PRIMARY KEY, ts INTEGER, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS stats_ts ON stats(ts);

CREATE TABLE IF NOT EXISTS status (rid INTEGER PRIMARY KEY, ts INTEGER, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS status_ts ON status(ts);

CREATE TABLE IF NOT EXISTS messages (rid INTEGER PRIMARY KEY, ts INTEGER, type TEXT, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS messages_type_ts ON messages(type, ts);

CREATE TABLE IF NOT EXISTS heatmap (
    camera TEXT NOT NULL, class TEXT NOT NULL, cx INTEGER NOT NULL, cy INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0, cell REAL, unit TEXT, ts INTEGER,
    PRIMARY KEY (camera, class, cx, cy));

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, type TEXT, json TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

EVENT_TYPES = ("hazard", "remote_hazard", "movement")
# tabella -> colonna usata dal filtro "kind" di query()
TABLES = {"events": "kind", "tracks": "class", "stats": None, "status": None, "messages": "type"}


def _dumps(msg):
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"), default=str)


def _int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class Storage:
    def __init__(self, path, retention_days=7, outbox_max=OUTBOX_MAX):
        self.path = None if str(path) == ":memory:" else Path(path)
        self.retention_days = retention_days
        self.outbox_max = outbox_max
        self._lock = threading.Lock()
        self._errors = {}
        self._dropped = 0
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = self._open()
        except sqlite3.DatabaseError as e:
            if self.path is None or not self.path.exists():
                raise
            # file rovinato (es. corrente staccata a meta' scrittura): lo metto da parte e riparto
            bad = self.path.with_name(f"{self.path.name}.corrotto-{int(time.time())}")
            print(f"[archivio] {self.path.name} illeggibile ({e}): rinominato in {bad.name}, ne creo uno nuovo")
            os.replace(self.path, bad)
            for suffix in ("-wal", "-shm"):
                p = Path(str(self.path) + suffix)
                if p.exists():
                    os.replace(p, Path(str(bad) + suffix))
            self._conn = self._open()
        self._outbox_n = self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        if self._outbox_n:
            print(f"[archivio] {self._outbox_n} messaggi ancora da inviare dalla sessione precedente")
        self.db_id = self._load_db_id()

    def _load_db_id(self):
        """Identita' numerica di questo file di archivio (scritta alla creazione): va nel cursore di
        export, cosi' il collettore si accorge se l'archivio e' stato ricreato. None se non leggibile."""
        def op(c):
            row = c.execute("SELECT value FROM meta WHERE key = 'db_id'").fetchone()
            if row and str(row[0]).isdigit():
                return int(row[0])
            nuovo = 1 + secrets.randbelow(999_999_999)
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('db_id', ?)", (str(nuovo),))
            return nuovo

        return self._db("db_id", op, None)

    def _open(self):
        conn = sqlite3.connect(":memory:" if self.path is None else str(self.path),
                               check_same_thread=False, timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=WAL")    # lettori (API, export) e scrittore insieme
            conn.execute("PRAGMA synchronous=NORMAL")  # sicuro con WAL e consuma meno la flash
            conn.executescript(SCHEMA)
            return conn
        except Exception:
            conn.close()
            raise

    # ------------------------------------------------------------------ utilita'

    def _err(self, where, e):
        n = self._errors[where] = self._errors.get(where, 0) + 1
        if n <= 3 or n % 100 == 0:  # non riempio la console se l'errore si ripete
            print(f"[archivio] errore in {where} (x{n}): {e!r}")

    def _db(self, where, fn, default=None):
        """Esegue fn(conn) sotto lock in una transazione; in caso di errore log + default."""
        with self._lock:
            if self._conn is None:  # archivio chiuso
                return default
            try:
                with self._conn:  # commit se va tutto bene, rollback se no
                    return fn(self._conn)
            except Exception as e:
                self._err(where, e)
                return default

    # ------------------------------------------------------------------ scrittura

    def save(self, msg):
        """Salva un messaggio nella tabella giusta in base a msg["type"]. Ritorna True se salvato."""
        if not isinstance(msg, dict):
            self._err("save", TypeError(f"messaggio non valido: {type(msg).__name__}"))
            return False
        tipo = msg.get("type")
        ts = _int(msg.get("ts")) or int(time.time() * 1000)

        def op(c):
            js = _dumps(msg)
            if tipo in EVENT_TYPES:
                c.execute("INSERT INTO events(id, ts, type, kind, action, severity, json) VALUES (?,?,?,?,?,?,?)",
                          (msg.get("id"), ts, tipo, msg.get("kind") or tipo, msg.get("action"),
                           _int(msg.get("severity")), js))
            elif tipo == "track":
                c.execute("INSERT INTO tracks(id, ts, ts_start, ts_end, camera, class, from_ap, to_ap, json)"
                          " VALUES (?,?,?,?,?,?,?,?,?)",
                          (msg.get("id"), ts, _int(msg.get("ts_start")), _int(msg.get("ts_end"), ts),
                           msg.get("camera"), msg.get("class"), msg.get("from"), msg.get("to"), js))
            elif tipo == "stats":
                c.execute("INSERT INTO stats(ts, json) VALUES (?,?)", (ts, js))
                self._heatmap_add(c, msg, ts)
            elif tipo == "status":
                c.execute("INSERT INTO status(ts, json) VALUES (?,?)", (ts, js))
            else:  # scene (se richiesta) e tipi futuri
                c.execute("INSERT INTO messages(ts, type, json) VALUES (?,?,?)", (ts, str(tipo), js))
            return True

        return self._db("save", op, False)

    def _heatmap_add(self, c, msg, ts):
        """Somma i conteggi della heatmap del messaggio stats a quelli gia' salvati."""
        for cam, data in (msg.get("cameras") or {}).items():
            if not isinstance(data, dict) or not isinstance(data.get("heatmap"), dict):
                continue
            cell, unit = data.get("heatmap_cell"), data.get("heatmap_unit")
            old = c.execute("SELECT cell, unit FROM heatmap WHERE camera=? LIMIT 1", (cam,)).fetchone()
            if old and (old[1] != unit or (old[0] is not None and cell is not None
                                          and abs(float(old[0]) - float(cell)) > 1e-9)):
                # celle di dimensione diversa (es. camera appena calibrata): non si possono sommare
                c.execute("DELETE FROM heatmap WHERE camera=?", (cam,))
                print(f"[archivio] heatmap di {cam} azzerata: cella cambiata ({old[0]} {old[1]} -> {cell} {unit})")
            rows = []
            for cls, cells in data["heatmap"].items():
                for key, n in (cells or {}).items():
                    try:
                        cx, cy = (int(float(v)) for v in str(key).split(","))
                        rows.append((cam, cls, cx, cy, int(n), cell, unit, ts))
                    except (TypeError, ValueError):
                        continue  # chiave cella malformata: la salto
            c.executemany(
                "INSERT INTO heatmap(camera, class, cx, cy, count, cell, unit, ts) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(camera, class, cx, cy) DO UPDATE SET count = count + excluded.count,"
                " cell = excluded.cell, unit = excluded.unit, ts = excluded.ts", rows)

    # ------------------------------------------------------------------ lettura

    def query(self, table, since_ms=0, limit=100, kind=None, msg_type=None):
        """Gli ultimi `limit` messaggi con ts >= since_ms, in ordine di ts crescente.

        kind filtra per kind (events), per classe (tracks) o per tipo (messages);
        msg_type filtra il tipo nella tabella events (hazard / remote_hazard / movement).
        limit None o <= 0 = tutti.
        """
        if table not in TABLES:
            self._err("query", ValueError(f"tabella sconosciuta: {table!r}"))
            return []
        where, args = ["ts >= ?"], [_int(since_ms, 0)]
        if kind is not None and TABLES[table]:
            where.append(f"{TABLES[table]} = ?")
            args.append(kind)
        if msg_type is not None and table == "events":
            where.append("type = ?")
            args.append(msg_type)
        n = -1 if limit is None else _int(limit, 100)
        args.append(n if n > 0 else -1)  # LIMIT -1 = nessun limite
        sql = (f"SELECT json FROM (SELECT rid, ts, json FROM {table} WHERE {' AND '.join(where)}"
               f" ORDER BY ts DESC, rid DESC LIMIT ?) ORDER BY ts, rid")
        rows = self._db("query", lambda c: c.execute(sql, args).fetchall(), [])
        return [m for m in (self._loads(r[0]) for r in rows) if m is not None]

    def latest(self, table):
        """L'ultimo messaggio di una tabella (es. lo stato piu' recente) o None."""
        if table not in TABLES:
            return None
        row = self._db("latest", lambda c: c.execute(
            f"SELECT json FROM {table} ORDER BY ts DESC, rid DESC LIMIT 1").fetchone())
        return self._loads(row[0]) if row else None

    def heatmap(self, camera=None, cls=None):
        """Conteggi cumulativi per cella: [{"camera","class","cx","cy","count","cell","unit"}]."""
        where, args = [], []
        if camera is not None:
            where.append("camera = ?")
            args.append(camera)
        if cls is not None:
            where.append("class = ?")
            args.append(cls)
        sql = ("SELECT camera, class, cx, cy, count, cell, unit FROM heatmap"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY camera, class, cy, cx")
        rows = self._db("heatmap", lambda c: c.execute(sql, args).fetchall(), [])
        return [{"camera": r[0], "class": r[1], "cx": r[2], "cy": r[3], "count": r[4],
                 "cell": r[5], "unit": r[6]} for r in rows]

    # ------------------------------------------------------------------ export (pull del collettore)

    @staticmethod
    def parse_cursor(cursor):
        """Cursore -> ({lettera: rid}, db_id o None). Malformato o vuoto -> ({}, None) = dall'inizio.
        Le tabelle che mancano nel cursore partono da 0; lettere sconosciute (tabelle future) ignorate."""
        if not isinstance(cursor, str) or not cursor.strip() or len(cursor) > 200:
            return {}, None
        pos, db_id = {}, None
        for tok in cursor.strip().split("."):
            m = _CURSOR_TOKEN.fullmatch(tok.strip())
            if m is None:
                return {}, None
            lettera, n = m.group(1), int(m.group(2))
            if lettera == "d":
                db_id = n
            else:
                pos[lettera] = n
        return pos, db_id

    def make_cursor(self, pos):
        parts = [f"d{self.db_id}"] if self.db_id else []
        parts += [f"{lettera}{max(0, _int(pos.get(lettera), 0))}" for _, lettera in EXPORT_TABLES]
        return ".".join(parts)

    def export(self, cursor=None, limit=500, max_bytes=EXPORT_MAX_BYTES):
        """Elementi salvati dopo il cursore, in ordine di arrivo (ts, poi tabella, poi rid).

        Ritorna {"cursor": nuovo cursore, "more": ci sono altri elementi, "items": [{"table", "rid",
        "ts", "msg"}], "count", "reset": [tabelle ripartite da capo]} e, se l'archivio e' in errore,
        "error" con lo stesso cursore (il collettore riprovera'). Per ogni tabella gli elementi
        restituiti sono sempre i primi dopo il cursore (in ordine di rid), quindi il cursore nuovo
        non salta mai nulla anche se i ts non sono in ordine. Mai eccezioni."""
        limit = min(max(_int(limit, 500) or 1, 1), EXPORT_LIMIT_MAX)
        max_bytes = max(1, _int(max_bytes, EXPORT_MAX_BYTES) or EXPORT_MAX_BYTES)
        pos, db_id = self.parse_cursor(cursor)
        reset_all = bool(pos) and db_id is not None and self.db_id is not None and db_id != self.db_id
        if reset_all:  # cursore di un altro archivio (file ricreato): riparto dall'inizio
            pos = {}
        skip = ",".join("?" * len(EXPORT_SKIP_TYPES))

        def filtro(table):
            if table == "messages":  # niente scene: una al secondo riempirebbe ogni pagina
                return f" AND type NOT IN ({skip})", list(EXPORT_SKIP_TYPES)
            return "", []

        def op(c):
            reset = [t for t, _ in EXPORT_TABLES] if reset_all else []
            heads = []  # [tabella, lettera, dopo, righe [(rid, ts, byte)], prossima riga da prendere]
            for table, lettera in EXPORT_TABLES:
                dopo = max(0, _int(pos.get(lettera), 0))
                top = c.execute(f"SELECT MAX(rid) FROM {table}").fetchone()[0] or 0
                if dopo > top:  # tabella svuotata dalla pulizia o archivio diverso: riparto da capo
                    if table not in reset:
                        reset.append(table)
                    dopo = 0
                extra, args = filtro(table)
                righe = c.execute(f"SELECT rid, ts, length(json) FROM {table} WHERE rid > ?{extra}"
                                  f" ORDER BY rid LIMIT ?", [dopo] + args + [limit + 1]).fetchall()
                heads.append([table, lettera, dopo, righe, 0])
            # fusione per ts delle teste delle tabelle: ogni tabella resta in ordine di rid
            ordine, totale = [], 0
            while len(ordine) < limit:
                cand = [(h[3][h[4]][1] or 0, k) for k, h in enumerate(heads) if h[4] < len(h[3])]
                if not cand:
                    break
                h = heads[min(cand)[1]]
                rid, ts, nbyte = h[3][h[4]]
                if ordine and totale + (nbyte or 0) > max_bytes:
                    break  # pagina piena: il resto alla prossima richiesta
                totale += nbyte or 0
                ordine.append((h[0], rid, ts or 0))
                h[4] += 1
            more = any(h[4] < len(h[3]) for h in heads)
            testi, nuovo = {}, {}
            for table, lettera, dopo, righe, preso in heads:
                nuovo[lettera] = righe[preso - 1][0] if preso else dopo
                if preso:
                    extra, args = filtro(table)
                    for rid, text in c.execute(f"SELECT rid, json FROM {table} WHERE rid > ? AND rid <= ?{extra}",
                                               [dopo, nuovo[lettera]] + args):
                        testi[(table, rid)] = text
            items = []
            for table, rid, ts in ordine:
                msg = self._loads(testi.get((table, rid)))
                if msg is not None:  # riga illeggibile: la salto ma il cursore va avanti
                    items.append({"table": table, "rid": rid, "ts": ts, "msg": msg})
            return {"cursor": self.make_cursor(nuovo), "more": more, "items": items, "count": len(items),
                    "reset": reset}

        res = self._db("export", op, None)
        if res is None:
            return {"cursor": cursor if isinstance(cursor, str) else "", "more": False, "items": [],
                    "count": 0, "reset": [], "error": "archivio non leggibile, riprova"}
        return res

    def _loads(self, text):
        try:
            return json.loads(text)
        except (TypeError, ValueError) as e:
            self._err("json", e)
            return None

    # ------------------------------------------------------------------ coda di invio

    def outbox_put(self, msg):
        """Mette un messaggio in coda verso il server. Ritorna True se accodato."""
        def op(c):
            c.execute("INSERT INTO outbox(ts, type, json) VALUES (?,?,?)",
                      (_int(msg.get("ts")) or int(time.time() * 1000), msg.get("type"), _dumps(msg)))
            n, tolti = self._outbox_n + 1, 0
            if n > self.outbox_max:  # coda piena (server giu' da molto): via i piu' vecchi
                tolti = max(c.execute("DELETE FROM outbox WHERE id IN (SELECT id FROM outbox ORDER BY id LIMIT ?)",
                                      (n - self.outbox_max,)).rowcount, 0)
            self._outbox_n = n - tolti  # aggiornato solo se la transazione non e' fallita prima
            if tolti:
                self._dropped += tolti
                if self._dropped == tolti or self._dropped % 1000 < tolti:
                    print(f"[archivio] coda di invio piena ({self.outbox_max}): scartati {self._dropped} "
                          f"messaggi piu' vecchi (restano nell'archivio locale)")
            return True

        if not isinstance(msg, dict):
            return False
        return self._db("outbox_put", op, False)

    def outbox_take(self, n):
        """I messaggi piu' vecchi in coda come [(id, msg)], senza toglierli (vedi outbox_ack)."""
        rows = self._db("outbox_take", lambda c: c.execute(
            "SELECT id, json FROM outbox ORDER BY id LIMIT ?", (max(1, _int(n, 1)),)).fetchall(), [])
        out, bad = [], []
        for rid, text in rows:
            m = self._loads(text)
            if m is None:
                bad.append(rid)  # riga illeggibile: non la mando, ma non deve bloccare la coda
            else:
                out.append((rid, m))
        if bad:
            self.outbox_ack(bad)
        return out

    def outbox_ack(self, ids):
        """Toglie dalla coda i messaggi arrivati al server."""
        ids = [i for i in (ids or []) if isinstance(i, int)]

        def op(c):
            tolti = 0
            for k in range(0, len(ids), 500):  # limite di parametri di SQLite
                part = ids[k:k + 500]
                cur = c.execute(f"DELETE FROM outbox WHERE id IN ({','.join('?' * len(part))})", part)
                tolti += max(cur.rowcount, 0)
            self._outbox_n = max(0, self._outbox_n - tolti)
            return tolti

        return self._db("outbox_ack", op, 0) if ids else 0

    def outbox_size(self):
        return self._outbox_n

    # ------------------------------------------------------------------ manutenzione

    def size_bytes(self):
        """Spazio occupato su disco (database + file WAL)."""
        try:
            if self.path is None:
                row = self._db("size", lambda c: c.execute(
                    "SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()").fetchone())
                return int(row[0]) if row else 0
            return sum(os.path.getsize(p) for p in (str(self.path), f"{self.path}-wal", f"{self.path}-shm")
                       if os.path.exists(p))
        except Exception as e:
            self._err("size_bytes", e)
            return 0

    def cleanup(self, now_s):
        """Cancella le righe piu' vecchie di retention_days (la coda di invio e la heatmap no)."""
        try:
            days = float(self.retention_days or 0)
        except (TypeError, ValueError):
            days = 0
        if days <= 0:  # 0 = tieni tutto
            return 0
        cutoff = int((now_s - days * 86400) * 1000)

        def op(c):
            # l'ultima riga di ogni tabella resta: cosi' i rid non ripartono da 1 e i cursori di
            # export del collettore restano validi anche dopo una pulizia che svuoterebbe la tabella
            return sum(max(c.execute(f"DELETE FROM {t} WHERE ts < ? AND rid < (SELECT MAX(rid) FROM {t})",
                                     (cutoff,)).rowcount, 0) for t in TABLES)

        tolti = self._db("cleanup", op, 0)
        if tolti:
            print(f"[archivio] pulizia: cancellate {tolti} righe piu' vecchie di {days:g} giorni")
            self._checkpoint()
        return tolti

    def _checkpoint(self):
        """Riporta nel file principale il contenuto del WAL e lo azzera (il WAL non cresce all'infinito)."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception as e:
                    self._err("checkpoint", e)

    def close(self):
        self._checkpoint()
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception as e:
                    self._err("close", e)
                self._conn = None
