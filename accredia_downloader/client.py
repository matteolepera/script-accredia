"""
Client Playwright per la banca dati Accredia.

Playwright viene usato per:

- mantenere un profilo persistente;
- conservare la sessione CAPTCHA;
- aprire la pagina dei risultati;
- effettuare richieste HTTP con gli stessi cookie del browser.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


RETRYABLE_HTTP_STATUSES = {
    429,
    500,
    502,
    503,
    504,
}


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

            launch_options: dict[str, object] = {
                "headless": self.headless,
                "viewport": {
                    "width": 1440,
                    "height": 1000,
                },
                "locale": "it-IT",

                # Mantiene attivo il sandbox di sicurezza di Chromium.
                # Evita inoltre l'avviso relativo a --no-sandbox
                # mostrato da Chrome e Microsoft Edge.
                "chromium_sandbox": True,
            }

            # `chromium` indica il browser distribuito da Playwright.
            # Chrome ed Edge richiedono invece il relativo channel.
            if self.browser_channel != "chromium":
                launch_options["channel"] = self.browser_channel

            self._context = (
                self._playwright.chromium
                .launch_persistent_context(
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

            finally:
                if response is not None:
                    response.dispose()

            if attempt < self.max_retries:
                # Backoff crescente: 1, 2, 4 secondi.
                time.sleep(2**attempt)

        raise HttpRequestError(
            f"Richiesta fallita dopo "
            f"{self.max_retries + 1} tentativi: {url}"
        ) from last_error