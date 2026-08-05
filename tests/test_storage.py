"""Test della struttura dei file e della scrittura atomica."""

import json
import tempfile
import unittest
from pathlib import Path

from accredia_downloader.storage import (
    StorageLayout,
    atomic_write_text,
    write_certificate,
)
from tests.factories import build_record


class StorageLayoutTests(unittest.TestCase):
    def test_creates_expected_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "documenti" / "accredia"
            layout = StorageLayout(root)

            layout.create_directories()

            self.assertTrue(layout.pages_dir.is_dir())
            self.assertTrue(layout.state_dir.is_dir())
            self.assertTrue(layout.runs_dir.is_dir())
            self.assertTrue(layout.archive_dir.is_dir())
            self.assertTrue(layout.errors_dir.is_dir())

    def test_uses_non_padded_page_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = StorageLayout(Path(temporary_directory))
            record = build_record(url_page=0)

            destination = layout.certificate_path(record)

            self.assertEqual(destination.parent.name, "1")
            self.assertEqual(
                destination.name,
                f"cert-{record.record_id}.json",
            )


class AtomicWriteTests(unittest.TestCase):
    def test_writes_utf8_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "test.json"

            atomic_write_text(
                destination,
                '{"city":"Città di Castello"}\n',
            )

            content = destination.read_text(encoding="utf-8")

            self.assertEqual(
                content,
                '{"city":"Città di Castello"}\n',
            )

    def test_replaces_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "test.json"

            atomic_write_text(destination, "prima versione\n")
            atomic_write_text(destination, "seconda versione\n")

            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "seconda versione\n",
            )

    def test_does_not_leave_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            destination = directory / "test.json"

            atomic_write_text(destination, "{}\n")

            temporary_files = list(directory.glob("*.tmp"))

            self.assertEqual(temporary_files, [])


class CertificateWriterTests(unittest.TestCase):
    def test_writes_valid_certificate_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = StorageLayout(Path(temporary_directory))
            record = build_record()

            destination = write_certificate(layout, record)

            self.assertTrue(destination.is_file())
            self.assertEqual(destination.parent.name, "1")

            payload = json.loads(
                destination.read_text(encoding="utf-8")
            )

            self.assertEqual(
                payload["record_id"],
                record.record_id,
            )
            self.assertEqual(
                payload["certificate_number"],
                "ABC-123",
            )
            self.assertEqual(
                payload["company"]["site"]["city"],
                "Caserta",
            )


if __name__ == "__main__":
    unittest.main()