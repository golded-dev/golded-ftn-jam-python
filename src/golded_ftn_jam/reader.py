"""Indexed JAM revision 1 reader. Read only stable, quiescent areas."""

import codecs
import errno
import re
import stat
import struct
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta
from os import PathLike
from pathlib import Path
from typing import NoReturn

from golded_ftn import (
    MessageControlLines,
    MessageProvenance,
    ParsedMessage,
    ParserException,
    ReaderOptions,
    detect_charset,
    parse_body,
    parse_message,
    synthetic_id,
    to_utf8,
)

_HEADER = struct.Struct("<4sHH17I")
_SUBFIELD = struct.Struct("<HHI")
_INDEX = struct.Struct("<II")
_DELETED = 0x80000000
_UNSUPPORTED = 0x00380000
_CONTROL_NAMES = {4: "MSGID", 5: "REPLY", 7: "PID", 2003: "FLAGS", 2004: "TZUTC"}
_KNOWN = {0, 1, 2, 3, 4, 5, 6, 7, 2000, 2001, 2002, 2003, 2004}


def _fail(path: Path, offset: int, error: Exception) -> NoReturn:
    raise ParserException(
        f"Cannot parse JAM file {path} at offset {offset}: {error}"
    ) from error


def _require(condition: bool, path: Path, offset: int, message: str) -> None:
    if not condition:
        _fail(path, offset, ValueError(message))


def _slice(data: bytes, offset: int, length: int, path: Path) -> bytes:
    _require(
        0 <= offset <= len(data) and 0 <= length <= len(data) - offset,
        path,
        offset,
        f"Range of {length} bytes is outside file of {len(data)} bytes",
    )
    return data[offset : offset + length]


def _file(base: Path, suffix: str) -> Path:
    candidates = [
        p
        for p in base.parent.iterdir()
        if p.stem == base.name and p.suffix.upper() == suffix
    ]
    if not candidates:
        raise FileNotFoundError(errno.ENOENT, "Missing JAM file", str(base) + suffix)
    _require(
        len(candidates) == 1,
        candidates[0],
        0,
        f"Ambiguous {suffix} files: {candidates}",
    )
    path = candidates[0]
    if not stat.S_ISREG(path.lstat().st_mode):
        raise OSError(errno.EINVAL, "JAM source must be a regular file", str(path))
    return path


def _subfields(
    data: bytes, start: int, length: int, path: Path
) -> list[tuple[int, bytes, int]]:
    end = start + length
    _slice(data, start, length, path)
    result: list[tuple[int, bytes, int]] = []
    offset = start
    while offset < end:
        _require(end - offset >= 8, path, offset, "Truncated subfield header")
        lo, hi, size = _SUBFIELD.unpack_from(data, offset)
        payload = offset + 8
        _require(size <= end - payload, path, offset, "Subfield exceeds declared block")
        if hi == 0 and lo in _KNOWN:
            maximum = 100 if lo <= 6 else 40 if lo == 7 else 255 if lo == 2000 else None
            _require(
                maximum is None or size <= maximum,
                path,
                offset,
                f"Subfield {lo} exceeds specification maximum length",
            )
            if lo == 2004:
                _require(
                    re.fullmatch(rb"[+-]?[0-9]{4}", data[payload : payload + size])
                    is not None,
                    path,
                    payload,
                    "Invalid TZUTCINFO",
                )
            result.append((lo, data[payload : payload + size], payload))
        offset = payload + size
    return result


def _decode(value: bytes, charset: str, path: Path, offset: int) -> str:
    try:
        return to_utf8(value, charset)
    except (ValueError, LookupError) as error:
        _fail(path, offset, error)


def _control(lo: int, value: str) -> str:
    if lo == 2000:
        return "\x01" + value.lstrip("\x01")
    if lo == 2001:
        return "SEEN-BY: " + value
    if lo == 2002:
        return "PATH: " + value
    return f"\x01{_CONTROL_NAMES[lo]}: {value}"


def _charset(
    fields: list[tuple[int, bytes, int]],
    body: bytes,
    options: ReaderOptions,
    jhr: Path,
    jdt: Path,
    text_offset: int,
) -> str:
    declarations: list[tuple[bytes, Path, int, bool]] = [
        (value, jhr, offset, True) for lo, value, offset in fields if lo == 2000
    ]
    declarations.append((body, jdt, text_offset, False))
    selected: str | None = None
    identity: str | None = None
    try:
        fallback = detect_charset(b"", options.fallback_charset)
    except (ValueError, LookupError) as error:
        _fail(jhr, 0, error)
    for raw, path, offset, is_subfield in declarations:
        prefix = rb"(?:^|\x01)" if is_subfield else rb"\x01"
        for match in re.finditer(
            prefix + rb"(?:CHRS|CHARSET):\s*([^\s\x00\x01]+)", raw, re.IGNORECASE
        ):
            declaration = b"\x01" + match[0].lstrip(b"\x01")
            charset = detect_charset(declaration, options.fallback_charset)
            # Unknown declarations use core fallback, but still must agree by name.
            name = match[1].decode("ascii", errors="replace").upper()
            cp = codecs.lookup(detect_charset(declaration, "CP850")).name
            utf = codecs.lookup(detect_charset(declaration, "UTF-8")).name
            key = cp if cp == utf else "unknown:" + name
            _require(
                identity is None or identity == key,
                path,
                offset + match.start(),
                "Conflicting charset declarations",
            )
            if selected is None:
                selected, identity = charset, key
    return selected or fallback


def _unique_controls(controls: MessageControlLines, path: Path, offset: int) -> None:
    seen: dict[str, str] = {}
    for control in controls.kludges:
        if control.name in {"MSGID", "REPLY"}:
            _require(
                control.name not in seen or seen[control.name] == control.value,
                path,
                offset,
                f"Conflicting {control.name} values",
            )
            seen[control.name] = control.value


class JamReader:
    """Validate all indexed records before returning messages in number order.

    The files must be stable; reading them separately does not create a snapshot.
    """

    def read(
        self, path: str | PathLike[str], options: ReaderOptions | None = None
    ) -> Iterable[ParsedMessage]:
        base = Path(path)
        jhr, jdt, jdx = (_file(base, suffix) for suffix in (".JHR", ".JDT", ".JDX"))
        headers, texts, index = jhr.read_bytes(), jdt.read_bytes(), jdx.read_bytes()
        _slice(headers, 0, 1024, jhr)
        _require(headers[:4] == b"JAM\0", jhr, 0, "Invalid area signature")
        base_number = int.from_bytes(headers[20:24], "little")
        _require(base_number >= 1, jhr, 20, "BaseMsgNum must be positive")
        _require(
            len(index) % 8 == 0, jdx, len(index) // 8 * 8, "Truncated index record"
        )
        _require(
            base_number + len(index) // 8 - 1 <= 0xFFFFFFFF,
            jdx,
            0,
            "Message number exceeds unsigned 32-bit range",
        )
        used: set[int] = set()
        result: list[ParsedMessage] = []
        for position in range(len(index) // 8):
            crc, offset = _INDEX.unpack_from(index, position * 8)
            if crc == offset == 0xFFFFFFFF:
                continue
            _require(offset not in used, jdx, position * 8 + 4, "Reused header offset")
            used.add(offset)
            _require(offset >= 1024, jhr, offset, "Header points into area header")
            message = self._message(
                headers,
                texts,
                jhr,
                jdt,
                offset,
                base_number + position,
                options or ReaderOptions(),
            )
            if message is not None:
                result.append(message)
        return tuple(result)

    @staticmethod
    def _message(
        headers: bytes,
        texts: bytes,
        jhr: Path,
        jdt: Path,
        offset: int,
        msgno: int,
        options: ReaderOptions,
    ) -> ParsedMessage | None:
        fixed = _HEADER.unpack(_slice(headers, offset, 76, jhr))
        signature, revision, _reserved, *words = fixed
        _require(signature == b"JAM\0", jhr, offset, "Invalid message signature")
        _require(revision == 1, jhr, offset + 4, "Unsupported message revision")
        _require(
            words[10] == msgno, jhr, offset + 48, "Message number disagrees with index"
        )
        fields = _subfields(headers, offset + 76, words[0], jhr)
        text_offset, text_length = words[13:15]
        raw_body = _slice(texts, text_offset, text_length, jdt)
        attributes = words[11]
        if attributes & _DELETED:
            return None
        _require(
            not attributes & _UNSUPPORTED,
            jhr,
            offset + 52,
            "Encrypted, compressed or escaped messages are unsupported",
        )
        charset = _charset(fields, raw_body, options, jhr, jdt, text_offset)
        values: dict[int, str] = {}
        control_text: list[str] = []
        for lo, raw, payload_offset in fields:
            value = _decode(raw, charset, jhr, payload_offset)
            if lo in {0, 1}:
                values.setdefault(lo, value)
            elif lo not in {2000, 2001, 2002}:
                _require(
                    lo not in values or values[lo] == value,
                    jhr,
                    payload_offset,
                    f"Conflicting subfield {lo} values",
                )
                values[lo] = value
            if lo in _CONTROL_NAMES or lo in {2000, 2001, 2002}:
                control_text.append(_control(lo, value))
        body = parse_body(_decode(raw_body, charset, jdt, text_offset))
        header_controls = parse_message("\n".join(control_text))
        body_controls = parse_message(body)
        _unique_controls(header_controls, jhr, offset + 76)
        _unique_controls(body_controls, jdt, text_offset)
        for name in ("msgid", "reply"):
            a, b = getattr(header_controls, name), getattr(body_controls, name)
            _require(
                a is None or b is None or a == b,
                jdt,
                text_offset,
                f"Header and body {name} disagree",
            )
        controls = replace(
            body_controls,
            kludges=header_controls.kludges + body_controls.kludges,
            msgid=header_controls.msgid
            if header_controls.msgid is not None
            else body_controls.msgid,
            reply=header_controls.reply
            if header_controls.reply is not None
            else body_controls.reply,
            charset=header_controls.charset
            if header_controls.charset is not None
            else body_controls.charset,
            seen_by=header_controls.seen_by + body_controls.seen_by,
            path=header_controls.path + body_controls.path,
        )
        date = datetime(1970, 1, 1) + timedelta(seconds=words[7]) if words[7] else None
        sender, recipient, subject = (values.get(lo, "") for lo in (2, 3, 6))
        external_id = controls.msgid
        if external_id is None:
            external_id = synthetic_id(
                sender, recipient, subject, date.isoformat() if date else None, body
            )
        return ParsedMessage(
            msgno=msgno,
            from_name=sender,
            to_name=recipient,
            subject=subject,
            body_text=body,
            attributes_raw=attributes,
            posted_at=date,
            external_id=external_id,
            from_address=values.get(0),
            to_address=values.get(1),
            reply_to_msgno=words[4] or None,
            reply1st_msgno=words[5] or None,
            reply_next_msgno=words[6] or None,
            control_lines=controls,
            provenance=MessageProvenance(
                source_type="jam",
                source_path=str(jhr),
                source_id=str(msgno),
                source_offset=offset,
            ),
        )
