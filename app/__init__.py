"""ISOForge(ZR) application package."""

# 2.8.0 fixes ISO/archive builds appearing to hang at "100%" for a long
# time with Cancel seemingly doing nothing. Root cause: pycdlib performs
# a final backward pass over the output file after all data has been
# written (patching volume descriptors, path tables and UDF structures
# once the whole layout is known), and separately, the OS can defer
# actually flushing several GB of buffered writes to physical disk until
# the file is closed -- both of which happened with zero progress
# reporting and zero opportunity to notice a cancellation request. See
# app/progress.py (ProgressWriter: always-report-on-write, periodic
# disk sync) and app/iso_backend.py / app/archive_backend.py
# (post-write cancellation safety net before committing) for the fixes,
# and app/main_window.py (ProgressDialog) for the clarified "still
# cancelling" messaging.
# 2.8.1 -- maintenance fixes:
#   * ZIP: members larger than ~2 GB were silently skipped because the
#     streaming writer never enabled ZIP64 (archive_backend
#     _open_zip_member_for_write).
#   * 7Z: members are streamed to py7zr instead of being read fully into
#     RAM first, so multi-GB files no longer risk exhausting memory.
#   * Extract after "Save As" into a different container type (e.g. ISO
#     -> ZIP) crashed; ForgeProject.source_iso / source_archive now decide
#     by the source object's type rather than the project's format.
#   * Installer version is now taken from __version__ by
#     build_installer.bat, so the setup file can't drift out of step.

__version__ = "2.8.1"

