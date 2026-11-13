# -*- coding: utf-8 -*-
"""sector_neighbors.py: neighbouring sectors of a position, listed per radius with direction, distance and sector name (CLI, no Tk).

Input is a system name (looked up in data/systems_dataset.db; a procedural name such as 'Noijoo AA-A h' falls back to the centre of its boxel), a bare sector name
('Noijoo' = the centre of that sector) or coordinates ('x,y,z'). Sectors are the 1280 ly cells of boxel_calculator (origin (-49985, -40985, -24105)); the names come from
data/sector_names_merged.json when it exists (else data/sector_names.json; learnt from the local systems database, see boxel_calculator.build_sector_table), so a sector that was never seen there is shown as 不明
(unknown: it can be an empty cell or a cell the database simply lacks; the grid i,j,k column still identifies it).

Each sector is listed in the first radius shell that contains its nearest point: distance 'edge' is to the nearest point of the sector (0 = you are in it), 'centre' to its
centre. Direction is the bearing of the centre in the galactic x-z plane, 0 deg = +Z (the way to Sagittarius A*), 90 deg = +X, 180 deg = -Z, 270 deg = -X, plus up/down (+Y/-Y)
when the centre is clearly above or below. 'AA-A h' is the position-derived name of the h boxel at the sector's corner (the whole sector), the string to search in the galaxy map.

Usage:
    python sector_neighbors.py "Noijoo AA-A h" [--radii 1500,3000] [--max-per-ring 60] [--hide-unnamed]
    python sector_neighbors.py -- "-12225,1895,32855"     (negative x needs the --)
"""
import argparse
import itertools
import math
import re
import sqlite3
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import boxel_calculator as bc
import sector_name_generator as gen
from boxel_calculator import Cell, Vec, SECTOR_SIZE, ORIGIN

MERGED_PATH = bc.DATA_DIR / "sector_names_merged.json"   # sector_names.json + the dump extraction (merge_sector_names.py)
DEFAULT_RADII = (1500.0, 3000.0)
SHOW_PREDICTED = True   # cells without a known name get the name the naming rule gives, marked 予測 (--no-predicted turns it off)
BEARINGS = ["+Z コア方向", "+Z+X", "+X", "-Z+X", "-Z 反コア", "-Z-X", "-X", "+Z-X"]
VERTICAL_RATIO = 0.35   # |dy| / distance above this adds an up/down mark


def use_merged_table() -> bool:
    """Load data/sector_names_merged.json in place of sector_names.json when it exists."""
    if not MERGED_PATH.exists():
        return False
    bc._sector_table = None
    bc.load_sector_table(MERGED_PATH)
    return True


def predicted_name(cell: Cell) -> Optional[str]:
    """The name sector_name_generator gives for a cell that has no known name (None outside the 7/6/7-bit cell range)."""
    if not (0 <= cell[0] < 128 and 0 <= cell[1] < 64 and 0 <= cell[2] < 128):
        return None
    return gen.sector_name(cell)


def sector_box(cell: Cell) -> Tuple[Vec, Vec]:
    lo = tuple(ORIGIN[i] + cell[i] * SECTOR_SIZE for i in range(3))
    return lo, tuple(v + SECTOR_SIZE for v in lo)  # type: ignore[return-value]


def box_distance(pos: Vec, lo: Vec, hi: Vec) -> float:
    return math.sqrt(sum(max(lo[i] - pos[i], 0.0, pos[i] - hi[i]) ** 2 for i in range(3)))


def bearing(delta: Vec) -> Tuple[float, str]:
    """(deg, label): 0 = +Z, 90 = +X, measured in the x-z plane; label adds up/down when |dy| is large."""
    dx, dy, dz = delta
    flat = math.hypot(dx, dz)
    deg = math.degrees(math.atan2(dx, dz)) % 360.0
    label = BEARINGS[int((deg + 22.5) // 45) % 8] if flat > 1e-6 else "真上/真下"
    total = math.sqrt(flat * flat + dy * dy)
    if total > 1e-6 and abs(dy) / total > VERTICAL_RATIO:
        label = (label + " " if flat > 1e-6 else "") + ("上(+Y)" if dy > 0 else "下(-Y)")
    return deg, label


def neighbours(pos: Vec, radii: Sequence[float], predict: bool = SHOW_PREDICTED) -> List[dict]:
    """Every sector whose nearest point is within max(radii) of pos, with ring index, distances, bearing and name (None if unknown; 'predicted' is the rule's name for those)."""
    rmax = max(radii)
    lo_c = bc.sector_cell(tuple(p - rmax for p in pos))  # type: ignore[arg-type]
    hi_c = bc.sector_cell(tuple(p + rmax for p in pos))  # type: ignore[arg-type]
    out: List[dict] = []
    for cell in itertools.product(*(range(lo_c[i], hi_c[i] + 1) for i in range(3))):
        lo, hi = sector_box(cell)  # type: ignore[arg-type]
        edge = box_distance(pos, lo, hi)
        if edge > rmax:
            continue
        centre = tuple((lo[i] + hi[i]) / 2 for i in range(3))
        delta = tuple(centre[i] - pos[i] for i in range(3))
        deg, label = bearing(delta)  # type: ignore[arg-type]
        ring = next(i for i, r in enumerate(sorted(radii)) if edge <= r)
        name = bc.sector_name(cell)
        out.append({"cell": cell, "name": name, "predicted": None if name or not predict else predicted_name(cell), "edge": edge, "centre_dist": math.sqrt(sum(d * d for d in delta)),
                    "deg": deg, "dir": label, "delta": delta, "ring": ring})
    out.sort(key=lambda s: (s["ring"], s["edge"], s["centre_dist"]))
    return out


def resolve(query: str) -> Tuple[Vec, str]:
    """(position, how it was resolved). Raises ValueError with a readable message."""
    q = query.strip()
    nums = re.findall(r"-?\d+(?:\.\d+)?", q) if re.fullmatch(r"[\s,;\-\d.]+", q) else []
    if len(nums) == 3:
        return (float(nums[0]), float(nums[1]), float(nums[2])), "座標"
    try:
        con = sqlite3.connect(f"file:{bc.DB_PATH.as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT x, y, z FROM systems WHERE name = ?", (q,)).fetchone()
        finally:
            con.close()
        if row:
            return (row[0], row[1], row[2]), "systems_dataset.db のシステム座標"
    except sqlite3.Error:
        pass  # DB missing or the drive is unavailable: fall back to the name arithmetic below
    m = bc._NAME_RE.match(q)
    if m:
        cells = _cells_named(m.group(1))
        if cells:
            b = bc.boxel_from_name(cells[0], m.group(2), m.group(3), int(m.group(4) or 0))
            note = f"{b.label(m.group(1))} のボクセル中心(DB に無い名前。実座標ではない)"
            if len(cells) > 1:
                note += f"; 同名のセクターが {len(cells)} 個あり最初を採用: {cells}"
            return tuple((b.lo[i] + b.hi[i]) / 2 for i in range(3)), note  # type: ignore[return-value]
    cells = _cells_named(re.sub(r"(?i)\s+sector$", "", q))
    if cells:
        lo, hi = sector_box(cells[0])
        return tuple((lo[i] + hi[i]) / 2 for i in range(3)), f"セクター {bc.sector_name(cells[0])} {cells[0]} の中心"  # type: ignore[return-value]
    raise ValueError(f"'{query}' を解決できない(DB に無く、セクター名表にも無い)。座標 'x,y,z' で指定できる")


def _cells_named(name: str) -> List[Cell]:
    return [c for c, n in bc.load_sector_table().items() if n.lower() == name.lower()]


def render(pos: Vec, how: str, radii: Sequence[float], rows: List[dict], max_per_ring: int, hide_unnamed: bool, predict: bool = SHOW_PREDICTED) -> str:
    here = bc.sector_cell(pos)
    here_pred = None if bc.sector_name(here) or not predict else predicted_name(here)
    lines = [f"位置 ({pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f}) ly  [{how}]",
             f"現在のセクター: {bc.sector_name(here) or (here_pred + ' (予測)' if here_pred else '不明')} {here}",
             "方位: 0°=+Z(Sgr A*側) 90°=+X 180°=-Z 270°=-X(x-z 平面)。距離 edge=そのセクターの最寄り点まで / centre=中心まで (ly)"]
    regions = bc.named_regions_containing(pos)
    if regions:
        lines.append(f"注意: 手名付き領域 {', '.join(regions)} の内側。この中では格子の名前・XX-Y が当てにならない")
    if predict and (here_pred or any(s["predicted"] for s in rows)):
        lines.append("予測: 表に名前が無いセルに、命名規則(sector_name_generator)から計算した名前を (予測) 付きで出している。ゲーム内の実在は未確認で、手名付き領域の中では上書きされることがある")
    bounds = [0.0] + sorted(radii)
    for ring, r in enumerate(sorted(radii)):
        members = [s for s in rows if s["ring"] == ring]
        shown = [s for s in members if s["name"] or not hide_unnamed]
        predicted = sum(1 for s in members if s["predicted"])
        unnamed = sum(1 for s in members if not s["name"] and not s["predicted"])
        counts = f"予測 {predicted}, 不明 {unnamed}" if predicted else f"不明 {unnamed}"
        lines += ["", f"=== 半径 {bounds[ring]:.0f} < edge ≤ {r:.0f} ly : {len(members)} セクター({counts}) ==="]
        lines.append(f"{'セクター名':<24}{'方向':<14}{'方位°':>6}{'edge':>8}{'centre':>8}  {'格子 i,j,k':<12}検索名(h, AA-A)")
        for s in shown[:max_per_ring]:
            name = s["name"] or (f"{s['predicted']} (予測)" if s["predicted"] else "不明")
            where = "(現在地)" if s["cell"] == here else s["dir"]
            search = f"{s['name']} AA-A h" if s["name"] else "-"
            lines.append(f"{name:<24}{where:<14}{s['deg']:>6.0f}{s['edge']:>8.0f}{s['centre_dist']:>8.0f}  {','.join(map(str, s['cell'])):<12}{search}")
        if len(shown) > max_per_ring:
            lines.append(f"... 他 {len(shown) - max_per_ring} 件(--max-per-ring で増やせる)")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="近傍セクターを半径ごとに 方角・距離・名前 で一覧する")
    ap.add_argument("where", help="システム名 / セクター名 / 'x,y,z'")
    ap.add_argument("--radii", default=",".join(f"{r:g}" for r in DEFAULT_RADII), help="半径(ly)のカンマ区切り。各殻は前の半径〜この半径")
    ap.add_argument("--max-per-ring", type=int, default=60)
    ap.add_argument("--hide-unnamed", action="store_true", help="表に名前が無いセクター(予測・不明)を一覧から隠す(件数は出る)")
    ap.add_argument("--no-predicted", action="store_true", help="命名規則から計算した予測名を出さない(表に無ければ 不明)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    use_merged_table()
    try:
        radii = [float(v) for v in args.radii.split(",") if v.strip()]
        if not radii or min(radii) <= 0:
            raise ValueError("--radii は正の数")
        pos, how = resolve(args.where)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    predict = SHOW_PREDICTED and not args.no_predicted
    print(render(pos, how, radii, neighbours(pos, radii, predict), args.max_per_ring, args.hide_unnamed, predict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
