"""RC5 block cipher used by the session frames.

w = 32, r = 6, 14 subkeys.  The key is never a fixed secret: each caller
derives it from the frame header it is about to decipher.
"""
MASK = 0xFFFFFFFF
P, Q = 0xB7E15163, 0x9E3779B9
ROUNDS = 6
W = 32


def _rotl32(x, n):
    n &= 31
    return ((x << n) | (x >> (32 - n))) & MASK

def rc5_setup(key: bytes):
    """RC5 key schedule for w=32, r=6 (14 subkeys)."""
    b = len(key)
    c = max(1, b // 4)
    L = [0] * c
    # fill L with the key bytes, high index down (per Rivest's spec)
    for i in range(b - 1, -1, -1):
        L[i // 4] = ((L[i // 4] << 8) + key[i]) & MASK

    t = 2 * (ROUNDS + 1)
    S = [0] * t
    S[0] = P
    for i in range(1, t):
        S[i] = (S[i - 1] + Q) & MASK

    A = B = i = j = 0
    for _ in range(3 * max(t, c)):
        A = S[i] = _rotl32((S[i] + A + B) & MASK, 3)
        B = L[j] = _rotl32((L[j] + A + B) & MASK, A + B)
        i = (i + 1) % t
        j = (j + 1) % c
    return S

def rc5_encrypt_block(S, blk: bytes, byteorder: str = "little") -> bytes:
    A = int.from_bytes(blk[0:4], byteorder)
    B = int.from_bytes(blk[4:8], byteorder)
    A = (A + S[0]) & MASK
    B = (B + S[1]) & MASK
    for i in range(1, ROUNDS + 1):
        A = (_rotl32(A ^ B, B) + S[2 * i]) & MASK
        B = (_rotl32(B ^ A, A) + S[2 * i + 1]) & MASK
    return A.to_bytes(4, byteorder) + B.to_bytes(4, byteorder)

def rc5_decrypt_block(S, blk: bytes, byteorder: str = "little") -> bytes:
    """Inverse of rc5_encrypt_block (the Feistel run backwards)."""
    A = int.from_bytes(blk[0:4], byteorder)
    B = int.from_bytes(blk[4:8], byteorder)
    for i in range(ROUNDS, 0, -1):
        B = (_rotl32((B - S[2 * i + 1]) & MASK, 32 - (A & 31)) ^ A) & MASK
        A = (_rotl32((A - S[2 * i]) & MASK, 32 - (B & 31)) ^ B) & MASK
    B = (B - S[1]) & MASK
    A = (A - S[0]) & MASK
    return A.to_bytes(4, byteorder) + B.to_bytes(4, byteorder)

