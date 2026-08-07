"""Test del recupero manuale della sessione CAPTCHA."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from accredia_downloader.batch import RegionDecision
from accredia_downloader.client import PageSnapshot
from accredia_downloader.parser import SessionExpiredError
from accredia_downloader.storage import RegionalRunSummary
from accredia_scraper import (
    SEARCH_URL,
    ScraperConfig,
    configured_results_url,
    recover_browser_session,
    run_all_regions,
    run_region_download,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "result_page.html"


class FakeClient:
    def __init__(self, result_html: str) -> None:
        self.result_html = result_html
        self.urls: list[str] = []

    def navigate(self, url: str) -> PageSnapshot:
        self.urls.append(url)
        html = "<html>Maschera CAPTCHA</html>"

        if url != SEARCH_URL:
            html = self.result_html

        return PageSnapshot(url=url, html=html)


def build_config(temporary_directory: str) -> ScraperConfig:
    return ScraperConfig(
        output_root=Path(temporary_directory),
        profile_dir=Path(temporary_directory) / "profile",
        delay_seconds=1,
        request_timeout_ms=1_000,
        network_retries=1,
        browser_channel="firefox",
        headless=False,
        check_session=False,
        region="Abruzzo",
        all_regions=False,
        refresh_existing=False,
        refresh_after_days=None,
    )


class SessionRecoveryTests(unittest.TestCase):
    @patch("accredia_scraper.CONSOLE.print")
    @patch("accredia_scraper.Confirm.ask", return_value=True)
    def test_recovers_session_and_validates_region_results(
        self,
        confirm: MagicMock,
        console_print: MagicMock,
    ) -> None:
        del confirm, console_print
        fixture = FIXTURE_PATH.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as temporary_directory:
            config = build_config(temporary_directory)
            client = FakeClient(fixture)

            snapshot = recover_browser_session(
                client,  # type: ignore[arg-type]
                config,
            )

            self.assertEqual(
                client.urls,
                [SEARCH_URL, configured_results_url(config)],
            )
            self.assertEqual(snapshot.url, configured_results_url(config))

    def test_headless_recovery_preserves_staging_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = replace(
                build_config(temporary_directory),
                headless=True,
            )
            client = FakeClient("")

            with self.assertRaises(SessionExpiredError):
                recover_browser_session(
                    client,  # type: ignore[arg-type]
                    config,
                )

            self.assertEqual(client.urls, [])

    @patch("accredia_scraper.print_region_summary")
    @patch("accredia_scraper.recover_browser_session")
    @patch("accredia_scraper.download_region_with_client")
    @patch("accredia_scraper.create_browser_client")
    def test_single_download_retries_region_after_recovery(
        self,
        create_client: MagicMock,
        download: MagicMock,
        recover: MagicMock,
        print_summary: MagicMock,
    ) -> None:
        del print_summary

        with tempfile.TemporaryDirectory() as temporary_directory:
            config = build_config(temporary_directory)
            summary = RegionalRunSummary(
                region="Abruzzo",
                total_results=2,
                total_pages=1,
                tables_processed=2,
                unique_records=2,
                duplicates_collapsed=0,
                new_records=2,
                updated_records=0,
                unchanged_records=0,
                missing_records=0,
                output_path=Path(temporary_directory) / "certificati.json",
            )
            download.side_effect = [
                SessionExpiredError("Sessione scaduta"),
                summary,
            ]

            result = run_region_download(config)

            self.assertEqual(result, 0)
            self.assertEqual(download.call_count, 2)
            recover.assert_called_once()
            create_client.assert_called_once_with(config)

    @patch("accredia_scraper.print_batch_summary")
    @patch("accredia_scraper.print_batch_plan")
    @patch("accredia_scraper.print_region_summary")
    @patch("accredia_scraper.recover_browser_session")
    @patch("accredia_scraper.download_region_with_client")
    @patch("accredia_scraper.create_browser_client")
    @patch("accredia_scraper.build_region_decisions")
    def test_batch_retries_current_region_after_recovery(
        self,
        decisions: MagicMock,
        create_client: MagicMock,
        download: MagicMock,
        recover: MagicMock,
        print_region: MagicMock,
        print_plan: MagicMock,
        print_batch: MagicMock,
    ) -> None:
        del print_region, print_plan, print_batch

        with tempfile.TemporaryDirectory() as temporary_directory:
            config = replace(
                build_config(temporary_directory),
                region=None,
                all_regions=True,
            )
            decisions.return_value = [
                RegionDecision(
                    region="Abruzzo",
                    should_download=True,
                    reason="snapshot mancante",
                )
            ]
            summary = RegionalRunSummary(
                region="Abruzzo",
                total_results=2,
                total_pages=1,
                tables_processed=2,
                unique_records=2,
                duplicates_collapsed=0,
                new_records=2,
                updated_records=0,
                unchanged_records=0,
                missing_records=0,
                output_path=Path(temporary_directory) / "certificati.json",
            )
            download.side_effect = [
                SessionExpiredError("Sessione scaduta"),
                summary,
            ]

            result = run_all_regions(config)

            self.assertEqual(result, 0)
            self.assertEqual(download.call_count, 2)
            recover.assert_called_once()
            create_client.assert_called_once_with(config)


if __name__ == "__main__":
    unittest.main()
