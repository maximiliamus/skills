"""Command-line interface for guided runbook sessions."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .model import OperatorDecisionRequired, RunbookError
from .model_binding import check_resume_request, resolve_binding, verify_model_id
from .registry import (
    command_list,
    command_register,
    command_unregister,
    load_registry,
    resolve_runbook,
)
from .state import (
    command_block,
    command_complete,
    command_finish,
    command_prune,
    command_rebind,
    command_skip,
    command_start,
    command_step,
    load_state,
    migrate_path_identity_session,
    read_state,
    require_active_state,
)
from .storage import (
    configure_standard_streams,
    find_repo_root,
    interprocess_lock,
    path_runbook_id,
    print_json,
    repository_registry_path,
    session_locks,
    state_payload,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage resumable local sessions for repository runbooks."
    )
    parser.add_argument("--repo-root", type=Path, default=None)

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List registered runbooks")
    register_parser = subparsers.add_parser("register", help="Register a runbook")
    register_parser.add_argument("runbook_id")
    register_parser.add_argument("path")
    register_parser.add_argument("--title")
    register_parser.add_argument("--description")

    unregister_parser = subparsers.add_parser("unregister", help="Unregister a runbook")
    unregister_parser.add_argument("runbook_id")

    resolve_parser = subparsers.add_parser("resolve", help="Resolve a runbook selector")
    resolve_parser.add_argument("selector")

    run_parser = subparsers.add_parser("run", help="Start or resume a runbook session")
    run_parser.add_argument("selector")
    run_parser.add_argument("--model-family")
    run_parser.add_argument("--model-id", required=True)
    run_options = run_parser.add_mutually_exclusive_group()
    run_options.add_argument("--restart", action="store_true")
    run_options.add_argument("--continue", dest="continue_outdated", action="store_true")
    run_options.add_argument("--ignore", dest="ignore_outdated", action="store_true")

    rebind_parser = subparsers.add_parser(
        "rebind", help="Apply an operator-authorized model change to a session"
    )
    rebind_parser.add_argument("selector")
    rebind_parser.add_argument("--model-family", required=True)
    rebind_parser.add_argument("--model-id", required=True)

    status_parser = subparsers.add_parser("status", help="Show a runbook session")
    status_parser.add_argument("selector")

    prune_parser = subparsers.add_parser(
        "prune",
        help="Explicitly remove archived sessions for one runbook",
    )
    prune_parser.add_argument("selector")
    prune_policy = prune_parser.add_mutually_exclusive_group(required=True)
    prune_policy.add_argument("--keep-last", type=int)
    prune_policy.add_argument("--older-than-days", type=int)
    prune_parser.add_argument("--dry-run", action="store_true")

    step_parser = subparsers.add_parser("step", help="Set the unresolved step")
    step_parser.add_argument("selector")
    step_parser.add_argument("step_id")
    step_parser.add_argument("--title", required=True)
    step_parser.add_argument("--retry", action="store_true")
    step_parser.add_argument("--model-id", required=True)

    complete_parser = subparsers.add_parser("complete", help="Complete the current step")
    complete_parser.add_argument("selector")
    complete_parser.add_argument("--evidence", required=True)
    complete_parser.add_argument("--result", choices=["pass", "fail"])
    complete_parser.add_argument("--score")
    complete_parser.add_argument("--model-id", required=True)

    block_parser = subparsers.add_parser("block", help="Record a blocker on the current step")
    block_parser.add_argument("selector")
    block_parser.add_argument("--reason", required=True)
    block_parser.add_argument("--model-id", required=True)

    skip_parser = subparsers.add_parser("skip", help="Skip the current step with a reason")
    skip_parser.add_argument("selector")
    skip_parser.add_argument("--reason", required=True)
    skip_parser.add_argument("--score")
    skip_parser.add_argument("--model-id", required=True)

    finish_parser = subparsers.add_parser("finish", help="Finish the runbook session")
    finish_parser.add_argument("selector")
    finish_parser.add_argument("--evidence", required=True)
    finish_parser.add_argument("--expected-step", action="append")
    finish_parser.add_argument("--decision", choices=["accept", "reject"])
    finish_parser.add_argument("--model-id", required=True)
    return parser


def main() -> int:
    configure_standard_streams()
    args = build_parser().parse_args()
    try:
        repo_root = (args.repo_root or find_repo_root()).resolve()
        if not repo_root.is_dir():
            raise RunbookError(f"Repository root is not a directory: {repo_root}")
        registry_path = repository_registry_path(repo_root)
        if args.command == "list" and not registry_path.exists():
            command_list({})
            return 0

        with interprocess_lock(repo_root, registry_path):
            registry_enabled = registry_path.is_file()
            registry = load_registry(registry_path, repo_root)
            if args.command == "list":
                command_list(registry, repo_root)
                return 0
            if args.command == "register":
                command_register(
                    registry_path,
                    registry,
                    repo_root,
                    args.runbook_id,
                    args.path,
                    args.title,
                    args.description,
                )
                return 0
            if args.command == "unregister":
                command_unregister(registry_path, registry, args.runbook_id)
                return 0

            runbook = resolve_runbook(
                args.selector,
                repo_root,
                registry,
                registry_enabled,
            )
            if args.command == "resolve":
                print_json(runbook)
                return 0
            model_request = {
                "modelFamily": getattr(args, "model_family", None),
                "modelId": getattr(args, "model_id", None),
            }

            def validate_session_candidate(source: Path, stored_id: object) -> None:
                read_state(source, {"id": stored_id})

            def before_migration(source: Path, runbook_id: str) -> None:
                saved = read_state(source, {"id": runbook_id})
                if args.command == "rebind":
                    require_active_state(saved, runbook_id)
                    resolve_binding(
                        {
                            **runbook,
                            "modelFamily": saved["modelFamily"],
                            "modelTier": saved["modelTier"],
                            "effortLevel": saved["effortLevel"],
                        },
                        model_request,
                    )
                    return
                check_resume_request(saved, model_request)
                verify_model_id(saved, args.model_id)

            legacy_path_id = path_runbook_id(runbook["relativePath"])
            session_ids = [runbook["id"]]
            if not runbook["registered"] and legacy_path_id != runbook["id"]:
                session_ids.append(legacy_path_id)
            with session_locks(
                repo_root,
                session_ids,
                validate_candidate=validate_session_candidate,
                before_migration=(
                    None if args.command in {"status", "prune"} else before_migration
                ),
            ) as state_paths:
                if legacy_path_id in state_paths:
                    migrate_path_identity_session(runbook, legacy_path_id, state_paths)
                path = state_paths[runbook["id"]]
                if args.command == "run":
                    command_start(
                        runbook,
                        repo_root,
                        args.restart,
                        args.continue_outdated,
                        args.ignore_outdated,
                        model_request,
                        args.model_id,
                    )
                elif args.command == "status":
                    print_json(
                        state_payload(
                            path,
                            load_state(path, runbook, allow_completed_outdated=True),
                        )
                    )
                elif args.command == "prune":
                    command_prune(
                        runbook,
                        repo_root,
                        args.keep_last,
                        args.older_than_days,
                        args.dry_run,
                    )
                elif args.command == "rebind":
                    command_rebind(
                        runbook,
                        repo_root,
                        args.model_family,
                        args.model_id,
                    )
                elif args.command == "step":
                    command_step(
                        runbook,
                        repo_root,
                        args.step_id,
                        args.title,
                        args.retry,
                        args.model_id,
                    )
                elif args.command == "complete":
                    command_complete(
                        runbook,
                        repo_root,
                        args.evidence,
                        args.result,
                        args.score,
                        args.model_id,
                    )
                elif args.command == "block":
                    command_block(
                        runbook, repo_root, args.reason, args.model_id,
                    )
                elif args.command == "skip":
                    command_skip(
                        runbook, repo_root, args.reason, args.score,
                        args.model_id,
                    )
                elif args.command == "finish":
                    command_finish(
                        runbook,
                        repo_root,
                        args.evidence,
                        args.expected_step,
                        args.decision,
                        args.model_id,
                    )
                else:
                    raise RunbookError(f"Unsupported command: {args.command}")
        return 0
    except OperatorDecisionRequired as exc:
        print_json(exc.payload)
        return 3
    except RunbookError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
