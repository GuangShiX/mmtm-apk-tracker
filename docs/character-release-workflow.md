# 新角色数据与横版海报流水线

`character_release.py` 把版本确认、原表缓存、正式导出、占位角色预览、素材复用、横版生成与离线校验串成一次可恢复的运行。海报统一使用 `split`（3840×2500），公开数据与未实装预览分别输出。

## 日常更新

```powershell
python -B character_release.py `
  --output fallback_dist `
  --character-id 155 `
  --preview-id 157 `
  --cache-dir reports/character-release-cache
```

每个角色参数可重复。省略 `--character-id` 时选取本次正式主表最新实装角色。脚本只获取一次匿名官方版本信息，并把 app、asset、master 版本及 URL 格式固定为本次快照；素材获取继续使用这个对象，不会在不同角色之间重新探测版本。

完整 APK 下载不属于这条流水线的前置条件。它仅下载生成角色数据与海报所需的 MasterBook、Addressables catalog、指定角色 Bundle 和原生横幅，不执行 Git 操作或账号游戏请求。

## 复用已验证快照和离线运行

已有 `reports/current-release-source.json` 时可直接复用：

```powershell
python -B character_release.py `
  --output fallback_dist `
  --character-id 155 `
  --preview-id 157 `
  --cache-dir reports/character-release-cache `
  --source-context reports/current-release-source.json `
  --offline
```

快照文件字段为 `app_version`、`asset_version`、`master_version`、`asset_uri_format`、`master_uri_format`、`raw_data_uri_format` 和 `raw_master_dir`。相对原表路径按快照文件所在目录解释。指定快照后不再访问版本接口。

在独立 worktree 中运行时，会根据快照文件及原表目录定位原工作区的 `skill-card-cache` 和 `official-poster-ui`。也可显式传入 `--asset-cache-dir D:/NewProjects/mmtm-apk-tracker/reports/skill-card-cache`，避免重新获取已有素材。

`--offline` 全程只读取缓存：缺失或损坏的主表、缺少 catalog、正式角色缺失立绘或技能图标都会明确失败，不会尝试网络回退。缓存齐全但没有此前状态记录时仍可离线重绘。

## 缓存与恢复

默认缓存放在 `reports/character-release-cache/`：

```text
master/<masterVersion>/       # 原始 MessagePack 与 master-catalog
<assetVersion>/catalog.json   # 所有角色共享的单份 catalog
<assetVersion>/<CharacterId>/ # 素材 Bundle、PNG、原生横幅
runs/<masterVersion>/
  source-context.json         # 本次固定来源
  state.json                  # 阶段、错误、逐角色证据和已完成图片
```

每个 MasterBook 按已缓存的官方 master-catalog 大小和 MD5 检查；只补下载缺失或损坏的表，并在校验成功后原子安装。已有原始表目录可通过快照指定，额外保存的其他表不会被清理。

catalog 优先复用共享缓存，其次复用同 assetVersion 的角色缓存，包括 `reports/skill-card-cache/<assetVersion>/*/catalog.json`。整个运行不按角色重复下载 catalog。

UI 图标、秘仪解锁头像优先读取输出目录的 `assets/`；角色素材也可复用同 assetVersion 的既有海报缓存。正式角色必须具有完整立绘和四张官方技能图标。预览缺失的原生横幅、专武数据或美术资源会记录在清单中，由模板显示未公开提示或占位，缺失文件不会伪造成原资产。

每张图完成后立即保存其来源 JSON 哈希、模板版本、真实 Noto 字体路径/字族/字面与哈希、官方初始速度、素材路径/哈希/大小和缺失资产清单。只有图片通过选中角色离线校验后才记为完成。

再次运行时，来源、模板、字体、素材和缺失项均未变化，且 PNG 哈希和离线校验仍通过的角色直接跳过重绘。中途中断后，已验证角色会复用，失败角色重新处理；错误 PNG 或变动的素材不会被当成完成品。

## 正式与预览输出

正式导出仍按当前 JST 过滤 `StartTimeFixJST`，生成全部已实装角色的 `skills/`，并维护真实的 `skills/latest.json`。指定正式 ID 必须存在于这次导出中。

`--preview-id` 直接从同一组已校验主表构造指定角色的 payload：

```text
preview/index.json
preview/skills/index.json
preview/skills/manifest.json
preview/skills/characters/<CharacterId>.json
preview/cards/manifest.json
preview/cards/characters/character-<CharacterId>-split-zh-CN.png
```

预览明确标记 `preview_notice`、`release_status` 和官方占位日期。2100 年日期不代表真实实装安排。预览不会写入正式 `skills/`，不会修改正式 latest，也不生成伪造的预览 latest。

正式海报保存在 `cards/characters/`。两组图片分别维护各自的清单，并使用 `cloud_verify_cards.verify_selected` 按原始 JSON 字节精确校验选中角色。

## 发布前验证

```powershell
python -B -m unittest discover -s tests -p test_character_release.py -v
python -B cloud_verify_cards.py `
  --character-ids 155 --skills-dir fallback_dist/skills `
  --output-dir fallback_dist/cards/characters
python -B cloud_verify_cards.py `
  --character-ids 157 --skills-dir fallback_dist/preview/skills `
  --output-dir fallback_dist/preview/cards/characters
```

脚本不自动暂存或发布仓库。发布时仍需分别检查正式数据、预览声明、图片清单和当前资产仓库验证结果；原始 MasterBook、缓存及运行状态留在忽略的 `reports/` 中。
