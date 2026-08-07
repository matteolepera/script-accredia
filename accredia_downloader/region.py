"""Orchestrazione del download completo di una regione."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime

from accredia_downloader.client import (
    AccrediaBrowserClient,
    retry_delay_seconds,
)
from accredia_downloader.models import SCHEMA_VERSION
from accredia_downloader.parser import (
    ParsingError,
    SessionExpiredError,
    build_page_url,
    calculate_total_pages,
    expected_records_for_page,
    has_captcha_gate,
    parse_result_page,
    parse_total_results,
)
from accredia_downloader.storage import (
    RegionStorageLayout,
    RegionalRunSummary,
    StagingDatabase,
    StorageError,
    finalize_region,
)


LOGGER = logging.getLogger(__name__)
PAGE_VALIDATION_RETRIES = 5


class RegionDownloadError(RuntimeError):
    """Errore nel ciclo completo di una regione."""


class RegionChangedError(RegionDownloadError):
    """Il totale regionale è cambiato durante l'esecuzione."""


StartCallback = Callable[[int, int, int, bool], None]
PageCallback = Callable[[int, int, int], None]


def _read_required_total(html: str) -> int:
    if has_captcha_gate(html):
        raise SessionExpiredError(
            "La sessione CAPTCHA non è più valida."
        )

    total_results = parse_total_results(html)

    if total_results is None:
        raise ParsingError(
            "Impossibile leggere il totale regionale."
        )

    return total_results


def _prepare_staging(
    *,
    layout: RegionStorageLayout,
    region: str,
    source_url: str,
    total_results: int,
    total_pages: int,
    started_at: datetime,
    retries: int,
) -> tuple[StagingDatabase, str, str, bool]:
    """Apre uno staging compatibile o ne crea uno nuovo."""

    staging_existed = layout.staging_path.exists()
    staging = StagingDatabase(layout)
    metadata = staging.metadata()
    current_date = started_at.date().isoformat()

    incompatibilities: list[str] = []

    if not metadata:
        incompatibilities.append("metadati assenti")
    else:
        checks = (
            ("regione", metadata.get("region"), region),
            (
                "totale risultati",
                metadata.get("total_results"),
                str(total_results),
            ),
            (
                "numero pagine",
                metadata.get("total_pages"),
                str(total_pages),
            ),
            (
                "versione schema",
                metadata.get("record_schema_version"),
                str(SCHEMA_VERSION),
            ),
            ("stato", metadata.get("status"), "running"),
        )

        for label, stored_value, current_value in checks:
            if stored_value != current_value:
                incompatibilities.append(
                    f"{label}: {stored_value!r} -> {current_value!r}"
                )

    # La data non invalida più lo staging. Per download molto lunghi è più
    # utile riprendere nei giorni successivi, purché totale e struttura della
    # ricerca siano ancora identici.
    compatible = bool(metadata) and not incompatibilities

    if staging_existed and not compatible:
        LOGGER.warning(
            "Staging precedente scartato (%s). Ripartenza dalla pagina 1.",
            "; ".join(incompatibilities),
        )
        staging.close()
        layout.remove_staging(retries=retries)
        staging = StagingDatabase(layout)
        metadata = {}

    resumed = bool(metadata) and compatible

    if resumed:
        completed_count = len(staging.completed_pages())
        LOGGER.info(
            "Staging compatibile: ripresa di %s da %s pagine completate "
            "(avviato il %s).",
            region,
            completed_count,
            metadata.get("started_at", "data sconosciuta"),
        )
        staging.set_metadata(
            last_resumed_at=started_at.isoformat(timespec="seconds"),
        )
        return (
            staging,
            metadata["run_id"],
            metadata["started_at"],
            True,
        )

    run_id = started_at.strftime("%Y%m%dT%H%M%S%z")
    started_at_text = started_at.isoformat(timespec="seconds")
    staging.set_metadata(
        region=region,
        run_id=run_id,
        run_date=current_date,
        started_at=started_at_text,
        source_url=source_url,
        total_results=total_results,
        total_pages=total_pages,
        record_schema_version=SCHEMA_VERSION,
        status="running",
    )
    return staging, run_id, started_at_text, False


def download_region(
    *,
    client: AccrediaBrowserClient,
    layout: RegionStorageLayout,
    region: str,
    results_url: str,
    delay_seconds: float,
    local_retries: int,
    on_start: StartCallback | None = None,
    on_page: PageCallback | None = None,
) -> RegionalRunSummary:
    """Scarica tutte le pagine e pubblica uno snapshot regionale."""

    started_now = datetime.now().astimezone()
    first_snapshot = client.navigate(results_url)
    total_results = _read_required_total(first_snapshot.html)
    total_pages = calculate_total_pages(total_results)

    staging, run_id, started_at, resumed = _prepare_staging(
        layout=layout,
        region=region,
        source_url=first_snapshot.url,
        total_results=total_results,
        total_pages=total_pages,
        started_at=started_now,
        retries=local_retries,
    )

    completed_successfully = False

    try:
        completed_pages = staging.completed_pages()

        if on_start is not None:
            on_start(
                total_results,
                total_pages,
                len(completed_pages),
                resumed,
            )

        completed_count = len(completed_pages)

        for url_page in range(total_pages):
            if url_page in completed_pages:
                continue

            page_url = build_page_url(first_snapshot.url, url_page)
            expected_records = expected_records_for_page(
                total_results,
                url_page,
            )

            for page_attempt in range(PAGE_VALIDATION_RETRIES + 1):
                if url_page == 0 and page_attempt == 0:
                    page_html = first_snapshot.html
                    page_url = first_snapshot.url
                else:
                    page_html = client.fetch_html(
                        page_url,
                        referer=first_snapshot.url,
                    )

                scraped_at = datetime.now().astimezone().isoformat(
                    timespec="seconds"
                )
                parsed_page = parse_result_page(
                    page_html,
                    source_url=page_url,
                    url_page=url_page,
                    scraped_at=scraped_at,
                )

                if parsed_page.record_count == expected_records:
                    break

                # Una pagina parziale può dipendere da un aggiornamento della
                # banca dati. Rileggiamo subito il totale prima di ritentare.
                current_first_html = client.fetch_html(
                    build_page_url(first_snapshot.url, 0),
                    referer=first_snapshot.url,
                )
                current_total = _read_required_total(current_first_html)

                if current_total != total_results:
                    staging.set_metadata(status="invalid")
                    raise RegionChangedError(
                        f"Pagina {url_page + 1} incoerente perché il totale "
                        f"di {region} è cambiato: {total_results} -> "
                        f"{current_total}. Lo staging verrà ricreato alla "
                        "prossima esecuzione."
                    )

                if page_attempt >= PAGE_VALIDATION_RETRIES:
                    raise ParsingError(
                        f"Pagina {url_page + 1} ancora incompleta dopo "
                        f"{PAGE_VALIDATION_RETRIES + 1} tentativi: trovati "
                        f"{parsed_page.record_count} record, attesi "
                        f"{expected_records}; totale regionale riconfermato "
                        f"a {total_results}. Le pagine precedenti restano "
                        "nello staging."
                    )

                retry_delay = retry_delay_seconds(page_attempt)
                LOGGER.warning(
                    "Pagina %s incompleta: trovati %s record, attesi %s. "
                    "Totale invariato; nuovo tentativo %s/%s tra %.0f s.",
                    url_page + 1,
                    parsed_page.record_count,
                    expected_records,
                    page_attempt + 2,
                    PAGE_VALIDATION_RETRIES + 1,
                    retry_delay,
                )
                time.sleep(retry_delay)

            staging.stage_page(
                parsed_page.records,
                url_page=url_page,
                completed_at=scraped_at,
            )
            completed_count += 1

            if on_page is not None:
                on_page(url_page, completed_count, total_pages)

            if completed_count < total_pages:
                time.sleep(delay_seconds)

        # Il totale viene ricontrollato prima di pubblicare lo snapshot.
        final_html = client.fetch_html(
            build_page_url(first_snapshot.url, 0),
            referer=first_snapshot.url,
        )
        final_total = _read_required_total(final_html)

        if final_total != total_results:
            staging.set_metadata(status="invalid")
            raise RegionChangedError(
                f"Il totale di {region} è cambiato durante il download: "
                f"{total_results} -> {final_total}. La regione verrà "
                "riscaricata dalla pagina 1."
            )

        completed_at = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        summary = finalize_region(
            layout=layout,
            staging=staging,
            region=region,
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            total_results=total_results,
            total_pages=total_pages,
            retries=local_retries,
        )
        completed_successfully = True
        return summary
    except (ParsingError, StorageError):
        raise
    finally:
        staging.close()

        if completed_successfully:
            layout.remove_staging(retries=local_retries)
