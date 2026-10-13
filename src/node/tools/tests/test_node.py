"""Test del nodo v2 intero (node.py): energia, sensore sul bordo, camera, attraversamento, luci, Tag,
corridoio di emergenza tra due incroci e richiesta del collettore via radio.

Due nodi veri della topologia (incrocio-124 e incrocio-134, 97 m a sud-ovest) su un bus radio virtuale
(peer.SimRadioBus), MCU finti (mcu.FakeMCU), orologio finto, detection finte di simulate.py.

Uso:  python test_node.py   (dalla cartella node/tools/tests; esce con codice 1 se qualcosa fallisce)
"""
import copy
import json
import sys
import tempfile
import traceback
from pathlib import Path

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI.parents[1] / "python"))
sys.path.insert(0, str(QUI.parents[0]))

import simulate as sim  # noqa: E402
from config import prepare  # noqa: E402
from mcu import FakeMCU, unpack_xwalk  # noqa: E402
from node import Node  # noqa: E402
from peer import SimRadioBus  # noqa: E402
from route import Topology  # noqa: E402

RISULTATI = []
TOPO = Topology.load(QUI.parents[1] / "python" / "topology.json")
A, B = "incrocio-124", "incrocio-134"
OROLOGIO = [1000.0]


def check(nome, ok, info=""):
    RISULTATI.append((nome, bool(ok), info))
    print(f"  [{'OK ' if ok else 'ERR'}] {nome}" + (f"  ({info})" if info and not ok else ""))


def cfg_nodo(node_id, tmp):
    cam = {k: copy.deepcopy(sim.CFG[k]) for k in ("frame_size", "calibration", "zones", "lines", "lanes", "approaches")}
    cam["zones"]["waiting_area"] = [[0.0, 0.58], [0.10, 0.58], [0.10, 0.72], [0.0, 0.72]]
    cam["approach_bearing"] = {"via": 207, "incrocio": 26}
    raw = {"node_id": node_id, "cameras": {"cam0": cam}, "traffic_light": {"mode": "none"},
           "crosswalks": [{"id": 0, "camera": "cam0", "zone": "crosswalk", "waiting": "waiting_area",
                           "curb": 0, "light": 0, "left": "via", "right": "incrocio"}],
           "power": {"boot_active_s": 2, "active_hold_s": 3, "cooldown_s": 1},
           "p2p": {"enabled": True, "transports": ["sim"]},
           "api": {"enabled": False}, "data": {"db_file": f"{node_id}.db", "status_interval_s": 2}}
    cfg, avvisi = prepare(raw)
    return cfg


class Citta:
    def __init__(self, tmp):
        self.bus = SimRadioBus(range_m=180.0)
        self.mcu = {n: FakeMCU({"curbs": 1}, clock=lambda: OROLOGIO[0]) for n in (A, B)}
        self.nodi = {n: Node(cfg_nodo(n, tmp), base_dir=tmp, start_threads=False, mcu=self.mcu[n],
                             sim_bus=self.bus, topology=TOPO, clock=lambda: OROLOGIO[0]) for n in (A, B)}
        self.ricevuti = {}

    def orecchio(self, ep, pos):
        """Un ascoltatore sul bus (Tag o collettore) che registra i messaggi."""
        box = self.ricevuti.setdefault(ep, [])
        self.bus.attach(ep, pos, lambda data, rssi: box.append(json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)))
        return box

    def manda(self, ep, obj):
        self.bus.broadcast(ep, json.dumps(obj, separators=(",", ":")).encode())
        self.pompa()

    def pompa(self):
        for _ in range(20):
            if sum(n.peer.poll_once(0) for n in self.nodi.values()) == 0:
                break

    def gira(self, secondi, gen=None, nodo=A, passo=0.25):
        t0 = OROLOGIO[0]
        k = 0
        while OROLOGIO[0] < t0 + secondi - 1e-9:
            OROLOGIO[0] = round(OROLOGIO[0] + passo, 3)
            if gen is not None:
                self.nodi[nodo].process(sim_det(gen, OROLOGIO[0] - t0), "cam0", OROLOGIO[0])
            self.pompa()
            for n in self.nodi.values():
                n.tick(OROLOGIO[0])
            self.pompa()
            k += 1

    def luce(self, n):
        last = self.mcu[n].last_signals
        return unpack_xwalk(last["xwalk"])[0] if last else None

    def close(self):
        for n in self.nodi.values():
            n.close()


def sim_det(gen, tau):
    det = {}
    for label, x1, y1, x2, y2 in gen(tau):
        det.setdefault(label, []).append({"confidence": 0.9, "bounding_box_xyxy": (x1, y1, x2, y2)})
    return det


ATTESA = sim.pedone(lambda t: (0.5, 10.5), 30.0)                      # pedone fermo nell'area d'attesa
AUTO = sim.auto(lambda t: (6.4, 30 - 8.33 * t), 3.6)                   # auto a 30 km/h verso le strisce


def test_energia_e_bordo(c):
    print("== energia e sensore sul bordo")
    a = c.nodi[A]
    check("all'avvio il nodo e' sveglio (boot)", a.power.state == "active")
    c.gira(7)
    check("senza nessuno si addormenta", a.power.state == "sleep", a.power.state)
    check("a riposo i fotogrammi vengono scartati", a.process(sim_det(ATTESA, 0), "cam0", OROLOGIO[0]) == []
          and a.dropped_frames >= 1)
    c.mcu[A].set_presence(0, True)
    c.gira(0.5)
    check("persona sul bordo: il nodo si sveglia", a.power.state == "active")
    st = a.latest_crosswalks[0]
    check("camera appena sveglia, nessun fotogramma: unknown (mai via libera)",
          st["state"] == "unknown" and not st["safe"], st["state"])
    check("luce unknown sopra le strisce", c.luce(A) == 6, c.luce(A))
    c.gira(2, gen=ATTESA)
    st = a.latest_crosswalks[0]
    check("pedone in attesa, nessun veicolo: occupied e safe", st["state"] == "occupied" and st["safe"], st)
    check("luce accesa (1)", c.luce(A) == 1, c.luce(A))


def test_veicolo_in_arrivo(c):
    print("== veicolo in arrivo")
    a = c.nodi[A]
    stati = []
    t0 = OROLOGIO[0]
    while OROLOGIO[0] < t0 + 3.0:
        OROLOGIO[0] = round(OROLOGIO[0] + 0.25, 3)
        a.process(sim_det(sim.insieme(ATTESA, AUTO), OROLOGIO[0] - t0), "cam0", OROLOGIO[0])
        a.tick(OROLOGIO[0])
        stati.append((a.latest_crosswalks[0]["state"], c.luce(A), a.latest_crosswalks[0]["safe"]))
    check("con l'auto in arrivo lo stato passa ad attention o conflict",
          any(s in ("attention", "conflict") for s, _, _ in stati), stati)
    check("luce 2 o 3 durante l'arrivo", any(l in (2, 3) for _, l, _ in stati), stati)
    check("mai safe mentre l'auto e' vicina",
          all(not safe for s, _, safe in stati if s in ("attention", "conflict")), stati)
    c.mcu[A].set_presence(0, False)
    c.gira(3, gen=ATTESA)


def test_tag_non_vedente(c):
    print("== CROSS Tag non vedente")
    a = c.nodi[A]
    box = c.orecchio("tag-b1", TOPO.pos(A))
    c.manda("tag-b1", {"v": 2, "t": "vru", "d": "tag-b1", "p": "blind", "q": 1})
    c.gira(1.0, gen=ATTESA)
    vicini = a.users.near(OROLOGIO[0])
    check("il Tag e' vicino al nodo A", any(v["device"] == "tag-b1" for v in vicini), vicini)
    check("il Tag non e' vicino al nodo B (97 m, RSSI basso)", not c.nodi[B].users.near(OROLOGIO[0]))
    adv = [m for m in box if m.get("t") == "adv" and ("blind" in (m.get("r") or []) or m.get("to") == "tag-b1")]
    check("il Tag riceve avvisi adv dal nodo A", adv and all(m.get("n") == A for m in adv), box[-3:])
    check("assist sulle strisce con camera pronta", a.latest_crosswalks[0]["state"] in ("assist", "attention", "conflict"),
          a.latest_crosswalks[0]["state"])
    check("avviso CROSS_OK (2) o di attesa, mai senza camera", adv and adv[-1]["c"] in (1, 2, 4, 5, 13), adv[-1:] if adv else None)


def test_corridoio_emergenza(c):
    print("== corridoio di emergenza")
    b = c.nodi[B]
    c.gira(8)
    check("il nodo B dorme prima del soccorso", b.power.state == "sleep", b.power.state)
    c.bus.attach("amb", TOPO.pos(A), lambda d, r: None)
    c.manda("amb", {"v": 2, "t": "emv", "d": "amb", "q": 1, "hd": 207, "st": "run"})
    c.gira(0.5)
    a = c.nodi[A]
    corr = [h for h in a._active.values() if h.get("kind") == "emergency_corridor"]
    check("A apre il corridoio di emergenza", corr, list(a._active))
    check("A mette le strisce in emergenza (luce 5)", c.luce(A) == 5, c.luce(A))
    rem = [h for h in b._active.values() if h.get("kind") == "emergency_corridor"]
    check("B riceve il corridoio (B e' verso 207 gradi)", rem, list(b._active))
    check("B si sveglia per l'emergenza", b.power.state == "active", b.power.state)
    check("anche le strisce di B in emergenza", c.luce(B) == 5, c.luce(B))
    c.gira(12)
    check("soccorso non piu' sentito: corridoio chiuso su A",
          not any(h.get("kind") == "emergency_corridor" for h in a._active.values()))


def test_richiesta_collettore(c):
    print("== richiesta del collettore via radio")
    box = c.orecchio("collettore", TOPO.pos(A))
    c.manda("collettore", {"v": 2, "t": "rq", "id": "rq-1", "w": "sum", "h": 0})
    rs = [m for m in box if m.get("t") == "rs" and m.get("id") == "rq-1"]
    check("risposta rs dal nodo A", any(m.get("n") == A for m in rs), box[-3:])
    check("riepilogo con stato di energia", rs and "pw" in (rs[0].get("p") or {}), rs[:1])


def test_guasto_sensore(c):
    print("== guasto del sensore sul bordo")
    a = c.nodi[A]
    c.mcu[A].set_fault(0, True)
    c.gira(0.5)
    check("guasto: evento wake_sensor_fault", any(h.get("kind") == "wake_sensor_fault" for h in a._active.values()))
    check("guasto: il nodo resta sveglio", a.power.awake and a.power.fault)
    c.mcu[A].set_fault(0, False)
    c.gira(0.5)
    check("sensore di nuovo ok: guasto chiuso", not a.power.fault
          and not any(h.get("kind") == "wake_sensor_fault" for h in a._active.values()))
    snap = a.status_snapshot()
    check("status_snapshot con energia, attraversamenti, Tag, peer",
          all(k in snap for k in ("power", "crosswalks", "users", "peers", "position", "name")), list(snap))
    check("nome dell'incrocio dalla topologia", snap["name"] and "Vela" in snap["name"], snap["name"])


def main():
    with tempfile.TemporaryDirectory() as tmp:
        c = Citta(tmp)
        try:
            for f in (test_energia_e_bordo, test_veicolo_in_arrivo, test_tag_non_vedente, test_corridoio_emergenza,
                      test_richiesta_collettore, test_guasto_sensore):
                try:
                    f(c)
                except Exception as e:
                    traceback.print_exc()
                    check(f"{f.__name__} senza eccezioni", False, repr(e))
        finally:
            c.close()
    ok = sum(1 for r in RISULTATI if r[1])
    print(f"\n{ok}/{len(RISULTATI)} controlli OK")
    for nome, esito, info in RISULTATI:
        if not esito:
            print("  FALLITO:", nome, str(info)[:400])
    sys.exit(0 if ok == len(RISULTATI) else 1)


if __name__ == "__main__":
    main()
