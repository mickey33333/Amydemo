from __future__ import annotations

import csv
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

START_URL = "https://www.fengm.cn/company/index"
OUTPUT_ROOT = Path("output")
CITY_ROOT = OUTPUT_ROOT / "无锡"
DEBUG_ROOT = OUTPUT_ROOT / "debug"

INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
COMPANY_HINTS = (
    "有限公司",
    "股份公司",
    "股份有限公司",
    "集团",
    "公司",
    "研究院",
    "研究所",
    "中心",
    "科技",
    "电子",
    "智能",
    "设备",
    "制造",
    "工厂",
    "厂",
)


def log(message: str) -> None:
    print(message, flush=True)


def safe_name(value: str, fallback: str = "未命名企业") -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = INVALID_FILENAME.sub("_", value).strip(" .")
    return value[:120] or fallback


def pick_company_name(text: str) -> str:
    lines = [re.sub(r"\s+", " ", x).strip() for x in (text or "").splitlines()]
    lines = [x for x in lines if 2 <= len(x) <= 100]
    if not lines:
        return ""

    ignored = {
        "企业",
        "企业列表",
        "企业详情",
        "查看详情",
        "详情",
        "收藏关注",
        "产品",
        "首页",
        "详细信息",
    }
    candidates = [x for x in lines if x not in ignored]
    for line in candidates:
        if any(hint in line for hint in COMPANY_HINTS):
            return line
    return candidates[0] if candidates else ""


def text_is_visible(page: Page, text: str) -> bool:
    loc = page.get_by_text(text, exact=True)
    for index in range(loc.count()):
        try:
            if loc.nth(index).is_visible():
                return True
        except Exception:
            pass
    return False


def click_exact_text(page: Page, text: str, prefer_last: bool = True, timeout_ms: int = 8000) -> bool:
    loc = page.get_by_text(text, exact=True)
    indices = list(range(loc.count()))
    if prefer_last:
        indices.reverse()
    for index in indices:
        item = loc.nth(index)
        try:
            if not item.is_visible():
                continue
            item.scroll_into_view_if_needed(timeout=timeout_ms)
            item.click(timeout=timeout_ms)
            log(f"已点击：{text}（第 {index + 1} 个可匹配元素）")
            return True
        except Exception as exc:
            log(f"点击 {text} 的候选元素失败：{exc}")
    return False


def wait_for_text(page: Page, text: str, timeout_seconds: float = 20.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if text_is_visible(page, text):
            return True
        page.wait_for_timeout(500)
    return False


def recursive_company_records(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        key_map = {str(k).lower(): k for k in value.keys()}
        name_key = None
        for candidate in ("companyname", "company_name", "enterprisename", "enterprise_name"):
            if candidate in key_map:
                name_key = key_map[candidate]
                break
        if name_key is not None and isinstance(value.get(name_key), str):
            name = value[name_key].strip()
            if name:
                found.append(value)
        for child in value.values():
            found.extend(recursive_company_records(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(recursive_company_records(child))
    return found


def extract_record_name(record: dict[str, Any]) -> str:
    for key in record:
        if str(key).lower() in {"companyname", "company_name", "enterprisename", "enterprise_name"}:
            value = record.get(key)
            if isinstance(value, str):
                return value.strip()
    return ""


def extract_record_id(record: dict[str, Any]) -> str:
    preferred = (
        "companyId",
        "company_id",
        "enterpriseId",
        "enterprise_id",
        "id",
        "objectId",
    )
    for wanted in preferred:
        for key, value in record.items():
            if str(key).lower() == wanted.lower() and value is not None:
                return str(value)
    return ""


def extract_dom_company_links(page: Page) -> list[dict[str, str]]:
    anchors = page.locator("a[href]").evaluate_all(
        """
        anchors => anchors.map(a => {
          const card = a.closest('li, article, [class*=company], [class*=enterprise], [class*=card], [class*=item]');
          return {
            href: a.href || a.getAttribute('href') || '',
            text: (a.innerText || a.textContent || '').trim(),
            context: card ? (card.innerText || card.textContent || '').trim() : ''
          };
        })
        """
    )

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in anchors:
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        full_url = urljoin(page.url, href)
        parsed = urlparse(full_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if "fengm.cn" not in parsed.netloc.lower():
            continue
        path_lower = parsed.path.lower().rstrip("/")
        if "/company/" not in path_lower:
            continue
        if path_lower in {"/company", "/company/index"} or path_lower.endswith("/company/index"):
            continue
        if full_url in seen:
            continue
        name = pick_company_name(str(anchor.get("text") or ""))
        if not name:
            name = pick_company_name(str(anchor.get("context") or ""))
        results.append({"name": name, "url": full_url})
        seen.add(full_url)
    return results


def next_page_button(page: Page):
    selectors = [
        ".el-pagination .btn-next",
        ".el-pager + button.btn-next",
        ".ant-pagination-next button",
        ".ant-pagination-next a",
        "button:has-text('下一页')",
        "a:has-text('下一页')",
        "button[aria-label='next page']",
        "button[aria-label='Next Page']",
        "a[aria-label='next page']",
    ]
    for selector in selectors:
        loc = page.locator(selector)
        for index in range(loc.count()):
            item = loc.nth(index)
            try:
                if not item.is_visible():
                    continue
                disabled = item.is_disabled() if item.evaluate("el => 'disabled' in el") else False
                cls = (item.get_attribute("class") or "").lower()
                aria_disabled = (item.get_attribute("aria-disabled") or "").lower() == "true"
                parent_cls = ""
                try:
                    parent_cls = item.locator("xpath=..").get_attribute("class") or ""
                except Exception:
                    pass
                if disabled or aria_disabled or "disabled" in cls or "disabled" in parent_cls.lower():
                    return None
                return item
            except Exception:
                continue
    return None


def body_fingerprint(page: Page) -> str:
    try:
        text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        text = page.content()
    text = re.sub(r"\s+", " ", text)
    return text[-5000:]


def auto_scroll(page: Page) -> None:
    page.evaluate(
        """
        async () => {
          await new Promise(resolve => {
            let total = 0;
            const step = 700;
            const timer = setInterval(() => {
              const height = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
              window.scrollBy(0, step);
              total += step;
              if (total >= height + 1000) {
                clearInterval(timer);
                resolve();
              }
            }, 120);
          });
        }
        """
    )
    page.wait_for_timeout(1000)
    try:
        page.evaluate(
            """
            async () => {
              const images = Array.from(document.images);
              await Promise.all(images.map(img => {
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
    page.wait_for_timeout(800)


def detail_page_name(page: Page, expected: str) -> str:
    selectors = [
        "h1",
        "h2",
        ".company-name",
        ".enterprise-name",
        "[class*=companyName]",
        "[class*=company-name]",
        "[class*=enterpriseName]",
        "[class*=enterprise-name]",
    ]
    for selector in selectors:
        loc = page.locator(selector)
        for index in range(min(loc.count(), 20)):
            item = loc.nth(index)
            try:
                if not item.is_visible():
                    continue
                candidate = pick_company_name(item.inner_text(timeout=3000))
                if candidate and len(candidate) >= 3:
                    return candidate
            except Exception:
                pass

    try:
        candidate = pick_company_name(page.locator("body").inner_text(timeout=5000))
        if candidate:
            return candidate
    except Exception:
        pass
    return expected


def screenshot_company(context: BrowserContext, company: dict[str, str], used_names: set[str], index: int, total: int) -> dict[str, str]:
    expected_name = company.get("name", "")
    url = company["url"]
    page = context.new_page()
    page.set_default_timeout(15000)
    page.set_default_navigation_timeout(90000)
    try:
        log(f"[{index}/{total}] 打开企业详情：{expected_name or url}")
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3500)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            pass
        auto_scroll(page)
        final_name = safe_name(detail_page_name(page, expected_name), fallback=f"企业_{index:03d}")
        unique_name = final_name
        suffix = 2
        while unique_name in used_names:
            unique_name = safe_name(f"{final_name}_{suffix}")
            suffix += 1
        used_names.add(unique_name)

        folder = CITY_ROOT / unique_name
        folder.mkdir(parents=True, exist_ok=True)
        screenshot_path = folder / f"{unique_name}.png"
        page.screenshot(path=str(screenshot_path), full_page=True, animations="disabled", timeout=90000)
        (folder / "来源网址.txt").write_text(page.url + "\n", encoding="utf-8")
        log(f"[{index}/{total}] 已保存：{screenshot_path}")
        return {"name": unique_name, "url": page.url, "status": "成功", "error": ""}
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log(f"[{index}/{total}] 截图失败：{error}")
        try:
            fallback_name = safe_name(expected_name, fallback=f"企业_{index:03d}")
            failure_dir = DEBUG_ROOT / "failed"
            failure_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(failure_dir / f"{index:03d}_{fallback_name}.png"), full_page=True, timeout=30000)
            (failure_dir / f"{index:03d}_{fallback_name}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        return {"name": expected_name or f"企业_{index:03d}", "url": url, "status": "失败", "error": error}
    finally:
        page.close()


def main() -> int:
    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    CITY_ROOT.mkdir(parents=True, exist_ok=True)
    DEBUG_ROOT.mkdir(parents=True, exist_ok=True)

    captured_records: list[dict[str, Any]] = []
    captured_requests: list[dict[str, Any]] = []
    capture_enabled = {"value": False}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1200},
            device_scale_factor=1,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            ignore_https_errors=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()
        page.set_default_timeout(15000)
        page.set_default_navigation_timeout(90000)

        def handle_response(response) -> None:
            if not capture_enabled["value"]:
                return
            try:
                resource_type = response.request.resource_type
                if resource_type not in {"xhr", "fetch"}:
                    return
                content_type = (response.headers.get("content-type") or "").lower()
                entry: dict[str, Any] = {
                    "url": response.url,
                    "status": response.status,
                    "content_type": content_type,
                }
                captured_requests.append(entry)
                if "json" not in content_type:
                    return
                data = response.json()
                records = recursive_company_records(data)
                for record in records:
                    captured_records.append(record)
            except Exception:
                return

        page.on("response", handle_response)

        try:
            log(f"打开企业列表：{START_URL}")
            page.goto(START_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(6000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass

            page.screenshot(path=str(DEBUG_ROOT / "01_初始页面.png"), full_page=True)
            (DEBUG_ROOT / "01_初始页面.html").write_text(page.content(), encoding="utf-8")
            log(f"当前页面：{page.url}；标题：{page.title()}")

            if not text_is_visible(page, "江苏省"):
                # 尝试展开地区筛选。
                click_exact_text(page, "地区", prefer_last=True)
                page.wait_for_timeout(1000)
                if not text_is_visible(page, "江苏省"):
                    click_exact_text(page, "中国", prefer_last=True)
                    page.wait_for_timeout(1500)

            if not click_exact_text(page, "江苏省", prefer_last=True):
                raise RuntimeError("未找到可点击的“江苏省”筛选项")
            if not wait_for_text(page, "无锡市", timeout_seconds=20):
                raise RuntimeError("选择江苏省后未出现“无锡市”筛选项")

            capture_enabled["value"] = True
            if not click_exact_text(page, "无锡市", prefer_last=True):
                raise RuntimeError("未找到可点击的“无锡市”筛选项")
            page.wait_for_timeout(5000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass

            page.screenshot(path=str(DEBUG_ROOT / "02_无锡筛选结果.png"), full_page=True)
            (DEBUG_ROOT / "02_无锡筛选结果.html").write_text(page.content(), encoding="utf-8")

            all_companies: dict[str, dict[str, str]] = {}
            seen_fingerprints: set[str] = set()
            page_number = 1
            while page_number <= 200:
                page.wait_for_timeout(2500)
                links = extract_dom_company_links(page)
                log(f"列表第 {page_number} 页提取到 {len(links)} 个企业详情链接")
                for item in links:
                    all_companies.setdefault(item["url"], item)

                if page_number <= 5 or page_number % 10 == 0:
                    try:
                        page.screenshot(path=str(DEBUG_ROOT / f"列表页_{page_number:03d}.png"), full_page=True)
                    except Exception:
                        pass

                fingerprint = body_fingerprint(page)
                if fingerprint in seen_fingerprints:
                    log("检测到列表页面内容重复，停止翻页")
                    break
                seen_fingerprints.add(fingerprint)

                button = next_page_button(page)
                if button is None:
                    log("未找到可用的下一页按钮，列表翻页结束")
                    break
                try:
                    button.scroll_into_view_if_needed(timeout=5000)
                    button.click(timeout=10000)
                    page_number += 1
                    page.wait_for_timeout(3500)
                except Exception as exc:
                    log(f"点击下一页失败，停止翻页：{exc}")
                    break

            # 补充 API 返回的企业名称，便于诊断；链接仍以页面真实链接为准。
            api_company_summary: list[dict[str, str]] = []
            seen_api: set[tuple[str, str]] = set()
            for record in captured_records:
                name = extract_record_name(record)
                record_id = extract_record_id(record)
                key = (name, record_id)
                if name and key not in seen_api:
                    seen_api.add(key)
                    api_company_summary.append({"name": name, "id": record_id})

            (DEBUG_ROOT / "网络请求.json").write_text(
                json.dumps(captured_requests, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (DEBUG_ROOT / "接口企业记录.json").write_text(
                json.dumps(api_company_summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (DEBUG_ROOT / "页面企业链接.json").write_text(
                json.dumps(list(all_companies.values()), ensure_ascii=False, indent=2), encoding="utf-8"
            )

            companies = list(all_companies.values())
            log(f"最终共发现 {len(companies)} 个唯一企业详情链接；接口捕获 {len(api_company_summary)} 个企业记录")
            if not companies:
                raise RuntimeError("没有提取到任何企业详情链接，请查看 output/debug 中的页面和接口记录")

            results: list[dict[str, str]] = []
            used_names: set[str] = set()
            total = len(companies)
            for index, company in enumerate(companies, start=1):
                results.append(screenshot_company(context, company, used_names, index, total))

            with (CITY_ROOT / "企业清单.csv").open("w", newline="", encoding="utf-8-sig") as file:
                writer = csv.DictWriter(file, fieldnames=["序号", "企业名称", "详情网址", "截图状态", "失败原因"])
                writer.writeheader()
                for index, result in enumerate(results, start=1):
                    writer.writerow(
                        {
                            "序号": index,
                            "企业名称": result["name"],
                            "详情网址": result["url"],
                            "截图状态": result["status"],
                            "失败原因": result["error"],
                        }
                    )

            success_count = sum(1 for item in results if item["status"] == "成功")
            failure_count = len(results) - success_count
            summary = {
                "筛选地区": "中国 > 江苏省 > 无锡市",
                "发现企业数": len(companies),
                "截图成功数": success_count,
                "截图失败数": failure_count,
                "生成时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            (CITY_ROOT / "任务说明.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            archive = shutil.make_archive(str(OUTPUT_ROOT / "无锡"), "zip", root_dir=OUTPUT_ROOT, base_dir="无锡")
            log(f"已生成压缩包：{archive}")
            log(json.dumps(summary, ensure_ascii=False))
            browser.close()
            return 0 if success_count > 0 and failure_count == 0 else 2

        except Exception as exc:
            log(f"任务异常：{type(exc).__name__}: {exc}")
            try:
                page.screenshot(path=str(DEBUG_ROOT / "异常页面.png"), full_page=True, timeout=30000)
                (DEBUG_ROOT / "异常页面.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            try:
                (DEBUG_ROOT / "错误.txt").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            except Exception:
                pass
            browser.close()
            return 1


if __name__ == "__main__":
    sys.exit(main())
