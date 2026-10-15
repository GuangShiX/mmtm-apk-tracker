"""Export an official character snapshot and render resumable split posters."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Callable, Iterable
import unicodedata

from PIL import Image

from asset_cdn import (
    OfficialAssetInfo, download_catalog, download_file, get_official_asset_info,
    load_catalog,
)
from cloud_verify_cards import verify_selected
from master import (
    CORE_BOOKS, GENERATED_MARKER, JST, LANGUAGE_BOOKS, MasterSource,
    _arcana_index, _book_path, _catalog_map, _character_payload,
    _exclusive_weapon_index, _parse_jst, _safe_id_map, _text_map, _validate_book,
    build_master_skill_repository, load_current_master_skill_repository,
    load_master_books,
)
from skill_card import (
    CardAssets, SPLIT_HEIGHT, SPLIT_TEMPLATE_VERSION, SPLIT_WIDTH,
    _card_manifest_key, _compact_skill_records, _font, _font_candidates,
    _update_card_manifest, fetch_official_card_assets, render_skill_card,
)


ROOT = Path(__file__).resolve().parent
REPOSITORY = "GuangShiX/mmtm-assets-fallback"
DEFAULT_CACHE = ROOT / "reports" / "character-release-cache"
LEGACY_CACHE = ROOT / "reports" / "skill-card-cache"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _positive_ids(values: Iterable[int]) -> list[int]:
    result = list(dict.fromkeys(values))
    if any(type(value) is not int or value <= 0 for value in result):
        raise ValueError("CharacterIds must be positive integers")
    return result


def source_snapshot(
    source_context: Path | None, *, offline: bool,
    discover: Callable[[], OfficialAssetInfo] = get_official_asset_info,
) -> tuple[OfficialAssetInfo, Path | None]:
    """Discover once, or reuse the explicit immutable context without probing."""
    if source_context is None:
        if offline:
            raise RuntimeError("--offline requires --source-context")
        info, raw = discover(), None
    else:
        context = read_json(source_context)
        if not isinstance(context, dict):
            raise ValueError("Source context must be a JSON object")
        fields = OfficialAssetInfo.__dataclass_fields__
        info = OfficialAssetInfo(**{
            name: context[name] for name in fields if name in context
        })
        raw_value = context.get("raw_master_dir")
        raw = Path(raw_value) if isinstance(raw_value, str) and raw_value else None
        if raw is not None and not raw.is_absolute():
            raw = source_context.resolve().parent / raw
    for name in ("app_version", "asset_version", "master_version"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", getattr(info, name)):
            raise ValueError(f"Invalid snapshot {name}")
    return info, raw.resolve() if raw is not None else None


def cache_master_books(
    info: OfficialAssetInfo, raw_dir: Path, languages: Iterable[str], *,
    offline: bool = False, downloader: Callable = download_file,
) -> dict[str, Any]:
    """Only fetch missing or catalog-invalid books; install verified bytes atomically."""
    languages = tuple(dict.fromkeys(languages))
    if not languages or any(language not in LANGUAGE_BOOKS for language in languages):
        raise ValueError("At least one supported language is required")
    raw_dir.mkdir(parents=True, exist_ok=True)
    try:
        catalog = _catalog_map(raw_dir)
    except (OSError, RuntimeError, ValueError):
        if offline:
            raise RuntimeError(f"Offline master-catalog is missing or invalid: {raw_dir}")
        target = raw_dir / "master-catalog"
        temporary = raw_dir / "master-catalog.download"
        try:
            downloader(info.master_url("master-catalog"), temporary)
            # Validate its shape before replacing any existing snapshot catalog.
            from master import _unpack
            downloaded = _unpack(temporary)
            if not isinstance(downloaded, dict) or not isinstance(downloaded.get("MasterBookInfoMap"), dict):
                raise RuntimeError("Downloaded master-catalog has no MasterBookInfoMap")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        catalog = _catalog_map(raw_dir)
    reused, downloaded = [], []
    names = list(dict.fromkeys([*CORE_BOOKS, *(LANGUAGE_BOOKS[item] for item in languages)]))
    for name in names:
        metadata = catalog.get(name)
        if not isinstance(metadata, dict):
            raise RuntimeError(f"Snapshot master-catalog is missing {name}")
        try:
            target = _book_path(raw_dir, name)
            _validate_book(target, name, metadata)
        except (OSError, RuntimeError, ValueError):
            if offline:
                raise RuntimeError(f"Offline MasterBook is missing or corrupt: {name}")
            target = raw_dir / name
            temporary = raw_dir / f"{name}.download"
            try:
                downloader(info.master_url(name), temporary)
                _validate_book(temporary, name, metadata)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            downloaded.append(name)
        else:
            reused.append(name)
    return {
        "raw_master_dir": str(raw_dir.resolve()),
        "catalog_sha256": sha256(_book_path(raw_dir, "master-catalog")),
        "reused_books": reused, "downloaded_books": downloaded,
    }


def shared_asset_catalog(
    info: OfficialAssetInfo, cache_dir: Path, *, offline: bool = False,
    downloader: Callable = download_catalog, legacy_cache: Path = LEGACY_CACHE,
) -> Path:
    """Share one catalog per assetVersion, including previous character caches."""
    target = cache_dir / info.asset_version / "catalog.json"
    candidates = [target]
    for base in dict.fromkeys((cache_dir, legacy_cache)):
        version_dir = base / info.asset_version
        candidates.extend(sorted(version_dir.glob("*/catalog.json")))
        candidates.extend(sorted(version_dir.glob("*/catalog-curl.json")))
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            load_catalog(candidate)
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate != target:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name("catalog.json.copy")
            shutil.copyfile(candidate, temporary)
            temporary.replace(target)
        return target
    if offline:
        raise RuntimeError(f"Offline asset catalog is missing: {info.asset_version}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name("catalog.json.download")
    try:
        downloader(info, temporary)
        load_catalog(temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def export_previews(
    raw_dir: Path, output: Path, info: OfficialAssetInfo, ids: Iterable[int],
    languages: Iterable[str], now: datetime,
) -> list[Path]:
    """Export selected future rows directly; never advance formal release time."""
    selected = _positive_ids(ids)
    if not selected:
        return []
    languages = tuple(languages)
    tables, metadata = load_master_books(raw_dir, languages)
    characters = _safe_id_map(tables["CharacterMB"], "CharacterMB")
    text_maps = {language: _text_map(tables[LANGUAGE_BOOKS[language]]) for language in languages}
    active = _safe_id_map(tables["ActiveSkillMB"], "ActiveSkillMB")
    passive = _safe_id_map(tables["PassiveSkillMB"], "PassiveSkillMB")
    weapons, arcanas = _exclusive_weapon_index(tables, text_maps), _arcana_index(tables, text_maps)
    source = MasterSource(info.app_version, info.asset_version, info.master_version)
    entries, paths = [], []
    for cid in selected:
        row = characters.get(cid)
        if row is None or row.get("IsIgnore"):
            raise ValueError(f"Preview CharacterId {cid} is absent or ignored")
        start = _parse_jst(row.get("StartTimeFixJST"))
        if start <= now:
            raise ValueError(f"CharacterId {cid} is already released; use --character-id")
        placeholder = start.year >= 2100
        payload = _character_payload(row, active, passive, text_maps, weapons, arcanas, source, metadata)
        payload.update(
            preview_notice="未实装预览",
            release_status="unreleased-placeholder" if placeholder else "unreleased-scheduled",
            release_notice=("2100 年日期是官方占位记录，不代表真实实装安排。" if placeholder
                            else "按官方候选数据制作；角色尚未实装，内容可能调整。"),
        )
        path = output / "preview" / "skills" / "characters" / f"{cid:06d}.json"
        write_json(path, payload)
        paths.append(path)
        entries.append({
            "id": cid, "path": path.relative_to(output).as_posix(),
            "sha256": sha256(path), "size": path.stat().st_size,
            "release_status": payload["release_status"],
            "official_placeholder_time_jst": row.get("StartTimeFixJST"),
            "localization_complete": payload["localization_complete"],
        })
    index = {
        "schema_version": 1, "game": "MementoMori", "preview": True,
        "preview_notice": "未实装预览；2100 为占位日期。正式最新角色以 skills/latest.json 为准。",
        "source": payload["source"], "characters": entries,
    }
    write_json(output / "preview" / "index.json", index)
    write_json(output / "preview" / "skills" / "index.json", index)
    write_json(output / "preview" / "skills" / "manifest.json", {
        **index, "files": entries,
    })
    marker = output / "preview" / "skills" / GENERATED_MARKER
    marker.write_text("Generated isolated unreleased previews.\n", encoding="utf-8")
    return paths


def actual_fonts() -> list[dict[str, Any]]:
    fonts = _font_candidates()
    result = []
    for role, path, options, index in (
        ("sans", fonts.sans, {}, fonts.sans_index),
        ("medium", fonts.medium, {"medium": True}, fonts.medium_index),
        ("serif", fonts.serif, {"serif": True}, fonts.serif_index),
    ):
        family, style = _font(fonts, 48, **options).getname()
        normalized = unicodedata.normalize("NFKC", family + " " + str(path)).casefold()
        if any(token in normalized for token in ("georgia", "ms gothic", "ms ゴシック")):
            raise RuntimeError(f"Prohibited font selected: {family}")
        if "noto" not in normalized:
            raise RuntimeError(f"The maintained template requires Noto fonts: {family}")
        result.append({"role": role, "family": family, "style": style, "index": index,
                       "path": str(path.resolve()), "sha256": sha256(path)})
    return result


@dataclass
class ReleaseServices:
    """Explicit I/O boundaries for repeatable offline tests and integrations."""
    discover: Callable = get_official_asset_info
    download: Callable = download_file
    catalog_download: Callable = download_catalog
    fetch_assets: Callable = fetch_official_card_assets
    render: Callable = render_skill_card
    verify: Callable = verify_selected
    fonts: Callable = actual_fonts


def _png(candidates: Iterable[Path]) -> Path | None:
    for path in dict.fromkeys(candidates):
        if not path.is_file():
            continue
        try:
            with Image.open(path) as image:
                if image.format != "PNG" or min(image.size) < 1:
                    continue
                image.verify()
        except (OSError, ValueError):
            continue
        return path
    return None


def cached_card_assets(
    payload: dict[str, Any], info: OfficialAssetInfo, output: Path,
    cache_dir: Path, *, legacy_cache: Path = LEGACY_CACHE,
) -> CardAssets:
    cid = payload["character"]["id"]
    cache_roots = tuple(dict.fromkeys((cache_dir, legacy_cache)))
    images = [base / info.asset_version / f"{cid:06d}" / "images" for base in cache_roots]
    local_ui = (output / "assets" / "ui", legacy_cache.parent / "official-poster-ui",
                ROOT / "reports" / "official-poster-ui")
    skills = _compact_skill_records(payload, "zh-CN")
    icons = {
        skill["id"]: path for skill in skills
        if (path := _png([*(base / f"CSK_{skill['id']:09d}.png" for base in images),
                          output / "assets" / "skills" / f"CSK_{skill['id']:09d}.png"]))
    }
    def image(name: str) -> Path | None:
        return _png(base / name for base in images)
    avatar_name = f"CHR_{cid:06d}_00_s.png"
    avatar = _png([output / "assets" / "characters" / avatar_name,
                   *(base / avatar_name for base in images)])
    arcana_ids = _arcana_ids(payload)
    arcana = {}
    for required in arcana_ids:
        name = f"CHR_{required:06d}_00_s.png"
        candidates = [output / "assets" / "characters" / name,
                      legacy_cache.parent / "official-poster-ui" / name,
                      ROOT / "reports" / "official-poster-ui" / name,
                      *(base / name for base in images)]
        candidates.extend(base / info.asset_version / f"{required:06d}" / "images" / name
                          for base in cache_roots)
        if (path := _png(candidates)) is not None:
            arcana[required] = path
    weapon = payload.get("exclusive_weapon") or {}
    weapon_id = weapon.get("icon_id")
    weapon_name = f"EQP_{weapon_id:06d}.png" if isinstance(weapon_id, int) else None
    weapon_path = (_png([output / "assets" / "equipment" / weapon_name,
                         *(base / weapon_name for base in images)]) if weapon_name else None)
    element = payload["character"].get("element_type")
    job = {1: "warrior", 2: "sniper", 4: "sorcerer"}.get(payload["character"].get("job_flags"))
    banners = [base / info.asset_version / f"{cid:06d}" / "banners" /
               f"MYPAGE_BANNER_CN_{variant}{cid:04d}.png"
               for variant in ("01", "02") for base in cache_roots]
    return CardAssets(
        art=image(f"CHR_{cid:06d}_00_l.png"), icons=icons,
        asset_version=info.asset_version, avatar=avatar, weapon=weapon_path,
        element=_png(base / f"icon_element_{element}.png" for base in local_ui),
        job_icon=_png(base / f"icon_job_{job}.png" for base in local_ui) if job else None,
        arcana_characters=arcana, banner=_png(banners),
    )


def _arcana_ids(payload: dict[str, Any]) -> list[int]:
    return sorted({row["id"] for arcana in payload.get("arcanas") or []
                   for row in arcana.get("required_characters") or []
                   if type(row.get("id")) is int and row["id"] > 0})


def asset_evidence(assets: CardAssets, payload: dict[str, Any]) -> tuple[dict, list[str]]:
    paths = {"full_art": assets.art, "banner": assets.banner,
             "character_avatar": assets.avatar, "exclusive_weapon": assets.weapon,
             "element_icon": assets.element, "job_icon": assets.job_icon}
    paths.update({f"skill_icon:{row['id']}": assets.icons.get(row["id"])
                  for row in _compact_skill_records(payload, "zh-CN")})
    paths.update({f"arcana_avatar:{cid}": assets.arcana_characters.get(cid)
                  for cid in _arcana_ids(payload)})
    found, missing = {}, []
    for label, path in paths.items():
        if path is None or _png([path]) is None:
            missing.append(label)
        else:
            found[label] = {"path": str(path.resolve()), "sha256": sha256(path),
                            "size": path.stat().st_size}
    if not payload.get("arcanas"):
        missing.append("arcana_data")
    if not payload.get("exclusive_weapon"):
        missing.append("exclusive_weapon_data")
    return found, sorted(missing)


def _merge_assets(cached: CardAssets, fetched: CardAssets) -> CardAssets:
    if fetched.asset_version != cached.asset_version:
        raise RuntimeError("Asset snapshot changed during poster generation")
    return replace(
        cached, art=cached.art or fetched.art, icons={**fetched.icons, **cached.icons},
        weapon=cached.weapon or fetched.weapon, avatar=cached.avatar or fetched.avatar,
        banner=cached.banner or fetched.banner,
        arcana_characters={**fetched.arcana_characters, **cached.arcana_characters},
    )


def _signature(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def run_release(
    *, output: Path, character_ids: Iterable[int] = (), preview_ids: Iterable[int] = (),
    cache_dir: Path = DEFAULT_CACHE, source_context: Path | None = None,
    asset_cache_dir: Path | None = None,
    offline: bool = False, languages: Iterable[str] = ("zh-CN", "ja-JP"),
    services: ReleaseServices | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    services = services or ReleaseServices()
    now = (now or datetime.now(JST)).astimezone(JST)
    output, cache_dir = output.resolve(), cache_dir.resolve()
    ids, previews = _positive_ids(character_ids), _positive_ids(preview_ids)
    if set(ids) & set(previews):
        raise ValueError("The same CharacterId cannot be both released and preview")
    languages = tuple(dict.fromkeys(languages))
    if "zh-CN" not in languages:
        raise ValueError("The maintained poster language is zh-CN")
    info, supplied_raw = source_snapshot(source_context, offline=offline, discover=services.discover)
    legacy_candidates = []
    if source_context is not None:
        legacy_candidates.append(source_context.resolve().parent / "skill-card-cache")
    if supplied_raw is not None:
        legacy_candidates.append(supplied_raw.parent / "skill-card-cache")
    legacy_candidates.append(LEGACY_CACHE)
    legacy_cache = asset_cache_dir.resolve() if asset_cache_dir is not None else next(
        (path for path in legacy_candidates if (path / info.asset_version).is_dir()), LEGACY_CACHE)
    run_dir = cache_dir / "runs" / info.master_version
    state_path = run_dir / "state.json"
    state = read_json(state_path, {"cards": {}})
    if not isinstance(state, dict) or not isinstance(state.get("cards"), dict):
        raise RuntimeError(f"Invalid persisted release state: {state_path}")
    raw_dir = supplied_raw or cache_dir / "master" / info.master_version
    snapshot = {**asdict(info), "raw_master_dir": str(raw_dir)}
    write_json(run_dir / "source-context.json", snapshot)
    def progress(stage: str, **details: Any) -> None:
        if stage != "failed":
            state.pop("error", None)
        state.update(stage=stage, source=snapshot, output=str(output), offline=offline,
                     updated_at=datetime.now(JST).isoformat(), **details)
        write_json(state_path, state)
        print(f"[{stage}] {details.get('character_id', '')}", flush=True)
    try:
        progress("master-validation")
        master_cache = cache_master_books(info, raw_dir, languages, offline=offline,
                                          downloader=services.download)
        state["master_cache"] = master_cache
        progress("skill-export")
        latest = load_current_master_skill_repository(output, info.master_version, languages, now)
        expected = {key: value for key, value in asdict(info).items()
                    if key in ("app_version", "asset_version", "master_version")}
        if latest is None or any(latest["source"].get(key) != value for key, value in expected.items()):
            latest = build_master_skill_repository(
                raw_dir, output, REPOSITORY,
                MasterSource(info.app_version, info.asset_version, info.master_version),
                languages, now,
            )
        preview_paths = export_previews(raw_dir, output, info, previews, languages, now)
        if not ids:
            ids = [row["id"] for row in latest["latest_characters"]]
        source_sets = [(output, output / "skills" / "characters" / f"{cid:06d}.json", False)
                       for cid in ids]
        source_sets.extend((output / "preview", path, True) for path in preview_paths)
        for _, path, _ in source_sets:
            if not path.is_file():
                raise ValueError(f"Released CharacterId is not in the current snapshot: {path.stem}")
        progress("asset-catalog")
        catalog = shared_asset_catalog(info, cache_dir, offline=offline,
                                       downloader=services.catalog_download, legacy_cache=legacy_cache)
        state["asset_catalog"] = {"path": str(catalog), "sha256": sha256(catalog)}
        fonts = services.fonts()
        rendered, skipped = [], []
        for destination, source, preview in source_sets:
            payload = read_json(source)
            cid = payload["character"]["id"]
            skills = _compact_skill_records(payload, "zh-CN")
            target = destination / "cards" / "characters" / f"character-{cid:06d}-split-zh-CN.png"
            key = ("preview:" if preview else "released:") + str(cid)
            assets = cached_card_assets(payload, info, output, cache_dir, legacy_cache=legacy_cache)
            evidence, missing = asset_evidence(assets, payload)
            speed = payload["character"].get("speed")
            if speed is None:
                speed = (payload["character"].get("master_record", {}).get("InitialBattleParameter") or {}).get("Speed")
            signature = _signature({"source_sha256": sha256(source), "snapshot": expected,
                                    "template": SPLIT_TEMPLATE_VERSION, "fonts": fonts,
                                    "initial_speed": speed, "preview": preview})
            previous = state["cards"].get(key, {})
            current_hashes = {label: item["sha256"] for label, item in evidence.items()}
            previous_hashes = {label: item["sha256"] for label, item in previous.get("official_assets", {}).items()}
            if (previous.get("input_signature") == signature and
                previous.get("missing_assets") == missing and previous_hashes == current_hashes and
                target.is_file() and previous.get("sha256") == sha256(target)):
                try:
                    services.verify(destination / "skills", target.parent, [cid], language="zh-CN")
                except (OSError, RuntimeError, ValueError):
                    pass
                else:
                    state["cards"][key].update(output_path=str(target), official_assets=evidence)
                    skipped.append(str(target))
                    progress("card-reused", character_id=cid)
                    continue
            progress("card-assets", character_id=cid)
            needs_fetch = any(label in missing for label in
                              ("full_art", "banner", "character_avatar", "exclusive_weapon")) or any(
                                  label.startswith(("skill_icon:", "arcana_avatar:")) for label in missing)
            if not offline and needs_fetch:
                weapon = payload.get("exclusive_weapon") or {}
                fetched = services.fetch_assets(
                    cid, [row["id"] for row in skills if row["id"] not in assets.icons], cache_dir,
                    art_size="l", catalog_path=catalog,
                    weapon_icon_id=weapon.get("icon_id") if assets.weapon is None else None,
                    include_avatar=assets.avatar is None, element_icon=assets.element,
                    arcana_character_ids=[item for item in _arcana_ids(payload)
                                          if item not in assets.arcana_characters],
                    include_banner=assets.banner is None, banner_language="CN",
                    allow_missing=True, official_info=info,
                )
                assets = _merge_assets(assets, fetched)
                evidence, missing = asset_evidence(assets, payload)
            required_missing = [label for label in missing if label == "full_art" or label.startswith("skill_icon:")]
            if len(skills) != 4:
                raise RuntimeError(f"Character {cid} does not have four display skills")
            if required_missing and not preview:
                raise RuntimeError(f"Released character {cid} is missing official images: {', '.join(required_missing)}")
            progress("card-render", character_id=cid, missing_assets=missing)
            services.render(payload, assets, target, language="zh-CN", mode="split")
            entry = {
                "key": _card_manifest_key(cid, "zh-CN", "split"),
                "character_id": cid, "language": "zh-CN", "mode": "split",
                "path": target.relative_to(destination / "cards").as_posix(),
                "source_sha256": sha256(source), "template_version": SPLIT_TEMPLATE_VERSION,
                "asset_version": info.asset_version, "width": SPLIT_WIDTH, "height": SPLIT_HEIGHT,
                "sha256": sha256(target), "font_families": [row["family"] for row in fonts],
                "fonts": fonts, "initial_speed": speed, "official_assets": evidence,
                "missing_assets": missing, "release_status": payload.get("release_status", "released"),
                "input_signature": signature,
            }
            _update_card_manifest(destination / "cards" / "manifest.json", [entry])
            services.verify(destination / "skills", target.parent, [cid], language="zh-CN")
            state["cards"][key] = {**entry, "output_path": str(target)}
            rendered.append(str(target))
            progress("card-verified", character_id=cid)
        progress("complete", character_id=None, rendered=rendered, skipped=skipped,
                 released_character_ids=ids, preview_character_ids=previews)
        return {"source": snapshot, "state_file": str(state_path), "rendered": rendered,
                "skipped": skipped, "released_character_ids": ids, "preview_character_ids": previews}
    except Exception as exc:
        progress("failed", error=f"{type(exc).__name__}: {exc}")
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "fallback_dist")
    parser.add_argument("--character-id", type=int, action="append", default=[])
    parser.add_argument("--preview-id", type=int, action="append", default=[])
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--source-context", type=Path)
    parser.add_argument("--asset-cache-dir", type=Path,
                        help="Existing skill-card-cache root, including from another worktree")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_release(output=args.output, character_ids=args.character_id,
                             preview_ids=args.preview_id, cache_dir=args.cache_dir,
                             source_context=args.source_context, asset_cache_dir=args.asset_cache_dir,
                             offline=args.offline)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        parser.exit(1, f"Character release failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
