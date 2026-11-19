"""Runtime hook: make bundled DLL directories explicit (PyInstaller onedir).

`cryptography`'s Rust extension lives in a package subdirectory and needs the
bundle root on the Windows DLL search path. PyInstaller usually adds it, but
relying on that has proven fragile across versions/AV setups; this hook makes
it explicit and harmless when the directories do not exist.
"""

import os
import sys

if getattr(sys, "frozen", False):
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    candidates = (
        base,
        os.path.join(base, "cryptography", "hazmat", "bindings"),
        os.path.join(base, "cryptography"),
    )
    for candidate in candidates:
        try:
            if os.path.isdir(candidate):
                os.add_dll_directory(candidate)
        except (AttributeError, OSError):
            # add_dll_directory is Windows-only and may fail on odd paths.
            pass
