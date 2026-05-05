import frappe
import json
import math
import requests

def get_query_embedding(query_text):
    text_content = frappe.utils.strip_html(query_text or "")
    if not text_content.strip(): 
        return None
        
    providers = frappe.get_all("WA LLM Provider", 
        filters={"is_active": 1, "is_embedding_provider": 1}, 
        fields=["name", "api_key", "base_url", "provider_type"])
        
    if not providers:
        providers = frappe.get_all("WA LLM Provider", 
            filters={"is_active": 1, "provider_type": "OpenAI"},
            fields=["name", "api_key", "base_url", "provider_type"])
            
    if not providers:
        return None
        
    prov = providers[0]
    url = "https://api.openai.com/v1/embeddings"
    if prov.base_url and "api.openai.com" not in prov.base_url:
        if prov.base_url.endswith("/"):
            url = prov.base_url + "embeddings"
        else:
            url = prov.base_url + "/embeddings"
            
    doc_prov = frappe.get_doc("WA LLM Provider", prov.name)
    api_key = doc_prov.get_password("api_key") or prov.api_key
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "input": text_content,
        "model": "text-embedding-ada-002"
    }
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception as e:
        frappe.log_error(f"Failed to vectorize search query: {str(e)}", "WA Vector Search Error")
        return None

def cosine_similarity(v1, v2):
    # Standard Python implementation (No heavy Numpy req). ~2 milliseconds for 1536 dim.
    if len(v1) != len(v2): return 0.0
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm_v1 = math.sqrt(sum(a * a for a in v1))
    norm_v2 = math.sqrt(sum(b * b for b in v2))
    if norm_v1 == 0 or norm_v2 == 0: return 0.0
    return dot_product / (norm_v1 * norm_v2)

def search_knowledge_base(query_text, top_k=2):
    query_vector = get_query_embedding(query_text)
    if not query_vector:
        return []
        
    records = frappe.get_all("WA Knowledge Base", 
        filters={"status": "Active"}, 
        fields=["name", "title", "content", "vector_embedding"]
    )
    
    results = []
    for r in records:
        if not r.vector_embedding:
            continue
        try:
            doc_vector = json.loads(r.vector_embedding)
            similarity = cosine_similarity(query_vector, doc_vector)
            
            # Require at least 0.70 vector match (semantic relation)
            if similarity > 0.70:
                results.append({
                    "title": r.title,
                    "content": frappe.utils.strip_html(r.content),
                    "score": similarity
                })
        except Exception:
            continue
            
    # Sort descending
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]
