import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Minimal stubs so scraper.py can be imported without optional deps
MODULES_TO_STUB = {
    "pytz": {"timezone": lambda name: None},
    "pyotp": {},
    "aiohttp": {},
    "aiofiles": {},
    "certifi": {},
    "playwright.async_api": {
        "async_playwright": None,
        "Browser": object,
        "BrowserContext": object,
        "Page": object,
        "TimeoutError": Exception,
        "expect": None,
    },
}


@pytest.fixture(scope="module")
def scraper_module():
    config_path = ROOT / "config.json"
    example_path = ROOT / "config.example.json"
    config_path.write_text(example_path.read_text())

    inserted = set()
    for name, attrs in MODULES_TO_STUB.items():
        parts = name.split(".")
        pkg = parts[0]
        if pkg not in sys.modules:
            sys.modules[pkg] = types.ModuleType(pkg)
            inserted.add(pkg)
        mod = sys.modules[pkg]
        if len(parts) > 1:
            if name not in sys.modules:
                submod = types.ModuleType(name)
                setattr(mod, parts[1], submod)
                sys.modules[name] = submod
                inserted.add(name)
            mod = sys.modules[name]
        else:
            if name not in sys.modules:
                sys.modules[name] = mod
                inserted.add(name)
            mod = sys.modules[name]
        for attr, val in attrs.items():
            setattr(mod, attr, val)

    spec = importlib.util.spec_from_file_location("scraper", ROOT / "scraper.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module

    config_path.unlink(missing_ok=True)
    sys.modules.pop("scraper", None)
    for name in inserted:
        sys.modules.pop(name, None)


def test_format_metric_with_emoji_uph(scraper_module):
    fn = scraper_module._format_metric_with_emoji
    result = fn("90", scraper_module.UPH_THRESHOLD, is_uph=True)
    assert result == f"90 {scraper_module.EMOJI_GREEN_CHECK}"


def test_format_metric_with_emoji_bad(scraper_module):
    fn = scraper_module._format_metric_with_emoji
    result = fn("70", scraper_module.UPH_THRESHOLD, is_uph=True)
    assert result == f"70 {scraper_module.EMOJI_RED_CROSS}"


def test_format_metric_with_emoji_invalid(scraper_module):
    fn = scraper_module._format_metric_with_emoji
    assert fn("N/A", scraper_module.UPH_THRESHOLD, is_uph=True) == "N/A"


def test_format_metric_with_color_good(scraper_module):
    fn = scraper_module._format_metric_with_color
    res = fn("<b>UPH:</b> 90", scraper_module.UPH_THRESHOLD, is_uph=True)
    assert res == f'<font color="{scraper_module.COLOR_GOOD}"><b>UPH:</b> 90</font>'


def test_format_metric_with_color_bad(scraper_module):
    fn = scraper_module._format_metric_with_color
    res = fn("<b>UPH:</b> 70", scraper_module.UPH_THRESHOLD, is_uph=True)
    assert res == f'<font color="{scraper_module.COLOR_BAD}"><b>UPH:</b> 70</font>'


def test_format_metric_with_color_invalid(scraper_module):
    fn = scraper_module._format_metric_with_color
    assert fn("--", scraper_module.UPH_THRESHOLD, is_uph=True) == "--"
