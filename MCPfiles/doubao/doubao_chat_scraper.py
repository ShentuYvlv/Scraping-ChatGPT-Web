#!/usr/bin/env python
"""
Doubao Chat Scraper

Based on the Kimi/DeepSeek scraper architecture but adapted for Doubao's UI:
- Messages live in a scrollable list container `div.inter-H_fm37`
- The latest model reply is one of the children of that container
- Chat input is `textarea[data-testid="chat_input_input"]`
- Main send button is `button[id="flow-end-msg-send"]` with different states
- Doubao automatically performs online search when needed (no explicit toggle)
- Web search references are surfaced via a "参考 X 篇资料" button

This script:
- Waits for manual login in a Camoufox-driven browser
- Sends prompts from `*_input_prompts.txt` files (one per line)
- Starts a (best-effort) new conversation per prompt
- Waits for streaming completion using text stability + send/stop button states
- Extracts structured web search references from the side panel after clicking
  the "参考 X 篇资料" button
- Saves results to NDJSON and Markdown files under `output/`
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


DOUBAO_HOME_URL = "https://www.doubao.com/chat/"
USER_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), ".camoufox_profile", "doubao"
)
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output")
SESSION_COOKIES_FILE = os.path.join(os.path.dirname(__file__), "doubao_cookies.json")
SESSION_STORAGE_FILE = os.path.join(os.path.dirname(__file__), "doubao_storage.json")

# Doubao-specific selectors based on provided UI hints
CHAT_INPUT_SELECTORS: List[str] = [
    'textarea[data-testid="chat_input_input"]',  # Primary selector
    "textarea[placeholder*='发送']",  # Fallback by placeholder (if any)
    "textarea",  # Last resort
]

# Message list container – holds all chat bubbles (user + assistant)
MESSAGE_LIST_SELECTOR = (
    "#chat-route-layout > div > main > div > div.flex.h-full.w-full.flex-col.items-center > "
    "div.flex.h-200.w-full.flex-shrink.flex-grow.flex-col.items-center > div > "
    "div.scroll-view-OEiNXD.container-gkoWqI.reverse-BdVQca > div > div > div.inter-H_fm37"
)

# Within Doubao messages we don't yet have a stable assistant-only class.
# We'll use the last child in the message list as the latest model reply.
ASSISTANT_MESSAGE_SELECTORS: List[str] = [
    "div[data-role='assistant']",
    "article[role='article'][data-author-role='assistant']",
]

# Generic citation links (Doubao does not use the DeepSeek-style "-6" markers)
CITATION_LINK_SELECTOR = 'a[href^="http"]'

# Send / stop button selectors for Doubao
SEND_BTN_DISABLED_SELECTOR = 'button[disabled][id="flow-end-msg-send"]'
SEND_BTN_ENABLED_SELECTOR = 'button[aria-disabled="false"][id="flow-end-msg-send"]'
STOP_BTN_SELECTOR = 'div[data-testid="chat_input_local_break_button"]:not(.hidden)'

# Deep thinking toggle ("深度思考") near the input box
DEEP_THINK_TOGGLE_WRAPPER_SELECTOR = 'div[data-testid="use-deep-thinking-switch-btn"]'
DEEP_THINK_TOGGLE_BUTTON_SELECTOR = (
    "div[data-testid='use-deep-thinking-switch-btn'] button"
)

# Web search reference button and side panel
SEARCH_REFERENCE_BUTTON_SELECTOR = 'div[data-testid="search-reference-ui"]'
# Side panel: a scroll container that holds "参考资料" search results.
# Doubao has used multiple layouts over time, so we keep a small list of
# candidate selectors and try them in order.
SEARCH_PANEL_SCROLL_SELECTORS: List[str] = [
    # Older layout: right-side <aside>
    "aside[data-testid='samantha_layout_right_side'] div.scroll-H09izL",
    # Newer layout shown in captured HTML: <div data-testid='canvas_panel_container'>
    "div[data-testid='canvas_panel_container'] div.scroll-H09izL",
    # Fallback: any scroll container that has the "参考资料" header inside
    "div.scroll-H09izL:has(span.page-search-GNq5Qg)",
]

# For reusing some helper logic
STOP_BUTTON_SELECTORS: List[str] = [STOP_BTN_SELECTOR]


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
    """Find the first visible element from a list of selectors."""
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
    """Returns True only when the authenticated Doubao chat UI is visible."""
    try:
        current_url = page.url or ""
        print(f"[DEBUG] Current URL: {current_url}")

        # Look for chat input (DOM-based detection only)
        print("[DEBUG] Looking for Doubao chat input textarea...")
        chat_input = pick_first_visible(page, CHAT_INPUT_SELECTORS)
        if not chat_input:
            print("[DEBUG] Doubao chat input not found yet")
            return False

        print("[DEBUG] Doubao chat input found!")
        return True
    except Exception as e:
        print(f"[DEBUG] Exception in is_chat_ui_ready (Doubao): {e}")
        return False


def wait_for_login(page, timeout_seconds: int = 300) -> bool:
    print("\n" + "=" * 60)
    print("⚠️  ACTION REQUIRED:")
    print("    1. Find the NEW browser window opened by this script")
    print("    2. Login to Doubao in THAT window (not your regular browser)")
    print("    3. Wait for the chat interface to appear")
    print("    4. Script will automatically continue once logged in")
    print(f"    5. Timeout: {timeout_seconds//60} minutes")
    print("=" * 60 + "\n")

    # First quick check - maybe already logged in
    print("[INFO] Checking if already logged in (Doubao)...")
    if is_chat_ui_ready(page):
        print("[INFO] ✓ Already logged in! Doubao chat interface ready.")
        return True

    # Give user time to start login process（缩短初始等待以更快进入轮询）
    print("[INFO] Waiting 3 seconds for you to start login...")
    time.sleep(20)

    print("[INFO] Monitoring for Doubao chat input box...")
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
            f"[DEBUG] Doubao login check #{check_count} (Elapsed: {int(time.time()-start)}s)"
        )
        if is_chat_ui_ready(page):
            print("\n" + "=" * 60)
            print("✓ SUCCESS: Doubao chat interface detected!")
            print("✓ Ready to send prompts.")
            print("=" * 60 + "\n")
            return True
        # 更快轮询，加速检测到登录完成
        time.sleep(1.5)

    print("\n[ERROR] Doubao chat input not found after timeout")
    print("[ERROR] Please make sure you logged in the SCRIPT'S browser window")
    return False


def get_conversation_id_from_url(url: str) -> str:
    """Extract a conversation-like ID from the URL (best-effort)."""
    try:
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        parts = [p for p in path.split("?", 1)[0].split("/") if p]
        if parts:
            for part in reversed(parts):
                if len(part) > 10:
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


def is_model_responding(page) -> bool:
    """Return True if the Doubao stop button is visible (model currently responding)."""
    try:
        stop_btn = page.locator(STOP_BTN_SELECTOR)
        if stop_btn.count() > 0 and stop_btn.first.is_visible(timeout=2000):
            return True
    except Exception:
        pass
    return False


def is_generating(page) -> bool:
    """Backward-compatible alias of `is_model_responding` used in stream wait logic."""
    return is_model_responding(page)


def is_send_button_enabled(page) -> bool:
    """True if the main send button is enabled (ready to send)."""
    try:
        btn = page.locator(SEND_BTN_ENABLED_SELECTOR)
        if btn.count() > 0 and btn.first.is_visible(timeout=2000):
            return True
    except Exception:
        pass
    return False


def is_send_button_disabled(page) -> bool:
    """True if the main send button is disabled (no input or reply finished)."""
    try:
        btn = page.locator(SEND_BTN_DISABLED_SELECTOR)
        if btn.count() > 0 and btn.first.is_visible(timeout=2000):
            return True
    except Exception:
        pass
    return False


def ensure_deep_thinking_enabled(page) -> None:
    """Ensure Doubao '深度思考' toggle is turned ON before sending a prompt.

    Does nothing if the toggle is already enabled or not found.
    """
    try:
        wrapper = page.locator(DEEP_THINK_TOGGLE_WRAPPER_SELECTOR)
        if wrapper.count() == 0:
            print("[DEBUG] Deep thinking toggle wrapper not found")
            return

        wrapper_el = wrapper.first
        if not wrapper_el.is_visible(timeout=5000):
            print("[DEBUG] Deep thinking toggle wrapper found but not visible")
            return

        # The actual clickable button is inside the wrapper
        btn_loc = wrapper_el.locator("button")
        if btn_loc.count() == 0:
            print("[DEBUG] Deep thinking inner button not found")
            return

        btn = btn_loc.first
        if not btn.is_visible(timeout=5000):
            print("[DEBUG] Deep thinking button found but not visible")
            return

        state = (btn.get_attribute("data-checked") or "").lower()
        if state == "true":
            print("[DEBUG] Deep thinking already enabled")
            return

        print("[INFO] Enabling Doubao '深度思考' mode...")
        btn.click()
        time.sleep(0.3)

        # Best-effort re-check
        state_after = (btn.get_attribute("data-checked") or "").lower()
        if state_after == "true":
            print("[INFO] Doubao '深度思考' mode enabled")
        else:
            print("[WARN] Could not confirm Doubao '深度思考' mode is enabled")
    except Exception as e:
        print(f"[WARN] Failed to ensure Doubao deep thinking mode: {e}")


def extract_web_search_results(
    page, assistant_container
) -> Tuple[List[Dict[str, str]], bool]:
    """Extract web search results if the Doubao response used web search.

    Doubao shows a "参考 X 篇资料" button (data-testid="search-reference-ui")
    that opens a side panel with references.

    Returns list of dicts with: {href, title, snippet}
    """
    results: List[Dict[str, str]] = []
    had_reference_button = False

    try:
        # Helper: try to locate an already-open search panel
        def find_panel():
            panel_local = None
            for selector in SEARCH_PANEL_SCROLL_SELECTORS:
                try:
                    loc = page.locator(selector)
                    if loc.count() == 0:
                        continue
                    try:
                        first = loc.first
                        # Prefer visible, but don't strictly require it
                        try:
                            _ = first.is_visible(timeout=5000)
                        except Exception:
                            pass
                        panel_local = first
                        print(f"[DEBUG] Using Doubao search panel selector: {selector}")
                        break
                    except Exception:
                        panel_local = loc.first
                        print(
                            f"[DEBUG] Fallback to first match for selector: {selector}"
                        )
                        break
                except Exception as e:
                    print(
                        f"[DEBUG] Error locating Doubao search panel with {selector}: {e}"
                    )
                    continue
            return panel_local

        # 1) First, see if the panel is already open (Doubao sometimes auto-opens it)
        panel = find_panel()

        # 2) If not open yet, try to click the "参考 X 篇资料" button to open it
        if not panel:
            search_button = None
            try:
                if assistant_container is not None:
                    loc = assistant_container.locator(SEARCH_REFERENCE_BUTTON_SELECTOR)
                    if loc.count() > 0:
                        search_button = loc
            except Exception:
                search_button = None

            if search_button is None or search_button.count() == 0:
                # Fallback: search at page level
                try:
                    loc = page.locator(SEARCH_REFERENCE_BUTTON_SELECTOR)
                    if loc.count() > 0:
                        search_button = loc
                except Exception:
                    search_button = None

            if search_button is not None and search_button.count() > 0:
                had_reference_button = True
                print(
                    "[DEBUG] Found Doubao search-reference button, trying to open panel..."
                )
                # 多次短点击重试，避免单次 click 卡 30 秒
                panel = find_panel()
                if not panel:
                    click_ok = False
                    for attempt in range(3):
                        try:
                            btn = search_button.last
                            if btn.is_visible(timeout=5000):
                                print(
                                    f"[DEBUG] Clicking Doubao search-reference button (attempt {attempt+1}/3)"
                                )
                                btn.click(timeout=5000)
                                time.sleep(0.25)
                                click_ok = True
                                break
                        except Exception as e:
                            print(
                                f"[WARN] Failed to click Doubao search-reference button (attempt {attempt+1}/3): {e}"
                            )
                            time.sleep(0.25)
                    if not click_ok:
                        print(
                            "[WARN] Giving up opening Doubao search panel for this answer after 3 attempts"
                        )
                    # Re-try finding the panel after click attempts（无论成功与否，都再尝试一次）
                    panel = find_panel()

        if not panel:
            print(
                "[WARN] Could not find Doubao search results side panel (panel closed or layout changed)"
            )
            return results, had_reference_button

        # Each result is rendered as a text search item under the scroll container.
        # Structure from captured HTML (simplified):
        # <div data-testid="search-text-item">
        #   <a class="search-lIUYwC" href="https://...">
        #     <div class="search-item-title-...">TITLE</div>
        #     <div class="search-item-summary-...">SNIPPET ...</div>
        #     <div class="search-item-footer-...">...</div>
        #   </a>
        # </div>
        # 搜索结果是异步渲染的，这里做一个短轮询，避免刚打开面板时 count==0。
        result_items = None
        count = 0
        # 略微缩短轮询时间，总体等待约 2 秒
        for attempt in range(10):
            result_items = panel.locator("div[data-testid='search-text-item']")
            try:
                count = result_items.count()
            except Exception:
                count = 0
            if count > 0:
                break
            time.sleep(0.2)

        print(f"[DEBUG] Found {count} potential Doubao search-text items")

        for i in range(count):
            try:
                item = result_items.nth(i)

                # Main link (href)
                link = item.locator("a[href^='http']")
                if link.count() == 0:
                    link = item.locator("a[href]")
                if link.count() == 0:
                    continue

                link_el = link.first
                href = link_el.get_attribute("href") or ""
                if not href or not href.startswith("http"):
                    continue

                # Title – prefer dedicated title element
                title = ""
                try:
                    title_loc = item.locator("div[class*='search-item-title']")
                    if title_loc.count() > 0:
                        title = (title_loc.first.inner_text() or "").strip()
                except Exception:
                    title = ""

                # Snippet – prefer dedicated summary element
                snippet = ""
                try:
                    snippet_loc = item.locator("div[class*='search-item-summary']")
                    if snippet_loc.count() > 0:
                        snippet = (snippet_loc.first.inner_text() or "").strip()
                except Exception:
                    snippet = ""

                # Fallback: derive title/snippet from full link text
                if not title or not snippet:
                    try:
                        full_text = (link_el.inner_text() or "").strip()
                    except Exception:
                        full_text = ""

                    if full_text:
                        parts = [p.strip() for p in full_text.splitlines() if p.strip()]
                        if not title and parts:
                            title = parts[0][:160]
                        if not snippet and len(parts) > 1:
                            snippet = " ".join(parts[1:])[:400]

                results.append(
                    {
                        "href": href,
                        "title": title or href,
                        "snippet": snippet,
                    }
                )
            except Exception as e:
                print(f"[WARN] Failed to extract Doubao result {i}: {e}")
                continue

        print(f"[DEBUG] Extracted {len(results)} Doubao web search results")
    except Exception as e:
        print(f"[WARN] Failed to extract Doubao web search results: {e}")

    return results, had_reference_button


def html_to_markdown(html: str) -> str:
    """Simple HTML to Markdown converter (generic, no DeepSeek-specific hacks)."""
    import re

    # Remove script/style tags
    html = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE
    )

    # Doubao 内联引用：例如 <span class="container-bhqnGO">中国科普网</span>
    # 这里不再尝试获取 URL，仅将其转成 [中国科普网] 这种括号形式。
    # 支持 class 使用单引号或双引号，以及额外的其它 class。
    html = re.sub(
        r'<span[^>]*class=[\'"][^\'"]*\bcontainer-bhqnGO\b[^\'"]*[\'"][^>]*>(.*?)</span>',
        r"[\1]",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Convert <a href="...">text</a> to [text](url)
    def replace_link(match):
        full_tag = match.group(0)
        href_match = re.search(r'href=["\']([^"\']+)["\']', full_tag)
        href = href_match.group(1) if href_match else ""
        inner = re.sub(r"<[^>]+>", "", match.group(1)).strip()

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

    # Decode basic HTML entities
    html = html.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    html = html.replace("&nbsp;", " ").replace("&quot;", '"').replace("&#39;", "'")

    # Clean up multiple newlines and spaces
    html = re.sub(r"\n{3,}", "\n\n", html)
    html = re.sub(r" +", " ", html)

    return html.strip()


def wait_for_stream_completion_and_get_text(
    page, assistant_message_count_before: int, timeout_seconds: int = 300
) -> Tuple[str, List[str]]:
    """Wait for the Doubao assistant's response to complete streaming.

    Returns (response_text, list_of_citation_hrefs).
    """
    start = time.time()
    last_text = ""
    stable_ticks = 0
    last_change_time = start
    # 缩短最大等待时间与稳定检测间隔，加快认为「生成完成」的速度
    max_stream_seconds = max(45, min(180, int(timeout_seconds * 0.7)))
    required_stable_ticks = 3  # ~0.75s with 0.25s sleep

    def get_latest_assistant():
        # First try using the message list container and last child
        try:
            msg_list = page.locator(MESSAGE_LIST_SELECTOR)
            if msg_list.count() > 0:
                container = msg_list.first
                children = container.locator(":scope > div")
                if children.count() > assistant_message_count_before:
                    return children.nth(children.count() - 1)
        except Exception:
            pass

        # Then try any explicit assistant-role selectors
        for selector in ASSISTANT_MESSAGE_SELECTORS:
            loc = page.locator(selector)
            try:
                if loc.count() > assistant_message_count_before:
                    return loc.nth(loc.count() - 1)
            except Exception:
                continue

        # Fallback: try to find a generic last message-like container
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
            print("[DEBUG] Doubao generation started (stop button visible)")
            break

        # Also check for content appearing
        try:
            container = get_latest_assistant()
            if container:
                text_now = container.inner_text().strip()
                if text_now and len(text_now) > len(last_text):
                    last_text = text_now
                    last_change_time = time.time()
                    print("[DEBUG] Doubao content started appearing")
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
            print(f"[INFO] Forcing Doubao stop after {max_stream_seconds}s")
            try:
                stop_btn = pick_first_visible(page, STOP_BUTTON_SELECTORS)
                if stop_btn:
                    stop_btn.click()
            except Exception:
                pass

        # Completion criteria:
        # 1) Text has been stable for several ticks
        # 2) Send button is in the disabled state (input empty / reply finished)
        if stable_ticks >= required_stable_ticks and len(text) > 0:
            if is_send_button_disabled(page) and (not is_model_responding(page)):
                print(
                    f"[DEBUG] Doubao response completed ({len(text)} chars, send button disabled)"
                )
                break

        # Fallback timeout: no changes and not generating
        if (not is_generating(page)) and (time.time() - last_change_time > 6):
            print("[DEBUG] Doubao: no changes for 6s, assuming complete")
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

                    # Extract generic citation links
                    citation_links = content_container.locator(CITATION_LINK_SELECTOR)
                    for i in range(citation_links.count()):
                        try:
                            href = citation_links.nth(i).get_attribute("href")
                            if href and href.startswith("http"):
                                citations.append(href)
                        except Exception:
                            continue
                else:
                    # Fallback to plain text
                    final_text = container.inner_text().strip()
            except Exception:
                final_text = container.inner_text().strip()
    except Exception:
        pass

    # De-duplicate citation URLs while preserving order
    return final_text, list(dict.fromkeys(citations))


def send_prompt_and_collect(
    page, prompt_text: str, website_name: str = "DOUBAO"
) -> Dict[str, Optional[str]]:
    """Send a prompt to Doubao and collect the response."""
    input_box = pick_first_visible(page, CHAT_INPUT_SELECTORS)
    if not input_box:
        # 有时在跳转新对话后，输入框渲染稍慢，这里做一次轻量自愈：
        print(
            "[WARN] Doubao chat input not found on first try, attempting to recover by reloading chat page..."
        )
        try:
            # 再走一遍“新对话”逻辑，通常能让输入框挂载完成
            click_new_conversation(page)
        except Exception as e:
            print(f"[WARN] Recovery click_new_conversation failed: {e}")

        input_box = pick_first_visible(page, CHAT_INPUT_SELECTORS)
        if not input_box:
            raise RuntimeError(
                "Doubao chat input not found. Please ensure you are logged in and on the chat page."
            )

    # Count messages before sending (best-effort)
    assistant_before = 0
    try:
        message_list = page.locator(MESSAGE_LIST_SELECTOR)
        if message_list.count() > 0:
            container = message_list.first
            children = container.locator(":scope > div")
            assistant_before = children.count()
    except Exception:
        pass

    # Input prompt
    print(f"[INFO] Sending prompt to Doubao: {prompt_text[:50]}...")
    # 直接 focus，不再等待 click 完成，避免前端动画/抖动导致的超时告警
    try:
        input_box.evaluate("el => el.focus()")
    except Exception:
        pass
    time.sleep(0.05)

    # Type the prompt（减少间隔，加快输入）
    page.keyboard.type(prompt_text)
    time.sleep(0.15)

    # 直接使用 Enter 发送，放弃对发送按钮的点击，避免 click 卡死
    send_ts = time.time()
    print("[DEBUG] Pressing Enter to send Doubao prompt (no click on send button)")
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
    had_reference_button = False
    try:
        message_list = page.locator(MESSAGE_LIST_SELECTOR)
        if message_list.count() > 0:
            container = message_list.first
            children = container.locator(":scope > div")
            if children.count() > 0:
                last_message = children.nth(children.count() - 1)
                web_search_results, had_reference_button = extract_web_search_results(
                    page, last_message
                )
    except Exception as e:
        print(f"[WARN] Failed to extract Doubao web search results: {e}")

    # For Doubao, treat mode_online as whether this specific answer used web search.
    # 判定逻辑更宽松：
    # - 抓到了 web_search_results
    # - 或页面上存在 "参考 X 篇资料" 的按钮
    # - 或回复文本里包含 "参考 X 篇资料" / "参考 X 篇资料"
    used_search_ui = False
    try:
        loc = page.locator(SEARCH_REFERENCE_BUTTON_SELECTOR)
        used_search_ui = loc.count() > 0
    except Exception:
        used_search_ui = False

    used_search_text = ("参考 " in response_text) or ("参考资料" in response_text)

    mode_online = (
        "true"
        if (web_search_results or used_search_ui or used_search_text)
        else "false"
    )

    # Doubao model name detection (best-effort / can be refined later)
    model_name = ""
    try:
        model_indicators = [
            "button[data-testid*='model']",
            "div[class*='model']",
            "div[aria-label*='模型']",
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

    # Determine status: 若有参考按钮但没解析出任何 web_search_results，则视为错误
    status = "ok"
    error_message = ""
    if not response_text:
        status = "empty"
        error_message = "No response text captured"
    elif had_reference_button and not web_search_results:
        status = "error"
        error_message = (
            "Doubao web search reference button present but results could not be parsed"
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

    为了提速，默认延迟缩短；如需更保守，可在调用处传入更大的区间。
    """
    time.sleep(random.uniform(min_s, max_s))


def click_new_conversation(page) -> bool:
    """Click the 'New Conversation' button in Doubao (best-effort).

    为了更稳定、也更快，这里直接通过跳转首页 URL 来"重置"会话，
    不再依赖左侧的「新对话」按钮及其文案/DOM 结构。
    """
    try:
        print("[INFO] Starting new Doubao conversation via home URL reload...")
        # 直接跳转到首页 chat URL，相当于在 UI 里点「新对话」
        page.goto(DOUBAO_HOME_URL)
        page.wait_for_load_state()
        # 页面通常很快 ready，减少额外等待
        time.sleep(0.4)

        # 使用与登录阶段相同的 DOM 判定逻辑，更稳地等待聊天输入框就绪
        print("[DEBUG] Waiting for Doubao chat UI to become ready after reload...")
        start = time.time()
        timeout = 25  # 最多等 25 秒
        while time.time() - start < timeout:
            if is_chat_ui_ready(page):
                print("[INFO] Doubao new conversation ready")
                return True
            time.sleep(0.5)

        print(
            "[WARN] Doubao new conversation may not be fully loaded (chat input still missing)"
        )
        return False
    except Exception as e:
        print(f"[WARN] Failed to start Doubao new conversation via home URL: {e}")
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
            print(f"[WARN] Failed to rewrite Doubao NDJSON with ok items only: {e}")

        print(
            f"[INFO] Found {len(processed)} already processed Doubao prompts (kept only status='ok')"
        )
        return processed
    except Exception as e:
        print(f"[WARN] Failed to load processed Doubao prompts: {e}")
        return set()


def main() -> None:
    ensure_dirs()

    # Parse sharding & multi-process arguments
    parser = argparse.ArgumentParser(description="Doubao chat scraper")
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
            f"[INFO] Spawning {worker_count} Doubao worker processes "
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
            print(
                f"[INFO]  - Starting Doubao worker shard {i}/{worker_count} with: {cmd}"
            )
            proc = subprocess.Popen(cmd)
            processes.append(proc)
            # 略微错峰，避免同时抢资源
            time.sleep(2.0)

        # 等待所有子进程结束
        exit_codes = []
        for i, p in enumerate(processes):
            code = p.wait()
            exit_codes.append(code)
            print(f"[INFO] Doubao worker shard {i} exited with code {code}")

        if any(code != 0 for code in exit_codes):
            print("[WARN] Some Doubao shards exited with non-zero status")
            sys.exit(1)
        else:
            print("[INFO] All Doubao shards completed successfully")
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
        print(f"[INFO] Processing Doubao task: {task_name}")
        print(f"[INFO] Input file: {input_file}")
        print(f"[INFO] Shard: index={args.shard_index}, count={args.shard_count}")
        print(f"{'='*60}\n")

        prompts = read_prompts(input_file)
        if not prompts:
            print(f"[WARN] No prompts found in {input_file}, skipping...")
            continue

        # Generate output file names
        output_ndjson = os.path.join(
            OUTPUT_DIR, f"doubao_conversations_{task_name}.ndjson"
        )
        output_md = os.path.join(OUTPUT_DIR, f"doubao_conversations_{task_name}.md")

        # Load already processed prompts
        processed_prompts = load_processed_prompts(output_ndjson)

        # Filter out already processed prompts
        new_prompts = [p for p in prompts if p.strip() not in processed_prompts]

        if not new_prompts:
            print(
                f"[INFO] All prompts in Doubao task '{task_name}' have been processed. Skipping..."
            )
            continue

        if len(new_prompts) < len(prompts):
            print(
                f"[INFO] Skipping {len(prompts) - len(new_prompts)} already processed prompts"
            )
            print(f"[INFO] Processing {len(new_prompts)} new prompts for Doubao")

        # Apply sharding: keep only prompts whose index matches this shard
        if args.shard_count > 1:
            sharded_prompts = [
                p
                for idx, p in enumerate(new_prompts)
                if idx % args.shard_count == args.shard_index
            ]
            print(
                f"[INFO] After sharding, this Doubao shard will process {len(sharded_prompts)} prompts"
            )
        else:
            sharded_prompts = new_prompts

        if not sharded_prompts:
            print(
                f"[INFO] No prompts assigned to Doubao shard {args.shard_index} for task '{task_name}', skipping..."
            )
            continue

        process_task(task_name, sharded_prompts, output_ndjson, output_md)


def process_task(
    task_name: str, prompts: List[str], output_ndjson: str, output_md: str
) -> None:
    """Process a single Doubao task with its prompts."""

    print("\n" + "=" * 60)
    print("⚠️  IMPORTANT: A NEW BROWSER WINDOW WILL OPEN (Doubao)")
    print("    Please login in THE NEW BROWSER WINDOW opened by the script")
    print("    NOT in your regular browser!")
    print("=" * 60 + "\n")

    with Camoufox(
        humanize=True,
        geoip=False,
        locale="zh-CN",
        headless=False,
    ) as browser:
        page = browser.new_page(
            locale="zh-CN",
        )

        # Load cookies before navigation
        load_cookies_into_context(page, SESSION_COOKIES_FILE)

        print(f"[INFO] Opening Doubao in the Camoufox browser window...")
        page.goto(DOUBAO_HOME_URL)
        page.wait_for_load_state()

        # Restore storage
        load_storage_from_file(page, SESSION_STORAGE_FILE)

        # Reload to apply storage
        try:
            page.goto(DOUBAO_HOME_URL)
            page.wait_for_load_state()
        except Exception:
            pass

        print("[INFO] Waiting for manual Doubao login (up to 5 minutes).")
        print(
            "[INFO] Please login to Doubao and wait for the chat interface to appear."
        )

        if not wait_for_login(page, timeout_seconds=300):
            print(
                "[ERROR] Doubao login not detected within timeout. Please login and rerun."
            )
            return

        # Persist session after successful login
        save_cookies_from_context(page, SESSION_COOKIES_FILE)
        save_storage_to_file(page, SESSION_STORAGE_FILE)

        total_processed = 0
        consecutive_failures = 0
        for idx, prompt in enumerate(prompts):
            print(f"\n[INFO] Processing Doubao prompt {idx + 1}/{len(prompts)}")

            # Always start a fresh conversation before each prompt
            print("[INFO] Preparing new Doubao conversation...")
            human_think_time(0.2, 0.5)
            click_new_conversation(page)
            human_think_time(0.1, 0.3)

            # Retry up to 3 times if status is not "ok"
            max_retries = 3
            item: Dict[str, Optional[str]] = {}
            for attempt in range(1, max_retries + 1):
                try:
                    item = send_prompt_and_collect(
                        page, prompt_text=prompt, website_name="DOUBAO"
                    )
                except Exception as e:
                    print(
                        f"[ERROR] Failed to process Doubao prompt (attempt {attempt}/{max_retries}): {e}"
                    )
                    url = page.url
                    item = {
                        "website_name": "DOUBAO",
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
                        f"[WARN] Doubao prompt failed with status '{status}', retrying after short delay..."
                    )
                    human_think_time(0.5, 1.2)

            # If still not ok after retries, skip saving this prompt
            if item.get("status") != "ok":
                print(
                    f"[ERROR] Doubao prompt failed after {max_retries} attempts, skipping save for this prompt."
                )
                consecutive_failures += 1
            else:
                print(
                    f"[INFO] Saving Doubao result {idx + 1}/{len(prompts)} to output files..."
                )
                write_outputs(output_ndjson, output_md, [item])
                total_processed += 1
                consecutive_failures = 0
                print(f"[INFO] ✓ Saved to {os.path.basename(output_ndjson)}")

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
        print(f"[INFO] ✓ Doubao task '{task_name}' completed!")
        print(f"[INFO] Processed {total_processed} prompts")
        print(f"[INFO] Results saved to:")
        print(f"  - {output_ndjson}")
        print(f"  - {output_md}")
        print(f"{'='*60}\n")

        # input("Press Enter to continue...")


if __name__ == "__main__":
    main()
