import asyncio
import random
import re
from html import unescape
from pathlib import Path
from playwright.async_api import Page
from linkedin.session import linkedin_session


async def random_delay(min_sec: float = 1.0, max_sec: float = 3.0) -> None:
    await asyncio.sleep(random.uniform(min_sec, max_sec))


async def navigate_to_profile(page: Page) -> None:
    await page.goto("https://www.linkedin.com/in/me/", wait_until="load")
    await asyncio.sleep(3)


async def extract_edit_urls(page: Page) -> dict:
    html = await page.content()

    def findall(pattern):
        return [unescape(u) for u in re.findall(pattern, html)]

    summary_urls = findall(r'href="(https://www\.linkedin\.com/in/[^"]+add-edit/(?:SUMMARY|GUIDED_EDIT_SUMMARY)/[^"]+)"')
    position_urls = findall(r'href="(https://www\.linkedin\.com/in/[^"]+add-edit/POSITION/\?[^"]+entityUrn[^"]+)"')
    skills_urls = findall(r'href="(https://www\.linkedin\.com/in/[^"]+add-edit/SKILL_AND_ASSOCIATION/[^"]+)"')
    slugs = re.findall(r'linkedin\.com/in/([a-zA-Z0-9\-]+)/', html)
    slug = next((s for s in slugs if s != 'me'), None)

    return {
        "summary": summary_urls[0] if summary_urls else None,
        "positions": position_urls,
        "skills": skills_urls[0] if skills_urls else None,
        "slug": slug,
    }


async def fill_modal_field(page: Page, text: str) -> None:
    field = page.locator(
        'textarea[id*="summary"], textarea[id*="description"], '
        'textarea[id$="-description"], div[contenteditable="true"]'
    ).first
    await field.wait_for(timeout=10_000)
    await field.fill(text)
    await random_delay()


async def save_modal(page: Page) -> None:
    save_btn = page.locator('button[aria-label="Save"], button:has-text("Save")').last
    await save_btn.click(timeout=10_000)
    await page.wait_for_load_state("load", timeout=20_000)
    await asyncio.sleep(2)


async def edit_headline(page: Page, headline: str) -> None:
    try:
        await page.locator('button[aria-label="Edit intro"]').first.click(timeout=10_000)
        await random_delay()
        headline_field = page.locator('input[id*="headline"], input[name="headline"]').first
        await headline_field.wait_for(timeout=10_000)
        await headline_field.fill(headline)
        await random_delay()
        await save_modal(page)
    except Exception as e:
        print(f"  Warning: could not edit headline — {e}")


async def edit_about(page: Page, about: str, edit_url: str) -> None:
    try:
        await page.goto(edit_url, wait_until="load")
        await asyncio.sleep(2)
        await fill_modal_field(page, about)
        await save_modal(page)
    except Exception as e:
        print(f"  Warning: could not edit about — {e}")


async def edit_experience_role(page: Page, exp: dict, edit_url: str) -> None:
    bullets_text = "\n".join(f"• {b}" for b in exp.get("bullets", []))
    try:
        await page.goto(edit_url, wait_until="load")
        await asyncio.sleep(2)
        await fill_modal_field(page, bullets_text)
        await save_modal(page)
    except Exception as e:
        print(f"  Warning: could not edit experience '{exp.get('title')}' — {e}")


async def edit_skills(page: Page, skills: list[str], add_url: str) -> None:
    for skill in skills[:15]:
        try:
            await page.goto(add_url, wait_until="load")
            await asyncio.sleep(2)
            skill_input = page.locator(
                'input[aria-label*="skill" i], input[placeholder*="skill" i], input[id*="skill" i]'
            ).first
            await skill_input.wait_for(timeout=8_000)
            await skill_input.fill(skill)
            await random_delay(0.8, 1.5)
            suggestion = page.locator('li[role="option"]').first
            if await suggestion.is_visible(timeout=2_000):
                await suggestion.click()
                await random_delay(0.5, 1.0)
            save_btn = page.locator('button[aria-label="Save"], button:has-text("Save")').last
            if await save_btn.is_visible(timeout=2_000):
                await save_btn.click()
                await page.wait_for_load_state("load", timeout=15_000)
                await asyncio.sleep(2)
        except Exception as e:
            print(f"  Could not add skill '{skill}' — {e}")


async def apply_edits(optimized: dict, email: str, password: str) -> None:
    async with linkedin_session(email, password) as page:
        await navigate_to_profile(page)
        urls = await extract_edit_urls(page)

        if optimized.get("headline"):
            await edit_headline(page, optimized["headline"])
            await navigate_to_profile(page)

        if optimized.get("about") and urls["summary"]:
            await edit_about(page, optimized["about"], urls["summary"])

        for i, exp in enumerate(optimized.get("experience", [])):
            if i < len(urls["positions"]):
                await edit_experience_role(page, exp, urls["positions"][i])

        if optimized.get("skills") and urls["skills"]:
            await edit_skills(page, optimized["skills"], urls["skills"])
