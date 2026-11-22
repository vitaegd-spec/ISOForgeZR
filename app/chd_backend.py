"""
chd_backend.py
--------------
Opening and saving CHD disc images.

A CHD is not a file system. It is a compressed CONTAINER around a disc image,
the same way a .zip is around files -- except that what is inside is itself a
disc image (ISO 9660), not a list of files. So ISOForge treats a CHD as a thin
wrapper in both directions and reuses everything `iso_backend` already does:

    OPEN   CHD --chdman extract--> temporary ISO --IsoSource--> file tree
    SAVE   file tree --build_iso--> temporary ISO --chdman create--> CHD

That is why `ChdSource` below is a SUBCLASS of `iso_backend.IsoSource`: once
the temporary ISO exists, listing, extracting and re-saving files work through
exactly the same code as for an ordinary .iso, with the same self-healing read
logic. The only things this module adds are getting to that ISO and cleaning it
up afterwards.

Unpacking a CD (the part that is not just "run chdman")
=========================================================
`chdman extractdvd` writes a finished .iso, so DVDs are easy. A CD is not: a
CD stores RAW 2352-byte sectors (sync pattern, header, then the payload, then
error-correction bytes), and `chdman extractcd` writes those raw sectors to a
.bin with a .cue sheet describing the tracks. ISO 9660 readers -- pycdlib
included -- expect plain 2048-byte sectors. So the data track is converted
here, sector by sector, by dropping everything around the 2048 payload bytes:

    MODE1/2352   16-byte header   | 2048 data | 288 ECC     (most PC CDs)
    MODE2/2352   24-byte header   | 2048 data | 280 ECC     (PlayStation, Saturn)
    MODE1/2048   nothing to strip                            (already cooked)

Only the FIRST DATA TRACK is used. Audio tracks hold no files, and that is
where the ISO 9660 file system lives on every disc ISOForge can sensibly show
-- including mixed-mode discs with audio after the data.

What cannot be opened, and why
================================
  * Dreamcast GD-ROM images: their data sits in a separate high-density area
    laid out at its own addresses, not as an ISO 9660 disc starting at sector 0.
  * Hard-disk and laserdisc CHDs: they contain no disc file system at all.
  * Discs using a console-specific file system instead of ISO 9660. This is
    detected up front (the volume descriptor is checked) so the message can say
    what is wrong instead of surfacing a library error.

For PlayStation-style MODE2 discs, files stored as raw XA streams (video and
audio) can't be reproduced bit-exactly through a 2048-byte view. Ordinary
files are unaffected.

Saving
=======
The project is first built as an ordinary ISO (the same adaptive format ladder
`iso_backend.build_iso` always uses), then compressed by chdman. A CHD made this
way stores 2048-byte sectors, which is exactly right for disc images meant for
PC, PlayStation 2, PSP, GameCube and similar. It is NOT a bit-for-bit copy of
a raw 2352-byte console CD -- ISOForge edits files, not raw sectors.

Disk space
===========
Both directions go through a full-size temporary image: opening needs roughly
the CHD's uncompressed size (about twice that for a CD, briefly, while the raw
data is converted), and saving needs room for the ISO before it is compressed.
Everything is removed again when the project is closed or the save finishes.

Deliberately Qt-free, like the other backends.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from typing import Callable, List, Optional

from . import chd_tools
from .fileops import BuildResult, OperationCancelled
from .iso_backend import IsoBackendError, IsoSource, build_iso
from .progress import PROGRESS_SCALE, ProgressReporter

ProgressCB = Optional[Callable[[str, int, int], None]]


class ChdBackendError(RuntimeError):
    """Raised for any user-facing failure originating in this module."""


# --------------------------------------------------------------------- #
# Reading the cue sheet chdman writes
# --------------------------------------------------------------------- #

_FILE_RE = re.compile(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))', re.IGNORECASE)
_TRACK_RE = re.compile(r"^\s*TRACK\s+(\d+)\s+(\S+)", re.IGNORECASE)
_INDEX_RE = re.compile(r"^\s*INDEX\s+(\d+)\s+(\d+):(\d+):(\d+)", re.IGNORECASE)

# Payload bytes to skip at the start of a sector, keyed by (family, size).
# See the module docstring for the layouts these come from.
_PAYLOAD_SKIP = {
    ("MODE1", 2352): 16,
    ("MODE2", 2352): 24,
    ("MODE1", 2048): 0,
    ("MODE2", 2048): 0,
    ("MODE2", 2336): 8,
}

_USER_DATA = 2048
_ISO_SYSTEM_AREA_SECTORS = 16     # the volume descriptors start at sector 16
_COOK_CHUNK_BYTES = 4 * 1024 * 1024


class CueTrack:
    """One TRACK entry from a cue sheet, plus where it sits in its file."""

    __slots__ = ("number", "mode", "family", "sector_size", "file",
                 "index00", "index01", "data_offset", "data_frames")

    def __init__(self, number: int, mode: str, file: str):
        self.number = number
        self.mode = mode.upper()
        self.file = file
        self.index00: Optional[int] = None
        self.index01: Optional[int] = None
        self.data_offset = 0
        self.data_frames: Optional[int] = None
        family, _, size = self.mode.partition("/")
        self.family = family
        try:
            self.sector_size = int(size) if size else 2352
        except ValueError:
            self.sector_size = 2352

    @property
    def is_audio(self) -> bool:
        return self.family == "AUDIO"


def _frames(minutes: str, seconds: str, frames: str) -> int:
    return (int(minutes) * 60 + int(seconds)) * 75 + int(frames)


def parse_cue(text: str) -> List[CueTrack]:
    """Reads the tracks out of a cue sheet.

    Only the few commands that matter for locating data are understood; the
    rest (PREGAP, FLAGS, CATALOG and so on) are ignored, which is correct:
    none of them occupy bytes in the .bin.
    """
    tracks: List[CueTrack] = []
    current_file = ""
    current: Optional[CueTrack] = None
    for line in (text or "").splitlines():
        match = _FILE_RE.match(line)
        if match:
            current_file = match.group(1) or match.group(2) or ""
            current = None
            continue
        match = _TRACK_RE.match(line)
        if match:
            current = CueTrack(int(match.group(1)), match.group(2), current_file)
            tracks.append(current)
            continue
        match = _INDEX_RE.match(line)
        if match and current is not None:
            position = _frames(match.group(2), match.group(3), match.group(4))
            if int(match.group(1)) == 0:
                current.index00 = position
            elif int(match.group(1)) == 1:
                current.index01 = position
    return [t for t in tracks if t.index01 is not None]


def _lay_out(tracks: List[CueTrack]) -> None:
    """Works out, for every track, the byte offset of its data in its file.

    Needed because the .bin is one continuous file but tracks can have
    different sector sizes (a 2048-byte data track followed by 2352-byte
    audio, for example), so a track's offset is the sum of the SIZES of what
    came before it, not simply its time position multiplied by one number.

    A track's region starts at its INDEX 00 (pregap) when it has one,
    otherwise at INDEX 01; its data starts at INDEX 01.
    """
    by_file: dict = {}
    for track in tracks:
        by_file.setdefault(track.file, []).append(track)
    for group in by_file.values():
        def region_start(t: CueTrack) -> int:
            return t.index00 if t.index00 is not None else t.index01

        position = region_start(group[0]) * group[0].sector_size
        for k, track in enumerate(group):
            start = region_start(track)
            track.data_offset = position + (track.index01 - start) * track.sector_size
            if k + 1 < len(group):
                following = region_start(group[k + 1])
                track.data_frames = max(0, following - track.index01)
                position += max(0, following - start) * track.sector_size
            else:
                track.data_frames = None     # runs to the end of the file


def find_data_track(tracks: List[CueTrack]) -> CueTrack:
    """The first track that is not audio. Raises a plain-English error if
    there isn't one, or it is a kind this module can't read."""
    if not tracks:
        raise ChdBackendError(
            "chdman produced a cue sheet with no usable tracks.")
    _lay_out(tracks)
    for track in tracks:
        if track.is_audio:
            continue
        if (track.family, track.sector_size) not in _PAYLOAD_SKIP:
            raise ChdBackendError(
                f"The data track on this disc uses a sector format "
                f"({track.mode}) that ISOForge can't read.")
        return track
    raise ChdBackendError(
        "This CHD holds only audio tracks, so there are no files to show.")


# --------------------------------------------------------------------- #
# Turning raw CD sectors into an ISO
# --------------------------------------------------------------------- #

def cook_track(bin_path: str, out_iso: str, track: CueTrack,
               on_fraction: Optional[Callable[[float], None]] = None,
               cancel_token=None) -> int:
    """Copies the 2048 payload bytes out of every sector of `track`.

    Returns how many sectors were written. Works in multi-megabyte chunks:
    a CD is a few hundred thousand sectors, and a per-sector read/write
    loop would spend nearly all its time in call overhead.
    """
    size = track.sector_size
    skip = _PAYLOAD_SKIP[(track.family, size)]
    try:
        file_size = os.path.getsize(bin_path)
    except OSError as exc:
        raise ChdBackendError(f"Could not read the unpacked disc data: {exc}") from exc

    available = max(0, (file_size - track.data_offset) // size)
    total = available if track.data_frames is None else min(track.data_frames, available)
    if total <= 0:
        raise ChdBackendError("The data track on this disc is empty.")

    per_chunk = max(1, _COOK_CHUNK_BYTES // size)
    done = 0
    try:
        with open(bin_path, "rb") as source, open(out_iso, "wb") as dest:
            source.seek(track.data_offset)
            while done < total:
                if cancel_token is not None and cancel_token.cancelled:
                    raise OperationCancelled()
                want = min(per_chunk, total - done)
                raw = source.read(want * size)
                got = len(raw) // size
                if got == 0:
                    break
                if skip == 0 and size == _USER_DATA:
                    dest.write(raw[: got * size])
                else:
                    view = memoryview(raw)
                    dest.write(b"".join(
                        view[i * size + skip: i * size + skip + _USER_DATA]
                        for i in range(got)))
                done += got
                if on_fraction is not None:
                    on_fraction(done / total)
                if got < want:
                    break
    except OSError as exc:
        raise ChdBackendError(
            f"Could not convert the disc data (is the disk full?): {exc}") from exc
    return done


def _has_iso9660(iso_path: str) -> bool:
    """True if the image carries an ISO 9660 volume descriptor.

    Every standard volume descriptor has "CD001" at bytes 1-5, and the first
    one sits at sector 16 -- so one 6-byte read is a reliable test, and a
    friendlier one than waiting for the ISO library to choke.
    """
    try:
        with open(iso_path, "rb") as fh:
            fh.seek(_ISO_SYSTEM_AREA_SECTORS * _USER_DATA)
            return fh.read(6)[1:6] == b"CD001"
    except OSError:
        return False


# --------------------------------------------------------------------- #
# Opening
# --------------------------------------------------------------------- #

class ChdSource(IsoSource):
    """A CHD opened for browsing: an `IsoSource` over its temporary ISO.

    Everything about reading files comes from `IsoSource`. This subclass only
    remembers where the CHD came from and removes the temporary files when
    the project is closed -- they can be gigabytes, so they must never be
    left behind.
    """

    def __init__(self, chd_path: str, iso_path: str, temp_dir: str, media: str):
        self.chd_path = chd_path
        self.disc_media = media
        self._temp_dir = temp_dir
        super().__init__(iso_path)

    def close(self) -> None:
        super().close()
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = ""


_UNSUPPORTED = {
    chd_tools.MEDIA_GDROM: (
        "This CHD holds a Dreamcast GD-ROM image. ISOForge can't browse "
        "those: the game data lives in a separate high-density area that "
        "isn't laid out like an ordinary disc.\n\n"
        "You can still unpack it with chdman (extractcd, writing a .gdi)."),
    chd_tools.MEDIA_HARDDISK: (
        "This CHD holds a hard-disk image, not a CD or DVD, so there is no "
        "disc file system for ISOForge to open."),
    chd_tools.MEDIA_LASERDISC: (
        "This CHD holds a laserdisc recording (video), not a data disc, so "
        "there are no files for ISOForge to show."),
}


def open_chd(path: str, progress_cb: ProgressCB = None,
             cancel_token=None) -> ChdSource:
    """Unpacks `path` to a temporary ISO and opens it for browsing.

    Progress is reported on the usual 0..1000 scale across both stages
    (chdman unpacking, then -- for CDs -- converting the raw sectors).
    Cancelling, or any failure, removes the temporary folder before the
    exception leaves this function.
    """
    info = chd_tools.read_info(path)
    if info.media in _UNSUPPORTED:
        raise ChdBackendError(_UNSUPPORTED[info.media])

    name = os.path.basename(path)
    reporter = ProgressReporter(progress_cb, PROGRESS_SCALE,
                                cancel_token=cancel_token)
    reporter.advance(0, f"Preparing to unpack {name}...")

    # A CHD of unrecognised type is tried both ways rather than refused:
    # one of the two will say clearly that it doesn't fit, and the other may
    # simply work.
    if info.media == chd_tools.MEDIA_CD:
        attempts = [chd_tools.MEDIA_CD]
    elif info.media == chd_tools.MEDIA_DVD:
        attempts = [chd_tools.MEDIA_DVD]
    else:
        attempts = [chd_tools.MEDIA_CD, chd_tools.MEDIA_DVD]

    temp_dir = tempfile.mkdtemp(prefix="isoforgechd_")
    try:
        iso_path = None
        media = attempts[0]
        failures: List[str] = []
        for media in attempts:
            attempt_dir = os.path.join(temp_dir, media)
            os.makedirs(attempt_dir, exist_ok=True)
            try:
                iso_path = _unpack(media, path, attempt_dir, name, reporter,
                                   cancel_token)
                break
            except chd_tools.ChdToolError as exc:
                failures.append(str(exc))
                shutil.rmtree(attempt_dir, ignore_errors=True)
        if iso_path is None:
            detail = failures[-1] if failures else ""
            if len(attempts) > 1:
                raise ChdBackendError(
                    f"ISOForge couldn't unpack '{name}' as a CD or as a "
                    f"DVD image.\n\n{detail}")
            raise ChdBackendError(detail)

        if not _has_iso9660(iso_path):
            raise ChdBackendError(
                f"'{name}' doesn't contain an ISO 9660 file system, which is "
                "what ISOForge reads.\n\n"
                "It may be an audio disc, or use a file system specific to a "
                "particular console.")
        try:
            source = ChdSource(path, iso_path, temp_dir, media)
        except IsoBackendError as exc:
            raise ChdBackendError(
                f"The disc inside '{name}' could not be read as an ISO "
                f"image.\n\n{exc}") from exc
        reporter.finish("Reading file list...")
        return source
    except BaseException:
        # BaseException on purpose: OperationCancelled is one, and a
        # cancelled open must clean up exactly like a failed one.
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _unpack(media: str, chd_path: str, work_dir: str, name: str,
            reporter: ProgressReporter, cancel_token) -> str:
    """Unpacks one CHD as `media` and returns the path of the resulting ISO."""
    scale = float(PROGRESS_SCALE)
    iso_path = os.path.join(work_dir, "disc.iso")

    if media == chd_tools.MEDIA_DVD:
        def on_dvd(fraction: float) -> None:
            reporter.set_phase_position(
                0, fraction * scale * 0.97,
                f"Unpacking {name} (DVD) — {fraction * 100:.0f}%")
        chd_tools.extract_dvd(chd_path, iso_path, on_dvd, cancel_token)
        return iso_path

    cue_path = os.path.join(work_dir, "disc.cue")
    bin_path = os.path.join(work_dir, "disc.bin")

    def on_cd(fraction: float) -> None:
        reporter.set_phase_position(
            0, fraction * scale * 0.80,
            f"Unpacking {name} (CD) — {fraction * 100:.0f}%")
    chd_tools.extract_cd(chd_path, cue_path, bin_path, on_cd, cancel_token)

    try:
        with open(cue_path, "rb") as fh:
            raw_cue = fh.read(2 * 1024 * 1024)
    except OSError as exc:
        raise ChdBackendError(f"Could not read the unpacked cue sheet: {exc}") from exc
    try:
        cue_text = raw_cue.decode("utf-8-sig")
    except UnicodeDecodeError:
        cue_text = raw_cue.decode("latin-1")
    track = find_data_track(parse_cue(cue_text))

    def on_cook(fraction: float) -> None:
        reporter.set_phase_position(
            0, scale * (0.80 + fraction * 0.17),
            f"Preparing {name} — {fraction * 100:.0f}%")
    cook_track(bin_path, iso_path, track, on_cook, cancel_token)
    # The raw data is the larger of the two files; free it straight away.
    try:
        os.remove(bin_path)
    except OSError:
        pass
    return iso_path


# --------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------- #

def build_chd(
    root_node,
    out_path: str,
    volume_label: str = "",
    preference: str = chd_tools.KIND_AUTO,
    progress_cb: ProgressCB = None,
    cancel_token=None,
) -> BuildResult:
    """Builds the project as a CHD at `out_path`.

    Two stages sharing one progress bar: building the ISO (first half), then
    compressing it with chdman (second half). `preference` decides CD versus
    DVD; automatic picks by size (see `chd_tools.resolve_kind`).

    The ISO is built in a private temporary folder and the CHD is only moved
    into place once chdman has finished -- by `chd_tools.convert_to_chd`, with
    the same guarantee it always gives -- so a failed or cancelled save can
    never damage an existing CHD.
    """
    if not chd_tools.can_convert():
        raise ChdBackendError(
            "Saving as CHD requires chdman.exe, which ships with MAME.\n\n"
            "Set its location under Tools > Settings, or save as ISO instead.")

    def stage(low: float, high: float):
        def report(label: str, done: int, total: int) -> None:
            if progress_cb is None:
                return
            fraction = (done / total) if total else 0.0
            progress_cb(label, int(low + fraction * (high - low)),
                        PROGRESS_SCALE)
        return report

    work_dir = tempfile.mkdtemp(prefix="isoforgechd_")
    try:
        tmp_iso = os.path.join(work_dir, "image.iso")
        iso_result = build_iso(
            root_node, tmp_iso, volume_label=volume_label,
            progress_cb=stage(0, PROGRESS_SCALE * 0.5),
            cancel_token=cancel_token)
        converted = chd_tools.convert_to_chd(
            tmp_iso, out_path, preference,
            progress_cb=stage(PROGRESS_SCALE * 0.5, PROGRESS_SCALE),
            cancel_token=cancel_token)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    label = f"CHD {chd_tools.kind_label(converted.kind)} image"
    if iso_result.used_fallback_format:
        label += f" ({iso_result.format_label})"
    return BuildResult(skipped=iso_result.skipped, format_label=label,
                       used_fallback_format=iso_result.used_fallback_format)


def quick_folder_to_chd(folder_path: str, out_path: str, volume_label: str = "",
                        progress_cb: ProgressCB = None,
                        cancel_token=None) -> BuildResult:
    """Convenience helper backing 'Convert Folder' when CHD is chosen."""
    from .diskscan import populate_from_disk
    from .vfs import VNode

    root = VNode(name="", is_dir=True)
    root.source_kind = "new"
    populate_from_disk(root, folder_path)
    label = volume_label or os.path.basename(os.path.normpath(folder_path)) or "ISOFORGE"
    return build_chd(root, out_path, volume_label=label,
                     progress_cb=progress_cb, cancel_token=cancel_token)
