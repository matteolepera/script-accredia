#!/usr/bin/env python
"""
Downloader dei certificati pubblicati nella banca dati Accredia.

Il programma utilizzerà:

- Playwright per condividere la sessione del browser;
- BeautifulSoup per interpretare le pagine HTML;
- file JSON distinti per ogni tabella/certificato;
- un indice locale per riconoscere aggiornamenti e duplicati.

Questo primo blocco contiene soltanto la configurazione generale.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup
from rich import box
from rich.console import Console, Group
from rich.logging import RichHandler
from rich.panel import Panel
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
    parse_total_results,
)

# ---------------------------------------------------------------------------
# Costanti dell'applicazione
# ---------------------------------------------------------------------------

APP_NAME = "Accredia SC"

APP_VERSION = __version__

# Console condivisa da tutta l'applicazione.
# Rich rileva automaticamente le capacità del terminale Windows.
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

# Accredia mostra normalmente 20 tabelle/certificati per pagina.
PAGE_SIZE = 20

# Selettori CSS confermati durante l'analisi della pagina.
RESULTS_CONTAINER_SELECTOR = "div.ppsearch"
CERTIFICATE_TABLE_SELECTOR = "div.ppsearch > table"

# Testo visualizzato quando la sessione CAPTCHA non è più valida.
CAPTCHA_GATE_TEXT = "Verifica reCAPTCHA richiesta"

# Percorsi relativi alla directory dalla quale viene avviato lo script.
DEFAULT_OUTPUT_ROOT = Path("documenti") / "accredia"
DEFAULT_PROFILE_DIR = Path(".accredia-playwright-profile")


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScraperConfig:
    """
    Configurazione immutabile dello scraper.

    `frozen=True` impedisce modifiche accidentali durante l'esecuzione.
    `slots=True` riduce leggermente l'uso di memoria e impedisce
    l'aggiunta involontaria di attributi.
    """

    output_root: Path
    profile_dir: Path
    delay_seconds: float
    request_timeout_ms: int
    max_retries: int
    browser_channel: str
    headless: bool
    check_session: bool

    @property
    def pages_dir(self) -> Path:
        """Directory che conterrà i JSON organizzati per pagina."""
        return self.output_root / "pages"

    @property
    def state_dir(self) -> Path:
        """Directory contenente indici e stato dell'esecuzione."""
        return self.output_root / "state"

    @property
    def runs_dir(self) -> Path:
        """Directory contenente i report delle singole esecuzioni."""
        return self.output_root / "runs"

    @property
    def archive_dir(self) -> Path:
        """Directory destinata ai record non più presenti."""
        return self.output_root / "archive"

    @property
    def errors_dir(self) -> Path:
        """Directory nella quale salvare pagine o record non interpretabili."""
        return self.output_root / "errors"

    def validate(self) -> None:
        """Controlla i parametri prima di avviare qualsiasi richiesta."""

        if self.delay_seconds < 1:
            raise ValueError(
                "Il ritardo tra le richieste non può essere inferiore "
                "a 1 secondo."
            )

        if self.request_timeout_ms < 1_000:
            raise ValueError(
                "Il timeout delle richieste deve essere di almeno 1 secondo."
            )

        if self.max_retries < 0:
            raise ValueError(
                "Il numero massimo di tentativi non può essere negativo."
            )


# ---------------------------------------------------------------------------
# Argomenti della riga di comando
# ---------------------------------------------------------------------------

def build_argument_parser() -> argparse.ArgumentParser:
    """Definisce i parametri accettati dalla riga di comando."""

    parser = argparse.ArgumentParser(
        prog="accredia_scraper",
        description=(
            "Scarica e aggiorna i certificati pubblicati nella banca dati "
            "Accredia."
        ),
    )

    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=(
            "Directory principale dei dati. "
            "Default: documenti/accredia"
        ),
    )

    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_PROFILE_DIR,
        help=(
            "Profilo persistente utilizzato da Playwright. "
            "Default: .accredia-playwright-profile"
        ),
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Secondi di attesa tra due pagine. Default: 2",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Timeout di ogni richiesta espresso in secondi. Default: 60",
    )

    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Numero massimo di nuovi tentativi per pagina. Default: 3",
    )

    parser.add_argument(
        "--browser-channel",
        choices=("chrome", "msedge", "chromium"),
        default="chrome",
        help="Browser controllato da Playwright. Default: chrome",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help=(
            "Avvia il browser senza finestra. Funziona soltanto quando "
            "la sessione persistente è ancora valida."
        ),
    )

    parser.add_argument(
        "--check-session",
        action="store_true",
        help=(
            "Apre Accredia e verifica che la sessione possa "
            "accedere alla pagina dei risultati."
        ),
    )

    return parser


def config_from_arguments(
    parser: argparse.ArgumentParser,
) -> ScraperConfig:
    """Converte gli argomenti CLI nella configurazione dell'applicazione."""

    arguments = parser.parse_args()

    config = ScraperConfig(
        # `resolve()` genera un percorso assoluto compatibile con Windows.
        output_root=arguments.output_root.resolve(),
        profile_dir=arguments.profile_dir.resolve(),
        delay_seconds=arguments.delay,
        request_timeout_ms=arguments.timeout * 1_000,
        max_retries=arguments.retries,
        browser_channel=arguments.browser_channel,
        headless=arguments.headless,
        check_session=arguments.check_session,
    )

    try:
        config.validate()
    except ValueError as error:
        # `parser.error()` mostra il messaggio e termina con exit code 2.
        parser.error(str(error))

    return config


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging() -> None:
    """
    Configura il logging usando Rich.

    Il logger rimarrà disponibile per errori, retry e diagnostica,
    mentre pannelli e tabelle verranno usati per le informazioni principali.
    """

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
        # `force=True` evita configurazioni duplicate se main viene richiamato
        # più volte durante i test.
        force=True,
    )

# ---------------------------------------------------------------------------
# Interfaccia del terminale
# ---------------------------------------------------------------------------

def print_application_header() -> None:
    """Mostra l'intestazione principale dell'applicazione."""

    title = Text()
    title.append("ACCREDIA", style="bold cyan")
    title.append(" DOWNLOADER", style="bold white")

    subtitle = Text(
        "Raccolta e aggiornamento dei certificati",
        style="dim white",
    )

    version = Text(
        f"Versione {APP_VERSION}",
        style="dim cyan",
    )

    content = Group(
        title,
        subtitle,
        Text(""),
        version,
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
    """Converte un valore booleano in uno stato leggibile."""

    if value:
        return Text("ATTIVATA", style="bold yellow")

    return Text("DISATTIVATA", style="bold green")


def print_configuration(config: ScraperConfig) -> None:
    """Mostra la configurazione in una tabella compatta."""

    table = Table(
        box=box.SIMPLE,
        show_header=False,
        pad_edge=False,
        safe_box=True,
        expand=True,
    )

    table.add_column(
        "Parametro",
        style="bold cyan",
        no_wrap=True,
        width=24,
    )

    table.add_column(
        "Valore",
        style="white",
        overflow="fold",
    )

    table.add_row("Directory dati", str(config.output_root))
    table.add_row("Directory pagine", str(config.pages_dir))
    table.add_row("Profilo Playwright", str(config.profile_dir))
    table.add_row("Browser", config.browser_channel)
    table.add_row("Modalità headless", format_enabled(config.headless))
    table.add_row(
        "Ritardo richieste",
        f"{config.delay_seconds:.1f} secondi",
    )
    table.add_row(
        "Timeout",
        f"{config.request_timeout_ms // 1_000} secondi",
    )
    table.add_row(
        "Retry massimi",
        str(config.max_retries),
    )
    table.add_row(
        "Controllo sessione",
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


def print_ready_message() -> None:
    """Comunica che la configurazione è stata validata."""

    message = Text()
    message.append("OK", style="bold green")
    message.append("  Configurazione validata. ")
    message.append(
        "Il downloader è pronto per essere avviato.",
        style="dim",
    )

    CONSOLE.print(
        Panel(
            message,
            border_style="green",
            padding=(0, 1),
            safe_box=True,
        )
    )

def is_results_page(snapshot: PageSnapshot) -> bool:
    """Controlla superficialmente se la pagina contiene risultati."""

    if CAPTCHA_GATE_TEXT.casefold() in snapshot.html.casefold():
        return False

    soup = BeautifulSoup(snapshot.html, "lxml")

    tables = [
        table
        for table in soup.select(
            CERTIFICATE_TABLE_SELECTOR
        )
        if CERTIFICATE_MARKER.casefold()
        in table.get_text(" ", strip=True).casefold()
    ]

    return (
        parse_total_results(snapshot.html) is not None
        and len(tables) > 0
    )


def print_manual_validation_instructions() -> None:
    """Mostra le istruzioni per la validazione manuale."""

    message = Text()

    message.append(
        "La sessione non consente ancora l'accesso ai risultati.\n\n",
        style="bold yellow",
    )
    message.append(
        "1. Completa manualmente il CAPTCHA nel browser.\n"
        "2. Lascia vuoti i filtri per ottenere tutti i risultati.\n"
        "3. Clicca Cerca.\n"
        "4. Attendi che venga mostrato l'elenco dei certificati."
    )

    CONSOLE.print(
        Panel(
            message,
            title="[bold]VERIFICA MANUALE[/bold]",
            title_align="left",
            border_style="yellow",
            padding=(1, 2),
            safe_box=True,
        )
    )


def print_session_success(
    *,
    total_results: int,
    records_on_page: int,
    current_url: str,
) -> None:
    """Mostra il risultato positivo del controllo."""

    table = Table(
        box=box.SIMPLE,
        show_header=False,
        pad_edge=False,
        safe_box=True,
        expand=True,
    )

    table.add_column(
        "Parametro",
        style="bold green",
        width=24,
        no_wrap=True,
    )
    table.add_column(
        "Valore",
        overflow="fold",
    )

    table.add_row(
        "Stato sessione",
        "[bold green]VALIDA[/bold green]",
    )
    table.add_row(
        "Risultati",
        f"{total_results:,}".replace(",", "."),
    )
    table.add_row(
        "Tabelle nella pagina",
        str(records_on_page),
    )
    table.add_row(
        "URL corrente",
        current_url,
    )

    CONSOLE.print(
        Panel(
            table,
            title="[bold]CONTROLLO COMPLETATO[/bold]",
            title_align="left",
            border_style="green",
            padding=(0, 1),
            safe_box=True,
        )
    )


def run_session_check(
    config: ScraperConfig,
) -> int:
    """
    Verifica la sessione senza scaricare o salvare certificati.

    Se necessario permette all'utente di completare manualmente il CAPTCHA.
    """

    try:
        with AccrediaBrowserClient(
            profile_dir=config.profile_dir,
            browser_channel=config.browser_channel,
            headless=config.headless,
            timeout_ms=config.request_timeout_ms,
            max_retries=config.max_retries,
        ) as client:
            CONSOLE.print(
                "\n[cyan]Apertura della pagina dei risultati...[/cyan]"
            )

            snapshot = client.navigate(RESULTS_URL)

            if not is_results_page(snapshot):
                if config.headless:
                    CONSOLE.print(
                        Panel(
                            "La sessione non è valida. "
                            "Ripeti il controllo senza --headless.",
                            border_style="red",
                            safe_box=True,
                        )
                    )
                    return 2

                # Torniamo alla maschera per permettere il CAPTCHA.
                client.navigate(SEARCH_URL)
                print_manual_validation_instructions()

                Confirm.ask(
                    "Hai completato il CAPTCHA e visualizzi i risultati?",
                    console=CONSOLE,
                    default=True,
                )

                snapshot = client.current_snapshot()

            if not is_results_page(snapshot):
                CONSOLE.print(
                    Panel(
                        "La pagina corrente non contiene risultati validi.",
                        border_style="red",
                        safe_box=True,
                    )
                )
                return 2

            soup = BeautifulSoup(snapshot.html, "lxml")

            tables = [
                table
                for table in soup.select(
                    CERTIFICATE_TABLE_SELECTOR
                )
                if CERTIFICATE_MARKER.casefold()
                in table.get_text(" ", strip=True).casefold()
            ]

            total_results = parse_total_results(
                snapshot.html
            )

            if total_results is None:
                return 2

            print_session_success(
                total_results=total_results,
                records_on_page=len(tables),
                current_url=snapshot.url,
            )

            return 0

    except BrowserClientError as error:
        CONSOLE.print(
            Panel(
                str(error),
                title="[bold]ERRORE BROWSER[/bold]",
                border_style="red",
                safe_box=True,
            )
        )
        return 2

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    """Punto di ingresso del programma."""

    configure_logging()
    print_application_header()

    parser = build_argument_parser()
    config = config_from_arguments(parser)

    print_configuration(config)

    if config.check_session:
        return run_session_check(config)

    print_ready_message()

    # Nei prossimi blocchi verrà avviato qui il downloader.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())