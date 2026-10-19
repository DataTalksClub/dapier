"""Seen store for tracking deduplication source-keys."""

import json
import os
from typing import Iterable, Optional, Set

DEFAULT_SEEN_PATH = os.path.expanduser("~/.dapier/crm-signals/seen-keys.json")


class SeenStore:
    """Persistent or in-memory set of processed source-keys."""

    def __init__(self, file_path: Optional[str] = None):
        self.file_path = os.path.expanduser(file_path) if file_path else None
        self._keys: Set[str] = set()
        self._load()

    def _ensure_dir(self):
        if self.file_path:
            directory = os.path.dirname(self.file_path)
            if directory:
                os.makedirs(directory, exist_ok=True)

    def _load(self):
        if not self.file_path or not os.path.exists(self.file_path):
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    self._keys = set(data)
                elif isinstance(data, dict):
                    self._keys = set(data.get("keys", []))
        except Exception:
            self._keys = set()

    def _save(self):
        if not self.file_path:
            return
        self._ensure_dir()
        temp_path = f"{self.file_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump({"keys": sorted(list(self._keys))}, f, indent=2)
        os.replace(temp_path, self.file_path)

    def has(self, source_key: str) -> bool:
        """Check if source_key is already recorded."""
        return str(source_key).strip() in self._keys

    def add(self, source_key: str):
        """Record a source_key and persist."""
        key = str(source_key).strip()
        if key and key not in self._keys:
            self._keys.add(key)
            self._save()

    def bulk_add(self, source_keys: Iterable[str]):
        """Record multiple source-keys."""
        changed = False
        for k in source_keys:
            clean = str(k).strip()
            if clean and clean not in self._keys:
                self._keys.add(clean)
                changed = True
        if changed:
            self._save()

    def count(self) -> int:
        return len(self._keys)
