"""JSON values used by the read-only SQLite transport; never use pickle."""
import base64
import binascii


_BYTES = '__dmcs_bytes__'


def encode(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {_BYTES: base64.b64encode(bytes(value)).decode('ascii')}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {_BYTES}:
            try:
                return base64.b64decode(value[_BYTES], validate=True)
            except (TypeError, ValueError, binascii.Error):
                raise ValueError('Invalid binary transport value') from None
        return {key: decode(item) for key, item in value.items()}
    return value
