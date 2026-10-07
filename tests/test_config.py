"""Config schema v1: strict keys, time windows (years/dates, seasons that
wrap New Year, leap days, open-ended), cross-field rules, migration hint."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from data_loader.config import ConfigError, Season, TimeSpec, config_from_dict, load_config, request_identity
from tests.fakes import make_config

TODAY = date(2026, 10, 7)


def _windows(**kw):
    return [(w.season_year, w.start.isoformat(), w.end.isoformat()) for w in TimeSpec(**kw).windows(TODAY)]


class StrictKeyTests(unittest.TestCase):
    def test_unknown_top_level_key_suggests_fix(self):
        with self.assertRaisesRegex(ConfigError, r"unknown key 'temporal_mod'.*did you mean 'temporal_mode'"):
            make_config("x", temporal_mod="scene")

    def test_unknown_nested_keys_rejected(self):
        for section, bad in (("time", {"seasn": {}}), ("output", {"dirr": "x"}), ("filters", {"cloud": 5}),
                             ("grid", {"epsg": 32610}), ("aoi", {"bbox": [1, 2, 3, 4]})):
            with self.subTest(section=section), self.assertRaisesRegex(ConfigError, "unknown key"):
                make_config("x", **{section: bad})

    def test_unknown_season_and_sensor_keys_rejected(self):
        with self.assertRaisesRegex(ConfigError, r"time.season: unknown key 'begin'"):
            make_config("x", time={"start_year": 2020, "season": {"begin": "06-01", "end": "09-30"}})
        with self.assertRaisesRegex(ConfigError, r"sensors\[0\]: unknown key 'resolution_m'"):
            make_config("x", sensors=[{"name": "landsat", "resolution_m": 30}])

    def test_bad_choices_suggest_fix(self):
        with self.assertRaisesRegex(ConfigError, "did you mean 'native'"):
            make_config("x", output={"encoding": "nativ"})
        with self.assertRaisesRegex(ConfigError, "output.bands"):
            make_config("x", output={"bands": ["reed"]})

    def test_pre_v1_config_gets_migration_hint(self):
        old = {"aoi": {}, "provider": "planetary_computer", "date_range": {"start": "2020-01-01"}}
        with self.assertRaisesRegex(ConfigError, "no `version:` key.*date_range with time"):
            config_from_dict(old)

    def test_types_are_checked(self):
        with self.assertRaisesRegex(ConfigError, "whole number"):
            make_config("x", time={"start_year": "1990"})
        with self.assertRaisesRegex(ConfigError, "true or false"):
            make_config("x", filters={"pixel_cloud_mask": "yes"})
        with self.assertRaisesRegex(ConfigError, r"\[lon, lat\]"):
            make_config("x", aoi={"upper_left": [1], "lower_right": [2, 3]})


class TimeWindowTests(unittest.TestCase):
    def test_years_without_season_are_calendar_years_clipped_to_today(self):
        self.assertEqual(_windows(start_year=2025, end_year=2026),
                         [(2025, "2025-01-01", "2025-12-31"), (2026, "2026-01-01", "2026-10-07")])

    def test_end_year_is_inclusive_whole_year(self):
        # the pre-1.0 trap: `end: 2020` meant 2020-01-01
        self.assertEqual(_windows(start_year=2020, end_year=2020), [(2020, "2020-01-01", "2020-12-31")])

    def test_open_ended_runs_through_today(self):
        spec = TimeSpec(start_year=2025)
        self.assertTrue(spec.open_ended)
        self.assertEqual(spec.windows(TODAY)[-1].end, TODAY)

    def test_season_within_each_year(self):
        self.assertEqual(_windows(start_year=2020, end_year=2021, season=Season("06-01", "09-30")),
                         [(2020, "2020-06-01", "2020-09-30"), (2021, "2021-06-01", "2021-09-30")])

    def test_season_wrapping_new_year_is_labelled_by_start_year(self):
        self.assertEqual(_windows(start_year=2000, end_year=2002, season=Season("11-01", "02-28")),
                         [(2000, "2000-11-01", "2001-02-28"), (2001, "2001-11-01", "2002-02-28"),
                          (2002, "2002-11-01", "2003-02-28")])

    def test_leap_day_season_end(self):
        w = _windows(start_year=2023, end_year=2024, season=Season("12-01", "02-29"))
        self.assertEqual(w, [(2023, "2023-12-01", "2024-02-29"), (2024, "2024-12-01", "2025-02-28")])

    def test_current_season_is_clipped_and_future_ones_dropped(self):
        w = _windows(start_year=2025, end_year=2030, season=Season("06-01", "09-30"))
        self.assertEqual(w, [(2025, "2025-06-01", "2025-09-30"), (2026, "2026-06-01", "2026-09-30")])
        w = _windows(start_year=2026, season=Season("11-01", "02-28"))
        self.assertEqual(w, [])

    def test_dates_with_season_clip_both_ends_including_a_wrapped_window(self):
        w = _windows(start_date=date(2021, 1, 15), end_date=date(2021, 12, 15), season=Season("11-01", "02-28"))
        self.assertEqual(w, [(2020, "2021-01-15", "2021-02-28"), (2021, "2021-11-01", "2021-12-15")])

    def test_dates_without_season(self):
        self.assertEqual(_windows(start_date=date(2023, 7, 1), end_date=date(2023, 7, 31)),
                         [(2023, "2023-07-01", "2023-07-31")])
        self.assertEqual(_windows(start_date=date(2022, 12, 20), end_date=date(2023, 1, 10)),
                         [(2022, "2022-12-20", "2022-12-31"), (2023, "2023-01-01", "2023-01-10")])

    def test_invalid_time_specs(self):
        with self.assertRaisesRegex(ConfigError, "not both"):
            TimeSpec(start_year=2020, start_date=date(2020, 1, 1))
        with self.assertRaisesRegex(ConfigError, "before start_year"):
            TimeSpec(start_year=2020, end_year=2019)
        with self.assertRaisesRegex(ConfigError, "MM-DD"):
            make_config("x", time={"start_year": 2020, "season": {"start": "6-1", "end": "09-30"}})
        with self.assertRaisesRegex(ConfigError, "MM-DD"):
            make_config("x", time={"start_year": 2020, "season": {"start": "02-30", "end": "09-30"}})

    def test_yaml_dates_unquoted_or_quoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.yaml"
            p.write_text("version: 1\nprovider: planetary_computer\nsensors: [landsat]\n"
                         "aoi: {upper_left: [-122.43, 44.29], lower_right: [-122.40, 44.27]}\n"
                         "time: {start_date: 2023-07-01, end_date: '2023-07-31', season: {start: '06-01', end: 09-30}}\n"
                         "temporal_mode: scene\noutput: {dir: out, bands: [red]}\n")
            cfg = load_config(p, output_dir="/elsewhere")
        self.assertEqual(cfg.time.start_date, date(2023, 7, 1))
        self.assertEqual(cfg.time.season, Season("06-01", "09-30"))
        self.assertEqual(cfg.output.dir, "/elsewhere")


class CrossFieldTests(unittest.TestCase):
    def test_defaults_keep_source_pixels(self):
        cfg = make_config("x")
        self.assertEqual(cfg.filters.max_cloud_percent, 100.0)
        self.assertFalse(cfg.filters.pixel_cloud_mask)
        self.assertEqual(cfg.output.encoding, "float32")

    def test_native_encoding_rules(self):
        with self.assertRaisesRegex(ConfigError, "indices need float32"):
            make_config("x", output={"encoding": "native", "indices": ["nbr"]})
        with self.assertRaisesRegex(ConfigError, "native is for scene mode"):
            make_config("x", temporal_mode="annual_composite", output={"encoding": "native"})

    def test_qa_band_needs_bands_and_scene_mode(self):
        with self.assertRaisesRegex(ConfigError, "list output.bands too"):
            make_config("x", output={"bands": None, "indices": ["nbr"], "qa_band": True})
        with self.assertRaisesRegex(ConfigError, "no single QA band"):
            make_config("x", temporal_mode="annual_composite", output={"qa_band": True})

    def test_tiles_imply_native_grid(self):
        cfg = make_config("x", aoi={"upper_left": None, "lower_right": None, "tiles": ["h003v004"]}, grid={})
        self.assertEqual(cfg.grid.crs, "native")
        with self.assertRaisesRegex(ConfigError, "native tile grid"):
            make_config("x", aoi={"upper_left": None, "lower_right": None, "tiles": ["h003v004"]},
                        grid={"crs": "EPSG:5070"})
        with self.assertRaisesRegex(ConfigError, "native needs aoi.tiles"):
            make_config("x", grid={"crs": "native"})

    def test_reduce_only_for_composites(self):
        with self.assertRaisesRegex(ConfigError, "only applies to temporal_mode: annual_composite"):
            make_config("x", reduce="mean")

    def test_request_identity_ignores_run_tuning_and_time_end(self):
        a = make_config("/a", workers=1, time={"start_year": 2020, "end_year": 2021, "start_date": None,
                                               "end_date": None}).to_dict()
        b = make_config("/b", workers=8, time={"start_year": 2020, "end_year": 2026, "start_date": None,
                                               "end_date": None}).to_dict()
        self.assertEqual(request_identity(a), request_identity(b))
        c = make_config("/a", output={"bands": ["nir"]}).to_dict()
        self.assertNotEqual(request_identity(a), request_identity(c))

    def test_provider_options_values_never_recorded(self):
        d = make_config("x", provider_options={"usgs_token": "SECRET"}).to_dict()
        self.assertEqual(d["provider_options"], ["usgs_token"])
        self.assertNotIn("SECRET", str(d))


if __name__ == "__main__":
    unittest.main()
