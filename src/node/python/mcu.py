"""Dialogo con lo sketch dell'MCU (STM32U585 della UNO Q) via Bridge: sensori di bordo e luci.

Funzioni esposte dallo sketch (src/node/sketch/sketch.ino, Bridge.provide) - docs/ARCHITETTURA.md par. 2.3:
    set_signals(int leds, int xwalk, int ttl_ms) -> 0    comando unico: 4 LED + fino a 4 canali luce
    get_sensors()                                -> int  bit 0-3 presenza sul bordo i, bit 4-7 sensore i
                                                         funzionante, bit 8 sketch pronto
    get_distance(int i)                          -> int  distanza del sensore i in mm, -1 se niente/guasto
  leds:  2 bit per LED (LED1 bit 0-1 ... LED4 bit 6-7): 0 spento, 1 acceso, 2 lento 1 Hz, 3 veloce 4 Hz
  xwalk: 4 bit per canale (canale 0 bit 0-3 ... canale 3 bit 12-15), codici luce LIGHT_CODES
Lo sketch:
  - riflesso locale: se il sensore i vede una presenza e il canale i non e' comandato dal Python
    (comando scaduto) accende subito il canale i in "unknown" (6), senza aspettare la camera;
  - fail-safe: se per ttl_ms non arriva nessun set_signals, LED di pericolo spenti, LED4 lampo breve
    ogni 2 s, canali luce guidati solo dai sensori (riflesso locale);
  - sensore: Modulino Distance letto a 10 Hz, presenza = distanza < 900 mm per 3 letture di fila,
    assenza dopo 10 letture sopra soglia.

Bridge.call e' bloccante (fino a 10 s): MCULink fa TUTTE le chiamate in un thread a parte.
  - send_signals mette il comando in una casella "solo l'ultimo valore" e torna subito;
  - lo stesso thread legge get_sensors ogni sensor_poll_s e get_distance ogni distance_poll_s;
    poll_sensors() e distance() restituiscono l'ultimo valore letto (None se troppo vecchio).
  Con start_thread=False (test, replay) le chiamate sono sincrone.
Se lo sketch caricato e' ancora quello della v1 (solo set_leds), i LED funzionano lo stesso
(set_leds) e lo si dice una volta nel log: luci delle strisce e sensori restano non disponibili.

Senza Bridge (PC, test, simulatore) si usa FakeMCU: stessa interfaccia, sensori impostabili a mano e
la stessa logica dello sketch (isteresi, riflesso locale, fail-safe) per verificarla nei test.
"""
import threading
import time
from collections import deque

CALL_TIMEOUT_S = 2.0       # oltre ttl_ms l'MCU e' comunque gia' in fail-safe
STALE_MIN_S = 2.0          # lettura dei sensori piu' vecchia di cosi' (e di 4 poll): "non so" (None)
LEGACY_RETRY_S = 60.0      # con lo sketch v1 riprovo set_signals ogni tanto (sketch aggiornato?)
ERR_BACKOFF_MAX_S = 30.0   # sensori che non rispondono: riprovo sempre piu' di rado, fino a 30 s
MAX_CURBS = 4              # sensori di bordo (bit 0-3 / 4-7 di get_sensors)
MAX_CHANNELS = 4           # canali luce dell'attraversamento (D6, D9, D10, D11)

# --- costanti dello sketch (tenere allineate a sketch.ino) ---
PRESENCE_MM = 900          # presenza: distanza sotto questa soglia...
PRESENCE_ON_READS = 3      # ...per 3 letture di fila (10 Hz)
PRESENCE_OFF_READS = 10    # assenza dopo 10 letture sopra soglia
OFFLINE_PERIOD_MS = 2000   # fail-safe: lampo del LED4 ogni 2 s...
OFFLINE_FLASH_MS = 150     # ...lungo 150 ms
MAX_TTL_MS = 60000         # un comando non vale mai piu' di 60 s: il fail-safe scatta comunque
SENSORS_MASK = 0x1FF       # get_sensors usa solo i bit 0-8: un valore fuori da qui non e' dello sketch v2
FAILSAFE_LED4 = 4          # modo "lampo breve ogni 2 s" del LED4 in fail-safe (solo in FakeMCU.output)

LIGHT_CODES = {0: "off", 1: "on", 2: "attention", 3: "danger", 4: "assist", 5: "emergency", 6: "unknown"}
LIGHT_BY_NAME = {v: k for k, v in LIGHT_CODES.items()}
LIGHT_UNKNOWN = 6
LED_MODES = {0: "spento", 1: "acceso", 2: "lento", 3: "veloce"}


def _num(x, default, lo=None, hi=None):
    try:
        v = float(x)
    except (TypeError, ValueError):
        v = float(default)
    if v != v:
        v = float(default)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


# ---------------------------------------------------------------------- codifica (come lo sketch)

def light_code(c):
    """Codice luce valido 0..6 da un intero o da un nome; un valore sconosciuto diventa "unknown" (6):
    nel dubbio la luce chiede prudenza, non si spegne."""
    if isinstance(c, str):
        if c in LIGHT_BY_NAME:
            return LIGHT_BY_NAME[c]
        try:
            c = int(c)
        except ValueError:
            return LIGHT_UNKNOWN
    try:
        n = int(c)
        if n != c:          # 2.5 non e' un codice: non lo tronco a 2 ("attention"), e' sconosciuto
            return LIGHT_UNKNOWN
    except (TypeError, ValueError, OverflowError):
        return LIGHT_UNKNOWN
    return n if n in LIGHT_CODES else LIGHT_UNKNOWN


def pack_leds(modes):
    """[m1, m2, m3, m4] -> intero, 2 bit per LED (LED1 bit 0-1)."""
    out = 0
    for i, m in enumerate(list(modes)[:4]):
        out |= (int(m) & 0x3) << (2 * i)
    return out


def unpack_leds(packed):
    return [(int(packed) >> (2 * i)) & 0x3 for i in range(4)]


def pack_xwalk(codes):
    """{canale: codice} (o lista) -> intero, 4 bit per canale (canale 0 bit 0-3)."""
    items = codes.items() if isinstance(codes, dict) else enumerate(codes or ())
    out = 0
    for ch, c in items:
        ch = int(ch)
        if 0 <= ch < MAX_CHANNELS:
            out |= (light_code(c) & 0xF) << (4 * ch)
    return out


def unpack_xwalk(xwalk):
    """Intero -> lista di 4 codici (valori 7..15 trattati come lo sketch: "unknown")."""
    return [light_code((int(xwalk) >> (4 * ch)) & 0xF) for ch in range(MAX_CHANNELS)]


def parse_sensors(word, curbs=1):
    """Ritorno di get_sensors -> {"ready", "curbs": [{"id", "present", "ok"}], "raw"}.
    La presenza conta solo se il sensore funziona. Un valore fuori formato (negativo, oltre il bit 8,
    non intero) solleva ValueError: meglio "non so" che sensori "funzionanti" inventati."""
    if isinstance(word, bool) or not isinstance(word, int):
        raise ValueError(f"get_sensors: valore non intero {word!r}")
    if not 0 <= word <= SENSORS_MASK:
        raise ValueError(f"get_sensors: valore fuori formato {word!r} (attesi i bit 0-8)")
    out = []
    for i in range(max(0, min(MAX_CURBS, int(curbs)))):
        ok = bool(word >> (4 + i) & 1)
        out.append({"id": i, "present": ok and bool(word >> i & 1), "ok": ok})
    return {"ready": bool(word >> 8 & 1), "curbs": out, "raw": word}


def led_on(mode, now_ms):
    """Livello di un LED di stato per un modo, come mode_level() dello sketch."""
    now_ms = int(now_ms)
    if mode == 1:
        return True
    if mode == 2:
        return now_ms % 1000 < 500
    if mode == 3:
        return now_ms % 250 < 125
    return False


def light_duty(code, now_ms):
    """Duty PWM 0..255 di un canale luce per un codice, come light_duty() dello sketch."""
    now_ms = int(now_ms)
    code = light_code(code)
    if code == 1:
        return 255
    if code == 2:
        return 255 if now_ms % 1000 < 500 else 0
    if code == 3:
        return 255 if now_ms % 250 < 125 else 0
    if code == 4:   # respiro: 20 % -> 100 % -> 20 % in 2 s
        fase = now_ms % 2000
        tri = fase if fase < 1000 else 2000 - fase
        return 51 + (204 * tri) // 1000
    if code == 5:   # doppio lampo ogni secondo
        fase = now_ms % 1000
        return 255 if fase < 100 or 200 <= fase < 300 else 0
    if code == 6:   # lampeggio lento con duty 25 %
        return 255 if now_ms % 1000 < 250 else 0
    return 0


# ---------------------------------------------------------------------- MCU vero (Bridge)

class MCULink:
    def __init__(self, bridge, cfg, start_thread=True):
        cfg = cfg if isinstance(cfg, dict) else {}
        self.bridge = bridge
        self.enabled = bool(cfg.get("enabled", True))
        self.curbs = int(_num(cfg.get("curbs"), 1, 0, MAX_CURBS))
        self.sensor_poll_s = _num(cfg.get("sensor_poll_s"), 0.25, 0.05, 10.0)
        self.distance_poll_s = _num(cfg.get("distance_poll_s"), 2.0, 0.0, 3600.0)   # 0 = mai
        self.call_timeout_s = _num(cfg.get("call_timeout_s"), CALL_TIMEOUT_S, 0.2, 10.0)
        self.stale_s = max(STALE_MIN_S, 4 * self.sensor_poll_s)

        self._lock = threading.Lock()
        self._sensors = None            # ultima lettura valida di get_sensors (gia' decodificata)
        self._sensors_at = None         # time.monotonic() di quella lettura
        self._distances = {}            # i -> (mm o None, monotonic)
        self._last_signals = None       # ultimo comando chiesto (leds, xwalk, ttl_ms)
        self._last_send = None
        self._sent = self._send_errors = 0
        self._reads = self._read_errors = 0
        self._send_ok = None            # esito dell'ultimo invio (None = mai provato)
        self._read_ok = None
        self._legacy = False            # sketch v1: solo set_leds
        self._legacy_tried = None       # monotonic dell'ultimo tentativo di capire se e' la v1
        self._use_timeout = True
        self._warned = {}
        self._last_err_print = {}

        self._cond = threading.Condition()
        self._slot = None               # "solo l'ultimo valore": il worker manda sempre il piu' recente
        self._closing = False
        self._thread = None
        if self.enabled and bridge is not None and start_thread:
            self._thread = threading.Thread(target=self._worker, name="mcu-bridge", daemon=True)
            self._thread.start()
        if not self.enabled:
            print("[mcu] disattivato (mcu.enabled = false): niente sensori di bordo, niente luci")
        elif bridge is None:
            print("[mcu] nessun Bridge: sensori di bordo non disponibili, segnali solo nel log")
        else:
            print(f"[mcu] Bridge verso lo sketch: {self.curbs} sensori di bordo, "
                  f"lettura ogni {self.sensor_poll_s:g} s")

    # ------------------------------------------------------------------ interfaccia

    def poll_sensors(self):
        """{"ready": bool, "curbs": [{"id", "present", "ok"}], "raw": int} oppure None se non so
        (niente Bridge, sketch che non risponde, lettura troppo vecchia)."""
        if not self.enabled or self.bridge is None:
            return None
        if self._thread is None and not self._read_sensors():   # sincrono (test/replay)
            return None
        with self._lock:
            if self._sensors is None or self._sensors_at is None:
                return None
            if self._thread is not None and time.monotonic() - self._sensors_at > self.stale_s:
                return None
            s = self._sensors
            return {"ready": s["ready"], "curbs": [dict(c) for c in s["curbs"]], "raw": s["raw"]}

    def distance(self, i):
        """Ultima distanza misurata dal sensore i in mm, None se niente, guasto o non so."""
        try:
            i = int(i)
        except (TypeError, ValueError):
            return None
        if not self.enabled or self.bridge is None or not 0 <= i < MAX_CURBS:
            return None
        if self._thread is None and not self._read_distance(i):
            return None
        with self._lock:
            v = self._distances.get(i)
            if v is None:
                return None
            mm, at = v
            if self._thread is not None and time.monotonic() - at > max(self.stale_s, 3 * self.distance_poll_s):
                return None
            return mm

    def send_signals(self, leds, xwalk, ttl_ms):
        """Comando unico verso lo sketch. Mai bloccante: se il Bridge e' lento vince l'ultimo valore.
        ttl_ms viene limitato a 1..MAX_TTL_MS (anche lo sketch lo limita)."""
        try:
            cmd = (int(leds) & 0xFF, int(xwalk) & 0xFFFF, min(MAX_TTL_MS, max(1, int(ttl_ms))))
        except (TypeError, ValueError, OverflowError) as e:
            self._warn("args", f"[mcu] send_signals: argomenti non validi ({e!r})")
            return
        with self._lock:
            self._last_signals = cmd
        if not self.enabled or self.bridge is None:
            return
        if self._thread is None:
            self._send(cmd)
            return
        with self._cond:
            self._slot = cmd
            self._cond.notify()

    def state(self):
        now = time.monotonic()
        with self._lock:
            s = self._sensors
            last = self._last_signals
            return {
                "enabled": self.enabled,
                "bridge": self.bridge is not None,
                "thread": self._thread is not None,
                "curbs": self.curbs,
                "ready": s["ready"] if s else None,
                "sensors": {"ready": s["ready"], "curbs": [dict(c) for c in s["curbs"]], "raw": s["raw"]} if s else None,
                "sensors_age_s": round(now - self._sensors_at, 2) if self._sensors_at is not None else None,
                "distances": {i: v[0] for i, v in sorted(self._distances.items())},
                "legacy_sketch": self._legacy,
                "last_signals": {"leds": last[0], "xwalk": last[1], "ttl_ms": last[2]} if last else None,
                "last_send": dict(self._last_send) if self._last_send else None,
                "sent": self._sent,
                "send_errors": self._send_errors,
                "sensor_reads": self._reads,
                "sensor_errors": self._read_errors,
            }

    def close(self):
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    # ------------------------------------------------------------------ thread del Bridge

    def _worker(self):
        prossimo_sensori = 0.0
        prossima_dist = 0.0
        attesa_err = self.sensor_poll_s
        while True:
            with self._cond:
                while not self._closing and self._slot is None:
                    adesso = time.monotonic()
                    scadenza = prossimo_sensori
                    if self.distance_poll_s > 0 and self.curbs > 0:
                        scadenza = min(scadenza, prossima_dist)
                    if adesso >= scadenza:
                        break
                    self._cond.wait(min(scadenza - adesso, 1.0))
                if self._closing:
                    return
                cmd, self._slot = self._slot, None
            try:
                if cmd is not None:
                    self._send(cmd)  # prima i comandi, poi i sensori se e' il momento
                adesso = time.monotonic()
                # get_sensors anche con curbs = 0: costa poco e dice se lo sketch v2 e' pronto (bit 8)
                if adesso >= prossimo_sensori:
                    if self._read_sensors():
                        attesa_err = self.sensor_poll_s
                        prossimo_sensori = time.monotonic() + self.sensor_poll_s
                    else:  # sketch che non risponde: riprovo sempre piu' di rado (non intaso il Bridge)
                        attesa_err = min(ERR_BACKOFF_MAX_S, max(1.0, attesa_err * 2))
                        prossimo_sensori = time.monotonic() + attesa_err
                if self.distance_poll_s > 0 and self.curbs > 0 and adesso >= prossima_dist:
                    if self._read_ok:
                        for i in range(self.curbs):
                            self._read_distance(i)
                    prossima_dist = time.monotonic() + max(self.distance_poll_s, attesa_err)
            except Exception as e:  # il thread del Bridge non deve morire mai
                self._warn("worker", f"[mcu] errore nel thread del Bridge: {e!r}")
                time.sleep(0.5)

    def _read_sensors(self):
        try:
            word = self._call("get_sensors")
            parsed = parse_sensors(word, self.curbs)   # fuori formato -> ValueError -> "non so"
        except Exception as e:  # sketch v1 (senza get_sensors), router assente, timeout...
            with self._lock:
                prima, self._read_ok = self._read_ok, False
                self._read_errors += 1
            self._report_error("sensori", prima, f"[mcu] lettura dei sensori fallita: {e!r} (lo sketch "
                                                 f"src/node/sketch/sketch.ino v2 e' caricato?)")
            return False
        with self._lock:
            prima, self._read_ok = self._read_ok, True
            self._sensors = parsed
            self._sensors_at = time.monotonic()
            self._reads += 1
        if prima is not True:
            stato = ", ".join(f"bordo {c['id']}: {'ok' if c['ok'] else 'GUASTO'}" for c in parsed["curbs"])
            print(f"[mcu] sensori raggiunti (sketch {'pronto' if parsed['ready'] else 'non ancora pronto'}"
                  f"{'; ' + stato if stato else ''})")
        return True

    def _read_distance(self, i):
        try:
            mm = int(self._call("get_distance", int(i)))
        except Exception as e:
            self._warn("distanza", f"[mcu] lettura della distanza fallita: {e!r}")
            with self._lock:
                self._distances.pop(i, None)
            return False
        with self._lock:
            self._distances[i] = (mm if mm >= 0 else None, time.monotonic())
        return True

    def _send(self, cmd):
        leds, xwalk, ttl_ms = cmd
        t0 = time.monotonic()
        try:
            modo = self._send_cmd(leds, xwalk, ttl_ms, t0)
            ok, err = True, None
        except Exception as e:  # router assente, sketch non caricato, timeout...
            modo, ok, err = "set_leds" if self._legacy else "set_signals", False, repr(e)
        ms = round((time.monotonic() - t0) * 1000, 1)
        with self._lock:
            prima, self._send_ok = self._send_ok, ok
            self._last_send = {"ok": ok, "call": modo, "leds": leds, "xwalk": xwalk, "ttl_ms": ttl_ms,
                               "error": err, "ms": ms, "ts": int(time.time() * 1000)}
            if ok:
                self._sent += 1
            else:
                self._send_errors += 1
        if ok and prima is not True:
            print(f"[mcu] MCU raggiunto: {modo} OK" if prima is None else "[mcu] MCU di nuovo raggiungibile")
        elif not ok:
            self._report_error("invio", prima, f"[mcu] invio all'MCU fallito: {err} (lo sketch "
                                               f"src/node/sketch/sketch.ino e' caricato? riprovo; intanto "
                                               f"l'MCU va in fail-safe)")

    def _send_cmd(self, leds, xwalk, ttl_ms, t0):
        """set_signals; se lo sketch e' quello della v1 (solo set_leds) ripiega sui soli LED.
        Ritorna il nome della funzione usata."""
        if self._legacy:
            if t0 - self._legacy_tried >= LEGACY_RETRY_S:   # ogni tanto riprovo: sketch aggiornato?
                self._legacy_tried = t0
                try:
                    self._call("set_signals", leds, xwalk, ttl_ms)
                    self._legacy = False
                    print("[mcu] lo sketch ora risponde a set_signals: luci delle strisce attive")
                    return "set_signals"
                except Exception:
                    pass
            self._call("set_leds", leds, ttl_ms)
            return "set_leds"
        try:
            self._call("set_signals", leds, xwalk, ttl_ms)
            return "set_signals"
        except Exception:
            # sketch v1? provo set_leds, al massimo una volta ogni LEGACY_RETRY_S (se anche set_leds
            # fallisce l'errore vero e' quello di set_signals: Bridge giu', timeout...)
            if self._legacy_tried is not None and t0 - self._legacy_tried < LEGACY_RETRY_S:
                raise
            self._legacy_tried = t0
            try:
                self._call("set_leds", leds, ttl_ms)
            except Exception:
                pass
            else:
                self._legacy = True
                print("[mcu] lo sketch caricato e' quello della v1 (solo set_leds): LED ok, ma luci delle "
                      "strisce e sensori di bordo non disponibili. Caricare src/node/sketch/sketch.ino")
                return "set_leds"
            raise

    def _call(self, name, *args):
        if self._use_timeout:
            try:
                return self.bridge.call(name, *args, timeout=self.call_timeout_s)
            except TypeError as e:
                if "timeout" not in str(e):
                    raise
                self._use_timeout = False  # Bridge senza parametro timeout (es. finto nei test)
        return self.bridge.call(name, *args)

    def _report_error(self, chiave, prima, testo):
        """Log al primo errore dopo un successo (o al primo in assoluto), poi al massimo ogni 60 s."""
        adesso = time.monotonic()
        if prima is not False or adesso - self._last_err_print.get(chiave, -1e9) > 60:
            self._last_err_print[chiave] = adesso
            print(testo)

    def _warn(self, chiave, testo, max_volte=3):
        n = self._warned.get(chiave, 0)
        if n < max_volte:
            self._warned[chiave] = n + 1
            print(testo)


# ---------------------------------------------------------------------- MCU finto

class FakeMCU:
    """MCU finto per PC, test e simulatore: stessa interfaccia di MCULink.

    Sensori impostabili per ogni bordo (set_curb, set_fault, reading per l'isteresi a 10 Hz);
    registra l'ultimo set_signals; output() dice cosa mostrerebbe lo sketch adesso, con il
    riflesso locale e il fail-safe calcolati come in sketch.ino.
    clock: funzione che restituisce i secondi (default time.monotonic, come il keep-alive di
    SignalController); nei test si puo' passare un orologio finto.
    ready = False: sketch appena partito, come quello vero get_sensors vale 0 e non c'e' riflesso.
    offline = True: Bridge giu' (poll_sensors/distance None, i comandi non arrivano).
    """

    def __init__(self, cfg=None, clock=None, curbs=None):
        if isinstance(cfg, int) and not isinstance(cfg, bool) and curbs is None:
            cfg, curbs = None, cfg
        cfg = cfg if isinstance(cfg, dict) else {}
        n = cfg.get("curbs", 1) if curbs is None else curbs
        self.curbs = int(_num(n, 1, 0, MAX_CURBS))
        self.clock = clock or time.monotonic
        self.ready = True
        self.offline = False                      # True = Bridge giu': nessuna chiamata arriva
        self._lock = threading.Lock()
        self._ok = [i < self.curbs for i in range(MAX_CURBS)]
        self._present = [False] * MAX_CURBS
        self._mm = [-1] * MAX_CURBS
        self._near = [0] * MAX_CURBS               # isteresi come lo sketch (reading)
        self._far = [0] * MAX_CURBS
        self.last_signals = None                  # {"leds", "xwalk", "ttl_ms", "at"}
        self.calls = deque(maxlen=1000)           # (leds, xwalk, ttl_ms) in ordine di arrivo
        self.sent = 0

    # ------------------------------------------------------------------ sensori (lato prova)

    def set_curb(self, i, present=None, distance_mm=None, ok=None):
        """Imposta direttamente il bordo i. Con solo distance_mm la presenza e' distanza < 900 mm
        (senza isteresi: per l'isteresi usare reading)."""
        i = int(i)
        if not 0 <= i < MAX_CURBS:
            return
        with self._lock:
            if ok is not None:
                self._ok[i] = bool(ok)
            if distance_mm is not None:
                mm = int(distance_mm)
                self._mm[i] = mm if mm >= 0 else -1
                if present is None:
                    present = 0 <= mm < PRESENCE_MM
            if present is not None:
                self._present[i] = bool(present)
                if present and self._mm[i] < 0:
                    self._mm[i] = PRESENCE_MM // 2
                if not present and 0 <= self._mm[i] < PRESENCE_MM:
                    self._mm[i] = -1
            if not self._ok[i]:
                self._present[i], self._mm[i] = False, -1
                self._near[i] = self._far[i] = 0

    def set_presence(self, i, present=True):
        self.set_curb(i, present=present)

    def set_distance(self, i, mm):
        self.set_curb(i, distance_mm=-1 if mm is None else mm)

    def set_fault(self, i, fault=True):
        """Sensore i guasto (o di nuovo funzionante con fault=False)."""
        self.set_curb(i, ok=not fault)

    def reading(self, i, mm):
        """Una lettura del sensore i (10 Hz) con l'isteresi dello sketch: presenza dopo 3 letture
        sotto PRESENCE_MM, assenza dopo 10 sopra (None o -1 = niente davanti)."""
        i = int(i)
        if not 0 <= i < MAX_CURBS:
            return False
        with self._lock:
            if not self._ok[i]:
                return False
            mm = -1 if mm is None else int(mm)
            self._mm[i] = mm if mm >= 0 else -1
            if 0 <= mm < PRESENCE_MM:
                self._far[i] = 0
                self._near[i] = min(255, self._near[i] + 1)
                if self._near[i] >= PRESENCE_ON_READS:
                    self._present[i] = True
            else:
                self._near[i] = 0
                self._far[i] = min(255, self._far[i] + 1)
                if self._far[i] >= PRESENCE_OFF_READS:
                    self._present[i] = False
            return self._present[i]

    def sensors_word(self):
        """Lo stesso intero che restituirebbe get_sensors dello sketch."""
        with self._lock:
            return self._word_unlocked()

    # ------------------------------------------------------------------ interfaccia di MCULink

    def poll_sensors(self):
        if self.offline:
            return None
        return parse_sensors(self.sensors_word(), self.curbs)

    def distance(self, i):
        try:
            i = int(i)
        except (TypeError, ValueError):
            return None
        if self.offline or not 0 <= i < self.curbs:
            return None
        with self._lock:
            mm = self._mm[i]
            return mm if self._ok[i] and mm >= 0 else None

    def send_signals(self, leds, xwalk, ttl_ms):
        if self.offline:
            return
        try:
            cmd = (int(leds) & 0xFF, int(xwalk) & 0xFFFF, min(MAX_TTL_MS, max(1, int(ttl_ms))))
        except (TypeError, ValueError, OverflowError):
            return
        with self._lock:
            self.last_signals = {"leds": cmd[0], "xwalk": cmd[1], "ttl_ms": cmd[2], "at": float(self.clock())}
            self.calls.append(cmd)
            self.sent += 1

    def state(self):
        out = self.output()
        with self._lock:
            last = dict(self.last_signals) if self.last_signals else None
            return {"enabled": True, "bridge": False, "fake": True, "curbs": self.curbs, "ready": self.ready,
                    "offline": self.offline,
                    "sensors": parse_sensors(self._word_unlocked(), self.curbs),
                    "distances": {i: (self._mm[i] if self._ok[i] and self._mm[i] >= 0 else None)
                                  for i in range(self.curbs)},
                    "last_signals": last, "sent": self.sent, "output": out}

    def close(self):
        pass

    # ------------------------------------------------------------------ cosa mostra lo sketch

    def alive(self, now=None):
        """Il comando del Python e' ancora valido (non scaduto)?"""
        now = float(self.clock()) if now is None else float(now)
        with self._lock:
            last = self.last_signals
            return bool(last) and 0 <= (now - last["at"]) * 1000 < last["ttl_ms"]

    def output(self, now=None):
        """Stato delle uscite come lo calcola loop() dello sketch:
        {"alive", "failsafe", "leds": [4 modi, 4 = lampo del fail-safe], "xwalk": [4 codici],
         "names": [4 nomi], "reflex": [4 bool]}."""
        now = float(self.clock()) if now is None else float(now)
        alive = self.alive(now)
        with self._lock:
            last = self.last_signals
            present = [self.ready and self._present[i] and self._ok[i] and i < self.curbs for i in range(MAX_CURBS)]
        if alive:
            leds = unpack_leds(last["leds"])
            xwalk = unpack_xwalk(last["xwalk"])
            reflex = [False] * MAX_CHANNELS
        else:
            leds = [0, 0, 0, FAILSAFE_LED4]
            reflex = [present[ch] for ch in range(MAX_CHANNELS)]
            xwalk = [LIGHT_UNKNOWN if reflex[ch] else 0 for ch in range(MAX_CHANNELS)]
        return {"alive": alive, "failsafe": not alive, "leds": leds, "xwalk": xwalk,
                "names": [LIGHT_CODES[c] for c in xwalk], "reflex": reflex}

    def duty(self, channel, now=None):
        """Duty PWM 0..255 del canale luce adesso (come lo sketch)."""
        now = float(self.clock()) if now is None else float(now)
        return light_duty(self.output(now)["xwalk"][int(channel)], int(now * 1000))

    def led_level(self, i, now=None):
        """Il LED i (0..3) e' acceso adesso? (come lo sketch, compreso il lampo del fail-safe)."""
        now = float(self.clock()) if now is None else float(now)
        mode = self.output(now)["leds"][int(i)]
        ms = int(now * 1000)
        if mode == FAILSAFE_LED4:
            return ms % OFFLINE_PERIOD_MS < OFFLINE_FLASH_MS
        return led_on(mode, ms)

    def _word_unlocked(self):
        if not self.ready:   # come lo sketch: finche' loop() non ha letto i sensori get_sensors vale 0
            return 0
        w = 0
        for i in range(MAX_CURBS):
            if i < self.curbs and self._ok[i]:
                w |= 1 << (4 + i)
                if self._present[i]:
                    w |= 1 << i
        return w | 1 << 8
