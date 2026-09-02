from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_SKILL_NAMES = [
    "commit-bulk-changes",
    "execute-guided-runbook",
]
INTERNAL_SKILL_NAMES = [
    "bump-skills-version",
    "release-skills",
]


def parse_frontmatter(content: str) -> dict[str, str]:
    if not content.startswith("---"):
        return {}
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    lines = parts[1].strip().splitlines()
    data: dict[str, str] = {}
    for line in lines:
        if ":" in line:
            key, val = line.split(":", 1)
            data[key.strip()] = val.strip()
    return data


def test_skills_exist():
    skills_dir = ROOT / "skills"
    internal_skills_dir = ROOT / ".agents" / "skills"
    for skill_name in PUBLIC_SKILL_NAMES:
        assert (skills_dir / skill_name / "SKILL.md").is_file()
        assert (skills_dir / skill_name / "agents" / "openai.yaml").is_file()
    for skill_name in INTERNAL_SKILL_NAMES:
        assert (internal_skills_dir / skill_name / "SKILL.md").is_file()
        assert (internal_skills_dir / skill_name / "agents" / "openai.yaml").is_file()
    assert (skills_dir / "commit-bulk-changes" / "README.md").is_file()
    assert (skills_dir / "execute-guided-runbook" / "README.md").is_file()
    assert (
        internal_skills_dir
        / "bump-skills-version"
        / "scripts"
        / "bump_skills_version.py"
    ).is_file()
    assert (
        internal_skills_dir
        / "release-skills"
        / "references"
        / "release-process.md"
    ).is_file()
    assert not (ROOT / "docs" / "RELEASING.md").exists()
    runbook_scripts = skills_dir / "execute-guided-runbook" / "scripts"
    assert (runbook_scripts / "runbook_session.py").is_file()
    runtime_package = runbook_scripts / "execute_guided_runbook_lib"
    assert not (runbook_scripts / "lib").exists()
    for module_name in ["cli.py", "model.py", "registry.py", "state.py", "storage.py"]:
        assert (runtime_package / module_name).is_file()
    assert (
        skills_dir / "execute-guided-runbook" / "references" / "runbook-registry.schema.json"
    ).is_file()


def test_skills_frontmatter():
    skills_dir = ROOT / "skills"
    internal_skills_dir = ROOT / ".agents" / "skills"
    skill_dirs = [skills_dir / skill_name for skill_name in PUBLIC_SKILL_NAMES]
    skill_dirs.extend(
        internal_skills_dir / skill_name for skill_name in INTERNAL_SKILL_NAMES
    )
    for skill_dir in skill_dirs:
        skill_md = skill_dir / "SKILL.md"
        assert skill_md.is_file()
        fm = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        assert "name" in fm
        assert "description" in fm
        assert fm["name"] == skill_dir.name


def test_openai_yaml_manifests():
    skills_dir = ROOT / "skills"
    internal_skills_dir = ROOT / ".agents" / "skills"
    skill_roots = [(skills_dir, name) for name in PUBLIC_SKILL_NAMES]
    skill_roots.extend((internal_skills_dir, name) for name in INTERNAL_SKILL_NAMES)
    for skill_root, skill_name in skill_roots:
        yaml_file = skill_root / skill_name / "agents" / "openai.yaml"
        assert yaml_file.is_file()
        text = yaml_file.read_text(encoding="utf-8")
        assert "interface:" in text
        assert "display_name:" in text
        fields: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if ":" not in stripped:
                continue
            key, raw_value = stripped.split(":", 1)
            if key in {"short_description", "default_prompt"}:
                fields[key] = json.loads(raw_value.strip())

        assert 25 <= len(fields["short_description"]) <= 64
        assert f"${skill_name}" in fields["default_prompt"]


def test_initial_commit_instructions_preserve_requested_scope():
    skill_path = ROOT / "skills" / "commit-bulk-changes" / "SKILL.md"
    content = skill_path.read_text(encoding="utf-8")
    normalized_content = " ".join(content.split())

    assert "absence of `HEAD` never expands the authorized scope" in normalized_content
    assert "Never use `git add --all` for a narrower request" in normalized_content


def test_runbook_registry_schema_restricts_paths():
    schema_path = (
        ROOT / "skills" / "execute-guided-runbook" / "references" / "runbook-registry.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    pattern = schema["properties"]["runbooks"]["items"]["properties"]["path"]["pattern"]

    assert re.search(pattern, "docs/release.md")
    assert re.search(pattern, r"docs\release.md")
    for invalid_path in [
        "/tmp/release.md",
        r"C:\tmp\release.md",
        "docs/release.txt",
        " docs/release.md",
        "docs/release.md\n",
        "../release.md",
        "docs/../release.md",
    ]:
        assert not re.search(pattern, invalid_path)

    item_properties = schema["properties"]["runbooks"]["items"]["properties"]
    assert re.search(item_properties["id"]["pattern"], "release")
    assert not re.search(item_properties["id"]["pattern"], "path-0123456789ab")
    assert not re.search(item_properties["title"]["pattern"], "   ")
    assert not re.search(item_properties["description"]["pattern"], "\t")


def test_release_skill_preserves_release_safety_boundaries():
    skill_content = (
        ROOT / ".agents" / "skills" / "release-skills" / "SKILL.md"
    ).read_text(encoding="utf-8")
    reference_content = (
        ROOT
        / ".agents"
        / "skills"
        / "release-skills"
        / "references"
        / "release-process.md"
    ).read_text(encoding="utf-8")
    normalized_skill = " ".join(skill_content.split())
    normalized_contract = " ".join((skill_content + reference_content).split())

    assert "references/release-process.md" in normalized_skill
    assert "$bump-skills-version" in normalized_contract
    assert "skills/commit-bulk-changes/SKILL.md" in normalized_contract
    assert "python -m pytest" in normalized_contract
    assert "python -m ruff check" in normalized_contract
    assert "markdownlint-cli2" in normalized_contract
    assert "git tag -a vX.Y.Z" in normalized_contract
    assert "git push --atomic origin master vX.Y.Z" in normalized_contract
    assert 'project].name = "maximiliamus-skills"' in normalized_contract
    assert "maximiliamus/skills" in normalized_contract
    assert "does not create a GitHub Release" in normalized_contract
    assert "do not delete, recreate, or retry" in normalized_contract
