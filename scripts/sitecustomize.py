"""Playwright patches used by the temporary Wuxi screenshot job."""

import re

try:
    from playwright.sync_api import Browser, Page

    _original_new_context = Browser.new_context
    _original_locator = Page.locator

    def _new_context_without_remote_fonts(self, *args, **kwargs):
        context = _original_new_context(self, *args, **kwargs)

        def _route_handler(route):
            if route.request.resource_type == "font":
                route.abort()
            else:
                route.continue_()

        context.route("**/*", _route_handler)
        return context

    def _locator_with_region_expansion(self, selector, *args, **kwargs):
        has_text = kwargs.get("has_text")
        pattern = getattr(has_text, "pattern", "")
        if selector == "div.option-label" and pattern == "^江苏省$":
            result = _original_locator(self, selector, *args, **kwargs)
            try:
                if result.count() == 0:
                    china = _original_locator(self, "div.option-label", has_text=re.compile(r"^中国$"))
                    for index in range(china.count()):
                        item = china.nth(index)
                        if item.is_visible():
                            item.click(timeout=8000)
                            self.wait_for_timeout(1200)
                            break
                    result = _original_locator(self, selector, *args, **kwargs)
            except Exception:
                pass
            return result
        return _original_locator(self, selector, *args, **kwargs)

    Browser.new_context = _new_context_without_remote_fonts
    Page.locator = _locator_with_region_expansion
except Exception:
    pass
