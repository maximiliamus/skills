"""Repository-local CLI for the guided-runbook journal."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from secrets import token_urlsafe

from . import publication
from .configuration import (
    config_locks,
    config_path,
    initialize_config,
    read_config,
    update_config,
)
from .contract import compile_contract
from .journal import Run
from .model import (
    JOURNAL_SCHEMA_VERSION,
    LEGACY_JOURNAL_SCHEMA_VERSION,
    REGISTRY_SCHEMA_VERSION,
    VERIFIED_OUTCOMES,
    RunbookError,
)
from .model_binding import normalize_model_family
from .registry import (
    document_title,
    normalize_registry_path,
    public_registry_entry,
    resolve_runbook,
    runbook_document_properties,
    validate_registered_id,
    validate_registry,
)
from .storage import (
    configure_standard_streams,
    find_repo_root,
    fixed_local_path,
    inside_repo,
    print_json,
    read_runbook_text,
    repository_registry_path,
    utc_now,
    write_json_atomic,
)
from .transitions import Transitions, blocked, consistent, prepare_change, same_tenure_replay
from .validation import (
    RequestError,
    decision,
    decode_json,
    digest,
    fields,
    integer,
    protected_path,
    require,
    uuid_string,
)

RUN_FILENAME = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.json")
RETRYABLE = {"CONFLICT", "BLOCKED", "DECISION_REQUIRED"}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise RequestError(message)


def build_parser():
    parser = Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--help", "-h", action="store_true")
    parser.add_argument("--repo-root", type=Path)
    commands = parser.add_subparsers(dest="command")
    for name, arguments in {
        "init": (),
        "update": (),
        "list": (),
        "register": ("id", "path"),
        "unregister": ("id",),
        "reserve": ("selector",),
        "status": ("selector",),
        "change": ("run_id",),
    }.items():
        sub = commands.add_parser(name, add_help=False, allow_abbrev=False)
        for argument in arguments:
            sub.add_argument(argument)
    return parser


def event(seq, request_id, request_digest, kind, data):
    return {
        "seq": seq,
        "at": utc_now(),
        "requestId": request_id,
        "requestDigest": request_digest,
        "type": kind,
        "data": data,
    }


class Repository:
    def __init__(self, root, path, config):
        self.root = root
        self.path = path
        self.config = config
        self.registry = validate_registry(config, path, root)
        state = fixed_local_path(root, ".runbooks", subject="Runbook state directory")
        self.runs = fixed_local_path(state, "runs", subject="Run journal directory")

    def journal_path(self, run_id):
        return fixed_local_path(self.runs, run_id + ".json", subject="Run journal")

    def load(self, run_id):
        path = self.journal_path(run_id)
        relative = path.relative_to(self.root).as_posix()
        path = protected_path(self.root, relative)
        require(path.exists(), f"Run does not exist: {run_id}", "NOT_FOUND")
        try:
            journal = decode_json(path.read_bytes())
        except (ValueError, UnicodeError, RequestError) as exc:
            raise RequestError(f"Unreadable run journal {path}: {exc}", "IO_ERROR") from exc
        version = journal.get("schemaVersion")
        require(type(version) is int, f"Invalid run journal schema: {relative}", "IO_ERROR")
        require(
            version in {LEGACY_JOURNAL_SCHEMA_VERSION, JOURNAL_SCHEMA_VERSION},
            f"Unsupported run journal schema: {relative}", "UNSUPPORTED_SCHEMA",
        )
        try:
            fields(journal, ("schemaVersion", "runId", "runbook", "contract", "events"), label="journal")
            require(journal["runId"] == run_id, "Mismatched journal ID")
            require(isinstance(journal["events"], list) and journal["events"], "Missing reservation event")
            return Run(self.root, journal)
        except (KeyError, IndexError, TypeError, AttributeError, ValueError, RequestError) as exc:
            raise RequestError(f"Invalid run journal {path}: {exc}", "IO_ERROR") from exc

    def journals(self):
        if not self.runs.exists():
            return
        for entry in self.runs.iterdir():
            if not RUN_FILENAME.fullmatch(entry.name):
                continue
            try:
                run = self.load(entry.stem)
            except RequestError as exc:
                # Other schema versions can occupy the same directory.
                if exc.code != "UNSUPPORTED_SCHEMA":
                    raise
            else:
                yield run

    def same_path(self, first, second):
        if self.root / first == self.root / second:
            return True
        try:
            old = inside_repo(self.root, self.root / first)
            new = inside_repo(self.root, self.root / second)
            return old.samefile(new)
        except (FileNotFoundError, RunbookError):
            return False

    def matches(self, run, path, registry_id):
        return self.same_path(run.runbook["path"], path) or (
            registry_id is not None and run.runbook.get("id") == registry_id
        )

    def open_run(self, path, registry_id=None):
        matches = [
            run for run in self.journals()
            if not run.terminal and self.matches(run, path, registry_id)
        ]
        consistent(
            len(matches) <= 1, "Multiple open runs match the runbook identity",
            runs=[run.run_id for run in matches],
        )
        return next(iter(matches), None)

    def status(self, selector):
        if RUN_FILENAME.fullmatch(selector.lower() + ".json"):
            return self.load(uuid_string(selector, "run ID")).projection()
        registry_id = selector if selector in self.registry else None
        raw_path = self.registry[selector]["path"] if registry_id else selector
        require(
            Path(raw_path).suffix.lower() == ".md", f"Unknown runbook selector: {selector}",
            "NOT_FOUND",
        )
        _, _, resolved = normalize_registry_path(raw_path, self.root)
        path = resolved.relative_to(self.root).as_posix()
        if registry_id is None:
            matching_ids = [key for key, entry in self.registry.items()
                            if self.same_path(entry["path"], path)]
            consistent(len(matching_ids) <= 1, "The path matches multiple registered identities")
            registry_id = next(iter(matching_ids), None)
        run = self.open_run(path, registry_id)
        if run is None:
            raise RequestError(f"No open run for {selector}", "NOT_FOUND")
        return run.projection()

    def reserve(self, selector, request):
        fields(
            request, ("runId",),
            ("stepContracts", "stepInventory", "modelFamily", "overrideDecision"), "reservation",
        )
        run_id = uuid_string(request["runId"], "runId")
        request_digest = digest({"selector": selector, "input": request | {"runId": run_id}})
        target = self.journal_path(run_id)
        if target.exists():
            run = self.load(run_id)
            return same_tenure_replay(run, run.events[0], request_digest)
        operator_family = None
        if "modelFamily" in request:
            require("overrideDecision" in request, "Model selection requires a decision",
                    "DECISION_REQUIRED")
            decision(request["overrideDecision"])
            operator_family = normalize_model_family(request["modelFamily"])
        else:
            require("overrideDecision" not in request, "A decision must accompany a model selection")
        document = resolve_runbook(selector, self.root, self.registry)
        path = document["relativePath"]
        registry_id = document["id"] if document["registered"] else None
        opened = self.open_run(path, registry_id)
        if opened is not None:
            raise RequestError(
                "The runbook already has an open run.",
                "CONFLICT",
                runId=opened.run_id,
                revision=opened.revision,
                schemaVersion=opened.journal["schemaVersion"],
            )
        step_inventory = request.get("stepInventory")
        if "stepInventory" in request:
            fields(step_inventory, ("sha256", "steps"), label="stepInventory")
        revision, contract = compile_contract(
            self.root, Path(document["path"]), run_id, request.get("stepContracts", {}),
            operator_family=operator_family, step_inventory=step_inventory,
        )
        runbook = {"path": path, "sha256": revision}
        if registry_id is not None:
            runbook["id"] = registry_id
        token = token_urlsafe(32)
        data = {"ownershipToken": token}
        if operator_family is not None:
            data.update(modelFamily=operator_family, decision=request["overrideDecision"])
        journal = {
            "schemaVersion": JOURNAL_SCHEMA_VERSION,
            "runId": run_id,
            "runbook": runbook,
            "contract": contract,
            "events": [event(1, run_id, request_digest, "reserve", data)],
        }
        self.runs.mkdir(mode=0o700, exist_ok=True)
        run = Run(self.root, journal)
        self.save(run)
        return run.projection() | {
            "ownershipToken": token, "duplicate": False, "committedRevision": run.revision,
        }

    def save(self, run):
        write_json_atomic(self.journal_path(run.run_id), run.journal, default_mode=0o600)

    def prepare_publication(self, sid, outputs, run, scope):
        intents = run.intents.values()
        if run.steps[sid].contract["deliverables"]:
            intents = [*intents, *(intent for other in self.journals()
                                    if other.run_id != run.run_id
                                    for intent in other.intents.values())]
        return publication.prepare(self.root, run, sid, outputs, scope, intents)

    def reuse_record(self, source, sid, receiving):
        fields(source, ("runId", "event"), label="reuse source")
        seq = integer(source["event"], "source event")
        run = self.load(uuid_string(source["runId"], "source run ID"))
        registry_id = receiving.runbook.get("id")
        blocked(self.matches(run, receiving.runbook["path"], registry_id),
                "Reuse requires a journal for this runbook")
        require(seq <= run.revision, "Unknown source event", "NOT_FOUND")
        entry = run.events[seq - 1]
        record = entry["data"]
        if entry["type"] == "resolve" and record.get("disposition") == "complete":
            record = run.events[record["intent"] - 1]["data"]["eventData"]
        blocked(record.get("stepId") == sid and "assessment" in record,
                "Source event is not an accepted assessment for this step")
        record = record["assessment"]
        blocked(record["outcome"] in VERIFIED_OUTCOMES, "A gap cannot be reused as a verified result")
        return record | {"runId": run.run_id}

    def change(self, identifier, request):
        run = self.load(uuid_string(identifier, "run ID"))
        durable_revision = run.revision
        try:
            replay, request_info = prepare_change(run, request)
            if request_info is None:
                return replay
            request_id, request_digest, operation = request_info
            data = Transitions(run, self).take(operation, request["data"])
            if "duplicateResult" in data:
                return run.projection() | {"duplicate": True, "committedRevision": data["duplicateResult"]}
            outputs = data.pop("_outputs", [])
            if outputs:
                intent_id = run.revision + 1
                intent = {
                    "operation": operation, "eventData": data, "artifacts": outputs,
                    "requestId": request_id, "requestDigest": request_digest,
                }
                run.append(event(intent_id, request_id, request_digest, "publication-intent", intent))
                self.save(run)
                durable_revision = run.revision
                publication.publish(self.root, outputs)
                self.require_published_applicability(run, data["assessment"])
                data = data | {"publication": intent_id}
            elif operation == "resolve" and data.get("disposition") == "complete":
                intent = run.intents[data["intent"]]
                publication.publish(self.root, intent["artifacts"], recover=True)
                self.require_published_applicability(run, intent["eventData"]["assessment"])
            run.append(event(run.revision + 1, request_id, request_digest, operation, data))
            self.save(run)
            response = run.projection() | {"duplicate": False, "committedRevision": run.revision}
            if operation == "takeover":
                response["ownershipToken"] = data["ownershipToken"]
            return response
        except RequestError as exc:
            exc.details.setdefault("revision", durable_revision)
            raise
        except (RunbookError, OSError, ValueError) as exc:
            code = (
                "IO_ERROR"
                if isinstance(exc, OSError) or isinstance(exc.__cause__, OSError)
                else "INVALID_REQUEST"
            )
            raise RequestError(str(exc), code, revision=durable_revision) from exc

    @staticmethod
    def require_published_applicability(run, assessment):
        blocked(
            run.applicable(assessment, {}),
            "Inputs, prerequisites or outputs changed during publication",
        )

    def discovery(self, *, changed=False):
        entries = []
        for runbook_id in sorted(self.registry):
            entry = public_registry_entry(self.registry[runbook_id])
            path = normalize_registry_path(entry["path"], self.root)[2]
            if path.is_file():
                props = runbook_document_properties(read_runbook_text(path), path)
                if "description" in props and not self.registry[runbook_id]["_descriptionOverride"]:
                    entry["description"] = props["description"]
                for key in ("modelFamily", "modelTier", "effortLevel"):
                    entry[key] = props[key]
            entries.append(entry)
        return {"ok": True, "changed": changed, "runbooks": entries}

    def edit_registry(self, identifier, raw_path=None):
        validate_registered_id(identifier)
        entry = self.registry.get(identifier)
        old_path = (
            normalize_registry_path(entry["path"], self.root)[2].relative_to(self.root).as_posix()
            if entry
            else None
        )
        new_file = None
        if raw_path is None:
            require(entry is not None, f"No registry entry for {identifier}", "NOT_FOUND")
            new_path = None
        else:
            _, _, new_file = normalize_registry_path(raw_path, self.root)
            require(new_file.is_file(), f"Runbook does not exist: {raw_path}", "NOT_FOUND")
            props = runbook_document_properties(read_runbook_text(new_file), new_file)
            require(
                props.get("id", identifier) == identifier,
                "Runbook frontmatter ID disagrees with registration",
            )
            new_path = new_file.relative_to(self.root).as_posix()
            normalize_registry_path(new_path, self.root)
        if old_path == new_path:
            return self.discovery()
        for path in {old_path, new_path} - {None}:
            opened = self.open_run(path, identifier)
            if opened is not None:
                raise RequestError(
                    "Registry edit would change an open run's identity",
                    "CONFLICT",
                    runId=opened.run_id,
                    revision=opened.revision,
                )
        entries = [raw for raw in self.config["runbooks"] if raw["id"] != identifier]
        if new_file is not None:
            previous = next((raw for raw in self.config["runbooks"] if raw["id"] == identifier), {})
            entries.append(
                {"title": document_title(new_file), **previous, "id": identifier, "path": new_path}
            )
        config = self.config | {"runbooks": entries}
        self.registry = validate_registry(config, self.path, self.root)
        response = self.discovery(changed=True)
        write_json_atomic(self.path, config, default_mode=0o666)
        return response


def execute(parser, args):
    if args.help:
        return {"ok": True, "help": parser.format_help()}
    require(args.command is not None, "A command is required")
    root = args.repo_root.resolve() if args.repo_root is not None else find_repo_root()
    require(root.is_dir(), f"Repository root is not a directory: {root}", "NOT_FOUND")
    payload = (
        decode_json(sys.stdin.buffer.read()) if args.command in {"reserve", "change"} else None
    )
    path = repository_registry_path(root)
    with config_locks(root):
        changed = False
        if args.command in {"init", "update"}:
            result = (initialize_config if args.command == "init" else update_config)(root)
            config = result["config"]
            changed = result["created"]
        else:
            path = config_path(root, path.name)
            config = (
                read_config(path, root)
                if path.exists()
                else {"schemaVersion": REGISTRY_SCHEMA_VERSION, "runbooks": []}
            )
        repo = Repository(root, path, config)
        if args.command in {"init", "update", "list"}:
            return repo.discovery(changed=changed)
        if args.command == "reserve":
            return repo.reserve(args.selector, payload)
        if args.command == "status":
            return repo.status(args.selector)
        if args.command == "change":
            return repo.change(args.run_id, payload)
        return repo.edit_registry(args.id, getattr(args, "path", None))


def main(argv=None):
    configure_standard_streams()
    parser = build_parser()
    try:
        response = execute(parser, parser.parse_args(argv))
        print_json(response)
        return 0
    except RequestError as exc:
        code, message, details = exc.code, str(exc), exc.details
    except (ValueError, UnicodeError, RecursionError) as exc:
        code, message, details = "INVALID_REQUEST", str(exc), {}
    except (OSError, RunbookError) as exc:
        code = (
            "IO_ERROR"
            if isinstance(exc, OSError) or isinstance(exc.__cause__, OSError)
            else "INVALID_REQUEST"
        )
        message, details = str(exc), {}
    # Unpaired surrogates can appear in OS exception messages.
    message = message.encode("utf-8", "backslashreplace").decode("utf-8")
    print_json({"ok": False, "code": code, "message": message, "details": details})
    return 2 if code in RETRYABLE else 1
