#!/usr/bin/env python3
"""
按 husi 官方 README 的构建顺序，从源码构建 APK，并做品牌定制：
- PACKAGE_NAME -> cn.xuexi.android
- app_name -> 学习强国
- versionCode -> 原 VERSION_CODE + 10000（最小 10001）
- 图标尝试替换为 Vivo 学习强国 APK 图标
- 使用 uber-apk-signer 一键签名
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

VIVO_API_BASE = "https://h5-api.appstore.vivo.com.cn"
VIVO_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)
DEFAULT_VIVO_APP_ID = "2407867"
HUSI_REPO = "https://codeberg.org/xchacha20-poly1305/husi"


def run(cmd, cwd=None, env=None):
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=cwd, env=env)


def fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_file(url: str, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": VIVO_USER_AGENT}
    if "vivo.com.cn" in url:
        headers["Referer"] = "https://appstore.vivo.com.cn/"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=600) as resp, open(out_path, "wb") as f:
        shutil.copyfileobj(resp, f)


def clone_husi_source(workdir: Path) -> Path:
    src = workdir / "husi-src"
    run(["git", "clone", "--depth", "1", HUSI_REPO, str(src)])
    gitmodules = src / ".gitmodules"
    if gitmodules.exists():
        t = gitmodules.read_text(encoding="utf-8")
        n = t.replace("git@github.com:", "https://github.com/")
        if n != t:
            gitmodules.write_text(n, encoding="utf-8")
            run(["git", "submodule", "sync", "--recursive"], cwd=src)
    run(["git", "submodule", "update", "--init", "--recursive"], cwd=src)
    return src


def read_husi_properties(props: Path):
    text = props.read_text(encoding="utf-8")
    pkg = re.search(r"^PACKAGE_NAME=(.+)$", text, re.MULTILINE).group(1).strip()
    vc = int(re.search(r"^VERSION_CODE=(\d+)$", text, re.MULTILINE).group(1))
    return pkg, vc, text


def write_husi_properties(props: Path, package_name: str, version_code: int):
    text = props.read_text(encoding="utf-8")
    text = re.sub(r"^PACKAGE_NAME=.*$", f"PACKAGE_NAME={package_name}", text, flags=re.MULTILINE)
    text = re.sub(r"^VERSION_CODE=.*$", f"VERSION_CODE={version_code}", text, flags=re.MULTILINE)
    props.write_text(text, encoding="utf-8")


def replace_app_name(repo_dir: Path, app_name: str):
    for p in repo_dir.rglob("strings.xml"):
        t = p.read_text(encoding="utf-8", errors="ignore")
        n = re.sub(
            r'(<string\s+name="app_name"[^>]*>)(.*?)(</string>)',
            rf"\1{app_name}\3",
            t,
            flags=re.DOTALL,
        )
        if n != t:
            p.write_text(n, encoding="utf-8")


def fetch_vivo_info(app_id: str):
    url = f"{VIVO_API_BASE}/detail/{app_id}?frompage=messageh5&app_version=2100"
    data = fetch_json(url, headers={"User-Agent": VIVO_USER_AGENT})
    if not data.get("id"):
        raise RuntimeError(f"Vivo API 返回异常: {data}")
    return data


def replace_icons(repo_dir: Path, workdir: Path, vivo_app_id: str):
    info = fetch_vivo_info(vivo_app_id)
    vivo_apk = workdir / f"xuexi-{info['version_code']}.apk"
    download_file(info["download_url"], vivo_apk)
    decoded = workdir / "decoded-vivo"
    run(["apktool", "d", "-f", str(vivo_apk), "-o", str(decoded)])

    manifest = (decoded / "AndroidManifest.xml").read_text(encoding="utf-8")
    icon_ref = re.search(r'android:icon="([^"]+)"', manifest)
    if not icon_ref:
        return
    icon_type, icon_name = icon_ref.group(1).removeprefix("@").split("/", 1)
    src_icons = list((decoded / "res").glob(f"{icon_type}*/{icon_name}.*"))
    if not src_icons:
        return

    for target in (repo_dir / "composeApp" / "src" / "androidMain" / "res").glob("mipmap*/ic_launcher*"):
        same_suffix = [s for s in src_icons if s.suffix == target.suffix]
        if not same_suffix:
            continue
        shutil.copy2(same_suffix[0], target)


def ensure_android_local_properties(repo_dir: Path):
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "/opt/android-sdk"
    ndk = os.environ.get("ANDROID_NDK_HOME") or os.environ.get("ANDROID_NDK_ROOT")
    lines = [f"sdk.dir={sdk}"]
    if ndk:
        lines.append(f"ndk.dir={ndk}")
    (repo_dir / "local.properties").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_apk_official_flow(repo_dir: Path):
    # 严格按 README 推荐顺序
    run(["make", "libcore_android"], cwd=repo_dir)
    run(["make", "assets"], cwd=repo_dir)
    run(["make", "aboutlibraries_go"], cwd=repo_dir)
    run(["make", "aboutlibraries_android"], cwd=repo_dir)
    # README 中 desktop jar 缺失会导致 android 编译阶段失败，这里放一个最小占位 jar
    libs = repo_dir / "composeApp" / "libs"
    libs.mkdir(parents=True, exist_ok=True)
    placeholder = libs / "libcore-desktop-linux-amd64.jar"
    if not placeholder.exists():
        with zipfile.ZipFile(placeholder, "w"):
            pass
    run(["make", "apk"], cwd=repo_dir)

    apks = list((repo_dir / "androidApp" / "build" / "outputs" / "apk").rglob("*.apk"))
    if not apks:
        raise RuntimeError("未找到 APK 产物")
    # 优先 release
    for p in apks:
        if "release" in str(p).lower():
            return p
    return apks[0]


def sign_with_uber(unsigned_apk: Path, workdir: Path, outdir: Path, version_code: int) -> Path:
    jar = workdir / "uber-apk-signer.jar"
    download_file(
        "https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar",
        jar,
    )
    run(["java", "-jar", str(jar), "-a", str(unsigned_apk), "--overwrite"])
    signed = unsigned_apk.with_name(unsigned_apk.stem + "-aligned-debugSigned.apk")
    if not signed.exists():
        signed = unsigned_apk
    if not signed.exists():
        raise RuntimeError("未找到 uber-apk-signer 输出")
    outdir.mkdir(parents=True, exist_ok=True)
    final_apk = outdir / f"husi-xuexi-cn.xuexi.android-vc{version_code}.apk"
    shutil.copy2(signed, final_apk)
    return final_apk


def compute_version_code(old_vc: int, offset: int, min_code: int):
    return max(min_code, old_vc + offset)


def main():
    p = argparse.ArgumentParser()
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

    repo = clone_husi_source(workdir)
    props = repo / "husi.properties"
    old_pkg, old_vc, _ = read_husi_properties(props)
    new_vc = compute_version_code(old_vc, args.version_offset, args.min_version_code)
    write_husi_properties(props, args.package_name, new_vc)

    # 使用官方 rename 流程（README 提供）
    run(["./run", "rename", args.package_name], cwd=repo)
    replace_app_name(repo, args.app_name)
    try:
        replace_icons(repo, workdir, args.vivo_app_id)
    except Exception as e:
        print(f"WARN: 图标替换失败: {e}")

    ensure_android_local_properties(repo)
    unsigned = build_apk_official_flow(repo)
    signed = sign_with_uber(unsigned, workdir, outdir, new_vc)
    print(json.dumps({"apk": str(signed), "version_code": new_vc, "old_package": old_pkg}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
