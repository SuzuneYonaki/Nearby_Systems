# -*- coding: utf-8 -*-
"""sphere_tour.py: Read-only FSDJump monitor and sphere nearest-first tour manager."""
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import nearby_systems as ns


def _extract_xyz(entry: Dict) -> Optional[Tuple[float, float, float]]:
    """Extract (x, y, z) float tuple from numeric keys or '(x, y, z)' string."""
    if "x" in entry and "y" in entry and "z" in entry:
        try:
            return (float(entry["x"]), float(entry["y"]), float(entry["z"]))
        except (TypeError, ValueError):
            pass
    coords = entry.get("coords")
    if isinstance(coords, dict) and "x" in coords and "y" in coords and "z" in coords:
        try:
            return (float(coords["x"]), float(coords["y"]), float(coords["z"]))
        except (TypeError, ValueError):
            pass
    if isinstance(coords, str):
        s = coords.strip().strip("()")
        parts = [p.strip() for p in s.split(",")]
        if len(parts) == 3:
            try:
                return (float(parts[0]), float(parts[1]), float(parts[2]))
            except ValueError:
                pass
    return None


class FSDJumpMonitor:
    """Read-only tail monitor for Elite Dangerous Journal.*.log FSDJump events.

    Emits each FSDJump event at most once (deduplicated by timestamp + StarSystem).
    """

    def __init__(self, journal_dir: Optional[Path] = None):
        self.journal_dir = Path(journal_dir) if journal_dir else Path(ns.JOURNAL_DIR)
        self._current_file: Optional[Path] = None
        self._file_offset: int = 0
        self._last_emitted_token: Optional[str] = None

    def _latest_journal_file(self) -> Optional[Path]:
        if not self.journal_dir.exists():
            return None
        files = sorted(self.journal_dir.glob("Journal.*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0] if files else None

    def arm(self) -> None:
        """Snap to the current end of the latest journal file so prior jumps do not fire."""
        latest = self._latest_journal_file()
        self._current_file = latest
        self._file_offset = latest.stat().st_size if latest and latest.exists() else 0
        self._last_emitted_token = None

    def poll_next_jump(self) -> Optional[Dict]:
        """Read newly appended lines (read-only) and return the newest unseen FSDJump event."""
        latest = self._latest_journal_file()
        if latest is None or not latest.exists():
            return None

        if self._current_file is None or latest != self._current_file:
            self._current_file = latest
            self._file_offset = 0

        size = latest.stat().st_size
        if size < self._file_offset:
            self._file_offset = 0

        newest_jump: Optional[Dict] = None
        with open(latest, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(self._file_offset)
            for line in f:
                s = line.strip()
                if not s or '"FSDJump"' not in s:
                    continue
                try:
                    ev = json.loads(s)
                except Exception:
                    continue
                if ev.get("event") != "FSDJump":
                    continue
                sys_name = ev.get("StarSystem")
                if not sys_name:
                    continue
                ts = str(ev.get("timestamp") or "")
                token = f"{ts}|{sys_name}"
                pos_raw = ev.get("StarPos")
                pos = (
                    (float(pos_raw[0]), float(pos_raw[1]), float(pos_raw[2]))
                    if isinstance(pos_raw, list) and len(pos_raw) == 3
                    else None
                )
                newest_jump = {
                    "system": str(sys_name),
                    "pos": pos,
                    "timestamp": ts,
                    "token": token,
                }
            self._file_offset = f.tell()

        if newest_jump is None:
            return None
        if newest_jump["token"] == self._last_emitted_token:
            return None

        self._last_emitted_token = newest_jump["token"]
        return newest_jump


class SphereTourManager:
    """Manages nearest-first traversal of systems inside a sphere centered at origin."""

    def __init__(self):
        self.origin: Optional[Tuple[float, float, float]] = None
        self.radius: float = 15.0
        self.state: str = "IDLE"  # IDLE | ARMED_WAITING_JUMP | ACTIVE | COMPLETED
        self.queue: List[Dict] = []
        self.visited: Set[str] = set()
        self.current_target: Optional[Dict] = None
        self.start_system: Optional[str] = None

    def arm_tour(
        self,
        origin: Tuple[float, float, float],
        systems: Sequence[Dict],
        radius: float = 15.0,
    ) -> int:
        """Build sorted sphere queue around origin and enter ARMED_WAITING_JUMP state."""
        self.origin = (float(origin[0]), float(origin[1]), float(origin[2]))
        self.radius = max(0.1, float(radius))
        self.visited.clear()
        self.current_target = None
        self.start_system = None

        seen: Set[str] = set()
        items: List[Dict] = []
        for s in systems:
            name = str(s.get("name") or "").strip()
            if not name or name.lower() in seen:
                continue
            xyz = _extract_xyz(s)
            if xyz is None:
                continue
            dist = math.dist(self.origin, xyz)
            if dist > self.radius:
                continue
            seen.add(name.lower())
            items.append({
                "name": name,
                "x": xyz[0],
                "y": xyz[1],
                "z": xyz[2],
                "distance": round(dist, 2),
                "raw": dict(s),
            })

        items.sort(key=lambda item: (item["distance"], item["name"].lower()))
        self.queue = items
        self.state = "ARMED_WAITING_JUMP" if items else "COMPLETED"
        return len(self.queue)

    def _select_next_unvisited(self) -> Optional[Dict]:
        for item in self.queue:
            if item["name"].lower() not in self.visited:
                return item
        return None

    def on_fsd_jump(self, jump_info: Dict) -> Optional[Dict]:
        """Handle a detected FSDJump event and return the next unvisited target once."""
        if self.state in ("IDLE", "COMPLETED"):
            return None
        arrived = str(jump_info.get("system") or "").strip()
        if not arrived:
            return None

        self.visited.add(arrived.lower())
        if self.state == "ARMED_WAITING_JUMP":
            self.start_system = arrived
            self.state = "ACTIVE"

        nxt = self._select_next_unvisited()
        self.current_target = nxt
        if nxt is None:
            self.state = "COMPLETED"
        return nxt

    def skip_current(self) -> Optional[Dict]:
        """Mark current_target as visited/skipped and advance to the next target."""
        if self.current_target is not None:
            self.visited.add(self.current_target["name"].lower())
        elif self.state == "ARMED_WAITING_JUMP":
            self.state = "ACTIVE"
        nxt = self._select_next_unvisited()
        self.current_target = nxt
        if nxt is None and self.state != "IDLE":
            self.state = "COMPLETED"
        return nxt

    def remaining_count(self) -> int:
        return sum(1 for item in self.queue if item["name"].lower() not in self.visited)
