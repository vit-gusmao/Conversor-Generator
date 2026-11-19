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

datas = []
if WEB_DIST.exists():
    datas.append((str(WEB_DIST), "apps/web/dist"))

excluded = ["tkinter", "matplotlib", "pytest", "IPython", "notebook"]

a = Analysis(
    ["desktop_entry.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
