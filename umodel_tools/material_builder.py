import json
import os
import re
import typing as t

import bpy

from . import asset_db
from . import map_importer
from . import utils
from .ops import override_materials as override_ops
from .ops.override_materials import strip_objectpath_trailing_dotnum


def _warn(importer, msg: str) -> None:
    try:
        importer._warn_print(msg)
    except Exception:
        print(msg)


def _profile_import_materials_enabled(importer) -> bool:
    try:
        return bool(getattr(importer, 'import_materials', True))
    except Exception:
        return True


def encode_base_material_slots(obj: bpy.types.Object) -> str:
    try:
        slots = []
        for slot in obj.material_slots:
            slots.append({
                "sn": slot.name or "",
                "mn": slot.material.name if slot.material else ""
            })
        return json.dumps(slots, ensure_ascii=False)
    except Exception:
        return "[]"


def decode_base_material_slots(s: str) -> t.Optional[list[dict[str, str]]]:
    if not s:
        return None
    try:
        arr = json.loads(s)
    except Exception:
        return None
    if not isinstance(arr, list):
        return None
    out: list[dict[str, str]] = []
    for entry in arr:
        if isinstance(entry, dict):
            out.append({
                "sn": str(entry.get("sn", "")) if entry.get("sn", "") is not None else "",
                "mn": str(entry.get("mn", "")) if entry.get("mn", "") is not None else "",
            })
    return out


def _get_or_link_material_from_objectpath(importer,
                                          material_name: str,
                                          material_object_path: str,
                                          umodel_export_dir: str,
                                          asset_dir: str,
                                          game_profile: str,
                                          db: t.Optional[asset_db.AssetDB] = None):
    return override_ops.get_or_link_material_from_objectpath(
        importer=importer,
        material_name=material_name,
        material_object_path=material_object_path,
        umodel_export_dir=umodel_export_dir,
        asset_dir=asset_dir,
        game_profile=game_profile,
        db=db,
    )


def _extract_ref_name(val) -> str:
    if isinstance(val, dict):
        for k in ("ObjectName", "ObjectPath", "AssetPathName"):
            v = val.get(k)
            if isinstance(v, str) and v:
                if k == "ObjectName":
                    s = v.rsplit("'", 1)[-1] if "'" in v else v
                    s = s.rsplit(".", 1)[-1]
                    return str(s)
                s = strip_objectpath_trailing_dotnum(v)
                return s.rsplit('/', 1)[-1]
    elif isinstance(val, str) and val:
        s = strip_objectpath_trailing_dotnum(val)
        return s.rsplit('/', 1)[-1]
    return ""


def _extract_ref_path(val) -> str:
    if isinstance(val, dict):
        for k in ("ObjectPath", "AssetPathName"):
            v = val.get(k)
            if isinstance(v, str) and v:
                return strip_objectpath_trailing_dotnum(v)
        v = val.get("ObjectName")
        if isinstance(v, str) and v:
            return strip_objectpath_trailing_dotnum(v)
    elif isinstance(val, str) and val:
        return strip_objectpath_trailing_dotnum(val)
    return ""


def _load_base_slots_from_mesh_json(importer,
                                    obj: bpy.types.Object,
                                    *,
                                    umodel_export_dir: str,
                                    asset_dir: str,
                                    game_profile: str,
                                    db: t.Optional[asset_db.AssetDB] = None) -> t.Optional[list[dict[str, str]]]:
    try:
        mesh_objpath = obj.get("_umodel_mesh_object_path", "")
        mesh_objpath = strip_objectpath_trailing_dotnum(str(mesh_objpath)) if mesh_objpath else ""
        if not mesh_objpath:
            return None

        rel = os.path.normpath(str(mesh_objpath).lstrip('/'))
        json_path = os.path.join(umodel_export_dir, rel) + ".json"
        if not os.path.isfile(json_path):
            return None

        with open(json_path, mode='r', encoding='utf-8') as f:
            data = json.load(f)

        entries = data if isinstance(data, list) else []
        static_mats = None
        for ent in entries:
            if not isinstance(ent, dict):
                continue
            props = ent.get("Properties") or {}
            sm = props.get("StaticMaterials")
            if isinstance(sm, list) and sm:
                static_mats = sm
                break

        if not isinstance(static_mats, list) or not static_mats:
            return None

        out: list[dict[str, str]] = []
        for sm in static_mats:
            if not isinstance(sm, dict):
                continue
            mi = sm.get("MaterialInterface", None)
            mat_name = _extract_ref_name(mi)
            mat_op = _extract_ref_path(mi)
            if not mat_name and mat_op:
                mat_name = strip_objectpath_trailing_dotnum(mat_op).rsplit('/', 1)[-1]
            if mat_op:
                mat_op = strip_objectpath_trailing_dotnum(mat_op)
            slot_name = sm.get("MaterialSlotName", "") or sm.get("ImportedMaterialSlotName", "") or ""
            out.append({
                "si": "",
                "sn": str(slot_name) if slot_name is not None else "",
                "mn": str(mat_name) if mat_name is not None else "",
                "op": str(mat_op) if mat_op is not None else "",
            })

        if _profile_import_materials_enabled(importer):
            for e in out:
                mn = e.get("mn", "")
                op = e.get("op", "")
                if not mn or not op:
                    continue
                if bpy.data.materials.get(mn) is not None:
                    continue
                alt = None
                prefix = mn + "."
                for m in bpy.data.materials:
                    if m.name.startswith(prefix) and m.name[len(prefix):].isdigit():
                        alt = m
                        break
                if alt is not None:
                    continue
                _get_or_link_material_from_objectpath(
                    importer,
                    material_name=mn,
                    material_object_path=op,
                    umodel_export_dir=umodel_export_dir,
                    asset_dir=asset_dir,
                    game_profile=game_profile,
                    db=db
                )

        return out
    except Exception:
        return None


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
            _warn(importer, f"[MaterialBuilder] Failed reading UMAP JSON for overrides/tints: {umap_json_path} ({exc})")
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
        packet_width = None

        # Prefer per-instance custom data when present.
        if getattr(sm, 'is_instanced', False):
            packet_width = getattr(sm, 'per_instance_packet_width', None)
            try:
                inst_idx = int(obj.get('_umodel_instance_index', -1))
            except Exception:
                inst_idx = -1
            per_instance = getattr(sm, 'per_instance_tint_ids', None)
            if isinstance(per_instance, list) and 0 <= inst_idx < len(per_instance):
                tint_ids = per_instance[inst_idx]

        # Fallback to component-level CustomPrimitiveData.Data when available.
        if tint_ids is None:
            cpd_tint_ids = getattr(sm, 'custom_primitive_tint_ids', None)
            if isinstance(cpd_tint_ids, list):
                tint_ids = cpd_tint_ids
                packet_width = getattr(sm, 'custom_primitive_packet_width', None)

        if tint_ids is None:
            tint_ids = []
        map_importer.color_palette_unwrapper.store_tint_custom_props(obj, tint_ids, packet_width=packet_width)
        return True
    except Exception as exc:
        _warn(importer, f"[MaterialBuilder] Failed regenerating tint props for {obj.name}: {exc}")
        return False


def persist_base_material_slots(importer, obj: bpy.types.Object) -> None:
    if obj.type != "MESH":
        return
    if obj.get("_umodel_base_material_slots", ""):
        return
    if len(obj.material_slots) == 0:
        return

    encoded = encode_base_material_slots(obj)
    try:
        decoded = decode_base_material_slots(encoded) or []
        useful = any((e.get("sn") or e.get("mn")) for e in decoded)
    except Exception:
        useful = True

    if useful:
        obj["_umodel_base_material_slots"] = encoded


def repair_base_material_slots(importer,
                               obj: bpy.types.Object,
                               *,
                               umodel_export_dir: str,
                               asset_dir: str,
                               game_profile: str,
                               db: asset_db.AssetDB,
                               force_reassign: bool = False) -> bool:
    if obj.type != "MESH" or len(obj.material_slots) == 0:
        return False

    def _is_placeholder_material(mat: t.Optional[bpy.types.Material]) -> bool:
        if mat is None:
            return True
        n = (getattr(mat, "name", "") or "").strip()
        if not n:
            return True
        if n.isdigit():
            return True
        return False

    s = obj.get("_umodel_base_material_slots", "")
    data = decode_base_material_slots(s) if s else None

    if not data or not any((e.get("sn") or e.get("mn")) for e in data):
        fallback = _load_base_slots_from_mesh_json(
            importer,
            obj,
            umodel_export_dir=umodel_export_dir,
            asset_dir=asset_dir,
            game_profile=game_profile,
            db=db,
        )
        if fallback:
            utils.verbose_print(f"Base slot JSON fallback for {obj.name}: slots={len(fallback)}")
            try:
                obj["_umodel_base_material_slots"] = json.dumps(
                    [{"sn": e.get("sn", ""), "mn": e.get("mn", "")} for e in fallback],
                    ensure_ascii=False
                )
            except Exception:
                pass
            entries = fallback
        else:
            try:
                mesh_objpath = obj.get("_umodel_mesh_object_path", "")
                mesh_objpath = strip_objectpath_trailing_dotnum(str(mesh_objpath)) if mesh_objpath else ""
                rel = os.path.normpath(str(mesh_objpath).lstrip('/')) if mesh_objpath else ""
                json_path = (os.path.join(umodel_export_dir, rel) + ".json") if rel else ""
                utils.verbose_print(
                    f"Base slot repair failed for {obj.name}: no persisted slots and no JSON fallback "
                    f"(mesh_objpath={mesh_objpath!r}, json_exists={os.path.isfile(json_path) if json_path else False})"
                )
            except Exception:
                pass
            return False
    else:
        entries = data

    if (not force_reassign and
            not any((slot.material is None) or (not slot.name) or _is_placeholder_material(slot.material)
                    for slot in obj.material_slots)):
        return False

    repaired = False
    override_ops.ensure_object_slot_overrides(obj)

    for seq_i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        desired_sn = entry.get("sn", "")
        desired_mn = entry.get("mn", "")
        desired_op = entry.get("op", "")
        slot_i = seq_i
        if slot_i < 0 or slot_i >= len(obj.material_slots):
            continue

        slot = obj.material_slots[slot_i]
        if desired_sn and not slot.name and not str(desired_sn).strip().isdigit():
            try:
                slot.name = desired_sn
                repaired = True
            except Exception:
                pass

        should_assign = bool(desired_mn) and (force_reassign or _is_placeholder_material(slot.material))
        if should_assign:
            mat = bpy.data.materials.get(desired_mn)
            if mat is None:
                prefix = desired_mn + "."
                for m in bpy.data.materials:
                    if m.name.startswith(prefix) and m.name[len(prefix):].isdigit():
                        mat = m
                        break
            if mat is None and desired_op and _profile_import_materials_enabled(importer):
                mat = _get_or_link_material_from_objectpath(
                    importer,
                    material_name=desired_mn,
                    material_object_path=desired_op,
                    umodel_export_dir=umodel_export_dir,
                    asset_dir=asset_dir,
                    game_profile=game_profile,
                    db=db
                )
            if mat is not None:
                slot.material = mat
                repaired = True

    if repaired:
        still_blank = sum(1 for s in obj.material_slots if (s.material is None) or (not s.name))
        if still_blank:
            utils.verbose_print(f"Base slot repair incomplete for {obj.name}: still_blank={still_blank}")
            try:
                want = [e.get('mn', '') for e in entries[:len(obj.material_slots)] if isinstance(e, dict)]
                have = [(s.name, getattr(s.material, 'name', None)) for s in obj.material_slots]
                utils.verbose_print(f"  wanted_mats={want}")
                utils.verbose_print(f"  have_slots={have}")
            except Exception:
                pass

    return repaired


def apply_override_materials_to_object(importer,
                                       obj: bpy.types.Object,
                                       override_materials: list[t.Optional[tuple[str, str]]] | None,
                                       *,
                                       umodel_export_dir: str,
                                       asset_dir: str,
                                       game_profile: str,
                                       db: asset_db.AssetDB) -> int:
    return override_ops.apply_override_materials_to_object(
        importer=importer,
        obj=obj,
        override_materials=override_materials,
        umodel_export_dir=umodel_export_dir,
        asset_dir=asset_dir,
        game_profile=game_profile,
        db=db,
    )
