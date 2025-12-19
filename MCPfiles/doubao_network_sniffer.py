#!/usr/bin/env python
"""
Doubao Network Sniffer - 用于分析豆包的网络请求和响应格式

运行此脚本来捕获豆包的 API 调用，了解：
1. 聊天 API 的 URL 格式
2. 响应数据的结构（JSON/SSE/其他）
3. 搜索结果在 JSON 中的位置
"""
from camoufox.sync_api import Camoufox
import json
import time
import os

DOUBAO_HOME_URL = "https://www.doubao.com/chat/"

def main():
    print("="*80)
    print("豆包网络抓包工具 v2.0 - 基于 Fetch Hook")
    print("="*80)
    print("\n请按照以下步骤操作：")
    print("1. 在弹出的浏览器窗口中登录豆包")
    print("2. 发送一个会触发联网搜索的测试问题")
    print("   推荐问题：\"2024年诺贝尔物理学奖得主是谁？\"")
    print("   或：\"推荐一些人气高的网红拉面产品\"")
    print("3. 等待回答完成（包括搜索结果加载）")
    print("4. 查看终端输出的捕获信息")
    print("5. 按 Ctrl+C 退出并保存数据\n")

    with Camoufox(
        humanize=True,
        headless=False,
        locale="zh-CN",
        geoip=False,
    ) as browser:
        page = browser.new_page(locale="zh-CN")

        # 注入 CSS 确保 Mac 上中文字体正常显示
        page.add_init_script("""
            (() => {
                const injectFont = () => {
                    const style = document.createElement('style');
                    style.textContent = `
                        * {
                            font-family: "PingFang SC", "Hiragino Sans GB", "STHeiti",
                                         "Microsoft YaHei", "Arial", sans-serif !important;
                        }
                    `;
                    (document.head || document.documentElement).appendChild(style);
                };

                if (document.readyState === 'loading') {
                    document.addEventListener('DOMContentLoaded', injectFont);
                } else {
                    injectFont();
                }
            })();
        """)

        print(f"[INFO] 打开豆包...")
        page.goto(DOUBAO_HOME_URL)
        page.wait_for_load_state()

        print("[INFO] 注入 Fetch Hook...")
        # 在页面加载完成后注入 Hook（使用 evaluate 而不是 add_init_script）
        page.evaluate("""
            (() => {
                window.__captured_requests__ = [];
                console.log('[HOOK_V2] Installing enhanced fetch hook...');

                const originalFetch = window.fetch;

                window.fetch = async function(url, options = {}) {
                    const method = options.method || 'GET';
                    const fullUrl = url.toString();

                    // 只关注聊天相关的请求
                    if (fullUrl.includes('/chat/completion')) {
                        console.log('[FETCH_HOOK] 🔴 Intercepted:', method, fullUrl.substring(0, 80));

                        try {
                            // 调用原始 fetch
                            const response = await originalFetch(url, options);

                            // 检查 content-type
                            const contentType = response.headers.get('content-type') || '';
                            console.log('[FETCH_HOOK] Response type:', contentType);

                            // 克隆响应（这样不影响原始请求）
                            const clonedResponse = response.clone();

                            // 处理 SSE 流
                            if (contentType.includes('event-stream') || contentType.includes('stream')) {
                                console.log('[FETCH_HOOK] 📡 Reading SSE stream...');

                                const reader = clonedResponse.body.getReader();
                                const decoder = new TextDecoder();
                                let chunkCount = 0;
                                let allData = '';

                                // 异步读取流（不阻塞原始请求）
                                (async () => {
                                    try {
                                        while (true) {
                                            const {done, value} = await reader.read();
                                            if (done) {
                                                console.log('[FETCH_HOOK] ✅ Stream finished. Total chunks:', chunkCount);
                                                break;
                                            }

                                            const chunk = decoder.decode(value, {stream: true});
                                            chunkCount++;
                                            allData += chunk;

                                            // 实时输出 chunk
                                            console.log(`[FETCH_HOOK] Chunk #${chunkCount}:`, chunk.substring(0, 150));

                                            // 查找 URL
                                            const urlRegex = /https?:\\/\\/[^\\s"'<>\\)\\]]+/g;
                                            const urls = chunk.match(urlRegex);
                                            if (urls && urls.length > 0) {
                                                console.log(`[FETCH_HOOK] 🎯 Found ${urls.length} URLs:`, urls);
                                            }

                                            // 保存每个 chunk
                                            window.__captured_requests__.push({
                                                type: 'SSE_CHUNK',
                                                url: fullUrl,
                                                chunkNumber: chunkCount,
                                                data: chunk,
                                                timestamp: Date.now()
                                            });
                                        }

                                        // 保存完整数据
                                        window.__captured_requests__.push({
                                            type: 'SSE_COMPLETE',
                                            url: fullUrl,
                                            totalChunks: chunkCount,
                                            fullData: allData,
                                            timestamp: Date.now()
                                        });

                                    } catch (e) {
                                        console.error('[FETCH_HOOK] ❌ Stream read error:', e);
                                    }
                                })();
                            }

                            // 返回原始响应（不影响页面正常工作）
                            return response;

                        } catch (error) {
                            console.error('[FETCH_HOOK] ❌ Fetch error:', error);
                            throw error;
                        }
                    } else {
                        // 其他请求直接放行
                        return originalFetch(url, options);
                    }
                };

                console.log('[HOOK_V2] ✅ Enhanced fetch hook installed!');
            })();
        """)

        print("[INFO] 监听网络请求中... 请在浏览器中操作")
        print("[INFO] 脚本会每秒检查捕获的数据")
        print("[INFO] 按 Ctrl+C 停止并保存\n")

        last_count = 0
        try:
            # 轮询检查捕获的数据
            while True:
                time.sleep(1)

                # 每秒检查一次捕获的数据数量
                try:
                    current_count = page.evaluate("() => (window.__captured_requests__ || []).length")

                    if current_count > last_count:
                        print(f"[INFO] 捕获进度: {current_count} 条数据 (+{current_count - last_count})")
                        last_count = current_count

                        # 如果检测到 SSE_COMPLETE，说明一次对话结束
                        has_complete = page.evaluate("""
                            () => {
                                const items = window.__captured_requests__ || [];
                                return items.some(item => item.type === 'SSE_COMPLETE');
                            }
                        """)

                        if has_complete:
                            print("[INFO] ✅ 检测到完整的 SSE 响应")

                            # 检查是否有 URL
                            url_count = page.evaluate("""
                                () => {
                                    const items = window.__captured_requests__ || [];
                                    let count = 0;
                                    items.forEach(item => {
                                        const data = item.data || '';
                                        if (data.includes('https://') || data.includes('http://')) {
                                            count++;
                                        }
                                    });
                                    return count;
                                }
                            """)

                            if url_count > 0:
                                print(f"[INFO] 🎯 发现 {url_count} 个包含 URL 的数据块")

                except Exception as e:
                    # 页面可能正在加载或刷新
                    pass

        except KeyboardInterrupt:
            print("\n\n[INFO] 停止监听")

            # 从浏览器中提取捕获的数据
            try:
                captured_data = page.evaluate("() => window.__captured_requests__ || []")

                if captured_data:
                    # 保存到 MCPfiles 目录
                    output_file = os.path.join(
                        os.path.dirname(__file__),
                        "doubao_captured_api_data.json"
                    )
                    with open(output_file, 'w', encoding='utf-8') as f:
                        json.dump(captured_data, f, ensure_ascii=False, indent=2)
                    print(f"\n[INFO] 已保存 {len(captured_data)} 条 API 数据到 {output_file}")

                    # 打印摘要
                    print(f"\n[INFO] 捕获数据摘要:")
                    for i, item in enumerate(captured_data[:5]):  # 只显示前5条
                        chunk_type = item.get('type', '')
                        chunk_num = item.get('chunkNumber', '')
                        chunk_preview = item.get('data', '')[:80].replace('\n', ' ')
                        print(f"  {i+1}. {chunk_type} #{chunk_num}: {chunk_preview}")
                    if len(captured_data) > 5:
                        print(f"  ... 还有 {len(captured_data) - 5} 条记录")

                    # 分析搜索结果
                    print(f"\n[INFO] 正在分析搜索结果位置...")
                    found_urls = []
                    for item in captured_data:
                        data = item.get('data', '')
                        # 查找包含 URL 的 chunk
                        if 'https://' in data or 'http://' in data:
                            # 尝试提取 URL
                            import re
                            urls = re.findall(r'https?://[^\s"\'<>\)]+', data)
                            if urls:
                                found_urls.append({
                                    'chunk': item.get('chunkNumber', '?'),
                                    'urls': urls,
                                    'data_preview': data[:200]
                                })

                    if found_urls:
                        print(f"[INFO] 🎯 找到 {len(found_urls)} 个包含 URL 的 chunk:")
                        for item in found_urls[:3]:  # 只显示前3个
                            print(f"  - Chunk #{item['chunk']}: {len(item['urls'])} 个 URL")
                            for url in item['urls'][:2]:  # 每个 chunk 只显示前2个 URL
                                print(f"    • {url[:80]}")
                    else:
                        print("[WARN] 未找到包含 URL 的数据")
                        print("[WARN] 可能因为：")
                        print("  1. 问题没有触发联网搜索")
                        print("  2. 搜索结果在其他字段中")
                        print("  3. 需要点击'参考 X 篇资料'才会加载")

                    # 保存完整数据的文本分析
                    complete_item = None
                    for item in captured_data:
                        if item.get('type') == 'SSE_COMPLETE':
                            complete_item = item
                            break

                    if complete_item:
                        full_data = complete_item.get('fullData', '')
                        # 检查关键字段
                        has_search = any(keyword in full_data for keyword in [
                            'web_search', 'search_result', 'reference', 'citation',
                            'search_info', 'search_item', 'search_content'
                        ])
                        if has_search:
                            print(f"\n[INFO] ✅ 在完整数据中检测到搜索相关字段！")
                            print(f"[INFO] 数据长度: {len(full_data)} 字符")
                        else:
                            print(f"\n[WARN] 完整数据中未找到明显的搜索字段")

                else:
                    print("[WARN] 未捕获到任何 XHR/Fetch 数据")
                    print("[INFO] 请确保你在浏览器中发送了消息")
            except Exception as e:
                print(f"[ERROR] 提取数据失败: {e}")

if __name__ == "__main__":
    main()
