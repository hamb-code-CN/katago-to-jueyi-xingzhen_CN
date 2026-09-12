# -*- coding: utf-8 -*-
"""打包发布 zip (本地与 CI 共用)。

用法:
    python tools/make_release.py                     # 核心包 (代码+引擎, 不含 ~93MB 模型)
    python tools/make_release.py --with-model        # 完整便携包 (含 katago/*.bin.gz)
    python tools/make_release.py --version v1.0.3 --out dist

产物:
    dist/GoAI-AutoPlayer-<版本>[-full].zip
    dist/GoAI-AutoPlayer-<版本>[-full].zip.sha256

设计要点: 只打 **git 跟踪的文件** (外加可选的模型), 因此不会把 venv、调试日志、
识别缓存、KataGoData 调优缓存等本机垃圾装进去 —— 这正是以前手工打包容易漏的坑。
"""
import os
import sys
import time
import hashlib
import argparse
import subprocess
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_GLOB = '.bin.gz'
SKIP_DIRS = {'.git', 'venv', '.venv', '__pycache__', 'dist', '.idea', '.github'}
SKIP_SUFFIX = ('.pyc', '.log', '.sgf')


def tracked_files():
    """git 跟踪的文件列表 (相对路径)。非 git 目录时回退为排除法遍历。"""
    try:
        r = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True,
                           text=True, encoding='utf-8', errors='replace', timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return [ln.strip().replace('/', os.sep)
                    for ln in r.stdout.splitlines() if ln.strip()]
    except Exception as e:
        print('[warn] git ls-files 失败(%r), 回退为目录遍历' % (e,))
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            p = os.path.join(base, f)
            rel = os.path.relpath(p, ROOT)
            if rel.endswith(SKIP_SUFFIX):
                continue
            out.append(rel)
    return out


def model_files():
    d = os.path.join(ROOT, 'katago')
    if not os.path.isdir(d):
        return []
    return [os.path.join('katago', f) for f in os.listdir(d)
            if f.lower().endswith(MODEL_GLOB)]


def detect_version():
    try:
        r = subprocess.run(['git', 'describe', '--tags', '--abbrev=0'], cwd=ROOT,
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return 'v' + time.strftime('%Y%m%d')


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def build(version, out_dir, with_model=False):
    files = tracked_files()
    if with_model:
        mf = model_files()
        if not mf:
            print('[错误] --with-model 但 katago/ 下没有 *.bin.gz 模型')
            return None
        files = files + mf
    # 去重 (git 里可能已含模型, 一般不会) 并剔除不存在项
    seen, final = set(), []
    for rel in files:
        if rel in seen:
            continue
        seen.add(rel)
        if os.path.isfile(os.path.join(ROOT, rel)):
            final.append(rel)

    suffix = '-full' if with_model else ''
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, 'GoAI-AutoPlayer-%s%s.zip' % (version, suffix))
    pkg_root = 'GoAI-AutoPlayer-%s' % version
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel in final:
            z.write(os.path.join(ROOT, rel), os.path.join(pkg_root, rel))
        # 附带一份校验与说明, 解压后一眼能看到
        z.writestr(os.path.join(pkg_root, 'PACKAGE_INFO.txt'),
                   'GoAI Auto-Player %s\n构建时间: %s\n文件数: %d\n含模型: %s\n'
                   % (version, time.strftime('%Y-%m-%d %H:%M:%S'), len(final),
                      '是' if with_model else '否 (请自行下载 .bin.gz 放入 katago/)'))
    digest = sha256(zip_path)
    with open(zip_path + '.sha256', 'w', encoding='utf-8') as f:
        f.write('%s  %s\n' % (digest, os.path.basename(zip_path)))
    size_mb = os.path.getsize(zip_path) / 1048576.0
    print('打包完成: %s' % zip_path)
    print('  文件数 %d | 大小 %.1f MB | SHA256 %s' % (len(final), size_mb, digest[:16] + '...'))
    return zip_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--version', default=None, help='版本号 (默认取 git 最近的 tag)')
    ap.add_argument('--out', default=os.path.join(ROOT, 'dist'))
    ap.add_argument('--with-model', action='store_true', help='把 katago/*.bin.gz 一并打进去')
    args = ap.parse_args()
    v = args.version or detect_version()
    p = build(v, args.out, with_model=args.with_model)
    return 0 if p else 1


if __name__ == '__main__':
    sys.exit(main())
