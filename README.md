# MementoMori APK Tracker

自动检查 MementoMori Android 更新，下载并校验 APK/XAPK，完整保留包内文件和 Unity 对象，同时把可识别资源导出成可直接使用的格式。

关键小图标和角色技能另有轻量自动更新流程：分别检查官方应用版本、Addressables 资源版本和 Master 版本，并提交到独立 GitHub 数据仓库。

## 能力范围

完整提取分为两层：

1. **原始无损层**：展开 XAPK 及全部嵌套 APK，保留每个 Bundle、Unity 数据文件和每个 Serialized Object 的原始字节。
2. **可用格式层**：额外导出 PNG、音频、字体、OBJ、文本，以及 Animation、Material、Shader、MonoBehaviour 等 JSON。

即使 UnityPy 暂时不能解码某种对象，它仍会出现在 `raw/` 和 `objects_raw/` 中，不会被过滤或丢失。每次提取结束都会生成 `extraction_report.json`，只有所有 Unity 数据源都可读取且原始对象覆盖率为 100% 时，状态才是 `complete`。

## 安装

要求：

- Python 3.10+
- `bin/apkeep.exe`
- 足够磁盘空间；完整模式会保留原包、原始对象和解码文件

```bash
pip install -r requirements.txt
```

项目固定使用 UnityPy 1.25.0，避免依赖升级改变导出行为。

## 自动更新

单次检查；发现新版本后自动下载、CRC 校验、解包并生成版本 diff：

```bash
python run.py
```

常驻运行，每小时检查一次：

```bash
python run.py --watch
```

自定义检查间隔：

```bash
python run.py --watch --interval 1800
```

Windows 计划任务方式，适合开机后长期自动运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_windows_task.ps1
```

卸载计划任务：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_windows_task.ps1 -Uninstall
```

单次运行、`--watch` 和计划任务共用单实例锁；已有更新进程运行时，后启动的实例会直接退出，避免同时写入同一版本。

计划任务输出写入 `reports/scheduled-task.log`。

## GitHub 轻量数据自动更新

公开数据仓库：[GuangShiX/mmtm-assets-fallback](https://github.com/GuangShiX/mmtm-assets-fallback)。它保存角色小头像、敌人、装备、符石、物品、头像合成公共边框、已锁定页面的 Prefab Sprite 依赖，以及从官方 Master 解包的角色技能。图片协议见 [docs/github-fallback.md](docs/github-fallback.md)。

```bash
# 查看官方应用版本、资源版本和 Master 版本
python fallback.py --remote-info

# 自动处理基础 APK 更新和同版本资源热更新
python fallback.py --auto-update --output fallback_dist

# 只合并官方 Addressables 热更新图片
python fallback.py --sync-hot-update --output fallback_dist

# 从已有 APK/XAPK 轻量生成基础图片集，不创建完整解包目录
python fallback.py --from-package --version 4.18.0 --apk apks/game.xapk --output fallback_dist

# 仍可从本机 complete 完整解包结果重建
python fallback.py --version 4.18.0 --output fallback_dist
```

自动流程使用两个更新键：

1. `appVersion` 变化时下载一次官方 APK，扫描全部 Bundle 但只导出关键图片；大文件断线重试会保留临时文件并通过 HTTP Range 续传。
2. `assetVersion` 变化时解析官方 Addressables catalog，只下载新增或内容哈希变化的关键图片 Bundle。

这能覆盖“不更新 APK、只通过游戏资源热更新发布新角色”的情况。图片 URL 始终使用 `main/assets/...` 固定路径，不带游戏版本；`manifest.json` 为每张 PNG 提供 SHA-256，消费端下载后应校验并保存到本地缓存。

角色和技能文本不在基础 APK 的 Addressables catalog 中，而是由 `getDataUri` 返回的 `masterVersion` 独立更新。`master.py` 会先按官方 `master-catalog` 校验每个 MessagePack MasterBook 的大小和 MD5，再解析角色、主动/被动技能、专属武器被动词条、专武技能效果、秘仪关系/等级效果和本地化文本：

```bash
# 下载并导出官方最新 Master，默认简体中文
python master.py --auto-update --output fallback_dist

# 从已经下载的 MasterBook 重建，适合本地复核
python master.py \
  --master-dir reports/master_1786600691299/raw \
  --app-version 4.20.0 \
  --asset-version 4c07f95ba454a60d9f1580d9466aaf01966b01a2_1 \
  --master-version 1786600691299 \
  --output fallback_dist
```

导出结果位于 `skills/`。`latest.json` 按 `StartTimeFixJST` 判断最新已实装角色，不依赖不连续的角色编号；未来时间的占位记录不会提前发布。若某个新角色已进入技能 MasterBook、但官方语言表尚未加入对应键，JSON 会将 `localization_complete` 设为 `false`，列出缺失键并保留技能记录中的日文 `Memo` 分段原文，后续 Master 更新会自动补齐官方文本。

### 通用技能介绍图

`skill_card.py` 使用 `skills/characters/*.json`、官方角色小头像、专武图标及技能图标生成统一介绍图。`full` 为 3840×2160 横版并保留官方各等级全文；`compact` 为 2160×3840 文字优先竖屏一图流：顶部使用无遮挡的大一级角色头像，主体依次展示四个技能，底部再展示专武、LR 专效果、秘仪 LR 效果与解锁角色头像。技能与专武图标使用同一尺寸，省流文本会去掉台词、合并等级/专武强化、压缩常见冗长句式，只留下最终效果。背景、中文排版、分句换行和字号适配均由代码确定，不依赖生成式图片模型，也不包含角色专用硬编码文案：

```bash
# 生成当前最新实装角色；同一时刻有多个角色时全部生成
python skill_card.py \
  --latest \
  --skills-dir fallback_dist/skills \
  --output-dir fallback_dist/cards/characters \
  --mode compact

# 不访问官方接口，校验最新角色卡是否匹配技能 JSON、模板版本、尺寸与哈希
python skill_card.py \
  --verify-latest \
  --skills-dir fallback_dist/skills \
  --output-dir fallback_dist/cards/characters \
  --mode compact

# 指定角色和输出文件
python skill_card.py \
  --character-id 150 \
  --skills-dir fallback_dist/skills \
  --output output/florence-150-compact.png \
  --mode compact

# 批量重建角色库中的全部技能图
python skill_card.py \
  --all \
  --skills-dir fallback_dist/skills \
  --output-dir fallback_dist/cards/characters
```

生成器会从官方 Addressables catalog 精确解析 `CharacterIcon/CHR_<ID>`、`Icon/Equipment/EQP_<图标ID>` 和 `Icon/Skill/CSK_<技能ID>` 的 Bundle，并缓存已校验下载。全文版文件名为 `character-<ID>-zh-CN.png`，省流版为 `character-<ID>-compact-zh-CN.png`。批量生成会同步维护 `cards/manifest.json`，记录角色技能 JSON 哈希、模板版本、素材版本、图片哈希和尺寸；CI 先做离线校验，只有最新角色缺图或数据/模板变化时才访问官方素材并重绘。每个技能面板会选择能够完整容纳正文的最大可读字号；若四技能无法在单图中清晰排入，生成会明确失败，不会缩成小字或裁掉内容。

helper 侧的实现边界和新 session 提示词见 [docs/helper-image-cache-handoff.md](docs/helper-image-cache-handoff.md)。

## 其他命令

```bash
# 只检查，不下载
python run.py --check-only

# 指定版本
python run.py --version 4.18.0

# 使用本地已有完整包
python run.py --version 4.18.0 --skip-download

# 强制从头重新提取
python run.py --version 4.18.0 --force

# 单独执行各阶段
python download.py apk-pure 4.18.0
python extract.py 4.18.0
python diff.py 4.17.0 4.18.0
```

提取过程中断后，重新运行同一版本即可从 `manifest.sqlite3.tmp` 按 Unity 数据源继续；已完整写入的数据源不会重复导出。只有 `--force` 会清除断点并从头开始。

## 输出结构

```text
extracted/<version>/
├── raw/
│   ├── archives/          # XAPK 中的原始 split APK
│   ├── apks/              # 每个 APK 的完整文件树
│   └── outer/             # XAPK 外层非 APK 文件
├── package_manifest.json  # 每个包内文件的 size、CRC32、SHA-256
├── objects_raw/           # 每个 Unity 对象的原始序列化字节
├── objects_json/          # 可读取 TypeTree 的结构化 JSON
├── assets/                # 可直接使用的 PNG/音频/字体/OBJ/文本/JSON
├── prefabs/               # UI Prefab 层级
├── unity_data/            # Unity 主数据便捷副本
├── catalog.json           # Addressable catalog
├── manifest.sqlite3       # 百万级全对象版本 diff 索引
└── extraction_report.json # 覆盖率、类型统计、回退和错误清单
```

`reports/<old>_vs_<new>.txt` 和 `.json` 按资源类型列出新增、删除和原始内容 hash 变化。版本对比直接使用 SQLite 执行，不会把多个百万对象清单全部载入内存。

## 完整性的边界

`complete` 表示包文件和 Unity Serialized Object 均已无损保留，不代表所有专有数据都能转换成通用编辑格式。缺少 TypeTree 的 IL2CPP MonoBehaviour、自定义压缩或游戏私有格式会保留原始字节，并在报告的 `fallback_counts` / `errors` 中显示。

## 测试

```bash
python -m unittest discover -s tests -v
```
