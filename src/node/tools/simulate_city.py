"""Simulatore della citta': tutti gli incroci della topologia come nodi CROSS veri, sul PC.

Ogni incrocio di src/node/python/topology.json diventa un Node completo (regole, energia, attraversamenti,
avvisi ai Tag, radio SC-P2P v2) con un MCU finto (mcu.FakeMCU) e il bus radio virtuale (peer.SimRadioBus:
un messaggio arriva solo agli incroci e ai Tag entro la portata, con RSSI calcolato dalla distanza).
I CROSS Tag sono virtuali: mandano i beacon e stampano gli avvisi che ricevono, come vibrerebbero.

Scenari (--scenario):
  blind_crossing    un non vedente attraversa all'incrocio della board: attesa, auto in arrivo, via libera
  ambulance_route   un'ambulanza attraversa la citta': gli incroci si svegliano e aprono il corridoio
  bike_lane         un'auto invade la ciclabile: il ciclista vicino viene avvisato, il pericolo va avanti
  pedestrian_night  di notte il nodo dorme; un pedone sul bordo lo sveglia, poi torna a riposo
  demo              tutti gli scenari uno dopo l'altro (con --loop all'infinito)
  idle              nessuno scenario: la citta' a riposo (per guardare la dashboard)

Uso:
  python simulate_city.py --scenario demo                     tempo reale, API su http://127.0.0.1:8200
  python simulate_city.py --scenario demo --loop --udp        + traffico radio copiato sul multicast UDP
  python simulate_city.py --scenario ambulance_route --fast   tempo accelerato, solo il resoconto
  python simulate_city.py --nodes 60                          solo i 60 incroci piu' vicini alla board

Con --udp il collettore in ascolto vede la radio in diretta. Per la dashboard, in un altro terminale:
  python src/collector/collector.py --config src/collector/collector_sim.json --port 8000
(collector_sim.json viene scritto all'avvio con gli indirizzi delle API dei nodi: /n/<id>/api/...).
I log dei nodi vanno in <out>/sim.log; a schermo solo il racconto degli scenari.
"""
import argparse
import contextlib
import copy
import json
import math
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

QUI = Path(__file__).resolve().parent
PY = QUI.parent / "python"
sys.path.insert(0, str(PY))
sys.path.insert(0, str(QUI))

import simulate as sim  # noqa: E402  (camera e detection finte)
from api import LocalAPI  # noqa: E402
from config import prepare  # noqa: E402
from mcu import FakeMCU, unpack_xwalk  # noqa: E402
from node import Node  # noqa: E402
from peer import SimRadioBus  # noqa: E402
from route import Topology, bearing_deg, dist_m  # noqa: E402
from users import CODES  # noqa: E402

BOARD = "incrocio-124"          # Via Bricherasio / Via Vela: l'incrocio del prototipo
CODE_NAMES = {v: k for k, v in CODES.items()}
LUCI = {0: "spenta", 1: "accesa", 2: "attenzione", 3: "PERICOLO", 4: "assistenza", 5: "EMERGENZA", 6: "incerta"}
STEP = 0.25                     # passo della simulazione = tick_s dei nodi = 4 fotogrammi al secondo
CAMERA_WARMUP_S = 1.0           # la camera di un nodo appena sveglio manda fotogrammi dopo 1 s
MARKER = ".cross_sim"
OUT = sys.__stdout__


def say(testo):
    """Il racconto per chi guarda (i log dei nodi vanno nel file)."""
    try:
        OUT.write(testo + "\n")
        OUT.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------- orologio e radio

class Orologio:
    """Tempo reale o accelerato (--fast): tutti i nodi e i Tag usano questo."""

    def __init__(self, fast):
        self.fast = fast
        self.t = time.time()

    def now(self):
        return self.t if self.fast else time.time()

    def advance(self, dt):
        if self.fast:
            self.t += dt
        else:
            time.sleep(max(0.0, dt))


class BusSpecchio(SimRadioBus):
    """Bus radio virtuale che copia ogni frame anche sul multicast UDP (solo verso il collettore in
    ascolto, che non trasmette): cosi' la dashboard vede la radio della citta' simulata."""

    def __init__(self, range_m, udp=False, group="239.255.42.99", port=47999):
        super().__init__(range_m=range_m)
        self._dest = (group, port)
        self._sock = None
        if udp:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
                self._sock = s
            except OSError as e:
                say(f"[sim] multicast UDP non disponibile ({e!r}): la dashboard vedra' solo i pull")

    def broadcast(self, endpoint_id, data):
        n = super().broadcast(endpoint_id, data)
        if self._sock is not None:
            try:
                self._sock.sendto(data if isinstance(data, bytes) else str(data).encode(), self._dest)
            except OSError:
                pass
        return n


# ---------------------------------------------------------------------------- Tag virtuali

class Tag:
    """CROSS Tag virtuale: beacon vru/emv sul bus e registro degli avvisi ricevuti (come vibrerebbe)."""

    def __init__(self, citta, dev, role, pos, heading=None, speed=None):
        self.citta, self.dev, self.role = citta, dev, role
        self.pos, self.heading, self.speed = list(pos), heading, speed
        self.next_node = None
        self.q = 0
        self.next_t = 0.0
        self.code = None
        self.storia = []
        citta.bus.attach(dev, self.pos, self._ricevi)

    def _ricevi(self, data, rssi):
        try:
            o = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        except Exception:
            return
        if o.get("t") != "adv" or not (self.role in (o.get("r") or []) or o.get("to") == self.dev):
            return
        c = o.get("c")
        if c != self.code:
            self.code = c
            self.storia.append(CODE_NAMES.get(c, c))
            say(f"    [{self.dev}] {CODE_NAMES.get(c, c):<22} da {o.get('n')}  gravita' {o.get('s')}")

    def move(self, pos):
        self.pos = list(pos)
        self.citta.bus.move(self.dev, self.pos)

    def beacon(self, t):
        periodo = 0.5 if self.role == "emergency" else 1.0
        if t < self.next_t:
            return
        self.next_t = t + periodo
        self.q += 1
        if self.role == "emergency":
            o = {"v": 2, "t": "emv", "d": self.dev, "q": self.q, "st": "run"}
            if self.next_node:
                o["nx"] = self.next_node
        else:
            o = {"v": 2, "t": "vru", "d": self.dev, "p": self.role, "q": self.q}
            if self.speed is not None:
                o["sp"] = round(self.speed, 1)
        if self.heading is not None:
            o["hd"] = int(self.heading) % 360
        self.citta.bus.broadcast(self.dev, json.dumps(o, separators=(",", ":")).encode())

    def close(self):
        self.citta.bus.detach(self.dev)
        self.citta.tags.pop(self.dev, None)


def vicino(pos, metri, gradi):
    """Punto a `metri` da pos in direzione `gradi`."""
    lat, lon = pos
    d = metri / 6371000.0
    b = math.radians(gradi)
    return [lat + math.degrees(d * math.cos(b)), lon + math.degrees(d * math.sin(b) / math.cos(math.radians(lat)))]


# ---------------------------------------------------------------------------- la citta'

def cfg_nodo(topo, nid, out):
    """Configurazione di un incrocio simulato: la camera di simulate.py orientata sulla prima strada."""
    cam = {k: copy.deepcopy(sim.CFG[k]) for k in ("frame_size", "calibration", "zones", "lines", "lanes", "approaches")}
    cam["zones"]["waiting_area"] = [[0.0, 0.58], [0.10, 0.58], [0.10, 0.72], [0.0, 0.72]]
    nb = topo.neighbors(nid)
    b = topo.bearing(nid, nb[0]) if nb else 0.0
    cam["approach_bearing"] = {"via": round(b), "incrocio": round((b + 180) % 360)}
    lat, lon = topo.pos(nid)
    raw = {"node_id": nid, "position": {"lat": lat, "lon": lon}, "cameras": {"cam0": cam},
           "traffic_light": {"mode": "none"},
           "crosswalks": [{"id": 0, "camera": "cam0", "zone": "crosswalk", "waiting": "waiting_area",
                           "curb": 0, "light": 0, "left": "via", "right": "incrocio", "bearing": round(b)}],
           "power": {"boot_active_s": 5, "active_hold_s": 15, "cooldown_s": 3},
           "p2p": {"enabled": True, "transports": ["sim"]},
           "api": {"enabled": False, "port": 0},
           "data": {"db_file": "nodo.db", "status_interval_s": 10, "stats_interval_s": 60}}
    cfg, _ = prepare(raw)
    return cfg


class Citta:
    def __init__(self, topo, out, orologio, ids, udp=False, range_m=180.0):
        self.topo, self.out, self.clock = topo, out, orologio
        self.bus = BusSpecchio(range_m, udp=udp)
        self.nodi, self.mcu, self.api, self.feed, self.tags = {}, {}, {}, {}, {}
        self.risvegli = []          # (t, nodo, da, a) per il racconto
        self.sveglio_da = {}        # nodo -> istante del risveglio (la camera parte dopo CAMERA_WARMUP_S)
        for nid in ids:
            cfg = cfg_nodo(topo, nid, out)
            base = out / nid
            base.mkdir(parents=True, exist_ok=True)
            self.mcu[nid] = FakeMCU(cfg["mcu"], clock=orologio.now)
            n = Node(cfg, base_dir=base, start_threads=False, mcu=self.mcu[nid], sim_bus=self.bus,
                     topology=topo, clock=orologio.now)
            n.power_listeners.append(lambda old, new, nid=nid: self._energia(nid, old, new))
            self.sveglio_da[nid] = orologio.now()
            self.nodi[nid] = n
            self.api[nid] = LocalAPI({"port": 0}, n)

    def _energia(self, nid, old, new):
        t = self.clock.now()
        self.risvegli.append((t, nid, old, new))
        if new == "sleep":
            self.sveglio_da.pop(nid, None)
        elif old == "sleep":
            self.sveglio_da[nid] = t

    # ---- attori
    def tag(self, dev, role, pos, **kw):
        t = self.tags[dev] = Tag(self, dev, role, pos, **kw)
        return t

    def camera(self, nid, gen):
        """Detection finte per la camera dell'incrocio (None = nessuno davanti)."""
        if gen is None:
            self.feed.pop(nid, None)
        else:
            self.feed[nid] = (gen, self.clock.now())

    # ---- ciclo
    def pompa(self):
        for _ in range(30):
            if sum(n.peer.poll_once(0) for n in self.nodi.values()) == 0:
                return

    def passo(self):
        t = self.clock.now()
        for tag in list(self.tags.values()):
            tag.beacon(t)
        for nid, (gen, t0) in list(self.feed.items()):
            n = self.nodi[nid]
            if not n.power.awake or t - self.sveglio_da.get(nid, t) < CAMERA_WARMUP_S:
                continue  # camera spenta o ancora in avvio
            det = {}
            for label, x1, y1, x2, y2 in gen(t - t0):
                det.setdefault(label, []).append({"confidence": 0.9, "bounding_box_xyxy": (x1, y1, x2, y2)})
            self.nodi[nid].process(det, "cam0", t)
        for nid, n in self.nodi.items():  # camere accese dei nodi svegli: fotogrammi senza nessuno
            if nid not in self.feed and n.power.awake and t - self.sveglio_da.get(nid, t) >= CAMERA_WARMUP_S:
                n.process({}, "cam0", t)
        self.pompa()
        for n in self.nodi.values():
            n.tick(t)
        self.pompa()

    def gira(self, secondi, ogni=None):
        """Fa scorrere il tempo; ogni(t) viene chiamata a ogni passo (per muovere gli attori)."""
        fine = self.clock.now() + secondi
        while self.clock.now() < fine - 1e-6:
            inizio = time.time()
            if ogni:
                ogni(self.clock.now())
            self.passo()
            self.clock.advance(STEP if self.clock.fast else STEP - (time.time() - inizio))

    def aspetta(self, cond, max_s):
        fine = self.clock.now() + max_s
        while self.clock.now() < fine and not cond():
            self.gira(STEP)
        return cond()

    # ---- letture
    def luce(self, nid):
        last = self.mcu[nid].last_signals
        return unpack_xwalk(last["xwalk"])[0] if last else 0

    def nome(self, nid):
        return self.topo.name(nid) or nid

    def riepilogo(self):
        t = self.clock.now()
        stati = [n.power.state for n in self.nodi.values()]
        st = [n.power.stats(t) for n in self.nodi.values()]
        risp = [s.get("saved_pct") for s in st if s.get("saved_pct") is not None]
        say(f"[sim] {len(stati)} incroci: {stati.count('sleep')} a riposo, {stati.count('active')} svegli, "
            f"{stati.count('cooldown')} in uscita; risparmio medio {sum(risp) / len(risp):.0f}%" if risp else "")

    def close(self):
        for n in self.nodi.values():
            try:
                n.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------- scenari

ATTESA = sim.pedone(lambda t: (0.5, 10.5), 1e9)                                  # pedone fermo nell'attesa
ATTRAVERSA = sim.pedone(lambda t: (min(0.5 + 1.2 * t, 9.5), 10.5), 1e9)          # pedone sulle strisce
AUTO = sim.auto(lambda t: (6.4, 30 - 8.33 * t), 3.6)                               # auto a 30 km/h verso le strisce


def _ora_dormono(c, nid):
    return c.nodi[nid].power.state == "sleep"


def blind_crossing(c, nid=BOARD):
    say(f"\n== Non vedente all'incrocio {nid} ({c.nome(nid)})")
    if nid not in c.nodi:
        return say(f"  {nid} non e' tra gli incroci simulati")
    c.aspetta(lambda: _ora_dormono(c, nid), 40)
    pos = c.topo.pos(nid)
    tag = c.tag("tag-cieco", "blind", vicino(pos, 4, 200))
    say("  arriva al bordo delle strisce con il suo Tag: il sensore sul bordo e il Tag svegliano il nodo")
    c.mcu[nid].set_presence(0, True)
    c.camera(nid, ATTESA)
    c.gira(0.5)
    say(f"  nodo {c.nodi[nid].power.state}, luce {LUCI[c.luce(nid)]} (la camera si sta accendendo)")
    c.gira(2.5)
    say(f"  la camera lo vede in attesa, nessun veicolo: luce {LUCI[c.luce(nid)]}")
    say("  arriva un'auto da sinistra a 30 km/h")
    c.camera(nid, sim.insieme(ATTESA, AUTO))
    massimo = [0]
    c.gira(4.0, ogni=lambda t: massimo.__setitem__(0, max(massimo[0], c.luce(nid)) if c.luce(nid) in (2, 3) else massimo[0]))
    say(f"  durante l'arrivo la luce sopra le strisce e' arrivata a: {LUCI.get(massimo[0], massimo[0])}")
    c.camera(nid, ATTESA)
    c.gira(3.0)
    say(f"  l'auto e' passata: luce {LUCI[c.luce(nid)]}")
    say("  il non vedente attraversa")
    tag.speed = 1.1
    c.camera(nid, ATTRAVERSA)
    c.mcu[nid].set_presence(0, False)
    c.gira(6.0)
    tag.close()
    c.camera(nid, None)
    c.gira(2.0)
    say(f"  avvisi ricevuti dal Tag: {' -> '.join(tag.storia) or 'nessuno'}")


def ambulance_route(c, src=None, dst=None):
    ids = list(c.nodi)
    src = src or (BOARD if BOARD in c.nodi else ids[0])
    if dst is None:  # l'incrocio simulato piu' lontano raggiungibile, fino a ~1,5 km
        lontani = sorted((d for d in ids if 600 < dist_m(c.topo.pos(src), c.topo.pos(d)) < 1500),
                         key=lambda d: -dist_m(c.topo.pos(src), c.topo.pos(d)))
        dst = next((d for d in lontani if len(c.topo.path(src, d)) >= 4), ids[-1])
    percorso = [p for p in c.topo.path(src, dst) if p in c.nodi]
    say(f"\n== Ambulanza da {src} a {dst}: {len(percorso)} incroci, "
        f"{c.topo.path_m(c.topo.path(src, dst)):.0f} m a 12 m/s")
    c.aspetta(lambda: all(_ora_dormono(c, n) for n in percorso[1:4]), 40)
    inizio = len(c.risvegli)
    amb = c.tag("ambulanza-1", "emergency", c.topo.pos(src))
    segmenti = list(zip(percorso, percorso[1:]))
    v = 12.0
    stato = {"i": 0, "s": 0.0}

    def muovi(t):
        if stato["i"] >= len(segmenti):
            return
        a, b = segmenti[stato["i"]]
        pa, pb = c.topo.pos(a), c.topo.pos(b)
        lung = max(1.0, dist_m(pa, pb))
        stato["s"] += v * STEP
        if stato["s"] >= lung:
            stato["i"] += 1
            stato["s"] = 0.0
            say(f"  ambulanza a {b} ({c.nome(b)}): strisce {LUCI[c.luce(b)]}")
            return
        f = stato["s"] / lung
        amb.move([pa[0] + (pb[0] - pa[0]) * f, pa[1] + (pb[1] - pa[1]) * f])
        amb.heading = bearing_deg(pa, pb)
        amb.next_node = b

    durata = sum(max(1.0, dist_m(c.topo.pos(a), c.topo.pos(b))) for a, b in segmenti) / v + 2
    c.gira(durata, ogni=muovi)
    amb.close()
    svegliati = sorted({n for _, n, old, new in c.risvegli[inizio:] if old == "sleep"})
    sul = [n for n in percorso if n in svegliati]
    say(f"  incroci svegliati dal soccorso: {len(svegliati)} (sul percorso {len(sul)}/{len(percorso)}), "
        f"gli altri {len(c.nodi) - len(svegliati)} sono rimasti a riposo")
    c.gira(12)
    say(f"  dopo il passaggio: corridoi ancora aperti "
        f"{sum(1 for n in c.nodi.values() for h in n._active.values() if h.get('kind') == 'emergency_corridor' and h.get('type') == 'hazard')}")


def bike_lane(c, nid=None):
    nid = nid or next((n for n in c.nodi if n != BOARD and len(c.topo.neighbors(n)) >= 3), BOARD)
    say(f"\n== Ciclabile invasa all'incrocio {nid} ({c.nome(nid)})")
    pos = c.topo.pos(nid)
    bici = c.tag("tag-ciclista", "cyclist", vicino(pos, 6, 90))
    c.gira(2)
    say("  un'auto entra due volte nella ciclabile davanti alla camera")
    c.camera(nid, sim.auto(sim.zigzag, 7.2))
    c.gira(8)
    c.camera(nid, None)
    arrivati = sorted(n for n, nodo in c.nodi.items() if n != nid and any(
        h.get("kind") == "bike_lane_invasion" and h.get("type") == "remote_hazard" for h in nodo._active.values()))
    say(f"  pericolo inoltrato via radio a: {', '.join(arrivati) or 'nessuno'}")
    say(f"  avvisi ricevuti dal ciclista: {' -> '.join(bici.storia) or 'nessuno'}")
    c.gira(3)
    bici.close()


def pedestrian_night(c, nid=None):
    nid = nid or next((n for n in reversed(list(c.nodi)) if n != BOARD), BOARD)
    say(f"\n== Notte all'incrocio {nid} ({c.nome(nid)}): nessun Tag, solo il sensore sul bordo")
    n = c.nodi[nid]
    c.aspetta(lambda: _ora_dormono(c, nid), 60)
    say(f"  il nodo dorme, inferenza ferma; consumo stimato {n.power.power_w['sleep']} W")
    c.mcu[nid].set_presence(0, True)
    c.camera(nid, ATTESA)
    c.gira(0.5)
    say(f"  pedone sul bordo: nodo {n.power.state}, luce {LUCI[c.luce(nid)]} (camera in avvio)")
    c.gira(2)
    say(f"  la camera lo vede: luce {LUCI[c.luce(nid)]}")
    c.camera(nid, ATTRAVERSA)
    c.mcu[nid].set_presence(0, False)
    c.gira(6)
    c.camera(nid, None)
    ok = c.aspetta(lambda: _ora_dormono(c, nid), 60)
    st = n.power.stats(c.clock.now())
    say(f"  {'di nuovo a riposo' if ok else 'ancora sveglio'}; risvegli {st.get('wakeups')}, "
        f"risparmio finora {st.get('saved_pct')}%")


SCENARI = {"blind_crossing": blind_crossing, "ambulance_route": ambulance_route, "bike_lane": bike_lane,
           "pedestrian_night": pedestrian_night}


def demo(c):
    for f in SCENARI.values():
        f(c)
        c.riepilogo()


# ---------------------------------------------------------------------------- API dei nodi

def avvia_api(citta, host, port):
    class H(BaseHTTPRequestHandler):
        server_version = "CROSSSimulatore/2"

        def do_GET(self):
            parts = urlsplit(self.path)
            seg = parts.path.strip("/").split("/")
            if len(seg) >= 3 and seg[0] == "n" and seg[1] in citta.api:
                code, obj = citta.api[seg[1]].handle("/" + "/".join(seg[2:]), parts.query)
            elif parts.path.rstrip("/") in ("", "/api/nodes"):
                code, obj = 200, {"nodes": {nid: f"/n/{nid}" for nid in citta.nodi}}
            else:
                code, obj = 404, {"error": "usa /n/<id>/api/..."}
            body = json.dumps(obj, ensure_ascii=False, default=str).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def scrivi_collector(citta, host, port, udp):
    p = QUI.parents[1] / "collector" / "collector_sim.json"
    h = "127.0.0.1" if host in ("0.0.0.0", "") else host
    cfg = {"_note": "Scritto da src/node/tools/simulate_city.py: i nodi sono quelli della simulazione.",
           "title": "CROSS Torino (simulazione)", "topology": "../node/python/topology.json",
           "nodes": {nid: f"http://{h}:{port}/n/{nid}" for nid in citta.nodi},
           "pull": {"mode": "interval", "interval_s": 120, "timeout_s": 4, "page_limit": 1000,
                    "max_pages": 50, "workers": 16},
           "listen": {"udp": bool(udp), "serial": "", "baud": 115200, "group": "239.255.42.99", "port": 47999,
                      "key": ""},
           "db": "collector_sim.db", "stale_s": 120, "tag_timeout_s": 10, "retention_days": 0}
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Simulatore della citta' CROSS v2")
    ap.add_argument("--scenario", default="demo", choices=list(SCENARI) + ["demo", "idle"])
    ap.add_argument("--loop", action="store_true", help="ripete lo scenario all'infinito")
    ap.add_argument("--fast", action="store_true", help="tempo accelerato (niente attese reali)")
    ap.add_argument("--nodes", type=int, default=0, help="solo gli N incroci piu' vicini alla board (0 = tutti)")
    ap.add_argument("--udp", action="store_true", help="copia la radio sul multicast UDP per il collettore")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8200, help="API dei nodi (0 = nessuna API)")
    ap.add_argument("--topology", default=str(PY / "topology.json"))
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "cross_sim"))
    ap.add_argument("--range", type=float, default=180.0, help="portata della radio in metri")
    args = ap.parse_args(argv)

    topo = Topology.load(args.topology)
    ids = topo.ids()
    centro = topo.pos(BOARD) if topo.pos(BOARD) else topo.pos(ids[0])
    if args.nodes and args.nodes < len(ids):
        ids = sorted(ids, key=lambda n: dist_m(centro, topo.pos(n)))[:args.nodes]
    out = Path(args.out)
    if out.exists() and (out / MARKER).exists():
        shutil.rmtree(out, ignore_errors=True)  # solo una cartella creata da questo simulatore
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text("cartella del simulatore: puo' essere cancellata\n", encoding="utf-8")

    orologio = Orologio(args.fast)
    log = (out / "sim.log").open("w", encoding="utf-8")
    say(f"[sim] costruisco {len(ids)} incroci da {Path(args.topology).name} (log dei nodi in {out / 'sim.log'})")
    t0 = time.time()
    with contextlib.redirect_stdout(log):
        citta = Citta(topo, out, orologio, ids, udp=args.udp, range_m=args.range)
    say(f"[sim] pronti in {time.time() - t0:.1f} s")
    srv = None
    if args.port:
        srv = avvia_api(citta, args.host, args.port)
        p = scrivi_collector(citta, args.host, args.port, args.udp)
        say(f"[sim] API dei nodi su http://{args.host}:{args.port}/n/<id>/api/status ; collettore: "
            f"python src/collector/collector.py --config {p.relative_to(QUI.parents[2])}")
    try:
        with contextlib.redirect_stdout(log):
            while True:
                if args.scenario == "idle":
                    citta.gira(30)
                    citta.riepilogo()
                elif args.scenario == "demo":
                    demo(citta)
                else:
                    SCENARI[args.scenario](citta)
                    citta.riepilogo()
                if not args.loop and args.scenario != "idle":
                    break
    except KeyboardInterrupt:
        say("\n[sim] interrotto")
    finally:
        if srv is not None:
            srv.shutdown()
        citta.close()
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
