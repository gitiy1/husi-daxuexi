#!/usr/bin/env python3
"""
从 husi 源码构建 APK（不走 husi APK 解包/smali）：
- 拉取 Codeberg 上 husi 源码
- 替换包名为 cn.xuexi.android（含源码与资源中的旧包名引用）
- 应用名改为 学习强国
- 图标替换为 Vivo 学习强国 APK 的 launcher 图标
- versionCode = husi 原始 versionCode + 10000（并满足最小值）

最终由 uber-apk-signer 一键签名后再发布。
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

CODEBERG_ARCHIVE_URLS = [
    "https://codeberg.org/xchacha20-poly1305/husi/archive/main.tar.gz",
    "https://codeberg.org/xchacha20-poly1305/husi/archive/main.zip",
]
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
    last_error = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_error = e
    raise RuntimeError(f"请求失败: {url}, {last_error}")


def download_file(url: str, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    last_error = None
    for _ in range(3):
        try:
            headers = {"User-Agent": VIVO_USER_AGENT}
            if "vivo.com.cn" in url:
                headers["Referer"] = "https://appstore.vivo.com.cn/"
                headers["Accept"] = "*/*"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=600) as resp, open(out_path, "wb") as f:
                shutil.copyfileobj(resp, f)
            return
        except Exception as e:
            last_error = e
    raise RuntimeError(f"下载失败: {url}, {last_error}")


def _extract_archive(archive_path: Path, out_dir: Path):
    if tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(out_dir)
        return
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(out_dir)
        return
    raise RuntimeError(f"无法识别源码归档格式: {archive_path}")


def download_and_extract_husi_source(workdir: Path) -> Path:
    clone_dir = workdir / "husi-src-git"
    if clone_dir.exists():
        shutil.rmtree(clone_dir)
    try:
        run(["git", "clone", "--depth", "1", "https://codeberg.org/xchacha20-poly1305/husi", str(clone_dir)])
        gitmodules = clone_dir / ".gitmodules"
        if gitmodules.exists():
            text = gitmodules.read_text(encoding="utf-8")
            rewritten = text.replace("git@github.com:", "https://github.com/")
            if rewritten != text:
                gitmodules.write_text(rewritten, encoding="utf-8")
                run(["git", "submodule", "sync", "--recursive"], cwd=clone_dir)
        run(["git", "submodule", "update", "--init", "--recursive"], cwd=clone_dir)
        return clone_dir
    except Exception as e:
        print(f"WARN: git clone 失败，回退到源码归档下载: {e}")

    last_error = None
    for i, url in enumerate(CODEBERG_ARCHIVE_URLS, start=1):
        ext = ".tar.gz" if url.endswith(".tar.gz") else ".zip"
        candidate = workdir / f"husi-main-{i}{ext}"
        src_root = workdir / "husi-src"
        if src_root.exists():
            shutil.rmtree(src_root)
        src_root.mkdir(parents=True, exist_ok=True)
        try:
            print(f"下载源码归档: {url}")
            download_file(url, candidate)
            _extract_archive(candidate, src_root)
            children = [p for p in src_root.iterdir() if p.is_dir()]
            if not children:
                raise RuntimeError("解压 husi 源码后未找到目录")
            return children[0]
        except Exception as e:
            last_error = e
            print(f"WARN: 下载失败，尝试下一个地址: {e}")
    raise RuntimeError(f"下载 husi 源码归档失败: {last_error}")


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
    preferred = repo_dir / "androidApp"
    if preferred.exists():
        return preferred
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


def update_husi_properties(repo_dir: Path, new_pkg: str, version_code: int):
    props = repo_dir / "husi.properties"
    if not props.exists():
        return None
    text = props.read_text(encoding="utf-8")
    old_pkg = read_first_match(text, [r"^PACKAGE_NAME=(.+)$"], "PACKAGE_NAME")
    text = re.sub(r"^PACKAGE_NAME=.*$", f"PACKAGE_NAME={new_pkg}", text, flags=re.MULTILINE)
    text = re.sub(r"^VERSION_CODE=.*$", f"VERSION_CODE={version_code}", text, flags=re.MULTILINE)
    props.write_text(text, encoding="utf-8")
    return old_pkg.strip()


def replace_app_name(search_root: Path, app_name: str):
    changed = 0
    for p in search_root.rglob("strings.xml"):
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
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new_text = text.replace(old_pkg, new_pkg).replace(old_slash, new_slash)
        if new_text != text:
            p.write_text(new_text, encoding="utf-8")
            replaced += 1
    print(f"已替换旧包名引用文件 {replaced} 个")


def move_source_package_dirs(search_root: Path, old_pkg: str, new_pkg: str):
    old_rel = Path(*old_pkg.split("."))
    new_rel = Path(*new_pkg.split("."))
    moved = 0
    for lang in ("java", "kotlin"):
        for src_set in search_root.rglob(lang):
            if "/src/" not in str(src_set).replace("\\", "/"):
                continue
            old_dir = src_set / old_rel
            new_dir = src_set / new_rel
            if old_dir.exists():
                new_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_dir), str(new_dir))
                moved += 1
    print(f"已迁移源码包目录 {moved} 个")


def move_schema_dirs(repo_dir: Path, old_pkg: str, new_pkg: str):
    schemas_root = repo_dir / "composeApp" / "schemas"
    if not schemas_root.exists():
        return
    old_name = f"{old_pkg}.database.SagerDatabase"
    new_name = f"{new_pkg}.database.SagerDatabase"
    old_dir = schemas_root / old_name
    new_dir = schemas_root / new_name
    if old_dir.exists():
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        if new_dir.exists():
            shutil.rmtree(new_dir)
        shutil.move(str(old_dir), str(new_dir))


def replace_icons(module_dir: Path, decoded_vivo: Path, vivo_icon_ref: Path):
    vivo_type, vivo_name = str(vivo_icon_ref).lstrip("@").split("/", 1)
    res_dir = None
    for p in module_dir.parent.rglob("res"):
        if p.is_dir() and list(p.glob("mipmap*/ic_launcher*")):
            res_dir = p
            break
    if res_dir is None:
        raise RuntimeError("未找到包含 ic_launcher 的资源目录")
    copied = 0
    source_icons = list((decoded_vivo / "res").glob(f"{vivo_type}*/{vivo_name}.*"))
    if not source_icons:
        raise RuntimeError("Vivo APK 中未找到可用 launcher 图标资源")
    for target in res_dir.glob("mipmap*/ic_launcher*"):
        density = target.parent.name
        same_suffix = [c for c in source_icons if c.suffix == target.suffix]
        if not same_suffix:
            continue
        preferred_pool = same_suffix
        preferred = [c for c in preferred_pool if c.parent.name == density]
        src = preferred[0] if preferred else preferred_pool[0]
        if src:
            shutil.copy2(src, target)
            copied += 1
    if copied == 0:
        print("WARN: 未替换到任何 ic_launcher 文件（可能项目使用自定义矢量图标资源）")
        return
    print(f"已替换图标文件 {copied} 个")


def compute_version_code(old_code: int, offset: int, min_code: int) -> int:
    return max(min_code, old_code + offset)


def build_unsigned_apk(repo_dir: Path, module_dir: Path) -> Path:
    gradlew = repo_dir / "gradlew"
    if not gradlew.exists():
        raise RuntimeError("源码仓库缺少 gradlew，无法构建")
    run(["chmod", "+x", str(gradlew)])
    module_name = module_dir.relative_to(repo_dir).parts[0]
    run(["./gradlew", f":{module_name}:assembleRelease"], cwd=repo_dir)
    candidates = list(module_dir.glob("build/outputs/apk/release/*.apk"))
    if not candidates:
        raise RuntimeError("未找到 release APK 输出")
    unsigned = next((c for c in candidates if "unsigned" in c.name), candidates[0])
    return unsigned


def ensure_android_sdk_location(repo_dir: Path):
    candidates = [
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        "/usr/lib/android-sdk",
        "/opt/android-sdk",
    ]
    sdk_dir = next((c for c in candidates if c and Path(c).exists()), None)
    if not sdk_dir:
        return
    local_props = repo_dir / "local.properties"
    text = local_props.read_text(encoding="utf-8") if local_props.exists() else ""
    lines = [line for line in text.splitlines() if not line.startswith("sdk.dir=")]
    lines.append(f"sdk.dir={sdk_dir}")
    local_props.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_desktop_placeholder_jar(repo_dir: Path):
    libs_dir = repo_dir / "composeApp" / "libs"
    libs_dir.mkdir(parents=True, exist_ok=True)
    jar = libs_dir / "libcore-desktop-linux-amd64.jar"
    if not jar.exists():
        with zipfile.ZipFile(jar, "w"):
            pass


def sign_with_uber(unsigned_apk: Path, workdir: Path, outdir: Path, version_code: int) -> Path:
    signer_jar = workdir / "uber-apk-signer.jar"
    download_file(
        "https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar",
        signer_jar,
    )
    run(["java", "-jar", str(signer_jar), "-a", str(unsigned_apk), "--overwrite"])
    signed = unsigned_apk.with_name(unsigned_apk.stem + "-aligned-debugSigned.apk")
    if not signed.exists():
        raise RuntimeError("uber-apk-signer 未生成预期签名产物")
    final_apk = outdir / f"husi-xuexi-cn.xuexi.android-vc{version_code}.apk"
    shutil.move(signed, final_apk)
    return final_apk


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
    props_file = repo_dir / "husi.properties"
    if props_file.exists():
        props_text = props_file.read_text(encoding="utf-8")
        old_version_code = int(read_first_match(props_text, [r"^VERSION_CODE=(\d+)$"], "VERSION_CODE"))
    else:
        gradle_text = gradle_file.read_text(encoding="utf-8")
        old_version_code = int(
            read_first_match(gradle_text, [r"versionCode\s+(\d+)", r"versionCode\s*=\s*(\d+)"], "versionCode")
        )
    new_version_code = compute_version_code(old_version_code, args.version_offset, args.min_version_code)

    old_pkg = update_husi_properties(repo_dir, args.package_name, new_version_code)
    if not old_pkg:
        old_pkg = update_gradle_identifiers(gradle_file, args.package_name, new_version_code)
    move_source_package_dirs(repo_dir, old_pkg, args.package_name)
    move_schema_dirs(repo_dir, old_pkg, args.package_name)
    replace_package_references(repo_dir, old_pkg, args.package_name)
    replace_app_name(repo_dir, args.app_name)

    try:
        vivo_info = fetch_latest_vivo_info(args.vivo_app_id)
        vivo_apk = workdir / f"xuexi-{vivo_info['version_code']}.apk"
        download_file(vivo_info["download_url"], vivo_apk)
        decoded_vivo = workdir / "decoded-vivo"
        vivo_icon_ref = decode_vivo_icon(vivo_apk, decoded_vivo)
        replace_icons(module_dir, decoded_vivo, vivo_icon_ref)
    except Exception as e:
        print(f"WARN: 图标替换失败，继续构建: {e}")

    ensure_android_sdk_location(repo_dir)
    ensure_desktop_placeholder_jar(repo_dir)
    unsigned = build_unsigned_apk(repo_dir, module_dir)
    final_signed = sign_with_uber(unsigned, workdir, outdir, new_version_code)
    print(json.dumps({"apk": str(final_signed), "version_code": new_version_code}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
