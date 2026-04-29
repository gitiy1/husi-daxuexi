# husi-daxuexi

自动将 `xchacha20-poly1305/husi` **源码**拉取后直接编译为 APK（不走 husi APK 解包/smali）：

- 包名：`cn.xuexi.android`
- 包名：通过 husi 官方 `./run rename` 流程改为 `cn.xuexi.android`（避免手工全局替换导致构建异常）
- 应用名：`学习强国`
- 应用图标：替换为 Vivo 应用商店「学习强国」最新版 APK 图标
- `versionCode`：按 `husi 原始 versionCode + 10000` 生成（且至少 `10001`）

## 运行方式

使用 GitHub Actions `Build Husi From Source` 手动触发。

构建成功后会自动使用 `uber-apk-signer` 进行一键签名，并发布到 GitHub Releases。
构建流程按 husi README 的官方顺序执行：`make libcore_android` → `make assets` → `make aboutlibraries_go` → `make aboutlibraries_android` → `make apk`。

## 本地调试

```bash
python3 scripts/repack_husi.py --vivo-app-id 2407867 --min-version-code 10001 --version-offset 10000
```
