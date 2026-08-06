"""Storage regionale transazionale ed esportazione JSON atomica."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from accredia_downloader.models import (
    SCHEMA_VERSION,
    CertificateRecord,
)


INDEX_FILENAME = "records-index.jsonl"
LAST_RUN_FILENAME = "last-run.json"
STAGING_FILENAME = "staging.sqlite"
CERTIFICATES_FILENAME = "certificati.json"


class StorageError(RuntimeError):
    """Errore nello staging o nell'esportazione regionale."""


@dataclass(frozen=True, slots=True)
class RegionStorageLayout:
    """Percorsi utilizzati da una singola regione."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    @property
    def certificates_path(self) -> Path:
        return self.root / CERTIFICATES_FILENAME

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def staging_path(self) -> Path:
        return self.state_dir / STAGING_FILENAME

    @property
    def index_path(self) -> Path:
        return self.state_dir / INDEX_FILENAME

    @property
    def last_run_path(self) -> Path:
        return self.state_dir / LAST_RUN_FILENAME

    def create_directories(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def staging_files(self) -> tuple[Path, ...]:
        """Database SQLite e possibili file WAL temporanei."""

        return (
            self.staging_path,
            Path(f"{self.staging_path}-wal"),
            Path(f"{self.staging_path}-shm"),
        )

    def remove_staging(self, *, retries: int = 3) -> None:
        """Elimina soltanto i file temporanei dello staging."""

        for path in self.staging_files():
            remove_file(path, retries=retries)


def atomic_write_lines(
    destination: Path,
    lines: Iterable[str],
    *,
    retries: int = 3,
    retry_delay_seconds: float = 0.25,
) -> None:
    """Scrive un file UTF-8 e lo sostituisce atomicamente."""

    if retries < 0:
        raise ValueError("Il numero di retry non può essere negativo.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None

    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary_file:
            for line in lines:
                temporary_file.write(line)

            temporary_file.flush()
            temporary_path = Path(temporary_file.name)

        for attempt in range(retries + 1):
            try:
                temporary_path.replace(destination)
                return
            except OSError:
                if attempt >= retries:
                    raise

                time.sleep(retry_delay_seconds * (attempt + 1))
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


def atomic_write_text(
    destination: Path,
    content: str,
    *,
    retries: int = 3,
) -> None:
    """Scrive atomicamente una singola stringa."""

    atomic_write_lines(destination, (content,), retries=retries)


def atomic_write_json(
    destination: Path,
    payload: dict[str, object],
    *,
    retries: int = 3,
) -> None:
    """Serializza e salva atomicamente un documento JSON."""

    content = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    atomic_write_text(destination, content, retries=retries)


def remove_file(
    path: Path,
    *,
    retries: int = 3,
    retry_delay_seconds: float = 0.25,
) -> None:
    """Rimuove un file con retry per i lock temporanei di Windows."""

    if not path.exists():
        return

    for attempt in range(retries + 1):
        try:
            path.unlink()
            return
        except OSError:
            if attempt >= retries:
                raise

            time.sleep(retry_delay_seconds * (attempt + 1))


@dataclass(frozen=True, slots=True)
class RegionalIndexEntry:
    """Voce compatta usata per confrontare due snapshot regionali."""

    record_id: str
    entity_id: str
    content_hash: str
    first_seen_run: str
    last_seen_run: str

    def to_dict(self) -> dict[str, str]:
        return {
            "record_id": self.record_id,
            "entity_id": self.entity_id,
            "content_hash": self.content_hash,
            "first_seen_run": self.first_seen_run,
            "last_seen_run": self.last_seen_run,
        }


class RegionalIndex:
    """Indice regionale mantenuto separato dal JSON con gli HTML."""

    def __init__(
        self,
        entries: dict[str, RegionalIndexEntry] | None = None,
    ) -> None:
        self.entries = entries or {}

    @classmethod
    def load(cls, path: Path) -> RegionalIndex:
        if not path.exists():
            return cls()

        entries: dict[str, RegionalIndexEntry] = {}

        with path.open("r", encoding="utf-8") as index_file:
            for line_number, line in enumerate(index_file, start=1):
                if not line.strip():
                    continue

                try:
                    payload = json.loads(line)
                    entry = RegionalIndexEntry(
                        record_id=str(payload["record_id"]),
                        entity_id=str(payload["entity_id"]),
                        content_hash=str(payload["content_hash"]),
                        first_seen_run=str(payload["first_seen_run"]),
                        last_seen_run=str(payload["last_seen_run"]),
                    )
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    json.JSONDecodeError,
                ) as error:
                    raise StorageError(
                        f"Indice non valido alla riga {line_number}: {path}"
                    ) from error

                if entry.record_id in entries:
                    raise StorageError(
                        f"record_id duplicato nell'indice: {entry.record_id}"
                    )

                entries[entry.record_id] = entry

        return cls(entries)

    def save(self, path: Path, *, retries: int = 3) -> None:
        def generate_lines() -> Iterator[str]:
            for record_id in sorted(self.entries):
                yield json.dumps(
                    self.entries[record_id].to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ) + "\n"

        atomic_write_lines(path, generate_lines(), retries=retries)


@dataclass(frozen=True, slots=True)
class RegionalRunSummary:
    """Statistiche prodotte al completamento di una regione."""

    region: str
    total_results: int
    total_pages: int
    tables_processed: int
    unique_records: int
    duplicates_collapsed: int
    new_records: int
    updated_records: int
    unchanged_records: int
    missing_records: int
    output_path: Path


class StagingDatabase:
    """Database SQLite temporaneo con transazioni per pagina."""

    def __init__(self, layout: RegionStorageLayout) -> None:
        self.layout = layout
        self.layout.create_directories()
        self.connection = sqlite3.connect(
            self.layout.staging_path,
            timeout=30,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    def __enter__(self) -> StagingDatabase:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if self.connection is None:
            return

        try:
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            self.connection.close()
            self.connection = None  # type: ignore[assignment]

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                entity_id TEXT NOT NULL,
                certificate_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                document_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS records_entity_id_idx
            ON records(entity_id);

            CREATE TABLE IF NOT EXISTS occurrences (
                url_page INTEGER NOT NULL,
                position INTEGER NOT NULL,
                record_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                scraped_at TEXT NOT NULL,
                PRIMARY KEY (url_page, position),
                FOREIGN KEY (record_id)
                    REFERENCES records(record_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS occurrences_record_id_idx
            ON occurrences(record_id);

            CREATE TABLE IF NOT EXISTS pages (
                url_page INTEGER PRIMARY KEY,
                record_count INTEGER NOT NULL,
                completed_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def set_metadata(self, **values: str | int) -> None:
        with self.connection:
            for key, value in values.items():
                self.connection.execute(
                    """
                    INSERT INTO metadata(key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, str(value)),
                )

    def metadata(self) -> dict[str, str]:
        rows = self.connection.execute(
            "SELECT key, value FROM metadata"
        )
        return {str(row["key"]): str(row["value"]) for row in rows}

    def completed_pages(self) -> set[int]:
        rows = self.connection.execute("SELECT url_page FROM pages")
        return {int(row["url_page"]) for row in rows}

    def stage_page(
        self,
        records: Iterable[CertificateRecord],
        *,
        url_page: int,
        completed_at: str,
    ) -> None:
        """Registra una pagina completa in una singola transazione."""

        page_records = tuple(records)

        if url_page < 0:
            raise ValueError("url_page non può essere negativo.")

        positions = [record.source.position for record in page_records]

        if len(positions) != len(set(positions)):
            raise StorageError(
                f"Pagina {url_page + 1}: posizioni duplicate."
            )

        if any(record.source.url_page != url_page for record in page_records):
            raise StorageError(
                f"Pagina {url_page + 1}: metadati sorgente incoerenti."
            )

        with self.connection:
            # Una pagina ritentata sostituisce integralmente la versione
            # incompleta precedente senza incrementare le occorrenze.
            self.connection.execute(
                "DELETE FROM occurrences WHERE url_page = ?",
                (url_page,),
            )
            self.connection.execute(
                "DELETE FROM pages WHERE url_page = ?",
                (url_page,),
            )
            self.connection.execute(
                """
                DELETE FROM records
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM occurrences
                    WHERE occurrences.record_id = records.record_id
                )
                """
            )

            for record in page_records:
                document_json = json.dumps(
                    record.to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )

                self.connection.execute(
                    """
                    INSERT INTO records(
                        record_id,
                        entity_id,
                        certificate_id,
                        content_hash,
                        document_json
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(record_id) DO NOTHING
                    """,
                    (
                        record.record_id,
                        record.entity_id,
                        record.certificate_id,
                        record.content_hash,
                        document_json,
                    ),
                )
                self.connection.execute(
                    """
                    INSERT INTO occurrences(
                        url_page,
                        position,
                        record_id,
                        source_url,
                        scraped_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        url_page,
                        record.source.position,
                        record.record_id,
                        record.source.url,
                        record.source.scraped_at,
                    ),
                )

            self.connection.execute(
                """
                INSERT INTO pages(url_page, record_count, completed_at)
                VALUES (?, ?, ?)
                """,
                (url_page, len(page_records), completed_at),
            )

    def validate_complete(
        self,
        *,
        total_results: int,
        total_pages: int,
    ) -> None:
        """Verifica pagine e tabelle prima dell'esportazione."""

        rows = self.connection.execute(
            "SELECT url_page, record_count FROM pages ORDER BY url_page"
        ).fetchall()
        page_numbers = [int(row["url_page"]) for row in rows]
        expected_pages = list(range(total_pages))

        if page_numbers != expected_pages:
            raise StorageError(
                "Lo staging non contiene tutte le pagine regionali."
            )

        recorded_tables = sum(int(row["record_count"]) for row in rows)
        occurrence_count = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM occurrences"
            ).fetchone()[0]
        )

        if recorded_tables != total_results:
            raise StorageError(
                f"Tabelle registrate {recorded_tables}, "
                f"risultati dichiarati {total_results}."
            )

        if occurrence_count != recorded_tables:
            raise StorageError(
                "Il conteggio delle occorrenze non coincide con le tabelle."
            )

    def counts(self) -> tuple[int, int, int]:
        """Restituisce tabelle, record unici e duplicati accorpati."""

        tables = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM occurrences"
            ).fetchone()[0]
        )
        unique = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]
        )
        return tables, unique, tables - unique

    def current_index_rows(self) -> list[tuple[str, str, str]]:
        rows = self.connection.execute(
            """
            SELECT record_id, entity_id, content_hash
            FROM records
            ORDER BY record_id
            """
        )
        return [
            (
                str(row["record_id"]),
                str(row["entity_id"]),
                str(row["content_hash"]),
            )
            for row in rows
        ]

    def iter_export_records(self) -> Iterator[dict[str, object]]:
        """Produce i record finali senza caricarli tutti in memoria."""

        rows = self.connection.execute(
            """
            WITH ranked_occurrences AS (
                SELECT
                    record_id,
                    url_page,
                    position,
                    source_url,
                    scraped_at,
                    COUNT(*) OVER (
                        PARTITION BY record_id
                    ) AS occurrence_count,
                    ROW_NUMBER() OVER (
                        PARTITION BY record_id
                        ORDER BY url_page, position
                    ) AS occurrence_rank
                FROM occurrences
            )
            SELECT
                records.document_json,
                ranked_occurrences.url_page,
                ranked_occurrences.position,
                ranked_occurrences.source_url,
                ranked_occurrences.scraped_at,
                ranked_occurrences.occurrence_count
            FROM records
            JOIN ranked_occurrences
                ON ranked_occurrences.record_id = records.record_id
            WHERE ranked_occurrences.occurrence_rank = 1
            ORDER BY
                ranked_occurrences.url_page,
                ranked_occurrences.position,
                records.record_id
            """
        )

        for row in rows:
            document = json.loads(str(row["document_json"]))
            source = document["source"]
            source["url"] = str(row["source_url"])
            source["url_page"] = int(row["url_page"])
            source["page"] = int(row["url_page"]) + 1
            source["position"] = int(row["position"])
            source["scraped_at"] = str(row["scraped_at"])
            document["source_occurrences"] = int(
                row["occurrence_count"]
            )
            yield document


def finalize_region(
    *,
    layout: RegionStorageLayout,
    staging: StagingDatabase,
    region: str,
    run_id: str,
    started_at: str,
    completed_at: str,
    total_results: int,
    total_pages: int,
    retries: int = 3,
) -> RegionalRunSummary:
    """Valida lo staging e pubblica il nuovo snapshot regionale."""

    staging.validate_complete(
        total_results=total_results,
        total_pages=total_pages,
    )
    tables, unique, duplicates = staging.counts()

    previous_index = RegionalIndex.load(layout.index_path)
    previous_entities = {
        entry.entity_id
        for entry in previous_index.entries.values()
    }
    current_rows = staging.current_index_rows()
    current_entities = {row[1] for row in current_rows}

    new_records = 0
    updated_records = 0
    unchanged_records = 0
    next_entries: dict[str, RegionalIndexEntry] = {}

    for record_id, entity_id, content_hash in current_rows:
        previous = previous_index.entries.get(record_id)

        if previous is not None:
            unchanged_records += 1
            first_seen_run = previous.first_seen_run
        elif entity_id in previous_entities:
            updated_records += 1
            first_seen_run = run_id
        else:
            new_records += 1
            first_seen_run = run_id

        next_entries[record_id] = RegionalIndexEntry(
            record_id=record_id,
            entity_id=entity_id,
            content_hash=content_hash,
            first_seen_run=first_seen_run,
            last_seen_run=run_id,
        )

    missing_records = sum(
        1
        for entry in previous_index.entries.values()
        if entry.entity_id not in current_entities
    )

    def generate_region_json() -> Iterator[str]:
        yield "{\n"
        yield '  "schema_version": 1,\n'
        yield f'  "record_schema_version": {SCHEMA_VERSION},\n'
        yield f'  "region": {json.dumps(region, ensure_ascii=False)},\n'
        yield f'  "run_id": {json.dumps(run_id)},\n'
        yield f'  "started_at": {json.dumps(started_at)},\n'
        yield f'  "completed_at": {json.dumps(completed_at)},\n'
        yield f'  "total_results_reported": {total_results},\n'
        yield f'  "total_pages": {total_pages},\n'
        yield f'  "tables_processed": {tables},\n'
        yield f'  "unique_records": {unique},\n'
        yield f'  "duplicates_collapsed": {duplicates},\n'
        yield '  "records": [\n'

        first_record = True

        for document in staging.iter_export_records():
            if not first_record:
                yield ",\n"

            encoded = json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
            )
            yield "    " + encoded.replace("\n", "\n    ")
            first_record = False

        yield "\n  ]\n}\n"

    atomic_write_lines(
        layout.certificates_path,
        generate_region_json(),
        retries=retries,
    )

    RegionalIndex(next_entries).save(
        layout.index_path,
        retries=retries,
    )

    atomic_write_json(
        layout.last_run_path,
        {
            "region": region,
            "run_id": run_id,
            "status": "completed",
            "started_at": started_at,
            "completed_at": completed_at,
            "total_results": total_results,
            "total_pages": total_pages,
            "tables_processed": tables,
            "unique_records": unique,
            "duplicates_collapsed": duplicates,
            "new_records": new_records,
            "updated_records": updated_records,
            "unchanged_records": unchanged_records,
            "missing_records": missing_records,
        },
        retries=retries,
    )

    return RegionalRunSummary(
        region=region,
        total_results=total_results,
        total_pages=total_pages,
        tables_processed=tables,
        unique_records=unique,
        duplicates_collapsed=duplicates,
        new_records=new_records,
        updated_records=updated_records,
        unchanged_records=unchanged_records,
        missing_records=missing_records,
        output_path=layout.certificates_path,
    )
