"""
Parser delle pagine dei risultati Accredia.

Il parser:

- riconosce una sessione CAPTCHA scaduta;
- legge il totale dei risultati;
- seleziona `div.ppsearch > table`;
- estrae i campi di ogni certificato;
- converte le date in formato ISO;
- genera oggetti CertificateRecord.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import (
    parse_qs,
    parse_qsl,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

from bs4 import BeautifulSoup, Tag

from accredia_downloader.models import (
    AccreditationBody,
    CertificateRecord,
    CompanySite,
    SourceMetadata,
    normalize_text,
)


CAPTCHA_GATE_TEXT = "Verifica reCAPTCHA richiesta"
RESULTS_CONTAINER_SELECTOR = "div.ppsearch"
CERTIFICATE_TABLE_SELECTOR = "div.ppsearch > table"
CERTIFICATE_MARKER = "N.Certificato"
PAGE_SIZE = 20
REGION_QUERY_PARAMETER = "PPSEARCH_COMPANY_SEARCH_MASK_REGIONE"

TOTAL_PATTERN = re.compile(
    r"Risultati\s*:\s*([\d.]+)",
    re.IGNORECASE,
)

DATE_PATTERN = re.compile(
    r"\b(\d{2})[-/](\d{2})[-/](\d{4})\b"
)

SITE_PATTERN = re.compile(
    r"^"
    r"(?P<type>.*?)"
    r"\s+-\s+"
    r"(?P<address>.*?)"
    r"\s+-\s+"
    r"(?P<postal_code>\d{5})"
    r"\s+-\s+"
    r"(?P<city>.*?)"
    r"\s*\(\s*(?P<province>[^)]+)\s*\)"
    r"\s+-\s+"
    r"(?P<region>.*)"
    r"$",
    re.IGNORECASE,
)

SECTOR_CODE_PATTERN = re.compile(
    r"(?<!\d)(\d{2})(?!\d)"
)


# ---------------------------------------------------------------------------
# Eccezioni
# ---------------------------------------------------------------------------

class ParsingError(RuntimeError):
    """Errore nella struttura o nel contenuto della pagina."""


class SessionExpiredError(ParsingError):
    """La sessione CAPTCHA non è più valida."""


# ---------------------------------------------------------------------------
# Risultato del parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ParsedPage:
    """Risultato dell'analisi di una pagina."""

    url_page: int
    total_results: int | None
    records: tuple[CertificateRecord, ...]

    @property
    def page(self) -> int:
        """Numero pagina one-based."""

        return self.url_page + 1

    @property
    def record_count(self) -> int:
        return len(self.records)


# ---------------------------------------------------------------------------
# Funzioni generali
# ---------------------------------------------------------------------------

def has_captcha_gate(html: str) -> bool:
    """Controlla se Accredia richiede nuovamente il CAPTCHA."""

    return CAPTCHA_GATE_TEXT.casefold() in html.casefold()


def parse_total_results(html: str) -> int | None:
    """
    Estrae il totale riportato dalla pagina.

    Il totale può essere assente nelle pagine successive.
    """

    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text(" ", strip=True)

    match = TOTAL_PATTERN.search(page_text)

    if not match:
        return None

    return int(match.group(1).replace(".", ""))


def calculate_total_pages(
    total_results: int,
    page_size: int = PAGE_SIZE,
) -> int:
    """Calcola il numero di pagine senza usare numeri in virgola mobile."""

    if total_results < 0:
        raise ValueError("total_results non può essere negativo.")

    if page_size < 1:
        raise ValueError("page_size deve essere positivo.")

    return (total_results + page_size - 1) // page_size


def expected_records_for_page(
    total_results: int,
    url_page: int,
    page_size: int = PAGE_SIZE,
) -> int:
    """Calcola quanti record devono essere presenti in una pagina."""

    if url_page < 0:
        raise ValueError("url_page non può essere negativo.")

    remaining = total_results - (url_page * page_size)

    if remaining <= 0:
        return 0

    return min(page_size, remaining)


def build_page_url(
    results_url: str,
    url_page: int,
) -> str:
    """
    Modifica esclusivamente il parametro `page`.

    Gli altri parametri e filtri vengono mantenuti nell'ordine originale.
    Eventuali parametri `page` duplicati vengono ridotti a uno.
    """

    if url_page < 0:
        raise ValueError("url_page non può essere negativo.")

    parts = urlsplit(results_url)
    query_items = parse_qsl(
        parts.query,
        keep_blank_values=True,
    )

    updated_items: list[tuple[str, str]] = []
    page_replaced = False

    for key, value in query_items:
        if key.casefold() == "page":
            if not page_replaced:
                updated_items.append((key, str(url_page)))
                page_replaced = True

            continue

        updated_items.append((key, value))

    if not page_replaced:
        updated_items.append(("page", str(url_page)))

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(updated_items),
            parts.fragment,
        )
    )


def build_region_url(
    results_url: str,
    region: str,
) -> str:
    """Aggiunge o sostituisce il filtro regione nell'URL dei risultati."""

    normalized_region = normalize_text(region)

    if not normalized_region:
        raise ValueError("La regione non può essere vuota.")

    parts = urlsplit(results_url)
    query_items = parse_qsl(
        parts.query,
        keep_blank_values=True,
    )

    updated_items: list[tuple[str, str]] = []
    region_replaced = False

    for key, value in query_items:
        if key.casefold() == REGION_QUERY_PARAMETER.casefold():
            if not region_replaced:
                updated_items.append(
                    (REGION_QUERY_PARAMETER, normalized_region)
                )
                region_replaced = True

            continue

        updated_items.append((key, value))

    if not region_replaced:
        updated_items.append(
            (REGION_QUERY_PARAMETER, normalized_region)
        )

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(updated_items),
            parts.fragment,
        )
    )


# ---------------------------------------------------------------------------
# Utilità per leggere le tabelle
# ---------------------------------------------------------------------------

def _table_lines(table: Tag) -> list[str]:
    """Restituisce le stringhe visibili e normalizzate della tabella."""

    return [
        value
        for text in table.stripped_strings
        if (value := normalize_text(text))
    ]


def _find_label_index(
    lines: list[str],
    label: str,
) -> int | None:
    """Trova l'indice della prima riga contenente un'etichetta."""

    normalized_label = label.casefold()

    for index, line in enumerate(lines):
        if normalized_label in line.casefold():
            return index

    return None


def _inline_value(
    line: str,
    label: str,
) -> str:
    """Estrae l'eventuale valore presente dopo un'etichetta."""

    normalized_line = line.casefold()
    normalized_label = label.casefold()
    label_position = normalized_line.find(normalized_label)

    if label_position < 0:
        return ""

    value_position = label_position + len(label)

    return normalize_text(
        line[value_position:].lstrip(" :")
    )


def _extract_single_value(
    lines: list[str],
    label: str,
) -> str:
    """
    Estrae un valore singolo.

    Gestisce sia:

        Partita IVA: 123

    sia:

        Partita IVA:
        123
    """

    index = _find_label_index(lines, label)

    if index is None:
        return ""

    inline = _inline_value(lines[index], label)

    if inline:
        return inline

    if index + 1 < len(lines):
        return lines[index + 1]

    return ""


def _extract_section(
    lines: list[str],
    label: str,
    stop_labels: tuple[str, ...],
) -> str:
    """Estrae tutto il testo compreso tra un'etichetta e la successiva."""

    start_index = _find_label_index(lines, label)

    if start_index is None:
        return ""

    values: list[str] = []

    inline = _inline_value(lines[start_index], label)

    if inline:
        values.append(inline)

    for line in lines[start_index + 1:]:
        normalized_line = line.casefold()

        if any(
            stop_label.casefold() in normalized_line
            for stop_label in stop_labels
        ):
            break

        values.append(line)

    return normalize_text(" ".join(values))


def _parse_date(value: str) -> str:
    """Converte `DD-MM-YYYY` o `DD/MM/YYYY` in `YYYY-MM-DD`."""

    match = DATE_PATTERN.search(value)

    if not match:
        return ""

    day, month, year = match.groups()

    try:
        parsed_date = datetime(
            year=int(year),
            month=int(month),
            day=int(day),
        )
    except ValueError:
        return ""

    return parsed_date.date().isoformat()


def _extract_body_code(detail_url: str) -> str:
    """Estrae il codice organismo dall'URL Accredia."""

    if not detail_url:
        return ""

    query = parse_qs(
        urlsplit(detail_url).query,
        keep_blank_values=True,
    )

    values = query.get("PPSEARCH_ORG_SEARCH_MASK_ORG", [])

    return normalize_text(values[0]) if values else ""


def _parse_site(raw_site: str) -> CompanySite:
    """
    Divide il testo della sede nei campi strutturati.

    Se il formato non corrisponde a quello italiano previsto,
    il testo completo viene comunque conservato in `raw`.
    """

    raw_site = normalize_text(raw_site)
    match = SITE_PATTERN.match(raw_site)

    if not match:
        return CompanySite(
            type="",
            address="",
            postal_code="",
            city="",
            province="",
            region="",
            raw=raw_site,
        )

    values = match.groupdict()

    return CompanySite(
        type=values["type"],
        address=values["address"],
        postal_code=values["postal_code"],
        city=values["city"],
        province=values["province"],
        region=values["region"],
        raw=raw_site,
    )


def _parse_sectors(value: str) -> tuple[str, ...]:
    """Estrae i codici IAF, mantenendo l'ordine e rimuovendo duplicati."""

    value = normalize_text(value)

    if not value:
        return ()

    codes = SECTOR_CODE_PATTERN.findall(value)

    if not codes:
        # Conserviamo comunque il valore se non segue il formato numerico.
        return (value,)

    return tuple(dict.fromkeys(codes))


def _remove_first_occurrence(
    values: list[str],
    target: str,
) -> None:
    """Rimuove la prima stringa corrispondente senza generare errori."""

    normalized_target = normalize_text(target).casefold()

    if not normalized_target:
        return

    for index, value in enumerate(values):
        if normalize_text(value).casefold() == normalized_target:
            values.pop(index)
            return


# ---------------------------------------------------------------------------
# Parsing della singola tabella
# ---------------------------------------------------------------------------

def parse_certificate_table(
    table: Tag,
    *,
    source_url: str,
    url_page: int,
    position: int,
    scraped_at: str,
) -> CertificateRecord:
    """Trasforma una tabella HTML in un CertificateRecord."""

    lines = _table_lines(table)

    if not any(
        CERTIFICATE_MARKER.casefold() in line.casefold()
        for line in lines
    ):
        raise ParsingError(
            f"Tabella {position} senza N.Certificato."
        )

    certificate_number = _extract_single_value(
        lines,
        "N.Certificato:",
    )

    issued_section = _extract_section(
        lines,
        "Emesso il",
        ("dall'organismo Accreditato:",),
    )

    issued_on = _parse_date(issued_section)

    # Dopo aver rimosso la data rimane normalmente lo stato.
    status = normalize_text(
        DATE_PATTERN.sub("", issued_section, count=1)
    )

    detail_anchor = table.find(
        "a",
        href=lambda href: (
            isinstance(href, str)
            and "accredia_orgmask.jsp" in href
        ),
    )

    detail_url = ""
    body_name = ""

    if isinstance(detail_anchor, Tag):
        detail_url = urljoin(
            source_url,
            normalize_text(detail_anchor.get("href", "")),
        )
        body_name = normalize_text(
            detail_anchor.get_text(" ", strip=True)
        )

    website_url = ""
    website_text = ""

    for anchor in table.find_all("a", href=True):
        href = normalize_text(anchor.get("href", ""))

        if (
            href.startswith(("http://", "https://"))
            and "accredia_orgmask.jsp" not in href
        ):
            website_url = urljoin(source_url, href)
            website_text = normalize_text(
                anchor.get_text(" ", strip=True)
            )
            break

    body_start = _find_label_index(
        lines,
        "dall'organismo Accreditato:",
    )
    scope_start = _find_label_index(lines, "Scopo:")

    organization_values: list[str] = []

    if (
        body_start is not None
        and scope_start is not None
        and body_start < scope_start
    ):
        inline_body = _inline_value(
            lines[body_start],
            "dall'organismo Accreditato:",
        )

        if inline_body:
            organization_values.append(inline_body)

        organization_values.extend(
            lines[body_start + 1:scope_start]
        )

    # L'organismo e il relativo sito sono già stati estratti dai link.
    _remove_first_occurrence(organization_values, body_name)
    _remove_first_occurrence(
        organization_values,
        website_text,
    )
    _remove_first_occurrence(
        organization_values,
        website_url,
    )

    company_name = (
        organization_values.pop(0)
        if organization_values
        else ""
    )

    raw_site = normalize_text(
        " ".join(organization_values)
    )

    scope = _extract_section(
        lines,
        "Scopo:",
        ("Norma:",),
    )

    standard = _extract_section(
        lines,
        "Norma:",
        (
            "Schema di Accreditamento:",
            "Settori:",
            "Dati aggiornati",
        ),
    )

    accreditation_scheme = _extract_section(
        lines,
        "Schema di Accreditamento:",
        (
            "Settori:",
            "Dati aggiornati",
        ),
    )

    sectors_text = _extract_section(
        lines,
        "Settori:",
        ("Dati aggiornati",),
    )

    updated_value = _extract_single_value(
        lines,
        "Dati aggiornati dall'Organismo il",
    )

    vat_or_tax_code = _extract_single_value(
        lines,
        "Partita IVA:",
    )

    required_values = {
        "certificate_number": certificate_number,
        "issued_on": issued_on,
        "body_name": body_name,
        "company_name": company_name,
        "standard": standard,
    }

    missing_fields = [
        name
        for name, value in required_values.items()
        if not value
    ]

    if missing_fields:
        raise ParsingError(
            f"Tabella {position}: campi mancanti: "
            + ", ".join(missing_fields)
        )

    body = AccreditationBody(
        code=_extract_body_code(detail_url),
        name=body_name,
        detail_url=detail_url,
        website=website_url,
    )

    source = SourceMetadata(
        url=source_url,
        url_page=url_page,
        page=url_page + 1,
        position=position,
        scraped_at=scraped_at,
    )

    return CertificateRecord.create(
        certificate_number=certificate_number,
        issued_on=issued_on,
        status=status,
        accreditation_body=body,
        company_name=company_name,
        vat_or_tax_code=vat_or_tax_code,
        site=_parse_site(raw_site),
        scope=scope,
        standard=standard,
        accreditation_scheme=accreditation_scheme,
        sectors=_parse_sectors(sectors_text),
        updated_on=_parse_date(updated_value),
        source=source,
        raw_text=" | ".join(lines),
        raw_html=str(table),
    )


# ---------------------------------------------------------------------------
# Parsing della pagina
# ---------------------------------------------------------------------------

def parse_result_page(
    html: str,
    *,
    source_url: str,
    url_page: int,
    scraped_at: str,
) -> ParsedPage:
    """Trasforma una pagina HTML nei relativi certificati."""

    if has_captcha_gate(html):
        raise SessionExpiredError(
            "La sessione CAPTCHA non è più valida."
        )

    soup = BeautifulSoup(html, "lxml")
    results_container = soup.select_one(
        RESULTS_CONTAINER_SELECTOR
    )

    if results_container is None:
        raise ParsingError(
            "Contenitore div.ppsearch non trovato."
        )

    tables = [
        table
        for table in soup.select(
            CERTIFICATE_TABLE_SELECTOR
        )
        if CERTIFICATE_MARKER.casefold()
        in table.get_text(" ", strip=True).casefold()
    ]

    if not tables:
        raise ParsingError(
            "Nessuna tabella certificato trovata."
        )

    records = tuple(
        parse_certificate_table(
            table,
            source_url=source_url,
            url_page=url_page,
            position=position,
            scraped_at=scraped_at,
        )
        for position, table in enumerate(tables, start=1)
    )

    return ParsedPage(
        url_page=url_page,
        total_results=parse_total_results(html),
        records=records,
    )
