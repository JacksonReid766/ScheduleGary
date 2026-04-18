import asyncio
import json
import random
from contextlib import asynccontextmanager
from pathlib import Path
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

SESSION_FILE = Path(__file__).parent / "session.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def random_delay(min_sec: float = 1.0, max_sec: float = 3.0) -> None:
    await asyncio.sleep(random.uniform(min_sec, max_sec))


async def _is_logged_in(page: Page) -> bool:
    await page.goto("https://www.linkedin.com/feed/", wait_until="load")
    await asyncio.sleep(2)
    return page.url.startswith("https://www.linkedin.com/feed")


async def _do_login(page: Page, email: str, password: str) -> None:
    await page.goto("https://www.linkedin.com/login", wait_until="load")
    await asyncio.sleep(3)

    username_input = page.locator('input[id="username"]')
    try:
        await username_input.wait_for(timeout=15_000)
        await username_input.fill(email)
        await random_delay(0.6, 1.2)
        await page.fill('input[id="password"]', password)
        await random_delay(0.6, 1.2)
        await page.click('button[type="submit"]')
        await page.wait_for_load_state("load", timeout=30_000)
        await asyncio.sleep(3)
    except Exception:
        print("  Login form not detected — complete login manually in the browser.")
        await page.wait_for_url("https://www.linkedin.com/feed/**", timeout=180_000)
        return

    if "checkpoint" in page.url or "challenge" in page.url or "two-step" in page.url:
        print("  2FA detected — complete it in the browser (up to 3 minutes).")
        await page.wait_for_url("https://www.linkedin.com/feed/**", timeout=180_000)


async def get_context(playwright, email: str, password: str) -> tuple[Browser, BrowserContext, Page]:
    browser = await playwright.chromium.launch(headless=False, slow_mo=50)

    if SESSION_FILE.exists():
        context = await browser.new_context(
            storage_state=str(SESSION_FILE),
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
        )
        page = await context.new_page()
        if await _is_logged_in(page):
            return browser, context, page
        await context.close()

    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=USER_AGENT,
    )
    page = await context.new_page()
    await _do_login(page, email, password)
    await context.storage_state(path=str(SESSION_FILE))

    return browser, context, page


@asynccontextmanager
async def linkedin_session(email: str, password: str):
    async with async_playwright() as pw:
        browser, context, page = await get_context(pw, email, password)
        try:
            yield page
        finally:
            try:
                await context.storage_state(path=str(SESSION_FILE))
            except Exception:
                pass
            await browser.close()
