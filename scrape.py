"""
Chase AI Skool Scraper
Scrapes all course content from the Chase AI community and saves to raw/

Usage:
  1. Export cookies from Chrome (EditThisCookie extension → Export as JSON)
  2. Save cookies to cookies.json in this directory
  3. python3 scrape.py
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path
from playwright.async_api import async_playwright

SKOOL_COMMUNITY = "https://www.skool.com/chase-ai"
COOKIES_FILE = Path(__file__).parent / "cookies.json"
RAW_DIR = Path(__file__).parent / "raw"
RAW_DIR.mkdir(exist_ok=True)


def safe_filename(text: str) -> str:
    text = re.sub(r'[^\w\s-]', '', text).strip()
    text = re.sub(r'[\s]+', '-', text)
    return text[:80].lower()


async def load_cookies(context, path: Path):
    with open(path) as f:
        raw = json.load(f)

    cookies = []
    for c in raw:
        cookie = {
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ".skool.com"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", True),
            "httpOnly": c.get("httpOnly", False),
        }
        if "expirationDate" in c:
            cookie["expires"] = int(c["expirationDate"])
        if cookie["name"] and cookie["value"]:
            cookies.append(cookie)

    await context.add_cookies(cookies)
    print(f"  Loaded {len(cookies)} cookies")


async def scrape_classroom(page) -> list[dict]:
    """Get all courses/modules from the classroom tab."""
    print("\nNavigating to classroom...")
    await page.goto(f"{SKOOL_COMMUNITY}/classroom", wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    # Get all course cards
    courses = []
    cards = await page.query_selector_all('[class*="course"], [class*="Course"], a[href*="/classroom/"]')

    for card in cards:
        href = await card.get_attribute("href")
        text = await card.inner_text()
        if href and "/classroom/" in href:
            courses.append({
                "title": text.strip()[:80],
                "url": href if href.startswith("http") else f"https://www.skool.com{href}"
            })

    # Deduplicate
    seen = set()
    unique = []
    for c in courses:
        if c["url"] not in seen:
            seen.add(c["url"])
            unique.append(c)

    print(f"  Found {len(unique)} courses")
    return unique


async def scrape_course(page, course: dict) -> list[dict]:
    """Get all lessons from a course."""
    await page.goto(course["url"], wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    lessons = []
    # Look for lesson links within the course
    links = await page.query_selector_all('a[href*="/classroom/"]')
    for link in links:
        href = await link.get_attribute("href")
        text = await link.inner_text()
        if href and href != course["url"] and "/classroom/" in href:
            url = href if href.startswith("http") else f"https://www.skool.com{href}"
            if url != course["url"]:
                lessons.append({"title": text.strip()[:80], "url": url})

    # Deduplicate
    seen = set()
    unique = []
    for l in lessons:
        if l["url"] not in seen:
            seen.add(l["url"])
            unique.append(l)

    return unique


async def scrape_lesson(page, lesson: dict) -> str:
    """Scrape the full text content of a lesson."""
    await page.goto(lesson["url"], wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    # Get page title
    title = await page.title()

    # Extract main content text
    content_parts = []

    # Try common content selectors
    selectors = [
        '[class*="content"]',
        '[class*="lesson"]',
        '[class*="post"]',
        'article',
        'main',
    ]
    for sel in selectors:
        els = await page.query_selector_all(sel)
        for el in els:
            text = await el.inner_text()
            if len(text.strip()) > 100:
                content_parts.append(text.strip())
        if content_parts:
            break

    # Fallback: get all visible text from body
    if not content_parts:
        body_text = await page.inner_text("body")
        content_parts.append(body_text)

    content = "\n\n".join(content_parts)

    # Remove excessive whitespace
    content = re.sub(r'\n{4,}', '\n\n\n', content)

    return f"# {title}\n\nURL: {lesson['url']}\nScraped: {time.strftime('%Y-%m-%d')}\n\n---\n\n{content}"


async def scrape_community_posts(page, max_posts: int = 50) -> list[str]:
    """Scrape recent community posts."""
    print("\nScraping community posts...")
    await page.goto(f"{SKOOL_COMMUNITY}", wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    posts = []
    post_links = await page.query_selector_all('a[href*="/p/"]')

    seen = set()
    for link in post_links[:max_posts]:
        href = await link.get_attribute("href")
        if href and href not in seen:
            seen.add(href)
            url = href if href.startswith("http") else f"https://www.skool.com{href}"
            posts.append(url)

    print(f"  Found {len(posts)} post links")
    return posts


async def main():
    if not COOKIES_FILE.exists():
        print(f"ERROR: cookies.json not found at {COOKIES_FILE}")
        print("Export cookies from Chrome using EditThisCookie extension and save as cookies.json")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # headless=False so you can see it
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        await load_cookies(context, COOKIES_FILE)
        page = await context.new_page()

        # Verify login
        print("Verifying login...")
        await page.goto(SKOOL_COMMUNITY, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        current_url = page.url
        if "login" in current_url or "sign" in current_url:
            print("ERROR: Not logged in — check your cookies")
            await browser.close()
            return

        print(f"  Logged in — at {current_url}")

        # Scrape classroom
        try:
            courses = await scrape_classroom(page)

            total_lessons = 0
            for i, course in enumerate(courses):
                course_slug = safe_filename(course["title"]) or f"course-{i}"
                course_dir = RAW_DIR / course_slug
                course_dir.mkdir(exist_ok=True)

                print(f"\nCourse: {course['title']}")
                lessons = await scrape_course(page, course)

                if not lessons:
                    # The course page itself might be the content
                    lessons = [course]

                print(f"  {len(lessons)} lessons")

                for j, lesson in enumerate(lessons):
                    lesson_slug = safe_filename(lesson["title"]) or f"lesson-{j}"
                    out_file = course_dir / f"{j+1:02d}-{lesson_slug}.md"

                    if out_file.exists():
                        print(f"  SKIP (exists): {lesson['title'][:50]}")
                        continue

                    print(f"  Scraping: {lesson['title'][:50]}...")
                    try:
                        content = await scrape_lesson(page, lesson)
                        out_file.write_text(content)
                        total_lessons += 1
                        await page.wait_for_timeout(500)  # polite delay
                    except Exception as e:
                        print(f"    ERROR: {e}")

        except Exception as e:
            print(f"Classroom scrape failed: {e}")

        print(f"\nDone — scraped content saved to raw/")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
