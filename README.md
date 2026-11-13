# Nearby Systems

Elite Dangerous 用の近傍星系ホッピング探索＆近傍セクター探査 GUI / CLI ツール集です。

## 主なツール

### 1. Nearby Systems (`nearby_systems_gui.py` / `nearby_systems.py`)
指定した星系や座標、またはジャーナルの現在地から半径 **15 ly**（デフォルト）以内の到達可能な星系を探索し、ダブルクリックで経由地を繋ぎながら探索ルート（ホップチェーン）を構築できるツールです。

```powershell
python nearby_systems_gui.py
```

### 2. Sector Neighbors (`sector_neighbors_gui.py` / `sector_neighbors.py`)
指定座標やセクターの周囲にある近傍セクター（1,280 ly 単位の格子）を方位角・距離・銀河マップ検索名（`AA-A h`）付きで一覧表示するツールです。

```powershell
python sector_neighbors_gui.py
```
