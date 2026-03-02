
"""
Color palette tint extraction and application helpers.

This module is intentionally "backend" oriented so it can be reused by:
- BPP builder imports
- UMAP imports
- any future batch tools

It does NOT assume any specific game, but provides a schema-driven approach
based on NumCustomDataFloats (packet width) that we observed in MindsEye content.
"""

from __future__ import annotations

import json
import math
import typing as t

import bpy

# Cache of material variants keyed by (base_key, tint_id)
_MAT_VARIANT_CACHE: dict[tuple[str,int], bpy.types.Material] = {}




# Known schemas keyed by per-instance packet width.
# Values are the custom-data indices that typically represent palette indices (in order of priority).
SCHEMAS: dict[int, list[int]] = {
    11: [0, 4, 5],
    8:  [0, 3, 4],
    7:  [0, 3],
    6:  [0, 3],
    18: [0, 5, 9, 10],
}

# Palette indices appear to be 0..255 (16x16). We keep this conservative.
MIN_INDEX = 0
MAX_INDEX = 255


def _is_int_like(v: float, eps: float = 1e-4) -> bool:
    if not isinstance(v, (int, float)):
        return False
    if math.isnan(v) or math.isinf(v):
        return False
    return abs(v - round(v)) <= eps


def extract_tint_ids_from_packet(packet: list[float], width: int) -> list[int]:
    """Return ordered, unique palette indices inferred from a single instance packet.

    Deterministic rule-set:
    1) If width has a known schema, read those indices in order.
       - keep only integer-like values in [0..255]
       - preserve order, unique
    2) If width is unknown, scan all fields left-to-right and keep integer-like values in [0..255].
    """
    indices: list[int] = []

    def add(v: float) -> None:
        if not _is_int_like(v):
            return
        iv = int(round(float(v)))
        if iv < MIN_INDEX or iv > MAX_INDEX:
            return
        if iv not in indices:
            indices.append(iv)

    schema = SCHEMAS.get(width)
    if schema:
        for i in schema:
            if 0 <= i < len(packet):
                add(packet[i])
        return indices

    for v in packet:
        add(v)
    return indices



def assign_tint_ids_to_tinted_slots(tint_ids: list[int], width: int, tinted_slot_count: int) -> list[int]:
    """Deterministically choose which TintIDs apply to the *tinted* material slots on an object.

    We only know the packet width (NumCustomDataFloats) and the ordered TintIDs extracted
    using SCHEMAS[width]. For some widths, SCHEMAS yields more candidate indices than
    the number of tinted material slots (e.g. width 11 -> [0,4,5] but a mesh might only
    have two tinted slots). In practice this dataset shows the *later* schema indices
    are used for secondary/region/slot tinting.

    Deterministic rule:
      - If there is exactly 1 tinted slot: use the FIRST TintID (primary tint).
        (Across the dataset, index-0 is the dominant "primary" tint channel.)
      - If there are 2+ tinted slots and we have more TintIDs than slots: take the LAST N TintIDs.
        (Secondary/region/slot tinting consistently comes from later schema indices.)
      - If we have fewer TintIDs than tinted slots: pad by repeating the last available (or 0 if none).
    """
    if tinted_slot_count <= 0:
        return []
    if not tint_ids:
        return [0] * tinted_slot_count

    # Single tinted material: always prefer the primary tint.
    if tinted_slot_count == 1:
        return [int(tint_ids[0])]

    if len(tint_ids) >= tinted_slot_count:
        return tint_ids[-tinted_slot_count:]
    # pad
    out = list(tint_ids)
    while len(out) < tinted_slot_count:
        out.append(out[-1])
    return out


def chunk_custom_data(per_instance_sm_custom_data: list[float], instance_count: int, width: int) -> list[list[float]]:
    if instance_count <= 0 or width <= 0:
        return []
    expected = instance_count * width
    if len(per_instance_sm_custom_data) < expected:
        # tolerate short arrays by truncating instance_count to available data
        instance_count = len(per_instance_sm_custom_data) // width
        expected = instance_count * width
    flat = per_instance_sm_custom_data[:expected]
    return [flat[i*width:(i+1)*width] for i in range(instance_count)]


def _find_tint_value_node(mat: bpy.types.Material, node_label: str) -> bpy.types.Node | None:
    if mat is None or not getattr(mat, "use_nodes", False) or mat.node_tree is None:
        return None
    for n in mat.node_tree.nodes:
        if getattr(n, "type", "") == "VALUE":
            if getattr(n, "name", "") == node_label or getattr(n, "label", "") == node_label:
                return n
    return None


def _material_base_key(mat: bpy.types.Material) -> str:
    """Return a stable base key for variant caching."""
    try:
        if "__PaletteBase" in mat:
            return str(mat["__PaletteBase"])
    except Exception:
        pass
    # name_full is more stable than name when Blender auto-suffixes
    return getattr(mat, "name_full", mat.name)


def _get_or_create_tinted_variant(mat: bpy.types.Material, tint_id: int, node_label: str) -> bpy.types.Material:
    """Return a material datablock that has TintID set to tint_id, without affecting other tints."""
    base_key = _material_base_key(mat)
    key = (base_key, int(tint_id))
    cached = _MAT_VARIANT_CACHE.get(key)
    if cached is not None and cached.users >= 0:
        return cached

    # Create a variant by copying the current material datablock
    mat_copy = mat.copy()
    try:
        mat_copy["__PaletteBase"] = base_key
        mat_copy["TintID"] = int(tint_id)
    except Exception:
        pass

    tint_node = _find_tint_value_node(mat_copy, node_label)
    if tint_node is not None:
        try:
            tint_node.outputs[0].default_value = float(tint_id)
        except Exception:
            pass

    _MAT_VARIANT_CACHE[key] = mat_copy
    return mat_copy


def apply_tint_id_to_object_materials(obj: bpy.types.Object, tint_id: int, *, node_label: str = "TintID") -> None:
    """Assign per-object tinted material variants so different objects don't fight over shared datablocks."""
    if obj is None or not hasattr(obj, "material_slots"):
        return

    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            continue

        # Only act on materials that actually contain the TintID value node
        if _find_tint_value_node(mat, node_label) is None:
            continue

        # Swap the slot material to a cached variant keyed by tint_id
        try:
            slot.material = _get_or_create_tinted_variant(mat, int(tint_id), node_label)
        except Exception:
            continue


def relink_tinted_materials_by_tint_id(objs: list[bpy.types.Object], *, node_label: str = "TintID") -> None:
    """Relink tinted materials so that objects only share materials when TintID matches.

    Some steps in the import pipeline (repairs/overrides) can cause Blender to re-use
    a material datablock across multiple objects after we set TintID, resulting in
    tint 'bleed' where a later object's TintID appears on earlier objects.

    Strategy:
      1) Identify all material slots that participate in tinting (contain the TintID value node).
      2) Unlink them by copying per-slot so no shared datablocks remain.
      3) Re-link them back using a cached variant per (base_key, TintID).

    This guarantees:
      - different TintID values never share a material
      - identical TintID values share a material (keeps material count under control)
    """

    if not objs:
        return

    base_source: dict[str, bpy.types.Material] = {}
    any_tinted = False
    for obj in objs:
        if obj is None or not hasattr(obj, "material_slots"):
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None:
                continue
            if _find_tint_value_node(mat, node_label) is None:
                continue
            any_tinted = True
            base_key = _material_base_key(mat)
            base_source.setdefault(base_key, mat)

    if not any_tinted:
        return

    # Step 1: unlink all tinted slots by copying their materials so nothing is shared.
    for obj in objs:
        if obj is None or not hasattr(obj, "material_slots"):
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None:
                continue
            if _find_tint_value_node(mat, node_label) is None:
                continue
            base_key = _material_base_key(mat)
            try:
                mcopy = mat.copy()
                try:
                    mcopy["__PaletteBase"] = base_key
                except Exception:
                    pass
                slot.material = mcopy
            except Exception:
                continue

    # Step 2: re-link each tinted slot to the correct cached variant by TintID.
    for obj in objs:
        if obj is None or not hasattr(obj, "material_slots"):
            continue
        # Determine per-slot tint ids from object custom props (source of truth).
        try:
            raw = obj.get("TintIDs", "[]")
            tint_ids = json.loads(raw) if isinstance(raw, str) else list(raw)
            tint_ids = [int(x) for x in tint_ids]
        except Exception:
            tint_ids = []
        try:
            width = int(obj.get("TintPacketWidth", 0))
        except Exception:
            width = 0

        # Count how many material slots are actually tinted (contain the TintID node)
        tinted_slots = []
        for s in obj.material_slots:
            m = s.material
            if m is None:
                continue
            if _find_tint_value_node(m, node_label) is None:
                continue
            tinted_slots.append(s)

        assigned_tints = assign_tint_ids_to_tinted_slots(tint_ids, width, len(tinted_slots))

        # Apply per-slot tint, in slot order
        tint_iter = iter(assigned_tints)

        for slot in obj.material_slots:
            mat = slot.material
            if mat is None:
                continue
            if _find_tint_value_node(mat, node_label) is None:
                continue
            base_key = _material_base_key(mat)
            base_mat = base_source.get(base_key, mat)
            try:
                if "__PaletteBase" not in base_mat:
                    base_mat["__PaletteBase"] = base_key
            except Exception:
                pass
            try:
                slot.material = _get_or_create_tinted_variant(base_mat, int(next(tint_iter, 0)), node_label)
            except Exception:
                continue


def store_tint_custom_props(obj: bpy.types.Object, tint_ids: list[int], *, packet_width: int | None = None) -> None:
    """Store TintID + TintIDs custom properties on object."""
    try:
        obj["TintIDs"] = json.dumps(tint_ids)
    except Exception:
        pass
    try:
        obj["TintID"] = int(tint_ids[0]) if tint_ids else 0
    except Exception:
        pass
    if packet_width is not None:
        try:
            obj["TintPacketWidth"] = int(packet_width)
        except Exception:
            pass
