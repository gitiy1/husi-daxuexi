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
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

VIVO_API_BASE = "https://h5-api.appstore.vivo.com.cn"
VIVO_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)
DEFAULT_VIVO_APP_ID = "2407867"
HUSI_REPO = "https://codeberg.org/xchacha20-poly1305/husi"
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
BITMAP_ICON_SUFFIXES = {".png", ".webp", ".jpg", ".jpeg", ".avif"}
LAUNCHER_ICON_SIZES = {
    "mipmap-mdpi": 48,
    "mipmap-hdpi": 72,
    "mipmap-xhdpi": 96,
    "mipmap-xxhdpi": 144,
    "mipmap-xxxhdpi": 192,
}
DENSITY_RANK = {
    "ldpi": 1,
    "mdpi": 2,
    "hdpi": 3,
    "xhdpi": 4,
    "xxhdpi": 5,
    "xxxhdpi": 6,
    "nodpi": 7,
}
VIVO_ICON_URL_KEYS = (
    "icon",
    "icon_url",
    "iconUrl",
    "logo",
    "logo_url",
    "logoUrl",
    "app_icon",
    "appIcon",
)
VIVO_DOWNLOAD_URL_KEYS = ("download_url", "downloadUrl", "apk_url", "apkUrl", "download")


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


def get_first_url(data: dict, keys) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
        if isinstance(value, dict):
            for nested_key in ("url", "download_url", "downloadUrl"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, str) and nested_value.startswith(("http://", "https://")):
                    return nested_value
    return None


def find_imagemagick() -> str:
    tool = shutil.which("magick") or shutil.which("convert")
    if not tool:
        raise RuntimeError("未找到 ImageMagick（magick/convert），无法生成 Android 启动图标")
    return tool


def image_dimensions(path: Path) -> tuple[int, int]:
    if not path.is_file():
        return 0, 0
    magick = shutil.which("magick")
    identify = shutil.which("identify")
    if magick:
        cmd = [magick, "identify", "-format", "%w %h", str(path)]
    elif identify:
        cmd = [identify, "-format", "%w %h", str(path)]
    else:
        return 0, 0
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
        width, height = out.split()
        return int(width), int(height)
    except Exception:
        return 0, 0


def density_rank(path: Path) -> int:
    parent = path.parent.name.lower()
    for density, rank in sorted(DENSITY_RANK.items(), key=lambda item: len(item[0]), reverse=True):
        if density in parent:
            return rank
    return 0


def bitmap_score(path: Path) -> tuple[int, int, int]:
    width, height = image_dimensions(path)
    return width * height, density_rank(path), path.stat().st_size


def convert_icon_to_png(source: Path, target: Path, size: int):
    target.parent.mkdir(parents=True, exist_ok=True)
    tool = find_imagemagick()
    run(
        [
            tool,
            str(source),
            "-auto-orient",
            "-resize",
            f"{size}x{size}",
            "-background",
            "none",
            "-gravity",
            "center",
            "-extent",
            f"{size}x{size}",
            "-strip",
            f"PNG32:{target}",
        ]
    )


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


def parse_resource_ref(ref: str) -> tuple[str, str] | None:
    if not ref or not ref.startswith("@") or ref.startswith("@android:"):
        return None
    value = ref[1:]
    if ":" in value:
        value = value.split(":", 1)[1]
    if "/" not in value:
        return None
    res_type, name = value.split("/", 1)
    if not res_type or not name:
        return None
    return res_type, name


def resource_candidates(decoded_dir: Path, ref: str) -> list[Path]:
    parsed = parse_resource_ref(ref)
    if not parsed:
        return []
    res_type, name = parsed
    return list((decoded_dir / "res").glob(f"{res_type}*/{name}.*"))


def best_bitmap(candidates: list[Path]) -> Path | None:
    bitmaps = [p for p in candidates if p.suffix.lower() in BITMAP_ICON_SUFFIXES and p.is_file()]
    if not bitmaps:
        return None
    return max(bitmaps, key=bitmap_score)


def referenced_drawables(xml_path: Path) -> list[str]:
    refs = []
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return refs
    for elem in root.iter():
        for attr in (f"{ANDROID_NS}drawable", f"{ANDROID_NS}src"):
            value = elem.attrib.get(attr)
            if value and value.startswith("@"):
                refs.append(value)
    return refs


def resolve_bitmap_resource(decoded_dir: Path, ref: str, seen: set[str] | None = None) -> Path | None:
    seen = seen or set()
    if ref in seen:
        return None
    seen.add(ref)

    candidates = resource_candidates(decoded_dir, ref)
    direct = best_bitmap(candidates)
    if direct:
        return direct

    for xml_path in sorted((p for p in candidates if p.suffix.lower() == ".xml"), key=density_rank, reverse=True):
        for child_ref in referenced_drawables(xml_path):
            resolved = resolve_bitmap_resource(decoded_dir, child_ref, seen)
            if resolved:
                return resolved
    return None


def extract_apk_launcher_icon(decoded_dir: Path) -> Path:
    manifest = (decoded_dir / "AndroidManifest.xml").read_text(encoding="utf-8")
    icon_ref = re.search(r'android:icon="([^"]+)"', manifest)
    if icon_ref:
        resolved = resolve_bitmap_resource(decoded_dir, icon_ref.group(1))
        if resolved:
            return resolved

    fallback = best_bitmap(
        list((decoded_dir / "res").glob("mipmap*/ic_launcher.*"))
        + list((decoded_dir / "res").glob("drawable*/ic_launcher.*"))
    )
    if fallback:
        return fallback
    raise RuntimeError("未能从学习强国 APK 中解析出可用的启动图标位图")


def download_vivo_store_icon(info: dict, workdir: Path) -> Path | None:
    icon_url = get_first_url(info, VIVO_ICON_URL_KEYS)
    if not icon_url:
        return None
    icon_path = workdir / "xuexi-store-icon"
    suffix = Path(icon_url.split("?", 1)[0]).suffix
    if suffix.lower() in BITMAP_ICON_SUFFIXES:
        icon_path = icon_path.with_suffix(suffix)
    else:
        icon_path = icon_path.with_suffix(".png")
    download_file(icon_url, icon_path)
    if image_dimensions(icon_path) == (0, 0):
        return None
    return icon_path


def clear_existing_launcher_icons(res_dir: Path):
    for target in res_dir.glob("mipmap*/ic_launcher*.*"):
        target.unlink()


def install_launcher_icons(repo_dir: Path, source_icon: Path) -> list[Path]:
    res_dir = repo_dir / "composeApp" / "src" / "androidMain" / "res"
    clear_existing_launcher_icons(res_dir)

    generated = []
    for dirname, size in LAUNCHER_ICON_SIZES.items():
        icon = res_dir / dirname / "ic_launcher.png"
        round_icon = res_dir / dirname / "ic_launcher_round.png"
        convert_icon_to_png(source_icon, icon, size)
        convert_icon_to_png(source_icon, round_icon, size)
        generated.extend([icon, round_icon])

    playstore_icon = repo_dir / "composeApp" / "src" / "androidMain" / "ic_launcher-playstore.png"
    convert_icon_to_png(source_icon, playstore_icon, 512)
    generated.append(playstore_icon)
    return generated


def replace_icons(repo_dir: Path, workdir: Path, vivo_app_id: str):
    info = fetch_vivo_info(vivo_app_id)
    version_code = info.get("version_code") or info.get("versionCode") or "latest"
    icon_sources = []

    try:
        store_icon = download_vivo_store_icon(info, workdir)
        if store_icon:
            icon_sources.append(store_icon)
    except Exception as e:
        print(f"WARN: Vivo 图标资源下载失败，将尝试从 APK 提取: {e}")

    download_url = get_first_url(info, VIVO_DOWNLOAD_URL_KEYS)
    if download_url:
        try:
            vivo_apk = workdir / f"xuexi-{version_code}.apk"
            download_file(download_url, vivo_apk)
            decoded = workdir / "decoded-vivo"
            run(["apktool", "d", "-f", str(vivo_apk), "-o", str(decoded)])
            icon_sources.append(extract_apk_launcher_icon(decoded))
        except Exception as e:
            if not icon_sources:
                raise RuntimeError(f"从学习强国 APK 提取图标失败: {e}") from e
            print(f"WARN: 从学习强国 APK 提取图标失败，将使用 Vivo 图标资源: {e}")
    elif not icon_sources:
        raise RuntimeError(f"Vivo API 未返回 APK 下载地址或图标资源: {info}")

    source_icon = max(icon_sources, key=bitmap_score)

    generated = install_launcher_icons(repo_dir, source_icon)
    print(f"INFO: 已用 {source_icon} 生成 {len(generated)} 个学习强国启动图标资源")


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
    p.add_argument(
        "--allow-icon-fallback",
        action="store_true",
        help="图标替换失败时继续构建；默认失败退出，避免发布旧 husi 图标",
    )
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
        if args.allow_icon_fallback:
            print(f"WARN: 图标替换失败，将继续构建: {e}")
        else:
            raise RuntimeError(f"图标替换失败，已停止构建以避免发布旧 husi 图标: {e}") from e

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
