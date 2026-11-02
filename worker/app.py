"""Procrastinate app. Run the worker with:

procrastinate --app=worker.app.app worker
"""

import procrastinate

from app.core.config import get_settings

app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=get_settings().queue_conninfo),
    import_paths=["worker.tasks"],
)
