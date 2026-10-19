"""Read-only MCP server for Windows Microsoft Sticky Notes (plum.sqlite).

Exposes list / get / search only. This server never writes to the database
or to any file.
"""

import os
import re
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

# .NET DateTime.Ticks epoch (0001-01-01 00:00:00 UTC)
TICKS_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)

DEFAULT_DB_PATH = Path(os.path.expandvars(
    r"%LOCALAPPDATA%\Packages\Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe\LocalState\plum.sqlite"
))

VALID_THEMES = {"Yellow", "Green", "Pink", "Purple", "Blue", "Grey", "Charcoal"}

MAX_LIMIT = 100

# Each stored line starts with a paragraph id: "\id=<uuid> "
RE_LINE_ID = re.compile(r"^\\id=[0-9a-f-]+ ?", re.IGNORECASE)
# List marker right after the paragraph id
RE_LIST_ITEM = re.compile(r"^\\l(?![A-Za-z0-9]) ?")
# Only known formatting control words. The lookahead keeps user text such as
# "C:\build\index" intact, since "\b" there is followed by a letter.
# An opening word may be followed by one delimiter space; a closing word is not.
RE_FORMAT_ON = re.compile(r"\\(strike|b|i)(?![A-Za-z0-9]) ?")
RE_FORMAT_OFF = re.compile(r"\\(strike0|b0|i0|l0)(?![A-Za-z0-9])")
FORMAT_TO_MARKDOWN = {
    "b": "**", "b0": "**",
    "i": "*", "i0": "*",
    "strike": "~~", "strike0": "~~",
    "l0": "",
}

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def get_db_path() -> Path:
    """Resolve database path from STICKY_NOTES_DB_PATH or the standard location."""
    env_path = os.getenv("STICKY_NOTES_DB_PATH")
    return Path(env_path) if env_path else DEFAULT_DB_PATH


def ticks_to_iso(ticks: int | None) -> str | None:
    """Convert .NET DateTime.Ticks to an ISO 8601 UTC string."""
    if not ticks:
        return None
    try:
        return (TICKS_EPOCH + timedelta(microseconds=ticks // 10)).isoformat()
    except (OverflowError, ValueError):
        return None


def clean_text(raw_text: str | None) -> str:
    """Convert Sticky Notes markup into readable Markdown."""
    if not raw_text:
        return ""
    lines = []
    for line in raw_text.splitlines():
        line = RE_LINE_ID.sub("", line)
        line = RE_LIST_ITEM.sub("- ", line)
        line = RE_FORMAT_ON.sub(lambda m: FORMAT_TO_MARKDOWN[m.group(1)], line)
        line = RE_FORMAT_OFF.sub(lambda m: FORMAT_TO_MARKDOWN[m.group(1)], line)
        lines.append(line)
    return "\n".join(lines).strip()


def clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def normalize_theme(theme: str) -> str:
    normalized = theme.strip().capitalize()
    if normalized not in VALID_THEMES:
        raise ValueError(f"Unknown theme: {theme}. Valid: {', '.join(sorted(VALID_THEMES))}")
    return normalized


def connect() -> sqlite3.Connection:
    """Open the database read-only."""
    db_path = get_db_path()
    if not db_path.is_file():
        raise FileNotFoundError("Sticky Notes database not found. Set STICKY_NOTES_DB_PATH if it is elsewhere.")
    uri = f"file:{urllib.parse.quote(db_path.as_posix())}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def fetch_notes(theme: str | None, is_open: bool | None, include_deleted: bool) -> list[sqlite3.Row]:
    clauses = []
    params: list[Any] = []
    if not include_deleted:
        clauses.append("DeletedAt IS NULL")
    if theme:
        clauses.append("Theme = ?")
        params.append(normalize_theme(theme))
    if is_open is not None:
        clauses.append("IsOpen = ?")
        params.append(1 if is_open else 0)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = connect()
    try:
        return conn.execute(
            f"""
            SELECT Id, Text, Theme, IsOpen, IsAlwaysOnTop, CreatedAt, UpdatedAt, DeletedAt
            FROM Note
            {where_sql}
            ORDER BY UpdatedAt DESC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()


def summarize(row: sqlite3.Row, text: str) -> dict[str, Any]:
    return {
        "id": row["Id"],
        "theme": row["Theme"] or "Yellow",
        "is_open": bool(row["IsOpen"]),
        "is_always_on_top": bool(row["IsAlwaysOnTop"]),
        "created_at": ticks_to_iso(row["CreatedAt"]),
        "updated_at": ticks_to_iso(row["UpdatedAt"]),
        "is_deleted": row["DeletedAt"] is not None,
        "preview": text.split("\n", 1)[0][:100] if text else "(Empty Note)",
    }


app = MCPServer(
    name="sticky-notes-readonly",
    version="0.1.0",
    description="Read-only access to Windows Microsoft Sticky Notes",
)


@app.tool(annotations=READ_ONLY)
def list_notes(
    limit: int = 20,
    offset: int = 0,
    theme: str | None = None,
    is_open: bool | None = None,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """List sticky notes with a one-line preview, newest updates first.

    Args:
        limit: Maximum notes to return (1-100, default 20).
        offset: Number of notes to skip for paging.
        theme: Filter by color (Yellow, Green, Pink, Purple, Blue, Grey, Charcoal).
        is_open: Filter by saved open/closed state.
        include_deleted: Include notes marked as deleted.
    """
    rows = fetch_notes(theme, is_open, include_deleted)
    start = max(0, int(offset))
    return [summarize(r, clean_text(r["Text"])) for r in rows[start:start + clamp_limit(limit)]]


@app.tool(annotations=READ_ONLY)
def get_note(note_id: str) -> dict[str, Any]:
    """Get the full text and details of one sticky note.

    Args:
        note_id: The note ID from list_notes or search_notes.
    """
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT Id, Text, Theme, IsOpen, IsAlwaysOnTop, CreatedAt, UpdatedAt, DeletedAt
            FROM Note WHERE Id = ?
            """,
            (note_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError(f"Note not found: {note_id}")
    text = clean_text(row["Text"])
    note = summarize(row, text)
    del note["preview"]
    note["text"] = text
    return note


@app.tool(annotations=READ_ONLY)
def search_notes(query: str, limit: int = 20, include_deleted: bool = False) -> list[dict[str, Any]]:
    """Search note text for a keyword (case-insensitive, plain substring match).

    Args:
        query: Text to look for.
        limit: Maximum notes to return (1-100, default 20).
        include_deleted: Include notes marked as deleted.
    """
    needle = query.strip().casefold()
    if not needle:
        raise ValueError("query must not be empty")
    results = []
    for row in fetch_notes(None, None, include_deleted):
        text = clean_text(row["Text"])
        if needle not in text.casefold():
            continue
        note = summarize(row, text)
        note["snippet"] = next((line for line in text.splitlines() if needle in line.casefold()), text)[:150]
        results.append(note)
        if len(results) >= clamp_limit(limit):
            break
    return results


def main() -> None:
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
