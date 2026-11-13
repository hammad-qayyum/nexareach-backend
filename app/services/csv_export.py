import csv
import io
from collections.abc import Iterable
from typing import Any

from fastapi.responses import StreamingResponse


def cell(value: Any) -> Any:
    """Stop spreadsheet apps from running a value as a formula (CSV injection): "=…" becomes "'=…"."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{value}"
    return value


def csv_response(filename: str, header: list[str], rows: Iterable[Iterable[Any]]) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if v is None else cell(v) for v in row])
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
