"""Test dei modelli e degli identificatori stabili."""

import json
import unittest

from accredia_downloader.models import (
    AccreditationBody,
    CertificateRecord,
    CompanySite,
    SourceMetadata,
)


def build_record(
    *,
    scope: str = "Erogazione di servizi.",
    address: str = "Via Roma, 1",
    url_page: int = 0,
) -> CertificateRecord:
    """Crea un record prevedibile riutilizzato nei test."""

    site = CompanySite(
        type="Sede Legale e Operativa",
        address=address,
        postal_code="81100",
        city="Caserta",
        province="CE",
        region="Campania",
        raw=(
            "Sede Legale e Operativa - "
            f"{address} - 81100 - Caserta ( CE ) - Campania"
        ),
    )

    source = SourceMetadata(
        url=f"https://example.test/results?page={url_page}",
        url_page=url_page,
        page=url_page + 1,
        position=1,
        scraped_at="2026-08-05T12:00:00+02:00",
    )

    return CertificateRecord.create(
        certificate_number="ABC-123",
        issued_on="2025-03-04",
        status="in corso di validità",
        accreditation_body=AccreditationBody(
            code="0895",
            name="APAVE CERTIFICATION ITALIA S.r.l.",
            detail_url="https://example.test/body/0895",
            website="https://italy.apave.com/it-IT",
        ),
        company_name="012 FACTORY S.P.A. SOCIETA' BENEFIT",
        vat_or_tax_code="04019110610",
        site=site,
        scope=scope,
        standard="UNI/PdR 125:2022",
        accreditation_scheme="SGQ",
        sectors=[],
        updated_on="2026-08-03",
        source=source,
        raw_text="Contenuto completo della tabella.",
        raw_html="<table><tr><td>Contenuto</td></tr></table>",
    )


class CertificateRecordTests(unittest.TestCase):
    def test_same_data_produces_same_identifiers(self) -> None:
        first = build_record()
        second = build_record()

        self.assertEqual(first.certificate_id, second.certificate_id)
        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(first.content_hash, second.content_hash)

    def test_source_page_does_not_change_hashes(self) -> None:
        first = build_record(url_page=0)
        moved = build_record(url_page=20)

        self.assertEqual(first.certificate_id, moved.certificate_id)
        self.assertEqual(first.record_id, moved.record_id)
        self.assertEqual(first.content_hash, moved.content_hash)

    def test_scope_update_changes_only_content_hash(self) -> None:
        original = build_record()
        updated = build_record(scope="Nuovo scopo certificato.")

        self.assertEqual(original.certificate_id, updated.certificate_id)
        self.assertEqual(original.record_id, updated.record_id)
        self.assertNotEqual(
            original.content_hash,
            updated.content_hash,
        )

    def test_different_site_changes_record_id(self) -> None:
        first = build_record(address="Via Roma, 1")
        second = build_record(address="Via Milano, 10")

        self.assertEqual(first.certificate_id, second.certificate_id)
        self.assertNotEqual(first.record_id, second.record_id)

    def test_json_preserves_unicode(self) -> None:
        record = build_record()
        payload = json.loads(record.to_json())

        self.assertEqual(
            payload["status"],
            "in corso di validità",
        )
        self.assertEqual(
            payload["company"]["site"]["city"],
            "Caserta",
        )
        self.assertEqual(payload["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()