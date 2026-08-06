"""Orchestrazione del download completo di una regione."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from accredia_downloader.client import AccrediaBrowserClient
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

    compatible = bool(metadata) and all(
        (
            metadata.get("region") == region,
            metadata.get("run_date") == current_date,
            metadata.get("total_results") == str(total_results),
            metadata.get("total_pages") == str(total_pages),
            metadata.get("record_schema_version")
            == str(SCHEMA_VERSION),
            metadata.get("status") == "running",
        )
    )

    if staging_existed and not compatible:
        staging.close()
        layout.remove_staging(retries=retries)
        staging = StagingDatabase(layout)
        metadata = {}

    resumed = bool(metadata) and compatible

    if resumed:
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

            if url_page == 0:
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
            expected_records = expected_records_for_page(
                total_results,
                url_page,
            )

            if parsed_page.record_count != expected_records:
                raise ParsingError(
                    f"Pagina {url_page + 1} incompleta: "
                    f"trovati {parsed_page.record_count} record, "
                    f"attesi {expected_records}."
                )

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
