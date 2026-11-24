"""给评论文案加"人为小瑕疵"，让它读起来不像机器写的。

设计原则：
1. 只加**不会改变意思**的瑕疵。商务场合一句话打错成别的意思，比机器味更致命。
2. 一条评论最多一处瑕疵，绝不叠加。
3. 概率默认很低（8%），大部分评论是干净的。
4. 按文本语言自动切换规则：中文用同音/形近字手误，英文用常见拼写错误。

三类瑕疵（按真实输入习惯）：
- typo：手误。中文是同音字，英文是常见拼写错误
- punct：标点疏忽，句末漏标点、句号写成逗号
- filler：口语填充词开头（嗯、对了 / well, honestly）
"""
from __future__ import annotations

import random
import re

# ---------- 中文：只收"错了也读得懂"的常见手误 ----------
# 必须是话术库里真实出现的高频词，否则永远不会触发
_ZH_MISTAKES: tuple[tuple[str, str], ...] = (
    ("你们", "你门"),
    ("我们", "我门"),
    ("问题", "问提"),
    ("时候", "时侯"),
    ("订单", "定单"),
    ("怎么", "怎末"),
    ("现在", "现再"),
    ("影响", "影向"),
    ("已经", "己经"),
    ("效率", "效律"),
    ("所以", "所已"),
    ("因为", "因位"),
)

# ---------- 英文：最常见的拼写错误，母语者也会犯 ----------
_EN_MISTAKES: tuple[tuple[str, str], ...] = (
    ("because", "becuase"),
    ("which", "wich"),
    ("their", "thier"),
    ("there", "ther"),
    ("receive", "recieve"),
    ("separate", "seperate"),
    ("definitely", "definately"),
    ("business", "buisness"),
    ("management", "managment"),
    ("successful", "succesful"),
    ("believe", "beleive"),
    ("quality", "qualtiy"),
    ("delivery", "delvery"),
    ("shipping", "shiping"),
    ("supply", "suply"),
    ("supplier", "suplier"),
    ("schedule", "shedule"),
    ("production", "prodcution"),
    ("experience", "expierence"),
    ("standard", "standrad"),
    ("really", "realy"),
    ("actually", "actully"),
    ("available", "availble"),
    ("different", "diffrent"),
    ("important", "importnat"),
    ("problem", "probelm"),
    ("customer", "costumer"),
    ("recommend", "recomend"),
)

_ZH_FILLERS: tuple[str, ...] = ("嗯，", "对了，", "话说，")
_EN_FILLERS: tuple[str, ...] = ("Well, ", "Honestly, ", "BTW, ", "Actually, ")

# 句首是这些词时可以安全改成小写（"Actually, because…"），
# 不在表里的多半是专有名词（LinkedIn / Amazon），不动它，避免写成 linkedIn
_EN_SAFE_LOWER = frozenset({
    "the", "this", "that", "these", "those", "we", "our", "it", "there",
    "their", "they", "because", "which", "what", "when", "if", "for", "in",
    "on", "at", "thanks", "thank", "honestly", "actually", "well", "great",
    "good", "nice", "same", "both", "most", "many", "one", "and", "but",
})

_MIN_LEN = 12  # 太短的句子加瑕疵会显得很怪


def _is_chinese(text: str) -> bool:
    """按 CJK 字符占比判断语言，混合文本以中文为主时走中文规则。"""
    if not text:
        return False
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk >= 3 and cjk >= len(text) * 0.15


def _replace_zh(text: str, table: tuple[tuple[str, str], ...], rng: random.Random):
    applicable = [(a, b) for a, b in table if a in text]
    if not applicable:
        return None
    correct, wrong = rng.choice(applicable)
    return text.replace(correct, wrong, 1)


def _replace_en(text: str, table: tuple[tuple[str, str], ...], rng: random.Random):
    """英文按整词匹配，并保留首字母大小写。"""
    matches = []
    for correct, wrong in table:
        pattern = re.compile(rf"\b{re.escape(correct)}\b", re.IGNORECASE)
        found = pattern.search(text)
        if found:
            matches.append((correct, wrong, found))
    if not matches:
        return None
    correct, wrong, found = rng.choice(matches)
    original = found.group(0)
    replacement = wrong.capitalize() if original[:1].isupper() else wrong
    return text[: found.start()] + replacement + text[found.end():]


def add_imperfection(
    text: str, rng: random.Random | None = None, probability: float = 0.08
) -> tuple[str, str]:
    """按概率给文案加一处小瑕疵，返回 (处理后文案, 瑕疵类型)。

    类型为 "" 表示没改动；否则是 typo / punct / filler。
    """
    rng = rng or random
    text = (text or "").strip()
    if len(text) < _MIN_LEN or probability <= 0:
        return text, ""
    if rng.random() >= probability:
        return text, ""

    chinese = _is_chinese(text)
    table = _ZH_MISTAKES if chinese else _EN_MISTAKES
    fillers = _ZH_FILLERS if chinese else _EN_FILLERS

    kind = rng.choices(("typo", "punct", "filler"), weights=(6, 2, 2), k=1)[0]

    if kind == "typo":
        replaced = _replace_zh(text, table, rng) if chinese else _replace_en(text, table, rng)
        if replaced:
            return replaced, "typo"
        kind = "punct"  # 没命中就退化成标点疏忽

    if kind == "punct":
        if text.endswith(("。", "？", "！", ".", "?", "!")):
            if rng.random() < 0.6:
                return text[:-1], "punct"  # 句末漏标点
            return text[:-1] + ("，" if chinese else ","), "punct"
        return text + ("，" if chinese else ","), "punct"

    # filler：口语填充词开头
    if any(text.startswith(f) for f in fillers):
        return text, ""
    if not chinese:
        # 英文加填充词后，句首大写要跟着改小写，否则 "Actually, Because…" 很别扭
        head = re.match(r"^([A-Za-z']+)", text)
        if head:
            word = head.group(1)
            if word == "I":
                pass  # 保持大写 I
            elif word[:1].isupper():
                if word.lower() not in _EN_SAFE_LOWER:
                    return text, ""  # 疑似专有名词，放弃这次填充
                text = text[0].lower() + text[1:]
    return rng.choice(fillers) + text, "filler"
