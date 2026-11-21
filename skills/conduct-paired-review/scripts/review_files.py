"""Configuration and Markdown filename helpers; no review database or snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import re
import sys
import uuid
from copy import deepcopy
from pathlib import Path

PROJECT_CONFIG_NAME = "conduct-paired-review.json"
CONFIG_SCHEMA_VERSION = 1
CONFIG_TEMPLATE = Path(__file__).resolve().parents[1] / "config.json"


class ReviewError(ValueError):
    """A request cannot safely proceed."""


class ClarificationNeeded(ReviewError):
    """The operator must clarify a conflicting or incomplete request."""


def require(condition, message):
    if not condition:
        raise ReviewError(message)



def nonempty(value):
    return isinstance(value, str) and bool(value.strip())



def digest(data):
    return hashlib.sha256(data).hexdigest()



def read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    require(isinstance(value, dict), "Expected a JSON object")
    return value



def checked(path, *, allow_hardlink=False):
    """Reject redirection before resolving paths, including junction ancestors."""
    path = Path(os.path.abspath(path))
    for entry in (path, *path.parents):
        require(not entry.is_symlink() and not entry.is_junction(), f"Redirected path: {entry}")
    if not allow_hardlink and path.exists() and path.is_file():
        require(path.stat().st_nlink == 1, f"Hard-linked file: {path}")
    if os.name == "nt":
        # Obtain the stored spelling only after rejecting path redirection.
        # Case aliases must select the same review family as the original file.
        path = path.resolve()
    return path



def find_project_root(directory):
    """Discover the active project from a working directory or source location."""
    directory = checked(directory)
    ancestors = [directory, *directory.parents]
    git_root = next((p for p in ancestors if (p / ".git").exists()), None)
    if git_root is not None:
        return git_root
    project = next((p for p in ancestors if (p / PROJECT_CONFIG_NAME).is_file()
                 or (p != Path.home()
                     and (p / ".agents/skills/conduct-paired-review/SKILL.md").is_file())), None)
    if project is not None:
        return project
    return next((p for p in ancestors if (p / "AGENTS.md").is_file()), directory)



def publish_new(temporary, path):
    """Publish complete bytes without replacing a concurrent writer's file."""
    if os.name == "nt":
        os.rename(temporary, path)  # Windows rename fails when the destination exists.
    else:
        os.link(temporary, path)  # Atomic no-replace publication on POSIX.



def recover_project_config(root):
    """Remove only our own staged link after interrupted config creation."""
    path = checked(root / PROJECT_CONFIG_NAME, allow_hardlink=True)
    if path.is_file() and path.stat().st_nlink == 2:
        prefix = f".{PROJECT_CONFIG_NAME}.init-"
        for staged in root.glob(f"{prefix}*.tmp"):
            if not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}\.tmp", staged.name):
                continue
            try:
                checked(staged, allow_hardlink=True)
                if staged.is_file() and staged.samefile(path):
                    validate_config(read_json(path))
                    current, temporary = path.stat(), staged.stat()
                    require(current.st_nlink == temporary.st_nlink == 2
                            and (current.st_dev, current.st_ino)
                            == (temporary.st_dev, temporary.st_ino),
                            "Configuration links changed during recovery")
                    staged.unlink(missing_ok=True)
                    break
            except FileNotFoundError:
                # Another recovery may remove this staged link at any boundary.
                # The final path check and caller validation still apply.
                if staged.exists():
                    raise
    return checked(path)



def update_project_config(root):
    """Validate the current format without rewriting user settings."""
    path = recover_project_config(root)
    require(path.is_file(), "No project config; run init first")
    config = read_json(path)
    # There is no older released profile format. Add explicit transformations only
    # when a subsequent schema is introduced; never merge the installed template.
    validate_config(config)
    return {"projectRoot": str(root), "configPath": str(path),
            "created": False, "updated": False, "config": config}



def validate_config(config):
    require(type(config.get("schemaVersion")) is int
            and config["schemaVersion"] == CONFIG_SCHEMA_VERSION, "Unsupported config schema")
    require(
        set(config) == {"schemaVersion", "participants", "defaultRoles", "maxStagnantTurns"},
        "Unexpected or missing config fields",
    )
    participants = config["participants"]
    require(
        isinstance(participants, dict) and len(participants) >= 2,
        "Configure at least two available participants",
    )
    for name, profile in participants.items():
        require(bool(re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name)), "Invalid participant name")
        require(
            isinstance(profile, dict)
            and set(profile) == {"runtime", "modelFamily", "modelTier", "effortLevel"},
            "Invalid participant profile",
        )
        require(nonempty(profile["runtime"]), "Missing runtime")
        family = profile["modelFamily"]
        require(
            isinstance(family, str)
            and (
                family in {"current", "frontier", "predecessor"}
                or (family.startswith(("family:", "exact:")) and nonempty(family.split(":", 1)[1]))
            ),
            "Invalid modelFamily",
        )
        require(profile["modelTier"] in {"light", "medium", "heavy"}, "Invalid modelTier")
        require(profile["effortLevel"] in {"low", "medium", "high", "extra"}, "Invalid effortLevel")
    roles = config["defaultRoles"]
    require(
        isinstance(roles, dict)
        and set(roles) == {"author", "critic"}
        and len(set(roles.values())) == 2
        and set(roles.values()) <= set(participants),
        "Roles must name distinct participants",
    )
    require(
        type(config["maxStagnantTurns"]) is int
        and 2 <= config["maxStagnantTurns"] <= 200
        and config["maxStagnantTurns"] % 2 == 0,
        "maxStagnantTurns must be an even number between 2 and 200",
    )



def resolve_roles(config, author=None, critic=None):
    roles = deepcopy(config["defaultRoles"])
    overrides = {"author": author, "critic": critic}
    for role, participant in overrides.items():
        if participant is None:
            continue
        require(participant in config["participants"], f"Unknown {role} participant")
        other = "critic" if role == "author" else "author"
        if overrides[other] is None and participant == roles[other]:
            roles[other] = roles[role]
        roles[role] = participant
    require(len(set(roles.values())) == 2, "Author and critic must be distinct participants")
    return roles



def review_author_role(path, prompt_role=None, confirmed_role=None, instruction=None):
    """Resolve roles without mutating files; conflicts require an operator answer."""
    for role in (prompt_role, confirmed_role):
        require(role is None or role in {"author", "critic"}, "Invalid seed role")
    match = re.fullmatch(r"(.+)-R([0-9]{2,})([AC])", Path(path).stem)
    marker_role = {"C": "critic", "A": "author"}[match[3]] if match else None
    if confirmed_role is not None:
        require(nonempty(instruction), "Record the operator's role clarification")
        role = confirmed_role
    elif prompt_role and marker_role and prompt_role != marker_role:
        raise ClarificationNeeded(
            "The prompt role conflicts with the filename marker. Ask the operator to clarify; "
            "do not start the dialogue until they answer."
        )
    else:
        role = prompt_role or marker_role
    if role is None:
        raise ClarificationNeeded("The initial review role is unspecified. Ask the operator.")
    return role




def initialize_project_config(root):
    path = recover_project_config(root)
    created = False
    if not path.exists():
        data = checked(CONFIG_TEMPLATE).read_bytes()
        config = json.loads(data)
        require(isinstance(config, dict), "Expected a configuration object")
        validate_config(config)
        temporary = checked(root / f".{PROJECT_CONFIG_NAME}.init-{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                publish_new(temporary, checked(path, allow_hardlink=True))
                created = True
            except FileExistsError:
                pass  # Another initializer won; preserve and validate its settings.
        finally:
            temporary.unlink(missing_ok=True)
    path = recover_project_config(root)
    config = read_json(path)
    validate_config(config)
    return {"projectRoot": str(root), "configPath": str(path),
            "created": created, "config": config}


def portable_stem(stem, limit=80):
    return (len(stem) <= limit
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", stem) is not None
            and not ntpath.isreserved(stem))


def document_stem(source):
    stem = source.stem
    if not portable_stem(stem):
        return "file-" + digest(source.name.encode("utf-8"))[:20]
    if source.suffix != ".md":
        return stem + "-" + digest(source.name.encode("utf-8"))[:12]
    return stem


def marker_stem(stem):
    # Generated non-Markdown names include a short suffix beyond the input limit.
    if portable_stem(stem, limit=93):
        return stem
    return document_stem(Path(stem + ".md"))


def review_files(source, *, as_source=False):
    require(Path(source).is_absolute(), "Supply an absolute file path")
    source = checked(source)
    match = None if as_source else re.fullmatch(r"(.+)-RP\.md", source.name)
    require(not source.exists() or source.is_file(), "Input must be a regular file")
    stem = marker_stem(match[1]) if match else document_stem(source)
    entries = list(source.parent.iterdir())
    if match is None and portable_stem(source.stem) and source.stem != source.stem.lower():
        # Earlier builds incorrectly hashed uppercase stems. Do not silently
        # create a second history or decide how existing files should be renamed.
        previous = "file-" + digest(source.name.encode("utf-8"))[:20]
        previous_pattern = re.compile(re.escape(previous) + r"-(?:RP|R[0-9]{2,}[AC])\.md")
        previous_files = sorted(str(p) for p in entries if previous_pattern.fullmatch(p.name))
        if previous_files:
            raise ClarificationNeeded(
                "Existing review files use an earlier hashed name for this source: "
                + ", ".join(previous_files)
                + ". Select the existing plan explicitly to continue that family, or ask "
                "the operator how to reconcile the filenames. No files were changed."
            )
    pattern = re.compile(re.escape(stem) + r"-R([0-9]{2,})([AC])\.md")
    replies = []
    for path in entries:
        turn = pattern.fullmatch(path.name)
        if turn:
            path = checked(path)
            require(path.is_file(), f"Reply is not a regular file: {path}")
            replies.append((int(turn[1]), turn[2], path))
    replies.sort()
    plan = checked(source.with_name(stem + "-RP.md"))
    require(not plan.exists() or plan.is_file(), "Plan is not a regular file")
    return {"plan": str(plan), "replies": [str(item[2]) for item in replies]}


def inspect_review(source, role=None, confirmed_role=None, instruction=None):
    require(Path(source).is_absolute(), "Supply an absolute review path")
    source = checked(source)
    require(source.is_file(), "Initial review must be a regular file")
    role = review_author_role(source, role, confirmed_role, instruction)
    plan = Path(review_files(source, as_source=True)["plan"])
    stem = plan.name.removesuffix("-RP.md")
    first_reply = source.with_name(f"{stem}-R01{'C' if role == 'author' else 'A'}.md")
    return {"source": str(source), "authorRole": role,
            "nextRole": "critic" if role == "author" else "author",
            "firstReply": str(first_reply), "plan": str(plan)}


def clean_review(source, *, as_source=False):
    paths = review_files(source, as_source=as_source)
    # The explicit clean command scopes deletion to this exact filename family.
    files = [Path(p) for p in paths["replies"]]
    plan = Path(paths["plan"])
    if plan.exists():
        files.append(plan)  # Leave the plan available if an earlier deletion fails.
    for path in files:
        own_review = review_files(path, as_source=True)
        if Path(own_review["plan"]).exists() or own_review["replies"]:
            raise ClarificationNeeded(
                f"Cleanup would remove a source with its own review files: {path}. "
                "Clarify the overlapping discussions before deleting any files."
            )
    versions = [(path, path.stat(), digest(path.read_bytes())) for path in files]
    for path, previous, checksum in versions:
        current = checked(path).stat()
        require((current.st_ino, current.st_mtime_ns, current.st_size)
                == (previous.st_ino, previous.st_mtime_ns, previous.st_size)
                and digest(path.read_bytes()) == checksum, f"File changed during cleanup: {path}")
        path.unlink()
    return {"removed": [str(path) for path in files]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["init", "update", "files", "inspect-review", "clean"])
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--author-role", choices=["author", "critic"])
    parser.add_argument("--as-source", action="store_true",
                        help="Treat the path as the original document, even with an RP suffix")
    parser.add_argument("--confirmed-role", choices=["author", "critic"])
    parser.add_argument("--operator-instruction")
    args = parser.parse_args(argv)
    try:
        require(not args.as_source or args.operation in {"files", "clean"},
                "--as-source is only valid with files or clean")
        if args.operation in {"init", "update"}:
            root = args.path or args.project or find_project_root(Path.cwd())
            require(root.is_absolute(), "Supply an absolute project path")
            root = checked(root)
            require(root.is_dir(), "Project must be a directory")
            require(args.project is None or checked(args.project) == root,
                    "The project argument and --project must agree")
            result = (initialize_project_config(root) if args.operation == "init"
                      else update_project_config(root))
        else:
            require(args.path is not None, "Supply an absolute file path")
            if args.operation == "inspect-review":
                result = inspect_review(args.path, args.author_role, args.confirmed_role,
                                        args.operator_instruction)
            elif args.operation == "clean":
                result = clean_review(args.path, as_source=args.as_source)
            else:
                result = review_files(args.path, as_source=args.as_source)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ClarificationNeeded as exc:
        print(json.dumps({"status": "needs-clarification", "error": str(exc)}), file=sys.stderr)
        return 2
    except (ReviewError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
