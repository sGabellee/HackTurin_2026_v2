"""Attraversamenti pedonali: stato di ogni attraversamento e luce da accendere (ARCHITETTURA §2.6).

Per ogni attraversamento configurato (config["crosswalks"]) a ogni tick il nodo passa:
  - la vista della camera (SituationEngine.crossing_view, None se la camera dorme);
  - il sensore sul bordo (Modulino Distance: presenza e funzionamento);
  - i CROSS Tag vicini (users.py: quanti non vedenti e pedoni);
  - l'eventuale mezzo di soccorso in arrivo e se la camera e' pronta.
Il controllore decide lo stato (dal piu' grave):

  stato      luce  quando
  emergency   5    un mezzo di soccorso arriva (dal nodo o visto dalla camera su queste strisce)
  conflict    3    qualcuno c'e' e un veicolo ha ETA <= conflict_eta_s (default 4 s)
  attention   2    qualcuno c'e' e un veicolo ha ETA <= safe_eta_s (default 8 s) o moto incerto vicino
  assist      4    un Tag "blind" e' vicino, camera pronta e fresca, nessun veicolo vicino
  unknown     6    qualcuno c'e' ma la camera non e' pronta o il fotogramma non e' fresco
  occupied    1    qualcuno aspetta o attraversa, nessun veicolo vicino
  idle        0    nessuno

"Qualcuno c'e'": presenza sul bordo OPPURE camera con waiting + crossing > 0 OPPURE Tag blind/pedestrian
vicino a quell'attraversamento.
safe = True solo con camera pronta e fotogramma fresco, nessun veicolo con ETA <= safe_eta_s (ne' veicoli
vicini con moto ancora ignoto), nessuna emergenza e stato mostrato idle/occupied/assist: mai un "via
libera" senza vedere.
Ogni stato resta almeno min_state_s (default 1,5 s) per non sfarfallare; i passaggi verso uno stato piu'
grave sono immediati. Durante la tenuta di uno stato piu' grave "safe" resta False.

Transizioni per gli eventi (§2.8): take_events() restituisce (e svuota) i messaggi "hazard" gia' pronti
del catalogo events.py: crossing_conflict ("new" quando un attraversamento entra in conflict, "cancel"
quando ne esce) e assisted_crossing ("new" quando un non vedente comincia a essere assistito con la
camera pronta, "cancel" quando il suo Tag non e' piu' vicino). transitions() da' gli stessi passaggi in
forma grezza.

Solo libreria standard. Un errore su un attraversamento non ferma gli altri ne' il nodo: log una volta,
quell'attraversamento passa in "unknown" (mai "safe").
"""
import math
import time
from collections import Counter

from events import STATION_TYPE, EventFactory

# Codici luce (§2.3) e ordine di gravita' degli stati (§2.6)
LIGHT = {"idle": 0, "occupied": 1, "attention": 2, "conflict": 3, "assist": 4, "emergency": 5, "unknown": 6}
ORDER = ("idle", "occupied", "unknown", "assist", "attention", "conflict", "emergency")
RANK = {s: i for i, s in enumerate(ORDER)}
SAFE_STATES = ("idle", "occupied", "assist")
MAX_CHANNELS = 4

# config["users"] (§2.12): valori di default e limiti. Le ultime voci servono a users.py.
USERS_DEFAULTS = {
    "near_rssi": -75, "user_timeout_s": 6.0, "adv_period_s": 1.0, "adv_ttl_ms": 3000,
    "safe_eta_s": 8.0, "conflict_eta_s": 4.0, "min_state_s": 1.5,
    "walk_mps": 0.3,           # Tag che si muove almeno cosi' dopo un CROSS_OK: sta attraversando
    "cross_window_s": 20.0,    # per quanto un CROSS_OK dato "vale" per capire che e' partito
    "cross_max_s": 40.0,       # un attraversamento non dura di piu': poi si torna ad "in attesa"
    "done_hold_s": 8.0,        # dopo l'attraversamento: niente nuovo CROSS_OK per N s
    "max_direct": 16,          # massimo di messaggi a un singolo Tag per giro (poi uno per ruolo)
    "crosswalk_offset_m": 12.0,  # distanza tipica delle strisce dal centro (posizione da "bearing")
    "assoc_max_deg": 60.0,     # associazione per direzione: scarto massimo accettato
}
USERS_LIMITS = {
    "near_rssi": (-120, 0), "user_timeout_s": (0.5, 600), "adv_period_s": (0.1, 60), "adv_ttl_ms": (200, 60000),
    "safe_eta_s": (0.5, 120), "conflict_eta_s": (0.1, 120), "min_state_s": (0.0, 30), "walk_mps": (0.05, 5),
    "cross_window_s": (1, 600), "cross_max_s": (5, 600), "done_hold_s": (0, 120), "max_direct": (0, 200),
    "crosswalk_offset_m": (0, 500), "assoc_max_deg": (5, 180),
}


def _log(testo):
    print("[attraversamenti]", testo, flush=True)


def _num(v, default=None):
    if isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _int_or_none(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if math.isfinite(f) and f == int(f) else None


def users_config(cfg):
    """Sezione "users" (§2.12) completata con i default e riportata nei limiti. Ritorna (dict, avvisi).
    cfg puo' essere la config completa o direttamente la sezione "users"."""
    w = []
    sez = cfg.get("users") if isinstance(cfg, dict) and isinstance(cfg.get("users"), dict) else None
    if sez is None:
        sez = cfg if isinstance(cfg, dict) and any(k in USERS_DEFAULTS for k in cfg) else {}
    out = dict(USERS_DEFAULTS)
    for k, v in sez.items():
        if k not in USERS_DEFAULTS:
            continue
        n = _num(v)
        lo, hi = USERS_LIMITS[k]
        if n is None:
            w.append(f"users.{k}: valore {v!r} non numerico -> {USERS_DEFAULTS[k]}")
            continue
        if not lo <= n <= hi:
            w.append(f"users.{k}: {n} fuori da {lo}..{hi} -> limitato")
            n = min(max(n, lo), hi)
        out[k] = int(n) if isinstance(USERS_DEFAULTS[k], int) else n
    if out["conflict_eta_s"] > out["safe_eta_s"]:
        w.append("users.conflict_eta_s maggiore di safe_eta_s -> uso safe_eta_s per entrambi")
        out["conflict_eta_s"] = out["safe_eta_s"]
    return out, w


def camera_zones(cfg):
    """{camera: {"zones": {...}, "approaches": {...}, "approach_bearing": {...}}} dalla config (preparata o grezza)."""
    out = {}
    if not isinstance(cfg, dict):
        return out
    cams = cfg.get("_cameras") if isinstance(cfg.get("_cameras"), dict) else None
    grezze = cfg.get("cameras") if isinstance(cfg.get("cameras"), dict) else {}
    if cams is None:
        cams = grezze or ({cfg.get("camera_id", "cam0"): cfg} if isinstance(cfg.get("zones"), dict) else {})
    for cam_id, cam in cams.items():
        if not isinstance(cam, dict):
            continue
        raw = grezze.get(cam_id) if isinstance(grezze.get(cam_id), dict) else {}
        zb = cam.get("approach_bearing") or raw.get("approach_bearing") or cfg.get("approach_bearing") or {}
        out[str(cam_id)] = {"zones": cam.get("zones") if isinstance(cam.get("zones"), dict) else {},
                            "approaches": cam.get("approaches") if isinstance(cam.get("approaches"), dict) else {},
                            "approach_bearing": zb if isinstance(zb, dict) else {}}
    return out


def _guess_waiting(zona, zones):
    """Area d'attesa di un attraversamento: stesso suffisso (crosswalk_nord -> waiting_area_nord)."""
    if not zones:
        return "waiting_area" if zona == "crosswalk" else None
    suff = zona[len("crosswalk"):]
    if ("waiting_area" + suff) in zones:
        return "waiting_area" + suff
    attese = [z for z in zones if z.startswith("waiting_area")]
    strisce = [z for z in zones if z.startswith("crosswalk")]
    if len(attese) == 1 and len(strisce) == 1:
        return attese[0]
    return None


def crosswalk_configs(cfg):
    """Lista degli attraversamenti (config["crosswalks"], §2.12) puliti. Ritorna (lista, avvisi).

    Ogni voce: {"id": int, "camera", "zone", "waiting" (str, lista o None), "curb" (indice sensore o None),
    "light" (canale 0..3 o None), "left", "right", "safe_eta_s"?, "conflict_eta_s"?, "position"?
    ([lat, lon]), "bearing"? (gradi bussola delle strisce viste dal centro dell'incrocio)}.
    Senza "crosswalks" in config: un attraversamento per ogni zona crosswalk* delle camere (id, sensore e
    canale luce in ordine), o almeno l'attraversamento 0 (sensore 0, canale 0) se le zone mancano."""
    w = []
    cams = camera_zones(cfg)
    prima_cam = next(iter(cams), None)
    raw = cfg.get("crosswalks") if isinstance(cfg, dict) else None
    if raw is None:
        raw = []
        for cam_id, cam in cams.items():
            zone = sorted((z for z in cam["zones"] if str(z).startswith("crosswalk")),
                          key=lambda z: (z != "crosswalk", z))
            for z in zone:
                i = len(raw)
                raw.append({"id": i, "camera": cam_id, "zone": z, "waiting": _guess_waiting(z, cam["zones"]),
                            "curb": i if i < MAX_CHANNELS else None, "light": i if i < MAX_CHANNELS else None})
        if not raw:
            raw = [{"id": 0, "camera": prima_cam, "zone": "crosswalk", "waiting": "waiting_area", "curb": 0, "light": 0}]
    elif not isinstance(raw, (list, tuple)):
        w.append("crosswalks deve essere una lista -> nessun attraversamento")
        raw = []
    out, ids, canali = [], set(), {}
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            w.append(f"crosswalks[{i}]: deve essere un oggetto -> ignorato")
            continue
        cid = _int_or_none(c.get("id", i))
        if cid is None:
            w.append(f"crosswalks[{i}]: id {c.get('id')!r} non intero -> uso {i}")
            cid = i
        if cid in ids:
            w.append(f"crosswalks[{i}]: id {cid} ripetuto -> ignorato")
            continue
        cam = c.get("camera") if isinstance(c.get("camera"), str) and c.get("camera") else prima_cam
        if cams and cam not in cams:
            w.append(f"crosswalk {cid}: camera '{cam}' inesistente -> vista mai disponibile (mai via libera)")
        zona = c.get("zone") if isinstance(c.get("zone"), str) and c.get("zone") else "crosswalk"
        zones = (cams.get(cam) or {}).get("zones") or {}
        if cams and cam in cams and zona not in zones:
            w.append(f"crosswalk {cid}: zona '{zona}' assente nella camera '{cam}' -> mai via libera")
        if "waiting" in c:
            attesa = c.get("waiting")
            if not (attesa is None or isinstance(attesa, str)
                    or (isinstance(attesa, (list, tuple)) and all(isinstance(x, str) for x in attesa))):
                w.append(f"crosswalk {cid}: waiting {attesa!r} non valido -> nessuna area d'attesa")
                attesa = None
        else:
            attesa = _guess_waiting(zona, zones)
        voce = {"id": cid, "camera": cam, "zone": zona,
                "waiting": list(attesa) if isinstance(attesa, (list, tuple)) else attesa,
                "curb": None, "light": None,
                "left": c.get("left") if isinstance(c.get("left"), str) and c.get("left") else None,
                "right": c.get("right") if isinstance(c.get("right"), str) and c.get("right") else None}
        for campo in ("curb", "light"):
            v = c.get(campo)
            if v is None:
                continue
            n = _int_or_none(v)
            if n is None or not 0 <= n < MAX_CHANNELS:
                w.append(f"crosswalk {cid}: {campo} {v!r} non valido (0..{MAX_CHANNELS - 1}) -> nessuno")
                continue
            voce[campo] = n
        if voce["light"] is not None:
            if voce["light"] in canali:
                w.append(f"crosswalk {cid}: canale luce {voce['light']} gia' usato da {canali[voce['light']]} "
                         "-> vince lo stato piu' grave")
            canali.setdefault(voce["light"], cid)
        for campo in ("safe_eta_s", "conflict_eta_s"):
            if c.get(campo) is not None:
                n = _num(c.get(campo))
                if n is None or not 0.1 <= n <= 120:
                    w.append(f"crosswalk {cid}: {campo} {c.get(campo)!r} non valido -> default")
                else:
                    voce[campo] = n
        pos = c.get("position")
        lat, lon = (pos[0], pos[1]) if isinstance(pos, (list, tuple)) and len(pos) == 2 else (c.get("lat"), c.get("lon"))
        lat, lon = _num(lat), _num(lon)
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            voce["position"] = [lat, lon]
        b = _num(c.get("bearing"))
        if b is not None:
            voce["bearing"] = b % 360
        ids.add(cid)
        out.append(voce)
    return out, w


def _lookup(d, k):
    """d[k] accettando chiavi intere o stringa ("0" == 0)."""
    if not isinstance(d, dict) or k is None:
        return None
    if k in d:
        return d[k]
    if str(k) in d:
        return d[str(k)]
    n = _int_or_none(k)
    return d.get(n) if n is not None else None


def curbs_by_index(curbs):
    """Sensori sul bordo in qualsiasi forma -> {indice: {"present", "ok"}}.
    Accetta {indice: {...}}, la lista di poll_sensors ([{"id", "present", "ok"}]) o il suo dict intero."""
    if isinstance(curbs, dict) and isinstance(curbs.get("curbs"), (list, tuple)):
        curbs = curbs["curbs"]
    if isinstance(curbs, (list, tuple)):
        out = {}
        for i, c in enumerate(curbs):
            if isinstance(c, dict):
                idx = _int_or_none(c.get("id", i))
                out[i if idx is None else idx] = c
        return out
    return curbs if isinstance(curbs, dict) else {}


class CrosswalkController:
    def __init__(self, cfg, node_id):
        """cfg = config completo (usa "crosswalks", "users" e le zone delle camere)."""
        self.node = str(node_id)
        cfg = cfg if isinstance(cfg, dict) else {}
        self.users_cfg, w1 = users_config(cfg)
        self.crosswalks, w2 = crosswalk_configs(cfg)
        for a in w1 + w2:
            _log(a)
        self.min_state_s = self.users_cfg["min_state_s"]
        self.factory = EventFactory(self.node, "crosswalk")
        self._errors = Counter()
        self._events = []        # messaggi hazard pronti (take_events)
        self._transitions = []   # passaggi grezzi (transitions)
        self._st = {}
        for cw in self.crosswalks:
            self._st[cw["id"]] = {"state": "idle", "since": None, "reason": "nessuno", "raw": "idle",
                                  "safe": False, "info": {}, "conflict_ev": None, "assist_ev": None}

    # ------------------------------------------------------------------ aiuti

    def _warn(self, chiave, testo):
        self._errors[chiave] += 1
        if self._errors[chiave] == 1:
            _log(testo)

    def eta_limits(self, cw):
        safe = cw.get("safe_eta_s", self.users_cfg["safe_eta_s"])
        conflict = min(cw.get("conflict_eta_s", self.users_cfg["conflict_eta_s"]), safe)
        return safe, conflict

    @staticmethod
    def _int(v):
        n = _num(v, 0)
        return max(0, int(n)) if n is not None else 0

    # ------------------------------------------------------------------ valutazione

    def _evaluate(self, cw, view, curb, usr, emergency, camera_ready):
        """Stato "grezzo" di un attraversamento (senza la tenuta minima) e le informazioni per users.py."""
        safe_eta, conflict_eta = self.eta_limits(cw)
        view = view if isinstance(view, dict) else None
        errore = view.get("error") if view else None
        fresh = bool(camera_ready) and view is not None and bool(view.get("fresh")) and not errore
        waiting = self._int(view.get("waiting")) if view else 0
        crossing = self._int(view.get("crossing")) if view else 0
        cyclists = self._int(view.get("cyclists")) if view else 0
        curb = curb if isinstance(curb, dict) else None
        curb_ok = None if curb is None else curb.get("ok") is not False
        curb_present = bool(curb and curb.get("present") and curb_ok)
        usr = usr if isinstance(usr, dict) else {}
        blind, peds = self._int(usr.get("blind")), self._int(usr.get("pedestrians"))
        present = curb_present or waiting + crossing > 0 or blind + peds > 0

        veicoli = []
        if fresh:
            for v in view.get("vehicles") or []:
                if not isinstance(v, dict):
                    continue
                eta = _num(v.get("eta_s"))
                veicoli.append({"track": v.get("track"), "class": v.get("class") or "vehicle",
                                "eta_s": None if eta is None else round(max(0.0, eta), 2),
                                "side": v.get("side") if v.get("side") in ("left", "right") else "unknown",
                                "speed": _num(v.get("speed"))})
        veicoli.sort(key=lambda v: (v["eta_s"] is None, v["eta_s"] or 0.0))
        etas = [v["eta_s"] for v in veicoli if v["eta_s"] is not None]
        min_eta = min(etas) if etas else None
        if min_eta is None and fresh and _num(view.get("min_eta_s")) is not None:
            min_eta = max(0.0, _num(view.get("min_eta_s")))  # vista senza elenco dei veicoli
        ignoto = any(v["eta_s"] is None and v["class"] != "emergency" for v in veicoli)
        soccorso_cam = any(v["class"] == "emergency" for v in veicoli)
        soccorso = emergency is not None and emergency is not False
        piu_urgente = veicoli[0] if veicoli else None
        lato = piu_urgente["side"] if piu_urgente else None

        safe = (fresh and not soccorso and not soccorso_cam and not ignoto
                and (min_eta is None or min_eta > safe_eta))
        if soccorso or soccorso_cam:
            raw = "emergency"
            eta_s = _num(emergency.get("eta_s")) if isinstance(emergency, dict) else None
            reason = "mezzo di soccorso in arrivo" + (f" (ETA {eta_s:.0f} s)" if eta_s is not None else "")
            if soccorso_cam and not soccorso:
                reason = "mezzo di soccorso visto dalla camera"
        elif present and fresh and min_eta is not None and min_eta <= conflict_eta:
            raw, reason = "conflict", f"veicolo {self._da(lato)} a {min_eta:.1f} s"
        elif present and fresh and ((min_eta is not None and min_eta <= safe_eta) or ignoto):
            if min_eta is not None and min_eta <= safe_eta:
                reason = f"veicolo {self._da(lato)} a {min_eta:.1f} s"
            else:
                reason = "veicolo vicino alle strisce, moto ancora incerto"
            raw = "attention"
        elif present and fresh and blind > 0:
            raw, reason = "assist", "utente non vedente assistito, nessun veicolo vicino"
        elif present and not fresh:
            raw = "unknown"
            if not camera_ready:
                reason = "qualcuno c'e' ma la camera non e' pronta"
            elif view is None:
                reason = "qualcuno c'e' ma la vista della camera manca"
            elif errore:
                reason = f"qualcuno c'e' ma la vista non e' affidabile ({errore})"
            else:
                reason = "qualcuno c'e' ma il fotogramma non e' recente"
        elif present:
            raw, reason = "occupied", "qualcuno aspetta o attraversa, nessun veicolo vicino"
        else:
            raw, reason = "idle", "nessuno"
        info = {"present": present, "waiting": waiting, "crossing": crossing, "cyclists": cyclists,
                "bike_lane_cyclists": self._int(view.get("bike_lane_cyclists")) if view else 0,
                "blind": blind, "pedestrians": peds, "curb": curb_present if curb is not None else None,
                "curb_ok": curb_ok, "fresh": fresh, "camera_ready": bool(camera_ready),
                "side": lato, "vehicles": veicoli[:6], "min_eta_s": min_eta, "unknown_motion": ignoto,
                "emergency": bool(soccorso or soccorso_cam), "safe_eta_s": safe_eta, "conflict_eta_s": conflict_eta,
                "most_urgent": piu_urgente}
        return raw, reason, safe, info

    @staticmethod
    def _da(lato):
        return {"left": "da sinistra", "right": "da destra"}.get(lato, "in arrivo")

    # ------------------------------------------------------------------ ciclo

    def update(self, t, views, curbs, users, emergency, camera_ready):
        """Aggiorna tutti gli attraversamenti e ritorna states().

        views:  id -> crossing_view (o None se la camera dorme)
        curbs:  indice del sensore (campo "curb" dell'attraversamento) -> {"present", "ok"}; accetta anche
                la lista o il risultato di MCULink.poll_sensors()
        users:  id -> {"blind", "pedestrians"} (UserLink.counts_by_crosswalk)
        emergency: {"eta_s", "from"} se un mezzo di soccorso arriva, altrimenti None
        """
        t = time.time() if t is None else float(t)
        views = views if isinstance(views, dict) else {}
        users = users if isinstance(users, dict) else {}
        try:
            curbs = curbs_by_index(curbs)
        except Exception:
            curbs = {}
        for cw in self.crosswalks:
            st = self._st[cw["id"]]
            try:
                curb = _lookup(curbs, cw["curb"]) if cw["curb"] is not None else None
                raw, reason, safe, info = self._evaluate(cw, _lookup(views, cw["id"]), curb,
                                                         _lookup(users, cw["id"]), emergency, camera_ready)
            except Exception as e:  # mai via libera con un errore: prudenza
                self._warn(f"valuta:{cw['id']}", f"errore sull'attraversamento {cw['id']}: {e!r} -> unknown")
                raw, reason, safe = "unknown", f"errore interno ({type(e).__name__})", False
                info = {**(st.get("info") or {}), "fresh": False, "camera_ready": bool(camera_ready)}
            try:
                self._apply(cw, st, t, raw, reason, safe, info)
            except Exception as e:
                self._warn(f"applica:{cw['id']}", f"errore nel cambio di stato di {cw['id']}: {e!r}")
                st["safe"] = False
        return self.states()

    def _apply(self, cw, st, t, raw, reason, safe_raw, info):
        cur = st["state"]
        st["raw"], st["info"] = raw, info
        if st["since"] is None:
            st["since"] = t
        if raw == cur:
            st["reason"] = reason
        elif RANK[raw] > RANK[cur] or t - st["since"] >= self.min_state_s:
            self._transition(cw, st, t, cur, raw, info)
            st["state"], st["since"], st["reason"] = raw, t, reason
        st["safe"] = bool(safe_raw) and st["state"] in SAFE_STATES
        self._assisted(cw, st, t, info)

    # ------------------------------------------------------------------ transizioni ed eventi

    def _where(self, cw):
        return {"crosswalk": cw["id"], "zone": cw["zone"], "channel": cw["light"]}

    def _transition(self, cw, st, t, old, new, info):
        self._transitions.append({"id": cw["id"], "t": t, "from": old, "to": new})
        if old == "conflict" and st.get("conflict_ev"):
            ev = self.factory.cancel(st["conflict_ev"], t, f"attraversamento ora {new}")
            ev["camera"] = cw["camera"]
            self._transitions.append({"id": cw["id"], "t": t, "kind": "crossing_conflict", "action": "cancel"})
            self._events.append(ev)
            st["conflict_ev"] = None
        if new == "conflict":
            mu = info.get("most_urgent") or {}
            eta = info.get("min_eta_s")
            sev = 3 if info.get("crossing", 0) > 0 or (eta is not None and eta <= info["conflict_eta_s"] / 2) else 2
            actor = None
            if mu:
                actor = {"track": mu.get("track"), "class": mu.get("class"),
                         "station_type": STATION_TYPE.get(mu.get("class"), 0), "side": mu.get("side")}
            ev = self.factory.hazard("crossing_conflict", t, sev, actor, {
                "where": self._where(cw),
                "details": {"min_eta_s": eta, "side": info.get("side"), "waiting": info.get("waiting", 0),
                            "crossing": info.get("crossing", 0), "curb": info.get("curb"),
                            "blind": info.get("blind", 0), "pedestrians": info.get("pedestrians", 0)}})
            ev["camera"] = cw["camera"]
            st["conflict_ev"] = ev
            self._transitions.append({"id": cw["id"], "t": t, "kind": "crossing_conflict", "action": "new",
                                      "severity": sev})
            self._events.append(ev)

    def _assisted(self, cw, st, t, info):
        """assisted_crossing: inizia quando un Tag blind e' vicino e la camera vede (fresca), finisce
        quando non ci sono piu' Tag blind vicini."""
        blind = info.get("blind", 0) or 0
        ev = st.get("assist_ev")
        if ev is None and blind > 0 and info.get("fresh"):
            ev = self.factory.hazard("assisted_crossing", t, 1, None, {
                "where": self._where(cw), "details": {"blind": blind, "state": st["state"]}})
            ev["camera"] = cw["camera"]
            st["assist_ev"] = ev
            self._transitions.append({"id": cw["id"], "t": t, "kind": "assisted_crossing", "action": "new"})
            self._events.append(ev)
        elif ev is not None and blind == 0:
            c = self.factory.cancel(ev, t, "utente non vedente non piu' vicino")
            st["assist_ev"] = None
            self._transitions.append({"id": cw["id"], "t": t, "kind": "assisted_crossing", "action": "cancel"})
            self._events.append(c)

    def take_events(self):
        """Messaggi "hazard" (crossing_conflict / assisted_crossing, new e cancel) nati dall'ultima chiamata."""
        out, self._events = self._events, []
        return out

    def transitions(self, clear=True):
        """Passaggi grezzi dall'ultima chiamata: {"id","t","from","to"} per i cambi di stato e
        {"id","t","kind","action"[,"severity"]} per gli eventi."""
        out = list(self._transitions)
        if clear:
            self._transitions = []
        return out

    # ------------------------------------------------------------------ uscite

    def states(self):
        """[{"id","state","light","safe","reason","min_eta_s","since_ms", ...}] nell'ordine della config.
        In piu' (per users.py, API e dashboard): "channel", "camera", "zone", "present", "waiting",
        "crossing", "cyclists", "bike_lane_cyclists", "blind", "pedestrians", "curb", "fresh",
        "camera_ready", "side" (lato del veicolo piu' urgente), "vehicles" (i piu' urgenti),
        "unknown_motion", "emergency", "raw" (stato senza tenuta), "safe_eta_s", "conflict_eta_s"."""
        out = []
        for cw in self.crosswalks:
            st = self._st[cw["id"]]
            info = st.get("info") or {}
            out.append({
                "id": cw["id"], "state": st["state"], "light": LIGHT[st["state"]], "safe": bool(st["safe"]),
                "reason": st["reason"], "min_eta_s": info.get("min_eta_s"),
                "since_ms": int(round(st["since"] * 1000)) if st["since"] is not None else None,
                "channel": cw["light"], "camera": cw["camera"], "zone": cw["zone"],
                "present": bool(info.get("present")), "waiting": info.get("waiting", 0),
                "crossing": info.get("crossing", 0), "cyclists": info.get("cyclists", 0),
                "bike_lane_cyclists": info.get("bike_lane_cyclists", 0),
                "blind": info.get("blind", 0), "pedestrians": info.get("pedestrians", 0),
                "curb": info.get("curb"), "fresh": bool(info.get("fresh")),
                "camera_ready": bool(info.get("camera_ready")), "side": info.get("side"),
                "vehicles": [dict(v) for v in info.get("vehicles") or []],
                "unknown_motion": bool(info.get("unknown_motion")), "emergency": bool(info.get("emergency")),
                "raw": st["raw"], "safe_eta_s": self.eta_limits(cw)[0], "conflict_eta_s": self.eta_limits(cw)[1],
            })
        return out

    def light_codes(self):
        """{canale: codice luce} dei canali usati; due attraversamenti sullo stesso canale: vince il piu' grave."""
        codes, ranks = {}, {}
        for cw in self.crosswalks:
            ch = cw["light"]
            if ch is None:
                continue
            s = self._st[cw["id"]]["state"]
            if ch not in ranks or RANK[s] > ranks[ch]:
                codes[ch], ranks[ch] = LIGHT[s], RANK[s]
        return codes

    def state(self):
        """Per l'API: attraversamenti, configurazione e contatori di errori."""
        return {"crosswalks": self.states(), "light_codes": self.light_codes(),
                "config": [dict(cw) for cw in self.crosswalks],
                "users": {k: self.users_cfg[k] for k in ("safe_eta_s", "conflict_eta_s", "min_state_s")},
                "errors": dict(self._errors)}
