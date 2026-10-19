"""Client interfaces for DataOps Sponsor CRM (organizations and notes)."""

import abc
from datetime import datetime, timezone
import json
import logging
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DataOpsCRMClient(abc.ABC):
    """Abstract base class for DataOps Sponsor CRM operations."""

    @abc.abstractmethod
    def find_organization_by_domain(self, domain: str) -> Optional[Dict[str, Any]]:
        """Look up an organization by its normalized domain."""
        raise NotImplementedError

    @abc.abstractmethod
    def create_organization(self, name: str, domain: str) -> Dict[str, Any]:
        """Create a new organization in the CRM."""
        raise NotImplementedError

    @abc.abstractmethod
    def add_note_to_organization(self, org_id: str, body: str, source_key: Optional[str] = None) -> Dict[str, Any]:
        """Append a note to an organization, optionally keyed by source_key."""
        raise NotImplementedError

    @abc.abstractmethod
    def list_organization_notes(self, org_id: str) -> List[Dict[str, Any]]:
        """List existing notes on an organization."""
        raise NotImplementedError

    @abc.abstractmethod
    def create_contact(self, org_id: str, contact_data: Dict[str, Any]) -> Dict[str, Any]:
        """Store a contact linked to an organization in the CRM."""
        raise NotImplementedError

    @abc.abstractmethod
    def list_organization_contacts(self, org_id: str) -> List[Dict[str, Any]]:
        """List contacts linked to an organization."""
        raise NotImplementedError


class MockDataOpsClient(DataOpsCRMClient):
    """Hermetic in-memory DataOps CRM client for testing."""

    def __init__(self):
        self.organizations: Dict[str, Dict[str, Any]] = {}
        self.notes: Dict[str, List[Dict[str, Any]]] = {}
        self.contacts: Dict[str, List[Dict[str, Any]]] = {}
        self._org_counter = 0
        self._note_counter = 0
        self._contact_counter = 0

    def find_organization_by_domain(self, domain: str) -> Optional[Dict[str, Any]]:
        clean_domain = str(domain or "").strip().lower()
        if not clean_domain:
            return None
        for org in self.organizations.values():
            org_domain = str(org.get("domain") or org.get("website") or "").strip().lower()
            if org_domain == clean_domain:
                return dict(org)
        return None

    def create_organization(self, name: str, domain: str) -> Dict[str, Any]:
        self._org_counter += 1
        org_id = f"org_{self._org_counter}"
        record = {
            "id": org_id,
            "displayName": name.strip(),
            "domain": domain.strip().lower(),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        self.organizations[org_id] = record
        self.notes.setdefault(org_id, [])
        self.contacts.setdefault(org_id, [])
        return dict(record)

    def add_note_to_organization(self, org_id: str, body: str, source_key: Optional[str] = None) -> Dict[str, Any]:
        if org_id not in self.organizations:
            raise ValueError(f"Organization {org_id} does not exist")
        self._note_counter += 1
        note_id = f"note_{self._note_counter}"
        note_record = {
            "id": note_id,
            "organizationId": org_id,
            "body": body,
            "sourceKey": source_key,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        self.notes.setdefault(org_id, []).append(note_record)
        return dict(note_record)

    def list_organization_notes(self, org_id: str) -> List[Dict[str, Any]]:
        return [dict(n) for n in self.notes.get(org_id, [])]

    def create_contact(self, org_id: str, contact_data: Dict[str, Any]) -> Dict[str, Any]:
        if org_id not in self.organizations:
            raise ValueError(f"Organization {org_id} does not exist")
        self._contact_counter += 1
        contact_id = f"contact_{self._contact_counter}"
        record = dict(contact_data)
        record["id"] = contact_id
        record["organizationId"] = org_id
        record["company"] = org_id
        record["createdAt"] = datetime.now(timezone.utc).isoformat()
        self.contacts.setdefault(org_id, []).append(record)
        return dict(record)

    def list_organization_contacts(self, org_id: str) -> List[Dict[str, Any]]:
        return [dict(c) for c in self.contacts.get(org_id, [])]


class DataOpsCliClient(DataOpsCRMClient):
    """DataOps CRM client that runs `dataops crm` CLI commands on the host."""

    def __init__(self, cli_path: Optional[str] = None):
        self.cli_path = cli_path or shutil.which("dataops") or "dataops"

    def _run(self, cmd: List[str]) -> Any:
        full_cmd = [self.cli_path] + cmd
        proc = subprocess.run(full_cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"DataOps CLI error (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
            )
        output = proc.stdout.strip()
        if not output:
            return {}
        try:
            return json.loads(output)
        except Exception:
            return {"output": output}

    def find_organization_by_domain(self, domain: str) -> Optional[Dict[str, Any]]:
        clean_domain = str(domain or "").strip().lower()
        if not clean_domain:
            return None
        res = self._run(["crm", "companies", "list", "--search", clean_domain, "--json"])
        items = res.get("items", []) if isinstance(res, dict) else []
        for item in items:
            dom = str(item.get("domain") or item.get("website") or item.get("displayName") or "").strip().lower()
            if clean_domain in dom:
                return item
        return None

    def create_organization(self, name: str, domain: str) -> Dict[str, Any]:
        payload = json.dumps({"displayName": name.strip(), "domain": domain.strip().lower()})
        return self._run(["crm", "company", "create", "--data", payload, "--json"])

    def add_note_to_organization(self, org_id: str, body: str, source_key: Optional[str] = None) -> Dict[str, Any]:
        cmd = ["crm", "note", "add", "organization", org_id, "--body", body, "--json"]
        if source_key:
            cmd.extend(["--source-key", source_key])
        return self._run(cmd)

    def list_organization_notes(self, org_id: str) -> List[Dict[str, Any]]:
        res = self._run(["crm", "notes", "list", "organization", org_id, "--json"])
        if isinstance(res, dict) and "items" in res:
            return res["items"]
        if isinstance(res, list):
            return res
        return []

    def create_contact(self, org_id: str, contact_data: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(contact_data)
        payload["company"] = org_id
        return self._run(["sponsors", "contact", "add", "--company", org_id, "--data", json.dumps(payload), "--json"])

    def list_organization_contacts(self, org_id: str) -> List[Dict[str, Any]]:
        res = self._run(["sponsors", "contact", "list", "--company", org_id, "--json"])
        if isinstance(res, dict) and "items" in res:
            return res["items"]
        if isinstance(res, list):
            return res
        return []

