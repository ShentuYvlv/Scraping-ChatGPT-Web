import time
import json
import os
import datetime
import re
from typing import Any, Dict, List, Tuple

from playwright.sync_api import sync_playwright


DEEPSEEK_CHAT_URL = "https://chat.deepseek.com/"
DEEPSEEK_SSE_PATH_SUBSTR = "/api/v0/chat/completion"
ONLINE_SEARCH_TOGGLE_SELECTOR = (
    'div[role="button"].ds-toggle-button:has-text("联网搜索")'
)

# This file lives at MCPfiles/deepseek/*.py, so repo root is 2 levels up.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "deepseek_data")
os.makedirs(OUTPUT_DIR, exist_ok=True)

SESSION_COOKIES_FILE = os.path.join(os.path.dirname(__file__), "deepseek_cookies.json")
SESSION_STORAGE_FILE = os.path.join(os.path.dirname(__file__), "deepseek_storage.json")


JS_HOOK_CODE = r"""
// DeepSeek 深度解析 Hook (based on MCPfiles/deepseek/deepseek-webhook.md, with Python-friendly capture)
(() => {
  if (window.__deepseek_hook_installed__) return;
  window.__deepseek_hook_installed__ = true;

  window.__deepseek_captured__ = window.__deepseek_captured__ || [];
  window.__deepseek_text__ = window.__deepseek_text__ || "";

  function parseDeepSeekPacket(json) {
    const findings = [];

    const extract = (item) => {
      if (!item) return;

      // 1) BATCH: { o: "BATCH", v: [ ... ] }
      if (item.o === "BATCH" && Array.isArray(item.v)) {
        item.v.forEach((subItem) => extract(subItem));
        return;
      }

      // 2) Search results: v is array and first element looks like {url,title,...}
      if (
        Array.isArray(item.v) &&
        item.v.length > 0 &&
        item.v[0] &&
        item.v[0].url &&
        item.v[0].title
      ) {
        findings.push({ type: "search", data: item.v });
        return;
      }

      // 3) Fragment append: v: [{ content: "...", type: "RESPONSE" }]
      if (Array.isArray(item.v)) {
        item.v.forEach((sub) => {
          if (sub && typeof sub.content === "string" && sub.content) {
            findings.push({ type: "text", data: sub.content });
          }
        });
        return;
      }

      // 4) String append: { v: "每年", p: ".../content", o: "APPEND" } OR { v: "每年" }
      if (typeof item.v === "string") {
        const ignoreValues = ["FINISHED", "WIP", "SEARCH", "DEFAULT", "ok"];
        const ignorePaths = [
          "/status",
          "/type",
          "/conversation_mode",
          "has_pending_fragment",
        ];

        const isSystemStatus = ignoreValues.includes(item.v);
        const isMetaPath = item.p && ignorePaths.some((k) => item.p.endsWith(k));
        if (!isSystemStatus && !isMetaPath) {
          findings.push({ type: "text", data: item.v });
        }
      }
    };

    extract(json);
    return findings;
  }

  function processRawText(text) {
    const lines = text.split("\n");
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const jsonStr = line.substring(6).trim();
      if (!jsonStr || jsonStr === "[DONE]") continue;

      try {
        const json = JSON.parse(jsonStr);
        const results = parseDeepSeekPacket(json);
        results.forEach((res) => {
          if (res.type === "text") {
            window.__deepseek_text__ += res.data;
          } else if (res.type === "search") {
            window.__deepseek_captured__.push({
              type: "search_results",
              data: res.data,
              time: Date.now(),
            });
          }
        });
      } catch (e) {
        // ignore JSON parse errors
      }
    }
  }

  console.log("[DS_HOOK] Installed (fetch+XHR) for DeepSeek completion stream.");

  // 1) Fetch interception
  const originalFetch = window.fetch;
  window.fetch = async function (url, options = {}) {
    const fullUrl = url.toString();
    if (!fullUrl.includes("/chat/completion")) {
      return originalFetch(url, options);
    }

    const response = await originalFetch(url, options);
    try {
      const clone = response.clone();
      const reader = clone.body.getReader();
      const decoder = new TextDecoder();

      (async () => {
        let buffer = "";
        let fullRaw = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          const chunk = decoder.decode(value, { stream: true });
          fullRaw += chunk;
          buffer += chunk;
          const lines = buffer.split("\n");
          buffer = lines.pop();
          processRawText(lines.join("\n"));
        }

        if (buffer) {
          fullRaw += buffer;
          processRawText(buffer);
        }

        window.__deepseek_captured__.push({
          type: "SSE_COMPLETE",
          via: "fetch",
          url: fullUrl,
          full_raw_data: fullRaw,
          reply: window.__deepseek_text__ || "",
          time: Date.now(),
        });
      })();
    } catch (e) {
      // do not break the page
    }

    return response;
  };

  // 2) XHR interception
  const originalXhrOpen = XMLHttpRequest.prototype.open;
  const originalXhrSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url) {
    this._url = url;
    return originalXhrOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function (body) {
    if (this._url && String(this._url).includes("/chat/completion")) {
      let lastIndex = 0;

      this.addEventListener("progress", () => {
        const newText = this.responseText.substring(lastIndex);
        lastIndex = this.responseText.length;
        processRawText(newText);
      });

      this.addEventListener("loadend", () => {
        try {
          window.__deepseek_captured__.push({
            type: "SSE_COMPLETE",
            via: "xhr",
            url: String(this._url),
            full_raw_data: this.responseText || "",
            reply: window.__deepseek_text__ || "",
            time: Date.now(),
          });
        } catch (e) {}
      });
    }
    return originalXhrSend.apply(this, arguments);
  };
})();
"""


def clean_text_final(text: str) -> str:
    return (text or "").strip()


def _extract_json_objects_from_sse(raw_data: str) -> List[Dict[str, Any]]:
    objects: List[Dict[str, Any]] = []

    for line in raw_data.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "{}" or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    return objects


def _extract_text_and_refs(obj: Any) -> Tuple[List[str], List[Dict[str, Any]]]:
    parts: List[str] = []
    refs: List[Dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "content" in node and isinstance(node.get("content"), str):
                parts.append(node["content"])
            if "references" in node and isinstance(node.get("references"), list):
                for r in node["references"]:
                    if isinstance(r, dict):
                        refs.append(r)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return parts, refs


def parse_deepseek_raw_data(raw_data: str) -> Dict[str, Any]:
    """
    Best-effort parser for DeepSeek SSE stream.

    The stream contains many `data: {...}` lines where the payload is JSON.
    We reconstruct the assistant reply by concatenating incremental text
    fragments we can find.
    """
    json_objects = _extract_json_objects_from_sse(raw_data)

    reply_parts: List[str] = []
    references: List[Dict[str, Any]] = []

    last_piece = ""
    for obj in json_objects:
        # Common shapes observed:
        # - {"v": "每年"} (a pure incremental token)
        # - {"v": {"response": {"fragments": [...]}}} (initial structure)
        # - {"v": [{"v": "在", "p": "response/fragments/0/content", "o": "APPEND"}], ...} (patch ops)
        v = obj.get("v", None)

        if isinstance(v, str):
            piece = v
            if piece and piece != last_piece:
                reply_parts.append(piece)
                last_piece = piece
            continue

        if isinstance(v, (dict, list)):
            parts, refs = _extract_text_and_refs(v)
            references.extend(refs)
            for piece in parts:
                if not piece:
                    continue
                if piece == last_piece:
                    continue
                reply_parts.append(piece)
                last_piece = piece
            continue

        # Fallback: walk the whole obj (rarely needed)
        parts, refs = _extract_text_and_refs(obj)
        references.extend(refs)
        for piece in parts:
            if piece and piece != last_piece:
                reply_parts.append(piece)
                last_piece = piece

    reply = clean_text_final("".join(reply_parts))

    # Deduplicate references (best-effort by url/href/title)
    uniq: List[Dict[str, Any]] = []
    seen: set = set()
    for r in references:
        key = r.get("url") or r.get("href") or r.get("title") or json.dumps(
            r, ensure_ascii=False, sort_keys=True
        )
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)

    return {"reply": reply, "references": uniq}


def save_result(
    prompt: str,
    raw_data: str,
    url: str,
    reply_text: str,
    search_results: List[Dict[str, Any]],
) -> bool:
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    reply_text = clean_text_final(reply_text)

    # Fallback: if hook failed to reconstruct text, try parsing raw SSE.
    if not reply_text and raw_data:
        try:
            parsed = parse_deepseek_raw_data(raw_data)
            reply_text = clean_text_final(parsed.get("reply", ""))
        except Exception:
            pass

    if not reply_text:
        print(
            f"[WARN] Parsed reply is empty. raw_data_length={len(raw_data)}. Saving debug dump..."
        )
        debug_file = os.path.join(OUTPUT_DIR, f"debug_failed_{timestamp}.txt")
        with open(debug_file, "w", encoding="utf-8") as f:
            f.write(raw_data)
        return False

    json_filename = os.path.join(OUTPUT_DIR, "all_logs.jsonl")
    log_entry = {
        "time": timestamp,
        "prompt": prompt,
        "url": url,
        "raw_data_length": len(raw_data),
        "raw_data_preview": raw_data[:200],
        "reply": reply_text,
        "search_results": search_results,
        "full_raw_data": raw_data,
    }
    with open(json_filename, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    print(f"[SAVE] Appended to: {json_filename}")
    return True


def load_cookies_into_context(context, cookies_path: str) -> None:
    try:
        if os.path.exists(cookies_path):
            print(f"[INIT] Loading cookies from {cookies_path}")
            with open(cookies_path, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            if isinstance(cookies, list) and len(cookies) > 0:
                context.add_cookies(cookies)
    except Exception as e:
        print(f"[WARN] Failed to load cookies: {e}")


def save_cookies_from_context(context, cookies_path: str) -> None:
    try:
        cookies = context.cookies()
        with open(cookies_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print(f"[SAVE] Cookies saved to {cookies_path}")
    except Exception as e:
        print(f"[WARN] Failed to save cookies: {e}")


def load_storage_from_file(page, storage_path: str) -> None:
    try:
        if not os.path.exists(storage_path):
            return
        print(f"[INIT] Loading storage from {storage_path}")
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
    except Exception as e:
        print(f"[WARN] Failed to load storage: {e}")


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
        print(f"[SAVE] Storage saved to {storage_path}")
    except Exception as e:
        print(f"[WARN] Failed to save storage: {e}")


def ensure_online_search_enabled(page, timeout_ms: int = 5000) -> None:
    """
    Ensure DeepSeek "联网搜索" toggle is enabled.

    The toggle button adds `ds-toggle-button--selected` when enabled.
    """
    toggle = page.locator(ONLINE_SEARCH_TOGGLE_SELECTOR).first
    try:
        if toggle.count() <= 0:
            print("[WARN] Online search toggle not found; skipping.")
            return
    except Exception:
        print("[WARN] Failed to query online search toggle; skipping.")
        return

    try:
        toggle.scroll_into_view_if_needed()
    except Exception:
        pass

    def is_enabled() -> bool:
        try:
            cls = toggle.get_attribute("class") or ""
            return "ds-toggle-button--selected" in cls
        except Exception:
            return False

    if is_enabled():
        print("[INFO] Online search already enabled.")
        return

    print("[INFO] Online search is OFF; enabling...")
    try:
        toggle.click(timeout=timeout_ms)
    except Exception as e:
        print(f"[WARN] Failed to click online search toggle: {e}")
        return

    start = time.time()
    while time.time() - start < max(timeout_ms, 500) / 1000.0:
        if is_enabled():
            print("[INFO] Online search enabled.")
            return
        time.sleep(0.2)

    print("[WARN] Online search toggle click did not reflect selected state.")


def run_scraper() -> None:
    print("[INIT] Starting DeepSeek network sniffer...")

    prompts_path = os.path.join(PROJECT_ROOT, "test_input_prompts.txt")
    questions: List[str] = []
    if os.path.exists(prompts_path):
        print(f"[INIT] Reading prompts from {prompts_path}")
        with open(prompts_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        questions = lines[:20]
    else:
        print(f"[WARN] Prompts file not found at {prompts_path}, using defaults.")
        questions = ["你好", "圣诞节是哪一天？"]

    print(f"[INIT] Loaded {len(questions)} prompts.")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        load_cookies_into_context(context, SESSION_COOKIES_FILE)

        page = context.new_page()
        page.add_init_script(JS_HOOK_CODE)

        print("[NAV] Opening DeepSeek...")
        page.goto(DEEPSEEK_CHAT_URL)
        page.wait_for_load_state()

        load_storage_from_file(page, SESSION_STORAGE_FILE)
        if os.path.exists(SESSION_STORAGE_FILE):
            print("[NAV] Reloading page to apply localStorage...")
            page.reload()
            page.wait_for_load_state()

        print("[LOGIN] Please login in the browser window. Script will continue automatically.")
        try:
            page.wait_for_selector("textarea", timeout=0)
            ensure_online_search_enabled(page)
            save_cookies_from_context(context, SESSION_COOKIES_FILE)
            save_storage_to_file(page, SESSION_STORAGE_FILE)
        except Exception:
            return

        for idx, q in enumerate(questions):
            print(f"\n{'='*50}")
            print(f"[ACTION] ({idx+1}/{len(questions)}) New conversation & prompt: {q}")

            while True:
                try:
                    print("[NAV] Going to home URL to start a new conversation...")
                    page.goto(DEEPSEEK_CHAT_URL)
                    page.wait_for_load_state()

                    try:
                        page.wait_for_selector("textarea", timeout=5000)
                    except Exception:
                        print("\n" + "!" * 50)
                        print("[BLOCK] Input box not found. Possibly blocked by captcha.")
                        print("[ACTION] Solve it in the browser window; script will wait...")
                        print("!" * 50 + "\n")
                        page.wait_for_selector("textarea", timeout=0)
                        time.sleep(2)
                        continue

                    ensure_online_search_enabled(page)

                    # Reset per-prompt capture buffers on the page.
                    page.evaluate("window.__deepseek_captured__ = []")
                    page.evaluate("window.__deepseek_text__ = ''")

                    input_box = page.locator("textarea").first
                    input_box.click()
                    input_box.fill(q)
                    time.sleep(0.5)
                    page.keyboard.press("Enter")
                except Exception as e:
                    print(f"[ERROR] Failed to send prompt: {e}. Retrying...")
                    time.sleep(2)
                    continue

                print("[WAIT] Waiting for SSE_COMPLETE...")
                start_time = time.time()
                request_success = False

                while time.time() - start_time < 120:
                    try:
                        hook_status = page.evaluate("() => typeof window.__deepseek_captured__")
                        if hook_status == "undefined":
                            print("[WARN] Hook lost. Reinitializing capture buffers...")
                            page.evaluate("window.__deepseek_captured__ = []")
                            page.evaluate("window.__deepseek_text__ = ''")

                        captured_list = page.evaluate("() => window.__deepseek_captured__")
                    except Exception as e:
                        if "Execution context was destroyed" in str(e):
                            time.sleep(1)
                            continue
                        print(f"[ERROR] Polling failed: {e}")
                        break

                    completed = [
                        req for req in captured_list if req.get("type") == "SSE_COMPLETE"
                    ]
                    if completed:
                        data_obj = completed[-1]
                        url = data_obj.get("url", "")
                        raw_data = data_obj.get("full_raw_data", "") or ""
                        reply_text = data_obj.get("reply", "") or ""

                        search_results: List[Dict[str, Any]] = []
                        for it in captured_list:
                            if it.get("type") == "search_results" and isinstance(
                                it.get("data"), list
                            ):
                                search_results.extend(it["data"])

                        if len(raw_data) > 200 or reply_text:
                            is_valid = save_result(
                                q, raw_data, url, reply_text, search_results
                            )
                            if is_valid:
                                print("[SUCCESS] Captured & parsed successfully.")
                                request_success = True
                                break

                            print(
                                "[BLOCK] Captured SSE but parsed empty; clearing capture & retrying..."
                            )
                            page.evaluate("window.__deepseek_captured__ = []")
                            page.evaluate("window.__deepseek_text__ = ''")
                            break

                        print(
                            f"[INFO] Captured short SSE ({len(raw_data)} chars). Clearing and waiting..."
                        )
                        page.evaluate("window.__deepseek_captured__ = []")
                        page.evaluate("window.__deepseek_text__ = ''")

                    time.sleep(1)

                if request_success:
                    break

                print("[WARN] This attempt failed/blocked; retrying the same prompt...")
                time.sleep(3)

            time.sleep(3)

        save_cookies_from_context(context, SESSION_COOKIES_FILE)
        save_storage_to_file(page, SESSION_STORAGE_FILE)
        print("\n[FINISH] All prompts processed.")
        time.sleep(5)


if __name__ == "__main__":
    run_scraper()
