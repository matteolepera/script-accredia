#!/usr/bin/env python
"""Downloader regionale dei certificati pubblicati da Accredia."""

from __future__ import annotations

import argparse
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup
from rich import box
from rich.console import Console, Group
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from accredia_downloader import __version__
from accredia_downloader.client import (
    AccrediaBrowserClient,
    BrowserClientError,
    PageSnapshot,
)
from accredia_downloader.parser import (
    CAPTCHA_GATE_TEXT,
    CERTIFICATE_MARKER,
    CERTIFICATE_TABLE_SELECTOR,
    ParsingError,
    SessionExpiredError,
    build_page_url,
    build_region_url,
    parse_total_results,
)
from accredia_downloader.region import (
    RegionDownloadError,
    download_region,
)
from accredia_downloader.storage import (
    RegionStorageLayout,
    RegionalRunSummary,
    StorageError,
)


APP_NAME = "Accredia Downloader"
APP_VERSION = __version__
CONSOLE = Console(highlight=False)

SEARCH_URL = (
    "https://services.accredia.it/ppsearch/"
    "accredia_companymask_remote.jsp"
    "?ID_LINK=1739&area=310"
)

RESULTS_URL = (
    "https://services.accredia.it/ppsearch/"
    "accredia_companymask_remote.jsp"
    "?recaptcha=true"
    "&LANG=%5BDEFAULT%5D"
    "&ID_LINK=1739"
    "&area=310"
    "&page=0"
    "&submit=Cerca"
)

DEFAULT_OUTPUT_ROOT = Path("documenti") / "accredia"
DEFAULT_PROFILE_DIR = Path(".accredia-playwright-profile")
DEFAULT_NETWORK_RETRIES = 10_000
LOCAL_FILE_RETRIES = 3


def region_slug(region: str) -> str:
    """Converte il nome della regione in una cartella portabile."""

    normalized = unicodedata.normalize("NFKD", region)
    without_accents = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    slug = re.sub(
        r"[^a-z0-9]+",
        "-",
        without_accents.casefold(),
    ).strip("-")

    if not slug:
        raise ValueError("Il nome della regione non è valido.")

    return slug


@dataclass(frozen=True, slots=True)
class ScraperConfig:
    """Configurazione validata della riga di comando."""

    output_root: Path
    profile_dir: Path
    delay_seconds: float
    request_timeout_ms: int
    network_retries: int
    browser_channel: str
    headless: bool
    check_session: bool
    region: str | None

    @property
    def storage_root(self) -> Path:
        if self.region is None:
            return self.output_root

        return self.output_root / "regioni" / region_slug(self.region)

    @property
    def certificates_path(self) -> Path:
        return self.storage_root / "certificati.json"

    @property
    def state_dir(self) -> Path:
        return self.storage_root / "state"

    def validate(self) -> None:
        if self.delay_seconds < 1:
            raise ValueError(
                "Il ritardo tra le richieste non può essere inferiore "
                "a 1 secondo."
            )

        if self.request_timeout_ms < 1_000:
            raise ValueError(
                "Il timeout delle richieste deve essere di almeno 1 secondo."
            )

        if self.network_retries < 0:
            raise ValueError(
                "Il numero massimo di tentativi non può essere negativo."
            )

        if self.region is not None:
            region_slug(self.region)


def build_argument_parser() -> argparse.ArgumentParser:
    """Definisce i parametri accettati dal programma."""

    parser = argparse.ArgumentParser(
        prog="accredia_scraper",
        description=(
            "Scarica e aggiorna un unico JSON per regione dalla banca dati "
            "Accredia."
        ),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Directory principale. Default: documenti/accredia",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_PROFILE_DIR,
        help=(
            "Profilo persistente Playwright. "
            "Default: .accredia-playwright-profile"
        ),
    )
    parser.add_argument(
        "--region",
        type=str,
        help="Regione da scaricare, per esempio Abruzzo.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Secondi tra due pagine. Default: 2",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Timeout di ogni richiesta in secondi. Default: 60",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_NETWORK_RETRIES,
        help="Nuovi tentativi di rete per richiesta. Default: 10000",
    )
    parser.add_argument(
        "--browser-channel",
        choices=("chrome", "msedge", "chromium", "firefox"),
        default="chrome",
        help="Browser controllato da Playwright. Default: chrome",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Avvia il browser senza finestra usando una sessione valida.",
    )
    parser.add_argument(
        "--check-session",
        action="store_true",
        help="Verifica la sessione senza scaricare certificati.",
    )
    return parser


def config_from_arguments(
    parser: argparse.ArgumentParser,
) -> ScraperConfig:
    arguments = parser.parse_args()
    region = (
        " ".join(arguments.region.split())
        if arguments.region
        else None
    )
    config = ScraperConfig(
        output_root=arguments.output_root.resolve(),
        profile_dir=arguments.profile_dir.resolve(),
        delay_seconds=arguments.delay,
        request_timeout_ms=arguments.timeout * 1_000,
        network_retries=arguments.retries,
        browser_channel=arguments.browser_channel,
        headless=arguments.headless,
        check_session=arguments.check_session,
        region=region,
    )

    try:
        config.validate()
    except ValueError as error:
        parser.error(str(error))

    return config


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            RichHandler(
                console=CONSOLE,
                show_time=True,
                show_level=True,
                show_path=False,
                markup=False,
                rich_tracebacks=True,
                tracebacks_show_locals=False,
            )
        ],
        force=True,
    )


def print_application_header() -> None:
    title = Text()
    title.append("ACCREDIA", style="bold cyan")
    title.append(" DOWNLOADER", style="bold white")
    content = Group(
        title,
        Text("Snapshot regionali dei certificati", style="dim white"),
        Text(""),
        Text(f"Versione {APP_VERSION}", style="dim cyan"),
    )
    CONSOLE.print()
    CONSOLE.print(
        Panel(
            content,
            border_style="cyan",
            padding=(1, 2),
            safe_box=True,
        )
    )


def format_enabled(value: bool) -> Text:
    return Text(
        "ATTIVATA" if value else "DISATTIVATA",
        style="bold yellow" if value else "bold green",
    )


def print_configuration(config: ScraperConfig) -> None:
    table = Table(
        box=box.SIMPLE,
        show_header=False,
        pad_edge=False,
        safe_box=True,
        expand=True,
    )
    table.add_column("Parametro", style="bold cyan", width=24)
    table.add_column("Valore", overflow="fold")
    table.add_row("Regione", config.region or "nessuna")
    table.add_row("JSON regionale", str(config.certificates_path))
    table.add_row("Directory stato", str(config.state_dir))
    table.add_row("Profilo Playwright", str(config.profile_dir))
    table.add_row("Browser", config.browser_channel)
    table.add_row("Modalità headless", format_enabled(config.headless))
    table.add_row("Ritardo richieste", f"{config.delay_seconds:.1f} s")
    table.add_row(
        "Timeout",
        f"{config.request_timeout_ms // 1_000} s",
    )
    table.add_row("Retry rete", str(config.network_retries))
    table.add_row(
        "Solo controllo sessione",
        format_enabled(config.check_session),
    )
    CONSOLE.print(
        Panel(
            table,
            title="[bold]CONFIGURAZIONE[/bold]",
            title_align="left",
            border_style="blue",
            padding=(0, 1),
            safe_box=True,
        )
    )


def configured_results_url(config: ScraperConfig) -> str:
    results_url = RESULTS_URL

    if config.region is not None:
        results_url = build_region_url(results_url, config.region)

    return build_page_url(results_url, 0)


def is_results_page(snapshot: PageSnapshot) -> bool:
    if CAPTCHA_GATE_TEXT.casefold() in snapshot.html.casefold():
        return False

    soup = BeautifulSoup(snapshot.html, "lxml")
    tables = [
        table
        for table in soup.select(CERTIFICATE_TABLE_SELECTOR)
        if CERTIFICATE_MARKER.casefold()
        in table.get_text(" ", strip=True).casefold()
    ]
    return parse_total_results(snapshot.html) is not None and bool(tables)


def print_manual_validation_instructions() -> None:
    CONSOLE.print(
        Panel(
            "1. Completa manualmente il CAPTCHA nel browser.\n"
            "2. Lascia vuoti i filtri.\n"
            "3. Clicca Cerca e attendi i risultati.\n"
            "4. Torna al terminale e conferma.",
            title="[bold]VERIFICA MANUALE[/bold]",
            title_align="left",
            border_style="yellow",
            padding=(1, 2),
            safe_box=True,
        )
    )


def run_session_check(config: ScraperConfig) -> int:
    try:
        with AccrediaBrowserClient(
            profile_dir=config.profile_dir,
            browser_channel=config.browser_channel,
            headless=config.headless,
            timeout_ms=config.request_timeout_ms,
            max_retries=config.network_retries,
        ) as client:
            results_url = configured_results_url(config)
            snapshot = client.navigate(results_url)

            if not is_results_page(snapshot):
                if config.headless:
                    raise SessionExpiredError(
                        "Sessione non valida: ripeti senza --headless."
                    )

                client.navigate(SEARCH_URL)
                print_manual_validation_instructions()
                confirmed = Confirm.ask(
                    "Hai completato il CAPTCHA e visualizzi i risultati?",
                    console=CONSOLE,
                    default=True,
                )

                if not confirmed:
                    return 2

                snapshot = client.navigate(results_url)

            if not is_results_page(snapshot):
                raise SessionExpiredError(
                    "La pagina corrente non contiene risultati validi."
                )

            soup = BeautifulSoup(snapshot.html, "lxml")
            total_results = parse_total_results(snapshot.html)
            table_count = len(
                soup.select(CERTIFICATE_TABLE_SELECTOR)
            )
            CONSOLE.print(
                Panel(
                    f"Sessione valida\n"
                    f"Regione: {config.region or 'tutte'}\n"
                    f"Risultati: {total_results:,}\n"
                    f"Tabelle nella prima pagina: {table_count}",
                    title="[bold]CONTROLLO COMPLETATO[/bold]",
                    border_style="green",
                    safe_box=True,
                )
            )
            return 0
    except (BrowserClientError, SessionExpiredError) as error:
        CONSOLE.print(
            Panel(
                str(error),
                title="[bold]SESSIONE NON VALIDA[/bold]",
                border_style="red",
                safe_box=True,
            )
        )
        return 2


def print_region_summary(summary: RegionalRunSummary) -> None:
    table = Table(
        box=box.SIMPLE,
        show_header=False,
        pad_edge=False,
        safe_box=True,
        expand=True,
    )
    table.add_column("Parametro", style="bold green", width=24)
    table.add_column("Valore", overflow="fold")
    table.add_row("Regione", summary.region)
    table.add_row("Pagine", str(summary.total_pages))
    table.add_row("Tabelle elaborate", str(summary.tables_processed))
    table.add_row("Record unici", str(summary.unique_records))
    table.add_row(
        "Duplicati accorpati",
        str(summary.duplicates_collapsed),
    )
    table.add_row("Nuovi", str(summary.new_records))
    table.add_row("Aggiornati", str(summary.updated_records))
    table.add_row("Invariati", str(summary.unchanged_records))
    table.add_row("Non più presenti", str(summary.missing_records))
    table.add_row("File", str(summary.output_path))
    CONSOLE.print(
        Panel(
            table,
            title="[bold]REGIONE COMPLETATA[/bold]",
            title_align="left",
            border_style="green",
            safe_box=True,
        )
    )


def run_region_download(config: ScraperConfig) -> int:
    if config.region is None:
        raise ValueError("Nessuna regione configurata.")

    layout = RegionStorageLayout(config.storage_root)
    progress = Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=CONSOLE,
        expand=True,
    )
    task_id: int | None = None

    def on_start(
        total_results: int,
        total_pages: int,
        completed_pages: int,
        resumed: bool,
    ) -> None:
        nonlocal task_id
        mode = "ripresa" if resumed else "nuova esecuzione"
        progress.console.print(
            f"[cyan]{config.region}[/cyan]: "
            f"{total_results:,} risultati, {total_pages:,} pagine "
            f"({mode})."
        )
        task_id = progress.add_task(
            config.region or "Regione",
            total=total_pages,
            completed=completed_pages,
        )

    def on_page(
        url_page: int,
        completed_pages: int,
        total_pages: int,
    ) -> None:
        del url_page, total_pages

        if task_id is not None:
            progress.update(task_id, completed=completed_pages)

    try:
        with AccrediaBrowserClient(
            profile_dir=config.profile_dir,
            browser_channel=config.browser_channel,
            headless=config.headless,
            timeout_ms=config.request_timeout_ms,
            max_retries=config.network_retries,
        ) as client:
            with progress:
                summary = download_region(
                    client=client,
                    layout=layout,
                    region=config.region,
                    results_url=configured_results_url(config),
                    delay_seconds=config.delay_seconds,
                    local_retries=LOCAL_FILE_RETRIES,
                    on_start=on_start,
                    on_page=on_page,
                )

        print_region_summary(summary)
        return 0
    except SessionExpiredError as error:
        title = "SESSIONE NON VALIDA"
        border_style = "yellow"
        error_message = str(error)
        exit_code = 2
    except KeyboardInterrupt:
        title = "ESECUZIONE INTERROTTA"
        border_style = "yellow"
        error_message = (
            "Le pagine già completate sono conservate nello staging. "
            "Puoi rilanciare lo stesso comando oggi per riprendere."
        )
        exit_code = 130
    except (
        BrowserClientError,
        ParsingError,
        RegionDownloadError,
        StorageError,
        OSError,
        ValueError,
    ) as error:
        title = "DOWNLOAD NON COMPLETATO"
        border_style = "red"
        error_message = str(error)
        exit_code = 2

    CONSOLE.print(
        Panel(
            error_message,
            title=f"[bold]{title}[/bold]",
            border_style=border_style,
            safe_box=True,
        )
    )
    return exit_code


def print_ready_message() -> None:
    CONSOLE.print(
        Panel(
            "Configurazione valida. Specifica --region per avviare "
            "il download oppure usa --check-session.",
            border_style="green",
            safe_box=True,
        )
    )


def main() -> int:
    configure_logging()
    print_application_header()
    parser = build_argument_parser()
    config = config_from_arguments(parser)
    print_configuration(config)

    if config.check_session:
        return run_session_check(config)

    if config.region is not None:
        return run_region_download(config)

    print_ready_message()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
