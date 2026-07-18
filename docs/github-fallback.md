# GitHub 关键图标备用源

公开备用仓库：<https://github.com/GuangShiX/mmtm-assets-fallback>

GitHub 备用源只保存 helper 需要的小型资源，不保存 APK、完整立绘、原始 Unity 对象、SQLite 清单或全部导出文件。Cloudflare R2 是主源，GitHub 只在 R2 失败时使用。

## 当前范围

4.18.0 的关键集合包含：

| 分类 | 数量 | 内容 |
|---|---:|---|
| `characters` | 145 | `CHR_*_s.png` 小头像 |
| `enemies` | 60 | 敌人头像 |
| `equipment` | 878 | 装备图标 |
| `spheres` | 64 | 符石/宝珠图标 |
| `items` | 238 | 常用物品图标 |
| `ui` | 18 | 九切片边框、装饰、属性和背景 |

合计 1,403 个 PNG、32,160,874 字节。单文件最大约 0.64 MB，不需要 Git LFS。

## 生成

从本地最新完整解包版本生成：

```powershell
python fallback.py --version 4.18.0 `
  --output D:\NewProjects\mmtm-assets-fallback `
  --repository GuangShiX/mmtm-assets-fallback
```

生成器执行以下校验：

- 提取状态必须满足项目的 `complete` 不变量；
- 每个逻辑文件名只选择一个规范 Sprite/Texture2D 输出；
- 18 个公共 UI 图标必须全部存在；
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

## helper 回退顺序

推荐服务端统一执行：

1. 命中 helper 本地磁盘缓存时直接返回。
2. 未命中时请求主 Cloudflare R2。
3. 主源超时、网络失败或返回非 2xx 时读取 GitHub `manifest.json`。
4. 按分类和文件名寻找资源。
5. 下载 PNG，核对大小与 SHA-256 后写入本地缓存。
6. GitHub 也失败时返回内置占位图，不让页面请求持续重试。

`latest.json` 和版本清单也应保存在本地。正常情况下 helper 不会直接向 GitHub 发请求；一次回退成功后，后续请求继续使用本地文件。

## 自动更新

备用仓库自己的 GitHub Actions 每 6 小时检查 APKPure：

1. 当前游戏版本与 `manifest.json` 相同则跳过解包。
2. 发现新版本后运行 tracker 的完整下载和解包流程。
3. 重建关键集合并提交。
4. 创建 `v<游戏版本>` 回滚标签。
5. R2 变量已配置时，把当前集合增量同步到固定对象路径。

完整解包可能超过 GitHub 托管 Runner 的磁盘或时间限制。需要时给备用仓库设置 `ASSET_RUNNER` Repository Variable，指向有足够磁盘的 Windows 自托管 Runner。

## 使用边界

GitHub 建议普通仓库保持在 1 GB 内，并阻止单个普通 Git 文件超过 100 MiB；当前集合远低于这些限制。GitHub Raw/Pages 没有面向生产 CDN 的可用性承诺，因此这里只把它作为低流量灾备源。

游戏图标版权属于相应权利方。备用仓库不为这些图片授予开源许可，并保留署名或移除请求入口。
