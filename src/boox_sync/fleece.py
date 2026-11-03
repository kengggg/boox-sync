"""Bounded, read-only Fleece decoding for BOOX notebook metadata.

Implemented from the documented wire format (not the Couchbase source):
https://github.com/couchbase/fleece/blob/master/Fleece.md
External references require a base document and are deliberately rejected.
"""

import struct


class FleeceError(ValueError):
    pass


_UNDEFINED = object()


def decode_fleece(data: bytes, shared_keys: list[str] | None = None):
    if not isinstance(data, bytes) or len(data) < 2 or len(data) % 2:
        raise FleeceError("Invalid Fleece document length")
    if len(data) > 16 * 1024 * 1024:
        raise FleeceError("Notebook metadata exceeds the size limit")
    remaining = 100_000

    def take(pos, size):
        if pos < 0 or size < 0 or pos + size > len(data):
            raise FleeceError("Truncated Fleece value")
        return data[pos : pos + size]

    def varint(pos):
        number = 0
        for shift in range(0, 35, 7):
            byte = take(pos, 1)[0]
            pos += 1
            number |= (byte & 127) << shift
            if byte < 128:
                return number, pos
        raise FleeceError("Invalid Fleece length")

    def value(pos, width=2, depth=0):
        nonlocal remaining
        remaining -= 1
        if depth > 64 or remaining < 0 or pos % 2:
            raise FleeceError("Invalid or excessively nested Fleece metadata")
        head = take(pos, 2)
        tag, small = head[0] >> 4, head[0] & 15
        if tag >= 8:
            if head[0] & 0x40:
                raise FleeceError("External Fleece references are unsupported")
            distance = int.from_bytes(take(pos, width), "big") & (
                (1 << (width * 8 - 2)) - 1
            )
            if not distance:
                raise FleeceError("Invalid Fleece pointer")
            return value(pos - distance * 2, 4, depth + 1)
        if tag == 0:
            number = (small << 8) | head[1]
            return number - 4096 if number >= 2048 else number
        if tag == 1:
            return int.from_bytes(
                take(pos + 1, (small & 7) + 1), "little", signed=not (small & 8)
            )
        if tag == 2:
            if small not in (0, 4, 8):
                raise FleeceError("Unsupported Fleece floating-point encoding")
            return struct.unpack(
                "<d" if small & 8 else "<f", take(pos + 2, 8 if small & 8 else 4)
            )[0]
        if tag == 3:
            if small not in (0, 4, 8, 12):
                raise FleeceError("Unknown Fleece special value")
            return {0: None, 4: False, 8: True, 12: _UNDEFINED}[small]
        if tag in (4, 5):
            length, start = (small, pos + 1) if small < 15 else varint(pos + 1)
            raw = take(start, length)
            try:
                return raw.decode("utf-8") if tag == 4 else raw
            except UnicodeDecodeError as exc:
                raise FleeceError("Invalid UTF-8 in notebook metadata") from exc
        count, start = ((small & 7) << 8) | head[1], pos + 2
        stride = 4 if small & 8 else 2
        if count == 2047:
            overflow, start = varint(start)
            count += overflow
            start += start % 2
        take(start, count * stride * (2 if tag == 7 else 1))
        if count > remaining:
            raise FleeceError("Too many Fleece collection values")
        if tag == 6:
            return [value(start + i * stride, stride, depth + 1) for i in range(count)]
        result = {}
        for i in range(count):
            key = value(start + i * 2 * stride, stride, depth + 1)
            item = value(start + (i * 2 + 1) * stride, stride, depth + 1)
            if type(key) is int and key == -2048:
                if i != 0 or not isinstance(item, dict):
                    raise FleeceError("Invalid inherited Fleece dictionary")
                result.update(item)
                continue
            if type(key) is int:
                if not shared_keys or not 0 <= key < len(shared_keys):
                    raise FleeceError("Notebook metadata needs valid shared keys")
                key = shared_keys[key]
            if not isinstance(key, str):
                raise FleeceError("Invalid notebook metadata key")
            if item is _UNDEFINED:
                result.pop(key, None)
            else:
                result[key] = item
        return result

    return value(len(data) - 2)
