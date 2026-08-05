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


# ---------------------------------------------------------------------------
# Costanti dell'applicazione
# ---------------------------------------------------------------------------

APP_NAME = "Accredia SC"

SEARCH_URL = (
    "https://services.accredia.it/ppsearch/"
    "accredia_companymask_remote.jsp"
    "?ID_LINK=1739&area=310"
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
    """Configura messaggi leggibili sia su Windows sia negli altri sistemi."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    """Punto di ingresso del programma."""

    configure_logging()

    parser = build_argument_parser()
    config = config_from_arguments(parser)

    logger = logging.getLogger(APP_NAME)

    logger.info("Configurazione valida.")
    logger.info("Directory dati: %s", config.output_root)
    logger.info("Directory pagine: %s", config.pages_dir)
    logger.info("Profilo Playwright: %s", config.profile_dir)
    logger.info("Browser: %s", config.browser_channel)
    logger.info("Modalità headless: %s", config.headless)
    logger.info("Ritardo tra richieste: %.1f secondi", config.delay_seconds)

    # Nei prossimi blocchi chiameremo qui il downloader effettivo.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())