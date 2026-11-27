"""评论任务编排：选目标、读正文、挑匹配的话术、控节奏。

P0 改动：
- 话术库支持话题标签 [supply,quality] 前缀
- 评论前先读帖子正文，按话题匹配话术，避免文不对题
- 文案被复用时自动加随机前后缀，降低重复特征
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from ..browser import BrowserManager
from ..database import Database
from ..human_text import _is_chinese, add_imperfection
from ..humanize import Humanizer
from ..platforms.base import BasePlatform
from ..platforms.linkedin import LinkedInPlatform
from ..risk_control import (
    AccountCoolingDown,
    CircuitBreakerTripped,
    QuotaExceeded,
    RiskController,
    TimeWindowClosed,
)

# 话题关键词 → 标签。中英双语，命中越多匹配度越高。
# 英文关键词必须整词匹配（见 _keyword_hits），否则 "AI" 会命中 available / said
TAG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "supply": (
        "供应链", "物流", "运输", "仓储", "货代", "清关", "海运", "库存",
        "supply chain", "logistics", "freight", "shipping", "warehouse",
        "inventory", "customs", "container",
    ),
    "quality": (
        "质量", "品控", "质检", "认证", "标准", "不良率", "返工", "检验",
        "quality", "defect", "inspection", "certification", "rework",
        "tolerance", "compliance",
    ),
    "cost": (
        "成本", "价格", "报价", "利润", "毛利", "降本", "费用", "预算",
        "cost", "price", "pricing", "margin", "profit", "quotation", "quote",
    ),
    "delivery": (
        "交期", "交付", "周期", "延期", "准时", "排产", "工期",
        "lead time", "delivery", "on time", "backlog", "delay",
    ),
    "market": (
        "市场", "客户", "订单", "展会", "出口", "买家", "需求", "渠道",
        "market", "customer", "trade show", "export", "buyer", "demand",
        "distributor",
    ),
    "production": (
        "生产", "产能", "工厂", "产线", "工艺", "模具", "车间", "制造",
        "production", "capacity", "factory", "tooling", "mold",
        "manufacturing", "yield", "changeover",
    ),
    "digital": (
        "数字化", "自动化", "系统", "数据", "AI", "智能", "算法",
        "digital", "automation", "system", "data", "software", "dashboard", "ERP",
    ),
}

def keyword_hits(body: str, keyword: str) -> int:
    """中文按子串匹配；英文按整词匹配，避免 AI 命中 available 这类误判。"""
    if keyword.isascii():
        return len(re.findall(rf"\b{re.escape(keyword)}\b", body, re.IGNORECASE))
    return body.count(keyword)


def keyword_weight(keyword: str) -> int:
    """越具体的词越能代表话题，给更高权重。

    英文短语(supply chain) > 单词(logistics)；中文长词(供应链) > 短词(物流)。
    避免几个弱词打平导致随机选题。
    """
    if keyword.isascii():
        return 2 if " " in keyword else 1
    return 2 if len(keyword) >= 3 else 1


def tag_scores(text: str) -> dict[str, int]:
    """每个话题的加权得分，用于判断帖子主要在讲什么。

    按命中次数加权（截断到 3 次）：整篇都在讲海运的帖子应该比
    一句带过海运的帖子得分高，但单关键词堆 10 次不应无限加分。
    """
    body = text or ""
    scores: dict[str, int] = {}
    for tag, keywords in TAG_KEYWORDS.items():
        score = sum(
            keyword_weight(k) * min(keyword_hits(body, k), 3) for k in keywords
        )
        if score:
            scores[tag] = score
    return scores


def tags_in(text: str) -> set[str]:
    return set(tag_scores(text))


# 复用时才加的随机前后缀，避免同一句话一字不差地重复出现。
# 按语言分开：英文评论绝不能出现中文前后缀（"同意，Great point…" 一眼假）
_OPENERS_ZH = ("", "", "同意，", "说得好，", "这个角度有意思，", "有同感，")
_CLOSERS_ZH = ("", "", "学习了。", "感谢分享。", "受教了。")
_OPENERS_EN = ("", "", "Agreed. ", "Well said. ", "Interesting angle. ", "Good point. ")
_CLOSERS_EN = ("", "", " Thanks for sharing.", " Good read.", " Worth a read.")

_TAG_LINE = re.compile(r"^\[([^\]]+)\]\s*(.+)$")

# 招聘、求职、纯通知类帖子不评论——在招聘帖下面谈供应链会很怪。
# 注意必须用招聘语境的锚定短语，不能用裸子串：
# "looking for a" 会误杀买家帖（"looking for a sanitary ware supplier" 正是目标客户），
# "resume" 会命中 "production will resume after CNY"，
# "cv"/"vacancy" 会命中 URL 片段和酒店房态讨论。
_SKIP_MARKERS: tuple[str, ...] = (
    "we're hiring", "we are hiring", "is hiring", "now hiring", "join our team",
    "looking to hire", "looking for candidates", "looking for a job",
    "job opening", "open position", "apply now", "apply here",
    "job opportunity", "#hiring", "#jobs",
    "招聘", "求职", "简历", "岗位", "面试",
)


@dataclass
class Template:
    text: str
    tags: tuple[str, ...] = field(default_factory=tuple)
    lang: str = "zh"


@dataclass
class Pick:
    """选中的话术。

    text 是最终要发出去的（可能加了变体/错字），
    base 是话术库里的原句——去重必须比对 base，否则加了变体就永远"算新的"。
    """

    text: str
    base: str


class Commenter:
    """评论任务处理器。第一版只支持领英。"""

    def __init__(
        self,
        browser: BrowserManager,
        humanizer: Humanizer,
        risk: RiskController,
        db: Database,
        config: Any,
    ):
        self.browser = browser
        self.humanizer = humanizer
        self.risk = risk
        self.db = db
        self.config = config
        self.rng = getattr(humanizer, "rng", random)

    # ---------- 话术库 ----------
    # 中文话术、英文话术分文件存放，加载时打上语言标记
    TEMPLATE_FILES: tuple[tuple[str, str], ...] = (
        ("comments_en.txt", "en"),
        ("comments.txt", "zh"),
    )

    def templates(self, lang: str | None = None) -> list[Template]:
        folder = self.config.path("storage.content_dir", "./content")
        out: list[Template] = []
        for filename, file_lang in self.TEMPLATE_FILES:
            if lang and lang != file_lang:
                continue
            path = folder / filename
            if not path.exists():
                continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                match = _TAG_LINE.match(line)
                if match:
                    tags = tuple(
                        t.strip() for t in match.group(1).split(",") if t.strip()
                    )
                    out.append(
                        Template(text=match.group(2).strip(), tags=tags, lang=file_lang)
                    )
                else:
                    out.append(Template(text=line, lang=file_lang))
        return out

    @staticmethod
    def _keyword_hits(body: str, keyword: str) -> int:
        return keyword_hits(body, keyword)

    @staticmethod
    def _keyword_weight(keyword: str) -> int:
        return keyword_weight(keyword)

    def _tag_scores(self, text: str) -> dict[str, int]:
        # 逻辑放在模块级函数里，follower（关注任务）也要用同一套话题判定
        return tag_scores(text)

    def _tags_in(self, text: str) -> set[str]:
        return tags_in(text)

    def is_discussable(self, post_text: str) -> bool:
        """判断这条帖子值不值得评论。

        三道筛：
        1. 太短（没实质内容）
        2. 招聘 / 求职 / 简历类（在招聘帖下谈供应链很怪）
        3. 跟自己的行业话题完全无关（可配置）
        """
        body = (post_text or "").strip()
        if len(body) < 15:
            return False
        lowered = body.lower()
        if any(marker in lowered for marker in _SKIP_MARKERS):
            return False
        if self.config.get("comment.require_topic_match", True):
            if not self._tag_scores(body):
                return False
        return True

    def _vary(self, text: str, lang: str = "zh") -> str:
        """给复用的文案加随机前后缀。前后缀必须与文案同语言。"""
        if lang == "en":
            opener = self.rng.choice(_OPENERS_EN)
            closer = self.rng.choice(_CLOSERS_EN)
            if opener and opener.strip(" .,").lower() in text[:24].lower():
                opener = ""
            result = f"{opener}{text}"
            if closer and not result.endswith((".", "?", "!")):
                result += "."
            if closer:
                result += closer
            return result
        opener = self.rng.choice(_OPENERS_ZH)
        closer = self.rng.choice(_CLOSERS_ZH)
        if opener and opener.rstrip("，") in text[:8]:
            opener = ""
        result = f"{opener}{text}"
        if closer and not result.endswith(("。", "？", "！", "?")):
            result += "。"
        if closer:
            result += closer
        return result

    def pick_for(
        self,
        post_text: str,
        used: set[str],
        allow_reuse: bool = True,
    ) -> "Pick | None":
        """按帖子正文的话题挑一条话术，优先没用过的。

        评论语言跟随帖子语言：英文帖用英文话术，中文帖用中文话术。
        """
        all_templates = self.templates()
        if not all_templates:
            return None

        # 正文为空时用主语言（默认英文，面向海外客户）
        if not (post_text or "").strip():
            lang = str(self.config.get("comment.primary_language", "en"))
        else:
            lang = "zh" if _is_chinese(post_text) else "en"
        pool = [t for t in all_templates if t.lang == lang] or all_templates

        # 按命中数量加权，取最强话题；避免"海运涨价"被当成纯成本话题
        scores = self._tag_scores(post_text)
        matched: list[Template] = []
        if scores:
            top_score = max(scores.values())
            top_tags = {tag for tag, score in scores.items() if score == top_score}
            matched = [t for t in pool if set(t.tags) & top_tags]
            if not matched:  # 最强话题没有对应话术，退而求其次
                matched = [t for t in pool if set(t.tags) & set(scores)]
        matched_fresh = [t for t in matched if t.text not in used]
        general = [t for t in pool if not t.tags]
        general_fresh = [t for t in general if t.text not in used]

        # 优先级：话题匹配且没用过 > 话题匹配 > 通用且没用过 > 通用 > 任意
        for candidates, is_reuse in (
            (matched_fresh, False),
            (matched, True),
            (general_fresh, False),
            (general, True),
            (pool, True),
        ):
            if not candidates:
                continue
            if is_reuse and not allow_reuse:
                continue
            chosen = self.rng.choice(candidates)
            text = self._vary(chosen.text, chosen.lang) if is_reuse else chosen.text
            return Pick(text=text, base=chosen.text)
        return None

    def pick_texts(self, count: int, used: set[str]) -> list[Pick]:
        """不依赖帖子内容的批量挑选（兼容旧用法）。"""
        picked: list[Pick] = []
        local_used = set(used)
        for _ in range(count):
            pick = self.pick_for("", local_used)
            if pick is None:
                break
            local_used.add(pick.base)
            picked.append(pick)
        return picked

    # ---------- 目标页 ----------
    def target_url(self, platform: str, keyword: str | None, url: str | None) -> str:
        if url:
            return url
        if keyword:
            base = self.config.get(f"platforms.{platform}.base_url", "")
            return f"{base}/search/results/content/?keywords={quote(keyword)}"
        return self.config.get(f"platforms.{platform}.feed_url", "")

    def platform(self, name: str) -> BasePlatform:
        if name != "linkedin":
            raise ValueError(f"评论模块暂只支持 linkedin，收到 {name}")
        return LinkedInPlatform(
            self.browser, self.humanizer, self.risk, self.db, self.config
        )

    def _divert_to_human(
        self, platform: str, url: str, text: str, reason: str, post_url: str = ""
    ) -> None:
        """转人工：同时建一条 comment 任务，approve 后机器能重新入队执行。

        只建待办不建任务的话，approve 会静默丢掉这条评论（没有 task_id 可回队）。
        post_url 是具体帖子的链接，方便人工直接去那条帖子下操作。
        """
        task_id = self.db.add_task(
            platform,
            "comment",
            {"url": url, "text": text, "post_url": post_url},
            status="waiting_human",
        )
        todo_id = self.db.add_human_todo(
            platform=platform,
            action="comment",
            detail={
                "url": post_url or url,
                "search_url": url,
                "suggested_text": text,
                "reason": reason,
                "task_id": task_id,
            },
            task_id=task_id,
        )
        self.db.log(
            platform, "comment", "diverted_to_human",
            {"todo_id": todo_id, "task_id": task_id, "reason": reason},
        )

    def run(
        self,
        count: int = 3,
        platform: str = "linkedin",
        keyword: str | None = None,
        url: str | None = None,
        texts: list[str] | None = None,
        skip_human: bool = False,
    ) -> dict:
        result: dict[str, Any] = {
            "platform": platform,
            "requested": count,
            "success": 0,
            "failed": 0,
            "waiting_human": 0,
            "details": [],
        }

        try:
            self.risk.assert_not_cooling(platform)
            self.risk.assert_time_window()
            self.risk.check_quota(platform, "comment")
        except (QuotaExceeded, TimeWindowClosed, AccountCoolingDown) as exc:
            result["error"] = str(exc)
            return result

        target = self.target_url(platform, keyword, url)
        try:
            page = self.browser.page(platform, target)
            self.risk.check_page(page, platform)
            # 软限流（"发得太快 / 评论到上限"）与硬风控分开扫，
            # 任何一边命中都会抛 CircuitBreakerTripped，这里统一拦下
            self.risk.check_soft_limit(page, platform)
        except CircuitBreakerTripped as exc:
            result["error"] = str(exc)
            return result
        self.humanizer.warmup(page)

        impl = self.platform(platform)
        # 账号级拦截页（补手机号 / checkpoint）要先报出来，
        # 否则会被当成"页面上没有卡片"这种含糊错误。
        blocker = getattr(impl, "detect_blocker", None)
        if callable(blocker):
            reason = blocker(page)
            if reason:
                result["error"] = reason
                return result
        # 卡片可能还在渲染，等一会儿再数（和关注步骤同样的坑）
        wait_cards = getattr(impl, "wait_cards", None)
        cards = wait_cards(page) if callable(wait_cards) else impl.count_cards(page)  # type: ignore[attr-defined]
        result["cards_found"] = cards
        if cards == 0:
            result["error"] = "页面上没有找到帖子卡片"
            return result

        used = self.db.recent_comment_texts(days=7)
        index = 0
        done = 0
        # 循环内重查卡片总数：评论过程中页面会懒加载出更多卡片，旧 count 会失效
        while done < count and index < impl.count_cards(page):  # type: ignore[attr-defined]
            index = impl.find_commentable_index(page, index)  # type: ignore[attr-defined]
            if index < 0:
                result["details"].append({"status": "no_more_cards"})
                break

            # 读正文，先判断值不值得评论（招聘帖等直接跳过，不占配额）
            post_text = impl.post_text(page, index)  # type: ignore[attr-defined]
            if not self.is_discussable(post_text):
                result["details"].append(
                    {"index": index, "status": "skipped_not_discussable",
                     "post": (post_text or "")[:40]}
                )
                index += 1
                continue

            # 选话术 → 加人为瑕疵（只对模板库话术加；
            # 用户用 --text 明确指定的原文必须原样发出，不能被动手脚）
            if texts and done < len(texts):
                base = text = texts[done]
                flaw = ""
            else:
                pick = self.pick_for(post_text, used)
                if pick is None:
                    result["error"] = "评论话术库为空，请编辑 content/comments.txt"
                    break
                base, text = pick.base, pick.text
                text, flaw = add_imperfection(
                    text,
                    self.rng,
                    float(self.config.get("comment.imperfection_probability", 0.08)),
                )

            if not skip_human and self.risk.human_todo_required(platform, "comment"):
                post_url = impl.card_permalink(page, index)  # type: ignore[attr-defined]
                self._divert_to_human(platform, target, text, "人工占比触发", post_url)
                result["waiting_human"] += 1
                result["details"].append({"status": "waiting_human", "text": text})
                index += 1
                done += 1
                continue

            try:
                self.risk.check_quota(platform, "comment")
            except QuotaExceeded as exc:
                result["error"] = str(exc)
                break

            outcome = impl.comment_on_card(  # type: ignore[attr-defined]
                page, index, text, base_text=base
            )
            if outcome.get("success"):
                result["success"] += 1
                used.add(text)
                used.add(base)
            else:
                result["failed"] += 1
            result["details"].append(
                {
                    "index": index,
                    "post": (post_text or "")[:40],
                    "tags": sorted(self._tags_in(post_text)),
                    "flaw": flaw,
                    **outcome,
                }
            )
            index += 1
            done += 1

            if outcome.get("success") and done < count:
                self.humanizer.between_tasks()

        return result

    def run_pending_tasks(self, platform: str = "linkedin", max_tasks: int = 5) -> dict:
        """消费已批准的评论任务（approve 后重新入队的那些）。

        人工已批准，执行时不再触发人工分流、也不再加人为瑕疵。
        """
        tasks = [
            t for t in self.db.pending_tasks(limit=max_tasks)
            if t["action"] == "comment" and t["platform"] == platform
        ]
        result: dict[str, Any] = {
            "processed": 0, "success": 0, "failed": 0, "details": [],
        }
        for task in tasks:
            payload = json.loads(task.get("payload") or "{}")
            text = (payload.get("text") or "").strip()
            if not text:
                self.db.update_task(task["id"], "failed", error="评论内容为空")
                result["failed"] += 1
                continue
            # 回队必须定位到具体帖子：只有搜索/信息流 URL 时无法复现当初那张卡，
            # 退化成盲评会把评论打到错误的帖子，宁可直接失败提示人工处理。
            post_url = (payload.get("post_url") or "").strip()
            if not post_url:
                self.db.update_task(
                    task["id"], "failed",
                    error="评论任务缺少帖子链接（permalink 未提取到），无法自动回队，请手动评论",
                )
                result["failed"] += 1
                result["details"].append({"task_id": task["id"], "ok": False})
                continue
            outcome = self.run(
                count=1, platform=platform, url=post_url, texts=[text], skip_human=True,
            )
            ok = int(outcome.get("success", 0)) > 0
            self.db.update_task(
                task["id"], "done" if ok else "failed", error=outcome.get("error")
            )
            result["processed"] += 1
            result["success" if ok else "failed"] += 1
            result["details"].append({"task_id": task["id"], "ok": ok})
            if ok:
                self.humanizer.between_tasks()
        return result
