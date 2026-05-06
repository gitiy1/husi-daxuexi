# husi-daxuexi

自动将 `xchacha20-poly1305/husi` **源码**拉取后直接编译为 APK（不走 husi APK 解包/smali）：

- 默认构建 husi 本体，也可以在 GitHub Actions 里选择构建 `hysteria2`、`juicity`、`mieru`、`naive`、`shadowquic` 插件
- 本体默认伪装 Vivo 应用商店 app id `2407867`（学习强国），插件默认伪装 app id `284567`
- 包名、应用名、图标、`versionCode`/`versionName` 会从 Vivo 接口自动获取，也可以在 Action 输入里覆盖
- 本体包名仍通过 husi 官方 `./run rename` 流程修改（避免手工全局替换导致构建异常）
- 插件 provider authority 前缀默认改为 `cn.xuexi.android.plugin`，Action 中可覆盖；本体会同步修改 husi 的插件识别前缀
- 本体默认只识别当前构建设置的插件前缀；如需兼容 SagerNet/Matsuri/dyhkwong 等旧插件前缀，可在 Action 中打开兼容开关
- 专家模式下的“自定义插件前缀”输入框默认显示当前构建设置的插件前缀，例如 `cn.xuexi.android.plugin.`；如果手动改为 `fr.husi.plugin.` 并重启应用，就会恢复检测 husi 官方插件
- 图标会从 Vivo APK/图标资源提取，并重新生成 Android launcher PNG
- 构建时只拉取当前目标需要的 husi 子模块，本体不会额外拉取所有插件源码

## 运行方式

使用 GitHub Actions `Build Husi From Source` 手动触发。

构建成功后会自动使用 `uber-apk-signer` 进行一键签名，并发布到 GitHub Releases。
图标替换失败时构建会直接失败，避免发布仍带 husi 原图标的 APK。
GitHub Action 使用 `sdkmanager --channel=3` 安装 `platforms;android-23`、`platforms;android-37.0`、`build-tools;36.0.0` 和 `build-tools;37.0.0`：API 23 供 husi/anja 生成 libcore AAR，API 37 满足 husi 当前依赖的 AAR metadata 要求，Build Tools 36/37 覆盖 Gradle/AGP 当前会用到的工具版本。
本体构建流程按 husi README 的官方顺序执行：`make libcore_android` → `make assets` → `make aboutlibraries_go` → `make aboutlibraries_android` → `make apk`。
插件构建使用 husi 的 `make plugin PLUGIN=<插件名>` 流程。

## 本地调试

需要本机可用 `apktool`、`magick`/`convert`（ImageMagick）和 Android/Go/JDK 构建环境。Android SDK 至少需要 `platforms;android-23`、`platforms;android-37.0`、`build-tools;36.0.0` 和 `build-tools;37.0.0`。

```bash
python3 scripts/repack_husi.py --target app --vivo-app-id 2407867 --provider-authority-prefix cn.xuexi.android.plugin --min-version-code 10001 --version-offset 10000
python3 scripts/repack_husi.py --target hysteria2 --vivo-app-id 284567 --provider-authority-prefix cn.xuexi.android.plugin --min-version-code 10001 --version-offset 10000
```
