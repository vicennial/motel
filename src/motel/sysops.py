"""Everything that touches devices, mounts and processes sits behind this small interface.

Tests replace it with ``motel.testing.FakeSysOps`` so they never need root. Commands are run with absolute
paths, a fixed environment and no shell.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Protocol

from motel.errors import Busy, Guard, SystemFault

GIB = 1024 ** 3
EDGE_BYTES = 4 * 1024 * 1024  # how much of each end of a new volume must read as zeros before it is formatted
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
MOUNT_OPTIONS = "nodev,nosuid,noatime,discard"


class CommandFailed(SystemFault):
    """A system command exited non-zero; ``returncode`` lets a caller tell "nothing found" from a real failure."""

    returncode = -1
    stderr: str | None = None  # the command's error output, when the runner kept it


def _unescape(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text: str) -> list:
    """/proc/PID/mountinfo -> [(major:minor, mount point)]."""
    out = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 5:
            out.append((fields[2], _unescape(fields[4])))
    return out


def mounts_at_or_under(entries: list, path: str) -> list:
    base = path.rstrip("/") or "/"
    prefix = base.rstrip("/") + "/"
    return sorted({mp for _, mp in entries if mp == base or mp.startswith(prefix)})


_O_PATH = getattr(os, "O_PATH", None)


def resolve_ancestry(root_path: str, target: str) -> list | None:
    """(device, inode) of the root and of every component of ``target`` below it, or None.

    ``root_path`` is ``/proc/PID/root`` and ``target`` the path that process sees (so it may be a path from another
    mount namespace). Each component is opened relative to its parent with O_PATH|O_NOFOLLOW: no symlink is followed,
    ``..`` is refused and no data of the file is touched. None means "cannot be resolved here".
    """
    if not target.startswith("/") or _O_PATH is None:  # O_PATH is Linux-only: elsewhere nothing can be placed
        return None
    flags = _O_PATH | os.O_CLOEXEC
    try:
        fd = os.open(root_path, flags | os.O_DIRECTORY)
    except OSError:
        return None
    idents = []
    try:
        st = os.fstat(fd)
        idents.append((st.st_dev, st.st_ino))
        for part in target.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                return None
            try:
                nxt = os.open(part, flags | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                return None
            os.close(fd)
            fd = nxt
            st = os.fstat(fd)
            idents.append((st.st_dev, st.st_ino))
        return idents
    except OSError:
        return None
    finally:
        os.close(fd)


def fdinfo_mnt_id(path: str) -> int | None:
    """The ``mnt_id:`` of a /proc/PID/fdinfo/N file (kernel text; reading it never touches the file's filesystem)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("mnt_id:"):
                    return int(line.split(":", 1)[1])
    except (OSError, ValueError):
        pass
    return None


def mount_table(mountinfo_path: str) -> list | None:
    """[(mount id, 'major:minor', mount point)] in file order from a mountinfo file, or None if unreadable.
    Mount points are relative to that process's root, like the paths its /proc links show."""
    try:
        with open(mountinfo_path, encoding="utf-8", errors="replace") as handle:
            rows = [line.split() for line in handle]
    except OSError:
        return None
    return [(int(f[0]), f[2], _unescape(f[4])) for f in rows if len(f) >= 5 and f[0].isdigit()]


def device_of_path(table: list, target: str) -> str | None:
    """'major:minor' of the mount that holds ``target`` (a path as the process sees it): the longest mount point
    that is a prefix, the later entry winning a tie (an over-mount). None when it cannot be told."""
    if not target.startswith("/"):
        return None  # "(unreachable)/..." and the like
    if target.endswith(" (deleted)"):
        target = target[:-len(" (deleted)")]
    best = None
    for _, mm, mp in table:
        base = mp.rstrip("/")
        if (mp == "/" or target == mp or target.startswith(base + "/")) and (best is None or len(mp) >= len(best[0])):
            best = (mp, mm)
    return best[1] if best else None


def scan_holders(proc_root: str, path: str, floor_mp: str | None = None, *, stat_fn=os.stat,
                 resolve_fn=resolve_ancestry) -> list:
    """'pid comm' of processes holding the filesystem of a floor open. Never kills anything.

    Evidence is by DEVICE: a process counts when its cwd, root, exe, an open fd or a mapping lives on the
    floor's filesystem, whatever path it sees (containers, chroots and other mount namespaces see other paths).
    - Floor level (``floor_mp`` is None, ``path`` is the mount point): any such reference is a hit.
    - Room level (``path`` is the room directory, ``floor_mp`` the floor's mount point): the reference is placed
      by identity. A path that leads to the object the process holds is judged by name. Otherwise (another mount
      namespace) the path is walked below ``/proc/PID/root`` and the room's own (device, inode) must be among its
      ancestors. A reference that is not inside the room is not a hit; one that cannot be resolved at all is
      conservatively a hit. An unlinked file ("(deleted)") has no place any more: it does not hold a room, it
      still holds the floor.

    To avoid touching unrelated filesystems, a reference is only stat'ed when its mount is not known to be on
    another device. For a descriptor the mount is its ``mnt_id`` in /proc/PID/fdinfo; for cwd, root and exe it is
    the mount that holds the path ``readlink`` shows (readlink on those links does not open the target). Both are
    looked up in the process's mountinfo, and unknown means "stat it". A hung NFS or FUSE mount elsewhere is thus
    not touched; ``RealSysOps.holders`` still runs the scan in a child process with a hard timeout as a backstop.
    """
    path = path.rstrip("/")
    floor_dev = stat_fn(floor_mp or path).st_dev
    key = f"{os.major(floor_dev):02x}:{os.minor(floor_dev):02x}"
    mm_key = f"{os.major(floor_dev)}:{os.minor(floor_dev)}"
    prefix = path + "/"
    room_ident = None
    if floor_mp is not None:
        room_stat = stat_fn(path)
        room_ident = (room_stat.st_dev, room_stat.st_ino)

    def named_inside(target: str, ident: tuple) -> bool | None:
        """True/False when the name leads to the held object, None when it does not."""
        try:
            again = stat_fn(target)
        except OSError:
            return None
        if (again.st_dev, again.st_ino) != ident:
            return None
        return target == path or target.startswith(prefix)

    def inside(base: str, target: str, ident: tuple) -> bool:
        if floor_mp is None:
            return True
        if target.startswith("/"):
            verdict = named_inside(target, ident)
            if verdict is not None:
                return verdict
            if target.endswith(" (deleted)"):
                return False
            chain = resolve_fn(os.path.join(base, "root"), target)
            if chain is not None:
                return room_ident in chain
        return True  # cannot be placed: assume it is in the room

    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError as exc:
        raise SystemFault(f"cannot scan {proc_root} for open files: {exc}") from exc
    out = []
    for pid in pids:
        base = os.path.join(proc_root, pid)
        table = mount_table(os.path.join(base, "mountinfo"))
        mounts = None if table is None else {mid: mm for mid, mm, _ in table}
        links = [(os.path.join(base, name), None) for name in ("cwd", "root", "exe")]
        try:
            links += [(os.path.join(base, "fd", fd), fd) for fd in os.listdir(os.path.join(base, "fd"))]
        except OSError:
            pass
        hit = False
        for link, fd_name in links:
            if table is not None:
                if fd_name:
                    other = mounts.get(fdinfo_mnt_id(os.path.join(base, "fdinfo", fd_name)))
                else:
                    try:
                        other = device_of_path(table, os.readlink(link))
                    except OSError:
                        other = None
                if other is not None and other != mm_key:
                    continue  # known to live on another device: do not stat it
            try:
                st = stat_fn(link)
            except OSError:
                continue
            if st.st_dev != floor_dev:
                continue
            try:
                target = os.readlink(link)
            except OSError:
                target = ""
            if inside(base, target, (st.st_dev, st.st_ino)):
                hit = True
                break
        if not hit:
            try:
                with open(os.path.join(base, "maps"), encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        parts = line.split(None, 5)
                        if len(parts) >= 6 and parts[3] == key and parts[5].strip().startswith("/"):
                            name = parts[5].strip()
                            try:
                                ident = (floor_dev, int(parts[4]))
                            except ValueError:
                                ident = (floor_dev, -1)
                            if inside(base, name, ident):
                                hit = True
                                break
            except OSError:
                pass
        if hit:
            try:
                with open(os.path.join(base, "comm"), encoding="utf-8", errors="replace") as handle:
                    comm = handle.read().strip()
            except OSError:
                comm = "?"
            out.append(f"{pid} {comm}")
    return out


SCAN_TIMEOUT = 20.0
_SCAN_CHILD = ("import sys, json; sys.path.insert(0, sys.argv[1]); from motel.sysops import scan_holders; "
               "json.dump(scan_holders(sys.argv[2], sys.argv[3], sys.argv[4] or None), sys.stdout)")


_ABANDONED: list = []


class ScanTimeout(Busy):
    """The holder scan did not finish: whether the filesystem is in use is unknown, which is never 'idle'."""


def _scan_identity(pid: int) -> str | None:
    """Start time of a live (non-zombie) process, or None. Together with the pid it identifies the process."""
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as handle:
            rest = handle.read().rsplit(")", 1)[1].split()
        return None if rest[0] in ("Z", "X") else rest[19]
    except (OSError, IndexError, ValueError):
        return None


def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def abandoned_scan(marker: str | None, *, cleanup: bool = False) -> int | None:
    """Pid of an earlier holder-scan child that was abandoned and is still alive (stuck in the kernel), or None.
    The marker holds pid, start time and boot id; one from another boot is stale by definition. Only with
    ``cleanup`` (the desk, never doctor) is a stale marker removed."""
    if not marker:
        return None
    try:
        with open(marker, encoding="utf-8") as handle:
            pid, started, boot = handle.read().split()
        if boot == _boot_id() and _scan_identity(int(pid)) == started:
            return int(pid)
    except (OSError, ValueError):
        pass
    if cleanup:
        try:
            os.unlink(marker)
        except OSError:
            pass
    return None


def scan_holders_guarded(proc_root: str, path: str, floor_mp: str | None, *, timeout: float = SCAN_TIMEOUT,
                         argv: list | None = None, marker: str | None = None) -> list:
    """Run ``scan_holders`` in a child process with a hard timeout.

    A process stuck on a hung NFS or FUSE mount cannot be interrupted, so the scan runs where it can be abandoned:
    on timeout the child is killed (and, if it is stuck in the kernel, left behind) and the answer is "unknown" (``ScanTimeout``,
    exit 4). The child does not inherit the store lock or any other descriptor.

    A child that survived its kill is recorded in ``marker`` (pid and start time). While it is alive no new scan is
    started - it would only get stuck on the same mount and pile up - and the answer is Busy.
    """
    stuck = abandoned_scan(marker, cleanup=True)
    if stuck is not None:
        raise ScanTimeout(f"an earlier holder scan (pid {stuck}) is still stuck on a hung network or FUSE mount; "
                          "not starting another", "motel doctor")
    srcdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = argv or [sys.executable, "-I", "-c", _SCAN_CHILD, srcdir, proc_root, path, floor_mp or ""]
    try:
        child = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                 env=SAFE_ENV, close_fds=True, shell=False)
    except OSError as exc:
        raise SystemFault(f"cannot start the holder scan: {exc}") from exc
    try:
        out, err = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except OSError:
            pass
        for pipe in (child.stdout, child.stderr):
            if pipe:
                pipe.close()
        try:
            child.wait(timeout=0.5)  # a killed child is gone at once; one stuck in the kernel is left behind
        except subprocess.TimeoutExpired:
            _ABANDONED.append(child)
            started = _scan_identity(child.pid)
            if marker and started:
                try:
                    tmp = f"{marker}.tmp"
                    with open(tmp, "w", encoding="utf-8") as handle:
                        handle.write(f"{child.pid} {started} {_boot_id()}\n")
                    os.replace(tmp, marker)
                except OSError:
                    pass
        raise ScanTimeout(f"could not tell within {timeout:g}s whether {path} is in use "
                          f"(a hung network or FUSE mount?); nothing was changed",
                          "motel doctor") from None
    if child.returncode != 0:
        raise SystemFault(f"holder scan failed (exit {child.returncode}): "
                          f"{err.decode('utf-8', 'replace').strip()[-200:]}")
    try:
        found = json.loads(out.decode("utf-8"))
    except ValueError:
        raise SystemFault("holder scan returned unreadable output") from None
    if not isinstance(found, list) or not all(isinstance(x, str) for x in found):
        raise SystemFault("holder scan returned unreadable output")
    return found


class SysOps(Protocol):
    def resolve_device(self, dev: str) -> str: ...
    def wait_device(self, dev: str, timeout: float = 90.0) -> None: ...
    def block_serial(self, dev: str) -> str: ...
    def fstype(self, dev: str) -> str: ...
    def mounted_source(self, mountpoint: str) -> str | None: ...
    def is_mounted(self, path: str) -> bool: ...
    def mount(self, dev: str, mountpoint: str) -> None: ...
    def umount(self, mountpoint: str) -> None: ...
    def prepare_fs(self, dev: str, label: str) -> None: ...
    def grow_fs(self, dev: str) -> None: ...
    def probe(self, dev: str) -> dict: ...
    def block_size(self, dev: str) -> int: ...
    def block_holders(self, dev: str) -> list: ...
    def zero_edges(self, dev: str) -> None: ...
    def make_fs(self, dev: str, label: str) -> None: ...
    def fs_label(self, dev: str) -> str: ...
    def rescan(self, dev: str, target_bytes: int | None = None, above: int | None = None) -> None: ...
    def chown(self, path: str, uid: int, gid: int) -> None: ...
    def usage(self, path: str) -> tuple: ...
    def holders(self, path: str, floor_mp: str | None = None) -> list: ...
    def mounts_under(self, path: str) -> list: ...
    def device_mounts(self, dev: str) -> list: ...
    def abandoned_scan(self) -> int | None: ...


def _first_existing(*candidates: str) -> str:
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


class RealSysOps:
    """Linux implementation. ``runner(argv, timeout) -> stdout`` can be replaced in tests."""

    root_uid = 0

    @staticmethod
    def _run(argv: list, timeout: float = 120.0) -> str:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                                  env=SAFE_ENV, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SystemFault(f"{argv[0]}: {exc}") from exc
        if proc.returncode != 0:
            fault = CommandFailed(f"{' '.join(argv[:3])} failed (exit {proc.returncode}): {proc.stderr.strip()[:200]}")
            fault.returncode = proc.returncode
            fault.stderr = proc.stderr.strip()
            raise fault
        return proc.stdout

    def run(self, argv: list, timeout: float = 120.0) -> str:
        return self._runner(argv, timeout)

    def resolve_device(self, dev: str) -> str:
        return os.path.realpath(dev)

    def wait_device(self, dev: str, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        while not os.path.exists(dev):
            if time.monotonic() > deadline:
                raise SystemFault(f"device {dev} did not appear")
            try:
                self.run([_first_existing("/usr/bin/udevadm", "/bin/udevadm"), "settle", "--timeout=10"], 20)
            except SystemFault:
                pass
            time.sleep(1.0)

    def block_serial(self, dev: str) -> str:
        return self.run([_first_existing("/usr/bin/lsblk", "/bin/lsblk"), "-dno", "SERIAL", dev], 20).strip()

    def fstype(self, dev: str) -> str:
        return self.run([_first_existing("/usr/sbin/blkid", "/sbin/blkid"), "-o", "value", "-s", "TYPE", dev], 20).strip()

    def mounted_source(self, mountpoint: str) -> str | None:
        try:
            out = self.run([_first_existing("/usr/bin/findmnt", "/bin/findmnt"), "-nro", "SOURCE", "--mountpoint",
                            mountpoint], 20).strip()
        except SystemFault:
            return None  # findmnt exits 1 when nothing is mounted there
        return os.path.realpath(out) if out else None

    def is_mounted(self, path: str) -> bool:
        return os.path.ismount(path)

    def mount(self, dev: str, mountpoint: str) -> None:
        self.run([_first_existing("/usr/bin/mount", "/bin/mount"), "-t", "ext4", "-o", MOUNT_OPTIONS, dev, mountpoint], 60)

    def umount(self, mountpoint: str) -> None:
        # No -l and no -f: a busy mount must fail, never be detached under a process.
        self.run([_first_existing("/usr/bin/umount", "/bin/umount"), mountpoint], 120)

    def prepare_fs(self, dev: str, label: str) -> None:
        self.run([_first_existing("/usr/sbin/tune2fs", "/sbin/tune2fs"), "-m", "0", "-L", label[:16], dev], 60)

    def grow_fs(self, dev: str) -> None:
        self.run([_first_existing("/usr/sbin/resize2fs", "/sbin/resize2fs"), dev], 600)

    def probe(self, dev: str) -> dict:
        """Every filesystem, RAID or partition-table signature low-level probing finds on ``dev`` (``{}`` = none found).

        ``blkid -p`` reads the device itself and bypasses the cache. It exits 2 both when it finds no signature and
        when it cannot read the device (the difference is only in the error text), so exit 2 counts as "nothing found"
        only when blkid also said nothing on stderr. Any other outcome is a fault, never a blank device.
        """
        try:
            out = self.run([_first_existing("/usr/sbin/blkid", "/sbin/blkid"), "-p", "-o", "export", dev], 60)
        except CommandFailed as exc:
            said = exc.stderr if exc.stderr is not None else str(exc).partition("): ")[2]
            if exc.returncode == 2 and not said.strip():
                return {}
            raise SystemFault(f"blkid could not probe {dev} ({said.strip()[:120] or 'exit ' + str(exc.returncode)}): "
                              "not treating it as blank") from exc
        found = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        found.pop("DEVNAME", None)
        return found

    block_sysfs = "/sys/class/block"

    def block_holders(self, dev: str) -> list:
        """Block devices stacked on ``dev`` (device-mapper, md, ...). Unknown is a fault: it cannot be called free."""
        path = os.path.join(self.block_sysfs, os.path.basename(os.path.realpath(dev)), "holders")
        try:
            return sorted(os.listdir(path))
        except OSError as exc:
            raise SystemFault(f"cannot tell whether {dev} is in use by another device ({path}: {exc.strerror})") from exc

    def zero_edges(self, dev: str, nbytes: int = EDGE_BYTES) -> None:
        """Open ``dev`` exclusively and require its first and last ``nbytes`` to read as zeros.

        The exclusive open (``O_EXCL`` on a block device) fails if anything has the device mounted or claimed. Any
        read error is a fault: an unreadable device is not a blank one.
        """
        try:
            fd = os.open(dev, os.O_RDONLY | os.O_EXCL | os.O_CLOEXEC)
        except OSError as exc:
            raise SystemFault(f"cannot open {dev} exclusively ({exc.strerror}): it is in use or unreadable") from exc
        try:
            size = os.lseek(fd, 0, os.SEEK_END)
            if size <= 0:
                raise SystemFault(f"{dev} reports no size")
            head = min(nbytes, size)
            spans = [(0, head)] + ([(size - min(nbytes, size - head), min(nbytes, size - head))] if size > head else [])
            for offset, length in spans:
                while length > 0:
                    chunk = os.pread(fd, min(length, 1 << 20), offset)
                    if not chunk:
                        raise SystemFault(f"short read from {dev} at byte {offset}")
                    if chunk.count(0) != len(chunk):
                        raise Guard(f"{dev} holds data near byte {offset}; refusing to format it",
                                    "this cloud may not hand out zeroed volumes: check the device, then run mkfs.ext4 "
                                    "-L motel-<floor> on it by hand")
                    offset += len(chunk)
                    length -= len(chunk)
        except OSError as exc:
            raise SystemFault(f"cannot read {dev}: {exc.strerror}") from exc
        finally:
            os.close(fd)

    def block_size(self, dev: str) -> int:
        text = self.run([_first_existing("/usr/bin/lsblk", "/bin/lsblk"), "-bdno", "SIZE", dev], 20).strip()
        if not text.isdigit():
            raise SystemFault(f"lsblk did not report a size for {dev}")
        return int(text)

    def make_fs(self, dev: str, label: str) -> None:
        self.run([_first_existing("/usr/sbin/mkfs.ext4", "/sbin/mkfs.ext4"), "-q", "-F", "-L", label[:16], dev], 3600)

    def fs_label(self, dev: str) -> str:
        return self.probe(dev).get("LABEL", "")

    rescan_timeout = 60.0

    def rescan(self, dev: str, target_bytes: int | None = None, above: int | None = None) -> None:
        """Ask the kernel to re-read a grown device's size, then wait for the new size.

        Some clouds (Azure, Scaleway) do not tell the guest about an online resize until the device is rescanned.
        A device without the sysfs knob (virtio) is already up to date; only the size wait applies.

        The wait needs both conditions: the device is at least ``target_bytes`` (the smaller reading of the new size) and,
        when ``above`` is given, strictly larger than ``above`` (the size read before the resize). The second one matters
        where the unit is unknown: a 200 GiB device already exceeds the decimal 210 GB target, so ``target_bytes`` alone
        would let the filesystem grow before the guest has seen anything.
        """
        name = os.path.basename(os.path.realpath(dev))
        knob = f"/sys/class/block/{name}/device/rescan"
        if os.path.exists(knob):
            try:
                with open(knob, "w", encoding="ascii") as handle:
                    handle.write("1")
            except OSError as exc:
                raise SystemFault(f"cannot rescan {dev}: {exc.strerror}") from exc
        if target_bytes is None and above is None:
            return
        deadline = time.monotonic() + self.rescan_timeout
        while True:
            size = self.block_size(dev)
            if size >= (target_bytes or 0) and (above is None or size > above):
                return
            if time.monotonic() > deadline:
                need = f"at least {target_bytes}" if above is None else f"more than {above} and at least {target_bytes or 0}"
                raise SystemFault(f"{dev} is {size} bytes after a rescan; expected {need}")
            time.sleep(1.0)

    def chown(self, path: str, uid: int, gid: int) -> None:
        os.chown(path, uid, gid, follow_symlinks=False)

    def usage(self, path: str) -> tuple:
        st = os.statvfs(path)
        return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize

    def _mountinfo_texts(self, all_namespaces: bool) -> list:
        paths = ["/proc/self/mountinfo"]
        if all_namespaces:
            try:
                paths += [f"/proc/{p}/mountinfo" for p in os.listdir("/proc") if p.isdigit()]
            except OSError as exc:
                raise SystemFault(f"cannot scan /proc for mounts: {exc}") from exc
        texts = []
        for path in paths:
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    texts.append(handle.read())
            except OSError:
                if path == "/proc/self/mountinfo":
                    raise SystemFault("cannot read /proc/self/mountinfo") from None
        return texts

    def mounts_under(self, path: str) -> list:
        """Mount points at or below ``path`` in this mount namespace (nested or bind mounts)."""
        return mounts_at_or_under(parse_mountinfo(self._mountinfo_texts(False)[0]), os.path.realpath(path))

    def device_mounts(self, dev: str) -> list:
        """Every mount point of the block device ``dev``, in every mount namespace we can read."""
        try:
            st = os.stat(os.path.realpath(dev))
        except OSError:
            return []  # the device link is gone: it is not mounted anywhere
        key = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
        return sorted({mp for text in self._mountinfo_texts(True) for mm, mp in parse_mountinfo(text) if mm == key})

    scan_timeout = SCAN_TIMEOUT
    scan_argv: list | None = None  # tests replace the child command

    def __init__(self, runner=None, *, scan_marker: str | None = None):
        self._runner = runner or self._run
        self.scan_marker = scan_marker  # where an abandoned scan child is recorded (under the state directory)

    def holders(self, path: str, floor_mp: str | None = None) -> list:
        return scan_holders_guarded("/proc", path, floor_mp, timeout=self.scan_timeout, argv=self.scan_argv,
                                    marker=self.scan_marker)

    def abandoned_scan(self) -> int | None:
        return abandoned_scan(self.scan_marker)


def check_serial(sysops: SysOps, provider, vol_id: str, dev: str, real: str) -> str | None:
    """Require the block-device serial the provider promises. Returns that serial (None when the cloud has none)."""
    serial = provider.expected_serial(vol_id)
    if serial is not None:
        actual = sysops.block_serial(real)
        if actual != serial:
            raise Guard(f"{dev} -> {real} has serial {actual!r}, expected {serial!r}")
    return serial


def fs_label_for(floor_id: str) -> str:
    return f"motel-{floor_id}"[:16]


def verify_device(sysops: SysOps, provider, vol_id: str, dev: str, label: str | None = None) -> str:
    """Resolve ``dev`` and prove it is the volume we mean before it is formatted, mounted or grown.

    A cloud with no block-device serial (Azure) cannot prove identity that way, so when ``label`` is given the ext4
    label ``motel-<floor id>`` (written when motel made the filesystem) is required as the extra check.
    """
    real = sysops.resolve_device(dev)
    serial = check_serial(sysops, provider, vol_id, dev, real)
    fstype = sysops.fstype(real)
    if fstype != "ext4":
        raise Guard(f"{real} filesystem is {fstype!r}, not ext4")
    if serial is None and label is not None:
        actual = sysops.fs_label(real)
        if actual != label:
            raise Guard(f"{real} has filesystem label {actual!r}, expected {label!r}")
    return real


def volume_bytes(provider, size_gb: int) -> tuple:
    """Sizes in bytes a ``size_gb`` volume may show in the guest: the provider's own unit (``gb_bytes``) if it
    declares one, else binary or decimal gigabytes."""
    unit = getattr(provider, "gb_bytes", None)
    return (size_gb * unit,) if unit else (size_gb * GIB, size_gb * 10 ** 9)


def grow_target_bytes(provider, size_gb: int) -> int:
    """The size a grown device must reach before the filesystem follows: the smallest size the formatting accepted."""
    return min(volume_bytes(provider, size_gb))


def format_blank_device(sysops: SysOps, provider, vol_id: str, dev: str, size_gb: int, floor_id: str,
                        own_mountpoint: str | None = None) -> bool:
    """Make the ext4 filesystem on a fresh volume from a cloud that hands out blank devices.

    Formatting destroys data, so the device must be proved blank by positive evidence and any doubt is a refusal:
    the serial matches (when the cloud has one); nothing mounts it and no other device sits on it; ``blkid -p``
    reads it and finds no signature (a read error is not "no signature"); its first and last 4 MiB read as zeros
    through an exclusive open; its size is the volume's. The serial and size are checked again immediately before
    mkfs. (The exclusive descriptor cannot stay open across mkfs: mke2fs opens the device exclusively itself.)

    The assumption behind the zero check is that a new cloud volume reads as zeros; a cloud that does not zero fails
    closed with a hint. A device that already holds exactly the ext4 filesystem this function would have made
    (label ``motel-<floor id>``) is left alone, mounted or not: an earlier run died after mkfs, or after mounting it
    at ``own_mountpoint``, and before the state changed. Returns True when mkfs ran.
    """
    real = sysops.resolve_device(dev)
    check_serial(sysops, provider, vol_id, dev, real)
    label = fs_label_for(floor_id)

    def ours(found: dict) -> bool:
        return found.get("TYPE") == "ext4" and found.get("LABEL") == label and "PTTYPE" not in found

    mounts = sysops.device_mounts(real)
    if mounts:
        resumable = own_mountpoint is not None and {os.path.realpath(m) for m in mounts} == {os.path.realpath(own_mountpoint)}
        if resumable and ours(sysops.probe(real)):
            return False
        raise Guard(f"{real} is mounted at {', '.join(mounts[:3])}; refusing to format it")
    holders = sysops.block_holders(real)
    if holders:
        raise Guard(f"{real} is in use by {', '.join(holders[:3])}; refusing to format it")
    found = sysops.probe(real)
    if found:
        if ours(found):
            return False
        kinds = ", ".join(sorted(f"{k}={v}" for k, v in found.items() if k in ("TYPE", "PTTYPE", "LABEL"))) or "unknown"
        raise Guard(f"{real} already carries a signature ({kinds}); refusing to format it")
    sysops.zero_edges(real)
    size, allowed = sysops.block_size(real), volume_bytes(provider, size_gb)
    if size not in allowed:
        raise Guard(f"{real} is {size} bytes but the volume should be {' or '.join(str(a) for a in allowed)}; "
                    "refusing to format it")
    check_serial(sysops, provider, vol_id, dev, real)  # again, right before the write
    if sysops.block_size(real) != size or sysops.device_mounts(real):
        raise Guard(f"{real} changed while it was being checked; refusing to format it")
    sysops.make_fs(real, label)
    return True
