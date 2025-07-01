# =======================================================================================
#               AMAZON SELLER CENTRAL SCRAPER (SIMPLIFIED VERSION)
# =======================================================================================
# This version scrapes a single, hardcoded store and posts a simple summary to chat.
# =======================================================================================

import logging
import urllib.parse
from datetime import datetime
from pytz import timezone
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError,
    expect,
)
import os
import json
import asyncio
import pyotp
from logging.handlers import RotatingFileHandler
import re
import aiohttp
import aiofiles
import ssl
import certifi
import io

# Use UK timezone for log timestamps
LOCAL_TIMEZONE = timezone('Europe/London')

class LocalTimeFormatter(logging.Formatter):
    def converter(self, ts: float):
        dt = datetime.fromtimestamp(ts, LOCAL_TIMEZONE)
        return dt.timetuple()

# --- Logging Setup ---
def setup_logging():
    app_logger = logging.getLogger('app')
    app_logger.setLevel(logging.INFO)
    app_file = RotatingFileHandler('app.log', maxBytes=10**7, backupCount=5)
    fmt = LocalTimeFormatter('%(asctime)s %(levelname)s %(message)s')
    app_file.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    app_logger.addHandler(app_file)
    app_logger.addHandler(console)
    return app_logger

app_logger = setup_logging()

# --- Config & Constants ---
try:
    with open('config.json', 'r') as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    app_logger.critical("config.json not found. Please create it before running.")
    exit(1)

DEBUG_MODE       = config.get('debug', False)
LOGIN_URL        = config['login_url']
CHAT_WEBHOOK_URL = config.get('chat_webhook_url')
TARGET_STORE     = config['target_store']  # MODIFIED: Get store info directly from config

# --- Emojis for Chat ---
EMOJI_GREEN_CHECK = "\u2705" # ✅
EMOJI_RED_CROSS = "\u274C"   # ❌
UPH_THRESHOLD = 80
LATES_THRESHOLD = 3.0
INF_THRESHOLD = 2.0

# REMOVED: All Google Form related constants (FORM_POST_URL, FIELD_MAP)
# REMOVED: All concurrency and worker-related constants and globals

LOG_FILE        = os.path.join('output', 'submissions.log')
JSON_LOG_FILE   = os.path.join('output', 'submissions.jsonl')
STORAGE_STATE   = 'state.json'
OUTPUT_DIR      = 'output'
os.makedirs(OUTPUT_DIR, exist_ok=True)

PAGE_TIMEOUT    = 90000
ACTION_TIMEOUT  = 30000
WAIT_TIMEOUT    = 20000
WORKER_RETRY_COUNT = 3

playwright = None
browser = None
log_lock = asyncio.Lock()


# --- Utility Functions ---

async def _save_screenshot(page: Page | None, prefix: str):
    if not page or page.is_closed(): return
    try:
        path = os.path.join(OUTPUT_DIR, f"{prefix}_{datetime.now(LOCAL_TIMEZONE).strftime('%Y%m%d_%H%M%S')}.png")
        await page.screenshot(path=path, full_page=True, timeout=15000)
        app_logger.info(f"Screenshot saved for debugging: {path}")
    except Exception as e:
        app_logger.error(f"Failed to save screenshot with prefix '{prefix}': {e}")

def ensure_storage_state():
    if not os.path.exists(STORAGE_STATE) or os.path.getsize(STORAGE_STATE) == 0: return False
    try:
        with open(STORAGE_STATE) as f: data = json.load(f)
        return isinstance(data, dict) and "cookies" in data and data["cookies"]
    except json.JSONDecodeError:
        return False

# REMOVED: load_default_data() - we no longer use urls.csv
# REMOVED: auto_concurrency_manager() and all related logic

# --- Authentication ---
async def check_if_login_needed(page: Page, test_url: str) -> bool:
    app_logger.info(f"Verifying session status by navigating to: {test_url}")
    try:
        await page.goto(test_url, timeout=PAGE_TIMEOUT, wait_until="load")
        if "signin" in page.url.lower() or "/ap/" in page.url: return True
        await expect(page.locator("#content")).to_be_visible(timeout=WAIT_TIMEOUT)
        app_logger.info("Session check successful.")
        return False
    except Exception as e:
        app_logger.error(f"Error during session check: {e}", exc_info=DEBUG_MODE)
        return True

async def perform_login(page: Page) -> bool:
    app_logger.info(f"Navigating to login page: {LOGIN_URL}")
    try:
        await page.goto(LOGIN_URL, timeout=PAGE_TIMEOUT, wait_until="load")
        await page.get_by_label("Email or mobile phone number").fill(config['login_email'])
        await page.get_by_label("Continue").click()
        await page.get_by_label("Password").fill(config['login_password'])
        await page.get_by_label("Sign in").click()
        
        otp_selector = 'input[id*="otp"]'
        dashboard_selector = "#content"
        await page.wait_for_selector(f"{otp_selector}, {dashboard_selector}", timeout=30000)

        if await page.locator(otp_selector).is_visible():
            app_logger.info("OTP is required.")
            otp_code = pyotp.TOTP(config['otp_secret_key']).now()
            await page.locator(otp_selector).fill(otp_code)
            await page.get_by_role("button", name="Sign in").click()

        await page.wait_for_selector(dashboard_selector, timeout=30000)
        app_logger.info("Login process appears successful.")
        return True
    except Exception as e:
        app_logger.critical(f"Critical error during login: {e}", exc_info=DEBUG_MODE)
        await _save_screenshot(page, "login_critical_failure")
        return False

async def prime_master_session() -> bool:
    global browser
    app_logger.info("Priming master session")
    ctx = await browser.new_context()
    try:
        page = await ctx.new_page()
        if not await perform_login(page): return False
        await ctx.storage_state(path=STORAGE_STATE)
        app_logger.info(f"Login successful. Auth state saved to '{STORAGE_STATE}'.")
        return True
    finally:
        await ctx.close()


# --- Core Logic ---

def _format_metric_with_emoji(value_str: str, threshold: float, is_uph: bool = False) -> str:
    """Applies a pass/fail emoji to a metric string based on a threshold."""
    try:
        numeric_value = float(re.sub(r'[^\d.]', '', value_str))
        is_good = (numeric_value >= threshold) if is_uph else (numeric_value <= threshold)
        emoji = EMOJI_GREEN_CHECK if is_good else EMOJI_RED_CROSS
        return f"{value_str} {emoji}"
    except (ValueError, TypeError):
        return value_str

# MODIFIED: Chat message is now a simple text summary.
async def post_to_chat_webhook(data: dict):
    """Sends a simple summary message to Google Chat."""
    if not CHAT_WEBHOOK_URL: return
    
    store_name = data.get("store", "Unknown Store")
    uph = _format_metric_with_emoji(data.get("uph", "N/A"), UPH_THRESHOLD, is_uph=True)
    lates = _format_metric_with_emoji(data.get("lates", "0.0 %"), LATES_THRESHOLD)
    inf = _format_metric_with_emoji(data.get("inf", "0.0 %"), INF_THRESHOLD)
    
    timestamp = datetime.now(LOCAL_TIMEZONE).strftime("%A %d %B, %H:%M")
    
    message_text = (
        f"*{store_name} Metrics Report* ({timestamp})\n"
        f"  •  *UPH:* {uph}\n"
        f"  •  *Lates:* {lates}\n"
        f"  •  *INF:* {inf}\n"
        f"  •  *Orders:* {data.get('orders', 'N/A')}\n"
        f"  •  *Units:* {data.get('units', 'N/A')}"
    )

    payload = {"text": message_text}
    
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.post(CHAT_WEBHOOK_URL, json=payload) as resp:
                if resp.status != 200:
                    app_logger.error(f"Chat webhook failed. Status: {resp.status}, Response: {await resp.text()}")
    except Exception as e:
        app_logger.error(f"Error posting to chat webhook: {e}", exc_info=DEBUG_MODE)

# MODIFIED: Renamed from log_submission and simplified. No longer interacts with chat.
async def log_results(data: dict):
    """Writes results to local log files."""
    async with log_lock:
        log_entry = {'timestamp': datetime.now(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'), **data}
        try:
            async with aiofiles.open(JSON_LOG_FILE, 'a', encoding='utf-8') as f:
                await f.write(json.dumps(log_entry) + '\n')
        except IOError as e:
            app_logger.error(f"Error writing to JSON log file {JSON_LOG_FILE}: {e}")

# MODIFIED: This is the core scraping logic, now returns data instead of queuing it.
async def scrape_store_data(browser: Browser, store_info: dict, storage_state: dict) -> dict | None:
    store_name = store_info['store_name']
    app_logger.info(f"Starting data collection for '{store_name}'")
    
    for attempt in range(WORKER_RETRY_COUNT):
        ctx: BrowserContext = None
        try:
            ctx = await browser.new_context(storage_state=storage_state)
            page = await ctx.new_page()
            
            dash_url = f"https://sellercentral.amazon.co.uk/snowdash?mons_sel_dir_mcid={store_info['merchant_id']}&mons_sel_mkid={store_info['marketplace_id']}"
            await page.goto(dash_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
            
            refresh_button = page.locator('button:has-text("Refresh")')
            await expect(refresh_button).to_be_visible(timeout=WAIT_TIMEOUT)
            
            async with page.expect_response(lambda r: "summationMetrics" in r.url, timeout=40_000) as resp_info:
                await refresh_button.click()
            
            api_data = await (await resp_info.value).json()
            
            # Scrape 'Lates' from the table header
            formatted_lates = "0 %"
            try:
                lates_cell = page.locator("kat-table-head kat-table-row").nth(1).locator("kat-table-cell").nth(10)
                await expect(lates_cell).to_be_visible(timeout=10000)
                cell_text = (await lates_cell.text_content() or "").strip()
                if re.fullmatch(r"\d+(\.\d+)?\s*%", cell_text):
                    formatted_lates = cell_text
            except Exception:
                app_logger.warning(f"Could not scrape 'Lates' value for {store_name}, defaulting to 0 %.")

            milliseconds = float(api_data.get('TimeAvailable_V2', 0.0))
            hours, rem = divmod(int(milliseconds / 1000), 3600)
            minutes, _ = divmod(rem, 60)
            
            # This is the dictionary of results
            results = {
                'store': store_name,
                'orders': str(api_data.get('OrdersShopped_V2', 0)),
                'units': str(api_data.get('RequestedQuantity_V2', 0)),
                'uph': f"{api_data.get('AverageUPH_V2', 0.0):.0f}",
                'inf': f"{api_data.get('ItemNotFoundRate_V2', 0.0):.1f} %",
                'lates': formatted_lates,
                'time_available': f"{hours}:{minutes:02d}"
            }
            app_logger.info(f"Data collection complete for {store_name}.")
            return results
        except Exception as e:
            app_logger.warning(f"Scrape attempt {attempt + 1} for {store_name} failed: {e}", exc_info=False)
            if attempt >= WORKER_RETRY_COUNT - 1:
                app_logger.error(f"All scrape attempts failed for {store_name}.")
                await _save_screenshot(page, f"{store_name}_scrape_failure")
                return None
        finally:
            if ctx: await ctx.close()
    return None

async def main():
    global playwright, browser
    app_logger.info("Starting up in simplified single-store mode...")
    
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=not DEBUG_MODE)
        
        # --- Session Check and Login ---
        if not ensure_storage_state() or not await prime_master_session():
             if not await prime_master_session():
                app_logger.critical("Fatal: Could not establish a login session. Aborting.")
                return

        with open(STORAGE_STATE) as f:
            storage_state = json.load(f)

        # --- Scrape, Log, and Post ---
        scraped_data = await scrape_store_data(browser, TARGET_STORE, storage_state)

        if scraped_data:
            await log_results(scraped_data)
            await post_to_chat_webhook(scraped_data)
            app_logger.info("Run completed successfully.")
        else:
            app_logger.error("Run failed: Could not retrieve data for the target store.")

    except Exception as e:
        app_logger.critical(f"A critical error occurred in main execution: {e}", exc_info=True)
    finally:
        app_logger.info("Shutting down...")
        if browser: await browser.close()
        if playwright: await playwright.stop()
        app_logger.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())