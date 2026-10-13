"""Disegna ciclabile, strisce, linee e vie su una foto della camera e salva tutto in config.json.

Uso:  python zone_editor.py foto.png  [..\\python\\config.json]  [--camera cam0]

La foto deve essere ESATTAMENTE l'inquadratura della camera (es. screenshot del solo video
della pagina web dell'app, ritagliato sui bordi). Le coordinate salvate sono 0..1, dentro
config["cameras"][camera] (formato v2) o al primo livello per i config a una camera.
Gli attraversamenti (config["crosswalks"]) vengono creati per ogni zona crosswalk* disegnata, con la
sua area d'attesa, il sensore sul bordo e il canale luce in ordine: ritoccateli poi a mano.

Comandi nella finestra:
    click sinistro = aggiungi punto     u = annulla ultimo punto
    INVIO = conferma elemento           s = salta (tiene quello che c'era)
    ESC = salva ed esci
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ELEMENTI = [
    ("calibration", "CALIBRAZIONE: 4 punti a terra che formano un rettangolo di misure note.\n"
                    "  Ordine: lontano-sinistra, lontano-destra, vicino-destra, vicino-sinistra"),
    ("zones.roadway", "contorno della CARREGGIATA (dove passano le auto, esclusa la ciclabile)"),
    ("zones.bike_lane", "contorno della PISTA CICLABILE"),
    ("zones.crosswalk", "contorno delle STRISCE PEDONALI (cliccate prima il lato lungo: definisce la direzione A->B)"),
    ("zones.sidewalk", "MARCIAPIEDE (facoltativo: serve per i ciclisti sul marciapiede, s per saltare)"),
    ("zones.waiting_area", "AREA D'ATTESA dei pedoni su un lato delle strisce (serve per avvisare i Tag)"),
    ("zones.waiting_area_dx", "AREA D'ATTESA sull'altro lato delle strisce (facoltativo)"),
    ("zones.crosswalk_2", "SECONDE STRISCE viste da questa camera (facoltativo)"),
    ("zones.waiting_area_2", "AREA D'ATTESA delle seconde strisce (facoltativo)"),
    ("zones.box", "CENTRO DELL'INCROCIO dove e' vietato fermarsi (facoltativo)"),
    ("lines.linea_continua", "LINEA CONTINUA (2 o piu' punti lungo la linea)"),
    ("lanes.corsia_verso_incrocio", "contorno della CORSIA che va VERSO l'incrocio (verso la camera)"),
    ("lanes.corsia_verso_incrocio.direction", "DIREZIONE di marcia di quella corsia: 2 punti, da dove arrivano a dove vanno"),
    ("lanes.corsia_verso_via", "contorno della CORSIA che si ALLONTANA dall'incrocio"),
    ("lanes.corsia_verso_via.direction", "DIREZIONE di marcia di quella corsia: 2 punti, da dove arrivano a dove vanno"),
    ("approaches.via", "fascia in FONDO alla strada (da dove arrivano / dove spariscono)"),
    ("approaches.incrocio", "fascia VICINA alla camera (centro dell'incrocio)"),
]
COLORI = {"calibration": (0, 255, 255), "zones": (255, 0, 255), "lines": (0, 0, 255), "approaches": (0, 255, 0)}


def main():
    args = list(sys.argv[1:])
    cam_id = "cam0"
    if "--camera" in args:
        i = args.index("--camera")
        cam_id = args[i + 1] if i + 1 < len(args) else cam_id
        del args[i:i + 2]
    foto = Path(args[0])
    cfg_path = Path(args[1]) if len(args) > 1 else Path(__file__).resolve().parents[1] / "python" / "config.json"
    radice = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    if "cameras" in radice or "zones" not in radice:  # formato v2: una sezione per camera
        cfg = radice.setdefault("cameras", {}).setdefault(cam_id, {})
    else:
        cfg = radice
    immagine = cv2.imread(str(foto))
    if immagine is None:
        sys.exit(f"non riesco ad aprire {foto}")
    scala = min(1.0, 1280 / immagine.shape[1])
    immagine = cv2.resize(immagine, None, fx=scala, fy=scala)
    h, w = immagine.shape[:2]
    punti = []

    def click(evento, x, y, *_):
        if evento == cv2.EVENT_LBUTTONDOWN:
            punti.append([round(x / w, 4), round(y / h, 4)])

    cv2.namedWindow("zone")
    cv2.setMouseCallback("zone", click)

    def disegna(corrente):
        img = immagine.copy()
        for sezione in ("zones", "lines", "approaches"):
            for nome, val in (cfg.get(sezione) or {}).items():
                pts = val["points"] if sezione == "lines" else val
                if pts:
                    p = [(int(a * w), int(b * h)) for a, b in pts]
                    cv2.polylines(img, [np.array(p)], sezione != "lines", COLORI[sezione], 2)
                    cv2.putText(img, nome, p[0], cv2.FONT_HERSHEY_SIMPLEX, 0.5, COLORI[sezione], 1)
        for nome, lane in (cfg.get("lanes") or {}).items():
            if lane.get("polygon"):
                cv2.polylines(img, [np.array([(int(a * w), int(b * h)) for a, b in lane["polygon"]])], True, (200, 200, 0), 1)
            if lane.get("direction") and len(lane["direction"]) == 2:
                (a1, b1), (a2, b2) = lane["direction"]
                cv2.arrowedLine(img, (int(a1 * w), int(b1 * h)), (int(a2 * w), int(b2 * h)), (200, 200, 0), 2, tipLength=0.05)
        for a, b in punti:
            cv2.circle(img, (int(a * w), int(b * h)), 5, (255, 255, 255), -1)
        cv2.putText(img, corrente, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.imshow("zone", img)

    for chiave, istruzioni in ELEMENTI:
        print("\n" + istruzioni + "\n  (INVIO conferma, u annulla, s salta, ESC salva ed esci)")
        punti.clear()
        while True:
            disegna(chiave)
            tasto = cv2.waitKey(30) & 0xFF
            if tasto == ord("u") and punti:
                punti.pop()
            elif tasto == ord("s"):
                break
            elif tasto == 27:
                return salva(radice, cfg, cam_id, cfg_path)
            elif tasto in (10, 13) and len(punti) >= 2:
                applica(cfg, chiave, list(punti))
                break
    salva(radice, cfg, cam_id, cfg_path)


def applica(cfg, chiave, pts):
    if chiave == "calibration":
        if len(pts) != 4:
            print("  servono esattamente 4 punti: calibrazione saltata")
            return
        larg = float(input("  larghezza del rettangolo in metri (sinistra-destra): ").replace(",", "."))
        lung = float(input("  lunghezza del rettangolo in metri (vicino-lontano): ").replace(",", "."))
        cfg["calibration"] = {"image_points": pts, "world_points_m": [[0, lung], [larg, lung], [larg, 0], [0, 0]]}
        return
    sezione, nome, *resto = chiave.split(".")
    cfg.setdefault(sezione, {})
    if sezione == "lanes":
        corsia = cfg[sezione].setdefault(nome, {})
        if resto:  # direzione
            corsia["direction"] = pts[:2]
        else:
            corsia["polygon"] = pts
    elif sezione == "lines":
        tipo = "stop" if "arresto" in nome else "solid"
        cfg[sezione][nome] = {"type": tipo, "points": pts}
    else:
        cfg[sezione][nome] = pts


def attraversamenti(radice, cam, cam_id):
    """Un attraversamento per ogni zona crosswalk* della camera che non ne ha ancora uno."""
    zone = cam.get("zones") or {}
    lista = radice.setdefault("crosswalks", [])
    gia = {(c.get("camera", cam_id), c.get("zone")) for c in lista if isinstance(c, dict)}
    for z in sorted((z for z in zone if z.startswith("crosswalk")), key=lambda z: (z != "crosswalk", z)):
        if (cam_id, z) in gia:
            continue
        suff = z[len("crosswalk"):]
        validi = (suff,) if suff else ("", "_dx")
        attesa = [a for a in zone if a.startswith("waiting_area") and a[len("waiting_area"):] in validi]
        i = len(lista)
        lista.append({"id": i, "camera": cam_id, "zone": z, "waiting": attesa or None,
                      "curb": i if i < 4 else None, "light": i if i < 4 else None, "left": "via", "right": "incrocio"})
        print(f"  attraversamento {i}: {z} (attesa {attesa or 'nessuna'}, sensore {i}, canale luce {i})")


def salva(radice, cam, cam_id, cfg_path):
    if radice is not cam:
        attraversamenti(radice, cam, cam_id)
    cfg_path.write_text(json.dumps(radice, indent=2, ensure_ascii=False), encoding="utf-8")
    cv2.destroyAllWindows()
    print(f"\nsalvato in {cfg_path}")


if __name__ == "__main__":
    main()
