"""Exercise device upgrade control flow with local fixtures; never access MTD.

Adapted to the CMCC RAX1800-Z native sysupgrade adapter (v19).  This board has
no E2631 28 MiB whole-system slots: OpenWrt lives in two split partitions
(mtd2 kernel + mtd3 rootfs, "slot A"), while the vendor system (mtd4/mtd5,
"slot B") is hardware read-only and is never a write target.

The two things under test, both with every MTD/mtd-utils call stubbed out:

  * lib/upgrade/platform.sh
      - platform_check_image(): accepts only a kernel+root sysupgrade TAR,
        checks the 480-byte version header magic, the kernel uImage magic,
        the root squashfs magic, and both partition sizes (from sysfs).
      - platform_do_upgrade(): prepends the 480-byte header to the bare
        uImage, writes rootfs FIRST / kernel LAST by mtd LABEL, then reads
        both back with `mtd verify` before returning success (which is what
        lets sysupgrade reboot).  A failed write or verify aborts.
  * sbin/e2631-upgrade
      - download/sha256/manifest/anti-downgrade flow (unchanged), with the
        package-format check now delivered by platform_check_image().

Shell portability (IMPORTANT, this cost a CI run to learn):
  * The script is written to a FILE and run as `sh <file>` / `busybox ash
    <file>`, never via `sh -c <string>`.  Passing a multi-line script as a
    single argv element gets its quotes/backslashes mangled by MSYS2 argv
    re-parsing on the Windows dev host (syntax error: unexpected EOF).
  * TEST_DIR is converted to an MSYS path (`C:\\x` -> `/c/x`).  GNU tar reads
    a bare `C:/x` as `host:path` ("Cannot connect to C: resolve failed").
  * CI installs busybox and runs under `busybox ash`, so the real hexdump/zcat
    applets exist.  The Windows dev host has no busybox.  A probe checks the
    real applet's exact outputs (4-byte `-v` file form, 2-byte stdin form);
    only when they differ does a `busybox()` shim in terms of `od`/`gzip` get
    defined.  The upshot: the REAL platform.sh helpers (ra_magic, ra_get_image,
    ra_tar_member, ra_unpack) run unchanged in both environments, and the
    harness is deterministic whether busybox is correct, wrong-format, or
    absent.
  * Only genuinely board-specific dependencies are stubbed: `ra_mtd_size`
    (reads /sys/class/mtd, absent off-device) and `mtd` (talks to hardware).

All shell snippets run under `busybox ash` when available (as on the board and
in CI), falling back to the system `sh` for local development.  Nothing here
ever touches a real MTD device.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import io
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'overlay/target/linux/sanechips/base-files'
UPDATER = (BASE / 'sbin/e2631-upgrade').read_text().split('\ncase "${1:-}" in\n')[0]
# Source the platform glue without depending on /lib/functions.sh (its only
# top-level side effect); all helpers it defines are pure shell.  Each test
# that needs the harness appends SHIMS + STUBS below AFTER this body so they
# override the board-only definitions (ra_mtd_size / mtd / RA_* paths).
PLATFORM = (BASE / 'lib/upgrade/platform.sh').read_text().replace('. /lib/functions.sh', ':')

# Shell runner: prefer busybox ash (board/CI), fall back to /bin/sh locally.
# No `-c`: the script goes in a file (see module docstring for why).
if shutil.which('busybox'):
    SHELL = ['busybox', 'ash']
else:
    SHELL = ['sh']


def _msys(path):
    """Native path -> Git-Bash/MSYS path, so GNU tar/dd/od accept it."""
    s = str(path).replace('\\', '/')
    if len(s) > 1 and s[1] == ':':          # C:/Users/x -> /c/Users/x
        s = '/' + s[0].lower() + s[2:]
    return s


def run_sh(script, env=None):
    """Run <script> in a temp file under SHELL; return CompletedProcess.

    Writing to a file (rather than `sh -c script`) is what keeps the quoting
    intact on the Windows dev host.  TEST_DIR is rewritten to MSYS form in the
    child environment; on Linux/CI _msys() is a no-op.
    """
    fd, sp = tempfile.mkstemp(prefix='ra-ut-', suffix='.sh')
    try:
        with os.fdopen(fd, 'w', newline='\n', encoding='utf-8') as f:
            f.write(script)
        e = dict(os.environ, **(env or {}))
        if 'TEST_DIR' in e:
            e['TEST_DIR'] = _msys(e['TEST_DIR'])
        return subprocess.run(SHELL + [_msys(sp)], text=True,
                              capture_output=True, env=e)
    finally:
        os.unlink(sp)


# platform.sh calls `busybox hexdump -v -e '1/1 "%02x"' -n 4 FILE` (ra_magic)
# and `busybox hexdump -n 2 -e ...` on stdin (ra_get_image), plus `busybox zcat`.
# Those applets are present on the board and in CI (which installs busybox),
# but absent on the local Windows dev host -- and some distro busybox builds
# format hexdump differently (e.g. with separators or a trailing `*`).  So we
# PROBE every exact call shape platform.sh uses: only if the real applet emits
# the contiguous lowercase hex we expect do we keep using it (on-device / CI
# fidelity); otherwise we define a `busybox()` shim in terms of od/gzip.
#   * probe4 = ra_magic's 4-byte `-v` form (catches multi-byte separators)
#   * probe2 = ra_get_image's 2-byte stdin form, no `-v`
#   * probeF = ra_magic's file-argument form (WXYZ -> 5758595a)
# Probing 4 distinct bytes catches separators a 1-byte probe would miss.  This
# makes the harness deterministic everywhere while still exercising the real
# busybox path wherever it is correct.
SHIMS = r'''
_ra_probe4=$(printf 'ABCD' | busybox hexdump -v -e '1/1 "%02x"' -n 4 2>/dev/null)
_ra_probe2=$(printf 'sy'   | busybox hexdump -n 2 -e '1/1 "%02x"' 2>/dev/null)
_ra_pf="$TEST_DIR/.ra_hexprobe"
printf 'WXYZ' > "$_ra_pf"
_ra_probeF=$(busybox hexdump -v -e '1/1 "%02x"' -n 4 "$_ra_pf" 2>/dev/null)
rm -f "$_ra_pf"
if [ "$_ra_probe4" != "41424344" ] || [ "$_ra_probe2" != "7379" ] || \
   [ "$_ra_probeF" != "5758595a" ]; then
busybox() {
    local sub=$1; shift
    case "$sub" in
        hexdump)
            local n=0 file=
            while [ $# -gt 0 ]; do
                case "$1" in
                    -v) shift ;;
                    -n) n=$2; shift 2 ;;
                    -e) shift 2 ;;
                    -*) shift ;;
                    *)  file=$1; shift ;;
                esac
            done
            if [ -n "$file" ]; then
                od -An -v -tx1 -N"$n" "$file" 2>/dev/null | tr -d ' \n'
            else
                od -An -v -tx1 -N"$n" 2>/dev/null | tr -d ' \n'
            fi
            ;;
        zcat) gzip -dc "$1" ;;
        *)    return 127 ;;
    esac
}
fi
'''

# Board-only dependencies, stubbed AFTER PLATFORM so they override it.
#  * ra_mtd_size -> echoes KMAX for `kernel`, RMAX for `rootfs` (no sysfs)
#  * mtd         -> records write/verify calls, returns $MTD_RC
STUBS = r'''
ra_mtd_size() {
    case "$1" in
        kernel) echo "${KMAX:-5242880}" ;;
        rootfs) echo "${RMAX:-33554432}" ;;
        *) return 1 ;;
    esac
}
RA_VERHDR="$TEST_DIR/verhdr"
RA_TARDIR="$TEST_DIR/unpack"
mtd() {
    echo "$*" >> "$TEST_DIR/mtd.calls"
    return "${MTD_RC:-0}"
}
'''


def make_sysupgrade_tar(kernel_magic=b'\x27\x05\x19\x56', root_magic=b'hsqs',
                        kernel_len=None, root_len=None):
    """Build a minimal in-memory *-squashfs-sysupgrade.bin (tar of kernel+root).

    kernel is a bare uImage (magic 0x27051956 at offset 0), root is a squashfs
    ('hsqs').  Sizes default to small fixtures; explicit lengths let a test
    exercise the partition-capacity rejection without huge files.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.USTAR_FORMAT) as tf:
        d = 'sysupgrade-cmcc_rax1800-z/'
        kdir = tarfile.TarInfo(d)
        kdir.type = tarfile.DIRTYPE
        kdir.mode = 0o755
        tf.addfile(kdir)
        for name, magic, size in (
            (d + 'CONTROL', b'{}\n', None),
            (d + 'kernel', kernel_magic, kernel_len),
            (d + 'root', root_magic, root_len),
        ):
            payload = magic + b'\x00' * (max(0, (size or 0) - len(magic)))
            ti = tarfile.TarInfo(name)
            ti.size = len(payload)
            ti.mtime = 0
            ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(payload))
    return buf.getvalue()


class DownloadTests(unittest.TestCase):
    """e2631-upgrade download/sha256/manifest/anti-downgrade flow."""
    # These are identical in spirit across ports; verify()'s format check is
    # stubbed with a `package_ok` switch because the real one delegates to
    # platform_check_image(), which the PlatformTests cover directly.  The
    # source of the real platform.sh is neutralised to ':' because /lib does
    # not exist on the test host; the injected stub checker is what runs.

    def download(self, manifest, *, fetch_ok=True, package_ok=True):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'asset').write_bytes(b'firmware fixture')
            (path / 'manifest').write_text(manifest)
            (path / 'version').write_text('OpenWrt v1.8.0\n')
            script = ('uci() { return 1; }\n' + UPDATER + '''
platform_check_image() {
    echo checked >> "$TEST_DIR/checked"
    [ "${PACKAGE_OK:-1}" = 1 ]
}
''')
            script = script.replace('. /lib/upgrade/platform.sh 2>/dev/null', ':')
            script = script.replace('/etc/.e2631-pending-version', '$TEST_DIR/pending')
            script = script.replace('/etc/e2631-version', '$TEST_DIR/version')
            script += '''
DLDIR="$TEST_DIR"
latest() { echo "v1.9 https://github.com/owner/repo/releases/download/v1.9/image.bin"; }
fetch() {
    case "$1" in
        */SHA256SUMS) [ "$FETCH_OK" = 1 ] && cp "$TEST_DIR/manifest" "$2" ;;
        *) cp "$TEST_DIR/asset" "$2" ;;
    esac
}
do_download
'''
            result = run_sh(script, dict(TEST_DIR=tmp, FETCH_OK=str(int(fetch_ok)),
                                         PACKAGE_OK=str(int(package_ok))))
            return result, (path / 'checked').exists(), (path / 'pending').exists()

    def test_exact_and_binary_entries(self):
        digest = hashlib.sha256(b'firmware fixture').hexdigest()
        for name in ('image.bin', '*image.bin'):
            with self.subTest(name=name):
                result, checked, pending = self.download(digest + '  ' + name + '\n')
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertTrue(checked and pending)

    def test_reject_incomplete_or_ambiguous_manifest(self):
        digest = hashlib.sha256(b'firmware fixture').hexdigest()
        cases = ['', digest + '  other-image.bin\n', 'x' * 64 + '  image.bin\n',
                 digest + '  image.bin\n' + digest + '  image.bin\n',
                 '0' * 64 + '  image.bin\n']
        for manifest in cases:
            with self.subTest(manifest=manifest):
                result, checked, pending = self.download(manifest)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(checked or pending)

    def test_refuse_downgrade(self):
        digest = hashlib.sha256(b'firmware fixture').hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'asset').write_bytes(b'firmware fixture')
            (path / 'manifest').write_text(digest + '  image.bin\n')
            (path / 'version').write_text('OpenWrt v1.8.0\n')
            script = 'uci() { return 1; }\n' + UPDATER
            script = script.replace('. /lib/upgrade/platform.sh 2>/dev/null', ':')
            script = script.replace('/etc/.e2631-pending-version', '$TEST_DIR/pending')
            script = script.replace('/etc/e2631-version', '$TEST_DIR/version')
            script += '''
platform_check_image() { return 0; }
DLDIR="$TEST_DIR"
latest() { echo "v1.3 https://github.com/owner/repo/releases/download/v1.3/image.bin"; }
fetch() { cp "$TEST_DIR/manifest" "$2"; }
do_download
'''
            result = run_sh(script, dict(TEST_DIR=tmp))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('not newer', result.stderr)

    def test_checksum_download_failure(self):
        result, checked, pending = self.download('', fetch_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(checked or pending)

    def test_package_check_still_required(self):
        digest = hashlib.sha256(b'firmware fixture').hexdigest()
        result, checked, pending = self.download(digest + '  image.bin\n',
                                                 package_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(pending)
        self.assertTrue(checked)  # format check ran and rejected the package


class PlatformTests(unittest.TestCase):
    """platform.sh against the RAX1800Z fixed-partition contract, no real MTD."""

    def _harness(self, tmp, *, verhdr_magic='99999999', verhdr_bytes=None,
                 fw_bytes=None, kmax='5242880', rmax='33554432'):
        """Write fixtures + return (script-prefix, child-env).

        Order matters: PLATFORM first (defines the real helpers / entry points),
        then SHIMS (busybox fallback) and STUBS (sysfs/mtd overrides), so the
        real ra_magic/ra_get_image/ra_unpack are exercised but all hardware and
        /sys dependencies are replaced.

        ``verhdr_bytes`` / ``fw_bytes`` let a test override the two fixtures.
        They are written HERE (at the same time as the defaults), never before
        ``check_image``/``do_upgrade`` call this — writing them into tmp first
        would only be clobbered by the default fixtures below.  ``verhdr_bytes``
        may be ``b''`` / ``None``-sentinel to simulate a missing header via
        ``verhdr_present=False``.
        """
        path = Path(tmp)
        if verhdr_bytes is None:
            verhdr_bytes = bytes.fromhex(verhdr_magic) + b'\x00' * (480 - 4)
        if fw_bytes is None:
            fw_bytes = make_sysupgrade_tar()
        (path / 'verhdr').write_bytes(verhdr_bytes)
        (path / 'fw.bin').write_bytes(fw_bytes)
        prefix = PLATFORM + SHIMS + STUBS
        return prefix, dict(KMAX=kmax, RMAX=rmax)

    def check_image(self, tmp, img='fw.bin', *, pre=None, **kw):
        """Run platform_check_image on tmp/<img>.  ``pre`` is an optional shell
        snippet run first (used to delete the verhdr after fixtures are laid
        down), so tests never pre-write files that _harness would overwrite."""
        prefix, extra = self._harness(tmp, **kw)
        pre = (pre or '') + '\n'
        script = prefix + pre + 'platform_check_image "$TEST_DIR/%s"\necho "RC=$?"\n' % img
        return run_sh(script, dict(TEST_DIR=tmp, **extra))

    def do_upgrade(self, tmp, *, mtd_rc='0', **kw):
        prefix, extra = self._harness(tmp, **kw)
        script = prefix + '''
platform_do_upgrade "$TEST_DIR/fw.bin"
echo "RC=$?"
'''
        return run_sh(script, dict(TEST_DIR=tmp, MTD_RC=mtd_rc, **extra))

    def _calls(self, tmp):
        f = Path(tmp) / 'mtd.calls'
        return f.read_text().splitlines() if f.exists() else []

    def test_check_image_accepts_valid_tar(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.check_image(tmp)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            self.assertIn('RC=0', r.stdout)
            self.assertIn('image OK', r.stdout)
            self.assertIn('kernel', r.stdout)

    def test_check_image_rejects_non_tar(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.check_image(tmp, fw_bytes=b'not a tar at all')
            self.assertEqual(r.returncode, 0, r.stderr)  # stub echo sets rc
            self.assertIn('RC=1', r.stdout)
            self.assertIn('not a kernel+root sysupgrade tar', r.stdout)

    def test_check_image_rejects_bad_verhdr_magic(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.check_image(tmp, verhdr_magic='00000000')
            self.assertIn('bad version-header magic', r.stdout)
            self.assertIn('RC=1', r.stdout)

    def test_check_image_rejects_missing_verhdr(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.check_image(tmp, pre='rm -f "$TEST_DIR/verhdr"')
            self.assertIn('missing', r.stdout)
            self.assertIn('RC=1', r.stdout)

    def test_check_image_rejects_bad_kernel_magic(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.check_image(tmp, fw_bytes=make_sysupgrade_tar(
                kernel_magic=b'\xde\xad\xbe\xef'))
            self.assertIn('kernel payload is not a uImage', r.stdout)
            self.assertIn('RC=1', r.stdout)

    def test_check_image_rejects_bad_root_magic(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.check_image(tmp, fw_bytes=make_sysupgrade_tar(
                root_magic=b'XXXX'))
            self.assertIn('root payload is not a squashfs', r.stdout)
            self.assertIn('RC=1', r.stdout)

    def test_check_image_rejects_kernel_too_big(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Shrink the stubbed mtd2 limit below kernel(4 B)+480 B header
            # instead of allocating a 5 MiB fixture: same `exceeds mtd2`
            # branch, no big file.
            r = self.check_image(tmp, kmax='10')
            self.assertIn('exceeds mtd2', r.stdout)
            self.assertIn('RC=1', r.stdout)

    def test_check_image_rejects_root_too_big(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Likewise shrink stubbed mtd3 below the 4-byte root fixture.
            r = self.check_image(tmp, rmax='2')
            self.assertIn('exceeds mtd3', r.stdout)
            self.assertIn('RC=1', r.stdout)

    def test_check_image_rejects_bad_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.check_image(tmp, img='fw.txt',
                                 pre='cp "$TEST_DIR/fw.bin" "$TEST_DIR/fw.txt"')
            self.assertIn('expected a *-squashfs-sysupgrade.bin', r.stdout)
            self.assertIn('RC=1', r.stdout)

    def test_do_upgrade_writes_rootfs_first_kernel_last_by_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.do_upgrade(tmp)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            self.assertIn('RC=0', r.stdout)
            calls = self._calls(tmp)
            # 4 mtd calls: write rootfs, write kernel, verify rootfs, verify kernel
            self.assertEqual(len(calls), 4, calls)
            self.assertTrue(calls[0].startswith('write ') and calls[0].endswith(' rootfs'), calls)
            self.assertTrue(calls[1].startswith('write ') and calls[1].endswith(' kernel'), calls)
            self.assertTrue(calls[2].startswith('verify ') and calls[2].endswith(' rootfs'), calls)
            self.assertTrue(calls[3].startswith('verify ') and calls[3].endswith(' kernel'), calls)
            # mtd LABEL, never /dev/mtdblockN (fails on this parallel NAND)
            joined = ' '.join(calls)
            self.assertNotIn('mtdblock', joined)
            # the kernel image actually written has the 480-byte header
            # prepended to the bare uImage (uImage magic at offset 0x1E0).
            written = calls[1].split()[1]
            blob = (Path(tmp) / 'unpack' / Path(written).name).read_bytes()
            self.assertEqual(len(blob), 484)  # 480-byte vendor hdr + 4-byte fixture kernel
            self.assertEqual(blob[:4].hex(), '99999999')
            self.assertEqual(blob[0x1e0:0x1e4].hex(), '27051956')

    def test_do_upgrade_aborts_on_verify_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            # make verify fail; platform_do_upgrade must exit 1 and NOT reach
            # the "safe to reboot" line.
            prefix, extra = self._harness(tmp)
            script = prefix + '''
mtd() {
    echo "$*" >> "$TEST_DIR/mtd.calls"
    case "$1" in verify) return 1 ;; *) return 0 ;; esac
}
platform_do_upgrade "$TEST_DIR/fw.bin"
echo "RC=$?"
'''
            r = run_sh(script, dict(TEST_DIR=tmp, **extra))
            self.assertNotIn('RC=0', r.stdout)
            self.assertNotIn('safe to reboot', r.stdout)
            self.assertIn('MISMATCH', r.stdout)
            calls = ' '.join(self._calls(tmp))
            self.assertIn('verify', calls)

    def test_do_upgrade_aborts_on_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            # mtd write fails on the FIRST call (rootfs): nothing verified,
            # no reboot, no kernel write.
            prefix, extra = self._harness(tmp)
            script = prefix + '''
mtd() {
    echo "$*" >> "$TEST_DIR/mtd.calls"
    case "$1" in write) return 1 ;; *) return 0 ;; esac
}
platform_do_upgrade "$TEST_DIR/fw.bin"
echo "RC=$?"
'''
            r = run_sh(script, dict(TEST_DIR=tmp, **extra))
            self.assertIn('rootfs write failed', r.stdout)
            self.assertNotIn('safe to reboot', r.stdout)
            self.assertNotIn('RC=0', r.stdout)

    def test_do_upgrade_never_touches_slot_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.do_upgrade(tmp)
            self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
            calls = ' '.join(self._calls(tmp))
            for ro_label in ('kernel1-stock', 'rootfs1-stock', 'u-boot',
                             'mtd4', 'mtd5', 'mtd0'):
                self.assertNotIn(ro_label, calls)


if __name__ == '__main__':
    unittest.main(verbosity=2)
