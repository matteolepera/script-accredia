"""Selezione delle regioni per i download automatici."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from accredia_downloader.storage import RegionStorageLayout


# I valori coincidono con le denominazioni mostrate dalla ricerca Accredia.
ITALIAN_REGIONS = (
    "Abruzzo",
    "Basilicata",
    "Calabria",
    "Campania",
    "Emilia-Romagna",
    "Friuli-Venezia Giulia",
    "Lazio",
    "Liguria",
    "Lombardia",
    "Marche",
    "Molise",
    "Piemonte",
    "Puglia",
    "Sardegna",
    "Sicilia",
    "Toscana",
    "Trentino-Alto Adige",
    "Umbria",
    "Valle d'Aosta",
    "Veneto",
)


@dataclass(frozen=True, slots=True)
class RegionDecision:
    """Decisione presa per una regione durante un ciclo nazionale."""

    region: str
    should_download: bool
    reason: str
    completed_at: datetime | None = None


def read_completed_at(
    layout: RegionStorageLayout,
    region: str,
) -> datetime | None:
    """Legge la data di uno snapshot locale completo e coerente."""

    required_paths = (
        layout.certificates_path,
        layout.index_path,
        layout.last_run_path,
    )

    if not all(path.is_file() for path in required_paths):
        return None

    try:
        with layout.last_run_path.open("r", encoding="utf-8") as report_file:
            payload = json.load(report_file)

        if payload.get("status") != "completed":
            return None

        if payload.get("region") != region:
            return None

        completed_at = datetime.fromisoformat(str(payload["completed_at"]))

        # I report prodotti dal downloader sono sempre timezone-aware.
        if completed_at.tzinfo is None:
            return None

        return completed_at
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        # Uno stato danneggiato non viene considerato completo: il download
        # ricostruirà i file senza fidarsi di metadati parziali.
        return None


def decide_region_download(
    *,
    layout: RegionStorageLayout,
    region: str,
    refresh_existing: bool,
    refresh_after_days: int | None,
    now: datetime | None = None,
) -> RegionDecision:
    """Decide se scaricare, aggiornare o saltare una regione."""

    completed_at = read_completed_at(layout, region)

    if completed_at is None:
        return RegionDecision(
            region=region,
            should_download=True,
            reason="snapshot mancante o incompleto",
        )

    if refresh_existing:
        return RegionDecision(
            region=region,
            should_download=True,
            reason="aggiornamento completo richiesto",
            completed_at=completed_at,
        )

    if refresh_after_days is not None:
        reference_time = now or datetime.now().astimezone()
        age = reference_time - completed_at.astimezone(
            reference_time.tzinfo
        )

        if age >= timedelta(days=refresh_after_days):
            return RegionDecision(
                region=region,
                should_download=True,
                reason=(
                    f"snapshot più vecchio di {refresh_after_days} giorni"
                ),
                completed_at=completed_at,
            )

        return RegionDecision(
            region=region,
            should_download=False,
            reason=(
                f"snapshot più recente di {refresh_after_days} giorni"
            ),
            completed_at=completed_at,
        )

    return RegionDecision(
        region=region,
        should_download=False,
        reason="snapshot già completo",
        completed_at=completed_at,
    )
