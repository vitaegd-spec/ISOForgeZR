# ISOForgeZR.spec -- PyInstaller build spec.
# -*- mode: python ; coding: utf-8 -*-

import os

block_cipher = None
project_root = os.path.abspath(os.path.dirname(SPEC))

a = Analysis(
    ['main.py'],
    pathex=[project_root],
    binaries=[], datas=[],
    # The archive libraries are imported lazily inside archive_backend so
    # a missing one only disables that one format at runtime. PyInstaller
    # cannot see lazy imports, so they are declared here explicitly --
    # otherwise the packaged build would silently lose ZIP-AES / 7Z / RAR
    # support even though the libraries were installed at build time.
    #
    # QtNetwork is required by app/single_instance.py, which groups
    # Explorer's per-file context-menu launches into one action.
    #
    # Rar.exe / UnRAR.exe are NOT bundled: they are RARLAB's proprietary
    # tools and cannot be redistributed. ISOForge locates a user-supplied
    # copy at runtime (see app/rar_tools.py), including one sitting next
    # to the packaged .exe.
    hiddenimports=[
        'py7zr', 'pyzipper', 'rarfile',
        'PySide6.QtNetwork',
        'Cryptodome', 'Cryptodome.Cipher', 'Cryptodome.Cipher.AES',
        'Cryptodome.Util.Padding', 'Cryptodome.Random',
    ],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    win_no_prefer_redirects=False, win_private_assemblies=False,
    cipher=block_cipher, noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='ISOForgeZR',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=False,
    icon='build/isoforge.ico',
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas,
               strip=False, upx=True, upx_exclude=[], name='ISOForgeZR')
