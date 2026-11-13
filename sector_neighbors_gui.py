# -*- coding: utf-8 -*-
"""sector_neighbors_gui.py: Tkinter front end for sector_neighbors.py (type a system / sector name or x,y,z, see the neighbouring sectors with name and XYZ direction).

All logic is in sector_neighbors.py (no Tk there). One input box (system name, sector name or 'x,y,z'), the radii (ly), two checkboxes (予測名を出す / 名前の無いセクターを隠す).
The table lists every sector whose nearest point is within the largest radius, shell by shell. Columns: 殻 (shell), セクター名, XYZ (which way in galactic X/Y/Z: the sign of the cell offset
from the current sector, +X = galactic east, +Z = towards Sagittarius A*), Δセル (cell offset x,y,z), 中心へのずれ (centre minus your position, ly), 方位 (8-way bearing in the x-z plane),
edge / centre distance, and the grid cell i,j,k. Names the table lacks are the naming rule's names marked (予測), or 不明.

Usage (from imitate_forge/):
    python sector_neighbors_gui.py ["Noijoo AA-A h"] [--radii 1500,3000]
"""
import argparse
import threading
import tkinter as tk
from tkinter import ttk
from typing import List, Optional, Sequence

import boxel_calculator as bc
import sector_neighbors as sn

COLUMNS = [("ring", "殻", 40, "center"), ("name", "セクター名", 190, "w"), ("xyz", "XYZ", 90, "center"), ("delta", "Δセル x,y,z", 90, "center"),
           ("off", "中心へのずれ ly (x, y, z)", 190, "center"), ("dir", "方位", 110, "w"), ("edge", "edge", 60, "e"), ("centre", "centre", 60, "e"), ("cell", "格子 i,j,k", 90, "center")]


def xyz_label(delta_cell: Sequence[int]) -> str:
    """'+X -Z' from the cell offset (x, y, z); '(現在地)' when it is 0,0,0."""
    parts = [f"{'+' if d > 0 else '-'}{axis}" + (str(abs(d)) if abs(d) > 1 else "") for d, axis in zip(delta_cell, "XYZ") if d]
    return " ".join(parts) or "(現在地)"


class SectorNeighborsApp(ttk.Frame):
    def __init__(self, master, where: str = "", radii: str = ""):
        super().__init__(master, padding=8)
        self.master.title("Sector Neighbors — 近隣セクターの名前と XYZ 方向")
        self.pack(fill="both", expand=True)
        self.busy = False
        self.where_var = tk.StringVar(value=where)
        self.radii_var = tk.StringVar(value=radii or ",".join(f"{r:g}" for r in sn.DEFAULT_RADII))
        self.predict = tk.BooleanVar(value=sn.SHOW_PREDICTED)
        self.hide_unnamed = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="システム名・セクター名・座標 'x,y,z' を入れて Enter")
        self._result = None
        sn.use_merged_table()
        self._build()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="現在地").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(top, textvariable=self.where_var, width=36)
        entry.grid(row=0, column=1, sticky="w", padx=(4, 12))
        entry.bind("<Return>", lambda _e: self.start_search())
        entry.focus_set()
        ttk.Label(top, text="半径 ly").grid(row=0, column=2, sticky="w")
        radii = ttk.Entry(top, textvariable=self.radii_var, width=14)
        radii.grid(row=0, column=3, sticky="w", padx=(4, 12))
        radii.bind("<Return>", lambda _e: self.start_search())
        self.button = ttk.Button(top, text="検索", command=self.start_search)
        self.button.grid(row=0, column=4, sticky="w")
        ttk.Button(top, text="名前をコピー", command=self.copy_name).grid(row=0, column=5, sticky="w", padx=6)
        ttk.Checkbutton(top, text="予測名を出す", variable=self.predict, command=self.refresh).grid(row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Checkbutton(top, text="名前の無いセクターを隠す", variable=self.hide_unnamed, command=self.refresh).grid(row=1, column=2, columnspan=2, sticky="w", pady=(4, 0))

        self.info = tk.Text(self, height=4, wrap="word")
        self.info.pack(fill="x", pady=(6, 0))
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, pady=6)
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in COLUMNS], show="headings", height=22, selectmode="browse")
        for key, head, width, anchor in COLUMNS:
            self.tree.heading(key, text=head)
            self.tree.column(key, width=width, anchor=anchor)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<ButtonRelease-1>", self.on_click)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        ttk.Label(self, textvariable=self.status).pack(fill="x")

    # ---- search (worker thread: the name lookup may touch the database on a slow drive)
    def start_search(self):
        if self.busy:
            return
        try:
            radii = [float(v) for v in self.radii_var.get().split(",") if v.strip()]
            if not radii or min(radii) <= 0:
                raise ValueError
        except ValueError:
            self.status.set("半径は正の数をカンマ区切りで(例 1500,3000)")
            return
        where = self.where_var.get().strip()
        if not where:
            self.status.set("現在地を入れる")
            return
        self.busy = True
        self.button.state(["disabled"])
        self.status.set("検索中…")
        self._result = None
        threading.Thread(target=self._worker, args=(where, radii), daemon=True).start()
        self.after(100, self._poll)

    def _worker(self, where, radii):
        try:
            pos, how = sn.resolve(where)
            self._result = (pos, how, radii, None)
        except Exception as e:   # shown in the status line
            self._result = (None, None, radii, e)

    def _poll(self):
        """the worker only stores its result; the Tk thread picks it up (no Tk call from the worker thread)."""
        if self._result is None:
            self.after(100, self._poll)
        else:
            self._done(*self._result)

    def _done(self, pos, how, radii, error):
        self.busy = False
        self.button.state(["!disabled"])
        if error is not None:
            self.status.set(f"エラー: {error}")
            return
        self.pos, self.how, self.radii = pos, how, radii
        self.refresh()

    def refresh(self):
        """rebuild the table from the last resolved position (the checkboxes only change what is shown)."""
        if getattr(self, "pos", None) is None:
            return
        predict = self.predict.get()
        rows = sn.neighbours(self.pos, self.radii, predict)
        here = bc.sector_cell(self.pos)
        here_pred = None if bc.sector_name(here) or not predict else sn.predicted_name(here)
        info = [f"位置 ({self.pos[0]:.2f}, {self.pos[1]:.2f}, {self.pos[2]:.2f}) ly  [{self.how}]",
                f"現在のセクター: {bc.sector_name(here) or (here_pred + ' (予測)' if here_pred else '不明')} {here}",
                "XYZ: +X = 銀河の東, +Z = Sgr A* 側, +Y = 上。方位 0°=+Z 90°=+X。edge = そのセクターの最寄り点までの距離, centre = 中心まで (ly)"]
        regions = bc.named_regions_containing(self.pos)
        if regions:
            info.append(f"注意: 手名付き領域 {', '.join(regions)} の内側。この中では格子の名前が当てにならない")
        self.info.delete("1.0", "end")
        self.info.insert("1.0", "\n".join(info))
        self.tree.delete(*self.tree.get_children())
        self.shown: List[dict] = []
        shown_n = predicted = unnamed = 0
        for s in rows:
            if not s["name"]:
                predicted += 1 if s["predicted"] else 0
                unnamed += 0 if s["predicted"] else 1
                if self.hide_unnamed.get():
                    continue
            name = s["name"] or (f"{s['predicted']} (予測)" if s["predicted"] else "不明")
            dcell = tuple(s["cell"][i] - here[i] for i in range(3))
            dx, dy, dz = s["delta"]
            where = "(現在地)" if s["cell"] == here else s["dir"]
            self.tree.insert("", "end", iid=str(shown_n), values=[s["ring"] + 1, name, xyz_label(dcell), ",".join(f"{d:+d}" if d else "0" for d in dcell),
                                                                  f"{dx:+.0f}, {dy:+.0f}, {dz:+.0f}", where, f"{s['edge']:.0f}", f"{s['centre_dist']:.0f}", ",".join(map(str, s["cell"]))])
            self.shown.append(s)
            shown_n += 1
        self.status.set(f"{len(rows)} セクター(表示 {shown_n}、予測 {predicted}、不明 {unnamed})  殻 1 = 〜{min(self.radii):g} ly" +
                        (f"、殻 2 = 〜{sorted(self.radii)[1]:g} ly" if len(self.radii) > 1 else ""))

    def on_click(self, event):
        """a click on the セクター名 cell copies that sector's name."""
        if self.tree.identify_region(event.x, event.y) != "cell" or self.tree.identify_column(event.x) != "#2":
            return
        row = self.tree.identify_row(event.y)
        if row:
            self.copy_name(row)

    def copy_name(self, row: Optional[str] = None):
        """copy the sector name of the given row (default: the selected row), without the 予測 mark."""
        if row is None:
            sel = self.tree.selection()
            if not sel:
                return
            row = sel[0]
        s = self.shown[int(row)]
        name = s["name"] or s["predicted"]
        if not name:
            return
        self.clipboard_clear()
        self.clipboard_append(name)
        self.status.set(f"コピーした: {name}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("where", nargs="?", default="", help="システム名 / セクター名 / 'x,y,z'")
    ap.add_argument("--radii", default="", help="半径(ly)のカンマ区切り。既定 1500,3000")
    args = ap.parse_args(argv)
    root = tk.Tk()
    root.geometry("1040x700")
    app = SectorNeighborsApp(root, args.where, args.radii)
    if args.where:
        root.after(200, app.start_search)
    root.mainloop()
    return 0


if __name__ == "__main__":
    main()
