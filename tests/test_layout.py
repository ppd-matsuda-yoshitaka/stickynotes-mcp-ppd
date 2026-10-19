"""Synthetic geometry and fake live windows; never operates on the desktop."""
import asyncio
import copy
import importlib
import json
import sqlite3

import pytest

import note_layout as layout
import server


def monitor(device="right", work=(0, 0, 1920, 1040), dpi=96):
    return {"device": device, "work": list(work), "dpi": dpi}


def window(index, body=None, device="right", rect=None, minimum=(200, 160)):
    rect = rect or [1450, index * 100, 360, 280]
    return {"hwnd": index + 10, "identity": f"pid:started:runtime:{index}",
            "body": body if body is not None else f"Note {index}", "device": device,
            "rect": rect, "frame": rect.copy(), "minimum": list(minimum)}


def assert_separated(targets, monitors):
    for index, target in enumerate(targets):
        m = next(m for m in monitors if m["device"] == target["device"])
        assert layout.inside(target["rect"], m["work"])
        assert all(not layout.overlaps(target["rect"], t["rect"]) for t in targets[:index])


def test_right_columns_and_original_vertical_order():
    matches = {str(i): window(i) for i in reversed(range(5))}
    monitors = [monitor()]
    targets = layout.plan_layout(matches, monitors)
    assert [t["id"] for t in targets] == [str(i) for i in range(5)]
    assert targets[0]["rect"] == [1544, 16, 360, 240]
    assert targets[1]["rect"][1] == 268
    assert targets[4]["rect"][0] == 1172
    assert_separated(targets, monitors)


@pytest.mark.parametrize("dpi", [96, 120, 144, 192])
def test_dpi_negative_monitor_and_taskbar(dpi):
    monitors = [monitor("left", (-3000, -100, 0, 1900), dpi)]
    matches = {str(i): window(i, device="left", rect=[-600, i*100, 360, 280]) for i in range(5)}
    targets = layout.plan_layout(matches, monitors)
    assert targets[0]["rect"][2] == round(360 * dpi / 96)
    assert_separated(targets, monitors)


def test_multi_monitor_keeps_each_monitor_and_can_target_one():
    monitors = [monitor(), monitor("left", (-1920, 0, 0, 1040), 144)]
    matches = {"a": window(0), "b": window(1, device="left")}
    assert {t["device"] for t in layout.plan_layout(matches, monitors)} == {"left", "right"}
    targets = layout.plan_layout(matches, monitors, monitor_device="right")
    assert all(t["device"] == "right" for t in targets)
    assert_separated(targets, monitors)


def test_app_minimum_is_respected_including_cross_dpi():
    monitors = [monitor(), monitor("left", (-3000, 0, 0, 2000), 192)]
    matches = {"a": window(0, minimum=(400, 300))}
    targets = layout.plan_layout(matches, monitors, monitor_device="left")
    assert targets[0]["rect"][2:] == [800, 600]


@pytest.mark.parametrize("kwargs", [{"width": 0}, {"height": -1}, {"gap": -1},
                                          {"margin": 201}, {"width": float("nan")},
                                          {"monitor_device": "missing"}])
def test_invalid_dimensions(kwargs):
    with pytest.raises(ValueError):
        layout.plan_layout({"a": window(0)}, [monitor()], **kwargs)


def test_insufficient_space_aborts_instead_of_overlap():
    with pytest.raises(ValueError, match="do not fit"):
        layout.plan_layout({str(i): window(i) for i in range(10)}, [monitor(work=(0,0,500,500))])


def test_exact_matching_not_prefix_and_provider_newlines():
    notes = [{"id": "a", "body": "日本語\nFull body"}]
    assert layout.match_windows(notes, [window(0, "日本語\r\nFull body\r\n")])["a"]["hwnd"] == 10
    with pytest.raises(ValueError):
        layout.match_windows(notes, [window(0, "日本語")])


@pytest.mark.parametrize("notes,windows", [
    ([{"id":"a","body":"same"},{"id":"b","body":"same"}], [window(0,"same")]),
    ([{"id":"a","body":"same"}], [window(0,"same"),window(1,"same")]),
    ([{"id":"a","body":""}], [window(0,"")]),
    ([{"id":"a","body":"unsaved"}], [window(0,"saved")]),
])
def test_ambiguous_empty_unsaved_text_never_maps(notes, windows):
    with pytest.raises(ValueError):
        layout.match_windows(notes, windows)


def test_duplicate_closed_note_also_prevents_mapping():
    note = {"id":"a","body":"same"}
    with pytest.raises(ValueError):
        layout.match_windows([note], [window(0,"same")], [note,{"id":"closed","body":"same"}])


class FakeBackend:
    def __init__(self):
        self.state = {"monitors": [monitor()], "windows": [window(0), window(1)]}
        self.calls = []

    def scan(self):
        return copy.deepcopy(self.state)

    def move(self, moves, monitors):
        self.calls.append((moves, monitors))
        for move in moves:
            w = next(w for w in self.state["windows"] if w["hwnd"] == move["expected"]["hwnd"])
            old, frame = w["rect"], w["frame"]
            dx, dy = move["rect"][0]-old[0], move["rect"][1]-old[1]
            dw, dh = move["rect"][2]-old[2], move["rect"][3]-old[3]
            w["frame"] = [frame[0]+dx, frame[1]+dy, frame[2]+dw, frame[3]+dh]
            w["rect"] = move["rect"]
        return {"verified": True, "count": len(moves)}


@pytest.fixture
def live(tmp_path, monkeypatch):
    backend = FakeBackend()
    path = tmp_path / "synthetic.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE Note(Id, Text, Theme, IsOpen, IsAlwaysOnTop, CreatedAt, UpdatedAt, DeletedAt, WindowPosition)")
    for i in range(2):
        conn.execute("INSERT INTO Note VALUES(?,?,?,1,0,1,1,NULL,?)", (str(i), f"\\id=11111111-2222-3333-4444-555555555555 Note {i}", "Pink", "untouched"))
    conn.commit()
    conn.close()
    monkeypatch.setenv("STICKY_NOTES_DB_PATH", str(path))
    monkeypatch.setenv(layout.LAYOUT_ENV, "1")
    monkeypatch.setenv("STICKY_NOTES_LAYOUT_DIR", str(tmp_path / "layouts"))
    monkeypatch.setattr(layout, "WindowsBackend", lambda: backend)
    return backend, path, tmp_path / "layouts"


def test_preview_does_not_save_or_move(live):
    backend, path, directory = live
    before = path.read_bytes()
    result = server.arrange_notes()
    assert result["dry_run"] is True
    assert backend.calls == [] and not directory.exists()
    assert path.read_bytes() == before


def test_save_apply_restore_and_db_unchanged(live):
    backend, path, directory = live
    before_db, before_windows = path.read_bytes(), backend.scan()
    saved = server.save_note_layout()
    result = server.arrange_notes(dry_run=False)
    assert result["verification"]["verified"]
    assert (directory / f"{result['snapshot_id']}.json").exists()
    # The automatic pre-apply snapshot is exactly the existing manual layout.
    auto = layout.load_snapshot(result["snapshot_id"])
    assert [n["rect"] for n in auto["notes"]] == [w["rect"] for w in before_windows["windows"]]
    assert "body" not in json.dumps(auto) and auto["notes"][0]["identity"]
    server.restore_note_layout(saved["snapshot_id"], dry_run=False)
    assert backend.scan() == before_windows
    assert path.read_bytes() == before_db


def test_snapshot_failure_prevents_any_move(live, monkeypatch):
    backend, _, _ = live
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(layout, "save_snapshot", fail)
    with pytest.raises(OSError):
        server.arrange_notes(dry_run=False)
    assert backend.calls == []


def test_backend_failure_reports_recovery_id(live, monkeypatch):
    backend, _, directory = live
    def fail(*args):
        raise RuntimeError("identity changed; no windows moved")
    monkeypatch.setattr(backend, "move", fail)
    with pytest.raises(RuntimeError, match="recovery snapshot_id="):
        server.arrange_notes(dry_run=False)
    assert len(list(directory.glob("*.json"))) == 1


def test_restore_rejects_topology_change_even_in_preview(live):
    backend, _, _ = live
    saved = server.save_note_layout()
    backend.state["monitors"][0]["dpi"] = 144
    with pytest.raises(ValueError, match="DPI changed"):
        server.restore_note_layout(saved["snapshot_id"])
    assert backend.calls == []


def test_restore_refuses_offscreen_saved_layout(live):
    backend, _, _ = live
    backend.state["windows"][0]["rect"][0] = -900
    saved = server.save_note_layout()
    with pytest.raises(ValueError, match="work area changed"):
        server.restore_note_layout(saved["snapshot_id"])
    assert backend.calls == []


def test_unselected_notes_cannot_be_overlapped(live):
    backend, _, _ = live
    backend.state["windows"][1]["rect"] = [1544,16,360,280]
    with pytest.raises(ValueError, match="overlap"):
        server.arrange_notes(note_ids=["0"], dry_run=False)
    assert backend.calls == []


def test_missing_ambiguous_note_prevents_all_moves(live):
    backend, _, _ = live
    backend.state["windows"][1]["body"] = "changed since database save"
    with pytest.raises(ValueError, match="uniquely"):
        server.arrange_notes(dry_run=False)
    assert backend.calls == []


def test_layout_requires_separate_enable(live, monkeypatch):
    backend, _, _ = live
    monkeypatch.delenv(layout.LAYOUT_ENV)
    with pytest.raises(PermissionError):
        server.arrange_notes(dry_run=False)
    assert backend.calls == []


def test_snapshot_path_is_opaque(live):
    with pytest.raises(ValueError):
        layout.load_snapshot("../something")


def test_layout_tools_registered_without_db_write_tools(monkeypatch):
    monkeypatch.setenv(layout.LAYOUT_ENV,"1")
    monkeypatch.delenv(server.WRITE_ENV, raising=False)
    try:
        importlib.reload(server)
        tools = {t.name:t for t in asyncio.run(server.app.list_tools())}
        assert set(tools) == {"list_notes","get_note","search_notes","inspect_note_layout", "save_note_layout","arrange_notes","restore_note_layout"}
        assert tools["inspect_note_layout"].annotations.read_only_hint is True
        assert tools["arrange_notes"].annotations.read_only_hint is False
    finally:
        monkeypatch.delenv(layout.LAYOUT_ENV)
        importlib.reload(server)


def border_window():
    w = window(0, rect=[1566, 16, 360, 280])
    # Outer right edge 1926; DWM visible right edge 1918. Eight-pixel borders.
    w["frame"] = [1574, 16, 344, 272]
    return w


def saved_target(w):
    return {"id": "0", **{k:w[k] for k in ("hwnd", "identity", "rect", "frame", "device")}}


def test_recorded_invisible_overflow_restores_exact_original_after_move(live):
    backend, _, _ = live
    backend.state["windows"][0] = border_window()
    original = backend.scan()
    saved = server.save_note_layout()
    server.arrange_notes(dry_run=False)
    preview = server.restore_note_layout(saved["snapshot_id"])
    assert preview["notes"][0]["rect"] == original["windows"][0]["rect"]
    server.restore_note_layout(saved["snapshot_id"], dry_run=False)
    assert backend.scan() == original


@pytest.mark.parametrize("changed", ["handle", "identity", "monitor", "borders", "visible_offscreen", "large_border", "legacy"])
def test_overflow_restore_requires_complete_matching_evidence(changed):
    w = border_window()
    target = copy.deepcopy(saved_target(w))
    if changed == "handle":
        w["hwnd"] += 1
    elif changed == "identity":
        w["identity"] += ":new"
    elif changed == "monitor":
        w["device"] = "other"
    elif changed == "borders":
        w["frame"][0] += 1
    elif changed == "visible_offscreen":
        target["frame"][2] += 8
    elif changed == "large_border":
        target["rect"][2] += 100
    elif changed == "legacy":
        target.pop("identity")
    assert not layout.known_frame_overflow(target, w, monitor())


def test_unknown_overflow_prevents_arrangement_before_any_move(live):
    backend, _, directory = live
    backend.state["windows"][0]["rect"][0] = 1800
    backend.state["windows"][0]["frame"][0] = 1800
    with pytest.raises(ValueError, match="work area changed"):
        server.arrange_notes(dry_run=False)
    assert backend.calls == [] and not directory.exists()


def test_disconnected_monitor_restore_refused_without_move(live):
    backend, _, _ = live
    saved = server.save_note_layout()
    backend.state["monitors"][0]["device"] = "replacement"
    backend.state["windows"][0]["device"] = "replacement"
    backend.state["windows"][1]["device"] = "replacement"
    with pytest.raises(ValueError, match="DPI changed"):
        server.restore_note_layout(saved["snapshot_id"], dry_run=False)
    assert backend.calls == []


def bordered(index, rect):
    # Geometry observed from the real app at 96 DPI: 7 px invisible left/right/bottom borders.
    w = window(index, rect=rect)
    w["frame"] = [rect[0] + 7, rect[1], rect[2] - 14, rect[3] - 7]
    return w


def visible(target):
    x, y, w, h = target["rect"]
    return [x + 7, y, w - 14, h - 7]


def test_margins_and_gaps_apply_to_visible_frame():
    monitors = [monitor()]
    matches = {str(i): bordered(i, [1450, i * 100, 360, 240]) for i in range(5)}
    targets = layout.plan_layout(matches, monitors)
    assert_separated(targets, monitors)
    first, second, next_col = (visible(t) for t in (targets[0], targets[1], targets[4]))
    assert 1920 - (first[0] + first[2]) == 16 and first[1] == 16
    assert second[1] - (first[1] + first[3]) == 12
    # Borders (7+7) exceed the gap, so outer rectangles touch rather than overlap.
    assert first[0] - (next_col[0] + next_col[2]) == 14


def test_zero_margin_keeps_outer_rect_inside_work_area():
    monitors = [monitor()]
    targets = layout.plan_layout({"0": bordered(0, [1450, 0, 360, 240])}, monitors, margin=0)
    assert targets[0]["rect"][0] + 360 == 1920
    assert_separated(targets, monitors)


def test_unrestorable_layout_requires_opt_in(live):
    backend, _, _ = live
    backend.state["windows"][0]["rect"][0] = 1800
    backend.state["windows"][0]["frame"][0] = 1800
    assert server.arrange_notes()["unrestorable"] == ["0"]
    with pytest.raises(ValueError, match="allow_unrestorable"):
        server.arrange_notes(dry_run=False)
    assert backend.calls == []
    result = server.arrange_notes(dry_run=False, allow_unrestorable=True)
    assert result["unrestorable"] == ["0"] and len(backend.calls) == 1
    # The backup records the offscreen geometry, but restoring it is still refused.
    with pytest.raises(ValueError, match="work area changed"):
        server.restore_note_layout(result["snapshot_id"])
