"""Unmatched hold store with 14-day TTL and Hub intake formatting for project orin."""

from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

from .models import SignalItem

DEFAULT_HOLD_PATH = os.path.expanduser("~/.dapier/crm-signals/unmatched-hold.json")
DEFAULT_TTL_DAYS = 14


class UnmatchedHoldStore:
    """Manages hold items for unclear company signals with an automatic 14-day expiration."""

    def __init__(self, file_path: Optional[str] = None, ttl_days: int = DEFAULT_TTL_DAYS):
        self.file_path = os.path.expanduser(file_path or DEFAULT_HOLD_PATH)
        self.ttl_seconds = ttl_days * 86400
        self._ensure_dir()

    def _ensure_dir(self):
        directory = os.path.dirname(self.file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def _now(self, now: Optional[float] = None) -> datetime:
        if now is not None:
            return datetime.fromtimestamp(now, tz=timezone.utc)
        return datetime.now(timezone.utc)

    def _load_raw(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.file_path):
            return []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("items", []) if isinstance(data, dict) else []
        except Exception:
            return []

    def _save_raw(self, items: List[Dict[str, Any]]):
        self._ensure_dir()
        temp_path = f"{self.file_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump({"version": "2026-10-19", "items": items}, f, indent=2)
        os.replace(temp_path, self.file_path)

    def purge_expired(self, now: Optional[float] = None) -> int:
        """Purge items whose expires_at is before now. Returns count of purged items."""
        now_dt = self._now(now)
        items = self._load_raw()
        valid = []
        purged = 0
        for item in items:
            exp_str = item.get("expires_at")
            if not exp_str:
                valid.append(item)
                continue
            try:
                exp_dt = datetime.fromisoformat(exp_str)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if exp_dt >= now_dt:
                    valid.append(item)
                else:
                    purged += 1
            except Exception:
                valid.append(item)
        if purged > 0:
            self._save_raw(valid)
        return purged

    def add_unmatched(self, item: SignalItem, reason: str = "unclear_company", now: Optional[float] = None) -> Dict[str, Any]:
        """Add an unclear signal item to the hold store with a 14-day TTL."""
        self.purge_expired(now=now)
        now_dt = self._now(now)
        expires_dt = now_dt + timedelta(seconds=self.ttl_seconds)

        url_hash = hashlib.sha256(str(item.evidence_url or "").encode("utf-8")).hexdigest()[:8]
        record_id = f"unmatched-{int(now_dt.timestamp())}-{url_hash}"

        item_dict = asdict(item)
        # Drop complex internal metadata
        item_dict.pop("metadata", None)

        record = {
            "id": record_id,
            "added_at": now_dt.isoformat(),
            "expires_at": expires_dt.isoformat(),
            "reason": reason,
            "item": item_dict,
        }

        items = self._load_raw()
        # Avoid duplicate hold records for the exact same evidence URL
        for existing in items:
            if existing.get("item", {}).get("evidence_url") == item.evidence_url:
                existing["expires_at"] = expires_dt.isoformat()
                existing["reason"] = reason
                self._save_raw(items)
                return existing

        items.append(record)
        self._save_raw(items)
        return record

    def list_items(self, include_expired: bool = False, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """List hold records."""
        if not include_expired:
            self.purge_expired(now=now)
        return self._load_raw()

    def format_hub_intake(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Format Hub intake task parameters for project orin.

        Unmatched items are filed to project 'orin' for human triage, not Alexey.
        """
        item = record.get("item", {})
        title = f"Unmatched signal: {item.get('company_name') or item.get('evidence_url') or record.get('id')}"
        key = record.get("id") or f"unmatched-{hashlib.sha256(str(item.get('evidence_url')).encode()).hexdigest()[:12]}"

        text_content = (
            f"Unmatched Company Signal (Hold TTL ~14d)\n"
            f"Hold ID: {record.get('id')}\n"
            f"Reason: {record.get('reason')}\n"
            f"Added At: {record.get('added_at')}\n"
            f"Expires At: {record.get('expires_at')}\n"
            f"Kind: {item.get('kind')}\n"
            f"Source: {item.get('source')}\n"
            f"Evidence: {item.get('evidence_url')}\n"
            f"Amount: {item.get('amount') or 'N/A'}\n"
            f"Round: {item.get('round') or 'N/A'}\n\n"
            f"Summary:\n{item.get('signal_summary') or ''}\n\n"
            f"Raw Text:\n{item.get('raw_text') or ''}\n"
        )

        cli_command = (
            f'hub intake create --project orin '
            f'--title "{title[:100]}" '
            f'--source-type forwarded '
            f'--source-reference "dapier:crm-signals" '
            f'--key "{key}"'
        )

        return {
            "project": "orin",
            "title": title,
            "key": key,
            "source_type": "forwarded",
            "source_reference": "dapier:crm-signals",
            "text": text_content,
            "cli_command": cli_command,
        }
