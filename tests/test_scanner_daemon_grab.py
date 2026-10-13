"""Tests for the round-4 T3 grab + lifecycle (scripts/scanner_daemon.py).

No hardware (the scanner is not guaranteed to be attached — CI has none):
``GrabbedDevice``'s ``opener``/``ioctl``/``closer`` are injected recording
fakes, verification runs against a fake sysfs tree in ``tmp_path`` (the T2
fixture pattern), and ``run_loop`` is driven with a fake ``scan_reader``.

The invariants under test (docs/plans/r4t3-FINAL.md):

* the grab ioctl is EVIOCGRAB=0x40044590 with value 1, and only AFTER the
  node's VID/PID was verified from sysfs (the human-keyboard guard);
* every exit path closes the fd — which is what auto-releases the grab;
* EBUSY / EACCES / ENODEV map to loud, distinguishable failures;
* device loss is transient: the re-discovery loop re-grabs and never exits;
* a lockfile refuses a second daemon instance.
"""

import errno
import os

import pytest

from scripts.scanner_daemon import (
    EVIOCGRAB,
    DaemonLock,
    DeviceLostError,
    GrabbedDevice,
    GrabBusyError,
    GrabPermissionError,
    InstanceAlreadyRunning,
    WrongDeviceError,
    discover_event_device,
    run_loop,
)
from tests.test_scanner_daemon import _make_event_node, _input_class

NODE = "/dev/input/event3"
VID_PID = (0x065A, 0xA001)


class FakeGrab:
    """Recording opener/ioctl/closer triple + the sysfs tree's node."""

    def __init__(self, *, open_error=None, grab_error=None):
        self.open_error = open_error
        self.grab_error = grab_error
        self.fds = iter(range(101, 1000))
        self.opened = []
        self.closed = []
        self.ioctl_log = []

    def opener(self, node):
        self.opened.append(node)
        if self.open_error is not None:
            raise self.open_error
        return next(self.fds)

    def ioctl(self, fd, request, value):
        self.ioctl_log.append((fd, request, value))
        if self.grab_error is not None and value == 1:
            raise self.grab_error

    def closer(self, fd):
        self.closed.append(fd)

    def grabs(self):
        return [entry for entry in self.ioctl_log if entry[2] == 1]

    def releases(self):
        return [entry for entry in self.ioctl_log if entry[2] == 0]


@pytest.fixture()
def scanner_sysfs(tmp_path):
    """A fake /sys/class/input containing exactly one 065a:a001 node."""
    root = _input_class(tmp_path)
    _make_event_node(root, "event3", "065a", "a001")
    _make_event_node(root, "event0", "001f", "0001")  # the human keyboard
    return root


# --- the grab contract ---------------------------------------------------------

def test_evIOCGRAB_value_is_pinned():
    # _IOW('E', 0x90, int) — r4rev M7, measured on this host's kernel.
    assert EVIOCGRAB == 0x40044590


def test_enter_grabs_and_exit_releases_and_closes(scanner_sysfs):
    fake = FakeGrab()
    with GrabbedDevice(NODE, sysfs_root=scanner_sysfs, opener=fake.opener,
                       ioctl=fake.ioctl, closer=fake.closer) as dev:
        assert dev.fd == 101
        assert fake.opened == [NODE]
        assert fake.grabs() == [(101, EVIOCGRAB, 1)]  # value 1 == grab
        assert fake.closed == []                       # held while inside
    assert fake.releases() == [(101, EVIOCGRAB, 0)]    # explicit un-grab
    assert fake.closed == [101]                        # …and the fd closes


def test_exception_inside_the_with_still_releases_and_closes(scanner_sysfs):
    fake = FakeGrab()
    with pytest.raises(RuntimeError, match="boom"):
        with GrabbedDevice(NODE, sysfs_root=scanner_sysfs, opener=fake.opener,
                           ioctl=fake.ioctl, closer=fake.closer):
            raise RuntimeError("boom")
    assert fake.grabs() == [(101, EVIOCGRAB, 1)]
    assert fake.releases() == [(101, EVIOCGRAB, 0)]
    assert fake.closed == [101]  # the close auto-releases even without it


def test_grab_error_inside_enter_still_closes(scanner_sysfs):
    fake = FakeGrab(grab_error=OSError(errno.EIO, "died mid-grab"))
    with pytest.raises(DeviceLostError):
        with GrabbedDevice(NODE, sysfs_root=scanner_sysfs, opener=fake.opener,
                           ioctl=fake.ioctl, closer=fake.closer):
            pass  # pragma: no cover — __enter__ raises
    assert fake.closed == [101]


# --- failure mapping -----------------------------------------------------------

def test_ebusy_means_another_process_holds_the_scanner(scanner_sysfs):
    fake = FakeGrab(grab_error=OSError(errno.EBUSY, "busy"))
    with pytest.raises(GrabBusyError, match="another process holds"):
        GrabbedDevice(NODE, sysfs_root=scanner_sysfs, opener=fake.opener,
                      ioctl=fake.ioctl, closer=fake.closer).__enter__()
    assert fake.grabs() == [(101, EVIOCGRAB, 1)]  # the grab WAS attempted
    assert fake.closed == [101]


def test_eacces_at_open_names_the_three_fixes(scanner_sysfs):
    fake = FakeGrab(open_error=PermissionError(errno.EACCES, "denied"))
    with pytest.raises(GrabPermissionError, match="udev rule"):
        GrabbedDevice(NODE, sysfs_root=scanner_sysfs, opener=fake.opener,
                      ioctl=fake.ioctl, closer=fake.closer).__enter__()
    assert fake.ioctl_log == []  # never grabbed


def test_enodev_at_open_is_transient_loss(scanner_sysfs):
    fake = FakeGrab(open_error=OSError(errno.ENODEV, "no device"))
    with pytest.raises(DeviceLostError):
        GrabbedDevice(NODE, sysfs_root=scanner_sysfs, opener=fake.opener,
                      ioctl=fake.ioctl, closer=fake.closer).__enter__()


# --- the human-keyboard guard --------------------------------------------------

def test_wrong_device_is_never_grabbed(scanner_sysfs):
    # The node exists and OPENS fine — but sysfs says it is the AT keyboard.
    fake = FakeGrab()
    with pytest.raises(WrongDeviceError, match="human-keyboard guard"):
        GrabbedDevice("/dev/input/event0", sysfs_root=scanner_sysfs,
                      opener=fake.opener, ioctl=fake.ioctl,
                      closer=fake.closer).__enter__()
    assert fake.ioctl_log == []  # THE pin: no ioctl on an unverified node
    assert fake.closed == [101]  # the opened fd is still cleaned up


def test_unverifiable_node_is_refused(tmp_path):
    # Garbage id/ contents (or a vanished device): unreadable == refused.
    root = _input_class(tmp_path)
    _make_event_node(root, "event4", "zzzz", "a001")
    _make_event_node(root, "event5", "065a", "a001", with_id=False)
    for node in ("/dev/input/event4", "/dev/input/event5"):
        fake = FakeGrab()
        with pytest.raises(WrongDeviceError):
            GrabbedDevice(node, sysfs_root=root, opener=fake.opener,
                          ioctl=fake.ioctl, closer=fake.closer).__enter__()
        assert fake.ioctl_log == []


def test_bare_node_name_is_accepted_too(scanner_sysfs):
    fake = FakeGrab()
    with GrabbedDevice("event3", sysfs_root=scanner_sysfs, opener=fake.opener,
                       ioctl=fake.ioctl, closer=fake.closer):
        pass
    assert fake.grabs() == [(101, EVIOCGRAB, 1)]


# --- the lockfile ----------------------------------------------------------------

def test_second_daemon_instance_is_refused(tmp_path):
    path = tmp_path / "hcrm-scanner-daemon.lock"
    with DaemonLock(path):
        with pytest.raises(InstanceAlreadyRunning, match="already running"):
            DaemonLock(path).acquire()


def test_lock_is_reacquirable_after_release(tmp_path):
    path = tmp_path / "hcrm-scanner-daemon.lock"
    lock = DaemonLock(path).acquire()
    lock.release()
    with DaemonLock(path):  # reacquires cleanly
        pass


def test_crashed_holder_frees_the_lock(tmp_path):
    # flock is released by the KERNEL when the holder's fd dies — simulate
    # a crash by closing the underlying fd without release(): no stale
    # lockfile problem, which is why flock beats O_EXCL here.
    path = tmp_path / "hcrm-scanner-daemon.lock"
    lock = DaemonLock(path).acquire()
    os.close(lock._fd)  # crash: no release() call
    with DaemonLock(path):
        pass


# --- the re-discovery run loop ----------------------------------------------------

def test_run_loop_forwards_scans_and_stops(scanner_sysfs):
    fake = FakeGrab()
    got, sleeps = [], []

    def reader(fd, on_unmapped=None):
        yield "8412345678905"
        yield "EX-007"
        return  # EOF ends the stream; the loop then honours stop()

    run_loop(got.append, sysfs_root=scanner_sysfs, opener=fake.opener,
             ioctl=fake.ioctl, closer=fake.closer, scan_reader=reader,
             sleep=sleeps.append, stop=lambda: True)
    assert got == ["8412345678905", "EX-007"]
    assert fake.grabs() == [(101, EVIOCGRAB, 1)]
    assert sleeps == [2.0]  # one re-discovery wait before the stop check


def test_run_loop_regrabs_after_device_loss(scanner_sysfs):
    # Grab 1 loses the device mid-stream (EOF → StopIteration); the loop
    # must release, sleep, re-discover, re-grab, and keep going — never exit.
    fake = FakeGrab()

    def reader_one(fd, on_unmapped=None):
        yield "A"
        return  # EOF: device gone

    def reader_two(fd, on_unmapped=None):
        yield "B"
        return

    readers = [reader_one, reader_two]
    scans, sleeps = [], []
    state = {"n": 0}

    def stop():
        state["n"] += 1
        return state["n"] >= 2  # end after the second regrab sleep

    run_loop(scans.append, sysfs_root=scanner_sysfs, opener=fake.opener,
             ioctl=fake.ioctl, closer=fake.closer,
             scan_reader=lambda fd, on_unmapped=None: readers.pop(0)(
                 fd, on_unmapped=on_unmapped),
             sleep=sleeps.append, stop=stop)
    assert scans == ["A", "B"]
    assert fake.opened == [NODE, NODE]      # TWO grabs: re-discovery worked
    assert fake.grabs() == [(101, EVIOCGRAB, 1), (102, EVIOCGRAB, 1)]
    assert fake.releases() == [(101, EVIOCGRAB, 0), (102, EVIOCGRAB, 0)]
    assert fake.closed == [101, 102]
    assert sleeps  # it waited between attempts


def test_run_loop_treats_enodev_read_error_as_transient_loss(scanner_sysfs):
    fake = FakeGrab()

    def bad_reader(fd, on_unmapped=None):
        yield "A"
        raise OSError(errno.ENODEV, "unplugged")
        yield "never"  # pragma: no cover

    def good_reader(fd, on_unmapped=None):
        yield "B"
        return

    readers = [bad_reader, good_reader]
    scans, sleeps = [], []
    state = {"n": 0}

    run_loop(scans.append, sysfs_root=scanner_sysfs, opener=fake.opener,
             ioctl=fake.ioctl, closer=fake.closer,
             scan_reader=lambda fd, on_unmapped=None: readers.pop(0)(
                 fd, on_unmapped=on_unmapped),
             sleep=sleeps.append,
             stop=lambda: (state.__setitem__("n", state["n"] + 1) or
                           state["n"] >= 2))
    assert scans == ["A", "B"]  # the ENODEV cost nothing but a re-grab


def test_run_loop_keeps_waiting_when_no_device_is_attached(tmp_path):
    # A tree with no scanner: the loop must NOT exit (qwen M1) — it sleeps
    # and retries until told to stop.
    root = _input_class(tmp_path)
    _make_event_node(root, "event0", "001f", "0001")
    fake, sleeps = FakeGrab(), []
    assert discover_event_device(sysfs_root=root) is None  # precondition
    run_loop(lambda scan: None, sysfs_root=root, opener=fake.opener,
             ioctl=fake.ioctl, closer=fake.closer,
             sleep=sleeps.append, stop=lambda: True)
    assert fake.opened == []       # nothing was ever opened or grabbed
    assert len(sleeps) == 1        # one retry wait, then the stop honoured


def test_run_loop_ebusy_is_loud_not_a_retry(scanner_sysfs):
    # Another process holds the grab: an operator problem — the loop must
    # propagate (T5's entrypoint turns this into a loud exit), not spin.
    fake = FakeGrab(grab_error=OSError(errno.EBUSY, "busy"))
    with pytest.raises(GrabBusyError):
        run_loop(lambda scan: None, sysfs_root=scanner_sysfs,
                 opener=fake.opener, ioctl=fake.ioctl, closer=fake.closer,
                 sleep=lambda s: None, stop=lambda: False)


def test_run_loop_pinned_node_is_used_once_then_discovery_takes_over(tmp_path):
    # A pinned node is a FIRST-attempt convenience only: after a loss the
    # loop trusts discovery (node numbers are recycled across replugs).
    root = _input_class(tmp_path)
    _make_event_node(root, "event3", "065a", "a001")
    _make_event_node(root, "event0", "001f", "0001")
    fake = FakeGrab()

    def reader(fd, on_unmapped=None):
        yield "A"
        return  # immediate loss

    calls, state = {"n": 0}, {"stop": False}

    def stop():
        calls["n"] += 1
        return calls["n"] >= 2

    run_loop(lambda s: None, node="/dev/input/event3", sysfs_root=root,
             opener=fake.opener, ioctl=fake.ioctl, closer=fake.closer,
             scan_reader=reader, sleep=lambda s: None, stop=stop)
    # First grab: the pinned node. Second: re-discovered to the same tree's
    # scanner node (not blindly re-opened by the stale path).
    assert fake.opened == ["/dev/input/event3", "/dev/input/event3"]


def test_run_loop_uses_discovery_when_node_pin_points_elsewhere(tmp_path):
    # The stronger variant: pin a node that is NOT the scanner — the loop's
    # own guard refuses it (loudly), proving the pin never bypasses checks.
    root = _input_class(tmp_path)
    _make_event_node(root, "event3", "065a", "a001")
    _make_event_node(root, "event0", "001f", "0001")
    fake = FakeGrab()
    with pytest.raises(WrongDeviceError):
        run_loop(lambda s: None, node="/dev/input/event0", sysfs_root=root,
                 opener=fake.opener, ioctl=fake.ioctl, closer=fake.closer,
                 sleep=lambda s: None, stop=lambda: False)
    assert fake.ioctl_log == []
