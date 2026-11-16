# -*- coding: utf-8 -*-
"""nearby_systems_gui.py: Tkinter front end for nearby_systems.py: the systems within R ly of a point, and a route that is built jump by jump.

Start: leave 現在地 empty (or press 'ジャーナルの現在地') for the commander's last journal position, or type 'x,y,z', an id64 or a system name, and press 検索. That system is the first stop of the 経路 and the
lower table lists the systems within 半径 (one jump, default 15 ly) of the route's last stop; ジャンプ回数 n (default 1, any number) chains n jumps (each hop = the systems within the radius of the previous
hop's systems), so a large n lists a lot: 戻る / 全リセット are the way out. 'Omit mode' (前の基準と重なる系を省く) hides the systems within the radius of an earlier stop of the route (they are
already covered from there). 最小距離 = the nearest distance listed (the table is filtered by it, no new search), 上限 = the most systems listed.
Double-click a system in the table: it is added to the end of the route (with the systems that lead to it when it is a hop 2+ system) and the table now lists what can be reached from it, the route
before it stays. 戻る undoes the last step (a search or an added system), 全リセット clears the route, the table and the inputs. 検索 with an unchanged 現在地 text asks again around the last stop of the
route (new radius / n / source) and keeps the route; changing the 現在地 text and pressing 検索 starts a new route. 取得元 chooses where the systems come from: EDSM (default; answers are cached on
disk and requests are rate limited, see nearby_systems.py), the local catalogue (offline) or both; if EDSM cannot be asked the catalogue is used, with a note. Clicking a 名前 cell (in either table) copies the name. The systems already on the route (before its last stop) are never listed as candidates.
経路の最後を削除 drops the last stop of the route (the first one stays) and lists what is within reach of the new last stop; 戻る undoes it.
経路を保存(CSV) writes the route (no, name, id64, x, y, z, leg_ly, total_ly; one row per stop) to a file chosen in a dialog (it asks before overwriting); 経路を読み込み(CSV) makes a saved route
the route again (the legs are recomputed from the coordinates) and lists what is within reach of its last stop; 戻る goes back to what was shown before the load. A route CSV is checked on loading (size, row
count, numbers, names cleaned) and a name that could be taken for a spreadsheet formula is written with a leading apostrophe.

All logic is in nearby_systems.py (no Tk), so it can be tested headless.

Usage (from imitate_forge/):
    python nearby_systems_gui.py [where] [--radius 15] [--hops 1]
"""
import argparse
import csv
import math
import os
import re
import threading
import tkinter as tk
from collections import Counter
from tkinter import filedialog, ttk
from typing import Dict, List, Optional, Sequence

from i18n import set_lang, tr
import nearby_systems as ns
import sphere_tour

def get_columns():
    return [("hop", "hop", 40, "center"), ("name", tr("Name", "名前"), 230, "w"), ("step", tr("From Prev", "直前から"), 70, "e"), ("dist", tr("From Origin", "中心から"), 70, "e"), ("parent", tr("Via System", "直前の系"), 190, "w"),
            ("src", tr("Source", "出どころ"), 70, "center"), ("x", "x", 90, "e"), ("y", "y", 90, "e"), ("z", "z", 90, "e"), ("id64", "id64", 130, "e")]


COLUMNS = get_columns()
SORT_KEYS = ("x", "y", "z")   # the columns whose header sorts the table (click: ascending, again: descending, again: the original nearest-first order)
CSV_HEADER = ["no", "name", "id64", "x", "y", "z", "leg_ly", "total_ly"]   # the route CSV: one row per stop; only name (or id64) and x y z are needed to load it
CSV_MAX_BYTES, CSV_MAX_STOPS = 2 * 1024 * 1024, 5000   # a route CSV larger than this, or with more stops, is refused (a hostile or broken file must not exhaust memory)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(text: str) -> str:
    """a name a spreadsheet could run as a formula (starts with = + - @ ...) gets a leading apostrophe, the usual CSV-injection guard."""
    return "'" + text if text[:1] in _FORMULA_START else text


def csv_unsafe(text: str) -> str:
    return text[1:] if text[:1] == "'" and text[1:2] in _FORMULA_START else text


def get_route_columns():
    return [("no", "#", 40, "center"), ("name", tr("Name", "名前"), 230, "w"), ("leg", tr("Leg ly", "区間 ly"), 80, "e"), ("total", tr("Total ly", "累計 ly"), 80, "e"), ("id64", "id64", 130, "e")]


ROUTE_COLUMNS = get_route_columns()


class NearbySystemsApp(ttk.Frame):
    def __init__(self, master, where: str = "", radius: float = ns.DEFAULT_RADIUS, hops: int = 1):
        super().__init__(master, padding=8)
        self.master.title(tr("Nearby Systems — Systems within R ly & Jump Route Builder", "Nearby Systems — 16 ly 以内の星系と、ジャンプで伸ばす経路"))
        self.pack(fill="both", expand=True)
        self.busy = False
        self.where_var = tk.StringVar(value=where)
        self.radius_var = tk.StringVar(value=f"{radius:g}")
        self.hops_var = tk.StringVar(value=str(hops))
        self.max_var = tk.StringVar(value=str(ns.DEFAULT_MAX))
        self.min_var = tk.StringVar(value="0")
        self.omit_var = tk.BooleanVar(value=False)
        self.source_var = tk.StringVar(value=ns.SOURCE_EDSM)
        self.status = tk.StringVar(value=tr("Enter origin and press Enter (empty = journal pos). Default source: EDSM (cached & rate-limited).", "現在地を入れて Enter(空 = ジャーナルの現在地)。取得元は既定で EDSM(キャッシュあり・間隔を空けて問い合わせる)"))
        self._result = None
        self.rows: List[dict] = []
        self.route: List[dict] = []                    # stops {name, pos, id64, leg} from the start to the last one; the table lists what is within reach of the last
        self.current: Optional[ns.SearchResult] = None  # the displayed table
        self.params = (ns.DEFAULT_RADIUS, 1, ns.SOURCE_EDSM)    # (radius, n, source) of the displayed table
        self.sort: Optional[tuple] = None              # (column key, descending) of the table, None = the original order (hop, distance); kept across searches
        self.start_text = ""                           # the 現在地 text that started the route
        self.history: list = []
        self.tour_manager = sphere_tour.SphereTourManager()
        self.jump_monitor = sphere_tour.FSDJumpMonitor()
        self.tour_status_var = tk.StringVar(value=tr("Tour: Idle", "巡回: 停止中"))
        self.tour_autocopy_var = tk.BooleanVar(value=True)
        self._tour_poll_after_id = None
        self._last_auto_copied_token: Optional[str] = None                        # snapshots (route, current, params, start_text), oldest first; 戻る pops the last
        self._build()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text=tr("Origin", "現在地")).grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(top, textvariable=self.where_var, width=40)
        entry.grid(row=0, column=1, columnspan=3, sticky="w", padx=(4, 8))
        entry.bind("<Return>", lambda _e: self.start_search())
        entry.focus_set()
        ttk.Button(top, text=tr("Journal Pos", "ジャーナルの現在地"), command=self.use_journal).grid(row=0, column=4, columnspan=2, sticky="w")
        ttk.Label(top, text=tr("Radius ly", "半径 ly")).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(top, textvariable=self.radius_var, width=7).grid(row=1, column=1, sticky="w", padx=(4, 8), pady=(6, 0))
        ttk.Label(top, text=tr("Hops n", "ジャンプ回数 n")).grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Entry(top, textvariable=self.hops_var, width=6).grid(row=1, column=3, sticky="w", padx=(4, 8), pady=(6, 0))
        ttk.Label(top, text=tr("Limit", "上限")).grid(row=1, column=4, sticky="w", pady=(6, 0))
        ttk.Entry(top, textvariable=self.max_var, width=7).grid(row=1, column=5, sticky="w", padx=(4, 8), pady=(6, 0))
        ttk.Label(top, text=tr("Min Dist ly", "最小距離 ly")).grid(row=1, column=6, sticky="w", pady=(6, 0))
        mind = ttk.Entry(top, textvariable=self.min_var, width=7)
        mind.grid(row=1, column=7, sticky="w", padx=(4, 8), pady=(6, 0))
        mind.bind("<Return>", lambda _e: self.apply_filter())
        mind.bind("<FocusOut>", lambda _e: self.apply_filter())
        ttk.Checkbutton(top, text=tr("Omit systems covered by earlier route stops", "前の基準と重なる系を省く(最後の系より前の経路の系の半径内にある系を表から外す)"), variable=self.omit_var,
                        command=self.apply_filter).grid(row=2, column=0, columnspan=8, sticky="w", pady=(6, 0))
        src = ttk.Frame(top)
        src.grid(row=3, column=0, columnspan=8, sticky="w", pady=(6, 0))
        ttk.Label(src, text=tr("Source", "取得元")).pack(side="left", padx=(0, 8))
        for value, label in ((ns.SOURCE_EDSM, "EDSM"), (ns.SOURCE_CATALOG, tr("Catalog (Offline)", "カタログ(オフライン)")), (ns.SOURCE_BOTH, tr("Both", "両方"))):
            ttk.Radiobutton(src, text=label, value=value, variable=self.source_var).pack(side="left", padx=(0, 10))
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=(6, 0))
        self.button = ttk.Button(buttons, text=tr("Search", "検索"), command=self.start_search)
        self.button.pack(side="left")
        ttk.Button(buttons, text=tr("Copy Name", "名前をコピー"), command=self.copy_selected).pack(side="left", padx=6)
        self.back_button = ttk.Button(buttons, text=tr("Back", "戻る"), command=self.go_back, state="disabled")
        self.back_button.pack(side="left", padx=(18, 6))
        self.reset_button = ttk.Button(buttons, text=tr("Reset All", "全リセット"), command=self.reset_all)
        self.reset_button.pack(side="left")
        self.delete_button = ttk.Button(buttons, text=tr("Drop Last Stop", "経路の最後を削除"), command=self.delete_last)
        self.delete_button.pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text=tr("Save Route (CSV)", "経路を保存(CSV)"), command=self.save_route).pack(side="left", padx=(18, 6))
        self.load_button = ttk.Button(buttons, text=tr("Load Route (CSV)", "経路を読み込み(CSV)"), command=self.load_route)
        self.load_button.pack(side="left")

        tour_bar = ttk.Frame(self)
        tour_bar.pack(fill="x", pady=(4, 0))
        self.tour_start_button = ttk.Button(tour_bar, text=tr("Start Sphere Tour (Wait FSDJump)", "15ly巡回開始 (FSDJump待機)"), command=self.start_sphere_tour)
        self.tour_start_button.pack(side="left")
        self.tour_copy_button = ttk.Button(tour_bar, text=tr("Copy Next", "次の星系をコピー"), command=self.copy_tour_target)
        self.tour_copy_button.pack(side="left", padx=(6, 2))
        self.tour_skip_button = ttk.Button(tour_bar, text=tr("Skip", "スキップ"), command=self.skip_tour_target)
        self.tour_skip_button.pack(side="left", padx=2)
        self.tour_stop_button = ttk.Button(tour_bar, text=tr("Stop", "停止"), command=self.stop_sphere_tour)
        self.tour_stop_button.pack(side="left", padx=2)
        ttk.Checkbutton(tour_bar, text=tr("Auto-copy", "自動コピー"), variable=self.tour_autocopy_var).pack(side="left", padx=(8, 2))
        ttk.Label(tour_bar, textvariable=self.tour_status_var, foreground="#0a58ca").pack(side="left", padx=(10, 4))

        self.info = tk.Text(self, height=4, wrap="word")
        self.info.pack(fill="x", pady=(6, 0))
        ttk.Label(self, text=tr("Route (Origin -> ... -> Last Stop; table shows reachable systems)", "経路(出発点 → … → 最後の系。下の表は最後の系から届く系)")).pack(fill="x", pady=(6, 0))
        rframe = ttk.Frame(self)
        rframe.pack(fill="x")
        self.route_tree = ttk.Treeview(rframe, columns=[c[0] for c in ROUTE_COLUMNS], show="headings", height=5, selectmode="browse")
        for key, head, width, anchor in ROUTE_COLUMNS:
            self.route_tree.heading(key, text=head)
            self.route_tree.column(key, width=width, anchor=anchor)
        rscroll = ttk.Scrollbar(rframe, orient="vertical", command=self.route_tree.yview)
        self.route_tree.configure(yscrollcommand=rscroll.set)
        self.route_tree.bind("<ButtonRelease-1>", lambda e: self.on_click(e, self.route_tree, self.route))
        self.route_tree.pack(side="left", fill="x", expand=True)
        rscroll.pack(side="right", fill="y")

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, pady=6)
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in COLUMNS], show="headings", height=16, selectmode="browse")
        for key, head, width, anchor in COLUMNS:
            self.tree.heading(key, text=head, command=(lambda k=key: self.toggle_sort(k)) if key in SORT_KEYS else "")
            self.tree.column(key, width=width, anchor=anchor)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<ButtonRelease-1>", lambda e: self.on_click(e, self.tree, self.rows))
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        ttk.Label(self, textvariable=self.status, wraplength=1100).pack(fill="x")

    def use_journal(self):
        self.where_var.set("")
        self.start_search(new=True)

    # ---- search (worker thread: the catalogue scan and EDSM are not instant)
    def _read_params(self):
        try:
            radius = float(self.radius_var.get())
            hops = int(self.hops_var.get())
            max_systems = int(self.max_var.get())
            if not (0 < radius <= ns.MAX_RADIUS_INPUT) or not 1 <= hops <= ns.MAX_HOPS_INPUT or not 1 <= max_systems <= ns.MAX_SYSTEMS_INPUT:
                raise ValueError
        except ValueError:
            self.status.set(tr("Radius in (0, {mr:g}], hops in 1..{mh:,}, limit in 1..{ml:,}.", "半径は 0 より大きく {mr:g} 以下、ジャンプ回数は 1〜{mh:,}、上限は 1〜{ml:,} の整数で入れる", mr=ns.MAX_RADIUS_INPUT, mh=ns.MAX_HOPS_INPUT, ml=ns.MAX_SYSTEMS_INPUT))
            return None
        return radius, hops, max_systems

    @staticmethod
    def _stop_centre(stop: dict) -> ns.Centre:
        return ns.Centre(tuple(stop["pos"]), stop["name"], stop["id64"], tr("Last route stop", "経路の最後の系"))  # type: ignore[arg-type]

    def start_search(self, new: bool = False):
        """検索: with an unchanged 現在地 text and a route, ask again around the route's last stop (route kept); otherwise start a new route from the text."""
        if self.busy:
            return
        p = self._read_params()
        if p is None:
            return
        text = self.where_var.get()
        if self.route and not new and text.strip() == self.start_text.strip():
            self._launch("refresh", "", self._stop_centre(self.route[-1]), p, None, text)
        else:
            self._launch("new", text, None, p, None, text)

    def _launch(self, mode, where, centre, p, new_route, text):
        radius, hops, max_systems = p
        source = self.source_var.get()
        if source not in ns.SOURCES:
            source = ns.SOURCE_EDSM
        self.busy = True
        self._update_buttons()
        self.status.set(tr("Searching…", "検索中…") + (tr(" (querying EDSM if uncached)", "(EDSM に問い合わせ中。キャッシュに無いときだけ通信する)") if source != ns.SOURCE_CATALOG else ""))
        self._result = None
        self._action = (mode, (radius, hops, source), new_route, text)
        threading.Thread(target=self._worker, args=(where, radius, hops, source, max_systems, centre), daemon=True).start()
        self.after(100, self._poll)

    def _worker(self, where, radius, hops, source, max_systems, centre):
        try:
            self._result = (ns.search(where, radius, hops, source, max_systems, centre=centre), None)
        except Exception as e:   # shown in the status line
            self._result = (None, e)

    def _poll(self):
        """the worker only stores its result; the Tk thread picks it up (no Tk call from the worker thread)."""
        if self._result is None:
            self.after(100, self._poll)
        else:
            self._done(*self._result)

    def _update_buttons(self):
        state = ["disabled"] if self.busy else ["!disabled"]
        for b in (self.button, self.reset_button, self.load_button, self.delete_button):
            b.state(state)
        self.back_button.state(["!disabled"] if self.history and not self.busy else ["disabled"])

    def _done(self, res: Optional[ns.SearchResult], error):
        self.busy = False
        if error is not None:
            self._update_buttons()
            self.status.set(tr("Error: {error}", "エラー: {error}", error=error))
            return
        mode, params, new_route, text = self._action
        if self.current is not None:   # what is replaced can be come back to with 戻る
            self.history.append((list(self.route), self.current, self.params, self.start_text))
        if mode == "new":
            c = res.centre
            self.route = [{"name": c.name or tr("(Coords)", "(座標)"), "pos": c.pos, "id64": c.id64, "leg": 0.0}]
            self.start_text = text
        elif mode in ("extend", "load"):
            self.route = new_route
            if mode == "load":   # the loaded route's first stop becomes the route's start text, so 検索 afterwards asks again around the last stop
                self.start_text = text
                self.where_var.set(text)
        self.current, self.params = res, params
        self._update_buttons()
        self._show()

    def _show(self):
        """display the route and the table of self.current (a new result or one restored by 戻る; nothing is searched again)."""
        res, (radius, hops, source) = self.current, self.params
        lo = self._min_distance()
        in_route = {ns.key32(*s["pos"]) for s in self.route[:-1]}   # the stops before the last one are never listed as candidates (the last one is the table's centre, hop 0)
        listed = [r for r in res.rows if r["hop"] == 0 or ns.key32(r["x"], r["y"], r["z"]) not in in_route]
        on_route = len(res.rows) - len(listed)
        self.rows = [r for r in listed if r["dist"] >= lo]   # 最小距離 filters the displayed rows only (the centre system itself drops out above 0)
        omitted = 0
        if self.omit_var.get() and len(self.route) > 1:   # 省くモード: systems within the radius of an earlier stop of the route are already covered from there
            earlier = [s["pos"] for s in self.route[:-1]]
            kept = [r for r in self.rows if r["hop"] == 0 or all(math.dist((r["x"], r["y"], r["z"]), p) > radius for p in earlier)]
            omitted = len(self.rows) - len(kept)
            self.rows = kept
        if self.sort:
            self.rows.sort(key=lambda r: r[self.sort[0]], reverse=self.sort[1])   # the tree's row ids are positions in self.rows, so sorting it sorts the table
        c = res.centre
        self.info.delete("1.0", "end")
        self.info.insert("1.0", tr("Center {name}  ({x:.4f}, {y:.4f}, {z:.4f}) ly", "候補の中心 {name}  ({x:.4f}, {y:.4f}, {z:.4f}) ly", name=c.name or tr("(Coords)", "(座標)"), x=c.pos[0], y=c.pos[1], z=c.pos[2]) + (f"  id64 {c.id64}" if c.id64 else "") + f"  [{c.how}]\n" +
                         tr("Radius {r:g} ly x {h} hop(s), dist {lo:g}-{r:g} ly  Source: {src}", "半径 {r:g} ly × {h} ジャンプ、距離 {lo:g}〜{r:g} ly  取得元: {src}", r=radius, h=hops, lo=lo, src=ns.SOURCE_LABEL.get(source, source)) +
                         (tr("  Omitted overlapped: {o}", "  前の基準と重なる系を省略: {o} 系", o=omitted) if self.omit_var.get() else "") + (tr("  Excluded in route: {r}", "  経路に入っている系を除外: {r} 系", r=on_route) if on_route else "") +
                         "".join("\n" + i for i in res.info))
        self.route_tree.delete(*self.route_tree.get_children())
        total = 0.0
        for n, s in enumerate(self.route):
            total += s["leg"]
            self.route_tree.insert("", "end", iid=str(n), values=[n + 1, s["name"], f"{s['leg']:.2f}" if n else "", f"{total:.2f}" if n else "", s["id64"] or ""])
        if self.route:
            self.route_tree.see(str(len(self.route) - 1))
        self.tree.delete(*self.tree.get_children())
        for n, r in enumerate(self.rows):
            self.tree.insert("", "end", iid=str(n), values=[r["hop"], r["name"], f"{r['step']:.2f}", f"{r['dist']:.2f}", r["parent"], r["src"],
                                                            f"{r['x']:.4f}", f"{r['y']:.4f}", f"{r['z']:.4f}", r["id64"] or ""])
        shown = [r for r in self.rows if r["hop"] > 0]
        counts = dict(sorted(Counter(r["hop"] for r in listed if r["hop"] > 0).items()))   # only the hops that exist (n can be huge)
        msg = ("経路 " + " → ".join(s["name"] for s in self.route) + f"(全長 {total:.2f} ly、{max(len(self.route) - 1, 0)} ジャンプ)\n" if self.route else "") + \
            "候補: " + "、".join(f"{h} ジャンプ目 {n}" for h, n in counts.items()) + f"  (合計 {sum(counts.values())}" + (f"、距離 {lo:g} ly 以上の {len(shown)} 系を表示" if lo > 0 else "") + \
            ")   名前クリック=コピー、ダブルクリック=経路に加える"
        if res.notes:
            msg += "\n注意: " + " / ".join(res.notes)
        self.status.set(msg)

    def delete_last(self):
        """drop the route's last stop; the table lists what is within reach of the new last stop. 戻る undoes it."""
        if self.busy:
            return
        if len(self.route) < 2:
            self.status.set(tr("Nothing to drop (origin stays; use Reset All to clear).", "削除できる系が無い(経路の最初の系は残る。全て消すなら全リセット)"))
            return
        p = self._read_params()
        if p is None:
            return
        shorter = self.route[:-1]
        self._launch("extend", "", self._stop_centre(shorter[-1]), p, shorter, self.start_text)

    # ---- 経路の CSV 保存 / 読み込み
    def write_route(self, path: str):
        """one row per stop: no, name, id64, x, y, z, leg_ly (from the previous stop), total_ly."""
        total = 0.0
        with open(path, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig so that Excel shows the names right
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            for n, s in enumerate(self.route, 1):
                total += s["leg"]
                w.writerow([n, csv_safe(s["name"]), s["id64"] or "", repr(float(s["pos"][0])), repr(float(s["pos"][1])), repr(float(s["pos"][2])), f"{s['leg']:.4f}", f"{total:.4f}"])

    @staticmethod
    def read_route(path: str) -> list:
        """the stops of a route CSV ({name, pos, id64, leg}); the legs are worked out again from the coordinates. Raises ValueError with a readable message."""
        try:
            if os.path.getsize(path) > CSV_MAX_BYTES:
                raise ValueError(tr("File too large (max {mb} MB)", "大きすぎる(上限 {mb} MB)", mb=CSV_MAX_BYTES // 1024 // 1024))
            with open(path, newline="", encoding="utf-8-sig") as f:
                rows = []
                for r in csv.DictReader(f):
                    rows.append(r)
                    if len(rows) > CSV_MAX_STOPS:
                        raise ValueError(tr("Too many stops (max {m})", "系が多すぎる(上限 {m})", m=CSV_MAX_STOPS))
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            raise ValueError(tr("Unreadable: {e}", "読めない: {e}", e=e))
        stops: list = []
        for ln, r in enumerate(rows, 2):
            try:
                pos = ns.check_position((r["x"], r["y"], r["z"]))
            except (KeyError, TypeError, ValueError):
                raise ValueError(tr("Line {ln}: missing/non-numeric x, y, z (cols: {cols})", "{ln} 行目: x, y, z の列が無いか、数値ではない(列: {cols})", ln=ln, cols=", ".join(CSV_HEADER)))
            id64 = (r.get("id64") or "").strip()
            if id64 and not re.fullmatch(r"[0-9]{1,19}", id64):
                raise ValueError(tr("Line {ln}: id64 is not an integer", "{ln} 行目: id64 が数字ではない", ln=ln))
            if id64 and not 0 < int(id64) < 2 ** 63:
                raise ValueError(tr("Line {ln}: id64 out of range", "{ln} 行目: id64 が範囲外", ln=ln))
            stops.append({"name": ns.clean_name(csv_unsafe((r.get("name") or "").strip())) or tr("(Coords)", "(座標)"), "pos": pos, "id64": int(id64) if id64 else None,
                          "leg": math.dist(pos, stops[-1]["pos"]) if stops else 0.0})
        if not stops:
            raise ValueError(tr("Route is empty (no rows)", "経路が空(行が無い)"))
        return stops

    def save_route(self):
        if not self.route:
            self.status.set(tr("No route to save (search first).", "保存する経路が無い(先に検索する)"))
            return
        safe = lambda s: re.sub(r'[\\/:*?"<>|\s]+', "_", s)[:40]
        path = filedialog.asksaveasfilename(title=tr("Save Route", "経路を保存"), defaultextension=".csv", filetypes=[("CSV", "*.csv"), (tr("All", "すべて"), "*.*")],
                                            initialfile=f"route_{safe(self.route[0]['name'])}_to_{safe(self.route[-1]['name'])}.csv")   # the dialog asks before overwriting
        if not path:
            return
        try:
            self.write_route(path)
        except OSError as e:
            self.status.set(tr("Save failed: {e}", "保存できない: {e}", e=e))
            return
        self.status.set(tr("Saved {n}-stop route: {path}", "経路 {n} 系を保存した: {path}", n=len(self.route), path=path))

    def load_route(self, path: Optional[str] = None):
        """read a route CSV and make it the route; the table lists what is within reach of its last stop (catalogue only, 戻る goes back to what was shown)."""
        if self.busy:
            return
        path = path or filedialog.askopenfilename(title=tr("Load Route", "経路を読み込み"), filetypes=[("CSV", "*.csv"), (tr("All", "すべて"), "*.*")])
        if not path:
            return
        p = self._read_params()
        if p is None:
            return
        try:
            stops = self.read_route(path)
        except ValueError as e:
            self.status.set(tr("Load failed: {e}", "読み込めない: {e}", e=e))
            return
        first = stops[0]
        text = str(first["id64"]) if first["id64"] else f"{first['pos'][0]!r},{first['pos'][1]!r},{first['pos'][2]!r}"
        self._launch("load", "", self._stop_centre(stops[-1]), p, stops, text)

    def toggle_sort(self, key: str):
        """x / y / z header click: ascending, then descending, then back to the original order."""
        if self.sort and self.sort[0] == key:
            self.sort = (key, True) if not self.sort[1] else None
        else:
            self.sort = (key, False)
        self._mark_headings()
        if self.current is not None and not self.busy:
            self._show()

    def _mark_headings(self):
        for k, head, _w, _a in COLUMNS:
            if k in SORT_KEYS:
                mark = " ▲" if self.sort == (k, False) else " ▼" if self.sort == (k, True) else ""
                self.tree.heading(k, text=head + mark)

    def _min_distance(self) -> float:
        try:
            return max(float(self.min_var.get() or 0), 0.0)
        except ValueError:
            return 0.0

    def apply_filter(self):
        """最小距離 changed: redraw the table from the displayed result (no new search)."""
        if self.current is not None and not self.busy:
            self._show()

    # ---- 戻る / 全リセット
    def go_back(self):
        """restore the previous route and table with their inputs (no new search, so an EDSM result stays as it was)."""
        if self.busy or not self.history:
            return
        self.route, self.current, self.params, self.start_text = self.history.pop()
        radius, hops, source = self.params
        self.where_var.set(self.start_text)
        self.radius_var.set(f"{radius:g}")
        self.hops_var.set(str(hops))
        self.source_var.set(source)
        self._update_buttons()
        self._show()

    def reset_all(self):
        """forget the route, the history and the table and put the inputs back to their defaults."""
        if self.busy:
            return
        self.history.clear()
        self.route = []
        self.current = None
        self.rows = []
        self.start_text = ""
        self.where_var.set("")
        self.radius_var.set(f"{ns.DEFAULT_RADIUS:g}")
        self.hops_var.set("1")
        self.max_var.set(str(ns.DEFAULT_MAX))
        self.min_var.set("0")
        self.omit_var.set(False)
        self.source_var.set(ns.SOURCE_EDSM)
        self.sort = None
        self._mark_headings()
        self.tree.delete(*self.tree.get_children())
        self.route_tree.delete(*self.route_tree.get_children())
        self.info.delete("1.0", "end")
        self._update_buttons()
        self.status.set(tr("Reset. Enter origin and press Enter (empty = journal pos).", "リセットした。現在地を入れて Enter(空 = ジャーナルの現在地)"))

    # ---- click = copy the name, double click = add to the route
    def on_click(self, event, tree, items):
        if tree.identify_region(event.x, event.y) == "cell" and tree.identify_column(event.x) == "#2":
            row = tree.identify_row(event.y)
            if row:
                self.copy_name(items[int(row)]["name"])

    def on_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row or self.busy or self.tree.identify_region(event.x, event.y) != "cell":
            return
        p = self._read_params()
        if p is None:
            return
        r = self.rows[int(row)]
        if r["hop"] == 0:
            self.status.set(tr("That is the last stop of the route (current center).", "その系は、経路の最後の系(候補の中心)"))
            return
        by_name: Dict[str, dict] = {x["name"]: x for x in self.current.rows}   # not self.rows: the parents may be hidden by a filter
        chain = [r]   # a hop 2+ system is reached through the systems that found it: add them too
        while chain[-1]["hop"] > 1 and chain[-1]["parent"] in by_name:
            chain.append(by_name[chain[-1]["parent"]])
        chain.reverse()
        in_route = {s["name"] for s in self.route}
        if any(c["name"] in in_route for c in chain):
            self.status.set(tr("Cannot add a system already in the route.", "経路に入っている系は加えられない"))
            return
        stops = [{"name": c["name"], "pos": (c["x"], c["y"], c["z"]), "id64": c["id64"], "leg": c["step"]} for c in chain]
        centre = ns.Centre((r["x"], r["y"], r["z"]), r["name"], r["id64"], tr("Added to route", "経路に加えた系"))
        self._launch("extend", "", centre, p, self.route + stops, self.start_text)

    def copy_selected(self):
        sel = self.tree.selection()
        if sel:
            self.copy_name(self.rows[int(sel[0])]["name"])
            return
        sel = self.route_tree.selection()
        if sel:
            self.copy_name(self.route[int(sel[0])]["name"])

    def copy_name(self, name: str):
        self.clipboard_clear()
        self.clipboard_append(name)
        self.status.set(tr("Copied: {name}", "コピーした: {name}", name=name))


    def _copy_text_to_clipboard(self, text: str) -> None:
        if not text:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
        except Exception:
            pass

    def start_sphere_tour(self):
        self.stop_sphere_tour(silent=True)
        try:
            radius = min(max(0.1, float(self.radius_var.get())), 15.0)
        except Exception:
            radius = 15.0

        # If search results already exist, arm directly from current center & rows
        if self.current is not None and getattr(self, "rows", None):
            origin = tuple(self.current["origin"].pos)
            self._arm_tour_with_systems(origin, radius, list(self.rows))
            return

        # Otherwise resolve & fetch sphere around input/journal origin in background
        where = self.where_var.get().strip()
        source = self.source_var.get()
        self.tour_status_var.set(tr("Tour: Loading sphere systems…", "巡回: 星系データ読込中…"))
        self.status.set(tr("Preparing 15ly sphere tour…", "15ly スフィア巡回を準備中…"))

        def _worker():
            try:
                res = ns.search(where, radius=radius, hops=1, source=source)
                origin = tuple(res.centre.pos)
                self.after(0, self._arm_tour_with_systems, origin, radius, list(res.rows))
            except Exception as e:
                self.after(0, lambda err=e: (
                    self.tour_status_var.set(tr("Tour: Error", "巡回: エラー")),
                    self.status.set(tr("Error: {error}", "エラー: {error}", error=err)),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _arm_tour_with_systems(self, origin, radius: float, systems: Sequence[Dict]):
        count = self.tour_manager.arm_tour(origin, systems, radius=radius)
        if count == 0:
            self.tour_status_var.set(tr("Tour: No systems (0)", "巡回: 対象なし (0件)"))
            self.status.set(tr("No systems found within {r:g} ly sphere.", "指定スフィア ({r:g} ly以内) に星系が見つかりませんでした。", r=radius))
            return
        self.jump_monitor.arm()
        self._last_auto_copied_token = None
        self.tour_status_var.set(tr("Tour: Waiting FSDJump ({c} systems / {r:g}ly)", "巡回: FSDJump待機中 ({c}件 / {r:g}ly)", c=count, r=radius))
        self.status.set(tr("Tour armed ({c} systems). Plot origin and jump; next nearest auto-copies on FSDJump.", "巡回準備完了 ({c}件)。起点をプロットしてFSDJump完了後に次の近い星系を自動コピーします。", c=count))
        self._schedule_tour_poll()

    def _schedule_tour_poll(self):
        if getattr(self, "_tour_poll_after_id", None) is not None:
            try:
                self.after_cancel(self._tour_poll_after_id)
            except Exception:
                pass
        self._tour_poll_after_id = self.after(2000, self._poll_tour_jump)

    def _poll_tour_jump(self):
        self._tour_poll_after_id = None
        if self.tour_manager.state not in ("ARMED_WAITING_JUMP", "ACTIVE"):
            return
        ev = self.jump_monitor.poll_next_jump()
        if ev is not None:
            token = ev.get("token")
            if token and token != self._last_auto_copied_token:
                self._last_auto_copied_token = token
                nxt = self.tour_manager.on_fsd_jump(ev)
                if nxt is not None:
                    auto_copy = getattr(self, "tour_autocopy_var", None)
                    if auto_copy is None or auto_copy.get():
                        self._copy_text_to_clipboard(nxt["name"])
                        msg = tr("FSDJump [{arr}] -> Auto-copied next: [{name}] ({dist:.1f} ly)", "FSDJump検知 [{arr}] → 次の星系 [{name}] ({dist:.1f} ly) を1度だけ自動コピーしました。", arr=ev["system"], name=nxt["name"], dist=nxt["distance"])
                    else:
                        msg = tr("FSDJump [{arr}] -> Next: [{name}] ({dist:.1f} ly) (Auto-copy OFF)", "FSDJump検知 [{arr}] → 次の星系 [{name}] ({dist:.1f} ly) (自動コピーOFF)", arr=ev["system"], name=nxt["name"], dist=nxt["distance"])
                    rem = self.tour_manager.remaining_count()
                    self.tour_status_var.set(tr("Touring ({rem} left) -> Next: {name} ({dist:.1f} ly)", "巡回中 (残{rem}件) → 次: {name} ({dist:.1f} ly)", rem=rem, name=nxt["name"], dist=nxt["distance"]))
                    self.status.set(msg)
                else:
                    self.tour_status_var.set(tr("Tour Complete (all visited)", "巡回完了 (すべて訪問済み)"))
                    self.status.set(tr("FSDJump [{arr}] -> Sphere tour complete!", "FSDJump検知 [{arr}] → スフィア内の巡回が完了しました！", arr=ev["system"]))
                    return
        if self.tour_manager.state in ("ARMED_WAITING_JUMP", "ACTIVE"):
            self._schedule_tour_poll()

    def copy_tour_target(self):
        tgt = self.tour_manager.current_target
        if tgt is None and self.tour_manager.state == "ARMED_WAITING_JUMP":
            tgt = self.tour_manager.skip_current()
        if tgt is None:
            self.status.set(tr("No tour target system to copy.", "コピー対象の巡回星系がありません。"))
            return
        self._copy_text_to_clipboard(tgt["name"])
        rem = self.tour_manager.remaining_count()
        self.tour_status_var.set(tr("Touring ({rem} left) -> Next: {name} ({dist:.1f} ly)", "巡回中 (残{rem}件) → 次: {name} ({dist:.1f} ly)", rem=rem, name=tgt["name"], dist=tgt["distance"]))
        self.status.set(tr("Copied next system [{name}] ({dist:.1f} ly) to clipboard.", "次の星系 [{name}] ({dist:.1f} ly) をクリップボードにコピーしました。", name=tgt["name"], dist=tgt["distance"]))

    def skip_tour_target(self):
        nxt = self.tour_manager.skip_current()
        if nxt is None:
            self.tour_status_var.set(tr("Tour Complete (none left)", "巡回完了 (残りなし)"))
            self.status.set(tr("No unvisited systems left in sphere.", "スフィア内の未訪問星系はもうありません。"))
            return
        self._copy_text_to_clipboard(nxt["name"])
        rem = self.tour_manager.remaining_count()
        self.tour_status_var.set(tr("Touring ({rem} left) -> Next: {name} ({dist:.1f} ly)", "巡回中 (残{rem}件) → 次: {name} ({dist:.1f} ly)", rem=rem, name=nxt["name"], dist=nxt["distance"]))
        self.status.set(tr("Skipped. Copied next system [{name}] ({dist:.1f} ly).", "スキップしました。次の星系 [{name}] ({dist:.1f} ly) をコピーしました。", name=nxt["name"], dist=nxt["distance"]))

    def stop_sphere_tour(self, silent: bool = False):
        if getattr(self, "_tour_poll_after_id", None) is not None:
            try:
                self.after_cancel(self._tour_poll_after_id)
            except Exception:
                pass
            self._tour_poll_after_id = None
        self.tour_manager.state = "IDLE"
        if not silent:
            self.tour_status_var.set(tr("Tour: Idle", "巡回: 停止中"))
            self.status.set(tr("Stopped sphere tour monitor.", "スフィア巡回監視を停止しました。"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("where", nargs="?", default="", help=tr("Empty = journal pos / 'x,y,z' / id64 / system name", "空 = ジャーナルの現在地 / 'x,y,z' / id64 / 星系名"))
    ap.add_argument("--radius", "-r", type=float, default=ns.DEFAULT_RADIUS)
    ap.add_argument("--hops", "-n", type=int, default=1, help=tr("Number of chained hops (n)", "ジャンプ回数 n(自動でつなげる回数)"))
    ap.add_argument("--lang", choices=["en", "ja"], default="en", help="UI language (en or ja)")
    args = ap.parse_args(argv)
    set_lang(args.lang)
    root = tk.Tk()
    root.geometry("1180x780")
    app = NearbySystemsApp(root, args.where, args.radius, args.hops)
    if args.where:
        root.after(200, app.start_search)
    root.mainloop()
    return 0


if __name__ == "__main__":
    main()
