"""updates check: installed tools vs the published devkit manifest."""

import pytest
from devin_doctor.checks import updates
from devin_doctor.model import Status


@pytest.fixture(autouse=True)
def _online_updates(monkeypatch):
    # conftest sets DEVIN_DOCTOR_OFFLINE=1 so full-scan tests stay hermetic;
    # these tests inject a fake fetcher and must reach it.
    monkeypatch.delenv("DEVIN_DOCTOR_OFFLINE", raising=False)


def remote_manifest():
    return {
        "schema": "devin-devkit-manifest/0.1",
        "registry_version": 11,
        "tools": [
            {
                "id": "devin-doctor", "manager": "uv", "status": "published",
                "package": "devin-doctor", "version": "0.2.0",
                "commands": ["devin-doctor"],
                "install_spec": "devin-doctor==0.2.0",
            },
            {
                "id": "devin-history", "manager": "uv", "status": "source",
                "package": "devin-history", "version": "0.1.0",
                "commands": ["devin-history"],
                "install_spec": "https://example.com/x.tar.gz",
            },
            {
                "id": "devin-office", "manager": "manual", "status": "manual",
                "package": "devin-office", "version": "0.1.0",
                "commands": ["devin-office"], "install_spec": None,
            },
        ],
    }


def test_warns_on_outdated_tool(ctx):
    findings = updates.run(
        ctx,
        fetcher=remote_manifest,
        which=lambda c: None,
        uv=lambda: {"devin-doctor": "0.1.0"},
        pipx=dict,
        npm=dict,
    )
    f = next(f for f in findings if "devin-doctor" in f.message)
    assert f.status is Status.WARN
    assert "0.1.0 -> 0.2.0" in f.message
    assert "devin-devkit update" in f.fix


def test_current_tools_pass(ctx):
    findings = updates.run(
        ctx,
        fetcher=remote_manifest,
        which=lambda c: None,
        uv=lambda: {"devin-doctor": "0.2.0", "devin-history": "0.1.0"},
        pipx=dict,
        npm=dict,
    )
    assert len(findings) == 1
    assert findings[0].status is Status.PASS
    assert "current" in findings[0].message


def test_path_only_tool_warns_version_unknown(ctx):
    findings = updates.run(
        ctx,
        fetcher=remote_manifest,
        which=lambda c: f"/usr/bin/{c}",
        uv=dict,
        pipx=dict,
        npm=dict,
        probe=lambda c: None,
    )
    f = next(f for f in findings if "devin-history" in f.message)
    assert f.status is Status.WARN
    assert "version unknown" in f.message
    # manual tools are ignored even when on PATH
    assert not any("devin-office" in f.message for f in findings)


def test_path_only_tool_resolved_by_version_probe(ctx):
    findings = updates.run(
        ctx,
        fetcher=remote_manifest,
        which=lambda c: f"/usr/bin/{c}",
        uv=dict,
        pipx=dict,
        npm=dict,
        probe=lambda c: {"devin-doctor": "0.2.0", "devin-history": "0.1.0"}.get(c),
    )
    assert len(findings) == 1
    assert findings[0].status is Status.PASS


def test_path_only_probe_older_than_remote_warns(ctx):
    findings = updates.run(
        ctx,
        fetcher=remote_manifest,
        which=lambda c: f"/usr/bin/{c}",
        uv=dict,
        pipx=dict,
        npm=dict,
        probe=lambda c: "0.0.1" if c == "devin-history" else "9.9.9",
    )
    f = next(f for f in findings if "devin-history" in f.message)
    assert f.status is Status.WARN
    assert "0.0.1 -> 0.1.0" in f.message


def test_offline_fetch_failure_skips(ctx):
    def boom():
        raise OSError("offline")

    findings = updates.run(ctx, fetcher=boom, which=lambda c: None)
    assert len(findings) == 1
    assert findings[0].status is Status.PASS
    assert "skipped" in findings[0].message


def test_offline_env_var_skips(ctx, monkeypatch):
    monkeypatch.setenv("DEVIN_DOCTOR_OFFLINE", "1")

    def boom():
        raise AssertionError("network must not be touched")

    findings = updates.run(ctx, fetcher=boom, which=lambda c: None)
    assert findings[0].status is Status.PASS


def test_manifest_url_uses_devkit_monorepo_path():
    # devin-devkit is a monorepo: the published manifest lives inside
    # packages/devkit/. A bare src/... path 404s and the whole check
    # silently degrades to "registry unreachable".
    assert "packages/devkit/src/devin_devkit/manifest.json" in updates.MANIFEST_URL


def test_version_key_handles_suffixes():
    assert updates._version_key("0.2.0") > updates._version_key("0.1.9")
    assert updates._version_key("0.1.0") == updates._version_key("v0.1.0".lstrip("v"))
