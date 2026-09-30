"""教模型：知识库与补充话术文件的安全读写。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from grannyflat_bot.config import DATA_DIR, KNOWLEDGE_DIR

PROMPTS_DIR = DATA_DIR / "prompts"
ALLOWED_ROOTS = {
    "knowledge": KNOWLEDGE_DIR,
    "prompts": PROMPTS_DIR,
}

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,120}\.(md|yaml|yml)$")


def ensure_prompt_defaults() -> None:
    """首次启动时创建可编辑的补充话术 / 引导三问配置。"""
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    extra = PROMPTS_DIR / "extra_instructions.md"
    if not extra.exists():
        extra.write_text(
            """# 补充话术 / Extra talk scripts（网页「教模型」可编辑）

以下内容会追加到每次对话的系统提示中。用中文写给内部同事看也可以；对客回复语言仍由系统规则控制。

## 推荐写法
- 用条目写清：遇到什么问题 → 怎么回答 → 不要说什么
- 示意 AUD **只写已核定数字**；待核定/空白不报数；不要用规则引擎数字对客报价
- 定不了 council 不报价；即使 council 已知，区间待核定也不承诺立刻给价；不主动要电话/邮箱

## 示例
- 客户问工期：说明模块厂制作 + 海运 + 现场基础/吊装，具体周数需看州与场地，不承诺固定交房日。
- 客户问「全包多少钱」：先确认地址/城区+邮编；能定 council 且该型号/范围已有获批数字才给示意；当前若待核定，说明核对方案、范围和场地后确认估价。
""",
            encoding="utf-8",
        )
    guided = PROMPTS_DIR / "guided_intake.yaml"
    if not guided.exists():
        from grannyflat_bot.intake.guided_flow import DEFAULT_GUIDED_STEPS

        lines = [
            "# 引导式连续提问（教模型可编辑）",
            "# 列表顺序 = 提问顺序；每次只问尚未填写的第一项。",
            "#",
            "# 第一问引导地址/城区+邮编（field 仍为 state；仅邮编往往不够定 council）。",
            "# 示意价仅来自补充话术已核定数字；须能确定 council 且区间非待核定才可报价。",
            "# 保存后点「重建知识索引」即可热加载。",
            "",
            "steps:",
        ]
        for step in DEFAULT_GUIDED_STEPS:
            lines.append(f"  - field: {step.field}")
            lines.append(f'    question_en: "{_yaml_escape(step.question_en)}"')
            lines.append(f'    question_zh: "{_yaml_escape(step.question_zh)}"')
            lines.append(f'    hint_en: "{_yaml_escape(step.hint_en)}"')
            lines.append(f'    hint_zh: "{_yaml_escape(step.hint_zh)}"')
            lines.append("")
        guided.write_text("\n".join(lines), encoding="utf-8")


def _yaml_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def list_teach_files() -> list[dict[str, Any]]:
    ensure_prompt_defaults()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for kind, root in ALLOWED_ROOTS.items():
        if not root.exists():
            continue
        paths = (
            list(root.glob("*.md"))
            + list(root.glob("*.yaml"))
            + list(root.glob("*.yml"))
        )
        for path in sorted(paths, key=lambda p: p.name.lower()):
            if not _SAFE_NAME.match(path.name):
                continue
            key = (kind, path.name)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "kind": kind,
                    "name": path.name,
                    "label": _label_for(kind, path.name),
                    "bytes": path.stat().st_size,
                    "updated_at": path.stat().st_mtime,
                }
            )
    return rows


def read_teach_file(kind: str, name: str) -> dict[str, Any]:
    path = _resolve(kind, name)
    content = path.read_text(encoding="utf-8")
    return {
        "kind": kind,
        "name": name,
        "label": _label_for(kind, name),
        "content": content,
        "bytes": len(content.encode("utf-8")),
        "updated_at": path.stat().st_mtime,
    }


def write_teach_file(kind: str, name: str, content: str) -> dict[str, Any]:
    path = _resolve(kind, name, must_exist=False)
    if kind == "knowledge" and not path.exists() and name.count(".") != 1:
        raise ValueError("invalid file name")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")
    if name in ("guided_intake.yaml", "guided_intake.yml"):
        from grannyflat_bot.intake.guided_flow import reload_guided_steps

        reload_guided_steps()
    return read_teach_file(kind, name)


def load_extra_instructions() -> str:
    """读取补充话术，追加到 system prompt。"""
    ensure_prompt_defaults()
    path = PROMPTS_DIR / "extra_instructions.md"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    return text


def _resolve(kind: str, name: str, *, must_exist: bool = True) -> Path:
    if kind not in ALLOWED_ROOTS:
        raise ValueError("kind must be knowledge or prompts")
    if not _SAFE_NAME.match(name):
        raise ValueError("invalid file name")
    root = ALLOWED_ROOTS[kind].resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise ValueError("path outside allowed directory")
    if must_exist and not path.exists():
        raise FileNotFoundError(name)
    return path


def _label_for(kind: str, name: str) -> str:
    labels = {
        ("prompts", "extra_instructions.md"): "补充话术（优先编辑）",
        ("prompts", "guided_intake.yaml"): "引导三问（顺序+文案）",
        ("prompts", "guided_intake.yml"): "引导三问（顺序+文案）",
        ("prompts", "learned_from_agents.md"): "客服回复自动收录",
        ("knowledge", "product_overview.md"): "产品概览",
        ("knowledge", "faq.md"): "常见问题 FAQ",
        ("knowledge", "cooperation_and_costs.md"): "合作与成本口径",
        ("knowledge", "sales_boundaries.md"): "销售边界 / 禁区",
    }
    return labels.get((kind, name), name)
