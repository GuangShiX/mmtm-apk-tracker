"""Verify exactly the requested card set without changing official snapshots."""
from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path
from typing import Callable, Iterable


def _validated_ids(ids: Iterable[int]) -> list[int]:
    selected = list(ids)
    if not selected or any(type(cid) is not int or cid <= 0 for cid in selected):
        raise ValueError("CharacterIds must be a nonempty set of positive integers")
    if len(set(selected)) != len(selected):
        raise ValueError("CharacterIds must be unique")
    return selected


def verify_selected(
    skills_dir: Path,
    output_dir: Path,
    ids: Iterable[int],
    *,
    language: str = "zh-CN",
    verifier: Callable | None = None,
) -> list[Path]:
    """Build an ephemeral selection view; retain every source payload byte."""
    selected = _validated_ids(ids)
    skills_dir, output_dir = Path(skills_dir), Path(output_dir)
    payloads: list[tuple[int, bytes]] = []
    for cid in selected:
        source = skills_dir / "characters" / f"{cid:06d}.json"
        original = source.read_bytes()
        payload = json.loads(original)
        character = payload.get("character") if isinstance(payload, dict) else None
        if not isinstance(character, dict) or type(character.get("id")) is not int or character["id"] != cid:
            raise ValueError(f"Character payload identity does not match requested ID {cid}")
        payloads.append((cid, original))
    if verifier is None:
        from skill_card import verify_latest_skill_cards
        verifier = verify_latest_skill_cards
    with tempfile.TemporaryDirectory(prefix="mmtm-card-selection-") as temporary:
        view = Path(temporary) / "skills"
        characters = view / "characters"
        characters.mkdir(parents=True)
        for cid, original in payloads:
            (characters / f"{cid:06d}.json").write_bytes(original)
        (view / "latest.json").write_text(
            json.dumps({
                "selection": "temporary-render-request-verification-only",
                "notice": "This is an ephemeral verification view, not official latest-release metadata.",
                "latest_characters": [{"id": cid} for cid in selected],
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return verifier(view, output_dir, language=language, mode="split")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--character-ids", required=True, help="Comma-separated requested CharacterIds")
    parser.add_argument("--skills-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--language", default="zh-CN")
    args = parser.parse_args()
    try:
        tokens = args.character_ids.split(",")
        if any(not re.fullmatch(r"[0-9]+", token) for token in tokens):
            raise ValueError("CharacterIds must be comma-separated positive integers")
        verified = verify_selected(args.skills_dir, args.output_dir, [int(token) for token in tokens], language=args.language)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Selected card verification failed: {exc}\n")
    print(json.dumps({"verified_character_ids": [int(token) for token in tokens], "cards": [str(path) for path in verified]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
