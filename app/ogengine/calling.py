"""RC5 decryption of the CALLING frame (the session handshake).

The 8-byte RC5 key is derived from the frame itself: bytes 0..4, bytes 20..23
and one zero byte.  Only the 8-byte blocks at 12..20 and from 24 onward carry
ciphertext.
"""
from .rc5 import rc5_setup, rc5_decrypt_block


def decrypt(frame: bytes) -> bytes:
    """Return the plaintext of a CALLING frame."""
    key = frame[0:4] + frame[20:23] + b"\x00"
    st = rc5_setup(key)
    out = bytearray(frame)
    for off in list(range(12, 20, 8)) + list(range(24, len(frame), 8)):
        if off + 8 > len(out):
            break
        out[off:off + 8] = rc5_decrypt_block(st, bytes(frame[off:off + 8]), "little")
    return bytes(out)
