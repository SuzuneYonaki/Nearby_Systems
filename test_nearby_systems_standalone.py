# -*- coding: utf-8 -*-
"""test_nearby_systems_standalone.py: Verify EDSM mode works without data/full_catalog."""
from pathlib import Path
import tempfile
import pytest

import nearby_systems as ns


def test_edsm_mode_without_full_catalog(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        missing_cat = Path(tmpdir) / "nonexistent_full_catalog"
        monkeypatch.setattr(ns, "CATALOG_DIR", missing_cat)

        assert ns.catalogue_available() is False
        assert ns.catalogue_by_id64(12345) is None
        assert ns.catalogue_box((0.0, 0.0, 0.0), 15.0).empty
        assert ns.fit_reach((0.0, 0.0, 0.0), 5000.0) == 5000.0

        fake_edsm = [
            {"name": "Sol", "x": 0.0, "y": 0.0, "z": 0.0, "id64": 10477373803},
            {"name": "Alpha Centauri", "x": 3.03, "y": -0.09, "z": 3.15, "id64": 2862403414852},
            {"name": "Barnard's Star", "x": -3.0, "y": 1.37, "z": 4.93, "id64": 1166580214596},
        ]

        res = ns.search(
            "0,0,0",
            radius=15.0,
            hops=1,
            source=ns.SOURCE_EDSM,
            fetch=lambda pos, r: fake_edsm,
        )
        names = [r["name"] for r in res.rows]
        assert "Alpha Centauri" in names
        assert "Barnard's Star" in names

        # SOURCE_CATALOG without full_catalog raises clear ValueError instead of FileNotFoundError
        with pytest.raises(ValueError, match="full_catalog"):
            ns.search("0,0,0", radius=15.0, hops=1, source=ns.SOURCE_CATALOG)
