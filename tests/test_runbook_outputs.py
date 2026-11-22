"""Named prose outputs through reservation, publication and close."""

import pytest
from test_runbook_session import EVIDENCE, Client, artifact, call, inferred_inventory
from test_runbook_session import repo as repo  # noqa: PLC0414 -- shared pytest fixture


def test_direct_prose_binds_and_verifies_every_output_through_close(repo):
    procedure = """\
# Audit

Write the preamble scratch file to `preamble.md`.

<!-- step: inspect -->
## Inspect inputs

Read the existing `report.md`.
The input inventory mentions `schema.json`.

<!-- step: report -->
## Record findings

> Write the quoted report to `quoted.md`.
Write the quoted continuation to `quoted-continuation.md`.

```markdown
Write the report to `fenced.md`.
```

<blockquote>
Write the HTML quotation to `html-quoted.md`.
</blockquote>

Example:

Write the report to `example-paragraph.md`.

### Example format

Write the report to `example-section.md`.

### Required work

Optionally write a summary to `optional.md`.
Write an optional summary to `also-optional.md`.
Write the appendix to `conditional.md` if asked.

Write the final audit report to `report.md`.
- Write the supporting evidence to `evidence/coverage.json`.
"""
    client = Client(repo, procedure)
    client.initial.pop("stepContracts")
    (repo / "report.md").write_bytes(b"Existing input")
    view = client.reserve()
    prefix = view["artifactsDirectory"]
    report = f"{prefix}/report.md"
    coverage = f"{prefix}/evidence/coverage.json"
    assert [step["deliverables"] for step in view["contract"]["steps"]] == [
        [], [report, coverage],
    ]
    assert client.reserve()["duplicate"]
    client.result(client.admit("inspect"))
    client.finish(code="BLOCKED")
    attempt = client.admit("report")

    with_report = client.stage(attempt, report, "Verified audit report")
    missing = client.result(attempt, deliverables=[with_report], code="BLOCKED")
    assert missing["details"]["paths"] == [report, coverage]
    with_coverage = client.stage(attempt, coverage, '{"checks": 4}')
    wrong_hash = with_coverage | {"sha256": "0" * 64}
    assert "Staged bytes changed" in client.result(
        attempt, deliverables=[with_report, wrong_hash], code="BLOCKED",
    )["message"]
    report_draft = repo / with_report["stagedPath"]
    report_draft.unlink()
    assert "Staged bytes changed" in client.result(
        attempt, deliverables=[with_report, with_coverage], code="BLOCKED",
    )["message"]
    assert not (repo / report).exists() and not (repo / coverage).exists()

    with_report = client.stage(attempt, report, "Verified audit report")
    request = client.request("observe", {
        "attempt": attempt,
        "kind": "result",
        "outcome": "PASS",
        "evidence": EVIDENCE,
        "deliverables": [with_report, with_coverage],
    })
    published = call(repo, "change", client.run, payload=request)
    assert published["steps"][1]["status"] == "done"
    assert call(repo, "change", client.run, payload=request)["duplicate"]
    assert (repo / "report.md").read_bytes() == b"Existing input"
    assert artifact(repo, report)["sha256"] == with_report["sha256"]
    assert artifact(repo, coverage)["sha256"] == with_coverage["sha256"]

    report_file = repo / report
    coverage_file = repo / coverage
    report_bytes = report_file.read_bytes()
    coverage_bytes = coverage_file.read_bytes()
    report_file.write_bytes(b"Altered after verification")
    client.status()
    assert client.finish(code="BLOCKED")["details"]["steps"] == ["report"]
    report_file.write_bytes(report_bytes)
    coverage_file.unlink()
    assert client.finish(code="BLOCKED")["details"]["steps"] == ["report"]
    coverage_file.write_bytes(coverage_bytes)
    assert client.finish()["result"] == "PASSED"


@pytest.mark.parametrize(
    ("intro", "following"),
    [
        ("As an example, this is only illustrative:", "### When verification finishes\n\n"),
        ("Examples\n--------\n", "Required results\n----------------\n\n"),
        ("If an appendix is requested, follow this optional instruction:", ""),
    ],
)
def test_example_and_conditional_blocks_do_not_create_required_outputs(repo, intro, following):
    procedure = (
        "# Audit\n\n<!-- step: report -->\n## Record findings\n\n"
        f"{intro}\nWrite the report to `illustrative.md`.\n\n"
        f"{following}Write the final report to `report.md`.\n"
    )
    client = Client(repo, procedure)
    line = next(
        index for index, text in enumerate(procedure.splitlines(), 1) if "illustrative.md" in text
    )
    client.initial["stepContracts"] = {"report": {"outputs": [{
        "line": line, "path": "illustrative.md", "scope": "runbook",
        "sha256": artifact(repo, client.relative)["sha256"],
    }]}}
    client.reserve(code="INVALID_REQUEST")
    del client.initial["stepContracts"]
    view = client.reserve()
    [required] = view["contract"]["steps"][0]["deliverables"]
    assert required == f"{view['artifactsDirectory']}/report.md"
    attempt = client.admit("report")
    client.result(attempt, deliverables=[client.stage(attempt, required, "Verified report")])
    assert client.finish()["phase"] == "finalized"


def test_multi_paragraph_example_ends_at_next_heading(repo):
    procedure = """\
# Audit

<!-- step: report -->
## Record findings

As an example, consider these two instructions:

Write the first output to `sample-one.md`.

Write the second output to `sample-two.md`.

### Required output

Write the final report to `report.md`.
"""
    client = Client(repo, procedure)
    line = next(n for n, text in enumerate(procedure.splitlines(), 1) if "sample-two.md" in text)
    client.initial["stepContracts"] = {"report": {"outputs": [{
        "line": line, "path": "sample-two.md", "scope": "runbook",
        "sha256": artifact(repo, client.relative)["sha256"],
    }]}}
    client.reserve(code="INVALID_REQUEST")
    client.initial.pop("stepContracts")
    view = client.reserve()
    [required] = view["contract"]["steps"][0]["deliverables"]
    assert required == f"{view['artifactsDirectory']}/report.md"
    attempt = client.admit("report")
    client.result(attempt, deliverables=[client.stage(attempt, required, "Verified report")])
    assert client.finish()["phase"] == "finalized"


@pytest.mark.parametrize("spacing", ["", "\n"], ids=["adjacent", "blank-line"])
@pytest.mark.parametrize(
    "fenced_example",
    [
        "~~~markdown\nWrite the report to `example.md`.\n~~~\n",
        (
            "````markdown\n```markdown\nWrite the report to `inner.md`.\n```\n"
            "Write the report to `example.md`.\n````\n"
        ),
    ],
    ids=["tilde-fence", "nested-backticks"],
)
def test_fenced_example_leaves_following_prose_operative(repo, fenced_example, spacing):
    procedure = (
        "# Audit\n\n<!-- step: report -->\n## Record findings\n\n"
        f"Example:\n\n{fenced_example}{spacing}Write the final report to `report.md`.\n"
    )
    client = Client(repo, procedure)
    view = client.reserve()
    required = f"{view['artifactsDirectory']}/report.md"
    assert view["contract"]["steps"][0]["deliverables"] == [required]
    attempt = client.admit("report")
    client.result(attempt, code="BLOCKED")
    client.result(attempt, deliverables=[client.stage(attempt, required, "Verified report")])
    assert client.finish()["result"] == "PASSED"


@pytest.mark.parametrize(
    ("condition", "scope"),
    [
        ("### If you are running this release audit", "step"),
        ("If you are running this release audit\n-------------------------------------", "runbook"),
        ("If you are running this release audit:", "step"),
    ],
)
def test_coordinator_can_cite_output_in_applicable_conditional_branch(repo, condition, scope):
    procedure = (
        "# Release audit\n\n<!-- step: report -->\n## Record findings\n\n"
        "### If this is a standalone audit\n\n"
        "Write the standalone summary to `standalone.md`.\n\n"
        "### Release report\n\n"
        f"{condition}\n\nWrite the mandatory release report to `report.md`.\n"
    )
    client = Client(repo, procedure)
    line = next(n for n, text in enumerate(procedure.splitlines(), 1) if "`report.md`" in text)
    client.initial["stepContracts"] = {"report": {"outputs": [{
        "line": line, "path": "report.md", "scope": scope,
        "sha256": artifact(repo, client.relative)["sha256"],
    }]}}
    view = client.reserve()
    [required] = view["contract"]["steps"][0]["deliverables"]
    assert required == f"{view['artifactsDirectory']}/report.md"
    attempt = client.admit("report")
    client.result(attempt, code="BLOCKED")
    client.result(attempt, deliverables=[client.stage(attempt, required, "Verified release report")])
    assert client.finish()["phase"] == "finalized"


def test_coordinator_cites_freeform_nested_prose_and_external_output(repo):
    procedure = """\
# Audit

1. Inspect inputs
   - Read `inventory.json`.
2. Record findings
   - The completed audit must include evidence/coverage.json.
   - Hand off the checked summary at `repo:exports/summary.md`.
   - Optional
     - Write a draft to `draft.md`.

   ~~~markdown
   Write the example to `example.md`.
   ~~~
"""
    client = Client(repo, procedure)
    client.initial["stepInventory"] = inferred_inventory(
        client, ("inspect", "1. Inspect inputs"), ("record", "2. Record findings"),
    )
    source_hash = artifact(repo, client.relative)["sha256"]

    def citation(path):
        number = next(n for n, line in enumerate(procedure.splitlines(), 1) if path in line)
        return {"path": path, "line": number, "sha256": source_hash}

    coverage = citation("evidence/coverage.json")
    summary = citation("repo:exports/summary.md")
    outputs = [coverage, summary]
    client.initial["stepContracts"] = {"inspect": {"outputs": []}, "record": {"outputs": outputs}}
    for rejected in (
        citation("inventory.json"), citation("example.md"), citation("draft.md"),
        coverage | {"line": summary["line"]},
    ):
        outputs.append(rejected)
        client.reserve(code="INVALID_REQUEST")
        outputs.pop()
    outputs[0] = coverage | {"sha256": "0" * 64}
    assert "changed" in client.reserve(code="CONFLICT")["message"]
    outputs[0] = coverage
    assert not client.path_to_journal.exists()

    view = client.reserve()
    private = f"{view['artifactsDirectory']}/evidence/coverage.json"
    assert [step["deliverables"] for step in view["contract"]["steps"]] == [
        [], [private, "exports/summary.md"],
    ]
    client.result(client.admit("inspect"))
    attempt = client.admit("record")
    client.result(attempt, deliverables=[
        client.stage(attempt, private, "{}"),
        client.stage(attempt, "exports/summary.md", "Verified summary"),
    ])
    assert client.finish()["result"] == "PASSED"


def test_legacy_marker_and_prose_share_the_same_frozen_output(repo):
    client = Client(repo, """\
# Audit

<!-- step: report -->
## Write report

Write the final audit report to `report.md`.

<!-- deliverable: report.md -->
""")
    view = client.reserve()
    assert view["contract"]["steps"][0]["deliverables"] == [
        f"{view['artifactsDirectory']}/report.md",
    ]


def test_shared_requirement_can_assign_output_to_step_without_repeating_the_name(repo):
    procedure = """\
---
description: 'Frontmatter mentions `frontmatter.md`.'
---
# Audit

## Example results

The Record findings step would write `example.md` in this example.

## Required results

The Record findings step must produce the final audit report at `report.md`.

<!-- step: inspect -->
## Inspect inputs

Verify the candidate inputs.

<!-- step: record -->
## Record findings

Write the required report using the verified findings.
"""
    client = Client(repo, procedure)
    source_hash = artifact(repo, client.relative)["sha256"]

    def citation(path):
        return {
            "line": next(n for n, line in enumerate(procedure.splitlines(), 1) if path in line),
            "path": path, "sha256": source_hash, "scope": "runbook",
        }

    required = citation("report.md")
    outputs = [{**required, "scope": "step"}]
    client.initial["stepContracts"] = {"inspect": {"outputs": []}, "record": {"outputs": outputs}}
    for invalid in (outputs[0], citation("frontmatter.md"), citation("example.md")):
        outputs[:] = [invalid]
        client.reserve(code="INVALID_REQUEST")
    outputs[:] = [required]

    view = client.reserve()
    path = f"{view['artifactsDirectory']}/report.md"
    assert [step["deliverables"] for step in view["contract"]["steps"]] == [[], [path]]
    client.result(client.admit("inspect"))
    attempt = client.admit("record")
    client.result(attempt, code="BLOCKED")
    client.result(attempt, deliverables=[client.stage(attempt, path, "Audit complete")])
    assert client.finish()["result"] == "PASSED"


@pytest.mark.parametrize("supply_other_output", [False, True])
def test_coordinator_list_cannot_remove_a_direct_requirement(repo, supply_other_output):
    client = Client(repo, """\
# Audit

<!-- step: report -->
## Record findings

Write the final audit report to `report.md`.
The finished audit must include manifest.json.
""")
    outputs = []
    if supply_other_output:
        outputs.append({
            "path": "manifest.json", "line": 7, "sha256": artifact(repo, client.relative)["sha256"],
        })
    client.initial["stepContracts"] = {"report": {"outputs": outputs}}
    view = client.reserve()
    folder = view["artifactsDirectory"]
    required = [f"{folder}/report.md"]
    if supply_other_output:
        required.append(f"{folder}/manifest.json")
    assert view["contract"]["steps"][0]["deliverables"] == required
    attempt = client.admit("report")
    staged = [client.stage(attempt, path, "Verified manifest") for path in required[1:]]
    assert client.result(attempt, deliverables=staged, code="BLOCKED")["details"]["paths"] == required


@pytest.mark.parametrize("coordinated", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_ordered_step_can_name_file_in_its_anchor_or_list_body(repo, coordinated, nested):
    directive = "Write the final audit report to `report.md`."
    anchor = "1. Record findings" if nested else f"1. {directive}"
    body = f"{anchor}\n"
    if nested:
        body += (
            "   - Optional outputs:\n\n"
            "     - Write a draft to `draft.md`.\n"
            f"   - {directive}\n"
        )
    client = Client(repo, f"# Audit\n\n{body}")
    client.initial["stepInventory"] = inferred_inventory(client, ("report", anchor))
    if coordinated:
        client.initial["stepContracts"] = {
            "report": {"outputs": [{
                "path": "report.md",
                "line": 7 if nested else 3,
                "sha256": artifact(repo, client.relative)["sha256"],
            }]},
        }
    view = client.reserve()
    assert view["contract"]["steps"][0]["deliverables"] == [
        f"{view['artifactsDirectory']}/report.md",
    ]
