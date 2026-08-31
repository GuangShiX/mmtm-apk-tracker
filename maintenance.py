"""Discover official MementoMori maintenance windows for CI scheduling."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import msgpack

from asset_cdn import AUTH_URL, _request_with_retries, get_official_app_version

NOTICE_URL = "https://prd1-auth.mememori-boi.com/api/notice/getNoticeInfoList"
FIXED_JST = timezone(timedelta(hours=9))
ALL = 0
GOOGLE_PLAY_STORE = 2


@dataclass(frozen=True)
class OfficialMaintenanceWindow:
    start_at_utc: datetime
    end_at_utc: datetime
    source: str
    notice_id: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "start_at_utc": _format_utc(self.start_at_utc),
            "end_at_utc": _format_utc(self.end_at_utc),
            "source": self.source,
            "notice_id": self.notice_id,
        }


@dataclass(frozen=True)
class MaintenanceDecision:
    should_run: bool
    reason: str
    window: OfficialMaintenanceWindow | None = None
    effective_start_at_utc: datetime | None = None
    effective_end_at_utc: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "should_run": self.should_run,
            "reason": self.reason,
            "window": self.window.to_json() if self.window else None,
            "effective_start_at_utc": (
                _format_utc(self.effective_start_at_utc)
                if self.effective_start_at_utc
                else None
            ),
            "effective_end_at_utc": (
                _format_utc(self.effective_end_at_utc)
                if self.effective_end_at_utc
                else None
            ),
        }


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fixed_jst_wall_clock_to_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        wall_clock = value.replace(tzinfo=None)
    elif isinstance(value, str):
        wall_clock = datetime.fromisoformat(value).replace(tzinfo=None)
    else:
        raise ValueError(f"unsupported maintenance timestamp: {value!r}")
    return wall_clock.replace(tzinfo=FIXED_JST).astimezone(timezone.utc)


def parse_data_uri_maintenance_windows(
    maintenance_infos: Iterable[dict[str, Any]] | None,
) -> tuple[OfficialMaintenanceWindow, ...]:
    windows: list[OfficialMaintenanceWindow] = []
    for maintenance in maintenance_infos or ():
        if maintenance.get("MaintenanceAreaType") != ALL:
            continue

        functions = maintenance.get("MaintenanceFunctionTypes") or []
        if any(int(function) != ALL for function in functions):
            continue

        platforms = maintenance.get("MaintenancePlatformTypes") or []
        if platforms and not any(
            int(platform) in (ALL, GOOGLE_PLAY_STORE) for platform in platforms
        ):
            continue

        try:
            start_at_utc = _fixed_jst_wall_clock_to_utc(
                maintenance.get("StartTimeFixJST")
            )
            end_at_utc = _fixed_jst_wall_clock_to_utc(
                maintenance.get("EndTimeFixJST")
            )
        except (TypeError, ValueError):
            continue
        if end_at_utc <= start_at_utc:
            continue
        windows.append(
            OfficialMaintenanceWindow(
                start_at_utc=start_at_utc,
                end_at_utc=end_at_utc,
                source="data-uri",
            )
        )
    return tuple(sorted(windows, key=lambda window: window.start_at_utc))


_NOTICE_DURATION = re.compile(
    r"Maintenance\s+Duration\s*"
    r"(?P<start>\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})\s*[~～]\s*"
    r"(?P<end>\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})\s*"
    r"\(UTC(?P<hours>[+-]\d{1,2})(?::(?P<minutes>\d{2}))?\)",
    re.IGNORECASE | re.DOTALL,
)


def parse_notice_maintenance_window(
    main_text: str,
    *,
    notice_id: int | None = None,
) -> OfficialMaintenanceWindow | None:
    plain_text = html.unescape(re.sub(r"<[^>]+>", "\n", main_text))
    match = _NOTICE_DURATION.search(plain_text)
    if not match:
        return None

    hours = int(match.group("hours"))
    minute_value = int(match.group("minutes") or 0)
    if hours < 0:
        minute_value = -minute_value
    source_timezone = timezone(timedelta(hours=hours, minutes=minute_value))
    start = datetime.strptime(match.group("start"), "%Y/%m/%d %H:%M")
    end = datetime.strptime(match.group("end"), "%Y/%m/%d %H:%M")
    return OfficialMaintenanceWindow(
        start_at_utc=start.replace(tzinfo=source_timezone).astimezone(timezone.utc),
        end_at_utc=end.replace(tzinfo=source_timezone).astimezone(timezone.utc),
        source="notice",
        notice_id=notice_id,
    )


def parse_notice_maintenance_windows(
    notices: Iterable[dict[str, Any]] | None,
) -> tuple[OfficialMaintenanceWindow, ...]:
    windows: list[OfficialMaintenanceWindow] = []
    for notice in notices or ():
        title = str(notice.get("Title") or notice.get("ButtonTitle") or "")
        if "maintenance" not in title.casefold():
            continue
        window = parse_notice_maintenance_window(
            str(notice.get("MainText") or ""),
            notice_id=int(notice["Id"]) if notice.get("Id") is not None else None,
        )
        if window:
            windows.append(window)
    return tuple(sorted(windows, key=lambda window: window.start_at_utc))


def _api_headers(app_version: str) -> dict[str, str]:
    return {
        "content-type": "application/json; charset=UTF-8",
        "ortegaaccesstoken": "",
        "ortegaappversion": app_version,
        "ortegadevicetype": "2",
        "ortegauuid": "0123456789abcdef0123456789abcdef",
        "accept-encoding": "gzip",
        "user-agent": "BestHTTP/2 v2.3.0",
    }


def _post_messagepack(url: str, app_version: str, body: dict[str, Any]) -> dict[str, Any]:
    response = _request_with_retries(
        "POST",
        url,
        headers=_api_headers(app_version),
        data=msgpack.packb(body),
        timeout=30,
    )
    status_code = response.headers.get("ortegastatuscode")
    if status_code and status_code != "0":
        raise RuntimeError(f"official API returned ortegastatuscode={status_code}")
    data = msgpack.unpackb(response.content, raw=False, timestamp=3)
    if not isinstance(data, dict):
        raise RuntimeError("official API returned a non-object MessagePack response")
    return data


def fetch_official_maintenance_windows() -> tuple[OfficialMaintenanceWindow, ...]:
    app_version = get_official_app_version()
    errors: list[str] = []
    data_uri_windows: tuple[OfficialMaintenanceWindow, ...] = ()
    notice_windows: tuple[OfficialMaintenanceWindow, ...] = ()

    try:
        data_uri = _post_messagepack(
            AUTH_URL,
            app_version,
            {"CountryCode": "CN", "UserId": 0},
        )
        data_uri_windows = parse_data_uri_maintenance_windows(
            data_uri.get("MaintenanceInfos")
        )
    except Exception as exc:
        errors.append(f"getDataUri: {exc}")

    try:
        notice_data = _post_messagepack(
            NOTICE_URL,
            app_version,
            {
                "AccessType": 1,
                "CountryCode": "US",
                "LanguageType": 2,
                "UserId": 0,
            },
        )
        notice_windows = parse_notice_maintenance_windows(
            notice_data.get("NoticeInfoList")
        )
    except Exception as exc:
        errors.append(f"notice: {exc}")

    if not data_uri_windows and not notice_windows and len(errors) == 2:
        raise RuntimeError("; ".join(errors))

    # Prefer the structured getDataUri record when the same window is also
    # present in a human-readable announcement.
    unique: dict[tuple[datetime, datetime], OfficialMaintenanceWindow] = {}
    for window in (*data_uri_windows, *notice_windows):
        unique.setdefault((window.start_at_utc, window.end_at_utc), window)
    return tuple(sorted(unique.values(), key=lambda window: window.start_at_utc))


def evaluate_maintenance_window(
    windows: Iterable[OfficialMaintenanceWindow],
    now_utc: datetime,
    *,
    start_offset_minutes: int = 15,
    end_offset_minutes: int = 15,
) -> MaintenanceDecision:
    now = now_utc.astimezone(timezone.utc)
    for window in windows:
        effective_start = window.start_at_utc + timedelta(
            minutes=start_offset_minutes
        )
        effective_end = window.end_at_utc + timedelta(minutes=end_offset_minutes)
        if effective_start <= now <= effective_end:
            return MaintenanceDecision(
                should_run=True,
                reason="maintenance-window",
                window=window,
                effective_start_at_utc=effective_start,
                effective_end_at_utc=effective_end,
            )
    return MaintenanceDecision(False, "outside-maintenance-window")


def _write_github_output(path: Path | None, decision: MaintenanceDecision) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"should_run={'true' if decision.should_run else 'false'}\n")
        output.write(f"reason={decision.reason}\n")
        output.write(
            "effective_start_at_utc="
            f"{_format_utc(decision.effective_start_at_utc) if decision.effective_start_at_utc else ''}\n"
        )
        output.write(
            "effective_end_at_utc="
            f"{_format_utc(decision.effective_end_at_utc) if decision.effective_end_at_utc else ''}\n"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect official maintenance announcements and gate CI updates."
    )
    parser.add_argument("--start-offset-minutes", type=int, default=15)
    parser.add_argument("--end-offset-minutes", type=int, default=15)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--now-utc", help="Override current UTC time for validation")
    parser.add_argument("--github-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    force = args.force or os.environ.get("FORCE_UPDATE", "").casefold() == "true"
    if force:
        decision = MaintenanceDecision(True, "forced-baseline-or-manual")
        windows: tuple[OfficialMaintenanceWindow, ...] = ()
    else:
        windows = fetch_official_maintenance_windows()
        now = (
            datetime.fromisoformat(args.now_utc.replace("Z", "+00:00"))
            if args.now_utc
            else datetime.now(timezone.utc)
        )
        decision = evaluate_maintenance_window(
            windows,
            now,
            start_offset_minutes=args.start_offset_minutes,
            end_offset_minutes=args.end_offset_minutes,
        )

    print(
        json.dumps(
            {
                "decision": decision.to_json(),
                "maintenance_windows": [window.to_json() for window in windows],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    output_path = args.github_output
    if output_path is None and os.environ.get("GITHUB_OUTPUT"):
        output_path = Path(os.environ["GITHUB_OUTPUT"])
    _write_github_output(output_path, decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
