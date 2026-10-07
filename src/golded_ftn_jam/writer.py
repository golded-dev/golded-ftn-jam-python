"""JAM mutation sessions. GoldED concurrency is deliberately not enabled."""

from __future__ import annotations

import os
import re
import struct
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path
from typing import Self, cast

from golded_ftn import (
    UNSET,
    ConflictError,
    ControlLine,
    FtnAddress,
    MessageIdentity,
    MessagePatch,
    OutgoingMessage,
    ParserException,
    ReaderOptions,
    RevisionToken,
    RollbackError,
    SessionMessage,
    UnsupportedOperationError,
    WriterError,
    WriteResult,
    WriterOptions,
    parse_message,
)
from golded_ftn._writer_io import IO, Transaction, locks, raw_revision, strict_encode

from .reader import _DELETED, _HEADER, _INDEX, _SUBFIELD, JamReader, _file


def _crc(raw: bytes) -> int:
    # GoldED seeds with FFFFFFFF and does not apply the final complement.
    return zlib.crc32(raw.lower()) ^ 0xFFFFFFFF


def _u32(value: int) -> int:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("JAM value exceeds unsigned 32-bit range")
    return value


def _date(value: datetime | None) -> int:
    if value is None:
        return 0
    return _u32(
        int(value.replace(tzinfo=UTC).timestamp())
        if value.tzinfo is None
        else int(value.timestamp())
    )


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


class JamWriter:
    """Create areas and open operation-scoped mutation sessions."""

    def create(self, path: str | PathLike[str]) -> None:
        base = Path(path)
        base.parent.mkdir(parents=True, exist_ok=True)
        paths = [
            Path(str(base) + suffix) for suffix in (".JHR", ".JDT", ".JDX", ".JLR")
        ]
        if any(p.exists() for p in paths) or any(
            p.stem == base.name and p.suffix.upper() in {".JHR", ".JDT", ".JDX", ".JLR"}
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


class JamSession:
    def __init__(self, base: Path, options: WriterOptions) -> None:
        if options.concurrent:
            raise UnsupportedOperationError(
                "JAM GoldED concurrency has not been verified"
            )
        self.base, self.options = base, options
        self.paths = tuple(_file(base, suffix) for suffix in (".JHR", ".JDT", ".JDX"))
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
                    self._validate(snapshot)
                    yield fds, snapshot
                finally:
                    os.close(index_fd)
            finally:
                os.close(text_fd)

    def _validate(self, raw: tuple[bytes, bytes, bytes]) -> None:
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
                self.paths[0],
                self.paths[1],
                offset,
                base + slot,
                ReaderOptions(fallback_charset=self.options.target_charset),
            )
            active += message is not None
        if int.from_bytes(headers[12:16], "little") != active:
            raise ParserException("JAM ActiveMsgs disagrees with live index records")

    def _target(
        self, raw: tuple[bytes, bytes, bytes], msgno: int
    ) -> tuple[int, list[int], bytes, bytes, RevisionToken]:
        headers, texts, index = raw
        base = int.from_bytes(headers[20:24], "little")
        slot = msgno - base
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
        identity = MessageIdentity(format="jam", base=str(self.base), msgno=msgno)
        revision = raw_revision(
            identity,
            (slot * 8, offset, words[13]),
            headers[offset : offset + 76],
            metadata,
            body,
        )
        return offset, words, metadata, body, revision

    def read(self, msgno: int) -> SessionMessage:
        with self._operation() as (_fds, raw):
            offset, _words, _metadata, _body, revision = self._target(raw, msgno)
            message = JamReader._message(
                raw[0],
                raw[1],
                self.paths[0],
                self.paths[1],
                offset,
                msgno,
                ReaderOptions(fallback_charset=self.options.target_charset),
            )
            assert message is not None
            return SessionMessage(
                message=message, identity=revision.identity, revision=revision
            )

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
        result = bytearray(raw[:1024])
        mod, active = struct.unpack_from("<II", result, 8)
        struct.pack_into("<II", result, 8, (mod + 1) & 0xFFFFFFFF, _u32(active + delta))
        return bytes(result)

    def append(self, message: OutgoingMessage) -> WriteResult:
        if message.reply_list:
            raise ValueError("JAM has three reply links, not an arbitrary reply list")
        strict_encode("", self.options, parse_message(message.body_text).kludges)
        if "\0" in message.body_text:
            raise ValueError("NUL is not representable in JAM text")
        metadata = _metadata(message, self.options.target_charset)
        body = strict_encode(
            message.body_text.replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", "\r"),
            self.options,
            message.control_lines,
        )
        subfields = b"".join(
            _field(lo, value) for lo, values in metadata.items() for value in values
        )
        attrs = _u32(message.attributes_raw or 0)
        if attrs & (_DELETED | 0x00380000):
            raise ValueError("Unsupported JAM attributes")
        with self._operation() as (fds, raw):
            msgno = _u32(int.from_bytes(raw[0][20:24], "little") + len(raw[2]) // 8)
            words = [
                len(subfields),
                0,
                _crc(metadata.get(4, [b""])[0]),
                _crc(metadata.get(5, [b""])[0]),
                _u32(message.reply_to_msgno or 0),
                _u32(message.reply1st_msgno or 0),
                _u32(message.reply_next_msgno or 0),
                _date(message.posted_at),
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
                self.paths[0],
                self.paths[1],
                offset,
                msgno,
                ReaderOptions(fallback_charset=self.options.target_charset),
            )
            self._commit(
                fds,
                "append",
                [
                    (fds[1], len(raw[1]), body),
                    (fds[0], offset, header),
                    (fds[2], len(raw[2]), index),
                    (fds[0], 0, self._info(raw[0], 1)),
                ],
            )
            identity = MessageIdentity(format="jam", base=str(self.base), msgno=msgno)
            return WriteResult(
                identity=identity,
                revision=raw_revision(
                    identity, (len(raw[2]), offset, words[13]), header, body
                ),
            )

    def _compare(
        self, identity: MessageIdentity, expected: RevisionToken, actual: RevisionToken
    ) -> None:
        if identity != actual.identity or expected != actual:
            raise ConflictError(
                "JAM target revision changed or belongs to another base"
            )

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
                    (fds[0], 0, self._info(raw[0], -1)),
                ],
            )
            return identity

    def update(
        self,
        identity: MessageIdentity,
        patch: MessagePatch,
        expected_revision: RevisionToken,
    ) -> WriteResult:
        if patch.provenance is not UNSET or patch.reply_list is not UNSET:
            raise ValueError("JAM cannot patch provenance or arbitrary reply lists")
        with self._operation() as (fds, raw):
            offset, words, metadata, body, actual = self._target(raw, identity.msgno)
            self._compare(identity, expected_revision, actual)
            existing = _raw_fields(metadata)
            original_text = (
                body.decode(self.options.target_charset, errors="strict")
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
                    else [
                        str(value).encode(self.options.target_charset, errors="strict")
                    ]
                )
                serialized = True
            if patch.control_lines is not UNSET:
                controls = (
                    cast(tuple[ControlLine, ...] | None, patch.control_lines) or ()
                )
                strict_encode("", self.options, controls)
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
                    text = (
                        control.value
                        if lo != 2000
                        else f"{control.name}: {control.value}"
                    )
                    encoded = text.encode(self.options.target_charset, errors="strict")
                    if control.name.upper() in {"FMPT", "TOPT", "INTL", "PATH"}:
                        name = control.name.upper()
                        retained_values = [
                            parts[1].strip()
                            for low, high, payload in existing
                            if low == 2000 and high == 0
                            for parts in [
                                re.split(
                                    r"[:\s]",
                                    payload.decode(self.options.target_charset),
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
                                payload.decode(self.options.target_charset)
                                for low, high, payload in existing
                                if low == 2002 and high == 0
                            )
                        if control.value.strip() not in retained_values:
                            raise ValueError(
                                f"Control {name} conflicts with omitted "
                                "structured field"
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
                                inline_id.encode(self.options.target_charset)
                                if inline_id
                                else None
                            )
                        expected_id = (
                            current_id
                            if patch.external_id is UNSET
                            else (
                                None
                                if patch.external_id is None
                                else cast(str, patch.external_id).encode(
                                    self.options.target_charset
                                )
                            )
                        )
                        if encoded != expected_id:
                            raise ValueError(
                                "MSGID conflicts with omitted or explicit external_id"
                            )
                        if patch.external_id is UNSET:
                            continue
                        replacements.setdefault(4, [])
                    if (
                        lo in (4, 5)
                        and replacements[lo]
                        and encoded not in replacements[lo]
                    ):
                        raise ValueError("Conflicting JAM control values")
                    replacements.setdefault(lo, []).append(encoded)
                serialized = True
            for name, lo in (("routing_seen_by", 2001), ("routing_path", 2002)):
                value = getattr(patch, name)
                if value is not UNSET:
                    replacements[lo] = [
                        text.encode(self.options.target_charset, errors="strict")
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
                    self.options,
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
                for line in (
                    original_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                ):
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
                        (
                            routing_name is not None
                            and getattr(patch, routing_name) is not UNSET
                        )
                        or (control_name == "MSGID" and patch.external_id is not UNSET)
                        or (
                            control_name is not None
                            and control_name
                            not in {"MSGID", "FMPT", "TOPT", "INTL", "PATH"}
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
                body = strict_encode(text.replace("\n", "\r"), self.options)
            if serialized:
                retained_controls = tuple(
                    ControlLine(name=parts[0], value=parts[1], raw="")
                    for lo, hi, payload in existing
                    if hi == 0 and lo == 2000 and lo not in replacements
                    for text in [
                        payload.decode(self.options.target_charset, errors="strict")
                    ]
                    for parts in [text.split(":", 1)]
                    if len(parts) == 2
                )
                strict_encode("", self.options, retained_controls)
                # Body controls must also agree, including an unchanged body.

                body_controls = parse_message(
                    body.decode(self.options.target_charset, errors="strict")
                )
                strict_encode("", self.options, body_controls.kludges)
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
                _field(lo, payload)
                for lo, values in replacements.items()
                for payload in values
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
            if patch.posted_at is not UNSET:
                words[7] = _date(cast(datetime | None, patch.posted_at))
            if patch.attributes_raw is not UNSET:
                if patch.attributes_raw is None:
                    raise ValueError("attributes_raw cannot be cleared with None")
                words[11] = _u32(cast(int, patch.attributes_raw))
                if words[11] & (_DELETED | 0x00380000):
                    raise ValueError("Unsupported JAM attributes")
            words[0] = _u32(len(metadata))
            original = _HEADER.unpack_from(raw[0], offset)
            writes = []
            if serialized:
                words[13], words[14] = _u32(len(raw[1])), _u32(len(body))
                offset = _u32(len(raw[0]))
                writes.append((fds[1], words[13], body))
            header = _HEADER.pack(original[0], original[1], original[2], *words)
            writes.append((fds[0], offset, header + metadata if serialized else header))
            recipient = next(
                (payload for lo, hi, payload in current if lo == 3 and hi == 0), b""
            )
            writes.extend(
                [
                    (
                        fds[2],
                        actual.location[0],
                        _INDEX.pack(_crc(recipient), offset)
                        if 3 in replacements
                        else raw[2][actual.location[0] : actual.location[0] + 4]
                        + struct.pack("<I", offset),
                    ),
                    (fds[0], 0, self._info(raw[0], 0)),
                ]
            )
            # Validate the proposed record before the first mutation.
            proposed_headers = (
                raw[0] + header + metadata
                if serialized
                else (raw[0][:offset] + header + raw[0][offset + 76 :])
            )
            proposed_texts = raw[1] + body if serialized else raw[1]
            JamReader._message(
                proposed_headers,
                proposed_texts,
                self.paths[0],
                self.paths[1],
                offset,
                identity.msgno,
                ReaderOptions(fallback_charset=self.options.target_charset),
            )
            self._commit(fds, "update", writes)
            return WriteResult(
                identity=identity,
                revision=raw_revision(
                    identity,
                    (actual.location[0], offset, words[13]),
                    header,
                    metadata,
                    body,
                ),
            )
