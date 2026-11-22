"""Store run folders and commit history changes with their triggering journal event."""

import re
import shutil
import tempfile
from functools import cached_property
from pathlib import Path

from .journal import Run
from .model import (
    JOURNAL_SCHEMA_VERSION,
    LEGACY_JOURNAL_SCHEMA_VERSION,
    RUN_FOLDER_MARKER,
    RunbookError,
)
from .storage import fixed_local_path, replace_with_retry, write_json_atomic
from .validation import (
    RequestError,
    decode_json,
    fields,
    integer,
    protected_path,
    require,
    sha256,
    uuid_string,
)

RUN_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class RunStore:
    """All methods run under the repository lock."""

    def __init__(self, root):
        self.root = root
        state = fixed_local_path(root, ".runbooks", subject="Runbook state directory")
        self.legacy = fixed_local_path(state, "runs", subject="Legacy journal directory")
        self.history = fixed_local_path(state, "history", subject="Run history directory")

    def folder(self, run_id):
        return fixed_local_path(self.root, f".runbooks/history/{run_id}", subject="Run folder")

    def journal_path(self, run_id):
        flat = self.safe_file(self.legacy / (run_id + ".json"))
        folder = self.folder(run_id)
        marker = self.safe_file(folder / RUN_FOLDER_MARKER)
        if not marker.exists():
            return flat
        require(
            marker.read_bytes() == (run_id + "\n").encode() and not flat.exists(),
            f"Invalid or duplicate run folder: {run_id}", "IO_ERROR",
        )
        nested = self.safe_file(folder / "journal.json")
        require(nested.is_file(), f"Missing journal in run folder: {run_id}", "IO_ERROR")
        return nested

    def safe_file(self, file):
        return protected_path(self.root, file.relative_to(self.root).as_posix())

    def read_run(self, run_id):
        path = self.journal_path(run_id)
        relative = path.relative_to(self.root).as_posix()
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
            fields(journal, ("schemaVersion", "runId", "runbook", "contract", "events"),
                   label="journal")
            require(journal["runId"] == run_id, "Mismatched journal ID")
            require(isinstance(journal["events"], list) and journal["events"],
                    "Missing reservation event")
            return Run(self.root, journal, flat=path.parent == self.legacy)
        except (KeyError, IndexError, TypeError, AttributeError, ValueError, RequestError) as exc:
            raise RequestError(f"Invalid run journal {path}: {exc}", "IO_ERROR") from exc

    def ids(self):
        legacy = {entry.stem for entry in self.legacy.glob("*.json")
                  if RUN_ID.fullmatch(entry.stem)}
        nested = {entry.name for entry in self.history.glob("*") if RUN_ID.fullmatch(entry.name)}
        for run_id in sorted((legacy | nested) - self.retired):
            if self.journal_path(run_id).exists():
                yield run_id

    @cached_property
    def index_path(self):
        return protected_path(self.root, ".runbooks/runs/.retention.json")

    @cached_property
    def index(self):
        empty = {"retired": [], "parked": {}}
        if not self.index_path.exists():
            return empty
        try:
            state = decode_json(self.index_path.read_bytes())
            fields(state, ("retired", "parked"), ("pending", "cleanup"), "run history state")
            require(isinstance(state["retired"], list), "Invalid retired run list")
            for run_id in state["retired"]:
                self.check_id(run_id)
            self.check_parked(state["parked"])
            cleanup = state.get("cleanup", {})
            self.check_layouts(cleanup)
            require(cleanup.keys() <= set(state["retired"]), "Cleanup includes a retained run")
            if "pending" in state:
                pending = state["pending"]
                fields(pending, ("runId", "seq", "digest", "retire", "park"), label="history change")
                self.check_id(pending["runId"])
                integer(pending["seq"], "triggering journal event")
                sha256(pending["digest"])
                self.check_layouts(pending["retire"])
                require(pending["runId"] not in pending["retire"], "Cannot retire the triggering run")
                self.check_parked(pending["park"])
            return state
        except (ValueError, RequestError) as exc:
            raise RequestError(f"Invalid run history state: {exc}", "IO_ERROR") from exc

    @staticmethod
    def check_id(run_id):
        require(uuid_string(run_id, "stored run ID") == run_id, "Noncanonical stored run ID")

    @classmethod
    def check_layouts(cls, runs):
        require(isinstance(runs, dict), "Invalid retirement list")
        for run_id, layout in runs.items():
            cls.check_id(run_id)
            require(layout in ("flat", "folder"), "Invalid journal layout")

    @classmethod
    def check_parked(cls, parked):
        require(isinstance(parked, dict), "Invalid parked run list")
        for run_id, tenure in parked.items():
            cls.check_id(run_id)
            integer(tenure, "parked tenure")

    @cached_property
    def committed(self):
        # The journal replacement is the commit point for both the event and its
        # history change. A failed save cannot hide or delete any old run.
        pending = self.index.get("pending")
        if pending is None:
            return None
        path = self.journal_path(pending["runId"])
        if path.exists():
            try:
                events = decode_json(path.read_bytes())["events"]
                if (len(events) >= pending["seq"]
                        and events[pending["seq"] - 1]["requestDigest"] == pending["digest"]):
                    return pending
            except (ValueError, TypeError, KeyError, IndexError, RequestError) as exc:
                raise RequestError(f"Invalid history trigger: {path}: {exc}", "IO_ERROR") from exc
        return None

    @property
    def retired(self):
        retired = set(self.index["retired"])
        if self.committed:
            retired.update(self.committed["retire"])
        return retired - {run_id for run_id, recoverable in self.protected_retirements.items()
                          if recoverable}

    def retirement_path(self, run_id, layout):
        if layout == "flat":
            return self.safe_file(self.legacy / (run_id + ".json"))
        return self.folder(run_id)

    def quarantine_path(self, run_id):
        return fixed_local_path(self.root, f".runbooks/runs/.retired-{run_id}",
                                subject="Quarantined run")

    @cached_property
    def protected_retirements(self):
        # Releases that allowed abandonment before effects settled may already
        # have placed such runs in a committed plan or in the cleanup queue.
        queued = self.index.get("cleanup", {})
        if self.committed:
            queued = queued | self.committed["retire"]
        protected = {}
        for run_id, layout in queued.items():
            try:
                if self.quarantine_path(run_id).exists():
                    continue
                original = self.retirement_path(run_id, layout)
                if not original.exists():
                    continue
                run = self.read_run(run_id)
                require(run.flat == (layout == "flat"), "Unexpected run layout in cleanup queue")
                if not run.settled:
                    protected[run_id] = True
            except (OSError, RunbookError):
                # Keep remaining data if a previous deletion removed the journal.
                protected[run_id] = False
        return protected

    def preserve_recovery(self):
        protected = self.protected_retirements
        if not protected:
            return
        recoverable = {run_id for run_id, can_recover in protected.items() if can_recover}
        # Incomplete folders must keep their UUID tombstone even though any
        # surviving files are no longer eligible for automatic deletion.
        retired = (set(self.index["retired"]) - recoverable) | (protected.keys() - recoverable)
        state = self.index | {
            "retired": sorted(retired),
            "cleanup": {run_id: layout for run_id, layout in self.index.get("cleanup", {}).items()
                        if run_id not in protected},
        }
        if pending := self.committed:
            state["pending"] = pending | {
                "retire": {run_id: layout for run_id, layout in pending["retire"].items()
                           if run_id not in protected},
            }
        self.write_index(state)

    def parked(self, run):
        previous = self.committed["park"] if self.committed else {}
        tenure = previous.get(run.run_id, self.index["parked"].get(run.run_id))
        return not run.terminal and tenure == run.owner["seq"]

    def write_index(self, state):
        write_json_atomic(self.index_path, state, default_mode=0o600)
        self.index = state
        self.clear_retirement_cache()

    def clear_retirement_cache(self):
        self.__dict__.pop("committed", None)
        self.__dict__.pop("protected_retirements", None)

    def checkpoint(self):
        self.preserve_recovery()
        if not (pending := self.committed):
            return
        parked = self.index["parked"] | pending["park"]
        for run_id in pending["retire"]:
            parked.pop(run_id, None)
        self.write_index({
            "retired": sorted(self.retired),
            "parked": parked,
            "cleanup": self.index.get("cleanup", {}) | pending["retire"],
        })

    def cleanup(self):
        if not (runs := self.index.get("cleanup")):
            return
        remaining = {}
        for run_id, layout in runs.items():
            try:
                quarantined = self.quarantine_path(run_id)
                if not quarantined.exists():
                    original = self.retirement_path(run_id, layout)
                    if not original.exists():
                        continue
                    replace_with_retry(original, quarantined)
                # A retry only deletes the quarantined data if its original path
                # has been reused since the first cleanup attempt.
                if layout == "flat":
                    self.safe_file(quarantined).unlink(missing_ok=True)
                else:
                    shutil.rmtree(quarantined)
            except (OSError, RunbookError):
                remaining[run_id] = layout
        if remaining != runs:
            self.write_index(self.index | {"cleanup": remaining})

    def stage(self, run, retire, park):
        self.preserve_recovery()
        trigger = run.events[-1]
        if not retire and not park:
            pending = self.index.get("pending")
            if pending and pending["runId"] == run.run_id and pending["seq"] == trigger["seq"]:
                # A retry can cancel a previously planned change before committing its event.
                self.checkpoint()
                state = self.index.copy()
                if state.pop("pending", None) is not None:
                    self.write_index(state)
            return
        self.checkpoint()
        pending = {
            "runId": run.run_id,
            "seq": trigger["seq"],
            "digest": trigger["requestDigest"],
            "retire": {item.run_id: "flat" if self.journal_path(item.run_id).parent == self.legacy
                       else "folder" for item in retire},
            "park": {item.run_id: item.owner["seq"] for item in park},
        }
        self.write_index(self.index | {"pending": pending})

    def finish_response(self, response, *, clean=False):
        self.clear_retirement_cache()
        try:
            self.checkpoint()
            if clean:
                self.cleanup()
        except (OSError, RunbookError):
            return response | {"cleanupPending": True}
        if self.index.get("cleanup"):
            return response | {"cleanupPending": True}
        return response

    def create(self, run):
        folder = self.folder(run.run_id)
        require(not folder.exists(), f"Run folder already exists: {run.run_id}", "CONFLICT")
        self.history.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".reserve-", dir=self.history, ignore_cleanup_errors=True,
        ) as name:
            staged = Path(name) / run.run_id
            staged.mkdir(mode=0o700)
            (staged / RUN_FOLDER_MARKER).write_bytes((run.run_id + "\n").encode())
            (staged / "artifacts").mkdir(mode=0o700)
            write_json_atomic(staged / "journal.json", run.journal, default_mode=0o600)
            replace_with_retry(staged, folder)
