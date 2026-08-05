"""Test dei modelli e degli identificatori stabili."""

import json
import unittest

from tests.factories import build_record

from accredia_downloader.models import (
    AccreditationBody,
    CertificateRecord,
    CompanySite,
    SourceMetadata,
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