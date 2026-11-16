# -*- coding: utf-8 -*-
"""test_nearby_systems_standalone.py: Verify EDSM mode & FSDJump tour work without data/full_catalog."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import pytest

import nearby_systems as ns
import nearby_systems_gui as nsg
import sphere_tour


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
        assert res.centre.how == "Coordinates"
        rendered = ns.render(res, 15.0, 1, ns.SOURCE_EDSM)
        assert "Center " in rendered and "Source: EDSM" in rendered
        names = [r["name"] for r in res.rows]
        assert "Alpha Centauri" in names
        assert "Barnard's Star" in names

        with pytest.raises(ValueError, match="full_catalog"):
            ns.search("0,0,0", radius=15.0, hops=1, source=ns.SOURCE_CATALOG)


def test_nearby_gui_fsdjump_sphere_tour():
    with tempfile.TemporaryDirectory() as tmpdir:
        jdir = Path(tmpdir)
        jfile = jdir / "Journal.2026-11-16T150000.01.log"
        jfile.write_text(
            json.dumps({"timestamp": "2026-11-16T15:00:00Z", "event": "FSDJump", "StarSystem": "Prior-Sys"}) + "\n",
            encoding="utf-8",
        )

        app = nsg.NearbySystemsApp.__new__(nsg.NearbySystemsApp)
        app.tour_manager = sphere_tour.SphereTourManager()
        app.jump_monitor = sphere_tour.FSDJumpMonitor(journal_dir=jdir)
        app.tour_status_var = SimpleNamespace(set=lambda v: None)
        app.status = SimpleNamespace(set=lambda v: None)
        app.tour_autocopy_var = SimpleNamespace(get=lambda: True)
        app._schedule_tour_poll = lambda: None
        app._tour_poll_after_id = None
        app._last_auto_copied_token = None

        copied = []
        app._copy_text_to_clipboard = lambda text: copied.append(text)

        systems = [
            {"name": "Origin-Sys", "x": 0.0, "y": 0.0, "z": 0.0},
            {"name": "Near-A", "x": 3.0, "y": 0.0, "z": 0.0},
            {"name": "Near-B", "x": 8.0, "y": 0.0, "z": 0.0},
        ]
        app._arm_tour_with_systems((0.0, 0.0, 0.0), 15.0, systems)
        assert app.tour_manager.state == "ARMED_WAITING_JUMP"
        assert copied == []

        with open(jfile, "a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2026-11-16T15:05:00Z", "event": "FSDJump", "StarSystem": "Origin-Sys"}) + "\n")

        app._poll_tour_jump()
        assert copied == ["Near-A"]
        app._poll_tour_jump()
        assert copied == ["Near-A"]

        app.copy_tour_target()
        assert copied == ["Near-A", "Near-A"]

        app.skip_tour_target()
        assert copied == ["Near-A", "Near-A", "Near-B"]

        # Verify Auto-copy OFF does not insert into clipboard
        app.tour_autocopy_var = SimpleNamespace(get=lambda: False)
        app._arm_tour_with_systems((0.0, 0.0, 0.0), 15.0, systems)
        copied.clear()
        with open(jfile, "a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": "2026-11-16T15:10:00Z", "event": "FSDJump", "StarSystem": "Origin-Sys"}) + "\n")
        app._poll_tour_jump()
        assert copied == []
        assert app.tour_manager.current_target["name"] == "Near-A"
