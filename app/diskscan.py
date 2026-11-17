"""
diskscan.py
-----------
Staging local files and folders into the virtual tree.

Lives in its own module for the same reason as `fileops.py`: both the ISO
and archive backends need it, and putting it in either one would force
the other to import a library it doesn't otherwise need (`pycdlib` in
particular). Keeping it here means adding a folder to a ZIP project works
whether or not the ISO stack is even installed.
"""
from __future__ import annotations

import os


class DiskScanError(RuntimeError):
    """Raised when a local folder can't be read while staging it."""


def populate_from_disk(parent_node, local_dir: str) -> None:
    """Recursively stages everything under `local_dir` as children of
    `parent_node`.

    Files are recorded as `source_kind="disk"` with a path rather than
    being read now -- their bytes are only pulled in at build time, so
    staging a large folder stays instant regardless of its size.
    """
    try:
        entries = sorted(
            os.scandir(local_dir),
            key=lambda e: (not e.is_dir(), e.name.lower()),
        )
    except OSError as exc:
        raise DiskScanError(f"Could not read '{local_dir}': {exc}") from exc

    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            child = parent_node.new_child(
                parent_node.unique_name(entry.name), is_dir=True)
            child.source_kind = "new"
            populate_from_disk(child, entry.path)
        else:
            child = parent_node.new_child(
                parent_node.unique_name(entry.name), is_dir=False)
            child.source_kind = "disk"
            child.source_path = entry.path
            try:
                child.size = entry.stat().st_size
            except OSError:
                child.size = 0
