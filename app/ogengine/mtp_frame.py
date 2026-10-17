"""MTP framing: the 0xc0 layer, 6-byte header plus payload.

    [0]     0xc0  magic
    [1]     0x10 | (class << 7)     bit7 selects the packet class
    [2]     length & 7              low bits of the length, upper bits kept
    [3]     length >> 3             length is the TOTAL frame length
    [4:6]   u16 LE = chval ^ u16LE(p[2:4])
    [6:]    payload

Bit 7 of [1] selects the receive handler:

    c0 90  MTP control (hello, type 0x25, meter)
    c0 10  a raw KCP segment follows at [6:]
    c0 80  relay

Bits 5..6 of [1] select the extra 8-byte header read by every handler.  The
send path masks them off, so they are always 0 and the payload starts at [6:].

chval covers the FIRST 24 BYTES OF PAYLOAD only:

    chval = 0
    for i in 0..11:
        chval ^= rotl16(u16LE(payload[2*i : 2*i+2]), i)
"""
import glob
import os
import struct
import sys

MAGIC = 0xC0


def rotl16(v, n):
    n &= 15
    return ((v << n) | (v >> (16 - n))) & 0xFFFF


def chval(payload):
    """iv_mtpfrm_calc_chval -- first 24 bytes of payload, rotating per index."""
    acc = 0
    for i in range(12):
        off = 2 * i
        if off + 2 > len(payload):
            break
        v = struct.unpack_from("<H", payload, off)[0]
        acc ^= rotl16(v, i)
    return acc & 0xFFFF


def wrap(payload, flag=1):
    """Build a complete MTP frame around `payload`.

    flag defaults to 1 because that is what the wire actually shows: every
    genuine frame starts `c0 90`, and the producer is

        p[1] = ((prev & 0x9f) | 0x10) & 0x7f | (flag << 7)     @ 0x5b214..0x5b248

    With a freshly-zeroed buffer (prev = 0) flag=0 yields 0x10 and flag=1
    yields 0x90.  The three real callers of iv_mtp_chnnel_send_mtp_frm pass:
        0x5c008  iv_mtp_chnnel_send_meter_frm   r3 = 1
        0x5c360  iv_create_kcb                  r3 = 0
        0x53598  giot_send_create_kcp_session   r3 = its own arg3
    """
    total = len(payload) + 6
    if total > 0x7FF:
        raise ValueError("MTP length field is 11 bits")
    b2 = (total & 7)
    b3 = (total >> 3) & 0xFF
    b1 = 0x10 | ((flag & 1) << 7)
    word24 = (b2 | (b3 << 8))          # u16 LE of p[2:4]
    chk = chval(payload) ^ word24
    return bytes([MAGIC, b1, b2, b3]) + struct.pack("<H", chk) + bytes(payload)


def unwrap(frame):
    """Return (payload, total_len, flag, ok) for a MTP frame, else None."""
    if len(frame) < 6 or frame[0] != MAGIC:
        return None
    total = (frame[2] & 7) | (frame[3] << 3)
    flag = (frame[1] >> 7) & 1
    payload = frame[6:total]
    ok = (struct.unpack_from("<H", frame, 4)[0]
          == (chval(payload) ^ struct.unpack_from("<H", frame, 2)[0]))
    return payload, total, flag, ok


def hexdump(b, base=0):
    out = []
    for off in range(0, len(b), 16):
        c = b[off:off + 16]
        asc = "".join(chr(x) if 32 <= x < 127 else "." for x in c)
        out.append("  %4d  %-47s  |%s|"
                   % (base + off, " ".join("%02x" % x for x in c), asc))
    return "\n".join(out)


def main():
    files = sorted(glob.glob(sys.argv[1])) if len(sys.argv) > 1 else []
    print("=== iv_mtpfrm_calc_chval check ===")
    good = bad = other = 0
    for fn in files:
        d = open(fn, "rb").read()
        if d[0] != MAGIC:
            other += 1
            continue
        r = unwrap(d)
        if r is None:
            other += 1
            continue
        payload, total, flag, ok = r
        if ok:
            good += 1
        else:
            bad += 1
            print("  MISMATCH %s len=%d expected=%04x got=%04x"
                  % (os.path.basename(fn), len(d),
                     struct.unpack_from("<H", d, 4)[0],
                     chval(payload) ^ struct.unpack_from("<H", d, 2)[0]))
    print("  MTP frames ok=%d bad=%d   (non-MTP frames skipped=%d)"
          % (good, bad, other))

    if files:
        d = open(files[0], "rb").read()
        print()
        print("=== decode %s ===" % os.path.basename(files[0]))
        print(hexdump(d))
        if d[0] == MAGIC:
            payload, total, flag, ok = unwrap(d)
            print("  magic=0x%02x flag=%d total=%d payload=%d chval_ok=%s"
                  % (d[0], flag, total, len(payload), ok))
            print("  --- payload ---")
            print(hexdump(payload, 6))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
