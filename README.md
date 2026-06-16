# MementoMori APK Tracker

自动检查 MementoMori Android 包更新，下载 APK/XAPK，解包 Unity AssetBundle，并生成版本资源差异报告。

## 当前保留的核心流程

```text
远端版本检查
  -> 下载缺失的 APK/XAPK
  -> 解包 Addressable catalog、Unity 数据、AssetBundle 资源、Prefab 层级
  -> 生成 manifest.json
  -> 和上一版 manifest 做 diff
```

## 文件说明

```text
mmtm-apk-tracker/
├── config.json       # 包名、下载源、目录配置
├── download.py       # apkeep 下载与完整性校验
├── extract.py        # XAPK 解包与 UnityPy 资源导出
├── diff.py           # manifest 版本差异报告
├── run.py            # 自动检查更新并串联完整流程
├── bin/
│   └── apkeep.exe    # APK 下载工具
├── apks/             # 下载产物，自动生成，已 gitignore
├── extracted/        # 解包产物，自动生成，已 gitignore
└── reports/          # 差异报告，自动生成，已 gitignore
```

## 依赖

- Python 3.10+
- UnityPy 1.25.0+
- `bin/apkeep.exe`

安装 Python 依赖：

```bash
pip install UnityPy
```

## 使用

自动检查远端最新版；如果本地未解包该版本，会下载并解包：

```bash
python run.py
```

只检查是否有更新，不下载、不解包：

```bash
python run.py --check-only
```

指定版本：

```bash
python run.py --version 4.10.0
```

只使用本地已有完整包，不联网下载：

```bash
python run.py --version 4.10.0 --skip-download
```

强制重新解包已存在版本：

```bash
python run.py --version 4.10.0 --force
```

也可以单独调用各步骤：

```bash
python download.py apk-pure 4.10.0
python extract.py 4.10.0
python diff.py 4.9.0 4.10.0
```

## 输出

`extracted/<version>/`：

- `catalog.json`：Addressable catalog
- `manifest.json`：导出的资源索引，用于版本对比
- `assets/`：导出的 Sprite、Texture2D、TextAsset、MonoBehaviour、AudioClip、Font、AnimationClip、Shader、Material
- `prefabs/`：Prefab 层级 JSON
- `unity_data/`：主 APK 中的 Unity 数据

`reports/<old>_vs_<new>.txt` 和 `reports/<old>_vs_<new>.json`：

- 新增资源
- 删除资源
- hash 变化的资源
- 按资源类型分组

## 清理策略

仓库只保留自动更新链路必需文件。以下内容都是可再生成或非核心工具，不纳入项目：

- 历史解包产物：`extracted/`
- 历史下载包：`apks/`
- 差异报告：`reports/`
- AssetStudio / AssetRipper / Unity Prefab 重建工具
- Python 缓存：`__pycache__/`
