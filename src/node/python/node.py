"""Il nodo incrocio v2: offline, peer-to-peer, a basso consumo, al servizio degli utenti della strada.

                 sensore sul bordo (MCU) ---+                     +--> SignalController --> MCU (LED + luci strisce)
                 CROSS Tag / vicini (radio) +--> PowerManager ----+--> main.py (avvia/ferma l'inferenza)
                                                  |
detections --> SituationEngine (una per camera) --+--> hazard / track --> Storage, Stats, radio (route.targets_for)
                       |
                       +--> crossing_view --> CrosswalkController --> luci sopra le strisce
                                                     |
                 CROSS Tag vicini --> UserLink <-----+--> avvisi "adv" ai Tag (pedoni, non vedenti, ciclisti,
                                                          conducenti, soccorso)
Incroci vicini <--> PeerLink (SC-P2P v2: ESP-NOW via ESP32 su USB, UDP, bus virtuale del simulatore)
Collettore (facoltativo) --> /api/export (pull) o richiesta "rq" via radio --> risposta "rs"

process() va chiamato a ogni fotogramma del modello (a riposo i fotogrammi vengono scartati),
tick() ogni tick_s (default 0,25 s) da un thread a parte: sensori, energia, attraversamenti, avvisi.
Nessun componente puo' far cadere il nodo: ogni chiamata passa da _safe().
"""
import threading
import time
from collections import OrderedDict
from pathlib import Path

from api import LocalAPI
from config import public_config
from crosswalk import CrosswalkController
from events import EventFactory
from fusion import CameraFusion
from health import Health
from mcu import MCULink
from peer import PeerLink
from power import PowerManager
from publisher import Publisher
from route import Topology
from signals import SignalController
from situation import SituationEngine
from stats import StatsAggregator
from storage import Storage
from users import UserLink

REMOTE_SEEN_MAX = 2000       # pericoli remoti gia' gestiti (lo stesso puo' arrivare da due trasporti)
ACTIVE_MAX = 500             # pericoli attivi ricordati per gli avvisi ai Tag
EMERGENCY_KINDS = ("emergency_vehicle", "emergency_corridor")
VIEW_FRESH_S = 1.0           # fotogramma piu' vecchio di cosi': la camera non "vede" (mai via libera)


class Node:
    def __init__(self, cfg, ui=None, base_dir=".", bridge=None, start_threads=True, mcu=None, sim_bus=None,
                 topology=None, clock=None):
        """mcu: oggetto con l'interfaccia di mcu.MCULink (FakeMCU nei test e nel simulatore); se manca e
        c'e' il Bridge di App Lab si usa MCULink. sim_bus: bus radio virtuale del simulatore.
        topology: route.Topology gia' caricata (il simulatore la condivide tra tutti i nodi).
        clock: funzione che da' i secondi (default time.time; il simulatore usa il suo tempo)."""
        self.clock = clock or time.time
        self.cfg = cfg
        self.node_id = cfg.get("node_id", "node")
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()
        self.started = self.clock()
        self.tick_s = float(cfg.get("tick_s", 0.25) or 0.25)
        data = cfg.get("data") or {}
        self.power_listeners = []    # f(old, new): main.py avvia/ferma l'inferenza
        self.dropped_frames = 0

        self.storage = None
        if data.get("store_db", True):
            try:
                self.storage = Storage(self.base_dir / data.get("db_file", "smartcrossing.db"),
                                       data.get("retention_days", 30))
            except Exception as e:  # disco o permessi: il nodo funziona lo stesso, senza archivio
                print("[nodo] archivio SQLite non disponibile:", repr(e))
        self.publisher = Publisher(cfg.get("publish") or {}, self.node_id, storage=self.storage, ui=ui,
                                   base_dir=self.base_dir, store_scene=bool(data.get("store_scene", False)))
        self.health = Health(cfg, self.node_id)

        # topologia della citta' e posizione del nodo
        self.topology = topology if topology is not None else self._load_topology()
        self.position = self._position()
        self._fill_neighbors()

        self.engines = {cam: SituationEngine(cfg, on_event=self._on_message, camera_id=cam)
                        for cam in cfg["_cameras"]}
        self.stats = StatsAggregator(cfg, self.node_id)
        self.fusion = CameraFusion(cfg, self.node_id) if len(self.engines) > 1 else None
        self.factory = EventFactory(self.node_id, "node")

        # MCU, energia, segnali
        mcu_cfg = cfg.get("mcu") or {}
        if mcu is None and bridge is not None:
            mcu = MCULink(bridge, mcu_cfg, start_thread=start_threads)
        self.mcu = mcu
        self.curbs = int(mcu_cfg.get("curbs", 1) or 0) if mcu is not None else 0
        self.signals = SignalController(cfg.get("signals") or {}, mcu)
        self.power = PowerManager(cfg.get("power") or {}, on_change=self._on_power)
        self.crosswalk = CrosswalkController(cfg, self.node_id)

        # radio e utenti della strada
        self.peer = PeerLink(cfg.get("p2p") or {}, self.node_id, on_message=self._on_peer,
                             start_thread=start_threads, sim_bus=sim_bus,
                             position=[self.position["lat"], self.position["lon"]] if self.position else None,
                             topology=self.topology if self._in_topology() else None)
        self.users = UserLink(cfg, self.node_id, send=self.peer.send_obj)

        self._remote_seen = OrderedDict()
        self._active = OrderedDict()       # id -> hazard attivo (locale o remoto) per gli avvisi ai Tag
        self._emergencies = {}             # chiave -> {"from", "until", "heading"}
        self._corridors = {}               # dispositivo del soccorso -> {"ev", "last", "seen"}
        self._sensor = {"none_since": None, "fault": False, "fault_ev": None}
        self.latest_scene = []
        self.latest_status = None
        self.latest_stats = None
        self.latest_crosswalks = []
        self._last_update = {cam: None for cam in self.engines}
        self._last_scene = 0.0
        self._last_slow = 0.0
        self._last_cleanup = 0.0
        self.api = None
        if (cfg.get("api") or {}).get("enabled"):
            self.api = LocalAPI(cfg["api"], self)
            if start_threads:
                self.api.start()

    # ------------------------------------------------------------------ topologia

    def _load_topology(self):
        nome = self.cfg.get("topology")
        if not nome:
            return None
        p = Path(nome)
        if not p.is_absolute():
            p = self.base_dir / p
            if not p.exists():
                p = Path(__file__).resolve().parent / nome
        try:
            topo = Topology.load(p)
            return topo if topo.ids() else None
        except Exception as e:  # senza topologia: avvisi a tutti i vicini radio, nessun percorso
            print(f"[nodo] topologia {p} non disponibile ({e!r}): pericoli inoltrati senza percorso")
            return None

    def _in_topology(self):
        return self.topology is not None and self.topology.pos(self.node_id) is not None

    def _position(self):
        pos = self.cfg.get("position")
        if isinstance(pos, dict) and pos.get("lat") is not None:
            return {"lat": float(pos["lat"]), "lon": float(pos["lon"])}
        if self._in_topology():
            lat, lon = self.topology.pos(self.node_id)
            return {"lat": lat, "lon": lon}
        if self.topology is not None:
            print(f"[nodo] '{self.node_id}' non e' nella topologia: controlla node_id "
                  f"(build_topology.py --mark lat,lon stampa l'incrocio piu' vicino)")
        return None

    def _fill_neighbors(self):
        """Accesso della camera -> incrocio vicino, dalla direzione dell'accesso (approach_bearing) e dalla
        topologia: serve alle regole per "next_node". Non tocca i neighbors scritti a mano."""
        if not self._in_topology():
            return
        nb = dict(self.cfg.get("neighbors") or {})
        for cam in (self.cfg.get("_cameras") or {}).values():
            for accesso, gradi in ((cam or {}).get("approach_bearing") or {}).items():
                if accesso in nb:
                    continue
                vicino = self._safe(self.topology.first_toward, self.node_id, gradi)
                if vicino:
                    nb[accesso] = vicino
        self.cfg["neighbors"] = nb

    def _heading_of(self, msg):
        """Direzione (gradi bussola) di un pericolo: dall'accesso verso cui va l'attore."""
        actor = msg.get("actor") if isinstance(msg.get("actor"), dict) else {}
        towards = actor.get("towards")
        for cam in (self.cfg.get("_cameras") or {}).values():
            b = ((cam or {}).get("approach_bearing") or {}).get(towards)
            if isinstance(b, (int, float)):
                return float(b) % 360
        return msg.get("heading") if isinstance(msg.get("heading"), (int, float)) else None

    def _targets(self, msg, heading=None):
        """Incroci da avvisare per un pericolo nato qui (None = vicini radio, senza percorso)."""
        if not self._in_topology():
            return None
        actor = msg.get("actor") if isinstance(msg.get("actor"), dict) else {}
        dst = msg.get("destination")
        return self.topology.targets_for(self.node_id, heading, msg.get("category", "risk"),
                                         actor.get("speed_kmh"), dst) or None

    # ------------------------------------------------------------------ ingresso: fotogrammi

    def process(self, detections, camera_id=None, t=None):
        """Da chiamare a ogni fotogramma con il dict del brick Video Object Detection."""
        cam = camera_id or next(iter(self.engines))
        t = self.clock() if t is None else t
        if not self.power.awake:
            self.dropped_frames += 1  # a riposo non si elabora (il brick andrebbe fermato da main.py)
            return []
        with self._lock:
            engine = self.engines.get(cam)
            if engine is None:
                self.health.on_error("process", KeyError(f"camera sconosciuta {cam}"))
                return []
            t0 = time.perf_counter()
            try:
                out = engine.update(detections, t)
            except Exception as e:  # mai fermare il nodo per un fotogramma
                self.health.on_error("engine", e)
                return []
            proc_ms = (time.perf_counter() - t0) * 1000
            self._last_update[cam] = t
            n_det = sum(len(v) for v in (detections or {}).values() if isinstance(v, (list, tuple))) \
                if isinstance(detections, dict) else 0
            self.health.on_frame(t, cam, proc_ms, n_det)
            recenti = engine.tracker_recent(t)
            if recenti:
                self.power.activity(t)  # qualcuno c'e': il nodo resta sveglio
            self._safe(self.stats.on_frame, t, cam, recenti, engine.space, engine.zones_of)
            self._safe(self.signals.set_presence, {tr.label for tr in recenti}, t, [tr.id for tr in recenti])
            self._safe(self.signals.push, t)
            self._scene(t)
            return out

    # ------------------------------------------------------------------ ciclo

    def tick(self, t=None):
        """Ogni tick_s: sensori, energia, attraversamenti, luci, avvisi ai Tag; ogni secondo il resto."""
        t = self.clock() if t is None else t
        with self._lock:
            sensori = self._safe(self._read_sensors, t)
            self._safe(self.power.tick, t)
            self._safe(self._expire, t)
            self._safe(self._crossings, t, sensori)
            if t - self._last_slow >= 1.0 or t < self._last_slow:
                self._last_slow = t
                self._slow(t)
        self._safe(self.peer.tick, t)
        self._safe(self.signals.tick, t, self.peer.online)

    def _read_sensors(self, t):
        """Sensore sul bordo: presenza = risveglio; guasto o MCU muto = nodo sveglio + evento una volta."""
        if self.mcu is None or self.curbs <= 0:
            return None
        s = self.mcu.poll_sensors()
        guasto = False
        if s is None:  # niente risposta dall'MCU: dopo sensor_missing_s conta come guasto
            ns = self._sensor["none_since"] = self._sensor["none_since"] or t
            guasto = t - ns >= float((self.cfg.get("power") or {}).get("sensor_missing_s", 5))
        else:
            self._sensor["none_since"] = None
            for c in (s.get("curbs") or [])[:self.curbs]:
                if not c.get("ok", True):
                    guasto = True
                elif c.get("present"):
                    self.power.trigger("curb", t)
        if guasto and not self._sensor["fault"]:
            self._sensor["fault"] = True
            self.power.trigger("sensor_fault", t)
            ev = self.factory.hazard("wake_sensor_fault", t, 1, None, {
                "details": {"reason": "MCU muto" if s is None else "sensore sul bordo non risponde"}})
            self._sensor["fault_ev"] = ev
            self._on_message(ev)
        elif not guasto and self._sensor["fault"]:
            self._sensor["fault"] = False
            self.power.clear_fault(t)
            if self._sensor["fault_ev"]:
                self._on_message(self.factory.cancel(self._sensor["fault_ev"], t, "sensore di nuovo funzionante"))
                self._sensor["fault_ev"] = None
        return s

    def _crossings(self, t, sensori):
        sveglio = self.power.awake
        views = {}
        if sveglio:
            for cw in self.crosswalk.crosswalks:
                eng = self.engines.get(cw["camera"])
                if eng is not None:
                    v = self._safe(eng.crossing_view, t, cw["zone"], cw["waiting"], cw.get("left"), cw.get("right"))
                    last = self._last_update.get(cw["camera"])
                    if isinstance(v, dict) and (last is None or t - last > VIEW_FRESH_S):
                        v = dict(v, fresh=False)  # doppia sicurezza: niente fotogrammi recenti = non si vede
                    views[cw["id"]] = v
        users = self._safe(self.users.counts_by_crosswalk, t) or {}
        emergenza = self._emergency(t)
        states = self.crosswalk.update(t, views, sensori, users, emergenza, sveglio)
        self.latest_crosswalks = states
        for ev in self.crosswalk.take_events():
            self._on_message(ev)
        self._safe(self.signals.set_crosswalks, self.crosswalk.light_codes())
        self._safe(self.users.advise, t, states, list(self._active.values()), emergenza, sveglio)

    def _slow(self, t):
        sveglio = self.power.awake
        for cam, engine in self.engines.items():
            last = self._last_update[cam]
            if sveglio and (last is None or t - last >= 1.0):  # nessun fotogramma: il tempo passa lo stesso
                try:
                    engine.update({}, t, frame=False)
                except Exception as e:
                    self.health.on_error("engine_tick", e)
        for msg in self._safe(self.stats.maybe_flush, t) or []:
            self.latest_stats = msg
            self._publish(msg)
        extra = {
            "power": self._safe(self.power.stats, t),
            "active_tracks": sum(len(e.tracker.tracks) for e in self.engines.values()),
            "db_bytes": self._safe(self.storage.size_bytes) if self.storage else None,
            "rule_errors": {f"{c}:{k}": v for c, e in self.engines.items() for k, v in e.rule_errors.items()},
            "p2p": self._safe(self.peer.state),
            "signals": self._safe(self.signals.state),
            "crosswalks": [{k: s.get(k) for k in ("id", "state", "light", "safe", "reason")}
                           for s in self.latest_crosswalks],
            "tags_near": len(self._safe(self.users.near, t) or []),
            "dropped_frames": self.dropped_frames,
            "spaces": {c: e.space for c, e in self.engines.items()},
        }
        status = self._safe(self.health.maybe_status, t, extra)
        if status:
            self.latest_status = status
            self._publish(status)
        if self.storage and t - self._last_cleanup > 3600:
            self._last_cleanup = t
            self._safe(self.storage.cleanup, t)

    def _on_power(self, old, new, source=None, t=None):
        """Cambio di stato di energia: segnali, heartbeat ai vicini, inferenza (main.py)."""
        if new == "sleep":  # camera ferma: i fotogrammi di prima non contano piu' (mai via libera al risveglio)
            with self._lock:
                for cam, eng in self.engines.items():
                    self._last_update[cam] = None
                    eng._last_frame_t = None
        self._safe(self.signals.set_power_state, new)
        self._safe(self.signals.push, t)
        self._safe(self.peer.set_power, new)
        for f in list(self.power_listeners):
            self._safe(f, old, new)

    # ------------------------------------------------------------------ pericoli e soccorso

    def _expire(self, t):
        ms = t * 1000
        for k in [k for k, h in self._active.items() if (h.get("expires") or ms + 1) < ms]:
            self._active.pop(k, None)
        for k in [k for k, e in self._emergencies.items() if e["until"] < t]:
            self._emergencies.pop(k, None)
        timeout = float((self.cfg.get("emergency") or {}).get("beacon_timeout_s", 8))
        for dev in [d for d, c in self._corridors.items() if t - c["seen"] > timeout]:
            c = self._corridors.pop(dev)
            if c.get("ev"):  # il mezzo di soccorso non si sente piu': fine del corridoio
                self._on_message(self.factory.cancel(c["ev"], t, "mezzo di soccorso non piu' in zona"))

    def _emergency(self, t):
        """Mezzo di soccorso in arrivo (dalla radio): {"eta_s", "from"} o None."""
        vivi = [e for e in self._emergencies.values() if e["until"] >= t]
        if not vivi:
            return None
        e = min(vivi, key=lambda e: e.get("eta_s") if e.get("eta_s") is not None else 1e9)
        return {"eta_s": e.get("eta_s"), "from": e.get("from")}

    def _remember(self, m):
        hid = m.get("id")
        if not hid:
            return
        if m.get("action") == "cancel":
            self._active.pop(hid, None)
            return
        self._active[hid] = m
        self._active.move_to_end(hid)
        while len(self._active) > ACTIVE_MAX:
            self._active.popitem(last=False)

    def _corridor(self, msg, t):
        """Un mezzo di soccorso si sente via radio: corridoio verso gli incroci che incontrera'."""
        dev = msg.get("device") or "?"
        ecfg = self.cfg.get("emergency") or {}
        c = self._corridors.setdefault(dev, {"ev": None, "last": None, "seen": t})
        c["seen"] = t
        self._emergencies["tag:" + dev] = {"from": dev, "until": t + float(ecfg.get("beacon_timeout_s", 8)),
                                           "heading": msg.get("heading"), "eta_s": None}
        if c["last"] is not None and t - c["last"] < float(ecfg.get("corridor_every_s", 10)):
            return
        c["last"] = t
        hd = msg.get("heading")
        ev = self.factory.hazard("emergency_corridor", t, 3, {"class": "emergency", "device": dev}, {
            "heading": hd, "details": {"device": dev, "siren": bool(msg.get("siren")),
                                       "next_node": msg.get("next_node"), "rssi": msg.get("rssi")}})
        if msg.get("next_node"):
            ev["destination"] = msg["next_node"]
        if c["ev"] is not None:
            ev["id"] = c["ev"]["id"]  # stesso corridoio: aggiorno, non ne apro un altro
        c["ev"] = ev
        self._on_message(ev, heading=hd)

    def _on_message(self, msg, heading=None):
        """Messaggi del nodo: hazard (regole, attraversamenti, sensori, corridoio) e track."""
        tipo = msg.get("type")
        if tipo == "hazard":
            t = msg.get("ts", 0) / 1000 or self.clock()
            self._safe(self.signals.on_hazard, msg, True, t)
            self._safe(self.signals.push, t)
            self._remember(msg)
            if msg.get("category") in ("risk", "violation", "emergency"):
                self.power.activity(t)
        self._publish(msg)
        if tipo == "hazard":
            self._safe(self.stats.on_event, msg)
            hd = heading if heading is not None else self._heading_of(msg)
            uscita = dict(msg)
            if hd is not None:
                uscita["heading"] = hd
            elif isinstance(uscita.get("actor"), dict):  # direzione nel riferimento della camera: non vale fuori
                uscita["actor"] = {k: v for k, v in uscita["actor"].items() if k != "heading_deg"}
            self._safe(self.peer.send_hazard, uscita, self._safe(self._targets, msg, hd))
        elif tipo == "track":
            self._safe(self.stats.on_track, msg)
            if self.fusion:
                for m in self._safe(self.fusion.on_track, msg) or []:
                    self._publish(m)

    def _on_peer(self, msg):
        """Messaggio dalla radio: pericolo di un incrocio, beacon di un Tag, richiesta del collettore."""
        if not isinstance(msg, dict):
            return
        t = self.clock()
        tipo = msg.get("type")
        with self._lock:
            if tipo == "vru_beacon":
                r = self._safe(self.users.on_beacon, msg, t)
                if r and r.get("near"):
                    self.power.trigger("radio_vru", t, detail={"role": r.get("role")})
                    if r.get("new"):
                        self._publish({"v": 2, "type": "vru_presence", "node": self.node_id, "ts": int(t * 1000),
                                       "device": r["device"], "role": r["role"], "rssi": msg.get("rssi")})
            elif tipo == "emergency_beacon":
                self._safe(self.users.on_beacon, msg, t)
                self.power.trigger("radio_emergency", t)
                self._safe(self._corridor, msg, t)
            elif tipo == "summary_request":
                self._safe(self._summary, msg, t)
            elif tipo == "remote_hazard":
                self._on_remote(msg, t)

    def _on_remote(self, msg, t):
        m = dict(msg)
        if m.get("id"):
            chiave = (m["id"], m.get("action", "new"))
            if chiave in self._remote_seen:
                return
            self._remote_seen[chiave] = t
            while len(self._remote_seen) > REMOTE_SEEN_MAX:
                self._remote_seen.popitem(last=False)
        self._publish(m)
        self._safe(self.signals.on_hazard, m, False, t)
        self._safe(self.signals.push, t)
        self._safe(self.stats.on_event, m)
        self._remember(m)
        per_me = self.node_id in (m.get("to") or []) or not m.get("to")
        if m.get("kind") in EMERGENCY_KINDS or m.get("category") == "emergency":
            if m.get("action") == "cancel":
                self._emergencies.pop("hz:" + str(m.get("id")), None)
            elif per_me:
                until = min((m.get("expires") or (t + 60) * 1000) / 1000, t + 120)
                self._emergencies["hz:" + str(m.get("id"))] = {"from": m.get("from_node"), "until": until,
                                                                "heading": m.get("heading"), "eta_s": None}
                self.power.trigger("radio_emergency", t)
        elif per_me and m.get("action") != "cancel" and m.get("to"):
            self.power.trigger("radio_hazard", t)

    def _summary(self, req, t):
        """Risposta "rs" a una richiesta "rq" del collettore (riepilogo compatto, sta in 240 byte)."""
        st = self._safe(self.power.stats, t) or {}
        rs = {"t": "rs", "id": req.get("id"), "h": 0,
              "p": {"pw": self.power.code, "hz": sum(1 for h in self._active.values() if h.get("type") == "hazard"),
                    "u": len(self._safe(self.users.near, t) or []), "wk": st.get("wakeups"),
                    "sv": st.get("saved_pct")}}
        self._safe(self.peer.send_obj, rs)

    # ------------------------------------------------------------------ uscite

    def _publish(self, msg):
        self._safe(self.publisher.publish, msg)

    def _scene(self, t):
        hz = (self.cfg.get("data") or {}).get("scene_hz", 1.0) or 0
        if hz <= 0 or t - self._last_scene < 1.0 / hz:
            return
        self._last_scene = t
        objs = []
        for e in self.engines.values():
            objs += self._safe(e.scene, t) or []
        self.latest_scene = objs
        self._publish({"v": 1, "type": "scene", "node": self.node_id, "ts": int(t * 1000), "objects": objs})

    def _safe(self, fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except Exception as e:  # un modulo accessorio non deve mai fermare il nodo
            self.health.on_error(getattr(fn, "__qualname__", str(fn)), e)
            return None

    # ------------------------------------------------------------------ per l'API

    def status_snapshot(self):
        t = self.clock()
        return {"node": self.node_id, "position": self.position,
                "name": self.topology.name(self.node_id) if self._in_topology() else None,
                "status": self.latest_status, "stats": self.latest_stats, "scene": self.latest_scene,
                "power": self._safe(self.power.stats, t), "crosswalks": self.latest_crosswalks,
                "users": self._safe(self.users.near, t) or [], "peers": self._safe(self.peer.peers) or {},
                "p2p": self._safe(self.peer.state), "signals": self._safe(self.signals.state),
                "hazards": [h for h in self._active.values()][-50:],
                "emergency": self._emergency(t), "config": public_config(self.cfg)}

    def close(self):
        for comp in (self.api, self.peer, self.signals, self.mcu, self.publisher, self.storage):
            if comp is not None and hasattr(comp, "close"):
                self._safe(comp.close)
