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

# 2.9.0 -- CHD conversion:
#   * New Tools > Settings section for chdman.exe (app/chd_tools.py,
#     app/settings_dialog.py, app/settings_store.py).
#   * New "Convert to CHD" toolbar/menu action; the original "Convert"
#     action is now called "Convert Folder" (app/main_window.py).
#   * New Explorer right-click item, "Convert to CHD with ISOForge", for
#     .cue / .gdi / .toc / .iso (app/shell_integration.py,
#     app/shell_commands.py).
# 2.9.1 -- the title-bar [X] on Settings, Convert Folder and Convert to CHD
#   did nothing. All pop-up dialogs now share app/dialog_base.ForgeDialog,
#   which declares its window flags explicitly, force-enables the system
#   menu's Close entry, and handles Windows' close commands directly. See
#   that module's docstring for the full reasoning.
# 2.10.0 -- CHD disc images can now be opened and saved, not just created:
#   * Open: a CHD is unpacked by chdman (extractcd / extractdvd) into a
#     temporary ISO and browsed like any other disc (app/chd_backend.py,
#     app/chd_tools.py).
#   * Save / Save As / Convert Folder: "CHD disc image (*.chd)" is offered
#     whenever chdman is available (app/formats.py, app/main_window.py).
# 2.10.1 -- "Compress with ISOForge" on several selected files opened a
#   separate session per file. Cause: every process Explorer launched checked
#   for a running instance, then spent about a second building its window
#   BEFORE it started listening, so none of them ever found another. Now the
#   first process claims the instance name immediately after startup and every
#   other process hands its file over to it (app/main_window.py run(),
#   app/single_instance.py). A file that still arrives late is added to the
#   same session instead of replacing it (MainWindow.stage_for_compression).
__version__ = "2.10.1"





