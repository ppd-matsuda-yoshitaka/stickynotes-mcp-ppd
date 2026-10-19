"""Tests run against a temporary database; the real Sticky Notes database is never touched."""

import asyncio
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

ID = "\\id=11111111-2222-3333-4444-555555555555 "


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "plum test #1.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(NOTE_SCHEMA)

    def add(note_id, text, theme="Yellow", updated=1, deleted=None, is_open=1):
        conn.execute(
            "INSERT INTO Note (Id, Text, Theme, IsOpen, IsAlwaysOnTop, CreatedAt, UpdatedAt, DeletedAt)"
            " VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (note_id, text, theme, is_open, updated, updated, deleted),
        )
        conn.commit()

    monkeypatch.setenv("STICKY_NOTES_DB_PATH", str(path))
    yield add
    conn.close()


def test_clean_text_formatting():
    raw = "\n".join([
        ID + "\\b Bold\\b0",
        ID + "\\l Item 1",
        ID + "\\strike Gone\\strike0 and \\i it\\i0",
    ])
    assert server.clean_text(raw) == "**Bold**\n- Item 1\n~~Gone~~ and *it*"


def test_clean_text_keeps_backslashes_in_user_text():
    raw = ID + "C:\\build\\index\\items\\lib \\\\server\\share"
    assert server.clean_text(raw) == "C:\\build\\index\\items\\lib \\\\server\\share"


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
    db("a", ID + "Title\n" + ID + "\\l item")
    note = server.get_note("a")
    assert note["text"] == "Title\n- item"
    assert "text_raw" not in note
    with pytest.raises(ValueError):
        server.get_note("missing")


def test_search_ignores_markup_and_ids(db):
    db("a", ID + "Release checklist")
    db("b", ID + "Path C:\\build\\out")
    assert server.search_notes("id") == []
    assert server.search_notes("1111") == []
    assert [n["id"] for n in server.search_notes("CHECKLIST")] == ["a"]
    assert server.search_notes("checklist")[0]["snippet"] == "Release checklist"
    assert [n["id"] for n in server.search_notes("c:\\build")] == ["b"]
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


def test_only_read_only_tools_exposed():
    tools = asyncio.run(server.app.list_tools())
    assert sorted(t.name for t in tools) == ["get_note", "list_notes", "search_notes"]
    for tool in tools:
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
