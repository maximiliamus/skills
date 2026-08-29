from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    assert (skills_dir / "commit-bulk-changes" / "README.md").is_file()
    assert (skills_dir / "commit-bulk-changes" / "SKILL.md").is_file()
    assert (skills_dir / "commit-bulk-changes" / "agents" / "openai.yaml").is_file()
    assert (skills_dir / "execute-guided-runbook" / "README.md").is_file()
    assert (skills_dir / "execute-guided-runbook" / "SKILL.md").is_file()
    assert (skills_dir / "execute-guided-runbook" / "agents" / "openai.yaml").is_file()
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
    for skill_dir in [skills_dir / "commit-bulk-changes", skills_dir / "execute-guided-runbook"]:
        skill_md = skill_dir / "SKILL.md"
        assert skill_md.is_file()
        fm = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        assert "name" in fm
        assert "description" in fm
        assert fm["name"] == skill_dir.name


def test_openai_yaml_manifests():
    skills_dir = ROOT / "skills"
    for skill_name in ["commit-bulk-changes", "execute-guided-runbook"]:
        yaml_file = skills_dir / skill_name / "agents" / "openai.yaml"
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
