#!/usr/bin/env python
"""Downloader regionale dei certificati pubblicati da Accredia."""

from __future__ import annotations

import argparse
import logging
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from rich import box
from rich.console import Console, Group
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from accredia_downloader import __version__
from accredia_downloader.batch import (
    ITALIAN_REGIONS,
    RegionDecision,
    decide_region_download,
)
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
TERMINAL_THEME = Theme(
    {
        "flux.cyan": "bold bright_cyan",
        "flux.amber": "bold bright_yellow",
        "flux.red": "bold bright_red",
        "flux.green": "bold bright_green",
        "flux.blue": "bold bright_blue",
        "flux.dim": "dim white",
    }
)
CONSOLE = Console(
    highlight=False,
    theme=TERMINAL_THEME,
)

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
    all_regions: bool
    refresh_existing: bool
    refresh_after_days: int | None

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

        if self.region is not None and self.all_regions:
            raise ValueError(
                "--region e --all-regions non possono essere usati insieme."
            )

        if (
            self.refresh_existing
            or self.refresh_after_days is not None
        ) and not self.all_regions:
            raise ValueError(
                "Le opzioni di aggiornamento automatico richiedono "
                "--all-regions."
            )

        if (
            self.refresh_existing
            and self.refresh_after_days is not None
        ):
            raise ValueError(
                "Usa soltanto una tra --refresh-existing e "
                "--refresh-after-days."
            )

        if (
            self.refresh_after_days is not None
            and self.refresh_after_days < 0
        ):
            raise ValueError(
                "--refresh-after-days non può essere negativo."
            )


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
    scope_group = parser.add_mutually_exclusive_group()
    scope_group.add_argument(
        "--region",
        type=str,
        help="Regione da scaricare, per esempio Abruzzo.",
    )
    scope_group.add_argument(
        "--all-regions",
        action="store_true",
        help="Cicla automaticamente tutte le 20 regioni italiane.",
    )
    refresh_group = parser.add_mutually_exclusive_group()
    refresh_group.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Con --all-regions aggiorna anche gli snapshot completi.",
    )
    refresh_group.add_argument(
        "--refresh-after-days",
        type=int,
        metavar="GIORNI",
        help=(
            "Con --all-regions aggiorna gli snapshot con almeno "
            "questi giorni."
        ),
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
        all_regions=arguments.all_regions,
        refresh_existing=arguments.refresh_existing,
        refresh_after_days=arguments.refresh_after_days,
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


def format_circuit_time(value: datetime) -> str:
    """Formatta l'orario come un display del circuito temporale."""

    months = (
        "GEN",
        "FEB",
        "MAR",
        "APR",
        "MAG",
        "GIU",
        "LUG",
        "AGO",
        "SET",
        "OTT",
        "NOV",
        "DIC",
    )
    month = months[value.month - 1]
    return f"{value.day:02d} {month} {value.year:04d}  {value:%H:%M:%S}"


def print_application_header(config: ScraperConfig) -> None:
    """Mostra il pannello principale ispirato ai circuiti temporali."""

    if config.check_session:
        destination = (
            config.region.upper()
            if config.region is not None
            else "SESSIONE PLAYWRIGHT"
        )
        mode = "DIAGNOSTICA"
    elif config.all_regions:
        destination = "ITALIA // 20 REGIONI"
        mode = "CICLO NAZIONALE"
    elif config.region is not None:
        destination = config.region.upper()
        mode = "TRATTA REGIONALE"
    else:
        destination = "NON PROGRAMMATA"
        mode = "STANDBY"

    title = Text(justify="center")
    title.append("ACCREDIA", style="flux.cyan")
    title.append(" // ", style="flux.dim")
    title.append("CERTIFICATE TIME CIRCUIT", style="bold white")

    circuits = Table.grid(expand=True, padding=(0, 1))
    circuits.add_column(width=20)
    circuits.add_column(ratio=1)
    circuits.add_row(
        Text("DESTINAZIONE", style="flux.red"),
        Text(destination, style="bold bright_red"),
    )
    circuits.add_row(
        Text("TEMPO PRESENTE", style="flux.green"),
        Text(
            format_circuit_time(datetime.now().astimezone()),
            style="bold bright_green",
        ),
    )
    circuits.add_row(
        Text("FLUSSO DATI", style="flux.amber"),
        Text(mode, style="bold bright_yellow"),
    )

    footer = Text(justify="right")
    footer.append("FLUX CAPACITOR: ", style="flux.dim")
    footer.append("ONLINE", style="flux.cyan")
    footer.append(f"   //   VERSIONE {APP_VERSION}", style="flux.dim")

    content = Group(title, Text(""), circuits, Text(""), footer)
    CONSOLE.print()
    CONSOLE.print(
        Panel(
            content,
            title="[flux.blue]SYSTEM READY[/flux.blue]",
            title_align="right",
            border_style="bright_cyan",
            padding=(1, 2),
            safe_box=True,
        )
    )


def format_enabled(value: bool) -> Text:
    return Text(
        "ARMATA" if value else "OFFLINE",
        style="flux.amber" if value else "flux.green",
    )


def print_configuration(config: ScraperConfig) -> None:
    table = Table(
        box=box.SIMPLE,
        show_header=False,
        pad_edge=False,
        safe_box=True,
        expand=True,
    )
    table.add_column("Circuito", style="flux.cyan", width=24)
    table.add_column("Valore", overflow="fold")
    scope = "tutte (automatico)" if config.all_regions else (
        config.region or "nessuna"
    )
    table.add_row("Regione", scope)
    table.add_row("Output", str(config.storage_root))
    table.add_row("Profilo Playwright", str(config.profile_dir))
    table.add_row("Browser", config.browser_channel)
    table.add_row("Modalità headless", format_enabled(config.headless))
    table.add_row("Ritardo richieste", f"{config.delay_seconds:.1f} s")
    table.add_row(
        "Timeout",
        f"{config.request_timeout_ms // 1_000} s",
    )
    table.add_row("Retry rete", str(config.network_retries))
    if config.all_regions:
        if config.refresh_existing:
            update_mode = "aggiorna tutti gli snapshot"
        elif config.refresh_after_days is not None:
            update_mode = (
                f"aggiorna dopo {config.refresh_after_days} giorni"
            )
        else:
            update_mode = "scarica soltanto le regioni mancanti"

        table.add_row("Politica aggiornamento", update_mode)
    table.add_row(
        "Solo controllo sessione",
        format_enabled(config.check_session),
    )
    CONSOLE.print(
        Panel(
            table,
            title="[flux.blue]TIME CIRCUITS // CONFIGURAZIONE[/flux.blue]",
            title_align="left",
            subtitle="[flux.dim]INPUT LOCKED[/flux.dim]",
            subtitle_align="right",
            border_style="bright_blue",
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
            title="[flux.amber]SESSION LOCK // VERIFICA MANUALE[/flux.amber]",
            title_align="left",
            border_style="bright_yellow",
            padding=(1, 2),
            safe_box=True,
        )
    )


def recover_browser_session(
    client: AccrediaBrowserClient,
    config: ScraperConfig,
) -> PageSnapshot:
    """Guida il rinnovo manuale del CAPTCHA e valida la nuova sessione."""

    if config.headless:
        raise SessionExpiredError(
            "La sessione è scaduta durante il download. Le pagine sono "
            "salve nello staging: rilancia senza --headless per completare "
            "manualmente il CAPTCHA e riprendere."
        )

    while True:
        CONSOLE.print(
            Panel(
                "L'operazione è in pausa; gli eventuali dati già registrati "
                "nello staging sono al sicuro. "
                "Firefox verrà portato alla maschera di verifica; dopo il "
                "CAPTCHA la stessa regione riprenderà automaticamente.",
                title="[flux.amber]SESSION LINK LOST // PAUSA[/flux.amber]",
                subtitle="[flux.dim]SQLITE CHECKPOINT SAFE[/flux.dim]",
                border_style="bright_yellow",
                safe_box=True,
            )
        )
        client.navigate(SEARCH_URL)
        print_manual_validation_instructions()
        confirmed = Confirm.ask(
            "Hai completato il CAPTCHA e visualizzi i risultati?",
            console=CONSOLE,
            default=True,
        )

        if not confirmed:
            raise SessionExpiredError(
                "Ripristino della sessione annullato. Le pagine completate "
                "rimangono nello staging."
            )

        snapshot = client.navigate(configured_results_url(config))

        if is_results_page(snapshot):
            CONSOLE.print(
                Panel(
                    "Sessione riattivata. Ripresa della regione dalla prima "
                    "pagina non ancora completata.",
                    title="[flux.green]SESSION LINK RESTORED[/flux.green]",
                    border_style="bright_green",
                    safe_box=True,
                )
            )
            return snapshot

        retry = Confirm.ask(
            "La sessione non risulta ancora valida. Vuoi riprovare?",
            console=CONSOLE,
            default=True,
        )

        if not retry:
            raise SessionExpiredError(
                "Sessione non ripristinata. Le pagine completate rimangono "
                "nello staging."
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
                snapshot = recover_browser_session(client, config)

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
                    title="[flux.green]SESSIONE AGGANCIATA[/flux.green]",
                    subtitle="[flux.dim]TEMPORAL LINK STABLE[/flux.dim]",
                    border_style="bright_green",
                    safe_box=True,
                )
            )
            return 0
    except (BrowserClientError, SessionExpiredError) as error:
        CONSOLE.print(
            Panel(
                str(error),
                title="[flux.red]SESSION LINK LOST[/flux.red]",
                border_style="bright_red",
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
    table.add_column("Telemetria", style="flux.green", width=24)
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
            title="[flux.green]DESTINAZIONE RAGGIUNTA[/flux.green]",
            title_align="left",
            subtitle="[flux.dim]SNAPSHOT LOCKED[/flux.dim]",
            subtitle_align="right",
            border_style="bright_green",
            safe_box=True,
        )
    )


def create_browser_client(config: ScraperConfig) -> AccrediaBrowserClient:
    """Crea il client condivisibile tra una o più regioni."""

    return AccrediaBrowserClient(
        profile_dir=config.profile_dir,
        browser_channel=config.browser_channel,
        headless=config.headless,
        timeout_ms=config.request_timeout_ms,
        max_retries=config.network_retries,
    )


def download_region_with_client(
    config: ScraperConfig,
    client: AccrediaBrowserClient,
) -> RegionalRunSummary:
    """Scarica una regione usando un client già avviato."""

    if config.region is None:
        raise ValueError("Nessuna regione configurata.")

    layout = RegionStorageLayout(config.storage_root)
    progress = Progress(
        SpinnerColumn("dots12", style="flux.cyan"),
        TextColumn("[flux.cyan]FLUX[/flux.cyan] {task.description}"),
        BarColumn(
            bar_width=None,
            style="bright_blue",
            complete_style="bright_cyan",
            finished_style="bright_green",
            pulse_style="bright_yellow",
        ),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
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
            f"[flux.amber]COORDINATE LOCKED[/flux.amber]  "
            f"[flux.cyan]{config.region}[/flux.cyan]  //  "
            f"{total_results:,} risultati  //  {total_pages:,} pagine  //  "
            f"{mode.upper()}"
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

    with progress:
        return download_region(
            client=client,
            layout=layout,
            region=config.region,
            results_url=configured_results_url(config),
            delay_seconds=config.delay_seconds,
            local_retries=LOCAL_FILE_RETRIES,
            on_start=on_start,
            on_page=on_page,
        )


def print_download_error(
    message: str,
    *,
    title: str,
    border_style: str,
) -> None:
    CONSOLE.print(
        Panel(
            message,
            title=f"[flux.red]{title}[/flux.red]",
            border_style=border_style,
            safe_box=True,
        )
    )


def run_region_download(config: ScraperConfig) -> int:
    """Esegue il comando manuale relativo a una singola regione."""

    try:
        with create_browser_client(config) as client:
            while True:
                try:
                    summary = download_region_with_client(config, client)
                    break
                except SessionExpiredError:
                    # SQLite è già stato chiuso dal ciclo regionale. Dopo la
                    # verifica manuale il nuovo tentativo riprenderà le pagine
                    # registrate nello staging compatibile.
                    recover_browser_session(client, config)

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
            "Puoi rilanciare lo stesso comando anche successivamente: la "
            "ripresa avverrà se totale e struttura sono ancora compatibili."
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

    print_download_error(
        error_message,
        title=title,
        border_style=border_style,
    )
    return exit_code


def build_region_decisions(config: ScraperConfig) -> list[RegionDecision]:
    """Costruisce il piano del ciclo nazionale dai file locali."""

    decisions: list[RegionDecision] = []

    for region in ITALIAN_REGIONS:
        region_config = replace(config, region=region)
        layout = RegionStorageLayout(region_config.storage_root)
        decisions.append(
            decide_region_download(
                layout=layout,
                region=region,
                refresh_existing=config.refresh_existing,
                refresh_after_days=config.refresh_after_days,
            )
        )

    return decisions


def print_batch_plan(decisions: list[RegionDecision]) -> None:
    """Mostra quali regioni saranno elaborate o saltate."""

    table = Table(box=box.SIMPLE, safe_box=True, expand=True)
    table.add_column("Coordinate", style="flux.cyan")
    table.add_column("Azione", width=14)
    table.add_column("Motivo", overflow="fold")

    for decision in decisions:
        action = (
            "[flux.green]IN CODA[/flux.green]"
            if decision.should_download
            else "[flux.dim]ARCHIVIATA[/flux.dim]"
        )
        table.add_row(decision.region, action, decision.reason)

    CONSOLE.print(
        Panel(
            table,
            title="[flux.blue]DESTINATION BOARD // ITALIA[/flux.blue]",
            title_align="left",
            subtitle="[flux.dim]20 COORDINATE TEMPORALI[/flux.dim]",
            subtitle_align="right",
            border_style="bright_blue",
            safe_box=True,
        )
    )


def print_batch_summary(
    *,
    completed: list[RegionalRunSummary],
    skipped: list[RegionDecision],
    failed: list[tuple[str, str]],
) -> None:
    """Mostra il riepilogo finale del ciclo nazionale."""

    table = Table(
        box=box.SIMPLE,
        show_header=False,
        safe_box=True,
        expand=True,
    )
    table.add_column("Telemetria", style="flux.cyan", width=24)
    table.add_column("Valore")
    table.add_row("Regioni completate", str(len(completed)))
    table.add_row("Regioni saltate", str(len(skipped)))
    table.add_row("Regioni fallite", str(len(failed)))
    table.add_row(
        "Record unici elaborati",
        f"{sum(item.unique_records for item in completed):,}",
    )

    if failed:
        table.add_row(
            "Errori",
            "\n".join(
                f"{region}: {message}"
                for region, message in failed
            ),
        )

    CONSOLE.print(
        Panel(
            table,
            title="[flux.green]VIAGGIO NAZIONALE COMPLETATO[/flux.green]",
            title_align="left",
            subtitle="[flux.dim]TEMPORAL RUN REPORT[/flux.dim]",
            subtitle_align="right",
            border_style="bright_red" if failed else "bright_green",
            safe_box=True,
        )
    )


def run_all_regions(config: ScraperConfig) -> int:
    """Esegue tutte le regioni con una sola sessione Playwright."""

    decisions = build_region_decisions(config)
    pending = [item for item in decisions if item.should_download]
    skipped = [item for item in decisions if not item.should_download]
    completed: list[RegionalRunSummary] = []
    failed: list[tuple[str, str]] = []

    print_batch_plan(decisions)

    if not pending:
        print_batch_summary(
            completed=completed,
            skipped=skipped,
            failed=failed,
        )
        return 0

    try:
        # Lo stesso browser e gli stessi cookie vengono mantenuti per tutto
        # il ciclo, evitando venti riavvii e nuove verifiche della sessione.
        with create_browser_client(config) as client:
            for index, decision in enumerate(pending, start=1):
                region_config = replace(config, region=decision.region)
                rule_title = (
                    f"[flux.amber]T-{index:02d}/{len(pending):02d}[/flux.amber] "
                    f"[flux.cyan]{decision.region}[/flux.cyan]"
                )
                CONSOLE.rule(rule_title, style="bright_blue")

                try:
                    while True:
                        try:
                            summary = download_region_with_client(
                                region_config,
                                client,
                            )
                            break
                        except SessionExpiredError:
                            # La sessione viene rinnovata nello stesso browser;
                            # poi la regione riparte dallo staging esistente.
                            recover_browser_session(
                                client,
                                region_config,
                            )

                    completed.append(summary)
                    print_region_summary(summary)
                except SessionExpiredError:
                    # Annullamento o modalità headless: interrompe il ciclo
                    # nazionale conservando gli staging regionali.
                    raise
                except (
                    ParsingError,
                    RegionDownloadError,
                    StorageError,
                    OSError,
                    ValueError,
                ) as error:
                    # Un problema limitato ai dati di una regione non deve
                    # impedire il tentativo delle regioni successive.
                    failed.append((decision.region, str(error)))
                    print_download_error(
                        str(error),
                        title=f"{decision.region}: DOWNLOAD NON COMPLETATO",
                        border_style="red",
                    )
    except SessionExpiredError as error:
        print_download_error(
            str(error),
            title="SESSIONE NON VALIDA: CICLO INTERROTTO",
            border_style="yellow",
        )
        return 2
    except KeyboardInterrupt:
        print_download_error(
            "Le pagine completate sono conservate nei rispettivi staging. "
            "Rilancia lo stesso comando anche successivamente: ogni regione "
            "compatibile riprenderà dalla prima pagina mancante.",
            title="CICLO INTERROTTO",
            border_style="yellow",
        )
        return 130
    except BrowserClientError as error:
        print_download_error(
            str(error),
            title="BROWSER NON DISPONIBILE: CICLO INTERROTTO",
            border_style="red",
        )
        return 2

    print_batch_summary(
        completed=completed,
        skipped=skipped,
        failed=failed,
    )
    return 2 if failed else 0


def print_ready_message() -> None:
    CONSOLE.print(
        Panel(
            "Configurazione valida. Specifica --region o --all-regions "
            "per avviare il download, oppure usa --check-session.",
            title="[flux.amber]DESTINAZIONE NON IMPOSTATA[/flux.amber]",
            subtitle="[flux.dim]SYSTEM STANDBY[/flux.dim]",
            border_style="bright_yellow",
            safe_box=True,
        )
    )


def main() -> int:
    configure_logging()
    parser = build_argument_parser()
    config = config_from_arguments(parser)
    print_application_header(config)
    print_configuration(config)

    if config.check_session:
        return run_session_check(config)

    if config.region is not None:
        return run_region_download(config)

    if config.all_regions:
        return run_all_regions(config)

    print_ready_message()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
