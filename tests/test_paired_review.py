"""Markdown-only review helpers; no model responses or operator approvals are simulated."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_paired_review_config import SKILL, review


@pytest.mark.parametrize("name", [
    "\u2603.md", "a" * 150 + ".md", "CON.txt", "CoN.md", "NUL.md", "COM1.md",
])
def test_unsafe_stems_have_portable_output_names(tmp_path, name):
    paths = review.review_files(tmp_path / name)
    assert Path(paths["plan"]).name.startswith("file-")
    assert len(Path(paths["plan"]).name) < 100
    assert Path(paths["plan"]).name.isascii()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name", [
    "SAMPLE-REVIEW-INPUT.md", "MixedCase_1.md", "UPPER.md",
    "claim-R01C.md", "claim-RP.md",
])
@pytest.mark.parametrize("handle", ["source", "plan"])
def test_original_spelling_is_used_for_inspection_discovery_and_cleanup(
    tmp_path, capsys, name, handle,
):
    source = tmp_path / name
    source.write_bytes(b"Original document.\r\n")
    original = source.read_bytes(), source.stat().st_mtime_ns
    stem = source.stem
    plan = tmp_path / f"{stem}-RP.md"
    replies = [tmp_path / f"{stem}-R{turn}.md" for turn in ("01C", "02A", "03C")]
    assert review.main([
        "inspect-review", str(source), "--author-role", "author",
        "--confirmed-role", "author", "--operator-instruction", "The author wrote it",
    ]) == 0
    inspection = json.loads(capsys.readouterr().out)
    assert inspection["firstReply"] == str(replies[0])
    assert inspection["plan"] == str(plan)
    for reply in reversed(replies):
        reply.write_text("Existing response", encoding="utf-8")
    plan.write_text(f"## Goal\n\nRefine [{name}]({name}).\n", encoding="utf-8")
    unrelated = tmp_path / "unrelated.md"
    unrelated.write_text("Keep", encoding="utf-8")
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    args = [str(source), "--as-source"] if handle == "source" else [str(plan)]
    assert review.main(["files", *args]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "plan": str(plan), "replies": list(map(str, replies)),
    }
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()} == before
    assert review.main(["clean", *args]) == 0
    assert json.loads(capsys.readouterr().out)["removed"] == list(map(str, [*replies, plan]))
    assert set(tmp_path.iterdir()) == {source, unrelated}
    assert (source.read_bytes(), source.stat().st_mtime_ns) == original


@pytest.mark.parametrize("operation", ["files", "inspect-review", "clean"])
@pytest.mark.parametrize("old_artifact", ["RP", "R01C"])
@pytest.mark.parametrize("named_history", [False, True])
def test_existing_hashed_uppercase_history_requires_explicit_selection(
    tmp_path, capsys, operation, old_artifact, named_history,
):
    # Earlier builds hashed the complete filename when the stem was uppercase.
    source = tmp_path / "SAMPLE-REVIEW-INPUT.md"
    source.write_text("Original document", encoding="utf-8")
    old_file = tmp_path / f"file-ada4cca8ea7621463229-{old_artifact}.md"
    old_file.write_text("Existing discussion", encoding="utf-8")
    if named_history:
        (tmp_path / "SAMPLE-REVIEW-INPUT-RP.md").write_text(
            "Another existing plan", encoding="utf-8",
        )
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    args = (["--author-role", "author"] if operation == "inspect-review" else ["--as-source"])
    assert review.main([operation, str(source), *args]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "needs-clarification" and old_file.name in error["error"]
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()} == before


def test_explicit_hashed_plan_preserves_existing_family_without_renaming(tmp_path):
    source = tmp_path / "SAMPLE-REVIEW-INPUT.md"
    source.write_text("Original document", encoding="utf-8")
    stem = "file-ada4cca8ea7621463229"
    plan = tmp_path / f"{stem}-RP.md"
    plan.write_text(f"## Goal\n\nRefine [{source.name}]({source.name}).\n", encoding="utf-8")
    reply = tmp_path / f"{stem}-R01C.md"
    reply.write_text("Existing response", encoding="utf-8")
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    assert review.review_files(plan) == {"plan": str(plan), "replies": [str(reply)]}
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()} == before


def test_different_input_formats_do_not_share_artifacts(tmp_path):
    names = {review.review_files(tmp_path / name)["plan"]
             for name in ("claim.md", "claim.txt", "claim.json")}
    assert len(names) == 3


def test_markdown_history_is_discovered_without_creating_state(tmp_path):
    source = tmp_path / "claim.md"
    source.write_bytes((SKILL / "assets/smoke-claim.md").read_bytes())
    replies = []
    for turn, role in [(1, "C"), (2, "A"), (3, "C")]:
        path = tmp_path / f"claim-R{turn:02}{role}.md"
        path.write_text("---\nmodel: test-model\nstatus: reviewing\n---\nResponse.\n", encoding="utf-8")
        replies.append(path)
    plan = tmp_path / "claim-RP.md"
    plan.write_text("---\nstatus: reviewing\n---\nCorrect the total.\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    for handle in (source, plan):
        result = review.review_files(handle)
        assert result == {"plan": str(plan), "replies": list(map(str, replies))}
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert all(p.is_file() for p in tmp_path.iterdir())


@pytest.mark.parametrize("status", ["reviewing", "approving", "applied"])
def test_clean_removes_only_discussion_and_never_applies_plan(tmp_path, capsys, status):
    source = tmp_path / "claim.md"
    original = (SKILL / "assets/smoke-claim.md").read_bytes()
    source.write_bytes(original)
    first = tmp_path / "claim-R01C.md"
    first.write_text("Correct the total from five to six.", encoding="utf-8")
    second = tmp_path / "claim-R02A.md"
    second.write_text("Agreed.", encoding="utf-8")
    plan = tmp_path / "claim-RP.md"
    plan.write_text(f"---\nstatus: {status}\n---\nCorrection task.\n", encoding="utf-8")
    unrelated = tmp_path / "other-R01C.md"
    unrelated.write_text("Keep", encoding="utf-8")
    settings = tmp_path / review.PROJECT_CONFIG_NAME
    settings.write_bytes((SKILL / "config.json").read_bytes())
    for attempt in range(2):
        assert review.main(["clean", str(plan)]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["removed"] == (list(map(str, [first, second, plan])) if attempt == 0 else [])
        assert set(tmp_path.iterdir()) == {source, unrelated, settings}
        assert source.read_bytes() == original and unrelated.read_text() == "Keep"


def test_interrupted_cleanup_can_remove_remaining_files(tmp_path, monkeypatch):
    first = tmp_path / "claim-R01C.md"
    plan = tmp_path / "claim-RP.md"
    for path in (first, plan):
        path.write_text("Review material", encoding="utf-8")
    unlink = Path.unlink

    def fail_plan(path, *args, **kwargs):
        if path == plan:
            raise OSError("Cleanup interrupted")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_plan)
        with pytest.raises(OSError, match="interrupted"):
            review.clean_review(plan)
    assert not first.exists() and plan.exists()
    assert review.clean_review(plan)["removed"] == [str(plan)]
    assert not list(tmp_path.iterdir())


def test_redirected_artifact_prevents_cleanup_before_deletion(tmp_path):
    first = tmp_path / "claim-R01C.md"
    first.write_text("Review", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "claim-RP.md"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(outside)],
                                capture_output=True, check=False)
        assert result.returncode == 0
    else:
        target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(review.ReviewError, match="Redirected"):
        review.clean_review(tmp_path / "claim.md", as_source=True)
    assert first.exists() and not list(outside.iterdir())


@pytest.mark.parametrize("author,critic,expected", [
    (None, None, {"author": "codex", "critic": "claude"}),
    ("claude", None, {"author": "claude", "critic": "codex"}),
    (None, "antigravity", {"author": "codex", "critic": "antigravity"}),
])
def test_model_role_selection(author, critic, expected):
    config = review.read_json(SKILL / "config.json")
    assert review.resolve_roles(config, author=author, critic=critic) == expected
    with pytest.raises(review.ReviewError):
        review.resolve_roles(config, "codex", "codex")


@pytest.mark.parametrize("letter", ["a", "A"])
def test_long_non_markdown_name_has_stable_plan_handle(tmp_path, letter):
    paths = review.review_files(tmp_path / (letter * 80 + ".txt"))
    assert Path(paths["plan"]).name.startswith(letter * 80 + "-")
    assert review.review_files(Path(paths["plan"])) == paths



@pytest.mark.parametrize("name,prompt,role", [
    ("claim.md", "author", "author"), ("claim.md", "critic", "critic"),
    ("claim-R01A.md", None, "author"), ("claim-R01C.md", None, "critic"),
    ("claim-R03A.md", "author", "author"), ("claim-R02C.md", "critic", "critic"),
])
def test_existing_review_is_separate_unchanged_source(tmp_path, capsys, name, prompt, role):
    source = tmp_path / name
    original = b"\xef\xbb\xbfInitial finding.\r\n"
    source.write_bytes(original)
    original_stat = source.stat()
    args = ["inspect-review", str(source)]
    if prompt:
        args += ["--author-role", prompt]
    assert review.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source"] == str(source) and result["authorRole"] == role
    assert result["nextRole"] == ("critic" if role == "author" else "author")
    assert Path(result["firstReply"]).name.endswith("-R01C.md" if role == "author" else "-R01A.md")
    assert Path(result["firstReply"]) != source
    assert source.read_bytes() == original and source.stat().st_mode == original_stat.st_mode
    assert source.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("name,role", [
    ("claim.md", None), ("claim-R01C.md", "author"), ("claim-R01A.md", "critic"),
])
def test_missing_or_conflicting_author_requires_clarification(tmp_path, capsys, name, role):
    source = tmp_path / name
    source.write_text("Original review", encoding="utf-8")
    args = ["inspect-review", str(source)]
    if role:
        args += ["--author-role", role]
    assert review.main(args) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "needs-clarification"
    assert source.read_text() == "Original review"
    assert list(tmp_path.iterdir()) == [source]


def test_operator_clarifies_role_without_renaming_source(tmp_path):
    source = tmp_path / "claim-R01C.md"
    source.write_text("Review", encoding="utf-8")
    with pytest.raises(review.ReviewError, match="clarification"):
        review.inspect_review(source, "author", "author")
    result = review.inspect_review(source, "author", "author", "The author wrote it")
    assert result["authorRole"] == "author" and result["nextRole"] == "critic"
    assert result["source"] == str(source)
    assert list(tmp_path.iterdir()) == [source]


def test_inspection_never_rewrites_existing_replies(tmp_path):
    source = tmp_path / "claim.md"
    source.write_text("Review", encoding="utf-8")
    result = review.inspect_review(source, "author")
    first, plan = Path(result["firstReply"]), Path(result["plan"])
    first.write_text("First actual dialogue turn", encoding="utf-8")
    plan.write_text("Agreed next steps", encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}
    assert review.inspect_review(source, "author") == result
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == before


@pytest.mark.parametrize("name", ["claim.md", "claim-R01A.md", "claim-RP.md", "\u2603.md"])
@pytest.mark.parametrize("role", ["author", "critic"])
def test_cleanup_preserves_original_review_and_accepted_plan(tmp_path, capsys, name, role):
    source = tmp_path / name
    source.write_text("Initial review", encoding="utf-8")
    result = review.inspect_review(source, role, role, "The stated role is correct")
    first, plan = Path(result["firstReply"]), Path(result["plan"])
    first.write_text("Verified finding and recommended correction", encoding="utf-8")
    plan.write_text("Transfer the accepted improvement plan into the original review", encoding="utf-8")
    # The source remains the output document after an authorized transfer.
    accepted = "# Review\n\n## Accepted improvement plan\n\nAdd idempotency.\n"
    source.write_text(accepted, encoding="utf-8")
    code = tmp_path / "payments.py"
    code.write_text("original implementation", encoding="utf-8")
    assert review.review_files(source, as_source=True)["plan"] == str(plan)
    assert review.review_files(plan)["replies"] == [str(first)]
    for attempt in range(2):
        assert review.main(["clean", str(source), "--as-source"]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["removed"] == ([str(first), str(plan)] if attempt == 0 else [])
        assert source.read_text() == accepted
        assert code.read_text() == "original implementation"
        assert set(tmp_path.iterdir()) == {source, code}


@pytest.mark.parametrize("operation", ["init", "update", "inspect-review"])
def test_source_path_override_is_rejected_for_other_commands(tmp_path, capsys, operation):
    assert review.main([operation, str(tmp_path / "claim.md"), "--as-source"]) == 1
    assert "only valid" in json.loads(capsys.readouterr().err)["error"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name", ["claim-R01C.md", "claim-R02A.md", "claim-RP.md"])
@pytest.mark.parametrize("evidence", ["plan", "reply"])
@pytest.mark.parametrize("handle", ["source", "plan"])
def test_cleanup_refuses_files_used_as_another_reviews_source(
    tmp_path, capsys, name, evidence, handle,
):
    source = tmp_path / "claim.md"
    source.write_text("Original intent", encoding="utf-8")
    plan = tmp_path / "claim-RP.md"
    plan.write_text("## Goal\n\nRefine [claim.md](claim.md).\n", encoding="utf-8")
    for turn in ("01C", "02A"):
        (tmp_path / f"claim-R{turn}.md").write_text("Review response", encoding="utf-8")
    other_source = tmp_path / name
    other_source.write_text("Independent review and accepted improvement plan", encoding="utf-8")
    other = review.inspect_review(other_source, "critic", "critic", "The critic wrote it")
    if evidence == "plan":
        Path(other["plan"]).write_text(
            f"## Goal\n\nImprove functionality in [{name}]({name}).\n", encoding="utf-8",
        )
    else:
        Path(other["firstReply"]).write_text("Remaining independent reply", encoding="utf-8")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in tmp_path.iterdir()}

    args = ["clean", str(source), "--as-source"] if handle == "source" else ["clean", str(plan)]
    assert review.main(args) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "needs-clarification" and str(other_source) in error["error"]
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in tmp_path.iterdir()} == before


@pytest.mark.skipif(os.name != "nt", reason="Windows path aliases")
@pytest.mark.parametrize("name", ["claim.md", "Claim.md"])
@pytest.mark.parametrize("handle", ["source", "plan"])
def test_windows_case_aliases_reuse_existing_review_and_cleanup(tmp_path, name, handle):
    source = tmp_path / name
    source.write_text("Original review", encoding="utf-8")
    original = source.read_bytes(), source.stat().st_mtime_ns
    alias = source.with_name(source.name.swapcase())
    assert source.samefile(alias)
    inspection = review.inspect_review(source, "author")
    first, plan = Path(inspection["firstReply"]), Path(inspection["plan"])
    first.write_text("Response", encoding="utf-8")
    plan.write_text(f"## Goal\n\nRefine [{name}]({name}).\n", encoding="utf-8")
    plan_alias = plan.with_name(plan.name.swapcase())
    expected = {"plan": str(plan), "replies": [str(first)]}

    assert review.inspect_review(alias, "author") == inspection
    assert review.review_files(alias, as_source=True) == expected
    assert review.review_files(plan_alias) == expected
    result = (review.clean_review(alias, as_source=True) if handle == "source"
              else review.clean_review(plan_alias))
    assert result["removed"] == [str(first), str(plan)]
    assert list(tmp_path.iterdir()) == [source]
    assert (source.read_bytes(), source.stat().st_mtime_ns) == original


def test_case_sensitive_files_keep_distinct_review_families(tmp_path):
    lower, upper = tmp_path / "claim.md", tmp_path / "CLAIM.MD"
    lower.write_text("First source", encoding="utf-8")
    if upper.exists() and lower.samefile(upper):
        pytest.skip("The filesystem does not distinguish case")
    upper.write_text("Second source", encoding="utf-8")
    lower_plan = Path(review.review_files(lower, as_source=True)["plan"])
    upper_plan = Path(review.review_files(upper, as_source=True)["plan"])
    assert lower_plan != upper_plan
    lower_plan.write_text("First plan", encoding="utf-8")
    upper_plan.write_text("Second plan", encoding="utf-8")
    assert review.clean_review(lower, as_source=True)["removed"] == [str(lower_plan)]
    assert upper_plan.read_text() == "Second plan"
    assert lower.read_text() == "First source" and upper.read_text() == "Second source"
