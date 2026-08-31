"""Poll official MementoMori versions during a maintenance update window."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from asset_cdn import get_official_asset_info

ROOT = Path(__file__).parent
DEFAULT_STATE_FILE = ROOT / "reports" / "maintenance-watch.json"
DEFAULT_REPOSITORY = "GuangShiX/mmtm-assets-fallback"
DEFAULT_WORKFLOW = "update-icons.yml"


@dataclass(frozen=True)
class VersionProbe:
    app_version: str
    asset_version: str
    master_version: str

    @classmethod
    def from_official_info(cls, info: Any) -> "VersionProbe":
        return cls(
            app_version=str(info.app_version),
            asset_version=str(info.asset_version),
            master_version=str(info.master_version),
        )

    def fingerprint(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"状态文件不可用，将重新建立: {path}: {exc}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def dispatch_workflow(
    repository: str,
    workflow: str,
    ref: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    command = ["gh", "workflow", "run", workflow, "--repo", repository, "--ref", ref]
    result = runner(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"触发 GitHub Actions 失败 (exit {result.returncode}): {detail}")


def probe_once(
    state_path: Path,
    *,
    dispatch: bool = False,
    repository: str = DEFAULT_REPOSITORY,
    workflow: str = DEFAULT_WORKFLOW,
    ref: str = "main",
    probe: Callable[[], VersionProbe] | None = None,
) -> tuple[VersionProbe, bool]:
    current = (probe or (lambda: VersionProbe.from_official_info(get_official_asset_info())))()
    state = _load_state(state_path)
    previous = state.get("last_seen")
    changed = isinstance(previous, dict) and VersionProbe(**previous).fingerprint() != current.fingerprint()
    dispatched = False
    if changed and dispatch and state.get("last_dispatched") != current.fingerprint():
        dispatch_workflow(repository, workflow, ref)
        state["last_dispatched"] = current.fingerprint()
        dispatched = True
    state["last_seen"] = asdict(current)
    state["last_checked_at_utc"] = _now()
    _save_state(state_path, state)
    return current, dispatched


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="以两分钟间隔探测官方版本，并在变化时触发现有技能图 CI。"
    )
    parser.add_argument("--interval-seconds", type=int, default=120)
    parser.add_argument("--duration-seconds", type=int, default=0, help="0 表示持续运行")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--dispatch", action="store_true", help="变化时触发 GitHub Actions")
    parser.add_argument("--once", action="store_true", help="只探测一次")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.interval_seconds < 60:
        print("探测间隔不能少于 60 秒", file=sys.stderr)
        return 2
    if args.duration_seconds < 0:
        print("运行时长不能为负数", file=sys.stderr)
        return 2

    started = time.monotonic()
    while True:
        try:
            current, dispatched = probe_once(
                args.state_file,
                dispatch=args.dispatch,
                repository=args.repository,
                workflow=args.workflow,
                ref=args.ref,
            )
            suffix = "；已触发 CI" if dispatched else ""
            print(
                f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}] "
                f"app={current.app_version} asset={current.asset_version} "
                f"master={current.master_version}{suffix}",
                flush=True,
            )
        except Exception as exc:
            print(f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}] 探测失败: {exc}", flush=True)

        if args.once or (
            args.duration_seconds and time.monotonic() - started >= args.duration_seconds
        ):
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
