#!/usr/bin/env python
"""
DeepSeek Chat Scraper

Based on Kimi scraper architecture but adapted for DeepSeek's specific UI:
- Citations are directly in <a> tags with href (no hover needed)
- Messages use ds-message class structure
- Web search results shown via "已阅读 X 个网页" button
- Input is textarea._27c9245
- Send via Enter key (more reliable than button click)

Key differences from Kimi:
1. No need to hover citations to reveal hrefs
2. Different message container structure (div.dad65929 > div.ds-message)
3. Direct web search panel access
"""
from camoufox.sync_api import Camoufox
from scrapy.http import HtmlResponse
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import time
import os
import json
import random
import argparse
import sys
import subprocess
from screeninfo import get_monitors


DEEPSEEK_HOME_URL = "https://chat.deepseek.com/"
USER_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), ".camoufox_profile", "deepseek"
)
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output")
SESSION_COOKIES_FILE = os.path.join(os.path.dirname(__file__), "deepseek_cookies.json")
SESSION_STORAGE_FILE = os.path.join(os.path.dirname(__file__), "deepseek_storage.json")

# DeepSeek-specific selectors based on actual UI structure
CHAT_INPUT_SELECTORS: List[str] = [
    "textarea[placeholder*='DeepSeek']",  # Match by placeholder text (most stable)
    "textarea[placeholder*='消息']",  # Fallback placeholder match
    "textarea.ds-scroll-area",  # Match by stable class name
    "textarea._27c9245",  # Hash class (may change)
    "textarea",  # Last resort: any textarea
]

# Send button - look for button near input area (may be disabled when input is empty)
SEND_BUTTON_SELECTORS: List[str] = [
    "button.f79352dc",  # Send button class
    'button:has(svg[data-icon*="send"])',
    'button[aria-disabled="false"]:has(svg)',
]

# Assistant message container - DeepSeek uses ds-message class
ASSISTANT_MESSAGE_SELECTORS: List[str] = [
    "div.ds-message._63c77b1",  # Main message container
    "div.ds-message",
    'div[class*="ds-message"]',
]

# Messages are in dad65929 container
MESSAGE_LIST_SELECTOR = "div.dad65929"

# Within assistant message, anchors with target="_blank" are citations
CITATION_LINK_SELECTOR = 'a[href][target="_blank"]'

# Web search button - "已阅读 X 个网页"
WEB_SEARCH_BUTTON_SELECTOR = "div._74c0879"

# Stop button during generation
STOP_BUTTON_SELECTORS: List[str] = [
    'button[aria-label*="停止"]',
    'button:has-text("停止")',
    'button:has(svg[name="stop"])',
]


def ensure_dirs() -> None:
    if not os.path.exists(USER_DATA_DIR):
        os.makedirs(USER_DATA_DIR, exist_ok=True)
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR, exist_ok=True)


def read_prompts(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f.readlines()]
    return [p for p in lines if p]


def pick_first_visible(page, selectors: List[str], timeout: int = 5000):
    """
    Find the first visible element from a list of selectors.
    Uses a short timeout to avoid hanging.
    """
    for selector in selectors:
        loc = page.locator(selector)
        try:
            if loc.count() > 0:
                first = loc.first
                if first.is_visible(timeout=timeout):
                    return first
        except Exception:
            continue
    return None


def is_chat_ui_ready(page) -> bool:
    """
    Returns True only when the authenticated chat UI is visible.
    """
    try:
        current_url = page.url or ""
        print(f"[DEBUG] Current URL: {current_url}")
        # NOTE:
        # DeepSeek sometimes keeps the URL as /sign_in even after the chat UI
        # is fully loaded (SPA navigation). Therefore we MUST NOT rely on the
        # URL containing or not containing 'sign_in' / 'login' to decide
        # whether the chat is ready. We only use DOM elements below.

        # Look for chat input
        print("[DEBUG] Looking for chat input...")
        chat_input = pick_first_visible(page, CHAT_INPUT_SELECTORS)
        if not chat_input:
            print("[DEBUG] Chat input not found yet")
            return False

        print("[DEBUG] Chat input found!")
        return True
    except Exception as e:
        print(f"[DEBUG] Exception in is_chat_ui_ready: {e}")
        return False


def wait_for_login(page, timeout_seconds: int = 300) -> bool:
    print("\n" + "=" * 60)
    print("⚠️  ACTION REQUIRED:")
    print("    1. Find the NEW browser window opened by this script")
    print("    2. Login to DeepSeek in THAT window (not your regular browser)")
    print("    3. Wait for the chat interface to appear")
    print("    4. Script will automatically continue once logged in")
    print(f"    5. Timeout: {timeout_seconds//60} minutes")
    print("=" * 60 + "\n")

    # First quick check - maybe already logged in
    print("[INFO] Checking if already logged in...")
    if is_chat_ui_ready(page):
        print("[INFO] ✓ Already logged in! Chat interface ready.")
        return True

    # Give user time to start login process（缩短初始等待）
    print("[INFO] Waiting 3 seconds for you to start login...")
    time.sleep(20)

    print("[INFO] Monitoring for chat input box...")
    start = time.time()
    remaining = timeout_seconds - 3
    check_count = 0
    last_url = ""
    while time.time() - start < remaining:
        check_count += 1
        current_url = page.url or ""

        # Notify when URL changes (indicates login progress)
        if current_url != last_url:
            print(f"\n[INFO] ⚠️  Page changed to: {current_url}")
            last_url = current_url

        print(
            f"[DEBUG] Login check #{check_count} (Elapsed: {int(time.time()-start)}s)"
        )
        if is_chat_ui_ready(page):
            print("\n" + "=" * 60)
            print("✓ SUCCESS: Chat interface detected!")
            print("✓ Ready to send prompts.")
            print("=" * 60 + "\n")
            return True
        # 更快轮询，加速检测登录完成
        time.sleep(1.5)

    print("\n[ERROR] Chat input not found after timeout")
    print("[ERROR] Please make sure you logged in the SCRIPT'S browser window")
    return False


def get_conversation_id_from_url(url: str) -> str:
    """
    Extract conversation ID from URL.
    DeepSeek might use patterns like /chat/<id> or /c/<id>
    """
    try:
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        parts = [p for p in path.split("?")[0].split("/") if p]
        if parts:
            # Try to find a UUID-like or ID-like part
            for part in reversed(parts):
                if len(part) > 10:  # Likely an ID
                    return part
            return parts[-1]
    except Exception:
        pass
    return ""


def detect_language(text: str) -> str:
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            return "zh"
    return "en"


def load_cookies_into_context(page, cookies_path: str) -> None:
    try:
        if os.path.exists(cookies_path):
            with open(cookies_path, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            if isinstance(cookies, list) and len(cookies) > 0:
                page.context.add_cookies(cookies)
    except Exception:
        pass


def save_cookies_from_context(page, cookies_path: str) -> None:
    try:
        cookies = page.context.cookies()
        with open(cookies_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_storage_from_file(page, storage_path: str) -> None:
    try:
        if not os.path.exists(storage_path):
            return
        with open(storage_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        local_items = data.get("localStorage", {})
        session_items = data.get("sessionStorage", {})
        if local_items:
            page.evaluate(
                """(items) => { for (const [k,v] of Object.entries(items)) localStorage.setItem(k, v) }""",
                local_items,
            )
        if session_items:
            page.evaluate(
                """(items) => { for (const [k,v] of Object.entries(items)) sessionStorage.setItem(k, v) }""",
                session_items,
            )
    except Exception:
        pass


def save_storage_to_file(page, storage_path: str) -> None:
    try:
        ls = page.evaluate("""() => Object.fromEntries(Object.entries(localStorage))""")
        ss = page.evaluate(
            """() => Object.fromEntries(Object.entries(sessionStorage))"""
        )
        with open(storage_path, "w", encoding="utf-8") as f:
            json.dump(
                {"localStorage": ls, "sessionStorage": ss},
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception:
        pass


def is_generating(page) -> bool:
    """
    Check if the assistant is currently generating a response.
    Look for stop button or other generation indicators.
    """
    try:
        stop_btn = pick_first_visible(page, STOP_BUTTON_SELECTORS)
        if stop_btn:
            return True
    except Exception:
        pass
    return False


def is_send_button_disabled(page) -> bool:
    """
    DeepSeek send/stop button state helper.
    When input is empty or last LLM reply has finished, the arrow send button
    becomes disabled (aria-disabled=true and/or has 'ds-icon-button--disabled').
    While LLM is responding, the icon changes to a square stop button and is enabled.
    """
    try:
        # The main send/stop button wrapper, based on provided HTML:
        # <div class="_7436101 ... ds-icon-button ds-icon-button--l ds-icon-button--sizing-container ...">
        btn = page.locator("div._7436101.ds-icon-button").first
        if not btn or not btn.is_visible(timeout=3000):
            return False

        aria = (btn.get_attribute("aria-disabled") or "").lower()
        cls = btn.get_attribute("class") or ""

        if aria == "true":
            return True
        if "ds-icon-button--disabled" in cls:
            return True
    except Exception:
        pass
    return False


def ensure_online_mode_enabled(page) -> None:
    """
    Ensure DeepSeek '联网搜索' toggle is turned ON before sending a prompt.
    Does nothing if the toggle is already enabled or not found.
    """
    try:
        # DeepSeek 在不同平台/版本下，有的用 <button>，有的用 <div role="button">
        toggle = page.locator(
            'button:has-text("联网搜索"), div[role="button"]:has-text("联网搜索")'
        )
        if toggle.count() == 0:
            print("[DEBUG] Online search toggle ('联网搜索') not found")
            return

        btn = toggle.first
        if not btn.is_visible(timeout=5000):
            print("[DEBUG] Online search toggle found but not visible")
            return

        classes = btn.get_attribute("class") or ""
        # Selected state has class 'ds-toggle-button--selected'
        if (
            "ds-toggle-button--selected" in classes
            or "selected" in classes
            or "active" in classes
        ):
            print("[DEBUG] Online search already enabled")
            return

        print("[INFO] Enabling '联网搜索' (online search) mode...")
        btn.click()
        time.sleep(0.3)

        # Best-effort re-check
        classes_after = btn.get_attribute("class") or ""
        if "ds-toggle-button--selected" in classes_after or "selected" in classes_after:
            print("[INFO] '联网搜索' mode enabled")
        else:
            print("[WARN] Could not confirm '联网搜索' mode is enabled")
    except Exception as e:
        print(f"[WARN] Failed to ensure online search mode: {e}")


def extract_web_search_results(
    page, assistant_container
) -> Tuple[List[Dict[str, str]], bool]:
    """
    Extract web search results if the response used web search.
    DeepSeek shows "已阅读 X 个网页" button that opens a side panel.

    Returns list of dicts with: {href, title, snippet}
    """
    results: List[Dict[str, str]] = []
    had_web_search_button = False

    try:
        # Check if web search was used - look for "已阅读 X 个网页" button.
        # Prefer searching inside the assistant container, but fall back to page-wide search.
        search_button = None
        try:
            if assistant_container is not None:
                loc = assistant_container.locator(WEB_SEARCH_BUTTON_SELECTOR)
                if loc.count() > 0:
                    search_button = loc
        except Exception:
            search_button = None

        if search_button is None or search_button.count() == 0:
            loc = page.locator(WEB_SEARCH_BUTTON_SELECTOR)
            if loc.count() > 0:
                search_button = loc

        if search_button is None or search_button.count() == 0:
            # No web search summary block detected for this answer
            print("[DEBUG] No web search summary button found for this response")
            return results, had_web_search_button

        had_web_search_button = True
        print(f"[DEBUG] Found web search button, extracting results...")

        # Click the "已阅读 X 个网页" 区域多次尝试打开 side panel
        panel = None
        click_ok = False
        for attempt in range(3):
            try:
                btn = search_button.last
                if not btn.is_visible(timeout=2000):
                    continue

                print(
                    f"[DEBUG] Clicking DeepSeek web search button (attempt {attempt+1}/3)"
                )
                # Prefer the inner span.d162f7b9 with text "已阅读 X 个网页"
                try:
                    text_span = btn.locator('span.d162f7b9:has-text("已阅读")')
                    if text_span.count() > 0 and text_span.first.is_visible(
                        timeout=5000
                    ):
                        try:
                            text_span.first.click(timeout=5000)
                        except Exception:
                            # 强制点击尝试
                            text_span.first.click(timeout=5000, force=True)
                    else:
                        try:
                            btn.click(timeout=5000)
                        except Exception:
                            btn.click(timeout=5000, force=True)
                except Exception:
                    # Fallback: click the whole container
                    try:
                        btn.click(timeout=5000)
                    except Exception:
                        btn.click(timeout=5000, force=True)
                time.sleep(0.4)  # Wait briefly for panel to open

                # 尝试在本次点击后立即寻找 panel，若找到即可停止重试
                side_panel_selectors = [
                    "div._519be07",
                    "div.dc433409",
                    "div[class*='scrollable']",
                ]
                for selector in side_panel_selectors:
                    try:
                        loc = page.locator(selector)
                        if loc.count() > 0 and loc.first.is_visible():
                            panel = loc.first
                            click_ok = True
                            break
                    except Exception:
                        continue
                if click_ok:
                    break
            except Exception as e:
                print(
                    f"[WARN] Failed to click DeepSeek web search button (attempt {attempt+1}/3): {e}"
                )
                # 最后一次再尝试 JS 触发点击，作为兜底
                if attempt == 2:
                    try:
                        btn = search_button.last
                        btn.evaluate("el => el.click()")
                        time.sleep(0.4)
                        side_panel_selectors = [
                            "div._519be07",
                            "div.dc433409",
                            "div[class*='scrollable']",
                        ]
                        for selector in side_panel_selectors:
                            try:
                                loc = page.locator(selector)
                                if loc.count() > 0 and loc.first.is_visible():
                                    panel = loc.first
                                    click_ok = True
                                    break
                            except Exception:
                                continue
                        if click_ok:
                            break
                    except Exception as ee:
                        print(f"[WARN] DeepSeek JS click fallback failed: {ee}")
                time.sleep(0.3)

        if not click_ok and panel is None:
            print(
                "[WARN] Could not open DeepSeek search results panel after 3 attempts"
            )

        # 如果前面的快速查找没能找到 panel，这里再按旧逻辑兜底找一次

        # Find the side panel with search results（兜底逻辑）
        if not panel:
            side_panel_selectors = [
                "div._519be07",
                "div.dc433409",
                "div[class*='scrollable']",
            ]

            for selector in side_panel_selectors:
                try:
                    loc = page.locator(selector)
                    if loc.count() > 0 and loc.first.is_visible():
                        panel = loc.first
                        break
                except Exception:
                    continue

            if not panel:
                print("[WARN] Could not find search results panel")
                return results, had_web_search_button

        # Narrow down to the actual results container if we matched the outer panel
        try:
            inner = panel.locator("div.dc433409")
            if inner.count() > 0:
                panel = inner.first
        except Exception:
            pass

        # Extract search result items
        # Each result is an <a class="_24fe229"> with title & snippet inside
        result_items = panel.locator("a._24fe229")
        if result_items.count() == 0:
            # Fallback: any anchor with href
            result_items = panel.locator("a[href]")
        count = result_items.count()
        print(f"[DEBUG] Found {count} search result items")

        for i in range(min(count, 20)):  # Limit to first 20 results
            try:
                item = result_items.nth(i)
                href = item.get_attribute("href") or ""

                if not href or not href.startswith("http"):
                    continue

                # Try to extract title and snippet
                title = ""
                snippet = ""

                try:
                    # DeepSeek search title: div.search-view-card__title
                    title_elem = item.locator(
                        "div.search-view-card__title, .search-view-card__title"
                    ).first
                    if title_elem:
                        title = title_elem.inner_text().strip()
                except Exception:
                    pass

                if not title:
                    title = item.inner_text().strip()[
                        :160
                    ]  # Fallback to first 160 chars

                try:
                    # DeepSeek search snippet: div.search-view-card__snippet
                    snippet_elem = item.locator(
                        "div.search-view-card__snippet, .search-view-card__snippet"
                    ).first
                    if snippet_elem:
                        snippet = snippet_elem.inner_text().strip()
                except Exception:
                    pass

                results.append(
                    {
                        "href": href,
                        "title": title,
                        "snippet": snippet,
                    }
                )
            except Exception as e:
                print(f"[WARN] Failed to extract result {i}: {e}")
                continue

        print(f"[DEBUG] Extracted {len(results)} web search results")
    except Exception as e:
        print(f"[WARN] Failed to extract web search results: {e}")

    return results, had_web_search_button


def html_to_markdown(html: str) -> str:
    """
    Simple HTML to Markdown converter.
    Preserves links and basic formatting.
    """
    import re

    # Remove script/style tags
    html = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE
    )

    # Convert <a href="...">text</a> to [text](url)
    def replace_link(match):
        full_tag = match.group(0)
        href_match = re.search(r'href=["\']([^"\']+)["\']', full_tag)
        href = href_match.group(1) if href_match else ""
        inner = re.sub(r"<[^>]+>", "", match.group(1)).strip()

        # DeepSeek citation links sometimes render as "-6" (an invisible "-" plus the index).
        # Normalize patterns like "-6" / "- 6" to "6" so that markdown becomes [6](url) instead of [-6](url).
        if inner:
            inner = re.sub(r"^-\s*(\d+)$", r"\1", inner)

        if not href:
            return inner if inner else ""
        display = inner or "link"
        return f"[{display}]({href})"

    html = re.sub(
        r"<a[^>]*>(.*?)</a>", replace_link, html, flags=re.DOTALL | re.IGNORECASE
    )

    # Convert <br> to newline
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)

    # Convert </p>, </div> to double newline
    html = re.sub(r"</(p|div)>", "\n\n", html, flags=re.IGNORECASE)

    # Convert <li> to "- "
    html = re.sub(r"<li[^>]*>", "\n- ", html, flags=re.IGNORECASE)
    html = re.sub(r"</li>", "", html, flags=re.IGNORECASE)

    # Convert lists
    html = re.sub(r"<ol[^>]*>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</ol>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"<ul[^>]*>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</ul>", "\n", html, flags=re.IGNORECASE)

    # Convert headings
    for i in range(6, 0, -1):
        html = re.sub(
            f"<h{i}[^>]*>(.*?)</h{i}>",
            "#" * i + r" \1\n\n",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )

    # Remove all other tags
    html = re.sub(r"<[^>]+>", "", html)

    # Decode HTML entities
    html = html.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    html = html.replace("&nbsp;", " ").replace("&quot;", '"').replace("&#39;", "'")

    # Clean up multiple newlines
    html = re.sub(r"\n{3,}", "\n\n", html)
    html = re.sub(r" +", " ", html)

    return html.strip()


def wait_for_stream_completion_and_get_text(
    page, assistant_message_count_before: int, timeout_seconds: int = 300
) -> Tuple[str, List[str]]:
    """
    Wait for the assistant's response to complete streaming.
    Returns (response_text, list_of_citation_hrefs).
    """
    start = time.time()
    last_text = ""
    stable_ticks = 0
    last_change_time = start
    # 缩短最大等待时间与检测周期，加快响应完成判定
    max_stream_seconds = max(45, min(180, int(timeout_seconds * 0.7)))
    required_stable_ticks = 3  # ~0.75s with 0.25s sleep

    def get_latest_assistant():
        # Try specific selectors first
        for selector in ASSISTANT_MESSAGE_SELECTORS:
            loc = page.locator(selector)
            try:
                if loc.count() > assistant_message_count_before:
                    return loc.nth(loc.count() - 1)
            except Exception:
                continue

        # Fallback: try to find last message-like container
        fallback_selectors = [
            "article:last-child",
            "div[class*='message']:last-child",
            "div[class*='response']:last-child",
        ]
        for selector in fallback_selectors:
            try:
                loc = page.locator(selector)
                if loc.count() > 0:
                    return loc.last
            except Exception:
                continue
        return None

    # Wait for generation to start
    start_phase_deadline = time.time() + min(30, timeout_seconds * 0.2)
    while time.time() < start_phase_deadline:
        if is_generating(page):
            print("[DEBUG] Generation started (stop button visible)")
            break

        # Also check for content appearing
        try:
            container = get_latest_assistant()
            if container:
                text_now = container.inner_text().strip()
                if text_now and len(text_now) > len(last_text):
                    last_text = text_now
                    last_change_time = time.time()
                    print("[DEBUG] Content started appearing")
                    break
        except Exception:
            pass
        time.sleep(0.15)

    # Wait for completion
    while time.time() - start < timeout_seconds:
        container = get_latest_assistant()
        try:
            text = container.inner_text().strip() if container else ""
        except Exception:
            text = ""

        if text and text == last_text:
            stable_ticks += 1
        else:
            stable_ticks = 0
            last_text = text
            if text:
                last_change_time = time.time()

        # Force stop if generation takes too long
        if is_generating(page) and (time.time() - start > max_stream_seconds):
            print(f"[INFO] Forcing stop after {max_stream_seconds}s")
            try:
                stop_btn = pick_first_visible(page, STOP_BUTTON_SELECTORS)
                if stop_btn:
                    stop_btn.click()
            except Exception:
                pass

        # Completion criteria:
        # 1) Text has been stable for several ticks
        # 2) Send button is in the disabled state, which for DeepSeek means:
        #    - Input is empty
        #    - Last LLM reply has finished
        if stable_ticks >= required_stable_ticks and len(text) > 0:
            if is_send_button_disabled(page):
                print(
                    f"[DEBUG] Response completed ({len(text)} chars, send button disabled)"
                )
                break

        # Fallback timeout
        if (not is_generating(page)) and (time.time() - last_change_time > 6):
            print("[DEBUG] No changes for 6s, assuming complete")
            break

        time.sleep(0.25)

    # Extract citations
    citations: List[str] = []
    final_text = last_text

    try:
        container = get_latest_assistant()
        if container:
            # Try to get HTML content for better formatting
            try:
                # Look for markdown or content container within the message
                content_selectors = [
                    "div[class*='markdown']",
                    "div[class*='content']",
                    "div[class*='message-body']",
                ]
                content_container = None
                for selector in content_selectors:
                    try:
                        loc = container.locator(selector)
                        if loc.count() > 0 and loc.first.is_visible():
                            content_container = loc.first
                            break
                    except Exception:
                        continue

                if content_container:
                    html_content = content_container.inner_html()
                    final_text = html_to_markdown(html_content)

                    # Extract citations
                    citation_links = content_container.locator(CITATION_LINK_SELECTOR)
                    for i in range(citation_links.count()):
                        try:
                            href = citation_links.nth(i).get_attribute("href")
                            if href and href.startswith("http"):
                                citations.append(href)
                        except Exception:
                            continue
            except Exception:
                # Fallback to plain text
                final_text = container.inner_text().strip()
    except Exception:
        pass

    return final_text, list(dict.fromkeys(citations))


def send_prompt_and_collect(
    page, prompt_text: str, website_name: str = "DEEPSEEK"
) -> Dict[str, Optional[str]]:
    """
    Send a prompt and collect the response.
    DeepSeek-specific implementation.
    """
    input_box = pick_first_visible(page, CHAT_INPUT_SELECTORS)
    if not input_box:
        # 与 Doubao 一致，先尝试自愈一次
        print(
            "[WARN] DeepSeek chat input not found on first try, attempting to recover by reloading chat page..."
        )
        try:
            click_new_conversation(page)
        except Exception as e:
            print(f"[WARN] DeepSeek recovery click_new_conversation failed: {e}")

        input_box = pick_first_visible(page, CHAT_INPUT_SELECTORS)
        if not input_box:
            raise RuntimeError(
                "Chat input not found. Please ensure you are logged in and on the chat page."
            )

    # Ensure '联网搜索' online mode is enabled before sending
    ensure_online_mode_enabled(page)

    # Count assistant messages before sending
    assistant_before = 0
    try:
        message_list = page.locator(MESSAGE_LIST_SELECTOR)
        if message_list.count() > 0:
            assistant_before = message_list.locator("div.ds-message._63c77b1").count()
    except Exception:
        pass

    # Input prompt
    print(f"[INFO] Sending prompt: {prompt_text[:50]}...")
    # 直接聚焦，不再等待 click 完成，避免因前端动画/抖动导致的超时告警
    try:
        input_box.evaluate("el => el.focus()")
    except Exception:
        pass
    time.sleep(0.05)

    # Type the prompt（减少间隔，加快输入）
    page.keyboard.type(prompt_text)
    time.sleep(0.15)

    # Send via Enter key (more reliable than button click for DeepSeek)
    send_ts = time.time()
    print("[DEBUG] Pressing Enter to send DeepSeek prompt (no click on send button)")
    page.keyboard.press("Enter")

    # Wait for response
    response_text, inline_citations = wait_for_stream_completion_and_get_text(
        page, assistant_before, timeout_seconds=300
    )
    latency_ms = int((time.time() - send_ts) * 1000)

    url = page.url
    conversation_id = get_conversation_id_from_url(url)

    # Extract web search results if used
    web_search_results: List[Dict[str, str]] = []
    had_web_search_button = False
    try:
        # Get the latest assistant message container
        message_list = page.locator(MESSAGE_LIST_SELECTOR)
        if message_list.count() > 0:
            messages = message_list.locator("div.ds-message._63c77b1")
            if messages.count() > 0:
                last_message = messages.last
                web_search_results, had_web_search_button = extract_web_search_results(
                    page, last_message
                )
    except Exception as e:
        print(f"[WARN] Failed to extract web search results: {e}")

    # Try to detect model name (if visible in UI)
    model_name = ""
    try:
        # DeepSeek may show model in header or toggle
        model_indicators = [
            'button:has-text("DeepSeek")',
            'div[class*="model"]',
        ]
        for selector in model_indicators:
            try:
                elem = page.locator(selector).first
                if elem and elem.is_visible():
                    model_name = elem.inner_text().strip()
                    if model_name:
                        break
            except Exception:
                continue
    except Exception:
        pass

    # Detect online mode from toggle buttons
    mode_online = ""
    try:
        # Look for "联网搜索" toggle button
        online_toggle = page.locator('button:has-text("联网搜索")').first
        if online_toggle and online_toggle.is_visible():
            # Check if button has selected/active class
            classes = online_toggle.get_attribute("class") or ""
            mode_online = (
                "true" if "selected" in classes or "active" in classes else "false"
            )
    except Exception:
        pass

    # Determine status：如果有 web search 按钮但没解析出任何结果，则视为错误
    status = "ok"
    error_message = ""
    if not response_text:
        status = "empty"
        error_message = "No response text captured"
    elif had_web_search_button and not web_search_results:
        status = "error"
        error_message = (
            "DeepSeek web search button present but results could not be parsed"
        )

    item: Dict[str, Optional[str]] = {
        "website_name": website_name,
        "conversation_id": conversation_id,
        "item_url": url,
        "model_name": model_name,
        "mode_online": mode_online,
        "prompt_text": prompt_text,
        "response_text": response_text,
        "web_search_results": web_search_results,
        "response_language": detect_language(response_text),
        "latency_ms": latency_ms,
        "status": status,
        "error_message": error_message,
    }

    return item


def write_outputs(
    ndjson_path: str, md_path: str, items: List[Dict[str, Optional[str]]]
) -> None:
    if not items:
        return

    # Determine write mode
    ndjson_mode = "a" if os.path.exists(ndjson_path) else "w"
    md_mode = "a" if os.path.exists(md_path) else "w"

    # Write NDJSON
    with open(ndjson_path, ndjson_mode, encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    # Write markdown
    with open(md_path, md_mode, encoding="utf-8") as f:
        for it in items:
            conv_id = it.get("conversation_id") or "unknown"
            f.write(f"# Conversation {conv_id}\n\n")
            f.write(f"- **Website**: {it.get('website_name')}\n")
            f.write(f"- **URL**: {it.get('item_url')}\n")
            f.write(f"- **Model**: {it.get('model_name')}\n")
            f.write(f"- **Online Mode**: {it.get('mode_online')}\n")
            f.write(f"- **Language**: {it.get('response_language')}\n")
            f.write(f"- **Latency**: {it.get('latency_ms')} ms\n")

            f.write("\n## Prompt\n\n")
            f.write((it.get("prompt_text") or "").strip() + "\n\n")

            f.write("## Response\n\n")
            f.write((it.get("response_text") or "").strip() + "\n\n")

            # Write web search results if available
            web_search_results = it.get("web_search_results", [])
            if web_search_results:
                f.write("## Web Search Results\n\n")
                for idx, result in enumerate(web_search_results, 1):
                    title = result.get("title", "Search Result")
                    href = result.get("href", "")
                    snippet = result.get("snippet", "")

                    f.write(f"### {idx}. {title}\n\n")
                    if href:
                        f.write(f"- **URL**: {href}\n")
                    if snippet:
                        f.write(f"- **Snippet**: {snippet}\n")
                    f.write("\n")

            f.write("---\n\n")


def human_think_time(min_s: float = 0.8, max_s: float = 2.2) -> None:
    """Artificial delay helper.

    为了整体加速，这里的默认延迟区间建议保持较小；如需更保守可在调用处覆盖。
    """
    time.sleep(random.uniform(min_s, max_s))


def click_new_conversation(page) -> bool:
    """
    Reset to a fresh DeepSeek conversation.

    为了更稳定、也更快，这里直接通过跳转首页 URL 来「开启新对话」，
    不再依赖页面上的「开启新对话」按钮及其具体 DOM 结构 / 文案。
    """
    try:
        print("[INFO] Starting new DeepSeek conversation via home URL reload...")

        # 直接跳转到首页，相当于点击「开启新对话」
        page.goto(DEEPSEEK_HOME_URL)
        page.wait_for_load_state()
        # 页面通常加载很快，缩短额外等待
        time.sleep(0.4)

        # 等待输入框准备就绪
        for selector in CHAT_INPUT_SELECTORS:
            try:
                elem = page.locator(selector).first
                if elem.is_visible(timeout=5000):
                    print("[INFO] New DeepSeek conversation ready")
                    return True
            except Exception:
                continue

        print("[WARN] New DeepSeek conversation may not be fully loaded")
        return False
    except Exception as e:
        print(f"[WARN] Failed to start new DeepSeek conversation via home URL: {e}")
        return False


def extract_task_name(input_file: str) -> str:
    """Extract task name from input file name."""
    basename = os.path.basename(input_file)
    if basename.endswith("_input_prompts.txt"):
        return basename[: -len("_input_prompts.txt")]
    elif basename.endswith(".txt"):
        return basename[:-4]
    else:
        return basename


def load_processed_prompts(ndjson_path: str) -> set:
    """Load already processed prompts from existing NDJSON file."""
    if not os.path.exists(ndjson_path):
        return set()

    processed = set()
    ok_items: List[Dict[str, Optional[str]]] = []
    try:
        with open(ndjson_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        item = json.loads(line)
                        prompt_text = item.get("prompt_text", "").strip()
                        status = item.get("status", "ok")
                        if status == "ok":
                            ok_items.append(item)
                            if prompt_text:
                                processed.add(prompt_text)
                    except json.JSONDecodeError:
                        continue
        # Rewrite NDJSON to keep only status == "ok" items
        try:
            with open(ndjson_path, "w", encoding="utf-8") as wf:
                for it in ok_items:
                    wf.write(json.dumps(it, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[WARN] Failed to rewrite NDJSON with ok items only: {e}")

        print(
            f"[INFO] Found {len(processed)} already processed prompts (kept only status='ok')"
        )
        return processed
    except Exception as e:
        print(f"[WARN] Failed to load processed prompts: {e}")
        return set()


def main() -> None:
    ensure_dirs()

    # Parse sharding & multi-process arguments
    parser = argparse.ArgumentParser(description="DeepSeek chat scraper")
    parser.add_argument(
        "--shard-index",
        type=int,
        default=0,
        help="Current shard index (0-based)",
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        default=1,
        help="Total number of shards (for multi-process parallelism)",
    )
    parser.add_argument(
        "--spawn-workers",
        type=int,
        default=0,
        help=(
            "If >0, run as master process and spawn N worker processes with "
            "different --shard-index/--shard-count; master itself will not scrape."
        ),
    )
    args, _ = parser.parse_known_args()

    # Master mode：只负责启动多个子进程，每个子进程跑一个 shard
    if args.spawn_workers and args.spawn_workers > 0:
        worker_count = args.spawn_workers
        script_path = os.path.abspath(__file__)

        print(
            f"[INFO] Spawning {worker_count} DeepSeek worker processes "
            f"for shards 0..{worker_count - 1}"
        )

        processes = []
        for i in range(worker_count):
            cmd = [
                sys.executable,
                script_path,
                "--shard-index",
                str(i),
                "--shard-count",
                str(worker_count),
            ]
            print(f"[INFO]  - Starting worker shard {i}/{worker_count} with: {cmd}")
            proc = subprocess.Popen(cmd)
            processes.append(proc)
            # 略微错峰，避免同时抢资源
            time.sleep(2.0)

        # 等待所有子进程结束
        exit_codes = []
        for i, p in enumerate(processes):
            code = p.wait()
            exit_codes.append(code)
            print(f"[INFO] Worker shard {i} exited with code {code}")

        # 如果有任意非零退出码，把整体退出码设为非零，方便上层监控
        if any(code != 0 for code in exit_codes):
            print("[WARN] Some DeepSeek shards exited with non-zero status")
            sys.exit(1)
        else:
            print("[INFO] All DeepSeek shards completed successfully")
            return

    # Worker 模式：真正执行某个 shard 的抓取任务
    if args.shard_count <= 0:
        print("[ERROR] --shard-count must be >= 1")
        return
    if not (0 <= args.shard_index < args.shard_count):
        print(
            f"[ERROR] --shard-index must be in [0, {args.shard_count - 1}], got {args.shard_index}"
        )
        return

    ensure_dirs()

    # Find all input files matching pattern *_input_prompts.txt
    project_root = os.path.dirname(os.path.dirname(__file__))
    import glob

    input_files = glob.glob(os.path.join(project_root, "*_input_prompts.txt"))

    if not input_files:
        print("[WARN] No input files found matching pattern '*_input_prompts.txt'")
        print("[INFO] Looking for 'input_prompts.txt' as fallback...")
        fallback = os.path.join(project_root, "input_prompts.txt")
        if os.path.exists(fallback):
            input_files = [fallback]
        else:
            print("[ERROR] No input files found. Please create a file with prompts.")
            return

    print(
        f"[INFO] Found {len(input_files)} input file(s): {[os.path.basename(f) for f in input_files]}"
    )

    # Process each input file as a separate task
    for input_file in input_files:
        task_name = extract_task_name(input_file)
        print(f"\n{'='*60}")
        print(f"[INFO] Processing task: {task_name}")
        print(f"[INFO] Input file: {input_file}")
        print(f"[INFO] Shard: index={args.shard_index}, count={args.shard_count}")
        print(f"{'='*60}\n")

        prompts = read_prompts(input_file)
        if not prompts:
            print(f"[WARN] No prompts found in {input_file}, skipping...")
            continue

        # Generate output file names
        output_ndjson = os.path.join(
            OUTPUT_DIR, f"deepseek_conversations_{task_name}.ndjson"
        )
        output_md = os.path.join(OUTPUT_DIR, f"deepseek_conversations_{task_name}.md")

        # Load already processed prompts (also normalizes NDJSON to status=='ok')
        processed_prompts = load_processed_prompts(output_ndjson)

        # Filter out already processed prompts
        new_prompts = [p for p in prompts if p.strip() not in processed_prompts]

        if not new_prompts:
            print(f"[INFO] All prompts in {task_name} have been processed. Skipping...")
            continue

        if len(new_prompts) < len(prompts):
            print(
                f"[INFO] Skipping {len(prompts) - len(new_prompts)} already processed prompts"
            )
            print(f"[INFO] Processing {len(new_prompts)} new prompts")

        # Apply sharding: keep only prompts whose index matches this shard
        if args.shard_count > 1:
            sharded_prompts = [
                p
                for idx, p in enumerate(new_prompts)
                if idx % args.shard_count == args.shard_index
            ]
            print(
                f"[INFO] After sharding, this shard will process {len(sharded_prompts)} prompts"
            )
        else:
            sharded_prompts = new_prompts

        if not sharded_prompts:
            print(
                f"[INFO] No prompts assigned to shard {args.shard_index} for task '{task_name}', skipping..."
            )
            continue

        process_task(task_name, sharded_prompts, output_ndjson, output_md)


def process_task(
    task_name: str, prompts: List[str], output_ndjson: str, output_md: str
) -> None:
    """Process a single task with its prompts."""

    print("\n" + "=" * 60)
    print("⚠️  IMPORTANT: A NEW BROWSER WINDOW WILL OPEN")
    print("    Please login in THE NEW BROWSER WINDOW opened by the script")
    print("    NOT in your regular browser!")
    print("=" * 60 + "\n")

    with Camoufox(
        humanize=True,
        geoip=False,
        locale="zh-CN",
        headless=False,  # Explicitly show browser window
    ) as browser:
        page = browser.new_page(
            locale="zh-CN",
        )

        # Load cookies before navigation
        load_cookies_into_context(page, SESSION_COOKIES_FILE)

        print(f"[INFO] Opening DeepSeek in the Camoufox browser window...")
        page.goto(DEEPSEEK_HOME_URL)
        page.wait_for_load_state()

        # Restore storage
        load_storage_from_file(page, SESSION_STORAGE_FILE)

        # Reload to apply storage
        try:
            page.goto(DEEPSEEK_HOME_URL)
            page.wait_for_load_state()
        except Exception:
            pass

        print("[INFO] Waiting for manual login (up to 5 minutes).")
        print(
            "[INFO] Please login to DeepSeek and wait for the chat interface to appear."
        )

        if not wait_for_login(page, timeout_seconds=300):
            print("[ERROR] Login not detected within timeout. Please login and rerun.")
            return

        # Persist session after successful login
        save_cookies_from_context(page, SESSION_COOKIES_FILE)
        save_storage_to_file(page, SESSION_STORAGE_FILE)

        total_processed = 0
        consecutive_failures = 0
        for idx, prompt in enumerate(prompts):
            print(f"\n[INFO] Processing prompt {idx + 1}/{len(prompts)}")

            # Always click "New Conversation" before each prompt to ensure clean state
            if idx == 0:
                # For the first prompt, ensure we're in a new conversation
                print("[INFO] Starting new conversation for first prompt...")
                human_think_time(0.2, 0.5)
                click_new_conversation(page)
                human_think_time(0.2, 0.5)

            # Retry up to 3 times if status is not "ok"
            max_retries = 3
            item: Dict[str, Optional[str]] = {}
            for attempt in range(1, max_retries + 1):
                try:
                    item = send_prompt_and_collect(
                        page, prompt_text=prompt, website_name="DEEPSEEK"
                    )
                except Exception as e:
                    print(
                        f"[ERROR] Failed to process prompt (attempt {attempt}/{max_retries}): {e}"
                    )
                    url = page.url
                    item = {
                        "website_name": "DEEPSEEK",
                        "conversation_id": get_conversation_id_from_url(url),
                        "item_url": url,
                        "model_name": "",
                        "mode_online": "",
                        "prompt_text": prompt,
                        "response_text": "",
                        "web_search_results": [],
                        "response_language": "",
                        "latency_ms": 0,
                        "status": "error",
                        "error_message": str(e),
                    }

                status = item.get("status", "ok")
                if status == "ok":
                    break

                if attempt < max_retries:
                    print(
                        f"[WARN] Prompt failed with status '{status}', retrying after short delay..."
                    )
                    human_think_time(0.5, 1.2)

            # If still not ok after retries, skip saving this prompt
            if item.get("status") != "ok":
                print(
                    f"[ERROR] Prompt failed after {max_retries} attempts, skipping save for this prompt."
                )
                consecutive_failures += 1
            else:
                print(f"[INFO] Saving result {idx + 1}/{len(prompts)}...")
                write_outputs(output_ndjson, output_md, [item])
                total_processed += 1
                consecutive_failures = 0
                print(f"[INFO] ✓ Saved to {os.path.basename(output_ndjson)}")

            # Start a new conversation for the next prompt
            if idx < len(prompts) - 1:
                human_think_time(0.3, 0.7)
                click_new_conversation(page)
                human_think_time(0.2, 0.5)

            # 如果连续多次未成功，暂停 5 分钟，避免持续失败
            if consecutive_failures >= 5:
                print(
                    "[WARN] Detected 5 consecutive non-ok results. Sleeping for 5 minutes to avoid cascading failures..."
                )
                time.sleep(300)
                consecutive_failures = 0

        # Save session state at the end
        save_cookies_from_context(page, SESSION_COOKIES_FILE)
        save_storage_to_file(page, SESSION_STORAGE_FILE)

        print(f"\n{'='*60}")
        print(f"[INFO] ✓ Task '{task_name}' completed!")
        print(f"[INFO] Processed {total_processed} prompts")
        print(f"[INFO] Results saved to:")
        print(f"  - {output_ndjson}")
        print(f"  - {output_md}")
        print(f"{'='*60}\n")

        # input("Press Enter to continue...")


if __name__ == "__main__":
    main()
