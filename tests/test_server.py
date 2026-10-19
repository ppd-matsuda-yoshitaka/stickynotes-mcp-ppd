"""Tests run against a temporary database; the real Sticky Notes database is never touched."""

import asyncio
import importlib
import sqlite3

import pytest

import server

NOTE_SCHEMA = """
CREATE TABLE "Note" (
    "Text" varchar, "WindowPosition" varchar, "IsOpen" integer, "IsAlwaysOnTop" integer,
    "CreationNoteIdAnchor" varchar, "Theme" varchar, "IsFutureNote" integer, "RemoteId" varchar,
    "ChangeKey" varchar, "LastServerVersion" varchar, "RemoteSchemaVersion" integer,
    "IsRemoteDataInvalid" integer, "PendingInsightsScan" integer, "Type" varchar,
    "Id" varchar primary key not null, "ParentId" varchar,
    "CreatedAt" bigint, "DeletedAt" bigint, "UpdatedAt" bigint
)
"""
USER_SCHEMA = """
CREATE TABLE "User" (
    "Type" varchar, "Id" varchar primary key not null, "ParentId" varchar,
    "CreatedAt" bigint, "DeletedAt" bigint, "UpdatedAt" bigint
)
"""

# Paragraph id prefixes as the app writes them: a space before plain text,
# no space before a control word.
ID = "\\id=11111111-2222-3333-4444-555555555555 "
ID_NOSPACE = "\\id=11111111-2222-3333-4444-555555555555"


@pytest.fixture
def db(tmp_path, monkeypatch):
    # "#" in the file name checks that the read-only URI escapes the path.
    path = tmp_path / "plum test #1.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(NOTE_SCHEMA)
    conn.execute(USER_SCHEMA)
    conn.execute("INSERT INTO User (Id) VALUES ('user-1')")
    conn.commit()

    def add(note_id, text, theme="Yellow", updated=1, deleted=None, is_open=1):
        conn.execute(
            "INSERT INTO Note (Id, Text, Theme, IsOpen, IsAlwaysOnTop, CreatedAt, UpdatedAt, DeletedAt)"
            " VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (note_id, text, theme, is_open, updated, updated, deleted),
        )
        conn.commit()

    add.path = path
    add.raw = lambda note_id: conn.execute("SELECT Text FROM Note WHERE Id = ?", (note_id,)).fetchone()[0]
    monkeypatch.setenv("STICKY_NOTES_DB_PATH", str(path))
    yield add
    conn.close()


@pytest.fixture
def writable(db, tmp_path, monkeypatch):
    monkeypatch.setenv(server.WRITE_ENV, "1")
    monkeypatch.setenv("STICKY_NOTES_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(server, "sticky_notes_running", lambda: False)
    return db


# --- Reading: stored format observed in notes made by the app ---

def test_clean_text_real_app_format():
    raw = "\n".join([
        ID + "Title",
        ID,
        ID_NOSPACE + "\\b bold\\b0 ",
        ID_NOSPACE + "\\i italic\\i0 ",
        ID_NOSPACE + "\\ul underline\\ul0 ",
        ID_NOSPACE + "\\strike gone\\strike0 ",
        ID,
        ID_NOSPACE + "\\l item",
        ID_NOSPACE + "\\l0 ",
    ])
    assert server.clean_text(raw) == "Title\n\n**bold**\n*italic*\nunderline\n~~gone~~\n\n- item"


def test_clean_text_mid_line_formatting():
    # The space after "\b0" is a delimiter; the next one is real text.
    assert server.clean_text(ID + "before \\b bold\\b0  after") == "before **bold** after"


def test_clean_text_unescapes_backslashes():
    raw = ID + "C:\\\\build\\\\index \\\\\\\\server\\\\share"
    assert server.clean_text(raw) == "C:\\build\\index \\\\server\\share"


def test_list_orders_filters_and_clamps(db):
    for i in range(150):
        db(f"n{i}", ID + f"note {i}", updated=i)
    db("trash", ID + "deleted", updated=999, deleted=999)
    db("green", ID + "green one", theme="Green", updated=1000, is_open=0)

    notes = server.list_notes(limit=1000)
    assert len(notes) == server.MAX_LIMIT
    assert notes[0]["id"] == "green"
    assert all(not n["is_deleted"] for n in notes)

    assert [n["id"] for n in server.list_notes(theme="green")] == ["green"]
    assert [n["id"] for n in server.list_notes(is_open=False)] == ["green"]
    assert server.list_notes(include_deleted=True)[1]["id"] == "trash"
    assert len(server.list_notes(limit=0)) == 1
    with pytest.raises(ValueError):
        server.list_notes(theme="Red")


def test_get_note(db):
    db("a", ID + "Title\n" + ID_NOSPACE + "\\l item")
    note = server.get_note("a")
    assert note["text"] == "Title\n- item"
    assert "text_raw" not in note
    with pytest.raises(ValueError):
        server.get_note("missing")


def test_search_ignores_markup_and_ids(db):
    db("a", ID + "Release checklist")
    db("b", ID + "Path C:\\\\build\\\\out")
    assert server.search_notes("id") == []
    assert server.search_notes("1111") == []
    assert [n["id"] for n in server.search_notes("CHECKLIST")] == ["a"]
    assert server.search_notes("checklist")[0]["snippet"] == "Release checklist"
    assert [n["id"] for n in server.search_notes("c:\\build")] == ["b"]
    # "%" must be matched literally, not as a SQL LIKE wildcard.
    assert server.search_notes("%") == []
    with pytest.raises(ValueError):
        server.search_notes("  ")


def test_database_is_opened_read_only(db):
    db("a", ID + "x")
    conn = server.connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM Note")
    finally:
        conn.close()


def test_missing_database(monkeypatch, tmp_path):
    monkeypatch.setenv("STICKY_NOTES_DB_PATH", str(tmp_path / "nope.sqlite"))
    with pytest.raises(FileNotFoundError):
        server.list_notes()


# --- Tool registration ---

def tool_map():
    return {t.name: t for t in asyncio.run(server.app.list_tools())}


def test_only_read_tools_by_default(monkeypatch):
    monkeypatch.delenv(server.WRITE_ENV, raising=False)
    importlib.reload(server)
    tools = tool_map()
    assert sorted(tools) == ["get_note", "list_notes", "search_notes"]
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True


def test_write_tools_registered_when_enabled(monkeypatch):
    monkeypatch.setenv(server.WRITE_ENV, "1")
    try:
        importlib.reload(server)
        tools = tool_map()
        assert sorted(tools) == ["append_to_note", "create_note", "get_note", "list_notes", "search_notes"]
        for name in ("create_note", "append_to_note"):
            assert tools[name].annotations.read_only_hint is False
    finally:
        monkeypatch.delenv(server.WRITE_ENV)
        importlib.reload(server)


# --- Writing ---

def test_write_refused_when_disabled(db, monkeypatch):
    monkeypatch.delenv(server.WRITE_ENV, raising=False)
    with pytest.raises(PermissionError):
        server.create_note("x")


def test_write_refused_while_app_running(writable, monkeypatch, tmp_path):
    writable("a", ID + "keep")
    monkeypatch.setattr(server, "sticky_notes_running", lambda: True)
    with pytest.raises(RuntimeError):
        server.append_to_note("a", "x")
    assert writable.raw("a") == ID + "keep"
    assert not (tmp_path / "backups").exists()


def test_write_aborts_when_backup_fails(writable, monkeypatch):
    writable("a", ID + "keep")

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(server, "make_backup", fail)
    with pytest.raises(OSError):
        server.append_to_note("a", "x")
    assert writable.raw("a") == ID + "keep"


def test_create_note_round_trip(writable, tmp_path):
    result = server.create_note("Title\nC:\\build\\index\n\nend", theme="pink", is_open=False)
    raw = writable.raw(result["id"])
    lines = raw.split("\n")
    assert len(lines) == 4
    assert all(server.RE_LINE_ID.match(line) for line in lines)
    assert "C:\\\\build\\\\index" in raw
    note = server.get_note(result["id"])
    assert note["text"] == "Title\nC:\\build\\index\n\nend"
    assert note["theme"] == "Pink"
    assert note["is_open"] is False
    assert note["created_at"] is not None
    assert any((tmp_path / "backups").glob("plum-*.sqlite"))
    assert sqlite3.connect(result["backup"]).execute("SELECT count(*) FROM User").fetchone()[0] == 1


def test_create_note_validates_input(writable):
    with pytest.raises(ValueError):
        server.create_note("   ")
    with pytest.raises(ValueError):
        server.create_note("x" * (server.MAX_WRITE_CHARS + 1))
    with pytest.raises(ValueError):
        server.create_note("x", theme="Red")


def test_append_keeps_existing_lines_exactly(writable):
    original = ID + "Title\n" + ID_NOSPACE + "\\b bold\\b0 "
    writable("a", original, updated=1)
    server.append_to_note("a", "new \\ line")
    raw = writable.raw("a")
    assert raw.startswith(original + "\n")
    assert server.get_note("a")["text"] == "Title\n**bold**\nnew \\ line"
    assert server.get_note("a")["updated_at"] != server.ticks_to_iso(1)


def test_append_after_list_closes_the_list(writable):
    writable("a", ID + "Title\n" + ID_NOSPACE + "\\l item")
    server.append_to_note("a", "after")
    last = writable.raw("a").split("\n")[-1]
    assert server.RE_LINE_ID.sub("", last) == "\\l0 after"
    assert server.get_note("a")["text"] == "Title\n- item\nafter"


def test_append_refuses_missing_or_deleted(writable):
    writable("gone", ID + "x", deleted=5)
    with pytest.raises(ValueError):
        server.append_to_note("missing", "x")
    with pytest.raises(ValueError):
        server.append_to_note("gone", "x")
    assert writable.raw("gone") == ID + "x"


def test_backups_are_pruned(writable, tmp_path):
    writable("a", ID + "x")
    for _ in range(server.KEEP_BACKUPS + 3):
        server.make_backup()
    assert len(list((tmp_path / "backups").glob("plum-*.sqlite"))) == server.KEEP_BACKUPS


@pytest.mark.parametrize("stdout, expected", [
    # "INFO: no tasks ..." in Japanese, encoded in CP932
    ("情報: 指定された条件に一致するタスクは実行されていません。\r\n".encode("cp932"), False),
    (b'"Microsoft.Notes.exe","36016","Console","1","161,568 K"\r\n', True),
])
def test_sticky_notes_running_parses_tasklist(monkeypatch, stdout, expected):
    class Result:
        pass

    result = Result()
    result.stdout = stdout
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: result)
    assert server.sticky_notes_running() is expected


def test_now_ticks_is_utc():
    from datetime import datetime, timezone
    iso = server.ticks_to_iso(server.now_ticks())
    delta = datetime.now(timezone.utc) - datetime.fromisoformat(iso)
    assert abs(delta.total_seconds()) < 5
