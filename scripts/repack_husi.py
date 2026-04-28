#!/usr/bin/env python3
"""
从 husi 源码构建 APK（不走 husi APK 解包/smali）：
- 拉取 Codeberg 上 husi 源码
- 替换包名为 cn.xuexi.android（含源码与资源中的旧包名引用）
- 应用名改为 学习强国
- 图标替换为 Vivo 学习强国 APK 的 launcher 图标
- versionCode = husi 原始 versionCode + 10000（并满足最小值）

最终由 GitHub Actions 使用 secrets 进行 release 签名与发布。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

CODEBERG_ARCHIVE_URL = "https://codeberg.org/xchacha20-poly1305/husi/archive/main.tar.gz"
VIVO_API_BASE = "https://h5-api.appstore.vivo.com.cn"
VIVO_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)
DEFAULT_VIVO_APP_ID = "2407867"


def run(cmd, cwd=None):
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=cwd)


def fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_file(url: str, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": VIVO_USER_AGENT})
    with urllib.request.urlopen(req, timeout=600) as resp, open(out_path, "wb") as f:
        shutil.copyfileobj(resp, f)


def download_and_extract_husi_source(workdir: Path) -> Path:
    tar_path = workdir / "husi-main.tar.gz"
    download_file(CODEBERG_ARCHIVE_URL, tar_path)
    src_root = workdir / "husi-src"
    src_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tf:
        tf.extractall(src_root)
    children = [p for p in src_root.iterdir() if p.is_dir()]
    if not children:
        raise RuntimeError("解压 husi 源码后未找到目录")
    return children[0]


def fetch_latest_vivo_info(app_id: str):
    url = f"{VIVO_API_BASE}/detail/{app_id}?frompage=messageh5&app_version=2100"
    data = fetch_json(url, headers={"User-Agent": VIVO_USER_AGENT})
    if not data.get("id"):
        raise RuntimeError(f"Vivo API 返回异常: {data}")
    return data


def decode_vivo_icon(vivo_apk: Path, out_dir: Path) -> Path:
    run(["apktool", "d", "-f", str(vivo_apk), "-o", str(out_dir)])
    manifest = out_dir / "AndroidManifest.xml"
    text = manifest.read_text(encoding="utf-8")
    m = re.search(r'android:icon="([^"]+)"', text)
    if not m:
        raise RuntimeError("未从 Vivo APK manifest 读取到 icon")
    return Path(m.group(1))


def find_android_app_module(repo_dir: Path) -> Path:
    for p in repo_dir.rglob("build.gradle"):
        content = p.read_text(encoding="utf-8", errors="ignore")
        if "com.android.application" in content or "applicationId" in content:
            return p.parent
    for p in repo_dir.rglob("build.gradle.kts"):
        content = p.read_text(encoding="utf-8", errors="ignore")
        if "com.android.application" in content or "applicationId" in content:
            return p.parent
    raise RuntimeError("未找到 Android application 模块（build.gradle/build.gradle.kts）")


def detect_gradle_file(module_dir: Path) -> Path:
    for name in ("build.gradle", "build.gradle.kts"):
        f = module_dir / name
        if f.exists():
            return f
    raise RuntimeError("未找到模块 gradle 文件")


def read_first_match(text: str, patterns, desc: str):
    for pat in patterns:
        m = re.search(pat, text, flags=re.MULTILINE)
        if m:
            return m.group(1)
    raise RuntimeError(f"未找到 {desc}")


def update_gradle_identifiers(gradle_file: Path, new_pkg: str, version_code: int):
    text = gradle_file.read_text(encoding="utf-8")
    old_pkg = read_first_match(
        text,
        [r'applicationId\s+"([^"]+)"', r'applicationId\s*=\s*"([^"]+)"'],
        "applicationId",
    )

    text = re.sub(r'applicationId\s+"[^"]+"', f'applicationId "{new_pkg}"', text)
    text = re.sub(r'applicationId\s*=\s*"[^"]+"', f'applicationId = "{new_pkg}"', text)
    text = re.sub(r'namespace\s+"[^"]+"', f'namespace "{new_pkg}"', text)
    text = re.sub(r'namespace\s*=\s*"[^"]+"', f'namespace = "{new_pkg}"', text)
    text = re.sub(r"versionCode\s+\d+", f"versionCode {version_code}", text)
    text = re.sub(r"versionCode\s*=\s*\d+", f"versionCode = {version_code}", text)

    gradle_file.write_text(text, encoding="utf-8")
    return old_pkg


def replace_app_name(module_dir: Path, app_name: str):
    changed = 0
    for p in (module_dir / "src").rglob("strings.xml"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        new_text = re.sub(
            r'(<string\s+name="app_name"[^>]*>)(.*?)(</string>)',
            rf"\1{app_name}\3",
            text,
            flags=re.DOTALL,
        )
        if new_text != text:
            p.write_text(new_text, encoding="utf-8")
            changed += 1
    if changed == 0:
        print("WARN: 未找到 app_name 字符串，可能需要手动确认应用名资源")


def replace_package_references(root: Path, old_pkg: str, new_pkg: str):
    old_slash = old_pkg.replace(".", "/")
    new_slash = new_pkg.replace(".", "/")
    suffixes = {
        ".kt", ".java", ".xml", ".gradle", ".kts", ".properties", ".pro", ".txt",
        ".json", ".yml", ".yaml",
    }
    replaced = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in suffixes and p.name not in {"AndroidManifest.xml"}:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        new_text = text.replace(old_pkg, new_pkg).replace(old_slash, new_slash)
        if new_text != text:
            p.write_text(new_text, encoding="utf-8")
            replaced += 1
    print(f"已替换旧包名引用文件 {replaced} 个")


def move_source_package_dirs(module_dir: Path, old_pkg: str, new_pkg: str):
    old_rel = Path(*old_pkg.split("."))
    new_rel = Path(*new_pkg.split("."))
    moved = 0
    for lang in ("java", "kotlin"):
        for src_set in (module_dir / "src").glob("*/" + lang):
            old_dir = src_set / old_rel
            new_dir = src_set / new_rel
            if old_dir.exists():
                new_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_dir), str(new_dir))
                moved += 1
    print(f"已迁移源码包目录 {moved} 个")


def replace_icons(module_dir: Path, decoded_vivo: Path, vivo_icon_ref: Path):
    vivo_type, vivo_name = str(vivo_icon_ref).lstrip("@").split("/", 1)
    res_dir = module_dir / "src" / "main" / "res"
    copied = 0
    for target in res_dir.glob("mipmap*/ic_launcher*"):
        density = target.parent.name
        src_candidates = list((decoded_vivo / "res").glob(f"{vivo_type}*/{vivo_name}{target.suffix}"))
        preferred = [c for c in src_candidates if c.parent.name == density]
        src = preferred[0] if preferred else (src_candidates[0] if src_candidates else None)
        if src:
            shutil.copy2(src, target)
            copied += 1
    if copied == 0:
        raise RuntimeError("未替换到任何 ic_launcher 图标文件，请检查 husi 源码资源命名")
    print(f"已替换图标文件 {copied} 个")


def compute_version_code(old_code: int, offset: int, min_code: int) -> int:
    return max(min_code, old_code + offset)


def build_unsigned_apk(repo_dir: Path, module_dir: Path) -> Path:
    gradlew = repo_dir / "gradlew"
    if not gradlew.exists():
        raise RuntimeError("源码仓库缺少 gradlew，无法构建")
    run(["chmod", "+x", str(gradlew)])
    module_name = module_dir.relative_to(repo_dir).parts[0]
    run([str(gradlew), f":{module_name}:assembleRelease"], cwd=repo_dir)
    candidates = list(module_dir.glob("build/outputs/apk/release/*.apk"))
    if not candidates:
        raise RuntimeError("未找到 release APK 输出")
    unsigned = next((c for c in candidates if "unsigned" in c.name), candidates[0])
    return unsigned


def main():
    p = argparse.ArgumentParser(description="从 husi 源码构建学习强国样式 APK")
    p.add_argument("--workdir", default="build/work")
    p.add_argument("--outdir", default="dist")
    p.add_argument("--package-name", default="cn.xuexi.android")
    p.add_argument("--app-name", default="学习强国")
    p.add_argument("--vivo-app-id", default=os.environ.get("VIVO_APP_ID", DEFAULT_VIVO_APP_ID))
    p.add_argument("--version-offset", type=int, default=10000)
    p.add_argument("--min-version-code", type=int, default=10001)
    args = p.parse_args()

    workdir = Path(args.workdir)
    outdir = Path(args.outdir)
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)

    repo_dir = download_and_extract_husi_source(workdir)
    module_dir = find_android_app_module(repo_dir)
    gradle_file = detect_gradle_file(module_dir)
    gradle_text = gradle_file.read_text(encoding="utf-8")
    old_version_code = int(
        read_first_match(gradle_text, [r"versionCode\s+(\d+)", r"versionCode\s*=\s*(\d+)"], "versionCode")
    )
    new_version_code = compute_version_code(old_version_code, args.version_offset, args.min_version_code)

    old_pkg = update_gradle_identifiers(gradle_file, args.package_name, new_version_code)
    replace_package_references(repo_dir, old_pkg, args.package_name)
    move_source_package_dirs(module_dir, old_pkg, args.package_name)
    replace_app_name(module_dir, args.app_name)

    vivo_info = fetch_latest_vivo_info(args.vivo_app_id)
    vivo_apk = workdir / f"xuexi-{vivo_info['version_code']}.apk"
    download_file(vivo_info["download_url"], vivo_apk)
    decoded_vivo = workdir / "decoded-vivo"
    vivo_icon_ref = decode_vivo_icon(vivo_apk, decoded_vivo)
    replace_icons(module_dir, decoded_vivo, vivo_icon_ref)

    unsigned = build_unsigned_apk(repo_dir, module_dir)
    final_unsigned = outdir / f"husi-xuexi-unsigned-vc{new_version_code}.apk"
    shutil.copy2(unsigned, final_unsigned)
    print(json.dumps({"unsigned_apk": str(final_unsigned), "version_code": new_version_code}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
