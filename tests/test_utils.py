import json
import asyncio
from pathlib import Path

import pytest
import types

from test_formatting import scraper_module


class DummyPage:
    def __init__(self):
        self.closed = False
        self.called = False
        self.args = None

    def is_closed(self):
        return self.closed

    async def screenshot(self, path, full_page=True, timeout=0):
        self.called = True
        self.args = (path, full_page, timeout)
        Path(path).write_text("dummy")


def test_ensure_storage_state(tmp_path, scraper_module, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(scraper_module, "STORAGE_STATE", str(state_file))

    # Missing file
    assert scraper_module.ensure_storage_state() is False

    # Empty file
    state_file.write_text("")
    assert scraper_module.ensure_storage_state() is False

    # Invalid JSON
    state_file.write_text("{")
    assert scraper_module.ensure_storage_state() is False

    # Missing cookies
    state_file.write_text(json.dumps({"foo": 1}))
    assert scraper_module.ensure_storage_state() is False

    # Valid cookies
    cookies = [{"name": "a"}]
    state_file.write_text(json.dumps({"cookies": cookies}))
    assert scraper_module.ensure_storage_state() == cookies


def test_save_screenshot(tmp_path, scraper_module, monkeypatch):
    page = DummyPage()
    monkeypatch.setattr(scraper_module, "OUTPUT_DIR", str(tmp_path))

    asyncio.run(scraper_module._save_screenshot(page, "test"))

    assert page.called is True
    saved_path = Path(page.args[0])
    assert saved_path.exists()
    assert saved_path.parent == tmp_path
    assert saved_path.name.startswith("test_") and saved_path.suffix == ".png"


def test_save_screenshot_closed_page(scraper_module):
    page = DummyPage()
    page.closed = True
    asyncio.run(scraper_module._save_screenshot(page, "ignored"))
    assert page.called is False


def test_log_results(tmp_path, scraper_module, monkeypatch):
    log_file = tmp_path / "submissions.jsonl"
    monkeypatch.setattr(scraper_module, "JSON_LOG_FILE", str(log_file))

    class DummyAioFile:
        def __init__(self, path):
            self._path = path
        async def __aenter__(self):
            self._f = open(self._path, "a", encoding="utf-8")
            return self
        async def __aexit__(self, exc_type, exc, tb):
            self._f.close()
        async def write(self, data):
            self._f.write(data)

    def dummy_open(path, mode="r", encoding=None):
        return DummyAioFile(path)

    monkeypatch.setattr(scraper_module, "aiofiles", types.SimpleNamespace(open=dummy_open))

    data = {"foo": "bar"}
    asyncio.run(scraper_module.log_results(data))

    content = log_file.read_text().strip()
    line = json.loads(content)
    assert line["foo"] == "bar"
    assert "timestamp" in line


def test_save_screenshot_error(tmp_path, scraper_module, monkeypatch, caplog):
    class BadPage(DummyPage):
        async def screenshot(self, *args, **kwargs):
            raise RuntimeError("fail")

    page = BadPage()
    monkeypatch.setattr(scraper_module, "OUTPUT_DIR", str(tmp_path))
    with caplog.at_level(scraper_module.logging.ERROR, logger="app"):
        asyncio.run(scraper_module._save_screenshot(page, "err"))

    assert any("Failed to save screenshot" in r.message for r in caplog.records)


def test_log_results_error(scraper_module, monkeypatch, caplog):
    class FailingFile:
        async def __aenter__(self):
            raise IOError("boom")
        async def __aexit__(self, exc_type, exc, tb):
            pass

    def failing_open(*args, **kwargs):
        return FailingFile()

    monkeypatch.setattr(scraper_module, "aiofiles", types.SimpleNamespace(open=failing_open))
    with caplog.at_level(scraper_module.logging.ERROR, logger="app"):
        asyncio.run(scraper_module.log_results({"a": 1}))

    assert any("Error writing to JSON log file" in r.message for r in caplog.records)
