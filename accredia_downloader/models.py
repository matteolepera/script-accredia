"""
Modelli dati e identificatori dell'Accredia Downloader.

Questo modulo non effettua richieste HTTP e non scrive file. Si occupa di:

- normalizzare i valori estratti dall'HTML;
- rappresentare un certificato/sede;
- generare identificatori SHA-256 stabili;
- calcolare l'hash dei contenuti;
- produrre il documento JSON finale.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any


SCHEMA_VERSION = 3

# Espressione compilata una sola volta e riutilizzata per tutti i record.
WHITESPACE_PATTERN = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Normalizzazione e hashing
# ---------------------------------------------------------------------------

def normalize_text(value: str | None) -> str:
    """
    Ripulisce un valore senza modificarne il significato.

    NFKC uniforma caratteri Unicode equivalenti e aiuta a evitare hash
    differenti per testi visivamente uguali.
    """

    if not value:
        return ""

    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.replace("\xa0", " ")

    return WHITESPACE_PATTERN.sub(" ", normalized).strip()


def normalize_identity(value: str | None) -> str:
    """
    Normalizza un valore usato nella costruzione di un identificatore.

    `casefold()` è più affidabile di `lower()` con i caratteri Unicode.
    """

    return normalize_text(value).casefold()


def canonical_json(payload: dict[str, Any]) -> str:
    """
    Serializza un dizionario in modo deterministico.

    Ordinamento e separatori compatti garantiscono che lo stesso contenuto
    produca sempre lo stesso hash.
    """

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_payload(payload: dict[str, Any]) -> str:
    """Calcola lo SHA-256 di un dizionario serializzato canonicamente."""

    encoded_payload = canonical_json(payload).encode("utf-8")
    return hashlib.sha256(encoded_payload).hexdigest()


# ---------------------------------------------------------------------------
# Modelli secondari
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AccreditationBody:
    """Organismo che ha emesso il certificato."""

    code: str
    name: str
    detail_url: str
    website: str

    def __post_init__(self) -> None:
        # Anche i modelli costruiti manualmente vengono normalizzati.
        object.__setattr__(self, "code", normalize_text(self.code))
        object.__setattr__(self, "name", normalize_text(self.name))
        object.__setattr__(
            self,
            "detail_url",
            normalize_text(self.detail_url),
        )
        object.__setattr__(
            self,
            "website",
            normalize_text(self.website),
        )

    def to_dict(self) -> dict[str, str]:
        """Restituisce una rappresentazione pronta per il JSON."""

        return {
            "code": self.code,
            "name": self.name,
            "detail_url": self.detail_url,
            "website": self.website,
        }


@dataclass(frozen=True, slots=True)
class CompanySite:
    """Sede aziendale coperta dal certificato."""

    type: str
    address: str
    postal_code: str
    city: str
    province: str
    region: str
    raw: str

    def __post_init__(self) -> None:
        for field_name in (
            "type",
            "address",
            "postal_code",
            "city",
            "province",
            "region",
            "raw",
        ):
            object.__setattr__(
                self,
                field_name,
                normalize_text(getattr(self, field_name)),
            )

    def identity_value(self) -> str:
        """
        Restituisce il valore usato per identificare la sede.

        Preferiamo il testo completo originale; i campi separati vengono
        utilizzati come fallback.
        """

        if self.raw:
            return normalize_identity(self.raw)

        return "|".join(
            normalize_identity(value)
            for value in (
                self.type,
                self.address,
                self.postal_code,
                self.city,
                self.province,
                self.region,
            )
        )

    def to_dict(self) -> dict[str, str]:
        """Restituisce una rappresentazione pronta per il JSON."""

        return {
            "type": self.type,
            "address": self.address,
            "postal_code": self.postal_code,
            "city": self.city,
            "province": self.province,
            "region": self.region,
            "raw": self.raw,
        }


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Posizione dalla quale è stato estratto il record."""

    url: str
    url_page: int
    page: int
    position: int
    scraped_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", normalize_text(self.url))
        object.__setattr__(
            self,
            "scraped_at",
            normalize_text(self.scraped_at),
        )

        if self.url_page < 0:
            raise ValueError("url_page non può essere negativo.")

        if self.page != self.url_page + 1:
            raise ValueError(
                "page deve essere uguale a url_page + 1."
            )

        if self.position < 1:
            raise ValueError("position deve partire da 1.")

    def to_dict(self) -> dict[str, str | int]:
        """Restituisce una rappresentazione pronta per il JSON."""

        return {
            "url": self.url,
            "url_page": self.url_page,
            "page": self.page,
            "position": self.position,
            "scraped_at": self.scraped_at,
        }


# ---------------------------------------------------------------------------
# Modello principale
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CertificateRecord:
    """Singola combinazione certificato/sede pubblicata da Accredia."""

    certificate_id: str
    entity_id: str
    record_id: str
    content_hash: str

    certificate_number: str
    issued_on: str
    status: str

    accreditation_body: AccreditationBody

    company_name: str
    vat_or_tax_code: str
    site: CompanySite

    scope: str
    standard: str
    accreditation_scheme: str
    sectors: tuple[str, ...]
    updated_on: str

    source: SourceMetadata

    raw_text: str
    raw_html: str

    @classmethod
    def create(
        cls,
        *,
        certificate_number: str,
        issued_on: str,
        status: str,
        accreditation_body: AccreditationBody,
        company_name: str,
        vat_or_tax_code: str,
        site: CompanySite,
        scope: str,
        standard: str,
        accreditation_scheme: str,
        sectors: tuple[str, ...] | list[str],
        updated_on: str,
        source: SourceMetadata,
        raw_text: str,
        raw_html: str,
    ) -> CertificateRecord:
        """
        Crea un record normalizzato e calcola tutti gli identificatori.

        Pagina, posizione e data dello scraping non partecipano agli hash:
        lo spostamento di una tabella non viene considerato un aggiornamento.
        """

        certificate_number = normalize_text(certificate_number)
        issued_on = normalize_text(issued_on)
        status = normalize_text(status)
        company_name = normalize_text(company_name)
        vat_or_tax_code = normalize_text(vat_or_tax_code)
        scope = normalize_text(scope)
        standard = normalize_text(standard)
        accreditation_scheme = normalize_text(accreditation_scheme)
        updated_on = normalize_text(updated_on)
        raw_text = normalize_text(raw_text)

        normalized_sectors = tuple(
            sector
            for sector in (
                normalize_text(value) for value in sectors
            )
            if sector
        )

        # Se il codice organismo è assente, usiamo il nome come fallback.
        body_identity = (
            accreditation_body.code
            or accreditation_body.name
        )

        # Se la P.IVA/CF è assente, usiamo la ragione sociale.
        company_identity = vat_or_tax_code or company_name

        certificate_identity_payload = {
            "accreditation_body": normalize_identity(body_identity),
            "certificate_number": normalize_identity(
                certificate_number
            ),
            "company": normalize_identity(company_identity),
            "standard": normalize_identity(standard),
        }

        certificate_id = sha256_payload(
            certificate_identity_payload
        )

        entity_identity_payload = {
            "certificate_id": certificate_id,
            "issued_on": issued_on,
            "site_type": normalize_identity(site.type),
            "site": site.identity_value(),
        }

        entity_id = sha256_payload(entity_identity_payload)

        # Il contenuto deriva dall'intera tabella HTML normalizzata da
        # BeautifulSoup. Anche una variazione di punteggiatura produce quindi
        # una versione distinta, mentre pagina e posizione restano escluse.
        normalized_raw_html = raw_html.strip()
        content_hash = sha256_payload(
            {
                "schema_version": SCHEMA_VERSION,
                "raw_html": normalized_raw_html,
            }
        )
        record_id = sha256_payload(
            {
                "entity_id": entity_id,
                "content_hash": content_hash,
            }
        )

        return cls(
            certificate_id=certificate_id,
            entity_id=entity_id,
            record_id=record_id,
            content_hash=content_hash,
            certificate_number=certificate_number,
            issued_on=issued_on,
            status=status,
            accreditation_body=accreditation_body,
            company_name=company_name,
            vat_or_tax_code=vat_or_tax_code,
            site=site,
            scope=scope,
            standard=standard,
            accreditation_scheme=accreditation_scheme,
            sectors=normalized_sectors,
            updated_on=updated_on,
            source=source,
            raw_text=raw_text,
            raw_html=normalized_raw_html,
        )

    def to_dict(self) -> dict[str, Any]:
        """Costruisce il documento finale da salvare nel file JSON."""

        return {
            "schema_version": SCHEMA_VERSION,
            "record_id": self.record_id,
            "entity_id": self.entity_id,
            "certificate_id": self.certificate_id,
            "certificate_number": self.certificate_number,
            "issued_on": self.issued_on,
            "status": self.status,
            "accreditation_body": self.accreditation_body.to_dict(),
            "company": {
                "name": self.company_name,
                "vat_or_tax_code": self.vat_or_tax_code,
                "site": self.site.to_dict(),
            },
            "scope": self.scope,
            "standard": self.standard,
            "accreditation_scheme": self.accreditation_scheme,
            "sectors": list(self.sectors),
            "updated_on": self.updated_on,
            "source": self.source.to_dict(),
            "raw_text": self.raw_text,
            "raw_html": self.raw_html,
            "content_hash": self.content_hash,
        }

    def to_json(self) -> str:
        """
        Serializza il record come JSON UTF-8 leggibile.

        La nuova riga finale evita problemi con editor e strumenti Git.
        """

        return (
            json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
