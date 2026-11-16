# Nearby Systems

GUI and CLI tools for **Elite Dangerous** to explore nearby star systems within jump range (default 15 ly) and inspect neighboring Stellar Forge sectors.

**Default UI Language:** English (`--lang en`, or `--lang ja` / `ED_NAV_LANG=ja` for Japanese).

---

## Tools

### 1. Nearby Systems (`nearby_systems_gui.py` / `nearby_systems.py`)
Finds reachable star systems within a configurable radius (default **15 ly**) from a system name, `id64`, `(x, y, z)` coordinates, or the Commander's current journal position.
- Supports multi-hop chaining (`Hops n`), overlapping-sphere omission, EDSM (cached & rate-limited) or offline local catalog lookup, and CSV route save/load.

```bash
python nearby_systems_gui.py
python nearby_systems_gui.py --lang ja
```

### 2. Sector Neighbors (`sector_neighbors_gui.py` / `sector_neighbors.py`)
Lists neighboring Stellar Forge sectors (1,280 ly grid cells) around a given system, sector name, or `(x, y, z)` coordinates, showing galactic XYZ directions (`+X` East, `+Z` toward Sagittarius A*, `+Y` Up), bearings, and edge/centre distances.

```bash
python sector_neighbors_gui.py
python sector_neighbors_gui.py --lang ja
```

---

## 日本語ガイド (Japanese Summary)

- **デフォルト言語**: 英語 UI がデフォルトです。`--lang ja`（または環境変数 `ED_NAV_LANG=ja`）で日本語表示に切り替えられます。
- **Nearby Systems (`nearby_systems_gui.py`)**: 現在地（ジャーナル）や指定座標から半径 15 ly（デフォルト）以内の到達可能な星系を検索し、ダブルクリックで経由地を繋ぎながら経路構築・CSV 保存/読込が可能です。
- **Sector Neighbors (`sector_neighbors_gui.py`)**: 指定座標やセクターの周囲の近隣セクター（1,280 ly 格子）を方位・距離付きで一覧表示します。
