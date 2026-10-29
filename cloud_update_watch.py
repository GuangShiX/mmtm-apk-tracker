"""Bounded cloud update watch. Raw MasterBooks never belong in --output.

The workflow owns cache/artifact upload, poster jobs, and publication. This
script performs no git writes, pushes, workflow dispatches, or account login.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

UTC = timezone.utc
MAX_DURATION = 19_800  # 5h30m; leave room inside GitHub's six-hour job limit.


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str:
    return value.astimezone(UTC).isoformat() if value else ""


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def fetch_windows() -> Any:
    from maintenance import fetch_official_maintenance_windows
    return fetch_official_maintenance_windows()


def probe_official() -> Any:
    import msgpack
    from asset_cdn import get_official_asset_info, _request_with_retries
    info = get_official_asset_info()
    response = _request_with_retries("GET", info.master_url("master-catalog"), timeout=30)
    catalog = msgpack.unpackb(response.content, raw=False, timestamp=3)
    return SimpleNamespace(**vars(info), master_url=info.master_url, catalog_hash=catalog_signature(catalog))


def catalog_signature(catalog: dict[str, Any]) -> str:
    books = catalog.get("MasterBookInfoMap")
    if not isinstance(books, dict) or not books:
        raise ValueError("master-catalog is missing MasterBookInfoMap")
    entries = {name: {"Hash": row.get("Hash"), "Size": row.get("Size")} for name, row in sorted(books.items())}
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def versions(info: Any) -> dict[str, str]:
    return {key: str(getattr(info, key, "")) for key in ("app_version", "asset_version", "master_version", "catalog_hash")}


def make_plan(windows: Any, now: datetime, max_duration: int = MAX_DURATION) -> dict[str, Any]:
    if not 1 <= max_duration <= MAX_DURATION:
        raise ValueError("watch duration must be within 1..19800 seconds")
    candidates = sorted((w for w in windows if w.end_at_utc + timedelta(minutes=30) >= now), key=lambda w: w.start_at_utc)
    if not candidates:
        return {"should_watch": False, "start_at_utc": "", "end_at_utc": "", "deadline_at_utc": "", "reason": "no-upcoming-maintenance"}
    window = candidates[0]
    start = window.start_at_utc - timedelta(minutes=30)
    end = window.end_at_utc + timedelta(minutes=30)
    return {"should_watch": start <= now <= end, "start_at_utc": iso(start), "end_at_utc": iso(end), "deadline_at_utc": iso(min(end, now + timedelta(seconds=max_duration))), "reason": "maintenance-window" if start <= now <= end else "before-maintenance-window"}


def github_outputs(path: Path | None, values: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            value = str(value).lower() if isinstance(value, bool) else str(value)
            if "\n" in value or "\r" in value:
                raise ValueError(f"multiline GitHub output: {key}")
            stream.write(f"{key}={value}\n")


def character_ids(output: Path, *, skills_dir: bool = False) -> list[int]:
    index = read_json((output if skills_dir else output / "skills") / "index.json", {})
    return sorted(int(row["id"]) for row in index.get("characters", []))


def cache_matches(output: Path, info: Any, now: datetime) -> bool:
    from master import load_current_master_skill_repository
    return load_current_master_skill_repository(output, str(info.master_version), ("zh-CN", "ja-JP"), now) is not None


def _localized(values: Any) -> str:
    values = values or {}
    return values.get("zh-CN") or values.get("ja-JP") or next((v for v in values.values() if v), "")


def build_brief(output: Path, latest: dict[str, Any], new_ids: list[int], future_ids: list[int] | None = None) -> str:
    source = latest.get("source", {})
    lines = ["# 新角色技能候选快报", "", f"Master: {source.get('master_version', '')}", "", "本地化缺失时保留日文原文；候选实装时间以官方记录为准。", ""]
    if not new_ids:
        lines.extend(["本轮未确认新增角色；冷缓存仅建立基线，旧角色不记为新增。", ""])
    if future_ids:
        lines.extend([f"原始主表有尚未实装的新角色 ID: {', '.join(map(str, future_ids))}。仅待预览，未写入正式 skills 或记为已实装。", ""])
    rows = latest.get("latest_characters", [])
    by_id = {int(row["id"]): row for row in rows}
    for cid in new_ids:
        by_id.setdefault(cid, {"id": cid, "path": f"skills/characters/{cid:06d}.json"})
    for cid, row in sorted(by_id.items()):
        payload = read_json(output / "skills" / "characters" / f"{cid:06d}.json", {})
        character = payload.get("character", {})
        lines.extend([f"## {_localized(character.get('names')) or row.get('display_name') or cid} ({cid})", "", f"实装时间 JST: {character.get('start_time_jst', '')}", f"官方本地化完整: {payload.get('localization_complete', False)}", ""])
        for skill in payload.get("active_skills", []) + payload.get("passive_skills", []):
            lines.extend([f"### {_localized(skill.get('names')) or skill.get('id')} ({skill.get('kind', '')})", ""])
            for level in skill.get("levels", []):
                text = _localized(level.get("descriptions")) or level.get("source_memo_ja") or "官方文本尚缺"
                lines.extend([f"- Lv {level.get('character_level', '')}: {text}", ""])
        weapon = payload.get("exclusive_weapon") or {}
        if weapon:
            lines.extend([f"### 专武：{_localized(weapon.get('names'))}", ""])
            for effect in weapon.get("skill_effects", []):
                lines.extend([f"- 稀有度 {effect.get('equipment_rarity_flags')}: {_localized(effect.get('descriptions')) or '官方文本尚缺'}", ""])
            lines.extend(["被动属性：" + json.dumps(weapon.get("passive_effects", []), ensure_ascii=False), ""])
        for arcana in payload.get("arcanas", []):
            lines.extend([f"### 秘仪：{_localized(arcana.get('names'))}", "", "所需角色：" + ", ".join(_localized(row.get("names")) or str(row.get("id")) for row in arcana.get("required_characters", [])), ""])
            for level in arcana.get("levels", []):
                lines.extend([f"- Lv {level.get('level')}: " + json.dumps(level.get("effects", []), ensure_ascii=False), ""])
    brief = "\n".join(lines)
    (output / "brief.md").write_text(brief + "\n", encoding="utf-8")
    return brief


def export_official(info: Any, output: Path, raw_dir: Path, as_of: datetime, previous_ids: list[int]) -> dict[str, Any]:
    from master import MasterSource, build_master_skill_repository, download_master_books, _unpack, _parse_jst, _book_path
    # Persist original books outside the artifact directory for simulator handoff.
    raw = raw_dir / str(info.master_version)
    languages = ("zh-CN", "ja-JP")
    download_master_books(info, raw, languages)
    expected_catalog = getattr(info, "catalog_hash", "")
    if expected_catalog and catalog_signature(_unpack(_book_path(raw, "master-catalog"))) != expected_catalog:
        raise RuntimeError("master-catalog changed during download; retry the coherent snapshot")
    future_rows = [row for row in _unpack(_book_path(raw, "CharacterMB")) if isinstance(row, dict) and isinstance(row.get("Id"), int) and not row.get("IsIgnore") and _parse_jst(row.get("StartTimeFixJST")) > as_of and row["Id"] not in previous_ids and _parse_jst(row.get("StartTimeFixJST")).year < 2100]
    future_ids = sorted(row["Id"] for row in future_rows)
    latest = build_master_skill_repository(raw, output, "GuangShiX/mmtm-assets-fallback", MasterSource(info.app_version, info.asset_version, info.master_version), languages, as_of)
    current_ids = character_ids(output)
    added = sorted(set(current_ids) - set(previous_ids)) if previous_ids else []
    brief = build_brief(output, latest, added, future_ids)
    preview_ids = []
    if future_rows:
        first_release = min(_parse_jst(row["StartTimeFixJST"]) for row in future_rows)
        preview_ids = sorted(row["Id"] for row in future_rows if _parse_jst(row["StartTimeFixJST"]) == first_release)
        preview_root = output / "preview"
        preview_latest = build_master_skill_repository(raw, preview_root, "GuangShiX/mmtm-assets-fallback", MasterSource(info.app_version, info.asset_version, info.master_version), languages, first_release)
        preview_brief = build_brief(preview_root, preview_latest, preview_ids)
        brief += "\n\n# 尚未实装的候选技能预览\n\n仅按官方原定开放时间生成独立预览，不能称为已经实装。\n\n" + preview_brief
        (output / "brief.md").write_text(brief + "\n", encoding="utf-8")
    else:
        import shutil
        preview_root = output / "preview"
        if preview_root.exists():
            if not (preview_root / "skills" / ".generated-by-mmtm-apk-tracker").is_file():
                raise RuntimeError("refusing to remove an unowned preview directory")
            shutil.rmtree(preview_root)
    return {"character_ids": current_ids, "new_character_ids": added, "preview_character_ids": preview_ids, "brief": brief}


def watch(output: Path, state_file: Path, raw_dir: Path, *, baseline_skills_dir: Path | None = None, interval: int = 20, max_duration: int = MAX_DURATION, refresh_interval: int = 300, clock: Callable[[], datetime] = utc_now, sleep: Callable[[float], None] = time.sleep, windows: Callable[[], Any] = fetch_windows, probe: Callable[[], Any] = probe_official, exporter: Callable[..., dict[str, Any]] = export_official, github_output: Path | None = None, summary_file: Path | None = None) -> dict[str, Any]:
    if not 1 <= interval <= 300 or not 1 <= max_duration <= MAX_DURATION or refresh_interval < 1:
        raise ValueError("invalid bounded watch interval/duration")
    raw_abs, out_abs = raw_dir.resolve(), output.resolve()
    if raw_abs == out_abs or raw_abs.is_relative_to(out_abs) or out_abs.is_relative_to(raw_abs):
        raise ValueError("--raw-dir and --output must be separate trees")
    started = clock()
    hard_deadline = started + timedelta(seconds=max_duration)
    current_windows = windows()
    plan = make_plan(current_windows, started, max_duration)
    print(json.dumps({"event": "maintenance-plan", "checked_at_utc": iso(started), **plan}), flush=True)
    if not plan["should_watch"]:
        result = {"exported": False, "has_new_characters": False, "new_character_ids": "", "reason": plan["reason"], "master_version": "", "output_dir": str(output.resolve())}
        github_outputs(github_output, result)
        return result
    state = read_json(state_file, {})
    previous_ids = state.get("character_ids") or character_ids(output) or (character_ids(baseline_skills_dir, skills_dir=True) if baseline_skills_dir else [])
    baseline_source = read_json(baseline_skills_dir / "latest.json", {}).get("source", {}) if baseline_skills_dir else {}
    refresh_at = started + timedelta(seconds=refresh_interval)
    last_error = ""
    while True:
        now = clock()
        if now >= hard_deadline:
            break
        if now >= refresh_at or now >= datetime.fromisoformat(plan["end_at_utc"]):
            try:
                refreshed = windows()
                if refreshed:
                    current_windows = refreshed
            except Exception as exc:
                print(f"Maintenance refresh failed; retaining last window: {exc}", flush=True)
            refresh_at = clock() + timedelta(seconds=refresh_interval)
        plan = make_plan(current_windows, now, max_duration)
        if not plan["should_watch"]:
            break
        try:
            info = probe()
            current = versions(info)
            if not current["master_version"]:
                raise ValueError("official master_version is empty")
            print(json.dumps({"event": "version-probe", "checked_at_utc": iso(clock()), **current}), flush=True)
            accepted = state.get("accepted_versions")
            content_changed = bool(accepted and any(accepted.get(key) != current[key] for key in ("master_version", "catalog_hash"))) or bool(not accepted and baseline_source.get("master_version") and baseline_source["master_version"] != current["master_version"])
            if content_changed or not cache_matches(output, info, now):
                result = exporter(info, output, raw_dir, now, previous_ids)
                added = result["new_character_ids"]
                preview_ids = result.get("preview_character_ids", [])
                if summary_file:
                    summary_file.parent.mkdir(parents=True, exist_ok=True)
                    with summary_file.open("a", encoding="utf-8") as stream:
                        stream.write(result["brief"] + "\n")
                deliver = content_changed or bool(added) or bool(preview_ids)
                values = {"exported": deliver, "has_new_characters": bool(added), "new_character_ids": ",".join(map(str, added)), "has_preview_characters": bool(preview_ids), "preview_character_ids": ",".join(map(str, preview_ids)), "master_version": current["master_version"], "output_dir": str(output.resolve()), "reason": "content-update" if content_changed else "new-character" if added or preview_ids else "cold-cache-baseline"}
                if deliver:
                    github_outputs(github_output, values)
                # Commit both the disk and in-memory baseline only after all output succeeds.
                new_state = {"accepted_versions": current, "character_ids": result["character_ids"], "accepted_at_utc": iso(clock())}
                write_json(state_file, new_state)
                state = new_state
                print(json.dumps(values, ensure_ascii=False), flush=True)
                if deliver:
                    return values
                previous_ids = result["character_ids"]
            elif accepted != current:
                state["accepted_versions"] = current
                write_json(state_file, state)
            last_error = ""
        except Exception as exc:
            last_error = str(exc)
            print(f"Update attempt failed; accepted version unchanged; retrying: {exc}", flush=True)
        remaining = min((hard_deadline - clock()).total_seconds(), (datetime.fromisoformat(plan["end_at_utc"]) - clock()).total_seconds())
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
    values = {"exported": False, "has_new_characters": False, "new_character_ids": "", "has_preview_characters": False, "preview_character_ids": "", "master_version": state.get("accepted_versions", {}).get("master_version", ""), "output_dir": str(output.resolve()), "reason": "deadline-with-errors" if last_error else "watch-window-ended"}
    github_outputs(github_output, values)
    if last_error:
        raise RuntimeError(f"watch ended without successful refresh: {last_error}")
    return values


@contextmanager
def single_instance(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--watch", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("cloud-candidate"))
    parser.add_argument("--state-file", type=Path, default=Path("cloud-state/watch.json"))
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--baseline-skills-dir", type=Path, help="Published checkout skills directory; provides cold-cache character baseline")
    parser.add_argument("--interval-seconds", type=int, default=20)
    parser.add_argument("--max-duration-seconds", type=int, default=MAX_DURATION)
    parser.add_argument("--refresh-window-seconds", type=int, default=300)
    parser.add_argument("--github-output", type=Path, default=Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None)
    parser.add_argument("--summary-file", type=Path, default=Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None)
    args = parser.parse_args()
    try:
        if args.plan:
            plan = make_plan(fetch_windows(), utc_now(), args.max_duration_seconds)
            github_outputs(args.github_output, plan)
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        if args.raw_dir is None:
            if not os.environ.get("RUNNER_TEMP"):
                parser.error("--raw-dir is required outside GitHub Actions")
            args.raw_dir = Path(os.environ["RUNNER_TEMP"]) / "mmtm-cloud-master"
        with single_instance(args.state_file.with_suffix(".lock")):
            watch(args.output, args.state_file, args.raw_dir, baseline_skills_dir=args.baseline_skills_dir, interval=args.interval_seconds, max_duration=args.max_duration_seconds, refresh_interval=args.refresh_window_seconds, github_output=args.github_output, summary_file=args.summary_file)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Cloud watch failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
