# -*- coding: utf-8 -*-
"""生成 GitHub Release 说明 (从 README 的更新日志里抽取当前版本那一节)。

用法:
    python tools/release_notes.py --version v1.0.3 --out RELEASE_BODY.md

为什么单独抽脚本: 以前 Release 正文是 workflow 里写死的一段通用模板, 每次发版
正文都一样、看不出这版改了什么。现在直接复用 README 的更新日志 —— 中英两份都贴上,
并且**发版说明与仓库文档永远一致**(改 README 就是改发版说明, 不会两处各自漂移)。
"""
import os
import re
import io
import sys
import argparse

# CI runner 的 stdout 可能是 cp1252, 打印中文会 UnicodeEncodeError 打断 job
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_text(path):
    with io.open(path, encoding='utf-8') as f:
        return f.read()


def extract_section(text, version):
    """抓出 `### <version>` 到下一个 `### ` 之间的正文 (不含标题行)。

    版本号后面必须跟非单词字符 (或行尾/空格), 否则 v1.0.3 会把 v1.0.30 的章节也吃掉。
    """
    # 标题后允许中文括号/空格, 例如 "### v1.0.3（2026-09-12）"
    pat = re.compile(r'^###[ \t]*' + re.escape(version) + r'(?![\w.])[^\n]*\n(.*?)(?=^###[ \t]|^##[ \t]|\Z)',
                     re.M | re.S)
    m = pat.search(text)
    return m.group(1).strip() if m else ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--version', required=True, help='版本号, 例如 v1.0.3')
    ap.add_argument('--out', default=os.path.join(ROOT, 'RELEASE_BODY.md'))
    args = ap.parse_args()
    v = args.version

    zh = extract_section(read_text(os.path.join(ROOT, 'README.zh-CN.md')), v)
    en = extract_section(read_text(os.path.join(ROOT, 'README.md')), v)

    parts = ['# GoAI Auto-Player %s' % v, '']
    # 只挂最新一个 tag 的短说明, 完整日志在仓库 README
    parts += [
        '自动构建产物 —— CI 打包, 发版前已跑过完整回归测试。',
        '',
        '## 下载哪个包',
        '',
        '| 文件 | 说明 |',
        '| --- | --- |',
        '| `GoAI-AutoPlayer-%s.zip` | 核心包: 代码 + KataGo 引擎, **不含**网络权重 |' % v,
        '| `GoAI-AutoPlayer-%s-full.zip` | 完整便携包: 额外含 `katago/*.bin.gz` 权重, 解压即用'
        '（仅在手动触发并填了模型地址时提供） |' % v,
        '',
        '每个 zip 同目录的 `.sha256` 是校验值, 可用 `certutil -hashfile <zip> SHA256` 比对。',
        '模型下载方式见 README「模型下载与加载」。',
        '',
        '> ⚠️ 仅供本地学习与研究使用。请遵守各平台服务条款, **严禁**用于在线自动对弈、代下、刷分等用途,',
        '> 相关风险与责任归属见 README「免责声明与使用限制」。',
        '',
    ]
    if zh:
        parts += ['---', '', '## 更新内容（中文）', '', zh, '']
    if en:
        parts += ['---', '', '## What changed (English)', '', en, '']
    if not (zh or en):
        parts += ['---', '', '（README 中未找到 %s 的更新日志小节）' % v, '']

    body = '\n'.join(parts)
    with io.open(args.out, 'w', encoding='utf-8', newline='\n') as f:
        f.write(body)
    print('生成 %s (%d 字符, 中文节 %s / 英文节 %s)'
          % (args.out, len(body), '有' if zh else '无', '有' if en else '无'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
