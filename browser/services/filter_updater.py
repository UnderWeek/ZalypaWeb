"""Asynchronous, atomic updates for EasyList-compatible subscriptions."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .adblock import AdBlocker

EASYLIST_URL = "https://easylist.to/easylist/easylist.txt"
MAX_FILTER_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FilterUpdateResult:
    source_url: str
    destination: Path
    rules_loaded: int
    bytes_downloaded: int
    updated_at: datetime


class FilterUpdateError(RuntimeError):
    """Raised when a remote subscription is unsafe or malformed."""


def _download_subscription(url: str, destination: Path, timeout: float) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise FilterUpdateError("Filter subscriptions must use HTTPS")
    request = Request(url, headers={"User-Agent": "Auralis-Browser/0.1 (+EasyList-compatible)"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - HTTPS validated above
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_FILTER_BYTES:
                raise FilterUpdateError("Filter subscription is larger than the safety limit")
            payload = response.read(MAX_FILTER_BYTES + 1)
    except FilterUpdateError:
        raise
    except Exception as error:
        raise FilterUpdateError(f"Could not download filter subscription: {error}") from error
    if len(payload) > MAX_FILTER_BYTES:
        raise FilterUpdateError("Filter subscription is larger than the safety limit")
    first_line = payload.lstrip(b"\xef\xbb\xbf").splitlines()[:1]
    if not first_line or b"Adblock" not in first_line[0]:
        raise FilterUpdateError("Downloaded file is not an Adblock-compatible subscription")

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=destination.name, suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return payload


async def update_filter_subscription(
    blocker: AdBlocker,
    destination: str | Path,
    *,
    url: str = EASYLIST_URL,
    source: str = "easylist",
    timeout: float = 30.0,
) -> FilterUpdateResult:
    """Fetch a list outside the GUI thread, atomically cache and activate it."""

    target = Path(destination)
    payload = await asyncio.to_thread(_download_subscription, url, target, timeout)
    rules = await asyncio.to_thread(
        blocker.load_rules, payload.decode("utf-8-sig", errors="replace"), source=source, replace_source=True
    )
    return FilterUpdateResult(
        source_url=url,
        destination=target,
        rules_loaded=rules,
        bytes_downloaded=len(payload),
        updated_at=datetime.now(UTC),
    )


__all__ = [
    "EASYLIST_URL",
    "FilterUpdateError",
    "FilterUpdateResult",
    "update_filter_subscription",
]
