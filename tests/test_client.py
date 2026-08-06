"""Test dei retry di rete del client Playwright."""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError

from accredia_downloader.client import (
    AccrediaBrowserClient,
    HttpRequestError,
    retry_delay_seconds,
)


class FakeResponse:
    status = 200

    def text(self) -> str:
        return "<html>ok</html>"

    def dispose(self) -> None:
        pass


class FakeRequest:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.calls = 0

    def get(self, *args: object, **kwargs: object) -> FakeResponse:
        del args, kwargs
        self.calls += 1

        if self.failures:
            raise self.failures.pop(0)

        return FakeResponse()


class FakeContext:
    def __init__(self, request: FakeRequest) -> None:
        self.request = request


def build_client(request: FakeRequest, retries: int) -> AccrediaBrowserClient:
    client = AccrediaBrowserClient(
        profile_dir=Path(".test-profile"),
        browser_channel="chromium",
        headless=True,
        timeout_ms=1_000,
        max_retries=retries,
    )
    client._context = FakeContext(request)  # type: ignore[assignment]
    return client


class RetryBackoffTests(unittest.TestCase):
    def test_backoff_is_capped_at_thirty_seconds(self) -> None:
        self.assertEqual(
            [retry_delay_seconds(index) for index in range(8)],
            [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0],
        )

    @patch("accredia_downloader.client.time.sleep")
    def test_retries_generic_etimedout_error(
        self,
        sleep: MagicMock,
    ) -> None:
        request = FakeRequest(
            [PlaywrightError("APIRequestContext.get: read ETIMEDOUT")]
        )
        client = build_client(request, retries=2)

        html = client.fetch_html("https://example.test/page=13")

        self.assertEqual(html, "<html>ok</html>")
        self.assertEqual(request.calls, 2)
        sleep.assert_called_once_with(1.0)

    @patch("accredia_downloader.client.time.sleep")
    def test_does_not_retry_non_network_error(
        self,
        sleep: MagicMock,
    ) -> None:
        request = FakeRequest(
            [PlaywrightError("Target page, context or browser closed")]
        )
        client = build_client(request, retries=10_000)

        with self.assertRaises(HttpRequestError):
            client.fetch_html("https://example.test/page=13")

        self.assertEqual(request.calls, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
