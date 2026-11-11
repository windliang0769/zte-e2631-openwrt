#!/bin/bash
# Get a clean Linux box to the point where `make world` can succeed: base tree at
# the pinned commit, the E2631 overlay applied, the two feeds that matter
# installed, and the seed .config in place.
#
#   bash scripts/bootstrap.sh          # or: make bootstrap
#
# Idempotent: every step checks before it acts, so re-running after a failed
# build costs seconds. It never deletes a tree - if something looks wrong it
# stops and says what.
set -eu
cd "$(dirname "$0")/.."
. scripts/env.sh

say() { printf 'bootstrap: %s\n' "$*"; }
die() { printf 'bootstrap: %s\n' "$*" >&2; exit 1; }

e2631_need_cmd git "clone the base tree and the feeds" || exit 1
e2631_need_cmd make "drive the OpenWrt build" || exit 1
e2631_need_cmd gcc "build the toolchain (build-essential)" || exit 1
e2631_need_cmd g++ "build some host tools (build-essential)" || exit 1
e2631_need_cmd file "identify the built payloads" || exit 1

# ---- 1. the base tree --------------------------------------------------------
# A full clone, not a shallow one: the port is pinned to a commit that may not be
# the branch head, and a shallow clone cannot check out an arbitrary SHA.
if [ ! -d "$E2631_TREE" ]; then
	say "cloning $E2631_BASE_REPO#$E2631_BASE_SHA into $E2631_TREE"
	git clone --branch "$E2631_BASE_BRANCH" "$E2631_BASE_REPO" "$E2631_TREE" ||
		die "clone failed - network, or the branch moved? $E2631_BASE_REPO can be overridden in .env"
fi
[ -d "$E2631_TREE/target/linux/sanechips" ] ||
	die "no sanechips target in $E2631_TREE - that is not the base tree this kit overlays (set E2631_TREE, or remove the directory and re-run)"

if ! git -C "$E2631_TREE" rev-parse --verify "$E2631_BASE_SHA^{commit}" >/dev/null 2>&1; then
	say "fetching $E2631_BASE_BRANCH (the pinned commit is not in this clone yet)"
	git -C "$E2631_TREE" fetch origin "$E2631_BASE_BRANCH" ||
		die "cannot fetch $E2631_BASE_SHA - it must be reachable from $E2631_BASE_BRANCH"
fi
if [ "$(git -C "$E2631_TREE" rev-parse HEAD)" != "$E2631_BASE_SHA" ]; then
	if [ -n "$(git -C "$E2631_TREE" status --porcelain)" ]; then
		die "$E2631_TREE is at $(git -C "$E2631_TREE" rev-parse --short HEAD), not the pinned $E2631_BASE_SHA, and has uncommitted changes.
       Look at 'git -C $E2631_TREE status' first: if those edits are yours, point E2631_TREE at a
       fresh clone instead of overwriting them. If they are only a stale copy of this overlay,
       reset the tree to the pinned commit and re-run."
	fi
	say "checking out the pinned commit $E2631_BASE_SHA"
	git -C "$E2631_TREE" checkout -q "$E2631_BASE_SHA"
fi

# ---- 2. the overlay ----------------------------------------------------------
say "applying the E2631 overlay to $E2631_TREE"
bash "$E2631_KIT/apply.sh" "$E2631_TREE"

# The two edits apply.sh must leave behind, checked here rather than trusted:
# both are what make the image boot on this board instead of the H3600.
grep -qx 'CONFIG_CMDLINE_FORCE=y' "$E2631_TREE/target/linux/sanechips/config-6.18" ||
	die "apply.sh left no forced kernel command line in $E2631_TREE - the vendor bootargs would win"
grep -q 'Makefile.e2631' "$E2631_TREE/target/linux/sanechips/image/Makefile" ||
	die "apply.sh did not hook image/Makefile - the E2631 device definition would be invisible"

# ---- 3. feeds ---------------------------------------------------------------
# Only packages and luci. Two traps, both hit in practice: a hand-cloned feed has
# no feeds/<name>.index and `feeds install` then says "Ignoring feed 'luci'" -
# the build still succeeds, just without LuCI, silently; and a killed half-clone
# leaves a shallow repo whose next pull re-fetches the full history.
for f in packages luci; do
	[ -d "$E2631_TREE/feeds/$f/.git" ] || {
		say "cloning feed $f"
		git clone -q --depth 1 --single-branch "https://github.com/openwrt/$f" "$E2631_TREE/feeds/$f"
	}
	[ -f "$E2631_TREE/feeds/$f.index" ] || {
		# The feed HEAD drifts and occasionally carries broken package
		# Makefiles that abort "Collecting package info" (seen live:
		# pdns-recursor, then dufs).  None of them are selected by this
		# image, so parse the failing package out of the scan log, drop it
		# and retry, instead of hand-maintaining a blacklist.
		i=0
		while [ "$i" -lt 8 ]; do
			i=$((i+1))
			logf="$E2631_TREE/feeds-$f-scan.log"
			if ( cd "$E2631_TREE" && ./scripts/feeds update "$f" ) >"$logf" 2>&1; then
				say "feed $f indexed clean on scan $i"
				break
			fi
			bad=$(sed -n -e 's|.* see logs/\(feeds/[^ ]*dump\.txt\).*|\1|p' "$logf" | head -1)
			[ -n "$bad" ] || die "feed $f failed the scan for an unknown reason: $(tail -5 "$logf")"
			bad="${bad%/dump.txt}"
			say "dropping broken feed package: $bad"
			rm -rf "$E2631_TREE/$bad"
		done
		[ "$i" -lt 8 ] || die "feed $f still failing after dropping 8 packages"
	}
done
say "installing feed packages"
( cd "$E2631_TREE" && ./scripts/feeds install -a 2>&1 | grep -E "Ignoring|Installing" | tail -8 )
[ -d "$E2631_TREE/package/feeds/packages" ] || die "feed install produced no package/feeds/packages"

# ---- 3b. the luci-base ucode plugin: reference-router getTempInfo ----------
# The RAX1800-Z user expects the exact upstream router command
# `ubus call luci getTempInfo` returning {"tempinfo": "CPU: ..."}.
# The luci object lives in luci-base's ucode rpcd plugin; the luci feed is
# cloned from upstream master WITHOUT a pin, so replacing the whole file
# would go stale on feed drift. Instead, append a method registration to
# the existing methods object right before the plugin's contract exit line
# `return { luci: methods };` - the file's stable shape - and refuse to
# blind-patch when that line is missing or duplicated.  This runs HERE, after
# the feeds exist (apply.sh runs before the feeds are cloned, and its first
# attempt there was skipped silently, which the build then caught).
LUCI_UCODE="$E2631_TREE/feeds/luci/modules/luci-base/root/usr/share/rpcd/ucode/luci"
GETTEMP_SNIPPET="$E2631_KIT/overlay/feeds-luci/getTempInfo.uc"
if [ -f "$LUCI_UCODE" ] && [ -f "$GETTEMP_SNIPPET" ]; then
	if ! grep -q 'getTempInfo' "$LUCI_UCODE"; then
		n=$(grep -c '^return { luci: methods };$' "$LUCI_UCODE" || true)
		[ "$n" = 1 ] ||
			die "luci ucode plugin: expected exactly one 'return { luci: methods };' exit line, found ${n:-0} - refusing to blind-patch"
		{
			grep -v '^return { luci: methods };$' "$LUCI_UCODE"
			echo ""
			cat "$GETTEMP_SNIPPET"
			echo ""
			echo 'return { luci: methods };'
		} > "$LUCI_UCODE.tmp"
		mv "$LUCI_UCODE.tmp" "$LUCI_UCODE"
		say "luci getTempInfo: method appended to the luci-base ucode plugin"
	fi
fi
grep -q 'methods.getTempInfo' "$LUCI_UCODE" 2>/dev/null ||
	die "luci getTempInfo did not land in the luci-base ucode plugin - LuCI would lose the reference temperature API"

# ---- 4. the seed config ----------------------------------------------------
say "seeding .config from seed/e2631.config"
cp "$E2631_KIT/seed/e2631.config" "$E2631_TREE/.config"
( cd "$E2631_TREE" && make defconfig >/dev/null 2>&1 )
# The first expansion can demote seed =y lines to =m: kconfig resolves the
# bare fragment's dependency edges against defaults on that pass (measured
# with the v1.9.1 plugin set: every baked =y came back =m, while the same =y
# re-asserted on the expanded config survived).  Re-assert the seed's own
# CONFIG lines over the expanded result and expand once more.
awk -F= '
	FILENAME ~ /seed/ { if ($0 ~ /^CONFIG_/) want[$1] = $0; next }
	{
		line = $0
		split(line, a, "=")
		if (a[1] in want) line = want[a[1]]
		print line
	}
' "$E2631_KIT/seed/e2631.config" "$E2631_TREE/.config" > "$E2631_TREE/.config.reseed"
mv "$E2631_TREE/.config.reseed" "$E2631_TREE/.config"
( cd "$E2631_TREE" && make defconfig >/dev/null 2>&1 )
for want in CONFIG_TARGET_DEVICE_sanechips_generic_DEVICE_cmcc_rax1800-z= \
	CONFIG_SOFT_FLOAT= CONFIG_PACKAGE_luci=; do
	grep -q "^$want" "$E2631_TREE/.config" || die "seed check: $want missing after defconfig"
done
say "seed ok: E2631 device selected, soft-float on, LuCI and the slot tool in the image"

say "done. next: bash scripts/build.sh   (about 40 minutes on 2 cores, then it packs)"
