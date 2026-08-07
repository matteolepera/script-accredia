"""Test del ciclo regionale senza effettuare richieste reali."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from accredia_downloader.client import PageSnapshot
from accredia_downloader.parser import ParsingError, parse_result_page
from accredia_downloader.region import (
    RegionChangedError,
    download_region,
)
from accredia_downloader.storage import (
    RegionStorageLayout,
    StagingDatabase,
)


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


class SequenceClient:
    def __init__(self, first_html: str, responses: list[str]) -> None:
        self.first_html = first_html
        self.responses = responses
        self.fetch_count = 0

    def navigate(self, url: str) -> PageSnapshot:
        return PageSnapshot(url=url, html=self.first_html)

    def fetch_html(
        self,
        url: str,
        *,
        referer: str | None = None,
    ) -> str:
        del url, referer
        self.fetch_count += 1

        if not self.responses:
            raise AssertionError("Nessuna risposta fittizia disponibile.")

        return self.responses.pop(0)


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
        soup = BeautifulSoup(cls.two_records_html, "lxml")
        tables = soup.select("div.ppsearch > table")
        tables[-1].decompose()
        cls.incomplete_html = str(soup)

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

    @patch("accredia_downloader.region.time.sleep")
    def test_retries_incomplete_page_when_total_is_unchanged(
        self,
        sleep: object,
    ) -> None:
        del sleep

        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))
            client = SequenceClient(
                self.incomplete_html,
                [
                    self.two_records_html,  # Ricontrollo del totale.
                    self.two_records_html,  # Retry della pagina.
                    self.two_records_html,  # Controllo finale.
                ],
            )

            summary = download_region(
                client=client,  # type: ignore[arg-type]
                layout=layout,
                region="Abruzzo",
                results_url=SOURCE_URL,
                delay_seconds=0,
                local_retries=1,
            )

            self.assertEqual(summary.tables_processed, 2)
            self.assertEqual(client.fetch_count, 3)

    def test_detects_total_change_from_incomplete_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))
            client = SequenceClient(
                self.incomplete_html,
                [self.changed_total_html],
            )

            with self.assertRaises(RegionChangedError):
                download_region(
                    client=client,  # type: ignore[arg-type]
                    layout=layout,
                    region="Abruzzo",
                    results_url=SOURCE_URL,
                    delay_seconds=0,
                    local_retries=1,
                )

            self.assertTrue(layout.staging_path.exists())

    @patch("accredia_downloader.region.time.sleep")
    def test_preserves_staging_after_six_incomplete_responses(
        self,
        sleep: object,
    ) -> None:
        del sleep

        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))
            responses: list[str] = []

            for _ in range(5):
                responses.extend(
                    [self.two_records_html, self.incomplete_html]
                )

            responses.append(self.two_records_html)
            client = SequenceClient(self.incomplete_html, responses)

            with self.assertRaisesRegex(
                ParsingError,
                "ancora incompleta dopo 6 tentativi",
            ):
                download_region(
                    client=client,  # type: ignore[arg-type]
                    layout=layout,
                    region="Abruzzo",
                    results_url=SOURCE_URL,
                    delay_seconds=0,
                    local_retries=1,
                )

            self.assertTrue(layout.staging_path.exists())
            self.assertFalse(layout.certificates_path.exists())

    def test_resumes_compatible_staging_from_previous_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))
            parsed_page = parse_result_page(
                self.two_records_html,
                source_url=SOURCE_URL,
                url_page=0,
                scraped_at="2026-08-06T20:00:00+02:00",
            )

            with StagingDatabase(layout) as staging:
                staging.set_metadata(
                    region="Abruzzo",
                    run_id="20260806T200000+0200",
                    run_date="2026-08-06",
                    started_at="2026-08-06T20:00:00+02:00",
                    source_url=SOURCE_URL,
                    total_results=2,
                    total_pages=1,
                    record_schema_version=3,
                    status="running",
                )
                staging.stage_page(
                    parsed_page.records,
                    url_page=0,
                    completed_at="2026-08-06T20:00:00+02:00",
                )

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
            self.assertTrue(layout.certificates_path.exists())
            self.assertFalse(layout.staging_path.exists())


if __name__ == "__main__":
    unittest.main()
