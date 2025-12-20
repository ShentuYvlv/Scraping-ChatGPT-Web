import time
import json
import os
import datetime
from playwright.sync_api import sync_playwright

# ==========================================
# 1. 配置与存储路径
# ==========================================
OUTPUT_DIR = "doubao_data"
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

SESSION_COOKIES_FILE = os.path.join("doubao_cookies.json")
SESSION_STORAGE_FILE = os.path.join("doubao_storage.json")

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
    # 基础清理，去除转义符
    # 注意：在内存中处理时，\n 就是换行符，不需要 replace('\\n', '\n')
    # 但如果为了保存到 JSON 字符串里不乱，保持原样即可，JSON dump 会自动转义
    # 这里我们只处理一些不需要的字符
    return text.strip()

def extract_search_info(content_list):
    """
    从 content_list 中提取搜索结果信息 (References)。
    返回: (queries_list, references_list)
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
                        # 只保留有 URL 或标题的有效引用
                        if ref_item["url"] or ref_item["title"]:
                            references.append(ref_item)
                            
    return queries, references

def parse_doubao_raw_data(raw_data: str) -> dict:
    """
    解析豆包的 SSE 原始数据，提取回复、搜索关键词和引用。
    """
    # 关键修正：内存中的 raw_data 换行符是 \n，不是 \\n
    chunks = raw_data.split('\n')
    
    reply_parts = []
    all_search_queries = []
    all_references = []
    seen_urls = set()
    
    for chunk in chunks:
        chunk = chunk.strip()
        if chunk.startswith("data: "):
            json_str = chunk[6:] # 去掉 "data: "
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
                        content_list = json.loads(data_obj["message"]["content"])
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

def save_result(prompt, raw_data, url):
    """保存数据到文件"""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # 使用新的解析函数
    parsed_data = parse_doubao_raw_data(raw_data)
    reply_text = parsed_data["reply"]
    queries = parsed_data["search_queries"]
    references = parsed_data["references"]
    
    # 调试信息
    if not reply_text:
        print(f"[WARN] ⚠️ 解析后回复为空！原始数据长度: {len(raw_data)}")
        # 可以选择把前500个字符打出来看看
        # print(f"[DEBUG] 原始数据预览: {raw_data[:500]}")
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
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        
    print(f"[SAVE] ✅ 已追加到: {json_filename}")

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
        # 读取上一级目录下的 test_input_prompts.txt
        prompts_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "test_input_prompts.txt")
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
        
        for q in questions:
            print(f"\\n{'='*50}")
            print(f"[ACTION] 提问: {q}")
            
            # 清空 JS 里的缓存
            page.evaluate("window.__captured_requests__ = []")
            
            # 输入并发送
            try:
                input_box = page.locator('textarea[data-testid="chat_input_input"]').first
                input_box.click()
                input_box.fill(q)
                time.sleep(0.5)
                page.keyboard.press("Enter")
            except Exception as e:
                print(f"[ERROR] 发送失败: {e}")
                continue
                
            print("[WAIT] 等待回答生成...")
            
            # 轮询检测是否拦截到数据
            start_time = time.time()
            success = False
            
            while time.time() - start_time < 120: # 给足时间等待思考和生成
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
                        print("[WARN] 页面上下文丢失 (可能正在跳转)，等待恢复...")
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
                        print(f"[SUCCESS] 抓取成功！长度: {len(raw_data)}")
                        
                        # === 保存数据 ===
                        save_result(q, raw_data, data_obj.get('url'))
                        success = True
                        break
                    else:
                        print(f"[INFO] 抓到短数据 ({len(raw_data)} chars)，可能是错误或心跳，继续等待...")
                        page.evaluate("window.__captured_requests__ = []")
                
                time.sleep(1)
            
            if not success:
                print("[WARN] 本轮提问超时或未抓取到有效数据。")
            
            # 随机休息，避免被封
            time.sleep(3)

        # === 4. 任务结束后再次保存凭据（更新 session） ===
        save_cookies_from_context(context, SESSION_COOKIES_FILE)
        save_storage_to_file(page, SESSION_STORAGE_FILE)

        print("\\n[FINISH] 任务全部完成。")
        time.sleep(5)

if __name__ == "__main__":
    run_scraper()
