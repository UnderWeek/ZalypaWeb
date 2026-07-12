"""A small, embeddable EasyList-compatible network request blocker.

It intentionally implements network rules only.  Cosmetic rules are retained by
filter list projects for browser-side CSS injection and are ignored here until a
WebEngine cosmetic-filter adapter is installed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_RESOURCE_TYPES = frozenset(
    {
        "document",
        "subdocument",
        "script",
        "stylesheet",
        "image",
        "font",
        "media",
        "object",
        "xmlhttprequest",
        "websocket",
        "ping",
        "other",
    }
)
_TYPE_ALIASES = {"xhr": "xmlhttprequest", "frame": "subdocument"}
_INDEX_TOKEN = re.compile(r"[a-z0-9]{4,}", re.IGNORECASE)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _domain_matches(host: str, domain: str) -> bool:
    normalized = domain.lower().lstrip(".").rstrip(".")
    return bool(normalized) and (host == normalized or host.endswith("." + normalized))


def _site_key(host: str) -> str:
    """Return a pragmatic site key without depending on a public suffix list."""

    if not host or host == "localhost" or ":" in host:
        return host
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) > 1 else host


def _is_third_party(request_url: str, first_party_url: str | None) -> bool:
    if not first_party_url:
        return False
    request_host = _host(request_url)
    first_host = _host(first_party_url)
    return bool(request_host and first_host and _site_key(request_host) != _site_key(first_host))


def _easylist_pattern_to_regex(pattern: str) -> re.Pattern[str]:
    if len(pattern) >= 2 and pattern.startswith("/") and pattern.endswith("/"):
        return re.compile(pattern[1:-1], re.IGNORECASE)

    domain_anchor = pattern.startswith("||")
    start_anchor = pattern.startswith("|") and not domain_anchor
    end_anchor = pattern.endswith("|")
    if domain_anchor:
        pattern = pattern[2:]
    elif start_anchor:
        pattern = pattern[1:]
    if end_anchor:
        pattern = pattern[:-1]

    pieces: list[str] = []
    for character in pattern:
        if character == "*":
            pieces.append(".*")
        elif character == "^":
            pieces.append(r"(?:[^\w\d_.%-]|$)")
        else:
            pieces.append(re.escape(character))
    body = "".join(pieces)
    if domain_anchor:
        # Anchor at the URL authority and allow any number of subdomains.
        body = r"^[a-z][a-z0-9+.-]*://(?:[^/?#]*\.)?" + body
    elif start_anchor:
        body = "^" + body
    if end_anchor:
        body += "$"
    return re.compile(body, re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AdBlockRule:
    raw: str
    pattern: re.Pattern[str] = field(compare=False, repr=False)
    exception: bool = False
    included_domains: frozenset[str] = frozenset()
    excluded_domains: frozenset[str] = frozenset()
    included_types: frozenset[str] = frozenset()
    excluded_types: frozenset[str] = frozenset()
    third_party: bool | None = None
    source: str = "custom"

    def matches(
        self,
        url: str,
        *,
        first_party_url: str | None = None,
        resource_type: str | None = None,
    ) -> bool:
        first_host = _host(first_party_url or url)
        if self.included_domains and not any(
            _domain_matches(first_host, domain) for domain in self.included_domains
        ):
            return False
        if any(_domain_matches(first_host, domain) for domain in self.excluded_domains):
            return False
        normalized_type = _TYPE_ALIASES.get(resource_type or "", resource_type or "other").lower()
        if self.included_types and normalized_type not in self.included_types:
            return False
        if normalized_type in self.excluded_types:
            return False
        if self.third_party is not None and _is_third_party(url, first_party_url) != self.third_party:
            return False
        return self.pattern.search(url) is not None


@dataclass(frozen=True, slots=True)
class AdBlockDecision:
    blocked: bool
    url: str
    matched_rule: str | None = None
    source: str | None = None
    reason: str | None = None


def parse_rule(line: str, *, source: str = "custom") -> AdBlockRule | None:
    raw = line.strip()
    if not raw or raw.startswith("!") or raw.startswith("["):
        return None
    if "##" in raw or "#@#" in raw or "#$#" in raw or "#?#" in raw:
        return None

    exception = raw.startswith("@@")
    body = raw[2:] if exception else raw
    pattern_text, separator, options_text = body.partition("$")
    if not pattern_text:
        return None
    included_domains: set[str] = set()
    excluded_domains: set[str] = set()
    included_types: set[str] = set()
    excluded_types: set[str] = set()
    third_party: bool | None = None

    if separator:
        for option in options_text.split(","):
            option = option.strip().lower()
            if not option:
                continue
            negated = option.startswith("~")
            key = option[1:] if negated else option
            key = _TYPE_ALIASES.get(key, key)
            if key == "third-party":
                third_party = not negated
            elif key.startswith("domain="):
                for domain in key.removeprefix("domain=").split("|"):
                    domain = domain.strip()
                    if not domain:
                        continue
                    if domain.startswith("~"):
                        excluded_domains.add(domain[1:])
                    else:
                        included_domains.add(domain)
            elif key in _RESOURCE_TYPES:
                (excluded_types if negated else included_types).add(key)
            # Unsupported options are ignored; rejecting an entire filter would
            # create surprising holes in otherwise useful EasyList rules.
    try:
        compiled = _easylist_pattern_to_regex(pattern_text)
    except re.error:
        logger.warning("Ignoring invalid ad-block regular expression from %s: %s", source, raw)
        return None
    return AdBlockRule(
        raw=raw,
        pattern=compiled,
        exception=exception,
        included_domains=frozenset(included_domains),
        excluded_domains=frozenset(excluded_domains),
        included_types=frozenset(included_types),
        excluded_types=frozenset(excluded_types),
        third_party=third_party,
        source=source,
    )


def _index_key(rule: AdBlockRule) -> str | None:
    """Return a mandatory literal fragment used to shortlist a network rule.

    Raw regular-expression filters stay in the generic bucket because branches
    such as ``(foo|bar)`` do not have one mandatory literal. EasyList's regular
    network patterns do, so a four/five-character fragment is a safe index.
    """

    raw = rule.raw.removeprefix("@@").partition("$")[0]
    if len(raw) >= 2 and raw.startswith("/") and raw.endswith("/"):
        return None
    tokens = _INDEX_TOKEN.findall(raw.casefold())
    if not tokens:
        return None
    longest = max(tokens, key=len)
    return longest[:5] if len(longest) >= 5 else longest


def _url_index_keys(url: str) -> set[str]:
    keys: set[str] = set()
    for token in _INDEX_TOKEN.findall(url.casefold()):
        if len(token) == 4:
            keys.add(token)
            continue
        keys.update(token[index : index + 5] for index in range(len(token) - 4))
        keys.update(token[index : index + 4] for index in range(len(token) - 3))
    return keys


class AdBlocker:
    """Thread-safe filter engine suitable for a WebEngine request interceptor."""

    def __init__(self, config_path: str | Path | None = None, *, enabled: bool = True) -> None:
        self.config_path = Path(config_path) if config_path is not None else None
        self.enabled = enabled
        self._rules: list[AdBlockRule] = []
        self._custom_rules: list[str] = []
        self._whitelist: set[str] = set()
        self._blocked_count = 0
        self._keyword_index: dict[str, tuple[AdBlockRule, ...]] = {}
        self._generic_rules: tuple[AdBlockRule, ...] = ()
        self._lock = threading.RLock()
        self._load_config()
        with self._lock:
            self._reindex_locked()

    @property
    def blocked_count(self) -> int:
        with self._lock:
            return self._blocked_count

    @property
    def rule_count(self) -> int:
        with self._lock:
            return len(self._rules)

    @property
    def whitelist(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._whitelist)

    @property
    def custom_rules(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._custom_rules)

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = bool(enabled)
            self._save_config()

    def load_rules(
        self,
        rules: str | Iterable[str],
        *,
        source: str = "easylist",
        replace_source: bool = False,
    ) -> int:
        lines = rules.splitlines() if isinstance(rules, str) else rules
        parsed = [rule for line in lines if (rule := parse_rule(line, source=source)) is not None]
        with self._lock:
            if replace_source:
                self._rules = [rule for rule in self._rules if rule.source != source]
            self._rules.extend(parsed)
            self._reindex_locked()
        logger.info("Loaded %d network rules from %s", len(parsed), source)
        return len(parsed)

    def load_filter_file(
        self, path: str | Path, *, source: str | None = None, replace_source: bool = True
    ) -> int:
        filter_path = Path(path)
        text = filter_path.read_text(encoding="utf-8-sig", errors="replace")
        return self.load_rules(text, source=source or filter_path.name, replace_source=replace_source)

    def clear_rules(self, source: str | None = None) -> None:
        with self._lock:
            if source is None:
                self._rules.clear()
                # Rehydrate persisted custom rules, which remain user settings.
                for raw in self._custom_rules:
                    if rule := parse_rule(raw, source="custom"):
                        self._rules.append(rule)
            else:
                self._rules = [rule for rule in self._rules if rule.source != source]
            self._reindex_locked()

    def add_custom_rule(self, raw_rule: str) -> AdBlockRule:
        rule = parse_rule(raw_rule, source="custom")
        if rule is None:
            raise ValueError("Not a supported network filter rule")
        with self._lock:
            if raw_rule.strip() not in self._custom_rules:
                self._custom_rules.append(raw_rule.strip())
                self._rules.append(rule)
                self._reindex_locked()
                self._save_config()
        return rule

    def remove_custom_rule(self, raw_rule: str) -> bool:
        normalized = raw_rule.strip()
        with self._lock:
            if normalized not in self._custom_rules:
                return False
            self._custom_rules.remove(normalized)
            self._rules = [
                rule for rule in self._rules if not (rule.source == "custom" and rule.raw == normalized)
            ]
            self._reindex_locked()
            self._save_config()
            return True

    def whitelist_domain(self, domain: str) -> None:
        normalized = domain.strip().lower().lstrip(".").rstrip(".")
        if not normalized or "/" in normalized:
            raise ValueError("Expected a domain name")
        with self._lock:
            self._whitelist.add(normalized)
            self._save_config()

    def remove_whitelist(self, domain: str) -> bool:
        normalized = domain.strip().lower().lstrip(".").rstrip(".")
        with self._lock:
            existed = normalized in self._whitelist
            self._whitelist.discard(normalized)
            if existed:
                self._save_config()
            return existed

    add_to_whitelist = whitelist_domain
    remove_from_whitelist = remove_whitelist

    def evaluate(
        self,
        url: str,
        *,
        first_party_url: str | None = None,
        resource_type: str | None = None,
    ) -> AdBlockDecision:
        request_host = _host(url)
        first_party_host = _host(first_party_url or "")
        with self._lock:
            if not self.enabled:
                return AdBlockDecision(False, url, reason="disabled")
            if any(
                _domain_matches(request_host, domain) or _domain_matches(first_party_host, domain)
                for domain in self._whitelist
            ):
                return AdBlockDecision(False, url, reason="whitelisted")
            candidates: list[AdBlockRule] = list(self._generic_rules)
            seen = {id(rule) for rule in candidates}
            for key in _url_index_keys(url):
                for rule in self._keyword_index.get(key, ()):
                    if id(rule) not in seen:
                        seen.add(id(rule))
                        candidates.append(rule)

        matched_block: AdBlockRule | None = None
        for rule in candidates:
            if not rule.matches(url, first_party_url=first_party_url, resource_type=resource_type):
                continue
            if rule.exception:
                return AdBlockDecision(False, url, rule.raw, rule.source, reason="exception_rule")
            if matched_block is None:
                matched_block = rule
        if matched_block is None:
            return AdBlockDecision(False, url)
        with self._lock:
            self._blocked_count += 1
        return AdBlockDecision(True, url, matched_block.raw, matched_block.source, reason="filter_rule")

    def should_block(
        self,
        url: str,
        first_party_url: str | None = None,
        resource_type: str | None = None,
    ) -> bool:
        return self.evaluate(url, first_party_url=first_party_url, resource_type=resource_type).blocked

    should_block_request = should_block

    def _reindex_locked(self) -> None:
        index: dict[str, list[AdBlockRule]] = {}
        generic: list[AdBlockRule] = []
        for rule in self._rules:
            key = _index_key(rule)
            if key is None:
                generic.append(rule)
            else:
                index.setdefault(key, []).append(rule)
        self._keyword_index = {key: tuple(values) for key, values in index.items()}
        self._generic_rules = tuple(generic)

    def _load_config(self) -> None:
        if self.config_path is None or not self.config_path.exists():
            return
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            if payload.get("version") != 1:
                raise ValueError("Unsupported ad-block settings version")
            self.enabled = bool(payload.get("enabled", self.enabled))
            self._whitelist = {
                str(domain).lower().lstrip(".").rstrip(".")
                for domain in payload.get("whitelist", [])
                if str(domain).strip()
            }
            for raw in payload.get("custom_rules", []):
                if rule := parse_rule(str(raw), source="custom"):
                    self._custom_rules.append(rule.raw)
                    self._rules.append(rule)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception("Could not read ad-block settings from %s", self.config_path)

    def _save_config(self) -> None:
        if self.config_path is None:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="adblock-", suffix=".tmp", dir=self.config_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": 1,
                        "enabled": self.enabled,
                        "whitelist": sorted(self._whitelist),
                        "custom_rules": self._custom_rules,
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.config_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise


# Backwards-friendly spelling used by some UI adapters.
AdBlockService = AdBlocker
