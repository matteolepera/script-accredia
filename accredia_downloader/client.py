"""
Client Playwright per la banca dati Accredia.

Playwright viene usato per:

- mantenere un profilo persistente;
- conservare la sessione CAPTCHA;
- aprire la pagina dei risultati;
- effettuare richieste HTTP con gli stessi cookie del browser.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


LOGGER = logging.getLogger(__name__)

RETRYABLE_HTTP_STATUSES = {
    429,
    500,
    502,
    503,
    504,
}

RETRYABLE_NETWORK_ERROR_MARKERS = (
    "connection",
    "econnrefused",
    "econnreset",
    "enetunreach",
    "enotfound",
    "err_internet_disconnected",
    "err_name_not_resolved",
    "err_network_changed",
    "etimedout",
    "network",
    "socket hang up",
    "timed out",
)

MAX_RETRY_DELAY_SECONDS = 30.0


def retry_delay_seconds(attempt: int) -> float:
    """Calcola un backoff esponenziale limitato a 30 secondi."""

    if attempt < 0:
        raise ValueError("Il numero del tentativo non può essere negativo.")

    # Limitare prima l'esponente evita interi enormi con migliaia di retry.
    return min(
        MAX_RETRY_DELAY_SECONDS,
        float(2 ** min(attempt, 5)),
    )


def is_retryable_network_error(error: PlaywrightError) -> bool:
    """Riconosce gli errori di rete temporanei prodotti da Playwright."""

    message = str(error).casefold()
    return any(
        marker in message
        for marker in RETRYABLE_NETWORK_ERROR_MARKERS
    )


class BrowserClientError(RuntimeError):
    """Errore nell'avvio o nell'utilizzo del browser."""


class HttpRequestError(BrowserClientError):
    """Errore durante una richiesta HTTP."""


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    """Contenuto e URL corrente di una pagina browser."""

    url: str
    html: str


class AccrediaBrowserClient:
    """Gestisce il browser persistente e le richieste condivise."""

    def __init__(
        self,
        *,
        profile_dir: Path,
        browser_channel: str,
        headless: bool,
        timeout_ms: int,
        max_retries: int,
    ) -> None:
        self.profile_dir = profile_dir.resolve()
        self.browser_channel = browser_channel
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.max_retries = max_retries

        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def __enter__(self) -> AccrediaBrowserClient:
        self.start()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def context(self) -> BrowserContext:
        """Restituisce il contesto attivo o genera un errore leggibile."""

        if self._context is None:
            raise BrowserClientError(
                "Il browser non è stato avviato."
            )

        return self._context

    @property
    def page(self) -> Page:
        """Restituisce la pagina attiva."""

        if self._page is None:
            raise BrowserClientError(
                "Nessuna pagina browser disponibile."
            )

        return self._page

    def start(self) -> None:
        """Avvia il profilo persistente Playwright."""

        if self._context is not None:
            return

        self.profile_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        try:
            self._playwright = sync_playwright().start()

            # Opzioni condivise da Chromium e Firefox.
            launch_options: dict[str, object] = {
                "headless": self.headless,
                "viewport": {
                    "width": 1440,
                    "height": 1000,
                },
                "locale": "it-IT",
            }

            if self.browser_channel == "firefox":
                # Firefox usa un motore separato e non accetta
                # le opzioni specifiche di Chromium.
                browser_type = self._playwright.firefox
            else:
                browser_type = self._playwright.chromium

                # Questa opzione è valida esclusivamente per Chromium.
                launch_options["chromium_sandbox"] = True

                # Chrome ed Edge sono canali del motore Chromium.
                # "chromium" usa il browser incluso in Playwright.
                if self.browser_channel != "chromium":
                    launch_options["channel"] = (
                        self.browser_channel
                    )

            # Ogni motore utilizza il proprio profilo persistente.
            self._context = (
                browser_type.launch_persistent_context(
                    str(self.profile_dir),
                    **launch_options,
                )
            )

            self._page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )

            self._page.set_default_timeout(
                self.timeout_ms
            )
            self._page.set_default_navigation_timeout(
                self.timeout_ms
            )

        except Exception as error:
            self.close()

            raise BrowserClientError(
                "Impossibile avviare il browser Playwright."
            ) from error

    def close(self) -> None:
        """Chiude browser e runtime Playwright in modo sicuro."""

        try:
            if self._context is not None:
                self._context.close()
        finally:
            self._context = None
            self._page = None

            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None

    def navigate(self, url: str) -> PageSnapshot:
        """Apre un URL nel browser e restituisce il contenuto."""

        try:
            self.page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.timeout_ms,
            )
        except PlaywrightTimeoutError as error:
            raise BrowserClientError(
                f"Timeout durante l'apertura di {url}"
            ) from error

        return self.current_snapshot()

    def current_snapshot(self) -> PageSnapshot:
        """Legge URL e HTML della pagina attualmente aperta."""

        return PageSnapshot(
            url=self.page.url,
            html=self.page.content(),
        )

    def fetch_html(
        self,
        url: str,
        *,
        referer: str | None = None,
    ) -> str:
        """
        Scarica HTML usando gli stessi cookie del browser.

        Questo metodo verrà utilizzato per le pagine successive alla prima.
        """

        headers: dict[str, str] = {}

        if referer:
            headers["Referer"] = referer

        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            response = None

            try:
                response = self.context.request.get(
                    url,
                    headers=headers,
                    timeout=self.timeout_ms,
                )

                status = response.status
                html = response.text()

                if status == 200:
                    return html

                if status not in RETRYABLE_HTTP_STATUSES:
                    raise HttpRequestError(
                        f"HTTP {status} durante la richiesta: {url}"
                    )

                last_error = HttpRequestError(
                    f"Errore HTTP temporaneo {status}: {url}"
                )

            except PlaywrightTimeoutError as error:
                last_error = error
            except PlaywrightError as error:
                # ETIMEDOUT e gli altri errori di connessione non sono
                # necessariamente PlaywrightTimeoutError. Vanno quindi
                # intercettati separatamente e ritentati.
                if not is_retryable_network_error(error):
                    raise HttpRequestError(
                        f"Errore Playwright durante la richiesta: {url}"
                    ) from error

                last_error = error

            finally:
                if response is not None:
                    try:
                        response.dispose()
                    except PlaywrightError:
                        # La risposta può risultare già chiusa dopo una
                        # disconnessione: non deve coprire l'errore originale.
                        pass

            if attempt < self.max_retries:
                delay = retry_delay_seconds(attempt)
                LOGGER.warning(
                    "Richiesta temporaneamente fallita "
                    "(tentativo %s/%s). Nuovo tentativo tra %.0f s.",
                    attempt + 1,
                    self.max_retries + 1,
                    delay,
                )
                time.sleep(delay)

        raise HttpRequestError(
            f"Richiesta fallita dopo "
            f"{self.max_retries + 1} tentativi: {url}"
        ) from last_error
