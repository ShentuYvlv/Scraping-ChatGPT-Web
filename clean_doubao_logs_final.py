
import json
import os

def clean_text_final(text):
    if not text:
        return ""
    # 基础清理，去除转义符
    text = text.replace('\\n', '\n').replace('\\"', '"')
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

def process_doubao_logs_final(input_path, output_path):
    output_dir = os.path.dirname(output_path)
    if not os.path.exists(output_dir) and output_dir:
        os.makedirs(output_dir)

    success_count = 0
    
    with open(input_path, 'r', encoding='utf-8') as infile, open(output_path, 'w', encoding='utf-8') as outfile:
        for line in infile:
            try:
                log_entry = json.loads(line)
                raw_data = log_entry.get("full_raw_data", "")
                
                if not raw_data:
                    continue

                # 提取基础信息
                time_str = log_entry.get("time", "")
                prompt_str = log_entry.get("prompt", "")
                origin_url = log_entry.get("url", "")

                chunks = raw_data.split('\n')
                
                # 存储所有的文本片段
                reply_parts = []
                
                # 存储搜索相关信息
                all_search_queries = []
                all_references = []
                
                # 使用 set 来去重引用 (根据 URL)
                seen_urls = set()
                
                for chunk in chunks:
                    chunk = chunk.strip()
                    if chunk.startswith("data: "):
                        json_str = chunk[6:]
                        if json_str == "{}": continue
                        
                        try:
                            data_obj = json.loads(json_str)
                            
                            # 提取函数，用于处理任何发现的 content_list
                            def process_content_list(c_list):
                                local_reply_parts = []
                                local_queries = []
                                local_refs = []
                                
                                if isinstance(c_list, list):
                                    # 1. 提取文本
                                    for item in c_list:
                                        # 忽略 loading_block
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

                            # Case A: message -> content (初始帧或完整帧)
                            if "message" in data_obj and "content" in data_obj["message"]:
                                try:
                                    content_list = json.loads(data_obj["message"]["content"])
                                    r_parts, qs, refs = process_content_list(content_list)
                                    # 注意：message 里的 content 通常包含开头，如果是 Prompt 重复，保留它，最后由用户决定是否使用
                                    reply_parts.extend(r_parts)
                                    all_search_queries.extend(qs)
                                    all_references.extend(refs)
                                except: pass
                            
                            # Case B: patch_op -> patch_value -> content_block (增量更新)
                            if "patch_op" in data_obj:
                                for op in data_obj["patch_op"]:
                                    if "patch_value" in op:
                                        val = op["patch_value"]
                                        
                                        # 检查 content_block
                                        if "content_block" in val:
                                            r_parts, qs, refs = process_content_list(val["content_block"])
                                            reply_parts.extend(r_parts)
                                            all_search_queries.extend(qs)
                                            all_references.extend(refs)

                        except:
                            pass
                
                # 拼接所有回复片段
                full_reply = "".join(reply_parts)
                full_reply = clean_text_final(full_reply)
                
                # 去重 References
                unique_references = []
                for ref in all_references:
                    if ref["url"] and ref["url"] not in seen_urls:
                        seen_urls.add(ref["url"])
                        unique_references.append(ref)
                    elif not ref["url"] and ref["title"]: # 没有URL但有标题的情况
                         # 简单防止完全重复的标题
                         is_dup = False
                         for existing in unique_references:
                             if existing["title"] == ref["title"]:
                                 is_dup = True
                                 break
                         if not is_dup:
                             unique_references.append(ref)

                # 去重 Queries
                unique_queries = list(dict.fromkeys(all_search_queries))
                
                if full_reply or unique_references:
                    cleaned_entry = {
                        "time": time_str,
                        "prompt": prompt_str,
                        "url": origin_url,  # 原始请求的 URL
                        "reply": full_reply,
                        "search_queries": unique_queries,
                        "references": unique_references
                    }
                    outfile.write(json.dumps(cleaned_entry, ensure_ascii=False) + '\n')
                    success_count += 1

            except Exception as e:
                # print(f"Error processing line: {e}")
                continue
                
    print(f"处理完成，共成功提取 {success_count} 条有效对话。")

if __name__ == "__main__":
    input_file = os.path.join("MCPfiles", "doubao_data", "all_logs.jsonl")
    output_file = os.path.join("MCPfiles", "doubao_data", "cleaned_logs_final.jsonl")
    process_doubao_logs_final(input_file, output_file)
