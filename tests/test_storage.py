"""Test della struttura dei file e della scrittura atomica."""

import json
import tempfile
import unittest
from pathlib import Path

from accredia_downloader.storage import (
    RecordChange,
    RecordIndex,
    StorageLayout,
    StorageManager,
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

class StorageManagerTests(unittest.TestCase):
    def test_saves_new_record_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = StorageManager.open(
                Path(temporary_directory)
            )
            record = build_record()

            result = manager.save_record(
                record,
                run_id="run-1",
            )
            manager.save_index()

            self.assertEqual(result.change, RecordChange.NEW)
            self.assertTrue(result.path.is_file())
            self.assertTrue(manager.layout.index_path.is_file())

            loaded_index = RecordIndex.load(
                manager.layout.index_path
            )
            entry = loaded_index.get(record.record_id)

            self.assertIsNotNone(entry)
            self.assertEqual(
                entry.content_hash,
                record.content_hash,
            )

    def test_does_not_rewrite_unchanged_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = StorageManager.open(
                Path(temporary_directory)
            )
            record = build_record()

            manager.save_record(record, run_id="run-1")

            result = manager.save_record(
                record,
                run_id="run-2",
            )

            self.assertEqual(
                result.change,
                RecordChange.UNCHANGED,
            )

    def test_updates_changed_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = StorageManager.open(
                Path(temporary_directory)
            )

            original = build_record(
                scope="Primo scopo."
            )
            updated = build_record(
                scope="Scopo aggiornato."
            )

            manager.save_record(original, run_id="run-1")
            result = manager.save_record(
                updated,
                run_id="run-2",
            )

            self.assertEqual(
                original.record_id,
                updated.record_id,
            )
            self.assertEqual(
                result.change,
                RecordChange.UPDATED,
            )

            payload = json.loads(
                result.path.read_text(encoding="utf-8")
            )

            self.assertEqual(
                payload["scope"],
                "Scopo aggiornato.",
            )

    def test_moves_record_to_new_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = StorageManager.open(
                Path(temporary_directory)
            )

            original = build_record(url_page=0)
            moved = build_record(url_page=1)

            first_result = manager.save_record(
                original,
                run_id="run-1",
            )
            moved_result = manager.save_record(
                moved,
                run_id="run-2",
            )

            self.assertEqual(
                moved_result.change,
                RecordChange.MOVED,
            )
            self.assertFalse(first_result.path.exists())
            self.assertTrue(moved_result.path.exists())
            self.assertEqual(
                moved_result.path.parent.name,
                "2",
            )

    def test_recovers_deleted_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = StorageManager.open(
                Path(temporary_directory)
            )
            record = build_record()

            first_result = manager.save_record(
                record,
                run_id="run-1",
            )
            first_result.path.unlink()

            recovered_result = manager.save_record(
                record,
                run_id="run-2",
            )

            self.assertEqual(
                recovered_result.change,
                RecordChange.RECOVERED,
            )
            self.assertTrue(recovered_result.path.exists())

    def test_marks_records_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = StorageManager.open(
                Path(temporary_directory)
            )
            record = build_record()

            manager.save_record(record, run_id="run-1")
            manager.finish_run("run-1")

            missing_count = manager.finish_run("run-2")
            entry = manager.index.get(record.record_id)

            self.assertEqual(missing_count, 1)
            self.assertIsNotNone(entry)
            self.assertEqual(entry.missing_runs, 1)

if __name__ == "__main__":
    unittest.main()