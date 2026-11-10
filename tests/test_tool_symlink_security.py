from pathlib import Path

from crows.tools import ToolRegistry


def test_search_skips_symlinked_file_outside_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("needle secret-value", encoding="utf-8")
    (workspace / "linked.txt").symlink_to(secret)

    result = ToolRegistry(workspace).execute("workspace.search_text", {"query": "needle"})

    assert result["matches"] == []


def test_search_does_not_traverse_symlinked_directory_outside_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle secret-value", encoding="utf-8")
    (workspace / "linked-dir").symlink_to(outside, target_is_directory=True)

    result = ToolRegistry(workspace).execute("workspace.search_text", {"query": "needle"})

    assert result["matches"] == []


def test_search_reads_regular_workspace_file_with_descriptor_based_open(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("needle stays inside", encoding="utf-8")

    result = ToolRegistry(workspace).execute("workspace.search_text", {"query": "needle"})

    assert result["matches"] == [
        {"file": "notes.txt", "line": 1, "text": "needle stays inside"}
    ]


def test_search_skips_symlinked_file_even_when_nested_in_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "actual.txt"
    target.write_text("needle target", encoding="utf-8")
    (workspace / "alias.txt").symlink_to(target)

    result = ToolRegistry(workspace).execute("workspace.search_text", {"query": "needle"})

    assert [match["file"] for match in result["matches"]] == ["actual.txt"]



def test_list_files_skips_symlinked_file_and_directory(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "real.txt").write_text("safe", encoding="utf-8")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret", encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "linked-file.txt").symlink_to(outside_file)
    (workspace / "linked-dir").symlink_to(outside_dir, target_is_directory=True)

    result = ToolRegistry(workspace).execute("workspace.list_files", {"path": "."})

    names = {entry["name"] for entry in result["entries"]}
    assert "real.txt" in names
    assert "linked-file.txt" not in names
    assert "linked-dir" not in names



def test_read_rejects_symlink_to_file_inside_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "actual.txt"
    target.write_text("secret target", encoding="utf-8")
    (workspace / "alias.txt").symlink_to(target)

    registry = ToolRegistry(workspace)
    from crows.tools import ToolError
    import pytest

    with pytest.raises(ToolError, match="Symlink paths are not supported"):
        registry.execute("workspace.read_file", {"path": "alias.txt"})


def test_read_rejects_symlinked_parent_directory_inside_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_dir = workspace / "real"
    real_dir.mkdir()
    (real_dir / "notes.txt").write_text("secret target", encoding="utf-8")
    (workspace / "alias-dir").symlink_to(real_dir, target_is_directory=True)

    registry = ToolRegistry(workspace)
    from crows.tools import ToolError
    import pytest

    with pytest.raises(ToolError, match="Symlink paths are not supported"):
        registry.execute("workspace.read_file", {"path": "alias-dir/notes.txt"})



def test_search_excludes_credential_directories_case_insensitively(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for relative in (".SSH/private.json", ".AWS/credentials.json",
                     ".Azure/tokens.json", ".GNUPG/private.json"):
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"needle": "secret-value"}', encoding="utf-8")
    (workspace / "safe.json").write_text('{"needle": "safe-value"}', encoding="utf-8")

    result = ToolRegistry(workspace).execute("workspace.search_text", {"query": "needle"})

    assert [match["file"] for match in result["matches"]] == ["safe.json"]


def test_search_rejects_explicit_credential_directory_path(tmp_path: Path):
    import pytest
    from crows.tools import ToolError

    workspace = tmp_path / "workspace"
    secret_dir = workspace / ".AWS"
    secret_dir.mkdir(parents=True)
    (secret_dir / "tokens.json").write_text('{"token": "secret"}', encoding="utf-8")

    with pytest.raises(ToolError, match="may contain credentials"):
        ToolRegistry(workspace).execute(
            "workspace.search_text", {"query": "token", "path": ".AWS"}
        )



def test_read_rejects_direct_credential_directory_path(tmp_path: Path):
    import pytest
    from crows.tools import ToolError

    workspace = tmp_path / "workspace"
    secret_dir = workspace / ".AWS"
    secret_dir.mkdir(parents=True)
    (secret_dir / "credentials.json").write_text('{"token": "secret"}', encoding="utf-8")

    with pytest.raises(ToolError, match="may contain credentials"):
        ToolRegistry(workspace).execute(
            "workspace.read_file", {"path": ".AWS/credentials.json"}
        )


def test_list_rejects_direct_credential_directory_path(tmp_path: Path):
    import pytest
    from crows.tools import ToolError

    workspace = tmp_path / "workspace"
    secret_dir = workspace / ".SSH"
    secret_dir.mkdir(parents=True)
    (secret_dir / "id_ed25519").write_text("private material", encoding="utf-8")

    with pytest.raises(ToolError, match="may contain credentials"):
        ToolRegistry(workspace).execute(
            "workspace.list_files", {"path": ".SSH"}
        )
