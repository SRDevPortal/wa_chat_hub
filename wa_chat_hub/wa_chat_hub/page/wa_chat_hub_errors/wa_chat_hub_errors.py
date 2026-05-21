import frappe


def get_context(context):
    context.no_cache = 1
    context.title = "WA Chat Hub Errors"
    return context
