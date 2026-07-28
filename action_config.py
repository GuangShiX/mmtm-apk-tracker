"""Decode monologue and memory action data from a complete extraction.

This module does not decrypt a cryptographic container.  The game stores the
scenario actions as Unity TypeTree data and the Live2D motions as serialized
AnimationClip curves.  The exporter adds readable enum names to the former and
converts the latter to Cubism motion3 JSON.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import shutil
import sqlite3
import struct
import sys
import zlib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

ANIMATION_TYPES = {
    0: "None",
    100: "EF_Idle",
    1000: "Idle",
    4000: "Talk_In",
    5000: "Talk_Loop",
    6000: "Talk_Out",
    7000: "Memory_In",
    8000: "Memory_Loop",
    8100: "Memory_Loop_SP",
    9000: "Memory_Out",
    10000: "Skill",
    11000: "Memory_Out_End",
    12000: "Idle_Action",
    13000: "Monologue_Loop",
}

EMOTION_FLAGS = {
    1: "Idle_Expression",
    2: "Face_Angry",
    4: "Face_Glad",
    8: "Face_Sad",
    16: "Face_Smile",
    32: "Face_Surprise",
    64: "Face_Tears",
    128: "Face_TearDrop",
    256: "Face_Eye_Grin",
    512: "Face_Blushing",
    524288: "Face_Eye_Close",
    1048576: "Face_Look_Up",
    2097152: "Face_Look_Down",
    4194304: "Face_Look_Right",
    8388608: "Face_Look_Left",
    16777216: "Face_Unique1",
    33554432: "Face_Unique2",
    67108864: "Face_Unique3",
    134217728: "QliphaOpenEyes",
    268435456: "Face_Unloop1",
}

LOOP_ANIMATION_TYPES = {
    0: "Loop1",
    1: "Loop2",
    2: "Loop3",
    3: "Loop4",
    1000: "LoopBlink",
}

BLINK_ANIMATION_TYPES = {
    0: "First",
    1: "Slow",
}

CHARACTER_BASE_POINT_TYPES = {
    -1: "None",
    0: "Face",
    1: "Chest",
    2: "MyPage",
    3: "BattleSkill",
    4: "TalkFace",
    1000: "MemoryFace",
    1001: "MemoryChest",
    1002: "MemoryFull",
    2000: "NewCharacter",
    3000: "Monologue",
}

LANGUAGE_TYPES = {
    0: "None",
    1: "jaJP",
    2: "enUS",
    3: "koKR",
    4: "zhTW",
    5: "frFR",
    6: "zhCN",
    7: "esMX",
    8: "ptBR",
    9: "thTH",
    10: "idID",
    11: "viVN",
    12: "ruRU",
    13: "deDE",
}

SCENARIO_TYPES = {
    0: "Memory",
    1: "Tutorial",
}

MEMORY_IN_OUT_TYPES = {
    0: "None",
    1: "InOut",
    2: "InOutEnd",
}

CHAPTER_TYPES = {
    0: "Main",
    1: "MemoryIn",
    2: "MemoryOut",
    3: "MemoryOutEnd",
    4: "Sub",
}

SCENARIO_ANIMATION_TYPES = {
    1000: "MEManim_default",
    2000: "MEManim_happy",
    3000: "MEManim_sad",
    4000: "MEManim_bloom",
    5000: "MEManim_bright",
    6000: "MEManim_memoryIn",
    7000: "MEManim_memoryOut",
    8000: "MEManim_memoryOutEnd",
    9000: "MEManim_blackout",
    10000: "MEManim_whiteout",
}

SCENARIO_EFFECT_TYPES = {
    0: "Normal",
    1: "FadeIn",
    2: "FadeOut",
    3: "ChangeColor",
    4: "Blur",
    5: "FadeInOut",
}

FADE_TYPES = {
    0: "None",
    1: "CrossFade",
    2: "WhiteIn",
    3: "WhiteOut",
    4: "BlackIn",
    5: "BlackOut",
}

LIVE2D_EFFECT_TYPES = {
    0: "None",
    1: "Play",
    2: "Stop",
}

CHARACTER_ALPHA_MASK_TYPES = {
    0: "None",
    1: "Off",
    2: "Right",
    3: "Left",
}

SCENARIO_BLINK_ASSET_TYPES = {
    0: "None",
    1: "Default",
    2: "Long",
    3: "Stop",
    4: "Auto",
}

TEXT_COLOR_TYPES = {
    -1: "Original",
    0: "None",
    1000: "Black",
    2000: "White",
    3000: "DarkRed",
    3100: "LightRed",
    4000: "DarkBlue",
    5000: "LightBlue",
    6000: "DarkGreen",
    7000: "LightGreen",
    8000: "Purple",
    9000: "DarkYellow",
    10000: "LightYellow",
    11000: "Pink",
    12000: "Gray",
    90000: "Unique1",
    90001: "Unique2",
    90002: "Unique3",
    90003: "Unique4",
    90004: "Unique5",
    90005: "Unique6",
}

ANIMATION_EVENT_TYPES = {
    0: "None",
    100: "SkillImpact",
}

MONOLOGUE_TEXT_TYPES = {
    0: "None",
    1: "SongLyrics",
    2: "Monologue",
}

MONOLOGUE_BGM_TYPES = {
    0: "Lament",
    1: "LamentAndVoice",
}

ENUM_MAPS = {
    "AnimationType": ANIMATION_TYPES,
    "EmotionFlags": EMOTION_FLAGS,
    "LoopAnimationType": LOOP_ANIMATION_TYPES,
    "BlinkAnimationType": BLINK_ANIMATION_TYPES,
    "CharacterBasePointType": CHARACTER_BASE_POINT_TYPES,
    "LanguageType": LANGUAGE_TYPES,
    "ScenarioType": SCENARIO_TYPES,
    "MemoryInOutType": MEMORY_IN_OUT_TYPES,
    "ChapterType": CHAPTER_TYPES,
    "ScenarioAnimationType": SCENARIO_ANIMATION_TYPES,
    "ScenarioEffectType": SCENARIO_EFFECT_TYPES,
    "FadeType": FADE_TYPES,
    "Live2dEffectType": LIVE2D_EFFECT_TYPES,
    "CharacterAlphaMaskType": CHARACTER_ALPHA_MASK_TYPES,
    "ScenarioCharacterBlinkAssetType": SCENARIO_BLINK_ASSET_TYPES,
    "TextColorType": TEXT_COLOR_TYPES,
    "AnimationEventType": ANIMATION_EVENT_TYPES,
    "MonologueTextType": MONOLOGUE_TEXT_TYPES,
    "MonologueBgmType": MONOLOGUE_BGM_TYPES,
}

# MonoScript PathIDs in the extracted 4.18.0 client.  Attribute hashes provide
# a second discriminator so the resolver remains useful when a future build
# changes the MonoScript object PathID.
CUBISM_PARAMETER_SCRIPT_ID = 3470045044580567963
CUBISM_PART_SCRIPT_ID = 1137068747574784598
CUBISM_PARAMETER_VALUE_HASH = zlib.crc32(b"Value") & 0xFFFFFFFF
CUBISM_PART_OPACITY_HASH = zlib.crc32(b"Opacity") & 0xFFFFFFFF

MEMORY_SCENARIO_PATTERN = "%/scenario/memory/%.asset"
MEMORY_MOTION_PATTERN = "%/motion/memory_%.anim"
MONOLOGUE_MOTION_PATTERN = "%/motion/monologue_%.anim"
CHARACTER_SETTING_RE = re.compile(
    r"/(CHR_\d{6})/(CHR_\d{6})_(basepoint|blend|effect|look)\.asset$",
    re.IGNORECASE,
)


def enum_name(value, mapping: dict[int, str]) -> str:
    """Return a stable readable enum label without hiding unknown values."""
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return f"Unknown({value})"
    return mapping.get(numeric, f"Unknown({numeric})")


def flag_names(value, mapping: dict[int, str] = EMOTION_FLAGS) -> list[str]:
    """Expand a flags value and retain unknown bits explicitly."""
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return [f"Unknown({value})"]
    if numeric == 0:
        return ["None"]
    names = [name for bit, name in sorted(mapping.items()) if numeric & bit]
    known = 0
    for bit in mapping:
        known |= bit
    unknown = numeric & ~known
    if unknown:
        names.append(f"UnknownBits(0x{unknown:X})")
    return names


def _add_enum_name(data: dict, field: str, mapping: dict[int, str], output_field: str | None = None):
    if field in data:
        data[output_field or f"{field}Name"] = enum_name(data[field], mapping)


def _add_flag_names(data: dict, field: str, output_field: str | None = None):
    if field in data:
        data[output_field or f"{field}Names"] = flag_names(data[field])


def _decode_effect(effect: dict | None):
    if isinstance(effect, dict):
        _add_enum_name(effect, "Type", SCENARIO_EFFECT_TYPES)


def _decode_text(text: dict | None):
    if not isinstance(text, dict):
        return
    _add_enum_name(text, "TextColorType", TEXT_COLOR_TYPES)
    _decode_effect(text.get("Effect"))
    _decode_effect(text.get("Background", {}).get("Effect"))


def _decode_emotion(emotion: dict):
    _add_flag_names(emotion, "EmotionFlags")
    _add_enum_name(emotion, "BlinkAssetType", SCENARIO_BLINK_ASSET_TYPES)
    _add_enum_name(emotion, "SubBlinkAssetType", SCENARIO_BLINK_ASSET_TYPES)


def _decode_live2d(live2d: dict):
    _add_enum_name(live2d, "AnimationType", ANIMATION_TYPES)
    _add_enum_name(live2d, "BasePointType", CHARACTER_BASE_POINT_TYPES)
    _add_enum_name(live2d, "AnimationBasePointType", CHARACTER_BASE_POINT_TYPES)
    _add_enum_name(live2d, "AlphaMaskType", CHARACTER_ALPHA_MASK_TYPES)
    _add_enum_name(live2d, "BlinkAssetType", SCENARIO_BLINK_ASSET_TYPES)
    _add_enum_name(live2d, "SubBlinkAssetType", SCENARIO_BLINK_ASSET_TYPES)
    _add_enum_name(live2d, "Live2DEffectType", LIVE2D_EFFECT_TYPES)
    _decode_effect(live2d.get("Effect"))
    for emotion in live2d.get("Emotions", []):
        _decode_emotion(emotion)
    for emotion in live2d.get("SubEmotions", []):
        _decode_emotion(emotion)


def decode_memory_scenario(source: dict) -> dict:
    """Add enum labels to a ScenarioScript TypeTree without dropping fields."""
    data = copy.deepcopy(source)
    _add_enum_name(data, "ScenarioType", SCENARIO_TYPES)
    _add_enum_name(data, "VoiceLanguageType", LANGUAGE_TYPES)
    _add_enum_name(data, "MemoryInOutType", MEMORY_IN_OUT_TYPES)
    for chapter in data.get("Chapters", []):
        _add_enum_name(chapter, "ChapterType", CHAPTER_TYPES)
        for page in chapter.get("Pages", []):
            _add_enum_name(page, "NameTextColorType", TEXT_COLOR_TYPES)
            _decode_text(page.get("Message"))
            for text in page.get("Text", []):
                _decode_text(text)
            for live2d in page.get("Live2d", []):
                _decode_live2d(live2d)
            animation = page.get("Animation")
            if isinstance(animation, dict):
                _add_enum_name(animation, "AnimationType", SCENARIO_ANIMATION_TYPES)
            fade = page.get("Fade")
            if isinstance(fade, dict):
                _add_enum_name(fade, "Type", FADE_TYPES)
    return data


def decode_character_setting(kind: str, source: dict) -> dict:
    """Add readable labels to per-character Live2D setting assets."""
    data = copy.deepcopy(source)
    kind = kind.casefold()
    if kind == "basepoint":
        for point in data.get("_points", []):
            _add_enum_name(point, "CharacterBasePointType", CHARACTER_BASE_POINT_TYPES)
    elif kind == "blend":
        for entry in data.get("_blendEntryList", []):
            _add_enum_name(entry, "_key", ANIMATION_TYPES, "_keyName")
            for blend in entry.get("_blendDataList", []):
                _add_enum_name(
                    blend,
                    "_blendedAnimationType",
                    ANIMATION_TYPES,
                    "_blendedAnimationTypeName",
                )
        if "_emotionList" in data:
            data["_emotionListNames"] = [
                flag_names(value) for value in data["_emotionList"]
            ]
        if "_subEmotionList" in data:
            data["_subEmotionListNames"] = [
                flag_names(value) for value in data["_subEmotionList"]
            ]
        for loop in data.get("_loopAnimationDatas", []):
            _add_enum_name(
                loop,
                "_loopAnimationType",
                LOOP_ANIMATION_TYPES,
                "_loopAnimationTypeName",
            )
            _add_enum_name(
                loop,
                "_blinkAnimationType",
                BLINK_ANIMATION_TYPES,
                "_blinkAnimationTypeName",
            )
            loop["_hideAnimationTypeNames"] = [
                enum_name(value, ANIMATION_TYPES)
                for value in loop.get("_hideAnimationTypes", [])
            ]
            loop["_resetAnimationTypeNames"] = [
                enum_name(value, ANIMATION_TYPES)
                for value in loop.get("_resetAnimationTypes", [])
            ]
            _add_flag_names(loop, "_hideEmotionFlags", "_hideEmotionFlagNames")
        for event in data.get("_animationEventDataList", []):
            _add_enum_name(
                event,
                "_animationType",
                ANIMATION_TYPES,
                "_animationTypeName",
            )
            _add_enum_name(
                event,
                "_animationEventType",
                ANIMATION_EVENT_TYPES,
                "_animationEventTypeName",
            )
    elif kind == "effect":
        for entry in data.get("_effectEntryList", []):
            _add_enum_name(entry, "_key", ANIMATION_TYPES, "_keyName")
    return data


def decode_monologue_master(source: list[dict]) -> list[dict]:
    """Add labels to MonologueMB timing records."""
    result = copy.deepcopy(source)
    for entry in result:
        _add_enum_name(entry, "MonologueBgmType", MONOLOGUE_BGM_TYPES)
        for field in ("MonologueSettingDatasJP", "MonologueSettingDatasUS"):
            for setting in entry.get(field, []):
                if "MonologueTextType" in setting:
                    _add_enum_name(
                        setting,
                        "MonologueTextType",
                        MONOLOGUE_TEXT_TYPES,
                    )
                elif "RecitationTextType" in setting:
                    _add_enum_name(
                        setting,
                        "RecitationTextType",
                        MONOLOGUE_TEXT_TYPES,
                    )
    return result


def _motion_number(value: float) -> int | float:
    rounded = round(float(value), 3)
    if abs(rounded) < 0.0005:
        return 0
    if rounded.is_integer():
        return int(rounded)
    return rounded


def _decode_streamed_curves(streamed: dict, start_time: float, stop_time: float) -> list[list[dict]]:
    words = streamed.get("data", [])
    raw = b"".join(
        struct.pack("<I", int(word) & 0xFFFFFFFF)
        for word in words
    )
    curves: list[list[dict]] = [
        [] for _ in range(int(streamed.get("curveCount", 0)))
    ]
    position = 0
    while position < len(raw):
        if len(raw) - position < 8:
            raise ValueError("StreamedClip 末尾不足一个帧头")
        time_value = struct.unpack_from("<f", raw, position)[0]
        key_count = struct.unpack_from("<i", raw, position + 4)[0]
        position += 8
        if key_count < 0 or key_count > len(curves):
            raise ValueError(f"StreamedClip 帧曲线数异常: {key_count}")
        for _ in range(key_count):
            if len(raw) - position < 20:
                raise ValueError("StreamedClip 关键帧被截断")
            curve_index = struct.unpack_from("<i", raw, position)[0]
            coefficients = struct.unpack_from("<4f", raw, position + 4)
            position += 20
            if curve_index < 0 or curve_index >= len(curves):
                raise ValueError(f"StreamedClip 曲线索引越界: {curve_index}")
            if (
                math.isfinite(time_value)
                and start_time - 0.0001 <= time_value <= stop_time + 0.01
            ):
                curves[curve_index].append({
                    "time": time_value,
                    "value": coefficients[3],
                    "out_slope": coefficients[2],
                    "coefficients": coefficients,
                })
    return curves


def _streamed_segments(keys: list[dict]) -> tuple[list[int | float], int, int]:
    if not keys:
        raise ValueError("StreamedClip 曲线没有有效关键帧")
    segments: list[int | float] = [
        _motion_number(keys[0]["time"]),
        _motion_number(keys[0]["value"]),
    ]
    segment_count = 0
    point_count = 1
    for previous, current in zip(keys, keys[1:]):
        delta = current["time"] - previous["time"]
        if delta <= 0:
            raise ValueError("StreamedClip 关键帧时间未严格递增")
        a, b, c, _ = previous["coefficients"]
        in_slope = 3 * a * delta * delta + 2 * b * delta + c
        is_flat_step = (
            abs(previous["value"] - current["value"]) <= 0.000001
            and abs(previous["out_slope"]) <= 0.00001
            and abs(in_slope) <= 0.00001
        )
        if is_flat_step:
            segments.extend([
                2,
                _motion_number(current["time"]),
                _motion_number(current["value"]),
            ])
            point_count += 1
        else:
            segments.extend([
                1,
                _motion_number(previous["time"] + delta / 3),
                _motion_number(
                    previous["value"] + previous["out_slope"] * delta / 3
                ),
                _motion_number(current["time"] - delta / 3),
                _motion_number(current["value"] - in_slope * delta / 3),
                _motion_number(current["time"]),
                _motion_number(current["value"]),
            ])
            point_count += 3
        segment_count += 1
    return segments, segment_count, point_count


def _dense_segments(dense: dict) -> list[list[int | float]]:
    curve_count = int(dense.get("m_CurveCount", 0))
    frame_count = int(dense.get("m_FrameCount", 0))
    if curve_count == 0:
        return []
    sample_rate = float(dense.get("m_SampleRate", 0))
    if sample_rate <= 0:
        raise ValueError("DenseClip 采样率无效")
    begin_time = float(dense.get("m_BeginTime", 0))
    samples = dense.get("m_SampleArray", [])
    expected = curve_count * frame_count
    if len(samples) != expected:
        raise ValueError(f"DenseClip 样本数异常: {len(samples)}/{expected}")
    result = []
    for curve_index in range(curve_count):
        segments: list[int | float] = []
        for frame_index in range(frame_count):
            time_value = begin_time + frame_index / sample_rate
            value = samples[frame_index * curve_count + curve_index]
            if frame_index == 0:
                segments.extend([
                    _motion_number(time_value),
                    _motion_number(value),
                ])
            else:
                segments.extend([
                    0,
                    _motion_number(time_value),
                    _motion_number(value),
                ])
        result.append(segments)
    return result


def animation_clip_to_motion3(clip: dict, bindings: list[dict]) -> dict:
    """Convert a Unity AnimationClip TypeTree to Cubism motion3 JSON."""
    muscle = clip["m_MuscleClip"]
    start_time = float(muscle.get("m_StartTime", 0))
    stop_time = float(muscle["m_StopTime"])
    packed_clip = muscle["m_Clip"]["data"]
    streamed = _decode_streamed_curves(
        packed_clip["m_StreamedClip"],
        start_time,
        stop_time,
    )
    dense = _dense_segments(packed_clip["m_DenseClip"])
    constant = packed_clip.get("m_ConstantClip", {}).get("data", [])
    expected = len(streamed) + len(dense) + len(constant)
    if len(bindings) != expected:
        raise ValueError(
            f"AnimationClip 绑定数与曲线数不一致: {len(bindings)}/{expected}"
        )

    segment_lists: list[list[int | float]] = []
    total_segments = 0
    total_points = 0
    for keys in streamed:
        segments, segment_count, point_count = _streamed_segments(keys)
        segment_lists.append(segments)
        total_segments += segment_count
        total_points += point_count
    for segments in dense:
        segment_lists.append(segments)
        segment_count = max(0, (len(segments) - 2) // 3)
        total_segments += segment_count
        total_points += segment_count + 1
    duration = stop_time - start_time
    for value in constant:
        segment_lists.append([
            _motion_number(start_time),
            _motion_number(value),
            1,
            _motion_number(start_time + duration / 3),
            _motion_number(value),
            _motion_number(start_time + duration * 2 / 3),
            _motion_number(value),
            _motion_number(stop_time),
            _motion_number(value),
        ])
        total_segments += 1
        total_points += 4

    curves = []
    for binding, segments in zip(bindings, segment_lists):
        curves.append({
            "Target": binding["target"],
            "Id": binding["id"],
            "FadeInTime": -1.0,
            "FadeOutTime": -1.0,
            "Segments": segments,
        })

    events = []
    total_user_data_size = 0
    for event in clip.get("m_Events", []):
        value = str(event.get("data", ""))
        total_user_data_size += len(value.encode("utf-8"))
        events.append({
            "Time": _motion_number(event.get("time", 0)),
            "Value": value,
        })

    return {
        "Version": 3,
        "Meta": {
            "Duration": _motion_number(duration),
            "Fps": _motion_number(clip.get("m_SampleRate", 0)),
            "Loop": bool(muscle.get("m_LoopTime", False)),
            "AreBeziersRestricted": True,
            "FadeInTime": 0.0,
            "FadeOutTime": 0.0,
            "CurveCount": len(curves),
            "TotalSegmentCount": total_segments,
            "TotalPointCount": total_points,
            "UserDataCount": len(events),
            "TotalUserDataSize": total_user_data_size,
        },
        "Curves": curves,
        "UserData": events,
    }


def _safe_filename(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:180] or "unnamed"


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


class ActionConfigReader:
    """Read decoded objects and resolve Cubism bindings from manifest.sqlite3."""

    def __init__(self, version_dir: Path):
        self.version_dir = version_dir.resolve()
        self.connection = sqlite3.connect(self.version_dir / "manifest.sqlite3")
        self.connection.row_factory = sqlite3.Row
        self._binding_indices: dict[str, dict[str, dict[int, set[str]]]] = {}

    def close(self):
        self.connection.close()

    def rows(self, query: str, parameters=()) -> list[sqlite3.Row]:
        return list(self.connection.execute(query, parameters))

    def load_decoded(self, row: sqlite3.Row) -> dict:
        decoded_files = json.loads(row["decoded_files"])
        if not decoded_files:
            raise ValueError(f"对象没有便捷 JSON: {row['resource_key']}")
        target = (self.version_dir / decoded_files[0]).resolve()
        if not target.is_relative_to(self.version_dir):
            raise ValueError(f"对象路径越界: {decoded_files[0]}")
        return json.loads(target.read_text(encoding="utf-8"))

    def _binding_index(self, bundle: str) -> dict[str, dict[int, set[str]]]:
        cached = self._binding_indices.get(bundle)
        if cached is not None:
            return cached
        parameter: dict[int, set[str]] = defaultdict(set)
        part: dict[int, set[str]] = defaultdict(set)
        rows = self.connection.execute(
            "SELECT resource_key FROM resources WHERE bundle = ? AND type = 'GameObject'",
            (bundle,),
        )
        for (resource_key,) in rows:
            marker = "/GameObject/"
            if marker not in resource_key:
                continue
            name = resource_key.split(marker, 1)[1].rsplit("__", 1)[0]
            parameter_hash = zlib.crc32(
                f"Parameters/{name}".encode("utf-8")
            ) & 0xFFFFFFFF
            part_hash = zlib.crc32(
                f"Parts/{name}".encode("utf-8")
            ) & 0xFFFFFFFF
            parameter[parameter_hash].add(name)
            part[part_hash].add(name)
        cached = {"Parameter": parameter, "PartOpacity": part}
        self._binding_indices[bundle] = cached
        return cached

    def resolve_bindings(self, bundle: str, clip: dict) -> tuple[list[dict], list[dict]]:
        indices = self._binding_index(bundle)
        resolved = []
        unresolved = []
        for curve_index, binding in enumerate(
            clip["m_ClipBindingConstant"]["genericBindings"]
        ):
            path_hash = int(binding.get("path", 0)) & 0xFFFFFFFF
            attribute_hash = int(binding.get("attribute", 0)) & 0xFFFFFFFF
            script_id = int(binding.get("script", {}).get("m_PathID", 0))
            if (
                script_id == CUBISM_PARAMETER_SCRIPT_ID
                or attribute_hash == CUBISM_PARAMETER_VALUE_HASH
            ):
                target = "Parameter"
            elif (
                script_id == CUBISM_PART_SCRIPT_ID
                or attribute_hash == CUBISM_PART_OPACITY_HASH
            ):
                target = "PartOpacity"
            else:
                target = "Model"
            names = indices.get(target, {}).get(path_hash, set())
            is_resolved = len(names) == 1
            identifier = (
                next(iter(names))
                if is_resolved
                else f"PathHash_{path_hash:08X}"
            )
            item = {
                "target": target,
                "id": identifier,
                "resolved": is_resolved,
                "path_hash": path_hash,
                "attribute_hash": attribute_hash,
                "script_path_id": script_id,
            }
            resolved.append(item)
            if not is_resolved:
                unresolved.append({"curve_index": curve_index, **item})
        return resolved, unresolved


def _prepare_output(output_dir: Path, replace: bool):
    resolved = output_dir.resolve()
    if not resolved.exists():
        return
    if not replace:
        raise FileExistsError(
            f"输出目录已存在: {resolved}；确认重建时添加 --force"
        )
    protected = {
        ROOT.resolve(),
        (ROOT / CONFIG["dirs"]["extracted"]).resolve(),
        (ROOT / CONFIG["dirs"]["reports"]).resolve(),
    }
    if resolved in protected or resolved.parent == resolved:
        raise ValueError(f"拒绝清理受保护目录: {resolved}")
    shutil.rmtree(resolved)


def _load_complete_report(version_dir: Path) -> dict:
    report_path = version_dir / "extraction_report.json"
    manifest_path = version_dir / "manifest.sqlite3"
    if not report_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(f"缺少完整提取报告或对象清单: {version_dir}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (
        report.get("status") != "complete"
        or report.get("raw_object_coverage") != 1.0
        or report.get("objects_raw_exported") != report.get("objects_total")
    ):
        raise ValueError(f"提取结果未满足 complete 不变量: {report_path}")
    return report


def export_action_configs(
    version: str,
    output_dir: Path | None = None,
    monologue_master: Path | None = None,
    replace: bool = False,
) -> tuple[Path, dict]:
    """Export readable action configurations for one complete version."""
    version_dir = ROOT / CONFIG["dirs"]["extracted"] / version
    report = _load_complete_report(version_dir)
    if output_dir is None:
        output_dir = (
            ROOT / CONFIG["dirs"]["reports"] / f"{version}_action_configs"
        )
    output_dir = output_dir.resolve()
    _prepare_output(output_dir, replace)
    output_dir.mkdir(parents=True)

    reader = ActionConfigReader(version_dir)
    try:
        scenario_rows = reader.rows(
            """SELECT resource_key, bundle, decoded_files
               FROM resources
               WHERE type = 'MonoBehaviour'
                 AND lower(resource_key) LIKE ?
                 AND lower(resource_key) NOT LIKE '%_text_layout.asset'
               ORDER BY resource_key""",
            (MEMORY_SCENARIO_PATTERN,),
        )
        scenario_records = []
        total_pages = 0
        for row in scenario_rows:
            decoded = decode_memory_scenario(reader.load_decoded(row))
            name = _safe_filename(decoded.get("m_Name", Path(row["resource_key"]).stem))
            relative = Path("memory_scenarios") / f"{name}.json"
            _write_json(
                output_dir / relative,
                {
                    "source_asset": row["resource_key"],
                    "config": decoded,
                },
            )
            pages = sum(
                len(chapter.get("Pages", []))
                for chapter in decoded.get("Chapters", [])
            )
            total_pages += pages
            scenario_records.append({
                "asset": row["resource_key"],
                "file": relative.as_posix(),
                "character_id": decoded.get("CharacterId"),
                "episode_id": decoded.get("EpisodeId"),
                "language": decoded.get("VoiceLanguageTypeName"),
                "memory_in_out": decoded.get("MemoryInOutTypeName"),
                "chapters": len(decoded.get("Chapters", [])),
                "pages": pages,
            })

        setting_rows = reader.rows(
            """SELECT resource_key, bundle, decoded_files
               FROM resources
               WHERE type = 'MonoBehaviour'
                 AND lower(resource_key) LIKE '%/characters/chr_%/%.asset'
               ORDER BY resource_key"""
        )
        character_settings: dict[str, dict] = defaultdict(dict)
        setting_asset_count = 0
        for row in setting_rows:
            match = CHARACTER_SETTING_RE.search(row["resource_key"])
            if not match:
                continue
            character, _, kind = match.groups()
            character_settings[character.upper()][kind.casefold()] = {
                "source_asset": row["resource_key"],
                "config": decode_character_setting(
                    kind,
                    reader.load_decoded(row),
                ),
            }
            setting_asset_count += 1
        character_records = []
        for character, settings in sorted(character_settings.items()):
            relative = Path("characters") / f"{character}.json"
            _write_json(
                output_dir / relative,
                {
                    "character": character,
                    "settings": settings,
                },
            )
            character_records.append({
                "character": character,
                "file": relative.as_posix(),
                "settings": sorted(settings),
            })

        motion_rows = reader.rows(
            """SELECT resource_key, bundle, decoded_files
               FROM resources
               WHERE type = 'AnimationClip'
                 AND (
                    lower(resource_key) LIKE ?
                    OR lower(resource_key) LIKE ?
                 )
               ORDER BY resource_key""",
            (MEMORY_MOTION_PATTERN, MONOLOGUE_MOTION_PATTERN),
        )
        motion_records = []
        unresolved_total = 0
        unresolved_motion_count = 0
        for row in motion_rows:
            clip = reader.load_decoded(row)
            bindings, unresolved = reader.resolve_bindings(row["bundle"], clip)
            motion = animation_clip_to_motion3(clip, bindings)
            character_match = re.search(
                r"/(CHR_\d{6})/motion/([^/]+)\.anim$",
                row["resource_key"],
                re.IGNORECASE,
            )
            if character_match:
                character = character_match.group(1).upper()
                motion_name = character_match.group(2)
            else:
                character = "unknown"
                motion_name = clip.get("m_Name", "motion")
            relative = (
                Path("motions")
                / character
                / f"{_safe_filename(motion_name)}.motion3.json"
            )
            _write_json(output_dir / relative, motion)
            if unresolved:
                unresolved_motion_count += 1
                unresolved_total += len(unresolved)
            motion_records.append({
                "asset": row["resource_key"],
                "file": relative.as_posix(),
                "character": character,
                "motion": motion_name,
                "animation_type": enum_name(
                    next(
                        (
                            value
                            for value, name in ANIMATION_TYPES.items()
                            if name.casefold() == motion_name.casefold()
                        ),
                        -999999,
                    ),
                    ANIMATION_TYPES,
                ),
                "duration": motion["Meta"]["Duration"],
                "loop": motion["Meta"]["Loop"],
                "curves": motion["Meta"]["CurveCount"],
                "segments": motion["Meta"]["TotalSegmentCount"],
                "unresolved_bindings": unresolved,
            })

        monologue_entries = 0
        monologue_setting_count = 0
        monologue_output = None
        if monologue_master is not None:
            master_path = monologue_master.resolve()
            raw_master = json.loads(master_path.read_text(encoding="utf-8"))
            decoded_master = decode_monologue_master(raw_master)
            monologue_output = "monologue_master.json"
            _write_json(output_dir / monologue_output, decoded_master)
            monologue_entries = len(decoded_master)
            monologue_setting_count = sum(
                len(entry.get("MonologueSettingDatasJP", []))
                + len(entry.get("MonologueSettingDatasUS", []))
                for entry in decoded_master
            )

        enum_output = {
            name: {str(value): label for value, label in mapping.items()}
            for name, mapping in ENUM_MAPS.items()
        }
        _write_json(output_dir / "enum_maps.json", enum_output)

        index = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "version": version,
            "source": {
                "extraction_report": str(
                    (version_dir / "extraction_report.json").relative_to(ROOT)
                ).replace("\\", "/"),
                "status": report["status"],
                "raw_object_coverage": report["raw_object_coverage"],
                "monologue_master": (
                    str(monologue_master.resolve())
                    if monologue_master is not None
                    else None
                ),
            },
            "summary": {
                "memory_scenario_configs": len(scenario_records),
                "memory_scenario_pages": total_pages,
                "character_setting_assets": setting_asset_count,
                "characters_with_settings": len(character_records),
                "motion_files": len(motion_records),
                "memory_motion_files": sum(
                    "/memory_" in row["asset"].casefold()
                    for row in motion_records
                ),
                "monologue_motion_files": sum(
                    "/monologue_" in row["asset"].casefold()
                    for row in motion_records
                ),
                "motions_with_unresolved_bindings": unresolved_motion_count,
                "unresolved_curve_bindings": unresolved_total,
                "monologue_master_entries": monologue_entries,
                "monologue_timing_settings": monologue_setting_count,
            },
            "coverage": {
                "scenario_typetrees": "decoded",
                "character_settings": "decoded",
                "animation_curves": "decoded",
                "binding_names": (
                    "partial" if unresolved_total else "complete"
                ),
                "unresolved_bindings_use": "PathHash_XXXXXXXX",
            },
            "files": {
                "enum_maps": "enum_maps.json",
                "monologue_master": monologue_output,
            },
            "memory_scenarios": scenario_records,
            "characters": character_records,
            "motions": motion_records,
        }
        _write_json(output_dir / "index.json", index)
        return output_dir, index
    finally:
        reader.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="导出独白与回忆的可读动作配置和 Live2D motion3 曲线"
    )
    parser.add_argument("version", help="已 complete 提取的游戏版本")
    parser.add_argument(
        "--output",
        type=Path,
        help="输出目录；默认 reports/<version>_action_configs",
    )
    parser.add_argument(
        "--monologue-master",
        type=Path,
        help="可选的 MonologueMB.json，用于合并独白歌词/朗读时间点",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="删除并重建已存在的精确输出目录",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output_dir, index = export_action_configs(
            args.version,
            output_dir=args.output,
            monologue_master=args.monologue_master,
            replace=args.force,
        )
    except (FileNotFoundError, FileExistsError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"动作配置导出失败: {exc}", file=sys.stderr)
        return 1
    summary = index["summary"]
    print(f"动作配置已导出: {output_dir}")
    print(
        f"  回忆场景: {summary['memory_scenario_configs']} 个配置 / "
        f"{summary['memory_scenario_pages']} 页"
    )
    print(
        f"  角色配置: {summary['character_setting_assets']} 个 / "
        f"{summary['characters_with_settings']} 名角色"
    )
    print(
        f"  动作曲线: {summary['motion_files']} 个 motion3，"
        f"{summary['unresolved_curve_bindings']} 条绑定仍保留哈希名"
    )
    if summary["monologue_master_entries"]:
        print(
            f"  独白时间轴: {summary['monologue_master_entries']} 条 / "
            f"{summary['monologue_timing_settings']} 个时间点"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
