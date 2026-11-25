"""An Expert's chosen Skills reach Workers in containers the native way.

A container's home is a folder of this computer mounted into it, and the Worker inside can write
there. The runtime places each Skill from outside, through no-follow descriptors: Claude loads it as
a plug-in folder, Codex finds it in HOME/.agents/skills. Synthetic Skills only; no container runs."""
import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from workers_projects_runtime.profile_runtime import (
    ClaudeCodeRuntime, CodexCliRuntime, RuntimeErrorBase, _place_expert_skills,
)

SKILL = {"name": "synthetic-brief", "files": [
    {"path": "SKILL.md", "content": "---\nname: synthetic-brief\ndescription: Write a synthetic brief\n---\nWrite it.\n"},
    {"path": "reference/notes.md", "content": "Synthetic notes.\n"},
]}


def container_worker(worker_id, profile, skills):
    return {"worker_id": worker_id, "name": "Container", "profile": profile, "execution_mode": "docker",
            "bootstrap_bundle_json": json.dumps({"expert_skills": skills})}


def pairs(command):
    return [command[i:i + 2] for i in range(len(command) - 1)]


def test_a_container_claude_worker_loads_the_experts_skill_from_its_own_home(tmp_path):
    runtime = ClaudeCodeRuntime(str(tmp_path / "data"))
    worker = container_worker("wrk_container_claude", "claude-code", [SKILL])
    command, _ = runtime._build_command(worker, "Do the work.", runtime._runtime_info(worker))
    inside = f"{runtime.sandbox.home_mount}/.glasshive/expert-skills/synthetic-brief"
    assert ["--plugin-dir", inside] in pairs(command)
    placed = runtime._home_dir("wrk_container_claude") / ".glasshive" / "expert-skills" / "synthetic-brief"
    assert (placed / "SKILL.md").read_text() == SKILL["files"][0]["content"]
    assert (placed / "reference" / "notes.md").read_text() == "Synthetic notes.\n"
    assert stat.S_IMODE((placed / "SKILL.md").stat().st_mode) == 0o600
    # A Worker without Skills gets no plug-in folder and nothing is written into its home.
    plain = container_worker("wrk_container_plain", "claude-code", [])
    command, _ = runtime._build_command(plain, "Do the work.", runtime._runtime_info(plain))
    assert "--plugin-dir" not in command
    assert not (runtime._home_dir("wrk_container_plain") / ".glasshive" / "expert-skills").exists()


def test_a_resumed_container_claude_worker_keeps_its_skills(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(str(tmp_path / "data"))
    seen = {}
    monkeypatch.setattr(runtime, "_provider_session_starts_fresh", lambda worker: False)
    monkeypatch.setattr(runtime, "_read_provider_session_key", lambda worker: "synthetic-session")

    def both(worker, *, resume_command, fresh_command):
        seen.update(resume=resume_command, fresh=fresh_command)
        return resume_command

    monkeypatch.setattr(runtime, "_resume_with_fresh_fallback", both)
    worker = container_worker("wrk_container_resume", "claude-code", [SKILL])
    runtime._build_command(worker, "Continue.", runtime._runtime_info(worker))
    inside = f"{runtime.sandbox.home_mount}/.glasshive/expert-skills/synthetic-brief"
    assert ["--plugin-dir", inside] in pairs(seen["resume"]) and ["--plugin-dir", inside] in pairs(seen["fresh"])


def test_a_container_codex_worker_finds_the_experts_skill_in_its_own_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CODEX_DANGEROUS", "0")
    runtime = CodexCliRuntime(str(tmp_path / "data"))
    worker = container_worker("wrk_container_codex", "codex-cli", [SKILL])
    runtime._build_command(worker, "Do the work.", runtime._runtime_info(worker))
    skills = runtime._home_dir("wrk_container_codex") / ".agents" / "skills"
    assert (skills / "synthetic-brief" / "SKILL.md").read_text() == SKILL["files"][0]["content"]
    record = runtime._state_dir("wrk_container_codex") / "expert-skills-codex.json"
    assert list(json.loads(record.read_text())["placed"]) == ["synthetic-brief"]  # in the runtime's own state
    assert not (skills / ".expert-skills.json").exists()
    # The next start without that Skill removes only what was placed; a Skill the Worker made stays.
    (skills / "worker-own").mkdir()
    (skills / "worker-own" / "SKILL.md").write_text("own")
    later = container_worker("wrk_container_codex", "codex-cli", [])
    runtime._build_command(later, "Do the work.", runtime._runtime_info(later))
    assert not (skills / "synthetic-brief").exists()
    assert (skills / "worker-own" / "SKILL.md").read_text() == "own"


def test_the_clean_room_boundary_carries_only_well_formed_skills():
    from workers_projects_runtime.store import canonical_expert_skills, canonical_parallel_clean_room_bootstrap
    kept = canonical_parallel_clean_room_bootstrap({"expert_skills": [SKILL], "unknown_context": "dropped"})
    assert kept == {"expert_skills": [SKILL]}
    bad = [
        "not a list",
        [{"name": "../escape", "files": SKILL["files"]}],
        [{"name": "synthetic", "files": [{"path": "/etc/passwd", "content": "x"}]}],
        [{"name": "synthetic", "files": [{"path": "../outside.md", "content": "x"}]}],
        [{"name": "synthetic", "files": [{"path": "SKILL.md", "content": 7}]}],
        [{"name": "synthetic", "files": [{"path": "SKILL.md", "content": "x", "mode": "0755"}]}],
        [{"name": "synthetic", "files": []}],
        [{"name": "synthetic", "files": [{"path": "SKILL.md", "content": "x" * (1024 * 1024 + 1)}]}],
    ]
    for value in bad:
        with pytest.raises(ValueError):
            canonical_expert_skills(value)


def test_placing_skills_refuses_links_and_folders_it_did_not_place(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    record = tmp_path / "state" / "expert-skills-codex.json"
    # A link planted above a shared Skills folder redirects neither a write nor a removal.
    planted = tmp_path / "planted"
    (planted / ".agents").mkdir(parents=True)
    os.symlink(outside, planted / ".agents" / "skills")
    with pytest.raises(RuntimeErrorBase, match="placed safely"):
        _place_expert_skills(planted, Path(".agents/skills"), [SKILL], record=record)
    assert sorted(path.name for path in outside.iterdir()) == ["keep.txt"]
    # A folder of the same name that the Worker made itself is refused, not overwritten.
    own = tmp_path / "own"
    (own / ".agents" / "skills" / "synthetic-brief").mkdir(parents=True)
    (own / ".agents" / "skills" / "synthetic-brief" / "SKILL.md").write_text("mine")
    with pytest.raises(RuntimeErrorBase, match="already used"):
        _place_expert_skills(own, Path(".agents/skills"), [SKILL], record=tmp_path / "state-own" / "record.json")
    assert (own / ".agents" / "skills" / "synthetic-brief" / "SKILL.md").read_text() == "mine"
    # A placed folder later swapped for a link is not what the runtime placed: never followed, never removed,
    # and the Skill is not placed over it; the place it points to is untouched.
    swapped = tmp_path / "swapped"
    swapped_record = tmp_path / "state-swapped" / "record.json"
    assert _place_expert_skills(swapped, Path(".agents/skills"), [SKILL], record=swapped_record) == ["synthetic-brief"]
    shutil.rmtree(swapped / ".agents" / "skills" / "synthetic-brief")
    os.symlink(outside, swapped / ".agents" / "skills" / "synthetic-brief")
    assert _place_expert_skills(swapped, Path(".agents/skills"), [], record=swapped_record) == []
    assert os.path.islink(swapped / ".agents" / "skills" / "synthetic-brief")
    with pytest.raises(RuntimeErrorBase, match="already used"):
        _place_expert_skills(swapped, Path(".agents/skills"), [SKILL], record=swapped_record)
    assert sorted(path.name for path in outside.iterdir()) == ["keep.txt"]
    # The runtime's own Skills folder (Claude's) is replaced whole: a link planted in its place is unlinked.
    owned = tmp_path / "owned"
    (owned / ".glasshive").mkdir(parents=True)
    os.symlink(outside, owned / ".glasshive" / "expert-skills")
    assert _place_expert_skills(owned, Path(".glasshive/expert-skills"), [SKILL]) == ["synthetic-brief"]
    assert not (owned / ".glasshive" / "expert-skills").is_symlink()
    assert sorted(path.name for path in outside.iterdir()) == ["keep.txt"]
    # A Skill carrying a native plug-in part is refused before anything is written.
    with pytest.raises(RuntimeErrorBase):
        _place_expert_skills(tmp_path / "bad", Path(".glasshive/expert-skills"), [
            {"name": "bad", "files": [{"path": "SKILL.md", "content": "x"}, {"path": "hooks/hooks.json", "content": "{}"}]}])
    assert not (tmp_path / "bad").exists()
