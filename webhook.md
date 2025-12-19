🔧 升级 Hook - 读取 SSE 响应流                                           
                                                                           
  请在控制台执行这个升级版 Hook：                                          
                                                                           
  // 完整的 Fetch Hook - 读取 SSE 流                                       
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
                                  const urlRegex = /https?:\/\/[^\s"'<>\)]+/g;
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
      console.log('[HOOK_V2] Send a message and watch the console...');    
  })();                                                                    
                                                                           
  然后：                                                                   
  1. 在聊天框发送一个新问题（例如："今天天气怎么样"）                      
  2. 观察控制台输出（应该会看到大量的 [FETCH_HOOK] Chunk #X 消息）         
  3. 等待回答完成                                                          
  4. 执行 window.__captured_requests__.length 查看捕获了多少条数据         
  5. 执行 window.__captured_requests__[0] 查看第一条数据的内容             
                                                                           
  📊 预期结果                                                              
                                                                           
  你应该看到类似这样的输出：                                               
                                                                           
  [FETCH_HOOK] 🔴 Intercepted: POST /chat/completion?...                   
  [FETCH_HOOK] Response type: text/event-stream                            
  [FETCH_HOOK] 📡 Reading SSE stream...                                    
  [FETCH_HOOK] Chunk #1: data: {"content": "今", ...}                      
  [FETCH_HOOK] Chunk #2: data: {"content": "天", ...}                      
  [FETCH_HOOK] Chunk #3: data: {"web_search": [...], ...}                  
  [FETCH_HOOK] 🎯 Found 3 URLs: ["https://...", "https://...", ...]        
  [FETCH_HOOK] Chunk #4: ...                                               
  [FETCH_HOOK] ✅ Stream finished. Total chunks: 45                        
                                                                           
  **执行后告诉我看到了什么！**这样我们就能知道数据的确切格式了。    