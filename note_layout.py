"""On-demand window layout. No database writes, background workers or app restarts."""

import json
import math
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

LAYOUT_ENV = "STICKY_NOTES_ENABLE_LAYOUT"


def canonical(text):
    # Only line endings and the provider's final paragraph terminator differ.
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def match_windows(notes, windows, universe=None):
    """Full body equality, unique in BOTH directions; never use titles or prefixes."""
    matches = {}
    for note in notes:
        body = canonical(note["body"])
        peers = [n for n in (universe if universe is not None else notes) if canonical(n["body"]) == body]
        candidates = [w for w in windows if canonical(w["body"]) == body]
        if not body.strip() or len(peers) != 1 or len(candidates) != 1:
            raise ValueError(f"Cannot uniquely identify visible note {note['id']}; no windows moved")
        matches[note["id"]] = candidates[0]
    if len({w["hwnd"] for w in matches.values()}) != len(matches):
        raise ValueError("Multiple notes map to one window; no windows moved")
    return matches


def inside(rect, work):
    x, y, w, h = rect
    l, t, r, b = work
    return w > 0 and h > 0 and l <= x and t <= y and x + w <= r and y + h <= b


def overlaps(a, b):
    x, y, w, h = a
    X, Y, W, H = b
    return x < X + W and X < x + w and y < Y + H and Y < y + h


def plan_layout(matches, monitors, width=360, height=240, gap=12, margin=16, monitor_device=None):
    """DIP inputs -> physical pixels. Keep monitors and right-column/top-down order."""
    for value, low, high in ((width, 200, 2000), (height, 160, 2000),
                             (gap, 0, 200), (margin, 0, 200)):
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("Invalid layout dimensions (width 200..2000, height 160..2000, gap/margin 0..200 DIP)")
    by_device = {m["device"]: m for m in monitors}
    if monitor_device is not None and monitor_device not in by_device:
        raise ValueError("Requested monitor is not connected")
    groups = {}
    for note_id, window in matches.items():
        device = monitor_device or window["device"]
        if device not in by_device:
            raise ValueError("Window monitor disappeared")
        groups.setdefault(device, []).append((note_id, window))
    result = []
    for device, items in groups.items():
        m = by_device[device]
        scale = m["dpi"] / 96
        px = lambda v: round(v * scale)
        l, t, r, b = m["work"]
        g, pad = px(gap), px(margin)
        minimum = lambda item, axis: math.ceil(item[1]["minimum"][axis] * m["dpi"] / by_device[item[1]["device"]]["dpi"])
        w = max(px(width), *(minimum(i, 0) for i in items))
        h = max(px(height), *(minimum(i, 1) for i in items))
        rows = (b - t - 2 * pad + g) // (h + g)
        cols = (r - l - 2 * pad + g) // (w + g)
        if rows < 1 or cols < 1 or rows * cols < len(items):
            raise ValueError("Notes do not fit the work area; choose smaller dimensions or another monitor")
        # Quantize horizontal position into existing columns, then preserve vertical order.
        items.sort(key=lambda i: (-round(i[1]["rect"][0] / max(1, w)), i[1]["rect"][1], i[0]))
        for index, (note_id, window) in enumerate(items):
            col, row = divmod(index, rows)
            rect = [r - pad - w - col * (w + g), t + pad + row * (h + g), w, h]
            result.append({"id": note_id, "rect": rect, "device": device})
    return result


class WindowsBackend:
    def call(self, request):
        if os.name != "nt":
            raise OSError("Layout requires Windows and the same interactive session as Sticky Notes")
        helper = Path(__file__).with_name("windows_layout.ps1")
        # A fixed script, no command interpolation or execution-policy overrides.
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(helper)],
            input=json.dumps(request, ensure_ascii=True).encode("utf-8"),
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise RuntimeError("Windows layout helper failed: " + result.stderr.decode("utf-8", errors="replace")[-3000:])
        return json.loads(result.stdout.decode("utf-8-sig"))

    def scan(self):
        return self.call({"action": "scan"})

    def move(self, moves, monitors):
        return self.call({"action": "move", "moves": moves, "monitors": monitors})


def snapshot_dir():
    configured = os.getenv("STICKY_NOTES_LAYOUT_DIR")
    return Path(configured) if configured else Path(os.environ["LOCALAPPDATA"]) / "sticky-notes-mcp-ppd" / "layouts"


def save_snapshot(matches, monitors):
    directory = snapshot_dir()
    directory.mkdir(parents=True, exist_ok=True)
    snapshot_id = str(uuid.uuid4())
    data = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "monitors": monitors,
            "notes": [{"id": k, "rect": v["rect"], "device": v["device"],
                       "hwnd": v["hwnd"], "identity": v["identity"], "frame": v.get("frame")}
                      for k, v in matches.items()]}
    # Exclusive creation; no automatic pruning of manual layouts.
    with (directory / f"{snapshot_id}.json").open("x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    return snapshot_id


def load_snapshot(snapshot_id):
    # Resolve only opaque IDs in our dedicated directory, never arbitrary paths.
    snapshot_id = str(uuid.UUID(snapshot_id))
    data = json.loads((snapshot_dir() / f"{snapshot_id}.json").read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError("Unsupported layout snapshot version")
    return data


def known_frame_overflow(target, window, monitor):
    """Restore only recorded invisible borders, never clamp or allow lost monitors.

    Requires the same live window instance, same monitor, unchanged border insets,
    a saved visible frame wholly within the work area, and <=16 DIP outer overflow.
    Legacy snapshots lacking identity/frame evidence keep the strict inside rule.
    """
    if (target.get("hwnd") != window["hwnd"] or target.get("identity") != window["identity"]
            or target["device"] != window["device"]):
        return False
    frame, current = target.get("frame"), window.get("frame")
    if not frame or not current or not inside(frame, monitor["work"]):
        return False
    def borders(rect, visible):
        x, y, w, h = rect
        X, Y, W, H = visible
        return [X-x, Y-y, x+w-X-W, y+h-Y-H]
    saved_borders = borders(target["rect"], frame)
    limit = math.ceil(16 * monitor["dpi"] / 96)
    if any(v < 0 or v > limit for v in saved_borders):
        return False
    if saved_borders != borders(window["rect"], current):
        return False
    x, y, w, h = target["rect"]
    l, t, r, b = monitor["work"]
    return max(l-x, t-y, x+w-r, y+h-b) <= limit


def validate_targets(matches, monitors, targets):
    # Validate ALL before taking a snapshot or moving any window.
    by_device = {m["device"]: m for m in monitors}
    if not targets or len({t["id"] for t in targets}) != len(targets):
        raise ValueError("Empty or duplicate layout targets")
    for target in targets:
        window = matches[target["id"]]
        rect = target["rect"]
        if len(rect) != 4 or any(type(v) is not int for v in rect):
            raise ValueError("Invalid snapshot rectangle")
        monitor = by_device.get(target["device"])
        if monitor is None or (not inside(rect, monitor["work"]) and not known_frame_overflow(target, window, monitor)):
            raise ValueError("Saved monitor/work area changed; preview a fresh arrangement")
        ratio = monitor["dpi"] / by_device[window["device"]]["dpi"]
        if rect[2] < math.ceil(window["minimum"][0] * ratio) or rect[3] < math.ceil(window["minimum"][1] * ratio):
            raise ValueError("Target is smaller than the app minimum")


def apply_layout(backend, matches, monitors, targets):
    if os.getenv(LAYOUT_ENV) != "1":
        raise PermissionError(f"Set {LAYOUT_ENV}=1 to enable layout changes")
    validate_targets(matches, monitors, targets)
    # Do not move away from a layout that cannot safely be restored.
    original = [{"id": key, **{k: window[k] for k in ("rect", "device", "hwnd", "identity")},
                 "frame": window.get("frame")} for key, window in matches.items()]
    validate_targets(matches, monitors, original)
    # Manual snapshots may intentionally contain overlap. Restore it exactly.
    backup = save_snapshot(matches, monitors)
    moves = [{"expected": matches[t["id"]], "rect": t["rect"]} for t in targets]
    try:
        result = backend.move(moves, monitors)
    except Exception as exc:
        raise RuntimeError(f"Layout failed; recovery snapshot_id={backup}. {exc}") from exc
    return {"snapshot_id": backup, "notes": targets, "verification": result}
