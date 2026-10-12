"""Utenti della strada: registro dei CROSS Tag vicini e avvisi per ruolo (ARCHITETTURA §2.7).

I Tag (pedestrian, blind, cyclist, driver, emergency) mandano un beacon ogni secondo (peer.py li
normalizza in "vru_beacon" / "emergency_beacon"). Il nodo li registra con on_beacon() e a ogni tick
chiama advise(), che decide un avviso per ogni Tag vicino e lo trasmette come messaggio "adv" (§3.2):

  {"v": 2, "t": "adv", "n": nodo, "id": id unico, "r": [ruolo] | "to": dispositivo,
   "c": codice, "s": 1..3, "x": attraversamento (-1 se nessuno), "k": tipo di pericolo (opz.), "ttl": ms}

- Un Tag e' vicino se rssi >= near_rssi (default -75 dBm) o se arriva da un trasporto senza RSSI con
  "near": true; resta vicino per user_timeout_s (default 6 s) dall'ultimo beacon vicino.
- Messaggi solo se ci sono Tag vicini: subito quando l'avviso cambia, poi ogni adv_period_s (1 s).
- Un messaggio per ruolo ("r") quando tutti i Tag di quel ruolo hanno lo stesso avviso; altrimenti un
  messaggio per dispositivo ("to"), fino a max_direct Tag (oltre: un solo messaggio per ruolo con
  l'avviso piu' prudente).
- Ogni messaggio sta in 240 byte (JSON compatto); oltre si toglie "k", se non basta non parte.

Associazione Tag -> attraversamento (scelta documentata):
  1. il nodo ha un solo attraversamento: tutti i Tag vicini sono associati a quello;
  2. il beacon indica l'attraversamento ("crosswalk" o "x"): quello;
  3. il beacon porta una posizione ("position" [lat, lon] o "lat"/"lon") e gli attraversamenti hanno
     una posizione (config "position", oppure "bearing" + posizione del nodo + crosswalk_offset_m): il
     piu' vicino;
  4. il beacon porta una direzione di marcia ("heading", con velocita' sconosciuta o >= walk_mps) e gli
     attraversamenti hanno un "bearing" (dove stanno rispetto al centro dell'incrocio): quello dal cui
     lato arriva il Tag (bearing ~ heading + 180, scarto <= assoc_max_deg);
  5. altrimenti TUTTI: il Tag riceve l'avviso piu' prudente tra gli attraversamenti e CROSS_OK solo se
     tutti sono sicuri.
  L'associazione resta quella dell'ultimo beacon informativo (un pedone fermo sul bordo non ha direzione).

Codici (c): vedi CODES. Regole principali:
  - pedone / non vedente: EMERGENCY_APPROACHING con un mezzo di soccorso; sulle strisce KEEP_GOING ma
    VEHICLE_LEFT/RIGHT se un veicolo e' in conflitto; in attesa VEHICLE_LEFT/RIGHT con un veicolo in
    arrivo (attention/conflict), WAIT se la camera non vede o non e' sicuro, CROSS_OK SOLO con safe = True.
    Il non vedente riceve ASSIST_UNAVAILABLE se la camera non e' pronta o non e' fresca.
    "Sulle strisce": dopo un CROSS_OK (entro cross_window_s) il Tag si muove (>= walk_mps) oppure la
    camera vede qualcuno sulle strisce e nessuno piu' sul bordo; finisce quando la camera non vede piu'
    nessuno sulle strisce (o dopo cross_max_s): poi per done_hold_s niente nuovo CROSS_OK (CLEAR).
  - ciclista: EMERGENCY_APPROACHING, VEHICLE_LEFT/RIGHT (veicolo in conflitto), WRONG_WAY,
    BIKE_LANE_BLOCKED (bike_lane_invasion o illegal_stop in una zona bike_lane qui), HAZARD_AHEAD, CLEAR.
  - conducente: EMERGENCY_APPROACHING, WRONG_WAY, PEDESTRIAN_AHEAD, HAZARD_AHEAD, CYCLIST_AHEAD, CLEAR.
  - soccorso: WRONG_WAY, CORRIDOR_CLEAR (camera pronta e fresca, nessuno sulle strisce, nessun altro
    veicolo vicino) o CORRIDOR_BUSY (anche quando non si puo' sapere).
  - WRONG_WAY a tutti con un veicolo contromano segnalato (qui o diretto qui).

Solo libreria standard; nessuna eccezione esce da questo modulo (log una volta e si continua).
"""
import json
import math
import threading
import time
from collections import Counter

from crosswalk import RANK, crosswalk_configs, users_config

CODES = {
    "CLEAR": 0, "WAIT": 1, "CROSS_OK": 2, "KEEP_GOING": 3, "VEHICLE_LEFT": 4, "VEHICLE_RIGHT": 5,
    "PEDESTRIAN_AHEAD": 6, "CYCLIST_AHEAD": 7, "BIKE_LANE_BLOCKED": 8, "EMERGENCY_APPROACHING": 9,
    "CORRIDOR_CLEAR": 10, "CORRIDOR_BUSY": 11, "HAZARD_AHEAD": 12, "ASSIST_UNAVAILABLE": 13, "WRONG_WAY": 14,
}
CODE_NAMES = {v: k for k, v in CODES.items()}
ROLES = ("pedestrian", "blind", "cyclist", "driver", "emergency")
# A chi e' destinato ogni codice (tabella §2.7)
CODE_ROLES = {
    0: ROLES, 1: ("pedestrian", "blind"), 2: ("pedestrian", "blind"), 3: ("pedestrian", "blind"),
    4: ("pedestrian", "blind", "cyclist"), 5: ("pedestrian", "blind", "cyclist"), 6: ("driver",),
    7: ("driver",), 8: ("cyclist",), 9: ("pedestrian", "blind", "cyclist", "driver"), 10: ("emergency",),
    11: ("emergency",), 12: ("driver", "cyclist"), 13: ("blind",), 14: ROLES,
}
# Prudenza di un avviso a parita' di gravita' "s" (serve solo quando bisogna sceglierne uno per tanti Tag)
CAUTION = {0: 0, 3: 1, 2: 2, 10: 2, 7: 3, 6: 4, 12: 5, 8: 5, 11: 6, 1: 6, 13: 7, 14: 8, 4: 9, 5: 9, 9: 10}
ALIASES = {"person": "pedestrian", "pedone": "pedestrian", "vehicle": "driver", "car": "driver",
           "conducente": "driver", "ciclista": "cyclist", "bike": "cyclist", "cieco": "blind",
           "non_vedente": "blind", "ambulance": "emergency", "soccorso": "emergency"}
PROFILE = {"pedestrian": "person", "blind": "person", "cyclist": "cyclist", "driver": "vehicle",
           "emergency": "vehicle"}
MAX_BYTES = 240
TAGS_MAX = 2000
EMERGENCY_KINDS = ("emergency_vehicle", "emergency_corridor")
# pericoli che hanno un codice proprio o che non sono un pericolo per chi e' in strada
NOT_HAZARD_AHEAD = ("wrong_way", "emergency_vehicle", "emergency_corridor", "crossing_conflict",
                    "assisted_crossing", "wake_sensor_fault")


def _log(testo):
    print("[utenti]", testo, flush=True)


def _num(v, default=None):
    if isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _int_or_none(v):
    n = _num(v)
    return int(n) if n is not None and n == int(n) else None


def role_of(p):
    """Ruolo del Tag (accetta anche i nomi della v1 e in italiano); sconosciuto -> pedestrian."""
    if not isinstance(p, str) or not p.strip():
        return "pedestrian"
    p = p.strip().lower()
    p = ALIASES.get(p, p)
    return p if p in ROLES else "pedestrian"


def _angle_diff(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def _metri(a, b):
    """Distanza approssimata in metri tra due [lat, lon] (piccole distanze)."""
    dy = (b[0] - a[0]) * 111320
    dx = (b[1] - a[1]) * 111320 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def _offset(pos, bearing, m):
    """[lat, lon] spostato di m metri verso bearing (gradi bussola)."""
    r = math.radians(bearing)
    lat = pos[0] + m * math.cos(r) / 111320
    lon = pos[1] + m * math.sin(r) / (111320 * max(0.01, math.cos(math.radians(pos[0]))))
    return [lat, lon]


def _latlon(v):
    if isinstance(v, dict):
        lat, lon = _num(v.get("lat")), _num(v.get("lon"))
    elif isinstance(v, (list, tuple)) and len(v) == 2:
        lat, lon = _num(v[0]), _num(v[1])
    else:
        return None
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return [lat, lon]


def _active(h, t):
    """Pericolo ancora in corso (non "cancel", non scaduto)."""
    if not isinstance(h, dict) or h.get("action") == "cancel":
        return False
    exp = _num(h.get("expires"))
    return exp is None or exp >= t * 1000


class UserLink:
    def __init__(self, cfg, node_id, send):
        """cfg = config completo (usa "users", "crosswalks", "position") o la sola sezione "users".
        send(obj) trasmette un oggetto SC-P2P (es. PeerLink.send_obj)."""
        cfg = cfg if isinstance(cfg, dict) else {}
        self.node = str(node_id)
        self.send = send
        self.cfg, w = users_config(cfg)
        if any(k in cfg for k in ("crosswalks", "cameras", "_cameras", "zones")):
            self.crosswalks, w2 = crosswalk_configs(cfg)
        else:  # solo la sezione "users": un attraversamento di default
            self.crosswalks, w2 = crosswalk_configs({})
        for a in w + w2:
            _log(a)
        self.ids = [cw["id"] for cw in self.crosswalks]
        node_pos = _latlon(cfg.get("position"))
        self._cw_pos = {}     # id -> [lat, lon] per l'associazione per posizione
        self._cw_bearing = {}  # id -> gradi per l'associazione per direzione
        for cw in self.crosswalks:
            if cw.get("position"):
                self._cw_pos[cw["id"]] = cw["position"]
            if cw.get("bearing") is not None:
                self._cw_bearing[cw["id"]] = cw["bearing"]
                if node_pos and cw["id"] not in self._cw_pos:
                    self._cw_pos[cw["id"]] = _offset(node_pos, cw["bearing"], self.cfg["crosswalk_offset_m"])
        self._lock = threading.Lock()
        self._tags = {}        # device -> registro (vedi on_beacon)
        self._last = {}        # chiave messaggio ("r", ruolo) | ("to", device) -> {"adv", "t"}
        self._advice = {}      # device -> avviso dell'ultimo giro (per state())
        self._errors = Counter()
        self._sent = 0
        self._send_fail = 0
        self._seq = 0
        self._boot = format(int(time.time()) % (36 ** 3), "x")
        self._id_prefix = (self.node if len(self.node) <= 40 else self.node[:40]) + "-a" + self._boot + "."

    # ------------------------------------------------------------------ aiuti

    def _warn(self, chiave, testo):
        self._errors[chiave] += 1
        if self._errors[chiave] == 1:
            _log(testo)

    # ------------------------------------------------------------------ beacon

    def on_beacon(self, msg, t=None):
        """Registra un beacon ("vru_beacon"/"emergency_beacon" di peer.py; accetta anche "vru"/"emv"
        grezzi). Ritorna {"device","role","near","new","crosswalks"} o None se non valido."""
        try:
            t = time.time() if t is None else float(t)
            if not isinstance(msg, dict):
                return None
            tipo = msg.get("type") or msg.get("t")
            dev = msg.get("device", msg.get("d"))
            if not isinstance(dev, str) or not dev or len(dev) > 64:
                return None
            if tipo in ("emergency_beacon", "emv"):
                role = "emergency"
            elif tipo in ("vru_beacon", "vru", None):
                role = role_of(msg.get("role", msg.get("p")))
            else:
                return None
            rssi = _num(msg.get("rssi"))
            near = rssi >= self.cfg["near_rssi"] if rssi is not None else msg.get("near", True) is not False
            with self._lock:
                e = self._tags.get(dev)
                era_vicino = e is not None and e.get("last_near") is not None \
                    and t - e["last_near"] <= self.cfg["user_timeout_s"]
                if e is None:
                    if len(self._tags) >= TAGS_MAX:
                        self._purge(t, force=True)
                    e = self._tags[dev] = {"device": dev, "first_seen": t, "last_near": None,
                                           "crosswalks": list(self.ids), "assoc": "tutti",
                                           "phase": "waiting", "phase_since": t, "ok_t": None}
                if e.get("role") != role or (near and not era_vicino):
                    # nuovo ruolo o Tag tornato vicino dopo la scadenza: si riparte da "in attesa"
                    e.update({"phase": "waiting", "phase_since": t, "ok_t": None, "empty_since": None})
                e["role"] = role
                e["rssi"] = rssi
                e["last_seen"] = t
                e["via"] = msg.get("via")
                hd = _num(msg.get("heading", msg.get("hd")))
                e["heading"] = hd % 360 if hd is not None else None
                sp = _num(msg.get("speed", msg.get("sp")))
                e["speed"] = sp
                e["speed_t"] = t if sp is not None else e.get("speed_t")
                b = _num(msg.get("battery", msg.get("b")))
                e["battery"] = int(max(0, min(100, b))) if b is not None else e.get("battery")
                if role == "emergency":
                    e["next_node"] = msg.get("next_node", msg.get("nx"))
                    e["siren"] = bool(msg.get("siren", msg.get("st") == "run"))
                if near:
                    e["last_near"] = t
                self._associate(e, msg)
                return {"device": dev, "role": role, "near": near, "new": near and not era_vicino,
                        "crosswalks": list(e["crosswalks"])}
        except Exception as ex:
            self._warn("beacon", f"beacon ignorato ({ex!r})")
            return None

    def _associate(self, e, msg):
        """Associazione Tag -> attraversamenti (vedi in testa al modulo)."""
        ids = self.ids
        if len(ids) <= 1:
            e["crosswalks"], e["assoc"] = list(ids), "unico"
            return
        x = _int_or_none(msg.get("crosswalk", msg.get("x")))
        if x is not None and x in ids:
            e["crosswalks"], e["assoc"] = [x], "esplicito"
            return
        pos = _latlon(msg.get("position")) or _latlon({"lat": msg.get("lat"), "lon": msg.get("lon")})
        if pos and self._cw_pos:
            best = min(self._cw_pos, key=lambda i: _metri(pos, self._cw_pos[i]))
            e["crosswalks"], e["assoc"] = [best], "posizione"
            return
        hd, sp = e.get("heading"), e.get("speed")
        if hd is not None and self._cw_bearing and (sp is None or sp >= self.cfg["walk_mps"]):
            da = (hd + 180) % 360  # lato da cui arriva il Tag
            best = min(self._cw_bearing, key=lambda i: _angle_diff(self._cw_bearing[i], da))
            if _angle_diff(self._cw_bearing[best], da) <= self.cfg["assoc_max_deg"]:
                e["crosswalks"], e["assoc"] = [best], "direzione"
                return
        if e.get("assoc") in ("esplicito", "posizione", "direzione") and all(i in ids for i in e["crosswalks"]):
            return  # resta l'ultima associazione informativa
        e["crosswalks"], e["assoc"] = list(ids), "tutti"

    def _purge(self, t, force=False):
        limite = max(60.0, 5 * self.cfg["user_timeout_s"])
        for dev in [d for d, e in self._tags.items() if t - e.get("last_seen", t) > limite]:
            del self._tags[dev]
        if force and len(self._tags) >= TAGS_MAX:
            for dev in sorted(self._tags, key=lambda d: self._tags[d].get("last_seen", 0))[:len(self._tags) // 10 + 1]:
                del self._tags[dev]

    def _near_entries(self, t):
        to = self.cfg["user_timeout_s"]
        with self._lock:
            self._purge(t)
            return [e for e in self._tags.values()
                    if e.get("last_near") is not None and -1.0 <= t - e["last_near"] <= to]

    def near(self, t=None):
        """Tag vicini: [{"device","role","rssi","age_s","crosswalk","crosswalks","assoc","battery","phase"}].
        "crosswalk" e' l'attraversamento associato se e' uno solo, altrimenti None (associato a tutti:
        vedi "crosswalks")."""
        try:
            t = time.time() if t is None else float(t)
            out = []
            for e in self._near_entries(t):
                cws = list(e.get("crosswalks") or [])
                out.append({"device": e["device"], "role": e["role"], "rssi": e.get("rssi"),
                            "age_s": round(max(0.0, t - e.get("last_seen", t)), 2),
                            "crosswalk": cws[0] if len(cws) == 1 else None, "crosswalks": cws,
                            "assoc": e.get("assoc"), "battery": e.get("battery"), "phase": e.get("phase")})
            out.sort(key=lambda d: (ROLES.index(d["role"]), d["device"]))
            return out
        except Exception as ex:
            self._warn("near", f"elenco dei Tag vicini non disponibile ({ex!r})")
            return []

    def counts_by_crosswalk(self, t=None):
        """{id attraversamento: {"blind","pedestrians","cyclists","drivers","emergency"}} dei Tag vicini
        (un Tag associato a piu' attraversamenti conta in ognuno)."""
        out = {i: {"blind": 0, "pedestrians": 0, "cyclists": 0, "drivers": 0, "emergency": 0} for i in self.ids}
        chiave = {"blind": "blind", "pedestrian": "pedestrians", "cyclist": "cyclists", "driver": "drivers",
                  "emergency": "emergency"}
        try:
            t = time.time() if t is None else float(t)
            for e in self._near_entries(t):
                for i in e.get("crosswalks") or []:
                    if i in out:
                        out[i][chiave[e["role"]]] += 1
        except Exception as ex:
            self._warn("counts", f"conteggio dei Tag non disponibile ({ex!r})")
        return out

    # ------------------------------------------------------------------ avvisi

    def advise(self, t, crossings, hazards, emergency, camera_ready):
        """Decide e trasmette gli avvisi (messaggi "adv"). crossings = stati di CrosswalkController
        (update()/states()), hazards = pericoli attivi (locali "hazard" e "remote_hazard"), emergency =
        {"eta_s","from"} o None. Ritorna la lista dei messaggi inviati in questo giro."""
        try:
            t = time.time() if t is None else float(t)
            return self._advise(t, crossings, hazards, emergency, camera_ready)
        except Exception as ex:
            self._warn("advise", f"avvisi non calcolati ({ex!r})")
            return []

    def _advise(self, t, crossings, hazards, emergency, camera_ready):
        near = self._near_entries(t)
        if not near:
            self._last.clear()  # al ritorno di un Tag l'avviso parte subito
            self._advice = {}
            return []
        ctx = self._context(t, crossings, hazards, emergency, camera_ready, near)
        per_dev = {}
        for e in near:
            try:
                per_dev[e["device"]] = self._advice_for(e, ctx, t)
            except Exception as ex:
                self._warn(f"avviso:{e.get('role')}", f"avviso per {e.get('role')} non calcolato ({ex!r})")
                per_dev[e["device"]] = self._fallback(e, ctx)

        messaggi = []  # (chiave, destinatario, avviso, dispositivi)
        for role in ROLES:
            devs = [e for e in near if e["role"] == role]
            if not devs:
                continue
            diversi = {per_dev[e["device"]] for e in devs}
            if len(diversi) == 1 or len(devs) > self.cfg["max_direct"]:
                adv = max(diversi, key=lambda a: (a[1], CAUTION.get(a[0], 0)))
                messaggi.append((("r", role), {"r": [role]}, adv, devs))
            else:
                for e in devs:
                    messaggi.append((("to", e["device"]), {"to": e["device"]}, per_dev[e["device"]], [e]))

        inviati, chiavi = [], set()
        periodo = self.cfg["adv_period_s"]
        for chiave, dest, adv, devs in messaggi:
            chiavi.add(chiave)
            for e in devs:  # il CROSS_OK davvero inviato serve per capire quando parte
                if adv[0] == CODES["CROSS_OK"]:
                    e["ok_t"] = t
            prec = self._last.get(chiave)
            if prec is not None and prec["adv"] == adv and t - prec["t"] < periodo - 1e-6:
                continue
            obj = self._message(dest, adv)
            self._last[chiave] = {"adv": adv, "t": t}
            if obj is None:
                continue
            ok = False
            try:
                ok = self.send(obj) is not False
            except Exception as ex:
                self._warn("send", f"invio dell'avviso fallito ({ex!r})")
            if ok:
                self._sent += 1
            else:
                self._send_fail += 1
            inviati.append(obj)
        for chiave in [k for k in self._last if k not in chiavi]:
            del self._last[chiave]
        self._advice = {d: {"code": a[0], "name": CODE_NAMES.get(a[0]), "s": a[1], "x": a[2], "k": a[3]}
                        for d, a in per_dev.items()}
        return inviati

    def _message(self, dest, adv):
        code, s, x, k = adv
        self._seq += 1
        obj = {"v": 2, "t": "adv", "n": self.node, "id": f"{self._id_prefix}{self._seq}", **dest,
               "c": int(code), "s": int(max(1, min(3, s))), "x": int(x) if x is not None else -1,
               "ttl": int(self.cfg["adv_ttl_ms"])}
        if k:
            obj["k"] = str(k)

        def size():
            return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        if size() > MAX_BYTES and "k" in obj:
            obj.pop("k")
        if size() > MAX_BYTES:
            self._warn("size", f"avviso oltre {MAX_BYTES} byte (id del nodo o del Tag troppo lunghi): non inviato")
            return None
        return obj

    # ------------------------------------------------------------------ contesto del giro

    def _context(self, t, crossings, hazards, emergency, camera_ready, near):
        states = {}
        for s in crossings or []:
            if isinstance(s, dict):
                i = _int_or_none(s.get("id"))
                if i is not None:
                    states[i] = s
        attivi = [h for h in (hazards or []) if _active(h, t)]

        def gravita(h):
            return (_num(h.get("severity"), 1), _num(h.get("ts"), 0))

        def locale(h):
            return h.get("type") != "remote_hazard" and h.get("node") in (None, self.node)

        contromano = [h for h in attivi if h.get("kind") == "wrong_way"]
        soccorso_haz = [h for h in attivi if h.get("kind") in EMERGENCY_KINDS]
        ciclabile = [h for h in attivi if locale(h) and (
            h.get("kind") == "bike_lane_invasion"
            or (h.get("kind") == "illegal_stop"
                and str(((h.get("where") or {}).get("zone")) or "").startswith("bike_lane")))]

        def per_profilo(profilo, escludi=()):
            """Pericolo piu' grave (rischio o violazione, gravita' >= 2) che interessa quel profilo."""
            hs = [h for h in attivi if h.get("kind") not in NOT_HAZARD_AHEAD and id(h) not in escludi
                  and h.get("category") in ("risk", "violation") and _num(h.get("severity"), 1) >= 2
                  and profilo in (h.get("targets") or ["person", "cyclist", "vehicle"])]
            return max(hs, key=gravita) if hs else None
        # al ciclista la ciclabile occupata qui si dice con BIKE_LANE_BLOCKED, non con HAZARD_AHEAD
        haz_cyc = per_profilo("cyclist", {id(h) for h in ciclabile})
        soccorso = (emergency is not None and emergency is not False) or bool(soccorso_haz) \
            or any(e["role"] == "emergency" for e in near) \
            or any(s.get("state") == "emergency" or s.get("emergency") for s in states.values())
        return {"states": states, "camera_ready": bool(camera_ready), "emergency": soccorso,
                "wrong_way": max(contromano, key=gravita) if contromano else None,
                "bike_blocked": max(ciclabile, key=gravita) if ciclabile else None,
                "haz_driver": per_profilo("vehicle"), "haz_cyclist": haz_cyc,
                "cyclist_tags": sum(1 for e in near if e["role"] == "cyclist")}

    def _states_of(self, e, ctx):
        """Stati degli attraversamenti associati al Tag (tutti se l'associazione non e' nota)."""
        states = ctx["states"]
        ids = [i for i in (e.get("crosswalks") or []) if i in states] or list(states)
        return [states[i] for i in ids]

    @staticmethod
    def _worst(sts):
        if not sts:
            return None
        return max(sts, key=lambda s: (RANK.get(s.get("state"), RANK["unknown"]),
                                       -(_num(s.get("min_eta_s"), 1e9))))

    def _fresh(self, s, ctx):
        if s is None:
            return False
        if "fresh" in s:
            return bool(s["fresh"]) and ctx["camera_ready"]
        return ctx["camera_ready"] and s.get("state") != "unknown"

    @staticmethod
    def _side_code(s):
        side = (s or {}).get("side")
        if side == "left":
            return CODES["VEHICLE_LEFT"]
        if side == "right":
            return CODES["VEHICLE_RIGHT"]
        return None

    @staticmethod
    def _x(s, e):
        if s is not None:
            return _int_or_none(s.get("id"))
        cws = e.get("crosswalks") or []
        return cws[0] if cws else -1

    def _fallback(self, e, ctx):
        """Avviso prudente quando il calcolo normale va in errore."""
        role = e.get("role")
        if role == "blind":
            return (CODES["ASSIST_UNAVAILABLE"], 2, -1, None)
        if role == "pedestrian":
            return (CODES["WAIT"], 2, -1, None)
        if role == "emergency":
            return (CODES["CORRIDOR_BUSY"], 2, -1, None)
        return (CODES["CLEAR"], 1, -1, None)

    def _advice_for(self, e, ctx, t):
        role = e["role"]
        if role in ("pedestrian", "blind"):
            return self._advice_walker(e, ctx, t, role == "blind")
        if role == "cyclist":
            return self._advice_cyclist(e, ctx)
        if role == "driver":
            return self._advice_driver(e, ctx)
        return self._advice_emergency(e, ctx)

    # ------------------------------------------------------------------ pedone e non vedente

    def _phase(self, e, sts, ctx, t):
        """Fase del Tag: "waiting" (sul bordo), "crossing" (sulle strisce), "done" (appena attraversato)."""
        ph = e.get("phase") or "waiting"
        visti = [s for s in sts if self._fresh(s, ctx)]
        if ph == "crossing":
            dur = t - e.get("phase_since", t)
            if visti and not any((s.get("crossing") or 0) > 0 for s in visti):
                e["empty_since"] = e.get("empty_since") or t
            else:
                e["empty_since"] = None
            vuote = e.get("empty_since") is not None and t - e["empty_since"] >= 2.0
            if dur > self.cfg["cross_max_s"] or (dur >= 3.0 and vuote):
                e.update({"phase": "done", "phase_since": t, "ok_t": None, "empty_since": None})
        elif ph == "done":
            if t - e.get("phase_since", t) >= self.cfg["done_hold_s"]:
                e.update({"phase": "waiting", "phase_since": t})
        else:
            ok_t = e.get("ok_t")
            recente = ok_t is not None and t - ok_t <= self.cfg["cross_window_s"]
            sp, sp_t = e.get("speed"), e.get("speed_t")
            cammina = sp is not None and sp >= self.cfg["walk_mps"] and sp_t is not None and t - sp_t <= 2.5
            solo_strisce = any((s.get("crossing") or 0) > 0 and not (s.get("waiting") or 0) and not s.get("curb")
                               for s in visti)
            if recente and (cammina or solo_strisce):
                e.update({"phase": "crossing", "phase_since": t, "empty_since": None})
        return e.get("phase", "waiting")

    def _advice_walker(self, e, ctx, t, blind):
        sts = self._states_of(e, ctx)
        s = self._worst(sts)
        x = self._x(s, e)
        fase = self._phase(e, sts, ctx, t)
        if ctx["emergency"]:
            em = next((q for q in sts if q.get("state") == "emergency"), s)
            return (CODES["EMERGENCY_APPROACHING"], 3, self._x(em, e), None)
        stato = s.get("state") if s else None
        conflitto = any(q.get("state") == "conflict" for q in sts)
        attenzione = any(q.get("state") == "attention" for q in sts)
        lato = self._side_code(s)
        fresca = bool(sts) and all(self._fresh(q, ctx) for q in sts)
        contromano = ctx["wrong_way"]
        k_cm = contromano.get("kind") if contromano else None
        if fase == "crossing":
            if conflitto:  # sulle strisce: da che parte arriva il veicolo (o fermati se non si sa)
                return (lato or CODES["WAIT"], 3, x, None)
            if contromano:
                return (CODES["WRONG_WAY"], 3, x, k_cm)
            if blind and not fresca:
                return (CODES["ASSIST_UNAVAILABLE"], 2, x, None)
            return (CODES["KEEP_GOING"], 2 if attenzione else 1, x, None)
        if fase == "done":
            if conflitto:
                return (lato or CODES["WAIT"], 3, x, None)
            if contromano:
                return (CODES["WRONG_WAY"], 3, x, k_cm)
            return (CODES["CLEAR"], 1, x, None)
        # in attesa sul bordo
        if blind and (not ctx["camera_ready"] or not fresca or stato == "unknown"):
            return (CODES["ASSIST_UNAVAILABLE"], 2, x, None)
        if conflitto:
            return (lato or CODES["WAIT"], 3, x, None)
        if contromano:
            return (CODES["WRONG_WAY"], 3, x, k_cm)
        if attenzione:
            return (lato or CODES["WAIT"], 2, x, None)
        if not sts or not fresca or stato == "unknown" or not ctx["camera_ready"]:
            return (CODES["WAIT"], 2, x, None)
        if all(q.get("safe") is True for q in sts):
            return (CODES["CROSS_OK"], 1, x, None)
        return (CODES["WAIT"], 2, x, None)

    # ------------------------------------------------------------------ ciclista, conducente, soccorso

    def _advice_cyclist(self, e, ctx):
        sts = self._states_of(e, ctx)
        s = self._worst(sts)
        x = self._x(s, e)
        if ctx["emergency"]:
            return (CODES["EMERGENCY_APPROACHING"], 3, x, None)
        for q in sorted(sts, key=lambda q: _num(q.get("min_eta_s"), 1e9)):
            eta = _num(q.get("min_eta_s"))
            limite = _num(q.get("conflict_eta_s"), self.cfg["conflict_eta_s"])
            if eta is not None and eta <= limite and self._fresh(q, ctx) and self._side_code(q):
                return (self._side_code(q), 3, self._x(q, e), None)
        if ctx["wrong_way"]:
            return (CODES["WRONG_WAY"], 3, x, ctx["wrong_way"].get("kind"))
        if ctx["bike_blocked"]:
            h = ctx["bike_blocked"]
            return (CODES["BIKE_LANE_BLOCKED"], max(2, min(3, int(_num(h.get("severity"), 2)))), x, h.get("kind"))
        if ctx["haz_cyclist"]:
            h = ctx["haz_cyclist"]
            return (CODES["HAZARD_AHEAD"], max(1, min(3, int(_num(h.get("severity"), 2)))), x, h.get("kind"))
        return (CODES["CLEAR"], 1, x, None)

    def _advice_driver(self, e, ctx):
        sts = self._states_of(e, ctx)
        s = self._worst(sts)
        x = self._x(s, e)
        if ctx["emergency"]:
            return (CODES["EMERGENCY_APPROACHING"], 3, x, None)
        if ctx["wrong_way"]:
            return (CODES["WRONG_WAY"], 3, x, ctx["wrong_way"].get("kind"))
        candidati = []  # (s, priorita', avviso)
        persone = [q for q in sts if q.get("present") or q.get("state") in ("occupied", "assist", "attention",
                                                                              "conflict", "unknown")
                   or (q.get("waiting") or 0) + (q.get("crossing") or 0) > 0]
        if persone:
            grave = [q for q in persone if q.get("state") == "conflict" or (q.get("crossing") or 0) > 0]
            q = (grave or persone)[0]
            candidati.append((3 if grave else 2, 3, (CODES["PEDESTRIAN_AHEAD"], 3 if grave else 2, self._x(q, e), None)))
        h = ctx["haz_driver"]
        if h:
            sev = max(1, min(3, int(_num(h.get("severity"), 2))))
            candidati.append((sev, 2, (CODES["HAZARD_AHEAD"], sev, x, h.get("kind"))))
        bici = [q for q in sts if (q.get("cyclists") or 0) + (q.get("bike_lane_cyclists") or 0) > 0]
        if bici or ctx["cyclist_tags"]:
            candidati.append((2, 1, (CODES["CYCLIST_AHEAD"], 2, self._x(bici[0], e) if bici else x, None)))
        if candidati:
            return max(candidati, key=lambda c: (c[0], c[1]))[2]
        return (CODES["CLEAR"], 1, x, None)

    def _advice_emergency(self, e, ctx):
        sts = list(ctx["states"].values())
        if ctx["wrong_way"]:
            return (CODES["WRONG_WAY"], 3, -1, ctx["wrong_way"].get("kind"))
        if not ctx["camera_ready"] or not sts:
            return (CODES["CORRIDOR_BUSY"], 2, -1, None)  # non si vede: mai "libero"
        for q in sts:
            limite = _num(q.get("conflict_eta_s"), self.cfg["conflict_eta_s"])
            altri = [v for v in q.get("vehicles") or [] if isinstance(v, dict) and v.get("class") != "emergency"]
            vicini = [v for v in altri if _num(v.get("eta_s")) is None or _num(v.get("eta_s")) <= limite]
            if "vehicles" not in q and _num(q.get("min_eta_s")) is not None and _num(q.get("min_eta_s")) <= limite \
                    and not q.get("emergency"):
                vicini = [q]
            persone = (q.get("crossing") or 0) + (q.get("cyclists") or 0)
            if not self._fresh(q, ctx) or persone > 0 or vicini:
                return (CODES["CORRIDOR_BUSY"], 3 if persone else 2, self._x(q, e), None)
        return (CODES["CORRIDOR_CLEAR"], 1, -1, None)

    # ------------------------------------------------------------------ stato

    def state(self, t=None):
        """Per l'API e la dashboard: Tag vicini, conteggi per ruolo, ultimi avvisi, contatori."""
        try:
            t = time.time() if t is None else float(t)
            vicini = self.near(t)
            return {"near": len(vicini), "tags": vicini,
                    "by_role": dict(Counter(d["role"] for d in vicini)),
                    "advice": {d: dict(a) for d, a in self._advice.items()},
                    "known": len(self._tags), "sent": self._sent, "send_failed": self._send_fail,
                    "errors": dict(self._errors),
                    "config": {k: self.cfg[k] for k in ("near_rssi", "user_timeout_s", "adv_period_s", "adv_ttl_ms")}}
        except Exception as ex:
            self._warn("state", f"stato non disponibile ({ex!r})")
            return {"near": 0, "tags": [], "errors": dict(self._errors)}
