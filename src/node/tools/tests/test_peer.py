"""Test del protocollo radio SC-P2P v2 (peer.py, ARCHITETTURA.md §3).

Prova: codifica e taglio a 240 byte, firma HMAC, dedup, rilancio con h, consegna solo ai
destinatari, ponte serial <-> udp (la radio ESP32 e' finta: un server TCP raggiunto con
"socket://", lo stesso codice di riga della seriale vera), ponte sim <-> udp, bus radio virtuale a
piu' salti (catena di 4 incroci a 150 m con portata 180 m), instradamento di route.py sul
topology.json vero con tutti gli incroci sul bus virtuale, beacon dei CROSS Tag con "near",
richiesta e risposta del collettore (rq/rs), heartbeat con lo stato di energia, robustezza, riga
di comando.

Uso:  python test_peer.py      (dalla cartella node/tools/tests; esce con codice 1 se qualcosa fallisce)
"""
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

QUI = Path(__file__).resolve().parent
PY = QUI.parents[1] / "python"
sys.path.insert(0, str(PY))

import peer  # noqa: E402
from peer import (MAX_BYTES, PeerLink, SimRadioBus, decode, encode, expand_id, hazard_to_p2p,  # noqa: E402
                  normalize, rssi_from_distance, sign, split_rssi, verify)
from route import Topology  # noqa: E402

RISULTATI = []
LAT0, LON0 = 45.0650, 7.6650


def check(nome, ok, info=""):
    RISULTATI.append((nome, bool(ok), info))
    print(f"[{'OK ' if ok else 'ERR'}] {nome}" + ("" if ok else f"  -> {info}"))


def test(fn):
    """Decoratore: un test che solleva un'eccezione conta come fallito, gli altri continuano."""
    def wrapper():
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, f"eccezione {e!r}\n{traceback.format_exc(limit=4)}")
    wrapper.__name__ = fn.__name__
    return wrapper


# ---------------------------------------------------------------------- attrezzi

def nord(m):
    """Punto m metri a nord del punto di partenza."""
    return (LAT0 + m / 111320, LON0)


def porta_libera(tipo=socket.SOCK_DGRAM):
    with socket.socket(socket.AF_INET, tipo) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ora_ms():
    return int(time.time() * 1000)


def hz(mid="incrocio-9-cam0-000001", n="incrocio-9", a="n", to=None, h=0, **kw):
    t = ora_ms()
    o = {"v": 2, "t": "hz", "id": mid, "n": n, "a": a, "k": "wrong_way", "c": "violation", "s": 3,
         "ts": t, "e": t + 30_000, "h": h}
    if to is not None:
        o["to"] = to
    o.update(kw)
    return o


def hazard(mid, kind="emergency_vehicle", category="emergency", **kw):
    t = ora_ms()
    m = {"type": "hazard", "id": mid, "kind": kind, "category": category, "severity": 3, "action": "new",
         "propagate": True, "ts": t, "expires": t + 60_000, "targets": ["person", "cyclist", "vehicle"]}
    m.update(kw)
    return m


class Nodo:
    """Un incrocio sul bus virtuale, senza thread: i messaggi normalizzati finiscono in msgs."""

    def __init__(self, bus, nid, pos, neighbors=None, topology=None, on_message=None, **cfg):
        self.msgs = []
        c = {"enabled": True, "transports": ["sim"], "heartbeat": False, **cfg}
        self.link = PeerLink(c, nid, on_message or self.msgs.append, start_thread=False, sim_bus=bus,
                             position=pos, neighbors=neighbors, topology=topology)

    def di(self, typ):
        return [m for m in self.msgs if m.get("type") == typ]


class Radio:
    """Endpoint di prova sul bus: trasmette frame grezzi e registra quelli che riceve."""

    def __init__(self, bus, name, pos):
        self.bus, self.name, self.rx = bus, name, []
        bus.attach(name, pos, lambda d, r: self.rx.append((d, r)))

    def send(self, obj, key=""):
        data = obj if isinstance(obj, (bytes, bytearray)) else encode(obj, key)
        self.bus.broadcast(self.name, data)

    def objs(self, t=None):
        out = [decode(d) for d, _ in self.rx]
        return [o for o in out if o and (t is None or o.get("t") == t)]


def pompa(bus, nodi, giri=30):
    """Fa girare bus e nodi finche' non resta niente da consegnare."""
    tot = 0
    for _ in range(giri):
        bus.flush()
        n = sum(x.link.poll_once(0) for x in nodi)
        tot += n
        if n == 0 and bus.pending() == 0:
            break
    return tot


def chiudi(*nodi):
    for x in nodi:
        x.link.close()


# ---------------------------------------------------------------------- 1. codifica

@test
def test_codifica():
    # pericolo tipico: mezzo di soccorso con 3 incroci davanti, direzione e firma
    m = hazard("incrocio-142-cam0-000123", heading=225)
    o = hazard_to_p2p(m, "incrocio-142", ["incrocio-117", "incrocio-118", "incrocio-119"])
    data = encode(o, "chiave-segreta")
    d = decode(data)
    check("hz tipico (3 destinatari, direzione, firma) entro 240 byte", data is not None and len(data) <= MAX_BYTES,
          len(data or b""))
    check("hz tipico: nessun destinatario perso", d["to"] == ["incrocio-117", "incrocio-118", "incrocio-119"], d.get("to"))
    check("hz tipico: direzione conservata", d.get("hd") == 225, d)
    check("hz tipico: id accorciato senza perdite", d["id"] == "~cam0-000123" and expand_id(d) == "incrocio-142-cam0-000123",
          d["id"])
    check("hz tipico: firma valida", verify(d, "chiave-segreta"))
    piccolo = encode(hazard_to_p2p(hazard("incrocio-1-c-1"), "incrocio-1", ["incrocio-2"]))
    check("hz piccolo: id lasciato com'e'", decode(piccolo)["id"] == "incrocio-1-c-1")
    check("hops di default = destinatari + 1", o["h"] == 4 and hazard_to_p2p(m, "x")["h"] == 1)
    check("hops limitati a 6", hazard_to_p2p(m, "x", [f"n{i}" for i in range(10)])["h"] == 6)
    check("hops espliciti", hazard_to_p2p(m, "x", ["a"], hops=0)["h"] == 0 and hazard_to_p2p(m, "x", ["a"], 9)["h"] == 6)
    check("se stesso e doppioni tolti da to", hazard_to_p2p(m, "x", ["a", "x", "a", "b"])["to"] == ["a", "b"])
    check("tg con tutti i profili non scritto", "tg" not in o)
    check("tg con un profilo scritto", hazard_to_p2p(hazard("i", targets=["cyclist"]), "x")["tg"] == ["cyclist"])
    check("fine pericolo: a = c", hazard_to_p2p(hazard("i", action="cancel"), "x")["a"] == "c")
    check("direzione dal nome del punto cardinale",
          hazard_to_p2p(hazard("i", actor={"towards": "nord-est"}), "x")["hd"] == 45)
    check("nessuna direzione se l'accesso non e' un punto cardinale",
          "hd" not in hazard_to_p2p(hazard("i", actor={"towards": "via"}), "x"))
    # troppo lungo: prima tg e hd, poi la lista to dalla fine
    lungo = hazard_to_p2p(hazard("osm-1234567890-cam0-000001", targets=["cyclist", "person"], heading=10),
                          "osm-9999999999", [f"osm-12345678{i:02d}" for i in range(8)])
    data = encode(lungo, "k")
    d = decode(data)
    check("hz lungo: entro 240 byte", data is not None and len(data) <= MAX_BYTES, len(data or b""))
    check("hz lungo: tolti tg e hd", "tg" not in d and "hd" not in d, d)
    check("hz lungo: to accorciato dalla fine (primi tenuti, in ordine)",
          1 <= len(d["to"]) < 8 and d["to"] == lungo["to"][:len(d["to"])], d["to"])
    check("hz lungo: firma valida dopo il taglio", verify(d, "k"))
    tenuto = decode(encode(lungo, "k", keep=("tg",)))
    check("keep: tg non si toglie", tenuto is not None and tenuto.get("tg") == ["cyclist", "person"], tenuto)
    kk = decode(encode({"v": 2, "t": "hz", "id": "x", "n": "y", "a": "n", "k": "k" * 300, "h": 0}))
    check("ultima risorsa: k accorciato", kk is not None and 0 < len(kk["k"]) < 300)
    check("impossibile: None", encode({"v": 2, "t": "hb", "n": "x" * 300}) is None)
    check("i Tag non firmano", "sig" not in decode(encode({"v": 2, "t": "vru", "d": "t", "p": "blind"}, "k")))
    rs = {"v": 2, "t": "rs", "id": "rq-1", "n": "incrocio-1", "h": 0,
          "p": {"pw": "a", "hz": [f"incrocio-1-cam0-{i:06d}" for i in range(20)], "u": 3, "ev": 120, "wk": 4}}
    d = decode(encode(rs, "k"))
    check("rs lungo: lista dei pericoli accorciata, stato di energia tenuto",
          d is not None and len(encode(rs, "k")) <= MAX_BYTES and d["p"]["pw"] == "a" and len(d["p"]["hz"]) < 20, d)
    # decodifica
    cattivi = [b"\xff\xfe", b"[]", b"{", b"null", b'{"v":3,"t":"hz"}', b'{"v":2,"t":"boh"}', b"[" * 100000,
               "non json", b'{"v":1,"t":"hz","id":"x"}']
    check("decode rifiuta il non valido", all(decode(x) is None for x in cattivi),
          [x[:20] for x in cattivi if decode(x) is not None])
    v1 = decode(b'{"v":1,"t":"vru","d":"polso-1","p":"cyclist","ts":1,"h":0}')
    check("decode accetta i beacon v1 dei braccialetti", v1 and v1["v"] == 2 and v1.get("v1") is True)
    check("split_rssi con prefisso", split_rssi(b'R-63 {"a":1}') == (-63, b'{"a":1}'))
    check("split_rssi senza prefisso", split_rssi(b' {"a":1}\r') == (None, b'{"a":1}'))
    check("split_rssi prefisso rotto", split_rssi(b"Rxx {}")[0] is None and split_rssi(b"R-300 {}") == (None, b"{}"))


# ---------------------------------------------------------------------- 2. firma

@test
def test_firma():
    o = hz(h=3)
    s = sign(o, "k")
    check("firma: 16 caratteri hex", len(s) == 16 and int(s, 16) >= 0)
    check("firma: h non conta", sign({**o, "h": 0}, "k") == s)
    check("firma: l'ordine delle chiavi non conta", sign(dict(reversed(list(o.items()))), "k") == s)
    d = decode(encode(o, "k"))
    check("verify: valida", verify(d, "k"))
    check("verify: campo cambiato", not verify({**d, "s": 1}, "k"))
    check("verify: chiave sbagliata", not verify(d, "altra"))
    check("verify: senza firma con chiave", not verify(o, "k"))
    check("verify: senza chiave accetta tutto", verify(o, ""))

    bus = SimRadioBus(180)
    b = Nodo(bus, "incrocio-5", nord(0), key="k")
    r = Radio(bus, "radio", nord(20))
    try:
        r.send(hz("incrocio-9-a", to=["incrocio-5"]), key="k")
        r.send(hz("incrocio-9-b", to=["incrocio-5"]))                 # non firmato
        r.send(hz("incrocio-9-c", to=["incrocio-5"]), key="sbagliata")
        r.send({"v": 2, "t": "vru", "d": "tag-1", "p": "blind", "q": 1})              # Tag: niente firma
        bad = {"v": 2, "t": "vru", "d": "tag-2", "p": "blind", "q": 1}
        r.send(_dumps({**bad, "sig": "0123456789abcdef"}))                             # Tag con firma falsa
        pompa(bus, [b])
        ids = [m["id"] for m in b.di("remote_hazard")]
        check("con la chiave: solo il firmato arriva", ids == ["incrocio-9-a"], ids)
        check("con la chiave: firme mancanti/errate contate", b.link.counters["bad_sig"] == 3, b.link.counters)
        check("Tag senza firma accettato", [m["device"] for m in b.di("vru_beacon")] == ["tag-1"], b.di("vru_beacon"))
        r.rx.clear()
        r.send(hz("incrocio-9-d", to=["altro"], h=1), key="k")
        pompa(bus, [b])
        rel = r.objs("hz")
        check("rilancio firmato: stessa firma, ancora valida", rel and rel[0]["h"] == 0 and verify(rel[0], "k"), rel)
    finally:
        chiudi(b)


def _dumps(o):
    return json.dumps(o, separators=(",", ":")).encode()


# ---------------------------------------------------------------------- 3. dedup e rilancio

@test
def test_dedup_e_rilancio():
    bus = SimRadioBus(180)
    b = Nodo(bus, "incrocio-5", nord(0))
    r = Radio(bus, "radio", nord(20))
    try:
        r.send(hz("x-1"))
        r.send(hz("x-1"))
        pompa(bus, [b])
        check("dedup: lo stesso pericolo consegnato una volta", len(b.di("remote_hazard")) == 1 and b.link.counters["dup"] == 1,
              b.link.counters)
        r.send(hz("x-1", a="c"))
        pompa(bus, [b])
        rh = b.di("remote_hazard")
        check("dedup: la fine dello stesso pericolo arriva", len(rh) == 2 and rh[1]["action"] == "cancel")
        r.rx.clear()
        r.send(hz("x-2", to=["lontano"], h=0))       # non per me e h = 0: ignorato e NON ricordato...
        r.send(hz("x-2", to=["lontano"], h=1))       # ...cosi' la copia con h > 0 si rilancia
        pompa(bus, [b])
        rel = r.objs("hz")
        check("copia non pertinente con h=0 non nasconde quella da rilanciare",
              len(rel) == 1 and rel[0]["id"] == "x-2" and rel[0]["h"] == 0, rel)
        check("non destinatario: non consegnato", all(m["id"] != "x-2" for m in b.msgs))
        r.rx.clear()
        r.send(hz("x-3", to=["lontano"], h=2))
        pompa(bus, [b])
        rel = r.objs("hz")
        check("rilancio con h - 1", len(rel) == 1 and rel[0]["h"] == 1 and rel[0]["id"] == "x-3", rel)
        r.rx.clear()
        r.send(hz("x-4", h=0))
        r.send(hz("x-5", to=["lontano"], h=100))     # h assurdo: limitato
        pompa(bus, [b])
        rel = r.objs("hz")
        check("h = 0: niente rilancio; h enorme limitato a 6",
              [(o["id"], o["h"]) for o in rel] == [("x-5", 5)], [(o["id"], o["h"]) for o in rel])
        r.rx.clear()
        b.link.relay = False
        r.send(hz("x-6", h=3))
        pompa(bus, [b])
        check("relay=False: solo ascolto", not r.objs("hz") and any(m["id"] == "x-6" for m in b.msgs))
        b.link.relay = True
        r.rx.clear()
        r.send(hz("x-7", n="incrocio-5", h=2))       # mio (eco di un rilancio altrui)
        r.send({"v": 2, "t": "vru", "d": "tag-1", "p": "pedestrian", "q": 1, "h": 3})
        r.send({"v": 2, "t": "vru", "d": "tag-1", "p": "pedestrian", "q": 1, "h": 3})
        r.send({"v": 2, "t": "adv", "n": "incrocio-6", "id": "a-1", "r": ["blind"], "c": 1, "s": 2, "x": 0, "ttl": 3000})
        pompa(bus, [b])
        check("messaggio mio: ignorato", b.link.counters["own"] == 1 and all(m.get("id") != "x-7" for m in b.msgs))
        check("beacon: niente dedup (due consegne)", len(b.di("vru_beacon")) == 2)
        check("beacon e adv: mai rilanciati", not r.rx, r.objs())
        check("adv di un altro nodo: ignorato", not any(m.get("type") not in ("remote_hazard", "vru_beacon") for m in b.msgs))
    finally:
        chiudi(b)


# ---------------------------------------------------------------------- 4. consegna

@test
def test_consegna():
    bus = SimRadioBus(180)
    b = Nodo(bus, "B", nord(0), neighbors=["A"])
    c = Nodo(bus, "C", nord(10), neighbors={"via": "X"})       # compatibilita' {via: id}
    d = Nodo(bus, "D", nord(20))                                # nessun vicino noto
    r = Radio(bus, "radio", nord(5))
    try:
        r.send(hz("h-1", n="A"))
        pompa(bus, [b, c, d])
        chi = [x.link.node_id for x in (b, c, d) if x.di("remote_hazard")]
        check("senza to: lo consegnano i vicini del mittente e chi non ha vicini", chi == ["B", "D"], chi)
        r.send(hz("h-2", n="A", to=["C"]))
        r.send(hz("h-3", n="A", to="D"))                        # to come stringa
        pompa(bus, [b, c, d])
        check("to: solo i destinatari", [m["id"] for m in c.di("remote_hazard")] == ["h-2"]
              and [m["id"] for m in d.di("remote_hazard")] == ["h-1", "h-3"]
              and [m["id"] for m in b.di("remote_hazard")] == ["h-1"])
        m = c.di("remote_hazard")[0]
        attesi = {"type": "remote_hazard", "via": "p2p", "transport": "sim", "id": "h-2", "kind": "wrong_way",
                  "category": "violation", "severity": 3, "action": "new", "from_node": "A", "to": ["C"],
                  "next_nodes": ["C"], "rssi": rssi_from_distance(5), "hops_left": 0}
        diff = {k: (m.get(k), v) for k, v in attesi.items() if m.get(k) != v}
        check("remote_hazard normalizzato", not diff, diff)
        check("remote_hazard: scadenza e tempi", m["expires"] - m["ts"] == 30_000)
        check("remote_hazard: causeCode dal catalogo", m.get("cause") in (14, None))
        t = ora_ms() - 3_600_000                                # orologio del mittente indietro di un'ora
        r.send(hz("h-4", n="A", to=["C"], ts=t, e=t + 20_000, hd=270))
        pompa(bus, [b, c, d])
        m = c.di("remote_hazard")[-1]
        check("orologio sfasato: tempi riportati sul mio, durata tenuta",
              abs(m["ts"] - ora_ms()) < 2000 and m["expires"] - m["ts"] == 20_000, (m["ts"], m["expires"]))
        check("remote_hazard: direzione", m["heading"] == 270)
        topo = Topology.from_dict({"nodes": {"T": {"lat": nord(40)[0], "lon": LON0,
                                                   "neighbors": {"A": {"m": 90}}},
                                             "A": {"lat": nord(130)[0], "lon": LON0}}})
        t_ = Nodo(bus, "T", None, topology=topo)
        check("topologia: vicini e posizione dal file", t_.link.neighbors == {"A"} and t_.link.position == nord(40),
              (t_.link.neighbors, t_.link.position))
        r.send(hz("h-5", n="A"))
        r.send(hz("h-6", n="Z"))
        pompa(bus, [t_])
        check("topologia: consegna dal vicino, non dagli altri", [x["id"] for x in t_.di("remote_hazard")] == ["h-5"])
        chiudi(t_)
    finally:
        chiudi(b, c, d)


# ---------------------------------------------------------------------- 5. ponte serial <-> udp

class RadioFinta:
    """ESP32 radio finta: server TCP, il nodo ci si collega con serial.port = socket://..."""

    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(2)
        self.srv.settimeout(2)
        self.port = self.srv.getsockname()[1]
        self.conn = None
        self.buf = b""

    def accept(self):
        self.conn, _ = self.srv.accept()
        self.conn.settimeout(0.05)
        self.buf = b""

    def riga(self, data):
        self.conn.sendall(data)

    def righe(self, link, attesa=1.0):
        """Righe scritte dal nodo sulla radio (facendo girare il nodo nel frattempo)."""
        fine, out = time.time() + attesa, []
        while time.time() < fine:
            link.poll_once(0.02)
            try:
                chunk = self.conn.recv(4096)
                if chunk:
                    self.buf += chunk
            except socket.timeout:
                pass
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                out.append(decode(line))
            if out:
                break
        return out

    def close(self):
        for s in (self.conn, self.srv):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass


def udp_ricevi(sock, link, attesa=1.0):
    fine, out = time.time() + attesa, []
    while time.time() < fine and not out:
        link.poll_once(0.02)
        try:
            data, _ = sock.recvfrom(4096)
            out.append(decode(data))
        except (socket.timeout, BlockingIOError):
            pass
    return out


def gira(link, cond, attesa=1.0):
    fine = time.time() + attesa
    while time.time() < fine and not cond():
        link.poll_once(0.02)
    return cond()


@test
def test_ponte_seriale_udp():
    radio = RadioFinta()
    pu = porta_libera()
    tu = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)       # un altro incrocio in UDP (peer unicast)
    tu.bind(("127.0.0.1", 0))
    tu.settimeout(0.05)
    msgs = []
    cfg = {"enabled": True, "transports": ["serial", "udp"], "serial": {"port": f"socket://127.0.0.1:{radio.port}"},
           "port": pu, "bind_port": pu, "peers": [f"127.0.0.1:{tu.getsockname()[1]}"], "heartbeat": False}
    link = PeerLink(cfg, "incrocio-5", msgs.append, start_thread=False)
    try:
        radio.accept()
        st = link.state()
        check("ponte: seriale (socket://) e UDP attivi", st["serial"] and st["udp"], st["transports"])
        radio.riga(b"R-60 " + encode(hz("p-1", to=["incrocio-9"], h=1)) + b"\n")
        u = udp_ricevi(tu, link)
        check("ponte seriale -> udp: rilanciato con h - 1", u and u[0]["id"] == "p-1" and u[0]["h"] == 0, u)
        s = radio.righe(link)
        check("rilancio anche sulla radio (piu' salti ESP-NOW)", s and s[0]["id"] == "p-1" and s[0]["h"] == 0, s)
        tu.sendto(encode(hz("p-2", to=["incrocio-9"], h=1)), ("127.0.0.1", pu))
        s = radio.righe(link)
        check("ponte udp -> seriale: rilanciato con h - 1", s and s[0]["id"] == "p-2" and s[0]["h"] == 0, s)
        u = udp_ricevi(tu, link)
        check("rilancio di cio' che arriva in UDP: ai peer unicast", u and u[0]["id"] == "p-2", u)
        check("contatori: rilanciati e passati di ponte", link.counters["relayed"] == 2 and link.counters["bridged"] == 2,
              link.counters)
        check("non destinatario: niente consegna", not any(m.get("type") == "remote_hazard" for m in msgs))
        radio.riga(b"R-60 " + encode(hz("p-3", to=["incrocio-5"])) + b"\n")
        gira(link, lambda: any(m.get("id") == "p-3" for m in msgs))
        m = next((m for m in msgs if m.get("id") == "p-3"), {})
        check("consegna dalla radio con RSSI", m.get("transport") == "serial" and m.get("rssi") == -60, m)
        vru = {"v": 2, "t": "vru", "d": "tag-7", "p": "blind", "q": 1}
        radio.riga(b"R-60 " + encode(vru) + b"\n" + b"R-90 " + encode({**vru, "q": 2}) + b"\n")
        gira(link, lambda: len([m for m in msgs if m.get("type") == "vru_beacon"]) >= 2)
        tu.sendto(encode({**vru, "q": 3}), ("127.0.0.1", pu))
        tu.sendto(b"R-88 " + encode({**vru, "q": 4}), ("127.0.0.1", pu))
        gira(link, lambda: len([m for m in msgs if m.get("type") == "vru_beacon"]) >= 4)
        vb = [(m["seq"], m["rssi"], m["near"], m["via"]) for m in msgs if m.get("type") == "vru_beacon"]
        check("beacon: RSSI dal prefisso, near secondo la soglia, UDP senza RSSI = near",
              vb == [(1, -60, True, "serial"), (2, -90, False, "serial"), (3, None, True, "udp"), (4, -88, False, "udp")], vb)
        prima = len(msgs)
        radio.riga(b"# radio CROSS pronta, canale 1\nets Jun  8 2016 00:22:57\n\n")
        mezzo = encode({**vru, "q": 5})
        radio.riga(b"R-50 " + mezzo[:20])
        link.poll_once(0.1)
        radio.riga(mezzo[20:] + b"\n")
        gira(link, lambda: len(msgs) > prima)
        check("righe di stato e di boot ignorate, riga spezzata ricomposta",
              [m.get("seq") for m in msgs[prima:]] == [5], msgs[prima:])
        check("stato della radio letto", (link.state()["transports"]["serial"].get("status") or "").startswith("# radio"))
        link.send_obj({"t": "adv", "id": "incrocio-5-a1", "r": ["blind"], "c": 1, "s": 2, "x": 0, "ttl": 3000})
        s = radio.righe(link)
        u = udp_ricevi(tu, link)
        check("adv del nodo: su radio e UDP", s and s[0]["t"] == "adv" and s[0]["n"] == "incrocio-5" and u
              and u[0]["t"] == "adv", (s, u))
        radio.conn.close()
        gira(link, lambda: not link.state()["serial"])
        check("radio scollegata: seriale giu', il nodo continua", not link.state()["serial"] and link.state()["udp"])
        link.tick(time.time() + 11)
        radio.accept()
        check("radio ricollegata dopo 10 s", link.state()["serial"])
        radio.riga(b"R-61 " + encode(hz("p-4", to=["incrocio-5"])) + b"\n")
        check("dopo la riconnessione si riceve ancora", gira(link, lambda: any(m.get("id") == "p-4" for m in msgs)))
    finally:
        link.close()
        radio.close()
        tu.close()


@test
def test_ponte_sim_udp():
    """Un incrocio con bus virtuale e UDP: cio' che arriva via radio passa in UDP e viceversa."""
    bus = SimRadioBus(180)
    pu = porta_libera()
    tu = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tu.bind(("127.0.0.1", 0))
    tu.settimeout(0.05)
    msgs = []
    link = PeerLink({"enabled": True, "transports": ["sim", "udp"], "port": pu, "bind_port": pu, "heartbeat": False,
                     "peers": [f"127.0.0.1:{tu.getsockname()[1]}"]}, "G", msgs.append, start_thread=False,
                    sim_bus=bus, position=nord(0))
    r = Radio(bus, "radio", nord(20))
    try:
        st = link.state()
        check("sim + udp attivi insieme", st["sim"] and st["udp"], st["transports"])
        r.send(hz("s-1", to=["lontano"], h=2))
        u = udp_ricevi(tu, link)
        check("ponte sim -> udp: rilanciato con h - 1", u and u[0]["id"] == "s-1" and u[0]["h"] == 1, u)
        check("... e di nuovo sulla radio virtuale", [(o["id"], o["h"]) for o in r.objs("hz")] == [("s-1", 1)], r.objs())
        r.rx.clear()
        tu.sendto(encode(hz("s-2", to=["lontano"], h=1)), ("127.0.0.1", pu))
        gira(link, lambda: bool(r.rx))
        check("ponte udp -> sim: rilanciato con h - 1", [(o["id"], o["h"]) for o in r.objs("hz")] == [("s-2", 0)], r.objs())
        u = udp_ricevi(tu, link)
        check("... e ai peer unicast (non al gruppo multicast)", u and u[0]["id"] == "s-2" and u[0]["h"] == 0, u)
        r.rx.clear()
        r.send({"v": 2, "t": "vru", "d": "tag-1", "p": "blind", "q": 1})
        gira(link, lambda: any(m.get("type") == "vru_beacon" for m in msgs))
        u = udp_ricevi(tu, link, 0.3)
        check("beacon dei Tag: consegnati ma mai passati di ponte", not u and not r.rx
              and any(m.get("type") == "vru_beacon" and m["via"] == "sim" for m in msgs), (u, r.rx))
        check("contatori del ponte", link.counters["bridged"] == 2 and link.counters["relayed"] == 2, link.counters)
    finally:
        link.close()
        tu.close()


@test
def test_topologia_dopo():
    """Topologia arrivata dopo l'avvio (o ricaricata): vicini e posizione sul bus aggiornati."""
    bus = SimRadioBus(180)
    n = Nodo(bus, "T", None)
    try:
        check("senza posizione: sentito da tutti con RSSI basso", bus.position("T") is None)
        topo = Topology.from_dict({"nodes": {"T": {"lat": nord(10)[0], "lon": LON0, "neighbors": {"U": {"m": 80}}},
                                             "U": {"lat": nord(90)[0], "lon": LON0}}})
        n.link.set_topology(topo)
        check("set_topology: vicini e posizione", n.link.neighbors == {"U"} and bus.position("T") == nord(10),
              (n.link.neighbors, bus.position("T")))
    finally:
        chiudi(n)


@test
def test_topologia_vera():
    """Instradamento (route.py) + protocollo sul topology.json vero, tutti gli incroci sul bus virtuale."""
    f = PY / "topology.json"
    topo = Topology.load(f) if f.exists() else None
    if topo is None or len(topo) < 5:
        check("topology.json vero assente: prova saltata", True)
        return
    ids = topo.ids()
    src = dst = None
    percorso, riserva = [], None

    def tratto_max(p):
        return max(peer._dist_m(topo.pos(a), topo.pos(b)) for a, b in zip(p, p[1:]))
    for s in ids[:80]:          # percorso di 3-5 tratti, se possibile con tratti entro la portata vera (180 m)
        ps = topo.pos(s)
        if not ps:
            continue
        lontani = [d for d in ids if topo.pos(d) and 250 <= peer._dist_m(ps, topo.pos(d)) <= 600][:6]
        for d in lontani:
            p = topo.path(s, d)
            if 4 <= len(p) <= 6 and all(topo.pos(x) for x in p):
                if tratto_max(p) <= 175:
                    src, dst, percorso = s, d, p
                    break
                riserva = riserva or (s, d, p)
        if src:
            break
    if not src and riserva:
        src, dst, percorso = riserva
    if not src:
        check("topology.json vero: nessun percorso di 3-5 tratti (prova saltata)", True)
        return
    nb = topo.neighbors(src)
    hd = topo.bearing(src, nb[0]) if nb else None
    tg = topo.targets_for(src, hd, category="emergency") if hd is not None else []
    catene = (percorso, [src] + tg if all(topo.pos(x) for x in tg) else [])
    salti = [peer._dist_m(topo.pos(a), topo.pos(b)) for c in catene for a, b in zip(c, c[1:])]
    portata = max(180.0, max(salti) * 1.05 + 5)   # ogni tratto della catena e' un salto radio possibile
    bus = SimRadioBus(portata)
    nodi = {nid: Nodo(bus, nid, None, topology=topo) for nid in ids if topo.pos(nid)}
    try:
        targets = topo.targets_for(src, dst=dst)
        check(f"route: destinatari verso {dst} = percorso piu' breve", targets == percorso[1:], (targets, percorso))
        t0 = time.time()
        ok = nodi[src].link.send_hazard(hazard(f"{src}-cam0-000001"), targets=targets)
        pompa(bus, list(nodi.values()), giri=40)
        chi = sorted(n for n, x in nodi.items() if any(m.get("id") == f"{src}-cam0-000001" for m in x.msgs))
        check(f"citta' vera ({len(nodi)} incroci, portata {portata:.0f} m): hz consegnato solo al percorso "
              f"({1000 * (time.time() - t0):.0f} ms)", ok and chi == sorted(targets), (chi, targets))
        if tg and catene[1]:
            nodi[src].link.send_hazard(hazard(f"{src}-cam0-000002", heading=hd), targets=tg)
            pompa(bus, list(nodi.values()), giri=40)
            chi = sorted(n for n, x in nodi.items() if any(m.get("id") == f"{src}-cam0-000002" for m in x.msgs))
            check(f"citta' vera: emergenza verso {hd:.0f} gradi arriva ai {len(tg)} incroci davanti", chi == sorted(tg),
                  (chi, tg))
        nodi[src].link.send_hazard(hazard(f"{src}-cam0-000003"))
        pompa(bus, list(nodi.values()), giri=40)
        chi = {n for n, x in nodi.items() if any(m.get("id") == f"{src}-cam0-000003" for m in x.msgs)}
        diretti = {n for n in nb if n in nodi and peer._dist_m(topo.pos(src), topo.pos(n)) <= portata}
        check("citta' vera: senza to lo consegnano solo i vicini del mittente", chi <= set(nb) and diretti <= chi,
              (sorted(chi), nb))
    finally:
        chiudi(*nodi.values())


# ---------------------------------------------------------------------- 6. bus virtuale a piu' salti

@test
def test_piu_salti():
    check("RSSI dalla distanza", (rssi_from_distance(1), rssi_from_distance(10), rssi_from_distance(100),
                                  rssi_from_distance(0.1)) == (-40, -65, -90, -40))
    bus = SimRadioBus(range_m=180)
    topo = Topology.from_dict({"nodes": {
        "A": {"lat": nord(0)[0], "lon": LON0, "neighbors": {"B": {}}},
        "B": {"lat": nord(150)[0], "lon": LON0, "neighbors": {"C": {}}},
        "C": {"lat": nord(300)[0], "lon": LON0, "neighbors": {"D": {}}},
        "D": {"lat": nord(450)[0], "lon": LON0}}})
    nodi = [Nodo(bus, nid, None, topology=topo) for nid in "ABCD"]
    a, b, c, d = nodi
    try:
        vicini = bus.in_range("A")
        check("portata 180 m: A sente solo B", [x[0] for x in vicini] == ["B"] and vicini[0][1] == 150.0, vicini)
        ok = a.link.send_hazard(hazard("A-cam0-000001"), targets=["D"])
        pompa(bus, nodi)
        chi = [x.link.node_id for x in nodi if x.di("remote_hazard")]
        check("hz con to=[D]: inviato", ok)
        check("hz con to=[D]: arriva solo a D", chi == ["D"], chi)
        check("passando dagli intermedi (B e C rilanciano, D no)",
              [x.link.counters["relayed"] for x in nodi] == [0, 1, 1, 0], [x.link.counters["relayed"] for x in nodi])
        m = d.di("remote_hazard")[0]
        check("a D arriva con h = 0 e l'RSSI dell'ultimo salto", m["hops_left"] == 0 and m["rssi"] == rssi_from_distance(150),
              m)
        check("A non si riconsegna il suo pericolo", not a.msgs)
        a.link.send_hazard(hazard("A-cam0-000002"), targets=["D"], hops=1)
        pompa(bus, nodi)
        check("h troppo piccolo: D non lo riceve", all(m["id"] != "A-cam0-000002" for m in d.msgs))
        a.link.send_hazard(hazard("A-cam0-000003"), targets=["B", "C", "D"])
        pompa(bus, nodi)
        chi = [x.link.node_id for x in nodi if any(m["id"] == "A-cam0-000003" for m in x.msgs)]
        check("percorso intero: arriva a tutti i destinatari", chi == ["B", "C", "D"], chi)
        a.link.send_hazard(hazard("A-cam0-000004"))
        pompa(bus, nodi)
        chi = [x.link.node_id for x in nodi if any(m["id"] == "A-cam0-000004" for m in x.msgs)]
        check("senza to: solo i vicini del mittente (topologia)", chi == ["B"], chi)
        check("senza propagate: non parte", not a.link.send_hazard(hazard("A-x", category="info", propagate=False)))
        check("emergenza parte comunque", a.link.send_hazard(hazard("A-y", propagate=False), targets=["B"]))
        pompa(bus, nodi)
        # passo passo con il bus in pausa
        bus.pause()
        a.link.send_hazard(hazard("A-cam0-000005"), targets=["D"])
        passi = 0
        while not any(m["id"] == "A-cam0-000005" for m in d.msgs) and passi < 10:
            check_pending = bus.pending()
            bus.flush()
            for x in nodi:
                x.link.poll_once(0)
            passi += 1
            if check_pending == 0:
                break
        check("bus in pausa: consegna passo passo (3 salti)", passi == 3, passi)
        bus.resume()
        check("contatori del bus", bus.counters["frames"] > 0 and bus.counters["out_of_range"] > 0, bus.counters)
        bus.move("D", nord(160))
        check("move: D ora e' vicino ad A e B", [x[0] for x in bus.in_range("D")] == ["A", "B", "C"], bus.in_range("D"))
    finally:
        chiudi(*nodi)


# ---------------------------------------------------------------------- 7. beacon dei Tag

@test
def test_beacon():
    bus = SimRadioBus(180)
    b = Nodo(bus, "B", nord(0))
    a = Nodo(bus, "A", nord(150))
    lontano = Nodo(bus, "L", nord(150), near_rssi=-100)
    tag = Radio(bus, "tag-1", nord(10))
    try:
        tag.send({"v": 2, "t": "vru", "d": "tag-1", "p": "blind", "q": 1, "b": 87, "hd": 180, "sp": 4.5})
        pompa(bus, [a, b, lontano])
        mb, ma, ml = b.di("vru_beacon")[0], a.di("vru_beacon")[0], lontano.di("vru_beacon")[0]
        check("vru a 10 m: near, RSSI -65", mb["near"] is True and mb["rssi"] == -65, mb)
        check("vru a 140 m: sentito ma non near", ma["near"] is False and ma["rssi"] == rssi_from_distance(140), ma)
        check("near_rssi configurabile", ml["near"] is True)
        diff = {k: mb.get(k) for k in ("type", "device", "role", "seq", "battery", "heading", "speed", "via")}
        check("vru_beacon normalizzato", diff == {"type": "vru_beacon", "device": "tag-1", "role": "blind", "seq": 1,
                                                 "battery": 87, "heading": 180, "speed": 4.5, "via": "sim"}, diff)
        bus.move("tag-1", nord(140))
        tag.send({"v": 2, "t": "vru", "d": "tag-1", "p": "blind", "q": 2})
        pompa(bus, [a, b])
        check("il Tag si sposta: ora e' vicino ad A", a.di("vru_beacon")[-1]["near"] and not b.di("vru_beacon")[-1]["near"])
        tag.send({"v": 2, "t": "emv", "d": "amb-1", "q": 5, "hd": 90, "nx": "incrocio-7", "st": "run"})
        tag.send({"v": 2, "t": "emv", "d": "amb-1", "q": 6})
        pompa(bus, [a])
        e = a.di("emergency_beacon")
        check("emv -> emergency_beacon", len(e) == 2 and e[0]["siren"] is True and e[0]["next_node"] == "incrocio-7"
              and e[0]["heading"] == 90 and e[0]["near"] and e[1]["siren"] is False and e[1]["next_node"] is None, e)
        tag.send(b'{"v":1,"t":"vru","d":"polso-1","p":"vehicle","ts":1,"h":0}')
        tag.send({"v": 2, "t": "vru", "p": "blind"})                  # senza id del dispositivo
        tag.send({"v": 2, "t": "vru", "d": "x" * 100, "p": "blind"})  # id assurdo
        pompa(bus, [a])
        check("beacon v1: profilo vehicle -> driver", a.di("vru_beacon")[-1]["role"] == "driver")
        check("beacon non validi scartati", a.link.counters["invalid"] == 2, a.link.counters)
        v = a.link.vrus()
        check("registro dei Tag", set(v) == {"tag-1", "amb-1", "polso-1"} and v["tag-1"]["role"] == "blind"
              and v["amb-1"]["role"] == "emergency", v)
    finally:
        chiudi(a, b, lontano)


# ---------------------------------------------------------------------- 8. collettore: rq / rs

@test
def test_rq_rs():
    bus = SimRadioBus(180)
    nodi = [Nodo(bus, n, nord(150 * i), key="k") for i, n in enumerate("ABC")]
    a, b, c = nodi
    visti = []
    coll = PeerLink({"enabled": True, "transports": ["sim"], "heartbeat": False, "key": "k"}, "collettore",
                    lambda m: None, start_thread=False, sim_bus=bus, position=nord(-100))
    coll.relay = False
    coll.monitor = lambda o, tr, rssi: visti.append((o, tr, rssi))

    class C:
        link = coll
    tutti = nodi + [C]
    try:
        check("rq inviato", coll.send_obj({"t": "rq", "id": "rq-1", "w": "sum"}))
        pompa(bus, tutti)
        sr = a.di("summary_request")
        check("rq: lo riceve chi e' in portata", len(sr) == 1 and sr[0]["id"] == "rq-1" and sr[0]["from"] == "collettore"
              and sr[0]["what"] == "sum" and not b.msgs, (sr, b.msgs))
        coll.send_obj({"t": "rq", "id": "rq-1", "w": "sum"})
        pompa(bus, tutti)
        check("rq: dedup", len(a.di("summary_request")) == 1)
        a.link.send_obj({"t": "rs", "id": "rq-1", "p": {"pw": "a", "hz": ["A-cam0-000001"], "u": 2, "ev": 10, "wk": 3}})
        pompa(bus, tutti)
        rs = [o for o, _, _ in visti if o.get("t") == "rs"]
        check("rs: arriva al collettore, firmata, h = 0", rs and rs[0]["n"] == "A" and rs[0]["h"] == 0 and verify(rs[0], "k"),
              rs)
        nr = normalize(rs[0], "sim", -90)
        check("normalize(rs) -> summary_response", nr["type"] == "summary_response" and nr["node"] == "A"
              and nr["summary"]["u"] == 2, nr)
        coll.send_obj({"t": "rq", "id": "rq-2", "w": "sum", "h": 2})
        pompa(bus, tutti)
        chi = [x.link.node_id for x in nodi if any(m["id"] == "rq-2" for m in x.di("summary_request"))]
        check("rq con h = 2: arriva a tutta la catena", chi == ["A", "B", "C"], chi)
        coll.send_obj({"t": "rq", "id": "rq-3", "w": "sum", "h": 1, "to": ["B"]})
        pompa(bus, tutti)
        chi = [x.link.node_id for x in nodi if any(m["id"] == "rq-3" for m in x.di("summary_request"))]
        check("rq con to: solo il destinatario risponde", chi == ["B"], chi)
        b.link.send_obj({"t": "rs", "id": "rq-1", "p": {"pw": "s"}, "h": 1})
        c.link.send_obj({"t": "rs", "id": "rq-1", "p": {"pw": "s"}, "h": 2})
        pompa(bus, tutti)
        da = sorted({o["n"] for o, _, _ in visti if o.get("t") == "rs"})
        check("rs di piu' nodi con lo stesso id (rilanciate se h > 0)", da == ["A", "B", "C"], da)
        # un collettore senza la chiave condivisa: i nodi scartano la sua richiesta
        estraneo = PeerLink({"enabled": True, "transports": ["sim"], "heartbeat": False}, "estraneo",
                            lambda m: None, start_thread=False, sim_bus=bus, position=nord(-90))
        try:
            bad = a.link.counters["bad_sig"]
            check("rq senza chiave: inviato", estraneo.send_obj({"t": "rq", "id": "rq-4", "w": "sum"}))
            pompa(bus, tutti)
            chi = [x.link.node_id for x in nodi if any(m["id"] == "rq-4" for m in x.di("summary_request"))]
            check("rq non firmato da chi non ha la chiave: scartato", not chi and a.link.counters["bad_sig"] == bad + 1,
                  (chi, a.link.counters))
        finally:
            estraneo.close()
        # rq senza "n" (collettore minimale): accettato se firmato
        r = Radio(bus, "radio-coll", nord(-50))
        r.send({"v": 2, "t": "rq", "id": "rq-5", "w": "sum"}, key="k")
        pompa(bus, tutti)
        sr = [m for m in a.di("summary_request") if m["id"] == "rq-5"]
        check("rq senza n ma firmato: consegnato", len(sr) == 1 and sr[0]["from"] is None, sr)
    finally:
        chiudi(*nodi)
        coll.close()


# ---------------------------------------------------------------------- 9. heartbeat

@test
def test_heartbeat():
    bus = SimRadioBus(180)
    a, b = Nodo(bus, "A", nord(0)), Nodo(bus, "B", nord(150))
    try:
        check("nessuno sentito: offline", not b.link.online and b.link.peers() == {})
        a.link.send_heartbeat("s")
        pompa(bus, [a, b])
        p = b.link.peers().get("A") or {}
        check("heartbeat: peer con stato di energia", set(p) == {"last_seen", "pw", "rssi", "via"} and p["pw"] == "s"
              and p["via"] == "sim" and p["rssi"] == rssi_from_distance(150), p)
        check("heartbeat: online", b.link.online)
        check("set_power: cambio -> heartbeat subito", a.link.set_power("active") is True)
        pompa(bus, [a, b])
        check("set_power: il vicino vede 'a'", b.link.peers()["A"]["pw"] == "a")
        check("set_power: stesso stato -> niente", a.link.set_power("a") is False and a.link.set_power("boh") is False)
        a.link.heartbeat = True
        a.link.send_heartbeat("sleep")
        tx = a.link.counters["tx"]
        now = time.time()
        a.link.tick(now + 1)
        check("a riposo: niente heartbeat prima di hb_sleep_s", a.link.counters["tx"] == tx)
        a.link.tick(now + 10.5)
        check("a riposo: heartbeat dopo hb_sleep_s", a.link.counters["tx"] == tx + 1)
        a.link.send_heartbeat("cooldown")
        tx = a.link.counters["tx"]
        a.link.tick(now + 13)
        check("sveglio: niente heartbeat prima di hb_active_s", a.link.counters["tx"] == tx)
        a.link.tick(now + 16)
        check("sveglio: heartbeat ogni hb_active_s", a.link.counters["tx"] == tx + 1 and a.link.power_state == "c")
        # orologio del simulatore (secondi dall'inizio della simulazione, non l'ora vera)
        s = Nodo(bus, "S", nord(300))
        s.link.heartbeat = True
        inviati = []
        for t in (0.0, 1.0, 4.9, 5.0, 7.5, 10.0, 10.1):
            tx = s.link.counters["tx"]
            s.link.tick(t)
            if s.link.counters["tx"] > tx:
                inviati.append(t)
        check("orologio simulato: heartbeat a 0, 5, 10 s", inviati == [0.0, 5.0, 10.0], inviati)
        chiudi(s)
        st = b.link.state()
        check("state()", st["online"] and st["sim"] and not st["udp"] and st["peers"] == ["A"] and "counters" in st, st)
    finally:
        chiudi(a, b)


# ---------------------------------------------------------------------- 10. robustezza

@test
def test_robustezza():
    bus = SimRadioBus(180)

    def esplode(m):
        raise RuntimeError("errore del nodo")
    x = Nodo(bus, "X", nord(0), on_message=esplode)
    x.link.monitor = lambda o, t, r: 1 / 0
    r = Radio(bus, "radio", nord(5))
    try:
        r.send({"v": 2, "t": "vru", "d": "tag-1", "p": "blind"})
        for cattivo in (b"\xff\xfe", b"[]", b"{", b"null", b'{"v":3}', b"[" * 100000,
                        b'{"v":2,"t":"hz","id":5,"n":"Y","a":"n"}', b'{"v":2,"t":"hz","id":"x","n":"Y","a":"boh"}',
                        b'{"v":2,"t":"hb"}', b'{"v":2,"t":"hz","id":"x","n":5}'):
            r.send(cattivo)
        pompa(bus, [x])
        check("on_message e monitor che sollevano: il link continua", x.link.counters["beacons"] == 1)
        check("frame non validi contati", x.link.counters["invalid"] == 10, x.link.counters)
    finally:
        chiudi(x)
    spento = PeerLink({}, "S", print)
    check("p2p spento: niente invii, niente eccezioni", not spento.enabled and not spento.send_hazard(hazard("s"))
          and not spento.send_obj({"t": "adv"}) and not spento.send_heartbeat() and spento.poll_once(0.01) == 0
          and spento.state()["enabled"] is False)
    spento.close()
    PeerLink(None, "N", print).close()
    strano = PeerLink({"enabled": True, "transports": "lora sim", "heartbeat": False}, "W", print, start_thread=False)
    check("trasporto sconosciuto ignorato, sim senza bus ignorato", strano.transports == [])
    strano.tick()
    strano.close()
    pc = PeerLink({"enabled": True, "transports": ["serial"], "serial": "socket://127.0.0.1:1", "heartbeat": False},
                  "P", print, start_thread=False)
    pc.tick(time.time() + 11)
    check("radio irraggiungibile: nessuna eccezione, seriale giu'", not pc.state()["serial"] and pc.poll_once(0.01) == 0)
    pc.close()
    if os.name != "posix":
        w = PeerLink({"enabled": True, "transports": ["serial"], "serial": {"port": "/dev/ttyACM0"}, "heartbeat": False},
                     "Q", print, start_thread=False)
        check("porta seriale Linux sul PC: disattivata senza eccezioni", not w.state()["serial"])
        w.close()
    bus = SimRadioBus(180)
    n = Nodo(bus, "N", nord(0))
    check("send_obj con tipo sbagliato: False", not n.link.send_obj({"t": "boh"}) and not n.link.send_obj("x"))
    check("send_hazard con un remote_hazard: False", not n.link.send_hazard({"type": "remote_hazard", "id": "x"}))
    check("send_hazard con dati strani: False o True, mai eccezioni",
          n.link.send_hazard({"id": None, "severity": "x", "ts": "y", "expires": None, "actor": 5}) in (True, False))
    n.link.close()
    n.link.close()
    check("dopo close: niente invii", not n.link.send_obj({"t": "adv", "id": "a"}) and n.link.poll_once(0) == 0)
    # con i thread veri
    bus = SimRadioBus(180)
    ricevuti = []
    a = PeerLink({"enabled": True, "transports": ["sim"], "heartbeat": False}, "A", lambda m: None,
                 sim_bus=bus, position=nord(0))
    b = PeerLink({"enabled": True, "transports": ["sim"], "heartbeat": False}, "B", ricevuti.append,
                 sim_bus=bus, position=nord(100))
    try:
        t0 = time.time()
        a.send_hazard(hazard("A-t-1"), targets=["B"])
        while not ricevuti and time.time() - t0 < 2:
            time.sleep(0.01)
        check(f"con i thread: consegnato in {1000 * (time.time() - t0):.0f} ms", ricevuti and ricevuti[0]["id"] == "A-t-1")
    finally:
        t0 = time.time()
        a.close()
        b.close()
        check("close con i thread: rapido", time.time() - t0 < 2)


# ---------------------------------------------------------------------- 11. normalize e riga di comando

@test
def test_normalize():
    o = decode(encode(hazard_to_p2p(hazard("incrocio-142-cam0-000123", heading=225), "incrocio-142",
                                    ["incrocio-117", "incrocio-118", "incrocio-119"])))
    m = normalize(o, "udp")
    check("normalize(hz): id ricostruito", m["type"] == "remote_hazard" and m["id"] == "incrocio-142-cam0-000123", m)
    hb = normalize({"v": 2, "t": "hb", "n": "A", "ts": 1, "pw": "s", "h": 0}, "serial", -70)
    check("normalize(hb)", hb["type"] == "peer_heartbeat" and hb["node"] == "A" and hb["pw"] == "s" and hb["rssi"] == -70)
    adv = normalize({"v": 2, "t": "adv", "n": "A", "id": "a1", "to": "tag-1", "c": 2, "s": 1, "x": 0, "ttl": 3000})
    check("normalize(adv)", adv["type"] == "advice" and adv["device"] == "tag-1" and adv["code"] == 2, adv)
    vru = normalize({"v": 2, "t": "vru", "d": "t", "p": "cyclist"}, "sim", -80, near_rssi=-75)
    check("normalize(vru)", vru["type"] == "vru_beacon" and vru["near"] is False and vru["role"] == "cyclist")
    check("normalize di spazzatura: None", normalize({"t": "boh"}) is None and normalize({"t": "hz"}) is None)
    ruoli = [peer.role_of(x) for x in ("blind", "Ciclista", "vehicle", "person", "ambulance", "astronauta", None, "")]
    check("ruoli: alias v1 e italiani, sconosciuto = pedone",
          ruoli == ["blind", "cyclist", "driver", "pedestrian", "emergency", "pedestrian", "pedestrian", "pedestrian"], ruoli)
    emv = normalize({"v": 2, "t": "emv", "d": "amb-1", "q": 3, "hd": "sud", "st": "run"}, "serial", -60)
    check("normalize(emv): direzione anche come punto cardinale", emv["type"] == "emergency_beacon"
          and emv["heading"] == 180 and emv["siren"] is True and emv["near"] is True, emv)
    rq = normalize({"v": 2, "t": "rq", "id": "rq-9", "w": "sum", "n": "collettore"}, "udp")
    check("normalize(rq) -> summary_request", rq["type"] == "summary_request" and rq["id"] == "rq-9", rq)


@test
def test_riga_di_comando():
    py = sys.executable
    r = subprocess.run([py, str(PY / "peer.py"), "--help"], capture_output=True, text=True, timeout=30)
    check("peer.py --help", r.returncode == 0 and "listen" in r.stdout, r.stderr[-300:])
    with tempfile.TemporaryDirectory() as tmp:
        porta = porta_libera()
        cfg = Path(tmp) / "config.json"
        cfg.write_text(json.dumps({"node_id": "prova", "p2p": {"port": porta, "bind_port": porta,
                                                               "transports": ["udp"]}}), encoding="utf-8")
        ascolto = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ascolto.bind(("127.0.0.1", 0))
        ascolto.settimeout(3)
        cfg.write_text(json.dumps({"node_id": "prova", "p2p": {"port": porta, "bind_port": porta, "transports": ["udp"],
                                                               "peers": [f"127.0.0.1:{ascolto.getsockname()[1]}"]}}),
                       encoding="utf-8")
        r = subprocess.run([py, str(PY / "peer.py"), "send", "--config", str(cfg), "--to", "incrocio-2", "incrocio-3",
                            "--heading", "est"], capture_output=True, text=True, timeout=30)
        check("peer.py send", r.returncode == 0 and "inviato" in r.stdout and "NON" not in r.stdout, r.stdout + r.stderr[-300:])
        try:
            o = decode(ascolto.recvfrom(4096)[0])
        except socket.timeout:
            o = None
        ascolto.close()
        check("peer.py send: hz v2 con to e direzione", o and o["t"] == "hz" and o["to"] == ["incrocio-2", "incrocio-3"]
              and o["hd"] == 90 and o["h"] == 3 and o["n"] == "prova-cli", o)


def main():
    for fn in (test_codifica, test_firma, test_dedup_e_rilancio, test_consegna, test_ponte_seriale_udp, test_ponte_sim_udp,
               test_topologia_dopo, test_piu_salti, test_topologia_vera, test_beacon, test_rq_rs, test_heartbeat,
               test_robustezza, test_normalize, test_riga_di_comando):
        print(f"\n== {fn.__name__}")
        fn()
    falliti = [r for r in RISULTATI if not r[1]]
    print(f"\n{len(RISULTATI) - len(falliti)}/{len(RISULTATI)} controlli OK")
    for nome, _, info in falliti:
        print(f"  FALLITO: {nome} -> {str(info)[:300]}")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
