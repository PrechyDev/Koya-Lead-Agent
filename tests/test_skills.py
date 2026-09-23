"""The five skills keep every rule from their source guides (build step 5.1)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "agent_plugin" / "skills"
ASSETS = ROOT.parent / "aat-c3-week-5-lead-agent-main" / "assets"

PAIRS = {
    "icp-refinement": "icp-refinement-guide.md",
    "lead-qualification": "lead-qualification-guide.md",
    "outbound-copywriting": "outbound-copywriting-guide.md",
    "lead-list-quality": "lead-list-quality-guide.md",
    "outreach-safety": "outreach-safety-guide.md",
}


def _rule_lines(markdown: str) -> list[str]:
    """Bullet and table lines from a guide: the actual rules."""
    lines = []
    for line in markdown.splitlines():
        s = line.strip()
        if re.match(r"^[-*] ", s) or (s.startswith("|") and not set(s) <= set("|- ")):
            lines.append(s)
    return lines


@pytest.mark.parametrize("skill,guide", PAIRS.items())
def test_skill_has_frontmatter(skill, guide):
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front = text.split("---", 2)[1]
    assert f"name: {skill}" in front and "description:" in front


@pytest.mark.parametrize("skill,guide", PAIRS.items())
def test_no_guide_rule_was_dropped(skill, guide):
    source = ASSETS / guide
    if not source.exists():
        pytest.skip("source guides not available (outside the repo)")
    skill_text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    missing = [line for line in _rule_lines(source.read_text(encoding="utf-8")) if line not in skill_text]
    assert missing == [], f"{skill} is missing rules from {guide}: {missing}"
