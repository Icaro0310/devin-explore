"""``devin-pm verify`` — cross-check against a synthetic hub registry.

All fixtures are synthetic: crafted ``registry.json`` documents in tmp
dirs, pyproject/.git manifests written by the tests, and the shared
``sessions_db`` fixture (alpha + beta projects). Nothing real is read and
no network/git subprocess is involved — git remotes are parsed from
``.git/config`` files the tests write themselves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import insert_session
from devin_pm.cli import main
from devin_pm.projects import group_sessions, load_sessions
from devin_pm.verify import (
    normalize_repo_url,
    read_git_remote,
    read_pyproject,
    verify,
)


def hub_entry(name, **extra):
    entry = {
        "name": name,
        "url": f"https://github.com/Icaro0310/{name}",
        "kind": "project",
        "visibility": "public",
        "wave": 0,
        "status": "active",
        "description": f"{name} description",
    }
    entry.update(extra)
    return entry


def write_hub_registry(path: Path, *entries) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 6,
                "generated": "2026-01-01",
                "owner": "Icaro0310",
                "repositories": list(entries),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def hub_registry(sessions_db):
    """Synthetic hub registry inside the workdir listing alpha + beta."""
    return write_hub_registry(
        sessions_db.parent / "hub" / "registry.json",
        hub_entry("alpha"),
        hub_entry("beta"),
    )


@pytest.fixture
def vflags(sessions_db, hub_registry):
    return [
        "--sessions-db", str(sessions_db),
        "--registry", str(hub_registry),
    ]


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


def test_verify_clean(vflags, capsys):
    assert main(["verify", *vflags]) == 0
    out = capsys.readouterr().out
    assert "result: OK" in out
    assert "in registry but unknown to pm (0)" in out


def test_verify_json(vflags, capsys):
    assert main(["verify", *vflags, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["projects"] == 2
    assert report["registry_entries"] == 2
    assert report["drift"] is False
    assert sorted(report["matched"]) == ["alpha", "beta"]


def test_unknown_to_pm_is_informational(vflags, hub_registry, capsys):
    write_hub_registry(
        hub_registry,
        hub_entry("alpha"),
        hub_entry("beta"),
        hub_entry("devin-ghost"),
    )
    assert main(["verify", *vflags]) == 0
    out = capsys.readouterr().out
    assert "devin-ghost" in out
    assert "result: OK" in out


def test_missing_relevant_project_is_drift(vflags, hub_registry, capsys):
    write_hub_registry(hub_registry, hub_entry("alpha"))
    assert main(["verify", *vflags]) == 1
    out = capsys.readouterr().out
    assert "beta" in out
    assert "result: DRIFT" in out


def test_missing_non_ecosystem_project_not_drift(
    sessions_db, vflags, capsys
):
    insert_session(
        sessions_db, sid="p-1", working_directory="/nonexistent/petsaas"
    )
    assert main(["verify", *vflags]) == 0
    out = capsys.readouterr().out
    assert "petsaas" in out
    assert "[non-ecosystem]" in out
    assert "result: OK" in out


def test_description_drift(sessions_db, vflags, capsys):
    alpha = sessions_db.parent / "alpha"
    (alpha / "pyproject.toml").write_text(
        '[project]\nname = "alpha"\ndescription = "different description"\n',
        encoding="utf-8",
    )
    assert main(["verify", *vflags]) == 1
    out = capsys.readouterr().out
    assert "alpha.description" in out
    assert "result: DRIFT" in out


def test_description_match_no_drift(sessions_db, vflags):
    alpha = sessions_db.parent / "alpha"
    (alpha / "pyproject.toml").write_text(
        '[project]\nname = "alpha"\ndescription = "alpha description"\n',
        encoding="utf-8",
    )
    assert main(["verify", *vflags]) == 0


def test_url_drift_and_normalization(sessions_db, vflags, capsys):
    gitconfig = sessions_db.parent / "alpha" / ".git"
    gitconfig.mkdir()
    # ssh remote that normalizes to the registry https url -> clean
    (gitconfig / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:Icaro0310/alpha.git\n',
        encoding="utf-8",
    )
    assert main(["verify", *vflags]) == 0
    # remote pointing elsewhere -> drift
    (gitconfig / "config").write_text(
        '[remote "origin"]\n'
        "\turl = https://github.com/SomeoneElse/alpha.git\n",
        encoding="utf-8",
    )
    assert main(["verify", *vflags]) == 1
    assert "alpha.url" in capsys.readouterr().out


def test_name_drift_from_pyproject(sessions_db, vflags, capsys):
    alpha = sessions_db.parent / "alpha"
    (alpha / "pyproject.toml").write_text(
        '[project]\nname = "alpha-renamed"\n', encoding="utf-8"
    )
    assert main(["verify", *vflags]) == 1
    assert "alpha.name" in capsys.readouterr().out


def test_local_dir_match(sessions_db, hub_registry, capsys):
    """A registry ``local_dir`` can match a project whose name differs."""
    write_hub_registry(
        hub_registry,
        hub_entry("alpha-renamed", local_dir="../alpha"),
        hub_entry("beta"),
    )
    assert main(["verify", "--sessions-db", str(sessions_db),
                 "--registry", str(hub_registry)]) == 0


def test_pm_registry_source(sessions_db, hub_registry, tmp_path, capsys):
    pm_reg = tmp_path / "pm-registry.json"
    pm_reg.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": [
                    {
                        "name": "alpha",
                        "working_directory": str(
                            sessions_db.parent / "alpha"
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    write_hub_registry(hub_registry, hub_entry("alpha"))
    rc = main(
        [
            "verify",
            "--pm-registry", str(pm_reg),
            "--registry", str(hub_registry),
            "--json",
        ]
    )
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["projects"] == 1


def test_default_registry_relative_to_cwd(
    sessions_db, hub_registry, tmp_path, monkeypatch, capsys
):
    eco = tmp_path / "eco"
    (eco / "devin-powerups").mkdir(parents=True)
    (eco / "devin-pm").mkdir()
    reg = write_hub_registry(
        eco / "devin-powerups" / "registry.json",
        hub_entry("alpha"),
        hub_entry("beta"),
    )
    monkeypatch.chdir(eco / "devin-pm")
    rc = main(["verify", "--sessions-db", str(sessions_db), "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert Path(report["registry"]).name == reg.name


def test_missing_registry_exit_2(sessions_db, tmp_path, capsys):
    rc = main(
        [
            "verify",
            "--sessions-db", str(sessions_db),
            "--registry", str(tmp_path / "nope.json"),
        ]
    )
    assert rc == 2


def test_malformed_registry_exit_1(sessions_db, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    rc = main(
        [
            "verify",
            "--sessions-db", str(sessions_db),
            "--registry", str(bad),
        ]
    )
    assert rc == 1


def test_missing_db_exit_2(hub_registry, tmp_path):
    rc = main(
        [
            "verify",
            "--sessions-db", str(tmp_path / "no.db"),
            "--registry", str(hub_registry),
        ]
    )
    assert rc == 2


# ---------------------------------------------------------------------------
# --fix-hints
# ---------------------------------------------------------------------------


def test_fix_hints_field_drift(sessions_db, vflags, capsys):
    """Each drift line gets a hint with the expected value + action."""
    alpha = sessions_db.parent / "alpha"
    (alpha / "pyproject.toml").write_text(
        '[project]\nname = "alpha"\ndescription = "different description"\n',
        encoding="utf-8",
    )
    assert main(["verify", *vflags, "--fix-hints"]) == 1
    out = capsys.readouterr().out
    assert "alpha.description" in out
    assert "hint: expected 'alpha description'" in out
    assert "pyproject.toml" in out  # suggested action target


def test_fix_hints_url_drift_action(sessions_db, vflags, capsys):
    """url drift suggests the concrete git remote command."""
    alpha = sessions_db.parent / "alpha"
    (alpha / ".git").mkdir()
    (alpha / ".git" / "config").write_text(
        '[remote "origin"]\n'
        "\turl = https://github.com/SomeoneElse/alpha.git\n",
        encoding="utf-8",
    )
    assert main(["verify", *vflags, "--fix-hints"]) == 1
    out = capsys.readouterr().out
    assert "remote set-url origin https://github.com/Icaro0310/alpha" in out


def test_fix_hints_missing_relevant(vflags, hub_registry, capsys):
    write_hub_registry(hub_registry, hub_entry("alpha"))
    assert main(["verify", *vflags, "--fix-hints"]) == 1
    out = capsys.readouterr().out
    assert "hint: expected a registry entry for 'beta'" in out
    assert "registry.json" in out


def test_fix_hints_clean_report_shows_none(vflags, capsys):
    assert main(["verify", *vflags, "--fix-hints"]) == 0
    assert "hint:" not in capsys.readouterr().out


def test_fix_hints_json_items_carry_hint(vflags, hub_registry, capsys):
    write_hub_registry(hub_registry, hub_entry("alpha"))
    assert main(["verify", *vflags, "--fix-hints", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert all("hint" in i for i in report["missing_from_registry"])
    assert all("hint" in i for i in report["unknown_to_pm"])
    beta = next(
        i for i in report["missing_from_registry"] if i["name"] == "beta"
    )
    assert "registry.json" in beta["hint"]


def test_no_fix_hints_keeps_output_unchanged(sessions_db, vflags, capsys):
    """Without the flag the drift block prints no hint lines."""
    alpha = sessions_db.parent / "alpha"
    (alpha / "pyproject.toml").write_text(
        '[project]\nname = "alpha"\ndescription = "different description"\n',
        encoding="utf-8",
    )
    assert main(["verify", *vflags]) == 1
    assert "hint:" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# manifest readers + url normalization
# ---------------------------------------------------------------------------


def test_read_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\nrequires = []\n\n"
        '[project]\nname = "devin-x"\n'
        'description = "does things"\nversion = "0.1.0"\n\n'
        "[project.optional-dependencies]\ndev = []\n",
        encoding="utf-8",
    )
    assert read_pyproject(tmp_path) == {
        "name": "devin-x",
        "description": "does things",
    }


def test_read_pyproject_missing(tmp_path):
    assert read_pyproject(tmp_path) == {}


def test_read_git_remote_missing(tmp_path):
    assert read_git_remote(tmp_path) is None


def test_read_git_remote_worktree_pointer(tmp_path):
    """``.git`` pointer files (linked worktrees) are followed."""
    gitdir = tmp_path / "main" / ".git" / "worktrees" / "wt1"
    gitdir.mkdir(parents=True)
    common = tmp_path / "main" / ".git"
    (gitdir / "commondir").write_text("../..", encoding="utf-8")
    (common / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/o/r.git\n',
        encoding="utf-8",
    )
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir}", encoding="utf-8")
    assert read_git_remote(wt) == "https://github.com/o/r.git"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("git@github.com:Icaro0310/devin-pm.git",
         "https://github.com/icaro0310/devin-pm"),
        ("https://github.com/Icaro0310/devin-pm",
         "https://github.com/icaro0310/devin-pm"),
        ("https://github.com/Icaro0310/devin-pm.git/",
         "https://github.com/icaro0310/devin-pm"),
        ("ssh://git@github.com/Icaro0310/devin-pm.git",
         "https://github.com/icaro0310/devin-pm"),
        (None, None),
    ],
)
def test_normalize_repo_url(raw, expected):
    assert normalize_repo_url(raw) == expected


def test_verify_pure_function(sessions_db, hub_registry):
    """verify() returns the same report the CLI renders."""
    projects = group_sessions(load_sessions(sessions_db))
    report = verify(projects, hub_registry)
    assert report["drift"] is False
    assert report["matched"] == ["alpha", "beta"]
