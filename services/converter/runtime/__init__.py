"""Explicit runtime directories (T11, doc 03 §3).

The packaged desktop app and the developer checkout use the same modules; the
only difference is that every mutable directory is provided explicitly through
the environment before `config.settings` is materialized:

``APP_DATA_DIR``  database, identity, backups
``WORK_DIR``      snapshots and temporaries
``LOG_DIR``       rotating logs
``WEB_DIST_DIR``  built SPA (read-only resource)
``STORAGE_LOCAL_ROOT`` documents/artifacts

Nothing is ever written next to installed resources.
"""

from runtime.paths import RuntimePaths

__all__ = ["RuntimePaths"]
