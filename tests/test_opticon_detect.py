"""The shared Opticon hardware map (scripts/opticon_detect.py).

Everything runs offline: ``find_linux_usb_devices`` takes an injectable sysfs
root, so discovery is exercised against a fake tree with no hardware attached.

The personality table is pinned deliberately. ``065A:A001`` is *observed* on
this project's dev machine but appears in **no** public Opticon document we
could find (SS13063 §18.5 documents only ``A002`` for USB-COM; §18.4 lists no
PID for USB-HID). ``test_unconfirmed_personality_keeps_its_hedge`` makes that
epistemic status load-bearing: turning the hedge into a claim requires the
hardware step that would prove it (scan the *USB COM Port* sheet, watch the
device re-enumerate as ``A002``) and a deliberate edit of this test — not a
quiet wording change in a doc.
"""

import pytest

from scripts.opticon_detect import (
    OPTICON_VID,
    PERSONALITIES,
    describe,
    find_linux_usb_devices,
    find_windows_ports,
    survey,
)


# --- sysfs discovery --------------------------------------------------------


def test_find_linux_usb_devices_fake_sysfs(tmp_path):
    root = tmp_path / "devices"
    # The scanner, with a bound opticon tty:
    dev = root / "1-3"
    (dev / "1-3:1.0" / "tty").mkdir(parents=True)
    (dev / "1-3:1.0" / "tty" / "ttyUSB0").mkdir()
    (dev / "idVendor").write_text("065a\n")
    (dev / "idProduct").write_text("0009\n")
    (dev / "busnum").write_text("1\n")
    (dev / "devnum").write_text("3\n")
    # An unrelated device that must be ignored:
    other = root / "1-4"
    other.mkdir(parents=True)
    (other / "idVendor").write_text("1234\n")
    (other / "idProduct").write_text("5678\n")

    found = find_linux_usb_devices(root=str(root))
    assert len(found) == 1
    assert found[0]["ttys"] == ["/dev/ttyUSB0"]
    assert (found[0]["busnum"], found[0]["devnum"]) == (1, 3)


def test_find_linux_usb_devices_no_tty_when_driver_missing(tmp_path):
    # Enumerated but no tty bound (e.g. WSL2 kernel without CONFIG_USB_SERIAL_OPTICON):
    root = tmp_path / "devices"
    dev = root / "2-1"
    (dev / "2-1:1.0").mkdir(parents=True)
    (dev / "idVendor").write_text("065a\n")
    (dev / "idProduct").write_text("0009\n")
    found = find_linux_usb_devices(root=str(root))
    assert len(found) == 1 and found[0]["ttys"] == []


def test_find_linux_usb_devices_reports_hidraw_nodes(tmp_path):
    """The HID personality binds hidraw, not a tty — discovery must see both."""
    root = tmp_path / "devices"
    dev = root / "1-9"
    hid = dev / "1-9:1.0" / "0003:065A:A001.0004" / "hidraw" / "hidraw0"
    hid.mkdir(parents=True)
    (dev / "idVendor").write_text("065a\n")
    (dev / "idProduct").write_text("a001\n")
    found = find_linux_usb_devices(root=str(root))
    assert len(found) == 1
    assert found[0]["hidraws"] == ["/dev/hidraw0"]
    assert found[0]["ttys"] == []


def test_find_linux_usb_devices_survives_a_garbage_sysfs(tmp_path):
    """A node without readable idVendor/idProduct must not abort the scan."""
    root = tmp_path / "devices"
    (root / "1-1").mkdir(parents=True)                     # no idVendor at all
    bad = root / "1-2"
    bad.mkdir()
    (bad / "idVendor").write_text("zzzz\n")                # not hex
    (bad / "idProduct").write_text("0009\n")
    good = root / "1-3"
    good.mkdir()
    (good / "idVendor").write_text("065a\n")
    (good / "idProduct").write_text("0009\n")
    found = find_linux_usb_devices(root=str(root))
    assert [f["sysfs"] for f in found] == [str(good)]


# --- the hardware map -------------------------------------------------------


def test_map_covers_the_three_personalities_we_have_met():
    assert OPTICON_VID == 0x065A
    assert set(PERSONALITIES) == {0x0009, 0xA001, 0xA002}
    for pid, info in PERSONALITIES.items():
        # Every entry must be actionable: what it is, how to reach it, what
        # tool receives from it, and where the claim comes from.
        for key in ("name", "transport", "backend", "nodes", "receive_with",
                    "evidence", "confirmed"):
            assert key in info, f"065A:{pid:04X} is missing {key!r}"
        assert info["evidence"].strip(), f"065A:{pid:04X} has no provenance"


def test_unconfirmed_personality_keeps_its_hedge():
    """P7, pinned: A001 stays 'observed, unconfirmed' until hardware proves it.

    If you have run the experiment (scan the *USB COM Port* configuration
    sheet; the device re-enumerates as 065A:A002), record the dated result in
    docs/opticon-hardware.md and flip ``confirmed`` here in the same commit.
    """
    a001 = PERSONALITIES[0xA001]
    assert a001["confirmed"] is False
    assert "OBSERVED LIVE" in a001["evidence"]
    assert "no" in a001["evidence"].lower() and "public Opticon document" in a001["evidence"]
    # …and the two that ARE documented must not be downgraded:
    assert PERSONALITIES[0x0009]["confirmed"] is True
    assert PERSONALITIES[0xA002]["confirmed"] is True
    assert "SS13063" in PERSONALITIES[0xA002]["evidence"]


def test_describe_never_invents_an_identity():
    unknown = describe(0xBEEF)
    assert unknown["known"] is False and unknown["confirmed"] is False
    assert unknown["backend"] is None and unknown["receive_with"] is None
    assert "do not guess" in unknown["evidence"]
    # A documented-but-unsupported sibling is 'known', still with no tool:
    sibling = describe(0x0001)
    assert sibling["known"] is True and sibling["receive_with"] is None
    # And the map itself round-trips:
    assert describe(0x0009)["receive_with"] == "scripts/opn2001.py read"
    assert describe(0xA001)["receive_with"] == "scripts/scanner_hid.py"


def test_survey_reports_no_device_without_inventing_one(monkeypatch):
    """On a host with nothing attached the survey says so — and stays usable."""
    monkeypatch.setattr("scripts.opticon_detect.find_linux_usb_devices",
                        lambda root=None, vendor=OPTICON_VID: [])
    monkeypatch.setattr("scripts.opticon_detect.probe_windows_pnp", lambda: None)
    monkeypatch.setattr("scripts.opticon_detect.sys.platform", "linux", raising=False)
    report = survey()
    assert report["devices"] == []
    assert any("no Opticon device" in g for g in report["guidance"])


def test_survey_names_the_reception_tool_for_a_device_it_finds(monkeypatch, tmp_path):
    """The survey must tell the operator which tool receives from what it found.

    This is the whole point of one shared map: the guidance names an in-tree
    tool, never a branch and never a file that does not exist.
    """
    dev = {"sysfs": str(tmp_path), "vid": OPTICON_VID, "pid": 0xA001,
           "busnum": 1, "devnum": 9, "ttys": [], "hidraws": ["/dev/hidraw0"]}
    monkeypatch.setattr("scripts.opticon_detect.find_linux_usb_devices",
                        lambda root=None, vendor=OPTICON_VID: [dev])
    monkeypatch.setattr("scripts.opticon_detect.sys.platform", "linux", raising=False)
    report = survey()
    assert report["devices"][0]["usable"] is True
    assert any("scripts/scanner_hid.py" in g for g in report["guidance"])
    # …and nothing in the report may point at a git branch:
    assert "feature/" not in str(report["guidance"])


def test_find_windows_ports_without_pyserial_is_empty_not_fatal(monkeypatch):
    """pyserial is not a dependency of this repo: absence degrades to []."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("serial"):
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert find_windows_ports() == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
