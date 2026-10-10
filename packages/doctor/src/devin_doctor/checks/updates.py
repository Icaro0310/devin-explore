"""Check 7 — updates: installed devin-* tools vs the published devkit manifest.

devin-devkit install specs are pinned (PyPI versions or GitHub archive SHAs),
so installs never move on their own. This check fetches the manifest published
in the devin-devkit repository — re-exported weekly from the maintainer
registry — and compares it against what ``uv tool list`` / ``npm ls -g`` report,
with bare PATH presence as a fallback for tools installed another way.

The check never blocks: offline hosts, missing managers and unreachable
registries produce a single skipped-style finding instead of a failure. Set
``DEVIN_DOCTOR_OFFLINE=1`` to skip the network call explicitly (corporate
Windows and other local-only environments).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from devin_doctor.model import Context, Finding, Status

CHECK_ID = "updates"

MANIFEST_URL = (
    "https://raw.githubusercontent.com/Icaro0310/devin-devkit/main/"
    "packages/devkit/src/devin_devkit/manifest.json"
)
_TIMEOUT = 10


def _fetch_manifest(url: str = MANIFEST_URL) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data.get("schema") != "devin-devkit-manifest/0.1":
        raise ValueError("unexpected manifest schema")
    return data


def _uv_tools() -> dict[str, str]:
    try:
        result = subprocess.run(
            ["uv", "tool", "list"], capture_output=True, text=True, check=False
        )
    except OSError:
        return {}
    if result.returncode != 0:
        return {}
    tools: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if not line.strip() or line.startswith("-"):
            continue
        name, _, version = line.strip().partition(" ")
        if name:
            tools[name] = version.lstrip("v")
    return tools


def _pipx_tools() -> dict[str, str]:
    try:
        result = subprocess.run(
            ["pipx", "list", "--json"], capture_output=True, text=True, check=False
        )
    except OSError:
        return {}
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return {
        name: meta.get("metadata", {}).get("main_package", {}).get("package_version", "")
        for name, meta in data.get("venvs", {}).items()
        if isinstance(meta, dict)
    }


def _npm_tools() -> dict[str, str]:
    try:
        result = subprocess.run(
            ["npm", "ls", "--global", "--depth=0", "--json"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return {}
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return {
        name: meta.get("version", "")
        for name, meta in data.get("dependencies", {}).items()
        if isinstance(meta, dict)
    }


def _probe_version(command: str) -> str | None:
    try:
        result = subprocess.run(
            [command, "--version"], capture_output=True, text=True,
            check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    import re
    match = re.search(r"(\d+\.\d+\.\d+)", (result.stdout or "") + (result.stderr or ""))
    return match.group(1) if match else None


def _version_key(version: str) -> tuple[int, ...]:
    parts = []
    for piece in str(version).split("."):
        digits = "".join(c for c in piece if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _skipped(reason: str) -> list[Finding]:
    return [
        Finding(CHECK_ID, Status.PASS, f"update check skipped ({reason})")
    ]


def run(
    ctx: Context,
    *,
    fetcher: Callable[[], dict[str, Any]] = _fetch_manifest,
    which: Callable[[str], str | None] = shutil.which,
    uv: Callable[[], dict[str, str]] = _uv_tools,
    pipx: Callable[[], dict[str, str]] = _pipx_tools,
    npm: Callable[[], dict[str, str]] = _npm_tools,
    probe: Callable[[str], str | None] = _probe_version,
) -> list[Finding]:
    if os.environ.get("DEVIN_DOCTOR_OFFLINE") == "1":
        return _skipped("DEVIN_DOCTOR_OFFLINE=1")
    try:
        remote = fetcher()
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return _skipped("registry unreachable")

    installed = {**pipx(), **uv(), **npm()}
    findings: list[Finding] = []
    for tool in remote.get("tools", []):
        if tool.get("manager") == "manual" or tool.get("status") == "manual":
            continue
        package = tool.get("package")
        if not package:
            continue
        if package in installed:
            current, latest = installed[package], tool.get("version", "")
            if _version_key(latest) > _version_key(current):
                findings.append(Finding(
                    CHECK_ID, Status.WARN,
                    f"{tool['id']} {current} -> {latest} available",
                    "run `devin-devkit update --apply`",
                ))
        else:
            command = next(
                (c for c in tool.get("commands", []) if which(c)), None
            )
            if command is None:
                continue
            probed = probe(command)
            latest = tool.get("version", "")
            if probed is None:
                findings.append(Finding(
                    CHECK_ID, Status.WARN,
                    f"{tool['id']} is on PATH but outside uv/pipx/npm; version unknown",
                    "reinstall via `devin-devkit update --apply`",
                ))
            elif _version_key(latest) > _version_key(probed):
                findings.append(Finding(
                    CHECK_ID, Status.WARN,
                    f"{tool['id']} {probed} -> {latest} available",
                    "run `devin-devkit update --apply`",
                ))

    if not findings:
        version = remote.get("registry_version", "?")
        return [Finding(
            CHECK_ID, Status.PASS,
            f"installed devin-* tools are current (registry v{version})",
        )]
    return findings
