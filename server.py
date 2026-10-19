"""MCP server for Windows Microsoft Sticky Notes (plum.sqlite).

Read tools (list / get / search) are always available and open the database
read-only. Write tools (create / append) are registered only when
STICKY_NOTES_ENABLE_WRITE=1. They never change or remove existing lines.
Before every write they also refuse to run while the Sticky Notes app is
open, and they take a consistent backup of the database first.
"""

import os
import re
import sqlite3
import subprocess
import urllib.parse
import uuid
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

# Stored text format, confirmed against notes made by the Sticky Notes app:
#   - one paragraph per LF-separated line, each starting with "\id=<uuid>"
#   - RTF-like control words: \b \i \ul \strike (closed by \b0 etc.), and
#     \l / \l0 to start / end a bulleted list
#   - one space after a control word (or after the id) is a delimiter, not text
#   - a literal backslash in user text is stored as "\\"
#   e.g. "\id=<uuid> before \b bold\b0  after" -> "before **bold** after"
RE_LINE_ID = re.compile(r"^\\id=[0-9a-f-]+ ?", re.IGNORECASE)
RE_LIST_START = re.compile(r"^\\l(?![A-Za-z0-9-])")
RE_TOKEN = re.compile(r"\\([A-Za-z]+)(-?\d+)? ?|\\(.)", re.DOTALL)
FORMAT_TO_MARKDOWN = {"b": "**", "i": "*", "strike": "~~"}

WRITE_ENV = "STICKY_NOTES_ENABLE_WRITE"
STICKY_NOTES_PROCESS = "Microsoft.Notes.exe"
MAX_WRITE_CHARS = 10_000
KEEP_BACKUPS = 20

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

# Adds data without changing or removing anything already stored.
ADDITIVE_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
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


def render_line(body: str) -> str:
    """Convert one stored line (without its paragraph id) into Markdown."""
    def replace(m: re.Match) -> str:
        word, param, literal = m.groups()
        if literal is not None:
            return literal
        if word == "l":
            return "- " if param is None and m.start() == 0 else ""
        # Unknown words such as \ul are dropped; Markdown has no equivalent.
        return FORMAT_TO_MARKDOWN.get(word, "")

    return RE_TOKEN.sub(replace, body)


def clean_text(raw_text: str | None) -> str:
    """Convert Sticky Notes markup into readable Markdown."""
    if not raw_text:
        return ""
    lines = [render_line(RE_LINE_ID.sub("", line)) for line in raw_text.split("\n")]
    return "\n".join(lines).strip()


def to_stored_lines(text: str) -> list[str]:
    """Convert plain text into stored lines, each with a new paragraph id."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").split("\n")
    return [f"\\id={uuid.uuid4()} {line.replace(chr(92), chr(92) * 2)}" for line in lines]


def now_ticks() -> int:
    """Current UTC time as .NET DateTime.Ticks."""
    delta = datetime.now(timezone.utc) - TICKS_EPOCH
    return (delta.days * 86_400 + delta.seconds) * 10_000_000 + delta.microseconds * 10


def clamp_limit(limit: int) -> int:
    """Keep a requested result count within 1..MAX_LIMIT."""
    return max(1, min(int(limit), MAX_LIMIT))


def normalize_theme(theme: str) -> str:
    """Return the stored theme name, or raise for an unknown color."""
    normalized = theme.strip().capitalize()
    if normalized not in VALID_THEMES:
        raise ValueError(f"Unknown theme: {theme}. Valid: {', '.join(sorted(VALID_THEMES))}")
    return normalized


def connect() -> sqlite3.Connection:
    """Open the database read-only.

    mode=ro makes SQLite refuse writes, and query_only guards the connection
    again in case a write statement is ever added by mistake.
    """
    db_path = get_db_path()
    if not db_path.is_file():
        raise FileNotFoundError("Sticky Notes database not found. Set STICKY_NOTES_DB_PATH if it is elsewhere.")
    uri = f"file:{urllib.parse.quote(db_path.as_posix())}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def write_enabled() -> bool:
    return os.getenv(WRITE_ENV) == "1"


def sticky_notes_running() -> bool:
    """Return True if the Sticky Notes app process is running."""
    # Read raw bytes: tasklist prints localized messages in the console code
    # page (e.g. CP932), which fails to decode in UTF-8 mode. The process
    # name itself is ASCII, so a lossy decode is enough.
    result = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {STICKY_NOTES_PROCESS}", "/NH", "/FO", "CSV"],
        capture_output=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = result.stdout.decode("ascii", errors="replace").lower()
    return STICKY_NOTES_PROCESS.lower() in output


def get_backup_dir() -> Path:
    env_path = os.getenv("STICKY_NOTES_BACKUP_DIR")
    if env_path:
        return Path(env_path)
    return Path(os.path.expandvars(r"%LOCALAPPDATA%\sticky-notes-mcp-ppd\backups"))


def make_backup() -> Path:
    """Copy the database (including WAL contents) with SQLite's backup API.

    Older backups beyond KEEP_BACKUPS are removed; only files this server
    created are touched.
    """
    backup_dir = get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"plum-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite"
    src = connect()
    try:
        dest = sqlite3.connect(target)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    for old in sorted(backup_dir.glob("plum-*.sqlite"))[:-KEEP_BACKUPS]:
        old.unlink()
    return target


def open_for_write() -> tuple[sqlite3.Connection, Path]:
    """Run every write precondition, then open a connection holding the write lock."""
    if not write_enabled():
        raise PermissionError(f"Writing is disabled. Set {WRITE_ENV}=1 to enable it.")
    if sticky_notes_running():
        raise RuntimeError("Close the Sticky Notes app before writing, then try again.")
    backup = make_backup()
    conn = sqlite3.connect(get_db_path(), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    return conn, backup


def check_write_text(text: str) -> None:
    if not text.strip():
        raise ValueError("text must not be empty")
    if len(text) > MAX_WRITE_CHARS:
        raise ValueError(f"text is too long (max {MAX_WRITE_CHARS} characters)")


def fetch_notes(theme: str | None, is_open: bool | None, include_deleted: bool) -> list[sqlite3.Row]:
    """Load matching notes, newest updates first."""
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
    """Build the metadata and one-line preview returned for a note."""
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
    name="sticky-notes-ppd",
    version="0.2.0",
    description="Access to Windows Microsoft Sticky Notes (read; optional create/append)",
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


def create_note(text: str, theme: str = "Yellow", is_open: bool = True) -> dict[str, Any]:
    """Create a new sticky note from plain text.

    Formatting is not interpreted; the text is stored exactly as written.
    The Sticky Notes app must be closed. A backup is taken before writing.

    Args:
        text: Note content. Multiple lines are allowed (max 10,000 characters).
        theme: Color (Yellow, Green, Pink, Purple, Blue, Grey, Charcoal). Default Yellow.
        is_open: Show the note on the desktop the next time the app starts.
    """
    check_write_text(text)
    theme = normalize_theme(theme)
    conn, backup = open_for_write()
    try:
        user = conn.execute("SELECT Id FROM User LIMIT 1").fetchone()
        if not user:
            raise RuntimeError("No Sticky Notes user found. Open the app and create a note once first.")
        note_id = str(uuid.uuid4())
        ticks = now_ticks()
        conn.execute(
            """
            INSERT INTO Note (
                Id, Text, WindowPosition, IsOpen, IsAlwaysOnTop, Theme,
                IsFutureNote, PendingInsightsScan, ParentId, CreatedAt, UpdatedAt
            ) VALUES (?, ?, ?, ?, 0, ?, 0, 0, ?, ?, ?)
            """,
            (note_id, "\n".join(to_stored_lines(text)), "ManagedPosition=",
             1 if is_open else 0, theme, user["Id"], ticks, ticks),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return {"id": note_id, "theme": theme, "created_at": ticks_to_iso(ticks), "backup": str(backup)}


def append_to_note(note_id: str, text: str) -> dict[str, Any]:
    """Add lines to the end of an existing sticky note.

    Existing lines are kept exactly as stored, including their formatting.
    The new text is stored as plain text. The Sticky Notes app must be closed.
    A backup is taken before writing.

    Args:
        note_id: The note ID from list_notes or search_notes.
        text: Lines to add (max 10,000 characters).
    """
    check_write_text(text)
    conn, backup = open_for_write()
    try:
        row = conn.execute("SELECT Text, DeletedAt FROM Note WHERE Id = ?", (note_id,)).fetchone()
        if not row:
            raise ValueError(f"Note not found: {note_id}")
        if row["DeletedAt"] is not None:
            raise ValueError(f"Note is deleted: {note_id}")
        existing = row["Text"].split("\n") if row["Text"] else []
        new_lines = to_stored_lines(text)
        # The app ends a bulleted list with "\l0" on the following line; do the
        # same so the appended text does not become part of the list.
        if existing and RE_LIST_START.match(RE_LINE_ID.sub("", existing[-1])):
            line_id, body = new_lines[0].split(" ", 1)
            new_lines[0] = f"{line_id}\\l0 {body}"
        ticks = now_ticks()
        conn.execute(
            "UPDATE Note SET Text = ?, UpdatedAt = ? WHERE Id = ?",
            ("\n".join(existing + new_lines), ticks, note_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return {"id": note_id, "updated_at": ticks_to_iso(ticks), "backup": str(backup)}


if write_enabled():
    app.add_tool(create_note, annotations=ADDITIVE_WRITE)
    app.add_tool(append_to_note, annotations=ADDITIVE_WRITE)


def main() -> None:
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
