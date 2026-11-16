# -*- coding: utf-8 -*-
"""nearby_systems.py: the systems within R ly (default 15, a jump) of a point, and the 'daisy chain' of jumps from there: n hops, each hop the systems within R ly of the previous hop's systems (CLI, no Tk).

The point is one of
    (empty / 'journal')  the commander's last position: the newest Location / FSDJump / CarrierJump with a StarPos in ~/Saved Games/Frontier Developments/Elite Dangerous/Journal.*.log
    'x,y,z'              coordinates in ly
    an id64 (digits)     looked up in journal_scans (data/systems_dataset.db), then in the catalogue (exact); when neither has it, the centre of the voxel the id64 encodes (approximate, flagged)
    a system name        data/systems_dataset.db (exact, then case-insensitive); a procedural name ('Wregoe AC-D d12-0') is turned into its id64 and found in the catalogue; failing that the voxel centre
The systems come from --source: edsm (default; EDSM api-v1/sphere-systems around the point, radius min(R * n, 100) ly: every system a chain of n hops can reach lies within R * n of the point),
catalog (data/full_catalog, the whole Spansh dump: id64 + x, y, z; one filtered scan, about 2 s; works offline) or both (merged, a system found by both is 両方). A system's name is EDSM's name; for a
catalogue system the name data/systems_dataset.db has for its exact coordinates (real names), else the procedural name computed from its id64 (the alias the game search accepts, e.g. Sol = 'Wregoe AC-D d12-0').
'Source' says where a system was found: カタログ, EDSM or 両方. Hop 0 is the point itself when it is a system. If EDSM cannot be asked the catalogue is used instead (with a note).

Network load (EDSM is a volunteer service): answers are cached on disk in data/edsm_cache (24 h, the lifetime EDSM itself declares); a search whose area lies inside a cached area asks nobody; a request
fetches a little more than needed (EDSM_PREFETCH) so that the next jumps of a route are cached; requests are at least EDSM_MIN_INTERVAL apart; after a failure nothing is sent for EDSM_COOLDOWN seconds
(an old cache is used instead); there are no retries. Hardening against what comes back from the network or from files: HTTPS only, redirects refused, answer size and count limits, every name and
coordinate checked and cleaned (control / bidi characters removed), the cache is JSON read back through the same checks; inputs (radius, n, coordinates, id64) are range-checked.

Usage (from imitate_forge/):
    python nearby_systems.py [where] [--radius 15] [--hops 1] [--source edsm|catalog|both] [--max 3000]
    python nearby_systems.py -n 3 -- "-135.1875,300.65625,-160.375"     (negative x needs the --, after the options)
"""
import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

import boxel_calculator as bc
import sector_name_generator as gen
import sector_neighbors as sn

CATALOG_DIR = bc.DATA_DIR / "full_catalog"
JOURNAL_DIR = Path(os.path.expanduser(r"~\Saved Games\Frontier Developments\Elite Dangerous"))
DEFAULT_RADIUS = 15.0       # the longest jump
DEFAULT_MAX = 3000          # systems listed at most (a chain through the core can run away)
MAX_BOX_ROWS = 3_000_000    # catalogue systems read for one search at most (memory); the search area is cut down to fit
CHECK_ABOVE, CHECK_START = 200.0, 3200.0   # ly: areas up to CHECK_ABOVE are read as they are; a bigger one is counted first, starting from CHECK_START
EDSM_URL = "https://www.edsm.net/api-v1/sphere-systems"
EDSM_SYSTEM_URL = "https://www.edsm.net/api-v1/system"
EDSM_MAX_RADIUS = 100.0     # the largest sphere asked of EDSM (answers up to this size were seen; its docs give 100 for the radius parameters)
EDSM_USER_AGENT = "nearby-systems"
EDSM_PREFETCH = 30.0        # ly: a request asks this much more than needed, so the next jumps of a route are answered from the cache
EDSM_MIN_INTERVAL = 1.5     # s between two requests of this process
EDSM_COOLDOWN = 60.0        # s without any request after a failure (or the server's Retry-After, at most EDSM_COOLDOWN_MAX)
EDSM_COOLDOWN_MAX = 600.0
EDSM_TIMEOUT = 30.0
EDSM_MAX_BYTES = 25 * 1024 * 1024   # an answer (and a cache file) larger than this is refused
EDSM_MAX_SYSTEMS = 200_000          # systems taken from one answer at most
CACHE_DIR = bc.DATA_DIR / "edsm_cache"
CACHE_TTL = 24 * 3600.0     # s; EDSM sends Cache-Control: max-age=86400
CACHE_MAX_BYTES = 300 * 1024 * 1024   # nothing is added to the cache once the folder holds this much (old files are never deleted by this program)
MAX_NAME = 120              # characters of a system name kept
COORD_LIMIT = 200_000.0     # |x|, |y|, |z| above this ly is not a position of the galaxy
MAX_RADIUS_INPUT, MAX_HOPS_INPUT, MAX_SYSTEMS_INPUT = 1000.0, 1_000_000, 1_000_000
JOURNAL_TAIL_BYTES = 8 * 1024 * 1024   # of a journal file only its end is read
POSITION_EVENTS = ("Location", "FSDJump", "CarrierJump")
SRC_CATALOG, SRC_EDSM, SRC_BOTH = "カタログ", "EDSM", "両方"
SOURCE_EDSM, SOURCE_CATALOG, SOURCE_BOTH = "edsm", "catalog", "both"
SOURCES = (SOURCE_EDSM, SOURCE_CATALOG, SOURCE_BOTH)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")   # control, zero-width and bidi-override characters (written as escapes: no invisible characters in this source)


class EdsmError(Exception):
    """EDSM could not be asked or answered something unexpected (the catalogue result is still shown)."""


@dataclass
class Centre:
    pos: Tuple[float, float, float]
    name: Optional[str]
    id64: Optional[int]
    how: str
    approximate: bool = False


@dataclass
class SearchResult:
    centre: Centre
    rows: List[dict]
    notes: List[str] = field(default_factory=list)
    truncated: bool = False
    info: List[str] = field(default_factory=list)   # where the systems came from (EDSM / cache), not a warning


# ---------------------------------------------------------------- id64
def decode_id64(v: int):
    """(mass code letter, local cell (x, y, z), sector cell (x, y, z), sequence): mass code | z cell | z sector | y cell | y sector | x cell | x sector | seq, low bits first."""
    mc = v & 7
    n = 7 - mc
    s = v >> 3
    out = []
    for bits in (n, 7, n, 6, n, 7):
        out.append(s & ((1 << bits) - 1))
        s >>= bits
    lz, zs, ly, ys, lx, xs = out
    return bc.CODES[mc], (lx, ly, lz), (xs, ys, zs), s


def encode_id64(code: str, local: Tuple[int, int, int], sector: Tuple[int, int, int], seq: int) -> int:
    k = bc.code_index(code)
    n = 7 - k
    v, shift = k, 3
    for val, bits in ((local[2], n), (sector[2], 7), (local[1], n), (sector[1], 6), (local[0], n), (sector[0], 7)):
        v |= val << shift
        shift += bits
    return v | (seq << shift)


def procedural_name(v: int) -> str:
    """the procedural system name an id64 encodes: '<sector> XX-Y <code>[<n1>-]<seq>' (Sol 10477373803 -> 'Wregoe AC-D d12-0')."""
    code, local, sector, seq = decode_id64(v)
    idx = local[0] + 128 * local[1] + 16384 * local[2]
    cube = bc.letters_from_remainder(idx % bc.LETTER_SPAN)
    n1 = idx // bc.LETTER_SPAN
    sec = bc.sector_name(sector) or gen.sector_name(sector) or f"<sector {sector[0]},{sector[1]},{sector[2]}>"
    return f"{sec} {cube} {code}{n1}-{seq}" if n1 else f"{sec} {cube} {code}{seq}"


def boxel_centre(v: int) -> Tuple[float, float, float]:
    code, local, sector, _seq = decode_id64(v)
    per = 128 >> bc.code_index(code)
    b = bc.boxel_from_cell(tuple(sector[i] * per + local[i] for i in range(3)), code)  # type: ignore[arg-type]
    return tuple((b.lo[i] + b.hi[i]) / 2 for i in range(3))  # type: ignore[return-value]


def key32(x: float, y: float, z: float) -> Tuple[int, int, int]:
    """coordinates on the game's 1/32 ly grid, to match the same system across sources."""
    return round(x * 32), round(y * 32), round(z * 32)


# ---------------------------------------------------------------- checks on everything that comes from outside
def clean_name(v, limit: int = MAX_NAME) -> Optional[str]:
    """a system name from outside (EDSM, a CSV, the journal) made safe to print and show: a string, no control / zero-width / bidi characters, at most `limit` characters; None when nothing is left."""
    if not isinstance(v, str):
        return None
    s = _CONTROL.sub("", v).strip()[:limit].strip()
    return s or None


def check_position(pos: Sequence[float]) -> Tuple[float, float, float]:
    """(x, y, z) as floats; ValueError unless all three are finite and inside the galaxy's range (an 'inf' or 1e999 would poison every distance)."""
    try:
        x, y, z = (float(v) for v in pos)
    except (TypeError, ValueError):
        raise ValueError("座標は数値 3 つ")
    if not all(math.isfinite(v) and abs(v) <= COORD_LIMIT for v in (x, y, z)):
        raise ValueError(f"座標は ±{COORD_LIMIT:,.0f} ly 以内の有限の数")
    return x, y, z


def check_id64(v: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not 0 < v < 2 ** 63:
        raise ValueError("id64 は 1 以上 2^63 未満の整数")
    return v


# ---------------------------------------------------------------- database and catalogue
def _db():
    return sqlite3.connect(f"file:{bc.DB_PATH.as_posix()}?mode=ro", uri=True)


def catalogue_available() -> bool:
    return CATALOG_DIR.exists()


def _catalog():
    import pyarrow.dataset as ds
    return ds.dataset(str(CATALOG_DIR), format="parquet")


def _box_filter(pos: Sequence[float], r: float):
    import pyarrow.dataset as ds
    flt = None
    for axis, v in zip("xyz", pos):
        g = (ds.field(axis) >= v - r) & (ds.field(axis) <= v + r)
        flt = g if flt is None else flt & g
    return flt


def catalogue_box(pos: Sequence[float], r: float):
    """DataFrame (id64, x, y, z) of the catalogue systems in the cube of half-side r around pos (one filtered scan)."""
    if not catalogue_available():
        import pandas as pd
        return pd.DataFrame(columns=["id64", "x", "y", "z"])
    return _catalog().to_table(columns=["id64", "x", "y", "z"], filter=_box_filter(pos, r)).to_pandas()


def fit_reach(pos: Sequence[float], reach: float) -> float:
    """reach, or the largest halving of it whose cube holds at most MAX_BOX_ROWS catalogue systems (a huge n would otherwise try to read the whole 190 million systems into memory).
    Cubes up to CHECK_ABOVE ly are never counted: they are small in any region, so the usual searches pay nothing."""
    if not catalogue_available() or reach <= CHECK_ABOVE:
        return reach
    r = min(reach, CHECK_START)
    while r > CHECK_ABOVE and _catalog().count_rows(filter=_box_filter(pos, r)) > MAX_BOX_ROWS:
        r /= 2
    return r


def catalogue_by_id64(v: int) -> Optional[Tuple[float, float, float]]:
    if not catalogue_available():
        return None
    import pyarrow.dataset as ds
    t = _catalog().to_table(columns=["x", "y", "z"], filter=ds.field("id64") == v).to_pylist()
    return (t[0]["x"], t[0]["y"], t[0]["z"]) if t else None


def db_names_in_box(pos: Sequence[float], r: float) -> Dict[Tuple[int, int, int], str]:
    """{1/32 ly coordinates: name} of the systems and anchors data/systems_dataset.db has in the cube (real names; empty when the database cannot be read)."""
    names: Dict[Tuple[int, int, int], str] = {}
    try:
        con = _db()
        try:
            args = [pos[0] - r, pos[0] + r, pos[1] - r, pos[1] + r, pos[2] - r, pos[2] + r]
            for table in ("systems", "anchors"):
                for n, x, y, z in con.execute(f"SELECT name, x, y, z FROM {table} WHERE x BETWEEN ? AND ? AND y BETWEEN ? AND ? AND z BETWEEN ? AND ?", args):
                    names.setdefault(key32(x, y, z), n)
        finally:
            con.close()
    except sqlite3.Error:
        pass
    return names


# ---------------------------------------------------------------- the point
def journal_position(journal_dir: Path = JOURNAL_DIR, max_files: int = 30) -> Tuple[str, Optional[int], Tuple[float, float, float]]:
    """(system name, id64, position) of the last Location / FSDJump / CarrierJump in the newest journal files. Raises ValueError when there is none."""
    files = sorted(Path(journal_dir).glob("Journal.*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    if not files:
        raise ValueError(f"ジャーナルが見つからない: {journal_dir}")
    for f in files:
        try:
            with open(f, "rb") as fh:   # only the end of the file: a journal can be huge, and a position is among its last lines
                fh.seek(0, os.SEEK_END)
                fh.seek(max(fh.tell() - JOURNAL_TAIL_BYTES, 0))
                lines = fh.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"StarPos"' not in line:
                continue
            try:
                e = json.loads(line)
            except (ValueError, RecursionError):
                continue   # a half-written last line, or the cut first line of the tail
            if not isinstance(e, dict) or e.get("event") not in POSITION_EVENTS or not isinstance(e.get("StarPos"), list) or len(e["StarPos"]) != 3:
                continue
            try:
                pos = check_position(e["StarPos"])
            except ValueError:
                continue
            addr = e.get("SystemAddress")
            return clean_name(e.get("StarSystem")) or "", addr if isinstance(addr, int) and not isinstance(addr, bool) and 0 < addr < 2 ** 63 else None, pos
    raise ValueError(f"最新 {len(files)} 本のジャーナルに位置(Location / FSDJump / CarrierJump)が無い")


def _real_name(pos: Sequence[float], id64: Optional[int]) -> str:
    names = db_names_in_box(pos, 0.01)
    return names.get(key32(*pos)) or (procedural_name(id64) if id64 else "")


def resolve_centre(query: str, journal_dir: Path = JOURNAL_DIR, allow_edsm: bool = False,
                   system_fetch: Optional[Callable[[str], Optional[dict]]] = None) -> Centre:
    """the point from '' / 'journal', 'x,y,z', an id64 or a system name (a name the local data lacks is asked of EDSM when allow_edsm). Raises ValueError with a readable message."""
    sn.use_merged_table()
    q = _CONTROL.sub("", query).strip()
    if len(q) > 200:
        raise ValueError("入力が長すぎる(200 文字まで)")
    if q == "" or q.lower() in ("journal", "ジャーナル"):
        name, id64, pos = journal_position(journal_dir)
        return Centre(pos, name or None, id64, "ジャーナルの現在地")
    nums = re.findall(r"-?\d+(?:\.\d+)?", q) if re.fullmatch(r"[\s,;\-\d.]+", q) else []
    if len(nums) == 3:
        return Centre(check_position(nums), None, None, "座標")
    if re.fullmatch(r"\d{4,}", q):
        return _centre_from_id64(check_id64(int(q)))
    return _centre_from_name(q, allow_edsm, system_fetch or edsm_system)


def _centre_from_id64(v: int) -> Centre:
    try:
        con = _db()
        try:
            row = con.execute("SELECT name, x, y, z FROM journal_scans WHERE system_address = ?", (v,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        row = None
    if row:
        return Centre((row[1], row[2], row[3]), row[0], v, "id64 → journal_scans(DB)")
    pos = catalogue_by_id64(v)
    if pos:
        return Centre(pos, _real_name(pos, v), v, "id64 → カタログ(正確な座標)")
    return Centre(boxel_centre(v), procedural_name(v), v, "id64 が示すボクセルの中心(カタログに無い。座標は近似でボクセルの半分ほど外れ得る)", True)


def _centre_from_name(q: str, allow_edsm: bool = False, system_fetch: Optional[Callable[[str], Optional[dict]]] = None) -> Centre:
    try:
        con = _db()
        try:
            row = con.execute("SELECT name, x, y, z FROM systems WHERE name = ?", (q,)).fetchone() or \
                con.execute("SELECT name, x, y, z FROM systems WHERE name = ? COLLATE NOCASE", (q,)).fetchone() or \
                con.execute("SELECT name, x, y, z FROM anchors WHERE name = ? COLLATE NOCASE", (q,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        row = None
    if row:
        return Centre((row[1], row[2], row[3]), row[0], None, "systems_dataset.db のシステム座標")
    m = bc._NAME_RE.match(q)
    if m:
        cells = sn._cells_named(m.group(1))
        if cells:
            try:
                b = bc.boxel_from_name(cells[0], m.group(2), m.group(3), int(m.group(4) or 0))
            except ValueError as e:
                raise ValueError(str(e))
            if m.group(5) is not None:
                v = encode_id64(m.group(3), b.local, b.sector, int(m.group(5)))
                pos = catalogue_by_id64(v)
                if pos:
                    return Centre(pos, q, v, "プロシージャル名 → id64 → カタログ(正確な座標)")
            approx = Centre(tuple((b.lo[i] + b.hi[i]) / 2 for i in range(3)), q, None,  # type: ignore[arg-type]
                            "プロシージャル名が示すボクセルの中心(カタログに無い。座標は近似)", True)
            if not allow_edsm:
                return approx
            try:   # EDSM may know the exact position of a system the catalogue lacks
                found = (system_fetch or edsm_system)(q)
            except EdsmError:
                found = None
            return Centre(found["pos"], found["name"], found["id64"], "EDSM のシステム座標") if found else approx
    if allow_edsm:
        try:
            found = (system_fetch or edsm_system)(q)
        except EdsmError as e:
            raise ValueError(f"'{q}' はローカルのデータに無く、EDSM にも問い合わせできなかった: {e}")
        if found:
            return Centre(found["pos"], found["name"], found["id64"], "EDSM のシステム座標")
    raise ValueError(f"'{q}' を解決できない(DB にもプロシージャル名の表にも" + ("EDSM にも" if allow_edsm else "") + "無い)。座標 'x,y,z' か id64 で指定できる")


# ---------------------------------------------------------------- EDSM (network input is untrusted: HTTPS only, no redirects, size / count limits, checked and cleaned, cached, rate limited)
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """a redirect is an error here: the answer must come from www.edsm.net over HTTPS, not from wherever a (compromised) server points to."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)   # default HTTPS handler: certificates and host names are verified
_last_request = 0.0       # time.monotonic() of the last request of this process
_cooldown_until = 0.0     # time.monotonic() before which no request is sent (set by a failure)


class EdsmRows(list):
    """the systems of an EDSM answer ([{'name', 'x', 'y', 'z', 'id64'}]) and where they came from (.origin: a short text for the notes)."""
    origin = ""


def _http_get_json(url: str):
    """the parsed JSON of one GET request, politely: at least EDSM_MIN_INTERVAL after the previous request, none during the cooldown after a failure, no retry. Raises EdsmError."""
    global _last_request, _cooldown_until
    if not url.startswith("https://www.edsm.net/"):
        raise EdsmError("EDSM 以外の宛先は拒否する")
    now = time.monotonic()
    if now < _cooldown_until:
        raise EdsmError(f"EDSM は直前に失敗したので、あと {_cooldown_until - now:.0f} 秒は問い合わせない")
    wait = _last_request + EDSM_MIN_INTERVAL - now
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": EDSM_USER_AGENT, "Accept": "application/json"})
    try:
        with _opener.open(req, timeout=EDSM_TIMEOUT) as r:
            body = r.read(EDSM_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        retry = e.headers.get("Retry-After") if e.headers else None
        _cooldown_until = time.monotonic() + (min(max(float(retry), 1.0), EDSM_COOLDOWN_MAX) if retry and retry.strip().isdigit() else EDSM_COOLDOWN)
        raise EdsmError(f"EDSM が HTTP {int(e.code)} を返した")   # the server's own text is not echoed
    except (urllib.error.URLError, OSError, ValueError) as e:      # includes timeouts and certificate errors
        _cooldown_until = time.monotonic() + EDSM_COOLDOWN
        raise EdsmError(f"EDSM に問い合わせできない({type(e).__name__})")
    if len(body) > EDSM_MAX_BYTES:
        _cooldown_until = time.monotonic() + EDSM_COOLDOWN
        raise EdsmError(f"EDSM の応答が大きすぎる(上限 {EDSM_MAX_BYTES // 1024 // 1024} MB)")
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):   # UnicodeDecodeError is a ValueError; RecursionError: absurdly nested JSON
        raise EdsmError("EDSM の応答が JSON ではない")


def _parse_system(s) -> Optional[dict]:
    """one system of an answer (or of a cache file) checked and cleaned; None when it is not a usable system."""
    if not isinstance(s, dict):
        return None
    name, c = clean_name(s.get("name")), s.get("coords")
    if name is None or not isinstance(c, dict):
        return None
    try:
        x, y, z = check_position([c["x"], c["y"], c["z"]])
    except (KeyError, ValueError):
        return None
    id64 = s.get("id64")
    return {"name": name, "x": x, "y": y, "z": z, "id64": id64 if isinstance(id64, int) and not isinstance(id64, bool) and 0 < id64 < 2 ** 63 else None}


def _to_raw(s: dict) -> dict:
    """a checked system back in EDSM's own shape, which is what the cache files hold (so that reading them back goes through the very same _parse_system)."""
    return {"name": s["name"], "id64": s["id64"], "coords": {"x": s["x"], "y": s["y"], "z": s["z"]}}


def _parse_systems(data) -> List[dict]:
    if not isinstance(data, list):
        raise EdsmError("EDSM の応答が想定外(一覧ではない)")
    out = []
    for s in data[:EDSM_MAX_SYSTEMS]:
        p = _parse_system(s)
        if p:
            out.append(p)
    return out


# the disk cache: data/edsm_cache/sphere_<x>_<y>_<z>_<r>.json = {"v": 1, "fetched": unix time, "systems": [...]}; the file name holds the centre and radius (numbers this program formatted, matched strictly),
# the content is read back through _parse_system again. Nothing is ever deleted; a file is only replaced by a newer answer for the same sphere.
_NUM = r"-?\d+\.\d+"
_SPHERE_FILE = re.compile(rf"^sphere_({_NUM})_({_NUM})_({_NUM})_(\d+\.\d)\.json$")


def _cache_usable() -> bool:
    return CACHE_DIR.is_dir() and not CACHE_DIR.is_symlink()


def _read_cache_file(path: Path) -> Optional[Tuple[float, List[dict]]]:
    try:
        if path.is_symlink() or path.stat().st_size > EDSM_MAX_BYTES * 2:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        fetched = data["fetched"]
        if not isinstance(fetched, (int, float)) or isinstance(fetched, bool) or not math.isfinite(fetched) or not isinstance(data["systems"], list):
            return None
        return float(fetched), _parse_systems(data["systems"])
    except (OSError, ValueError, KeyError, TypeError, RecursionError, EdsmError):
        return None


def _cache_lookup(pos: Sequence[float], r: float, fresh_only: bool, now: float) -> Optional[Tuple[float, List[dict]]]:
    """(fetched time, the systems within r of pos) from a cached sphere that contains the wanted one (the newest such; with fresh_only only one younger than CACHE_TTL), or None."""
    if not _cache_usable():
        return None
    best = None
    for p in CACHE_DIR.iterdir():
        m = _SPHERE_FILE.match(p.name)
        if not m:
            continue
        cx, cy, cz, cr = (float(v) for v in m.groups())
        if math.dist((cx, cy, cz), pos) + r > cr + 1e-6:
            continue   # the wanted sphere is not inside this one
        got = _read_cache_file(p)
        if got is None or (fresh_only and now - got[0] > CACHE_TTL):
            continue
        if best is None or got[0] > best[0]:
            best = got
    if best is None:
        return None
    return best[0], [s for s in best[1] if math.dist((s["x"], s["y"], s["z"]), pos) <= r]


def _cache_store(pos: Sequence[float], r: float, systems: List[dict], now: float) -> bool:
    """write the answer atomically; False (and nothing written) when the folder is a symlink, too full or not writable."""
    try:
        if CACHE_DIR.is_symlink():
            return False
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if sum(f.stat().st_size for f in CACHE_DIR.iterdir() if f.is_file()) > CACHE_MAX_BYTES:
            return False
        target = CACHE_DIR / f"sphere_{pos[0]:.3f}_{pos[1]:.3f}_{pos[2]:.3f}_{r:.1f}.json"
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"v": 1, "fetched": now, "systems": [_to_raw(s) for s in systems]}, f, ensure_ascii=False)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)   # only the temporary file this call made
            except OSError:
                pass
            raise
        return True
    except OSError:
        return False


def fetch_edsm(pos: Sequence[float], radius: float, now: Optional[float] = None) -> List[dict]:
    """[{'name', 'x', 'y', 'z', 'id64'}] of the systems EDSM has within radius ly (<= EDSM_MAX_RADIUS) of pos, as an EdsmRows (.origin says cache / network).
    A fresh cached sphere that contains the wanted one answers without a request; otherwise one request asks for EDSM_PREFETCH ly more (cached for the next jumps). If EDSM cannot be asked an old cache is
    used (flagged); with none, EdsmError."""
    pos = check_position(pos)
    now = time.time() if now is None else now
    r = min(float(radius), EDSM_MAX_RADIUS)
    hit = _cache_lookup(pos, r, True, now)
    if hit is not None:
        rows = EdsmRows(hit[1])
        rows.origin = f"キャッシュ({(now - hit[0]) / 3600:.1f} 時間前に取得。通信なし)"
        return rows
    big = min(r + EDSM_PREFETCH, EDSM_MAX_RADIUS)
    params = {"x": f"{pos[0]:.5f}", "y": f"{pos[1]:.5f}", "z": f"{pos[2]:.5f}", "radius": f"{big:.1f}", "showCoordinates": 1, "showId": 1}
    try:
        systems = _parse_systems(_http_get_json(f"{EDSM_URL}?{urllib.parse.urlencode(params)}"))
    except EdsmError as e:
        old = _cache_lookup(pos, r, False, now)
        if old is None:
            raise
        rows = EdsmRows(old[1])
        rows.origin = f"古いキャッシュ({(now - old[0]) / 3600:.0f} 時間前。EDSM に問い合わせできなかった: {e})"
        return rows
    systems = [s for s in systems if math.dist((s["x"], s["y"], s["z"]), pos) <= big]
    stored = _cache_store(pos, big, systems, now)
    rows = EdsmRows(s for s in systems if math.dist((s["x"], s["y"], s["z"]), pos) <= r)
    rows.origin = f"EDSM から取得({len(systems)} 系、半径 {big:g} ly" + ("、キャッシュに保存)" if stored else "、キャッシュには保存できなかった)")
    return rows


def edsm_system(name: str, now: Optional[float] = None) -> Optional[dict]:
    """{'name', 'pos', 'id64'} of one system by name from EDSM (api-v1/system), None when EDSM does not know it. Cached 24 h as system_<hash of the lower-cased name>.json (also a negative answer)."""
    key = clean_name(name, 200)
    if key is None:
        return None
    now = time.time() if now is None else now
    path = CACHE_DIR / f"system_{hashlib.sha256(key.lower().encode('utf-8')).hexdigest()[:32]}.json"
    old = None
    if _cache_usable() and path.is_file() and not path.is_symlink():
        try:
            if path.stat().st_size <= 65536:
                old = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(old, dict) and isinstance(old.get("fetched"), (int, float)) and not isinstance(old["fetched"], bool) and now - old["fetched"] <= CACHE_TTL:
                    return _system_from_cache(old)
        except (OSError, ValueError, RecursionError):
            old = None
    params = {"systemName": key, "showCoordinates": 1, "showId": 1}
    try:
        data = _http_get_json(f"{EDSM_SYSTEM_URL}?{urllib.parse.urlencode(params)}")
    except EdsmError:
        if isinstance(old, dict):
            return _system_from_cache(old)   # an old answer is better than none
        raise
    s = _parse_system(data) if isinstance(data, dict) else None
    if isinstance(data, dict) and s is None and data:
        raise EdsmError("EDSM の応答が想定外(システムの形ではない)")
    try:
        if not CACHE_DIR.is_symlink():
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"v": 1, "fetched": now, "system": None if s is None else _to_raw(s)}, f, ensure_ascii=False)
            os.replace(tmp, path)
    except OSError:
        pass
    return None if s is None else {"name": s["name"], "pos": (s["x"], s["y"], s["z"]), "id64": s["id64"]}


def _system_from_cache(d: dict) -> Optional[dict]:
    s = _parse_system(d.get("system"))
    return None if s is None else {"name": s["name"], "pos": (s["x"], s["y"], s["z"]), "id64": s["id64"]}


# ---------------------------------------------------------------- search
def gather(pos: Sequence[float], reach: float, edsm: Optional[List[dict]] = None):
    """the systems within `reach` ly of pos, without names (they are only worked out for the systems a search finally lists): (points (N, 3) array, id64 list (None = EDSM without id64),
    source list, EDSM name list (None where EDSM does not have the system)); the catalogue systems first, then the EDSM-only ones."""
    df = catalogue_box(pos, reach)
    xyz = np.column_stack([df["x"].to_numpy(float), df["y"].to_numpy(float), df["z"].to_numpy(float)]) if len(df) else np.empty((0, 3))
    keep = np.linalg.norm(xyz - np.asarray(pos, float), axis=1) <= reach
    xyz = xyz[keep]
    ids: List[Optional[int]] = [int(v) for v in df["id64"].to_numpy()[keep]]
    src = [SRC_CATALOG] * len(ids)
    ed_name: List[Optional[str]] = [None] * len(ids)
    extra: List[dict] = []
    if edsm:
        ed = {key32(s["x"], s["y"], s["z"]): s for s in edsm}
        keys = np.rint(xyz * 32).astype(np.int64)
        fold = lambda a: a[:, 0] * 1000003 + a[:, 1] * 10007 + a[:, 2]   # cheap hash to find the few matches without a Python loop over every point
        ed_keys = np.asarray(list(ed), dtype=np.int64).reshape(-1, 3)
        seen = set()
        for i in np.nonzero(np.isin(fold(keys), fold(ed_keys)))[0].tolist():
            k = tuple(int(v) for v in keys[i])
            if k in ed:
                src[i], ed_name[i] = SRC_BOTH, ed[k]["name"]
                seen.add(k)
        extra = [e for k, e in ed.items() if k not in seen and math.dist((e["x"], e["y"], e["z"]), pos) <= reach]
    if extra:
        xyz = np.vstack([xyz, np.asarray([[e["x"], e["y"], e["z"]] for e in extra], float)])
        ids += [int(e["id64"]) if e.get("id64") else None for e in extra]
        src += [SRC_EDSM] * len(extra)
        ed_name += [e["name"] for e in extra]
    return xyz, ids, src, ed_name


def daisy_chain(centre: Sequence[float], pts: np.ndarray, radius: float, hops: int, max_systems: int):
    """breadth-first jumps: hop 1 = points within `radius` of the centre, hop h = unvisited points within `radius` of a hop h-1 point.
    Returns (hop, parent, step) arrays (hop 0 = not reached or the centre system itself, parent = -1 for a hop-1 point, step = distance from the parent or the centre) and whether max_systems cut it short."""
    from scipy.spatial import cKDTree
    n = len(pts)
    hop = np.full(n, -1, dtype=int)
    parent = np.full(n, -1, dtype=int)
    step = np.zeros(n)
    if n == 0:
        return hop, parent, step, False
    tree = cKDTree(pts)
    taken = 0
    truncated = False
    for i in tree.query_ball_point(np.asarray(centre, float), 1e-6):   # the centre system itself
        hop[i] = 0
    frontier: List[Optional[int]] = [None]
    for h in range(1, hops + 1):
        origins = np.asarray([centre] if frontier == [None] else pts[frontier], float)
        found = tree.query_ball_point(origins, radius)
        nxt: List[int] = []
        for oi, js in enumerate(found):
            if len(js) > 1:   # nearest first, so that a cut by max_systems drops the far ones
                js = np.asarray(js)[np.argsort(np.linalg.norm(pts[js] - origins[oi], axis=1))].tolist()
            for j in js:
                if hop[j] != -1:
                    continue
                if taken >= max_systems:
                    truncated = True
                    break
                hop[j] = h
                parent[j] = -1 if frontier[oi] is None else frontier[oi]
                step[j] = float(np.linalg.norm(pts[j] - origins[oi]))
                nxt.append(j)
                taken += 1
            if truncated:
                break
        if truncated or not nxt:
            break
        frontier = nxt
    return hop, parent, step, truncated


def gather_edsm(pos: Sequence[float], reach: float, edsm: Sequence[dict]):
    """the EDSM systems within `reach` ly of pos like gather() (points, id64 list, source list, name list); two systems on the same 1/32 ly spot (EDSM has test systems on Sol's) count once, the first."""
    seen = set()
    pts, ids, names = [], [], []
    for s in edsm:
        k = key32(s["x"], s["y"], s["z"])
        if k in seen or math.dist((s["x"], s["y"], s["z"]), pos) > reach:
            continue
        seen.add(k)
        pts.append((s["x"], s["y"], s["z"]))
        ids.append(s.get("id64"))
        names.append(s["name"])
    return np.asarray(pts, float).reshape(-1, 3), ids, [SRC_EDSM] * len(ids), names


def search(where: str, radius: float = DEFAULT_RADIUS, hops: int = 1, source: str = SOURCE_EDSM, max_systems: int = DEFAULT_MAX,
           journal_dir: Path = JOURNAL_DIR, fetch: Callable[[Sequence[float], float], List[dict]] = fetch_edsm,
           centre: Optional[Centre] = None, system_fetch: Optional[Callable[[str], Optional[dict]]] = None) -> SearchResult:
    """resolve the point, gather the systems within radius * hops from `source` (edsm / catalog / both) and chain them. When EDSM cannot be asked the catalogue is used (a note says so)."""
    if source not in SOURCES:
        raise ValueError(f"ソースは {', '.join(SOURCES)} のどれか")
    if not (0 < radius <= MAX_RADIUS_INPUT and math.isfinite(radius)):
        raise ValueError(f"半径は 0 より大きく {MAX_RADIUS_INPUT:g} ly 以下")
    if not 1 <= hops <= MAX_HOPS_INPUT:
        raise ValueError(f"ジャンプ回数は 1 以上 {MAX_HOPS_INPUT:,} 以下")
    if not 1 <= max_systems <= MAX_SYSTEMS_INPUT:
        raise ValueError(f"上限は 1 以上 {MAX_SYSTEMS_INPUT:,} 以下")
    use_edsm = source != SOURCE_CATALOG
    c = centre or resolve_centre(where, journal_dir, use_edsm, system_fetch)
    c = Centre(check_position(c.pos), clean_name(c.name), c.id64, c.how, c.approximate)
    reach = radius * hops
    notes: List[str] = []
    info: List[str] = []
    ed: Optional[List[dict]] = None
    fallback = False
    if use_edsm:
        try:
            ed = fetch(c.pos, min(reach, EDSM_MAX_RADIUS))
            origin = getattr(ed, "origin", "")
            if origin:
                info.append(f"EDSM: {origin}")
            if reach > EDSM_MAX_RADIUS:
                notes.append(f"EDSM は中心から {EDSM_MAX_RADIUS:g} ly までしか取得していない(半径×n = {reach:,.0f} ly)。その先へは伸びない" + ("" if source == SOURCE_BOTH else "(カタログなら --source catalog / both)"))
        except EdsmError as e:
            ed = None
            fallback = source == SOURCE_EDSM
            notes.append(f"EDSM: {e}。" + ("カタログで代用した" if fallback else "カタログだけで表示している"))
    use_cat = (source in (SOURCE_CATALOG, SOURCE_BOTH) or fallback) and catalogue_available()
    if source == SOURCE_CATALOG and not catalogue_available():
        raise ValueError("Local catalog (data/full_catalog) is not installed. Please use EDSM mode.")
    if fallback and not catalogue_available():
        notes.append("Local catalog (data/full_catalog) is not installed; fallback unavailable.")
    if use_cat:
        area = fit_reach(c.pos, reach)
        if area < reach:
            notes.append(f"カタログは中心から {area:g} ly の立方体までしか読んでいない(半径×n = {reach:,.0f} ly は系が多すぎる。上限 {MAX_BOX_ROWS:,} 系)。その外側へは伸びない")
        pts, ids, srcs, ed_names = gather(c.pos, area, ed)
    else:
        area = min(reach, EDSM_MAX_RADIUS)
        pts, ids, srcs, ed_names = gather_edsm(c.pos, area, ed or [])
    hop, parent, step, truncated = daisy_chain(c.pos, pts, radius, hops, max_systems)
    if truncated:
        notes.append(f"上限 {max_systems} 系で打ち切った(--max で変えられる)。後ろのジャンプは一部しか出ていない")
    if c.approximate:
        notes.append("中心の座標が近似(ボクセルの中心)。半径内の顔ぶれは実際の位置とずれ得る")
    reached = [int(i) for i in np.nonzero(hop >= 0)[0]]
    db_names = db_names_in_box(c.pos, area) if use_cat and reached else {}
    names = {i: clean_name(db_names.get(key32(*pts[i]))) or ed_names[i] or (procedural_name(ids[i]) if ids[i] else "(名前なし)") for i in reached}   # only the listed systems get a name
    rows = []
    for i in reached:
        rows.append({"hop": int(hop[i]), "name": names[i], "x": float(pts[i][0]), "y": float(pts[i][1]), "z": float(pts[i][2]), "id64": ids[i], "src": srcs[i],
                     "step": float(step[i]), "dist": math.dist(pts[i], c.pos), "parent": names[int(parent[i])] if parent[i] >= 0 else ("中心" if hop[i] else "")})
    rows.sort(key=lambda r: (r["hop"], r["step"]))
    return SearchResult(c, rows, notes, truncated, info)


SOURCE_LABEL = {SOURCE_EDSM: "EDSM", SOURCE_CATALOG: "カタログ(オフライン)", SOURCE_BOTH: "カタログ + EDSM"}


def render(res: SearchResult, radius: float, hops: int, source: str) -> str:
    c = res.centre
    lines = [f"中心 {c.name or '(座標)'}  ({c.pos[0]:.4f}, {c.pos[1]:.4f}, {c.pos[2]:.4f}) ly" + (f"  id64 {c.id64}" if c.id64 else "") + f"  [{c.how}]",
             f"半径 {radius:g} ly × {hops} ジャンプ  ソース: {SOURCE_LABEL.get(source, source)}"]
    lines += [f"取得: {i}" for i in res.info]
    counts = dict(sorted(Counter(r["hop"] for r in res.rows).items()))   # only the hops that exist (n can be huge)
    lines.append("系の数: " + "、".join(f"{'中心' if h == 0 else f'{h} ジャンプ目'} {n}" for h, n in counts.items()) + f"  (合計 {sum(counts.values())})")
    lines.append(f"{'hop':>3}  {'直前から':>8} {'中心から':>8}  {'名前':<34}{'直前の系':<30}{'出どころ':<7}{'x':>12}{'y':>12}{'z':>12}  id64")
    for r in res.rows:
        lines.append(f"{r['hop']:>3}  {r['step']:>8.2f} {r['dist']:>8.2f}  {r['name']:<34}{r['parent']:<30}{r['src']:<7}{r['x']:>12.4f}{r['y']:>12.4f}{r['z']:>12.4f}  {r['id64'] or ''}")
    for note in res.notes:
        lines.append(f"注意: {note}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ある地点から 15 ly 以内の星系(と、ジャンプを n 回つなげた範囲)を一覧する")
    ap.add_argument("where", nargs="?", default="", help="空 = ジャーナルの現在地 / 'x,y,z' / id64 / 星系名")
    ap.add_argument("--radius", "-r", type=float, default=DEFAULT_RADIUS, help="1 ジャンプの半径 ly(既定 15)")
    ap.add_argument("--hops", "-n", type=int, default=1, help="ジャンプ回数 n(既定 1)。各ジャンプ先からさらに半径内を探す")
    ap.add_argument("--source", choices=SOURCES, default=SOURCE_EDSM, help="系を取ってくる元(既定 edsm。catalog = ローカルのカタログ、オフライン可。both = 両方)")
    ap.add_argument("--max", type=int, default=DEFAULT_MAX, help=f"出す系の上限(既定 {DEFAULT_MAX})")
    ap.add_argument("--journal-dir", default=str(JOURNAL_DIR))
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        res = search(args.where, args.radius, args.hops, args.source, args.max, Path(args.journal_dir))
    except (ValueError, OSError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    print(render(res, args.radius, args.hops, args.source))
    return 0


if __name__ == "__main__":
    sys.exit(main())
