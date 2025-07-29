# =======================================================================================
#         AMAZON SELLER CENTRAL SCRAPER (FINAL VERSION V3.7 – ENHANCED LOGIN)
# =======================================================================================
# - Swapped in the robust login & session checks from the INF-items scraper
# - Retained widget-based decoratedText output for per-shopper breakdown
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

DEBUG_MODE               = config.get('debug', False)
LOGIN_URL                = config['login_url']
CHAT_WEBHOOK_URL         = config.get('chat_webhook_url')
SUMMARY_CHAT_WEBHOOK_URL = config.get('summary_chat_webhook_url')
TARGET_STORE             = config['target_store']

# --- Emojis and Colors for Chat ---
EMOJI_GREEN_CHECK = "\u2705"  # ✅
EMOJI_RED_CROSS   = "\u274C"  # ❌
COLOR_GOOD        = "#2E8B57" 
COLOR_BAD         = "#CD5C5C"

UPH_THRESHOLD    = 80
LATES_THRESHOLD = 3.0
INF_THRESHOLD   = 2.0

JSON_LOG_FILE = os.path.join('output', 'submissions.jsonl')
STORAGE_STATE  = 'state.json'
OUTPUT_DIR     = 'output'
os.makedirs(OUTPUT_DIR, exist_ok=True)

PAGE_TIMEOUT       = 90_000
ACTION_TIMEOUT     = 45_000
WAIT_TIMEOUT       = 45_000
WORKER_RETRY_COUNT = 3

playwright = None
browser    = None
log_lock   = asyncio.Lock()


# =======================================================================================
#    AUTHENTICATION & SESSION MANAGEMENT (ENHANCED)
# =======================================================================================

async def _save_screenshot(page: Page | None, prefix: str):
    if not page or page.is_closed():
        return
    try:
        path = os.path.join(
            OUTPUT_DIR,
            f"{prefix}_{datetime.now(LOCAL_TIMEZONE).strftime('%Y%m%d_%H%M%S')}.png"
        )
        await page.screenshot(path=path, full_page=True, timeout=15000)
        app_logger.info(f"Screenshot saved: {path}")
    except Exception as e:
        app_logger.error(f"Failed to save screenshot: {e}")

def ensure_storage_state() -> list | bool:
    """Return stored cookies or False if the storage state is invalid."""
    if not os.path.exists(STORAGE_STATE) or os.path.getsize(STORAGE_STATE) == 0:
        return False
    try:
        with open(STORAGE_STATE, "r", encoding="utf-8") as f:
            data = json.load(f)
        cookies = data.get("cookies") if isinstance(data, dict) else None
        if isinstance(cookies, list) and cookies:
            return cookies
        return False
    except Exception:
        return False

async def check_if_login_needed(page: Page, test_url: str) -> bool:
    """Navigate to a protected page and see if we get redirected to sign-in."""
    try:
        await page.goto(test_url, timeout=PAGE_TIMEOUT, wait_until="load")
        if "signin" in page.url.lower() or "/ap/" in page.url:
            app_logger.info("Session invalid, login required.")
            return True
        await expect(page.locator("#range-selector")).to_be_visible(timeout=WAIT_TIMEOUT)
        app_logger.info("Existing session still valid.")
        return False
    except Exception:
        app_logger.warning("Error verifying session; assuming login required.")
        return True

async def perform_login(page: Page) -> bool:
    """Perform the full email → password → OTP flow, with account‐picker detection."""
    app_logger.info("Starting login flow")
    try:
        await page.goto(LOGIN_URL, timeout=PAGE_TIMEOUT, wait_until="load")
        cont_input = 'input[type="submit"][aria-labelledby="continue-announce"]'
        cont_btn   = 'button:has-text("Continue shopping")'
        email_sel  = 'input#ap_email'
        await page.wait_for_selector(f"{cont_input}, {cont_btn}, {email_sel}", timeout=ACTION_TIMEOUT)
        if await page.locator(cont_input).is_visible():
            await page.locator(cont_input).click()
        elif await page.locator(cont_btn).is_visible():
            await page.locator(cont_btn).click()

        await expect(page.locator(email_sel)).to_be_visible(timeout=WAIT_TIMEOUT)
        await page.get_by_label("Email or mobile phone number").fill(config['login_email'])
        await page.get_by_label("Continue").click()

        pw = page.get_by_label("Password")
        await expect(pw).to_be_visible(timeout=WAIT_TIMEOUT)
        await pw.fill(config['login_password'])
        await page.get_by_label("Sign in").click()

        otp_sel  = 'input[id*="otp"]'
        dash_sel = "#content"
        acct_sel = 'h1:has-text("Select an account")'
        await page.wait_for_selector(f"{otp_sel}, {dash_sel}, {acct_sel}", timeout=WAIT_TIMEOUT)

        if await page.locator(otp_sel).is_visible():
            code = pyotp.TOTP(config['otp_secret_key']).now()
            await page.locator(otp_sel).fill(code)
            await page.get_by_role("button", name="Sign in").click()
            await page.wait_for_selector(f"{dash_sel}, {acct_sel}", timeout=WAIT_TIMEOUT)

        if await page.locator(acct_sel).is_visible():
            app_logger.info("Account-picker shown; navigating to target store.")
            dash_url = (
                f"https://sellercentral.amazon.co.uk/snowdash"
                f"?mons_sel_dir_mcid={TARGET_STORE['merchant_id']}"
                f"&mons_sel_mkid={TARGET_STORE['marketplace_id']}"
            )
            await page.goto(dash_url, timeout=PAGE_TIMEOUT)
            await expect(page.locator(dash_sel)).to_be_visible(timeout=WAIT_TIMEOUT)
            await _save_screenshot(page, "login_account_picker")
            app_logger.info("Navigated to store from account-picker page.")
            return True

        await expect(page.locator(dash_sel)).to_be_visible(timeout=WAIT_TIMEOUT)
        app_logger.info("Login successful.")
        return True

    except Exception as e:
        app_logger.critical(f"Login failed: {e}", exc_info=DEBUG_MODE)
        await _save_screenshot(page, "login_failure")
        return False

async def prime_master_session() -> bool:
    """Open a fresh context, login, and write storage_state."""
    global browser
    app_logger.info("Priming master session")
    ctx = await browser.new_context()
    try:
        page = await ctx.new_page()
        if not await perform_login(page):
            return False
        await ctx.storage_state(path=STORAGE_STATE)
        app_logger.info("Saved new session state.")
        return True
    finally:
        await ctx.close()


# =======================================================================================
#                       UTILITIES & CORE SCRAPING LOGIC
# =======================================================================================

def _format_metric_with_emoji(value_str: str, threshold: float, is_uph: bool = False) -> str:
    try:
        numeric_value = float(re.sub(r'[^\d.]', '', value_str))
        is_good = (numeric_value >= threshold) if is_uph else (numeric_value <= threshold)
        emoji = EMOJI_GREEN_CHECK if is_good else EMOJI_RED_CROSS
        return f"{value_str} {emoji}"
    except:
        return value_str
        
def _format_metric_with_color(value_str: str, threshold: float, is_uph: bool = False) -> str:
    try:
        numeric_value = float(re.sub(r'[^\d.]', '', value_str))
        is_good = (numeric_value >= threshold) if is_uph else (numeric_value <= threshold)
        color = COLOR_GOOD if is_good else COLOR_BAD
        return f'<font color="{color}">{value_str}</font>'
    except:
        return value_str

async def log_results(data: dict):
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
            # Capture a screenshot immediately after clicking the tab to help
            # diagnose issues where the date picker fails to appear. Previously
            # the saved screenshot showed the page before this click occurred.
            await _save_screenshot(page, f"{store_name}_after_customised")

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
            sorted_shoppers = sorted(
                shopper_stats,
                key=lambda x: float(x["inf"].replace("%", "").strip())
            )
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
    if not CHAT_WEBHOOK_URL:
        return

    overall, shoppers = data.get("overall"), data.get("shoppers")
    if not overall or not shoppers:
        app_logger.warning("post_to_chat_webhook called with incomplete data.")
        return

    store_name = overall.get("store", "Unknown Store")
    timestamp  = datetime.now(LOCAL_TIMEZONE).strftime("%A %d %B, %H:%M")
    runtime    = datetime.now(LOCAL_TIMEZONE).strftime("%H:%M")
    
    summary_text = (
        f"  •  <b>UPH (Store Avg):</b> {_format_metric_with_emoji(overall.get('uph'), UPH_THRESHOLD, is_uph=True)}<br>"
        f"  •  <b>Lates (Store Avg):</b> {_format_metric_with_emoji(overall.get('lates'), LATES_THRESHOLD)}<br>"
        f"  •  <b>INF (Store Avg):</b> {_format_metric_with_emoji(overall.get('inf'), INF_THRESHOLD)}<br>"
        f"  •  <b>Total Orders:</b> {overall.get('orders')}<br>"
        f"  •  <b>Total Units:</b> {overall.get('units')}"
    )

    shopper_widgets = []
    for s in shoppers:
        uph_fmt   = _format_metric_with_color(f"<b>UPH:</b> {s['uph']}", UPH_THRESHOLD, is_uph=True)
        inf_fmt   = _format_metric_with_color(f"<b>INF:</b> {s['inf']}", INF_THRESHOLD)
        lates_fmt = _format_metric_with_color(f"<b>Lates:</b> {s['lates']}", LATES_THRESHOLD)
        metrics_text = f"{uph_fmt} | {inf_fmt} | {lates_fmt}"
        shopper_widgets.append({
            "decoratedText": {
                "icon": {"knownIcon": "PERSON"},
                "topLabel": f"<b>{s['name']}</b> ({s['orders']} Orders)",
                "text": metrics_text
            }
        })

    payload = {
        "cardsV2": [{
            "cardId": f"store-summary-{store_name.replace(' ', '-')}",
            "card": {
                "header": {
                    "title": f"Amazon Metrics - {store_name}",
                    "subtitle": f"{timestamp} | Up to {runtime}",
                    "imageUrl": "https://i.pinimg.com/originals/01/ca/da/01cada77a0a7d326d85b7969fe26a728.jpg",
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
                        "widgets": shopper_widgets
                    }
                ]
            }
        }]
    }

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
            async with session.post(CHAT_WEBHOOK_URL, json=payload) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    app_logger.error(f"Chat webhook failed. Status: {resp.status}, Response: {err}")
    except Exception as e:
        app_logger.error(f"Error posting to chat webhook: {e}", exc_info=True)

async def post_summary_webhook(data: dict):
    if not SUMMARY_CHAT_WEBHOOK_URL:
        return

    overall = data.get("overall")
    if not overall:
        app_logger.warning("post_summary_webhook called with incomplete data.")
        return

    store_name = overall.get("store", "Unknown Store")
    timestamp  = datetime.now(LOCAL_TIMEZONE).strftime("%A %d %B, %H:%M")
    runtime    = datetime.now(LOCAL_TIMEZONE).strftime("%H:%M")

    summary_text = (
        f"  •  <b>UPH (Store Avg):</b> {_format_metric_with_emoji(overall.get('uph'), UPH_THRESHOLD, is_uph=True)}<br>"
        f"  •  <b>Lates (Store Avg):</b> {_format_metric_with_emoji(overall.get('lates'), LATES_THRESHOLD)}<br>"
        f"  •  <b>INF (Store Avg):</b> {_format_metric_with_emoji(overall.get('inf'), INF_THRESHOLD)}<br>"
        f"  •  <b>Total Orders:</b> {overall.get('orders')}<br>"
        f"  •  <b>Total Units:</b> {overall.get('units')}"
    )

    payload = {
        "cardsV2": [{
            "cardId": f"store-summary-{store_name.replace(' ', '-')}-overall",
            "card": {
                "header": {
                    "title": f"Amazon Metrics - {store_name}",
                    "subtitle": f"{timestamp} | Up to {runtime}",
                    "imageUrl": "https://i.pinimg.com/originals/01/ca/da/01cada77a0a7d326d85b7969fe26a728.jpg",
                    "imageType": "CIRCLE"
                },
                "sections": [
                    {
                        "header": "Store-Wide Performance (Weighted Avg)",
                        "widgets": [{"textParagraph": {"text": summary_text}}]
                    }
                ]
            }
        }]
    }

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        async with aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
            async with session.post(SUMMARY_CHAT_WEBHOOK_URL, json=payload) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    app_logger.error(f"Summary webhook failed. Status: {resp.status}, Response: {err}")
    except Exception as e:
        app_logger.error(f"Error posting to summary webhook: {e}", exc_info=True)


async def main():
    global playwright, browser
    app_logger.info("Starting up in single-store mode...")

    try:
        playwright = await async_playwright().start()
        browser    = await playwright.chromium.launch(headless=not DEBUG_MODE)

        # Check or establish login session
        login_required = True
        if ensure_storage_state():
            app_logger.info("Found existing storage_state; verifying session")
            ctx = await browser.new_context(storage_state=json.load(open(STORAGE_STATE)))
            pg  = await ctx.new_page()
            test_url = (
                f"https://sellercentral.amazon.co.uk/snowdash"
                f"?mons_sel_dir_mcid={TARGET_STORE['merchant_id']}"
                f"&mons_sel_mkid={TARGET_STORE['marketplace_id']}"
            )
            login_required = await check_if_login_needed(pg, test_url)
            await ctx.close()

        if login_required:
            if not await prime_master_session():
                app_logger.critical("Could not establish a login session. Aborting.")
                return

        storage_state = json.load(open(STORAGE_STATE))
        scraped_data  = await scrape_store_data(browser, TARGET_STORE, storage_state)
        if scraped_data:
            await log_results(scraped_data)
            await post_to_chat_webhook(scraped_data)
            await post_summary_webhook(scraped_data)
            app_logger.info("Run completed successfully.")
        else:
            app_logger.error("Run failed: Could not retrieve data for the target store.")

    except Exception as e:
        app_logger.critical(f"A critical error occurred in main execution: {e}", exc_info=True)
    finally:
        app_logger.info("Shutting down...")
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()
        app_logger.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())
