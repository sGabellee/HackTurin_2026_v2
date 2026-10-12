"""Segnali dell'incrocio: 4 LED di stato + luci sopra l'attraversamento (CROSS v2, ARCHITETTURA.md par. 2.4).

Sostituisce leds.py mantenendone il comportamento sui 4 LED e aggiunge i canali luce delle strisce.
Tutto parte verso lo sketch con UN solo comando: mcu.send_signals(leds, xwalk, ttl_ms) (mcu.py).

4 LED di stato (D2..D5). Ogni LED ha un ruolo (config signals.roles) e un modo: 0 spento, 1 acceso,
2 lampeggio lento (1 Hz), 3 lampeggio veloce (4 Hz). Ruoli di default, come sono cablati da noi:
    hazard    (rosso)  pericolo: visto qui (acceso / veloce se grave) o da un vicino (lento)
    cyclist   (verde)  acceso finche' la camera vede un ciclista
    person    (giallo) acceso finche' la camera vede una persona
    emergency (blu)    lampeggio veloce: mezzo di emergenza visto qui o segnalato da un vicino
Altri ruoli: local, remote, status, any, vehicle, none.
Un pericolo visto QUI resta acceso solo finche' la camera vede ancora l'oggetto che lo causa (la sua
traccia), al massimo hold_s; un pericolo arrivato da un VICINO resta acceso fino al suo cancel o alla
sua scadenza. I 4 modi viaggiano in un intero: 2 bit per LED (LED1 = bit 0-1).

Canali luce dell'attraversamento (D6, D9, D10, D11): il codice di ogni canale lo decide crosswalk.py
(set_crosswalks), 4 bit per canale (canale 0 = bit 0-3). Codici (mcu.LIGHT_CODES):
    0 off, 1 on, 2 attention (1 Hz), 3 danger (4 Hz), 4 assist (respiro), 5 emergency (doppio lampo),
    6 unknown (lampeggio lento duty 25 %). Un codice sconosciuto diventa "unknown": prudenza.

Energia (set_power_state): a riposo ("sleep") i LED di presenza, di pericolo locale, di pericolo
remoto non di emergenza e di stato sono spenti; restano accesi i pericoli remoti di categoria
emergency (LED blu) e le luci delle strisce comandate da crosswalk.py.

Tempo reale: il nodo chiama push() appena cambia qualcosa (presenze a ogni frame, pericoli all'arrivo);
il keep-alive verso l'MCU resta in tick(): se lo sketch non riceve comandi per ttl_ms va in fail-safe
(LED spenti tranne un lampo del LED4, strisce guidate solo dal sensore), per questo il comando viene
rimandato ogni refresh_s anche quando non cambia. mcu.send_signals non blocca mai (vedi mcu.py).
"""
import threading
import time

from mcu import LIGHT_BY_NAME, LIGHT_CODES, MAX_CHANNELS, light_code, pack_leds, pack_xwalk

RUOLI = ("person", "cyclist", "vehicle", "hazard", "emergency", "local", "remote", "status", "any", "none")
RUOLI_DEFAULT = ["hazard", "cyclist", "person", "emergency"]
STATI_ENERGIA = ("sleep", "active", "cooldown")
PRESENZA_HOLD_S = 0.5             # una classe resta "presente" ancora 0,5 s: niente sfarfallio
NOMI_MODI = {0: "spento", 1: "acceso", 2: "lento", 3: "veloce"}
LOCAL_MODE = {3: 3, 2: 1, 1: 2}   # severity del pericolo locale -> modo
MAX_REMOTE_S = 600                # un pericolo remoto non tiene acceso un LED piu' di cosi'
MAX_ATTIVI = 200                  # per lato: protezione da raffiche di messaggi


def _num(x, default=None):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return default if v != v else v


def _codice_valido(c):
    """Codice luce 0..6 (intero o nome come "on", "assist")."""
    if isinstance(c, str):
        return c in LIGHT_BY_NAME or (c.isdigit() and int(c) in LIGHT_CODES)
    try:
        return int(c) == c and int(c) in LIGHT_CODES
    except (TypeError, ValueError, OverflowError):
        return False


class SignalController:
    def __init__(self, cfg, mcu):
        cfg = cfg if isinstance(cfg, dict) else {}
        self.enabled = bool(cfg.get("enabled", True))
        self.mcu = mcu
        self.hold_s = _num(cfg.get("hold_s"), 10.0)
        self.min_severity = int(_num(cfg.get("min_severity"), 2))
        self.ttl_ms = int(min(max(_num(cfg.get("ttl_ms"), 3000), 500), 60000))
        # il keep-alive deve arrivare ben prima che scada il ttl dell'MCU
        self.refresh_s = min(max(_num(cfg.get("refresh_s"), 1.0), 0.2), self.ttl_ms / 2000)
        self.channels = int(min(max(_num(cfg.get("channels"), 1), 0), MAX_CHANNELS))

        roles = cfg.get("roles")
        roles = list(roles)[:4] if isinstance(roles, (list, tuple)) else list(RUOLI_DEFAULT)
        roles += ["none"] * (4 - len(roles))
        self.roles = []
        for i, r in enumerate(roles):
            if r not in RUOLI:
                print(f"[segnali] LED {i + 1}: ruolo '{r}' sconosciuto -> 'none' (validi: {', '.join(RUOLI)})")
                r = "none"
            self.roles.append(r)

        self._lock = threading.Lock()
        # calcolo + invio in un solo passo: con push() da un thread e tick() da un altro, un comando
        # calcolato prima non deve arrivare all'MCU DOPO uno piu' recente (es. "danger" coperto da "off")
        self._tick_lock = threading.Lock()
        self._local = {}    # id -> {"sev", "emergency", "until", "track"}
        self._remote = {}
        self._seen = {}     # classe vista dalla camera -> ultimo istante
        self._tracks = {}   # traccia vista dalla camera -> ultimo istante
        self._online = False
        self._power = "active"
        self._codes = [0] * MAX_CHANNELS   # codice luce per canale (da crosswalk.py)
        self._modes = [0, 0, 0, 0]
        self._packed = None                # ultimo valore dei LED calcolato
        self._xwalk = None                 # ultimo valore dei canali calcolato
        self._last_queued = None           # time.monotonic() dell'ultimo invio
        self._sent = self._errors = 0
        self._warned = {}
        if self.enabled:
            dove = "verso l'MCU" if mcu is not None else "nessun MCU: solo log"
            print(f"[segnali] LED {self.roles}, {self.channels} canali luce strisce, {dove}")

    # ------------------------------------------------------------------ ingresso

    def on_hazard(self, msg, local, t):
        """hazard locale (local=True) o remote_hazard da un vicino: "new" accende, "cancel" spegne quell'id."""
        if not self.enabled:
            return
        try:
            self._on_hazard(msg, bool(local), float(t))
        except Exception as e:  # un messaggio strano non deve fermare il nodo
            self._warn("hazard", f"[segnali] messaggio ignorato ({e!r})")

    def _on_hazard(self, msg, local, t):
        hid = msg.get("id")
        if hid is None:
            return
        hid = str(hid)
        attivi = self._local if local else self._remote
        if msg.get("action") == "cancel":
            with self._lock:
                attivi.pop(hid, None)
            return

        sev = int(min(max(_num(msg.get("severity"), 1), 1), 3))
        emergency = msg.get("kind") == "emergency_vehicle" or msg.get("category") == "emergency"
        ts = _num(msg.get("ts"))
        exp = _num(msg.get("expires"))
        if local:
            # acceso fino al cancel, ma al massimo hold_s (e mai oltre la validita' dell'evento)
            base = ts / 1000 if ts else t
            until = base + self.hold_s
            if exp:
                until = min(until, exp / 1000)
        else:
            # "expires" viene dall'orologio di un altro incrocio: se la board e' indietro
            # non lo tengo acceso piu' della validita' dichiarata (expires - ts)
            until = exp / 1000 if exp else t + self.hold_s
            if exp and ts and exp > ts:
                until = min(until, t + (exp - ts) / 1000)
            until = min(until, t + MAX_REMOTE_S)
        with self._lock:
            if until <= t:
                attivi.pop(hid, None)
                if not local:
                    self._warn("scaduto", f"[segnali] pericolo remoto {hid} gia' scaduto all'arrivo "
                                          f"(messaggio vecchio o orologio della board non sincronizzato?)")
                return
            actor = msg.get("actor") if isinstance(msg.get("actor"), dict) else {}
            track = actor.get("track") if local and actor.get("track") not in (None, 0, "") else None
            attivi[hid] = {"sev": sev, "emergency": emergency, "until": until, "track": track}
            if track is not None:
                self._tracks.setdefault(str(track), t)
            if len(attivi) > MAX_ATTIVI:
                del attivi[min(attivi, key=lambda k: attivi[k]["until"])]

    def set_presence(self, classes, t, tracks=()):
        """Classi e tracce viste adesso dalla camera (person, cyclist, vehicle, emergency)."""
        if not self.enabled:
            return
        try:
            t = float(t)
            with self._lock:
                for c in classes or ():
                    self._seen[str(c)] = t
                for tr in tracks or ():
                    self._tracks[str(tr)] = t
                if len(self._tracks) > 4 * MAX_ATTIVI:  # tracce vecchie: non servono piu'
                    for k in [k for k, v in self._tracks.items() if t - v > 60]:
                        del self._tracks[k]
        except Exception as e:
            self._warn("presence", f"[segnali] presenze ignorate ({e!r})")

    def set_crosswalks(self, codes):
        """Codici luce dei canali (canale -> codice, par. 2.3). I canali assenti si spengono (0)."""
        if not self.enabled:
            return
        try:
            nuovi = [0] * MAX_CHANNELS
            items = codes.items() if isinstance(codes, dict) else enumerate(codes or ())
            for ch, c in items:
                try:
                    ch = int(ch)
                except (TypeError, ValueError, OverflowError):
                    # una chiave rotta non deve far perdere l'aggiornamento degli altri canali
                    self._warn(f"canale:{ch!r}", f"[segnali] canale luce {ch!r} non valido: ignorato")
                    continue
                if not 0 <= ch < MAX_CHANNELS:
                    self._warn(f"canale:{ch}", f"[segnali] canale luce {ch} inesistente (0..{MAX_CHANNELS - 1}): ignorato")
                    continue
                if ch >= self.channels:
                    self._warn(f"canale:{ch}", f"[segnali] canale luce {ch} oltre signals.channels = "
                                               f"{self.channels}: lo mando lo stesso (controllare config)")
                if not _codice_valido(c):
                    self._warn(f"codice:{c!r}", f"[segnali] codice luce {c!r} sconosciuto -> unknown (6)")
                nuovi[ch] = light_code(c)
            with self._lock:
                self._codes = nuovi
        except Exception as e:
            self._warn("crosswalks", f"[segnali] codici luce ignorati ({e!r})")

    def set_power_state(self, state):
        """Stato di energia del nodo (power.py): a riposo restano solo emergenze remote e strisce."""
        state = str(state)
        if state not in STATI_ENERGIA:
            self._warn(f"power:{state}", f"[segnali] stato di energia '{state}' sconosciuto: lo tratto come attivo")
            state = "active"
        with self._lock:
            cambiato = state != self._power
            self._power = state
        return cambiato

    def push(self, t=None):
        """Aggiorna subito LED e luci se qualcosa e' cambiato (senza aspettare il tick)."""
        self.tick(t, None)

    def tick(self, t, online):
        """Ricalcola LED e luci e li manda all'MCU se cambiati, o comunque ogni refresh_s (keep-alive).
        online=None: lascia lo stato di collegamento dell'ultimo tick."""
        if not self.enabled:
            return
        try:
            t = time.time() if t is None else float(t)
            with self._tick_lock:   # mcu.send_signals non blocca: tenerlo qui dentro costa poco
                with self._lock:
                    if online is not None:
                        self._online = bool(online)
                    modes = self._compute(t, self._online)
                    packed = pack_leds(modes)
                    xwalk = pack_xwalk(self._codes)
                    cambiato = packed != self._packed or xwalk != self._xwalk
                    self._modes, self._packed, self._xwalk = modes, packed, xwalk
                    codes = list(self._codes)
                if cambiato:
                    testo = "  ".join(f"LED{i + 1}({r})={NOMI_MODI[m]}" for i, (r, m) in enumerate(zip(self.roles, modes)))
                    luci = "  ".join(f"{ch}={LIGHT_CODES[c]}" for ch, c in enumerate(codes) if ch < self.channels or c)
                    print(f"[segnali] {testo}" + (f"  | strisce {luci}" if luci else ""))
                if self.mcu is None:
                    return
                # il keep-alive va a tempo reale (monotonic): il ttl dell'MCU non sa nulla del t simulato
                now = time.monotonic()
                if cambiato or self._last_queued is None or now - self._last_queued >= self.refresh_s - 0.05:
                    self._last_queued = now
                    self._send(packed, xwalk)
        except Exception as e:
            self._warn("tick", f"[segnali] errore in tick: {e!r}")

    def _compute(self, t, online):
        for attivi in (self._local, self._remote):
            for k in [k for k, h in attivi.items() if h["until"] <= t]:
                del attivi[k]
        # pericolo locale legato a un oggetto: si spegne appena la camera non lo vede piu'
        for k in [k for k, h in self._local.items() if h.get("track") is not None
                  and t - self._tracks.get(str(h["track"]), -1e9) > PRESENZA_HOLD_S]:
            del self._local[k]
        if self._power == "sleep":
            # a riposo: niente presenze, niente pericoli locali, solo le emergenze dei vicini
            local_d = {}
            remote_d = {k: h for k, h in self._remote.items() if h["emergency"]}
            vede = set()
        else:
            local_d, remote_d = self._local, self._remote
            vede = {c for c, ts in self._seen.items() if t - ts <= PRESENZA_HOLD_S}
        local_sev = max((h["sev"] for h in local_d.values() if h["sev"] >= self.min_severity), default=0)
        local = LOCAL_MODE.get(local_sev, 0)
        remote_sev = max((h["sev"] for h in remote_d.values()), default=0)
        remote = 0 if not remote_sev else (3 if remote_sev >= 3 else 2)
        emerg = any(h["emergency"] for d in (local_d, remote_d) for h in d.values())
        # rosso: pericoli che non sono emergenze (le emergenze hanno il loro LED blu)
        loc_h = max((h["sev"] for h in local_d.values() if h["sev"] >= self.min_severity
                     and not h["emergency"]), default=0)
        rem_h = max((h["sev"] for h in remote_d.values() if not h["emergency"]), default=0)
        hazard = LOCAL_MODE.get(loc_h, 0) or (0 if not rem_h else (3 if rem_h >= 3 else 2))
        status = 0 if self._power == "sleep" else (1 if online else 2)
        per_ruolo = {"local": local, "remote": remote, "emergency": 3 if emerg or "emergency" in vede else 0,
                     "status": status, "any": max(local, remote), "none": 0,
                     "hazard": hazard, "person": 1 if "person" in vede else 0,
                     "cyclist": 1 if "cyclist" in vede else 0, "vehicle": 1 if "vehicle" in vede else 0}
        return [per_ruolo[r] for r in self.roles]

    def _send(self, packed, xwalk):
        try:
            self.mcu.send_signals(int(packed), int(xwalk), int(self.ttl_ms))
            with self._lock:
                self._sent += 1
        except Exception as e:  # mcu.send_signals non dovrebbe mai sollevare: lo dico e continuo
            with self._lock:
                self._errors += 1
            self._warn("send", f"[segnali] invio all'MCU fallito: {e!r}")

    # ------------------------------------------------------------------ stato

    def state(self):
        with self._lock:
            n = max([self.channels] + [ch + 1 for ch, c in enumerate(self._codes) if c])
            return {
                "enabled": self.enabled,
                "power": self._power,
                "leds": {"roles": list(self.roles), "modes": list(self._modes),
                         "names": [NOMI_MODI[m] for m in self._modes], "packed": self._packed,
                         "active": sorted(self._local) + sorted(self._remote)},
                "crosswalks": {ch: LIGHT_CODES[self._codes[ch]] for ch in range(n)},
                "xwalk": self._xwalk,
                "ttl_ms": self.ttl_ms,
                "mcu": self.mcu is not None,
                "sent": self._sent,
                "send_errors": self._errors,
            }

    def close(self):
        """L'MCU lo chiude chi lo ha creato (node.py): qui non c'e' nessun thread."""
        return None

    def _warn(self, chiave, testo, max_volte=3):
        n = self._warned.get(chiave, 0)
        if n < max_volte:
            self._warned[chiave] = n + 1
            print(testo)
