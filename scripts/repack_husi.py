#!/usr/bin/env python3
"""
将 Codeberg 上的 husi APK 重打包为:
- 包名: cn.xuexi.android
- 应用名: 学习强国
- 图标: 替换为 Vivo 应用商店「学习强国」最新版 APK 的图标

并将 versionCode 设为 husi 原始 versionCode + 10000（且最小不低于 min-version-code）。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

CODEBERG_LATEST_RELEASE_API = (
    "https://codeberg.org/api/v1/repos/xchacha20-poly1305/husi/releases/latest"
)
VIVO_API_BASE = "https://h5-api.appstore.vivo.com.cn"
VIVO_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)
DEFAULT_VIVO_APP_ID = "2407867"  # 学习强国，必要时可通过参数覆盖


def run(cmd, cwd=None):
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=cwd)


def fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download_file(url: str, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".downloading")
    req = urllib.request.Request(url, headers={"User-Agent": VIVO_USER_AGENT})
    with urllib.request.urlopen(req, timeout=600) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.rename(out_path)


def fetch_latest_husi_apk(workdir: Path) -> Path:
    data = fetch_json(CODEBERG_LATEST_RELEASE_API)
    assets = data.get("assets") or []
    apk_asset = None
    for a in assets:
        name = a.get("name", "")
        if name.endswith(".apk"):
            apk_asset = a
            break
    if not apk_asset:
        raise RuntimeError("未在 husi 最新 release 中找到 APK 资产")

    url = apk_asset.get("browser_download_url") or apk_asset.get("download_url")
    if not url:
        raise RuntimeError(f"APK 资产缺少下载地址: {apk_asset}")

    out = workdir / "husi-original.apk"
    print(f"下载 husi APK: {url}")
    download_file(url, out)
    return out


def fetch_latest_vivo_info(app_id: str):
    url = f"{VIVO_API_BASE}/detail/{app_id}?frompage=messageh5&app_version=2100"
    data = fetch_json(url, headers={"User-Agent": VIVO_USER_AGENT})
    if not data.get("id"):
        raise RuntimeError(f"Vivo API 返回异常: {data}")
    return data


def fetch_latest_xuexi_apk(workdir: Path, app_id: str) -> Path:
    info = fetch_latest_vivo_info(app_id)
    url = info["download_url"]
    out = workdir / f"xuexi-{info['version_code']}.apk"
    print(f"下载学习强国 APK: {url}")
    download_file(url, out)
    return out


def read_manifest_value(manifest: Path, pattern: str, desc: str):
    text = manifest.read_text(encoding="utf-8")
    m = re.search(pattern, text)
    if not m:
        raise RuntimeError(f"未找到 {desc}")
    return m.group(1)


def replace_manifest(manifest: Path, package_name: str, app_name: str, version_code: int):
    text = manifest.read_text(encoding="utf-8")
    text = re.sub(r'package="[^"]+"', f'package="{package_name}"', text, count=1)
    text = re.sub(
        r'android:versionCode="\d+"',
        f'android:versionCode="{version_code}"',
        text,
        count=1,
    )

    if 'android:label="' in text:
        text = re.sub(r'android:label="[^"]*"', f'android:label="{app_name}"', text, count=1)
    else:
        text = text.replace("<application ", f"<application android:label=\"{app_name}\" ", 1)

    manifest.write_text(text, encoding="utf-8")


def copy_icons(from_res: Path, to_res: Path, source_icon_ref: str, target_icon_ref: str):
    # @mipmap/ic_launcher -> mipmap, ic_launcher
    s_type, s_name = source_icon_ref.removeprefix("@").split("/", 1)
    t_type, t_name = target_icon_ref.removeprefix("@").split("/", 1)

    copied = 0
    for src_file in from_res.glob(f"{s_type}*/{s_name}.*"):
        folder = src_file.parent.name
        dst_dir = to_res / folder.replace(s_type, t_type, 1)
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst_file = dst_dir / f"{t_name}{src_file.suffix}"
        shutil.copy2(src_file, dst_file)
        copied += 1

    if copied == 0:
        raise RuntimeError(
            f"未复制到任何图标文件，source_icon_ref={source_icon_ref}, target_icon_ref={target_icon_ref}"
        )
    print(f"已复制图标文件 {copied} 个")


def compute_version_code(husi_version_code: int, offset: int = 10000, min_code: int = 10001) -> int:
    return max(min_code, husi_version_code + offset)


def main():
    p = argparse.ArgumentParser(description="重打包 husi 为学习强国样式 APK")
    p.add_argument("--workdir", default="build/work", help="工作目录")
    p.add_argument("--outdir", default="dist", help="输出目录")
    p.add_argument("--package-name", default="cn.xuexi.android")
    p.add_argument("--app-name", default="学习强国")
    p.add_argument("--vivo-app-id", default=os.environ.get("VIVO_APP_ID", DEFAULT_VIVO_APP_ID))
    p.add_argument("--min-version-code", type=int, default=10001)
    p.add_argument("--version-offset", type=int, default=10000, help="最终 versionCode = husi versionCode + 该偏移量")
    args = p.parse_args()

    workdir = Path(args.workdir)
    outdir = Path(args.outdir)
    decoded_husi = workdir / "decoded-husi"
    decoded_xuexi = workdir / "decoded-xuexi"

    shutil.rmtree(workdir, ignore_errors=True)
    outdir.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)

    husi_apk = fetch_latest_husi_apk(workdir)
    xuexi_apk = fetch_latest_xuexi_apk(workdir, args.vivo_app_id)

    run(["apktool", "d", "-f", str(husi_apk), "-o", str(decoded_husi)])
    run(["apktool", "d", "-f", str(xuexi_apk), "-o", str(decoded_xuexi)])

    husi_manifest = decoded_husi / "AndroidManifest.xml"
    xuexi_manifest = decoded_xuexi / "AndroidManifest.xml"

    husi_icon = read_manifest_value(husi_manifest, r'android:icon="([^"]+)"', "husi icon")
    xuexi_icon = read_manifest_value(xuexi_manifest, r'android:icon="([^"]+)"', "学习强国 icon")

    copy_icons(
        from_res=decoded_xuexi / "res",
        to_res=decoded_husi / "res",
        source_icon_ref=xuexi_icon,
        target_icon_ref=husi_icon,
    )

    husi_version_code = int(
        read_manifest_value(husi_manifest, r'android:versionCode="(\d+)"', "husi versionCode")
    )
    vc = compute_version_code(
        husi_version_code=husi_version_code,
        offset=args.version_offset,
        min_code=args.min_version_code,
    )
    replace_manifest(husi_manifest, args.package_name, args.app_name, vc)

    unsigned_apk = outdir / f"husi-xuexi-unsigned-vc{vc}.apk"
    run(["apktool", "b", str(decoded_husi), "-o", str(unsigned_apk)])

    signer_jar = workdir / "uber-apk-signer.jar"
    download_file(
        "https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar",
        signer_jar,
    )
    run(["java", "-jar", str(signer_jar), "-a", str(unsigned_apk), "--overwrite"])

    signed_apk = unsigned_apk.with_name(unsigned_apk.stem + "-aligned-debugSigned.apk")
    final_apk = outdir / f"husi-xuexi-cn.xuexi.android-vc{vc}.apk"
    shutil.move(signed_apk, final_apk)

    print(f"输出 APK: {final_apk}")
    print(json.dumps({"apk": str(final_apk), "version_code": vc}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
