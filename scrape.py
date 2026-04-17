"""
Chase AI Skool Scraper — reads __NEXT_DATA__ to extract full course tree.
Saves each lesson as a markdown file in raw/{course}/{lesson}.md
Usage: python3 scrape.py
"""
import asyncio
import json
import re
import time
from pathlib import Path
from playwright.async_api import async_playwright

SKOOL_BASE = "https://www.skool.com"
COMMUNITY = "chase-ai-community"
COOKIES_FILE = Path(__file__).parent / "cookies.txt"
RAW_DIR = Path(__file__).parent / "raw"
RAW_DIR.mkdir(exist_ok=True)


def safe(text: str) -> str:
    text = re.sub(r'[^\w\s-]', '', text).strip()
    return re.sub(r'\s+', '-', text)[:70].lower()


def load_cookies_list() -> list:
    cookies = []
    with open(COOKIES_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) < 7:
                continue
            domain, _, path, secure, expiry, name, value = parts[:7]
            cookies.append({
                'name': name, 'value': value, 'domain': domain, 'path': path,
                'secure': secure.upper() == 'TRUE',
                'expires': int(expiry) if expiry.isdigit() else -1,
            })
    return cookies


def extract_modules(node: dict, path: str = "") -> list[dict]:
    """Recursively walk the course tree and collect all modules (lessons)."""
    results = []
    course = node.get('course', {})
    unit_type = course.get('unitType', '')
    meta = course.get('metadata', {})
    title = meta.get('title', '').strip()
    # Strip emoji from title for filesystem
    title_clean = re.sub(r'[^\x00-\x7F]+', '', title).strip()

    current_path = f"{path}/{title_clean}" if title_clean else path

    if unit_type == 'module' and meta.get('hasAccess') == 1:
        results.append({
            'id': course.get('id'),
            'name': course.get('name'),
            'title': title,
            'title_clean': title_clean or f"module-{course.get('id','')[:8]}",
            'path': path,
            'video_link': meta.get('videoLink', ''),
            'video_id': meta.get('videoId', ''),
            'resources': meta.get('resources', '[]'),
        })

    for child in node.get('children', []):
        results.extend(extract_modules(child, current_path))

    return results


async def scrape_module(page, course_name: str, module: dict) -> str:
    """Navigate to a module page and extract its text content."""
    url = f"{SKOOL_BASE}/{COMMUNITY}/classroom/{course_name}?md={module['id']}"
    await page.goto(url, wait_until='domcontentloaded', timeout=30000)
    await page.wait_for_timeout(2500)

    # Get __NEXT_DATA__ for this module
    try:
        data = await page.evaluate('() => window.__NEXT_DATA__')
        pp = data.get('props', {}).get('pageProps', {})
        selected = pp.get('selectedModule', '')

        # Try to get rendered text from the lesson content area
        content_text = ""
        for sel in ['[class*="PostText"]', '[class*="post-content"]', '[class*="lessonContent"]',
                    '[class*="moduleContent"]', '[class*="textContent"]', 'article', 'main']:
            els = await page.query_selector_all(sel)
            for el in els:
                t = await el.inner_text()
                if len(t.strip()) > 50:
                    content_text = t.strip()
                    break
            if content_text:
                break

    except Exception as e:
        content_text = f"[Error reading page: {e}]"

    # Parse resources JSON (contains text blocks)
    resources_md = ""
    try:
        resources = json.loads(module['resources']) if module['resources'] else []
        if isinstance(resources, list):
            for r in resources:
                if isinstance(r, dict):
                    rtype = r.get('type', '')
                    if rtype == 'text':
                        resources_md += r.get('content', '') + "\n\n"
                    elif rtype == 'link':
                        resources_md += f"- [{r.get('name', r.get('url',''))}]({r.get('url','')})\n"
                    elif rtype == 'file':
                        resources_md += f"- File: {r.get('name', '')}\n"
    except Exception:
        pass

    # Build markdown output
    lines = [
        f"# {module['title']}",
        f"",
        f"URL: {page.url}",
        f"Scraped: {time.strftime('%Y-%m-%d')}",
    ]
    if module.get('video_link'):
        lines.append(f"Video: {module['video_link']}")
    lines.append("")
    lines.append("---")
    lines.append("")

    if resources_md:
        lines.append("## Resources")
        lines.append(resources_md)

    if content_text:
        lines.append("## Content")
        lines.append(content_text)

    return "\n".join(lines)


async def main():
    cookies = load_cookies_list()
    print(f"Loaded {len(cookies)} cookies")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={'width': 1280, 'height': 900},
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()

        # ── Get all courses ──────────────────────────────────────────────────
        print("\nLoading classroom index...")
        await page.goto(f"{SKOOL_BASE}/{COMMUNITY}/classroom", wait_until='domcontentloaded', timeout=30000)
        await page.wait_for_timeout(4000)

        data = await page.evaluate('() => window.__NEXT_DATA__')
        all_courses = data['props']['pageProps']['allCourses']
        accessible = [c for c in all_courses if c.get('metadata', {}).get('hasAccess') == 1]

        print(f"Accessible courses: {len(accessible)}")
        for c in accessible:
            print(f"  [{c['metadata']['numModules']} lessons] {c['metadata']['title']}")

        # ── Scrape each course ───────────────────────────────────────────────
        total_saved = 0
        total_skipped = 0

        for course_meta in accessible:
            course_title = course_meta['metadata']['title']
            course_name = course_meta['name']  # short slug e.g. "4fe79bd0"
            course_id = course_meta['id']

            print(f"\n{'='*60}")
            print(f"Course: {course_title}")

            # Load course page to get full module tree
            await page.goto(
                f"{SKOOL_BASE}/{COMMUNITY}/classroom/{course_id}",
                wait_until='domcontentloaded', timeout=30000
            )
            await page.wait_for_timeout(3000)

            course_data = await page.evaluate('() => window.__NEXT_DATA__')
            course_node = course_data['props']['pageProps']['course']
            modules = extract_modules(course_node)
            print(f"  {len(modules)} accessible modules")

            course_dir = RAW_DIR / safe(course_title)
            course_dir.mkdir(exist_ok=True)

            for i, mod in enumerate(modules):
                # Use section path + title for filename
                section = safe(mod['path'].split('/')[-1]) if mod['path'] else ''
                filename = f"{i+1:03d}-{safe(mod['title_clean'])}.md"
                out_file = course_dir / filename

                if out_file.exists():
                    total_skipped += 1
                    continue

                print(f"  [{i+1}/{len(modules)}] {mod['title'][:60]}")
                try:
                    content = await scrape_module(page, course_name, mod)
                    out_file.write_text(content)
                    total_saved += 1
                    await asyncio.sleep(0.3)
                except Exception as e:
                    print(f"    ERROR: {e}")

        print(f"\n{'='*60}")
        print(f"Done — {total_saved} saved, {total_skipped} skipped (already existed)")
        await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
