"""Disable remote web-font requests for deterministic Playwright screenshots."""

try:
    from playwright.sync_api import Browser

    _original_new_context = Browser.new_context

    def _new_context_without_remote_fonts(self, *args, **kwargs):
        context = _original_new_context(self, *args, **kwargs)

        def _route_handler(route):
            if route.request.resource_type == "font":
                route.abort()
            else:
                route.continue_()

        context.route("**/*", _route_handler)
        return context

    Browser.new_context = _new_context_without_remote_fonts
except Exception:
    pass
