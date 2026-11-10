from pathlib import Path
import pytest
from crows.tools import ToolRegistry, ToolError, provider_tool_name


def test_list_and_search_workspace(tmp_path: Path):
    (tmp_path / "hello.py").write_text("print('hello')\n# Aether test\n", encoding="utf-8")
    registry = ToolRegistry(tmp_path)
    listing = registry.execute("workspace.list_files", {"path": "."})
    assert any(entry["name"] == "hello.py" for entry in listing["entries"])
    result = registry.execute("workspace.search_text", {"query": "Aether"})
    assert result["matches"][0]["file"] == "hello.py"


def test_path_escape_is_rejected(tmp_path: Path):
    registry = ToolRegistry(tmp_path / "workspace")
    with pytest.raises(ToolError):
        registry.execute("workspace.list_files", {"path": "../outside"})


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("shell.run", {"command": "whoami"}),
        # Provider-style names must not become executable merely because they
        # use the provider's underscore separator instead of internal dots.
        ("shell__run", {"command": "whoami"}),
        ("workspace__delete_file", {"path": "important.txt"}),
    ],
)
def test_unknown_tool_names_are_rejected(tmp_path: Path, name: str, payload: dict):
    registry = ToolRegistry(tmp_path)
    with pytest.raises(ToolError, match="Unknown tool"):
        registry.execute(name, payload)


def test_provider_tool_names_are_valid_and_reversible(tmp_path: Path):
    registry = ToolRegistry(tmp_path)
    internal_names = [item["name"] for item in registry.describe()]
    exposed_names = [provider_tool_name(name) for name in internal_names]

    assert len(exposed_names) == len(set(exposed_names))
    assert all(name and len(name) <= 64 for name in exposed_names)
    assert all(all(char.isalnum() or char in "_-" for char in name) for name in exposed_names)
    assert dict(zip(exposed_names, internal_names)) == {
        provider_tool_name(name): name for name in internal_names
    }


def test_read_file_returns_real_contents(tmp_path: Path):
    source = tmp_path / "sample.py"
    source.write_text("first\nsecond\nthird\n", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    result = registry.execute(
        "workspace.read_file",
        {"path": "sample.py", "start_line": 2, "max_lines": 1},
    )

    assert result["path"] == "sample.py"
    assert result["content"] == "second"
    assert result["total_lines"] == 3
    assert result["truncated"] is True


def test_read_file_rejects_path_escape(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret", encoding="utf-8")
    registry = ToolRegistry(workspace)

    with pytest.raises(ToolError):
        registry.execute("workspace.read_file", {"path": "../outside.py"})


def test_read_file_rejects_directory(tmp_path: Path):
    registry = ToolRegistry(tmp_path)
    with pytest.raises(ToolError):
        registry.execute("workspace.read_file", {"path": "."})


def test_workspace_tools_exclude_common_secret_files_but_allow_env_examples(tmp_path: Path):
    (tmp_path / ".env").write_text("API_KEY=secret-value", encoding="utf-8")
    (tmp_path / ".env.example").write_text("API_KEY=replace-me", encoding="utf-8")
    (tmp_path / "id_rsa").write_text("private-key-material", encoding="utf-8")
    (tmp_path / "credentials.json").write_text('{"token":"secret-value"}', encoding="utf-8")
    (tmp_path / "server.pem").write_text("certificate-material", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ordinary workspace note", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    listed = {entry["name"] for entry in registry.execute("workspace.list_files", {"path": "."})["entries"]}
    assert ".env" not in listed
    assert "id_rsa" not in listed
    assert "credentials.json" not in listed
    assert "server.pem" not in listed
    assert ".env.example" in listed

    for path in (".env", "id_rsa", "credentials.json", "server.pem"):
        with pytest.raises(ToolError, match="excluded"):
            registry.execute("workspace.read_file", {"path": path})

    search = registry.execute("workspace.search_text", {"query": "secret-value"})
    assert all(match["file"] not in {".env", "credentials.json"} for match in search["matches"])


def test_workspace_search_excludes_sensitive_named_directories(tmp_path: Path):
    secrets = tmp_path / "credentials"
    secrets.mkdir()
    (secrets / "notes.txt").write_text("needle-secret-value", encoding="utf-8")
    nested_env = tmp_path / ".env.backup"
    nested_env.mkdir()
    (nested_env / "notes.txt").write_text("needle-secret-value", encoding="utf-8")
    ordinary = tmp_path / "docs"
    ordinary.mkdir()
    (ordinary / "notes.txt").write_text("needle-secret-value", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    result = registry.execute("workspace.search_text", {"query": "needle-secret-value"})

    assert [match["file"] for match in result["matches"]] == ["docs/notes.txt"]


def test_workspace_list_and_read_reject_sensitive_directory_paths(tmp_path: Path):
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "notes.txt").write_text("private note", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    with pytest.raises(ToolError, match="excluded"):
        registry.execute("workspace.list_files", {"path": "credentials"})
    with pytest.raises(ToolError, match="excluded"):
        registry.execute("workspace.read_file", {"path": "credentials/notes.txt"})
    with pytest.raises(ToolError, match="excluded"):
        registry.execute("workspace.search_text", {"query": "private", "path": "credentials"})


@pytest.mark.parametrize("max_results", [0, -1, -50])
def test_workspace_search_nonpositive_limit_returns_no_matches(
    tmp_path: Path, max_results: int
):
    (tmp_path / "notes.txt").write_text("needle", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    result = registry.search_text("needle", max_results=max_results)

    assert result["matches"] == []
    assert result["truncated"] is False


def test_workspace_tool_descriptions_distinguish_read_list_and_search(tmp_path: Path):
    registry = ToolRegistry(tmp_path)
    descriptions = {item["name"]: item["description"] for item in registry.describe()}

    assert "READ A SPECIFIC FILE" in descriptions["workspace.read_file"]
    assert "never guess contents" in descriptions["workspace.read_file"]
    assert "directory listing" in descriptions["workspace.list_files"]
    assert "not for reading a known file" in descriptions["workspace.search_text"]


@pytest.mark.parametrize(
    "name",
    [
        ".npmrc", ".pypirc", ".netrc", ".dockercfg",
        "auth.json", "tokens.json", "service-account.json",
        "application_default_credentials.json", "auth.toml",
        "credentials.toml", "kubeconfig", "client.key",
        "backup.pgp", "vault.kdbx",
    ],
)
def test_workspace_tools_reject_additional_credential_file_patterns(
    tmp_path: Path, name: str
):
    (tmp_path / name).write_text("placeholder-secret", encoding="utf-8")
    registry = ToolRegistry(tmp_path)

    listed = {
        entry["name"]
        for entry in registry.execute("workspace.list_files", {"path": "."})["entries"]
    }
    assert name not in listed

    with pytest.raises(ToolError, match="excluded"):
        registry.execute("workspace.read_file", {"path": name})

    search = registry.execute("workspace.search_text", {"query": "placeholder-secret"})
    assert all(match["file"] != name for match in search["matches"])
