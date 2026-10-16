"""Check 4 — config: ``credentials.toml`` presence (values are NEVER printed)
and ``.devin/`` hooks/MCP config sanity in the working directory's repos.

Devin config files are JSONC (comments allowed), so parsing strips comments
before ``json.loads``. ``.devin/`` directories are checked in ``ctx.cwd``
itself and in each immediate child directory that has one — the "repos under
cwd" case.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import tomllib

from devin_doctor.model import Context, Finding, Status
from devin_doctor.paths import (
    cli_anchor_present,
    credentials_path,
    user_config_path,
    user_mcp_config_path,
)

CHECK_ID = "config"

KNOWN_HOOK_EVENTS = {
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "UserPromptSubmit",
    "Stop",
    "PostCompaction",
    "SessionStart",
    "SessionEnd",
}

_PROJECT_CONFIG_FILES = (
    "config.json",
    "config.local.json",
    "mcp_config.json",
    "mcp_config.local.json",
    "hooks.v1.json",
)

_MAX_REPOS = 20


def _strip_jsonc(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments while respecting string literals."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _load_jsonc(path: Path) -> Any:
    return json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# credentials.toml
# ---------------------------------------------------------------------------


def _credentials_finding(path: Path) -> Finding:
    if not path.is_file():
        return Finding(
            CHECK_ID,
            Status.WARN,
            f"credentials.toml not found ({path})",
            fix="Authenticate Devin (sign in / `devin login`) — the file is "
            "created on first auth.",
        )
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return Finding(
            CHECK_ID,
            Status.FAIL,
            f"credentials.toml is not valid TOML: {exc}",
            fix="The credentials file is malformed — re-authenticate to "
            "regenerate it.",
        )
    n_keys = sum(1 for v in data.values() if not isinstance(v, dict))
    n_sections = sum(1 for v in data.values() if isinstance(v, dict))
    return Finding(
        CHECK_ID,
        Status.PASS,
        f"credentials.toml present — {n_sections} section(s), "
        f"{n_keys} top-level key(s), values masked",
    )


# ---------------------------------------------------------------------------
# hooks / mcp shape validation
# ---------------------------------------------------------------------------


def _hooks_problems(obj: Any) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a hooks object."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(obj, dict):
        return ["hooks object must be a JSON object"], []
    for event, groups in obj.items():
        if event not in KNOWN_HOOK_EVENTS:
            warnings.append(f"unknown hook event '{event}'")
        if not isinstance(groups, list):
            errors.append(f"'{event}' must be a list of hook groups")
            continue
        for gi, group in enumerate(groups):
            if not isinstance(group, dict) or not isinstance(
                group.get("hooks"), list
            ):
                errors.append(
                    f"'{event}' group {gi} must be an object with a 'hooks' list"
                )
                continue
            for hi, hook in enumerate(group["hooks"]):
                if not isinstance(hook, dict):
                    errors.append(f"'{event}' group {gi} hook {hi} not an object")
                    continue
                htype = hook.get("type")
                if htype == "command" and not isinstance(
                    hook.get("command"), str
                ):
                    errors.append(
                        f"'{event}' group {gi} hook {hi}: 'command' hooks need"
                        " a 'command' string"
                    )
                elif htype == "prompt" and not isinstance(
                    hook.get("prompt"), str
                ):
                    errors.append(
                        f"'{event}' group {gi} hook {hi}: 'prompt' hooks need"
                        " a 'prompt' string"
                    )
                elif htype not in ("command", "prompt"):
                    errors.append(
                        f"'{event}' group {gi} hook {hi}: type must be"
                        " 'command' or 'prompt'"
                    )
    return errors, warnings


def _mcp_problems(obj: Any) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    if not isinstance(obj, dict):
        return ["mcp config must be a JSON object"], []
    servers = obj.get("mcpServers")
    if servers is None:
        warnings.append("no 'mcpServers' key")
        return [], warnings
    if not isinstance(servers, dict):
        return ["'mcpServers' must be an object"], warnings
    for name, server in servers.items():
        if not isinstance(server, dict) or not (
            isinstance(server.get("command"), str)
            or isinstance(server.get("url"), str)
        ):
            warnings.append(
                f"server '{name}' has neither a 'command' nor a 'url'"
            )
    return [], warnings


def _config_problems(obj: Any) -> tuple[list[str], list[str]]:
    """Problems inside a ``config.json``-style file: nested ``hooks`` get the
    hooks validation; a top-level ``mcpServers`` key is the legacy location."""
    if not isinstance(obj, dict):
        return ["config file must be a JSON object"], []
    errors: list[str] = []
    warnings: list[str] = []
    if "hooks" in obj:
        e, w = _hooks_problems(obj["hooks"])
        errors += e
        warnings += w
    if "mcpServers" in obj:
        warnings.append(
            "'mcpServers' in config.json is the legacy location — Devin "
            "migrates it to mcp_config.json automatically"
        )
    return errors, warnings


def _file_finding(label: str, path: Path, kind: str) -> Finding:
    validator = {
        "hooks": _hooks_problems,
        "mcp": _mcp_problems,
        "config": _config_problems,
    }[kind]
    try:
        obj = _load_jsonc(path)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return Finding(
            CHECK_ID,
            Status.FAIL,
            f"{label}: invalid JSON — {exc}",
            fix="Fix the syntax (comments are allowed, trailing commas are "
            "not) or delete the file.",
        )
    errors, warnings = validator(obj)
    if errors:
        return Finding(
            CHECK_ID,
            Status.FAIL,
            f"{label}: {errors[0]}" + (" …" if len(errors) > 1 else ""),
            fix="Hook/MCP entries must match the documented shape — see "
            "devin docs (extensibility/hooks).",
        )
    if warnings:
        return Finding(CHECK_ID, Status.WARN, f"{label}: {warnings[0]}")
    return Finding(CHECK_ID, Status.PASS, f"{label}: OK")


def _kind_for(filename: str) -> str:
    if filename == "hooks.v1.json":
        return "hooks"
    if filename.startswith("mcp_config"):
        return "mcp"
    return "config"


def _project_config_findings(cwd: Path) -> tuple[list[Finding], bool]:
    """Check ``.devin/`` config in ``cwd`` and each immediate child dir that
    has one (the 'repos under cwd' layout). Returns the findings plus
    whether any ``.devin/`` dir was seen at all."""
    candidates = [cwd]
    try:
        children = sorted(
            d for d in cwd.iterdir() if d.is_dir() and (d / ".devin").is_dir()
        )
    except OSError:
        children = []
    candidates += children[:_MAX_REPOS]

    findings: list[Finding] = []
    seen_devin = False
    for repo in candidates:
        devin_dir = repo / ".devin"
        if not devin_dir.is_dir():
            continue
        seen_devin = True
        prefix = repo.name if repo != cwd else ".devin"
        for name in _PROJECT_CONFIG_FILES:
            path = devin_dir / name
            if path.is_file():
                findings.append(
                    _file_finding(
                        f"{prefix}/{name}", path, _kind_for(name)
                    )
                )
    return findings, seen_devin


def _user_config_findings(ctx: Context) -> list[Finding]:
    config_root = ctx.config_dir or ctx.data_dir
    files = [
        (user_config_path(config_root), "config"),
        (user_mcp_config_path(config_root), "mcp"),
    ]
    existing = [(p, k) for p, k in files if p.is_file()]
    if not existing:
        return [
            Finding(
                CHECK_ID,
                Status.PASS,
                "no user-level config.json/mcp_config.json (defaults in "
                "effect)",
            )
        ]
    return [_file_finding(p.name, p, kind) for p, kind in existing]


def run(ctx: Context) -> list[Finding]:
    # User-level config belongs to the Devin install — skip it entirely on
    # a machine without one. Project ``.devin/`` files are checked
    # regardless: a no-Devin user can still share repos with Devin users.
    devin_present = ctx.data_dir.is_dir() or (
        ctx.config_dir is not None and ctx.config_dir.is_dir()
    )
    findings: list[Finding] = []
    if ctx.data_dir.is_dir() and cli_anchor_present(ctx.data_dir):
        # CLI credentials live under the CLI data root — a Desktop-only
        # install has no business reporting them missing. The bare root
        # is not evidence: on case-insensitive filesystems it unifies
        # with the Desktop config root.
        findings.append(_credentials_finding(credentials_path(ctx.data_dir)))
    if (ctx.config_dir or ctx.data_dir).is_dir():
        findings += _user_config_findings(ctx)
    project, seen_devin = _project_config_findings(ctx.cwd)
    findings += project
    if not seen_devin and devin_present:
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                f"no .devin/ config to check under {ctx.cwd}",
            )
        )
    return findings
