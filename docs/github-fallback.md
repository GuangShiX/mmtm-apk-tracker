# GitHub 关键图标源

公开仓库：<https://github.com/GuangShiX/mmtm-assets-fallback>

该仓库只保存 helper 需要的小型资源，不保存 APK、完整立绘、原始 Unity 对象、SQLite 清单或全部导出文件。低访问量场景直接使用 GitHub Raw，helper 在首次使用后把图片保存到本地。

## 当前范围

4.18.0 的关键集合包含：

| 分类 | 数量 | 内容 |
|---|---:|---|
| `characters` | 146 | `CHR_*_s.png` 小头像 |
| `enemies` | 60 | 敌人头像 |
| `equipment` | 884 | 装备图标 |
| `spheres` | 64 | 符石/宝珠图标 |
| `items` | 238 | 常用物品图标 |
| `ui` | 21 | 九切片边框、装饰、属性、底板和星标 |

合计 1,413 个 PNG、32,331,628 字节。单文件最大约 0.64 MB，不需要 Git LFS。数量包含 4.18.0 基础包以及当前官方资源热更新。

## 生成

自动检测并更新：

```powershell
python fallback.py --auto-update `
  --output D:\NewProjects\mmtm-assets-fallback `
  --repository GuangShiX/mmtm-assets-fallback
```

也可以从本地完整解包版本重建：

```powershell
python fallback.py --version 4.18.0 `
  --output D:\NewProjects\mmtm-assets-fallback `
  --repository GuangShiX/mmtm-assets-fallback
```

生成器执行以下校验：

- 提取状态必须满足项目的 `complete` 不变量；
- 每个逻辑文件名只选择一个规范 Sprite/Texture2D 输出；
- 21 个公共 UI 图标必须全部存在；
- 总文件数不得超过 3,000；
- 单文件不得超过 5 MiB，总体积不得超过 100 MiB；
- 每个文件写入 SHA-256、大小和来源资源键。

输出目录 `assets/` 带有所有权标记。生成器只会清理带标记的目录，避免误删人工文件。

## URL 协议

稳定入口：

```text
https://raw.githubusercontent.com/GuangShiX/mmtm-assets-fallback/main/latest.json
```

`latest.json` 和图片都使用 `main` 分支固定路径：

```text
manifest_url = https://raw.githubusercontent.com/GuangShiX/mmtm-assets-fallback/main/manifest.json
base_url     = https://raw.githubusercontent.com/GuangShiX/mmtm-assets-fallback/main
asset URL    = {base_url}/{manifest.assets[n].path}
```

游戏版本仍写入清单并保留 `v<游戏版本>` Git 标签用于回滚，但不进入图片 URL。消费端通过每条资源的 SHA-256 判断本地缓存是否需要更新，不通过版本目录切换地址。

## helper 本地缓存顺序

推荐服务端统一执行：

1. 命中 helper 本地磁盘缓存时直接返回。
2. 未命中时读取本地缓存的 `manifest.json`，必要时向 GitHub 重新验证清单。
3. 按分类和文件名寻找资源。
4. 从 GitHub Raw 下载 PNG，核对大小与 SHA-256。
5. 通过临时文件原子写入本地缓存，之后的请求只读本地文件。
6. 网络或校验失败时返回内置占位图，不让页面持续重试。

`latest.json` 和版本清单也应保存在本地。正常情况下 helper 不会直接向 GitHub 发请求；一次回退成功后，后续请求继续使用本地文件。

## 自动更新

图片仓库自己的 GitHub Actions 每 6 小时检查官方版本：

1. 从官方 `vars.js` 获取 `appVersion`。
2. 从匿名 `getDataUri` 获取 `assetVersion` 和官方 Addressables 地址。
3. `assetVersion` 变化但应用版本不变时，只下载 catalog 中新增或变化的关键图片 Bundle。
4. `appVersion` 变化时下载官方 APK，扫描 Bundle 并仅导出关键图片，不创建完整解包的百万级中间文件。
5. 校验图片数量、公共 UI、文件大小和 SHA-256 后提交变化。
6. 新应用版本创建 `v<游戏版本>` 回滚标签；图片 URL 始终保持 `main/assets/...`。

`appVersion` 与 `assetVersion` 必须同时检测。只检查 APK 版本会漏掉同一客户端版本下发布的新角色头像。

## 使用边界

GitHub 建议普通仓库保持在 1 GB 内，并阻止单个普通 Git 文件超过 100 MiB；当前集合远低于这些限制。GitHub Raw 不是有 SLA 的商用 CDN，但适合当前低流量、本地缓存优先的使用方式。

游戏图标版权属于相应权利方。备用仓库不为这些图片授予开源许可，并保留署名或移除请求入口。
