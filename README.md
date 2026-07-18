# MementoMori APK Tracker

自动检查 MementoMori Android 更新，下载并校验 APK/XAPK，完整保留包内文件和 Unity 对象，同时把可识别资源导出成可直接使用的格式。

关键小图标另有轻量自动更新流程：同时检查官方应用版本和 Addressables 资源版本，只下载发生变化的 Bundle，并提交到独立 GitHub 图片仓库。

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

## GitHub 图片自动更新

公开图片仓库：[GuangShiX/mmtm-assets-fallback](https://github.com/GuangShiX/mmtm-assets-fallback)。它保存角色小头像、敌人、装备、符石、物品以及头像合成所需的公共边框，详细协议见 [docs/github-fallback.md](docs/github-fallback.md)。

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

1. `appVersion` 变化时下载一次官方 APK，扫描全部 Bundle 但只导出关键图片。
2. `assetVersion` 变化时解析官方 Addressables catalog，只下载新增或内容哈希变化的关键图片 Bundle。

这能覆盖“不更新 APK、只通过游戏资源热更新发布新角色”的情况。图片 URL 始终使用 `main/assets/...` 固定路径，不带游戏版本；`manifest.json` 为每张 PNG 提供 SHA-256，消费端下载后应校验并保存到本地缓存。

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
