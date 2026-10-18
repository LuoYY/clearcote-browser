"""Widevine on a Mac: the CDM a Mac asks Google for, and that it lands where a Mac browser loads it.

Before this, ``_cdm_platform`` knew Linux and sent everything else down the Windows branch, so the
native macOS PRO build (an Apple Silicon Chromium.app) was handed widevinecdm.dll under win_x64 -- a
library no Mac can load, seeded into every profile. The coordinates here are what Google's server
really answers (2026-10-17: an arm64 Mac gets ``oimompecagnajdejgnnjijobebaeigek_4.10.3112.0_mac_arm64_
....crx3``, whose zip holds _platform_specific/mac_arm64/libwidevinecdm.dylib). There is no Mac in CI,
so the host is faked (``sys`` of the module + ``release.host_machine``, as test_macos_native does) and
Google is a stub that records every request.
"""

import hashlib
import io
import json
import os
import struct
import types
import zipfile

import pytest

import clearcote._widevine as W
import clearcote.release as release

VERSION = "4.10.3112.0"


def _on(monkeypatch, platform, machine="arm64"):
    monkeypatch.setattr(W, "sys", types.SimpleNamespace(platform=platform))
    monkeypatch.setattr(release, "host_machine", lambda: machine)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("CLEARCOTE_WIDEVINE_DIR", str(tmp_path / "wv"))
    for k in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(k, str(tmp_path / "home"))
    W._reset_default_state()
    yield
    W._reset_default_state()


def crx(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    header = b"\x01\x02\x03\x04"
    return b"Cr24" + struct.pack("<I", 3) + struct.pack("<I", len(header)) + header + buf.getvalue()


def mac_crx():
    """The macOS package's real layout (library + its .sig + manifest)."""
    return crx({"manifest.json": '{"version": "%s"}' % VERSION,
                "_platform_specific/mac_arm64/libwidevinecdm.dylib": b"\xcf\xfa\xed\xfe fake mach-o",
                "_platform_specific/mac_arm64/libwidevinecdm.dylib.sig": b"sig"})


class FakeGoogle:
    """Google's update server + download host behind urllib, recording the check's body and every UA."""

    def __init__(self, monkeypatch, blob):
        self.blob = blob
        self.sha = hashlib.sha256(blob).hexdigest()
        self.bodies, self.agents = [], []
        monkeypatch.setattr(W.urllib.request, "urlopen", self._urlopen)

    def _urlopen(self, req, timeout=None):
        self.agents.append(req.get_header("User-agent") or "")
        if req.full_url == W.OMAHA_URL:
            self.bodies.append(json.loads(req.data))
            # The classic shape Google really returns (codebase + package), XSSI guard included.
            payload = (")]}'\n" + json.dumps({"response": {"app": [{"updatecheck": {
                "status": "ok", "urls": {"url": [{"codebase": "https://dl.example.invalid/"}]},
                "manifest": {"version": VERSION, "packages": {"package": [
                    {"name": "cdm.crx3", "hash_sha256": self.sha}]}}}}]}})).encode()
        else:
            assert req.full_url == "https://dl.example.invalid/cdm.crx3", req.full_url
            payload = self.blob

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return payload

        return _Resp()


# -- coordinates -----------------------------------------------------------------------------------

def test_an_apple_silicon_mac_asks_for_the_mac_arm64_dylib():
    assert W._cdm_platform("darwin", "arm64") == ("mac", "Mac OS X", "14.0.0", "mac_arm64", "libwidevinecdm.dylib")
    assert W._cdm_arch("darwin", "arm64") == ("arm64", "arm", "arm64")
    # Python reports Apple Silicon as arm64; aarch64 is accepted like the PRO download does.
    assert W._cdm_platform("darwin", "aarch64")[3] == "mac_arm64"


def test_an_intel_mac_asks_for_the_mac_x64_dylib():
    assert W._cdm_platform("darwin", "x86_64") == ("mac", "Mac OS X", "14.0.0", "mac_x64", "libwidevinecdm.dylib")
    assert W._cdm_arch("darwin", "x86_64") == ("x64", "x86-64", "x86_64")


@pytest.mark.parametrize("machine", ["amd64", "x86_64", "arm64"])
def test_windows_and_linux_are_what_they_were_whatever_the_cpu(machine):
    assert W._cdm_platform("win32", machine) == ("win", "Windows", "10.0.19045.0", "win_x64", "widevinecdm.dll")
    assert W._cdm_platform("linux", machine) == ("Linux", "Linux", "6.1.0", "linux_x64", "libwidevinecdm.so")
    assert W._cdm_arch("win32", machine) == ("x64", "x86-64", "x86_64")
    assert W._cdm_arch("linux", machine) == ("x64", "x86-64", "x86_64")


def test_the_defaults_follow_this_host(monkeypatch):
    _on(monkeypatch, "darwin", "arm64")
    assert W._cdm_platform()[3] == "mac_arm64"
    assert W._cdm_arch()[0] == "arm64"


# -- the request -----------------------------------------------------------------------------------

def _before(at_os, platform, version):
    """The body the SDK sent before macOS existed, for Windows / Linux."""
    return {"request": {
        "@os": at_os, "@updater": "clearcote", "acceptformat": "crx3", "protocol": "3.1",
        "arch": "x64", "nacl_arch": "x86-64", "prodversion": "149.0.0.0", "updaterversion": "149.0.0.0",
        "dedup": "cr", "os": {"arch": "x86_64", "platform": platform, "version": version},
        "app": [{"appid": W.WIDEVINE_APP_ID, "version": "0.0.0.0", "updatecheck": {}, "ping": {"r": -2}}],
    }}


def test_a_mac_asks_as_googles_own_mac_client_does(monkeypatch):
    _on(monkeypatch, "darwin", "arm64")
    r = W._omaha_request_body()["request"]
    assert r["@os"] == "mac"
    assert (r["arch"], r["nacl_arch"]) == ("arm64", "arm")
    assert r["os"] == {"arch": "arm64", "platform": "Mac OS X", "version": "14.0.0"}


def test_windows_and_linux_bodies_are_byte_for_byte_the_old_ones(monkeypatch):
    _on(monkeypatch, "win32", "arm64")
    assert json.dumps(W._omaha_request_body()) == json.dumps(_before("win", "Windows", "10.0.19045.0"))
    _on(monkeypatch, "linux", "x86_64")
    assert json.dumps(W._omaha_request_body()) == json.dumps(_before("Linux", "Linux", "6.1.0"))


def test_the_user_agent_names_the_os_asked_for():
    assert W._user_agent("darwin").startswith("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)")
    assert "(Windows NT 10.0; Win64; x64)" in W._user_agent("win32")
    assert "(X11; Linux x86_64)" in W._user_agent("linux")


# -- end to end against a stub of Google ------------------------------------------------------------

def test_fetch_widevine_on_a_mac_installs_the_dylib_with_a_mac_ua_on_every_request(monkeypatch):
    _on(monkeypatch, "darwin", "arm64")
    g = FakeGoogle(monkeypatch, mac_crx())
    d = W.fetch_widevine(quiet=True)
    assert len(g.bodies) == 1
    assert g.bodies[0]["request"]["@os"] == "mac" and g.bodies[0]["request"]["arch"] == "arm64"
    assert len(g.agents) == 2  # the check and the download
    assert all("Macintosh" in ua for ua in g.agents)
    assert os.path.isfile(os.path.join(d, "_platform_specific", "mac_arm64", "libwidevinecdm.dylib"))
    assert os.path.isfile(os.path.join(d, "manifest.json"))


def test_a_package_without_the_mac_library_is_refused(monkeypatch):
    _on(monkeypatch, "darwin", "arm64")
    FakeGoogle(monkeypatch, crx({"manifest.json": "{}", "_platform_specific/win_x64/widevinecdm.dll": b"MZ"}))
    with pytest.raises(Exception, match="libwidevinecdm.dylib"):
        W.fetch_widevine(quiet=True)


def test_seed_widevine_copies_the_mac_cdm_into_the_profile(monkeypatch, tmp_path):
    _on(monkeypatch, "darwin", "arm64")
    FakeGoogle(monkeypatch, mac_crx())
    target = W.seed_widevine(str(tmp_path / "profile"), quiet=True)
    lib = os.path.join(target, "_platform_specific", "mac_arm64", "libwidevinecdm.dylib")
    with open(lib, "rb") as f:
        assert b"fake mach-o" in f.read()


@pytest.mark.parametrize("platform, scan", [("darwin", True), ("win32", True), ("linux", False)])
def test_the_startup_scan_is_forced_on_mac_and_windows_not_linux(monkeypatch, tmp_path, platform, scan):
    _on(monkeypatch, platform, "arm64")
    monkeypatch.setattr(W, "seed_widevine", lambda *a, **k: str(tmp_path))
    kwargs = {}
    W.apply_widevine_launch(str(tmp_path / "udd"), kwargs, quiet=True)
    assert ("--component-updater=fast-update" in kwargs["args"]) is scan
