# Copyright (c) 2026, Administrator and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
import requests
import json

class WAKnowledgeBase(Document):
    def before_save(self):
        # Clean up HTML tags if user used rich text editor
        text_content = frappe.utils.strip_html(self.content or "")
        if not text_content.strip():
            return
            
        if self.is_new() or self.has_value_changed("content"):
            self.generate_embedding(text_content)
            
    def generate_embedding(self, text_content):
        # Fetch the designated embedding provider
        providers = frappe.get_all("WA LLM Provider", 
            filters={"is_active": 1, "is_embedding_provider": 1}, 
            fields=["name", "api_key", "base_url", "provider_type"])
            
        if not providers:
            # Fallback to any active OpenAI provider
            providers = frappe.get_all("WA LLM Provider", 
                filters={"is_active": 1, "provider_type": "OpenAI"},
                fields=["name", "api_key", "base_url", "provider_type"])
                
        if not providers:
            frappe.log_error("No OpenAI Embedding Provider Configured.", "Knowledge Base Vector Error")
            return
            
        prov = providers[0]
        # We explicitly use openai.com unless a base_url explicitly covers it natively
        url = "https://api.openai.com/v1/embeddings"
        
        # If they configure azure or custom endpoints
        if prov.base_url and "api.openai.com" not in prov.base_url:
            if prov.base_url.endswith("/"):
                url = prov.base_url + "embeddings"
            else:
                url = prov.base_url + "/embeddings"
                
        # Handle the get_password mechanism properly for Frappe API tokens
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
            if resp.status_code != 200:
                frappe.log_error(f"WA Knowledge Base Embedding Error: {resp.text}", "Embedding Failed")
                self.vector_embedding = ""
                return
                
            data = resp.json()
            self.vector_embedding = json.dumps(data["data"][0]["embedding"])
        except Exception as e:
            frappe.log_error(f"Embedding failed: {str(e)}", "Knowledge Base Vector Error")
            self.vector_embedding = ""
