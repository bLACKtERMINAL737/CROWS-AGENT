"""Declarative, reviewable skill manifests.

Skills are metadata and tool allowlists only. This module deliberately does not
load Python, shell, prompt, or other executable content from skill directories.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .file_lock import file_lock


_SKILL_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$")
def _version_key(version: str) -> tuple[int, int, int, bool, str]:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise SkillError("Invalid skill version")
    major, minor, patch = match.groups()
    prerelease = version.partition("-")[2] or None
    # Stable releases sort after prereleases of the same numeric version.
    return int(major), int(minor), int(patch), prerelease is None, prerelease or ""


_PERMISSION_FOR_TOOL = {
    "workspace.list_files": "workspace.read",
    "workspace.read_file": "workspace.read",
    "workspace.search_text": "workspace.read",
    "web.search": "web.read",
    "web.open": "web.read",
}


class SkillError(ValueError):
    """A safe, user-actionable skill registry error."""


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=2, max_length=63)
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=5, max_length=64)
    description: str = Field(min_length=1, max_length=1000)
    tools: list[str] = Field(min_length=1, max_length=12)
    permissions: list[Literal["workspace.read", "web.read"]] = Field(default_factory=list, max_length=2)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _SKILL_ID.fullmatch(value):
            raise ValueError("id must be a lowercase slug containing letters, digits, and hyphens")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not _VERSION.fullmatch(value):
            raise ValueError("version must use semantic version format, such as 1.0.0")
        return value

    @field_validator("tools")
    @classmethod
    def validate_tools(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("tools must not contain duplicates")
        return value

    @field_validator("permissions")
    @classmethod
    def validate_permissions(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("permissions must not contain duplicates")
        return value


class SkillRegistry:
    """Stores immutable skill versions and an explicit active-version index."""

    def __init__(self, root: Path, allowed_tools: set[str]):
        self.root = root.expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise SkillError("Skills directory must not be a symlink")
        self.root = self.root.resolve()
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        self.allowed_tools = set(allowed_tools)
        self.state_path = self.root / "active.json"

    @contextmanager
    def _state_lock(self):
        lock_path = self.root / ".active.lock"
        fd = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with file_lock(fd):
                yield
        finally:
            os.close(fd)

    def _skill_dir(self, skill_id: str) -> Path:
        if not _SKILL_ID.fullmatch(skill_id):
            raise SkillError("Invalid skill ID")
        path = self.root / skill_id
        if path.is_symlink():
            raise SkillError("Skill paths must not be symlinks")
        return path

    def _manifest_path(self, skill_id: str, version: str) -> Path:
        if not _VERSION.fullmatch(version):
            raise SkillError("Invalid skill version")
        directory = self._skill_dir(skill_id)
        path = directory / f"{version}.json"
        if path.is_symlink():
            raise SkillError("Skill manifests must not be symlinks")
        return path

    def _validate_capabilities(self, manifest: SkillManifest) -> None:
        unknown = sorted(set(manifest.tools) - self.allowed_tools)
        if unknown:
            raise SkillError(f"Skill references unavailable tools: {', '.join(unknown)}")
        required = {_PERMISSION_FOR_TOOL[name] for name in manifest.tools}
        missing = sorted(required - set(manifest.permissions))
        if missing:
            raise SkillError(f"Skill is missing required permissions: {', '.join(missing)}")

    def _read_state(self) -> dict[str, str]:
        if self.state_path.is_symlink():
            raise SkillError("Skill state file must not be a symlink")
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillError("Skill activation state is unreadable; inspect the registry before changing it") from exc
        if not isinstance(data, dict) or any(
            not isinstance(key, str) or not _SKILL_ID.fullmatch(key)
            or not isinstance(value, str) or not _VERSION.fullmatch(value)
            for key, value in data.items()
        ):
            raise SkillError("Skill activation state is invalid")
        return data

    def _write_json_atomic(self, path: Path, payload: dict) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".crows-skill-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def register(self, raw_manifest: SkillManifest | dict) -> dict:
        try:
            manifest = raw_manifest if isinstance(raw_manifest, SkillManifest) else SkillManifest.model_validate(raw_manifest)
        except Exception as exc:
            raise SkillError("Skill manifest is invalid") from exc
        self._validate_capabilities(manifest)
        directory = self._skill_dir(manifest.id)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._manifest_path(manifest.id, manifest.version)
        encoded = json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SkillError("Existing skill version is unreadable") from exc
            if existing != manifest.model_dump(mode="json"):
                raise SkillError("That skill version already exists with different content")
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        return self.get(manifest.id, manifest.version)

    def get(self, skill_id: str, version: str | None = None) -> dict:
        active = self._read_state()
        selected = version or active.get(skill_id)
        if selected is None:
            versions = self.versions(skill_id)
            if not versions:
                raise SkillError("Skill not found")
            selected = max(versions, key=_version_key)
        path = self._manifest_path(skill_id, selected)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            manifest = SkillManifest.model_validate(payload)
            self._validate_capabilities(manifest)
        except FileNotFoundError as exc:
            raise SkillError("Skill version not found") from exc
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise SkillError("Skill manifest is invalid or unreadable") from exc
        return {**manifest.model_dump(mode="json"), "enabled": active.get(skill_id) == selected}

    def versions(self, skill_id: str) -> list[str]:
        directory = self._skill_dir(skill_id)
        if not directory.exists():
            return []
        if not directory.is_dir():
            raise SkillError("Skill path is not a directory")
        return sorted(
            (path.stem for path in directory.iterdir()
             if path.is_file() and not path.is_symlink()
             and path.suffix == ".json" and _VERSION.fullmatch(path.stem)),
            key=_version_key,
        )

    def list(self) -> list[dict]:
        items = []
        for directory in sorted(self.root.iterdir(), key=lambda item: item.name):
            if directory.is_symlink() or not directory.is_dir() or not _SKILL_ID.fullmatch(directory.name):
                continue
            for version in self.versions(directory.name):
                items.append(self.get(directory.name, version))
        return items

    def activate(self, skill_id: str, version: str, *, approved: bool) -> dict:
        if approved is not True:
            raise SkillError("Explicit approval is required before activating a skill")
        skill = self.get(skill_id, version)
        with self._state_lock():
            state = self._read_state()
            state[skill_id] = version
            self._write_json_atomic(self.state_path, state)
        return self.get(skill_id, version)

    def resolve_runtime_profile(self, skill_ids: list[str]) -> dict:
        """Resolve explicitly selected, active skills into a bounded tool capability profile."""
        if not isinstance(skill_ids, list) or not 1 <= len(skill_ids) <= 5:
            raise SkillError("Select between one and five skills")
        if len(skill_ids) != len(set(skill_ids)):
            raise SkillError("Skill selection must not contain duplicates")

        active = self._read_state()
        manifests = []
        allowed_tools: set[str] = set()
        permissions: set[str] = set()
        for skill_id in skill_ids:
            if not isinstance(skill_id, str) or not _SKILL_ID.fullmatch(skill_id):
                raise SkillError("Invalid skill ID")
            version = active.get(skill_id)
            if version is None:
                raise SkillError(f"Skill '{skill_id}' is not active")
            manifest = self.get(skill_id, version)
            if manifest.get("enabled") is not True:
                raise SkillError(f"Skill '{skill_id}' is not active")
            manifests.append(manifest)
            allowed_tools.update(manifest["tools"])
            permissions.update(manifest["permissions"])

        return {
            "skills": manifests,
            "tools": sorted(allowed_tools),
            "permissions": sorted(permissions),
        }

    def disable(self, skill_id: str) -> dict:
        with self._state_lock():
            state = self._read_state()
            if skill_id not in state:
                raise SkillError("Skill is already disabled or not registered")
            del state[skill_id]
            self._write_json_atomic(self.state_path, state)
        return {"id": skill_id, "enabled": False}
