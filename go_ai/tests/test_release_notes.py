# -*- coding: utf-8 -*-
"""发版说明生成测试 (tools/release_notes.py)。

为什么要测: Release 正文是从 README 更新日志里**正则抽取**的, 抽取规则一旦失效,
发版时不会报错 —— 只会静默产出一句「未找到更新日志小节」, 发出去才发现。
v1.0.3 首次发版就因为别的原因失败过一次, 这里把抽取逻辑锁住。
"""
import io
import os
import sys
import importlib.util
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def _load():
    p = os.path.join(ROOT, 'tools', 'release_notes.py')
    spec = importlib.util.spec_from_file_location('release_notes', p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rn = _load()


def _read(path):
    with io.open(path, encoding='utf-8') as f:
        return f.read()


class TestExtractSection(unittest.TestCase):
    SAMPLE = (
        '# Changelog\n'
        '\n'
        '### v1.0.3 (2026-09-12)\n'
        '\n'
        '**New**\n'
        '\n'
        '- thing A\n'
        '- thing B\n'
        '\n'
        '### v1.0.2 (2026-09-11)\n'
        '\n'
        '- old stuff\n'
    )

    def test_extracts_only_that_version(self):
        body = rn.extract_section(self.SAMPLE, 'v1.0.3')
        self.assertIn('thing A', body)
        self.assertIn('thing B', body)
        self.assertNotIn('old stuff', body)   # 不能把下一版的内容吃进来
        self.assertNotIn('v1.0.2', body)

    def test_stops_at_next_h2(self):
        text = '### v2.0.0\n\n- x\n\n## Some other section\n\n- y\n'
        body = rn.extract_section(text, 'v2.0.0')
        self.assertIn('- x', body)
        self.assertNotIn('- y', body)

    def test_chinese_paren_title(self):
        """中文全角括号的标题 (README.zh-CN.md 就是这种) 也要匹配。"""
        text = '### v1.0.3（2026-09-12）\n\n- 中文条目\n\n### v1.0.2（2026-09-11）\n\n- 旧的\n'
        body = rn.extract_section(text, 'v1.0.3')
        self.assertIn('中文条目', body)
        self.assertNotIn('旧的', body)

    def test_missing_version_returns_empty(self):
        self.assertEqual(rn.extract_section(self.SAMPLE, 'v9.9.9'), '')

    def test_version_not_prefix_matched(self):
        """v1.0.3 不能匹配到 v1.0.30 —— 否则抽取会串到别的版本。"""
        text = '### v1.0.30\n\n- newer\n\n### v1.0.3\n\n- target\n'
        body = rn.extract_section(text, 'v1.0.3')
        self.assertIn('target', body)
        self.assertNotIn('newer', body)


class TestRealReadme(unittest.TestCase):
    """真实 README 必须能抽出当前版本, 否则发版说明就是空的。"""

    def test_latest_changelog_section_present(self):
        version = 'v1.0.3'
        for name, must in (('README.zh-CN.md', '回归测试'), ('README.md', 'Regression tests')):
            text = _read(os.path.join(ROOT, name))
            body = rn.extract_section(text, version)
            self.assertTrue(body, '%s 里没找到 %s 的更新日志小节' % (name, version))
            self.assertIn(must, body, '%s 的 %s 小节内容可疑' % (name, version))
            # 不能把下一版的内容混进来
            self.assertNotIn('v1.0.2', body, '%s 的抽取越界到了 v1.0.2' % name)

    def test_generated_body_has_disclaimer_and_lang_sections(self):
        out = os.path.join(tempfile.gettempdir(), '_rn_test_body.md')
        argv = sys.argv
        try:
            sys.argv = ['release_notes.py', '--version', 'v1.0.3', '--out', out]
            rn.main()
        finally:
            sys.argv = argv
        body = _read(out)
        self.assertIn('严禁', body)          # 免责声明必须在
        self.assertIn('更新内容（中文）', body)
        self.assertIn('What changed (English)', body)
        self.assertIn('sha256', body)
        os.remove(out)


if __name__ == '__main__':
    unittest.main()
