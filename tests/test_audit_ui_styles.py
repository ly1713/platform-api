"""Gate: every class a page renders must have a stylesheet source.

The bug behind this gate: 关注列表 的「移除」按钮点了没反应。原因不是点击处理器，
而是 `.ui-modal` 只存在于别的页面的 scoped CSS 里，本页漏抄，确认弹层就退化成
文档流末尾的一个普通 div —— 不可见、不可点、也关不掉。没有任何单元测试会发现
"浮层不在视口里"。

所以这里把审计器本身钉成测试：仓库里出现裸类名，CI 就红。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "scripts" / "audit_ui_styles.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_ui_styles", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit_ui_styles = _load()


def test_repository_has_no_unstyled_classes():
    findings = audit_ui_styles.audit() + audit_ui_styles.hooks_per_element()
    detail = "\n".join(
        f"  {f.file}:{f.line}  .{f.token}  ({f.reason})" for f in findings
    )
    assert not findings, f"以下 class 渲染了却没有样式来源：\n{detail}"


def test_comparison_literals_are_not_treated_as_class_names():
    """`tab === 'keys'` 比对的是值，不是在写 class 名。

    第一版审计器把每个 tab key 都当成漏样式的类，报了 22 条假警报 —— 假警报
    会把门禁变成噪音，最后没人看。
    """
    tokens = [t for _, t in audit_ui_styles._tokens(
        '<template><b :class="{ on: tab === \'keys\' }" /></template>'
    )]
    assert tokens == ["on"]


def test_array_literals_and_ternaries_are_still_class_names():
    tokens = [t for _, t in audit_ui_styles._tokens(
        """<template><i :class="['pm-sm', cond ? 'ok' : 'bad']" /></template>"""
    )]
    assert "pm-sm" in tokens
    assert "ok" in tokens
    assert "bad" in tokens


def test_hook_on_a_naked_element_is_rejected(tmp_path, monkeypatch):
    """标记类单独出现 = 那个元素其实没有外观，正是"看起来有样式"的陷阱。"""
    vue = tmp_path / "Naked.vue"
    vue.write_text(
        '<template><div><span class="ov-seg">x</span></div></template>\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(audit_ui_styles, "WEB_SRC", tmp_path)
    monkeypatch.setattr(audit_ui_styles, "SHARED_CSS", tmp_path / "styles")
    hooks = audit_ui_styles.hooks_per_element()
    assert [f.token for f in hooks] == ["ov-seg"]


def test_main_exits_nonzero_on_findings(tmp_path, monkeypatch, capsys):
    vue = tmp_path / "Bad.vue"
    vue.write_text(
        '<template><div><span class="totally-undefined">x</span></div></template>\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(audit_ui_styles, "WEB_SRC", tmp_path)
    monkeypatch.setattr(audit_ui_styles, "SHARED_CSS", tmp_path / "styles")
    assert audit_ui_styles.main([]) == 1
    assert "totally-undefined" in capsys.readouterr().err
