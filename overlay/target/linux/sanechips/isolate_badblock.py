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
import zlib

EB = 0x20000      # NAND erase block size (128 KiB)
PAD = 0x100000    # 1 MiB skip past the marginal block

# Vendor kernel header (480 bytes) template baked into the rootfs.  At
# runtime platform.sh used to patch the three length fields (0x058/0x064/
# 0x068) for the actual uImage size but could NOT recompute the header CRC
# at 0x110 (crc32(head[0x14:0x110])) in the sysupgrade ramfs -- no python /
# zlib there.  U-Boot's do_mcupg then rejected the image with
# "header crc error" and fell back to the (stale) stock slot.  Since this
# build step is Python, prepare a fully patched header here and ship it as
# an extra tar member; platform.sh uses it verbatim, no runtime CRC needed.
def make_verhdr(kernel_size, template_path):
    hdr = bytearray(open(template_path, "rb").read())
    if len(hdr) != 480 or hdr[:4] != b"\x99\x99\x99\x99":
        raise SystemExit("isolate-badblock: bad verhdr template")
    struct.pack_into("<I", hdr, 0x058, kernel_size)         # L0 (loader length)
    struct.pack_into("<I", hdr, 0x064, kernel_size)         # Lr
    struct.pack_into("<I", hdr, 0x068, kernel_size + 483)   # Lt
    struct.pack_into("<I", hdr, 0x110,
                     zlib.crc32(hdr[0x14:0x110]) & 0xffffffff)
    print("isolate-badblock: verhdr.ready L0=%d crc=0x%08x"
          % (kernel_size, struct.unpack_from("<I", hdr, 0x110)[0]))
    return bytes(hdr)

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

def main(path, template_path):
    with tarfile.open(path) as t:
        members = t.getmembers()
        payloads = [(m, t.extractfile(m).read() if m.isfile() else None)
                    for m in members]
    out = io.BytesIO()
    hit = root_hit = False
    kernel_size = None
    prefix = None
    with tarfile.open(fileobj=out, mode="w", format=tarfile.GNU_FORMAT) as t:
        for m, blob in payloads:
            if m.isfile() and m.name.endswith("/root"):
                blob = pad_root(blob)
                m.size = len(blob)
                root_hit = True
            if m.isfile() and m.name.endswith("/kernel"):
                kernel_size = len(blob)
                prefix = m.name.rsplit("/", 1)[0]
            if m.isfile() and m.name.endswith("/CONTROL"):
                hit = True
            t.addfile(m, io.BytesIO(blob) if blob is not None else None)
        if kernel_size is None or prefix is None:
            raise SystemExit("isolate-badblock: no kernel member in sysupgrade tar")
        # Add a fully pre-patched vendor header (length fields + CRC).  This
        # avoids needing zlib in the sysupgrade ramfs at runtime.
        vh = make_verhdr(kernel_size, template_path)
        vm = tarfile.TarInfo(prefix + "/verhdr.ready")
        vm.size = len(vh)
        vm.mode = 0o644
        t.addfile(vm, io.BytesIO(vh))
    if not root_hit or not hit:
        raise SystemExit("isolate-badblock: tar missing root or CONTROL member")
    with open(path, "wb") as f:
        f.write(out.getvalue())

if __name__ == "__main__":
    # argv[2] = path to the 480-byte vendor header template in the build tree
    main(sys.argv[1], sys.argv[2])
