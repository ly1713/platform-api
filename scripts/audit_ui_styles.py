#!/usr/bin/env python3
"""Fail when a page renders a class that no stylesheet defines.

Why this exists: the 「移除」 button on 关注列表 did nothing for weeks.  The
cause was not a broken click handler.  ``.ui-modal`` was defined by copy-paste
in each page's ``<style scoped>`` block, and 关注列表 was the one page that
never got the copy.  So the confirm dialog rendered as a plain ``div`` at the
very bottom of the document flow: invisible, unreachable, and impossible to
close.  Every unit test passed because nothing tested whether the overlay was
in the viewport.

A missing class never throws.  It silently degrades: overlays stop floating,
flex rows wrap, badges lose their borders.  That is precisely the failure mode
this repository cannot afford, so it is checked mechanically instead of by eye.

What is checked:
- static ``class="..."`` tokens in every ``.vue`` template;
- class tokens produced by ``:class`` object syntax (``{ 'a': cond }``);
- ``:class="[..]"`` array literals;
- each token must be defined either in a shared stylesheet
  (``web/src/styles/*.css``) or in the file's own ``<style>`` block, or be a
  documented pass-through hook.

Pass-through hooks (``clickable``, ``ov-seg``, ...) are allowed only when the
element also carries at least one styled class, because their job is to give a
test/selector a stable name, not to carry appearance.  A hook on an otherwise
unstyled element is rejected: that is how a "styled" element ends up naked.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WEB_SRC = ROOT / "web" / "src"
SHARED_CSS = WEB_SRC / "styles"

STATIC_CLASS = re.compile(r'(?<![\w:-])class="([^"]*)"')
BOUND_CLASS = re.compile(r':class="([^"]*)"')
# `{ on: cond }` 里的键按 JS 规则可以不加引号，而仓库里绝大多数就是不加的。
# 早先的写法要求键带引号，等于把 `:class="{ on: ... }"` 整类表达式的键全漏掉 ——
# 审计器"通过"但根本没看，比报错更危险。键必须在 `{` 或 `,` 之后，才不至于把
# 三元 `c ? x : y` 的冒号误当成对象键。
OBJECT_KEY = re.compile(r"""(?:[{,]\s*)(?:['"]([A-Za-z][\w-]*)['"]|([A-Za-z][\w-]*))\s*:""")
ARRAY_STRING = re.compile(r"""['"]([A-Za-z][\w-]*)['"]""")
# `tab === 'keys'` / `status !== 'ok'` / `plats.includes('dy')` 比对的是值，
# 不是在写 class 名。先抹掉这些字面量，否则 audit 会把每个 tab key 当成漏样式的类。
COMPARISON = re.compile(r"""(?:===|!==|==|!=)\s*(['"])[^'"]*\1""")
MEMBER_TEST = re.compile(r"""\.(?:startsWith|endsWith|includes)\(\s*(['"])[^'"]*\1""")
STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S)
TEMPLATE_START = re.compile(r"<template>|<script")
# Every `.name` in a selector defines that class, including compound selectors
# like `.ui-btn.sm` (no whitespace before the dot) and descendant rules like
# `.ui-card .name a`.  Comments and url(...) contents are stripped first so a
# file path or a decimal never registers as a class.
CSS_SELECTOR = re.compile(r"\.(-?[A-Za-z_][\w-]*)")
CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
CSS_URL = re.compile(r"url\([^)]*\)", re.I)

# Classes whose job is a stable selector for tests / descendant rules, never
# appearance.  Keep this list short and justified; every entry is a place where
# a naked element could hide.
PASS_THROUGH_HOOKS = {
    "ov-seg",        # scope for `.ui-seg` sizing overrides elsewhere
    "executor-strip",  # reading-order anchor inside the panel grid
    "prompt-panel",    # same
    "article-modal",   # disambiguates two overlays that share `.ui-modal`
    "player-box",      # same, for the video overlay
}
# 曾经是纯标记类、后来补上了样式落点的，别再放回上面那张表：
# `clickable` 现在在 ui.css 里给了 `.ui-table tbody tr.clickable` 的手型，
# `cat-label` 在 Overview.vue 里有了截断规则。
# Element Plus owns its own class namespace.
FRAMEWORK_PREFIXES = ("el-", "is-")

MIN_REASON = "add the rule to a stylesheet, or use an existing component class"


@dataclass(frozen=True, order=True)
class Finding:
    file: str
    line: int
    token: str
    reason: str


def _display_path(vue: Path) -> str:
    """Repo-relative when possible; tests point WEB_SRC at a tmp dir."""
    try:
        return vue.relative_to(ROOT).as_posix()
    except ValueError:
        return vue.as_posix()


def _shared_classes() -> set[str]:
    found: set[str] = set()
    for css in sorted(SHARED_CSS.glob("*.css")):
        text = CSS_URL.sub("", CSS_COMMENT.sub("", css.read_text(encoding="utf-8")))
        found.update(CSS_SELECTOR.findall(text))
    return found


def _local_classes(source: str) -> set[str]:
    found: set[str] = set()
    for block in STYLE_BLOCK.findall(source):
        found.update(CSS_SELECTOR.findall(CSS_URL.sub("", CSS_COMMENT.sub("", block))))
    return found


def _tokens(source: str) -> list[tuple[int, str]]:
    """Static and bound class tokens with their 1-indexed line numbers."""
    end = TEMPLATE_START.search(source, 1)
    template = source[: end.start()] if end else source
    out: list[tuple[int, str]] = []

    for match in STATIC_CLASS.finditer(template):
        raw = match.group(1)
        line = template.count("\n", 0, match.start()) + 1
        for token in raw.split():
            if "{" in token or "}" in token:
                continue
            out.append((line, token))

    for match in BOUND_CLASS.finditer(template):
        expr = match.group(1)
        line = template.count("\n", 0, match.start()) + 1
        # 对象语法的键就是类名（带不带引号都算）；值一侧的字符串只在数组字面量
        # 或三元分支里才是类名，比较运算的右值不是。
        for quoted, bare in OBJECT_KEY.findall(expr):
            out.append((line, quoted or bare))
        values = COMPARISON.sub("", MEMBER_TEST.sub("", expr))
        for literal in ARRAY_STRING.findall(values):
            out.append((line, literal))
    # 同一个表达式里带引号的键会被两条规则各抓一次，去重后再交回去。
    return list(dict.fromkeys(out))


def audit() -> list[Finding]:
    shared = _shared_classes()
    findings: list[Finding] = []
    for vue in sorted(WEB_SRC.rglob("*.vue")):
        source = vue.read_text(encoding="utf-8")
        known = shared | _local_classes(source)
        rel = _display_path(vue)
        seen_here: set[tuple[int, str]] = set()
        for line, token in _tokens(source):
            if (line, token) in seen_here:
                continue
            seen_here.add((line, token))
            if token.startswith(FRAMEWORK_PREFIXES) or token in known:
                continue
            if token in PASS_THROUGH_HOOKS:
                continue
            findings.append(Finding(rel, line, token, MIN_REASON))
    return findings


def hooks_per_element() -> list[Finding]:
    """Reject a pass-through hook on an element with no styled class.

    ``<span class="cat-label">`` alone renders default text; the hook was
    supposed to inherit its look from a sibling rule that never existed.
    """
    shared = _shared_classes()
    findings: list[Finding] = []
    for vue in sorted(WEB_SRC.rglob("*.vue")):
        source = vue.read_text(encoding="utf-8")
        known = shared | _local_classes(source)
        rel = _display_path(vue)
        end = TEMPLATE_START.search(source, 1)
        template = source[: end.start()] if end else source
        for match in STATIC_CLASS.finditer(template):
            tokens = [t for t in match.group(1).split() if "{" not in t and "}" not in t]
            if not tokens:
                continue
            hooks = [t for t in tokens if t in PASS_THROUGH_HOOKS]
            styled = [t for t in tokens if t in known and t not in PASS_THROUGH_HOOKS]
            if hooks and not styled:
                line = template.count("\n", 0, match.start()) + 1
                for hook in hooks:
                    findings.append(
                        Finding(rel, line, hook, "标记类不能单独存在：同元素至少有一个有样式的类")
                    )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    findings = audit() + hooks_per_element()
    if args.json:
        json.dump([f.__dict__ for f in findings], sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0 if not findings else 1

    if not findings:
        print("ui style audit OK: 每个渲染的 class 都有样式来源")
        return 0
    print(f"ui style audit: {len(findings)} 处没有样式来源的 class", file=sys.stderr)
    for finding in findings:
        print(f"  {finding.file}:{finding.line}  .{finding.token}  ({finding.reason})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
