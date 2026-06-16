# AGENTS.md

## Project Overview

MementoMori APK Tracker 是一个 Python 自动化工具链，用于检查 MementoMori Android 包更新、下载 APK/XAPK、解包 Unity AssetBundle，并输出版本资源差异。

当前项目目标是保持核心流程精简：

```text
check latest version -> download package -> extract assets -> diff manifests
```

## Core Files

- `run.py` - 自动更新入口。默认检查远端最新版，发现新版本后下载、解包、生成 diff。
- `download.py` - 使用 `bin/apkeep.exe` 下载 APK/XAPK，并校验 ZIP 完整性。
- `extract.py` - 使用 UnityPy 解包 XAPK 中的 catalog、Unity 数据、AssetBundle 资源和 Prefab 层级。
- `diff.py` - 对比两个版本的 `manifest.json`，输出 txt/json 报告。
- `config.json` - 包名、下载源和目录配置。
- `bin/apkeep.exe` - APK 下载工具，核心流程必需。

## Generated Directories

这些目录由脚本自动生成，不应提交到 git：

- `apks/` - 下载的 APK/XAPK
- `extracted/` - 解包输出
- `reports/` - 版本差异报告
- `__pycache__/` - Python 缓存

## Usage

```bash
# 自动检查更新；有新版本时下载并解包
python run.py

# 只检查远端是否有更新
python run.py --check-only

# 指定版本
python run.py --version 4.10.0

# 只使用本地已有完整包，不下载
python run.py --version 4.10.0 --skip-download

# 强制重新解包
python run.py --version 4.10.0 --force
```

也可以单独执行：

```bash
python download.py apk-pure 4.10.0
python extract.py 4.10.0
python diff.py 4.9.0 4.10.0
```

## Dependencies

- Python 3.10+
- UnityPy 1.25.0+
- apkeep v0.18.0 at `bin/apkeep.exe`

Install Python dependency:

```bash
pip install UnityPy
```

## Cleanup Policy

不要重新引入非核心工具目录，除非用户明确要求：

- AssetStudio
- AssetStudioMod
- AssetRipper
- Unity Prefab 重建工具
- Il2CppDumper

这些不是当前自动更新、下载、解包链路的必需组件。历史导出产物和报告也应视为可再生成数据。
