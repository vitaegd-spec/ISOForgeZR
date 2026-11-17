# Building ISOForge(ZR) into a Windows Installer

Turns the project into a single **ISOForgeZR-Setup-x.x.x.exe** requiring
nothing pre-installed on the target PC.

## What you'll end up with

```
ISOForgeZR/
├── dist/ISOForgeZR/ISOForgeZR.exe
└── installer/Output/ISOForgeZR-Setup-2.2.0.exe
```

## Prerequisites

1. **Python 3.9+** — <https://www.python.org/downloads/>
2. **Inno Setup** (free) — <https://jrsoftware.org/isdl.php>

## Quick path

Double-click **`build\build_all.bat`**. It creates a build venv, installs
dependencies, regenerates the icon, runs PyInstaller, and (if Inno Setup
is installed) compiles the final installer.

## Manual path

```bat
python -m venv build_venv
build_venv\Scripts\activate.bat
pip install -r requirements.txt
pip install pyinstaller pillow
python build\make_icon.py
pyinstaller ISOForgeZR.spec --noconfirm
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\IsoForge.iss
```

## Explorer menu registration

The installer offers an **"Add ISOForge to the Explorer right-click menu"**
task. It runs `ISOForgeZR.exe --register-shell` with Inno's
`runasoriginaluser` flag — which matters: the entries live in
`HKEY_CURRENT_USER`, so running them as the elevated installer account
would write them into the administrator's hive where the actual user
would never see them.

`[UninstallRun]` calls `--unregister-shell` before deleting the files,
so the menu is cleaned up automatically. See `SHELL_INTEGRATION.md`.

## RAR tools are deliberately not bundled

`Rar.exe` and `UnRAR.exe` are RARLAB's proprietary tools and must not be
redistributed. Neither the PyInstaller spec nor the Inno Setup script
includes them.

End users supply their own copies (both ship with WinRAR) via
**Tools ▸ Settings**, or simply by dropping them into the install folder
— ISOForge auto-detects them there. See `RAR_SETUP.md`.

## Troubleshooting

### `'C:\Program' is not recognized...` when build_all.bat runs Inno Setup

Fixed in the current script (uses `goto`-based branching instead of
parenthesized `if/else` blocks, since `%ProgramFiles(x86)%` contains
literal parentheses that corrupt cmd's paren-counting inside a block).

### The packaged .exe loses ZIP-AES / 7Z / RAR support

The archive libraries are imported lazily, which PyInstaller can't see.
They're declared in `hiddenimports` in `ISOForgeZR.spec` — don't remove
them. `PySide6.QtNetwork` is listed there too, since the Explorer
multi-select batching depends on it.
