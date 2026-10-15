# AGENTS.md

## Project Goal

MementoMori APK Tracker must automatically detect game updates, download and validate APK/XAPK packages, preserve all package and Unity serialized data, export supported resources to usable formats, and generate version diffs.

## Core Files

- `run.py`: one-shot and `--watch` automatic update entrypoint.
- `download.py`: apkeep integration, retries, ZIP directory and CRC validation.
- `extract.py`: complete archive expansion, raw object preservation, decoded exports, Prefab derivation, and coverage report.
- `diff.py`: SQLite manifest comparison.
- `asset_cdn.py`: official app/asset version discovery and Addressables catalog resolution.
- `fallback.py`: lightweight critical-image extraction and GitHub repository auto-update.
- `config.json`: update interval, retry, verification, and directory settings.
- `scripts/install_windows_task.ps1`: recurring Windows scheduled task installer.
- `tests/`: standard-library regression tests.

## Completeness Invariant

Extraction may be marked `complete` only when:

- the source package was fully expanded and `package_manifest.json` was written;
- at least one AssetBundle and one Unity object were found;
- every discovered Unity source loaded successfully;
- every enumerated Unity object was written to `objects_raw/`;
- `manifest.sqlite3` contains one indexed row for every enumerated Unity object;
- `extraction_report.json` reports `status: complete` and raw object coverage 1.0.

Interrupted extraction resumes from the committed `manifest.sqlite3.tmp` rows. The manifest uses SQLite WAL durability; a partially indexed Unity source is deleted and re-exported before completion.

Decoded files are additional convenience outputs. Unsupported proprietary structures must remain available through `raw/` and `objects_raw/`, with fallbacks/errors explicitly reported.

## Commands

```bash
python run.py
python run.py --watch
python run.py --check-only
python run.py --version 4.18.0 --force
python fallback.py --remote-info
python fallback.py --auto-update --output fallback_dist
python -m unittest discover -s tests -v
```

## Skill Cards

All future skill cards use the `split` landscape template (3840×2500).
Generation, offline verification, documentation, and the asset repository's
GitHub Actions workflow must use this same mode. Do not generate or publish
the retired `compact` portrait template or `both` mode.

Show official initial speed (`character.speed`, from
`CharacterMB.InitialBattleParameter.Speed`) at the right of the exclusive-weapon
panel header. Do not include equipment, growth, or skill bonuses in that value.
Use `character_release.py` for resumable snapshot-based poster runs. Explicit
unreleased/2100 placeholder previews belong under `preview/`; never change the
formal export date or put those records in the released latest/index files.

## Generated Data

Do not commit `apks/`, `extracted/`, `reports/`, or Python caches. Do not reintroduce AssetStudio, AssetRipper, Unity Editor rebuild tools, or Il2CppDumper unless explicitly requested.
