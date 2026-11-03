"""Minimal protobuf wire codec for the fields a .note zip actually uses."""

import struct


def encode_varint(value: int) -> bytes:
    """Encode an unsigned varint."""
    out = bytearray()
    while True:
        piece = value & 0x7F
        value >>= 7
        if value:
            out.append(piece | 0x80)
        else:
            out.append(piece)
            return bytes(out)


def encode_key(field: int, wire: int) -> bytes:
    """Encode a protobuf field key."""
    return encode_varint((field << 3) | wire)


def encode_varint_field(field: int, value: int) -> bytes:
    """Encode a varint field."""
    return encode_key(field, 0) + encode_varint(value)


def encode_bytes_field(field: int, payload: bytes) -> bytes:
    """Encode a length-delimited field."""
    return encode_key(field, 2) + encode_varint(len(payload)) + payload


def encode_float_field(field: int, value: float) -> bytes:
    """Encode a 32-bit float field."""
    return encode_key(field, 5) + struct.pack("<f", value)


def parse_message(buf: bytes) -> list[tuple[int, object]]:
    """Return (field number, value) pairs.

    Varints are ints. Length-delimited fields and fixed32 fields are bytes.

    Raises:
        ValueError: The buffer ends inside a field.
    """
    fields: list[tuple[int, object]] = []
    index = 0
    length = len(buf)
    while index < length:
        key, index = _read_varint(buf, index)
        wire = key & 7
        field = key >> 3
        if wire == 0:
            value, index = _read_varint(buf, index)
            fields.append((field, value))
        elif wire == 2:
            size, index = _read_varint(buf, index)
            end = index + size
            if end > length:
                raise ValueError("length-delimited field overruns the message")
            fields.append((field, buf[index:end]))
            index = end
        elif wire == 5:
            end = index + 4
            if end > length:
                raise ValueError("fixed32 field overruns the message")
            fields.append((field, buf[index:end]))
            index = end
        elif wire == 1:
            end = index + 8
            if end > length:
                raise ValueError("fixed64 field overruns the message")
            fields.append((field, buf[index:end]))
            index = end
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return fields


def grouped(fields: list[tuple[int, object]]) -> dict[int, list[object]]:
    """Group parsed fields by field number, keeping repeated order."""
    buckets: dict[int, list[object]] = {}
    for number, value in fields:
        buckets.setdefault(number, []).append(value)
    return buckets


def first(
    buckets: dict[int, list[object]], number: int, default: object = None
) -> object:
    """Return the first value for a field number."""
    values = buckets.get(number)
    if not values:
        return default
    return values[0]


def as_text(value: object) -> str:
    """Decode a length-delimited field as UTF-8 text."""
    if not isinstance(value, bytes):
        return ""
    return value.decode("utf-8")


def as_float(value: object) -> float | None:
    """Decode a fixed32 field as a float."""
    if not isinstance(value, bytes) or len(value) != 4:
        return None
    return struct.unpack("<f", value)[0]


def _read_varint(buf: bytes, index: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while True:
        if index >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
        if shift > 70:
            raise ValueError("varint is too long")
