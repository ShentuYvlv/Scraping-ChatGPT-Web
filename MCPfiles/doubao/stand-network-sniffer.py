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

# 字节跳动滑块/验证码 iframe 选择器
# 常见域名包括 rmc.bytedance.com 或 verifycenter
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


def is_captcha_iframe_visible(page) -> bool:
    """
    检测验证码 iframe 是否存在且可见
    """
    try:
        # 检查是否有匹配的 iframe 元素
        iframe_loc = page.locator(CAPTCHA_IFRAME_SELECTOR)
        count = iframe_loc.count()
        if count > 0:
            # 只要有一个主要 iframe 可见，就认为被拦截了
            # 注意：有时候 iframe 存在但 display:none，所以要 check visibility
            if iframe_loc.first.is_visible(timeout=200):
                return True
        return False
    except Exception:
        return False


def wait_for_captcha_iframe_clear(page) -> None:
    """
    阻塞等待，直到 VerifyCenter 验证码 iframe 消失。
    用户需手动在浏览器中完成验证。
    """
    print("\\n" + "!"*50)
    print("[BLOCK] 🛑 检测到验证码/滑块！脚本已暂停。")
    print("[ACTION] 请前往浏览器窗口手动完成验证。")
    print("!"*50 + "\\n")
    
    last_notice = 0.0
    while True:
        # 如果不可见，说明验证成功或页面刷新了
        if not is_captcha_iframe_visible(page):
            print("[RESUME] ✅ 验证码 iframe 已消失，继续运行...")
            return

        now = time.time()
        if now - last_notice > 5:
            last_notice = now
            print("[WAIT] 等待手动验证中...")
        time.sleep(1.0)

def extract_search_info(content_list):
    queries = []
    references = []
    
    for item in content_list:
        if "content" in item and "search_query_result_block" in item["content"]:
            search_block = item["content"]["search_query_result_block"]
            
            if "queries" in search_block and isinstance(search_block["queries"], list):
                queries.extend(search_block["queries"])
            
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
    # 匹配行首的 data: {...}
    json_pattern = re.compile(r'^data:\s*(\{.*\})$', re.MULTILINE)
    json_strs = json_pattern.findall(raw_data)
    
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
                    for item in c_list:
                        if "content" in item and "loading_block" in item["content"]:
                            continue
                        if "content" in item and "text_block" in item["content"]:
                            text = item["content"]["text_block"].get("text", "")
                            if text:
                                local_reply_parts.append(text)
                    
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
    
    # 去重
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

def save_result(prompt, raw_data, url):
    """保存数据到文件"""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    parsed_data = parse_doubao_raw_data(raw_data)
    reply_text = parsed_data["reply"]
    queries = parsed_data["search_queries"]
    references = parsed_data["references"]
    
    if not reply_text:
        print(f"[WARN] ⚠️ 解析后回复为空 (Raw Length: {len(raw_data)})")
    else:
        print(f"[INFO] 解析成功，回复长度: {len(reply_text)}")

    json_filename = os.path.join(OUTPUT_DIR, "all_logs.jsonl")
    log_entry = {
        "time": timestamp,
        "prompt": prompt,
        "url": url,
        "raw_data_length": len(raw_data),
        "reply": reply_text,
        "search_queries": queries,
        "references": references,
        # "full_raw_data": raw_data # 如果文件太大可以注释掉
    }
    with open(json_filename, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        
    print(f"[SAVE] ✅ 已追加到: {json_filename}")
    return True

# ==========================================
# 4. 凭证管理函数
# ==========================================

def load_cookies_into_context(context, cookies_path: str) -> None:
    try:
        if os.path.exists(cookies_path):
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
    except Exception as e:
        print(f"[WARN] Failed to save cookies: {e}")

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
    except Exception as e:
        print(f"[WARN] Failed to save storage: {e}")

# ==========================================
# 5. 主程序
# ==========================================
def run_scraper(prompt_text: str, repeat_times: int = 20):
    print(f"[INIT] 启动浏览器...")
    print(f"[TASK] 目标: 提问 '{prompt_text}' 共 {repeat_times} 次")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        # === 1. 加载 Cookies ===
        load_cookies_into_context(context, SESSION_COOKIES_FILE)

        page = context.new_page()
        page.add_init_script(JS_HOOK_CODE)
        
        print("[NAV] 正在访问豆包...")
        page.goto("https://www.doubao.com/chat/")
        
        # === 2. 加载 Storage 并刷新 ===
        load_storage_from_file(page, SESSION_STORAGE_FILE)
        if os.path.exists(SESSION_STORAGE_FILE):
            print("[NAV] 刷新页面以应用 LocalStorage...")
            page.reload()
            page.wait_for_load_state()
        
        print("[LOGIN] 请扫码登录，登录成功后脚本将自动继续...")
        try:
            # 等待登录成功（输入框出现）
            page.wait_for_selector('textarea[data-testid="chat_input_input"]', timeout=0)
            print("[LOGIN] ✅ 登录成功！")
            
            save_cookies_from_context(context, SESSION_COOKIES_FILE)
            save_storage_to_file(page, SESSION_STORAGE_FILE)
            
        except:
            return

        # === 循环重复提问 ===
        for i in range(repeat_times):
            current_count = i + 1
            print(f"\\n{'='*50}")
            print(f"[ACTION] ({current_count}/{repeat_times}) 开启新对话并提问: {prompt_text}")
            
            # 内部循环：用于处理拦截重试
            while True:
                # 1. 检查是否一开始就被拦截
                if is_captcha_iframe_visible(page):
                    wait_for_captcha_iframe_clear(page)
                    # 验证码消失后，重新进行当前轮次
                    print("[RETRY] 验证完成，重试当前步骤...")
                    continue 

                # === 开启新对话 ===
                try:
                    print("[NAV] 跳转首页...")
                    page.goto("https://www.doubao.com/chat/")
                    
                    # 再次检查 iframe (goto 后容易触发)
                    if is_captcha_iframe_visible(page):
                        wait_for_captcha_iframe_clear(page)
                        continue

                    # 等待输入框
                    try:
                        page.wait_for_selector('textarea[data-testid="chat_input_input"]', timeout=5000)
                    except:
                        # 超时了，很有可能是因为验证码挡住了输入框
                        if is_captcha_iframe_visible(page):
                            wait_for_captcha_iframe_clear(page)
                            continue
                        else:
                            print("[ERROR] 找不到输入框且未检测到iframe，刷新重试...")
                            page.reload()
                            time.sleep(2)
                            continue

                    # 清空 Hook 缓存
                    page.evaluate("window.__captured_requests__ = []")
                    
                    # 输入并发送
                    input_box = page.locator('textarea[data-testid="chat_input_input"]').first
                    input_box.click()
                    input_box.fill(prompt_text)
                    time.sleep(0.5)
                    page.keyboard.press("Enter")
                except Exception as e:
                    print(f"[ERROR] 操作异常: {e}，重试...")
                    time.sleep(2)
                    continue
                    
                print("[WAIT] 等待回答生成...")
                
                # 轮询检测数据或拦截
                start_time = time.time()
                request_success = False
                block_detected = False
                
                while time.time() - start_time < 120: 
                    # --- 核心修改：优先检测 Iframe ---
                    if is_captcha_iframe_visible(page):
                        print("[BLOCK] 🛑 在等待结果时检测到验证码！")
                        wait_for_captcha_iframe_clear(page)
                        block_detected = True
                        break # 跳出等待循环，外层 while True 会触发 continue 重试
                    
                    # 检查数据
                    try:
                        captured_list = page.evaluate("() => window.__captured_requests__")
                    except:
                        time.sleep(1)
                        continue
                    
                    # 寻找完成的请求
                    completed = [req for req in captured_list if req.get('type') == 'SSE_COMPLETE']
                    
                    if completed:
                        data_obj = completed[-1]
                        raw_data = data_obj.get('fullData', '')
                        
                        # --- 核心修改：不再判断 len(raw_data) > 500 ---
                        # 只要 hook 抓到了 SSE_COMPLETE，就认为请求结束了，直接尝试保存
                        print(f"[INFO] 抓取到数据 (Length: {len(raw_data)})")
                        save_result(prompt_text, raw_data, data_obj.get('url'))
                        
                        request_success = True
                        break # 成功，跳出等待循环
                    
                    time.sleep(1)
                
                # 根据等待结果决定下一步
                if block_detected:
                    # 被拦截了，且在上面已经 wait_for_captcha_iframe_clear 了
                    # 这里重置 hook，然后 continue 外层循环重试
                    page.evaluate("window.__captured_requests__ = []")
                    print("[RETRY] 准备重新发送问题...")
                    continue 
                
                if request_success:
                    # 成功拿到数据，退出 retry 循环，进行下一次提问
                    print(f"[SUCCESS] 第 {current_count} 次任务完成。")
                    break
                else:
                    print(f"[TIMEOUT] 第 {current_count} 次等待超时，可能网络卡顿，重试...")
                    time.sleep(2)

            # 每次成功后的间隔
            time.sleep(3)

        save_cookies_from_context(context, SESSION_COOKIES_FILE)
        save_storage_to_file(page, SESSION_STORAGE_FILE)

        print("\\n[FINISH] 任务全部完成。")
        time.sleep(5)

if __name__ == "__main__":
    MY_PROMPT = "推荐5款比较火的蛋白粉"
    REPEAT_COUNT = 2

    run_scraper(MY_PROMPT, REPEAT_COUNT)