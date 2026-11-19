# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: onedir build of the converter backend (T11).

Build:  pyinstaller --clean --noconfirm packaging/converter.spec
Output: dist/converter/converter.exe (Windows) or dist/converter/converter

Directory mode is validated first (PyInstaller's own recommendation): the
bundled web dist and model-independent assets stay external to the archive,
and nothing mutable is written inside the bundle.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).resolve().parent          # services/converter
REPO = ROOT.parents[1]
WEB_DIST = REPO / "apps" / "web" / "dist"

block_cipher = None

hiddenimports = [
    # FastAPI/uvicorn dynamic imports
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # Storage / DB drivers resolved at runtime
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.postgresql",
    "pypdf",
    "pdfplumber",
    "openpyxl",
    "pypdfium2",
    "PIL.Image",
    # Our dynamic submodules (capability probing imports them lazily)
    "ingestion.spreadsheet_input",
    "ingestion.spreadsheet_pipeline",
    "ingestion.image_input",
    "ingestion.image_pipeline",
    "sources.path_policy",
    "sources.scanner",
    "sources.snapshot",
    "sources.importer",
    "sources.submission",
    "sources.gateway_submission",
    "automation.calendar",
    "automation.scheduler",
    "automation.executor",
    "automation.loop",
    "automation.cleanup",
    "knowledge.job_runner",
    "exports.csv_writer",
    "exports.text_writer",
    "engine.excel.writer",
]

# `pdfminer` (via pdfplumber) imports `cryptography` unconditionally for
# encrypted PDFs, and PyJWT uses it for asymmetric algorithms. Its Rust
# extension needs its own DLLs collected, or the frozen app dies on import.
hiddenimports += collect_submodules("cryptography")

# Only the compiled SPA is bundled as data. Nothing from the repo tree (.env,
# databases, uploads, storage, work, .venv) is collected: the backend reads
# and writes all mutable state under APP_DATA_DIR at runtime.
if not (WEB_DIST / "index.html").is_file():
    raise SystemExit(
        f"apps/web/dist ausente em {WEB_DIST}: rode `npm --prefix apps/web run build` antes do PyInstaller."
    )
_private = [
    p
    for p in WEB_DIST.rglob("*")
    if p.is_file() and (p.name.startswith(".env") or p.suffix in {".db", ".sqlite", ".sqlite3"})
]
if _private:
    raise SystemExit(f"Arquivo privado encontrado em apps/web/dist, recusando empacotar: {_private[0]}")
datas = [(str(WEB_DIST), "apps/web/dist")]

# Rust-based crypto extension: keep its binaries next to the package.
binaries = collect_dynamic_libs("cryptography")

# Python 3.15 ships the `abi3t` compatibility shim (`python3t.dll`). Wheels built
# for that ABI (cryptography 50) import it, so the frozen app must carry it.
# Its location varies by installer/build machine: search the known places
# instead of assuming one. The first match wins; the bundle lands at "." so it
# sits next to converter.exe / _internal and the rthook's DLL directory.
_abi3t_candidates = [
    Path(sys.base_prefix) / "abi3t-compat" / "python3t.dll",
    Path(sys.base_prefix) / "python3t.dll",
    Path(sys.executable).resolve().parent / "python3t.dll",
]
_abi3t = next((candidate for candidate in _abi3t_candidates if candidate.is_file()), None)
if _abi3t is not None:
    binaries.append((str(_abi3t), "."))
else:
    print(
        "WARNING: python3t.dll not found in "
        + ", ".join(str(c.parent) for c in _abi3t_candidates)
        + "; the frozen backend will fail to import cryptography if it needs it. "
        "The build pipeline smoke test catches this.",
        file=sys.stderr,
    )

excluded = ["tkinter", "matplotlib", "pytest", "IPython", "notebook"]

a = Analysis(
    [str(ROOT / "desktop_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "packaging" / "rthook_dll.py")],
    excludes=excluded,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="converter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="converter",
)
