"""Lancia tutti i test del progetto uno dopo l'altro e stampa il riepilogo.

Uso:  python run_tests.py        (da qualsiasi cartella; esce con codice 1 se qualcosa fallisce)
"""
import re
import subprocess
import sys
from pathlib import Path

QUI = Path(__file__).resolve().parent
TESTS = [QUI / "test_suite.py"] + sorted((QUI / "tests").glob("test_*.py"))


def main():
    falliti = []
    for t in TESTS:
        r = subprocess.run([sys.executable, str(t)], cwd=str(t.parent), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
        m = re.findall(r"(\d+)/(\d+) controlli (?:OK|superati)", r.stdout)
        esito = f"{m[-1][0]}/{m[-1][1]}" if m else "?"
        ok = r.returncode == 0
        print(f"[{'OK ' if ok else 'ERR'}] {t.name:<26} {esito}")
        if not ok:
            falliti.append(t.name)
            print("\n".join("      " + l for l in (r.stdout + r.stderr).splitlines()[-12:]))
    print(f"\n{len(TESTS) - len(falliti)}/{len(TESTS)} script superati" + (f"; falliti: {', '.join(falliti)}" if falliti else ""))
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(main())
