"""CROSS v2 - nodo incrocio (Arduino UNO Q, App Lab).

camera -> Video Object Detection (modello Edge Impulse) -> Node (regole, attraversamenti, Tag, radio)

A riposo (power.py) l'inferenza viene fermata: la riattivano il sensore sul bordo delle strisce,
un CROSS Tag vicino o un incrocio vicino via radio.
"""
import threading
import time
from pathlib import Path

from arduino.app_bricks.video_objectdetection import VideoObjectDetection
from arduino.app_bricks.web_ui import WebUI
from arduino.app_utils import App, Bridge

from config import load_config
from node import Node

BASE = Path(__file__).resolve().parent
cfg, avvisi = load_config(BASE / "config.json")
for a in avvisi:
    print("[config]", a)

ui = WebUI()
node = Node(cfg, ui=ui, base_dir=BASE, bridge=Bridge)
cams = list(cfg["_cameras"])


def crea_detector(cam):
    """(detector, True se usa davvero quella camera). Se App Lab non accetta la scelta della camera
    si usa quella di default, una volta sola: le zone delle altre camere restano senza fotogrammi."""
    kw = {"confidence": cfg.get("confidence", 0.45), "debounce_sec": 0.0}
    if len(cams) > 1:
        try:
            return VideoObjectDetection(camera=cam, **kw), True
        except TypeError:
            print(f"[main] il brick non accetta la scelta della camera: {cam} -> camera di default")
    return VideoObjectDetection(**kw), False


detectors = {}
default_usata = False
for cam in cams:
    try:
        d, propria = crea_detector(cam)
    except Exception as e:
        print(f"[main] camera {cam} non disponibile: {e!r}")
        continue
    if not propria:
        if default_usata:
            continue
        default_usata = True
    d.on_detect_all(lambda detections, cam=cam: node.process(detections, cam))
    detectors[cam] = d


def on_power(old, new):
    """A riposo si ferma l'inferenza (il risparmio vero); sveglio la si riavvia."""
    for cam, d in detectors.items():
        metodo = getattr(d, "stop" if new == "sleep" else "start", None)
        if not callable(metodo):
            continue  # brick senza stop/start: i fotogrammi arrivano ma node.process li scarta
        try:
            metodo()
        except Exception as e:
            print(f"[main] {cam}: {'stop' if new == 'sleep' else 'start'} non riuscito ({e!r})")


node.power_listeners.append(on_power)


def ciclo():
    """Ogni tick_s: sensori, energia, attraversamenti, luci, avvisi ai Tag."""
    while True:
        time.sleep(node.tick_s)
        try:
            node.tick()
        except Exception as e:  # il ciclo non deve morire mai
            print("[ciclo]", repr(e))


threading.Thread(target=ciclo, name="tick", daemon=True).start()
App.run()
