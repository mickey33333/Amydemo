from __future__ import annotations

import csv
import json
import re
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

START_URL = "https://www.fengm.cn/company/index"
OUTPUT_ROOT = Path("output")
CITY_ROOT = OUTPUT_ROOT / "无锡"
DEBUG_ROOT = OUTPUT_ROOT / "debug"
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def log(message: str) -> None:
    print(message, flush=True)


def safe_name(value: str, fallback: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = INVALID_FILENAME.sub("_", value).strip(" .")
    return value[:120] or fallback


def wait_network_quiet(page, timeout: int = 15000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        pass


def click_region_option(page, text: str) -> None:
    exact = re.compile(rf"^{re.escape(text)}$")
    candidates = [
        page.locator("div.option-label", has_text=exact),
        page.locator("div.option-item", has_text=exact),
        page.get_by_text(text, exact=True),
    ]
    for locator in candidates:
        for i in range(locator.count()):
            item = locator.nth(i)
            try:
                if item.is_visible():
                    item.scroll_into_view_if_needed(timeout=8000)
                    item.click(timeout=8000)
                    log(f"已点击地区：{text}")
                    return
            except Exception as exc:
                log(f"点击 {text} 候选项失败：{exc}")
    raise RuntimeError(f"未找到地区选项：{text}")


def wait_body_contains(page, text: str, timeout_seconds: float = 30) -> None:
    expected = re.sub(r"\s+", "", text)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            body = re.sub(r"\s+", "", page.locator("body").inner_text(timeout=5000))
            if expected in body:
                return
        except Exception:
            pass
        page.wait_for_timeout(500)
    raise RuntimeError(f"页面未出现：{text}")


def next_button(page):
    for selector in [
        ".ant-pagination-next button",
        ".ant-pagination-next a",
        ".el-pagination .btn-next",
        "button:has-text('下一页')",
        "a:has-text('下一页')",
    ]:
        locator = page.locator(selector)
        for i in range(locator.count()):
            item = locator.nth(i)
            try:
                if not item.is_visible():
                    continue
                cls = (item.get_attribute("class") or "").lower()
                parent_cls = (item.locator("xpath=..").get_attribute("class") or "").lower()
                disabled = False
                try:
                    disabled = item.is_disabled()
                except Exception:
                    pass
                if disabled or "disabled" in cls or "disabled" in parent_cls:
                    return None
                return item
            except Exception:
                continue
    return None


def collect_rows(page) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    rows = page.locator("tbody.ant-table-tbody tr[data-row-key]")
    for i in range(rows.count()):
        row = rows.nth(i)
        company_id = (row.get_attribute("data-row-key") or "").strip()
        title = row.locator("span.title")
        name = title.first.inner_text(timeout=5000).strip() if title.count() else row.locator("td").first.inner_text(timeout=5000).strip()
        cells = row.locator("td")
        city = cells.nth(1).inner_text(timeout=5000).strip() if cells.count() > 1 else ""
        if company_id and name:
            records.append({"id": company_id, "name": name, "city": city})
    return records


def auto_scroll(page) -> None:
    page.evaluate(
        """
        async () => {
          await new Promise(resolve => {
            let y = 0;
            const timer = setInterval(() => {
              const height = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
              window.scrollBy(0, 650);
              y += 650;
              if (y >= height + 800) {
                clearInterval(timer);
                resolve();
              }
            }, 100);
          });
        }
        """
    )
    page.wait_for_timeout(700)
    try:
        page.evaluate(
            """
            async () => {
              await Promise.all(Array.from(document.images).map(img => {
                if (img.complete) return Promise.resolve();
                return new Promise(resolve => {
                  img.addEventListener('load', resolve, {once:true});
                  img.addEventListener('error', resolve, {once:true});
                  setTimeout(resolve, 8000);
                });
              }));
            }
            """
        )
    except Exception:
        pass
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(1000)


def discover_detail_url(page, context, company: dict[str, str]) -> str:
    company_id = company["id"]
    row = page.locator(f'tbody.ant-table-tbody tr[data-row-key="{company_id}"]').first
    before_url = page.url
    before_pages = list(context.pages)

    for target in [row, row.locator("span.title").first]:
        try:
            target.scroll_into_view_if_needed(timeout=8000)
            target.click(timeout=10000)
        except Exception:
            continue
        deadline = time.time() + 20
        while time.time() < deadline:
            for candidate in context.pages:
                if candidate not in before_pages:
                    candidate.wait_for_timeout(1500)
                    url = candidate.url
                    try:
                        candidate.screenshot(path=str(DEBUG_ROOT / "03_详情网址识别.png"), full_page=True, timeout=30000)
                    except Exception:
                        pass
                    candidate.close()
                    if company_id in url:
                        return url
            if page.url != before_url:
                page.wait_for_timeout(1500)
                url = page.url
                try:
                    page.screenshot(path=str(DEBUG_ROOT / "03_详情网址识别.png"), full_page=True, timeout=30000)
                except Exception:
                    pass
                if company_id in url:
                    return url
                raise RuntimeError(f"详情网址未包含企业ID：{url}")
            page.wait_for_timeout(500)
    raise RuntimeError("点击企业行后未进入详情页")


def screenshot_company(context, company: dict[str, str], url: str, index: int, total: int, used_names: set[str]) -> dict[str, str]:
    base_name = safe_name(company["name"], f"企业_{index:03d}")
    name = base_name
    suffix = 2
    while name in used_names:
        name = safe_name(f"{base_name}_{suffix}", f"企业_{index:03d}_{suffix}")
        suffix += 1
    used_names.add(name)

    page = context.new_page()
    page.set_default_timeout(20000)
    page.set_default_navigation_timeout(90000)
    try:
        log(f"[{index}/{total}] 正在截图：{company['name']}")
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3500)
        wait_network_quiet(page)
        wait_body_contains(page, company["name"], 25)
        auto_scroll(page)

        folder = CITY_ROOT / name
        folder.mkdir(parents=True, exist_ok=True)
        screenshot_path = folder / f"{name}.png"
        page.screenshot(path=str(screenshot_path), full_page=True, animations="disabled", timeout=90000)
        (folder / "来源网址.txt").write_text(page.url + "\n", encoding="utf-8")
        return {"name": name, "url": page.url, "status": "成功", "error": ""}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log(f"[{index}/{total}] 截图失败：{error}")
        failed = DEBUG_ROOT / "failed"
        failed.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(failed / f"{index:03d}_{name}.png"), full_page=True, timeout=30000)
            (failed / f"{index:03d}_{name}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        return {"name": name, "url": url, "status": "失败", "error": error}
    finally:
        page.close()


def main() -> int:
    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    CITY_ROOT.mkdir(parents=True, exist_ok=True)
    DEBUG_ROOT.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1260, "height": 1100},
            device_scale_factor=1,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            ignore_https_errors=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()
        page.set_default_timeout(20000)
        page.set_default_navigation_timeout(90000)

        requests: list[str] = []
        page.on("response", lambda response: requests.append(response.url) if response.request.resource_type in {"xhr", "fetch"} else None)

        try:
            page.goto(START_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(5000)
            wait_network_quiet(page)
            page.screenshot(path=str(DEBUG_ROOT / "01_初始页面.png"), full_page=True)

            click_region_option(page, "江苏省")
            page.wait_for_timeout(3000)
            click_region_option(page, "无锡市")
            page.wait_for_timeout(4000)
            wait_network_quiet(page)
            wait_body_contains(page, "中国>江苏省>无锡市", 30)

            page.screenshot(path=str(DEBUG_ROOT / "02_无锡筛选结果.png"), full_page=True)
            (DEBUG_ROOT / "02_无锡筛选结果.html").write_text(page.content(), encoding="utf-8")

            companies_by_id: dict[str, dict[str, str]] = {}
            page_number = 1
            last_rows: list[dict[str, str]] = []
            while page_number <= 200:
                page.wait_for_timeout(1500)
                rows = collect_rows(page)
                last_rows = rows
                if not rows:
                    raise RuntimeError(f"第 {page_number} 页没有读取到企业")
                log(f"第 {page_number} 页：{len(rows)} 家企业")
                for company in rows:
                    if company["city"] != "无锡市":
                        raise RuntimeError(f"地区筛选未生效：{company['name']} - {company['city']}")
                    companies_by_id.setdefault(company["id"], company)

                button = next_button(page)
                if button is None:
                    break
                old_ids = [x["id"] for x in rows]
                button.scroll_into_view_if_needed(timeout=8000)
                button.click(timeout=10000)
                page_number += 1
                deadline = time.time() + 30
                while time.time() < deadline:
                    page.wait_for_timeout(500)
                    current = collect_rows(page)
                    if current and [x["id"] for x in current] != old_ids:
                        break
                else:
                    raise RuntimeError("翻页后列表内容未变化")

            companies = list(companies_by_id.values())
            if not companies:
                raise RuntimeError("无锡企业列表为空")
            log(f"无锡企业总数：{len(companies)}")
            (DEBUG_ROOT / "企业记录.json").write_text(json.dumps(companies, ensure_ascii=False, indent=2), encoding="utf-8")
            (DEBUG_ROOT / "网络请求.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2), encoding="utf-8")

            sample = last_rows[0]
            sample_url = discover_detail_url(page, context, sample)
            template = sample_url.replace(sample["id"], "{company_id}")
            log(f"详情网址模板：{template}")

            results: list[dict[str, str]] = []
            used_names: set[str] = set()
            for index, company in enumerate(companies, 1):
                url = template.format(company_id=company["id"])
                results.append(screenshot_company(context, company, url, index, len(companies), used_names))

            with (CITY_ROOT / "企业清单.csv").open("w", newline="", encoding="utf-8-sig") as file:
                writer = csv.DictWriter(file, fieldnames=["序号", "企业名称", "详情网址", "截图状态", "失败原因"])
                writer.writeheader()
                for index, result in enumerate(results, 1):
                    writer.writerow({"序号": index, "企业名称": result["name"], "详情网址": result["url"], "截图状态": result["status"], "失败原因": result["error"]})

            success = sum(1 for x in results if x["status"] == "成功")
            failed = len(results) - success
            summary = {"筛选地区": "中国 > 江苏省 > 无锡市", "发现企业数": len(companies), "截图成功数": success, "截图失败数": failed, "生成时间": time.strftime("%Y-%m-%d %H:%M:%S")}
            (CITY_ROOT / "任务说明.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            shutil.make_archive(str(OUTPUT_ROOT / "无锡"), "zip", root_dir=OUTPUT_ROOT, base_dir="无锡")
            log(json.dumps(summary, ensure_ascii=False))
            browser.close()
            return 0 if failed == 0 else 2
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log(error)
            (DEBUG_ROOT / "错误.txt").write_text(error + "\n", encoding="utf-8")
            try:
                page.screenshot(path=str(DEBUG_ROOT / "异常页面.png"), full_page=True, timeout=30000)
                (DEBUG_ROOT / "异常页面.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            browser.close()
            return 1


if __name__ == "__main__":
    sys.exit(main())
