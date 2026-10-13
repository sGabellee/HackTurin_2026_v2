"""Test di fumo del simulatore della citta' (simulate_city.py): 30 incroci veri, tempo accelerato.

Controlla gli scenari: il non vedente riceve prima ASSIST_UNAVAILABLE (camera in avvio) e CROSS_OK solo dopo,
VEHICLE_LEFT con l'auto in arrivo, luce di pericolo, KEEP_GOING sulle strisce; l'ambulanza sveglia gli incroci
del percorso e lascia gli altri a riposo, poi nessun corridoio resta aperto; il nodo di notte torna a riposo.

Uso:  python test_city.py   (dalla cartella node/tools/tests)
"""
import io
import re
import sys
import tempfile
import contextlib
from pathlib import Path

QUI = Path(__file__).resolve().parent
sys.path.insert(0, str(QUI.parents[1] / "python"))
sys.path.insert(0, str(QUI.parents[0]))

import simulate_city as sc  # noqa: E402

RISULTATI = []


def check(nome, ok, info=""):
    RISULTATI.append((nome, bool(ok), info))
    print(f"  [{'OK ' if ok else 'ERR'}] {nome}" + (f"  ({info})" if info and not ok else ""))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        racconto = io.StringIO()
        sc.OUT = racconto
        with contextlib.redirect_stdout(io.StringIO()):
            sc.main(["--fast", "--nodes", "30", "--port", "0", "--scenario", "demo", "--out", tmp])
        sc.OUT = sys.__stdout__
    t = racconto.getvalue()
    check("non vedente: prima ASSIST_UNAVAILABLE, poi CROSS_OK, VEHICLE_LEFT, KEEP_GOING",
          "ASSIST_UNAVAILABLE -> CROSS_OK -> VEHICLE_LEFT -> CROSS_OK -> KEEP_GOING" in t, t[:900])
    check("luce di pericolo con l'auto in arrivo", "arrivata a: PERICOLO" in t)
    m = re.search(r"sul percorso (\d+)/(\d+)", t)
    check("ambulanza: sveglia quasi tutto il percorso", m and int(m.group(1)) >= int(m.group(2)) - 1, m and m.group(0))
    m = re.search(r"svegliati dal soccorso: (\d+).*gli altri (\d+) sono rimasti a riposo", t)
    check("ambulanza: gli incroci lontani restano a riposo", m and int(m.group(2)) > 0, m and m.group(0))
    check("nessun corridoio aperto dopo il passaggio", "corridoi ancora aperti 0" in t)
    check("notte: il nodo torna a riposo", "di nuovo a riposo" in t)
    ok = sum(1 for r in RISULTATI if r[1])
    print(f"\n{ok}/{len(RISULTATI)} controlli OK")
    sys.exit(0 if ok == len(RISULTATI) else 1)


if __name__ == "__main__":
    main()
