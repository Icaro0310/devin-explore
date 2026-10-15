
from devin_search.paths import (
    acp_dir_candidates,
    default_acp_dir,
    default_index_path,
    default_sessions_db,
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
