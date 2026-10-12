"""Stati di energia del nodo e risvegli (CROSS v2, docs/ARCHITETTURA.md par. 2.2).

Il nodo resta A RIPOSO (camera e intelligenza artificiale ferme, LED spenti, radio in ascolto)
finche' un sensore non lo sveglia. Tre stati:
    sleep     a riposo: la camera non lavora, i fotogrammi vengono scartati
    active    sveglio: camera, regole, LED, avvisi agli utenti della strada
    cooldown  sveglio ma "in uscita": se non succede nulla per cooldown_s torna a riposo

Transizioni:
    sleep    --trigger------------------------------------>  active
    active   --nessuna attivita' per active_hold_s -------->  cooldown
    cooldown --cooldown_s senza attivita' ----------------->  sleep
    cooldown --trigger o attivita' ------------------------>  active
Un trigger con hold_s (es. 60 s per un mezzo di soccorso) tiene il nodo attivo almeno fino a
t + hold_s. Un trigger conta anche come attivita' (riparte il conteggio di active_hold_s), tranne
"boot" e "schedule" che tengono sveglio solo per il loro tempo (boot_active_s / sample_s).

Sorgenti di risveglio (source):
    curb             presenza sul bordo delle strisce (Modulino Distance letto dallo sketch)
    radio_vru        un CROSS Tag vicino con ruolo in wake_roles (detail = {"role": ...})
    radio_emergency  un mezzo di soccorso (beacon "emv")
    radio_hazard     un pericolo diretto qui da un incrocio vicino
    manual, schedule, boot, sensor_fault
Le sorgenti radio_* valgono solo con wake_on_radio = true (default): la radio e' comunque accesa.

Sicurezza prima del risparmio: trigger("sensor_fault") tiene il nodo SEMPRE attivo finche' il
sensore non torna (clear_fault(t)). Con power.enabled = false il nodo e' sempre attivo.

All'avvio il nodo e' attivo per boot_active_s (trigger "boot"), poi segue le regole. L'avvio
avviene alla prima chiamata con un tempo (tick, trigger, activity, stats): cosi' funziona sia con
l'orologio vero sia con il tempo simulato. Tutte le chiamate devono usare lo stesso orologio.

Stima dell'energia: secondi passati in ogni stato x consumo dello stato (power_w, watt), confrontati
con un nodo sempre acceso (tutto il tempo al consumo "active").
on_change(old, new, source, t) viene chiamato a ogni cambio di stato, fuori dal lock e sempre
nell'ordine in cui i cambi sono avvenuti, anche se trigger/tick arrivano da thread diversi (cosi'
main.py non puo' fermare la camera DOPO averla riavviata). Una callback lenta non blocca gli altri
thread: se un thread sta gia' consegnando, consegna anche i cambi degli altri. Un errore nella
callback viene loggato e ignorato.

Orologio rimesso (NTP o RTC dopo un avvio offline, salto di piu' di un giorno in avanti o di piu'
di 60 s indietro): il salto non viene contato come tempo in uno stato e tutte le scadenze (fine
dell'attivita', hold_s, risveglio programmato) si spostano con lui.
"""
import math
import threading
from collections import deque

STATI = ("sleep", "active", "cooldown")
SORGENTI = ("curb", "radio_vru", "radio_emergency", "radio_hazard", "manual", "schedule", "boot",
            "sensor_fault")
CODICI = {"sleep": "s", "active": "a", "cooldown": "c"}     # campo "pw" del heartbeat (par. 3.2)
NOMI = {"sleep": "riposo", "active": "attivo", "cooldown": "in uscita"}
POWER_DEFAULTS = {"enabled": True, "active_hold_s": 20.0, "cooldown_s": 5.0, "boot_active_s": 30.0,
                  "wake_on_radio": True, "wake_roles": ["blind", "emergency"],
                  "sample_every_s": 0.0, "sample_s": 10.0,
                  "power_w": {"sleep": 2.5, "active": 9.0, "cooldown": 9.0}}
SALTO_INDIETRO_S = 60.0       # orologio tornato indietro di piu' di cosi': lo considero rimesso
SALTO_AVANTI_S = 86400.0      # ...o andato avanti di piu' di un giorno tra due chiamate (tick ogni 0,25 s)
MAX_HOLD_S = 3600.0           # un solo trigger non tiene sveglio il nodo piu' di un'ora
MAX_EVENTI = 1000             # cambi di stato in attesa di on_change (callback bloccata)


def _tempo(t):
    """Istante in secondi: un numero finito, altrimenti ValueError (NaN o inf rovinerebbero i conti)."""
    v = float(t)
    if not math.isfinite(v):
        raise ValueError(f"tempo non valido: {t!r}")
    return v


def _num(x, default, lo=0.0, hi=None):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float(default)
    if v != v:  # NaN
        return float(default)
    v = max(lo, v)
    return min(hi, v) if hi is not None else v


def _role_of(detail):
    """Ruolo del Tag da detail ({"role": ...} o {"p": ...} come nel beacon), None se non c'e'."""
    if isinstance(detail, dict):
        r = detail.get("role", detail.get("p"))
        return str(r) if r is not None else None
    return None


class PowerManager:
    def __init__(self, cfg, on_change=None):
        cfg = cfg if isinstance(cfg, dict) else {}
        d = POWER_DEFAULTS
        self.enabled = bool(cfg.get("enabled", d["enabled"]))
        self.active_hold_s = _num(cfg.get("active_hold_s"), d["active_hold_s"])
        self.cooldown_s = _num(cfg.get("cooldown_s"), d["cooldown_s"])
        self.boot_active_s = _num(cfg.get("boot_active_s"), d["boot_active_s"])
        self.wake_on_radio = bool(cfg.get("wake_on_radio", d["wake_on_radio"]))
        roles = cfg.get("wake_roles", d["wake_roles"])
        self.wake_roles = [str(r) for r in roles] if isinstance(roles, (list, tuple)) else list(d["wake_roles"])
        self.sample_every_s = _num(cfg.get("sample_every_s"), d["sample_every_s"])
        self.sample_s = _num(cfg.get("sample_s"), d["sample_s"])
        pw = dict(d["power_w"])
        if isinstance(cfg.get("power_w"), dict):
            for k in STATI:
                if k in cfg["power_w"]:
                    pw[k] = _num(cfg["power_w"][k], d["power_w"][k])
        self.power_w = pw
        self.on_change = on_change

        self._lock = threading.Lock()
        self._state = "active"          # all'avvio il nodo e' sveglio (boot)
        self._started = False
        self._since = None              # istante di ingresso nello stato attuale
        self._last_t = None             # fin dove ho contato i secondi per stato
        self._seconds = {s: 0.0 for s in STATI}
        self._wakeups = 0               # passaggi a "active" da riposo (o dall'avvio)
        self._by_source = {}            # sorgente -> risvegli causati
        self._triggers = {}             # sorgente -> trigger accettati (anche a nodo gia' sveglio)
        self._last_activity = None      # ultima attivita' (o trigger) che tiene sveglio
        self._hold_until = float("-inf")
        self._fault = False
        self._fault_since = None
        self._next_sample = None
        self._warned = {}
        self._eventi = deque(maxlen=MAX_EVENTI)   # cambi di stato da notificare, in ordine
        self._cb_lock = threading.RLock()         # una sola consegna di on_change alla volta
        if not self.enabled:
            print("[energia] gestione energia disattivata (power.enabled = false): nodo sempre attivo")
        else:
            prog = f", risveglio programmato ogni {self.sample_every_s:g} s per {self.sample_s:g} s" \
                if self.sample_every_s > 0 else ""
            print(f"[energia] riposo dopo {self.active_hold_s:g} s senza attivita' + {self.cooldown_s:g} s "
                  f"in uscita; risvegli radio {'attivi' if self.wake_on_radio else 'disattivati'} "
                  f"(ruoli {', '.join(self.wake_roles) or 'nessuno'}){prog}")

    # ------------------------------------------------------------------ proprieta'

    @property
    def state(self):
        return self._state

    @property
    def awake(self):
        """active o cooldown: la camera lavora."""
        return self._state != "sleep"

    @property
    def fault(self):
        return self._fault

    @property
    def code(self):
        """Stato in una lettera per il heartbeat SC-P2P (s/a/c)."""
        return CODICI.get(self._state, "a")

    # ------------------------------------------------------------------ ingresso

    def trigger(self, source, t, hold_s=None, detail=None):
        """Risveglio (o prolungamento) da una sorgente. True se accettato (il nodo ora e' attivo),
        False se ignorato (sorgente radio disattivata o ruolo non in wake_roles).
        Una sorgente sconosciuta viene accettata come "manual" (con un avviso nel log)."""
        try:
            t = _tempo(t)
            source = str(source)
            with self._lock:
                self._start(t)
                self._advance(t)
                if source not in SORGENTI:
                    self._warn("src:" + source, f"[energia] sorgente di risveglio sconosciuta '{source}' "
                                                f"(valide: {', '.join(SORGENTI)}): accettata come manuale")
                    source = "manual"
                if source == "sensor_fault" and not self._fault:
                    # registrato anche con power.enabled = false: lo stato del sensore resta visibile
                    self._fault = True
                    self._fault_since = t
                    print("[energia] sensore di risveglio guasto: il nodo resta sveglio finche' non torna")
                if not self.enabled:
                    self._count(self._triggers, source)
                    return True
                if source.startswith("radio_") and not self.wake_on_radio:
                    return False
                if source == "radio_vru":
                    role = _role_of(detail)
                    if role is not None and role not in self.wake_roles:
                        return False
                self._count(self._triggers, source)
                if source == "boot":
                    self._hold_until = max(self._hold_until, t + self.boot_active_s)
                elif source == "schedule":
                    self._hold_until = max(self._hold_until, t + self.sample_s)
                else:
                    self._last_activity = t
                h = _num(hold_s, 0.0, hi=MAX_HOLD_S) if hold_s is not None else 0.0
                if h > 0:
                    self._hold_until = max(self._hold_until, t + h)
                if self._state != "active":
                    self._set("active", source, t)
            return True
        except Exception as e:  # un argomento strano non deve fermare il nodo
            self._warn("trigger", f"[energia] trigger ignorato ({e!r})")
            return False
        finally:
            self._notify()

    def activity(self, t):
        """Qualcosa di rilevante in scena (tracce confermate): resta sveglio. A riposo non fa nulla
        (la camera e' ferma: i fotogrammi che arrivano lo stesso vengono scartati dal nodo)."""
        try:
            t = _tempo(t)
            with self._lock:
                self._start(t)
                self._advance(t)
                if not self.enabled or self._state == "sleep":
                    return
                self._last_activity = t if self._last_activity is None else max(self._last_activity, t)
                if self._state == "cooldown":
                    self._set("active", "activity", t)
        except Exception as e:
            self._warn("activity", f"[energia] errore in activity: {e!r}")
        finally:
            self._notify()

    def clear_fault(self, t):
        """Il sensore di risveglio funziona di nuovo: si torna alle regole normali (resta sveglio
        ancora active_hold_s, poi cooldown e riposo se non succede nulla)."""
        try:
            t = _tempo(t)
            with self._lock:
                self._start(t)
                self._advance(t)
                if not self._fault:
                    return
                self._fault = False
                self._fault_since = None
                self._last_activity = t
            print("[energia] sensore di risveglio di nuovo funzionante: riposo di nuovo possibile")
        except Exception as e:
            self._warn("clear_fault", f"[energia] errore in clear_fault: {e!r}")

    def tick(self, t):
        """Transizioni a tempo: fine dell'attivita', fine del cooldown, risveglio programmato."""
        try:
            t = _tempo(t)
            with self._lock:
                self._start(t)
                self._advance(t)
                if not self.enabled:
                    return
                if self.sample_every_s > 0 and self._next_sample is not None and t >= self._next_sample:
                    # prossimo risveglio dopo t, senza ciclare un passo alla volta (dopo un lungo
                    # intervallo tra due tick sarebbero milioni di giri)
                    passi = math.floor((t - self._next_sample) / self.sample_every_s) + 1
                    self._next_sample += passi * self.sample_every_s
                    if self._next_sample <= t:   # arrotondamenti
                        self._next_sample = t + self.sample_every_s
                    self._hold_until = max(self._hold_until, t + self.sample_s)
                    self._count(self._triggers, "schedule")
                    if self._state != "active":
                        self._set("active", "schedule", t)
                for _ in range(3):  # al massimo active -> cooldown -> sleep nello stesso tick
                    if not self._step(t):
                        break
        except Exception as e:
            self._warn("tick", f"[energia] errore in tick: {e!r}")
        finally:
            self._notify()

    def _step(self, t):
        if self._fault:
            if self._state != "active":
                self._set("active", "sensor_fault", t)
                return True
            return False
        if self._state == "active":
            fine = self._hold_until
            if self._last_activity is not None:
                fine = max(fine, self._last_activity + self.active_hold_s)
            if t >= fine:
                self._set("cooldown", "idle", t)
                return True
        elif self._state == "cooldown":
            if t >= self._since + self.cooldown_s:
                self._set("sleep", "idle", t)
                return True
        return False

    # ------------------------------------------------------------------ stato

    def stats(self, t):
        """Riepilogo: stato, risvegli per sorgente, secondi per stato ed energia stimata."""
        try:
            t = _tempo(t)
        except (TypeError, ValueError):
            t = None
        with self._lock:
            if t is None:   # tempo non valido: riepilogo fino all'ultimo istante noto
                t = self._last_t
            if t is not None:
                self._start(t)
                self._advance(t)
            sec = dict(self._seconds)
            totale = sum(sec.values())
            energia = sum(sec[s] * self.power_w.get(s, 0.0) for s in STATI) / 3600.0
            sempre = totale * self.power_w.get("active", 0.0) / 3600.0
            risparmio = sempre - energia
            return {
                "state": self._state,
                "since_ms": int(round(self._since * 1000)) if self._since is not None else None,
                "wakeups": self._wakeups,
                "by_source": dict(self._by_source),
                "seconds": {s: round(v, 1) for s, v in sec.items()},
                "energy_wh": round(energia, 4),
                "always_on_wh": round(sempre, 4),
                "saved_wh": round(risparmio, 4),
                "saved_pct": round(100.0 * risparmio / sempre, 1) if sempre > 0 else 0.0,
                "enabled": self.enabled,
                "fault": self._fault,
                "triggers": dict(self._triggers),
            }

    # ------------------------------------------------------------------ interni

    def _start(self, t):
        if self._started:
            return
        self._started = True
        self._since = t
        self._last_t = t
        self._wakeups = 1
        self._count(self._by_source, "boot")
        self._count(self._triggers, "boot")
        if not self.enabled:
            return
        self._hold_until = t + self.boot_active_s
        if self.sample_every_s > 0:
            self._next_sample = t + self.sample_every_s
        print(f"[energia] avvio: attivo per {self.boot_active_s:g} s, poi a riposo se nessun sensore "
              f"lo sveglia")

    def _advance(self, t):
        """Conta il tempo passato nello stato attuale fino a t."""
        dt = t - self._last_t
        if 0 <= dt <= SALTO_AVANTI_S:
            self._seconds[self._state] += dt
            self._last_t = t
        elif dt > SALTO_AVANTI_S or dt < -SALTO_INDIETRO_S:
            # orologio rimesso: il salto non e' tempo passato in questo stato, e le scadenze si
            # spostano con lui (altrimenti un salto avanti chiuderebbe subito un hold_s, uno
            # indietro terrebbe sveglio il nodo per tutto il salto)
            self._rebase(dt)
            self._last_t = t
            self._warn(f"salto:{'+' if dt > 0 else '-'}", f"[energia] orologio spostato di {dt:+.0f} s: "
                                                         f"tempi e scadenze riallineati", max_volte=3)
        # piccoli passi indietro (thread con istanti appena diversi): ignorati

    def _rebase(self, delta):
        if self._since is not None:
            self._since += delta
        if math.isfinite(self._hold_until):
            self._hold_until += delta
        if self._last_activity is not None:
            self._last_activity += delta
        if self._next_sample is not None:
            self._next_sample += delta
        if self._fault_since is not None:
            self._fault_since += delta

    def _set(self, nuovo, source, t):
        """Cambio di stato (sotto il lock): on_change parte dopo, da _notify."""
        vecchio = self._state
        if nuovo == vecchio:
            return
        self._state = nuovo
        self._since = t
        if nuovo == "active" and vecchio == "sleep":
            self._wakeups += 1
            self._count(self._by_source, source)
        if nuovo == "cooldown":
            # chi entra in cooldown non ha piu' tempi da rispettare: un nuovo trigger li rimette
            self._hold_until = float("-inf")
        self._eventi.append((vecchio, nuovo, source, t))
        print(f"[energia] {NOMI.get(vecchio, vecchio)} -> {NOMI.get(nuovo, nuovo)} ({source})")

    def _notify(self):
        """Consegna i cambi di stato a on_change fuori dal lock, uno alla volta e nell'ordine in cui
        sono avvenuti. Se un altro thread sta gia' consegnando non aspetto: li consegna lui (ricontrolla
        la coda prima di uscire). Lo stesso thread puo' rientrare (callback che chiama trigger)."""
        while True:
            if not self._cb_lock.acquire(blocking=False):
                return
            try:
                while True:
                    with self._lock:
                        if not self._eventi:
                            break
                        ev = self._eventi.popleft()
                    if self.on_change is None:
                        continue
                    try:
                        self.on_change(*ev)
                    except Exception as e:  # la callback (avvio/arresto camera) non deve fermare il nodo
                        self._warn("on_change", f"[energia] errore nella callback on_change: {e!r}")
            finally:
                self._cb_lock.release()
            with self._lock:   # un altro thread puo' aver aggiunto un cambio mentre rilasciavo
                if not self._eventi:
                    return

    @staticmethod
    def _count(d, key):
        d[key] = d.get(key, 0) + 1

    def _warn(self, chiave, testo, max_volte=1):
        n = self._warned.get(chiave, 0)
        if n < max_volte:
            self._warned[chiave] = n + 1
            print(testo)
