"""Make Playwright screenshots deterministic and pre-expand the China region."""

import re

try:
    from playwright.sync_api import Browser

    _original_new_context = Browser.new_context

    def _new_context_with_site_patches(self, *args, **kwargs):
        context = _original_new_context(self, *args, **kwargs)

        def _route_handler(route):
            if route.request.resource_type == "font":
                route.abort()
            else:
                route.continue_()

        def _prepare_page(page):
            def _expand_china_region():
                if "/company/index" not in page.url:
                    return
                try:
                    page.wait_for_timeout(1200)
                    exact = re.compile(r"^中国$")
                    locator = page.locator("div.option-label", has_text=exact)
                    for index in range(locator.count()):
                        item = locator.nth(index)
                        if item.is_visible():
                            item.click(timeout=8000)
                            break
                except Exception:
                    pass

            page.on("load", lambda: _expand_china_region())

        context.route("**/*", _route_handler)
        context.on("page", _prepare_page)
        return context

    Browser.new_context = _new_context_with_site_patches
except Exception:
    pass
