"""一次性脚本：删除自己刚发的测试评论。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import Config
from src.browser import BrowserManager

TARGET = sys.argv[1] if len(sys.argv) > 1 else "这是一条测试评论，稍后删除"

FIND_COMMENT = """
(target) => {
  const leaf = [...document.querySelectorAll('*')].find(el =>
    el.children.length === 0 && (el.innerText || '').trim() === target);
  if (!leaf) return null;
  const r = leaf.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, visible: r.width > 0};
}
"""

DUMP_BUTTONS = """
(target) => {
  const leaf = [...document.querySelectorAll('*')].find(el =>
    el.children.length === 0 && (el.innerText || '').trim() === target);
  if (!leaf) return [];
  let node = leaf;
  const out = [];
  for (let i = 0; i < 10 && node; i++) {
    for (const b of node.querySelectorAll('button')) {
      const r = b.getBoundingClientRect();
      out.push({
        a: (b.getAttribute('aria-label') || '').slice(0, 40),
        t: (b.innerText || '').trim().slice(0, 20),
        vis: r.width > 0 && r.height > 0,
        x: Math.round(r.left + r.width / 2),
        y: Math.round(r.top + r.height / 2),
      });
    }
    node = node.parentElement;
  }
  return out.filter((v, i, arr) => arr.findIndex(z => z.a === v.a && z.t === v.t && z.vis === v.vis) === i);
}
"""

FIND_TEXT_BTN = """
(texts) => {
  for (const b of document.querySelectorAll('button, [role="menuitem"], div[role="button"]')) {
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

with BrowserManager(Config.load()) as browser:
    page = browser.page(
        "linkedin", "https://www.linkedin.com/in/me/recent-activity/comments/"
    )
    page.wait_for_timeout(6000)

    found = page.evaluate(FIND_COMMENT, TARGET)
    if not found:
        print("未找到目标评论，可能未发布成功")
        page.screenshot(path="data/dc_notfound.png")
        raise SystemExit(1)

    print("1) 评论位置:", found)
    page.screenshot(path="data/dc_found.png")

    # 悬停让操作菜单出现
    page.mouse.move(found["x"], found["y"])
    page.wait_for_timeout(1200)
    btns = page.evaluate(DUMP_BUTTONS, TARGET)
    print("2) 附近的按钮:")
    for b in btns:
        if b["vis"]:
            print("   ", json.dumps(b, ensure_ascii=False))

    menu = next(
        (b for b in btns if b["vis"] and ("菜单" in b["a"] or "更多" in b["a"]
         or "Options" in b["a"] or "More" in b["a"])),
        None,
    )
    print("3) 操作菜单按钮:", menu)
    if not menu:
        page.screenshot(path="data/dc_nomenu.png")
        raise SystemExit(1)

    page.mouse.click(menu["x"], menu["y"])
    page.wait_for_timeout(1500)
    page.screenshot(path="data/dc_menu.png")

    item = page.evaluate(FIND_TEXT_BTN, ["删除", "删除评论", "Delete", "Delete comment"])
    print("4) 删除项:", item)
    if not item:
        page.screenshot(path="data/dc_noitem.png")
        raise SystemExit(1)
    page.mouse.click(item["x"], item["y"])
    page.wait_for_timeout(1500)
    page.screenshot(path="data/dc_confirm.png")

    confirm = page.evaluate(FIND_TEXT_BTN, ["删除", "Delete"])
    if confirm:
        print("5) 确认删除:", confirm)
        page.mouse.click(confirm["x"], confirm["y"])
        page.wait_for_timeout(2500)

    # 校验必须回到"我的评论"页，信息流页看不到自己的旧评论会造成假阴性/假阳性
    page.goto(
        "https://www.linkedin.com/in/me/recent-activity/comments/",
        wait_until="domcontentloaded",
    )
    page.wait_for_timeout(5000)
    gone = TARGET not in page.locator("body").inner_text()
    print("6) 评论区已不含该文字:", gone)
    page.screenshot(path="data/dc_final.png")
