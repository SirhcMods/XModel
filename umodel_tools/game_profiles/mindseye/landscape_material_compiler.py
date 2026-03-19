# umodel_tools/game_profiles/mindseye/landscape_material_compiler.py

from __future__ import annotations

import json
import os
import re
import shutil
import typing as t

import bpy


LAYER_TEXTURE_MAP: dict[str, dict[str, str]] = {
    "CityAsphalt_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_Asphalt_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_Asphalt_N.png",
    },
    "CityAsphaltOld_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_AsphaltOld_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_AsphaltOld_N.png",
    },
    "CityConcrete_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_Concrete_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_Concrete_N.png",
    },
    "CityDecorativeGravel_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_DecorativeGravel_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_DecorativeGravel_N.png",
    },
    "CityFoliage01_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_CityFoliage01_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_CityFoliage01_N.png",
    },
    "CityFoliage02_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_CityFoliage_02_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_CityFoliage_02_N.png",
    },
    "DryLakeCrackedSoil_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundSoilDryLake_01_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundSoilDryLake_01_N.png",
    },
    "DryLakeSand_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundSand_01_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundSand_01_N.png",
    },
    "ExtraLayer01_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundSand_01_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundSand_01_N.png",
    },
    "Pebbles_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundRocky_01_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundRocky_01_N.png",
    },
    "Rock_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_Rock_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_Rock_N.png",
    },
    "SoilBase_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundGeneric_01_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundGeneric_01_N.png",
    },
    "SoilDark_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundDarkSoil_01_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundDarkSoil_01_N.png",
    },
    "WildFoliageDry_LayerInfo": {
        "base": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundWoodDebris_01_D.png",
        "normal": r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_GroundWoodDebris_01_N.png",
    },
}

SOIL_LAYERS = {
    "SoilBase_LayerInfo",
    "SoilDark_LayerInfo",
}

MACRO_DETAIL_TEXTURE = r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_LandscapeMacroDetail_M.png"
SOIL_DETAIL_TEXTURE  = r"\MindsEye\Content\Storm\Maps\Landscape\Material\Layers4\T_LandscapeSoilDetail_M.png"

UMAP_ID_RE = re.compile(
    r"LandscapeNaniteMesh_\d+_([A-Z0-9]+)(?:\.mo)?(?:\.\d+)?$",
    re.IGNORECASE,
)

SLOT_KEY_RE = re.compile(r"(LandscapeMaterialInstanceConstant_\d+)", re.IGNORECASE)
LAYER_INFO_RE = re.compile(r"LandscapeLayerInfoObject'([^']+)'")
WEIGHTMAP_NAME_RE = re.compile(r"Texture2D'.*\.([^']+)'")
MATERIAL_INSTANCE_RE = re.compile(r"LandscapeMaterialInstanceConstant'[^']*\.([^'.]+)'")

def _new_math(nodes, operation: str, location: tuple[float, float], default_0=None, default_1=None):
    node = nodes.new("ShaderNodeMath")
    node.operation = operation
    node.location = location
    if default_0 is not None:
        node.inputs[0].default_value = default_0
    if default_1 is not None:
        node.inputs[1].default_value = default_1
    return node


def _new_value(nodes, value: float, location: tuple[float, float]):
    node = nodes.new("ShaderNodeValue")
    node.location = location
    node.outputs[0].default_value = value
    return node


def _norm(path: str) -> str:
    return os.path.normpath(os.path.abspath(bpy.path.abspath(path)))


def _clean_rel_export_path(rel_path: str) -> str:
    rel_path = (rel_path or "").strip().strip("{}").strip()
    rel_path = rel_path.lstrip("/\\")
    return rel_path


def _export_abs(export_dir: str, rel_path: str) -> str:
    return os.path.normpath(os.path.join(_norm(export_dir), _clean_rel_export_path(rel_path)))


def _find_umap_json(umap_dir: str, umap_id: str) -> str | None:
    direct = os.path.join(_norm(umap_dir), f"{umap_id}.json")
    if os.path.isfile(direct):
        return direct

    for root, _, files in os.walk(_norm(umap_dir)):
        for name in files:
            if name.lower() == f"{umap_id.lower()}.json":
                return os.path.join(root, name)
    return None


def _extract_umap_id(obj_name: str) -> str | None:
    m = UMAP_ID_RE.search(obj_name or "")
    return m.group(1) if m else None


def _extract_slot_key(slot: bpy.types.MaterialSlot) -> str | None:
    candidates: list[str] = []
    if getattr(slot, "material", None) is not None:
        candidates.append(slot.material.name)
    candidates.append(getattr(slot, "name", ""))

    for raw in candidates:
        if not raw:
            continue
        m = SLOT_KEY_RE.search(raw)
        if m:
            return m.group(1)
    return None


def _load_json(json_path: str) -> list[dict[str, t.Any]]:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def _extract_material_instance_key(obj_name: str) -> str | None:
    m = MATERIAL_INSTANCE_RE.search(obj_name or "")
    if m:
        return m.group(1)
    m2 = SLOT_KEY_RE.search(obj_name or "")
    return m2.group(1) if m2 else None


def _find_component_for_slot(entries: list[dict[str, t.Any]], slot_key: str) -> dict[str, t.Any] | None:
    for entry in entries:
        if entry.get("Type") != "LandscapeComponent":
            continue
        props = entry.get("Properties", {}) or {}
        mat_instances = props.get("MaterialInstances", []) or []
        for mi in mat_instances:
            mi_key = _extract_material_instance_key(mi.get("ObjectName", ""))
            if mi_key == slot_key:
                return entry
    return None


def _extract_layer_info_name(layer_alloc: dict[str, t.Any]) -> str | None:
    layer_info = (layer_alloc.get("LayerInfo") or {})
    obj_name = layer_info.get("ObjectName", "")
    m = LAYER_INFO_RE.search(obj_name)
    return m.group(1) if m else None


def _extract_weightmap_name(weightmap_tex_entry: dict[str, t.Any]) -> str | None:
    obj_name = weightmap_tex_entry.get("ObjectName", "")
    m = WEIGHTMAP_NAME_RE.search(obj_name)
    if m:
        return m.group(1)
    if "." in obj_name:
        return obj_name.split(".")[-1].replace("'", "")
    return None


def _resolve_weightmap_png(weightmap_root: str, umap_id: str, weightmap_name: str) -> str | None:
    base_dir = os.path.join(_norm(weightmap_root), umap_id)

    direct = os.path.join(base_dir, f"{weightmap_name}.png")
    if os.path.isfile(direct):
        return direct

    if not os.path.isdir(base_dir):
        return None

    for root, _, files in os.walk(base_dir):
        for file_name in files:
            if file_name.lower() == f"{weightmap_name.lower()}.png":
                return os.path.join(root, file_name)
    return None




def _materialize_unique_weightmap_png(src_path: str, umap_id: str) -> str:
    """Create a uniquely named copy of a weightmap PNG by appending the UMAP id.
    Returns the path to the unique file. Leaves the original source untouched.
    """
    if not src_path or not os.path.exists(src_path):
        return src_path

    directory = os.path.dirname(src_path)
    base = os.path.basename(src_path)
    name, ext = os.path.splitext(base)
    unique_name = f"{name}_{umap_id}{ext}"
    unique_path = os.path.join(directory, unique_name)

    if not os.path.exists(unique_path):
        shutil.copy2(src_path, unique_path)

    return unique_path

def _get_or_load_image(filepath: str,
                       non_color: bool = False,
                       desired_name: str | None = None) -> bpy.types.Image:
    filepath = _norm(filepath)

    # Prefer exact filepath match first so repeated calls reuse the same datablock.
    for img in bpy.data.images:
        try:
            if _norm(img.filepath) == filepath:
                if desired_name:
                    try:
                        if img.name != desired_name:
                            img.name = desired_name
                    except Exception:
                        pass
                if non_color:
                    try:
                        img.colorspace_settings.name = "Non-Color"
                    except Exception:
                        pass
                return img
        except Exception:
            continue

    # If a datablock with the requested name already exists and points to the same file,
    # reuse it directly.
    if desired_name:
        existing = bpy.data.images.get(desired_name)
        if existing is not None:
            try:
                if _norm(existing.filepath) == filepath:
                    if non_color:
                        try:
                            existing.colorspace_settings.name = "Non-Color"
                        except Exception:
                            pass
                    return existing
            except Exception:
                pass

    img = bpy.data.images.load(filepath=filepath, check_existing=True)

    if desired_name:
        try:
            img.name = desired_name
        except Exception:
            # Fall back to Blender's automatic uniquifying if the exact name is already taken.
            base_name = desired_name
            suffix = 1
            while bpy.data.images.get(f"{base_name}.{suffix:03d}") is not None:
                suffix += 1
            try:
                img.name = f"{base_name}.{suffix:03d}"
            except Exception:
                pass

    if non_color:
        try:
            img.colorspace_settings.name = "Non-Color"
        except Exception:
            pass
    return img


def _new_mix_rgb(nodes, blend_type: str, location: tuple[float, float]):
    node = nodes.new("ShaderNodeMixRGB")
    node.blend_type = blend_type
    node.inputs["Fac"].default_value = 1.0
    node.location = location
    return node


def _make_weight_socket(nodes, links, weight_tex_node, channel_index: int, location: tuple[float, float]):
    """
    channel_index: 0=R, 1=G, 2=B, 3=A
    """
    if channel_index in (0, 1, 2):
        sep = nodes.new("ShaderNodeSeparateColor")
        sep.location = location
        links.new(weight_tex_node.outputs["Color"], sep.inputs["Color"])
        channel_name = ("Red", "Green", "Blue")[channel_index]
        return sep.outputs[channel_name]

    # Alpha
    return weight_tex_node.outputs["Alpha"]


def _prepare_material(mat_name: str) -> bpy.types.Material:
    """
    For landscape materials, rebuilding should happen in-place.
    We do NOT remove datablocks from bpy.data.materials because those
    materials may still be assigned to object slots while we are compiling.
    """

    mat = bpy.data.materials.get(mat_name)

    if mat is None:
        mat = bpy.data.materials.new(mat_name)

    mat.use_nodes = True
    nt = mat.node_tree

    # Always rebuild in-place when requested, and in practice this is safe
    # for both reuse and force-rebuild behavior.
    nt.nodes.clear()
    nt.links.clear()

    return mat

def _build_landscape_slot_material(
    *,
    export_dir: str,
    weightmap_root: str,
    umap_id: str,
    obj_name: str,
    slot_key: str,
    component: dict[str, t.Any],
) -> bpy.types.Material:
    mat_name = slot_key
    mat = _prepare_material(mat_name)

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (2200, 0)

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (1900, 0)

    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    weight_uv = nodes.new("ShaderNodeUVMap")
    weight_uv.uv_map = "EXTRAUVS2"
    weight_uv.location = (-2200, 0)

    props = component.get("Properties", {}) or {}
    layer_allocations = props.get("WeightmapLayerAllocations", []) or []
    weightmap_textures = props.get("WeightmapTextures", []) or []

    # --------------------------------------------------
    # Optional soil detail textures
    # --------------------------------------------------
    macro_detail_img = None
    soil_detail_img = None

    macro_path = _export_abs(export_dir, MACRO_DETAIL_TEXTURE)
    soil_path = _export_abs(export_dir, SOIL_DETAIL_TEXTURE)

    if os.path.isfile(macro_path):
        macro_detail_img = _get_or_load_image(macro_path, non_color=True)

    if os.path.isfile(soil_path):
        soil_detail_img = _get_or_load_image(soil_path, non_color=True)

    # --------------------------------------------------
    # PASS 1: collect valid layer data and raw weights
    # --------------------------------------------------
    layer_entries = []
    raw_weight_sockets = []

    y_step = -420

    for i, layer_alloc in enumerate(layer_allocations):
        layer_info_name = _extract_layer_info_name(layer_alloc)
        if not layer_info_name:
            print(f"[LandscapeCompiler] Skipping layer {i}: missing LayerInfo name")
            continue

        tex_map = LAYER_TEXTURE_MAP.get(layer_info_name)
        if not tex_map:
            print(f"[LandscapeCompiler] No layer mapping for {layer_info_name}")
            continue

        weightmap_index = int(layer_alloc.get("WeightmapTextureIndex", -1))
        weightmap_channel = int(layer_alloc.get("WeightmapTextureChannel", -1))

        if weightmap_index < 0 or weightmap_index >= len(weightmap_textures):
            print(
                f"[LandscapeCompiler] Invalid weightmap index for {layer_info_name}: "
                f"{weightmap_index}"
            )
            continue

        if weightmap_channel not in (0, 1, 2, 3):
            print(
                f"[LandscapeCompiler] Invalid weightmap channel for {layer_info_name}: "
                f"{weightmap_channel}"
            )
            continue

        weightmap_name = _extract_weightmap_name(weightmap_textures[weightmap_index])
        if not weightmap_name:
            print(f"[LandscapeCompiler] Could not extract weightmap name for layer {layer_info_name}")
            continue

        weightmap_png = _resolve_weightmap_png(weightmap_root, umap_id, weightmap_name)
        if not weightmap_png:
            print(
                f"[LandscapeCompiler] Missing weightmap PNG for {layer_info_name}: "
                f"{os.path.join(weightmap_root, umap_id, weightmap_name + '.png')}"
            )
            continue

        base_path = _export_abs(export_dir, tex_map["base"])
        normal_path = _export_abs(export_dir, tex_map["normal"])

        if not os.path.isfile(base_path):
            print(f"[LandscapeCompiler] Missing base texture for {layer_info_name}: {base_path}")
            continue
        if not os.path.isfile(normal_path):
            print(f"[LandscapeCompiler] Missing normal texture for {layer_info_name}: {normal_path}")
            continue

        layer_slot_index = len(layer_entries)
        base_y = layer_slot_index * y_step

        frame = nodes.new("NodeFrame")
        frame.label = f"Layer{layer_slot_index} | {layer_info_name} | {weightmap_name} | channel {weightmap_channel}"
        frame.location = (-2100, base_y + 150)

        channel_suffix = "RGBA"[weightmap_channel] if 0 <= weightmap_channel <= 3 else str(weightmap_channel)

        # Weightmap image
        weight_tex = nodes.new("ShaderNodeTexImage")
        weight_tex.location = (-1950, base_y)
        weight_tex.name = f"Layer{layer_slot_index}_WM_{channel_suffix}"
        weight_tex.label = f"Layer{layer_slot_index}_WM_{channel_suffix}"
        weight_tex.parent = frame
        # Weightmap filenames repeat across different UMAPs. Create a uniquely
        # named PNG on disk by appending the source UMAP id before loading it,
        # and keep the Blender image datablock name in sync.
        unique_weightmap_png = _materialize_unique_weightmap_png(weightmap_png, umap_id)
        unique_weightmap_image_name = f"{weightmap_name}_{umap_id}"
        weight_tex.image = _get_or_load_image(
            unique_weightmap_png,
            non_color=True,
            desired_name=unique_weightmap_image_name,
        )
        links.new(weight_uv.outputs["UV"], weight_tex.inputs["Vector"])

        raw_weight_socket = _make_weight_socket(
            nodes,
            links,
            weight_tex,
            weightmap_channel,
            (-1725, base_y),
        )

        raw_weight_sockets.append(raw_weight_socket)

        layer_entries.append({
            "layer_slot_index": layer_slot_index,
            "frame": frame,
            "weight_tex": weight_tex,
            "layer_info_name": layer_info_name,
            "base_path": base_path,
            "normal_path": normal_path,
            "weight_socket": raw_weight_socket,
            "base_y": base_y,
            "weightmap_name": weightmap_name,
            "weightmap_channel": weightmap_channel,
        })

    if not layer_entries:
        print(f"[LandscapeCompiler] No valid layers found for {slot_key}")
        return mat

    # --------------------------------------------------
    # PASS 2: normalize weights
    # --------------------------------------------------
    total_weight_socket = None

    for i, ws in enumerate(raw_weight_sockets):
        if total_weight_socket is None:
            total_weight_socket = ws
        else:
            add_node = _new_math(nodes, "ADD", (-1450, i * -120))
            links.new(total_weight_socket, add_node.inputs[0])
            links.new(ws, add_node.inputs[1])
            total_weight_socket = add_node.outputs[0]

    clamp_node = _new_math(nodes, "MAXIMUM", (-1200, 0), default_1=0.001)
    links.new(total_weight_socket, clamp_node.inputs[0])
    safe_total_socket = clamp_node.outputs[0]

    for i, entry in enumerate(layer_entries):
        div_node = _new_math(nodes, "DIVIDE", (-980, entry["base_y"]))
        links.new(entry["weight_socket"], div_node.inputs[0])
        links.new(safe_total_socket, div_node.inputs[1])
        entry["normalized_weight_socket"] = div_node.outputs[0]

    # --------------------------------------------------
    # PASS 3: build color + normal branches
    # --------------------------------------------------
    last_color_socket = None
    last_normal_socket = None

    for i, entry in enumerate(layer_entries):
        layer_slot_index = entry["layer_slot_index"]
        frame = entry["frame"]
        layer_info_name = entry["layer_info_name"]
        base_path = entry["base_path"]
        normal_path = entry["normal_path"]
        weight_socket = entry["normalized_weight_socket"]
        base_y = entry["base_y"]
        weightmap_name = entry["weightmap_name"]
        weightmap_channel = entry["weightmap_channel"]

        # Base color
        base_tex = nodes.new("ShaderNodeTexImage")
        base_tex.location = (-750, base_y + 100)
        base_tex.name = f"Layer{layer_slot_index}_D"
        base_tex.label = f"Layer{layer_slot_index}_D"
        base_tex.parent = frame
        base_tex.image = _get_or_load_image(base_path, non_color=False)

        base_input_socket = base_tex.outputs["Color"]

        # --------------------------------------------------
        # Soil detail modulation (base color only)
        # LayerColor * (1 + ((detail - 0.5) * strength))
        # This avoids hard seam darkening
        # --------------------------------------------------
        if layer_info_name in SOIL_LAYERS and macro_detail_img and soil_detail_img:
            macro_tex = nodes.new("ShaderNodeTexImage")
            macro_tex.image = macro_detail_img
            macro_tex.location = (-1100, base_y + 320)
            macro_tex.name = f"Layer{layer_slot_index}_MacroDetail"
            macro_tex.label = f"Layer{layer_slot_index}_MacroDetail"
            macro_tex.parent = frame

            soil_tex = nodes.new("ShaderNodeTexImage")
            soil_tex.image = soil_detail_img
            soil_tex.location = (-1100, base_y + 180)
            soil_tex.name = f"Layer{layer_slot_index}_SoilDetail"
            soil_tex.label = f"Layer{layer_slot_index}_SoilDetail"
            soil_tex.parent = frame

            macro_sep = nodes.new("ShaderNodeSeparateColor")
            macro_sep.location = (-900, base_y + 320)
            links.new(macro_tex.outputs["Color"], macro_sep.inputs["Color"])

            soil_sep = nodes.new("ShaderNodeSeparateColor")
            soil_sep.location = (-900, base_y + 180)
            links.new(soil_tex.outputs["Color"], soil_sep.inputs["Color"])

            # macro detail remap
            macro_sub = _new_math(nodes, "SUBTRACT", (-700, base_y + 320), default_1=0.5)
            links.new(macro_sep.outputs["Green"], macro_sub.inputs[0])

            macro_mul_strength = _new_math(nodes, "MULTIPLY", (-520, base_y + 320), default_1=0.18)
            links.new(macro_sub.outputs[0], macro_mul_strength.inputs[0])

            macro_add_one = _new_math(nodes, "ADD", (-340, base_y + 320), default_1=1.0)
            links.new(macro_mul_strength.outputs[0], macro_add_one.inputs[0])

            macro_apply = _new_mix_rgb(nodes, "MULTIPLY", (-150, base_y + 260))
            links.new(base_input_socket, macro_apply.inputs["Color1"])
            links.new(macro_add_one.outputs[0], macro_apply.inputs["Color2"])

            # fine soil detail remap
            soil_sub = _new_math(nodes, "SUBTRACT", (-700, base_y + 180), default_1=0.5)
            links.new(soil_sep.outputs["Green"], soil_sub.inputs[0])

            soil_mul_strength = _new_math(nodes, "MULTIPLY", (-520, base_y + 180), default_1=0.10)
            links.new(soil_sub.outputs[0], soil_mul_strength.inputs[0])

            soil_add_one = _new_math(nodes, "ADD", (-340, base_y + 180), default_1=1.0)
            links.new(soil_mul_strength.outputs[0], soil_add_one.inputs[0])

            soil_apply = _new_mix_rgb(nodes, "MULTIPLY", (40, base_y + 220))
            links.new(macro_apply.outputs["Color"], soil_apply.inputs["Color1"])
            links.new(soil_add_one.outputs[0], soil_apply.inputs["Color2"])

            base_input_socket = soil_apply.outputs["Color"]

        # Apply normalized weight to base color
        base_mul = _new_mix_rgb(nodes, "MULTIPLY", (260, base_y + 90))
        links.new(base_input_socket, base_mul.inputs["Color1"])
        links.new(weight_socket, base_mul.inputs["Color2"])

        # Normal branch
        normal_tex = nodes.new("ShaderNodeTexImage")
        normal_tex.location = (-750, base_y - 140)
        normal_tex.name = f"Layer{layer_slot_index}_N"
        normal_tex.label = f"Layer{layer_slot_index}_N"
        normal_tex.parent = frame
        normal_tex.image = _get_or_load_image(normal_path, non_color=True)

        normal_map = nodes.new("ShaderNodeNormalMap")
        normal_map.location = (-500, base_y - 140)
        normal_map.parent = frame
        links.new(normal_tex.outputs["Color"], normal_map.inputs["Color"])

        if last_color_socket is None:
            last_color_socket = base_mul.outputs["Color"]
        else:
            add_node = _new_mix_rgb(nodes, "ADD", (520, base_y + 90))
            links.new(last_color_socket, add_node.inputs["Color1"])
            links.new(base_mul.outputs["Color"], add_node.inputs["Color2"])
            last_color_socket = add_node.outputs["Color"]

        if last_normal_socket is None:
            last_normal_socket = normal_map.outputs["Normal"]
        else:
            normal_mix = _new_mix_rgb(nodes, "MIX", (260, base_y - 140))
            links.new(weight_socket, normal_mix.inputs["Fac"])
            links.new(last_normal_socket, normal_mix.inputs["Color1"])
            links.new(normal_map.outputs["Normal"], normal_mix.inputs["Color2"])
            last_normal_socket = normal_mix.outputs["Color"]


    if last_color_socket is not None:
        links.new(last_color_socket, bsdf.inputs["Base Color"])

    if last_normal_socket is not None:
        links.new(last_normal_socket, bsdf.inputs["Normal"])

    return mat


def compile_selected_landscapes(context: bpy.types.Context, report_cb=None) -> set[str]:
    from umodel_tools.preferences import get_addon_preferences

    scene = context.scene
    prefs = get_addon_preferences()
    profile = prefs.get_active_profile()

    if profile is None:
        msg = "No active profile"
        if report_cb:
            report_cb({'ERROR'}, msg)
        return {'CANCELLED'}

    export_dir = getattr(profile, "umodel_export_dir", "") or ""
    umap_dir = getattr(scene, "umodel_landscape_umap_dir", "") or ""
    weightmap_root = getattr(scene, "umodel_landscape_weightmap_dir", "") or ""

    export_dir = _norm(export_dir) if export_dir else ""
    umap_dir = _norm(umap_dir) if umap_dir else ""
    weightmap_root = _norm(weightmap_root) if weightmap_root else ""

    if not export_dir or not os.path.isdir(export_dir):
        msg = "Active profile Export Directory is invalid"
        if report_cb:
            report_cb({'ERROR'}, msg)
        return {'CANCELLED'}

    if not umap_dir or not os.path.isdir(umap_dir):
        msg = "Landscape UMAP Folder is invalid"
        if report_cb:
            report_cb({'ERROR'}, msg)
        return {'CANCELLED'}

    if not weightmap_root or not os.path.isdir(weightmap_root):
        msg = "Weightmap Folder is invalid"
        if report_cb:
            report_cb({'ERROR'}, msg)
        return {'CANCELLED'}

    selected_meshes = [obj for obj in context.selected_objects if getattr(obj, "type", None) == "MESH"]
    if not selected_meshes:
        msg = "No selected mesh objects"
        if report_cb:
            report_cb({'WARNING'}, msg)
        return {'CANCELLED'}

    built_count = 0

    for obj in selected_meshes:
        umap_id = _extract_umap_id(obj.name)
        if not umap_id:
            print(f"[LandscapeCompiler] Skipping non-landscape mesh: {obj.name}")
            continue

        json_path = _find_umap_json(umap_dir, umap_id)
        if not json_path:
            print(f"[LandscapeCompiler] Missing UMAP JSON for {obj.name}: {umap_id}.json")
            continue

        print(f"[LandscapeCompiler] Object: {obj.name}")
        print(f"[LandscapeCompiler]   UMAP ID: {umap_id}")
        print(f"[LandscapeCompiler]   JSON: {json_path}")

        entries = _load_json(json_path)

        for slot_index, slot in enumerate(obj.material_slots):
            slot_key = _extract_slot_key(slot)
            if not slot_key:
                print(f"[LandscapeCompiler]   Slot {slot_index}: could not determine LandscapeMaterialInstanceConstant key")
                continue

            component = _find_component_for_slot(entries, slot_key)
            if component is None:
                print(f"[LandscapeCompiler]   Slot {slot_index}: no LandscapeComponent found for {slot_key}")
                continue

            try:
                new_mat = _build_landscape_slot_material(
                    export_dir=export_dir,
                    weightmap_root=weightmap_root,
                    umap_id=umap_id,
                    obj_name=obj.name,
                    slot_key=slot_key,
                    component=component,
                )
                slot.material = new_mat
                built_count += 1
                print(f"[LandscapeCompiler]   Slot {slot_index}: built {new_mat.name}")
            except Exception as exc:
                print(f"[LandscapeCompiler]   Slot {slot_index}: FAILED for {slot_key}: {exc}")

    if report_cb:
        report_cb({'INFO'}, f"Built {built_count} landscape material slot(s)")
    return {'FINISHED'}