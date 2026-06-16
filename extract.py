"""AssetBundle 解包模块 — 解压 XAPK，用 UnityPy 提取全量资源并保留原始路径"""

import hashlib
import json
import sys
import zipfile
import shutil
from pathlib import Path

import UnityPy
from UnityPy.enums import ClassIDType

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

# ── 导出的资源类型 ──────────────────────────────────────────────
# 图片
IMAGE_TYPES = {ClassIDType.Sprite, ClassIDType.Texture2D}
# 数据
DATA_TYPES = {ClassIDType.TextAsset, ClassIDType.MonoBehaviour}
# 音频
AUDIO_TYPES = {ClassIDType.AudioClip}
# 字体
FONT_TYPES = {ClassIDType.Font}
# 动画
ANIM_TYPES = {ClassIDType.AnimationClip}
# Shader / Material
SHADER_TYPES = {ClassIDType.Shader, ClassIDType.Material}

ALL_EXPORT_TYPES = IMAGE_TYPES | DATA_TYPES | AUDIO_TYPES | FONT_TYPES | ANIM_TYPES | SHADER_TYPES

# Prefab 结构类型（用于提取 UI 层级）
PREFAB_STRUCTURE_TYPES = {
    ClassIDType.GameObject,
    ClassIDType.Transform,
    ClassIDType.RectTransform,
    ClassIDType.CanvasRenderer,
    ClassIDType.Canvas,
}


def find_apk(version: str) -> Path | None:
    """根据版本号找到对应的 APK/XAPK 文件"""
    apks_dir = ROOT / CONFIG["dirs"]["apks"]
    for pattern in [f"*@{version}.*apk*", f"*{version}*"]:
        matches = [f for f in apks_dir.glob(pattern) if f.suffix in (".apk", ".xapk")]
        if matches:
            return matches[0]
    return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def extract_xapk_manifest(xapk_path: Path) -> dict | None:
    """从 XAPK 中读取 manifest.json"""
    try:
        with zipfile.ZipFile(xapk_path) as zf:
            if "manifest.json" in zf.namelist():
                return json.loads(zf.read("manifest.json"))
    except Exception:
        pass
    return None


def extract_catalog(xapk_path: Path, out_dir: Path) -> dict | None:
    """从 XAPK 中提取 Addressable catalog.json"""
    catalog_out = out_dir / "catalog.json"
    if catalog_out.exists():
        return json.loads(catalog_out.read_text(encoding="utf-8"))

    try:
        with zipfile.ZipFile(xapk_path) as xapk:
            unity_apk_name = None
            for name in xapk.namelist():
                if "Unity" in name and name.endswith(".apk"):
                    unity_apk_name = name
                    break
            if not unity_apk_name:
                if "assets/aa/catalog.json" in xapk.namelist():
                    data = xapk.read("assets/aa/catalog.json")
                    catalog_out.write_bytes(data)
                    return json.loads(data)
                return None

            with xapk.open(unity_apk_name) as inner_f:
                with zipfile.ZipFile(inner_f) as apk:
                    if "assets/aa/catalog.json" in apk.namelist():
                        data = apk.read("assets/aa/catalog.json")
                        catalog_out.write_bytes(data)
                        return json.loads(data)
    except Exception as e:
        print(f"  提取 catalog 失败: {e}")
    return None


def parse_catalog_keys(catalog: dict) -> list[str]:
    """从 catalog 中提取所有资源的 address key"""
    ids = catalog.get("m_InternalIds", [])
    return [id for id in ids if id.startswith("Assets/")]


def extract_bundles_from_xapk(xapk_path: Path, work_dir: Path) -> Path:
    """从 XAPK 中提取所有 AssetBundle 到工作目录"""
    bundles_dir = work_dir / "bundles"
    if bundles_dir.exists() and any(bundles_dir.glob("*.bundle")):
        count = len(list(bundles_dir.glob("*.bundle")))
        print(f"  已提取 {count} 个 bundle，跳过")
        return bundles_dir

    bundles_dir.mkdir(parents=True, exist_ok=True)
    print(f"  从 XAPK 提取 AssetBundle...")

    with zipfile.ZipFile(xapk_path) as xapk:
        unity_apk_name = None
        for name in xapk.namelist():
            if "Unity" in name and name.endswith(".apk"):
                unity_apk_name = name
                break

        if unity_apk_name:
            with xapk.open(unity_apk_name) as inner_f:
                with zipfile.ZipFile(inner_f) as apk:
                    bundle_names = [n for n in apk.namelist()
                                    if n.startswith("assets/aa/Android/") and n.endswith(".bundle")]
                    print(f"  找到 {len(bundle_names)} 个 bundle")
                    for i, name in enumerate(bundle_names):
                        out_path = bundles_dir / Path(name).name
                        out_path.write_bytes(apk.read(name))
                        if (i + 1) % 500 == 0:
                            print(f"    提取进度: {i+1}/{len(bundle_names)}")
        else:
            bundle_names = [n for n in xapk.namelist()
                            if n.startswith("assets/aa/Android/") and n.endswith(".bundle")]
            print(f"  找到 {len(bundle_names)} 个 bundle")
            for i, name in enumerate(bundle_names):
                out_path = bundles_dir / Path(name).name
                out_path.write_bytes(xapk.read(name))
                if (i + 1) % 500 == 0:
                    print(f"    提取进度: {i+1}/{len(bundle_names)}")

    count = len(list(bundles_dir.glob("*.bundle")))
    print(f"  提取完成: {count} 个 bundle")
    return bundles_dir


def extract_unity_data(xapk_path: Path, out_dir: Path):
    """从主 APK 提取 assets/bin/Data/ 下的 Unity 项目数据"""
    data_dir = out_dir / "unity_data"
    if data_dir.exists() and any(data_dir.iterdir()):
        print(f"  unity_data 已存在，跳过")
        return

    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"  从主 APK 提取 Unity 项目数据...")

    with zipfile.ZipFile(xapk_path) as xapk:
        main_apk = None
        for name in xapk.namelist():
            if name.endswith(".apk") and "Unity" not in name and "config" not in name:
                main_apk = name
                break
        if not main_apk:
            print("  未找到主 APK")
            return

        with xapk.open(main_apk) as f:
            with zipfile.ZipFile(f) as apk:
                for name in apk.namelist():
                    if name.startswith("assets/bin/Data/") and not name.endswith("/"):
                        rel = name.replace("assets/bin/Data/", "")
                        out_path = data_dir / rel
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        out_path.write_bytes(apk.read(name))

    count = sum(1 for _ in data_dir.rglob("*") if _.is_file())
    print(f"  提取完成: {count} 个文件")


# ── 资源导出函数 ──────────────────────────────────────────────

def export_image(obj, data, out_dir: Path, rel_path: str) -> dict | None:
    """导出图片资源"""
    img = data.image
    if Path(rel_path).suffix.lower() in ('.png', '.jpg', '.jpeg', '.tga', '.bmp'):
        out_path = out_dir / rel_path
    else:
        out_path = out_dir / f"{rel_path}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return {"hash": sha256_bytes(out_path.read_bytes()), "file": str(out_path.relative_to(out_dir))}


def export_text_asset(obj, data, out_dir: Path, rel_path: str) -> dict | None:
    """导出文本资源"""
    raw = data.script
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw:
        return None
    # 检测格式
    if raw[:1] == b"{" or raw[:1] == b"[":
        ext = ".json"
    elif raw[:5] == b"<?xml":
        ext = ".xml"
    elif b"," in raw[:200] and b"\n" in raw[:500]:
        ext = ".csv"
    else:
        ext = ".txt"
    out_path = out_dir / f"{rel_path}{ext}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    return {"hash": sha256_bytes(raw), "file": str(out_path.relative_to(out_dir))}


def export_monobehaviour(obj, data, out_dir: Path, rel_path: str) -> dict | None:
    """导出 MonoBehaviour（序列化为 JSON）"""
    try:
        if data.serialized_type and data.serialized_type.nodes:
            tree = obj.read_typetree()
            raw = json.dumps(tree, ensure_ascii=False, default=str).encode("utf-8")
        else:
            raw = data.raw_data
            if not raw or len(raw) < 4:
                return None
    except Exception:
        raw = getattr(data, "raw_data", b"")
        if not raw or len(raw) < 4:
            return None

    out_path = out_dir / f"{rel_path}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    return {"hash": sha256_bytes(raw), "file": str(out_path.relative_to(out_dir))}


def export_audio(obj, data, out_dir: Path, rel_path: str) -> dict | None:
    """导出音频资源"""
    samples = data.samples
    if not samples:
        return None
    # 通常只有一个 sample
    for name, audio_data in samples.items():
        ext = Path(name).suffix or ".wav"
        out_path = out_dir / f"{rel_path}{ext}"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio_data)
        return {"hash": sha256_bytes(audio_data), "file": str(out_path.relative_to(out_dir))}
    return None


def export_font(obj, data, out_dir: Path, rel_path: str) -> dict | None:
    """导出字体资源"""
    raw = data.m_FontData
    if not raw:
        return None
    # 检测字体格式
    if raw[:4] == b"\x00\x01\x00\x00" or raw[:4] == b"true":
        ext = ".ttf"
    elif raw[:4] == b"OTTO":
        ext = ".otf"
    elif raw[:4] == b"wOFF":
        ext = ".woff"
    else:
        ext = ".ttf"
    out_path = out_dir / f"{rel_path}{ext}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    return {"hash": sha256_bytes(raw), "file": str(out_path.relative_to(out_dir))}


def export_animation(obj, data, out_dir: Path, rel_path: str) -> dict | None:
    """导出动画数据（序列化为 JSON）"""
    try:
        if hasattr(obj, 'read_typetree'):
            tree = obj.read_typetree()
            raw = json.dumps(tree, ensure_ascii=False, default=str).encode("utf-8")
        else:
            raw = json.dumps({"name": getattr(data, "m_Name", ""), "type": "AnimationClip"}, ensure_ascii=False).encode("utf-8")
    except Exception:
        return None

    out_path = out_dir / f"{rel_path}.anim.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    return {"hash": sha256_bytes(raw), "file": str(out_path.relative_to(out_dir))}


def export_shader(obj, data, out_dir: Path, rel_path: str) -> dict | None:
    """导出 Shader（保存名称和属性信息）"""
    try:
        info = {"name": getattr(data, "m_Name", ""), "type": obj.type.name}
        if hasattr(obj, 'read_typetree'):
            tree = obj.read_typetree()
            info = tree
        raw = json.dumps(info, ensure_ascii=False, default=str).encode("utf-8")
    except Exception:
        return None

    ext = ".shader.json" if obj.type == ClassIDType.Shader else ".mat.json"
    out_path = out_dir / f"{rel_path}{ext}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    return {"hash": sha256_bytes(raw), "file": str(out_path.relative_to(out_dir))}


def export_object(obj, data, out_dir: Path, container: str, bundle_name: str = "") -> dict | None:
    """导出单个资源对象，返回 manifest 条目"""
    type_name = obj.type.name

    # 确定输出路径
    if container:
        rel_path = container.lstrip("/")
    else:
        # 无 container 的资源按 bundle 名 + 类型 + 资源名分目录
        name = getattr(data, "m_Name", "") or getattr(data, "name", "")
        if not name:
            name = f"pathid_{obj.path_id}"
        bundle_short = Path(bundle_name).stem if bundle_name else "_unknown"
        rel_path = f"_by_bundle/{bundle_short}/{type_name}/{name}"

    info = {
        "container": container or "",
        "type": type_name,
        "path_id": obj.path_id,
        "bundle": bundle_name,
    }

    try:
        result = None
        if obj.type in IMAGE_TYPES:
            result = export_image(obj, data, out_dir, rel_path)
        elif obj.type == ClassIDType.TextAsset:
            result = export_text_asset(obj, data, out_dir, rel_path)
        elif obj.type == ClassIDType.MonoBehaviour:
            result = export_monobehaviour(obj, data, out_dir, rel_path)
        elif obj.type in AUDIO_TYPES:
            result = export_audio(obj, data, out_dir, rel_path)
        elif obj.type in FONT_TYPES:
            result = export_font(obj, data, out_dir, rel_path)
        elif obj.type in ANIM_TYPES:
            result = export_animation(obj, data, out_dir, rel_path)
        elif obj.type in SHADER_TYPES:
            result = export_shader(obj, data, out_dir, rel_path)

        if result:
            info.update(result)
            return info

    except Exception as e:
        info["error"] = str(e)

    return None


def _read_vec2(val):
    if val is None: return None
    return {"x": round(val.x, 2), "y": round(val.y, 2)}

def _read_vec3(val):
    if val is None: return None
    return {"x": round(val.x, 2), "y": round(val.y, 2), "z": round(val.z, 2)}

def _read_color(val):
    if val is None: return None
    return {"r": round(val.r, 3), "g": round(val.g, 3), "b": round(val.b, 3), "a": round(val.a, 3)}


def _extract_image_data_tt(tree, obj_map, sprite_index=None):
    """从 typetree dict 提取 Image 组件数据: color, type, sprite name, 9-slice border"""
    result = {}
    c = tree.get('m_Color')
    if c and isinstance(c, dict):
        result["color"] = {"r": round(c.get("r", 0), 3), "g": round(c.get("g", 0), 3),
                           "b": round(c.get("b", 0), 3), "a": round(c.get("a", 0), 3)}
    if 'm_Type' in tree:
        result["type"] = int(tree['m_Type'])
    # 解析 m_Sprite PPtr → 读取 Sprite 获取 name/border/rect
    sp = tree.get('m_Sprite')
    if sp and isinstance(sp, dict) and sp.get('m_PathID', 0) != 0:
        sprite_pid = sp['m_PathID']
        # 优先从本 bundle 的 obj_map 读取（可获取 border/rect）
        if sprite_pid in obj_map:
            try:
                sprite = obj_map[sprite_pid].read()
                if hasattr(sprite, 'm_Name'):
                    result["spriteName"] = sprite.m_Name
                if hasattr(sprite, 'm_Border'):
                    b = sprite.m_Border
                    result["spriteBorder"] = {
                        "left": round(b.x, 1), "bottom": round(b.y, 1),
                        "right": round(b.z, 1), "top": round(b.w, 1)
                    }
                if hasattr(sprite, 'm_Rect'):
                    r = sprite.m_Rect
                    result["spriteRect"] = {
                        "x": round(r.x, 1), "y": round(r.y, 1),
                        "w": round(r.width, 1), "h": round(r.height, 1)
                    }
            except Exception:
                pass
        # 回退: 从全局 sprite_index 查找名称
        if 'spriteName' not in result and sprite_index and sprite_pid in sprite_index:
            result["spriteName"] = sprite_index[sprite_pid]
    if 'm_RaycastTarget' in tree:
        result["raycastTarget"] = bool(tree['m_RaycastTarget'])
    return result if result else None


def _extract_text_data_tt(tree):
    """从 typetree dict 提取 Text/OrtegaText 组件数据"""
    result = {}
    c = tree.get('m_Color')
    if c and isinstance(c, dict):
        result["color"] = {"r": round(c.get("r", 0), 3), "g": round(c.get("g", 0), 3),
                           "b": round(c.get("b", 0), 3), "a": round(c.get("a", 0), 3)}
    if 'm_Text' in tree:
        result["text"] = str(tree['m_Text']) if tree['m_Text'] else ""
    fd = tree.get('m_FontData')
    if fd and isinstance(fd, dict):
        for src, key, conv in [
            ('m_FontSize', 'fontSize', int), ('m_Alignment', 'alignment', int),
            ('m_BestFit', 'bestFit', bool), ('m_MinSize', 'minSize', int),
            ('m_MaxSize', 'maxSize', int), ('m_HorizontalOverflow', 'horizontalOverflow', int),
            ('m_VerticalOverflow', 'verticalOverflow', int),
        ]:
            if src in fd:
                result[key] = conv(fd[src])
        if 'm_LineSpacing' in fd:
            result["lineSpacing"] = round(float(fd['m_LineSpacing']), 2)
    return result if result else None


def _extract_outline_data_tt(tree):
    """从 typetree dict 提取 Outline/Shadow 组件数据"""
    result = {}
    c = tree.get('m_EffectColor')
    if c and isinstance(c, dict):
        result["color"] = {"r": round(c.get("r", 0), 3), "g": round(c.get("g", 0), 3),
                           "b": round(c.get("b", 0), 3), "a": round(c.get("a", 0), 3)}
    d = tree.get('m_EffectDistance')
    if d and isinstance(d, dict):
        result["distance"] = {"x": round(d.get("x", 0), 1), "y": round(d.get("y", 0), 1)}
    if 'm_Enabled' in tree:
        result["enabled"] = bool(tree['m_Enabled'])
    return result if result else None


def _extract_layout_element_data_tt(tree):
    """从 typetree dict 提取 LayoutElement 数据"""
    result = {}
    for src in ['m_MinWidth', 'm_MinHeight', 'm_PreferredWidth', 'm_PreferredHeight',
                'm_FlexibleWidth', 'm_FlexibleHeight']:
        if src in tree:
            val = tree[src]
            if val != -1:
                result[src.replace('m_', '')] = round(float(val), 1)
    return result if result else None


# script_id → 组件类型名 的缓存（运行时自动填充）
_script_id_cache: dict[bytes, str] = {}


def _identify_monobehaviour(comp_obj, obj_map):
    """识别 MonoBehaviour 组件类型并提取数据，使用 typetree + script_id 缓存"""
    st = comp_obj.serialized_type
    script_id = getattr(st, 'script_id', None)

    # 先查缓存
    if script_id and script_id in _script_id_cache:
        cached = _script_id_cache[script_id]
        if cached == '_skip':
            return cached, None
        # 有缓存类型名，读 typetree 提取数据
        tree = comp_obj.read_typetree()
        return cached, tree

    # 无缓存 → 读 typetree 判断类型
    if not (st and st.nodes):
        if script_id:
            _script_id_cache[script_id] = '_skip'
        return '_skip', None

    tree = comp_obj.read_typetree()
    keys = set(tree.keys())

    # 按字段签名识别 Unity 内置组件
    class_name = 'MonoBehaviour'  # 默认
    if 'm_Sprite' in keys and 'm_Type' in keys:
        class_name = 'Image'
    elif 'm_Texture' in keys and 'm_Color' in keys and 'm_Sprite' not in keys:
        class_name = 'RawImage'
    elif 'm_Text' in keys and 'm_FontData' in keys:
        class_name = 'Text'  # 也覆盖 OrtegaText
    elif 'm_EffectColor' in keys and 'm_EffectDistance' in keys:
        # Outline 和 Shadow 字段相同，用 m_UseGraphicAlpha 区分不了
        # 但 script_id 不同，先标记为 Outline，后续遇到第二个同结构的标记为 Shadow
        class_name = 'Outline'
    elif 'm_Alpha' in keys and 'm_Interactable' in keys and 'm_BlocksRaycasts' in keys:
        class_name = 'CanvasGroup'
    elif 'm_MinWidth' in keys and 'm_PreferredWidth' in keys:
        class_name = 'LayoutElement'
    elif 'm_Navigation' in keys and 'm_Transition' in keys:
        class_name = 'Button'

    if script_id:
        _script_id_cache[script_id] = class_name

    return class_name, tree


def _build_node(rt_obj, obj_map, sprite_index=None):
    """递归构建树形节点，包含 RectTransform 布局 + 完整组件数据"""
    data = rt_obj.read()
    node = {}

    # GameObject 名称 + 激活状态
    if hasattr(data, 'm_GameObject') and data.m_GameObject:
        try:
            go = data.m_GameObject.read()
            node["gameObject"] = go.m_Name
            node["active"] = bool(go.m_IsActive) if hasattr(go, 'm_IsActive') else True
        except Exception:
            pass

    # RectTransform 布局属性
    for attr, reader in [
        ('m_AnchoredPosition', _read_vec2), ('m_SizeDelta', _read_vec2),
        ('m_AnchorMin', _read_vec2), ('m_AnchorMax', _read_vec2),
        ('m_Pivot', _read_vec2), ('m_LocalScale', _read_vec3),
        ('m_LocalPosition', _read_vec3), ('m_LocalRotation', lambda v: {
            "x": round(v.x, 4), "y": round(v.y, 4), "z": round(v.z, 4), "w": round(v.w, 4)
        } if v else None),
    ]:
        if hasattr(data, attr):
            val = reader(getattr(data, attr))
            if val:
                node[attr] = val

    # 提取 GameObject 上的组件数据
    if hasattr(data, 'm_GameObject') and data.m_GameObject:
        try:
            go = data.m_GameObject.read()
            comp_types = []
            outline_count = 0
            for comp_ref in go.m_Components:
                comp_pid = comp_ref.path_id
                if comp_pid not in obj_map:
                    continue
                comp_obj = obj_map[comp_pid]
                comp_type = comp_obj.type.name

                if comp_type == 'MonoBehaviour':
                    try:
                        class_name, tree = _identify_monobehaviour(comp_obj, obj_map)
                        if class_name == '_skip' or tree is None:
                            comp_types.append('MonoBehaviour')
                            continue
                        comp_type = f"MonoBehaviour:{class_name}"

                        if class_name == 'Image':
                            d = _extract_image_data_tt(tree, obj_map, sprite_index)
                            if d: node["imageData"] = d
                        elif class_name == 'RawImage':
                            c = tree.get('m_Color')
                            if c and isinstance(c, dict):
                                node["rawImageData"] = {"color": {
                                    "r": round(c.get("r", 0), 3), "g": round(c.get("g", 0), 3),
                                    "b": round(c.get("b", 0), 3), "a": round(c.get("a", 0), 3)}}
                        elif class_name == 'Text':
                            d = _extract_text_data_tt(tree)
                            if d: node["textData"] = d
                        elif class_name == 'Outline':
                            d = _extract_outline_data_tt(tree)
                            if d:
                                # 同一 GO 上第一个是 Outline，第二个是 Shadow
                                if outline_count == 0:
                                    node["outlineData"] = d
                                else:
                                    node["shadowData"] = d
                                outline_count += 1
                        elif class_name == 'LayoutElement':
                            d = _extract_layout_element_data_tt(tree)
                            if d: node["layoutElementData"] = d
                        elif class_name == 'CanvasGroup':
                            if 'm_Alpha' in tree:
                                node["canvasGroupAlpha"] = round(float(tree['m_Alpha']), 3)
                    except Exception:
                        pass

                elif comp_type == 'CanvasGroup':
                    try:
                        cg = comp_obj.read()
                        if hasattr(cg, 'm_Alpha'):
                            node["canvasGroupAlpha"] = round(float(cg.m_Alpha), 3)
                    except Exception:
                        pass

                if comp_type not in ('RectTransform', 'Transform', 'CanvasRenderer'):
                    comp_types.append(comp_type)

            if comp_types:
                node["components"] = comp_types
        except Exception:
            pass

    # 递归子节点
    children = []
    if hasattr(data, 'm_Children'):
        for child_ref in data.m_Children:
            child_pid = child_ref.path_id
            if child_pid in obj_map:
                child_node = _build_node(obj_map[child_pid], obj_map, sprite_index)
                if child_node:
                    children.append(child_node)
    if children:
        node["children"] = children

    return node


def extract_prefab_hierarchy(env, bundle_name: str, out_dir: Path, sprite_index=None) -> list[dict]:
    """提取 bundle 中的 Prefab/GameObject 层级结构（树形 + 完整组件数据）"""
    # 建立 path_id → object 索引
    obj_map = {}
    for obj in env.objects:
        obj_map[obj.path_id] = obj

    # 找到所有 RectTransform，确定哪些是根节点（没有父引用指向它们）
    all_rt_ids = set()
    child_ids = set()
    for obj in env.objects:
        if obj.type == ClassIDType.RectTransform:
            all_rt_ids.add(obj.path_id)
            try:
                data = obj.read()
                if hasattr(data, 'm_Children'):
                    for child in data.m_Children:
                        child_ids.add(child.path_id)
            except Exception:
                continue

    root_ids = all_rt_ids - child_ids
    if not root_ids:
        # 回退: 找 Transform 根节点
        all_t_ids = set()
        for obj in env.objects:
            if obj.type == ClassIDType.Transform:
                all_t_ids.add(obj.path_id)
                try:
                    data = obj.read()
                    if hasattr(data, 'm_Children'):
                        for child in data.m_Children:
                            child_ids.add(child.path_id)
                except Exception:
                    continue
        root_ids = all_t_ids - child_ids

    if not root_ids:
        return []

    trees = []
    for root_id in root_ids:
        tree = _build_node(obj_map[root_id], obj_map, sprite_index)
        if tree:
            trees.append(tree)

    if not trees:
        return []

    # 如果只有一棵树，直接返回；多棵树合并到一个结构
    result = {"bundle": bundle_name}
    if len(trees) == 1:
        result["tree"] = trees[0]
    else:
        result["tree"] = trees

    return [result]


def extract_version(version: str, force: bool = False) -> Path | None:
    """解包指定版本的 APK/XAPK，返回输出目录"""
    apk_path = find_apk(version)
    if not apk_path:
        print(f"未找到版本 {version} 的 APK 文件")
        return None

    out_dir = ROOT / CONFIG["dirs"]["extracted"] / version
    manifest_path = out_dir / "manifest.json"

    if manifest_path.exists() and not force:
        print(f"版本 {version} 已解包，跳过（用 --force 强制重新解包）")
        return out_dir

    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / "_work"
    work_dir.mkdir(exist_ok=True)

    print(f"解包版本 {version}: {apk_path.name} ({apk_path.stat().st_size / 1024 / 1024:.0f} MB)")

    # Step 1: 提取 catalog
    print("\nStep 1: 提取 Addressable catalog...")
    catalog = extract_catalog(apk_path, out_dir)
    if catalog:
        asset_keys = parse_catalog_keys(catalog)
        print(f"  catalog 包含 {len(asset_keys)} 个资源地址")
    else:
        print("  未找到 catalog.json")

    # Step 2: 提取 Unity 项目数据
    print("\nStep 2: 提取 Unity 项目数据...")
    extract_unity_data(apk_path, out_dir)

    # Step 3: 提取 AssetBundle 文件
    print("\nStep 3: 提取 AssetBundle 文件...")
    bundles_dir = extract_bundles_from_xapk(apk_path, work_dir)

    # Step 4: UnityPy 解包全量资源
    print("\nStep 4: UnityPy 解包全量资源...")
    manifest = {}
    export_dir = out_dir / "assets"
    prefab_dir = out_dir / "prefabs"
    prefab_dir.mkdir(parents=True, exist_ok=True)
    total_exported = 0
    total_prefabs = 0
    errors = 0
    type_counts = {}
    sprite_index = {}  # path_id → sprite_name（用于跨 bundle 解析 Image sprite 引用）
    prefab_bundles = []  # 记录含 Prefab 的 bundle，第二遍提取

    bundle_files = sorted(bundles_dir.glob("*.bundle"))
    total_bundles = len(bundle_files)

    # Pass 1: 导出资源 + 构建 sprite 索引
    for i, bundle_path in enumerate(bundle_files):
        bundle_name = bundle_path.name
        try:
            env = UnityPy.load(str(bundle_path))
        except Exception:
            errors += 1
            continue

        # 建立 path_id → container 映射
        container_map = {}
        if hasattr(env, "container"):
            for container_path, obj_info in env.container.items():
                if hasattr(obj_info, "path_id"):
                    container_map[obj_info.path_id] = container_path

        # 记录含 Prefab 结构的 bundle
        has_gameobjects = any(obj.type in PREFAB_STRUCTURE_TYPES for obj in env.objects)
        if has_gameobjects:
            prefab_bundles.append(bundle_path)

        # 导出资源 + 收集 Sprite 名称
        for obj in env.objects:
            if obj.type == ClassIDType.Sprite:
                try:
                    data = obj.read()
                    if hasattr(data, 'm_Name') and data.m_Name:
                        sprite_index[obj.path_id] = data.m_Name
                except Exception:
                    pass

            if obj.type not in ALL_EXPORT_TYPES:
                continue

            container = container_map.get(obj.path_id, "")

            try:
                data = obj.read()
            except Exception:
                errors += 1
                continue

            info = export_object(obj, data, export_dir, container, bundle_name)
            if info and "file" in info:
                manifest[info["file"]] = info
                total_exported += 1
                t = info.get("type", "Unknown")
                type_counts[t] = type_counts.get(t, 0) + 1

        if (i + 1) % 200 == 0:
            print(f"  [{i+1}/{total_bundles}] 已导出 {total_exported} 资源, {errors} 错误")

    print(f"  Sprite 索引: {len(sprite_index)} 个")

    # Pass 2: 提取 Prefab 层级（使用全局 sprite_index）
    print(f"\n  提取 Prefab 层级 ({len(prefab_bundles)} 个含 UI 的 bundle)...")
    _script_id_cache.clear()
    for bundle_path in prefab_bundles:
        bundle_name = bundle_path.name
        try:
            env = UnityPy.load(str(bundle_path))
            hierarchies = extract_prefab_hierarchy(env, bundle_name, prefab_dir, sprite_index)
            for h in hierarchies:
                if h.get("tree"):
                    pfab_path = prefab_dir / f"{Path(bundle_name).stem}.json"
                    pfab_path.write_text(
                        json.dumps(h, ensure_ascii=False, default=str, indent=1),
                        encoding="utf-8",
                    )
                    total_prefabs += 1
        except Exception:
            pass

    # Step 5: 保存 manifest
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n解包完成:")
    print(f"  版本: {version}")
    print(f"  Bundle 数: {total_bundles}")
    print(f"  导出资源: {total_exported}")
    print(f"  Prefab 层级: {total_prefabs}")
    print(f"  错误: {errors}")
    print(f"  按类型:")
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"    {t}: {c}")
    print(f"  输出目录: {out_dir}")

    # Step 6: 清理工作目录
    if work_dir.exists():
        size_mb = sum(f.stat().st_size for f in work_dir.rglob("*") if f.is_file()) / 1024 / 1024
        print(f"\n清理工作目录 ({size_mb:.0f} MB)...")
        try:
            shutil.rmtree(work_dir)
        except PermissionError as e:
            print(f"  清理失败（文件被占用），请手动删除: {work_dir}")

    return out_dir


def reextract_prefabs(version: str) -> None:
    """仅重新提取 Prefab 层级数据（需要已解包的 bundle 或 XAPK）"""
    out_dir = ROOT / CONFIG["dirs"]["extracted"] / version
    prefab_dir = out_dir / "prefabs"

    # 优先使用已解压的 bundles，否则从 XAPK 临时解压
    work_dir = out_dir / "_work"
    bundles_dir = work_dir / "bundles"
    cleanup_work = False

    if not (bundles_dir.exists() and any(bundles_dir.glob("*.bundle"))):
        apk_path = find_apk(version)
        if not apk_path:
            print(f"未找到版本 {version} 的 APK 文件")
            return
        print(f"从 XAPK 提取 bundle...")
        work_dir.mkdir(parents=True, exist_ok=True)
        bundles_dir = extract_bundles_from_xapk(apk_path, work_dir)
        cleanup_work = True

    bundle_files = sorted(bundles_dir.glob("*.bundle"))
    total_bundles = len(bundle_files)

    # Pass 1: 构建全局 sprite_index (path_id → sprite_name)
    print(f"Pass 1: 构建 Sprite 索引 ({total_bundles} 个 bundle)...")
    sprite_index = {}
    for i, bundle_path in enumerate(bundle_files):
        try:
            env = UnityPy.load(str(bundle_path))
        except Exception:
            continue
        for obj in env.objects:
            if obj.type == ClassIDType.Sprite:
                try:
                    data = obj.read()
                    if hasattr(data, 'm_Name') and data.m_Name:
                        sprite_index[obj.path_id] = data.m_Name
                except Exception:
                    continue
        if (i + 1) % 500 == 0:
            print(f"  [{i+1}/{total_bundles}] 已索引 {len(sprite_index)} 个 Sprite")
    print(f"  Sprite 索引完成: {len(sprite_index)} 个")

    # Pass 2: 提取 Prefab 层级
    # 清空旧 prefab
    if prefab_dir.exists():
        shutil.rmtree(prefab_dir)
    prefab_dir.mkdir(parents=True, exist_ok=True)

    _script_id_cache.clear()
    total_prefabs = 0
    errors = 0

    print(f"Pass 2: 提取 Prefab 层级...")

    for i, bundle_path in enumerate(bundle_files):
        bundle_name = bundle_path.name
        try:
            env = UnityPy.load(str(bundle_path))
        except Exception:
            errors += 1
            continue

        has_gameobjects = any(obj.type in PREFAB_STRUCTURE_TYPES for obj in env.objects)
        if has_gameobjects:
            try:
                hierarchies = extract_prefab_hierarchy(env, bundle_name, prefab_dir, sprite_index)
                for h in hierarchies:
                    if h.get("tree"):
                        pfab_path = prefab_dir / f"{Path(bundle_name).stem}.json"
                        pfab_path.write_text(
                            json.dumps(h, ensure_ascii=False, default=str, indent=1),
                            encoding="utf-8",
                        )
                        total_prefabs += 1
            except Exception as e:
                errors += 1

        if (i + 1) % 200 == 0:
            print(f"  [{i+1}/{total_bundles}] {total_prefabs} prefab, {errors} 错误")

    print(f"\nPrefab 重新提取完成:")
    print(f"  Prefab 数: {total_prefabs}")
    print(f"  Sprite 索引: {len(sprite_index)}")
    print(f"  错误: {errors}")
    print(f"  输出目录: {prefab_dir}")

    if cleanup_work and work_dir.exists():
        size_mb = sum(f.stat().st_size for f in work_dir.rglob("*") if f.is_file()) / 1024 / 1024
        print(f"\n清理工作目录 ({size_mb:.0f} MB)...")
        try:
            shutil.rmtree(work_dir)
        except PermissionError:
            print(f"  清理失败，请手动删除: {work_dir}")


def main():
    if len(sys.argv) < 2:
        print("用法: python extract.py <version> [--force] [--prefabs-only]")
        print("示例: python extract.py 4.10.0")
        print("      python extract.py 4.10.0 --force         # 强制重新解包")
        print("      python extract.py 4.10.0 --prefabs-only  # 仅重新提取 Prefab")
        apks_dir = ROOT / CONFIG["dirs"]["apks"]
        if apks_dir.exists():
            files = list(apks_dir.glob("*.*apk*"))
            if files:
                print(f"\n可用的 APK 文件:")
                for f in sorted(files):
                    print(f"  {f.name} ({f.stat().st_size / 1024 / 1024:.0f} MB)")
        sys.exit(1)

    version = sys.argv[1]
    if "--prefabs-only" in sys.argv:
        reextract_prefabs(version)
    else:
        force = "--force" in sys.argv
        extract_version(version, force=force)


if __name__ == "__main__":
    main()
