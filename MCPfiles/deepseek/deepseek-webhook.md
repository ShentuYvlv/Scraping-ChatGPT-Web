// DeepSeek 深度解析 Hook - V4 (支持搜索结果 + BATCH + 复杂结构)
(() => {
    window.__deepseek_captured__ = [];
    
    // ==========================================
    // 🧠 核心解析器：专门处理 DeepSeek 复杂的协议
    // ==========================================
    function parseDeepSeekPacket(json) {
        const findings = []; // 收集本次包里所有的有价值信息

        // 内部递归函数：处理单层对象
        const extract = (item) => {
            if (!item) return;

            // 1. 处理 BATCH (批量操作)
            // 结构: { o: "BATCH", v: [ ... ] }
            if (item.o === 'BATCH' && Array.isArray(item.v)) {
                item.v.forEach(subItem => extract(subItem));
                return;
            }

            // 2. 处理搜索结果 (Search Results)
            // 特征: 路径通常包含 /results，且 v 是数组，数组里有 url 和 title
            if (Array.isArray(item.v) && item.v.length > 0 && item.v[0].url && item.v[0].title) {
                findings.push({ type: 'search', data: item.v });
                return;
            }

            // 3. 处理对象形式的内容 (Fragment Append)
            // 结构: v: [{ content: "我", type: "RESPONSE" }]
            if (Array.isArray(item.v)) {
                item.v.forEach(sub => {
                    if (sub.content && typeof sub.content === 'string') {
                        findings.push({ type: 'text', data: sub.content });
                    }
                });
                return;
            }

            // 4. 处理纯文本追加 (String Append)
            // 结构: { p: ".../content", v: "无法" } 或者 { v: "文字" }
            if (typeof item.v === 'string') {
                // 过滤掉非内容的系统状态文本
                const ignoreValues = ['FINISHED', 'WIP', 'SEARCH', 'DEFAULT', 'ok'];
                // 过滤掉路径结尾是 status, type, mode 的
                const ignorePaths = ['/status', '/type', '/conversation_mode', 'has_pending_fragment'];

                const isSystemStatus = ignoreValues.includes(item.v);
                const isMetaPath = item.p && ignorePaths.some(k => item.p.endsWith(k));

                if (!isSystemStatus && !isMetaPath) {
                    findings.push({ type: 'text', data: item.v });
                }
            }
        };

        extract(json);
        return findings;
    }

    console.log('[DS_HOOK] 🕵️‍♂️ 深度解析器 V4 已就绪 (含搜索抓取)...');

    // ==========================================
    // 📡 通用流读取器 (Fetch + XHR 共用)
    // ==========================================
    function processRawText(text, sourceName) {
        const lines = text.split('\n');
        let processedContent = '';

        for (const line of lines) {
            if (line.startsWith('data: ')) {
                const jsonStr = line.substring(6).trim();
                if (!jsonStr || jsonStr === '[DONE]') continue;

                try {
                    const json = JSON.parse(jsonStr);
                    const results = parseDeepSeekPacket(json);

                    results.forEach(res => {
                        if (res.type === 'text') {
                            // 打印文本
                            console.log(`%c[TEXT] ${res.data}`, 'color: #4CAF50;'); 
                            processedContent += res.data;
                        } else if (res.type === 'search') {
                            // 漂亮地打印搜索结果
                            console.group('🔍 [DeepSeek] 捕获到搜索结果:');
                            res.data.forEach((s, i) => {
                                console.log(`${i+1}. ${s.title}\n🔗 ${s.url}\n📝 ${s.snippet.substring(0, 50)}...`);
                            });
                            console.groupEnd();
                            
                            // 保存搜索结果
                            window.__deepseek_captured__.push({
                                type: 'search_results',
                                data: res.data,
                                time: new Date()
                            });
                        }
                    });

                } catch (e) {
                    // console.warn('JSON Parse Error', e);
                }
            }
        }
        return processedContent;
    }


    // ==========================================
    // 1. Fetch 拦截
    // ==========================================
    const originalFetch = window.fetch;
    window.fetch = async function(url, options = {}) {
        const fullUrl = url.toString();
        if (fullUrl.includes('/chat/completion')) {
            console.log('[DS_HOOK] 🟢 [Fetch] 链接捕获');
            try {
                const response = await originalFetch(url, options);
                const clone = response.clone();
                const reader = clone.body.getReader();
                const decoder = new TextDecoder();
                
                (async () => {
                    let buffer = '';
                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\n');
                        buffer = lines.pop(); // 保留未完成行
                        
                        // 重新组合完整的行传给解析器
                        processRawText(lines.map(l => l).join('\n'), 'Fetch');
                    }
                })();
                return response;
            } catch (e) { return originalFetch(url, options); }
        }
        return originalFetch(url, options);
    };

    // ==========================================
    // 2. XHR 拦截
    // ==========================================
    const originalXhrOpen = XMLHttpRequest.prototype.open;
    const originalXhrSend = XMLHttpRequest.prototype.send;
    
    XMLHttpRequest.prototype.open = function(method, url) {
        this._url = url;
        return originalXhrOpen.apply(this, arguments);
    };
    
    XMLHttpRequest.prototype.send = function(body) {
        if (this._url && this._url.includes('/chat/completion')) {
            console.log('[DS_HOOK] 🟢 [XHR] 链接捕获');
            let lastIndex = 0;
            
            this.addEventListener('progress', () => {
                const newText = this.responseText.substring(lastIndex);
                lastIndex = this.responseText.length;
                processRawText(newText, 'XHR');
            });
        }
        return originalXhrSend.apply(this, arguments);
    };

    console.log('[DS_HOOK] ✅ 启动成功！尝试触发联网搜索看看...');
})();