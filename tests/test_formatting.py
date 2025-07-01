import importlib.util
import sys
import json
from pathlib import Path
import types
from zoneinfo import ZoneInfo
import pytest

@pytest.fixture
def scraper_module(tmp_path, monkeypatch):
    """Import scraper.py with a temporary config.json."""
    root_dir = Path(__file__).resolve().parents[1]
    config = {
        "debug": False,
        "login_url": "https://example.com/login",
        "login_email": "user",
        "login_password": "pass",
        "otp_secret_key": "secret",
        "target_store": {"merchant_id": "1", "marketplace_id": "1", "store_name": "Test"}
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.chdir(tmp_path)

    # Provide minimal stand-ins for missing third-party modules
    fake_pytz = types.ModuleType("pytz")
    fake_pytz.timezone = ZoneInfo
    fake_pyotp = types.ModuleType("pyotp")
    class DummyTOTP:
        def __init__(self, *a, **k):
            pass
        def now(self):
            return "000000"
    fake_pyotp.TOTP = DummyTOTP
    fake_playwright = types.ModuleType("playwright")
    fake_async_api = types.ModuleType("playwright.async_api")
    fake_async_api.async_playwright = lambda: None
    fake_async_api.Browser = object
    fake_async_api.BrowserContext = object
    fake_async_api.Page = object
    fake_async_api.TimeoutError = Exception
    fake_async_api.expect = lambda *a, **k: None
    fake_playwright.async_api = fake_async_api

    for name, mod in {
        "pytz": fake_pytz,
        "pyotp": fake_pyotp,
        "playwright": fake_playwright,
        "playwright.async_api": fake_async_api,
        "aiohttp": types.ModuleType("aiohttp"),
        "aiofiles": types.ModuleType("aiofiles"),
        "certifi": types.ModuleType("certifi"),
    }.items():
        sys.modules.setdefault(name, mod)

    spec = importlib.util.spec_from_file_location("scraper", root_dir / "scraper.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["scraper"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("scraper", None)
