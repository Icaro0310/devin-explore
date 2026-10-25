
from devin_doctor.paths import (
    default_config_dir,
    default_data_dir,
    default_state_dir,
    locate_stores,
)


def test_linux_discovers_cli_data_and_desktop_config_separately(tmp_path):
    data_home = tmp_path / "data"
    config_home = tmp_path / "config"
    data_dir = data_home / "devin"
    config_dir = config_home / "Devin"
    acp_dir = config_dir / "User" / "acp-messages"
    state_db = config_dir / "User" / "globalStorage" / "state.vscdb"
    (data_dir / "cli").mkdir(parents=True)
    acp_dir.mkdir(parents=True)
    state_db.parent.mkdir(parents=True)
    (data_dir / "cli" / "sessions.db").touch()
    state_db.touch()
    env = {"XDG_DATA_HOME": str(data_home), "XDG_CONFIG_HOME": str(config_home)}

    assert default_data_dir(environ=env, platform="linux") == data_dir
    assert default_config_dir(environ=env, platform="linux") == config_dir
    stores = locate_stores(data_dir, config_dir=config_dir)
    assert stores.sessions_db == data_dir / "cli" / "sessions.db"
    assert stores.acp_dir == acp_dir
    assert stores.state_vscdb == state_db


def test_legacy_colocated_store_layout_still_works(tmp_path):
    root = tmp_path / "legacy-devin"
    (root / "User" / "acp-messages").mkdir(parents=True)
    (root / "cli").mkdir(parents=True)

    stores = locate_stores(root)

    assert stores.acp_dir == root / "User" / "acp-messages"
    assert stores.sessions_db == root / "cli" / "sessions.db"


def test_default_state_dir_is_per_platform_not_per_data_dir(tmp_path):
    """The automation baseline lives in doctor's own ``devin-doctor``
    dir — never inside the Devin data dir being inspected (which
    ``--data-dir`` can point at an arbitrary checkout)."""
    xdg = tmp_path / "xdg"
    assert (
        default_state_dir(environ={"XDG_DATA_HOME": str(xdg)}, platform="linux")
        == xdg / "devin-doctor"
    )
    local = tmp_path / "AppData" / "Local"
    assert (
        default_state_dir(
            environ={"LOCALAPPDATA": str(local)}, platform="win32"
        )
        == local / "devin-doctor"
    )
    # the inspected data dir moves independently — no relation between
    # where Devin stores live and where doctor keeps its baseline
    env = {"XDG_DATA_HOME": str(xdg)}
    assert (
        default_state_dir(environ=env, platform="linux").parent
        != default_data_dir(environ=env, platform="linux")
    )


def test_default_state_dir_env_override(tmp_path):
    custom = tmp_path / "custom-state"
    for plat in ("linux", "win32", "darwin"):
        assert (
            default_state_dir(
                environ={"DEVIN_DOCTOR_STATE_DIR": str(custom)},
                platform=plat,
            )
            == custom
        )
