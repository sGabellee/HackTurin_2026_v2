"""Test di signals.py e mcu.py (segnali e MCU, docs/ARCHITETTURA.md par. 2.3 e 2.4).

Prova:
  - codifica: LED 2 bit per LED, canali luce 4 bit per canale, codici sconosciuti -> unknown,
    bit di get_sensors;
  - FakeMCU: sensori impostabili, isteresi 3/10 letture, riflesso locale, fail-safe, resa PWM dei codici;
  - MCULink su un Bridge finto: chiamate sincrone, Bridge senza timeout, errori, sketch v1 (solo
    set_leds), thread "solo l'ultimo valore" che non blocca mai;
  - SignalController: i 4 LED come leds.py (presenze in tempo reale, pericolo locale che si spegne
    con la sua traccia, remoti fino a scadenza, emergenze, stato, keep-alive), luci delle strisce
    impacchettate in un solo send_signals, comportamento a riposo.

Uso:  python test_signals.py      (dalla cartella node/tools/tests; esce con codice 1 se qualcosa fallisce)
"""
import contextlib
import io
import sys
import threading
import time
import traceback
from pathlib import Path

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI.parents[1] / "python"))

import mcu as M  # noqa: E402
from mcu import FakeMCU, MCULink  # noqa: E402
from signals import SignalController  # noqa: E402

RISULTATI = []


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


class Orologio:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeBridge:
    """Bridge finto: risponde come lo sketch v2 (o v1 con legacy=True)."""

    def __init__(self, word=0x111, dist=(450, -1, -1, -1), legacy=False, timeout_kw=True, delay=0.0):
        self.calls = []
        self.word = word
        self.dist = list(dist)
        self.legacy = legacy
        self.timeout_kw = timeout_kw
        self.delay = delay
        self.fail = False
        self._lock = threading.Lock()

    def call(self, name, *args, **kw):
        if "timeout" in kw and not self.timeout_kw:
            raise TypeError("call() got an unexpected keyword argument 'timeout'")
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.calls.append((name, args, dict(kw)))
        if self.fail:
            raise ConnectionError("router non raggiungibile")
        if self.legacy and name != "set_leds":
            raise RuntimeError(f"method not found: {name}")
        if not self.legacy and name == "set_leds":
            raise RuntimeError("method not found: set_leds")
        if name == "get_sensors":
            return self.word
        if name == "get_distance":
            return self.dist[args[0]]
        return 0

    def named(self, name):
        with self._lock:
            return [c for c in self.calls if c[0] == name]


def modes(fake):
    return M.unpack_leds(fake.last_signals["leds"]) if fake.last_signals else None


def codes(fake):
    return M.unpack_xwalk(fake.last_signals["xwalk"]) if fake.last_signals else None


# ---------------------------------------------------------------------- codifica

def test_codifica():
    check("pack_leds: LED1 bit 0-1 ... LED4 bit 6-7", M.pack_leds([1, 2, 3, 0]) == 0b00111001
          and M.unpack_leds(0b11000110) == [2, 1, 0, 3])
    check("pack_xwalk: 4 bit per canale (canale 0 bit 0-3)", M.pack_xwalk({0: 4, 1: 3}) == 0x34
          and M.pack_xwalk({3: 5}) == 0x5000 and M.pack_xwalk([1, 0, 2, 6]) == 0x6201)
    check("pack_xwalk: nomi dei codici", M.pack_xwalk({0: "assist", 2: "danger"}) == 0x304)
    check("codice sconosciuto -> unknown (6)", M.light_code(9) == 6 and M.light_code("boh") == 6
          and M.light_code(None) == 6 and M.pack_xwalk({0: 12}) == 6)
    check("unpack_xwalk: 7..15 resi come unknown (come lo sketch)", M.unpack_xwalk(0xF0A1) == [1, 6, 0, 6])
    check("canale fuori range ignorato", M.pack_xwalk({4: 1, -1: 1}) == 0)
    p = M.parse_sensors(0x111, curbs=1)
    check("get_sensors: bit 0 presenza, bit 4 funzionante, bit 8 pronto",
          p["ready"] and p["curbs"] == [{"id": 0, "present": True, "ok": True}], p)
    p = M.parse_sensors(0x001, curbs=2)
    check("get_sensors: presenza ignorata se il sensore non funziona, non pronto",
          not p["ready"] and p["curbs"][0] == {"id": 0, "present": False, "ok": False}
          and p["curbs"][1]["ok"] is False, p)
    p = M.parse_sensors(0x165, curbs=4)   # presenza su 0 e 2, funzionanti 1 e 2
    check("get_sensors: 4 bordi", [c["present"] for c in p["curbs"]] == [False, False, True, False]
          and [c["ok"] for c in p["curbs"]] == [False, True, True, False], p)


# ---------------------------------------------------------------------- FakeMCU

def test_fake_sensori():
    f = FakeMCU({"curbs": 1})
    s = f.poll_sensors()
    check("FakeMCU: pronto, bordo 0 funzionante e libero", s["ready"] and s["curbs"] == [{"id": 0, "present": False, "ok": True}], s)
    f.set_curb(0, present=True)
    check("FakeMCU: presenza impostata", f.poll_sensors()["curbs"][0]["present"] and f.distance(0) is not None)
    f.set_distance(0, 1200)
    check("FakeMCU: distanza oltre 900 mm = nessuno", not f.poll_sensors()["curbs"][0]["present"] and f.distance(0) == 1200)
    f.set_distance(0, 600)
    check("FakeMCU: distanza 600 mm = presenza", f.poll_sensors()["curbs"][0]["present"] and f.distance(0) == 600)
    f.set_fault(0)
    s = f.poll_sensors()
    check("FakeMCU: guasto -> ok False, presenza False, distanza None",
          s["curbs"][0] == {"id": 0, "present": False, "ok": False} and f.distance(0) is None, s)
    f.set_fault(0, False)
    check("FakeMCU: sensore di nuovo funzionante", f.poll_sensors()["curbs"][0]["ok"])
    check("FakeMCU: bordo inesistente -> distanza None", f.distance(3) is None and f.distance("x") is None)
    f.ready = False
    check("FakeMCU: sketch non pronto (bit 8)", f.poll_sensors()["ready"] is False and f.sensors_word() & 0x100 == 0)
    f.offline = True
    check("FakeMCU: Bridge giu' -> poll_sensors None", f.poll_sensors() is None)
    f2 = FakeMCU(2)
    check("FakeMCU(2): due bordi", len(f2.poll_sensors()["curbs"]) == 2)


def test_fake_isteresi():
    f = FakeMCU({"curbs": 1})
    r = [f.reading(0, 500) for _ in range(3)]
    check("isteresi: presenza solo alla 3a lettura sotto 900 mm", r == [False, False, True], r)
    r = [f.reading(0, 1500) for _ in range(9)]
    check("isteresi: ancora presente dopo 9 letture sopra soglia", all(r), r)
    check("isteresi: assente alla 10a lettura", f.reading(0, None) is False)
    f.reading(0, 500)
    f.reading(0, 500)
    f.reading(0, 2000)
    check("isteresi: letture sotto soglia non di fila non bastano", f.reading(0, 500) is False)

    g = FakeMCU({"curbs": 1}, clock=Orologio(10.0))
    g.reading(0, 700)
    g.reading(0, 700)
    ok = g.output()["xwalk"][0] == 0
    g.reading(0, 700)
    check("isteresi + riflesso: il canale 0 passa a unknown solo alla 3a lettura (senza comando)",
          ok and g.output()["xwalk"][0] == 6 and g.poll_sensors()["curbs"][0]["present"]
          and g.distance(0) == 700, g.output())


def test_fake_riflesso_failsafe():
    clk = Orologio(100.0)
    f = FakeMCU({"curbs": 1}, clock=clk)
    o = f.output()
    check("avvio senza comandi: fail-safe (LED4 lampo, strisce spente)",
          o["failsafe"] and o["leds"] == [0, 0, 0, M.FAILSAFE_LED4] and o["xwalk"] == [0, 0, 0, 0], o)
    f.set_curb(0, present=True)
    o = f.output()
    check("riflesso locale: presenza sul bordo senza comando -> canale 0 unknown subito",
          o["xwalk"][0] == 6 and o["reflex"][0] and o["names"][0] == "unknown", o)
    check("riflesso: solo il canale del sensore", o["xwalk"][1:] == [0, 0, 0], o)

    f.send_signals(M.pack_leds([1, 0, 0, 0]), M.pack_xwalk({0: 0}), 3000)
    o = f.output()
    check("comando valido: decide il Python (canale 0 off anche con presenza)",
          o["alive"] and o["xwalk"][0] == 0 and not o["reflex"][0] and o["leds"] == [1, 0, 0, 0], o)
    f.send_signals(0, M.pack_xwalk({0: 3}), 3000)
    check("comando valido: codice danger sul canale 0", f.output()["xwalk"][0] == 3)
    clk.t += 2.9
    check("comando ancora valido a 2,9 s (ttl 3 s)", f.output()["alive"])
    clk.t += 0.2
    o = f.output()
    check("fail-safe dopo ttl_ms: LED spenti tranne lampo LED4, riflesso sul bordo",
          o["failsafe"] and o["leds"] == [0, 0, 0, M.FAILSAFE_LED4] and o["xwalk"][0] == 6, o)
    f.set_curb(0, present=False)
    check("fail-safe senza nessuno sul bordo: strisce spente", f.output()["xwalk"] == [0, 0, 0, 0])
    f.set_curb(0, present=True)
    f.set_fault(0)
    check("fail-safe con sensore guasto: niente riflesso", f.output()["xwalk"][0] == 0)
    f.set_fault(0, False)
    f.offline = True
    f.send_signals(0, 1, 3000)
    check("Bridge giu': il comando non arriva, resta il fail-safe", f.output()["failsafe"])
    f.offline = False
    f.send_signals(0, 0x1, 3000)
    check("registra l'ultimo comando", f.last_signals["xwalk"] == 1 and f.last_signals["ttl_ms"] == 3000
          and f.sent >= 3 and list(f.calls)[-1] == (0, 1, 3000), f.last_signals)
    # lampo del fail-safe e resa PWM
    clk.t += 10
    acceso = sum(f.led_level(3, now=200.0 + k / 1000) for k in range(2000))
    check("fail-safe: LED4 acceso 150 ms ogni 2 s", acceso == 150, acceso)


def test_resa_pwm():
    on = lambda code: sum(1 for ms in range(1000) if M.light_duty(code, ms) == 255)  # noqa: E731
    check("off: sempre spenta", all(M.light_duty(0, ms) == 0 for ms in range(0, 2000, 7)))
    check("on: sempre accesa", all(M.light_duty(1, ms) == 255 for ms in range(0, 2000, 7)))
    check("attention: 1 Hz, meta' del tempo", on(2) == 500)
    check("danger: 4 Hz (4 lampi da 125 ms al secondo)", on(3) == 500
          and sum(1 for ms in range(1, 1000) if M.light_duty(3, ms) and not M.light_duty(3, ms - 1)) == 3)
    duty = [M.light_duty(4, ms) for ms in range(2000)]
    check("assist: respiro tra 20 % e 100 %", min(duty) == 51 and max(duty) == 255 and duty[0] == 51
          and duty[1000] == 255, (min(duty), max(duty)))
    check("emergency: doppio lampo (200 ms accesi al secondo, 2 fronti)", on(5) == 200
          and sum(1 for ms in range(1, 1000) if M.light_duty(5, ms) and not M.light_duty(5, ms - 1)) == 1
          and M.light_duty(5, 0) == 255)
    check("unknown: lampeggio lento con duty 25 %", on(6) == 250)
    check("codice non valido reso come unknown", on(11) == 250)
    f = FakeMCU(clock=Orologio(5.0))
    f.send_signals(0, M.pack_xwalk({0: 1}), 3000)
    check("FakeMCU.duty: canale acceso fisso", f.duty(0) == 255 and f.duty(1) == 0)


# ---------------------------------------------------------------------- MCULink

def test_mcu_sincrono():
    b = FakeBridge()
    m = MCULink(b, {"curbs": 1}, start_thread=False)
    m.send_signals(0x12, 0x0034, 3000)
    c = b.named("set_signals")
    check("send_signals: una chiamata set_signals(leds, xwalk, ttl) con timeout",
          c and c[-1][1] == (0x12, 0x34, 3000) and "timeout" in c[-1][2], c)
    s = m.poll_sensors()
    check("poll_sensors: decodifica get_sensors", s and s["ready"] and s["curbs"] == [{"id": 0, "present": True, "ok": True}], s)
    check("distance: mm dal sensore", m.distance(0) == 450)
    b.dist[0] = -1
    check("distance: -1 -> None", m.distance(0) is None)
    check("distance: indice fuori range -> None", m.distance(7) is None)
    st = m.state()
    check("state: invii, ultimo comando, sensori", st["sent"] == 1 and st["last_signals"]["xwalk"] == 0x34
          and st["sensors"]["curbs"][0]["present"] and not st["legacy_sketch"], st)

    b2 = FakeBridge(timeout_kw=False)
    m2 = MCULink(b2, {}, start_thread=False)
    m2.send_signals(1, 2, 3000)
    m2.send_signals(1, 3, 3000)
    check("Bridge senza parametro timeout: ripiego senza timeout",
          len(b2.named("set_signals")) == 2 and m2.state()["send_errors"] == 0, b2.calls)

    b3 = FakeBridge()
    b3.fail = True
    m3 = MCULink(b3, {}, start_thread=False)
    m3.send_signals(1, 2, 3000)
    check("Bridge giu': nessuna eccezione, errore contato, poll None",
          m3.state()["send_errors"] == 1 and m3.poll_sensors() is None and m3.distance(0) is None)
    b3.fail = False
    m3.send_signals(1, 2, 3000)
    check("Bridge di nuovo su: invio riuscito", m3.state()["sent"] == 1)

    m4 = MCULink(None, {}, start_thread=False)
    m4.send_signals(1, 2, 3000)
    check("senza Bridge: niente sensori, nessuna eccezione", m4.poll_sensors() is None
          and m4.state()["last_signals"]["leds"] == 1)
    m4.send_signals("x", None, 3000)
    check("argomenti non validi: ignorati", m4.state()["last_signals"]["leds"] == 1)


def test_mcu_sketch_v1():
    b = FakeBridge(legacy=True)
    m = MCULink(b, {}, start_thread=False)
    m.send_signals(0x40, 0x1, 3000)
    check("sketch v1: ripiego su set_leds(leds, ttl)", b.named("set_leds") and b.named("set_leds")[-1][1] == (0x40, 3000)
          and m.state()["legacy_sketch"] and m.state()["sent"] == 1, b.calls)
    n = len(b.named("set_signals"))
    m.send_signals(0x41, 0x1, 3000)
    check("sketch v1: i comandi successivi vanno diretti a set_leds",
          len(b.named("set_signals")) == n and b.named("set_leds")[-1][1] == (0x41, 3000))
    check("sketch v1: poll_sensors None (get_sensors non esiste)", m.poll_sensors() is None)


def test_mcu_thread():
    b = FakeBridge(delay=0.15)
    m = MCULink(b, {"sensor_poll_s": 0.1, "distance_poll_s": 0.2}, start_thread=True)
    try:
        t0 = time.monotonic()
        for k in range(20):
            m.send_signals(k, k, 3000)
        dt = time.monotonic() - t0
        check("thread: send_signals non blocca mai (20 invii con Bridge lento)", dt < 0.05, dt)
        fine = time.monotonic() + 3
        while time.monotonic() < fine:
            c = b.named("set_signals")
            if c and c[-1][1][0] == 19:
                break
            time.sleep(0.02)
        c = b.named("set_signals")
        check("thread: vince l'ultimo valore (niente coda di vecchi comandi)",
              c and c[-1][1][0] == 19 and len(c) <= 3, [x[1][0] for x in c])
        fine = time.monotonic() + 3
        while time.monotonic() < fine and (m.poll_sensors() is None or m.distance(0) is None):
            time.sleep(0.05)
        check("thread: sensori e distanza letti in background", m.poll_sensors() is not None and m.distance(0) == 450)
    finally:
        m.close()
    check("thread: close() ferma il thread", m._thread is None or not m._thread.is_alive())

    b0 = FakeBridge()
    m0 = MCULink(b0, {"curbs": 0, "sensor_poll_s": 0.1}, start_thread=True)
    try:
        fine = time.monotonic() + 3
        while time.monotonic() < fine and m0.poll_sensors() is None:
            time.sleep(0.05)
        s = m0.poll_sensors()
        check("thread con curbs=0: get_sensors letto lo stesso (sketch pronto, nessun bordo)",
              s is not None and s["ready"] and s["curbs"] == [] and not b0.named("get_distance"), s)
    finally:
        m0.close()


# ---------------------------------------------------------------------- SignalController

RUOLI_SUITE = ["person", "emergency", "hazard", "cyclist"]


def test_led_come_prima():
    """Scenari di test_led_pericolo_si_spegne / test_led_tempo_reale della suite v1."""
    f = FakeMCU()
    s = SignalController({"roles": RUOLI_SUITE}, f)
    t = time.time()
    now = int(t * 1000)
    s.on_hazard({"id": "h1", "kind": "bike_lane_invasion", "severity": 3, "ts": now, "expires": now + 30000,
                 "actor": {"track": "cam0-9", "class": "vehicle"}}, True, t)
    s.set_presence({"vehicle"}, t, ["cam0-9"])
    s.push(t)
    check("LED rosso acceso mentre l'auto e' nella ciclabile", modes(f)[2] == 3, modes(f))
    s.set_presence({"vehicle"}, t + 0.3, ["cam0-9"])
    s.push(t + 0.3)
    check("LED rosso ancora acceso finche' l'auto si vede", modes(f)[2] > 0)
    s.push(t + 1.2)
    check("LED rosso spento appena l'auto sparisce (non dopo 10 s)", modes(f)[2] == 0, modes(f))
    s.on_hazard({"id": "r1", "kind": "bike_lane_invasion", "severity": 2, "ts": now, "expires": now + 30000,
                 "from_node": "incrocio-2"}, False, t + 1.2)
    s.push(t + 5)
    check("pericolo di un vicino: LED rosso lento anche senza oggetti in vista", modes(f)[2] == 2, modes(f))
    s.on_hazard({"id": "r1", "action": "cancel"}, False, t + 6)
    s.push(t + 6)
    check("cancel del pericolo del vicino: LED rosso spento", modes(f)[2] == 0)
    s.on_hazard({"id": "r2", "severity": 3, "ts": now, "expires": now + 10000}, False, t)
    s.push(t + 9)
    ok = modes(f)[2] == 3
    s.push(t + 10.5)
    check("pericolo remoto grave: veloce fino alla scadenza, poi spento", ok and modes(f)[2] == 0, modes(f))

    s.set_presence({"person"}, t + 20)
    s.push(t + 20)
    check("LED giallo acceso appena la camera vede una persona", modes(f)[0] == 1)
    s.push(t + 20.4)
    ok = modes(f)[0] == 1
    s.push(t + 21)
    check("LED giallo spento quando la persona sparisce (dopo 0,5 s)", ok and modes(f)[0] == 0, modes(f))
    s.set_presence({"cyclist"}, t + 30)
    s.push(t + 30)
    check("LED verde acceso con un ciclista", modes(f)[3] == 1, modes(f))
    s.set_presence({"emergency"}, t + 40)
    s.push(t + 40)
    check("LED blu lampeggio veloce con un mezzo di emergenza in vista", modes(f)[1] == 3, modes(f))
    s.on_hazard({"id": "e1", "kind": "emergency_vehicle", "category": "emergency", "severity": 3, "ts": now,
                 "expires": now + 60000}, False, t + 41)
    s.push(t + 42)
    check("ambulanza segnalata da un vicino: LED blu veloce, rosso spento", modes(f)[1] == 3 and modes(f)[2] == 0, modes(f))
    s.on_hazard({"id": "e1", "action": "cancel"}, False, t + 43)
    s.push(t + 43)
    check("cancel dell'ambulanza: LED blu spento", modes(f)[1] == 0, modes(f))
    s.on_hazard({"id": "lieve", "severity": 1, "ts": now, "expires": now + 30000}, True, t + 44)
    s.push(t + 44)
    check("pericolo locale sotto min_severity: LED rosso spento", modes(f)[2] == 0)
    s.on_hazard("non un dict", True, t)
    s.on_hazard({"severity": 3}, True, t)
    check("messaggi strani ignorati senza eccezioni", True)


def test_ruoli():
    f = FakeMCU()
    s = SignalController({}, f)
    check("ruoli di default: hazard, cyclist, person, emergency", s.roles == ["hazard", "cyclist", "person", "emergency"])
    s2 = SignalController({"roles": ["status", "pippo"]}, f)
    check("ruolo sconosciuto -> none, mancanti -> none", s2.roles == ["status", "none", "none", "none"], s2.roles)
    s2.tick(time.time(), True)
    check("status: acceso se online", modes(f)[0] == 1)
    s2.tick(time.time(), False)
    check("status: lento se offline", modes(f)[0] == 2)
    s3 = SignalController({"roles": ["local", "remote", "any", "vehicle"]}, f)
    t = time.time()
    now = int(t * 1000)
    s3.on_hazard({"id": "a", "severity": 2, "ts": now, "expires": now + 30000}, True, t)
    s3.on_hazard({"id": "b", "severity": 3, "ts": now, "expires": now + 30000}, False, t)
    s3.set_presence({"vehicle"}, t)
    s3.push(t)
    check("ruoli local/remote/any/vehicle", modes(f) == [1, 3, 3, 1], modes(f))


def test_strisce():
    f = FakeMCU()
    s = SignalController({"channels": 2}, f)
    t = time.time()
    s.set_crosswalks({0: 1})
    s.push(t)
    check("set_crosswalks {0: on}: xwalk = 1", f.last_signals["xwalk"] == 1, f.last_signals)
    n = f.sent
    s.set_crosswalks({0: 4, 1: 3})
    s.set_presence({"person"}, t)
    s.push(t)
    check("un solo send_signals con LED e strisce insieme", f.sent == n + 1 and f.last_signals["xwalk"] == 0x34
          and modes(f)[2] == 1, (f.sent - n, f.last_signals))
    check("codici impacchettati 4 bit per canale", codes(f) == [4, 3, 0, 0], codes(f))
    s.set_crosswalks({1: "danger"})
    s.push(t)
    check("canali assenti si spengono, nomi accettati", codes(f) == [0, 3, 0, 0], codes(f))
    s.set_crosswalks({0: 9})
    s.push(t)
    check("codice non valido -> unknown (6)", codes(f)[0] == 6, codes(f))
    s.set_crosswalks({0: 5, 3: 2, 9: 1})
    s.push(t)
    check("canale oltre channels mandato, canale inesistente ignorato", codes(f) == [5, 0, 0, 2], codes(f))
    st = s.state()
    check("state(): leds e crosswalks {canale: nome}",
          st["crosswalks"] == {0: "emergency", 1: "off", 2: "off", 3: "attention"}
          and st["leds"]["modes"] == modes(f) and st["leds"]["roles"] == s.roles and st["xwalk"] == 0x2005, st)
    s.set_crosswalks({0: 2})
    s.push(t)
    o = f.output()
    check("FakeMCU: lo sketch mostra il codice comandato", o["alive"] and o["xwalk"][0] == 2, o)
    s.set_crosswalks(None)
    s.set_crosswalks("rotto")
    s.push(t)
    check("set_crosswalks con valori strani: nessuna eccezione", f.last_signals is not None)


def test_keepalive():
    f = FakeMCU()
    s = SignalController({"refresh_s": 0.2, "ttl_ms": 1000}, f)
    t = time.time()
    s.tick(t, True)
    n = f.sent
    s.tick(t + 0.01, True)
    s.push(t + 0.02)
    check("niente invii doppi se non cambia nulla prima di refresh_s", f.sent == n, f.sent - n)
    time.sleep(0.25)
    s.tick(t + 0.3, True)
    check("keep-alive: rimanda il comando ogni refresh_s anche se non cambia", f.sent == n + 1, f.sent - n)
    check("ttl_ms passato allo sketch", f.last_signals["ttl_ms"] == 1000)
    s.set_presence({"person"}, t + 0.31)
    s.push(t + 0.31)
    check("tempo reale: un cambiamento parte subito con push()", f.sent == n + 2)
    s2 = SignalController({"refresh_s": 10, "ttl_ms": 3000}, f)
    check("refresh_s limitato a ttl_ms/2 (prima che l'MCU vada in fail-safe)", s2.refresh_s <= 1.5, s2.refresh_s)


def test_riposo():
    f = FakeMCU()
    s = SignalController({"roles": ["hazard", "cyclist", "person", "emergency"]}, f)
    t = time.time()
    now = int(t * 1000)
    s.on_hazard({"id": "l1", "kind": "near_miss", "severity": 3, "ts": now, "expires": now + 30000}, True, t)
    s.on_hazard({"id": "r1", "kind": "wrong_way", "severity": 2, "ts": now, "expires": now + 30000}, False, t)
    s.set_presence({"person", "cyclist"}, t)
    s.set_crosswalks({0: 1})
    s.tick(t, True)
    check("sveglio: rosso, verde e giallo accesi", modes(f)[:3] == [3, 1, 1], modes(f))
    check("set_power_state('sleep') segnala il cambio", s.set_power_state("sleep") is True)
    s.set_presence({"person", "cyclist"}, t + 0.1)
    s.tick(t + 0.1, True)
    check("a riposo: LED di presenza e pericolo (locale e remoto non di emergenza) spenti",
          modes(f) == [0, 0, 0, 0], modes(f))
    check("a riposo: le luci delle strisce restano comandate", codes(f)[0] == 1, codes(f))
    s.on_hazard({"id": "amb", "kind": "emergency_vehicle", "category": "emergency", "severity": 3, "ts": now,
                 "expires": now + 60000}, False, t + 0.2)
    s.set_crosswalks({0: 5})
    s.tick(t + 0.2, True)
    check("a riposo: emergenza remota accesa (LED blu veloce) e strisce in emergency",
          modes(f)[3] == 3 and modes(f)[0] == 0 and codes(f)[0] == 5, (modes(f), codes(f)))
    check("state(): stato di energia", s.state()["power"] == "sleep")
    s.set_power_state("active")
    s.set_presence({"person"}, t + 0.3)
    s.tick(t + 0.3, True)
    check("di nuovo sveglio: pericolo locale, presenze ed emergenza tornano visibili",
          modes(f) == [3, 1, 1, 3], modes(f))
    s.on_hazard({"id": "l1", "action": "cancel"}, True, t + 0.4)
    s.push(t + 0.4)
    check("di nuovo sveglio: il pericolo remoto non di emergenza torna visibile (lento)", modes(f)[0] == 2, modes(f))
    s.set_power_state("boh")
    check("stato di energia sconosciuto trattato come attivo", s.state()["power"] == "active")
    s3 = SignalController({"roles": ["status", "none", "none", "none"]}, f)
    s3.set_power_state("sleep")
    s3.tick(t, True)
    check("a riposo: anche il LED di stato e' spento", modes(f)[0] == 0)


def test_robustezza_segnali():
    s = SignalController({}, None)
    s.set_crosswalks({0: 1})
    s.tick(time.time(), True)
    check("senza MCU: nessuna eccezione, solo stato", s.state()["crosswalks"][0] == "on" and not s.state()["mcu"])

    class Rotto:
        def send_signals(self, *a):
            raise RuntimeError("bridge rotto")
    s2 = SignalController({}, Rotto())
    s2.tick(time.time(), True)
    check("MCU che solleva: errore contato, nodo vivo", s2.state()["send_errors"] == 1)
    s3 = SignalController({"enabled": False}, FakeMCU())
    s3.tick(time.time(), True)
    check("signals.enabled=false: nessun invio", s3.mcu.sent == 0)


def test_insieme_fail_safe():
    """Python + sketch finto: finche' il Python manda comandi decide lui, se si ferma riflesso e fail-safe."""
    clk = Orologio(50.0)
    f = FakeMCU({"curbs": 1}, clock=clk)
    s = SignalController({"ttl_ms": 3000}, f)
    s.set_crosswalks({0: 4})
    s.tick(time.time(), True)
    f.set_curb(0, present=True)
    check("Python vivo: canale 0 in assist (comandato)", f.output()["xwalk"][0] == 4)
    clk.t += 3.5     # il Python si e' fermato
    o = f.output()
    check("Python fermo: fail-safe con riflesso unknown sul bordo occupato", o["failsafe"] and o["xwalk"][0] == 6
          and o["leds"][3] == M.FAILSAFE_LED4, o)


def main():
    for fn in (test_codifica, test_fake_sensori, test_fake_isteresi, test_fake_riflesso_failsafe, test_resa_pwm,
               test_mcu_sincrono, test_mcu_sketch_v1, test_mcu_thread, test_led_come_prima, test_ruoli,
               test_strisce, test_keepalive, test_riposo, test_robustezza_segnali, test_insieme_fail_safe):
        print(f"\n== {fn.__name__}")
        esegui(fn)
    falliti = [r for r in RISULTATI if not r[1]]
    print(f"\n{len(RISULTATI) - len(falliti)}/{len(RISULTATI)} controlli OK")
    for nome, _, info in falliti:
        print(f"  FALLITO: {nome} -> {str(info)[:300]}")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
