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
# 3. 数据解析与保存函数
# ==========================================
def parse_doubao_content(raw_data: str) -> str:
    """
    从原始 SSE 流中提取纯文本回答。
    豆包的数据通常是 data: {"message": {"content": {"parts": ["..."]}}}
    """
    final_text = ""
    lines = raw_data.split('\n')
    
    for line in lines:
        line = line.strip()
        # 过滤掉心跳包和非数据行
        if not line.startswith("data:") or len(line) < 10:
            continue
            
        json_str = line[5:].strip() # 去掉 "data: "
        if json_str == "[DONE]": 
            continue
            
        try:
            data = json.loads(json_str)
            
            # 尝试提取 message -> content -> parts
            # 豆包有时返回全量，有时返回增量，这里假设取最长的一次作为结果（简单策略）
            if isinstance(data, dict):
                parts = data.get("message", {}).get("content", {}).get("parts", [])
                if parts:
                    content = parts[0]
                    # 如果这次的内容比上次长，说明是全量更新，更新结果
                    if len(content) > len(final_text):
                        final_text = content
        except:
            pass
            
    return final_text if final_text else "[解析为空，请查看原始 JSON]"

def save_result(prompt, raw_data, url):
    """保存数据到文件"""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    parsed_text = parse_doubao_content(raw_data)
    
    # 1. 保存为易读的 Markdown
    md_filename = os.path.join(OUTPUT_DIR, f"reply_{timestamp}.md")
    with open(md_filename, "w", encoding="utf-8") as f:
        f.write(f"# Question: {prompt}\n\n")
        f.write(f"- Time: {timestamp}\n")
        f.write(f"- URL: {url}\n\n")
        f.write("## Answer:\n\n")
        f.write(parsed_text)
    
    # 2. 保存原始数据 JSONL (备份用)
    json_filename = os.path.join(OUTPUT_DIR, "all_logs.jsonl")
    log_entry = {
        "time": timestamp,
        "prompt": prompt,
        "url": url,
        "raw_data_length": len(raw_data),
        "raw_data_preview": raw_data[:200], # 仅预览
        "parsed_text": parsed_text,
        "full_raw_data": raw_data # 完整数据
    }
    with open(json_filename, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        
    print(f"[SAVE] ✅ 已保存: {md_filename}")

# ==========================================
# 4. 主程序
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
        
        page = context.new_page()
        page.add_init_script(JS_HOOK_CODE) # 注入拦截器
        
        print("[NAV] 正在访问豆包...")
        page.goto("https://www.doubao.com/chat/")
        
        print("[LOGIN] 请扫码登录，登录成功后脚本将自动继续...")
        try:
            page.wait_for_selector('textarea[data-testid="chat_input_input"]', timeout=0)
            print("[LOGIN] ✅ 登录成功！")
        except:
            return

        # === 提问列表 ===
        questions = ["你好","2024诺贝尔奖得主"]
        
        for q in questions:
            print(f"\n{'='*50}")
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
                captured_list = page.evaluate("() => window.__captured_requests__")
                
                # 寻找 SSE_COMPLETE 标记
                completed = [req for req in captured_list if req.get('type') == 'SSE_COMPLETE']
                
                if completed:
                    # 拿到最新的一条
                    data_obj = completed[-1]
                    raw_data = data_obj.get('fullData', '')
                    
                    # 简单过滤：如果数据太短（比如只有几十字节），可能是报错信息，继续等待更好的结果
                    if len(raw_data) > 500: 
                        print(f"[SUCCESS] 抓取成功！长度: {len(raw_data)}")
                        
                        # === 保存数据 ===
                        save_result(q, raw_data, data_obj.get('url'))
                        success = True
                        break
                    else:
                        print(f"[INFO] 抓到短数据 ({len(raw_data)} chars)，可能是错误或心跳，继续等待...")
                        # 清空当前捕获，继续等下一条更好的
                        page.evaluate("window.__captured_requests__ = []")
                
                time.sleep(1)
            
            if not success:
                print("[WARN] 本轮提问超时或未抓取到有效数据。")
            
            # 随机休息，避免被封
            time.sleep(3)

        print("\n[FINISH] 任务全部完成。")
        time.sleep(5)

if __name__ == "__main__":
    run_scraper()