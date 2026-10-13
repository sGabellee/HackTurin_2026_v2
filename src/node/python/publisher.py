"""Uscita locale dei messaggi del nodo: archivio, pagina web di App Lab, console.

Nella v2 il nodo non invia piu' nulla a un server: i dati restano sulla board e il collettore li
scarica quando serve (pull su /api/export, vedi api.py e src/collector/). publish(msg) fa, in
quest'ordine (ogni passo e' isolato: se uno fallisce gli altri vanno avanti):
  1. archivio: SQLite (Storage) oppure, se non c'e', una riga JSONL in log_file;
  2. pagina web dell'app (WebUI di App Lab): hazard, remote_hazard e status;
  3. una riga leggibile in console per ogni pericolo.
"""
import json
import os
import threading
from pathlib import Path

LOG_MAX_BYTES = 50 * 1024 * 1024  # oltre, log_file viene ruotato in log_file.1
UI_TYPES = ("hazard", "remote_hazard", "status")


def _dumps(msg):
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"), default=str)


class Publisher:
    def __init__(self, cfg, node_id, storage=None, ui=None, base_dir=".", store_scene=None, **_):
        cfg = cfg or {}
        self.node_id = node_id
        self.storage = storage
        self.ui = ui
        self.base_dir = Path(base_dir)
        # scene a 1 Hz riempirebbe l'archivio: si salva solo se richiesto (data.store_scene)
        self.store_scene = bool(cfg.get("store_scene", False) if store_scene is None else store_scene)
        log_file = cfg.get("log_file", "events.jsonl")
        self.log_path = self.base_dir / log_file if log_file else None
        self._file_lock = threading.Lock()
        self._errors = {}
        self.archived = 0

    def publish(self, msg):
        if not isinstance(msg, dict):
            self._err("publish", TypeError(f"messaggio non valido: {type(msg).__name__}"))
            return
        tipo = msg.get("type")
        self._step("archivio", self._archive, msg, tipo)
        if self.ui is not None and tipo in UI_TYPES:
            self._step("ui", self.ui.send_message, "situation", msg)
        if tipo in ("hazard", "remote_hazard"):
            self._step("console", self._print_hazard, msg, tipo)

    def _step(self, where, fn, *args):
        try:
            fn(*args)
        except Exception as e:  # un'uscita guasta (UI, disco) non deve fermare le altre
            self._err(where, e)

    def _err(self, where, e):
        n = self._errors[where] = self._errors.get(where, 0) + 1
        if n <= 3 or n % 100 == 0:
            print(f"[uscita] errore in {where} (x{n}): {e!r}")

    def _archive(self, msg, tipo):
        if tipo == "scene" and not self.store_scene:
            return
        if self.storage is not None:
            try:
                if self.storage.save(msg) is not False:
                    self.archived += 1
                    return
            except Exception as e:  # SQLite rotto: si ripiega sul file
                self._err("save", e)
        if self.log_path is not None:
            self._append_jsonl(self.log_path, [_dumps(msg)])
            self.archived += 1

    def _append_jsonl(self, path, lines):
        with self._file_lock:
            try:
                if path.stat().st_size > LOG_MAX_BYTES:
                    os.replace(path, path.with_name(path.name + ".1"))
            except FileNotFoundError:
                pass
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write("".join(line + "\n" for line in lines))

    def _print_hazard(self, msg, tipo):
        a = msg.get("actor") or {}
        if tipo == "hazard":
            print(f"[PERICOLO] {msg.get('kind')} sev={msg.get('severity')} azione={msg.get('action', 'new')} "
                  f"traccia={a.get('track', '-')} classe={a.get('class', '-')} camera={msg.get('camera', '-')}")
        else:
            print(f"[PERICOLO VICINO] {msg.get('kind')} sev={msg.get('severity')} "
                  f"azione={msg.get('action', 'new')} da={msg.get('from_node', '?')}")

    def backlog(self):
        """Compatibilita': nella v2 non c'e' coda di invio."""
        return 0

    def info(self):
        return {"archived": self.archived, "errors": dict(self._errors)}

    def close(self):
        pass
