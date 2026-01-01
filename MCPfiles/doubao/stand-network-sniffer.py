import asyncio
import json
import os
import datetime
import re
import random
from playwright.async_api import async_playwright

# ==========================================
# 1. 配置与存储路径
# ==========================================
OUTPUT_DIR = "doubao_data"
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

# ⚠️ 账号配置：请确保目录下有对应的 json 文件
# 逻辑：每个账号对应一套 Cookie 和 Storage 文件
ACCOUNT_CONFIGS = [
    {
        "id": f"user_{i}", 
        "cookies_path": f"doubao_cookies_{i}.json",
        "storage_path": f"doubao_storage_{i}.json" # 还原 storage 逻辑
    } 
    for i in range(2) # 示例：2个账号，根据实际情况修改
]

# 验证码 iframe 选择器 (还原)
CAPTCHA_IFRAME_SELECTOR = (
    'iframe[src*="rmc.bytedance.com/verifycenter/captcha"], '
    'iframe[src*="verifycenter/captcha"]'
)

# Login detection selectors
LOGIN_MODAL_SELECTOR = 'div[role="dialog"][aria-modal="true"] div[data-testid="login_content"]'
LOGIN_PHONE_INPUT_SELECTOR = 'input[data-testid="login_phone_number_input"]'
LOGIN_QR_SWITCHER_SELECTOR = 'div[data-testid="qrcode_switcher"]'
CHAT_INPUT_SELECTOR = 'textarea[data-testid="chat_input_input"]'

# Login wait config
LOGIN_WAIT_TIMEOUT_SEC = 180
LOGIN_POLL_INTERVAL_SEC = 1.5

# Login cookie groups (used to verify login state)
LOGIN_COOKIE_GROUP_A = ["sessionid", "sessionid_ss"]
LOGIN_COOKIE_GROUP_B = ["sid_tt", "sid_guard"]

# 随机 User-Agent 池
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0"
]

# 随机分辨率池
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864}
]

# ==========================================
# 2. JS HOOK 代码 (保持不变)
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
        if (fullUrl.includes('/chat/completion')) {
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
            } catch (error) { throw error; }
        } else { return originalFetch(url, options); }
    };
})();
"""

# ==========================================
# 3. 辅助功能函数 (还原你的原始逻辑)
# ==========================================

# --- 验证码相关 (转为 Async) ---
async def is_captcha_iframe_visible(page) -> bool:
    try:
        # 这里的 count() 和 is_visible() 必须 await
        iframe_loc = page.locator(CAPTCHA_IFRAME_SELECTOR)
        count = await iframe_loc.count()
        if count <= 0:
            return False
        try:
            # 取第一个可见性
            return await iframe_loc.first.is_visible(timeout=500)
        except:
            return True
    except:
        return False

async def wait_for_captcha_iframe_clear(page, worker_id) -> None:
    """
    等待验证码消失 (Async 版)
    """
    last_notice = 0.0
    while True:
        try:
            visible = await is_captcha_iframe_visible(page)
            if not visible:
                return
        except:
            pass

        now = datetime.datetime.now().timestamp()
        if now - last_notice > 5:
            last_notice = now
            print(f"[Worker-{worker_id}] [WAIT] 🛑 正在等待验证码 iframe 消失 (请手动验证)...")
        
        await asyncio.sleep(1.5) # 异步等待，不阻塞其他账号

# --- Cookie/Storage 管理 (转为 Async) ---
def read_storage_file(storage_path: str):
    if not os.path.exists(storage_path):
        return {}, {}
    try:
        with open(storage_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        local_items = data.get("localStorage", {}) or {}
        session_items = data.get("sessionStorage", {}) or {}
        if not isinstance(local_items, dict):
            local_items = {}
        if not isinstance(session_items, dict):
            session_items = {}
        return local_items, session_items
    except Exception as e:
        print(f"[WARN] Read storage failed: {e}")
        return {}, {}

async def add_storage_init_script(page, storage_path: str) -> bool:
    """Inject storage before first navigation."""
    local_items, session_items = read_storage_file(storage_path)
    if not local_items and not session_items:
        return False
    payload = {"localItems": local_items, "sessionItems": session_items}
    payload_json = json.dumps(payload, ensure_ascii=True)
    script = f"""(() => {{
        try {{
            const payload = {payload_json};
            const localItems = payload && payload.localItems ? payload.localItems : {{}};
            const sessionItems = payload && payload.sessionItems ? payload.sessionItems : {{}};
            for (const [k, v] of Object.entries(localItems)) localStorage.setItem(k, v);
            for (const [k, v] of Object.entries(sessionItems)) sessionStorage.setItem(k, v);
        }} catch (e) {{
            console.warn('[INIT] Storage init failed', e);
        }}
    }})();"""
    await page.add_init_script(script)
    print(f"[INIT] Storage init script installed from {storage_path}")
    return True

async def load_storage_to_page(page, storage_path: str):
    """还原 Storage 加载逻辑"""
    try:
        local_items, session_items = read_storage_file(storage_path)
        
        if local_items:
            await page.evaluate("""(items) => { 
                for (const [k,v] of Object.entries(items)) localStorage.setItem(k, v) 
            }""", local_items)
        if session_items:
            await page.evaluate("""(items) => { 
                for (const [k,v] of Object.entries(items)) sessionStorage.setItem(k, v) 
            }""", session_items)
        print(f"[INIT] Loaded storage from {storage_path}")
    except Exception as e:
        print(f"[WARN] Load storage failed: {e}")

async def save_storage_from_page(page, storage_path: str):
    """还原 Storage 保存逻辑"""
    try:
        ls = await page.evaluate("""() => Object.fromEntries(Object.entries(localStorage))""")
        ss = await page.evaluate("""() => Object.fromEntries(Object.entries(sessionStorage))""")
        with open(storage_path, "w", encoding="utf-8") as f:
            json.dump({"localStorage": ls, "sessionStorage": ss}, f, ensure_ascii=False, indent=2)
        print(f"[SAVE] Storage saved to {storage_path}")
    except Exception as e:
        print(f"[WARN] Save storage failed: {e}")

async def save_cookies_from_context(context, cookies_path: str):
    try:
        cookies = await context.cookies()
        with open(cookies_path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print(f"[SAVE] Cookies saved to {cookies_path}")
    except Exception as e:
        print(f"[WARN] Save cookies failed: {e}")

def read_cookies_file(cookies_path: str):
    if not os.path.exists(cookies_path):
        return []
    try:
        with open(cookies_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            print(f"[WARN] Cookies file format invalid: {cookies_path}")
            return []
        return data
    except Exception as e:
        print(f"[WARN] Read cookies failed: {e}")
        return []

async def is_login_modal_visible(page) -> bool:
    try:
        modal = page.locator(LOGIN_MODAL_SELECTOR)
        count = await modal.count()
        if count <= 0:
            return False
        try:
            return await modal.first.is_visible(timeout=500)
        except:
            return True
    except:
        return False

async def is_chat_input_visible(page) -> bool:
    try:
        inp = page.locator(CHAT_INPUT_SELECTOR)
        count = await inp.count()
        if count <= 0:
            return False
        try:
            return await inp.first.is_visible(timeout=500)
        except:
            return True
    except:
        return False

def has_login_cookies(cookie_list) -> bool:
    names = {c.get("name") for c in cookie_list if isinstance(c, dict)}
    group_a = any(name in names for name in LOGIN_COOKIE_GROUP_A)
    group_b = any(name in names for name in LOGIN_COOKIE_GROUP_B)
    return group_a and group_b

async def ensure_logged_in(page, context, cookies_path: str, storage_path: str, worker_id: int, require_login_modal: bool) -> bool:
    start = datetime.datetime.now().timestamp()
    last_notice = 0.0
    seen_login_modal = False

    while True:
        if await is_captcha_iframe_visible(page):
            await wait_for_captcha_iframe_clear(page, worker_id)

        login_visible = await is_login_modal_visible(page)
        if login_visible:
            seen_login_modal = True
            now = datetime.datetime.now().timestamp()
            if now - last_notice > 5:
                last_notice = now
                print(f"[Worker-{worker_id}] [LOGIN] 需要登录，请手动完成登录。")
        else:
            if await is_chat_input_visible(page):
                cookies = await context.cookies()
                if require_login_modal and not seen_login_modal and not has_login_cookies(cookies):
                    await asyncio.sleep(LOGIN_POLL_INTERVAL_SEC)
                    continue
                if not has_login_cookies(cookies):
                    now = datetime.datetime.now().timestamp()
                    if now - last_notice > 5:
                        last_notice = now
                        print(f"[Worker-{worker_id}] [LOGIN] 发现输入框但登录 Cookie 不完整，继续等待。")
                    await asyncio.sleep(LOGIN_POLL_INTERVAL_SEC)
                    continue
                print(f"[Worker-{worker_id}] [LOGIN] 登录成功。")
                await save_cookies_from_context(context, cookies_path)
                await save_storage_from_page(page, storage_path)
                return True

        if datetime.datetime.now().timestamp() - start > LOGIN_WAIT_TIMEOUT_SEC:
            print(f"[Worker-{worker_id}] [LOGIN] 等待登录超时。")
            return False

        await asyncio.sleep(LOGIN_POLL_INTERVAL_SEC)

# ==========================================
# 4. 数据解析逻辑 (完全还原)
# ==========================================
def clean_text_final(text):
    if not text: return ""
    return text.strip()

def extract_search_info(content_list):
    queries, references = [], []
    for item in content_list:
        if "content" in item and "search_query_result_block" in item["content"]:
            search_block = item["content"]["search_query_result_block"]
            if "queries" in search_block: queries.extend(search_block["queries"])
            if "results" in search_block:
                for res in search_block["results"]:
                    if "text_card" in res:
                        card = res["text_card"]
                        references.append({
                            "title": clean_text_final(card.get("title", "")),
                            "url": card.get("url", ""),
                            "sitename": clean_text_final(card.get("sitename", "")),
                            "publish_time": card.get("publish_time_second", "")
                        })
    return queries, references

def parse_doubao_raw_data(raw_data: str) -> dict:
    """完全使用你提供的解析逻辑"""
    json_pattern = re.compile(r'^data:\s*(\{.*\})$', re.MULTILINE)
    json_strs = json_pattern.findall(raw_data)
    if not json_strs:
        chunks = raw_data.split('\n')
        for chunk in chunks:
            chunk = chunk.strip()
            if chunk.startswith("data: "):
                json_strs.append(chunk[6:])

    reply_parts, all_queries, all_refs, seen_urls = [], [], [], set()
    
    for json_str in json_strs:
        if json_str == "{}": continue
        try:
            data_obj = json.loads(json_str)
            def process_content_list(c_list):
                l_parts, l_qs, l_refs = [], [], []
                if isinstance(c_list, list):
                    for item in c_list:
                        if "content" in item and "text_block" in item["content"]:
                            text = item["content"]["text_block"].get("text", "")
                            if text: l_parts.append(text)
                    q, refs = extract_search_info(c_list)
                    l_qs.extend(q)
                    l_refs.extend(refs)
                return l_parts, l_qs, l_refs

            if "message" in data_obj and "content" in data_obj["message"]:
                try:
                    raw = data_obj["message"]["content"]
                    content_list = json.loads(raw) if isinstance(raw, str) else raw
                    p, q, r = process_content_list(content_list)
                    reply_parts.extend(p)
                    all_queries.extend(q)
                    all_refs.extend(r)
                except: pass
            
            if "patch_op" in data_obj:
                for op in data_obj["patch_op"]:
                    if "patch_value" in op and "content_block" in op["patch_value"]:
                        p, q, r = process_content_list(op["patch_value"]["content_block"])
                        reply_parts.extend(p)
                        all_queries.extend(q)
                        all_refs.extend(r)
        except: pass

    # 去重
    unique_refs = []
    for ref in all_refs:
        if ref["url"] and ref["url"] not in seen_urls:
            seen_urls.add(ref["url"])
            unique_refs.append(ref)
        elif not ref["url"] and ref["title"]:
             is_dup = any(existing["title"] == ref["title"] for existing in unique_refs)
             if not is_dup: unique_refs.append(ref)

    return {
        "reply": clean_text_final("".join(reply_parts)),
        "search_queries": list(dict.fromkeys(all_queries)),
        "references": unique_refs
    }

# ==========================================
# 5. Worker 逻辑 (集成所有原版逻辑)
# ==========================================

async def worker_process(worker_id: int, account_config: dict, queue: asyncio.Queue, browser, file_lock: asyncio.Lock):
    context = None
    try:
        # --- 1. Context 初始化 (指纹) ---
        ua = random.choice(USER_AGENTS)
        vp = random.choice(VIEWPORTS)
        print(f"[Worker-{worker_id}] 启动 | ID: {account_config['id']}")
        
        context = await browser.new_context(
            user_agent=ua,
            viewport=vp,
            ignore_https_errors=True,
            java_script_enabled=True
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        # --- 2. 加载 Cookies (还原) ---
        cookies_path = account_config['cookies_path']
        storage_path = account_config['storage_path']
        
        page = await context.new_page()
        await add_storage_init_script(page, storage_path)
        await page.add_init_script(JS_HOOK_CODE)

        cookies = read_cookies_file(cookies_path)
        storage_local, storage_session = read_storage_file(storage_path)
        has_saved_creds = bool(cookies) or bool(storage_local) or bool(storage_session)
        if cookies:
            try:
                await context.add_cookies(cookies)
                print(f"[INIT] Cookies loaded from {cookies_path}")
            except Exception as e:
                print(f"[WARN] Add cookies failed: {e}")
        
        print(f"[Worker-{worker_id}] 访问首页...")
        await page.goto("https://www.doubao.com/chat/")
        
        # --- 3. 登录检查/等待 (手动登录) ---
        if not await ensure_logged_in(page, context, cookies_path, storage_path, worker_id, require_login_modal=not has_saved_creds):
            print(f"[Worker-{worker_id}] [LOGIN] Exit worker: not logged in.")
            return
        
        # --- 4. 循环任务处理 ---
        while not queue.empty():
            try:
                # 检查验证码 (任务开始前先看一眼)
                await wait_for_captcha_iframe_clear(page, worker_id)
                
                # 检查输入框
                try:
                    await page.wait_for_selector(CHAT_INPUT_SELECTOR, timeout=5000)
                except:
                    if await is_captcha_iframe_visible(page):
                        print(f"[Worker-{worker_id}] ⚠️ 发现验证码！")
                        await wait_for_captcha_iframe_clear(page, worker_id)
                        continue
                    if await is_login_modal_visible(page):
                        if not await ensure_logged_in(page, context, cookies_path, storage_path, worker_id, require_login_modal=True):
                            break
                        continue
                    print(f"[Worker-{worker_id}] ❌ 无法找到输入框且无验证码/登录弹窗")
                    break # 跳出任务循环

                # 领取任务
                try:
                    prompt = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                
                print(f"[Worker-{worker_id}] 提问: {prompt[:10]}...")

                # 清理 hook 缓存
                await page.evaluate("window.__captured_requests__ = []")

                # 输入操作
                try:
                    textarea = page.locator(CHAT_INPUT_SELECTOR).first
                    await textarea.click()
                    await textarea.fill(prompt)
                    await asyncio.sleep(random.uniform(0.5, 1.2))
                    await page.keyboard.press("Enter")
                except Exception as e:
                    print(f"[Worker-{worker_id}] 输入失败: {e}")
                    queue.task_done()
                    continue

                # --- 5. 轮询获取数据 (包含验证码检测) ---
                wait_start = datetime.datetime.now()
                success = False
                
                while (datetime.datetime.now() - wait_start).seconds < 120:
                    # A. 实时检测验证码
                    if await is_captcha_iframe_visible(page):
                        print(f"[Worker-{worker_id}] 🛑 提问后弹出验证码！等待处理...")
                        await wait_for_captcha_iframe_clear(page, worker_id)
                        # 验证码消失后，可能需要重发，这里简单处理为继续等待或重新提问
                        # 为简化逻辑，这里选择继续等待(如果流还在)或跳出
                        await asyncio.sleep(1) 
                    
                    # B. 检查 Hook 数据
                    try:
                        captured = await page.evaluate("() => window.__captured_requests__")
                    except:
                        captured = []

                    completed_reqs = [x for x in captured if x.get('type') == 'SSE_COMPLETE']
                    
                    if completed_reqs:
                        latest = completed_reqs[-1]
                        raw_data = latest.get('fullData', '')
                        
                        if len(raw_data) > 300: # 简单过滤
                            # 解析
                            parsed = parse_doubao_raw_data(raw_data)
                            if parsed['reply']:
                                # 保存结果
                                entry = {
                                    "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "account": account_config['id'],
                                    "prompt": prompt,
                                    "reply": parsed['reply'],
                                    "references": parsed['references'],
                                    "queries": parsed['search_queries']
                                }
                                async with file_lock:
                                    with open(os.path.join(OUTPUT_DIR, "all_logs.jsonl"), "a", encoding="utf-8") as f:
                                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                                
                                print(f"[Worker-{worker_id}] ✅ 成功")
                                success = True
                                break # 退出轮询
                        else:
                            # 数据太短，清空继续等
                            await page.evaluate("window.__captured_requests__ = []")
                    
                    await asyncio.sleep(1)
                
                if not success:
                    print(f"[Worker-{worker_id}] ❌ 超时/失败: {prompt[:10]}")
                    # 可以选择把任务放回队列: await queue.put(prompt)

                # 任务结束后清理 & 随机休息
                await page.evaluate("window.__captured_requests__ = []")
                
                # --- 6. 每次任务后保存凭据 (还原) ---
                # 这保证了如果账号活跃度更新，我们能存下来
                await save_cookies_from_context(context, cookies_path)
                await save_storage_from_page(page, storage_path)
                
                await asyncio.sleep(random.uniform(3, 6))
                queue.task_done()

            except Exception as e:
                print(f"[Worker-{worker_id}] 循环异常: {e}")
                queue.task_done() # 防止队列卡死

    except Exception as e:
        print(f"[Worker-{worker_id}] 致命错误: {e}")
    finally:
        if context:
            await context.close()

# ==========================================
# 6. 主程序
# ==========================================
async def main():
    prompts = [
        "推荐5款比较火的蛋白粉",
        "健身前后吃什么对比",
        # ... 你的其他问题
    ] * 2 # 增加任务量测试

    queue = asyncio.Queue()
    for p in prompts:
        queue.put_nowait(p)
    
    file_lock = asyncio.Lock()
    
    print(f"[INIT] 队列任务: {queue.qsize()} | 账号数: {len(ACCOUNT_CONFIGS)}")

    async with async_playwright() as p:
        # 启动浏览器
        browser = await p.chromium.launch(
            headless=False, # 调试设为False，稳定后设为True
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        
        workers = []
        for i, conf in enumerate(ACCOUNT_CONFIGS):
            workers.append(asyncio.create_task(
                worker_process(i, conf, queue, browser, file_lock)
            ))
            
        await asyncio.gather(*workers)
        await browser.close()
        print("[FINISH] 全部完成")

if __name__ == "__main__":
    # if os.name == 'nt':
    #     asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
