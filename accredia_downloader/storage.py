"""
Archiviazione e indice degli aggiornamenti.

Il modulo gestisce:

- struttura delle directory;
- scrittura atomica compatibile con Windows;
- indice JSONL dei record;
- riconoscimento di nuovi record e aggiornamenti;
- spostamenti tra pagine;
- ripristino dei file mancanti.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from accredia_downloader.models import CertificateRecord


INDEX_FILENAME = "records-index.jsonl"


# ---------------------------------------------------------------------------
# Tipi di modifica
# ---------------------------------------------------------------------------

class RecordChange(str, Enum):
    """Possibili risultati del salvataggio di un record."""

    NEW = "new"
    UPDATED = "updated"
    MOVED = "moved"
    UNCHANGED = "unchanged"
    RECOVERED = "recovered"


@dataclass(frozen=True, slots=True)
class SaveResult:
    """Risultato del salvataggio di un certificato."""

    change: RecordChange
    path: Path
    previous_path: Path | None = None


# ---------------------------------------------------------------------------
# Struttura delle directory
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class StorageLayout:
    """Descrive la struttura delle directory dello scraper."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    @property
    def pages_dir(self) -> Path:
        return self.root / "pages"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def errors_dir(self) -> Path:
        return self.root / "errors"

    @property
    def index_path(self) -> Path:
        return self.state_dir / INDEX_FILENAME

    def create_directories(self) -> None:
        """Crea tutte le directory principali."""

        for directory in (
            self.pages_dir,
            self.state_dir,
            self.runs_dir,
            self.archive_dir,
            self.errors_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def page_dir(self, page: int) -> Path:
        """Restituisce la directory one-based di una pagina."""

        if page < 1:
            raise ValueError("Il numero della pagina deve partire da 1.")

        return self.pages_dir / str(page)

    def certificate_path(
        self,
        record: CertificateRecord,
    ) -> Path:
        """Calcola il percorso definitivo di un certificato."""

        filename = f"cert-{record.record_id}.json"
        return self.page_dir(record.source.page) / filename

    def relative_path(self, path: Path) -> str:
        """
        Converte un percorso in formato portabile.

        Nell'indice utilizziamo sempre `/`, anche quando il programma
        viene eseguito su Windows.
        """

        return path.resolve().relative_to(self.root).as_posix()

    def absolute_path(self, relative_path: str) -> Path:
        """
        Ricostruisce un percorso locale partendo dal formato dell'indice.

        Il controllo di `..` impedisce che un indice danneggiato possa
        puntare fuori dalla directory principale.
        """

        portable_path = PurePosixPath(relative_path)

        if portable_path.is_absolute() or ".." in portable_path.parts:
            raise ValueError(
                f"Percorso non valido nell'indice: {relative_path}"
            )

        return self.root.joinpath(*portable_path.parts)


# ---------------------------------------------------------------------------
# Scrittura atomica
# ---------------------------------------------------------------------------

def atomic_write_lines(
    destination: Path,
    lines: Iterable[str],
    *,
    retries: int = 10000,
    retry_delay_seconds: float = 0.25,
) -> None:
    """
    Scrive una sequenza di stringhe in modo atomico.

    L'uso di un iterabile permette di salvare un indice molto grande senza
    costruire l'intero contenuto in memoria.
    """

    if retries < 0:
        raise ValueError("Il numero di retry non può essere negativo.")

    if retry_delay_seconds < 0:
        raise ValueError("Il ritardo dei retry non può essere negativo.")

    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None

    try:
        # Su Windows il file deve essere chiuso prima della sostituzione.
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

                time.sleep(
                    retry_delay_seconds * (attempt + 1)
                )

    finally:
        if temporary_path and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


def atomic_write_text(
    destination: Path,
    content: str,
    *,
    retries: int = 3,
    retry_delay_seconds: float = 0.25,
) -> None:
    """Scrive atomicamente una singola stringa."""

    atomic_write_lines(
        destination,
        (content,),
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
    )


def remove_file(
    path: Path,
    *,
    retries: int = 3,
    retry_delay_seconds: float = 0.25,
) -> None:
    """Rimuove un file con retry per eventuali blocchi temporanei Windows."""

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


def write_certificate(
    layout: StorageLayout,
    record: CertificateRecord,
    *,
    retries: int = 3,
) -> Path:
    """Scrive il JSON del certificato e restituisce il percorso."""

    destination = layout.certificate_path(record)

    atomic_write_text(
        destination,
        record.to_json(),
        retries=retries,
    )

    return destination


# ---------------------------------------------------------------------------
# Indice JSONL
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RecordIndexEntry:
    """Voce dell'indice locale di un certificato/sede."""

    record_id: str
    content_hash: str
    path: str
    first_seen_run: str
    last_seen_run: str
    source_page: int
    source_position: int
    missing_runs: int = 0

    def to_dict(self) -> dict[str, str | int]:
        return {
            "record_id": self.record_id,
            "content_hash": self.content_hash,
            "path": self.path,
            "first_seen_run": self.first_seen_run,
            "last_seen_run": self.last_seen_run,
            "source_page": self.source_page,
            "source_position": self.source_position,
            "missing_runs": self.missing_runs,
        }

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, object],
    ) -> RecordIndexEntry:
        """Ricostruisce e valida una voce letta dal file JSONL."""

        return cls(
            record_id=str(payload["record_id"]),
            content_hash=str(payload["content_hash"]),
            path=str(payload["path"]),
            first_seen_run=str(payload["first_seen_run"]),
            last_seen_run=str(payload["last_seen_run"]),
            source_page=int(payload["source_page"]),
            source_position=int(payload["source_position"]),
            missing_runs=int(payload.get("missing_runs", 0)),
        )


class RecordIndex:
    """Indice in memoria dei record già conosciuti."""

    def __init__(
        self,
        entries: dict[str, RecordIndexEntry] | None = None,
    ) -> None:
        self.entries = entries or {}

    @classmethod
    def load(cls, index_path: Path) -> RecordIndex:
        """
        Carica l'indice una riga alla volta.

        Un file inesistente rappresenta semplicemente una prima esecuzione.
        """

        if not index_path.exists():
            return cls()

        entries: dict[str, RecordIndexEntry] = {}

        with index_path.open(
            mode="r",
            encoding="utf-8",
        ) as index_file:
            for line_number, line in enumerate(index_file, start=1):
                stripped_line = line.strip()

                if not stripped_line:
                    continue

                try:
                    payload = json.loads(stripped_line)
                    entry = RecordIndexEntry.from_dict(payload)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ValueError(
                        "Indice non valido alla riga "
                        f"{line_number}: {index_path}"
                    ) from error

                if entry.record_id in entries:
                    raise ValueError(
                        "Record duplicato nell'indice: "
                        f"{entry.record_id}"
                    )

                entries[entry.record_id] = entry

        return cls(entries)

    def get(
        self,
        record_id: str,
    ) -> RecordIndexEntry | None:
        return self.entries.get(record_id)

    def set(self, entry: RecordIndexEntry) -> None:
        self.entries[entry.record_id] = entry

    def save(self, index_path: Path) -> None:
        """
        Salva l'indice ordinato per record_id.

        Le righe vengono prodotte tramite generatore per limitare l'uso
        di memoria anche con centinaia di migliaia di record.
        """

        def generate_lines() -> Iterable[str]:
            for record_id in sorted(self.entries):
                entry = self.entries[record_id]

                yield (
                    json.dumps(
                        entry.to_dict(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )

        atomic_write_lines(index_path, generate_lines())

    def mark_missing(
        self,
        current_run_id: str,
    ) -> int:
        """
        Incrementa `missing_runs` per i record non visti nel ciclo corrente.

        Restituisce il numero di record mancanti.
        """

        missing_count = 0

        for entry in self.entries.values():
            if entry.last_seen_run != current_run_id:
                entry.missing_runs += 1
                missing_count += 1
            else:
                entry.missing_runs = 0

        return missing_count


# ---------------------------------------------------------------------------
# Gestore principale dell'archiviazione
# ---------------------------------------------------------------------------

class StorageManager:
    """Coordina file JSON e indice degli aggiornamenti."""

    def __init__(
        self,
        layout: StorageLayout,
        index: RecordIndex,
    ) -> None:
        self.layout = layout
        self.index = index

    @classmethod
    def open(cls, root: Path) -> StorageManager:
        """Apre o inizializza l'archivio locale."""

        layout = StorageLayout(root)
        layout.create_directories()

        index = RecordIndex.load(layout.index_path)

        return cls(layout=layout, index=index)

    def save_record(
        self,
        record: CertificateRecord,
        *,
        run_id: str,
        retries: int = 3,
    ) -> SaveResult:
        """
        Salva un record soltanto quando necessario.

        L'indice viene aggiornato in memoria. Il chiamante potrà salvarlo
        su disco al completamento della pagina.
        """

        if not run_id.strip():
            raise ValueError("run_id non può essere vuoto.")

        destination = self.layout.certificate_path(record)
        existing = self.index.get(record.record_id)

        previous_path: Path | None = None

        if existing is None:
            change = RecordChange.NEW
            first_seen_run = run_id

        else:
            previous_path = self.layout.absolute_path(existing.path)
            first_seen_run = existing.first_seen_run

            content_changed = (
                existing.content_hash != record.content_hash
            )

            source_changed = (
                existing.source_page != record.source.page
                or existing.source_position != record.source.position
            )

            path_changed = previous_path != destination
            file_missing = not previous_path.is_file()

            if content_changed:
                change = RecordChange.UPDATED
            elif file_missing:
                change = RecordChange.RECOVERED
            elif source_changed or path_changed:
                change = RecordChange.MOVED
            else:
                change = RecordChange.UNCHANGED

        # I record invariati non vengono riscritti.
        if change is not RecordChange.UNCHANGED:
            write_certificate(
                self.layout,
                record,
                retries=retries,
            )

            # Rimuoviamo il vecchio file soltanto dopo aver scritto
            # correttamente quello nuovo.
            if (
                previous_path is not None
                and previous_path != destination
                and previous_path.exists()
            ):
                remove_file(
                    previous_path,
                    retries=retries,
                )

        entry = RecordIndexEntry(
            record_id=record.record_id,
            content_hash=record.content_hash,
            path=self.layout.relative_path(destination),
            first_seen_run=first_seen_run,
            last_seen_run=run_id,
            source_page=record.source.page,
            source_position=record.source.position,
            missing_runs=0,
        )

        self.index.set(entry)

        return SaveResult(
            change=change,
            path=destination,
            previous_path=previous_path,
        )

    def save_index(self) -> None:
        """Salva atomicamente l'indice corrente."""

        self.index.save(self.layout.index_path)

    def finish_run(self, run_id: str) -> int:
        """
        Completa il ciclo e marca i record non incontrati.

        L'archiviazione dopo due assenze consecutive verrà implementata
        separatamente.
        """

        missing_count = self.index.mark_missing(run_id)
        self.save_index()

        return missing_count