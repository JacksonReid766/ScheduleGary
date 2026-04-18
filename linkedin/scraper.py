import asyncio
import random
from playwright.async_api import Page
from linkedin.session import linkedin_session


async def random_delay(min_sec: float = 1.0, max_sec: float = 3.0) -> None:
    await asyncio.sleep(random.uniform(min_sec, max_sec))


async def navigate_to_own_profile(page: Page) -> str:
    await page.goto("https://www.linkedin.com/in/me/", wait_until="load")
    await asyncio.sleep(3)
    return page.url


async def scrape_headline(page: Page) -> str:
    try:
        el = page.locator('div.text-body-medium.break-words').first
        return (await el.inner_text(timeout=5_000)).strip()
    except Exception:
        return ""


async def scrape_about(page: Page) -> str:
    try:
        see_more = page.locator('section:has(#about) button:has-text("see more")').first
        if await see_more.is_visible(timeout=2_000):
            await see_more.click()
            await random_delay(0.5, 1.0)
    except Exception:
        pass

    for selector in [
        'section:has(#about) .display-flex.ph5.pv3 span[aria-hidden="true"]',
        '#about ~ div .pv-shared-text-with-see-more span[aria-hidden="true"]',
    ]:
        try:
            el = page.locator(selector).first
            text = (await el.inner_text(timeout=5_000)).strip()
            if text:
                return text
        except Exception:
            pass
    return ""


async def scrape_experience(page: Page) -> list[dict]:
    experience = []
    try:
        exp_section = page.locator('section:has(#experience)')
        see_more_buttons = exp_section.locator('button:has-text("see more")')
        for i in range(await see_more_buttons.count()):
            try:
                await see_more_buttons.nth(i).click()
                await random_delay(0.3, 0.7)
            except Exception:
                pass

        items = exp_section.locator('li.artdeco-list__item')
        for i in range(await items.count()):
            item = items.nth(i)
            try:
                title = (await item.locator('span[aria-hidden="true"]').first.inner_text(timeout=3_000)).strip()
            except Exception:
                title = ""

            span_texts = []
            try:
                spans = item.locator('span[aria-hidden="true"]')
                for j in range(await spans.count()):
                    t = (await spans.nth(j).inner_text(timeout=2_000)).strip()
                    if t:
                        span_texts.append(t)
            except Exception:
                span_texts = [title]

            company = span_texts[1] if len(span_texts) > 1 else ""
            duration = span_texts[2] if len(span_texts) > 2 else ""

            bullets_raw = []
            try:
                desc_el = item.locator('.pvs-list__item--with-top-padding .display-flex span[aria-hidden="true"]')
                for k in range(await desc_el.count()):
                    t = (await desc_el.nth(k).inner_text(timeout=2_000)).strip()
                    if t and t not in (title, company, duration):
                        bullets_raw.append(t)
            except Exception:
                pass

            if title:
                experience.append({"title": title, "company": company, "duration": duration, "bullets": bullets_raw})
    except Exception as e:
        print(f"  Warning: experience scrape incomplete — {e}")
    return experience


async def scrape_skills(page: Page) -> list[str]:
    skills = []
    try:
        skills_url = page.url.rstrip("/") + "/details/skills/"
        await page.goto(skills_url, wait_until="load")
        await asyncio.sleep(3)
        skill_els = page.locator('span[aria-hidden="true"]')
        seen = set()
        for i in range(await skill_els.count()):
            t = (await skill_els.nth(i).inner_text(timeout=2_000)).strip()
            if t and len(t) < 80 and t not in seen:
                seen.add(t)
                skills.append(t)
    except Exception as e:
        print(f"  Warning: skills scrape incomplete — {e}")
    await page.go_back()
    await asyncio.sleep(2)
    return skills[:50]


async def scrape_profile(email: str, password: str) -> dict:
    async with linkedin_session(email, password) as page:
        await navigate_to_own_profile(page)
        headline = await scrape_headline(page)
        about = await scrape_about(page)
        experience = await scrape_experience(page)
        skills = await scrape_skills(page)
        return {"headline": headline, "about": about, "experience": experience, "skills": skills}
