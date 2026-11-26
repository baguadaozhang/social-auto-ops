"""一次性脚本：删除领英上指定文案的动态。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import Config
from src.browser import BrowserManager

TARGET = "测试内容，稍后删除。"
URL = "https://www.linkedin.com/in/me/recent-activity/all/"

FIND_MENU_JS = """
(target) => {
  const leaf = [...document.querySelectorAll('*')].find(el =>
    el.children.length === 0 && (el.innerText || '').trim() === target);
  if (!leaf) return null;
  let node = leaf;
  for (let i = 0; i < 14 && node; i++) {
    for (const b of node.querySelectorAll('button')) {
      const label = ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).trim();
      if (/控制菜单|更多操作|更多|Open control menu|More actions|More/i.test(label)) {
        const r = b.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
          return {x: r.left + r.width / 2, y: r.top + r.height / 2, label: label};
        }
      }
    }
    node = node.parentElement;
  }
  return null;
}
"""

FIND_TEXT_BTN_JS = """
(texts) => {
  for (const b of document.querySelectorAll('button, [role="menuitem"], div[role="button"], a')) {
    const t = (b.innerText || '').trim();
    if (!texts.includes(t)) continue;
    const r = b.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) {
      return {x: r.left + r.width / 2, y: r.top + r.height / 2, text: t};
    }
  }
  return null;
}
"""


def main() -> int:
    config = Config.load()
    with BrowserManager(config) as browser:
        page = browser.page("linkedin", URL)
        page.wait_for_timeout(6000)
        print("1) 打开动态页:", page.url[:80])

        body = page.locator("body").inner_text()
        if TARGET not in body:
            print("   页面上没找到目标动态，可能已删除")
            return 0

        point = page.evaluate(FIND_MENU_JS, TARGET)
        print("2) 控制菜单按钮:", point)
        if not point:
            page.screenshot(path="data/del_step1_nomenu.png")
            print("   未找到菜单按钮，已截图")
            return 1
        page.mouse.click(point["x"], point["y"])
        page.wait_for_timeout(1800)
        page.screenshot(path="data/del_step2_menu.png")

        item = page.evaluate(FIND_TEXT_BTN_JS, ["删除动态", "删除帖子", "Delete post", "Delete"])
        print("3) 删除菜单项:", item)
        if not item:
            page.screenshot(path="data/del_step3_noitem.png")
            print("   未找到删除项，已截图")
            return 1
        page.mouse.click(item["x"], item["y"])
        page.wait_for_timeout(1800)
        page.screenshot(path="data/del_step3_confirm.png")

        confirm = page.evaluate(FIND_TEXT_BTN_JS, ["删除", "Delete"])
        print("4) 确认按钮:", confirm)
        if confirm:
            page.mouse.click(confirm["x"], confirm["y"])
            page.wait_for_timeout(3000)
        else:
            print("   未出现确认弹窗（可能直接删除）")

        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        gone = TARGET not in page.locator("body").inner_text()
        page.screenshot(path="data/del_step4_final.png")
        print("5) 删除后动态页不再包含该文案:", gone)
        return 0 if gone else 1


if __name__ == "__main__":
    raise SystemExit(main())
