import json
import os
import typing as t

import bpy

from .. import utils

# Regex to strip trailing '.<digits>' from UE ObjectPath strings
import re
_RE_TRAILING_OBJPATH_DOTNUM = re.compile(r"\.(\d+)$")

def strip_objectpath_trailing_dotnum(object_path: str) -> str:
    """Strip trailing '.<digits>' from UE ObjectPath while preserving inner periods."""
    if not object_path:
        return object_path
    m = _RE_TRAILING_OBJPATH_DOTNUM.search(object_path)
    if m:
        return object_path[:m.start()]
    return object_path
from .. import asset_db

# NOTE:
# This contains OverrideMaterials related logic.
# Its meant to be called from the importer context (MapImporter/AssetImporter),
# and must preserve the linked-library workflow (no mesh datablock duplication).

OverrideEntry = tuple[str, str]  # (material_name, material_object_path)
OverrideList = list[t.Optional[OverrideEntry]]


def encode_override_materials(override_materials: t.Optional[OverrideList]) -> str:
    """Encode override list to a JSON string for storing on Blender objects."""
    if not override_materials:
        return "[]"
    packed: list[t.Optional[dict[str, str]]] = []
    for entry in override_materials:
        if entry is None:
            packed.append(None)
        else:
            n, p = entry
            packed.append({"n": n, "p": p})
    return json.dumps(packed, ensure_ascii=False)


def decode_override_materials(s: str) -> t.Optional[OverrideList]:
    """Decode override list from a JSON string stored on Blender objects."""
    if not s:
        return None
    try:
        arr = json.loads(s)
    except Exception:
        return None
    if not isinstance(arr, list):
        return None
    out: OverrideList = []
    for entry in arr:
        if entry is None:
            out.append(None)
        elif isinstance(entry, dict):
            n = entry.get("n", "")
            p = entry.get("p", "")
            if n and p:
                out.append((n, p))
            else:
                out.append(None)
        else:
            out.append(None)
    return out


def ensure_object_slot_overrides(obj: bpy.types.Object) -> None:
    """Switch object material slots to OBJECT link once, preserving base materials.

    This prevents global bleed (shared datablocks) while avoiding empty/no-name slot issues.
    """
    if obj.get("_umodel_override_slots_ready") == 1:
        return
    if obj.type != 'MESH' or obj.data is None:
        return

    # Snapshot effective materials from current slots (not obj.data.materials).
    base_mats = [slot.material for slot in obj.material_slots]
    slot_count = len(obj.material_slots)

    for i in range(slot_count):
        slot = obj.material_slots[i]
        try:
            slot.link = 'OBJECT'
        except Exception:
            pass
        # Restore previous effective material so we don't create blank slots.
        if i < len(base_mats) and base_mats[i] is not None:
            slot.material = base_mats[i]

    obj["_umodel_override_slots_ready"] = 1


def _material_has_valid_nodes(mat: t.Optional[bpy.types.Material]) -> bool:
    if mat is None:
        return False
    try:
        if not getattr(mat, "use_nodes", False):
            return False
        node_tree = getattr(mat, "node_tree", None)
        if node_tree is None:
            return False
        return len(node_tree.nodes) > 0
    except Exception:
        return False


def get_or_link_material_from_objectpath(importer: t.Any,
                                         material_name: str,
                                         material_object_path: str,
                                         umodel_export_dir: str,
                                         asset_dir: str,
                                         game_profile: str,
                                         db: t.Optional[asset_db.AssetDB] = None
                                         ) -> t.Optional[bpy.types.Material]:
    """Ensure material exists in the asset library and is linked into the current file.

    Uses the addon's existing material import pipeline (links from asset library) and
    preserves the linked-data performance model.
    """
    if not material_name or not material_object_path:
        return None

    mat = bpy.data.materials.get(material_name)
    if _material_has_valid_nodes(mat):
        return mat
    if mat is not None:
        try:
            importer._warn_print(f'[MaterialBuilder] Rebuilding invalid existing material: {material_name}')
        except Exception:
            print(f'[MaterialBuilder] Rebuilding invalid existing material: {material_name}')

    # Convert UE object path -> library relative path (no ext, strip trailing .0/.1/etc)
    stripped = strip_objectpath_trailing_dotnum(material_object_path)
    rel_no_ext = os.path.normpath(stripped.lstrip('/'))
    material_lib_path = os.path.join(asset_dir, rel_no_ext) + ".blend"

    try:
        # Ensure the library .blend exists. If not, import it using the addon pipeline.
        if not os.path.isfile(material_lib_path):
            if db is None:
                db = asset_db.AssetDB(db_root_path=asset_dir)
            importer._import_material_to_library(  # noqa: SLF001 - must be called in importer context
                material_name=material_name,
                material_path_local_no_ext=rel_no_ext,
                db=db,
                umodel_export_dir=umodel_export_dir,
                asset_library_dir=asset_dir,
                game_profile=game_profile
            )

        # Already linked from that library?
        existing = utils.linked_libraries_search(material_lib_path, bpy.types.Material)
        if _material_has_valid_nodes(existing):
            return existing
        if existing is not None:
            try:
                importer._warn_print(f'[MaterialBuilder] Re-linking invalid library material: {existing.name}')
            except Exception:
                print(f'[MaterialBuilder] Re-linking invalid library material: {existing.name}')

        # Link it
        with utils.redirect_cstdout():
            with bpy.data.libraries.load(filepath=material_lib_path, link=True) as (data_from, data_to):
                chosen_name = None
                for n in data_from.materials:
                    if n == material_name:
                        chosen_name = n
                        break
                if chosen_name is None and data_from.materials:
                    chosen_name = data_from.materials[0]
                data_to.materials = [chosen_name] if chosen_name else []

            return data_to.materials[0] if data_to.materials else None

    except Exception as e:
        try:
            importer._warn_print(f'Warning: Override material "{material_name}" failed to load: {e}')
        except Exception:
            print(f'Warning: Override material "{material_name}" failed to load: {e}')
        return None


def apply_override_materials_to_object(importer: t.Any,
                                      obj: bpy.types.Object,
                                      override_materials: t.Optional[OverrideList],
                                      umodel_export_dir: str,
                                      asset_dir: str,
                                      game_profile: str,
                                      db: t.Optional[asset_db.AssetDB] = None) -> None:
    """Apply OverrideMaterials by slot index.

    Rules:
    - Only applies per-object (instance-safe)
    - Preserves base materials if override fails
    - Handles shorter lists and None entries
    """
    if obj.type != 'MESH' or obj.data is None:
        return
    if not override_materials:
        return
    if not any(x is not None for x in override_materials):
        return

    # Persist override list on object so we can re-apply after library reload
    try:
        obj["_umodel_override_materials"] = encode_override_materials(override_materials)
    except Exception:
        pass

    ensure_object_slot_overrides(obj)

    slot_count = len(obj.material_slots)
    limit = min(slot_count, len(override_materials))

    applied = 0
    missing = 0

    for i in range(limit):
        entry = override_materials[i]
        if entry is None:
            continue

        mat_name, mat_objpath = entry
        ov_mat = get_or_link_material_from_objectpath(
            importer=importer,
            material_name=mat_name,
            material_object_path=mat_objpath,
            umodel_export_dir=umodel_export_dir,
            asset_dir=asset_dir,
            game_profile=game_profile,
            db=db
        )

        if ov_mat is None:
            missing += 1
            continue

        obj.material_slots[i].material = ov_mat
        applied += 1

    if applied or missing:
        utils.verbose_print(f"OverrideMaterials applied for {obj.name}: applied={applied}, missing={missing}")


def reapply_overrides_in_collection(importer: t.Any,
                                   collection_name: str,
                                   umodel_export_dir: str,
                                   asset_dir: str,
                                   game_profile: str) -> int:
    """Re-apply overrides for objects inside a collection (recursive), based on stored object props."""

    coll = bpy.data.collections.get(collection_name)
    if coll is None:
        return 0

    def _iter_objects_recursive(c: bpy.types.Collection):
        for o in c.objects:
            yield o
        for cc in c.children:
            yield from _iter_objects_recursive(cc)

    reapplied = 0
    for obj in _iter_objects_recursive(coll):
        s = obj.get("_umodel_override_materials", "")
        if not s:
            continue
        overrides = decode_override_materials(s)
        if not overrides or not any(x is not None for x in overrides):
            continue
        apply_override_materials_to_object(
            importer=importer,
            obj=obj,
            override_materials=overrides,
            umodel_export_dir=umodel_export_dir,
            asset_dir=asset_dir,
            game_profile=game_profile,
            db=None
        )
        reapplied += 1

    return reapplied
