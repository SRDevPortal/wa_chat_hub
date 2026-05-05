import frappe
from frappe.utils.background_jobs import enqueue
import requests
import json
from wa_chat_hub.api.vector_search import search_knowledge_base

def on_message_received(doc, method):
    if doc.direction == 'Inbound':
        settings = frappe.get_single('WA Chat Hub Settings')
        if settings.enable_ai_autopilot:
            enqueue(
                "wa_chat_hub.api.ai_bot.process_message",
                queue="short",
                message_id=doc.name
            )

def process_message(message_id):
    msg_doc = frappe.get_doc("Chat Message", message_id)
    conversation = msg_doc.conversation
    
    # Get history
    history = frappe.get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=["direction", "body"],
        order_by="creation asc",
        limit=20
    )
    
    settings = frappe.get_single('WA Chat Hub Settings')
    system_prompt = settings.system_prompt or "You are a helpful AI assistant."
    
    last_user_query = ""
    for msg in reversed(history):
        if msg.direction == "Inbound" and str(msg.body).strip():
            last_user_query = str(msg.body)
            break
            
    if last_user_query:
        kb_results = search_knowledge_base(last_user_query, top_k=3)
        if kb_results:
            system_prompt += "\n\n# Relevant Knowledge Base Context\n"
            for kb in kb_results:
                system_prompt += f"--- {kb['title']} ---\n{kb['content']}\n\n"
            system_prompt += "Use the knowledge base context provided above to accurately assist the user."
    
    # Get Providers
    providers = frappe.get_all(
        "WA LLM Provider",
        filters={"is_active": 1},
        fields=["name", "provider_type", "model_name", "api_key", "base_url"],
        order_by="priority asc"
    )
    
    if not providers:
        frappe.log_error("No active WA LLM Providers found.", "WA AI Bot Error")
        return
        
    for provider in providers:
        try:
            response_text = call_provider(provider, system_prompt, history)
            if response_text:
                # Insert the reply!
                reply = frappe.new_doc("Chat Message")
                reply.conversation = conversation
                reply.direction = "Outbound"
                reply.content_type = "Text"
                reply.body = response_text
                reply.insert(ignore_permissions=True)
                
                # Broadcast
                frappe.publish_realtime("wa_chat_new_message", {"conversation": conversation, "message": reply.as_dict()})
                return # success
                
        except Exception as e:
            frappe.log_error(f"LLM Provider {provider.name} failed: {str(e)}", "WA AI Fallback Warning")
            continue
            
    frappe.log_error(f"All LLM Providers failed for conversation {conversation}.", "WA AI Fatal Error")

def call_provider(provider, system_prompt, history):
    # Standardize to OpenAI Chat Format
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        role = "user" if h.direction == "Inbound" else "assistant"
        
        # Don't append empty messages to avoid API errors
        if str(h.body).strip():
            messages.append({"role": role, "content": str(h.body)})
        
    if provider.provider_type == "OpenAI" or provider.provider_type == "Custom":
        return call_openai_format(provider, messages)
    elif provider.provider_type == "Gemini":
        # Gemini provides an official OpenAI Compatibility API
        if not provider.base_url:
            provider.base_url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        return call_openai_format(provider, messages)
    elif provider.provider_type == "Anthropic":
        raise Exception("Anthropic specific MCP format requires SDK. Please use OpenAI/Gemini/Custom.")
        
    raise Exception(f"Unsupported provider type {provider.provider_type}")

def fetch_mcp_tools():
    # Return available tools in standard OpenAI function calling format
    tools_docs = frappe.get_all("WA MCP Tool Endpoint", filters={"is_active": 1}, fields=["tool_name", "description", "parameters_schema", "endpoint_url", "http_method"])
    tools = []
    for t in tools_docs:
        try:
            params = json.loads(t.parameters_schema) if t.parameters_schema else {"type": "object", "properties": {}}
        except:
            params = {"type": "object", "properties": {}}
            
        tools.append({
            "type": "function",
            "function": {
                "name": t.tool_name,
                "description": t.description or "No description",
                "parameters": params
            },
            "_meta": {
                "url": t.endpoint_url,
                "method": t.http_method
            }
        })
    return tools

def execute_mcp_tool(tool_name, arguments_dict):
    tools = fetch_mcp_tools()
    tool_meta = next((t["_meta"] for t in tools if t["function"]["name"] == tool_name), None)
    if not tool_meta:
        return f"Error: Tool {tool_name} not found."
        
    try:
        url = tool_meta["url"]
        
        if url.startswith("http"):
            if tool_meta["method"] == "POST":
                resp = requests.post(url, json=arguments_dict, timeout=10)
            else:
                resp = requests.get(url, params=arguments_dict, timeout=10)
            return resp.text
        else:
            # Internal frappe dotted path like `erpnext.something.api.get`
            fn = frappe.get_attr(url)
            res = fn(**arguments_dict)
            return json.dumps(res)
    except Exception as e:
        return f"Error executing {tool_name}: {str(e)}"

def call_openai_format(provider, messages):
    url = provider.base_url or "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json"
    }
    
    # Strict API URL mapping helper
    if url.endswith("/") and "chat/completions" not in url:
        url += "chat/completions"
    elif not url.endswith("chat/completions"):
        # Usually it's expected to be the full endpoint but if they omit we force it safely if possible
        pass 
        
    tools = fetch_mcp_tools()
    api_tools = [{"type": t["type"], "function": t["function"]} for t in tools] if tools else None
    
    payload = {
        "model": provider.model_name,
        "messages": messages,
    }
    if api_tools:
        payload["tools"] = api_tools
        
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    
    if resp.status_code != 200:
        frappe.log_error(f"API Error {resp.status_code}: {resp.text}", "WA AI Provider API Failure")
        resp.raise_for_status()
        
    data = resp.json()
    message = data["choices"][0]["message"]
    
    # Handle Tool Call
    if message.get("tool_calls"):
        messages.append(message) # Keep AI's tool call invocation in history
        
        for tc in message["tool_calls"]:
            try:
                args = json.loads(tc["function"]["arguments"])
            except:
                args = {}
            tool_res = execute_mcp_tool(tc["function"]["name"], args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": tc["function"]["name"],
                "content": str(tool_res)
            })
        
        # Second call with the tool results context
        payload["messages"] = messages
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"].get("content", "")
        
    return message.get("content", "")
