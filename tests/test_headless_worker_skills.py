"""Extra skill dirs reach worker agent sessions without machine-wide leakage."""

import os

from src.dapier import headless_worker
from src.dapier.headless_worker import ensure_workspace_skills, env_skill_dirs


def _with_entries(path, names):
    os.makedirs(path, exist_ok=True)
    for name in names:
        os.makedirs(os.path.join(path, name), exist_ok=True)
    return path


def _linked(skills_dir):
    return {entry.name: os.readlink(entry) for entry in os.scandir(skills_dir)
            if entry.is_symlink()}


def test_extras_link_beside_pool_replacing_the_symlink(tmp_path):
    pool = _with_entries(tmp_path / "pool", ["fetch-zoom", "prose-write"])
    labs = _with_entries(tmp_path / "labs", ["ai-shipping-labs-event-recaps"])
    workspace = tmp_path / "ws"
    skills = workspace / ".claude" / "skills"
    skills.parent.mkdir(parents=True)
    skills.symlink_to(pool)

    ensure_workspace_skills(workspace, [str(labs)], pool=str(pool))

    assert not skills.is_symlink() and skills.is_dir()
    linked = _linked(skills)
    assert linked["fetch-zoom"] == str(pool / "fetch-zoom")
    assert linked["ai-shipping-labs-event-recaps"] == str(labs / "ai-shipping-labs-event-recaps")


def test_without_extras_the_setup_is_untouched(tmp_path):
    pool = _with_entries(tmp_path / "pool", ["fetch-zoom"])
    workspace = tmp_path / "ws"
    skills = workspace / ".claude" / "skills"
    skills.parent.mkdir(parents=True)
    skills.symlink_to(pool)

    ensure_workspace_skills(workspace, [], pool=str(pool))

    assert skills.is_symlink() and os.readlink(skills) == str(pool)


def test_pool_wins_clashes_and_hand_placed_entries_survive(tmp_path):
    pool = _with_entries(tmp_path / "pool", ["shared"])
    labs = _with_entries(tmp_path / "labs", ["shared"])
    workspace = tmp_path / "ws"
    skills = workspace / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "manual").mkdir()

    ensure_workspace_skills(workspace, [str(labs)], pool=str(pool))

    linked = _linked(skills)
    assert linked["shared"] == str(pool / "shared")
    assert (skills / "manual").is_dir() and not (skills / "manual").is_symlink()


def test_removed_and_added_skills_settle_on_the_next_job(tmp_path):
    pool = _with_entries(tmp_path / "pool", ["fetch-zoom"])
    labs = _with_entries(tmp_path / "labs", ["ai-shipping-labs-event-recaps"])
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ensure_workspace_skills(workspace, [str(labs)], pool=str(pool))

    (labs / "ai-shipping-labs-event-recaps").rmdir()
    _with_entries(labs, ["ai-shipping-labs-slack"])
    ensure_workspace_skills(workspace, [str(labs)], pool=str(pool))

    linked = _linked(workspace / ".claude" / "skills")
    assert "ai-shipping-labs-event-recaps" not in linked
    assert linked["ai-shipping-labs-slack"] == str(labs / "ai-shipping-labs-slack")


def test_unresolvable_sources_change_nothing(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    ensure_workspace_skills(workspace, [str(tmp_path / "missing")],
                            pool=str(tmp_path / "missing"))

    assert not (workspace / ".claude").exists()


def test_env_skill_dirs_split_and_expand(monkeypatch):
    monkeypatch.setenv("DAPIER_SKILL_DIRS", "~/labs : /opt/skills::")
    assert env_skill_dirs() == [os.path.expanduser("~/labs"), "/opt/skills"]
    monkeypatch.setenv("DAPIER_SKILL_DIRS", "")
    assert env_skill_dirs() == []
    monkeypatch.delenv("DAPIER_SKILL_DIRS", raising=False)
    assert env_skill_dirs({}) == []


class _Api:
    def __init__(self):
        self.finished = None

    def call(self, operation, body=None):
        self.finished = body
        return {}


class _Process:
    pid = 4242

    def __init__(self):
        self.stdin = self._Stdin()

    class _Stdin:
        def write(self, data):
            return len(data)

        def close(self):
            pass

    def poll(self):
        return 0

    def wait(self):
        return 0


def test_run_job_wires_skills_into_the_job_workspace(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(headless_worker, "ensure_workspace_skills",
                        lambda workspace, extras: seen.append((workspace, extras)))
    monkeypatch.setattr(headless_worker, "env_skill_dirs", lambda: ["/opt/skills"])
    (tmp_path / "proj").mkdir()
    api = _Api()

    headless_worker.run_job(
        {"task_id": "t", "lease_id": "l", "prompt": "work", "workspace": "proj"},
        api, workspace_root=str(tmp_path), popen=lambda *args, **kwargs: _Process(),
        clock=lambda: 0.0, sleep=lambda seconds: None)

    assert api.finished["status"] == "succeeded"
    assert len(seen) == 1
    workspace, extras = seen[0]
    assert str(workspace) == str(tmp_path / "proj")
    assert extras == ["/opt/skills"]
