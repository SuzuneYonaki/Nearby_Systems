# -*- coding: utf-8 -*-
"""boxel_calculator.py: coordinates <-> Stellar Forge boxel ("mass code" cell) names.

Every procedural system sits in one cell of an octree for each mass code
a..h; the cell edge is 10 * 2**k ly (a=10, b=20, c=40, d=80, e=160, f=320,
g=640, h=1280 ly) and the cell of code k holds 8 cells of code k-1. A system's
own mass code says which of those nested cells it was generated in. The part
of its name that depends only on position is

    <Sector name> <XX-Y> <code>[<n1>-]<sequence>
                  ^^^^^^^^^^^^^^^^^^^^^   <- computed here (the sequence is not)

Facts this module relies on (all checked against ~3.8M catalog names, see
test_boxel_calculator.py):
  * the grid origin is (-49985, -40985, -24105) ly; a sector is 1280 ly
    (= one h cell) and holds (128 >> k) cells per axis for code k;
  * lx, ly, lz = cell index inside the sector; idx = lx + 128*ly + 16384*lz;
    n1 = idx // 17576, r = idx % 17576 and XX-Y is the letters
    (r % 26, (r // 26) % 26, r // 676) written "c1c2-c3";
  * coordinates are multiples of 1/32 ly; a point exactly on a cell face
    belongs to the cell on its positive side.
The sector *name* cannot be computed here (it comes from the game's own
generator); it is looked up in data/sector_names.json, which is built from the
local systems database with ``--build-sector-table``.
"""
import argparse
import json
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

CODES = "abcdefgh"
ORIGIN = (-49985.0, -40985.0, -24105.0)
BASE_SIZE = 10.0
SECTOR_SIZE = 1280.0
LETTER_SPAN = 26 ** 3  # 17576

DATA_DIR = Path(__file__).resolve().parent / "data"
SECTOR_TABLE_PATH = DATA_DIR / "sector_names.json"
NAMED_REGIONS_PATH = DATA_DIR / "named_regions.json"
DB_PATH = DATA_DIR / "systems_dataset.db"

_NAME_RE = re.compile(r"^(.+) ([A-Z]{2}-[A-Z]) ([a-h])(?:(\d+)-)?(\d+)?$")

Vec = Tuple[float, float, float]
Cell = Tuple[int, int, int]


def code_index(code: str) -> int:
    code = code.lower()
    if len(code) != 1 or code not in CODES:
        raise ValueError(f"mass code must be one of a-h, got {code!r}")
    return CODES.index(code)


def cell_size(code: str) -> float:
    return BASE_SIZE * 2 ** code_index(code)


def _floor_div(value: float, size: float) -> int:
    """floor(value / size) that is exact for the game's 1/32 ly coordinates."""
    v32 = value * 32.0
    if abs(v32 - round(v32)) < 1e-6:
        v32 = round(v32)
    return int(math.floor(v32 / (size * 32.0)))


def global_cell(pos: Vec, code: str) -> Cell:
    size = cell_size(code)
    return tuple(_floor_div(pos[i] - ORIGIN[i], size) for i in range(3))  # type: ignore[return-value]


def sector_cell(pos: Vec) -> Cell:
    return tuple(_floor_div(pos[i] - ORIGIN[i], SECTOR_SIZE) for i in range(3))  # type: ignore[return-value]


def letters_from_remainder(r: int) -> str:
    c1, c2, c3 = r % 26, (r // 26) % 26, r // 676
    return f"{chr(65 + c1)}{chr(65 + c2)}-{chr(65 + c3)}"


def remainder_from_letters(prefix: str) -> int:
    m = re.fullmatch(r"([A-Z])([A-Z])-([A-Z])", prefix.upper())
    if not m:
        raise ValueError(f"prefix must look like 'AA-Z', got {prefix!r}")
    c1, c2, c3 = (ord(ch) - 65 for ch in m.groups())
    return c1 + 26 * c2 + 676 * c3


@dataclass(frozen=True)
class Boxel:
    code: str
    cell: Cell                 # global cell index at this code's resolution
    sector: Cell               # sector cell index (1280 ly)
    local: Cell                # index inside the sector
    idx: int
    n1: int
    prefix: str                # "AA-Z"
    lo: Vec
    hi: Vec

    @property
    def size(self) -> float:
        return cell_size(self.code)

    def label(self, sector_name: Optional[str] = None) -> str:
        """The position-derived part of a system name, e.g. 'Noijo AA-Z e'."""
        head = sector_name if sector_name else f"<sector {self.sector[0]},{self.sector[1]},{self.sector[2]}>"
        code = f"{self.code}{self.n1}-" if self.n1 else self.code
        return f"{head} {self.prefix} {code}"


def boxel_from_cell(cell: Cell, code: str) -> Boxel:
    k = code_index(code)
    per_axis = 128 >> k
    sector = tuple(c // per_axis for c in cell)
    local = tuple(c % per_axis for c in cell)
    idx = local[0] + 128 * local[1] + 16384 * local[2]
    r = idx % LETTER_SPAN
    size = cell_size(code)
    lo = tuple(ORIGIN[i] + cell[i] * size for i in range(3))
    hi = tuple(lo[i] + size for i in range(3))
    return Boxel(code.lower(), cell, sector, local, idx, idx // LETTER_SPAN,  # type: ignore[arg-type]
                 letters_from_remainder(r), lo, hi)  # type: ignore[arg-type]


def boxel_at(pos: Vec, code: str) -> Boxel:
    return boxel_from_cell(global_cell(pos, code), code)


def boxel_from_name(sector: Cell, prefix: str, code: str, n1: int = 0) -> Boxel:
    """Inverse: the boxel named by (sector cell, 'AA-Z', code, n1)."""
    k = code_index(code)
    per_axis = 128 >> k
    idx = remainder_from_letters(prefix) + LETTER_SPAN * n1
    local = (idx % 128, (idx // 128) % 128, idx // 16384)
    if any(v >= per_axis for v in local) or idx // 16384 >= per_axis:
        raise ValueError(f"{prefix} n1={n1} is outside a code-{code} sector ({per_axis} cells per axis)")
    cell = tuple(sector[i] * per_axis + local[i] for i in range(3))
    return boxel_from_cell(cell, code)  # type: ignore[arg-type]


# ---------------------------------------------------------------- sector names
_sector_table: Optional[Dict[Cell, str]] = None


def load_sector_table(path: Path = SECTOR_TABLE_PATH) -> Dict[Cell, str]:
    global _sector_table
    if _sector_table is None:
        table: Dict[Cell, str] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for key, name in raw.items():
                table[tuple(int(v) for v in key.split(","))] = name  # type: ignore[assignment]
        _sector_table = table
    return _sector_table


def sector_name(cell: Cell) -> Optional[str]:
    return load_sector_table().get(cell)


def sector_cell_by_name(name: str) -> Optional[Cell]:
    for cell, n in load_sector_table().items():
        if n.lower() == name.lower():
            return cell
    return None


def build_sector_table(db_path: Path = DB_PATH, out_path: Path = SECTOR_TABLE_PATH) -> Tuple[int, int]:
    """Learn sector cell -> sector name from procedural system names in the DB.

    Returns (sectors, named_regions). The most common name in a cell is its
    grid name; any other name seen there belongs to a hand-named region."""
    counts: Dict[Cell, Counter] = defaultdict(Counter)
    rows: List[Tuple[Cell, str, float, float, float]] = []
    con = sqlite3.connect(str(db_path))
    try:
        for name, x, y, z in con.execute("SELECT name, x, y, z FROM systems"):
            m = _NAME_RE.match(name)
            if not m:
                continue
            c = sector_cell((x, y, z))
            counts[c][m.group(1)] += 1
            rows.append((c, m.group(1), x, y, z))
    finally:
        con.close()
    table = {c: cnt.most_common(1)[0][0] for c, cnt in counts.items()}
    # Hand-named regions (Col 285 Sector, M7 Sector, ...) override the grid name inside
    # their own volume and do not follow the grid's XX-Y numbering; remember where they are.
    pts: Dict[str, List[Vec]] = defaultdict(list)
    for c, sec, x, y, z in rows:
        if sec != table[c]:
            pts[sec].append((x, y, z))
    regions = {}
    for sec, p in pts.items():
        if len(p) < 3:
            continue
        cx, cy, cz = (sum(v[i] for v in p) / len(p) for i in range(3))
        d = sorted(math.dist(v, (cx, cy, cz)) for v in p)
        regions[sec] = {"n": len(p), "center": [cx, cy, cz], "radius": d[int(0.99 * (len(d) - 1))]}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({f"{c[0]},{c[1]},{c[2]}": n for c, n in table.items()},
                                   ensure_ascii=False, sort_keys=True), encoding="utf-8")
    NAMED_REGIONS_PATH.write_text(json.dumps(regions, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    global _sector_table, _named_regions
    _sector_table = _named_regions = None
    return len(table), len(regions)


_named_regions: Optional[Dict[str, dict]] = None


def named_regions_containing(pos: Vec) -> List[str]:
    """Hand-named regions whose (estimated) volume contains pos. Inside them the
    grid-based XX-Y / n1 computed here is often wrong (see module doc)."""
    global _named_regions
    if _named_regions is None:
        _named_regions = (json.loads(NAMED_REGIONS_PATH.read_text(encoding="utf-8"))
                          if NAMED_REGIONS_PATH.exists() else {})
    return sorted(n for n, r in _named_regions.items() if math.dist(pos, r["center"]) <= r["radius"])


# ------------------------------------------------------------------ geometry
def face_distances(pos: Vec, b: Boxel) -> List[Tuple[str, float]]:
    """Distance from pos to each of the 6 faces of boxel b, nearest first."""
    out = []
    for i, axis in enumerate("xyz"):
        out.append((f"-{axis}", pos[i] - b.lo[i]))
        out.append((f"+{axis}", b.hi[i] - pos[i]))
    return sorted(out, key=lambda t: t[1])


def _box_distance(pos: Vec, lo: Vec, hi: Vec) -> float:
    d2 = 0.0
    for i in range(3):
        d = max(lo[i] - pos[i], 0.0, pos[i] - hi[i])
        d2 += d * d
    return math.sqrt(d2)


def reached_boxels(pos: Vec, code: str, radius: float) -> List[Tuple[Boxel, float]]:
    """Boxels of ``code`` touched by the sphere (pos, radius), with distance to each."""
    home = global_cell(pos, code)
    reach = int(math.ceil(radius / cell_size(code)))
    out = []
    for dx in range(-reach, reach + 1):
        for dy in range(-reach, reach + 1):
            for dz in range(-reach, reach + 1):
                b = boxel_from_cell((home[0] + dx, home[1] + dy, home[2] + dz), code)
                d = _box_distance(pos, b.lo, b.hi)
                if d <= radius:
                    out.append((b, d))
    out.sort(key=lambda t: t[1])
    return out


def path_crossings(a: Vec, b: Vec, codes: Sequence[str]) -> List[Tuple[float, Vec, str, str]]:
    """Points on segment a->b where the boxel of a code changes.

    Returns (distance from a, point, code, axis) sorted by distance."""
    length = math.dist(a, b)
    out = []
    for code in codes:
        size = cell_size(code)
        for i, axis in enumerate("xyz"):
            d = b[i] - a[i]
            if d == 0:
                continue
            lo, hi = sorted((a[i], b[i]))
            first = math.floor((lo - ORIGIN[i]) / size) + 1
            last = math.floor((hi - ORIGIN[i]) / size)
            for k in range(first, last + 1):
                plane = ORIGIN[i] + k * size
                t = (plane - a[i]) / d
                if 0 < t <= 1:
                    pt = tuple(a[j] + (b[j] - a[j]) * t for j in range(3))
                    out.append((t * length, pt, code, axis))
    out.sort(key=lambda r: (r[0], r[2]))
    return out  # type: ignore[return-value]


# ----------------------------------------------------------------------- CLI
def _fmt_vec(v: Vec) -> str:
    return "(" + ", ".join(f"{c:g}" for c in v) + ")"


def describe_point(pos: Vec, codes: Sequence[str], radius: float) -> str:
    lines = [f"座標 {_fmt_vec(pos)}  セクター {sector_cell(pos)} = {sector_name(sector_cell(pos)) or '(名前未登録)'}"]
    regions = named_regions_containing(pos)
    if regions:
        lines.append("注意: 名前付きリージョン内の可能性 → " + ", ".join(regions[:6]) +
                     "。この中ではXX-Yやn1がグリッド計算とずれることがあります(DB照合で約3割しか一致しない)")
    lines.append("コード  一辺    名前の先頭部分                      セル範囲(x / y / z)                           最寄りの面")
    for code in codes:
        b = boxel_at(pos, code)
        name = b.label(sector_name(b.sector))
        rng = " / ".join(f"[{b.lo[i]:g}, {b.hi[i]:g})" for i in range(3))
        face, dist = face_distances(pos, b)[0]
        lines.append(f"  {code}    {b.size:>5g}  {name:<32} {rng}   {face} 面まで {dist:g}ly")
    if radius > 0:
        lines.append(f"\n半径 {radius:g}ly の球が届くセル:")
        for code in codes:
            hits = reached_boxels(pos, code, radius)
            home = boxel_at(pos, code)
            if len(hits) == 1:
                lines.append(f"  {code}: 枠を越えない(すべて {home.label(sector_name(home.sector))}内)")
                continue
            lines.append(f"  {code}: 枠を越える → {len(hits)}個のセルに届く")
            for b, d in hits:
                mark = "  <- 自分のセル" if b.cell == home.cell else ""
                lines.append(f"      {b.label(sector_name(b.sector)):<34} 距離 {d:5.2f}ly{mark}")
    return "\n".join(lines)


def _nudge(pt: Vec, axis: int, direction: float) -> Vec:
    """pt moved 1/64 ly along one axis so it lands clearly on one side of a face."""
    out = list(pt)
    out[axis] += direction / 64.0
    return tuple(out)  # type: ignore[return-value]


def describe_path(a: Vec, b: Vec, codes: Sequence[str]) -> str:
    cross = path_crossings(a, b, codes)
    lines = [f"{_fmt_vec(a)} → {_fmt_vec(b)}  全長 {math.dist(a, b):.2f}ly  枠の切り替わり {len(cross)} 回 ({','.join(codes)})"]
    for dist, pt, code, axis in cross:
        i = "xyz".index(axis)
        sign = 1.0 if b[i] > a[i] else -1.0
        before = boxel_at(_nudge(pt, i, -sign), code)
        after = boxel_at(_nudge(pt, i, sign), code)
        lines.append(f"  {dist:8.2f}ly  {code}  {axis}方向  {_fmt_vec(tuple(round(c, 3) for c in pt))}  "
                     f"{before.label(sector_name(before.sector))} → {after.label(sector_name(after.sector))}")
    return "\n".join(lines)


def describe_name(sector: str, prefix: str, code: str, n1: int) -> str:
    cell = sector_cell_by_name(sector)
    if cell is None:
        raise SystemExit(f"セクター名 '{sector}' は data/sector_names.json にありません(--build-sector-table で作成)")
    b = boxel_from_name(cell, prefix, code, n1)
    rng = " / ".join(f"[{b.lo[i]:g}, {b.hi[i]:g})" for i in range(3))
    return (f"{b.label(sector)}  一辺 {b.size:g}ly\n  範囲 {rng}\n"
            f"  中心 {_fmt_vec(tuple((b.lo[i] + b.hi[i]) / 2 for i in range(3)))}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="座標 <-> ボクセル(枠)名の計算機")
    ap.add_argument("xyz", nargs="*", type=float, help="X Y Z (ly)")
    ap.add_argument("--codes", default="cde", help="表示する質量コード(既定 cde、全部なら abcdefgh)")
    ap.add_argument("--radius", type=float, default=0.0, help="この半径(ly)の球が枠を越えるか調べる")
    ap.add_argument("--to", nargs=3, type=float, metavar=("X", "Y", "Z"), help="2点間で枠が切り替わる位置を列挙")
    ap.add_argument("--name", nargs=4, metavar=("SECTOR", "PREFIX", "CODE", "N1"),
                    help="名前の先頭部分からセル範囲を逆引き 例: --name Noijo AA-Z e 0")
    ap.add_argument("--build-sector-table", action="store_true", help="DBからセクター名の表を作る")
    args = ap.parse_args(argv)
    codes = [c for c in args.codes.lower() if c in CODES]
    if args.build_sector_table:
        n, regions = build_sector_table()
        print(f"{n} sectors -> {SECTOR_TABLE_PATH}")
        print(f"{regions} named regions -> {NAMED_REGIONS_PATH}")
        return 0
    if args.name:
        print(describe_name(args.name[0], args.name[1], args.name[2], int(args.name[3])))
        return 0
    if len(args.xyz) != 3:
        ap.error("X Y Z が必要です")
    pos = tuple(args.xyz)
    if args.to:
        print(describe_path(pos, tuple(args.to), codes))  # type: ignore[arg-type]
    else:
        print(describe_point(pos, codes, args.radius))  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
