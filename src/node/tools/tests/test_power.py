"""Test di power.py (stati di energia e risvegli, docs/ARCHITETTURA.md par. 2.2).

Prova: avvio attivo per boot_active_s, tutte le transizioni (sleep/active/cooldown), active_hold_s,
hold_s, attivita', sorgenti radio e wake_roles, guasto del sensore, power.enabled = false,
risveglio programmato, stima dell'energia e robustezza della callback.

Uso:  python test_power.py      (dalla cartella node/tools/tests; esce con codice 1 se qualcosa fallisce)
"""
import contextlib
import io
import sys
import traceback
from pathlib import Path

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI.parents[1] / "python"))

from power import PowerManager  # noqa: E402

RISULTATI = []
T0 = 1000.0


def check(nome, ok, info=""):
    RISULTATI.append((nome, bool(ok), info))


def esegui(fn):
    """Esegue un test con i log del modulo nascosti, poi stampa [OK ]/[ERR] per ogni controllo.
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


def pm(**kw):
    cambi = []
    cfg = {"active_hold_s": 20, "cooldown_s": 5, "boot_active_s": 30}
    cfg.update(kw)
    p = PowerManager(cfg, on_change=lambda o, n, s, t: cambi.append((o, n, s, round(t, 2))))
    return p, cambi


def scorri(p, da, a, passo=0.25):
    """tick ogni passo secondi da 'da' (escluso) ad 'a' (incluso)."""
    n = int(round((a - da) / passo))
    for k in range(1, n + 1):
        p.tick(da + k * passo)


def addormenta(p, t=T0):
    """Avvio e attesa fino al riposo: ritorna l'istante in cui e' a riposo."""
    p.tick(t)
    scorri(p, t, t + p.boot_active_s + p.cooldown_s + 1)
    return t + p.boot_active_s + p.cooldown_s + 1


# ---------------------------------------------------------------------- test

def test_avvio():
    p, cambi = pm()
    check("avvio: attivo prima di qualsiasi tick", p.state == "active" and p.awake)
    p.tick(T0)
    scorri(p, T0, T0 + 29.75)
    check("avvio: ancora attivo a boot_active_s - 0,25", p.state == "active", p.state)
    scorri(p, T0 + 29.75, T0 + 30.0)
    check("avvio: cooldown a boot_active_s", p.state == "cooldown" and p.awake, (p.state, cambi))
    scorri(p, T0 + 30.0, T0 + 34.75)
    check("cooldown: ancora sveglio prima di cooldown_s", p.state == "cooldown", p.state)
    scorri(p, T0 + 34.75, T0 + 35.0)
    check("cooldown -> sleep dopo cooldown_s", p.state == "sleep" and not p.awake, p.state)
    check("on_change: active->cooldown->sleep con sorgente idle",
           cambi == [("active", "cooldown", "idle", T0 + 30.0), ("cooldown", "sleep", "idle", T0 + 35.0)], cambi)
    st = p.stats(T0 + 35)
    check("stats: risveglio di avvio contato come boot", st["wakeups"] == 1 and st["by_source"] == {"boot": 1}
          and st["triggers"].get("boot") == 1, st)
    check("codice heartbeat a riposo = 's'", p.code == "s", p.code)


def test_boot_corto():
    p, _ = pm(boot_active_s=5)
    p.tick(T0)
    scorri(p, T0, T0 + 4.75)
    ok1 = p.state == "active"
    scorri(p, T0 + 4.75, T0 + 5.0)
    check("boot_active_s piu' corto di active_hold_s: dura boot_active_s", ok1 and p.state == "cooldown", p.state)


def test_trigger_e_hold():
    p, cambi = pm()
    t = addormenta(p)
    cambi.clear()
    ok = p.trigger("curb", t)
    check("trigger curb da riposo: accettato e attivo", ok and p.state == "active", p.state)
    check("on_change sleep->active (curb)", cambi == [("sleep", "active", "curb", round(t, 2))], cambi)
    check("codice heartbeat da sveglio = 'a'", p.code == "a")
    scorri(p, t, t + 19.75)
    check("active_hold_s: ancora attivo a 19,75 s", p.state == "active", p.state)
    scorri(p, t + 19.75, t + 20.0)
    check("active_hold_s: cooldown a 20 s senza attivita'", p.state == "cooldown", p.state)
    check("codice heartbeat in cooldown = 'c'", p.code == "c")

    # trigger in cooldown: torna attivo ma non e' un nuovo risveglio da riposo
    w = p.stats(t + 20)["wakeups"]
    p.trigger("curb", t + 21)
    check("trigger in cooldown -> active", p.state == "active", p.state)
    check("trigger in cooldown non conta come risveglio", p.stats(t + 21)["wakeups"] == w)

    # attivita' prolunga
    scorri(p, t + 21, t + 36)
    p.activity(t + 36)
    scorri(p, t + 36, t + 55.75)
    check("activity prolunga lo stato attivo di active_hold_s", p.state == "active", p.state)
    scorri(p, t + 55.75, t + 56.0)
    check("activity: cooldown 20 s dopo l'ultima attivita'", p.state == "cooldown", p.state)
    p.activity(t + 57)
    check("activity in cooldown -> active", p.state == "active", p.state)
    scorri(p, t + 57, t + 90)
    check("di nuovo a riposo", p.state == "sleep", p.state)
    p.activity(t + 91)
    check("activity a riposo non sveglia (camera ferma)", p.state == "sleep", p.state)


def test_hold_s():
    p, _ = pm()
    t = addormenta(p)
    p.trigger("radio_emergency", t, hold_s=60)
    scorri(p, t, t + 59.75)
    check("hold_s=60: attivo fino a t+60 senza attivita'", p.state == "active", p.state)
    scorri(p, t + 59.75, t + 60.0)
    check("hold_s=60: cooldown a t+60", p.state == "cooldown", p.state)
    t2 = t + 100
    scorri(p, t + 60, t2)
    p.trigger("curb", t2, hold_s=5)
    scorri(p, t2, t2 + 19.75)
    check("hold_s piu' corto di active_hold_s non accorcia", p.state == "active", p.state)
    st = p.stats(t2 + 20)
    check("by_source: curb e radio_emergency contati",
           st["by_source"].get("radio_emergency") == 1 and st["by_source"].get("curb") == 1, st["by_source"])


def test_radio():
    p, _ = pm(wake_on_radio=False)
    t = addormenta(p)
    check("wake_on_radio=false: radio_vru ignorato", p.trigger("radio_vru", t, detail={"role": "blind"}) is False
           and p.state == "sleep", p.state)
    check("wake_on_radio=false: radio_hazard ignorato", p.trigger("radio_hazard", t) is False and p.state == "sleep")
    check("wake_on_radio=false: il bordo sveglia comunque", p.trigger("curb", t) and p.state == "active")

    p, _ = pm()
    t = addormenta(p)
    check("radio_vru con ruolo fuori da wake_roles (cyclist) ignorato",
           p.trigger("radio_vru", t, detail={"role": "cyclist"}) is False and p.state == "sleep", p.state)
    check("radio_vru con ruolo blind sveglia",
           p.trigger("radio_vru", t + 1, detail={"role": "blind"}) is True and p.state == "active", p.state)
    p2, _ = pm(wake_roles=["blind", "emergency", "pedestrian"])
    t = addormenta(p2)
    check("wake_roles configurabile (pedestrian)", p2.trigger("radio_vru", t, detail={"p": "pedestrian"})
           and p2.state == "active")


def test_guasto_sensore():
    p, cambi = pm()
    t = addormenta(p)
    p.trigger("sensor_fault", t)
    check("sensor_fault sveglia il nodo", p.state == "active" and p.fault, p.state)
    scorri(p, t, t + 600, passo=1.0)
    check("sensor_fault: resta attivo per sempre (10 min senza attivita')", p.state == "active", p.state)
    check("stats: fault=True", p.stats(t + 600)["fault"] is True)
    p.clear_fault(t + 600)
    check("clear_fault: fault spento", not p.fault)
    scorri(p, t + 600, t + 619.75)
    check("clear_fault: ancora attivo per active_hold_s", p.state == "active", p.state)
    scorri(p, t + 619.75, t + 626)
    check("clear_fault: poi cooldown e riposo", p.state == "sleep", (p.state, cambi[-2:]))

    # guasto mentre e' gia' sveglio: niente cooldown
    p, _ = pm()
    p.tick(T0)
    p.trigger("sensor_fault", T0 + 1)
    scorri(p, T0 + 1, T0 + 200, passo=1.0)
    check("sensor_fault a nodo sveglio: mai cooldown", p.state == "active", p.state)


def test_disattivato():
    p, cambi = pm(enabled=False)
    p.tick(T0)
    scorri(p, T0, T0 + 300, passo=1.0)
    check("power.enabled=false: sempre attivo", p.state == "active" and p.awake and not cambi, (p.state, cambi))
    check("power.enabled=false: trigger accettato", p.trigger("curb", T0 + 300) is True)
    st = p.stats(T0 + 300)
    check("power.enabled=false: nessun risparmio", st["saved_wh"] == 0 and st["saved_pct"] == 0.0
           and st["seconds"]["active"] == 300.0, st)


def test_programmato():
    p, cambi = pm(sample_every_s=100, sample_s=10)
    t = addormenta(p)          # a riposo a T0+36
    scorri(p, t, T0 + 99.75)
    check("programmato: a riposo prima di sample_every_s", p.state == "sleep", p.state)
    scorri(p, T0 + 99.75, T0 + 100.0)
    check("programmato: sveglio a sample_every_s (schedule)", p.state == "active"
           and cambi[-1][:3] == ("sleep", "active", "schedule"), cambi[-1:])
    scorri(p, T0 + 100, T0 + 109.75)
    ok = p.state == "active"
    scorri(p, T0 + 109.75, T0 + 110.0)
    check("programmato: attivo per sample_s poi cooldown", ok and p.state == "cooldown", p.state)
    scorri(p, T0 + 110, T0 + 200)
    st = p.stats(T0 + 200)
    check("programmato: secondo risveglio a 2 x sample_every_s",
           st["by_source"].get("schedule") == 2, st["by_source"])


def test_energia():
    p, _ = pm(boot_active_s=10, active_hold_s=10, cooldown_s=5)
    p.tick(T0)
    scorri(p, T0, T0 + 100)
    st = p.stats(T0 + 100)
    sec = st["seconds"]
    check("energia: secondi per stato (10 attivo, 5 cooldown, 85 riposo)",
           sec == {"sleep": 85.0, "active": 10.0, "cooldown": 5.0}, sec)
    atteso = (10 * 9.0 + 5 * 9.0 + 85 * 2.5) / 3600
    check("energia: energy_wh = somma secondi x watt", abs(st["energy_wh"] - round(atteso, 4)) < 1e-4, st)
    check("energia: always_on_wh = tutto al consumo attivo", abs(st["always_on_wh"] - round(100 * 9.0 / 3600, 4)) < 1e-4)
    check("energia: saved_wh e saved_pct", abs(st["saved_wh"] - round(100 * 9 / 3600 - atteso, 4)) < 1e-4
           and abs(st["saved_pct"] - 61.4) < 0.05, st)
    check("stats: tutti i campi della specifica",
           {"state", "since_ms", "wakeups", "by_source", "seconds", "energy_wh", "always_on_wh", "saved_wh",
            "saved_pct"} <= set(st), sorted(st))
    check("stats: since_ms = ingresso nello stato attuale (ms)", st["since_ms"] == int((T0 + 15) * 1000), st["since_ms"])

    p2, _ = pm(boot_active_s=10, active_hold_s=10, cooldown_s=5,
               power_w={"sleep": 1.0, "active": 10.0, "cooldown": 4.0})
    p2.tick(T0)
    scorri(p2, T0, T0 + 100)
    st2 = p2.stats(T0 + 100)
    check("energia: power_w configurabile", abs(st2["energy_wh"] - round((100 + 20 + 85) / 3600, 4)) < 1e-4, st2)


def test_robustezza():
    def rompi(*a):
        raise RuntimeError("camera rotta")
    p = PowerManager({"active_hold_s": 1, "cooldown_s": 1, "boot_active_s": 1}, on_change=rompi)
    p.tick(T0)
    scorri(p, T0, T0 + 5)
    check("callback che solleva: le transizioni continuano", p.state == "sleep", p.state)
    check("trigger con tempo non valido: False senza eccezioni", p.trigger("curb", None) is False)
    p.tick(T0 + 4)     # orologio un po' indietro
    p.tick("x")        # tempo non valido
    check("tick con tempo indietro o non valido: nessuna eccezione", p.state in ("sleep", "active", "cooldown"))
    p.trigger("pippo", T0 + 6)
    check("sorgente sconosciuta: accettata come manuale", p.state == "active")
    p3 = PowerManager({"active_hold_s": 2, "cooldown_s": 0, "boot_active_s": 2})
    p3.tick(T0)
    p3.tick(T0 + 2)
    check("cooldown_s=0: da attivo a riposo nello stesso tick", p3.state == "sleep", p3.state)
    p4 = PowerManager(None)
    p4.tick(T0)
    check("cfg mancante: valori di default", p4.active_hold_s == 20 and p4.boot_active_s == 30
           and p4.power_w["sleep"] == 2.5 and p4.state == "active")


def main():
    for fn in (test_avvio, test_boot_corto, test_trigger_e_hold, test_hold_s, test_radio, test_guasto_sensore,
               test_disattivato, test_programmato, test_energia, test_robustezza):
        print(f"\n== {fn.__name__}")
        esegui(fn)
    falliti = [r for r in RISULTATI if not r[1]]
    print(f"\n{len(RISULTATI) - len(falliti)}/{len(RISULTATI)} controlli OK")
    for nome, _, info in falliti:
        print(f"  FALLITO: {nome} -> {str(info)[:300]}")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
