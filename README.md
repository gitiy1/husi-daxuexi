# husi-daxuexi

自动将 `xchacha20-poly1305/husi` 最新 APK 重打包为：

- 包名：`cn.xuexi.android`
- 应用名：`学习强国`
- 应用图标：替换为 Vivo 应用商店「学习强国」最新版 APK 图标
- `versionCode`：按 `husi 原始 versionCode + 10000` 生成（且至少 `10001`）

## 运行方式

使用 GitHub Actions `Build Repacked Husi` 手动触发。

构建成功后会自动发布到 GitHub Releases，并上传 APK 产物。

## 本地调试

```bash
python3 scripts/repack_husi.py --vivo-app-id 2407867 --min-version-code 10001 --version-offset 10000
```
