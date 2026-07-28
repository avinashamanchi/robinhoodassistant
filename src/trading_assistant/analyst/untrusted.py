"""Deterministic quarantine boundary for raw external text.

Raw provider text is accepted only by :class:`UntrustedContentGateway`. The
gateway normalizes and strips active content, records metadata-only audit
events, and returns immutable typed content. It has no network, file, model,
connector, broker, or tool authority.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
)
from sqlalchemy import case, func, or_, text as sql_text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..db.models import UntrustedIngestEvent

MAX_ITEMS_PER_REQUEST = 20
MAX_CONTENT_BYTES = 16 * 1024
MAX_NORMALIZED_CHARACTERS = 16_000
_MAX_SOURCE_ID_CHARACTERS = 256
_MAX_SOURCE_NAME_CHARACTERS = 256
_MAX_ENCODED_CANDIDATE_CHARACTERS = 4_096
_MAX_CANONICALIZATION_PASSES = 4

SourceKind = Literal["alpaca_news", "filing", "search", "pasted"]
FindingSeverity = Literal["low", "medium", "high"]

_BIDI_CONTROLS = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)
_SUPPRESSED_HTML_ELEMENTS = frozenset(
    {"script", "style", "form", "iframe", "object", "embed"}
)
_DIRECT_INSTRUCTION_RE = re.compile(
    r"(?is)\b(?:ignore|disregard|forget|override)\s+"
    r"(?:all\s+|any\s+|the\s+|your\s+)?"
    r"(?:previous|prior|above|system|developer)?\s*"
    r"(?:instructions?|prompts?|rules?)\b[^.!?\n]*[.!?]?"
)
_MUTABLE_TOOL_NAMES = (
    "propose_order",
    "submit_order",
    "create_conditional_rule",
    "cancel_rule",
    "approve_order",
    "reject_order",
    "reset_killswitch",
)
_TOOL_NAME_PATTERN = "|".join(re.escape(name) for name in _MUTABLE_TOOL_NAMES)
_INDIRECT_TOOL_RE = re.compile(
    rf"(?is)\b(?:call|use|invoke|execute|run|trigger)\s+"
    rf"(?:the\s+)?[`'\"]*(?:{_TOOL_NAME_PATTERN})[`'\"]*"
    rf"\b[^.!?\n]*[.!?]?"
)
_TOOL_JSON_MARKER_RE = re.compile(
    rf"""(?is)
    ["'](?:type|tool|tool_name|name)["']\s*:\s*
    ["'][^"']*(?:tool_use|{_TOOL_NAME_PATTERN})[^"']*["']
    |
    ["'](?:{_TOOL_NAME_PATTERN})["']\s*:
    """,
    re.VERBOSE,
)
_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]\r\n]{0,1024}\]\((?:[^()\r\n]|\([^)\r\n]*\)){0,4096}\)"
)
_DATA_URL_RE = re.compile(r"(?is)\bdata:[^\s<>'\"\])]{1,8192}")
_ENCODED_PAYLOAD_CUE_RE = re.compile(
    r"(?is)\b(?:"
    r"base64"
    r"|decode\s+and\s+obey"
    r"(?:\s+(?:this|the\s+following)(?:\s+payload)?)?"
    r")\s*:\s*"
)
_BASE64_TOKEN_RE = re.compile(r"[A-Za-z0-9_+/-]+={0,2}")
_GENERIC_BASE64_RE = re.compile(
    rf"(?<![A-Za-z0-9_+/-])"
    rf"[A-Za-z0-9_+/-]{{16,{_MAX_ENCODED_CANDIDATE_CHARACTERS}}}={{0,2}}"
    rf"(?![A-Za-z0-9_+/-])"
)


class InjectionFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    severity: FindingSeverity


class UntrustedContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_kind: SourceKind
    source_id: str = Field(min_length=1, max_length=_MAX_SOURCE_ID_CHARACTERS)
    source_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=_MAX_SOURCE_NAME_CHARACTERS,
    )
    source_url: HttpUrl | None = None
    published_at: datetime | None = None
    received_at: datetime
    normalized_text: str = Field(max_length=MAX_NORMALIZED_CHARACTERS)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    findings: tuple[InjectionFinding, ...] = ()

    @field_validator("published_at", "received_at")
    @classmethod
    def timestamps_are_timezone_aware(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError("timestamp must be timezone-aware")
        return value


class UntrustedFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=2_000)
    source_ref: str = Field(min_length=1, max_length=_MAX_SOURCE_ID_CHARACTERS)


class UntrustedSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: tuple[UntrustedFact, ...] = Field(max_length=20)
    uncertainties: tuple[str, ...] = Field(max_length=10)
    source_refs: tuple[str, ...] = Field(max_length=20)
    injection_flags: tuple[str, ...] = Field(max_length=20)

    @field_validator("uncertainties")
    @classmethod
    def bound_uncertainties(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or len(value) > 1_000 for value in values):
            raise ValueError("uncertainty must contain 1..1000 characters")
        return values

    @field_validator("source_refs")
    @classmethod
    def bound_source_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not value or len(value) > _MAX_SOURCE_ID_CHARACTERS
            for value in values
        ):
            raise ValueError("source reference is invalid")
        return values

    @field_validator("injection_flags")
    @classmethod
    def bound_injection_flags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        pattern = re.compile(r"^[a-z0-9_]{1,64}$")
        if any(pattern.fullmatch(value) is None for value in values):
            raise ValueError("injection flag is invalid")
        return values


class UntrustedContentError(ValueError):
    """Stable redacted rejection raised at the quarantine boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed_depth = 0
        self.saw_markup = False
        self.saw_active_markup = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        self.saw_markup = True
        if tag.casefold() in _SUPPRESSED_HTML_ELEMENTS:
            self.saw_active_markup = True
            self.suppressed_depth += 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() in _SUPPRESSED_HTML_ELEMENTS:
            self.suppressed_depth -= 1

    def handle_endtag(self, tag: str) -> None:
        self.saw_markup = True
        if (
            tag.casefold() in _SUPPRESSED_HTML_ELEMENTS
            and self.suppressed_depth > 0
        ):
            self.suppressed_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.suppressed_depth == 0:
            self.parts.append(data)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_source_hash(source_kind: str, source_id: str) -> str:
    return _sha256(f"{source_kind}\x00{source_id}".encode("utf-8"))


def _add_finding(
    findings: dict[str, InjectionFinding],
    code: str,
    severity: FindingSeverity,
) -> None:
    findings.setdefault(code, InjectionFinding(code=code, severity=severity))


def _strip_hidden_controls(
    text: str,
    findings: dict[str, InjectionFinding],
) -> str:
    output: list[str] = []
    for character in text:
        if character == "\x00":
            _add_finding(findings, "nul_control", "high")
            continue
        if character in _BIDI_CONTROLS:
            _add_finding(findings, "bidi_control", "high")
            continue
        category = unicodedata.category(character)
        if category in {"Cc", "Cf"} and character not in {"\n", "\t"}:
            _add_finding(findings, "hidden_control", "high")
            continue
        output.append(character)
    return "".join(output)


def _strip_html(
    text: str,
    findings: dict[str, InjectionFinding],
) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        _add_finding(findings, "malformed_html", "medium")
        return re.sub(r"(?s)<[^>]*>", " ", text)
    if parser.saw_active_markup:
        _add_finding(findings, "active_html", "high")
    elif parser.saw_markup:
        _add_finding(findings, "html_markup", "low")
    return " ".join(parser.parts)


def _enforce_canonical_bounds(
    text: str,
    code: str = "canonicalization_too_large",
) -> None:
    if (
        len(text) > MAX_NORMALIZED_CHARACTERS
        or len(text.encode("utf-8")) > MAX_CONTENT_BYTES
    ):
        raise UntrustedContentError(code)


def _canonicalize_active_content_once(
    text: str,
    findings: dict[str, InjectionFinding],
) -> str:
    try:
        text = html.unescape(text)
    except Exception:
        raise UntrustedContentError("canonicalization_failed") from None
    _enforce_canonical_bounds(text)
    text = unicodedata.normalize("NFC", text)
    _enforce_canonical_bounds(text)
    text = _strip_hidden_controls(text, findings)
    text = _strip_html(text, findings)

    if _DATA_URL_RE.search(text):
        _add_finding(findings, "data_url", "high")
    if _MARKDOWN_IMAGE_RE.search(text):
        _add_finding(findings, "remote_image", "high")
    text = _MARKDOWN_IMAGE_RE.sub(" ", text)
    text = _DATA_URL_RE.sub(" ", text)
    _enforce_canonical_bounds(text)
    return text


def _canonicalize_active_content(
    raw_text: str,
    findings: dict[str, InjectionFinding],
) -> str:
    text = unicodedata.normalize("NFC", raw_text)
    _enforce_canonical_bounds(text, "normalized_content_too_large")
    for _attempt in range(_MAX_CANONICALIZATION_PASSES):
        canonical = _canonicalize_active_content_once(text, findings)
        if canonical == text:
            return canonical
        text = canonical
    raise UntrustedContentError("canonicalization_not_converged")


def _decoded_instruction(candidate: str) -> bool:
    if len(candidate) > _MAX_ENCODED_CANDIDATE_CHARACTERS:
        return False
    compact = candidate.strip()
    if not compact:
        return False
    padded = compact + ("=" * (-len(compact) % 4))
    decoded: bytes | None = None
    for altchars in (None, b"-_"):
        try:
            decoded = base64.b64decode(
                padded.encode("ascii"),
                altchars=altchars,
                validate=True,
            )
            break
        except (UnicodeEncodeError, binascii.Error, ValueError):
            continue
    if decoded is None or len(decoded) > MAX_CONTENT_BYTES:
        return False
    try:
        decoded_text = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return False
    decoded_findings: dict[str, InjectionFinding] = {}
    try:
        canonical = _canonicalize_active_content(
            decoded_text,
            decoded_findings,
        )
    except UntrustedContentError:
        return True
    folded = canonical.casefold()
    return bool(
        _DIRECT_INSTRUCTION_RE.search(canonical)
        or _INDIRECT_TOOL_RE.search(canonical)
        or _TOOL_JSON_MARKER_RE.search(canonical)
        or any(name in folded for name in _MUTABLE_TOOL_NAMES)
        or any(
            finding.code
            in {
                "active_html",
                "bidi_control",
                "hidden_control",
                "nul_control",
            }
            for finding in decoded_findings.values()
        )
    )


def _base64_tokens_after_cue(
    text: str,
    start: int,
) -> tuple[list[tuple[str, int, int]], int]:
    scan_end = min(len(text), start + MAX_CONTENT_BYTES)
    position = start
    while position < scan_end and text[position].isspace():
        position += 1
    first_start = position
    tokens: list[tuple[str, int, int]] = []

    while position < scan_end:
        match = _BASE64_TOKEN_RE.match(text, position, scan_end)
        if match is None:
            break
        token = match.group(0)
        if tokens and len(token.rstrip("=")) not in {2, 3, 4}:
            break
        tokens.append((token, match.start(), match.end()))
        position = match.end()
        if len(tokens) == 1 and len(token.rstrip("=")) > 4:
            break
        if token.endswith("=") or position >= scan_end:
            break
        separator = re.match(r"[ \t\r\n]+", text[position:scan_end])
        if separator is None:
            break
        position += separator.end()

    if tokens:
        return tokens, tokens[-1][2]
    malformed_end = first_start
    while (
        malformed_end < scan_end
        and not text[malformed_end].isspace()
    ):
        malformed_end += 1
    return [], malformed_end


def _malicious_fragmented_prefix(
    tokens: Sequence[tuple[str, int, int]],
) -> int | None:
    if not tokens:
        return None
    if len(tokens) == 1:
        return 1 if _decoded_instruction(tokens[0][0]) else None
    compact = "".join(token for token, _start, _end in tokens)
    if len(compact) > _MAX_ENCODED_CANDIDATE_CHARACTERS:
        return None
    prefix_lengths: list[int] = []
    total = 0
    for token, _start, _end in tokens:
        total += len(token)
        prefix_lengths.append(total)
    for count in range(len(tokens), 3, -1):
        candidate = compact[: prefix_lengths[count - 1]]
        if _decoded_instruction(candidate):
            return count
    return None


def _strip_cued_encoded_payloads(
    text: str,
    findings: dict[str, InjectionFinding],
) -> str:
    intervals: list[tuple[int, int]] = []
    cursor = 0
    while True:
        cue = _ENCODED_PAYLOAD_CUE_RE.search(text, cursor)
        if cue is None:
            break
        tokens, candidate_end = _base64_tokens_after_cue(text, cue.end())
        malicious_count = _malicious_fragmented_prefix(tokens)
        if malicious_count is not None:
            _add_finding(findings, "encoded_instruction", "high")
            candidate_end = tokens[malicious_count - 1][2]
        else:
            _add_finding(findings, "malformed_encoding", "medium")
        interval_end = max(cue.end(), candidate_end)
        intervals.append((cue.start(), interval_end))
        cursor = max(interval_end, cue.end())

    for start, end in reversed(intervals):
        text = text[:start] + " " + text[end:]
    return text


def _strip_encoded_payloads(
    text: str,
    findings: dict[str, InjectionFinding],
) -> str:
    text = _strip_cued_encoded_payloads(text, findings)

    def replace_generic(match: re.Match[str]) -> str:
        candidate = match.group(0)
        if not _decoded_instruction(candidate):
            return candidate
        _add_finding(findings, "encoded_instruction", "high")
        return " "

    return _GENERIC_BASE64_RE.sub(replace_generic, text)


def _strip_suspicious_sentences(text: str, pattern: re.Pattern[str]) -> str:
    intervals: list[tuple[int, int]] = []
    for match in pattern.finditer(text):
        start = match.start()
        while start > 0 and text[start - 1] not in ".!?\n":
            start -= 1
        end = match.end()
        while end < len(text) and text[end] not in ".!?\n":
            end += 1
        if end < len(text):
            end += 1
        intervals.append((start, end))
    for start, end in reversed(intervals):
        text = text[:start] + " " + text[end:]
    return text


def _strip_tool_json(
    text: str,
    findings: dict[str, InjectionFinding],
) -> str:
    intervals: list[tuple[int, int]] = []
    stack: list[int] = []
    in_string: str | None = None
    escaped = False
    for index, character in enumerate(text):
        if in_string is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == in_string:
                in_string = None
            continue
        if character in {'"', "'"}:
            in_string = character
        elif character == "{":
            stack.append(index)
        elif character == "}" and stack:
            start = stack.pop()
            if not stack:
                candidate = text[start : index + 1]
                if _TOOL_JSON_MARKER_RE.search(candidate):
                    intervals.append((start, index + 1))
    if stack:
        start = stack[0]
        candidate = text[start:]
        if _TOOL_JSON_MARKER_RE.search(candidate):
            intervals.append((start, len(text)))
    if intervals:
        _add_finding(findings, "tool_call_json", "high")
        for start, end in reversed(intervals):
            text = text[:start] + " " + text[end:]
    if _TOOL_JSON_MARKER_RE.search(text):
        _add_finding(findings, "tool_call_json", "high")
        text = _strip_suspicious_sentences(text, _TOOL_JSON_MARKER_RE)
    return text


def _sanitize(raw_text: str) -> tuple[str, tuple[InjectionFinding, ...]]:
    findings: dict[str, InjectionFinding] = {}
    text = _canonicalize_active_content(raw_text, findings)

    text = _strip_tool_json(text, findings)
    if _DIRECT_INSTRUCTION_RE.search(text):
        _add_finding(findings, "direct_instruction", "high")
        text = _strip_suspicious_sentences(text, _DIRECT_INSTRUCTION_RE)
    if _INDIRECT_TOOL_RE.search(text):
        _add_finding(findings, "indirect_tool_instruction", "high")
        text = _strip_suspicious_sentences(text, _INDIRECT_TOOL_RE)

    text = _strip_encoded_payloads(text, findings)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, tuple(findings.values())


class UntrustedContentGateway:
    """Normalize external text and persist metadata-only ingest evidence."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def ingest(
        self,
        *,
        source_kind: SourceKind,
        source_id: str,
        raw_text: str,
        source_name: str | None = None,
        source_url: str | None = None,
        published_at: datetime | None = None,
        received_at: datetime | None = None,
    ) -> UntrustedContent:
        return self._ingest_one(
            source_kind=source_kind,
            source_id=source_id,
            raw_text=raw_text,
            source_name=source_name,
            source_url=source_url,
            published_at=published_at,
            received_at=received_at,
        )

    def ingest_many(
        self,
        items: Sequence[Mapping[str, Any]],
    ) -> tuple[UntrustedContent, ...]:
        if len(items) > MAX_ITEMS_PER_REQUEST:
            raise UntrustedContentError("too_many_items")
        return tuple(self._ingest_mapping(item) for item in items)

    def _ingest_mapping(self, item: Mapping[str, Any]) -> UntrustedContent:
        allowed = {
            "source_kind",
            "source_id",
            "raw_text",
            "source_name",
            "source_url",
            "published_at",
            "received_at",
        }
        if set(item) - allowed:
            raise UntrustedContentError("invalid_ingest_item")
        try:
            return self._ingest_one(**item)
        except TypeError as exc:
            raise UntrustedContentError("invalid_ingest_item") from None

    def _ingest_one(
        self,
        *,
        source_kind: SourceKind,
        source_id: str,
        raw_text: str,
        source_name: str | None = None,
        source_url: str | None = None,
        published_at: datetime | None = None,
        received_at: datetime | None = None,
    ) -> UntrustedContent:
        if source_kind not in {"alpaca_news", "filing", "search", "pasted"}:
            raise UntrustedContentError("invalid_source_kind")
        if (
            not isinstance(source_id, str)
            or not source_id
            or len(source_id) > _MAX_SOURCE_ID_CHARACTERS
        ):
            raise UntrustedContentError("invalid_source_id")
        if not isinstance(raw_text, str):
            raise UntrustedContentError("invalid_content")
        if source_name is not None and (
            not isinstance(source_name, str)
            or not source_name
            or len(source_name) > _MAX_SOURCE_NAME_CHARACTERS
        ):
            raise UntrustedContentError("invalid_source_name")

        source_hash = _stable_source_hash(source_kind, source_id)
        try:
            raw_bytes = raw_text.encode("utf-8")
        except UnicodeEncodeError:
            self._persist_rejection(
                source_hash,
                _sha256(raw_text.encode("utf-8", errors="replace")),
                0,
                "invalid_utf8",
                received_at,
            )
            raise UntrustedContentError("invalid_utf8") from None
        if len(raw_bytes) > MAX_CONTENT_BYTES:
            self._persist_rejection(
                source_hash,
                _sha256(raw_bytes),
                len(raw_bytes),
                "content_too_large",
                received_at,
            )
            raise UntrustedContentError("content_too_large")

        observed_at = received_at or self._clock()
        self._validate_timestamp(observed_at, "invalid_received_at")
        if published_at is not None and (
            not isinstance(published_at, datetime)
            or published_at.tzinfo is None
            or published_at.utcoffset() is None
        ):
            self._persist_rejection(
                source_hash,
                _sha256(raw_bytes),
                len(raw_bytes),
                "invalid_published_at",
                observed_at,
            )
            raise UntrustedContentError("invalid_published_at")

        try:
            normalized_text, findings = _sanitize(raw_text)
        except UntrustedContentError as exc:
            self._persist_rejection(
                source_hash,
                _sha256(raw_bytes),
                len(raw_bytes),
                exc.code,
                observed_at,
            )
            raise
        normalized_bytes = normalized_text.encode("utf-8")
        if (
            len(normalized_bytes) > MAX_CONTENT_BYTES
            or len(normalized_text) > MAX_NORMALIZED_CHARACTERS
        ):
            self._persist_rejection(
                source_hash,
                _sha256(normalized_bytes),
                len(normalized_bytes),
                "normalized_content_too_large",
                observed_at,
            )
            raise UntrustedContentError("normalized_content_too_large")
        content_hash = _sha256(normalized_bytes)

        try:
            content = UntrustedContent(
                source_kind=source_kind,
                source_id=source_id,
                source_name=source_name,
                source_url=source_url,
                published_at=published_at,
                received_at=observed_at,
                normalized_text=normalized_text,
                content_sha256=content_hash,
                findings=findings,
            )
        except ValidationError:
            code = "invalid_source_url" if source_url is not None else "invalid_metadata"
            self._persist_rejection(
                source_hash,
                content_hash,
                len(normalized_bytes),
                code,
                observed_at,
                additional_flag_codes=[
                    finding.code for finding in findings
                ],
            )
            raise UntrustedContentError(code) from None

        self._persist_event(
            source_hash=source_hash,
            content_hash=content_hash,
            byte_length=len(normalized_bytes),
            flag_codes=[finding.code for finding in findings],
            state="received",
            received_at=observed_at,
        )
        return content

    @staticmethod
    def _validate_timestamp(value: datetime, code: str) -> None:
        if not isinstance(value, datetime) or (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise UntrustedContentError(code)

    def _persist_rejection(
        self,
        source_hash: str,
        content_hash: str,
        byte_length: int,
        code: str,
        received_at: datetime | None,
        *,
        additional_flag_codes: Sequence[str] = (),
    ) -> None:
        observed_at = received_at or self._clock()
        self._validate_timestamp(observed_at, "invalid_received_at")
        self._persist_event(
            source_hash=source_hash,
            content_hash=content_hash,
            byte_length=byte_length,
            flag_codes=[code, *additional_flag_codes],
            state="rejected",
            received_at=observed_at,
        )

    def _persist_event(
        self,
        *,
        source_hash: str,
        content_hash: str,
        byte_length: int,
        flag_codes: Sequence[str],
        state: Literal["received", "rejected"],
        received_at: datetime,
    ) -> None:
        incoming = sqlite_insert(UntrustedIngestEvent).values(
            source_hash=source_hash,
            content_hash=content_hash,
            byte_length=byte_length,
            flags_json=json.dumps(
                sorted(set(flag_codes)),
                separators=(",", ":"),
            ),
            state=state,
            received_at=received_at.astimezone(timezone.utc),
        )
        merged_flags = sql_text(
            "("
            "SELECT json_group_array(code) "
            "FROM ("
            "SELECT value AS code "
            "FROM json_each(untrusted_ingest_events.flags_json) "
            "UNION "
            "SELECT value AS code "
            "FROM json_each(excluded.flags_json) "
            "ORDER BY code"
            "))"
        )
        statement = incoming.on_conflict_do_update(
            index_elements=["source_hash", "content_hash"],
            set_={
                "byte_length": func.max(
                    UntrustedIngestEvent.byte_length,
                    incoming.excluded.byte_length,
                ),
                "flags_json": merged_flags,
                "state": case(
                    (
                        or_(
                            UntrustedIngestEvent.state == "rejected",
                            incoming.excluded.state == "rejected",
                        ),
                        "rejected",
                    ),
                    else_="received",
                ),
                "received_at": func.min(
                    UntrustedIngestEvent.received_at,
                    incoming.excluded.received_at,
                ),
            },
        )
        with self._session_factory() as session:
            session.execute(statement)
            session.commit()


__all__ = [
    "InjectionFinding",
    "MAX_CONTENT_BYTES",
    "MAX_ITEMS_PER_REQUEST",
    "UntrustedContent",
    "UntrustedContentError",
    "UntrustedContentGateway",
    "UntrustedFact",
    "UntrustedSummary",
]
