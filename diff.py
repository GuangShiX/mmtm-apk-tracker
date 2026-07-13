"""版本对比模块 — 比较两个版本的 manifest，生成变更报告"""

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def manifest_path(version: str) -> Path:
    path = ROOT / CONFIG["dirs"]["extracted"] / version / "manifest.sqlite3"
    if not path.exists():
        raise FileNotFoundError(f"未找到版本 {version} 的 manifest: {path}")
    return path


def diff_versions(old_ver: str, new_ver: str) -> dict:
    """使用 SQLite 对比百万级对象索引，避免全量载入内存。"""
    connection = sqlite3.connect(":memory:")
    connection.execute("ATTACH DATABASE ? AS old", (str(manifest_path(old_ver)),))
    connection.execute("ATTACH DATABASE ? AS new", (str(manifest_path(new_ver)),))

    total_old = connection.execute("SELECT COUNT(*) FROM old.resources").fetchone()[0]
    total_new = connection.execute("SELECT COUNT(*) FROM new.resources").fetchone()[0]

    def group_by_type(query: str):
        groups = defaultdict(list)
        for key, type_name in connection.execute(query):
            groups[type_name or "Unknown"].append(key)
        return dict(groups)

    added = group_by_type("""
        SELECT n.resource_key, n.type
        FROM new.resources n
        LEFT JOIN old.resources o ON o.resource_key = n.resource_key
        WHERE o.resource_key IS NULL
        ORDER BY n.type, n.resource_key
    """)
    removed = group_by_type("""
        SELECT o.resource_key, o.type
        FROM old.resources o
        LEFT JOIN new.resources n ON n.resource_key = o.resource_key
        WHERE n.resource_key IS NULL
        ORDER BY o.type, o.resource_key
    """)
    modified = group_by_type("""
        SELECT n.resource_key, n.type
        FROM new.resources n
        INNER JOIN old.resources o ON o.resource_key = n.resource_key
        WHERE n.hash <> o.hash
        ORDER BY n.type, n.resource_key
    """)
    added_count = sum(map(len, added.values()))
    removed_count = sum(map(len, removed.values()))
    modified_count = sum(map(len, modified.values()))
    unchanged = total_new - added_count - modified_count
    connection.close()

    return {
        "old_version": old_ver,
        "new_version": new_ver,
        "summary": {
            "total_old": total_old,
            "total_new": total_new,
            "added": added_count,
            "removed": removed_count,
            "modified": modified_count,
            "unchanged": unchanged,
        },
        "added": added,
        "removed": removed,
        "modified": modified,
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
        report_path = d / "extraction_report.json"
        if not d.is_dir() or not (d / "manifest.sqlite3").exists() or not report_path.exists():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if report.get("status") == "complete":
            versions.append(d.name)
    return sorted(versions, key=version_key)


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
