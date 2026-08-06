"""Test dello staging SQLite e del JSON regionale."""

import json
import tempfile
import unittest
from pathlib import Path

from accredia_downloader.storage import (
    RegionStorageLayout,
    RegionalIndex,
    StagingDatabase,
    atomic_write_text,
    finalize_region,
)
from tests.factories import build_record


class RegionStorageLayoutTests(unittest.TestCase):
    def test_creates_regional_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "regioni" / "abruzzo"
            layout = RegionStorageLayout(root)

            layout.create_directories()

            self.assertTrue(layout.root.is_dir())
            self.assertTrue(layout.state_dir.is_dir())
            self.assertEqual(
                layout.certificates_path.name,
                "certificati.json",
            )
            self.assertEqual(
                layout.staging_path.name,
                "staging.sqlite",
            )


class AtomicWriteTests(unittest.TestCase):
    def test_replaces_utf8_file_without_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            destination = directory / "test.json"

            atomic_write_text(destination, "prima versione\n")
            atomic_write_text(
                destination,
                "{\"città\":\"L'Aquila\"}\n",
            )

            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "{\"città\":\"L'Aquila\"}\n",
            )
            self.assertEqual(list(directory.glob("*.tmp")), [])


class StagingDatabaseTests(unittest.TestCase):
    def test_collapses_only_identical_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))

            first = build_record(position=1)
            identical = build_record(position=2)
            punctuation_change = build_record(
                position=3,
                scope="Erogazione di servizi,",
            )

            with StagingDatabase(layout) as staging:
                staging.stage_page(
                    (first, identical, punctuation_change),
                    url_page=0,
                    completed_at="2026-08-06T12:01:00+02:00",
                )

                self.assertEqual(staging.counts(), (3, 2, 1))

    def test_replacing_page_does_not_duplicate_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))
            records = (
                build_record(position=1),
                build_record(position=2),
            )

            with StagingDatabase(layout) as staging:
                staging.stage_page(
                    records,
                    url_page=0,
                    completed_at="2026-08-06T12:01:00+02:00",
                )
                staging.stage_page(
                    records,
                    url_page=0,
                    completed_at="2026-08-06T12:02:00+02:00",
                )

                self.assertEqual(staging.counts(), (2, 1, 1))
                self.assertEqual(staging.completed_pages(), {0})

    def test_exports_one_json_for_the_region(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))
            first = build_record(position=1)
            identical = build_record(position=2)
            different = build_record(
                position=3,
                scope="Scopo differente.",
            )

            with StagingDatabase(layout) as staging:
                staging.stage_page(
                    (first, identical, different),
                    url_page=0,
                    completed_at="2026-08-06T12:01:00+02:00",
                )

                summary = finalize_region(
                    layout=layout,
                    staging=staging,
                    region="Abruzzo",
                    run_id="run-1",
                    started_at="2026-08-06T12:00:00+02:00",
                    completed_at="2026-08-06T12:02:00+02:00",
                    total_results=3,
                    total_pages=1,
                )

            payload = json.loads(
                layout.certificates_path.read_text(encoding="utf-8")
            )

            self.assertEqual(summary.tables_processed, 3)
            self.assertEqual(summary.unique_records, 2)
            self.assertEqual(summary.duplicates_collapsed, 1)
            self.assertEqual(payload["region"], "Abruzzo")
            self.assertEqual(len(payload["records"]), 2)
            self.assertEqual(
                sorted(
                    record["source_occurrences"]
                    for record in payload["records"]
                ),
                [1, 2],
            )
            self.assertTrue(layout.index_path.is_file())
            self.assertTrue(layout.last_run_path.is_file())
            self.assertEqual(
                len(RegionalIndex.load(layout.index_path).entries),
                2,
            )

    def test_classifies_changed_content_as_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = RegionStorageLayout(Path(temporary_directory))

            with StagingDatabase(layout) as first_staging:
                first_staging.stage_page(
                    (build_record(),),
                    url_page=0,
                    completed_at="2026-08-06T12:01:00+02:00",
                )
                finalize_region(
                    layout=layout,
                    staging=first_staging,
                    region="Abruzzo",
                    run_id="run-1",
                    started_at="2026-08-06T12:00:00+02:00",
                    completed_at="2026-08-06T12:02:00+02:00",
                    total_results=1,
                    total_pages=1,
                )

            layout.remove_staging()

            with StagingDatabase(layout) as second_staging:
                second_staging.stage_page(
                    (build_record(scope="Scopo aggiornato."),),
                    url_page=0,
                    completed_at="2026-08-07T12:01:00+02:00",
                )
                summary = finalize_region(
                    layout=layout,
                    staging=second_staging,
                    region="Abruzzo",
                    run_id="run-2",
                    started_at="2026-08-07T12:00:00+02:00",
                    completed_at="2026-08-07T12:02:00+02:00",
                    total_results=1,
                    total_pages=1,
                )

            self.assertEqual(summary.updated_records, 1)
            self.assertEqual(summary.missing_records, 0)


if __name__ == "__main__":
    unittest.main()
