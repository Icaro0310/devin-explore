"""Skill/plugin surface contract: the shipped skill exists, has valid
frontmatter, and the plugin manifest is self-consistent."""

import json
from pathlib import Path

ADAPTERS = Path(__file__).parents[1] / "adapters"
SKILL = ADAPTERS / "skills" / "devin-history" / "SKILL.md"
MANIFEST = ADAPTERS / ".devin-plugin" / "plugin.json"


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), "SKILL.md missing frontmatter"
    block = text.split("---", 2)[1]
    out = {}
    for line in block.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def test_skill_exists_with_required_frontmatter():
    assert SKILL.is_file()
    fm = _frontmatter(SKILL)
    assert fm["name"] == "devin-history"
    assert fm["description"]


def test_skill_is_read_only_by_text():
    """The skill must not instruct the agent to change any store."""
    body = SKILL.read_text(encoding="utf-8").lower()
    assert "read-only" in body
    for banned in ("--apply", "--fix", "--repair", "--delete", "vacuum"):
        assert banned not in body


def test_plugin_manifest_self_consistent():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["name"] == "devin-history"
    # skills resolve under the plugin root with the default convention.
    assert (ADAPTERS / "skills" / "devin-history" / "SKILL.md").is_file()
    servers = manifest.get("mcpServers", {})
    assert "devin-history" in servers
    # the declared server must be the package's own entrypoint.
    assert "devin-history-mcp" in json.dumps(servers["devin-history"])
