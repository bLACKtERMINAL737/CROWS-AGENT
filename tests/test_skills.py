from pathlib import Path

import pytest

from crows.skills import SkillError, SkillRegistry


TOOLS = {"workspace.list_files", "workspace.read_file", "workspace.search_text", "web.search", "web.open"}


def manifest(**updates):
    value = {
        "id": "repo-review",
        "name": "Repository review",
        "version": "1.0.0",
        "description": "Inspect a repository using read-only tools.",
        "tools": ["workspace.list_files", "workspace.read_file"],
        "permissions": ["workspace.read"],
    }
    value.update(updates)
    return value


def test_registers_disabled_manifest_and_activates_only_after_explicit_approval(tmp_path: Path):
    registry = SkillRegistry(tmp_path / "skills", TOOLS)
    saved = registry.register(manifest())
    assert saved["enabled"] is False
    assert registry.list() == [saved]

    with pytest.raises(SkillError, match="Explicit approval"):
        registry.activate("repo-review", "1.0.0", approved=False)
    active = registry.activate("repo-review", "1.0.0", approved=True)
    assert active["enabled"] is True
    assert registry.get("repo-review")["enabled"] is True


def test_versions_are_immutable_and_rollback_is_explicit(tmp_path: Path):
    registry = SkillRegistry(tmp_path / "skills", TOOLS)
    registry.register(manifest())
    registry.register(manifest(version="1.1.0", description="Improved read-only review."))
    registry.register(manifest(version="1.10.0", description="Latest read-only review."))
    registry.activate("repo-review", "1.0.0", approved=True)
    registry.activate("repo-review", "1.10.0", approved=True)
    assert registry.get("repo-review", "1.0.0")["enabled"] is False
    assert registry.get("repo-review", "1.1.0")["enabled"] is False
    assert registry.get("repo-review", "1.10.0")["enabled"] is True

    with pytest.raises(SkillError, match="different content"):
        registry.register(manifest(description="Changed without a version bump."))

    assert registry.activate("repo-review", "1.0.0", approved=True)["enabled"] is True


def test_disable_is_persistent(tmp_path: Path):
    registry = SkillRegistry(tmp_path / "skills", TOOLS)
    registry.register(manifest())
    registry.activate("repo-review", "1.0.0", approved=True)
    assert registry.disable("repo-review") == {"id": "repo-review", "enabled": False}
    assert registry.get("repo-review", "1.0.0")["enabled"] is False


@pytest.mark.parametrize(
    "payload",
    [
        manifest(id="../escape"),
        manifest(version="../../active"),
        manifest(extra_code="print('unsafe')"),
        manifest(tools=["shell.run"], permissions=["workspace.read"]),
        manifest(tools=["web.open"], permissions=[]),
        manifest(tools=["workspace.read_file", "workspace.read_file"]),
    ],
)
def test_rejects_unsafe_or_invalid_manifests(tmp_path: Path, payload):
    registry = SkillRegistry(tmp_path / "skills", TOOLS)
    with pytest.raises((SkillError, ValueError)):
        registry.register(payload)


def test_rejects_symlinked_manifest_and_state(tmp_path: Path):
    registry = SkillRegistry(tmp_path / "skills", TOOLS)
    registry.register(manifest())
    skill_dir = tmp_path / "skills" / "repo-review"
    (skill_dir / "1.2.0.json").symlink_to(skill_dir / "1.0.0.json")
    with pytest.raises(SkillError, match="symlink"):
        registry.get("repo-review", "1.2.0")

    registry.state_path.symlink_to(skill_dir / "1.0.0.json")
    with pytest.raises(SkillError, match="symlink"):
        registry.disable("repo-review")


def test_runtime_profile_contains_only_explicitly_selected_active_skill_tools(tmp_path: Path):
    registry = SkillRegistry(tmp_path / "skills", TOOLS)
    registry.register(manifest())
    registry.register(manifest(
        id="web-research",
        name="Web research",
        version="1.0.0",
        tools=["web.search"],
        permissions=["web.read"],
    ))
    with pytest.raises(SkillError, match="not active"):
        registry.resolve_runtime_profile(["repo-review"])

    registry.activate("repo-review", "1.0.0", approved=True)
    profile = registry.resolve_runtime_profile(["repo-review"])
    assert profile["tools"] == ["workspace.list_files", "workspace.read_file"]
    assert profile["permissions"] == ["workspace.read"]
    assert profile["skills"][0]["enabled"] is True

    with pytest.raises(SkillError, match="not active"):
        registry.resolve_runtime_profile(["repo-review", "web-research"])


def test_runtime_profile_rejects_duplicate_or_excessive_skill_selection(tmp_path: Path):
    registry = SkillRegistry(tmp_path / "skills", TOOLS)
    registry.register(manifest())
    registry.activate("repo-review", "1.0.0", approved=True)
    with pytest.raises(SkillError, match="duplicates"):
        registry.resolve_runtime_profile(["repo-review", "repo-review"])
    with pytest.raises(SkillError, match="one and five"):
        registry.resolve_runtime_profile([])
    with pytest.raises(SkillError, match="one and five"):
        registry.resolve_runtime_profile(["repo-review"] * 6)
