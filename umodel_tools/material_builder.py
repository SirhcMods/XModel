import json
import os
import re
import typing as t

import bpy

from . import asset_db
from . import map_importer


def resolve_object_umap_json_path(obj: bpy.types.Object, umap_root: str) -> str | None:
    if not umap_root or not os.path.isdir(umap_root):
        return None

    candidates: list[str] = []

    try:
        stored = str(obj.get("_umodel_source_umap", "") or "").strip()
    except Exception:
        stored = ""
    if stored:
        candidates.append(stored)
        base = os.path.basename(stored.replace('\\', '/'))
        if base:
            candidates.append(base)
            stem = os.path.splitext(base)[0]
            if stem:
                candidates.append(stem)

    try:
        for coll in getattr(obj, 'users_collection', []) or []:
            cname = str(getattr(coll, 'name', '') or '').strip()
            if not cname:
                continue
            candidates.append(cname)
            candidates.append(re.sub(r"\.\d{3}$", "", cname))
    except Exception:
        pass

    seen = set()
    norm_candidates = []
    for c in candidates:
        if not c:
            continue
        for v in (c, os.path.splitext(c)[0]):
            v = str(v or '').strip()
            if v and v not in seen:
                seen.add(v)
                norm_candidates.append(v)

    for cand in norm_candidates:
        direct = os.path.join(umap_root, cand)
        if os.path.isfile(direct):
            return direct
        if os.path.isfile(direct + '.json'):
            return direct + '.json'

    target_stems = {os.path.splitext(c)[0].lower() for c in norm_candidates if c}
    if not target_stems:
        return None
    for root, _dirs, files in os.walk(umap_root):
        for filename in files:
            if not filename.lower().endswith('.json'):
                continue
            if os.path.splitext(filename)[0].lower() in target_stems:
                return os.path.join(root, filename)
    return None


def _matrix_key_from_object(obj: bpy.types.Object, mesh_objpath: str) -> tuple | None:
    try:
        vals = []
        mw = obj.matrix_world
        for r in range(3):
            for c in range(4):
                vals.append(round(float(mw[r][c]), 3))
        return (str(mesh_objpath or ''), *vals)
    except Exception:
        return None


def _matrix_key_from_static_mesh(static_mesh: map_importer.StaticMesh) -> list[tuple]:
    keys = []
    mesh_path = getattr(static_mesh, 'mesh_object_path', '')
    if static_mesh.invalid:
        return keys
    try:
        if getattr(static_mesh, 'is_instanced', False):
            for inst in getattr(static_mesh, 'instance_transforms', []) or []:
                mat_world = static_mesh.transform.matrix_4x4 @ inst.matrix_4x4
                if static_mesh.parent_mtx is not None:
                    mat_world = static_mesh.parent_mtx @ mat_world
                vals = []
                for r in range(3):
                    for c in range(4):
                        vals.append(round(float(mat_world[r][c]), 3))
                keys.append((str(mesh_path or ''), *vals))
        else:
            mat_world = static_mesh.transform.matrix_4x4
            if static_mesh.parent_mtx is not None:
                mat_world = static_mesh.parent_mtx @ mat_world
            vals = []
            for r in range(3):
                for c in range(4):
                    vals.append(round(float(mat_world[r][c]), 3))
            keys.append((str(mesh_path or ''), *vals))
    except Exception:
        return []
    return keys


def find_umap_static_mesh_match_for_object(importer, obj: bpy.types.Object, umap_json_path: str):
    if not umap_json_path or not os.path.isfile(umap_json_path):
        return None
    mesh_objpath = str(obj.get("_umodel_mesh_object_path", "") or "")
    if not mesh_objpath:
        return None
    obj_key = _matrix_key_from_object(obj, mesh_objpath)
    if obj_key is None:
        return None

    cache = getattr(importer, '_umap_override_cache', None)
    if cache is None:
        cache = {}
        importer._umap_override_cache = cache

    per_file = cache.get(umap_json_path)
    if per_file is None:
        try:
            with open(umap_json_path, mode='r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as exc:
            importer._warn_print(f"[MaterialBuilder] Failed reading UMAP JSON for overrides/tints: {umap_json_path} ({exc})")
            cache[umap_json_path] = {}
            return None

        per_file = {}
        entries = data if isinstance(data, list) else []
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            ent_type = ent.get('Type')
            if ent_type not in map_importer.StaticMesh.static_mesh_types:
                continue
            try:
                sm = map_importer.StaticMesh(data, ent, ent_type)
            except Exception:
                continue
            if getattr(sm, 'invalid', False):
                continue
            for k in _matrix_key_from_static_mesh(sm):
                if k not in per_file:
                    per_file[k] = sm
        cache[umap_json_path] = per_file

    return per_file.get(obj_key)


def regen_tint_custom_props_from_static_mesh(importer, obj: bpy.types.Object, sm) -> bool:
    if sm is None:
        return False
    try:
        tint_ids = None
        packet_width = getattr(sm, 'per_instance_packet_width', None)
        if getattr(sm, 'is_instanced', False):
            try:
                inst_idx = int(obj.get('_umodel_instance_index', -1))
            except Exception:
                inst_idx = -1
            per_instance = getattr(sm, 'per_instance_tint_ids', None)
            if isinstance(per_instance, list) and 0 <= inst_idx < len(per_instance):
                tint_ids = per_instance[inst_idx]
        if tint_ids is None:
            tint_ids = []
        map_importer.color_palette_unwrapper.store_tint_custom_props(obj, tint_ids, packet_width=packet_width)
        return True
    except Exception as exc:
        importer._warn_print(f"[MaterialBuilder] Failed regenerating tint props for {obj.name}: {exc}")
        return False


def persist_base_material_slots(importer, obj: bpy.types.Object) -> None:
    return map_importer.MapImporter._persist_base_material_slots(importer, obj)


def repair_base_material_slots(importer,
                               obj: bpy.types.Object,
                               *,
                               umodel_export_dir: str,
                               asset_dir: str,
                               game_profile: str,
                               db: asset_db.AssetDB,
                               force_reassign: bool = False) -> bool:
    return map_importer.MapImporter._repair_base_material_slots(
        importer,
        obj,
        umodel_export_dir=umodel_export_dir,
        asset_dir=asset_dir,
        game_profile=game_profile,
        db=db,
        force_reassign=force_reassign,
    )


def apply_override_materials_to_object(importer,
                                       obj: bpy.types.Object,
                                       override_materials: list[t.Optional[tuple[str, str]]] | None,
                                       *,
                                       umodel_export_dir: str,
                                       asset_dir: str,
                                       game_profile: str,
                                       db: asset_db.AssetDB) -> int:
    return map_importer.MapImporter._apply_override_materials_to_object(
        importer,
        obj,
        override_materials,
        umodel_export_dir=umodel_export_dir,
        asset_dir=asset_dir,
        game_profile=game_profile,
        db=db,
    )
