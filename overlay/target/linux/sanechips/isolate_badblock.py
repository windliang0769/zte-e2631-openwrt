#!/usr/bin/env python3
# Isolate unit B's marginal NAND erase block (flash 0x1840000) behind the
# squashfs image of a RAX1800-Z sysupgrade tar.
#
# The rootfs member of the tar is a squashfs image whose superblock
# bytes_used field (u64 LE at offset 40) tells mtdsplit_squashfs where the
# rootfs_data partition begins: round_up(bytes_used, erase_block).  The
# stock image is 0xb59ca0 bytes -> split at flash 0xc00000 + 0xb60000 =
# 0x1760000, which makes erase block 7 of rootfs_data (flash 0x1840000)
# reachable by jffs2.  That block corrupts data on readback while writes
# appear to succeed and jffs2 never marks it bad, so the overlay randomly
# fails to mount (tmpfs fallback, config loss).
#
# Fix: extend the squashfs image by 1 MiB (erase-aligned, zero padding) and
# rewrite bytes_used accordingly.  mtdsplit then places rootfs_data at
# flash 0x1860000 and the marginal block ends up in dead space behind the
# squashfs, which only reads blocks referenced by its metadata.  The padded
# region is written to flash but never read back.
import io
import struct
import sys
import tarfile

EB = 0x20000      # NAND erase block size (128 KiB)
PAD = 0x100000    # 1 MiB skip past the marginal block

def pad_root(data):
    if data[:4] != b"hsqs":
        raise SystemExit("isolate-badblock: not a little-endian squashfs image")
    old = struct.unpack_from("<Q", data, 40)[0]
    size = len(data)
    target = ((size + PAD + EB - 1) // EB) * EB
    if target < old:
        raise SystemExit("isolate-badblock: padded target smaller than bytes_used")
    buf = bytearray(data)
    struct.pack_into("<Q", buf, 40, target)
    buf += b"\0" * (target - size)
    print("isolate-badblock: root %d (0x%x) bytes, bytes_used 0x%x -> 0x%x"
          % (size, size, old, target))
    return bytes(buf)

def main(path):
    with tarfile.open(path) as t:
        members = t.getmembers()
        payloads = [(m, t.extractfile(m).read() if m.isfile() else None)
                    for m in members]
    out = io.BytesIO()
    hit = False
    with tarfile.open(fileobj=out, mode="w", format=tarfile.GNU_FORMAT) as t:
        for m, blob in payloads:
            if m.isfile() and m.name.endswith("/root"):
                blob = pad_root(blob)
                m.size = len(blob)
                hit = True
            t.addfile(m, io.BytesIO(blob) if blob is not None else None)
    if not hit:
        raise SystemExit("isolate-badblock: no root member in sysupgrade tar")
    with open(path, "wb") as f:
        f.write(out.getvalue())

if __name__ == "__main__":
    main(sys.argv[1])
