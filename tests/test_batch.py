"""Test della selezione automatica delle regioni."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from accredia_downloader.batch import (
    ITALIAN_REGIONS,
    decide_region_download,
)
from accredia_downloader.storage import RegionStorageLayout


COMPLETED_AT = "2026-08-01T12:00:00+02:00"
NOW = datetime.fromisoformat("2026-08-06T12:00:00+02:00")


def create_completed_snapshot(
    layout: RegionStorageLayout,
    *,
    region: str = "Abruzzo",
) -> None:
    layout.create_directories()
    layout.certificates_path.write_text("{}\n", encoding="utf-8")
    layout.index_path.write_text("", encoding="utf-8")
    layout.last_run_path.write_text(
        json.dumps(
            {
                "region": region,
                "status": "completed",
                "completed_at": COMPLETED_AT,
            }
        ),
        encoding="utf-8",
    )


class RegionCatalogTests(unittest.TestCase):
    def test_contains_twenty_unique_regions(self) -> None:
        self.assertEqual(len(ITALIAN_REGIONS), 20)
        self.assertEqual(len(set(ITALIAN_REGIONS)), 20)
        self.assertIn("Abruzzo", ITALIAN_REGIONS)
        self.assertIn("Valle d'Aosta", ITALIAN_REGIONS)


class RegionDecisionTests(unittest.TestCase):
    def test_downloads_missing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))

            decision = decide_region_download(
                layout=layout,
                region="Abruzzo",
                refresh_existing=False,
                refresh_after_days=None,
                now=NOW,
            )

            self.assertTrue(decision.should_download)

    def test_skips_completed_snapshot_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))
            create_completed_snapshot(layout)

            decision = decide_region_download(
                layout=layout,
                region="Abruzzo",
                refresh_existing=False,
                refresh_after_days=None,
                now=NOW,
            )

            self.assertFalse(decision.should_download)

    def test_refreshes_existing_snapshot_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))
            create_completed_snapshot(layout)

            decision = decide_region_download(
                layout=layout,
                region="Abruzzo",
                refresh_existing=True,
                refresh_after_days=None,
                now=NOW,
            )

            self.assertTrue(decision.should_download)

    def test_refreshes_only_stale_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))
            create_completed_snapshot(layout)

            fresh = decide_region_download(
                layout=layout,
                region="Abruzzo",
                refresh_existing=False,
                refresh_after_days=7,
                now=NOW,
            )
            stale = decide_region_download(
                layout=layout,
                region="Abruzzo",
                refresh_existing=False,
                refresh_after_days=5,
                now=NOW,
            )

            self.assertFalse(fresh.should_download)
            self.assertTrue(stale.should_download)

    def test_rejects_report_for_another_region(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))
            create_completed_snapshot(layout, region="Lazio")

            decision = decide_region_download(
                layout=layout,
                region="Abruzzo",
                refresh_existing=False,
                refresh_after_days=None,
                now=NOW,
            )

            self.assertTrue(decision.should_download)


if __name__ == "__main__":
    unittest.main()
