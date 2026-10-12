"""Protocollo radio SC-P2P v2: incroci, CROSS Tag e collettore si parlano direttamente, senza server.

CROSS v2 (docs/ARCHITETTURA.md §3): ogni incrocio funziona da solo e avvisa i vicini via radio;
gli utenti della strada (pedoni, non vedenti, ciclisti, conducenti, soccorso) portano un CROSS Tag
che parla con l'incrocio piu' vicino sulla stessa radio. Il collettore e' solo un ascoltatore che
ogni tanto chiede un riepilogo (rq -> rs).

Trasporti (p2p.transports, uno o piu' per nodo, stesso messaggio su tutti):
  - serial: ESP32 "radio" in USB (ESP-NOW <-> seriale, canale Wi-Fi 1, 115200 baud). Una riga JSON
    per messaggio; in ricezione la radio antepone l'RSSI: "R-63 {...}". Le righe che iniziano con
    "#" sono di stato, quelle senza "{" sono log del boot. p2p.serial.port: "/dev/ttyACM0",
    "auto" (primo /dev/ttyACM* o /dev/ttyUSB*) oppure "socket://host:porta" (seriale su TCP, es.
    ser2net o una radio di prova: utile anche sul PC). Solo Linux per le porte vere (UNO Q).
  - udp: multicast 239.255.42.99:47999 con TTL 1 (solo la rete locale) + peer unicast "ip:porta"
    per le reti che bloccano il multicast. Anche qui un datagramma puo' iniziare con "R<rssi> ".
  - sim: bus radio virtuale del simulatore (SimRadioBus): consegna agli endpoint entro range_m con
    RSSI calcolato dalla distanza (-40 - 25*log10(d), d minimo 1 m).
Il nodo che riceve da un trasporto un messaggio da rilanciare (h > 0) lo rilancia su TUTTI i suoi
trasporti: cosi' fa da ponte serial <-> udp <-> sim. Un rilancio di un messaggio arrivato in UDP
non torna al gruppo multicast (l'hanno gia' sentito tutti), solo ai peer unicast.

Messaggi (JSON compatto UTF-8, massimo 240 byte: sta in un frame ESP-NOW da 250; "v": 2):
  hb   nodo -> nodi      {"t":"hb","n","ts","pw":"s|a|c","h":0}          ogni hb_sleep_s / hb_active_s
  hz   nodo -> nodi      {"t":"hz","id","n","a":"n|c","k","c","s","ts","e","hd"?,"to"?,"tg"?,"h"}
  vru  Tag -> nodi       {"t":"vru","d","p":ruolo,"q","b"?,"hd"?,"sp"?}   ogni 1 s, nessun orologio
  emv  Tag soccorso      {"t":"emv","d","q","hd"?,"nx"?,"st":"run"?}      ogni 0,5 s
  adv  nodo -> Tag       {"t":"adv","n","id","r"|"to","c","s","x","k"?,"ttl"}
  rq   collettore -> nodi {"t":"rq","id","w":"sum"}
  rs   nodo -> collettore {"t":"rs","id","n","p":{...},"h":0}
Regole:
  - firma (se c'e' una chiave condivisa p2p.key): sig = primi 16 hex di HMAC-SHA256(chiave, JSON
    canonico senza "sig" e "h", chiavi ordinate, separatori (",", ":"), UTF-8 non escapato). La
    firmano i messaggi dei nodi e del collettore (hb hz adv rq rs); con la chiave quelli non firmati
    o con firma sbagliata si scartano. I Tag non firmano: vru/emv si accettano senza firma (se
    firmati, la firma va verificata);
  - dimensione: oltre 240 byte prima si accorcia l'id di un hz senza perdere nulla: se inizia con
    "<n>-" diventa "~<resto>" e chi riceve lo ricostruisce (expand_id); poi si tolgono "tg", "hd",
    "sp", "b", "nx", poi si accorciano le liste "to" e "r" dalla fine (resta almeno il primo), poi
    le liste del riepilogo "p", poi "k". Il destinatario accetta qualunque sottoinsieme dei campi
    facoltativi;
  - dedup: hz per (id, a), rq per id, rs per (id, n), ricordati per max(scadenza, 60 s). I beacon
    (senza id) non si deduplicano e non si rilanciano mai;
  - rilancio: se h > 0 e mai visto, il nodo ritrasmette lo stesso oggetto con h - 1 (la firma resta
    valida: "h" non e' firmato). I Tag non rilanciano mai;
  - consegna di un hz al nodo: se il nodo e' in "to"; senza "to" se il mittente e' un vicino (dalla
    topologia) o se non ci sono vicini noti. h = numero dei destinatari + 1 (massimo 6): basta per
    arrivare anche a incroci fuori portata radio passando dagli intermedi;
  - "adv" di altri nodi: ignorati dal nodo (sono per i Tag).
Messaggi normalizzati verso on_message (§3.3): remote_hazard, vru_beacon, emergency_beacon,
summary_request. Un beacon e' "near" se rssi >= near_rssi (default -75 dBm) o se il trasporto non
da' l'RSSI. normalize() fa la stessa conversione per chi ascolta e basta (collettore), in piu'
per hb (peer_heartbeat), adv (advice) e rs (summary_response).

Prova da riga di comando (sulla board o sul PC):
  python peer.py listen                         tutto il traffico (ponte e rilanci spenti)
  python peer.py send --kind emergency_vehicle --to incrocio-2 incrocio-3 --heading 90
  python peer.py tag --role blind --device tag-prova --seconds 10     finto CROSS Tag
  python peer.py ask                            chiede il riepilogo ai nodi (rq) e mostra le rs
  aggiungere --serial /dev/ttyACM0 per usare la radio ESP-NOW (non mentre l'app la sta usando).
"""
import hashlib
import hmac
import json
import math
import os
import random
import select
import socket
import struct
import threading
import time
from collections import deque

try:  # stessa cartella: conversione dei punti cardinali e distanze
    from route import compass_deg, dist_m as _dist_m
except Exception:  # route.py mancante o rotto: il protocollo funziona lo stesso
    compass_deg = None

    def _dist_m(a, b):
        dy = (b[0] - a[0]) * 111320
        dx = (b[1] - a[1]) * 111320 * math.cos(math.radians((a[0] + b[0]) / 2))
        return math.hypot(dx, dy)

VERSION = 2
MAX_BYTES = 240            # payload massimo (frame ESP-NOW = 250 byte)
MAX_HOPS = 6               # h massimo (anche per i messaggi ricevuti)
SEEN_MIN_S = 60            # dedup: ricorda gli id almeno per 60 s
SEEN_MAX = 20_000          # protezione da raffiche: oltre si dimenticano i piu' vecchi
TAGS_MAX = 5_000
SKEW_MS = 5_000            # ts sfasato oltre 5 s (orologio non sincronizzato): uso l'ora di ricezione
FAR_RSSI = -95             # RSSI dato dal bus virtuale quando una posizione manca: sentito, mai "vicino"
NODE_TYPES = ("hb", "hz", "adv", "rq", "rs")   # da nodi/collettore: firmati se c'e' la chiave
TAG_TYPES = ("vru", "emv")                     # dai CROSS Tag: firma facoltativa
TYPES = NODE_TYPES + TAG_TYPES
ROLES = ("pedestrian", "blind", "cyclist", "driver", "emergency")
ROLE_ALIASES = {"person": "pedestrian", "pedone": "pedestrian", "vehicle": "driver", "car": "driver",
                "conducente": "driver", "ciclista": "cyclist", "bike": "cyclist", "cieco": "blind",
                "non_vedente": "blind", "ambulance": "emergency", "soccorso": "emergency"}
ALL_TARGETS = ["person", "cyclist", "vehicle"]   # profili interessati (catalogo eventi): "tg" assente = tutti
DROP_ORDER = ("tg", "hd", "sp", "b", "nx")       # campi tolti (in quest'ordine) oltre i 240 byte
TRIM_LISTS = ("to", "r")                         # poi liste accorciate dalla fine
PW_CODES = {"sleep": "s", "active": "a", "cooldown": "c", "s": "s", "a": "a", "c": "c"}
TRANSPORTS = ("serial", "udp", "sim")
P2P_DEFAULTS = {"enabled": False, "transports": ["serial", "udp"], "serial": {"port": "", "baud": 115200},
                "group": "239.255.42.99", "port": 47999, "mcast_ttl": 1, "bind_port": None,
                "peers": [], "key": "", "hb_sleep_s": 10, "hb_active_s": 5, "near_rssi": -75,
                "max_hops": MAX_HOPS, "heartbeat": True}


def _log(text):
    print("[p2p]", text, flush=True)


def _int(v, default=0):
    if isinstance(v, bool):
        return default
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return default


def _num(v, default=None):
    if isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _now_ms():
    return int(time.time() * 1000)


def _dumps(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _str_list(v):
    """"to" puo' essere una lista di id o un id solo."""
    if isinstance(v, str):
        return [v] if v else []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if isinstance(x, (str, int)) and str(x)]
    return []


def _latlon(p):
    if isinstance(p, dict):
        p = (p.get("lat"), p.get("lon"))
    if isinstance(p, (list, tuple)) and len(p) >= 2:
        lat, lon = _num(p[0]), _num(p[1])
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            return (lat, lon)
    return None


def _heading(v):
    """Gradi (numero) o nome del punto cardinale -> gradi interi in [0, 360), oppure None."""
    n = _num(v)
    if n is None and isinstance(v, str) and compass_deg is not None:
        n = compass_deg(v)
    return int(round(n)) % 360 if n is not None else None


def role_of(p):
    """Ruolo del Tag (accetta anche i profili v1: person, vehicle). Un ruolo sconosciuto conta
    come pedone: e' l'utente piu' esposto, meglio un avviso da pedone che nessun avviso."""
    if not isinstance(p, str) or not p:
        return "pedestrian"
    p = p.strip().lower()
    p = ROLE_ALIASES.get(p, p)
    return p if p in ROLES else "pedestrian"


def rssi_from_distance(d_m):
    """RSSI del bus radio virtuale: -40 dBm a 1 m, -25 dB per decade (ambiente urbano)."""
    return int(round(-40 - 25 * math.log10(max(1.0, float(d_m)))))


def split_rssi(raw):
    """b"R-63 {...}" -> (-63, b"{...}"); senza prefisso (None, riga)."""
    s = bytes(raw).strip()
    if s[:1] == b"R":
        sp = s.find(b" ")
        if sp > 1:
            try:
                rssi = int(s[1:sp])
            except ValueError:
                return None, s
            return (rssi if -150 <= rssi <= 20 else None), s[sp + 1:].strip()
    return None, s


# ---------------------------------------------------------------------------- codifica

def sign(obj, key):
    """Primi 16 caratteri hex di HMAC-SHA256(key, JSON canonico senza "sig" e "h")."""
    body = {k: v for k, v in obj.items() if k not in ("sig", "h")}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hmac.new(key.encode("utf-8"), canon, hashlib.sha256).hexdigest()[:16]


def verify(obj, key):
    """Senza chiave accetta tutto; con la chiave serve una firma valida."""
    if not key:
        return True
    sig = obj.get("sig")
    return isinstance(sig, str) and hmac.compare_digest(sig, sign(obj, key))


def compact_id(o):
    """Id di un hz accorciato senza perdite: "incrocio-42-cam0-000123" con n = "incrocio-42" ->
    "~cam0-000123". None se non si puo'."""
    mid, n = o.get("id"), o.get("n")
    if o.get("t") == "hz" and isinstance(mid, str) and isinstance(n, str) and n \
            and mid.startswith(n + "-") and len(mid) > len(n) + 1:
        return "~" + mid[len(n) + 1:]
    return None


def expand_id(o):
    """Id completo di un messaggio ricevuto (inverso di compact_id)."""
    mid, n = o.get("id"), o.get("n")
    if o.get("t") == "hz" and isinstance(mid, str) and mid.startswith("~") and isinstance(n, str) and n:
        return f"{n}-{mid[1:]}"
    return mid


def encode(obj, key="", keep=()):
    """Oggetto -> bytes <= 240 (firmati se c'e' la chiave e il tipo e' di un nodo). Se non ci sta
    toglie/accorcia i campi facoltativi (vedi in testa; mai quelli in keep). None se non ci sta."""
    o = {k: v for k, v in obj.items() if k != "sig"}
    signed = bool(key) and o.get("t") in NODE_TYPES

    def dump():
        if signed:
            o.pop("sig", None)
            o["sig"] = sign(o, key)
        return _dumps(o)

    data = dump()
    if len(data) > MAX_BYTES and "id" not in keep:
        cid = compact_id(o)                    # senza perdite: chi riceve lo ricostruisce
        if cid is not None:
            o["id"] = cid
            data = dump()
    for field in DROP_ORDER:
        if len(data) <= MAX_BYTES:
            return data
        if field in o and field not in keep:
            del o[field]
            data = dump()
    for field in TRIM_LISTS:
        while len(data) > MAX_BYTES and field not in keep and isinstance(o.get(field), list) and len(o[field]) > 1:
            o[field] = o[field][:-1]
            data = dump()
    if len(data) > MAX_BYTES and isinstance(o.get("p"), dict) and "p" not in keep:
        o["p"] = dict(o["p"])                  # riepilogo rs: liste piu' corte, poi campi in meno
        for k in [k for k, v in o["p"].items() if isinstance(v, list)]:
            while len(data) > MAX_BYTES and o["p"][k]:
                o["p"][k] = o["p"][k][:-1]
                data = dump()
        for k in sorted(o["p"], reverse=True):
            if len(data) <= MAX_BYTES:
                break
            if k != "pw":
                del o["p"][k]
                data = dump()
    while len(data) > MAX_BYTES and isinstance(o.get("k"), str) and o["k"] and "k" not in keep:
        o["k"] = o["k"][:max(0, len(o["k"]) - (len(data) - MAX_BYTES))]
        data = dump()
    return data if len(data) <= MAX_BYTES else None


def decode(data):
    """bytes/str -> dict SC-P2P v2, oppure None se non valido. Accetta anche i beacon "vru" v1
    dei vecchi braccialetti (profilo person/cyclist/vehicle)."""
    try:
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8")
        o = json.loads(data)
    except Exception:          # ValueError, UnicodeDecodeError, RecursionError (JSON molto annidato)...
        return None
    if not isinstance(o, dict):
        return None
    if o.get("v") == VERSION and o.get("t") in TYPES:
        return o
    if o.get("v") == 1 and o.get("t") == "vru":
        return {**o, "v": VERSION, "v1": True}
    return None


def hazard_to_p2p(msg, node_id, targets=None, hops=None):
    """Messaggio "hazard" del nodo (catalogo events.py) -> oggetto compatto "hz".
    targets = incroci destinatari ("to"); hops di default = len(targets) + 1, al massimo 6."""
    actor = msg.get("actor") if isinstance(msg.get("actor"), dict) else {}
    ts = _int(msg.get("ts"), _now_ms())
    o = {"v": VERSION, "t": "hz", "id": str(msg.get("id") or f"{node_id}-{ts}"), "n": str(node_id),
         "a": "c" if msg.get("action") == "cancel" else "n",
         "k": str(msg.get("kind") or ""), "c": str(msg.get("category") or "info"),
         "s": max(1, min(3, _int(msg.get("severity"), 1))), "ts": ts,
         "e": _int(msg.get("expires"), ts + 30_000)}
    hd = None
    for v in (msg.get("heading"), actor.get("heading"), actor.get("heading_deg"), actor.get("towards")):
        hd = _heading(v)
        if hd is not None:
            break
    if hd is not None:
        o["hd"] = hd
    to = [x for x in dict.fromkeys(_str_list(targets)) if x != str(node_id)]
    if to:
        o["to"] = to
    tg = [str(x) for x in (msg.get("targets") or []) if x] if isinstance(msg.get("targets"), list) else []
    if tg and not (set(ALL_TARGETS) <= set(tg) or set(ROLES) <= set(tg)):   # tutti = "tg" assente
        o["tg"] = tg
    h = len(to) + 1 if hops is None else _int(hops, len(to) + 1)
    o["h"] = max(0, min(MAX_HOPS, h))
    return o


_KINDS = None


def _cause(kind):
    """causeCode ETSI dal catalogo eventi (se disponibile)."""
    global _KINDS
    if _KINDS is None:
        try:
            from events import KINDS
            _KINDS = KINDS if isinstance(KINDS, dict) else {}
        except Exception:
            _KINDS = {}
    try:
        c = _KINDS[kind][0]
        return c if isinstance(c, int) else None
    except Exception:
        return None


def p2p_to_remote(o, ts, expires, transport, rssi=None):
    """Oggetto "hz" ricevuto -> messaggio "remote_hazard" per il nodo (v1 + "to", "heading")."""
    to = _str_list(o.get("to"))
    tg = [str(x) for x in o.get("tg") if x] if isinstance(o.get("tg"), list) else []
    m = {"v": VERSION, "type": "remote_hazard", "via": "p2p", "transport": transport, "id": expand_id(o),
         "kind": str(o.get("k") or ""), "category": str(o.get("c") or "info"),
         "severity": max(1, min(3, _int(o.get("s"), 1))), "action": "cancel" if o.get("a") == "c" else "new",
         "from_node": o.get("n"), "ts": ts, "expires": expires, "targets": tg or list(ALL_TARGETS),
         "towards": None, "next_nodes": to, "to": to, "heading": _heading(o.get("hd")),
         "rssi": rssi, "hops_left": max(0, min(_int(o.get("h"), 0), MAX_HOPS))}
    cause = _cause(m["kind"])
    if cause is not None:
        m["cause"] = cause
    return m


def hz_times(o, now_ms=None):
    """(ts, scadenza) di un hz in ms; con l'orologio del mittente sfasato oltre 5 s (nodo offline
    senza NTP) i tempi si riportano sul mio: via radio il ritardo e' di millisecondi."""
    now_ms = _now_ms() if now_ms is None else now_ms
    ts = _int(o.get("ts"), now_ms)
    exp = _int(o.get("e"), ts)
    if abs(now_ms - ts) > SKEW_MS:
        ts, exp = now_ms, now_ms + max(0, exp - ts)
    return ts, exp


def beacon_to_msg(o, transport, rssi=None, near_rssi=-75, now_ms=None):
    """Beacon di un CROSS Tag (vru/emv) -> vru_beacon / emergency_beacon (None se non valido).
    near = rssi >= near_rssi, oppure trasporto senza RSSI."""
    dev = o.get("d")
    if not isinstance(dev, str) or not dev or len(dev) > 64:
        return None
    now_ms = _now_ms() if now_ms is None else now_ms
    near = rssi is None or rssi >= near_rssi
    seq = _int(o.get("q"), None) if o.get("q") is not None else None
    if o.get("t") == "vru":
        b = _num(o.get("b"))
        return {"v": VERSION, "type": "vru_beacon", "device": dev, "role": role_of(o.get("p")), "rssi": rssi,
                "near": near, "seq": seq, "battery": int(max(0, min(100, b))) if b is not None else None,
                "heading": _heading(o.get("hd")), "speed": _num(o.get("sp")), "via": transport, "ts": now_ms}
    if o.get("t") == "emv":
        nx = o.get("nx")
        return {"v": VERSION, "type": "emergency_beacon", "device": dev, "role": "emergency",
                "heading": _heading(o.get("hd")), "next_node": str(nx) if isinstance(nx, (str, int)) and str(nx) else None,
                "siren": o.get("st") == "run", "rssi": rssi, "near": near, "seq": seq, "via": transport, "ts": now_ms}
    return None


def normalize(o, transport="udp", rssi=None, near_rssi=-75):
    """Qualsiasi oggetto SC-P2P v2 -> messaggio normalizzato, senza filtri di consegna ne' dedup
    (per chi ascolta e basta, es. il collettore). hz -> remote_hazard, vru/emv -> *_beacon,
    rq -> summary_request, hb -> peer_heartbeat, adv -> advice, rs -> summary_response."""
    try:
        t = o.get("t")
        now = _now_ms()
        if t == "hz":
            if not isinstance(o.get("id"), str):
                return None
            ts, exp = hz_times(o, now)
            return p2p_to_remote(o, ts, exp, transport, rssi)
        if t in TAG_TYPES:
            return beacon_to_msg(o, transport, rssi, near_rssi, now)
        if t == "rq":
            return {"v": VERSION, "type": "summary_request", "id": o.get("id"), "what": str(o.get("w") or "sum"),
                    "from": o.get("n"), "via": transport, "rssi": rssi, "ts": now,
                    "hops_left": max(0, min(_int(o.get("h"), 0), MAX_HOPS))}
        if t == "hb":
            return {"v": VERSION, "type": "peer_heartbeat", "node": o.get("n"), "pw": o.get("pw"),
                    "node_ts": _int(o.get("ts"), None), "via": transport, "rssi": rssi, "ts": now}
        if t == "adv":
            return {"v": VERSION, "type": "advice", "node": o.get("n"), "id": o.get("id"),
                    "roles": _str_list(o.get("r")), "device": o.get("to") if isinstance(o.get("to"), str) else None,
                    "code": _int(o.get("c"), None), "severity": _int(o.get("s"), None), "crosswalk": o.get("x"),
                    "kind": o.get("k"), "ttl_ms": _int(o.get("ttl"), None), "via": transport, "rssi": rssi, "ts": now}
        if t == "rs":
            return {"v": VERSION, "type": "summary_response", "id": o.get("id"), "node": o.get("n"),
                    "summary": o.get("p") if isinstance(o.get("p"), dict) else {}, "via": transport,
                    "rssi": rssi, "ts": now}
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------- bus radio virtuale

class SimRadioBus:
    """Radio virtuale per il simulatore e i test: chi trasmette raggiunge tutti gli endpoint entro
    range_m, con RSSI calcolato dalla distanza. Posizioni (lat, lon). Un endpoint senza posizione
    sente e raggiunge tutti con RSSI FAR_RSSI (es. un ascoltatore del collettore).

    Di norma la consegna e' immediata (nel thread di chi trasmette: deliver deve solo accodare).
    Con pause() i frame restano in coda finche' non si chiama flush() o resume(): consegna passo
    passo e deterministica per i test."""

    def __init__(self, range_m=180.0, loss=0.0, seed=None):
        self.range_m = max(0.0, _num(range_m, 180.0))
        self.loss = min(max(_num(loss, 0.0), 0.0), 1.0)
        self.paused = False
        self.counters = dict.fromkeys(("frames", "deliveries", "out_of_range", "lost", "errors"), 0)
        self.recent = deque(maxlen=300)   # ultimi frame: {"ts","from","to","bytes"} (simulatore/dashboard)
        self._rnd = random.Random(seed)
        self._eps = {}                    # endpoint -> [posizione, deliver]
        self._queue = deque()
        self._lock = threading.RLock()
        self._logged = set()

    @staticmethod
    def rssi_at(d_m):
        return rssi_from_distance(d_m)

    def attach(self, endpoint_id, position, deliver):
        with self._lock:
            self._eps[str(endpoint_id)] = [_latlon(position), deliver]

    def detach(self, endpoint_id):
        with self._lock:
            self._eps.pop(str(endpoint_id), None)

    def move(self, endpoint_id, position):
        with self._lock:
            ep = self._eps.get(str(endpoint_id))
            if ep is not None:
                ep[0] = _latlon(position)

    def position(self, endpoint_id):
        with self._lock:
            ep = self._eps.get(str(endpoint_id))
            return ep[0] if ep else None

    def endpoints(self) -> list:
        with self._lock:
            return sorted(self._eps)

    def in_range(self, endpoint_id) -> list:
        """[(endpoint, metri | None, rssi)] raggiungibili da endpoint_id."""
        with self._lock:
            me = self._eps.get(str(endpoint_id))
            pos = me[0] if me else None
            eps = [(k, v[0]) for k, v in self._eps.items() if k != str(endpoint_id)]
        out = []
        for k, p in sorted(eps):
            if pos is None or p is None:
                out.append((k, None, FAR_RSSI))
            else:
                d = _dist_m(pos, p)
                if d <= self.range_m:
                    out.append((k, round(d, 1), rssi_from_distance(d)))
        return out

    def broadcast(self, endpoint_id, data) -> int:
        """Trasmette un frame. Ritorna a quanti endpoint e' stato consegnato (0 se in pausa)."""
        data = bytes(data)
        with self._lock:
            self.counters["frames"] += 1
            if self.paused:
                self._queue.append((str(endpoint_id), data))
                return 0
        return self._deliver(str(endpoint_id), data)

    def pause(self):
        with self._lock:
            self.paused = True

    def resume(self) -> int:
        with self._lock:
            self.paused = False
        return self.flush()

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def flush(self, max_frames=100_000) -> int:
        """Consegna i frame in coda (in ordine). Ritorna quanti frame ha consegnato."""
        n = 0
        while n < max_frames:
            with self._lock:
                if not self._queue:
                    break
                sender, data = self._queue.popleft()
            self._deliver(sender, data)
            n += 1
        return n

    def _deliver(self, sender, data):
        with self._lock:
            me = self._eps.get(sender)
            pos = me[0] if me else None
            eps = [(k, v[0], v[1]) for k, v in self._eps.items() if k != sender]
            dests = []
            for k, p, fn in eps:
                if pos is None or p is None:
                    rssi = FAR_RSSI
                else:
                    d = _dist_m(pos, p)
                    if d > self.range_m:
                        self.counters["out_of_range"] += 1
                        continue
                    rssi = rssi_from_distance(d)
                if self.loss and self._rnd.random() < self.loss:
                    self.counters["lost"] += 1
                    continue
                dests.append((k, fn, rssi))
        n = 0
        for k, fn, rssi in dests:   # fuori dal lock: deliver puo' anche ritrasmettere
            try:
                fn(data, rssi)
                n += 1
            except Exception as e:
                with self._lock:
                    self.counters["errors"] += 1
                if k not in self._logged:
                    self._logged.add(k)
                    _log(f"bus virtuale: consegna a '{k}' fallita ({e!r})")
        with self._lock:
            self.counters["deliveries"] += n
            self.recent.append({"ts": _now_ms(), "from": sender, "to": [d[0] for d in dests], "bytes": len(data)})
        return n


# ---------------------------------------------------------------------------- trasporti

class _Transport:
    name = "?"
    has_rssi = False

    def fileno(self):
        """Oggetto da passare a select (socket o fd), None se non serve."""
        return None

    def read(self):
        """[(payload, rssi | None, indirizzo)] dei messaggi in attesa."""
        return []

    def write(self, data, relay_from=None) -> bool:
        return False

    @property
    def up(self) -> bool:
        return False

    def maintain(self, now):
        pass

    def close(self):
        pass

    def state(self) -> dict:
        return {"up": self.up}


class _UdpTransport(_Transport):
    name = "udp"

    def __init__(self, link):
        c = link.cfg
        self.link = link
        self.group = str(c.get("group") or P2P_DEFAULTS["group"])
        self.port = _int(c.get("port"), 47999)
        if not 0 < self.port < 65536:
            self.port = 47999
        bp = c.get("bind_port")
        self.bind_port = self.port if bp in (None, "") else _int(bp, self.port)
        if not 0 <= self.bind_port < 65536:   # 0 = porta libera qualsiasi (test)
            self.bind_port = self.port
        self.ttl = max(1, _int(c.get("mcast_ttl"), 1))
        self.unicast = []
        self.sock = None
        self.multicast = False
        self._last_join = 0.0
        self._parse_peers(c.get("peers"))
        self._open()

    def _parse_peers(self, peers):
        """Peer unicast "ip:porta" (o solo "ip"). Accetta anche una stringa singola."""
        if isinstance(peers, str):
            peers = [peers]
        for p in peers if isinstance(peers, (list, tuple)) else []:
            try:
                host, sep, port = str(p).strip().rpartition(":")
                if not sep:
                    host, port = port, ""
                port = _int(port, -1) if port else self.port
                if not host or not 0 < port < 65536:
                    raise ValueError("serve ip:porta")
                self.unicast.append((socket.gethostbyname(host), port))
            except Exception as e:
                _log(f"peer '{p}' non valido ({e}): ignorato")

    def _open(self):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            if hasattr(socket, "SIO_UDP_CONNRESET"):   # Windows: niente errori ICMP in ricezione
                try:
                    s.ioctl(socket.SIO_UDP_CONNRESET, False)
                except (OSError, ValueError):
                    pass
            s.bind(("", self.bind_port))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.ttl)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            s.setblocking(False)
            self.sock = s
        except Exception as e:
            if s is not None:
                s.close()
            _log(f"UDP non disponibile sulla porta {self.bind_port} ({e}): continuo senza")
            return
        self._join()

    def _join(self):
        self._last_join = time.time()
        try:
            mreq = struct.pack("4s4s", socket.inet_aton(self.group), socket.inet_aton("0.0.0.0"))
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self.multicast = True
            _log(f"multicast {self.group}:{self.port} attivo (porta locale {self.bind_port})")
        except Exception as e:
            self.link._log_once("join", f"multicast non disponibile ({e}): solo peer unicast, riprovo ogni 30 s")

    @property
    def up(self):
        return self.sock is not None

    def fileno(self):
        return self.sock

    def read(self):
        out, s = [], self.sock
        if s is None:
            return out
        for _ in range(200):
            try:
                data, addr = s.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                break
            except ConnectionResetError:   # Windows: ICMP "porta chiusa" di un invio precedente
                continue
            except OSError:                # socket chiuso nel frattempo
                break
            rssi, payload = split_rssi(data)
            out.append((payload, rssi, f"{addr[0]}:{addr[1]}"))
        return out

    def write(self, data, relay_from=None):
        s = self.sock
        if s is None:
            return False
        # un rilancio di cio' che e' arrivato in UDP non torna al gruppo: l'hanno gia' sentito tutti
        dests = ([] if relay_from == "udp" else [(self.group, self.port)]) + self.unicast
        ok = False
        for addr in dests:
            try:
                s.sendto(data, addr)
                ok = True
            except Exception as e:   # OSError, ma anche socket chiuso nel frattempo
                self.link._log_once(f"tx:{addr}", f"invio a {addr[0]}:{addr[1]} fallito: {e}")
        return ok

    def maintain(self, now):
        if self.sock is not None and not self.multicast and now - self._last_join >= 30:
            self._join()

    def close(self):
        s, self.sock = self.sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    def state(self):
        return {"up": self.up, "multicast": self.multicast, "port": self.port,
                "peers": [f"{h}:{p}" for h, p in self.unicast]}


class _SerialTransport(_Transport):
    """ESP32 radio su seriale (Linux) o su TCP ("socket://host:porta")."""
    name = "serial"
    has_rssi = True

    def __init__(self, link):
        scfg = link.cfg["serial"]
        self.link = link
        self.port_cfg = str(scfg.get("port") or "").strip()
        self.baud = _int(scfg.get("baud"), 115200)
        self.device = None
        self.last_status = None
        self._fd = None
        self._sock = None
        self._buf = b""
        self._retry = 0.0
        self._lock = threading.RLock()
        if not self.port_cfg:
            link._log_once("ser_none", "trasporto serial senza porta (p2p.serial.port): disattivato")
        else:
            self._open()

    @property
    def up(self):
        return self._fd is not None or self._sock is not None

    def fileno(self):
        return self._sock if self._sock is not None else self._fd

    def _open(self):
        port = self.port_cfg
        self._retry = time.time() + 10
        if port.startswith("socket://"):
            self._open_tcp(port[len("socket://"):])
            return
        if os.name != "posix":
            self._retry = math.inf
            self.link._log_once("ser_os", "radio ESP-NOW su seriale supportata solo su Linux (UNO Q): "
                                          "sul PC usare socket://host:porta")
            return
        dev = self._auto() if port == "auto" else port
        if not dev:
            self.link._log_once("ser_auto", "nessuna radio ESP32 trovata (/dev/ttyACM*, /dev/ttyUSB*): riprovo ogni 10 s")
            return
        try:
            import termios
            import tty
            fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                tty.setraw(fd)
                attrs = termios.tcgetattr(fd)
                speed = getattr(termios, f"B{self.baud}", termios.B115200)
                attrs[2] |= termios.CLOCAL | termios.CREAD
                attrs[4] = attrs[5] = speed
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
                termios.tcflush(fd, termios.TCIOFLUSH)
            except Exception:
                os.close(fd)
                raise
            with self._lock:
                self._fd, self._buf, self.device = fd, b"", dev
            self.link._logged.discard("ser_open")
            _log(f"radio ESP-NOW su {dev} a {self.baud} baud")
        except Exception as e:
            self.link._log_once("ser_open", f"seriale {dev} non apribile ({e}): riprovo ogni 10 s")

    @staticmethod
    def _auto():
        import glob
        for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*"):
            found = sorted(glob.glob(pattern))
            if found:
                return found[0]
        return None

    def _open_tcp(self, rest):
        try:
            host, sep, port = rest.rpartition(":")
            port = _int(port, -1)
            if not sep or not host or not 0 < port < 65536:
                raise ValueError("serve socket://host:porta")
            s = socket.create_connection((host, port), timeout=1.0)
            s.setblocking(False)
            try:
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            with self._lock:
                self._sock, self._buf, self.device = s, b"", f"socket://{host}:{port}"
            self.link._logged.discard("ser_open")
            _log(f"radio ESP-NOW su socket://{host}:{port}")
        except Exception as e:
            self.link._log_once("ser_open", f"radio su socket://{rest} non raggiungibile ({e}): riprovo ogni 10 s")

    def close(self, retry=True):
        with self._lock:   # (RLock) mai chiudere mentre un altro thread ci scrive
            fd, self._fd = self._fd, None
            s, self._sock = self._sock, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
        if (fd is not None or s is not None) and retry:
            _log("radio ESP-NOW scollegata: riprovo ogni 10 s")
        self._retry = time.time() + 10 if retry else math.inf

    def maintain(self, now):
        if self.port_cfg and not self.up and now >= self._retry:
            self._open()

    def _read_chunk(self):
        """bytes letti, b"" se non c'e' niente, None se la radio si e' scollegata."""
        try:
            if self._sock is not None:
                chunk = self._sock.recv(4096)
            elif self._fd is not None:
                chunk = os.read(self._fd, 4096)
            else:
                return b""
        except (BlockingIOError, InterruptedError):
            return b""
        except (OSError, TypeError, AttributeError):
            return None
        return chunk if chunk else None   # 0 byte = fine del flusso (radio scollegata)

    def read(self):
        chunk = self._read_chunk()
        if chunk is None:
            self.close()
            return []
        if not chunk:
            return []
        *lines, self._buf = (self._buf + chunk).split(b"\n")
        if len(self._buf) > 2048:      # riga senza fine: spazzatura
            self._buf = b""
        out = []
        for line in lines:
            s = line.strip()
            if not s:
                continue
            if s.startswith(b"#"):     # riga di stato/errore della radio
                self.last_status = s[:120].decode("utf-8", "replace")
                continue
            rssi, payload = split_rssi(s)
            i = payload.find(b"{")     # le altre righe senza "{" sono log del boot
            if i >= 0:
                out.append((payload[i:], rssi, self.device or "serial"))
        return out

    def write(self, data, relay_from=None):
        line, deadline = bytes(data) + b"\n", time.time() + 0.5
        with self._lock:
            if not self.up:
                return False
            try:
                while line:
                    try:
                        if self._sock is not None:
                            line = line[self._sock.send(line):]
                        else:
                            line = line[os.write(self._fd, line):]
                    except BlockingIOError:
                        if time.time() > deadline:
                            raise TimeoutError("buffer della seriale pieno")
                        time.sleep(0.005)
                return True
            except (OSError, TypeError, AttributeError) as e:
                self.link._log_once("ser_tx", f"scrittura sulla radio fallita: {e}")
                self.close()
                return False

    def state(self):
        return {"up": self.up, "device": self.device, "port": self.port_cfg, "status": self.last_status}


class _SimTransport(_Transport):
    name = "sim"
    has_rssi = True

    def __init__(self, link, bus, position):
        self.link = link
        self.bus = bus
        self.position = position
        self.queue = deque(maxlen=5000)
        bus.attach(link.node_id, position, self._deliver)
        self._attached = True

    def _deliver(self, data, rssi):
        self.queue.append((bytes(data), rssi))
        self.link._wake.set()

    @property
    def up(self):
        return self._attached

    def read(self):
        out = []
        while self.queue and len(out) < 1000:
            data, rssi = self.queue.popleft()
            out.append((data, rssi, "sim"))
        return out

    def write(self, data, relay_from=None):
        if not self._attached:
            return False
        self.bus.broadcast(self.link.node_id, data)
        return True

    def close(self):
        if self._attached:
            self._attached = False
            try:
                self.bus.detach(self.link.node_id)
            except Exception:
                pass

    def state(self):
        return {"up": self.up, "position": list(self.position) if self.position else None,
                "range_m": getattr(self.bus, "range_m", None)}


# ---------------------------------------------------------------------------- collegamento

class PeerLink:
    """Canale radio del nodo: pericoli tra incroci, beacon dei CROSS Tag, avvisi verso i Tag,
    richieste del collettore. Nessun metodo solleva eccezioni: se un trasporto non e' disponibile
    si logga una volta e si continua con gli altri.

    on_message(dict) riceve i messaggi normalizzati (remote_hazard, vru_beacon, emergency_beacon,
    summary_request). monitor (attributo, facoltativo): monitor(obj, transport, rssi) per ogni
    messaggio valido ricevuto, prima di ogni filtro (riga di comando, collettore in ascolto).
    neighbors: id degli incroci vicini; in alternativa topology (route.Topology) da cui si
    ricavano vicini e posizione."""

    def __init__(self, cfg, node_id, on_message, start_thread=True, sim_bus=None, position=None,
                 neighbors=None, topology=None):
        cfg = cfg if isinstance(cfg, dict) else {}
        ser = cfg.get("serial")
        ser = {"port": ser} if isinstance(ser, str) else (ser if isinstance(ser, dict) else {})
        c = {**P2P_DEFAULTS, **cfg, "serial": {**P2P_DEFAULTS["serial"], **ser}}
        self.cfg = c
        self.node_id = str(node_id)
        self.on_message = on_message
        self.monitor = None
        self.enabled = bool(c.get("enabled"))
        self.key = str(c.get("key") or "")
        self.near_rssi = _num(c.get("near_rssi"), -75.0)
        self.hb_sleep_s = max(0.5, _num(c.get("hb_sleep_s"), 10.0))
        self.hb_active_s = max(0.2, _num(c.get("hb_active_s", c.get("heartbeat_s")), 5.0))
        self.max_hops = max(0, min(_int(c.get("max_hops"), MAX_HOPS), MAX_HOPS))
        self.relay = True              # False = solo ascolto (riga di comando, collettore)
        self.heartbeat = bool(c.get("heartbeat", True))   # False = non si annuncia (ascoltatori)
        self.power_state = "a"
        self.neighbors = set()
        self.position = _latlon(position)
        self.counters = dict.fromkeys(("tx", "rx", "delivered", "relayed", "bridged", "dup", "ignored", "own",
                                       "beacons", "bad_sig", "invalid"), 0)
        self.transports = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._seen = {}                # chiave di dedup -> scadenza (s)
        self._peers = {}               # node_id -> {"last_seen", "pw", "rssi", "via"}
        self._tags = {}                # device -> {"role", "last_seen", "rssi", "near", "via"}
        self._logged = set()
        self._last_hb = None           # ultimo heartbeat, con l'orologio di tick (reale o simulato)
        self._tick_now = None          # ultimo "now" passato a tick
        self._last_prune = 0.0         # pulizia della memoria: sempre con l'orologio reale
        self._thread = None
        try:
            if topology is not None:
                self.set_topology(topology)
            if neighbors is not None:
                self.set_neighbors(neighbors)
        except Exception as e:
            _log(f"vicini non leggibili ({e!r}): consegna a tutti i mittenti")
        if not self.enabled:
            return
        try:
            for name in self._transport_names(c.get("transports"), sim_bus):
                try:
                    if name == "udp":
                        self.transports.append(_UdpTransport(self))
                    elif name == "serial":
                        self.transports.append(_SerialTransport(self))
                    elif name == "sim":
                        if self.position is None:
                            _log(f"'{self.node_id}' senza posizione sul bus virtuale: sente e raggiunge tutti")
                        self.transports.append(_SimTransport(self, sim_bus, self.position))
                except Exception as e:
                    _log(f"trasporto {name} non disponibile ({e!r}): continuo senza")
            if start_thread:
                self._thread = threading.Thread(target=self._run, name="p2p", daemon=True)
                self._thread.start()
        except Exception as e:  # mai bloccare l'avvio del nodo per la radio
            _log(f"avvio P2P incompleto ({e!r}): continuo con quello che funziona")

    def _transport_names(self, names, sim_bus):
        if isinstance(names, str):
            names = names.replace(",", " ").split()
        names = [str(n).strip().lower() for n in (names if isinstance(names, (list, tuple)) else [])]
        out = []
        for n in names:
            if n not in TRANSPORTS:
                _log(f"trasporto '{n}' sconosciuto (validi: {', '.join(TRANSPORTS)}): ignorato")
            elif n not in out:
                out.append(n)
        if sim_bus is not None and "sim" not in out:
            _log("bus radio virtuale: uso solo il trasporto sim")
            out = ["sim"]
        if "sim" in out and sim_bus is None:
            _log("trasporto sim senza bus radio virtuale: ignorato")
            out.remove("sim")
        if not out:
            _log("nessun trasporto attivo: il nodo non parla con nessuno")
        return out

    def set_neighbors(self, neighbors):
        """Incroci vicini (lista di id; per compatibilita' anche {via: id})."""
        if isinstance(neighbors, dict):
            neighbors = neighbors.values()
        if isinstance(neighbors, str):
            neighbors = [neighbors]
        self.neighbors = {str(n) for n in (neighbors or ()) if n and str(n) != self.node_id}

    def set_topology(self, topology):
        """Vicini (e posizione, se manca) dalla topologia della citta'. Si puo' richiamare quando
        la topologia viene ricaricata."""
        self.set_neighbors(topology.neighbors(self.node_id))
        if self.position is None and hasattr(topology, "pos"):
            self.position = _latlon(topology.pos(self.node_id))
            for t in self.transports:   # posizione arrivata dopo l'avvio: aggiorno il bus virtuale
                if isinstance(t, _SimTransport) and self.position is not None:
                    t.position = self.position
                    t.bus.move(self.node_id, self.position)

    # ------------------------------------------------------------------ API

    def send_hazard(self, msg, targets=None, hops=None) -> bool:
        """Trasmette un "hazard" locale agli incroci. targets = destinatari ("to", calcolati da
        route.targets_for); hops di default len(targets) + 1 (massimo 6). Parte solo se il pericolo
        va inoltrato (propagate, default vero) o e' un'emergenza. Mai eccezioni."""
        try:
            if not self.enabled or self._stop.is_set() or not isinstance(msg, dict) \
                    or msg.get("type", "hazard") != "hazard":
                return False
            if msg.get("propagate", True) is False and msg.get("category") != "emergency":
                return False
            o = hazard_to_p2p(msg, self.node_id, targets, hops)
            o["h"] = min(o["h"], self.max_hops)
            return self._send_obj(o)
        except Exception as e:
            self._log_once("send_hazard", f"invio pericolo fallito: {e!r}")
            return False

    def send_obj(self, obj, keep=()) -> bool:
        """Trasmette un oggetto gia' nel formato v2 (adv, rs, rq, ...). Completa "v" e, per i tipi
        dei nodi, "n"; hb e rs partono con h = 0 se non indicato. Mai eccezioni."""
        try:
            if not self.enabled or self._stop.is_set():
                return False
            if not isinstance(obj, dict) or obj.get("t") not in TYPES:
                self._log_once("send_obj_t", f"tipo di messaggio non valido: {str(obj)[:80]}")
                return False
            o = dict(obj)
            o["v"] = VERSION
            if o["t"] in NODE_TYPES:
                o.setdefault("n", self.node_id)
            if o["t"] in ("hb", "rs"):
                o.setdefault("h", 0)
            if "h" in o:
                o["h"] = max(0, min(_int(o["h"], 0), self.max_hops))
            return self._send_obj(o, keep)
        except Exception as e:
            self._log_once("send_obj", f"invio {obj.get('t') if isinstance(obj, dict) else '?'} fallito: {e!r}")
            return False

    def send_heartbeat(self, power_state="a") -> bool:
        """Heartbeat con lo stato di energia ("s"/"a"/"c" o "sleep"/"active"/"cooldown"), che resta
        quello dei heartbeat periodici successivi."""
        try:
            self.power_state = PW_CODES.get(str(power_state).lower(), self.power_state)
            self._last_hb = self._tick_now if self._tick_now is not None else time.time()
            if not self.enabled or self._stop.is_set():
                return False
            o = {"v": VERSION, "t": "hb", "n": self.node_id, "ts": _now_ms(), "pw": self.power_state, "h": 0}
            return self._send_obj(o)
        except Exception as e:
            self._log_once("heartbeat", f"heartbeat non inviato: {e!r}")
            return False

    def forward_remote(self, msg) -> bool:
        """Compatibilita' v1 (inoltro dei pericoli arrivati dal server): nella v2 non c'e' server,
        quindi non fa niente. Resta solo perche' un node.py non ancora aggiornato non si fermi."""
        return False

    def set_power(self, state) -> bool:
        """Nuovo stato di energia: se cambia, heartbeat subito (i vicini lo sanno senza aspettare)."""
        pw = PW_CODES.get(str(state).lower())
        if pw is None or pw == self.power_state:
            return False
        return self.send_heartbeat(pw)

    def poll_once(self, timeout=0.0) -> int:
        """Elabora quello che e' arrivato su tutti i trasporti, aspettando al massimo timeout s.
        Ritorna quanti messaggi validi ha letto."""
        if not self.enabled or self._stop.is_set():
            return 0
        n = 0
        try:
            self._wake.clear()
            n += self._drain_queues()
            wait = 0.0 if n else max(0.0, _num(timeout, 0.0))
            has_queue = any(t.fileno() is None and t.up for t in self.transports)
            sel = [t for t in self.transports if t.fileno() is not None]
            if sel:
                if has_queue:
                    wait = min(wait, 0.02)   # i frame del bus virtuale non svegliano select
                fds = [t.fileno() for t in sel]
                ready, _, _ = select.select(fds, [], [], wait)
                for t in sel:
                    fd = t.fileno()
                    if fd is not None and any(fd is r or fd == r for r in ready):
                        for payload, rssi, addr in t.read():
                            n += self._handle(payload, t.name, rssi, addr)
            elif has_queue and wait > 0:
                self._wake.wait(wait)
            elif wait > 0:
                self._stop.wait(wait)
            n += self._drain_queues()
        except Exception as e:
            if not self._stop.is_set():
                self._log_once("poll", f"errore in ricezione: {e!r}")
        return n

    def tick(self, now=None):
        """Manutenzione: heartbeat periodico, riapertura della seriale, multicast, pulizia della
        memoria. La chiama il thread; senza thread (simulatore, test) la chiama chi usa il link.
        now (default time.time()) e' l'orologio del ritmo dei heartbeat e della manutenzione: il
        simulatore puo' passare il suo tempo; dedup e registri usano sempre l'orologio reale."""
        now = time.time() if now is None else _num(now, time.time())
        self._tick_now = now
        if self.enabled and not self._stop.is_set():
            every = self.hb_sleep_s if self.power_state == "s" else self.hb_active_s
            last = self._last_hb
            # mai inviato, periodo scaduto o orologio tornato indietro (cambio di orologio): invio
            if self.heartbeat and (last is None or now < last or now - last >= every):
                self.send_heartbeat(self.power_state)
            for t in self.transports:
                try:
                    t.maintain(now)
                except Exception as e:
                    self._log_once(f"maint:{t.name}", f"manutenzione {t.name} fallita: {e!r}")
        real = time.time()
        if real - self._last_prune >= 10:
            self._last_prune = real
            with self._lock:
                self._seen = {k: v for k, v in self._seen.items() if v > real}
                for d in [d for d, v in self._tags.items() if real - v["last_seen"] > 600]:
                    self._tags.pop(d, None)
                for p in [p for p, v in self._peers.items() if real - v["last_seen"] > 3600]:
                    self._peers.pop(p, None)

    def peers(self) -> dict:
        """Incroci sentiti direttamente (heartbeat): node_id -> {"last_seen","pw","rssi","via"}."""
        with self._lock:
            return {k: dict(v) for k, v in self._peers.items()}

    def vrus(self) -> dict:
        """CROSS Tag sentiti di recente: device -> {"role","last_seen","rssi","near","via"}."""
        with self._lock:
            return {k: dict(v) for k, v in self._tags.items()}

    @property
    def online(self) -> bool:
        """Almeno un incrocio sentito negli ultimi 3 heartbeat (con il ritmo piu' lento, a riposo)."""
        now = time.time()
        window = 3 * max(self.hb_sleep_s, self.hb_active_s)
        with self._lock:
            return any(now - p["last_seen"] <= window for p in self._peers.values())

    def state(self) -> dict:
        """Riassunto per lo stato del nodo."""
        tr = {}
        for t in self.transports:
            try:
                tr[t.name] = t.state()
            except Exception as e:
                tr[t.name] = {"up": False, "error": repr(e)}
        udp = tr.get("udp") or {}
        return {"enabled": self.enabled, "online": self.online, "power": self.power_state,
                "transports": tr, "udp": bool(udp.get("up")), "multicast": bool(udp.get("multicast")),
                "serial": bool((tr.get("serial") or {}).get("up")), "sim": bool((tr.get("sim") or {}).get("up")),
                "neighbors": sorted(self.neighbors), "peers": sorted(self.peers()),
                "tags": len(self._tags), "counters": dict(self.counters)}

    def close(self):
        self._stop.set()
        self._wake.set()
        try:
            t = self._thread
            if t is not None and t is not threading.current_thread():   # close() dentro on_message
                t.join(timeout=2.0)
        except Exception as e:
            _log(f"chiusura del thread: {e!r}")
        for t in self.transports:
            try:
                if isinstance(t, _SerialTransport):
                    t.close(retry=False)
                else:
                    t.close()
            except Exception as e:
                _log(f"chiusura {t.name}: {e!r}")

    # ------------------------------------------------------------------ ricezione

    def _drain_queues(self):
        n = 0
        for t in self.transports:
            if t.fileno() is None and t.up:
                for payload, rssi, addr in t.read():
                    n += self._handle(payload, t.name, rssi, addr)
        return n

    def _handle(self, data, transport, rssi=None, addr=None):
        o = decode(data)
        if o is None:
            self.counters["invalid"] += 1
            return 0
        self.counters["rx"] += 1
        if self.monitor is not None:
            try:
                self.monitor(o, transport, rssi)
            except Exception as e:
                self._log_once("monitor", f"errore nel monitor: {e!r}")
        try:
            t = o["t"]
            if t in TAG_TYPES:
                self._on_tag(o, transport, rssi)
                return 1
            n = o.get("n")
            if n is not None and not isinstance(n, str):
                self.counters["invalid"] += 1
                return 1
            if n == self.node_id:
                self.counters["own"] += 1     # mio (loopback multicast o rilancio altrui)
                return 1
            if not n and t != "rq":
                self.counters["invalid"] += 1
                return 1
            if not verify(o, self.key):
                self.counters["bad_sig"] += 1
                self._log_once(f"sig:{n}", f"firma mancante o errata da '{n}' via {transport} ({addr}): scartato")
                return 1
            if t == "hb":
                self._on_hb(o, transport, rssi)
            elif t == "hz":
                self._on_hz(o, transport, rssi)
            elif t == "rq":
                self._on_rq(o, transport, rssi)
            elif t == "rs":
                self._on_rs(o, transport)
            else:                             # adv di un altro nodo: e' per i Tag
                self.counters["ignored"] += 1
        except Exception as e:
            self.counters["invalid"] += 1
            self._log_once("handle", f"messaggio non gestibile ({e!r}): {bytes(data)[:80]!r}")
        return 1

    def _remember(self, key, until):
        """Da chiamare con il lock preso."""
        self._seen[key] = until
        if len(self._seen) > SEEN_MAX:
            for k in list(self._seen)[:len(self._seen) - SEEN_MAX]:
                self._seen.pop(k, None)

    def _hops(self, o):
        return max(0, min(_int(o.get("h"), 0), MAX_HOPS))

    def _relay(self, o, h, transport):
        data = _dumps({**o, "h": h - 1})    # stessa firma: "h" non e' firmato
        if len(data) > MAX_BYTES:
            self._log_once("relay_size", f"messaggio {o.get('id')} oltre {MAX_BYTES} byte: non rilanciato")
            return
        if self._send_bytes(data, relay_from=transport):
            self.counters["relayed"] += 1
            if any(t.name != transport for t in self.transports):
                self.counters["bridged"] += 1

    def _on_hb(self, o, transport, rssi):
        pw = o.get("pw") if o.get("pw") in ("s", "a", "c") else None
        with self._lock:
            self._peers[o["n"]] = {"last_seen": time.time(), "pw": pw, "rssi": rssi, "via": transport}

    def _on_hz(self, o, transport, rssi):
        mid, act = expand_id(o), o.get("a")
        if not isinstance(mid, str) or not mid or mid == "~" or act not in ("n", "c"):
            self.counters["invalid"] += 1
            return
        now = time.time()
        ts, exp = hz_times(o, int(now * 1000))
        h = self._hops(o)
        relay = self.relay and h > 0
        relevant = self._relevant(o)
        key = ("hz", mid, act)
        with self._lock:
            if key in self._seen:
                self.counters["dup"] += 1
                return
            if not (relevant or relay):      # non per me e da non rilanciare: non lo ricordo, cosi'
                self.counters["ignored"] += 1  # una copia con h > 0 arrivata dopo si rilancia ancora
                return
            self._remember(key, min(max(exp / 1000, now + SEEN_MIN_S), now + 3600))
        if relay:
            self._relay(o, h, transport)
        if relevant:
            self.counters["delivered"] += 1
            self._emit(p2p_to_remote(o, ts, exp, transport, rssi))

    def _relevant(self, o):
        to = _str_list(o.get("to"))
        if to:
            return self.node_id in to
        return not self.neighbors or o.get("n") in self.neighbors

    def _on_rq(self, o, transport, rssi):
        mid = o.get("id")
        if not isinstance(mid, str) or not mid:
            self.counters["invalid"] += 1
            return
        h = self._hops(o)
        with self._lock:
            if ("rq", mid) in self._seen:
                self.counters["dup"] += 1
                return
            self._remember(("rq", mid), time.time() + SEEN_MIN_S)
        if self.relay and h > 0:
            self._relay(o, h, transport)
        to = _str_list(o.get("to"))
        if to and self.node_id not in to:
            self.counters["ignored"] += 1
            return
        self.counters["delivered"] += 1
        self._emit(normalize(o, transport, rssi, self.near_rssi))

    def _on_rs(self, o, transport):
        """Riepilogo di un altro nodo: interessa al collettore (monitor), il nodo lo rilancia solo
        se h > 0."""
        mid = o.get("id")
        if not isinstance(mid, str) or not mid:
            self.counters["invalid"] += 1
            return
        key = ("rs", mid, o.get("n"))
        h = self._hops(o)
        with self._lock:
            if key in self._seen:
                self.counters["dup"] += 1
                return
            self._remember(key, time.time() + SEEN_MIN_S)
        if self.relay and h > 0:
            self._relay(o, h, transport)
        else:
            self.counters["ignored"] += 1

    def _on_tag(self, o, transport, rssi):
        if self.key and "sig" in o and not verify(o, self.key):
            self.counters["bad_sig"] += 1
            return
        msg = beacon_to_msg(o, transport, rssi, self.near_rssi)
        if msg is None:
            self.counters["invalid"] += 1
            return
        dev, now = msg["device"], time.time()
        with self._lock:
            if dev not in self._tags and len(self._tags) >= TAGS_MAX:
                self._tags.pop(next(iter(self._tags)), None)
            self._tags[dev] = {"role": msg.get("role"), "last_seen": now, "rssi": rssi, "near": msg["near"],
                               "via": transport}
        self.counters["beacons"] += 1
        self._emit(msg)

    def _emit(self, msg):
        try:
            self.on_message(msg)
        except Exception as e:  # un errore del nodo non deve fermare la ricezione
            self._log_once("callback", f"errore in on_message: {e!r}")

    # ------------------------------------------------------------------ invio

    def _send_obj(self, o, keep=()):
        data = encode(o, self.key, keep)
        if data is None:
            self._log_once("size", f"messaggio {o.get('t')} {o.get('id')} troppo lungo anche dopo il taglio: non inviato")
            return False
        now = time.time()
        t = o.get("t")
        with self._lock:   # non riconsegnarlo a me stesso (loopback, rilanci altrui)
            if t == "hz":
                exp = _int(o.get("e"), 0) / 1000
                self._remember(("hz", o["id"], o["a"]), min(max(exp, now + SEEN_MIN_S), now + 3600))
            elif t == "rq" and isinstance(o.get("id"), str):
                self._remember(("rq", o["id"]), now + SEEN_MIN_S)
            elif t == "rs" and isinstance(o.get("id"), str):
                self._remember(("rs", o["id"], o.get("n")), now + SEEN_MIN_S)
        return self._send_bytes(data)

    def _send_bytes(self, data, relay_from=None):
        ok = False
        for t in self.transports:
            try:
                ok = t.write(data, relay_from=relay_from) or ok
            except Exception as e:
                self._log_once(f"tx:{t.name}", f"invio su {t.name} fallito: {e!r}")
        if ok:
            self.counters["tx"] += 1
        return ok

    # ------------------------------------------------------------------ thread

    def _run(self):
        while not self._stop.is_set():
            self.poll_once(0.2)
            try:
                self.tick(time.time())
            except Exception as e:
                self._log_once("tick", f"errore di manutenzione: {e!r}")

    def _log_once(self, key, text):
        if key not in self._logged:
            self._logged.add(key)
            _log(text)


# ---------------------------------------------------------------------------- riga di comando

if __name__ == "__main__":
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(description="Prova del canale radio SC-P2P v2")
    ap.add_argument("cmd", choices=["listen", "send", "tag", "ask"])
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    ap.add_argument("--node", help="id del nodo (default: node_id del config + '-cli')")
    ap.add_argument("--transports", help="es. udp oppure serial,udp (default: quelli del config)")
    ap.add_argument("--serial", help="porta della radio ESP-NOW, es. /dev/ttyACM0 o socket://host:porta")
    ap.add_argument("--kind", default="emergency_vehicle")
    ap.add_argument("--severity", type=int, default=3)
    ap.add_argument("--to", nargs="*", default=[], help="incroci destinatari (to)")
    ap.add_argument("--hops", type=int, default=None)
    ap.add_argument("--heading", help="direzione in gradi o punto cardinale")
    ap.add_argument("--cancel", action="store_true", help="invia la fine dell'evento --id")
    ap.add_argument("--id", default=None)
    ap.add_argument("--role", default="pedestrian", help="tag: pedestrian, blind, cyclist, driver, emergency")
    ap.add_argument("--device", default="tag-cli")
    ap.add_argument("--seconds", type=float, default=10.0)
    args = ap.parse_args()

    try:
        raw = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    try:  # chiave p2p dal .env, come fa il nodo
        from config import load_env, _secrets_from_env
        load_env(Path(args.config).resolve().parent)
        _secrets_from_env(raw)
    except Exception:
        pass
    pcfg = {**(raw.get("p2p") or {}), "enabled": True, "heartbeat": False}   # la riga di comando non si annuncia
    if args.transports:
        pcfg["transports"] = args.transports
    if args.serial:
        pcfg["serial"] = {**(pcfg.get("serial") if isinstance(pcfg.get("serial"), dict) else {}), "port": args.serial}
        if "serial" not in str(pcfg.get("transports", "serial")):
            pcfg["transports"] = list(pcfg.get("transports") or []) + ["serial"]
    # chi ascolta ha un id diverso da chi invia dallo stesso PC (altrimenti scarta i messaggi come "suoi")
    node = args.node or f"{raw.get('node_id', 'incrocio-1')}-{'ascolto' if args.cmd == 'listen' else 'cli'}"

    def show(m):
        print(json.dumps(m, ensure_ascii=False), flush=True)

    link = PeerLink(pcfg, node, show)
    try:
        if args.cmd == "send":
            try:
                from events import KINDS
                cause, _, targets, _, validity, _, cat = KINDS[args.kind]
            except Exception:
                cause, targets, validity, cat = 99, ALL_TARGETS, 30, "risk"
            t = _now_ms()
            msg = {"type": "hazard", "id": args.id or f"{node}-{t}", "kind": args.kind, "category": cat,
                   "severity": args.severity, "cause": cause, "targets": targets, "propagate": True,
                   "action": "cancel" if args.cancel else "new", "ts": t,
                   "expires": t if args.cancel else t + validity * 1000, "heading": args.heading}
            ok = link.send_hazard(msg, targets=args.to, hops=args.hops)
            print("inviato" if ok else "NON inviato", msg["id"], "a", args.to or "vicini")
            time.sleep(0.5)
        elif args.cmd == "tag":
            role = role_of(args.role)
            period = 0.5 if role == "emergency" else 1.0
            print(f"CROSS Tag finto '{args.device}' ({role}) per {args.seconds:.0f} s")
            q, end = 0, time.time() + args.seconds
            link.relay = False                       # i Tag non rilanciano mai
            link.on_message = lambda m: None         # (i propri beacon tornano dal multicast)
            link.monitor = lambda o, tr, rssi: o.get("t") == "adv" and print("adv:", json.dumps(o, ensure_ascii=False))
            while time.time() < end:
                q += 1
                if role == "emergency":
                    o = {"t": "emv", "d": args.device, "q": q, "st": "run"}
                else:
                    o = {"t": "vru", "d": args.device, "p": role, "q": q, "b": 90}
                hd = _heading(args.heading)
                if hd is not None:
                    o["hd"] = hd
                link.send_obj(o)
                time.sleep(period)
        elif args.cmd == "ask":
            rid = f"rq-{_now_ms() % 100000000}"
            link.relay = False
            link.monitor = lambda o, tr, rssi: o.get("t") == "rs" and print(
                f"rs da {o.get('n')} via {tr} rssi={rssi}:", json.dumps(o.get("p"), ensure_ascii=False))
            print("richiesta", rid, "inviata" if link.send_obj({"t": "rq", "id": rid, "w": "sum"}) else "NON inviata")
            time.sleep(max(1.0, args.seconds if args.seconds != 10.0 else 3.0))
        else:
            link.relay = False
            link._relevant = lambda o: True    # in ascolto mostro tutto
            link.monitor = lambda o, tr, rssi: print(f"<{tr}{'' if rssi is None else f' {rssi} dBm'}>",
                                                     json.dumps(o, ensure_ascii=False), flush=True)
            print(f"in ascolto come '{node}' (Ctrl+C per uscire)")
            while True:
                time.sleep(5)
                print("incroci:", {k: (round(time.time() - v["last_seen"], 1), v["pw"]) for k, v in link.peers().items()},
                      "tag:", list(link.vrus()), link.counters, flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        link.close()
