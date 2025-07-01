import os
import json
import pytest

from test_formatting import scraper_module


def test_ensure_storage_state_absent(tmp_path, monkeypatch, scraper_module):
    path = tmp_path / "state.json"
    monkeypatch.setattr(scraper_module, "STORAGE_STATE", str(path))
    assert scraper_module.ensure_storage_state() is False


def test_ensure_storage_state_invalid_json(tmp_path, monkeypatch, scraper_module):
    path = tmp_path / "state.json"
    path.write_text("{invalid")
    monkeypatch.setattr(scraper_module, "STORAGE_STATE", str(path))
    assert scraper_module.ensure_storage_state() is False


def test_ensure_storage_state_valid(tmp_path, monkeypatch, scraper_module):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"cookies": [1]}))
    monkeypatch.setattr(scraper_module, "STORAGE_STATE", str(path))
    assert bool(scraper_module.ensure_storage_state()) is True


class DummyPage:
    def __init__(self):
        self.called = []
        self.closed = False

    def is_closed(self):
        return self.closed

    async def screenshot(self, path, full_page=True, timeout=15000):
        self.called.append(path)


@pytest.mark.asyncio
async def test_save_screenshot(tmp_path, monkeypatch, scraper_module):
    dummy = DummyPage()
    monkeypatch.setattr(scraper_module, "OUTPUT_DIR", str(tmp_path))
    prefix = "testprefix"
    await scraper_module._save_screenshot(dummy, prefix)
    assert len(dummy.called) == 1
    saved = dummy.called[0]
    assert os.path.commonpath([saved, str(tmp_path)]) == str(tmp_path)
    assert os.path.basename(saved).startswith(prefix + "_")
