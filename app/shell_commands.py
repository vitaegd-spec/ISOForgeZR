"""
shell_commands.py
-----------------
Turns Explorer's context-menu invocations into ISOForge actions.

Four verbs are registered (see `shell_integration.py`):

    --shell-open PATH     open the archive in the normal editor window
    --extract-here PATH   unpack beside the archive, no window
    --extract-to PATH     unpack into a new subfolder, no window
    --compress PATH       stage file(s)/folder(s) into a fresh project

The two extract verbs deliberately run WITHOUT showing the main window:
the user asked for files on disk, not an editor, so they get a progress
dialog and a confirmation and nothing else. `--compress` and
`--shell-open` do show the window, because the user's next step is to
choose a format and save.

Batching
=========
`build_request()` produces one payload per launched process;
`merge_requests()` folds a whole batch (see `single_instance.py`) into a
single action. Merging rules:

  * several `--compress` launches become ONE project containing every
    path -- this is the case that matters, since it's what a multi-file
    selection produces;
  * several extract launches are performed in sequence within one
    process, rather than spawning a window each;
  * a mixed batch (which shouldn't normally happen, since a selection
    triggers one verb) falls back to the first request's verb so
    something sensible still happens.
"""
from __future__ import annotations

import os
from typing import List, Optional

# Verb identifiers, also used as the `action` field in IPC payloads.
OPEN = "open"
EXTRACT_HERE = "extract-here"
EXTRACT_TO = "extract-to"
COMPRESS = "compress"

_EXTRACT_ACTIONS = (EXTRACT_HERE, EXTRACT_TO)


class ShellRequest:
    """One resolved context-menu action."""

    __slots__ = ("action", "paths")

    def __init__(self, action: str, paths: List[str]):
        self.action = action
        self.paths = paths

    @property
    def is_extract(self) -> bool:
        return self.action in _EXTRACT_ACTIONS

    @property
    def wants_window(self) -> bool:
        return not self.is_extract

    def to_payload(self) -> dict:
        return {"action": self.action, "paths": list(self.paths)}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ShellRequest({self.action!r}, {self.paths!r})"


def build_request(args) -> Optional[ShellRequest]:
    """Builds a request from parsed argparse output, or None.

    Only ONE verb can be active per launch, which is guaranteed by the
    registry entries themselves; the ordering here simply resolves a
    hand-typed command line that supplies more than one.
    """
    if getattr(args, "compress", None):
        return ShellRequest(COMPRESS, list(args.compress))
    if getattr(args, "extract_here", None):
        return ShellRequest(EXTRACT_HERE, [args.extract_here])
    if getattr(args, "extract_to", None):
        return ShellRequest(EXTRACT_TO, [args.extract_to])
    if getattr(args, "shell_open", None):
        return ShellRequest(OPEN, [args.shell_open])
    return None


def request_from_payload(payload: dict) -> Optional[ShellRequest]:
    action = (payload or {}).get("action")
    paths = (payload or {}).get("paths") or []
    if action not in (OPEN, EXTRACT_HERE, EXTRACT_TO, COMPRESS):
        return None
    cleaned = [str(p) for p in paths if p]
    if not cleaned:
        return None
    return ShellRequest(action, cleaned)


def merge_requests(requests: List[ShellRequest]) -> Optional[ShellRequest]:
    """Folds a batch of same-verb requests into one.

    Duplicate paths are dropped: Explorer can occasionally invoke a verb
    twice for the same item, and silently de-duplicating avoids adding
    the same file to a project twice.
    """
    requests = [r for r in requests if r and r.paths]
    if not requests:
        return None

    action = requests[0].action
    # A mixed batch is anomalous; preferring COMPRESS keeps the most
    # destructive-to-get-wrong case (dropping files the user selected)
    # from happening.
    if any(r.action != action for r in requests):
        if any(r.action == COMPRESS for r in requests):
            action = COMPRESS

    merged: List[str] = []
    seen = set()
    for request in requests:
        if request.action != action and action != COMPRESS:
            continue
        for path in request.paths:
            key = os.path.normcase(os.path.abspath(path))
            if key not in seen:
                seen.add(key)
                merged.append(path)

    return ShellRequest(action, merged) if merged else None


# --------------------------------------------------------------------- #
# Destination helpers
# --------------------------------------------------------------------- #
def destination_for(action: str, archive_path: str) -> str:
    """Where an extract verb should put its output.

    "Extract Here" unpacks directly beside the archive. "Extract to
    subfolder" creates a folder named after the archive, matching what
    WinRAR and 7-Zip do.
    """
    folder = os.path.dirname(os.path.abspath(archive_path))
    if action == EXTRACT_HERE:
        return folder
    stem = os.path.splitext(os.path.basename(archive_path))[0]
    return os.path.join(folder, stem or "Extracted")


def unique_directory(path: str) -> str:
    """Returns `path`, or `path (2)` / `path (3)` ... if it's taken.

    Only used by "Extract to subfolder". Extracting two different
    archives that happen to share a stem should not silently merge their
    contents into one folder, which is what reusing an existing
    directory would do.
    """
    if not os.path.exists(path):
        return path
    counter = 2
    while True:
        candidate = f"{path} ({counter})"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def suggested_archive_name(paths: List[str]) -> str:
    """A sensible default filename when compressing a selection.

    One item is named after that item; several are named after their
    common parent folder, which is what a user would have called it.
    """
    if not paths:
        return "Archive"
    if len(paths) == 1:
        base = os.path.basename(os.path.abspath(paths[0].rstrip("\\/")))
        stem = base if os.path.isdir(paths[0]) else os.path.splitext(base)[0]
        return stem or "Archive"
    parent = os.path.basename(
        os.path.dirname(os.path.abspath(paths[0].rstrip("\\/"))))
    return parent or "Archive"
