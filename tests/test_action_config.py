import json
import struct
import tempfile
import unittest
from pathlib import Path

from action_config import (
    ANIMATION_TYPES,
    animation_clip_to_motion3,
    decode_character_setting,
    decode_memory_scenario,
    decode_monologue_master,
    enum_name,
    flag_names,
)


def encode_streamed_frames(frames):
    raw = bytearray()
    for time_value, keys in frames:
        raw.extend(struct.pack("<f", time_value))
        raw.extend(struct.pack("<i", len(keys)))
        for curve_index, coefficients in keys:
            raw.extend(struct.pack("<i", curve_index))
            raw.extend(struct.pack("<4f", *coefficients))
    return [
        struct.unpack_from("<I", raw, offset)[0]
        for offset in range(0, len(raw), 4)
    ]


class ActionEnumTests(unittest.TestCase):
    def test_known_and_unknown_enum_values_are_explicit(self):
        self.assertEqual(enum_name(7000, ANIMATION_TYPES), "Memory_In")
        self.assertEqual(enum_name(7777, ANIMATION_TYPES), "Unknown(7777)")

    def test_emotion_flags_expand_and_preserve_unknown_bits(self):
        self.assertEqual(flag_names(2 | 16), ["Face_Angry", "Face_Smile"])
        self.assertEqual(flag_names(0), ["None"])
        self.assertEqual(flag_names(1 << 30)[0], "UnknownBits(0x40000000)")


class ActionConfigDecorationTests(unittest.TestCase):
    def test_memory_scenario_adds_action_names_without_dropping_values(self):
        source = {
            "ScenarioType": 0,
            "VoiceLanguageType": 1,
            "MemoryInOutType": 1,
            "Chapters": [{
                "ChapterType": 1,
                "Pages": [{
                    "NameTextColorType": 2000,
                    "Message": {"TextColorType": 0, "Effect": {"Type": 1}},
                    "Text": [],
                    "Live2d": [{
                        "AnimationType": 7000,
                        "BasePointType": 1000,
                        "EmotionFlags": 0,
                        "Emotions": [{"EmotionFlags": 18}],
                        "SubEmotions": [],
                    }],
                    "Animation": {"AnimationType": 6000},
                    "Fade": {"Type": 4},
                }],
            }],
        }
        decoded = decode_memory_scenario(source)
        page = decoded["Chapters"][0]["Pages"][0]
        self.assertEqual(decoded["ScenarioTypeName"], "Memory")
        self.assertEqual(decoded["VoiceLanguageTypeName"], "jaJP")
        self.assertEqual(decoded["MemoryInOutTypeName"], "InOut")
        self.assertEqual(decoded["Chapters"][0]["ChapterTypeName"], "MemoryIn")
        self.assertEqual(page["Live2d"][0]["AnimationTypeName"], "Memory_In")
        self.assertEqual(
            page["Live2d"][0]["Emotions"][0]["EmotionFlagsNames"],
            ["Face_Angry", "Face_Smile"],
        )
        self.assertEqual(page["Animation"]["AnimationTypeName"], "MEManim_memoryIn")
        self.assertEqual(page["Fade"]["TypeName"], "BlackIn")
        self.assertNotIn("ScenarioTypeName", source)

    def test_character_and_monologue_settings_are_named(self):
        blend = decode_character_setting("blend", {
            "_blendEntryList": [{
                "_key": 13000,
                "_blendDataList": [{
                    "_blendedAnimationType": 7000,
                    "_blendTime": 0.5,
                }],
            }],
            "_emotionList": [2],
            "_subEmotionList": [],
            "_loopAnimationDatas": [{
                "_loopAnimationType": 1000,
                "_blinkAnimationType": 1,
                "_hideAnimationTypes": [13000],
                "_resetAnimationTypes": [7000],
                "_hideEmotionFlags": 16,
            }],
            "_animationEventDataList": [],
        })
        self.assertEqual(blend["_blendEntryList"][0]["_keyName"], "Monologue_Loop")
        self.assertEqual(
            blend["_loopAnimationDatas"][0]["_hideEmotionFlagNames"],
            ["Face_Smile"],
        )

        master = decode_monologue_master([{
            "MonologueBgmType": 1,
            "MonologueSettingDatasJP": [{"MonologueTextType": 2}],
            "MonologueSettingDatasUS": [],
        }])
        self.assertEqual(master[0]["MonologueBgmTypeName"], "LamentAndVoice")
        self.assertEqual(
            master[0]["MonologueSettingDatasJP"][0]["MonologueTextTypeName"],
            "Monologue",
        )


class AnimationClipDecoderTests(unittest.TestCase):
    def test_streamed_and_constant_curves_convert_to_motion3(self):
        stream_words = encode_streamed_frames([
            (0.0, [(0, (0.0, 0.0, 1.0, 0.0))]),
            (1.0, [(0, (0.0, 0.0, 0.0, 1.0))]),
            (float("inf"), []),
        ])
        clip = {
            "m_SampleRate": 60.0,
            "m_MuscleClip": {
                "m_StartTime": 0.0,
                "m_StopTime": 1.0,
                "m_LoopTime": True,
                "m_Clip": {
                    "data": {
                        "m_StreamedClip": {
                            "data": stream_words,
                            "curveCount": 1,
                        },
                        "m_DenseClip": {
                            "m_FrameCount": 0,
                            "m_CurveCount": 0,
                            "m_SampleRate": 60.0,
                            "m_BeginTime": 0.0,
                            "m_SampleArray": [],
                        },
                        "m_ConstantClip": {"data": [2.0]},
                    },
                },
            },
            "m_Events": [{"time": 0.5, "data": "event"}],
        }
        bindings = [
            {"target": "Parameter", "id": "ParamAngleX"},
            {"target": "Parameter", "id": "ParamConstant"},
        ]
        motion = animation_clip_to_motion3(clip, bindings)
        self.assertEqual(motion["Meta"]["CurveCount"], 2)
        self.assertEqual(motion["Meta"]["TotalSegmentCount"], 2)
        self.assertEqual(motion["Meta"]["TotalPointCount"], 8)
        self.assertTrue(motion["Meta"]["Loop"])
        self.assertEqual(
            motion["Curves"][0]["Segments"],
            [0, 0, 1, 0.333, 0.333, 0.667, 0.667, 1, 1],
        )
        self.assertEqual(
            motion["Curves"][1]["Segments"],
            [0, 2, 1, 0.333, 2, 0.667, 2, 1, 2],
        )
        self.assertEqual(motion["UserData"], [{"Time": 0.5, "Value": "event"}])

    def test_binding_count_must_cover_every_curve(self):
        clip = {
            "m_SampleRate": 60.0,
            "m_MuscleClip": {
                "m_StartTime": 0.0,
                "m_StopTime": 1.0,
                "m_LoopTime": False,
                "m_Clip": {
                    "data": {
                        "m_StreamedClip": {"data": [], "curveCount": 0},
                        "m_DenseClip": {
                            "m_FrameCount": 0,
                            "m_CurveCount": 0,
                            "m_SampleRate": 60.0,
                            "m_BeginTime": 0.0,
                            "m_SampleArray": [],
                        },
                        "m_ConstantClip": {"data": [1.0]},
                    },
                },
            },
            "m_Events": [],
        }
        with self.assertRaisesRegex(ValueError, "绑定数与曲线数不一致"):
            animation_clip_to_motion3(clip, [])


if __name__ == "__main__":
    unittest.main()
