# =======================================================================================
#         AMAZON SELLER CENTRAL SCRAPER (FINAL VERSION V3.6 - WIDGET FIX)
# =======================================================================================
# This version replaces the fragile text-padding alignment with a robust,
# widget-based approach (`decoratedText`) for the per-shopper breakdown.
# This guarantees a clean, readable layout on both desktop and mobile.
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
TARGET_STORE     = config['target_store']

# --- Emojis and Colors for Chat ---
EMOJI_GREEN_CHECK = "\u2705" # ✅
EMOJI_RED_CROSS = "\u274C"   # ❌
COLOR_GOOD = "#2E8B57"  # A nice green (SeaGreen)
COLOR_BAD = "#CD5C5C"   # A softer red (IndianRed)

UPH_THRESHOLD = 80
LATES_THRESHOLD = 3.0
INF_THRESHOLD = 2.0

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
    try:
        numeric_value = float(re.sub(r'[^\d.]', '', value_str))
        is_good = (numeric_value >= threshold) if is_uph else (numeric_value <= threshold)
        emoji = EMOJI_GREEN_CHECK if is_good else EMOJI_RED_CROSS
        return f"{value_str} {emoji}"
    except (ValueError, TypeError):
        return value_str
        
def _format_metric_with_color(value_str: str, threshold: float, is_uph: bool = False) -> str:
    """Formats a metric string with red/green font color based on a threshold."""
    try:
        numeric_value = float(re.sub(r'[^\d.]', '', value_str))
        is_good = (numeric_value >= threshold) if is_uph else (numeric_value <= threshold)
        color = COLOR_GOOD if is_good else COLOR_BAD
        return f'<font color="{color}">{value_str}</font>'
    except (ValueError, TypeError):
        return value_str

async def log_results(data: dict):
    """Writes results to local log files."""
    async with log_lock:
        log_entry = {'timestamp': datetime.now(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S'), **data}
        try:
            async with aiofiles.open(JSON_LOG_FILE, 'a', encoding='utf-8') as f:
                await f.write(json.dumps(log_entry) + '\n')
        except IOError as e:
            app_logger.error(f"Error writing to JSON log file {JSON_LOG_FILE}: {e}")

async def scrape_store_data(browser: Browser, store_info: dict, storage_state: dict) -> dict | None:
    store_name = store_info['store_name']
    app_logger.info(f"Starting detailed data collection for '{store_name}'")

    for attempt in range(WORKER_RETRY_COUNT):
        ctx: BrowserContext = None
        page: Page = None
        try:
            ctx = await browser.new_context(storage_state=storage_state)
            page = await ctx.new_page()

            # Navigate to the store dashboard
            dash_url = (
                f"https://sellercentral.amazon.co.uk/snowdash"
                f"?mons_sel_dir_mcid={store_info['merchant_id']}"
                f"&mons_sel_mkid={store_info['marketplace_id']}"
            )
            app_logger.info(f"Navigating to base URL: {dash_url}")
            await page.goto(dash_url, timeout=PAGE_TIMEOUT)

            # STEP 1: Wait for the "Refresh" button to ensure the dashboard is loaded
            app_logger.info("Waiting for dashboard to be ready…")
            refresh_button = page.get_by_role("button", name="Refresh")
            await expect(refresh_button).to_be_visible(timeout=WAIT_TIMEOUT)
            app_logger.info("Dashboard is ready.")

            # STEP 2: Click the "Customised" tab via your precise CSS selector
            customised_tab = page.locator(
                "#content > div > div.mainAppContainerExternal > div.paddingTop "
                "> div > div > div > div > span:nth-child(4)"
            )
            await expect(customised_tab).to_be_visible(timeout=WAIT_TIMEOUT)
            await customised_tab.scroll_into_view_if_needed(timeout=ACTION_TIMEOUT)
            await customised_tab.click(timeout=ACTION_TIMEOUT)
            app_logger.info("Clicked 'Customised' tab.")

            # STEP 3: Wait for the date-range picker to appear
            date_picker = page.locator("kat-date-range-picker")
            await expect(date_picker).to_be_visible(timeout=WAIT_TIMEOUT)
            app_logger.info("Date-range picker is visible.")

            # STEP 4: Fill in today's date for both start and end
            now = datetime.now(LOCAL_TIMEZONE).strftime("%m/%d/%Y")
            date_inputs = date_picker.locator('input[type="text"]')
            await date_inputs.nth(0).fill(now)
            await date_inputs.nth(1).fill(now)
            app_logger.info(f"Filled date fields with: {now}")

            # STEP 5: Click "Apply" and capture its /api/metrics response
            apply_btn = page.get_by_role("button", name="Apply")
            async with page.expect_response(lambda r: "/api/metrics" in r.url, timeout=30000) as apply_info:
                await apply_btn.click(timeout=ACTION_TIMEOUT)
            apply_response = await apply_info.value
            api_data = await apply_response.json()
            app_logger.info("Received /api/metrics response after Apply.")

            # STEP 6: Click "Refresh" and capture its /api/metrics response
            async with page.expect_response(lambda r: "/api/metrics" in r.url, timeout=40000) as refresh_info:
                await refresh_button.click(timeout=ACTION_TIMEOUT)
            refresh_response = await refresh_info.value
            api_data = await refresh_response.json()
            app_logger.info("Received /api/metrics response after Refresh.")

            # --- Process api_data as before ---
            shopper_stats = []
            store_total_units_picked = store_total_pick_time_sec = store_total_orders = 0
            store_total_requested_units = store_total_items_not_found = store_total_weighted_lates = 0

            for entry in api_data:
                metrics = entry.get("metrics", {})
                shopper_name = entry.get("shopperName")
                if entry.get("type") == "MASTER" and shopper_name and shopper_name != "SHOPPER_NAME_NOT_FOUND":
                    orders = metrics.get("OrdersShopped_V2", 0)
                    if orders == 0:
                        continue
                    units = metrics.get("PickedUnits_V2", 0)
                    pick_time_sec = metrics.get("PickTimeInSec_V2", 0)
                    uph = (units / (pick_time_sec / 3600)) if pick_time_sec > 0 else 0.0

                    shopper_stats.append({
                        "name": shopper_name,
                        "uph": f"{uph:.0f}",
                        "inf": f"{metrics.get('ItemNotFoundRate_V2', 0.0):.1f} %",
                        "lates": f"{metrics.get('LatePicksRate', 0.0):.1f} %",
                        "orders": int(orders),
                    })

                    store_total_units_picked += units
                    store_total_pick_time_sec += pick_time_sec
                    store_total_orders += orders

                    req_units = metrics.get("RequestedQuantity_V2", 0)
                    inf_rate = metrics.get("ItemNotFoundRate_V2", 0.0) / 100.0
                    store_total_requested_units += req_units
                    store_total_items_not_found += req_units * inf_rate
                    lates_rate = metrics.get("LatePicksRate", 0.0) / 100.0
                    store_total_weighted_lates += lates_rate * orders

            if not shopper_stats:
                app_logger.warning(f"No active shoppers found for {store_name}.")
                return None

            overall_uph = (store_total_units_picked / (store_total_pick_time_sec / 3600)) if store_total_pick_time_sec > 0 else 0
            overall_inf = (store_total_items_not_found / store_total_requested_units) * 100 if store_total_requested_units > 0 else 0
            overall_lates = (store_total_weighted_lates / store_total_orders) * 100 if store_total_orders > 0 else 0

            overall_metrics = {
                'store': store_name,
                'orders': str(int(store_total_orders)),
                'units': str(int(store_total_units_picked)),
                'uph': f"{overall_uph:.0f}",
                'inf': f"{overall_inf:.1f} %",
                'lates': f"{overall_lates:.1f} %"
            }
            sorted_shoppers = sorted(shopper_stats, key=lambda x: x['name'])
            app_logger.info(f"Aggregated data for {len(sorted_shoppers)} shoppers in {store_name}.")

            return {"overall": overall_metrics, "shoppers": sorted_shoppers}

        except Exception as e:
            app_logger.warning(f"Attempt {attempt+1} failed for {store_name}: {e}", exc_info=True)
            if attempt == WORKER_RETRY_COUNT - 1:
                app_logger.error(f"All attempts failed for {store_name}.")
                await _save_screenshot(page, f"{store_name}_error")
        finally:
            if ctx:
                await ctx.close()

    return None

async def post_to_chat_webhook(data: dict):
    """Sends a rich card using decoratedText widgets for a clean, mobile-friendly layout."""
    if not CHAT_WEBHOOK_URL:
        return

    overall, shoppers = data.get("overall"), data.get("shoppers")
    if not overall or not shoppers:
        app_logger.warning("post_to_chat_webhook called with incomplete data.")
        return

    store_name = overall.get("store", "Unknown Store")
    timestamp = datetime.now(LOCAL_TIMEZONE).strftime("%A %d %B, %H:%M")
    
    summary_text = (f"  •  <b>UPH (Store Avg):</b> {_format_metric_with_emoji(overall.get('uph'), UPH_THRESHOLD, is_uph=True)}<br>"
                    f"  •  <b>Lates (Store Avg):</b> {_format_metric_with_emoji(overall.get('lates'), LATES_THRESHOLD)}<br>"
                    f"  •  <b>INF (Store Avg):</b> {_format_metric_with_emoji(overall.get('inf'), INF_THRESHOLD)}<br>"
                    f"  •  <b>Total Orders:</b> {overall.get('orders')}<br>"
                    f"  •  <b>Total Units:</b> {overall.get('units')}")

    # --- Build the per-shopper breakdown using a list of decoratedText widgets ---
    shopper_widgets = []
    for s in shoppers:
        # Format each metric with its label and color, then combine them.
        uph_formatted = _format_metric_with_color(f"<b>UPH:</b> {s['uph']}", UPH_THRESHOLD, is_uph=True)
        inf_formatted = _format_metric_with_color(f"<b>INF:</b> {s['inf']}", INF_THRESHOLD)
        lates_formatted = _format_metric_with_color(f"<b>Lates:</b> {s['lates']}", LATES_THRESHOLD)
        
        metrics_text = f"{uph_formatted} | {inf_formatted} | {lates_formatted}"
        
        # Each shopper gets their own widget object.
        widget = {
            "decoratedText": {
                "icon": {"knownIcon": "PERSON"},
                "topLabel": f"<b>{s['name']}</b> ({s['orders']} Orders)",
                "text": metrics_text
            }
        }
        shopper_widgets.append(widget)

    # Construct the final payload
    payload = {
        "cardsV2": [{
            "cardId": f"store-summary-{store_name.replace(' ', '-')}",
            "card": {
                "header": {
                    "title": f"{store_name} Metrics Report",
                    "subtitle": timestamp,
                    "imageUrl": "https://i.imgur.com/u0e3d2x.png",
                    "imageType": "CIRCLE"
                },
                "sections": [
                    {
                        "header": "Store-Wide Performance (Weighted Avg)",
                        "widgets": [{"textParagraph": {"text": summary_text}}]
                    },
                    {
                        "header": f"Per-Shopper Breakdown ({len(shoppers)} Active)",
                        "collapsible": True,
                        "uncollapsibleWidgetsCount": 0,
                        "widgets": shopper_widgets  # Insert the list of widgets here
                    }
                ]
            }
        }]
    }

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.post(CHAT_WEBHOOK_URL, json=payload) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    app_logger.error(f"Chat webhook failed. Status: {resp.status}, Response: {error_text}")
    except Exception as e:
        app_logger.error(f"Error posting to chat webhook: {e}", exc_info=True)


async def main():
    global playwright, browser
    app_logger.info("Starting up in simplified single-store mode...")
    
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=not DEBUG_MODE)
        if not ensure_storage_state():
             if not await prime_master_session():
                app_logger.critical("Fatal: Could not establish a login session. Aborting.")
                return
        with open(STORAGE_STATE) as f:
            storage_state = json.load(f)
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