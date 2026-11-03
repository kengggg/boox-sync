"""Tiny synthetic Fleece writer for notebook metadata tests; no tablet data."""

from boox_sync.proto import encode_varint


def fleece(document, shared_keys=None, prefix=b""):
    out = bytearray(prefix)
    if len(out) % 2:
        out.append(0)

    def append(raw):
        offset = len(out)
        out.extend(raw)
        if len(out) % 2:
            out.append(0)
        return offset

    def pointer(target):
        distance = (len(out) - target) // 2
        assert 0 < distance < 16384
        out.extend((0x8000 | distance).to_bytes(2, "big"))

    def write(value):
        if value is None:
            return append(b"\x30\x00")
        if type(value) is bool:
            return append(b"\x38\x00" if value else b"\x34\x00")
        if type(value) is int:
            assert -2048 <= value < 2048
            return append((value & 4095).to_bytes(2, "big"))
        if isinstance(value, str):
            raw = value.encode()
            tag = (
                bytes([0x40 + len(raw)])
                if len(raw) < 15
                else b"\x4f" + encode_varint(len(raw))
            )
            return append(tag + raw)
        if isinstance(value, dict):
            items = []
            for key, item in value.items():
                if shared_keys and key in shared_keys:
                    key = shared_keys.index(key)
                items.extend([write(key), write(item)])
            offset = append((0x7000 | len(value)).to_bytes(2, "big"))
        else:
            items = [write(item) for item in value]
            offset = append((0x6000 | len(value)).to_bytes(2, "big"))
        for item in items:
            pointer(item)
        return offset

    pointer(write(document))
    return bytes(out)


def notebook(title="Notebook", status=1, **fields):
    return {"source": "Local", "title": title, "type": 1, "status": status, **fields}
