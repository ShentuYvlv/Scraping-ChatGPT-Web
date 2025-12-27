import time
import json
import os
import datetime
import re
from playwright.sync_api import sync_playwright

# ==========================================
# 1. 配置与存储路径
# ==========================================
OUTPUT_DIR = "doubao_data"
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

SESSION_COOKIES_FILE = os.path.join("doubao_cookies.json")
SESSION_STORAGE_FILE = os.path.join("doubao_storage.json")

# ByteDance VerifyCenter captcha iframe (seen on Doubao):
# <iframe src="https://rmc.bytedance.com/verifycenter/captcha/v2?...">
CAPTCHA_IFRAME_SELECTOR = (
    'iframe[src*="rmc.bytedance.com/verifycenter/captcha"], '
    'iframe[src*="verifycenter/captcha"]'
)

# ==========================================
# 2. JS HOOK 代码 (核心拦截逻辑)
# ==========================================
JS_HOOK_CODE = """
(() => {
    if (window.__hook_installed__) return;
    window.__hook_installed__ = true;
    window.__captured_requests__ = [];
    console.log('[HOOK_V2] Installing enhanced fetch hook...');

    const originalFetch = window.fetch;

    window.fetch = async function(url, options = {}) {
        const fullUrl = url.toString();
        // 标记：只拦截 chat/completion 接口
        if (fullUrl.includes('/chat/completion')) {
            console.log('[FETCH_HOOK] 🔴 Intercepted:', fullUrl.substring(0, 50) + "...");
            try {
                const response = await originalFetch(url, options);
                const clonedResponse = response.clone();
                const contentType = response.headers.get('content-type') || '';

                if (contentType.includes('event-stream') || contentType.includes('stream')) {
                    const reader = clonedResponse.body.getReader();
                    const decoder = new TextDecoder();
                    let allData = '';

                    (async () => {
                        try {
                            while (true) {
                                const {done, value} = await reader.read();
                                if (done) {
                                    // 流读取完毕，推送到全局变量
                                    window.__captured_requests__.push({
                                        type: 'SSE_COMPLETE',
                                        url: fullUrl,
                                        fullData: allData,
                                        timestamp: Date.now()
                                    });
                                    break;
                                }
                                allData += decoder.decode(value, {stream: true});
                            }
                        } catch (e) {
                            console.error('[FETCH_HOOK] Stream error:', e);
                        }
                    })();
                }
                return response;
            } catch (error) {
                throw error;
            }
        } else {
            return originalFetch(url, options);
        }
    };
})();
"""

# ==========================================
# 3. 数据解析与清理函数
# ==========================================

def clean_text_final(text):
    if not text:
        return ""
    return text.strip()

def extract_search_info(content_list):
    """
    从 content_list 中提取搜索结果信息 (References)。
    """
    queries = []
    references = []
    
    for item in content_list:
        # 检查是否包含 search_query_result_block
        if "content" in item and "search_query_result_block" in item["content"]:
            search_block = item["content"]["search_query_result_block"]
            
            # 提取搜索关键词
            if "queries" in search_block and isinstance(search_block["queries"], list):
                queries.extend(search_block["queries"])
            
            # 提取搜索结果 (引用来源)
            if "results" in search_block and isinstance(search_block["results"], list):
                for res in search_block["results"]:
                    if "text_card" in res:
                        card = res["text_card"]
                        ref_item = {
                            "title": clean_text_final(card.get("title", "")),
                            "url": card.get("url", ""),
                            "sitename": clean_text_final(card.get("sitename", "")),
                            "publish_time": card.get("publish_time_second", "")
                        }
                        if ref_item["url"] or ref_item["title"]:
                            references.append(ref_item)
                            
    return queries, references

def parse_doubao_raw_data(raw_data: str) -> dict:
    """
    解析豆包的 SSE 原始数据，提取回复、搜索关键词和引用。
    使用正则提取 JSON，更加稳健。
    """
    # 匹配行首的 data: {...}
    json_pattern = re.compile(r'^data:\s*(\{.*\})$', re.MULTILINE)
    json_strs = json_pattern.findall(raw_data)
    
    # 如果正则没匹配到，尝试降级为简单的 split
    if not json_strs:
        chunks = raw_data.split('\n')
        for chunk in chunks:
            chunk = chunk.strip()
            if chunk.startswith("data: "):
                json_strs.append(chunk[6:])

    reply_parts = []
    all_search_queries = []
    all_references = []
    seen_urls = set()
    
    for json_str in json_strs:
        if json_str == "{}": continue
        
        try:
            data_obj = json.loads(json_str)
            
            def process_content_list(c_list):
                local_reply_parts = []
                local_queries = []
                local_refs = []
                
                if isinstance(c_list, list):
                    # 1. 提取文本
                    for item in c_list:
                        if "content" in item and "loading_block" in item["content"]:
                            continue
                            
                        if "content" in item and "text_block" in item["content"]:
                            text = item["content"]["text_block"].get("text", "")
                            if text:
                                local_reply_parts.append(text)
                    
                    # 2. 提取搜索信息
                    q, refs = extract_search_info(c_list)
                    local_queries.extend(q)
                    local_refs.extend(refs)
                    
                return local_reply_parts, local_queries, local_refs

            # Case A: message -> content
            if "message" in data_obj and "content" in data_obj["message"]:
                try:
                    content_raw = data_obj["message"]["content"]
                    if isinstance(content_raw, str):
                        content_list = json.loads(content_raw)
                    else:
                        content_list = content_raw
                        
                    r_parts, qs, refs = process_content_list(content_list)
                    reply_parts.extend(r_parts)
                    all_search_queries.extend(qs)
                    all_references.extend(refs)
                except: pass
            
            # Case B: patch_op
            if "patch_op" in data_obj:
                for op in data_obj["patch_op"]:
                    if "patch_value" in op:
                        val = op["patch_value"]
                        if "content_block" in val:
                            r_parts, qs, refs = process_content_list(val["content_block"])
                            reply_parts.extend(r_parts)
                            all_search_queries.extend(qs)
                            all_references.extend(refs)
        except:
            pass

    full_reply = "".join(reply_parts)
    full_reply = clean_text_final(full_reply)
    
    # 去重 References
    unique_references = []
    for ref in all_references:
        if ref["url"] and ref["url"] not in seen_urls:
            seen_urls.add(ref["url"])
            unique_references.append(ref)
        elif not ref["url"] and ref["title"]:
             is_dup = False
             for existing in unique_references:
                 if existing["title"] == ref["title"]:
                     is_dup = True
                     break
             if not is_dup:
                 unique_references.append(ref)

    unique_queries = list(dict.fromkeys(all_search_queries))
    
    return {
        "reply": full_reply,
        "search_queries": unique_queries,
        "references": unique_references
    }


def is_probably_human_challenge(page, raw_data: str = "") -> bool:
    """
    Best-effort detection of captcha / human verification overlays.
    We intentionally keep this heuristic broad because Doubao's challenge
    UI may change over time.
    """
    try:
        if raw_data:
            lowered = raw_data.lower()
            if any(k in lowered for k in ("captcha", "verify", "verification", "challenge", "geetest")):
                return True
    except Exception:
        pass

    try:
        # Common embedded challenge providers / iframes
        iframe_loc = page.locator(
            'iframe[src*="captcha"], iframe[src*="challenge"], iframe[src*="verify"], iframe[src*="geetest"]'
        )
        if iframe_loc.count() > 0:
            try:
                if iframe_loc.first.is_visible(timeout=500):
                    return True
            except Exception:
                return True

        # Common DOM markers (ids/classes)
        marker_loc = page.locator(
            '[id*="captcha"], [class*="captcha"], [id*="geetest"], [class*="geetest"], [class*="challenge"]'
        )
        if marker_loc.count() > 0:
            try:
                if marker_loc.first.is_visible(timeout=500):
                    return True
            except Exception:
                return True

        # Common CN texts
        for t in ("验证码", "安全验证", "完成验证", "人机验证", "滑动验证", "拖动滑块", "验证后继续"):
            try:
                # Only treat as challenge if the text is actually visible.
                if page.get_by_text(t, exact=False).first.is_visible(timeout=500):
                    return True
            except Exception:
                continue
    except Exception:
        # If we fail to query DOM (navigation, etc), don't treat as challenge here.
        return False

    return False


def is_captcha_iframe_visible(page) -> bool:
    try:
        iframe_loc = page.locator(CAPTCHA_IFRAME_SELECTOR)
        if iframe_loc.count() <= 0:
            return False
        try:
            return iframe_loc.first.is_visible(timeout=500)
        except Exception:
            return True
    except Exception:
        return False


def wait_for_captcha_iframe_clear(page) -> None:
    """Wait until the VerifyCenter captcha iframe disappears (best-effort)."""
    last_notice = 0.0
    while True:
        try:
            if not is_captcha_iframe_visible(page):
                return
        except Exception:
            pass

        now = time.time()
        if now - last_notice > 5:
            last_notice = now
            print("[WAIT] Still waiting for captcha iframe to disappear...")
        time.sleep(1.5)


def wait_for_human_challenge_clear(page) -> None:
    """
   方案1：检测到疑似验证码/拦截后，原地等待用户手动验证，不做 goto/reload/重试导航。
    """
    print("\n" + "!" * 60)
    print("[BLOCK] 🛑 疑似触发验证码/人机验证。")
    print("[ACTION] 请在脚本打开的浏览器窗口里手动完成验证。")
    print("[WAIT] 脚本将原地等待验证完成（不会刷新/跳转页面）...")
    print("!" * 60 + "\n")
    time.sleep(3)
    last_notice = 0.0
    while True:
        try:
            iframe_visible = is_captcha_iframe_visible(page)
            if not iframe_visible:
                print("[RESUME] ✅ 验证 iframe 已消失，准备继续...")
                time.sleep(2)
                return

            now = time.time()
            if now - last_notice > 5:
                last_notice = now
                if iframe_visible:
                    print("[WAIT] 仍在等待验证码 iframe 消失...")
                elif is_probably_human_challenge(page):
                    print("[WAIT] 验证相关 DOM 仍存在，继续等待...")
                else:
                    print("[WAIT] 输入框尚未恢复，继续等待...")
        except Exception:
            # Navigation or transient errors; keep waiting.
            pass

        time.sleep(1.5)

def save_result(prompt, raw_data, url):
    """保存数据到文件，返回是否保存成功（是否有有效内容）"""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 使用新的解析函数
    parsed_data = parse_doubao_raw_data(raw_data)
    reply_text = parsed_data["reply"]
    queries = parsed_data["search_queries"]
    references = parsed_data["references"]
    
    # 调试信息
    if not reply_text:
        print(f"[WARN] ⚠️ 解析后回复为空！原始数据长度: {len(raw_data)}")
        # 调试用：保存失败的 raw_data
        # debug_file = os.path.join(OUTPUT_DIR, f"debug_failed_{timestamp}.txt")
        # with open(debug_file, "w", encoding="utf-8") as f:
        #     f.write(raw_data)
        # return False
    else:
        print(f"[INFO] 解析成功，回复长度: {len(reply_text)}")

    # 保存原始数据 JSONL，包含解析后的字段
    json_filename = os.path.join(OUTPUT_DIR, "all_logs.jsonl")
    log_entry = {
        "time": timestamp,
        "prompt": prompt,
        "url": url,
        "raw_data_length": len(raw_data),
        "raw_data_preview": raw_data[:200], 
        "reply": reply_text,
        "search_queries": queries,
        "references": references,
        "full_raw_data": raw_data 
    }
    with open(json_filename, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\\n")
        
    print(f"[SAVE] ✅ 已追加到: {json_filename}")
    return True

# ==========================================
# 4. 凭证管理函数
# ==========================================

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

# ==========================================
# 5. 主程序
# ==========================================
def run_scraper():
    print("[INIT] 启动浏览器...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        # 屏蔽 webdriver 特征
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        # === 1. 加载 Cookies ===
        load_cookies_into_context(context, SESSION_COOKIES_FILE)

        page = context.new_page()
        page.add_init_script(JS_HOOK_CODE) # 注入拦截器
        
        print("[NAV] 正在访问豆包...")
        page.goto("https://www.doubao.com/chat/")
        
        # === 2. 加载 Storage 并刷新 ===
        load_storage_from_file(page, SESSION_STORAGE_FILE)
        # 如果加载了 storage，最好刷新一下页面让它生效
        if os.path.exists(SESSION_STORAGE_FILE):
            print("[NAV] 刷新页面以应用 LocalStorage...")
            page.reload()
            page.wait_for_load_state()
        
        print("[LOGIN] 请扫码登录，登录成功后脚本将自动继续...")
        try:
            page.wait_for_selector('textarea[data-testid="chat_input_input"]', timeout=0)
            print("[LOGIN] ✅ 登录成功！")
            
            # === 3. 登录成功后立即保存凭据 ===
            save_cookies_from_context(context, SESSION_COOKIES_FILE)
            save_storage_to_file(page, SESSION_STORAGE_FILE)
            
        except:
            return

        # === 提问列表 ===
        # Read prompts from repo root (script is under MCPfiles/doubao/)
        prompts_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "test_input_prompts.txt",
        )
        questions = []
        if os.path.exists(prompts_path):
            print(f"[INIT] Reading prompts from {prompts_path}")
            with open(prompts_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
                questions = lines[:20] # 只取前20条
        else:
            print(f"[WARN] Prompts file not found at {prompts_path}, using default.")
            questions = ["你好", "2024诺贝尔奖得主"]
        
        print(f"[INIT] Loaded {len(questions)} prompts.")
        
        for idx, q in enumerate(questions):
            print(f"\\n{'='*50}")
            print(f"[ACTION] ({idx+1}/{len(questions)}) 开启新对话并提问: {q}")
            
            # 循环重试当前问题，直到成功
            while True:
                # === 开启新对话 ===
                try:
                    print("[NAV] 正在跳转首页以开启新对话...")
                    page.goto("https://www.doubao.com/chat/")
                    
                    # 尝试等待输入框
                    try:
                        page.wait_for_selector('textarea[data-testid="chat_input_input"]', timeout=5000)
                    except:
                        # 超时，说明可能被拦截
                        print("\\n" + "!"*50)
                        print("[BLOCK] 🛑 未检测到输入框，可能触发了验证码！")
                        print("[ACTION] 请现在去浏览器窗口手动完成验证/登录。")
                        print("[WAIT] 脚本正在无限期等待输入框恢复，完成后将自动继续...")
                        print("!"*50 + "\\n")

                        # 验证通过的判断：验证码 iframe 消失（实时检测，自动继续）
                        wait_for_captcha_iframe_clear(page)
                        print("[RESUME] ✅ 验证 iframe 已消失，准备重试当前问题...")
                        time.sleep(1)
                        continue # 重新开始当前问题的流程（重新跳转或直接输入）

                    # 清空 JS 里的缓存
                    page.evaluate("window.__captured_requests__ = []")
                    
                    # 输入并发送
                    input_box = page.locator('textarea[data-testid="chat_input_input"]').first
                    input_box.click()
                    input_box.fill(q)
                    time.sleep(0.5)
                    page.keyboard.press("Enter")
                except Exception as e:
                    print(f"[ERROR] 操作失败: {e}，稍后重试...")
                    time.sleep(2)
                    continue
                    
                print("[WAIT] 等待回答生成...")
                
                # 轮询检测是否拦截到数据
                start_time = time.time()
                request_success = False
                
                while time.time() - start_time < 120: 
                    try:
                        # 检查 hook 是否还存在 (页面可能刷新了)
                        hook_status = page.evaluate("() => typeof window.__captured_requests__")
                        if hook_status == 'undefined':
                            print("[WARN] Hook 丢失 (页面可能刷新)，重新初始化...")
                            page.evaluate("window.__captured_requests__ = []")
                            
                        captured_list = page.evaluate("() => window.__captured_requests__")
                    except Exception as e:
                        # 忽略执行上下文被销毁的错误，稍后重试
                        if "Execution context was destroyed" in str(e):
                            time.sleep(1)
                            continue
                        else:
                            print(f"[ERROR] 轮询出错: {e}")
                            break
                    
                    # 寻找 SSE_COMPLETE 标记
                    completed = [req for req in captured_list if req.get('type') == 'SSE_COMPLETE']
                    
                    if completed:
                        # 拿到最新的一条
                        data_obj = completed[-1]
                        raw_data = data_obj.get('fullData', '')
                        
                        if len(raw_data) > 500: 
                            # === 关键修改：检查是否解析出有效内容 ===
                            is_valid = save_result(q, raw_data, data_obj.get('url'))
                            
                            if is_valid:
                                print(f"[SUCCESS] 抓取并解析成功！")
                                request_success = True
                                break # 跳出轮询等待
                            else:
                                print(f"[BLOCK] 🛑 抓取到数据但解析为空 (Reply Empty)，认定为验证码拦截！")
                                # 方案1：原地等待用户完成验证码验证，避免马上 goto 导致页面刷新
                                wait_for_human_challenge_clear(page)
                                # 清空当前捕获，避免重复处理这条坏数据
                                page.evaluate("window.__captured_requests__ = []")
                                # 触发重试逻辑：跳出轮询，外层 while True 会重新开始
                                break 
                        else:
                            print(f"[INFO] 抓到短数据 ({len(raw_data)} chars)，可能是错误或心跳，继续等待...")
                            page.evaluate("window.__captured_requests__ = []")
                    
                    time.sleep(1)
                
                if request_success:
                    # 成功完成当前问题，跳出 while True，进入下一题
                    break
                else:
                    print("[WARN] 本轮提问失败或被拦截，准备重试当前问题...")
                    time.sleep(3)

            # 随机休息，避免被封
            time.sleep(3)

        # === 4. 任务结束后再次保存凭据（更新 session） ===
        save_cookies_from_context(context, SESSION_COOKIES_FILE)
        save_storage_to_file(page, SESSION_STORAGE_FILE)

        print("\\n[FINISH] 任务全部完成。")
        time.sleep(5)

if __name__ == "__main__":
    run_scraper()
