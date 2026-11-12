#!/bin/sh
# sysupgrade platform glue for the CMCC RAX1800-Z (ZX279128R, parallel NAND).
#
# WHY THIS IS NOT THE E2631 platform.sh
# -------------------------------------
# The upstream E2631 port this tree is forked from stores two whole-system
# 28 MiB "slots" and rewrites the running one via `e2631-fwslot`.  That model
# does not describe this board.  Verified live over TTL (/proc/mtd + sysfs):
#
#     mtd2 "kernel"  @ 0x00200000  5 MiB   writable  -- slot A / OpenWrt
#     mtd3 "rootfs"  @ 0x00c00000  32 MiB  writable  -- slot A / OpenWrt
#     mtd4 "kernel1-stock" @ 0x00700000  5 MiB   READ-ONLY (flags=0x0)
#     mtd5 "rootfs1-stock" @ 0x02c00000  32 MiB  READ-ONLY (flags=0x0)
#
# There are no "slot0"/"slot1" labels and no mtd covering a whole slot: on
# this board a slot is TWO split partitions with the other slot's kernel in
# between.  `e2631-fwslot info` returns "running slot: -1" here, so calling it
# would only ever reject the flash.  We write the two partitions directly.
#
# THE 480-BYTE HEADER (the reason a bare kernel write would not boot)
# ------------------------------------------------------------------
# Vendor U-Boot boots with `bootm 0x440001e0`: it reads the whole kernel
# partition to 0x44000000 and starts 0x1e0 (480) bytes in, where the uImage
# begins.  The first 480 bytes are a ZTE version header.  OpenWrt's sysupgrade
# `kernel` member is a BARE uImage (magic 0x27051956 at offset 0).  Writing it
# verbatim to mtd2 makes U-Boot boot from the middle of the uImage -> no boot.
# So we PREPEND /lib/upgrade/rax1800z.verhdr (a verbatim copy of vendor slot A's
# 480-byte header, from the 128 MiB NAND backup) to rebuild the expected layout:
#     [ 0x000 .. 0x1DF ]  ZTE 480-byte header
#     [ 0x1E0 ..        ]  OpenWrt uImage
# U-Boot does not use this header to select the slot (that is BootPara's
# BootImageNum in mtd8/others); it only skips it.  uImage integrity is checked
# by its own built-in hcrc/dcrc.  The vendor header is therefore safe to reuse
# byte-for-byte with a different, larger uImage.
#
# KERNEL COMMAND LINE
# -------------------
# The kernel is compiled with CONFIG_CMDLINE_FORCE and root=/dev/mtdblock3
# (mtd3, slot A rootfs); apply.sh / verify_img.mjs assert this.  Once U-Boot
# loads slot A's kernel, the kernel mounts slot A's rootfs regardless of the
# vendor bootargs (which name mtdblock6).  First NAND boot therefore needs
# BootPara BootImageNum=0 (point at slot A kernel) -- a one-time step done by
# the first-install flow, NOT by sysupgrade.
#
# SAFETY
# ------
# Slot B (mtd4/mtd5) is HARDWARE read-only and is never a write target; the
# vendor system there is the permanent rollback.  U-Boot code (mtd0
# 0x0..0x180000) is never written, so the TTL rescue console always survives.
# Writes are ordered rootfs-first, kernel-last (the kernel is the boot switch,
# so the shortest possible window has a half-written kernel).  Both partitions
# are read back with `mtd verify` before sysupgrade is allowed to reboot.  A
# failed/interrupted upgrade can at worst soft-brick slot A; the board still
# reaches U-Boot, from where the vendor slot or a full NAND image is restored.
# This board's U-Boot has no proven automatic other-slot fallback, so we rely
# on ordering + verification + the untouched vendor slot, not on a rollback we
# cannot prove.
#
# UPGRADE PACKAGE
# ---------------
# The stock OpenWrt `*-squashfs-sysupgrade.bin` this tree already builds is a
# TAR containing exactly CONTROL + `kernel` (bare uImage) + `root` (squashfs
# "hsqs").  No factory .bin and no e2631-fwslot are required.

. /lib/functions.sh

# SELF-CONTAINED HELPERS
# ----------------------
# Both entry points must work:
#   * image validation from /sbin/sysupgrade -> ... -> do_stage2, which does
#     `include /lib/upgrade` (so common.sh defines get_image/v), and
#   * /sbin/e2631-upgrade verify(), which sources THIS FILE DIRECTLY without
#     common.sh -- there get_image/v would otherwise be "not found" and every
#     online-upgrade check would fail.
# Define local, unambiguously-named fallbacks and use them below so we never
# depend on load order.  They mirror the on-device common.sh implementations
# (verified over TTL: get_image at common.sh:84, `1f8b` -> busybox zcat).
ra_get_image() { # <file> -> decompressed tar stream on stdout
	local from=$1 magic
	magic=$(dd if="$from" bs=2 count=1 2>/dev/null | busybox hexdump -n 2 -e '1/1 "%02x"' 2>/dev/null)
	case "$magic" in
		1f8b) busybox zcat "$from" ;;
		*)    cat "$from" ;;
	esac
}

ra_v() { echo "sysupgrade: $*"; }

# Copy these into the sysupgrade ramfs (all confirmed present on-device).
# /sbin/mtd is the mtd-utils binary (not a busybox applet), so it must be
# copied explicitly; tar/dd/cat/sha256sum are busybox applets already present
# in the stage2 ramfs but listing them is harmless and guards minimal ramfs.
RAMFS_COPY_BIN="/sbin/mtd /bin/tar /usr/bin/sha256sum /bin/dd /bin/cat"
RAMFS_COPY_DATA="/lib/upgrade/rax1800z.verhdr"

RA_VERHDR=/lib/upgrade/rax1800z.verhdr
RA_TARDIR=/tmp/rax1800z-sysupgrade

# All tool calls below use ONLY applets verified present on this board.
# NOTE: `od`, `stat`, `hexdump` are NOT available as standalone commands; use
# `busybox hexdump` and `wc -c` instead.  `mtd verify` and `mtd write` exist.

# First 4 bytes of a file as lowercase hex (e.g. 27051956).
ra_magic() { busybox hexdump -v -e '1/1 "%02x"' -n 4 "$1" 2>/dev/null; }

ra_size() { wc -c < "$1" | tr -d ' \n'; }

# Patch a little-endian u32 at <offset> inside <file> in place.  Used to
# update the vendor version-header length fields for a NEW uImage size.
ra_patch_le32() { # <file> <offset_dec> <value_dec>
	local f=$1 off=$2 v=$3
	printf "$(printf '\\x%02x\\x%02x\\x%02x\\x%02x' \
		$(( v        & 255 )) \
		$(( v >> 8   & 255 )) \
		$(( v >> 16  & 255 )) \
		$(( v >> 24  & 255 )))" \
		| dd of="$f" bs=1 seek="$off" conv=notrunc 2>/dev/null
}

# Size of an mtd partition by label, in bytes (decimal, from sysfs).
ra_mtd_size() {
	local label=$1 m
	for m in /sys/class/mtd/mtd[0-9]*; do
		[ "$(cat "$m/name" 2>/dev/null)" = "$label" ] || continue
		cat "$m/size" 2>/dev/null
		return 0
	done
	return 1
}

# Extract one member of the sysupgrade tar to stdout.  We first resolve the
# full stored path with `tar -tf` (the image builder wraps members in one
# top-level directory), then extract by that exact path.
# Usage: ra_tar_member <tar> <basename> > outfile
ra_tar_member() {
	local tar=$1 member=$2 path
	path=$(tar -tf "$tar" 2>/dev/null | grep "/$member\$" | head -1)
	[ -z "$path" ] && path=$(tar -tf "$tar" 2>/dev/null | grep "^$member\$" | head -1)
	[ -n "$path" ] || return 1
	tar -xOf "$tar" "$path" 2>/dev/null
}

ra_unpack() { # ra_unpack <image>  ->  $RA_TARDIR/{kernel,root,img.tar}	local img=$1
	rm -rf "$RA_TARDIR"; mkdir -p "$RA_TARDIR"
	# ra_get_image transparently gunzips a .tar.gz; normalise to a plain tar.
	ra_get_image "$img" > "$RA_TARDIR/img.tar"
	ra_tar_member "$RA_TARDIR/img.tar" kernel > "$RA_TARDIR/kernel" || return 1
	ra_tar_member "$RA_TARDIR/img.tar" root   > "$RA_TARDIR/root"   || return 1
	[ -s "$RA_TARDIR/kernel" ] && [ -s "$RA_TARDIR/root" ] || return 1
	return 0
}

platform_check_image() {
	local img=$1 ks rs kmax rmax
	case "$img" in
	*.bin|*.tar|*.tar.gz|*.tgz) ;;
	*) echo "RAX1800Z: expected a *-squashfs-sysupgrade.bin (tar of kernel+root)"; return 1 ;;
	esac

	[ -f "$RA_VERHDR" ] || { echo "RAX1800Z: missing $RA_VERHDR"; return 1; }
	[ "$(ra_magic "$RA_VERHDR")" = "99999999" ] || {
		echo "RAX1800Z: bad version-header magic"; return 1; }

	ra_unpack "$img" || { echo "RAX1800Z: image is not a kernel+root sysupgrade tar"; return 1; }

	[ "$(ra_magic "$RA_TARDIR/kernel")" = "27051956" ] || {
		echo "RAX1800Z: kernel payload is not a uImage (bad magic)"; return 1; }
	case "$(ra_magic "$RA_TARDIR/root")" in
		68737173|73717368) ;; # hsqs / sqsh
		*) echo "RAX1800Z: root payload is not a squashfs (bad magic)"; return 1 ;;
	esac

	ks=$(ra_size "$RA_TARDIR/kernel"); rs=$(ra_size "$RA_TARDIR/root")
	kmax=$(ra_mtd_size kernel); rmax=$(ra_mtd_size rootfs)
	[ -n "$kmax" ] && [ -n "$rmax" ] || { echo "RAX1800Z: cannot read mtd sizes"; return 1; }
	[ $((ks + 480)) -le "$kmax" ] || { echo "RAX1800Z: kernel ($ks + 480) exceeds mtd2 ($kmax)"; return 1; }
	[ "$rs" -le "$rmax" ] || { echo "RAX1800Z: root ($rs) exceeds mtd3 ($rmax)"; return 1; }

	ra_v "RAX1800Z: image OK (kernel $ks B + 480 B hdr -> mtd2, root $rs B -> mtd3)"
	return 0
}

platform_do_upgrade() {
	local img=$1 kpart rpart full
	# IMPORTANT: use the mtd LABEL, not find_mtd_part().  Measured on this
	# board: find_mtd_part(kernel) returns /dev/mtdblock2, and `mtd write` /
	# `mtd verify` CANNOT open a /dev/mtdblockN device on this parallel NAND
	# ("Could not get MTD device info" + Buffer I/O error).  A bare "mtd3"
	# also fails ("Could not open mtd device").  The mtd-utils tool resolves
	# the partition LABEL to the correct /dev/mtdN character device itself --
	# verified live: `mtd verify /tmp/x rootfs` works, `/dev/mtd3` works,
	# `mtd3` and `/dev/mtdblock3` both fail.  So pass labels verbatim.
	kpart=kernel
	rpart=rootfs
	[ -n "$(ra_mtd_size "$kpart")" ] && [ -n "$(ra_mtd_size "$rpart")" ] || {
		ra_v "RAX1800Z: kernel/rootfs mtd label not found"; exit 1; }

	ra_unpack "$img" || { ra_v "RAX1800Z: unpack failed - nothing written"; exit 1; }
	[ "$(ra_magic "$RA_TARDIR/kernel")" = "27051956" ] || { ra_v "bad kernel magic in ramfs"; exit 1; }

	# Rebuild [480B vendor header][bare uImage] for `bootm 0x440001e0`.
	# CRITICAL: the vendor header is the STOCK slot-A header; its field 0x058
	# holds the OLD uImage size, and U-Boot's internal loader computes the
	# NAND read length as mtd_length = [0x058] + 483 (verified live: a 5 MiB
	# kernel with an unpatched header was still read as 0x353de0 bytes).  For
	# any NEW uImage size L0 we must patch:
	#   0x058 = L0            (the length the loader actually uses)
	#   0x064 = L0            (Lr, informational)
	#   0x068 = L0 + 483      (Lt = L0 + header, informational)
	# The CRC fields (0x060/0x06c/0x110) belong to the vendor upgrade tools,
	# which never run on OpenWrt; U-Boot's bootm integrity check uses the
	# uImage's OWN header CRC (inside the uImage, not this wrapper).
	local uimg_size=$RA_TARDIR/kernel size_l0
	uimg_size=$(ra_size "$RA_TARDIR/kernel")
	size_l0=$(( uimg_size + 0 ))
	cp "$RA_VERHDR" "$RA_TARDIR/verhdr.patched"
	ra_patch_le32 "$RA_TARDIR/verhdr.patched" 88  "$size_l0"          # 0x058
	ra_patch_le32 "$RA_TARDIR/verhdr.patched" 100 "$size_l0"          # 0x064
	ra_patch_le32 "$RA_TARDIR/verhdr.patched" 104 "$(( size_l0 + 483 ))" # 0x068
	full=$RA_TARDIR/kernel.ra
	cat "$RA_TARDIR/verhdr.patched" "$RA_TARDIR/kernel" > "$full"
	ra_v "RAX1800Z: verhdr patched L0=$size_l0 (fields 0x058/0x064/0x068)"

	# Rootfs FIRST, kernel LAST: the kernel is the boot switch, so a power cut
	# leaves the shortest possible window with a half-written kernel.
	ra_v "RAX1800Z: writing rootfs -> $rpart (kernel still bootable)"
	mtd write "$RA_TARDIR/root" "$rpart" || { ra_v "rootfs write failed"; exit 1; }
	ra_v "RAX1800Z: writing kernel(480B hdr + uImage) -> $kpart"
	mtd write "$full" "$kpart" || { ra_v "kernel write failed"; exit 1; }

	# Read back both partitions and byte-compare with what we wrote.  `mtd
	# verify` compares exactly imagefile-length bytes, so the untouched tail of
	# the partition (old data / 0xFF) is correctly ignored.
	ra_v "RAX1800Z: verifying writes (mtd verify read-back)"
	mtd verify "$RA_TARDIR/root" "$rpart" || { ra_v "RAX1800Z: rootfs verify MISMATCH - do not reboot"; exit 1; }
	mtd verify "$full" "$kpart" || { ra_v "RAX1800Z: kernel verify MISMATCH - do not reboot"; exit 1; }
	ra_v "RAX1800Z: both partitions verified; slot B (mtd4/mtd5) untouched; safe to reboot"
}

# Config is deliberately NOT preserved (matches the E2631 port's decision and
# the download-based backup workflow in the LuCI page).  A clean squashfs boot
# also guarantees compiled-in WiFi defaults take effect untouched.
platform_copy_config() {
	true
}
