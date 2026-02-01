"""Mindseye (FModel) material support.

This profile adds support for rebuilding Blender materials from MaterialInstance JSON
exports (FModel) and UMAP OverrideMaterials, while keeping the existing props.txt
(UModel) pipeline intact.
"""

from __future__ import annotations

# Material descriptor extension for this profile (FModel MI JSON)
MATERIAL_DESC_EXT = ".json"


import json
import os
import typing as t

import bpy
import lark

from umodel_tools import utils


GAME_NAME = "Mindseye (FModel)"
GAME_DESCRIPTION = "Mindseye: build materials from FModel MaterialInstance JSON + UMAP OverrideMaterials"


# ---- Helpers -----------------------------------------------------------------

def _remap_asset_path(p: str) -> str:
    """Remap UE-style asset root used in some exports.

    Example:
      /Game/... -> /MindsEye/Content/...
    """
    if p.startswith("/Game/"):
        return "/MindsEye/Content/" + p[len("/Game/"):]
    return p


def _blend_mode_to_str(val: t.Any) -> str | None:
    """Convert BlendMode value from MI JSON to the string format expected by the importer."""
    # Some dumps store BlendMode as int, some as dict/string.
    if isinstance(val, int):
        code = val
    elif isinstance(val, str):
        # try to parse int-ish strings
        try:
            code = int(val)
        except ValueError:
            return None
    else:
        return None

    match code:
        case 0:
            return "BLEND_Opaque (0)"
        case 1:
            return "BLEND_Masked (1)"
        case 2:
            return "BLEND_Translucent (2)"
        case 3:
            return "BLEND_Additive (3)"
        case 4:
            return "BLEND_Modulate (4)"
        case _:
            return None


def _ensure_separate_rgb(nodes: bpy.types.Nodes, links: bpy.types.NodeLinks, src: bpy.types.NodeSocket):
    sep = nodes.new("ShaderNodeSeparateColor")
    sep.mode = "RGB"
    links.new(src, sep.inputs["Color"])
    return sep


def _ensure_normal_map(nodes: bpy.types.Nodes, links: bpy.types.NodeLinks, color_socket: bpy.types.NodeSocket):
    n = nodes.new("ShaderNodeNormalMap")
    links.new(color_socket, n.inputs["Color"])
    return n


def _set_noncolor(img_node: bpy.types.ShaderNodeTexImage) -> None:
    try:
        img_node.image.colorspace_settings.name = "Non-Color"
    except Exception:
        pass


# ---- Descriptor parsing (plugin hook) ----------------------------------------

def parse_material_descriptor(material_desc_abs: str) -> tuple[t.Any, dict[str, str], dict[str, t.Any] | None]:
    """Parse a MaterialInstance JSON exported by FModel.

    Returns a (desc_ast, texture_infos, base_prop_overrides) triple compatible with the importer.
    - desc_ast: the raw MI JSON dict (used by this profile for optional parameters like tint)
    - texture_infos: mapping of (tex_type -> "/Path/To/Asset.AssetName")
    - base_prop_overrides: dict with BlendMode / OpacityMaskClipValue / TwoSided where available
    """
    with open(material_desc_abs, "r", encoding="utf-8") as f:
        mi_raw = json.load(f)

    # FModel MI JSON may be wrapped in a list of exports
    if isinstance(mi_raw, list):
        mi = None
        for entry in mi_raw:
            if isinstance(entry, dict) and ("Textures" in entry or "Parameters" in entry):
                mi = entry
                break

        if mi is None:
            if utils.preferences.get_addon_preferences().verbose:
                utils.verbose_print(
                    f"[Mindseye] WARNING: No MI data block found in '{os.path.basename(material_desc_abs)}'"
                )
            mi = {}
        else:
            if utils.preferences.get_addon_preferences().verbose:
                utils.verbose_print(
                    f"[Mindseye] MI JSON '{os.path.basename(material_desc_abs)}' wrapped in list, "
                    f"using entry with keys: {list(mi.keys())}"
                )
    else:
        mi = mi_raw if isinstance(mi_raw, dict) else {}

    tex_infos: dict[str, str] = {}
    for k, v in (mi.get("Textures") or {}).items():
        if not isinstance(v, str) or not v:
            continue
        tex_infos[k] = _remap_asset_path(v)

    params = mi.get("Parameters") or {}
    props = params.get("Properties") or {}
    base_overrides: dict[str, t.Any] = {}

    if (bm := _blend_mode_to_str(params.get("BlendMode"))) is not None:
        base_overrides["BlendMode"] = bm

    bpo = props.get("BasePropertyOverrides") or {}
    if isinstance(bpo, dict):
        if "OpacityMaskClipValue" in bpo:
            base_overrides["OpacityMaskClipValue"] = bpo.get("OpacityMaskClipValue")
        if "TwoSided" in bpo:
            base_overrides["TwoSided"] = bool(bpo.get("TwoSided"))

    if utils.preferences.get_addon_preferences().verbose:
        utils.verbose_print(
            f"[Mindseye] Parsed MI JSON '{os.path.basename(material_desc_abs)}' with "
            f"{len(tex_infos)} texture reference(s)."
        )

    return mi, tex_infos, (base_overrides or None)


def _iter_obj_paths(node: t.Any) -> t.Iterator[str]:
    """Yield any ObjectPath strings found in a nested FModel JSON structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "ObjectPath" and isinstance(v, str):
                yield v
            else:
                yield from _iter_obj_paths(v)
    elif isinstance(node, list):
        for it in node:
            yield from _iter_obj_paths(it)


def _load_json_maybe_list(path: str) -> t.Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _material_objectpaths_from_staticmesh_json(mesh_json_abs: str) -> list[str]:
    """Best-effort extraction of material interface references from a StaticMesh JSON export.

    We look for ObjectPath values whose basename starts with 'MI_' (material instance) or 'M_' (material).
    """
    try:
        data = _load_json_maybe_list(mesh_json_abs)
    except Exception:
        return []

    out: list[str] = []
    for obj_path in _iter_obj_paths(data):
        # Common forms:
        #   /StormMaterialLibrary/.../MI_Foo.0
        #   /Game/.../MI_Foo.MI_Foo
        base = obj_path.split(".", 1)[0]
        name = os.path.basename(base)
        if name.startswith("MI_") or name.startswith("M_"):
            out.append(obj_path)
    # De-dup while preserving order
    seen = set()
    dedup: list[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            dedup.append(p)
    return dedup


def get_material_descriptors_for_mesh(
    asset_psk_path_noext: str,
    override_materials: list[dict] | None,
    umodel_export_dir: str,
    verbose: bool = False,
) -> list[str]:
    """Resolve material descriptors for a mesh.

    Priority:
      1) UMAP OverrideMaterials (per-instance overrides)
      2) StaticMesh JSON export (default materials baked into the mesh asset)

    IMPORTANT: asset_importer expects each descriptor in the form:
        "<relative_path_no_ext>.<MaterialName>"

    For Mindseye/FModel, we map a MaterialInstance ObjectPath like:
        "/StormMaterialLibrary/.../MI_Foo.0"
    to:
        "StormMaterialLibrary/.../MI_Foo.MI_Foo"

    The importer will then append the correct descriptor extension (".json") for this profile.
    """
    obj_paths: list[str] = []

    # 1) UMAP OverrideMaterials
    if override_materials:
        for entry in override_materials:
            if not isinstance(entry, dict):
                continue
            obj_path = entry.get("ObjectPath")
            if isinstance(obj_path, str) and obj_path:
                obj_paths.append(obj_path)

    # 2) StaticMesh JSON export fallback
    if not obj_paths:
        # asset_psk_path_noext is an absolute path without extension (under umodel_export_dir).
        # FModel commonly exports a companion JSON for the StaticMesh UObject at the same relative path.
        candidates = [
            asset_psk_path_noext + ".json",
            asset_psk_path_noext + ".uasset.json",
        ]
        mesh_json_abs = next((p for p in candidates if os.path.isfile(p)), None)
        if mesh_json_abs:
            obj_paths = _material_objectpaths_from_staticmesh_json(mesh_json_abs)
            if verbose:
                utils.verbose_print(
                    f"[Mindseye] StaticMesh JSON fallback for '{os.path.basename(asset_psk_path_noext)}' -> "
                    f"{len(obj_paths)} material reference(s)"
                )
        else:
            if verbose:
                utils.verbose_print(
                    f"[Mindseye] No OverrideMaterials and no StaticMesh JSON for '{os.path.basename(asset_psk_path_noext)}'"
                )

    if not obj_paths:
        return []

    out: list[str] = []
    for obj_path in obj_paths:
        if not isinstance(obj_path, str) or not obj_path:
            continue

        # Strip leading "/" and any ".<index>" suffix
        rel_no_ext = obj_path.lstrip("/")
        rel_no_ext = rel_no_ext.split(".", 1)[0]
        rel_no_ext = os.path.normpath(rel_no_ext)

        mat_name = os.path.basename(rel_no_ext)
        if not mat_name:
            continue

        out.append(f"{rel_no_ext}.{mat_name}")

    # De-dup preserve order
    seen = set()
    dedup: list[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            dedup.append(p)

    if verbose:
        utils.verbose_print(
            f"[Mindseye] Resolved {len(dedup)} material descriptor(s) for '{os.path.basename(asset_psk_path_noext)}'"
        )
        if dedup:
            utils.verbose_print(f"[Mindseye] First material descriptor: {dedup[0]}")
    return dedup

# ---- GameHandler protocol implementations ------------------------------------

def process_material(mat: bpy.types.Material, desc_ast: lark.Tree, use_pbr: bool) -> None:
    # No-op for now; we could apply tint/scalars here in future.
    # desc_ast is the MI JSON dict in this profile.
    _ = (desc_ast, use_pbr)


def do_process_texture(tex_type: str, tex_short_name: str) -> bool:
    # Mindseye MI JSON already uses semantic-ish keys; accept most.
    _ = tex_short_name
    return True


def is_diffuse_tex_type(tex_type: str, tex_short_name: str) -> bool:
    _ = tex_short_name
    t0 = tex_type.lower()
    return any(k in t0 for k in ("basecolor", "diffuse", "albedo", "color"))


def handle_material_texture_pbr(mat: bpy.types.Material,
                                tex_type: str,
                                tex_short_name: str,
                                img_node: bpy.types.ShaderNodeTexImage,
                                ao_mix_node: bpy.types.ShaderNodeMix,
                                bsdf_node: bpy.types.ShaderNodeBsdfPrincipled,
                                out_node: bpy.types.ShaderNodeOutputMaterial) -> None:
    # Normalize for comparisons
    _ = (mat, out_node, tex_short_name)
    t0 = tex_type.lower()

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    # Base Color / Diffuse
    if any(k in t0 for k in ("basecolor", "diffuse", "albedo", "color")):
        links.new(img_node.outputs["Color"], ao_mix_node.inputs[6])  # Color1
        return

    # Normal
    if "normal" in t0:
        _set_noncolor(img_node)
        n = _ensure_normal_map(nodes, links, img_node.outputs["Color"])
        links.new(n.outputs["Normal"], bsdf_node.inputs["Normal"])
        return

    # Emissive
    if "emiss" in t0:
        links.new(img_node.outputs["Color"], bsdf_node.inputs["Emission"])
        return

    # Opacity / Alpha
    if "opacity" in t0 or "alpha" in t0:
        _set_noncolor(img_node)
        links.new(img_node.outputs["Color"], bsdf_node.inputs["Alpha"])
        return

    # Packed masks (best-effort): MRO/ORM/Mask
    if any(k in t0 for k in ("mro", "orm", "mask", "packed", "sro", "mroh")):
        _set_noncolor(img_node)
        sep = _ensure_separate_rgb(nodes, links, img_node.outputs["Color"])

        # Heuristic (UE-common):
        # R = Metallic, G = Roughness, B = AO
        links.new(sep.outputs["Red"], bsdf_node.inputs["Metallic"])
        links.new(sep.outputs["Green"], bsdf_node.inputs["Roughness"])
        links.new(sep.outputs["Blue"], ao_mix_node.inputs[7])  # Color2 (AO multiplier)
        return

    # Fallback: treat unknown as base color
    links.new(img_node.outputs["Color"], ao_mix_node.inputs[6])


def handle_material_texture_simple(mat: bpy.types.Material,
                                   tex_type: str,
                                   tex_short_name: str,
                                   img_node: bpy.types.ShaderNodeTexImage,
                                   bsdf_node: bpy.types.ShaderNodeBsdfDiffuse) -> None:
    _ = tex_short_name
    if is_diffuse_tex_type(tex_type, tex_short_name):
        mat.node_tree.links.new(img_node.outputs["Color"], bsdf_node.inputs["Color"])


def end_process_material(mat: bpy.types.Material) -> None:
    _ = mat
