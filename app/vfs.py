"""
vfs.py
------
A tiny, self-contained virtual filesystem tree used to represent the
contents of a project while the user is working on it in ISOForge(ZR).

Deliberately format-agnostic
============================
This tree knows nothing about ISO9660, ZIP, 7Z, RAR or self-extracting
EXEs. A node just records WHERE its bytes currently live
(`source_kind`), and the relevant backend module knows how to fetch
them. That is what allows the exact same editing, navigation,
rename/delete and progress logic to serve every container format, and
why adding archive support needed no changes to the tree's structure at
all -- only one extra `source_kind` value.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import List, Optional, Tuple


class VNode:
    """A single file or folder inside a project."""

    def __init__(self, name: str, is_dir: bool, parent: Optional["VNode"] = None):
        self.id: str = uuid.uuid4().hex
        self.name: str = name
        self.is_dir: bool = is_dir
        self.parent: Optional["VNode"] = parent
        self.children: List["VNode"] = []
        self.size: int = 0
        self.mtime: float = time.time()

        # Where this node's bytes currently live:
        #   "new"     -> created inside the app; nothing to copy
        #   "disk"    -> staged from the local filesystem
        #   "iso"     -> still inside a previously opened .iso  (source_iso)
        #   "archive" -> still inside a previously opened archive
        #                (source_archive)
        self.source_kind: str = "new"
        self.source_path: Optional[str] = None
        self.source_iso = None
        self.source_archive = None

    def new_child(self, name: str, is_dir: bool) -> "VNode":
        child = VNode(name, is_dir, parent=self)
        self.children.append(child)
        return child

    def add_child(self, child: "VNode") -> None:
        child.parent = self
        self.children.append(child)

    def remove_child(self, child: "VNode") -> None:
        if child in self.children:
            self.children.remove(child)

    def find_child(self, name: str, is_dir: Optional[bool] = None) -> Optional["VNode"]:
        for c in self.children:
            if c.name.lower() == name.lower() and (is_dir is None or c.is_dir == is_dir):
                return c
        return None

    def unique_name(self, desired: str) -> str:
        existing = {c.name.lower() for c in self.children}
        if desired.lower() not in existing:
            return desired
        stem, ext = os.path.splitext(desired)
        n = 2
        while True:
            candidate = f"{stem} ({n}){ext}"
            if candidate.lower() not in existing:
                return candidate
            n += 1

    def path(self) -> str:
        parts = []
        node = self
        while node is not None and node.parent is not None:
            parts.append(node.name)
            node = node.parent
        return "/" + "/".join(reversed(parts))

    def path_length(self) -> int:
        return len(self.path())

    def total_size(self) -> int:
        if not self.is_dir:
            return self.size
        return sum(c.total_size() for c in self.children)

    def file_count(self) -> int:
        if not self.is_dir:
            return 1
        return sum(c.file_count() for c in self.children)

    def folder_count(self) -> int:
        if not self.is_dir:
            return 0
        return sum((1 + c.folder_count()) for c in self.children if c.is_dir)

    def sorted_children(self) -> List["VNode"]:
        return sorted(self.children, key=lambda c: (not c.is_dir, c.name.lower()))


def find_long_paths(start: VNode, threshold: int) -> List[Tuple[VNode, int]]:
    results: List[Tuple[VNode, int]] = []

    def walk(node: VNode) -> None:
        if node.parent is not None:
            length = node.path_length()
            if length >= threshold:
                results.append((node, length))
        if node.is_dir:
            for child in node.children:
                walk(child)

    walk(start)
    results.sort(key=lambda pair: -pair[1])
    return results


class ForgeProject:
    """Holds the state for the container currently being edited --
    whether that's an ISO, ZIP, 7Z, RAR or self-extracting EXE.

    Purely in-memory and created fresh every time MainWindow is
    constructed; nothing here is ever persisted between sessions.
    """

    def __init__(self):
        self.root: VNode = VNode(name="", is_dir=True)
        self.current_dir: VNode = self.root
        self.dirty: bool = False
        self.save_path: Optional[str] = None
        self.volume_label: str = ""

        # The currently-open source, if this project was loaded from an
        # existing file. Holds either an iso_backend.IsoSource or an
        # archive_backend.ArchiveSource -- `fmt` says which.
        self.source = None
        self.fmt: Optional[str] = None

        # Password last used for this project, so a re-save of an
        # encrypted archive can keep it without re-prompting.
        self.password: Optional[str] = None

        self._history: List[VNode] = [self.root]
        self._history_index: int = 0

    # -- typed accessors, so callers don't have to inspect `fmt` ------ #
    #
    # These inspect the SOURCE OBJECT itself rather than `fmt`. The two can
    # legitimately disagree: after "Save As" to a different format (e.g.
    # open an ISO, save it as a ZIP), `fmt` becomes the new format while
    # `source` is still the original IsoSource the unmodified nodes read
    # from. Deciding by `fmt` handed an IsoSource to callers expecting an
    # ArchiveSource -- Extract then crashed with "'IsoSource' object has no
    # attribute 'needs_password'". Only ArchiveSource has `archive_path`,
    # so that attribute identifies the source type without importing
    # either backend (iso_backend would drag in pycdlib).

    @property
    def source_iso(self):
        """The source, but only when it's an ISO -- otherwise None."""
        src = self.source
        if src is None or hasattr(src, "archive_path"):
            return None
        return src

    @property
    def source_archive(self):
        """The source, but only when it's an archive -- otherwise None."""
        src = self.source
        return src if src is not None and hasattr(src, "archive_path") else None

    @property
    def is_archive(self) -> bool:
        from . import formats
        return formats.is_archive(self.fmt)

    def close_source(self) -> None:
        if self.source is not None:
            try:
                self.source.close()
            except Exception:
                pass
            self.source = None

    # -- navigation --------------------------------------------------- #
    def navigate_to(self, node: VNode, record_history: bool = True) -> None:
        if not node.is_dir:
            return
        self.current_dir = node
        if record_history:
            del self._history[self._history_index + 1:]
            self._history.append(node)
            self._history_index = len(self._history) - 1

    def can_go_back(self) -> bool:
        return self._history_index > 0

    def can_go_forward(self) -> bool:
        return self._history_index < len(self._history) - 1

    def go_back(self) -> Optional[VNode]:
        if self.can_go_back():
            self._history_index -= 1
            self.current_dir = self._history[self._history_index]
            return self.current_dir
        return None

    def go_forward(self) -> Optional[VNode]:
        if self.can_go_forward():
            self._history_index += 1
            self.current_dir = self._history[self._history_index]
            return self.current_dir
        return None

    def go_up(self) -> Optional[VNode]:
        if self.current_dir.parent is not None:
            self.navigate_to(self.current_dir.parent)
            return self.current_dir
        return None

    # -- mutation ----------------------------------------------------- #
    def mark_dirty(self) -> None:
        self.dirty = True

    def new_folder(self, parent: VNode, name: str) -> VNode:
        final_name = parent.unique_name(name)
        node = parent.new_child(final_name, is_dir=True)
        node.source_kind = "new"
        self.mark_dirty()
        return node

    def delete_nodes(self, nodes: List[VNode]) -> None:
        for node in nodes:
            if node.parent is not None:
                node.parent.remove_child(node)
        self.mark_dirty()

    def rename_node(self, node: VNode, new_name: str) -> bool:
        if node.parent is None:
            return False
        if node.parent.find_child(new_name) is not None:
            return False
        node.name = new_name
        self.mark_dirty()
        return True

    def rename_volume(self, new_label: str) -> None:
        self.volume_label = new_label
        self.mark_dirty()

    def total_objects(self) -> int:
        return self.root.file_count() + self._count_folders(self.root)

    def _count_folders(self, node: VNode) -> int:
        total = 0
        for c in node.children:
            if c.is_dir:
                total += 1 + self._count_folders(c)
        return total


# The project class was called IsoProject before archive support existed.
# Keeping the old name as an alias means any code (or muscle memory)
# still referring to it keeps working.
IsoProject = ForgeProject

