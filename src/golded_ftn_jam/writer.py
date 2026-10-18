"""JAM mutation sessions. GoldED concurrency is deliberately not enabled."""

from __future__ import annotations

import os
import re
import stat
import struct
import time
import zlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from os import PathLike
from pathlib import Path
from typing import Literal, NoReturn, Self, cast
from uuid import UUID

from golded_ftn import (
    UNSET,
    ConflictError,
    ControlLine,
    FtnAddress,
    MessageIdentity,
    MessagePatch,
    OutgoingMessage,
    ParsedMessage,
    ParserException,
    PhysicalBaseIdentity,
    PreparedMutation,
    ReaderOptions,
    RecoveryError,
    RecoveryOperationKind,
    RevisionToken,
    RollbackError,
    SessionMessage,
    WriterError,
    WriteResult,
    WriterOptions,
    parse_message,
)
from golded_ftn._recovery import (
    BaseRecoverableSession,
    FileMutationPlan,
    RecoveryStore,
)
from golded_ftn._writer_io import IO, Transaction, locks, raw_revision, strict_encode

from .reader import _DELETED, _HEADER, _INDEX, _SUBFIELD, JamReader, _file

_SIDECARS = (".JHR", ".JDT", ".JDX", ".JLR")


@dataclass(frozen=True, slots=True, kw_only=True)
class JamWriterOptions(WriterOptions):
    extension_case: Literal["upper", "lower"] = "upper"

    def __post_init__(self) -> None:
        WriterOptions.__post_init__(self)
        if self.extension_case not in {"upper", "lower"}:
            raise ValueError("extension_case must be 'upper' or 'lower'")


def _sidecar_suffixes(options: WriterOptions | None) -> tuple[str, ...]:
    case = options.extension_case if isinstance(options, JamWriterOptions) else "upper"
    if case == "lower":
        return tuple(suffix.lower() for suffix in _SIDECARS)
    return _SIDECARS


def _crc(raw: bytes) -> int:
    # GoldED seeds with FFFFFFFF and does not apply the final complement.
    return zlib.crc32(raw.lower()) ^ 0xFFFFFFFF


def _u32(value: int) -> int:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("JAM value exceeds unsigned 32-bit range")
    return value


_TZUTC_RE = re.compile(r"([+-]?)([0-9]{2})([0-9]{2})")
_TZUTC_MAGNITUDE_LIMIT = 24 * 60
_ROLES = ("jhr", "jdt", "jdx")
_AWARE_WITHOUT_TZUTC = "Aware posted_at requires a TZUTC control"
_AWARE_TZUTC_MISMATCH = "Aware posted_at offset does not match TZUTC"
_RECOVERY_INPUT_ERROR = "JAM recovery input is not representable"


def _parse_tzutc(value: str | bytes | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            raw = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    elif isinstance(value, str):
        raw = value
    else:
        return None
    match = _TZUTC_RE.fullmatch(raw.strip())
    if match is None or int(match.group(3)) > 59:
        return None
    total = int(match.group(2)) * 60 + int(match.group(3))
    if total >= _TZUTC_MAGNITUDE_LIMIT:
        return None
    return -total if match.group(1) == "-" else total


def _tzutc_minutes_from_controls(controls: Sequence[ControlLine]) -> int | None:
    minutes: int | None = None
    for control in controls:
        if control.name.upper() == "TZUTC":
            parsed = _parse_tzutc(control.value)
            if parsed is not None:
                minutes = parsed
    return minutes


def _tzutc_minutes_from_fields(fields: Sequence[tuple[int, int, bytes]]) -> int | None:
    minutes: int | None = None
    for lo, hi, payload in fields:
        if lo == 2004 and hi == 0:
            parsed = _parse_tzutc(payload)
            if parsed is not None:
                minutes = parsed
    return minutes


def _is_aware(value: datetime) -> bool:
    return value.utcoffset() is not None


def _date(value: datetime | None, tzutc_minutes: int | None = None) -> int:
    if value is None:
        return 0
    if _is_aware(value):
        if tzutc_minutes is None:
            raise ValueError(_AWARE_WITHOUT_TZUTC)
        current_offset = value.utcoffset()
        if (
            current_offset is None
            or current_offset.total_seconds() != tzutc_minutes * 60
        ):
            raise ValueError(_AWARE_TZUTC_MISMATCH)
        return _u32(int(value.timestamp()))
    if tzutc_minutes is None:
        return _u32(int(value.replace(tzinfo=UTC).timestamp()))
    zone = timezone(timedelta(minutes=tzutc_minutes))
    return _u32(int(value.replace(tzinfo=zone).timestamp()))


def _wall_time_from_epoch(epoch: int, tzutc_minutes: int | None) -> datetime | None:
    if not epoch:
        return None
    posted = datetime(1970, 1, 1) + timedelta(seconds=epoch)
    if tzutc_minutes is None:
        return posted
    return posted + timedelta(minutes=tzutc_minutes)


def _adapt_posted_at(message: ParsedMessage) -> ParsedMessage:
    if message.posted_at is None or message.control_lines is None:
        return message
    minutes = _tzutc_minutes_from_controls(message.control_lines.kludges)
    if minutes is None:
        return message
    return replace(message, posted_at=message.posted_at + timedelta(minutes=minutes))


def _field(lo: int, raw: bytes, hi: int = 0) -> bytes:
    limit = 100 if lo <= 6 else 40 if lo == 7 else 255 if lo == 2000 else None
    if limit is not None and len(raw) > limit:
        raise ValueError(f"JAM subfield {lo} exceeds {limit} bytes")
    if any(byte in raw for byte in (b"\0", b"\r", b"\n", b"\x01")):
        raise ValueError(
            "JAM metadata must be a single line without control separators"
        )
    return _SUBFIELD.pack(lo, hi, len(raw)) + raw


def _raw_fields(raw: bytes) -> list[tuple[int, int, bytes]]:
    result = []
    offset = 0
    while offset < len(raw):
        lo, hi, size = _SUBFIELD.unpack_from(raw, offset)
        result.append((lo, hi, raw[offset + 8 : offset + 8 + size]))
        offset += 8 + size
    return result


def _address(value: object) -> None:
    if isinstance(value, FtnAddress):
        for part in (value.zone, value.net, value.node, value.point or 0):
            if not 0 <= part <= 65535:
                raise ValueError("FTN address component exceeds unsigned 16-bit range")
        if value.domain is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", value.domain
        ):
            raise ValueError("Invalid FTN address domain")


def _metadata(message: OutgoingMessage, charset: str) -> dict[int, list[bytes]]:
    _address(message.from_address)
    _address(message.to_address)
    result: dict[int, list[bytes]] = {}
    for lo, value in (
        (0, message.from_address),
        (1, message.to_address),
        (2, message.from_name),
        (3, message.to_name),
        (6, message.subject),
        (4, message.external_id),
    ):
        if value is not None:
            result[lo] = [str(value).encode(charset, errors="strict")]
    names = {"MSGID": 4, "REPLY": 5, "PID": 7, "FLAGS": 2003, "TZUTC": 2004}
    for control in message.control_lines:
        if re.fullmatch(r"[A-Za-z0-9_-]+", control.name) is None:
            raise ValueError("Invalid control name")
        lo = names.get(control.name.upper(), 2000)
        payload = control.value if lo != 2000 else f"{control.name}: {control.value}"
        encoded = payload.encode(charset, errors="strict")
        if lo in (4, 5) and lo in result and encoded not in result[lo]:
            raise ValueError("Conflicting JAM MSGID or REPLY controls")
        if encoded not in result.setdefault(lo, []):
            result[lo].append(encoded)
    for lo, values in ((2001, message.routing_seen_by), (2002, message.routing_path)):
        if values:
            result[lo] = [value.encode(charset, errors="strict") for value in values]
    return result


@dataclass(frozen=True, slots=True)
class _JamMutation:
    writes: tuple[tuple[str, int, bytes], ...]
    result: WriteResult


def _jam_paths(base: Path) -> tuple[Path, Path, Path]:
    return _file(base, ".JHR"), _file(base, ".JDT"), _file(base, ".JDX")


def _raise_recovery_os(code: str = "unsafe_recovery_path") -> NoReturn:
    raise RecoveryError(code=code) from None


def _raise_recovery_input() -> NoReturn:
    raise ValueError(_RECOVERY_INPUT_ERROR) from None


def _require_not_symlink(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        _raise_recovery_os()
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RecoveryError(code="unsafe_recovery_path")


def _require_fd_matches_path(fd: int, path: Path) -> None:
    try:
        fd_stat = os.fstat(fd)
        path_stat = os.lstat(path)
    except OSError:
        _raise_recovery_os()
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise RecoveryError(code="unsafe_recovery_path")
    if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        raise RecoveryError(code="unsafe_recovery_path")


def _resolve_recovery_base(
    path: str | PathLike[str],
) -> tuple[Path, tuple[Path, Path, Path], tuple[str, str, str], Path]:
    raw = Path(path)
    try:
        base_root = Path(os.path.realpath(raw.parent))
    except OSError:
        _raise_recovery_os()
    base = base_root / raw.name
    try:
        paths = _jam_paths(base)
    except OSError:
        _raise_recovery_os()
    for candidate in paths:
        _require_not_symlink(candidate)
    rels = tuple(os.path.relpath(candidate, base_root) for candidate in paths)
    for rel in rels:
        parsed = Path(rel)
        if parsed.is_absolute() or ".." in parsed.parts:
            raise RecoveryError(code="unsafe_recovery_path")
    return base, paths, cast(tuple[str, str, str], rels), base_root


@contextmanager
def _acquire_recovery_lock(jhr: Path, timeout: float) -> Iterator[int]:
    cm = locks.acquire(jhr, timeout=timeout)
    try:
        fd = cm.__enter__()
    except OSError:
        _raise_recovery_os()
    try:
        yield fd
    except BaseException as error:
        if cm.__exit__(type(error), error, error.__traceback__):
            return
        raise
    else:
        cm.__exit__(None, None, None)


def _raw_from_fds(io: IO, fds: tuple[int, int, int]) -> tuple[bytes, bytes, bytes]:
    raw = tuple(io.read(fd, 0, os.fstat(fd).st_size) for fd in fds)
    return cast(tuple[bytes, bytes, bytes], raw)


def _fd_writes(
    fds: tuple[int, int, int], writes: Sequence[tuple[str, int, bytes]]
) -> list[tuple[int, int, bytes]]:
    role_fd = {"jhr": fds[0], "jdt": fds[1], "jdx": fds[2]}
    return [(role_fd[role], offset, data) for role, offset, data in writes]


def _apply_mutation(
    io: IO, fds: tuple[int, int, int], mutation: _JamMutation, parent: Path
) -> WriteResult:
    for fd, offset, data in _fd_writes(fds, mutation.writes):
        io.write(fd, offset, data)
    for fd in fds:
        io.flush(fd)
    if os.name != "nt":
        dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    return mutation.result


def _file_plans(
    rels: tuple[str, str, str],
    fds: tuple[int, int, int],
    raw: tuple[bytes, bytes, bytes],
    writes: Sequence[tuple[str, int, bytes]],
) -> list[FileMutationPlan]:
    grouped: dict[str, list[tuple[int, bytes]]] = {role: [] for role in _ROLES}
    for role, offset, data in writes:
        grouped[role].append((offset, data))
    plans: list[FileMutationPlan] = []
    for role, rel, fd, pre in zip(_ROLES, rels, fds, raw, strict=True):
        items = grouped[role]
        ranges = tuple((offset, offset + len(data)) for offset, data in items)
        post_len = len(pre)
        for offset, data in items:
            post_len = max(post_len, offset + len(data))
        plans.append(
            FileMutationPlan(
                role=role,
                rel_path=rel,
                fd=fd,
                planned_write_ranges=ranges,
                post_min_len=post_len,
                post_max_len=post_len,
            )
        )
    return plans


def _validate_base(
    raw: tuple[bytes, bytes, bytes],
    paths: tuple[Path, Path, Path],
    options: WriterOptions,
) -> None:
    headers, texts, index = raw
    if len(headers) < 1024 or headers[:4] != b"JAM\0" or len(index) % 8:
        raise ParserException("Invalid JAM base structure")
    base = int.from_bytes(headers[20:24], "little")
    if base < 1 or base + len(index) // 8 - 1 > 0xFFFFFFFF:
        raise ParserException("Invalid JAM base message number")
    seen = set()
    active = 0
    for slot in range(len(index) // 8):
        crc, offset = _INDEX.unpack_from(index, slot * 8)
        if crc == offset == 0xFFFFFFFF:
            continue
        if offset < 1024 or offset in seen:
            raise ParserException("Invalid or reused JAM header offset")
        seen.add(offset)
        message = JamReader._message(
            headers,
            texts,
            paths[0],
            paths[1],
            offset,
            base + slot,
            ReaderOptions(fallback_charset=options.target_charset),
        )
        active += message is not None
    if int.from_bytes(headers[12:16], "little") != active:
        raise ParserException("JAM ActiveMsgs disagrees with live index records")


def _target(
    raw: tuple[bytes, bytes, bytes], msgno: int, base: Path
) -> tuple[int, list[int], bytes, bytes, RevisionToken]:
    headers, texts, index = raw
    area_base = int.from_bytes(headers[20:24], "little")
    slot = msgno - area_base
    if slot < 0 or slot >= len(index) // 8:
        raise ConflictError("JAM message is missing")
    crc, offset = _INDEX.unpack_from(index, slot * 8)
    if crc == offset == 0xFFFFFFFF:
        raise ConflictError("JAM message is deleted")
    fixed = _HEADER.unpack_from(headers, offset)
    words = list(fixed[3:])
    if words[11] & _DELETED:
        raise ConflictError("JAM message is deleted")
    metadata = headers[offset + 76 : offset + 76 + words[0]]
    body = texts[words[13] : words[13] + words[14]]
    identity = MessageIdentity(format="jam", base=str(base), msgno=msgno)
    revision = raw_revision(
        identity,
        (slot * 8, offset, words[13]),
        headers[offset : offset + 76],
        metadata,
        body,
    )
    return offset, words, metadata, body, revision


def _compare_revision(
    identity: MessageIdentity, expected: RevisionToken, actual: RevisionToken
) -> None:
    if identity != actual.identity or expected != actual:
        raise ConflictError("JAM target revision changed or belongs to another base")


def _info(raw: bytes, delta: int) -> bytes:
    result = bytearray(raw[:1024])
    mod, active = struct.unpack_from("<II", result, 8)
    struct.pack_into("<II", result, 8, (mod + 1) & 0xFFFFFFFF, _u32(active + delta))
    return bytes(result)


def _session_message(
    raw: tuple[bytes, bytes, bytes],
    paths: tuple[Path, Path, Path],
    base: Path,
    options: WriterOptions,
    msgno: int,
) -> SessionMessage:
    offset, _words, _metadata, _body, revision = _target(raw, msgno, base)
    message = JamReader._message(
        raw[0],
        raw[1],
        paths[0],
        paths[1],
        offset,
        msgno,
        ReaderOptions(fallback_charset=options.target_charset),
    )
    assert message is not None
    return SessionMessage(
        message=_adapt_posted_at(message),
        identity=revision.identity,
        revision=revision,
    )


def _plan_append(
    raw: tuple[bytes, bytes, bytes],
    message: OutgoingMessage,
    options: WriterOptions,
    base: Path,
    paths: tuple[Path, Path, Path],
) -> _JamMutation:
    if message.reply_list:
        raise ValueError("JAM has three reply links, not an arbitrary reply list")
    strict_encode("", options, parse_message(message.body_text).kludges)
    if "\0" in message.body_text:
        raise ValueError("NUL is not representable in JAM text")
    metadata = _metadata(message, options.target_charset)
    body = strict_encode(
        message.body_text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r"),
        options,
        message.control_lines,
    )
    subfields = b"".join(
        _field(lo, value) for lo, values in metadata.items() for value in values
    )
    # Format-owned JAM Attribute bits; packet attributes_raw is never copied here.
    attrs = _u32(message.attributes_raw or 0)
    if attrs & (_DELETED | 0x00380000):
        raise ValueError("Unsupported JAM attributes")
    msgno = _u32(int.from_bytes(raw[0][20:24], "little") + len(raw[2]) // 8)
    tzutc_minutes = _tzutc_minutes_from_controls(message.control_lines)
    words = [
        len(subfields),
        0,
        _crc(metadata.get(4, [b""])[0]),
        _crc(metadata.get(5, [b""])[0]),
        _u32(message.reply_to_msgno or 0),
        _u32(message.reply1st_msgno or 0),
        _u32(message.reply_next_msgno or 0),
        _date(message.posted_at, tzutc_minutes),
        0,
        0,
        msgno,
        attrs,
        0,
        _u32(len(raw[1])),
        _u32(len(body)),
        0xFFFFFFFF,
        0,
    ]
    header = _HEADER.pack(b"JAM\0", 1, 0, *words) + subfields
    offset = _u32(len(raw[0]))
    index = _INDEX.pack(_crc(metadata[3][0]), offset)
    JamReader._message(
        raw[0] + header,
        raw[1] + body,
        paths[0],
        paths[1],
        offset,
        msgno,
        ReaderOptions(fallback_charset=options.target_charset),
    )
    identity = MessageIdentity(format="jam", base=str(base), msgno=msgno)
    return _JamMutation(
        writes=(
            ("jdt", len(raw[1]), body),
            ("jhr", offset, header),
            ("jdx", len(raw[2]), index),
            ("jhr", 0, _info(raw[0], 1)),
        ),
        result=WriteResult(
            identity=identity,
            revision=raw_revision(
                identity, (len(raw[2]), offset, words[13]), header, body
            ),
        ),
    )


def _plan_update(
    raw: tuple[bytes, bytes, bytes],
    identity: MessageIdentity,
    patch: MessagePatch,
    expected_revision: RevisionToken,
    options: WriterOptions,
    base: Path,
    paths: tuple[Path, Path, Path],
) -> _JamMutation:
    if patch.provenance is not UNSET or patch.reply_list is not UNSET:
        raise ValueError("JAM cannot patch provenance or arbitrary reply lists")
    offset, words, metadata, body, actual = _target(raw, identity.msgno, base)
    _compare_revision(identity, expected_revision, actual)
    existing = _raw_fields(metadata)
    original_text = (
        body.decode(options.target_charset, errors="strict")
        if any(
            getattr(patch, name) is not UNSET
            for name in (
                "body_text",
                "control_lines",
                "external_id",
                "routing_seen_by",
                "routing_path",
            )
        )
        else ""
    )
    replacements: dict[int, list[bytes]] = {}
    serialized = False
    for name, lo in (
        ("from_name", 2),
        ("to_name", 3),
        ("subject", 6),
        ("external_id", 4),
        ("from_address", 0),
        ("to_address", 1),
    ):
        value = getattr(patch, name)
        if value is UNSET:
            continue
        if value is None and name in {"from_name", "to_name", "subject"}:
            raise ValueError(f"{name} cannot be cleared with None")
        _address(value)
        replacements[lo] = (
            []
            if value is None
            else [str(value).encode(options.target_charset, errors="strict")]
        )
        serialized = True
    if patch.control_lines is not UNSET:
        controls = cast(tuple[ControlLine, ...] | None, patch.control_lines) or ()
        strict_encode("", options, controls)
        for lo in (5, 7, 2000, 2003, 2004):
            replacements[lo] = []
        for control in controls:
            if re.fullmatch(r"[A-Za-z0-9_-]+", control.name) is None:
                raise ValueError("Invalid control name")
            lo = {
                "MSGID": 4,
                "REPLY": 5,
                "PID": 7,
                "FLAGS": 2003,
                "TZUTC": 2004,
            }.get(control.name.upper(), 2000)
            text = control.value if lo != 2000 else f"{control.name}: {control.value}"
            encoded = text.encode(options.target_charset, errors="strict")
            if control.name.upper() in {"FMPT", "TOPT", "INTL", "PATH"}:
                name = control.name.upper()
                retained_values = [
                    parts[1].strip()
                    for low, high, payload in existing
                    if low == 2000 and high == 0
                    for parts in [
                        re.split(
                            r"[:\s]",
                            payload.decode(options.target_charset),
                            maxsplit=1,
                        )
                    ]
                    if len(parts) == 2 and parts[0].upper() == name
                ]
                retained_values.extend(
                    parts[1].strip()
                    for line in original_text.replace("\r", "\n").split("\n")
                    if line.startswith("\x01")
                    for parts in [re.split(r"[:\s]", line[1:], maxsplit=1)]
                    if len(parts) == 2 and parts[0].upper() == name
                )
                if name == "PATH":
                    retained_values.extend(
                        payload.decode(options.target_charset)
                        for low, high, payload in existing
                        if low == 2002 and high == 0
                    )
                if control.value.strip() not in retained_values:
                    raise ValueError(
                        f"Control {name} conflicts with omitted structured field"
                    )
                continue
            if lo == 4:
                current_id = next(
                    (
                        payload
                        for low, high, payload in existing
                        if low == 4 and high == 0
                    ),
                    None,
                )
                if current_id is None:
                    inline_id = parse_message(original_text).msgid
                    current_id = (
                        inline_id.encode(options.target_charset) if inline_id else None
                    )
                expected_id = (
                    current_id
                    if patch.external_id is UNSET
                    else (
                        None
                        if patch.external_id is None
                        else cast(str, patch.external_id).encode(options.target_charset)
                    )
                )
                if encoded != expected_id:
                    raise ValueError(
                        "MSGID conflicts with omitted or explicit external_id"
                    )
                if patch.external_id is UNSET:
                    continue
                replacements.setdefault(4, [])
            if lo in (4, 5) and replacements[lo] and encoded not in replacements[lo]:
                raise ValueError("Conflicting JAM control values")
            replacements.setdefault(lo, []).append(encoded)
        serialized = True
    for name, lo in (("routing_seen_by", 2001), ("routing_path", 2002)):
        value = getattr(patch, name)
        if value is not UNSET:
            replacements[lo] = [
                text.encode(options.target_charset, errors="strict")
                for text in value or ()
            ]
            serialized = True
    if patch.body_text is not UNSET:
        if patch.body_text is None:
            raise ValueError("body_text cannot be cleared with None")
        if "\0" in cast(str, patch.body_text):
            raise ValueError("NUL is not representable in JAM text")
        body = strict_encode(
            cast(str, patch.body_text)
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", "\r"),
            options,
        )
        serialized = True
    text_metadata_changed = any(
        getattr(patch, name) is not UNSET
        for name in (
            "body_text",
            "control_lines",
            "external_id",
            "routing_seen_by",
            "routing_path",
        )
    )
    if text_metadata_changed:
        preserved = []
        retained_body = []
        for line in original_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            control_name = (
                re.split(r"[:\s]", line[1:], maxsplit=1)[0].upper()
                if line.startswith("\x01")
                else None
            )
            routing_name = (
                "routing_seen_by"
                if line.upper().startswith("SEEN-BY:")
                else "routing_path"
                if control_name == "PATH" or line.upper().startswith("PATH:")
                else None
            )
            is_metadata = control_name is not None or routing_name is not None
            remove = (
                (routing_name is not None and getattr(patch, routing_name) is not UNSET)
                or (control_name == "MSGID" and patch.external_id is not UNSET)
                or (
                    control_name is not None
                    and control_name not in {"MSGID", "FMPT", "TOPT", "INTL", "PATH"}
                    and patch.control_lines is not UNSET
                )
            )
            if not remove:
                retained_body.append(line)
                if is_metadata:
                    preserved.append(line)
        text = (
            "\n".join(preserved + [cast(str, patch.body_text)])
            if patch.body_text is not UNSET
            else "\n".join(retained_body)
        )
        body = strict_encode(text.replace("\n", "\r"), options)
    if serialized:
        retained_controls = tuple(
            ControlLine(name=parts[0], value=parts[1], raw="")
            for lo, hi, payload in existing
            if hi == 0 and lo == 2000 and lo not in replacements
            for text in [payload.decode(options.target_charset, errors="strict")]
            for parts in [text.split(":", 1)]
            if len(parts) == 2
        )
        strict_encode("", options, retained_controls)
        body_controls = parse_message(
            body.decode(options.target_charset, errors="strict")
        )
        strict_encode("", options, body_controls.kludges)
    metadata = b"".join(
        _SUBFIELD.pack(lo, hi, len(payload)) + payload
        for lo, hi, payload in existing
        if hi != 0
        or lo not in replacements
        or (
            lo == 2000
            and re.split(rb"[:\s]", payload, maxsplit=1)[0].upper()
            in {b"FMPT", b"TOPT", b"INTL"}
        )
    ) + b"".join(
        _field(lo, payload) for lo, values in replacements.items() for payload in values
    )
    current = _raw_fields(metadata)
    for lo, word in ((4, 2), (5, 3)):
        if lo in replacements:
            words[word] = _crc(replacements[lo][0] if replacements[lo] else b"")
    for name, word in (
        ("reply_to_msgno", 4),
        ("reply1st_msgno", 5),
        ("reply_next_msgno", 6),
    ):
        value = getattr(patch, name)
        if value is not UNSET:
            words[word] = _u32(value or 0)
    new_tzutc = _tzutc_minutes_from_fields(current)
    if patch.posted_at is not UNSET:
        words[7] = _date(cast(datetime | None, patch.posted_at), new_tzutc)
    elif 2004 in replacements:
        words[7] = _date(
            _wall_time_from_epoch(words[7], _tzutc_minutes_from_fields(existing)),
            new_tzutc,
        )
    if patch.attributes_raw is not UNSET:
        if patch.attributes_raw is None:
            raise ValueError("attributes_raw cannot be cleared with None")
        words[11] = _u32(cast(int, patch.attributes_raw))
        if words[11] & (_DELETED | 0x00380000):
            raise ValueError("Unsupported JAM attributes")
    words[0] = _u32(len(metadata))
    original = _HEADER.unpack_from(raw[0], offset)
    planned: list[tuple[str, int, bytes]] = []
    if serialized:
        words[13], words[14] = _u32(len(raw[1])), _u32(len(body))
        offset = _u32(len(raw[0]))
        planned.append(("jdt", words[13], body))
    header = _HEADER.pack(original[0], original[1], original[2], *words)
    planned.append(("jhr", offset, header + metadata if serialized else header))
    recipient = next(
        (payload for lo, hi, payload in current if lo == 3 and hi == 0), b""
    )
    planned.extend(
        [
            (
                "jdx",
                actual.location[0],
                _INDEX.pack(_crc(recipient), offset)
                if 3 in replacements
                else raw[2][actual.location[0] : actual.location[0] + 4]
                + struct.pack("<I", offset),
            ),
            ("jhr", 0, _info(raw[0], 0)),
        ]
    )
    proposed_headers = (
        raw[0] + header + metadata
        if serialized
        else (raw[0][:offset] + header + raw[0][offset + 76 :])
    )
    proposed_texts = raw[1] + body if serialized else raw[1]
    JamReader._message(
        proposed_headers,
        proposed_texts,
        paths[0],
        paths[1],
        offset,
        identity.msgno,
        ReaderOptions(fallback_charset=options.target_charset),
    )
    return _JamMutation(
        writes=tuple(planned),
        result=WriteResult(
            identity=identity,
            revision=raw_revision(
                identity,
                (actual.location[0], offset, words[13]),
                header,
                metadata,
                body,
            ),
        ),
    )


class JamWriter:
    """Create areas and open operation-scoped mutation sessions."""

    def create(
        self,
        path: str | PathLike[str],
        options: WriterOptions | None = None,
    ) -> None:
        base = Path(path)
        base.parent.mkdir(parents=True, exist_ok=True)
        paths = [Path(str(base) + suffix) for suffix in _sidecar_suffixes(options)]
        if any(p.exists() for p in paths) or any(
            p.stem == base.name and p.suffix.upper() in set(_SIDECARS)
            for p in base.parent.iterdir()
        ):
            raise FileExistsError(str(base))
        header = bytearray(1024)
        struct.pack_into(
            "<4s5I", header, 0, b"JAM\0", int(time.time()), 0, 0, 0xFFFFFFFF, 1
        )
        created = []
        try:
            for p, data in zip(paths, (header, b"", b"", b""), strict=True):
                with p.open("xb") as stream:
                    created.append(p)
                    stream.write(bytes(data))
                    stream.flush()
                    os.fsync(stream.fileno())
        except BaseException:
            for p in created:
                p.unlink()
            raise

    def open(
        self, path: str | PathLike[str], options: WriterOptions | None = None
    ) -> JamSession:
        return JamSession(Path(path).resolve(), options or WriterOptions())

    @contextmanager
    def recovery_session(
        self,
        path: str | PathLike[str],
        *,
        recovery_root: str | PathLike[str],
        options: WriterOptions | None = None,
    ) -> Iterator[JamRecoverableSession]:
        options = options or WriterOptions()
        base, paths, rels, base_root = _resolve_recovery_base(path)
        jhr, jdt, jdx = paths
        with _acquire_recovery_lock(jhr, options.lock_timeout) as header_fd:
            try:
                lock_stat = os.fstat(header_fd)
            except OSError:
                _raise_recovery_os()
            _require_fd_matches_path(header_fd, jhr)
            binary_flags = (
                os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                text_fd = os.open(jdt, binary_flags)
            except OSError:
                _raise_recovery_os()
            try:
                try:
                    index_fd = os.open(jdx, binary_flags)
                except OSError:
                    _raise_recovery_os()
                try:
                    _require_fd_matches_path(text_fd, jdt)
                    _require_fd_matches_path(index_fd, jdx)
                    fds = (header_fd, text_fd, index_fd)
                    physical_base = PhysicalBaseIdentity(
                        format="jam",
                        canonical_key=os.path.realpath(jhr),
                        lock_inode=(lock_stat.st_dev, lock_stat.st_ino),
                    )
                    store = RecoveryStore(
                        physical_base=physical_base,
                        recovery_root=recovery_root,
                        base_root=base_root,
                        retained_fds={
                            ("base", rels[0]): header_fd,
                            ("base", rels[1]): text_fd,
                            ("base", rels[2]): index_fd,
                        },
                    )
                    session = JamRecoverableSession(
                        store,
                        base=base,
                        paths=paths,
                        rels=rels,
                        fds=fds,
                        options=options,
                    )
                    try:
                        yield session
                    finally:
                        session.close()
                finally:
                    os.close(index_fd)
            finally:
                os.close(text_fd)


class JamSession:
    def __init__(self, base: Path, options: WriterOptions) -> None:
        self.base, self.options = base, options
        self.paths = _jam_paths(base)
        self._io = IO()
        self._closed = False
        self._poisoned = False

    def __enter__(self) -> Self:
        self._check()
        return self

    def __exit__(self, *args: object) -> None:
        self._closed = True

    def _check(self) -> None:
        if self._closed or self._poisoned:
            raise WriterError("JAM session is closed or unusable")

    @contextmanager
    def _operation(
        self,
    ) -> Iterator[tuple[tuple[int, int, int], tuple[bytes, bytes, bytes]]]:
        self._check()
        with locks.acquire(
            self.paths[0], timeout=self.options.lock_timeout
        ) as header_fd:
            binary_flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
            text_fd = os.open(self.paths[1], binary_flags)
            try:
                index_fd = os.open(self.paths[2], binary_flags)
                try:
                    fds = header_fd, text_fd, index_fd
                    raw = tuple(
                        self._io.read(fd, 0, os.fstat(fd).st_size) for fd in fds
                    )
                    snapshot = cast(tuple[bytes, bytes, bytes], raw)
                    _validate_base(snapshot, self.paths, self.options)
                    yield fds, snapshot
                finally:
                    os.close(index_fd)
            finally:
                os.close(text_fd)

    def _validate(self, raw: tuple[bytes, bytes, bytes]) -> None:
        _validate_base(raw, self.paths, self.options)

    def _target(
        self, raw: tuple[bytes, bytes, bytes], msgno: int
    ) -> tuple[int, list[int], bytes, bytes, RevisionToken]:
        return _target(raw, msgno, self.base)

    def read(self, msgno: int) -> SessionMessage:
        with self._operation() as (_fds, raw):
            return _session_message(raw, self.paths, self.base, self.options, msgno)

    def _commit(
        self,
        fds: tuple[int, int, int],
        operation: str,
        writes: list[tuple[int, int, bytes]],
    ) -> None:
        try:
            with Transaction(self._io, str(self.base), operation) as transaction:
                for fd in fds:
                    transaction.watch(fd)
                for fd, offset, data in writes:
                    self._io.write(fd, offset, data)
        except RollbackError:
            self._poisoned = True
            raise

    def _info(self, raw: bytes, delta: int) -> bytes:
        return _info(raw, delta)

    def append(self, message: OutgoingMessage) -> WriteResult:
        with self._operation() as (fds, raw):
            mutation = _plan_append(raw, message, self.options, self.base, self.paths)
            self._commit(fds, "append", _fd_writes(fds, mutation.writes))
            return mutation.result

    def _compare(
        self, identity: MessageIdentity, expected: RevisionToken, actual: RevisionToken
    ) -> None:
        _compare_revision(identity, expected, actual)

    def delete(
        self, identity: MessageIdentity, expected_revision: RevisionToken
    ) -> MessageIdentity:
        with self._operation() as (fds, raw):
            offset, words, metadata, _body, actual = self._target(raw, identity.msgno)
            self._compare(identity, expected_revision, actual)
            words[11] |= _DELETED
            original = _HEADER.unpack_from(raw[0], offset)
            header = _HEADER.pack(original[0], original[1], original[2], *words)
            self._commit(
                fds,
                "delete",
                [
                    (fds[0], offset, header),
                    (fds[2], actual.location[0], _INDEX.pack(0xFFFFFFFF, offset)),
                    (fds[0], 0, _info(raw[0], -1)),
                ],
            )
            return identity

    def update(
        self,
        identity: MessageIdentity,
        patch: MessagePatch,
        expected_revision: RevisionToken,
    ) -> WriteResult:
        with self._operation() as (fds, raw):
            mutation = _plan_update(
                raw,
                identity,
                patch,
                expected_revision,
                self.options,
                self.base,
                self.paths,
            )
            self._commit(fds, "update", _fd_writes(fds, mutation.writes))
            return mutation.result


class JamRecoverableSession(BaseRecoverableSession):
    """Held-lock JAM recovery session. Does not re-enter public lock methods."""

    def __init__(
        self,
        store: RecoveryStore,
        *,
        base: Path,
        paths: tuple[Path, Path, Path],
        rels: tuple[str, str, str],
        fds: tuple[int, int, int],
        options: WriterOptions,
    ) -> None:
        super().__init__(store)
        self._base = base
        self._paths = paths
        self._rels = rels
        self._fds = fds
        self._options = options
        self._io = IO()

    def _snapshot(self) -> tuple[bytes, bytes, bytes]:
        return _raw_from_fds(self._io, self._fds)

    def read(self, msgno: int) -> SessionMessage:
        self.check_usable()
        raw = self._snapshot()
        _validate_base(raw, self._paths, self._options)
        return _session_message(raw, self._paths, self._base, self._options, msgno)

    def _begin_prepare(self) -> None:
        self.check_usable()
        self._store.require_binding()
        if self._active_mutation is not None:
            self.poison()
            raise RecoveryError(code="invalid_prepared_mutation")
        if self._store.pending():
            raise RecoveryError(code="invalid_prepared_mutation")

    def _plan_for_recovery(
        self, planner: Callable[[tuple[bytes, bytes, bytes]], _JamMutation]
    ) -> tuple[tuple[bytes, bytes, bytes], _JamMutation]:
        try:
            raw = self._snapshot()
            _validate_base(raw, self._paths, self._options)
            mutation = planner(raw)
        except RecoveryError:
            raise
        except (UnicodeEncodeError, UnicodeDecodeError):
            _raise_recovery_input()
        except OSError:
            _raise_recovery_os()
        return raw, mutation

    def prepare_append(
        self, transaction_id: UUID, message: OutgoingMessage
    ) -> PreparedMutation:
        self._begin_prepare()

        def plan(raw: tuple[bytes, bytes, bytes]) -> _JamMutation:
            return _plan_append(raw, message, self._options, self._base, self._paths)

        raw, mutation = self._plan_for_recovery(plan)
        return self._register(
            transaction_id, RecoveryOperationKind.APPEND, raw, mutation
        )

    def prepare_update(
        self,
        transaction_id: UUID,
        identity: MessageIdentity,
        patch: MessagePatch,
        expected_revision: RevisionToken,
    ) -> PreparedMutation:
        self._begin_prepare()

        def plan(raw: tuple[bytes, bytes, bytes]) -> _JamMutation:
            return _plan_update(
                raw,
                identity,
                patch,
                expected_revision,
                self._options,
                self._base,
                self._paths,
            )

        raw, mutation = self._plan_for_recovery(plan)
        return self._register(
            transaction_id, RecoveryOperationKind.UPDATE, raw, mutation
        )

    def _register(
        self,
        transaction_id: UUID,
        operation: RecoveryOperationKind,
        raw: tuple[bytes, bytes, bytes],
        mutation: _JamMutation,
    ) -> PreparedMutation:
        plans = _file_plans(self._rels, self._fds, raw, mutation.writes)

        def apply_fn() -> WriteResult:
            try:
                return _apply_mutation(
                    self._io, self._fds, mutation, self._paths[0].parent
                )
            except RecoveryError:
                raise
            except OSError:
                _raise_recovery_os("unexpected_base_modification")
            except (UnicodeEncodeError, UnicodeDecodeError):
                _raise_recovery_input()

        return self.register_prepared_mutation(
            transaction_id, operation, plans, apply_fn
        )
