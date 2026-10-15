
from devin_search.paths import (
    acp_dir_candidates,
    codex_root_candidates,
    copilot_root_candidates,
    default_acp_dir,
    default_codex_dir,
    default_copilot_dirs,
    default_index_path,
    default_sessions_db,
    default_vscode_chat_dirs,
    vscode_chat_userdir_candidates,
)


def test_linux_discovers_data_and_config_stores_separately(tmp_path):
    data_home = tmp_path / "data"
    config_home = tmp_path / "config"
    sessions_db = data_home / "devin" / "cli" / "sessions.db"
    acp_dir = config_home / "Devin" / "User" / "acp-messages"
    sessions_db.parent.mkdir(parents=True)
    acp_dir.mkdir(parents=True)
    sessions_db.touch()
    env = {"XDG_DATA_HOME": str(data_home), "XDG_CONFIG_HOME": str(config_home)}

    assert default_sessions_db(environ=env, platform="linux") == sessions_db
    assert acp_dir_candidates(environ=env, platform="linux")[0] == acp_dir
    assert default_acp_dir(environ=env, platform="linux") == acp_dir
    assert default_index_path(environ=env, platform="linux") == (
        data_home / "devin-search" / "search.db"
    )


def test_linux_keeps_legacy_store_candidates(tmp_path):
    config_home = tmp_path / "config"
    legacy_acp = config_home / "devin" / "User" / "acp-messages"
    legacy_acp.mkdir(parents=True)
    env = {"XDG_CONFIG_HOME": str(config_home)}

    assert default_acp_dir(environ=env, platform="linux") == legacy_acp


def test_darwin_probes_capitalized_devin_config_dir(tmp_path):
    """macOS UI config dir is `Devin`, not lowercase `devin` — case-
    sensitive volumes report the store missing otherwise."""
    lib = tmp_path / "Library" / "Application Support"
    acp = lib / "Devin" / "User" / "acp-messages"
    acp.mkdir(parents=True)
    env = {"HOME": str(tmp_path)}

    cands = acp_dir_candidates(environ=env, platform="darwin")
    assert cands[0] == acp


def test_copilot_candidates_env_then_home(tmp_path):
    env = {
        "HOME": str(tmp_path),
        "COPILOT_DIR": str(tmp_path / "cp-dir"),
        "COPILOT_HOME": str(tmp_path / "cp-home"),
    }
    cands = copilot_root_candidates(environ=env, platform="linux")
    assert cands == [
        tmp_path / "cp-dir",
        tmp_path / "cp-home",
        tmp_path / ".copilot",
    ]


def test_copilot_default_dirs_merges_all_existing(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / ".copilot"
    a.mkdir()
    b.mkdir()
    env = {"HOME": str(tmp_path), "COPILOT_DIR": str(a)}
    assert default_copilot_dirs(environ=env, platform="linux") == [a, b]


def test_codex_candidates_env_then_dot_codex(tmp_path):
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / "cx")}
    assert codex_root_candidates(environ=env, platform="linux") == [
        tmp_path / "cx",
        tmp_path / ".codex",
    ]
    env2 = {"HOME": str(tmp_path)}
    assert codex_root_candidates(environ=env2, platform="linux") == [
        tmp_path / ".codex"
    ]


def test_codex_default_first_existing_wins(tmp_path):
    cx = tmp_path / "cx"
    dot = tmp_path / ".codex"
    cx.mkdir()
    dot.mkdir()
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(cx)}
    assert default_codex_dir(environ=env, platform="linux") == cx


def test_vscode_chat_userdirs_linux(tmp_path):
    env = {"HOME": str(tmp_path)}
    cands = vscode_chat_userdir_candidates(environ=env, platform="linux")
    base = tmp_path / ".config"
    assert cands == [
        base / "Code" / "User",
        base / "Code - Insiders" / "User",
        tmp_path / ".vscode-server" / "data" / "User",
    ]


def test_vscode_chat_userdirs_appdata_wins(tmp_path):
    appdata = tmp_path / "AppData" / "Roaming"
    env = {"HOME": str(tmp_path), "APPDATA": str(appdata)}
    cands = vscode_chat_userdir_candidates(environ=env, platform="linux")
    assert cands[0] == appdata / "Code" / "User"
    assert cands[-1] == tmp_path / ".vscode-server" / "data" / "User"


def test_vscode_chat_default_dirs_all_variants(tmp_path):
    code = tmp_path / ".config" / "Code" / "User"
    code.mkdir(parents=True)
    env = {"HOME": str(tmp_path)}
    assert default_vscode_chat_dirs(environ=env, platform="linux") == [code]
