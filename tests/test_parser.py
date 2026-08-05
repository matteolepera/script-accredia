"""Test del parser delle pagine Accredia."""

import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from accredia_downloader.parser import (
    ParsingError,
    SessionExpiredError,
    build_page_url,
    calculate_total_pages,
    expected_records_for_page,
    parse_result_page,
    parse_total_results,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "result_page.html"
)

SOURCE_URL = (
    "https://services.accredia.it/ppsearch/"
    "accredia_companymask_remote.jsp"
    "?ID_LINK=1739&area=310&page=0&submit=Cerca"
)

SCRAPED_AT = "2026-08-05T12:00:00+02:00"


class PaginationTests(unittest.TestCase):
    def test_parses_total_with_thousands_separator(self) -> None:
        html = "<div>Risultati: 369.135</div>"

        self.assertEqual(
            parse_total_results(html),
            369135,
        )

    def test_calculates_total_pages(self) -> None:
        self.assertEqual(
            calculate_total_pages(369135),
            18457,
        )

    def test_calculates_last_page_size(self) -> None:
        self.assertEqual(
            expected_records_for_page(369135, 18456),
            15,
        )

    def test_builds_page_url_preserving_parameters(self) -> None:
        result = build_page_url(SOURCE_URL, 27)
        query = parse_qs(urlsplit(result).query)

        self.assertEqual(query["page"], ["27"])
        self.assertEqual(query["ID_LINK"], ["1739"])
        self.assertEqual(query["area"], ["310"])
        self.assertEqual(query["submit"], ["Cerca"])


class ResultPageParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = FIXTURE_PATH.read_text(
            encoding="utf-8"
        )

    def test_parses_only_direct_certificate_tables(self) -> None:
        page = parse_result_page(
            self.html,
            source_url=SOURCE_URL,
            url_page=0,
            scraped_at=SCRAPED_AT,
        )

        self.assertEqual(page.record_count, 2)
        self.assertEqual(page.total_results, 36913)

    def test_parses_certificate_fields(self) -> None:
        page = parse_result_page(
            self.html,
            source_url=SOURCE_URL,
            url_page=0,
            scraped_at=SCRAPED_AT,
        )

        record = page.records[0]

        self.assertEqual(
            record.certificate_number,
            "ABC-123",
        )
        self.assertEqual(record.issued_on, "2025-03-04")
        self.assertEqual(
            record.status,
            "in corso di validità",
        )
        self.assertEqual(
            record.accreditation_body.code,
            "0895",
        )
        self.assertEqual(
            record.company_name,
            "012 FACTORY S.P.A. SOCIETA' BENEFIT",
        )
        self.assertEqual(
            record.vat_or_tax_code,
            "04019110610",
        )
        self.assertEqual(
            record.site.postal_code,
            "81100",
        )
        self.assertEqual(record.site.city, "CASERTA")
        self.assertEqual(record.site.province, "CE")
        self.assertEqual(record.site.region, "Campania")
        self.assertEqual(
            record.standard,
            "UNI/PdR 125:2022",
        )
        self.assertEqual(record.sectors, ("35", "37"))
        self.assertEqual(
            record.updated_on,
            "2026-08-03",
        )

    def test_detects_expired_session(self) -> None:
        html = """
        <html>
            <body>
                Verifica reCAPTCHA richiesta
            </body>
        </html>
        """

        with self.assertRaises(SessionExpiredError):
            parse_result_page(
                html,
                source_url=SOURCE_URL,
                url_page=0,
                scraped_at=SCRAPED_AT,
            )

    def test_rejects_page_without_results_container(self) -> None:
        with self.assertRaises(ParsingError):
            parse_result_page(
                "<html><body>Pagina vuota</body></html>",
                source_url=SOURCE_URL,
                url_page=0,
                scraped_at=SCRAPED_AT,
            )


if __name__ == "__main__":
    unittest.main()