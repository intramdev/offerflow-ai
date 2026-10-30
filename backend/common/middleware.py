"""Normalize API paths stripped of their trailing slash by proxies.

The frontend proxy forwards `/api/.../` without its trailing slash, and
Django cannot slash-redirect a POST (the body would be lost), so every API
write would 500. Re-append the slash before routing for API paths only;
page URLs keep standard APPEND_SLASH behavior. Harmless when the proxy
preserves slashes: already-slashed paths pass through untouched.
"""

from __future__ import annotations


class PreserveApiSlashMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        if path.startswith("/api/") and not path.endswith("/"):
            request.path_info = f"{path}/"
        return self.get_response(request)
