"""
Gestione dei file e delle directory dell'Accredia Downloader.

Questo modulo si occupa esclusivamente di:

- creare la struttura documenti/accredia;
- calcolare il percorso di ogni certificato;
- scrivere i JSON atomicamente;
- mantenere i nomi compatibili con Windows.

L'indice degli aggiornamenti verrà aggiunto nel prossimo blocco.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from accredia_downloader.models import CertificateRecord


@dataclass(frozen=True, slots=True)
class StorageLayout:
    """Descrive la struttura delle directory dello scraper."""

    root: Path

    def __post_init__(self) -> None:
        # Il percorso viene trasformato in assoluto una sola volta.
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

    def create_directories(self) -> None:
        """
        Crea le directory principali.

        `exist_ok=True` permette di richiamare il metodo più volte senza
        generare errori.
        """

        for directory in (
            self.pages_dir,
            self.state_dir,
            self.runs_dir,
            self.archive_dir,
            self.errors_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def page_dir(self, page: int) -> Path:
        """
        Restituisce la directory one-based di una pagina.

        Usiamo `pages/1`, `pages/2`, ecc. senza riempimento con zeri.
        """

        if page < 1:
            raise ValueError("Il numero della pagina deve partire da 1.")

        return self.pages_dir / str(page)

    def certificate_path(
        self,
        record: CertificateRecord,
    ) -> Path:
        """
        Calcola il percorso definitivo del certificato.

        Il nome contiene soltanto caratteri sicuri per Windows:
        `cert-` seguito da 64 caratteri esadecimali.
        """

        filename = f"cert-{record.record_id}.json"

        return self.page_dir(record.source.page) / filename


def atomic_write_text(
    destination: Path,
    content: str,
    *,
    retries: int = 3,
    retry_delay_seconds: float = 0.25,
) -> None:
    """
    Scrive un file di testo senza rischiare di lasciarlo troncato.

    Il contenuto viene prima scritto in un file temporaneo nella stessa
    directory. Soltanto dopo la chiusura del file temporaneo viene effettuata
    la sostituzione del file definitivo.

    Scrivere il temporaneo nella stessa directory garantisce che la rinomina
    avvenga sullo stesso filesystem.
    """

    if retries < 0:
        raise ValueError("Il numero di retry non può essere negativo.")

    if retry_delay_seconds < 0:
        raise ValueError("Il ritardo dei retry non può essere negativo.")

    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None

    try:
        # `delete=False` è necessario su Windows: un file aperto non può
        # essere rinominato o sostituito.
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()

            temporary_path = Path(temporary_file.name)

        # A questo punto il file temporaneo è stato chiuso.
        for attempt in range(retries + 1):
            try:
                temporary_path.replace(destination)
                return
            except OSError:
                if attempt >= retries:
                    raise

                # Antivirus e indicizzazione Windows possono mantenere il
                # file occupato per pochi millisecondi.
                time.sleep(
                    retry_delay_seconds * (attempt + 1)
                )

    finally:
        # Se la sostituzione fallisce definitivamente, rimuoviamo il
        # temporaneo senza nascondere l'errore originale.
        if temporary_path and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


def write_certificate(
    layout: StorageLayout,
    record: CertificateRecord,
    *,
    retries: int = 3,
) -> Path:
    """
    Salva un certificato e restituisce il suo percorso definitivo.

    Se il file esiste già viene sostituito atomicamente.
    Il controllo degli aggiornamenti verrà introdotto nel prossimo blocco.
    """

    destination = layout.certificate_path(record)

    atomic_write_text(
        destination,
        record.to_json(),
        retries=retries,
    )

    return destination