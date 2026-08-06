"""Test del ciclo regionale senza effettuare richieste reali."""

import tempfile
import unittest
from pathlib import Path

from accredia_downloader.client import PageSnapshot
from accredia_downloader.region import (
    RegionChangedError,
    download_region,
)
from accredia_downloader.storage import RegionStorageLayout


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "result_page.html"
SOURCE_URL = "https://example.test/results?page=0"


class FakeClient:
    def __init__(self, first_html: str, final_html: str) -> None:
        self.first_html = first_html
        self.final_html = final_html

    def navigate(self, url: str) -> PageSnapshot:
        return PageSnapshot(url=url, html=self.first_html)

    def fetch_html(
        self,
        url: str,
        *,
        referer: str | None = None,
    ) -> str:
        del url, referer
        return self.final_html


class RegionDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixture = FIXTURE_PATH.read_text(encoding="utf-8")
        cls.two_records_html = fixture.replace(
            "Risultati: 36.913",
            "Risultati: 2",
        )
        cls.changed_total_html = fixture.replace(
            "Risultati: 36.913",
            "Risultati: 3",
        )

    def test_completes_region_and_removes_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))
            client = FakeClient(
                self.two_records_html,
                self.two_records_html,
            )

            summary = download_region(
                client=client,
                layout=layout,
                region="Abruzzo",
                results_url=SOURCE_URL,
                delay_seconds=0,
                local_retries=1,
            )

            self.assertEqual(summary.tables_processed, 2)
            self.assertTrue(layout.certificates_path.is_file())
            self.assertFalse(layout.staging_path.exists())

    def test_keeps_staging_when_total_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))
            client = FakeClient(
                self.two_records_html,
                self.changed_total_html,
            )

            with self.assertRaises(RegionChangedError):
                download_region(
                    client=client,
                    layout=layout,
                    region="Abruzzo",
                    results_url=SOURCE_URL,
                    delay_seconds=0,
                    local_retries=1,
                )

            self.assertTrue(layout.staging_path.exists())
            self.assertFalse(layout.certificates_path.exists())


if __name__ == "__main__":
    unittest.main()
