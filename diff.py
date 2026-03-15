"""版本对比模块 — 比较两个版本的 manifest，生成变更报告"""

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def load_manifest(version: str) -> dict:
    """加载指定版本的 manifest.json"""
    path = ROOT / CONFIG["dirs"]["extracted"] / version / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"未找到版本 {version} 的 manifest: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def diff_versions(old_ver: str, new_ver: str) -> dict:
    """对比两个版本，返回 diff 结果"""
    old_manifest = load_manifest(old_ver)
    new_manifest = load_manifest(new_ver)

    old_keys = set(old_manifest.keys())
    new_keys = set(new_manifest.keys())

    added = new_keys - old_keys
    removed = old_keys - new_keys
    common = old_keys & new_keys

    modified = set()
    for key in common:
        old_hash = old_manifest[key].get("hash", "")
        new_hash = new_manifest[key].get("hash", "")
        if old_hash and new_hash and old_hash != new_hash:
            modified.add(key)

    # 按资源类型分组
    def group_by_type(keys, manifest):
        groups = defaultdict(list)
        for key in sorted(keys):
            t = manifest[key].get("type", "Unknown")
            groups[t].append(key)
        return dict(groups)

    return {
        "old_version": old_ver,
        "new_version": new_ver,
        "summary": {
            "total_old": len(old_keys),
            "total_new": len(new_keys),
            "added": len(added),
            "removed": len(removed),
            "modified": len(modified),
            "unchanged": len(common) - len(modified),
        },
        "added": group_by_type(added, new_manifest),
        "removed": group_by_type(removed, old_manifest),
        "modified": group_by_type(modified, new_manifest),
    }


def format_report(diff: dict) -> str:
    """将 diff 结果格式化为可读报告"""
    lines = []
    s = diff["summary"]

    lines.append(f"{'=' * 60}")
    lines.append(f"MementoMori 版本对比报告")
    lines.append(f"{diff['old_version']} → {diff['new_version']}")
    lines.append(f"{'=' * 60}")
    lines.append("")
    lines.append(f"旧版本资源数: {s['total_old']}")
    lines.append(f"新版本资源数: {s['total_new']}")
    lines.append(f"新增: {s['added']}  删除: {s['removed']}  修改: {s['modified']}  未变: {s['unchanged']}")
    lines.append("")

    for section, label in [("added", "新增资源"), ("removed", "删除资源"), ("modified", "修改资源")]:
        groups = diff[section]
        if not groups:
            continue
        total = sum(len(v) for v in groups.values())
        lines.append(f"--- {label} ({total}) ---")
        for type_name, keys in sorted(groups.items()):
            lines.append(f"  [{type_name}] ({len(keys)})")
            for key in keys[:20]:  # 每类最多显示 20 个
                lines.append(f"    {key}")
            if len(keys) > 20:
                lines.append(f"    ... 还有 {len(keys) - 20} 个")
        lines.append("")

    return "\n".join(lines)


def run_diff(old_ver: str, new_ver: str) -> Path:
    """执行 diff 并保存报告"""
    print(f"对比版本: {old_ver} → {new_ver}")

    diff = diff_versions(old_ver, new_ver)
    report = format_report(diff)

    # 保存文本报告
    reports_dir = ROOT / CONFIG["dirs"]["reports"]
    reports_dir.mkdir(parents=True, exist_ok=True)

    report_path = reports_dir / f"{old_ver}_vs_{new_ver}.txt"
    report_path.write_text(report, encoding="utf-8")

    # 保存 JSON 详细数据
    json_path = reports_dir / f"{old_ver}_vs_{new_ver}.json"
    json_path.write_text(json.dumps(diff, ensure_ascii=False, indent=2), encoding="utf-8")

    print(report)
    print(f"\n报告已保存:")
    print(f"  文本: {report_path}")
    print(f"  JSON: {json_path}")

    return report_path


def list_extracted_versions() -> list[str]:
    """列出已解包的版本"""
    extracted_dir = ROOT / CONFIG["dirs"]["extracted"]
    if not extracted_dir.exists():
        return []
    versions = []
    for d in extracted_dir.iterdir():
        if d.is_dir() and (d / "manifest.json").exists():
            versions.append(d.name)
    return sorted(versions)


def main():
    if len(sys.argv) < 3:
        print("用法: python diff.py <旧版本> <新版本>")
        print("示例: python diff.py 4.8.0 4.9.1")
        versions = list_extracted_versions()
        if versions:
            print(f"\n已解包的版本: {', '.join(versions)}")
        else:
            print("\n暂无已解包的版本，请先运行 extract.py")
        sys.exit(1)

    old_ver = sys.argv[1]
    new_ver = sys.argv[2]
    run_diff(old_ver, new_ver)


if __name__ == "__main__":
    main()
