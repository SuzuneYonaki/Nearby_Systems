# -*- coding: utf-8 -*-
"""sector_name_generator.py: Elite Dangerous procedural sector name from the sector cell (i, j, k) (= the x, y, z sector fields of the id64), a Python port of the published
algorithm (klightspeed's PGSectors.cs, itself based on EDTS pgnames.py / pgdata.py by Esvandiary; word tables in pgnames_tables.py).

    offset = k * 16384 + j * 128 + i              (7 bits per axis)
    hash(offset) = Bob Jenkins' 32-bit integer hash; bit 0 == 0 -> 'C1' (one word), else 'C2' (two words)
    C1: offset is split by run-length tables into prefix / infix [/ infix] / suffix  (Noijoo = Noij + oo)
    C2: offset's even bits and odd bits are two 11-bit indices; each picks a prefix and a suffix  (Hypao Phyloe = Hyp+ao Phyl+oe)
Hand-named regions (Col 285 Sector, ...) are not produced by this; only the grid names.

Usage (from imitate_forge/):
    python sector_name_generator.py 39 32 18        # -> Wregoe
    python sector_name_generator.py --check         # compare with data/sector_names_merged.json (every named cell)
"""
import argparse
import json
import sys
from typing import Dict, List, Optional, Tuple

import boxel_calculator as bc
from pgnames_tables import (C1PREFIXINFIX2, C2PREFIXSUFFIX2, INFIXES1, INFIXES2, INFIXRUNLENGTHS, PREFIXES, PREFIXRUNLENGTHS, SUFFIXES1, SUFFIXES2)

_C1_VOWEL_PREFIX = {p.lower() for p in C1PREFIXINFIX2}
_C2_VOWEL_PREFIX = {p.lower() for p in C2PREFIXSUFFIX2}
_prefix_run = {p.lower(): n for p, n in PREFIXRUNLENGTHS.items()}
_infix_run = {p.lower(): n for p, n in INFIXRUNLENGTHS.items()}


def _fill_offsets(items: List[str], runs: Dict[str, int], default: int) -> Tuple[Dict[str, int], int]:
    offsets: Dict[str, int] = {}
    cnt = 0
    for p in items:
        key = p.lower()
        runs.setdefault(key, default)
        offsets[key] = cnt
        cnt += runs[key]
    return offsets, cnt


_prefix_off, PREFIX_TOTAL = _fill_offsets(PREFIXES, _prefix_run, 35)
_infix_off: Dict[str, int] = {}
_o, INFIX1_TOTAL = _fill_offsets(INFIXES1, _infix_run, len(SUFFIXES2)); _infix_off.update(_o)
_o, INFIX2_TOTAL = _fill_offsets(INFIXES2, _infix_run, len(SUFFIXES1)); _infix_off.update(_o)


def _last(items: List[str], offsets: Dict[str, int], value: int) -> str:
    """the last item whose offset <= value (C# `.Last(p => Offsets[p] <= value)`)."""
    best = None
    for p in items:
        if offsets[p.lower()] <= value:
            best = p
    return best  # type: ignore[return-value]


def is_c1(offset: int) -> bool:
    key = offset & 0xFFFFFFFF
    key = (key + (key << 12)) & 0xFFFFFFFF
    key ^= key >> 22
    key = (key + (key << 4)) & 0xFFFFFFFF
    key ^= key >> 9
    key = (key + (key << 10)) & 0xFFFFFFFF
    key ^= key >> 2
    key = (key + (key << 7)) & 0xFFFFFFFF
    key ^= key >> 12
    return (key & 1) == 0


def _c1_name(offset: int) -> Optional[str]:
    prefix_cnt, cur = divmod(offset, PREFIX_TOTAL)
    prefix = _last(PREFIXES, _prefix_off, cur)
    cur -= _prefix_off[prefix.lower()]
    infix1_s2 = prefix.lower() in _C1_VOWEL_PREFIX
    total = INFIX2_TOTAL if infix1_s2 else INFIX1_TOTAL
    infixes = INFIXES2 if infix1_s2 else INFIXES1
    infix1_cnt, cur = divmod(prefix_cnt * _prefix_run[prefix.lower()] + cur, total)
    infix1 = _last(infixes, _infix_off, cur)
    cur -= _infix_off[infix1.lower()]
    suffixes = SUFFIXES1 if infix1_s2 else SUFFIXES2
    next_idx = _infix_run[infix1.lower()] * infix1_cnt + cur
    frags = [prefix, infix1]
    if next_idx >= len(suffixes):
        infix2_s2 = not infix1_s2
        total2 = INFIX2_TOTAL if infix2_s2 else INFIX1_TOTAL
        infix2_cnt, cur = divmod(next_idx, total2)
        infix2 = _last(INFIXES2 if infix2_s2 else INFIXES1, _infix_off, cur)
        cur -= _infix_off[infix2.lower()]
        suffixes = SUFFIXES1 if infix2_s2 else SUFFIXES2
        next_idx = _infix_run[infix2.lower()] * infix2_cnt + cur
        frags.append(infix2)
    if next_idx >= len(suffixes):
        return None
    frags.append(suffixes[next_idx])
    return "".join(frags)


def _deinterleave2(val: int) -> Tuple[int, int]:
    a = b = 0
    for m in range(16):
        a |= ((val >> (2 * m)) & 1) << m
        b |= ((val >> (2 * m + 1)) & 1) << m
    return a, b


def _c2_name(offset: int) -> Optional[str]:
    i0, i1 = _deinterleave2(offset)
    p1 = _last(PREFIXES, _prefix_off, i0)
    p2 = _last(PREFIXES, _prefix_off, i1)
    s1s = SUFFIXES2 if p1.lower() in _C2_VOWEL_PREFIX else SUFFIXES1
    s2s = SUFFIXES2 if p2.lower() in _C2_VOWEL_PREFIX else SUFFIXES1
    a, b = i0 - _prefix_off[p1.lower()], i1 - _prefix_off[p2.lower()]
    if a >= len(s1s) or b >= len(s2s):
        return None
    return f"{p1}{s1s[a]} {p2}{s2s[b]}"


def sector_name(cell: bc.Cell) -> Optional[str]:
    """grid name of sector cell (i, j, k); None when the tables give no name (index past a table)."""
    offset = (cell[2] << 14) + (cell[1] << 7) + cell[0]
    return _c1_name(offset) if is_c1(offset) else _c2_name(offset)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cell", nargs="*", type=int, help="i j k")
    ap.add_argument("--check", action="store_true", help="compare with data/sector_names_merged.json")
    a = ap.parse_args(argv)
    if a.cell:
        print(sector_name(tuple(a.cell)))  # type: ignore[arg-type]
        return 0
    if a.check:
        path = bc.DATA_DIR / "sector_names_merged.json"
        names = {tuple(int(v) for v in k.split(",")): n for k, n in json.loads(path.read_text(encoding="utf-8")).items()}
        ok = bad = none = 0
        wrong = []
        for c, n in names.items():
            g = sector_name(c)  # type: ignore[arg-type]
            if g is None:
                none += 1
            elif g.lower() == n.lower():
                ok += 1
            else:
                bad += 1
                wrong.append((c, n, g))
        print(f"named cells {len(names):,}: match {ok:,}, mismatch {bad:,}, generator gave nothing {none:,}")
        for w in wrong[:15]:
            print("  ", w)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
