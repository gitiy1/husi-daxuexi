#!/usr/bin/env python3
"""
按 husi 官方 README 的构建顺序，从源码构建 APK，并做品牌定制：
- 本体默认伪装 Vivo 学习强国（2407867），插件默认伪装 Vivo app id 284567
- packageName/app name/versionCode 尽量从 Vivo 接口自动获取，也支持命令行覆盖
- 图标替换为 Vivo 目标 APK/图标资源，并生成 Android launcher PNG
- 使用 uber-apk-signer 一键签名
"""

import argparse
from dataclasses import dataclass
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
from xml.sax.saxutils import escape as xml_escape

VIVO_API_BASE = "https://h5-api.appstore.vivo.com.cn"
VIVO_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)
DEFAULT_APP_VIVO_APP_ID = "2407867"
DEFAULT_PLUGIN_VIVO_APP_ID = "284567"
HUSI_REPO = "https://codeberg.org/xchacha20-poly1305/husi"
DEFAULT_COMPILE_SDK = 37
DEFAULT_BUILD_TOOLS = "37.0.0"
DEFAULT_PROVIDER_AUTHORITY_PREFIX = "cn.xuexi.android.plugin"
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
BITMAP_ICON_SUFFIXES = {".png", ".webp", ".jpg", ".jpeg", ".avif"}
BUILD_TARGETS = ("app", "hysteria2", "juicity", "mieru", "naive", "shadowquic")
COMMON_SUBMODULES = (
    "library/DragDropSwipeLazyColumn",
    "library/compose-code-editor",
)
PLUGIN_SUBMODULES = {
    "hysteria2": ("plugin/hysteria2/src/main/go/hysteria2",),
    "juicity": ("plugin/juicity/src/main/go/juicity",),
    "mieru": ("plugin/mieru/src/main/go/mieru",),
    "naive": ("plugin/naive/src/main/jni/naiveproxy",),
    "shadowquic": ("plugin/shadowquic/src/main/rust/shadowquic",),
}
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
VIVO_TITLE_KEYS = ("title_zh", "title_en", "name", "app_name", "appName")
VIVO_PACKAGE_KEYS = ("package_name", "packageName", "pkg_name", "pkgName")
VIVO_VERSION_CODE_KEYS = ("version_code", "versionCode")
VIVO_VERSION_NAME_KEYS = ("version_name", "versionName")


@dataclass(frozen=True)
class BrandInfo:
    vivo_app_id: str
    package_name: str
    app_name: str
    version_code: int | None
    version_name: str | None
    raw: dict


def run(cmd, cwd=None, env=None):
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=cwd, env=env)


def go_env_value(name: str) -> str | None:
    try:
        value = subprocess.check_output(["go", "env", name], text=True, stderr=subprocess.DEVNULL).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return value or None


def husi_build_env() -> dict[str, str]:
    env = os.environ.copy()
    candidates: list[str] = []

    gobin = go_env_value("GOBIN")
    if gobin:
        candidates.append(gobin)

    gopath = go_env_value("GOPATH")
    if gopath:
        candidates.extend(str(Path(path) / "bin") for path in gopath.split(os.pathsep) if path)

    path_parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    prepend = [path for path in candidates if path and path not in path_parts]
    if prepend:
        env["PATH"] = os.pathsep.join(prepend + path_parts)
    return env


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


def get_first_string(data: dict, keys) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def get_first_int(data: dict, keys) -> int | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def optional_arg(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def validate_package_name(package_name: str):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+", package_name):
        raise ValueError(f"非法 Android package/applicationId: {package_name}")


def normalize_provider_authority_prefix(prefix: str | None) -> str:
    prefix = (prefix or DEFAULT_PROVIDER_AUTHORITY_PREFIX).strip().rstrip(".")
    validate_package_name(prefix)
    return prefix


def provider_authority_match_prefix(prefix: str) -> str:
    return normalize_provider_authority_prefix(prefix) + "."


def required_submodules(target: str) -> list[str]:
    submodules = list(COMMON_SUBMODULES)
    if target != "app":
        submodules.extend(PLUGIN_SUBMODULES.get(target, ()))
    return submodules


def safe_file_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return token or "apk"


def plugin_prop_prefix(plugin: str) -> str:
    return plugin.upper().replace("-", "_")


def choose_vivo_app_id(target: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    return DEFAULT_APP_VIVO_APP_ID if target == "app" else DEFAULT_PLUGIN_VIVO_APP_ID


def resolve_brand_info(
    vivo_app_id: str,
    info: dict,
    package_name_override: str | None,
    app_name_override: str | None,
) -> BrandInfo:
    package_name = package_name_override or get_first_string(info, VIVO_PACKAGE_KEYS)
    app_name = app_name_override or get_first_string(info, VIVO_TITLE_KEYS)
    if not package_name:
        raise RuntimeError(f"Vivo API 未返回 package_name，请使用 --package-name 覆盖: {info}")
    if not app_name:
        app_name = package_name
    validate_package_name(package_name)
    return BrandInfo(
        vivo_app_id=vivo_app_id,
        package_name=package_name,
        app_name=app_name,
        version_code=get_first_int(info, VIVO_VERSION_CODE_KEYS),
        version_name=get_first_string(info, VIVO_VERSION_NAME_KEYS),
        raw=info,
    )


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


def clone_husi_source(workdir: Path, target: str) -> Path:
    src = workdir / "husi-src"
    run(["git", "clone", "--depth", "1", HUSI_REPO, str(src)])
    gitmodules = src / ".gitmodules"
    if gitmodules.exists():
        t = gitmodules.read_text(encoding="utf-8")
        n = t.replace("git@github.com:", "https://github.com/")
        if n != t:
            gitmodules.write_text(n, encoding="utf-8")
            run(["git", "submodule", "sync", "--recursive"], cwd=src)
    run(["git", "submodule", "update", "--init", "--recursive", *required_submodules(target)], cwd=src)
    return src


def set_property(text: str, key: str, value: str | int) -> str:
    replacement = f"{key}={value}"
    pattern = rf"^{re.escape(key)}=.*$"
    if re.search(pattern, text, flags=re.MULTILINE):
        return re.sub(pattern, replacement, text, flags=re.MULTILINE)
    return text.rstrip() + f"\n{replacement}\n"


def read_husi_properties(props: Path):
    text = props.read_text(encoding="utf-8")
    pkg = re.search(r"^PACKAGE_NAME=(.+)$", text, re.MULTILINE).group(1).strip()
    vc = int(re.search(r"^VERSION_CODE=(\d+)$", text, re.MULTILINE).group(1))
    return pkg, vc, text


def write_husi_properties(props: Path, package_name: str, version_code: int, version_name: str | None = None):
    text = props.read_text(encoding="utf-8")
    text = set_property(text, "PACKAGE_NAME", package_name)
    text = set_property(text, "VERSION_CODE", version_code)
    if version_name:
        text = set_property(text, "VERSION_NAME", version_name)
    props.write_text(text, encoding="utf-8")


def read_plugin_version(props: Path, plugin: str) -> tuple[str | None, int]:
    text = props.read_text(encoding="utf-8")
    prefix = plugin_prop_prefix(plugin)
    version_name_match = re.search(rf"^{prefix}_VERSION_NAME=(.+)$", text, re.MULTILINE)
    version_match = re.search(rf"^{prefix}_VERSION=(\d+)$", text, re.MULTILINE)
    if not version_match:
        raise RuntimeError(f"husi.properties 中未找到 {prefix}_VERSION")
    version_name = version_name_match.group(1).strip() if version_name_match else None
    return version_name, int(version_match.group(1))


def write_plugin_properties(props: Path, plugin: str, version_code: int, version_name: str | None = None):
    text = props.read_text(encoding="utf-8")
    prefix = plugin_prop_prefix(plugin)
    text = set_property(text, f"{prefix}_VERSION", version_code)
    if version_name:
        text = set_property(text, f"{prefix}_VERSION_NAME", version_name)
    props.write_text(text, encoding="utf-8")


def replace_app_name(repo_dir: Path, app_name: str):
    for p in repo_dir.rglob("strings.xml"):
        t = p.read_text(encoding="utf-8", errors="ignore")
        n = re.sub(
            r'(<string\s+name="app_name"[^>]*>)(.*?)(</string>)',
            lambda m: f"{m.group(1)}{xml_escape(app_name)}{m.group(3)}",
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
    raise RuntimeError("未能从 Vivo APK 中解析出可用的启动图标位图")


def download_vivo_store_icon(info: dict, workdir: Path, prefix: str) -> Path | None:
    icon_url = get_first_url(info, VIVO_ICON_URL_KEYS)
    if not icon_url:
        return None
    icon_path = workdir / f"{safe_file_token(prefix)}-store-icon"
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


def install_launcher_icons(source_set_dir: Path, source_icon: Path) -> list[Path]:
    res_dir = source_set_dir / "res"
    clear_existing_launcher_icons(res_dir)

    generated = []
    for dirname, size in LAUNCHER_ICON_SIZES.items():
        icon = res_dir / dirname / "ic_launcher.png"
        round_icon = res_dir / dirname / "ic_launcher_round.png"
        convert_icon_to_png(source_icon, icon, size)
        convert_icon_to_png(source_icon, round_icon, size)
        generated.extend([icon, round_icon])

    playstore_icon = source_set_dir / "ic_launcher-playstore.png"
    convert_icon_to_png(source_icon, playstore_icon, 512)
    generated.append(playstore_icon)
    return generated


def replace_icons(source_set_dir: Path, workdir: Path, brand: BrandInfo, prefix: str):
    version_code = brand.version_code or "latest"
    icon_sources = []

    try:
        store_icon = download_vivo_store_icon(brand.raw, workdir, prefix)
        if store_icon:
            icon_sources.append(store_icon)
    except Exception as e:
        print(f"WARN: Vivo 图标资源下载失败，将尝试从 APK 提取: {e}")

    download_url = get_first_url(brand.raw, VIVO_DOWNLOAD_URL_KEYS)
    if download_url:
        try:
            vivo_apk = workdir / f"{safe_file_token(prefix)}-{version_code}.apk"
            download_file(download_url, vivo_apk)
            decoded = workdir / f"decoded-{safe_file_token(prefix)}"
            run(["apktool", "d", "-f", str(vivo_apk), "-o", str(decoded)])
            icon_sources.append(extract_apk_launcher_icon(decoded))
        except Exception as e:
            if not icon_sources:
                raise RuntimeError(f"从 Vivo APK 提取图标失败: {e}") from e
            print(f"WARN: 从 Vivo APK 提取图标失败，将使用 Vivo 图标资源: {e}")
    elif not icon_sources:
        raise RuntimeError(f"Vivo API 未返回 APK 下载地址或图标资源: {brand.raw}")

    source_icon = max(icon_sources, key=bitmap_score)

    generated = install_launcher_icons(source_set_dir, source_icon)
    print(f"INFO: 已用 {source_icon} 为 {brand.app_name} 生成 {len(generated)} 个启动图标资源")


def ensure_android_local_properties(repo_dir: Path):
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "/opt/android-sdk"
    ndk = os.environ.get("ANDROID_NDK_HOME") or os.environ.get("ANDROID_NDK_ROOT")
    lines = [f"sdk.dir={sdk}"]
    if ndk:
        lines.append(f"ndk.dir={ndk}")
    (repo_dir / "local.properties").write_text("\n".join(lines) + "\n", encoding="utf-8")


def android_sdk_dir() -> Path:
    return Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "/opt/android-sdk")


def android_platform_exists(sdk_dir: Path, api_level: int) -> bool:
    platform_dir = sdk_dir / "platforms"
    candidates = (platform_dir / f"android-{api_level}", platform_dir / f"android-{api_level}.0")
    return any((candidate / "android.jar").is_file() for candidate in candidates)


def suggested_platform_package(api_level: int) -> str:
    suffix = f"{api_level}.0" if api_level >= 37 else str(api_level)
    return f"platforms;android-{suffix}"


def require_android_sdk_platforms(compile_sdk: int):
    sdk_dir = android_sdk_dir()
    required = [23, compile_sdk]
    missing = [api for api in required if not android_platform_exists(sdk_dir, api)]
    if not missing:
        return

    packages = " ".join(f'"{suggested_platform_package(api)}"' for api in missing)
    raise RuntimeError(
        f"Android SDK 缺少平台 {', '.join(map(str, missing))}（SDK: {sdk_dir}）。"
        f"请先安装：sdkmanager --channel=3 {packages}"
    )


def patch_android_sdk_versions(repo_dir: Path, compile_sdk: int, build_tools: str):
    replacements = [
        (repo_dir / "buildSrc" / "src" / "main" / "kotlin" / "Helpers.kt", r'buildToolsVersion = "\d+(?:\.\d+)*"', f'buildToolsVersion = "{build_tools}"'),
        (repo_dir / "buildSrc" / "src" / "main" / "kotlin" / "Helpers.kt", r"compileSdk = \d+", f"compileSdk = {compile_sdk}"),
    ]
    replacements.extend(
        (path, r"compileSdk = \d+", f"compileSdk = {compile_sdk}")
        for path in repo_dir.rglob("build.gradle.kts")
    )
    for path, pattern, replacement in replacements:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        new_text = re.sub(pattern, replacement, text)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")


def patch_app_provider_authority_prefix(repo_dir: Path, provider_authority_prefix: str):
    plugins_file = repo_dir / "composeApp" / "src" / "androidMain" / "kotlin" / "fr" / "husi" / "plugin" / "Plugins.kt"
    if not plugins_file.exists():
        raise RuntimeError(f"未找到 husi 插件识别文件: {plugins_file}")
    text = plugins_file.read_text(encoding="utf-8")
    match_prefix = provider_authority_match_prefix(provider_authority_prefix)
    new_text = re.sub(
        r'const val AUTHORITIES_PREFIX_HUSI_EXE = "[^"]+"',
        f'const val AUTHORITIES_PREFIX_HUSI_EXE = "{match_prefix}"',
        text,
        count=1,
    )
    if new_text == text:
        raise RuntimeError("未能替换 AUTHORITIES_PREFIX_HUSI_EXE")
    plugins_file.write_text(new_text, encoding="utf-8")


def plugin_source_set_dir(repo_dir: Path, plugin: str) -> Path:
    path = repo_dir / "plugin" / plugin / "src" / "main"
    if not path.is_dir():
        raise RuntimeError(f"未知或不存在的插件: {plugin}")
    return path


def replace_plugin_brand(repo_dir: Path, plugin: str, brand: BrandInfo, provider_authority_prefix: str):
    plugin_dir = repo_dir / "plugin" / plugin
    build_gradle = plugin_dir / "build.gradle.kts"
    manifest = plugin_dir / "src" / "main" / "AndroidManifest.xml"

    text = build_gradle.read_text(encoding="utf-8")
    text = re.sub(
        r'applicationId\s*=\s*"[^"]+"',
        f'applicationId = "{brand.package_name}"',
        text,
        count=1,
    )
    build_gradle.write_text(text, encoding="utf-8")

    authority = f"{normalize_provider_authority_prefix(provider_authority_prefix)}.{brand.package_name}.{plugin}.BinaryProvider"
    text = manifest.read_text(encoding="utf-8")
    text = re.sub(
        r'android:label="[^"]*"',
        lambda _: f'android:label="{xml_escape(brand.app_name)}"',
        text,
        count=1,
    )
    text = re.sub(
        r'android:authorities="[^"]+"',
        f'android:authorities="{authority}"',
        text,
        count=1,
    )
    manifest.write_text(text, encoding="utf-8")


def build_apk_official_flow(repo_dir: Path):
    # 严格按 README 推荐顺序
    env = husi_build_env()
    run(["make", "libcore_android"], cwd=repo_dir, env=env)
    run(["make", "assets"], cwd=repo_dir, env=env)
    run(["make", "aboutlibraries_go"], cwd=repo_dir, env=env)
    run(["make", "aboutlibraries_android"], cwd=repo_dir, env=env)
    # README 中 desktop jar 缺失会导致 android 编译阶段失败，这里放一个最小占位 jar
    libs = repo_dir / "composeApp" / "libs"
    libs.mkdir(parents=True, exist_ok=True)
    placeholder = libs / "libcore-desktop-linux-amd64.jar"
    if not placeholder.exists():
        with zipfile.ZipFile(placeholder, "w"):
            pass
    run(["make", "apk"], cwd=repo_dir, env=env)

    return collect_output_apks(repo_dir / "androidApp" / "build" / "outputs" / "apk", "APK")


def collect_output_apks(output_dir: Path, label: str) -> list[Path]:
    apks = sorted(output_dir.rglob("*.apk"))
    if not apks:
        raise RuntimeError("未找到 APK 产物")
    release_apks = [p for p in apks if "release" in str(p).lower()]
    selected = release_apks or apks
    print(f"INFO: 找到 {len(selected)} 个 {label} 产物")
    return selected


def build_plugin_flow(repo_dir: Path, plugin: str):
    run(["make", "plugin", f"PLUGIN={plugin}"], cwd=repo_dir, env=husi_build_env())
    return collect_output_apks(repo_dir / "plugin" / plugin / "build" / "outputs" / "apk", f"{plugin} 插件 APK")


def download_uber_signer(workdir: Path) -> Path:
    jar = workdir / "uber-apk-signer.jar"
    if not jar.exists():
        download_file(
            "https://github.com/patrickfav/uber-apk-signer/releases/download/v1.3.0/uber-apk-signer-1.3.0.jar",
            jar,
        )
    return jar


def apk_variant_token(unsigned_apk: Path) -> str:
    stem = unsigned_apk.stem.replace("-unsigned", "")
    for abi in ("arm64-v8a", "armeabi-v7a", "x86_64", "x86"):
        if abi in stem:
            return abi
    return safe_file_token(stem)


def sign_with_uber(unsigned_apk: Path, signer_jar: Path, outdir: Path, version_code: int, target: str, package_name: str) -> Path:
    run(["java", "-jar", str(signer_jar), "-a", str(unsigned_apk), "--overwrite"])
    signed = unsigned_apk.with_name(unsigned_apk.stem + "-aligned-debugSigned.apk")
    if not signed.exists():
        signed = unsigned_apk
    if not signed.exists():
        raise RuntimeError("未找到 uber-apk-signer 输出")
    outdir.mkdir(parents=True, exist_ok=True)
    variant = apk_variant_token(unsigned_apk)
    final_apk = outdir / f"husi-{safe_file_token(target)}-{safe_file_token(package_name)}-{variant}-vc{version_code}.apk"
    shutil.copy2(signed, final_apk)
    return final_apk


def sign_apks_with_uber(unsigned_apks: list[Path], workdir: Path, outdir: Path, version_code: int, target: str, package_name: str) -> list[Path]:
    signer_jar = download_uber_signer(workdir)
    return [sign_with_uber(apk, signer_jar, outdir, version_code, target, package_name) for apk in unsigned_apks]


def clear_previous_outputs(outdir: Path, version_code: int, target: str, package_name: str):
    if not outdir.exists():
        return
    base = f"husi-{safe_file_token(target)}-{safe_file_token(package_name)}"
    for apk in outdir.glob(f"{base}*vc{version_code}.apk"):
        apk.unlink()


def compute_version_code(old_vc: int, offset: int, min_code: int, spoof_vc: int | None = None):
    candidates = [min_code, old_vc + offset]
    if spoof_vc:
        candidates.append(spoof_vc)
    return max(candidates)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", choices=BUILD_TARGETS, default=os.environ.get("BUILD_TARGET", "app"))
    p.add_argument("--workdir", default="build/work")
    p.add_argument("--outdir", default="dist")
    p.add_argument("--package-name", default=os.environ.get("PACKAGE_NAME"))
    p.add_argument("--app-name", default=os.environ.get("APP_NAME"))
    p.add_argument("--vivo-app-id", default=os.environ.get("VIVO_APP_ID"))
    p.add_argument("--version-offset", type=int, default=10000)
    p.add_argument("--min-version-code", type=int, default=10001)
    p.add_argument("--compile-sdk", type=int, default=DEFAULT_COMPILE_SDK)
    p.add_argument("--build-tools", default=DEFAULT_BUILD_TOOLS)
    p.add_argument(
        "--provider-authority-prefix",
        default=os.environ.get("PROVIDER_AUTHORITY_PREFIX", DEFAULT_PROVIDER_AUTHORITY_PREFIX),
        help=f"插件 provider authority 前缀；默认 {DEFAULT_PROVIDER_AUTHORITY_PREFIX}",
    )
    p.add_argument(
        "--allow-icon-fallback",
        action="store_true",
        help="图标替换失败时继续构建；默认失败退出，避免发布旧图标",
    )
    args = p.parse_args()
    args.package_name = optional_arg(args.package_name)
    args.app_name = optional_arg(args.app_name)
    args.vivo_app_id = choose_vivo_app_id(args.target, optional_arg(args.vivo_app_id))
    args.provider_authority_prefix = normalize_provider_authority_prefix(args.provider_authority_prefix)

    workdir = Path(args.workdir)
    outdir = Path(args.outdir)
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)

    require_android_sdk_platforms(args.compile_sdk)

    repo = clone_husi_source(workdir, args.target)
    patch_android_sdk_versions(repo, args.compile_sdk, args.build_tools)

    info = fetch_vivo_info(args.vivo_app_id)
    brand = resolve_brand_info(args.vivo_app_id, info, args.package_name, args.app_name)

    props = repo / "husi.properties"
    ensure_android_local_properties(repo)

    if args.target == "app":
        old_pkg, old_vc, _ = read_husi_properties(props)
        new_vc = compute_version_code(old_vc, args.version_offset, args.min_version_code, brand.version_code)
        write_husi_properties(props, brand.package_name, new_vc, brand.version_name)
        patch_app_provider_authority_prefix(repo, args.provider_authority_prefix)

        # 使用官方 rename 流程（README 提供）
        run(["./run", "rename", brand.package_name], cwd=repo)
        replace_app_name(repo, brand.app_name)
        try:
            replace_icons(repo / "composeApp" / "src" / "androidMain", workdir, brand, args.target)
        except Exception as e:
            if args.allow_icon_fallback:
                print(f"WARN: 图标替换失败，将继续构建: {e}")
            else:
                raise RuntimeError(f"图标替换失败，已停止构建以避免发布旧图标: {e}") from e
        unsigned_apks = build_apk_official_flow(repo)
        old_package = old_pkg
    else:
        _, old_vc = read_plugin_version(props, args.target)
        new_vc = compute_version_code(old_vc, args.version_offset, args.min_version_code, brand.version_code)
        write_plugin_properties(props, args.target, new_vc, brand.version_name)
        replace_plugin_brand(repo, args.target, brand, args.provider_authority_prefix)
        try:
            replace_icons(plugin_source_set_dir(repo, args.target), workdir, brand, args.target)
        except Exception as e:
            if args.allow_icon_fallback:
                print(f"WARN: 图标替换失败，将继续构建: {e}")
            else:
                raise RuntimeError(f"图标替换失败，已停止构建以避免发布旧图标: {e}") from e
        unsigned_apks = build_plugin_flow(repo, args.target)
        old_package = f"fr.husi.plugin.{args.target}"

    clear_previous_outputs(outdir, new_vc, args.target, brand.package_name)
    signed_apks = sign_apks_with_uber(unsigned_apks, workdir, outdir, new_vc, args.target, brand.package_name)
    print(
        json.dumps(
            {
                "apk": str(signed_apks[0]),
                "apks": [str(apk) for apk in signed_apks],
                "target": args.target,
                "vivo_app_id": brand.vivo_app_id,
                "package_name": brand.package_name,
                "app_name": brand.app_name,
                "version_code": new_vc,
                "vivo_version_code": brand.version_code,
                "provider_authority_prefix": args.provider_authority_prefix,
                "old_package": old_package,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
