"""Repository-local CLI for the guided-runbook journal."""

from __future__ import annotations

import argparse
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
from .history import RUN_ID, RunStore
from .journal import Run
from .model import (
    DEFAULT_JOURNAL_RETENTION,
    JOURNAL_SCHEMA_VERSION,
    REGISTRY_SCHEMA_VERSION,
    VERIFIED_OUTCOMES,
    RunbookError,
)
from .model_binding import normalize_model_family
from .registry import (
    document_title,
    normalize_registry_path,
    public_registry_entry,
    registered_ids_for_path,
    resolve_runbook,
    runbook_document_properties,
    same_runbook_path,
    select_runbook,
    validate_registered_id,
    validate_registry,
)
from .storage import (
    configure_standard_streams,
    find_repo_root,
    inside_repo,
    print_json,
    read_runbook_text,
    repository_registry_path,
    utc_now,
    write_json_atomic,
)
from .transitions import (
    Transitions,
    blocked,
    consistent,
    evidence,
    prepare_change,
    same_tenure_replay,
)
from .validation import (
    RequestError,
    decision,
    decode_json,
    digest,
    fields,
    integer,
    matches_digest,
    require,
    run_artifacts,
    run_staging,
    uuid_string,
)

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
        self.store = RunStore(root)

    def load(self, run_id):
        require(run_id not in self.store.retired, f"Run was retired: {run_id}", "NOT_FOUND")
        return self.store.read_run(run_id)

    def journals(self):
        for run_id in self.store.ids():
            try:
                run = self.load(run_id)
            except RequestError as exc:
                # Other schema versions can occupy the same directory.
                if exc.code != "UNSUPPORTED_SCHEMA":
                    raise
            else:
                yield run

    def matches(self, run, path, registry_id):
        recorded_id = run.runbook.get("id")
        if registry_id is not None and recorded_id == registry_id:
            return True
        if recorded_id in self.registry and registry_id in self.registry:
            return False
        return same_runbook_path(self.root, self.current_path(run), path)

    def history(self, path, registry_id, *, journals=None):
        if journals is None:
            journals = self.journals()
        return sorted(
            (run for run in journals if self.matches(run, path, registry_id)),
            key=lambda run: (run.reservation_order, run.started, run.run_id),
            reverse=True,
        )

    def current_path(self, run):
        entry = self.registry.get(run.runbook.get("id"), run.runbook)
        return entry["path"]

    def retirement_candidates(self, run):
        path = self.current_path(run)
        registry_id = run.runbook.get("id")
        matches = registered_ids_for_path(self.root, path, self.registry)
        if len(matches) > 1:
            return []
        if matches:
            registry_id = matches[0]
        limit = self.registry.get(registry_id, {}).get(
            "journalRetention", self.config.get("journalRetention", DEFAULT_JOURNAL_RETENTION),
        )
        if limit == "all":
            return []
        history = self.history(path, registry_id)
        if not any(item.run_id == run.run_id for item in history[:limit]):
            return []
        return [
            item for item in history[limit:]
            if item.settled and (item.terminal or self.store.parked(item))
        ]

    def source_matches(self, run):
        try:
            source = inside_repo(self.root, self.root / self.current_path(run))
            return matches_digest(source, run.runbook["sha256"])
        except (OSError, RunbookError):
            return False

    def view(self, run, response=None):
        response = run.projection() if response is None else response
        matches = self.source_matches(run)
        parked = self.store.parked(run)
        if not matches or parked or run.owner["data"].get("recoveryOnly"):
            response["ready"] = []
        return response | {
            "sourceMatches": matches,
            "parked": parked,
            "artifactsDirectory": None if run.flat else run_artifacts(run.run_id),
            "stagingDirectory": run_staging(run.run_id, flat=run.flat),
        }

    def parkable(self, run, *, authorized=False):
        require(
            run.settled and (authorized or run.phase == "paused"),
            "Settle the current run and its effects and publications before switching runs.",
            "BLOCKED", runId=run.run_id, revision=run.revision,
        )

    def status(self, selector):
        if RUN_ID.fullmatch(selector.lower()):
            return self.view(self.load(uuid_string(selector, "run ID")))
        path, registry_id = select_runbook(selector, self.root, self.registry)
        history = self.history(path.relative_to(self.root).as_posix(), registry_id)
        require(history, f"No saved run for {selector}", "NOT_FOUND")
        return self.view(history[0])

    def reserve(self, selector, request):
        fields(
            request, ("runId",),
            ("stepContracts", "stepInventory", "modelFamily", "overrideDecision", "restart"),
            "reservation",
        )
        run_id = uuid_string(request["runId"], "runId")
        request_digest = digest({"selector": selector, "input": request | {"runId": run_id}})
        consistent(run_id not in self.store.retired, "Run ID was previously used and retired")
        target = self.store.journal_path(run_id)
        if target.exists():
            run = self.load(run_id)
            consistent(not self.store.parked(run), "Coordinator tenure was superseded")
            replay = same_tenure_replay(run, run.events[0], request_digest)
            return self.store.finish_response(self.view(run, replay))
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
        journals = list(self.journals())
        history = self.history(path, registry_id, journals=journals)
        latest = history[0] if history else None
        restart_id = None
        if "restart" in request:
            restart = request["restart"]
            fields(restart, ("runId", "ifRevision", "decision", "evidence"), label="restart")
            restart_id = uuid_string(restart["runId"], "restart run ID")
            revision = integer(restart["ifRevision"], "restart revision")
            decision(restart["decision"])
            evidence(restart["evidence"])
            consistent(
                latest is not None and not latest.terminal and latest.run_id == restart_id
                and latest.revision == revision,
                "The latest open run changed; check status before restarting",
            )
        if latest is not None and not latest.terminal and restart_id is None:
            raise RequestError(
                "The latest run is open.", "CONFLICT", runId=latest.run_id,
                revision=latest.revision, schemaVersion=latest.journal["schemaVersion"],
            )
        park = [run for run in history if not run.terminal and not self.store.parked(run)]
        for previous in park:
            self.parkable(previous, authorized=previous.run_id == restart_id)
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
        data = {
            "ownershipToken": token,
            "reservationOrder": max((run.reservation_order for run in journals), default=0) + 1,
        }
        if restart_id is not None:
            data["restart"] = request["restart"]
        if operator_family is not None:
            data.update(modelFamily=operator_family, decision=request["overrideDecision"])
        journal = {
            "schemaVersion": JOURNAL_SCHEMA_VERSION,
            "runId": run_id,
            "runbook": runbook,
            "contract": contract,
            "events": [event(1, run_id, request_digest, "reserve", data)],
        }
        run = Run(self.root, journal)
        self.store.stage(run, [], park)
        self.store.create(run)
        return self.store.finish_response(self.view(run) | {
            "ownershipToken": token, "duplicate": False, "committedRevision": run.revision,
        })

    def save(self, run):
        write_json_atomic(self.store.journal_path(run.run_id), run.journal, default_mode=0o600)

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
        blocked(self.matches(run, self.current_path(receiving), registry_id),
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
            parked = self.store.parked(run)
            if parked:
                consistent(isinstance(request, dict) and request.get("type") == "takeover",
                           "Run was parked; an explicit takeover is required")
            replay, request_info = prepare_change(run, request)
            completing = request["type"] == "close" and request["data"].get("mode") == "finish"
            if request_info is None:
                if parked:
                    raise RequestError("Coordinator tenure was superseded", "CONFLICT")
                return self.store.finish_response(self.view(run, replay), clean=completing)
            request_id, request_digest, operation = request_info
            data = Transitions(run, self).take(operation, request["data"])
            park = []
            if operation == "takeover":
                if data.get("recoveryOnly"):
                    blocked(not parked, "A parked run has no active work to settle")
                else:
                    blocked(self.source_matches(run), "The runbook source changed or is unavailable")
                    registry_id = run.runbook.get("id")
                    selector = registry_id if registry_id in self.registry else self.current_path(run)
                    path, selected_id = select_runbook(selector, self.root, self.registry)
                    park = [other for other in
                            self.history(path.relative_to(self.root).as_posix(), selected_id)
                            if other.run_id != run.run_id and not other.terminal
                            and not self.store.parked(other)]
                    for other in park:
                        self.parkable(other)
            elif operation in {"admit", "assess"} or (
                operation == "set-pause" and not data["requested"]
            ):
                blocked(
                    self.source_matches(run) and not run.owner["data"].get("recoveryOnly"),
                    "Continuing work requires the original runbook source and execution ownership",
                )
            if "duplicateResult" in data:
                return self.view(run) | {"duplicate": True, "committedRevision": data["duplicateResult"]}
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
                publication.publish(self.root, outputs, run_id=run.run_id, flat=run.flat)
                self.require_published_applicability(run, data["assessment"])
                data = data | {"publication": intent_id}
            elif operation == "resolve" and data.get("disposition") == "complete":
                intent = run.intents[data["intent"]]
                publication.publish(
                    self.root, intent["artifacts"], run_id=run.run_id, flat=run.flat, recover=True,
                )
                self.require_published_applicability(run, intent["eventData"]["assessment"])
            run.append(event(run.revision + 1, request_id, request_digest, operation, data))
            retire = self.retirement_candidates(run) if completing else []
            self.store.stage(run, retire, park)
            self.save(run)
            response = self.store.finish_response(
                self.view(run) | {"duplicate": False, "committedRevision": run.revision},
                clean=completing,
            )
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
            opened = next((run for run in self.history(path, identifier) if not run.terminal), None)
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
