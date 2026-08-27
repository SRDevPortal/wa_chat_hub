from frappe import _

def get_data():
    return [
        {
            "module_name": "WA Chat Hub",
            "type": "module",
            "label": _("WA Chat Hub"),
            "color": "blue",
            "icon": "octicon octicon-comment-discussion",
            "description": _("Unified WhatsApp operations hub for ERPNext")
        }
    ]
