import io
import json
import urllib.error

import pytest

pytest.importorskip("PySide6")

from tgdl.gui import updater  # noqa: E402
from tgdl.gui.updater import Release, UpdateError  # noqa: E402

EXE = b"MZ new version " * 1000


def response(data):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
    return Response(data)


def release_json(tag="v1.0.7", digest=None):
    asset = {"name": "TelegramDownloader.exe", "size": len(EXE),
             "browser_download_url": "https://example/TelegramDownloader.exe"}
    if digest:
        asset["digest"] = digest
    return {"tag_name": tag, "body": "تسجيل الدخول برمز QR",
            "assets": [{"name": "other.zip"}, asset]}


@pytest.mark.parametrize("latest, current, newer", [
    ("1.0.7", "1.0.6", True), ("v1.0.10", "1.0.9", True), ("1.0.6", "1.0.6", False),
    ("1.0.5", "1.0.6", False), ("1.0.7", "dev", False), ("nightly", "1.0.0", False),
])
def test_is_newer(latest, current, newer):
    assert updater.is_newer(latest, current) is newer


def test_fetch_latest_reads_release_and_digest():
    sha = "ab" * 32
    data = json.dumps(release_json(digest=f"sha256:{sha.upper()}")).encode()
    rel = updater.fetch_latest(urlopen=lambda req, timeout: response(data))
    assert rel == Release("1.0.7", "https://example/TelegramDownloader.exe",
                          len(EXE), sha, "تسجيل الدخول برمز QR")


def test_fetch_latest_private_repo_explains():
    def not_found(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
    with pytest.raises(UpdateError, match="Public"):
        updater.fetch_latest(urlopen=not_found)


def test_fetch_latest_offline():
    def offline(req, timeout):
        raise urllib.error.URLError("no network")
    with pytest.raises(UpdateError, match="الإنترنت"):
        updater.fetch_latest(urlopen=offline)


def test_download_verifies_size_and_hash(tmp_path):
    import hashlib
    good = Release("1.0.7", "https://example/x.exe", len(EXE), hashlib.sha256(EXE).hexdigest())
    seen = []
    dest = updater.download(good, tmp_path / "new.exe", progress=lambda d, t: seen.append(d),
                            urlopen=lambda req, timeout: response(EXE))
    assert dest.read_bytes() == EXE and seen[-1] == len(EXE)

    bad_hash = Release("1.0.7", "https://example/x.exe", len(EXE), "0" * 64)
    with pytest.raises(UpdateError, match="تالف"):
        updater.download(bad_hash, tmp_path / "x.exe", urlopen=lambda req, timeout: response(EXE))
    short = Release("1.0.7", "https://example/x.exe", len(EXE) + 1)
    with pytest.raises(UpdateError, match="ناقص"):
        updater.download(short, tmp_path / "y.exe", urlopen=lambda req, timeout: response(EXE))
    with pytest.raises(UpdateError, match="إلغاء"):
        updater.download(good, tmp_path / "z.exe", cancelled=lambda: True,
                         urlopen=lambda req, timeout: response(EXE))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["new.exe"]   # no leftovers


def test_install_swaps_files_and_cleanup(tmp_path):
    exe = tmp_path / "TelegramDownloader.exe"
    exe.write_bytes(b"old")
    new = tmp_path / "TelegramDownloader.exe.new"
    new.write_bytes(b"new")
    updater.install(new, exe)
    assert exe.read_bytes() == b"new"
    assert updater.old_path(exe).read_bytes() == b"old"
    updater.cleanup_old(exe)
    assert not updater.old_path(exe).exists()


def test_install_rolls_back_if_new_file_missing(tmp_path):
    exe = tmp_path / "TelegramDownloader.exe"
    exe.write_bytes(b"old")
    with pytest.raises(OSError):
        updater.install(tmp_path / "missing.exe", exe)
    assert exe.read_bytes() == b"old"


def test_restart_starts_a_fresh_onefile_process(monkeypatch):
    calls = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "C:/temp/_MEI123")
    monkeypatch.setenv("_MEIPASS2", "C:/temp/_MEI123")
    updater.restart("C:/apps/TelegramDownloader.exe")
    args, kw = calls[0]
    assert args == ["C:/apps/TelegramDownloader.exe"]
    assert kw["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert "_PYI_APPLICATION_HOME_DIR" not in kw["env"] and "_MEIPASS2" not in kw["env"]
