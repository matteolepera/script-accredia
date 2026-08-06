"""Factory condivise dai test automatici."""

from accredia_downloader.models import (
    AccreditationBody,
    CertificateRecord,
    CompanySite,
    SourceMetadata,
)


def build_record(
    *,
    scope: str = "Erogazione di servizi.",
    address: str = "Via Roma, 1",
    issued_on: str = "2025-03-04",
    url_page: int = 0,
    position: int = 1,
) -> CertificateRecord:
    """Crea un record prevedibile per i test."""

    site = CompanySite(
        type="Sede Legale e Operativa",
        address=address,
        postal_code="81100",
        city="Caserta",
        province="CE",
        region="Campania",
        raw=(
            "Sede Legale e Operativa - "
            f"{address} - 81100 - Caserta ( CE ) - Campania"
        ),
    )

    source = SourceMetadata(
        url=f"https://example.test/results?page={url_page}",
        url_page=url_page,
        page=url_page + 1,
        position=position,
        scraped_at="2026-08-05T12:00:00+02:00",
    )

    return CertificateRecord.create(
        certificate_number="ABC-123",
        issued_on=issued_on,
        status="in corso di validità",
        accreditation_body=AccreditationBody(
            code="0895",
            name="APAVE CERTIFICATION ITALIA S.r.l.",
            detail_url="https://example.test/body/0895",
            website="https://italy.apave.com/it-IT",
        ),
        company_name="012 FACTORY S.P.A. SOCIETA' BENEFIT",
        vat_or_tax_code="04019110610",
        site=site,
        scope=scope,
        standard="UNI/PdR 125:2022",
        accreditation_scheme="SGQ",
        sectors=[],
        updated_on="2026-08-03",
        source=source,
        raw_text=(
            f"Certificato ABC-123 emesso il {issued_on}. {scope}"
        ),
        raw_html=(
            "<table><tr><td>"
            f"Certificato ABC-123 emesso il {issued_on}. {scope}"
            "</td></tr></table>"
        ),
    )
