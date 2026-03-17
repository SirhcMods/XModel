
import os
import typing as t

import numpy as np
import tqdm
import tqdm.contrib
import bpy
import gc
import math
import bpy_extras.io_utils
import importlib.util
import json
from pathlib import Path
import mathutils as mu
import re

from . import utils
from . import asset_importer
from . import asset_db
from . import map_importer
from . import preferences
from . import fmodel_json_parser
from . import props_txt_parser
from . import enums
from . import game_profiles

from .ops.bake_tints_to_attr import bake_tints_to_attr_on_selected
from .utils import _profile_feature_enabled
from .utils import _resolve_dir_path


def _get_object_aabb_verts(obj: bpy.types.Object) -> list[tuple[float, float, float]]:
    return [obj.matrix_world @ mu.Vector(corner) for corner in obj.bound_box]


def _strip_objectpath_trailing_dotnum(obj_path: str) -> str:
    if not obj_path:
        return ""
    head, dot, tail = obj_path.rpartition('.')
    if dot and tail.isdigit():
        return head
    return obj_path


def _extract_prop_asset_path_from_json(json_path: str) -> tuple[str, str] | tuple[None, None]:
    """Return (asset_name, asset_path_without_dotnum) from a mesh json beside a PSK/PSKX."""
    try:
        with open(json_path, mode='r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None, None

    entries = data if isinstance(data, list) else [data]
    static_mesh_ent = None
    for ent in entries:
        if isinstance(ent, dict) and ent.get('Type') == 'StaticMesh':
            static_mesh_ent = ent
            break
    if static_mesh_ent is None:
        return None, None

    asset_name = str(static_mesh_ent.get('Name') or Path(json_path).stem)

    obj_path = ""
    outer = static_mesh_ent.get('Outer')
    if isinstance(outer, dict):
        obj_path = str(outer.get('ObjectPath') or "")

    if not obj_path:
        props = static_mesh_ent.get('Properties')
        if isinstance(props, dict):
            aud = props.get('AssetUserData')
            if isinstance(aud, list):
                for item in aud:
                    if isinstance(item, dict):
                        candidate = item.get('ObjectPath')
                        if candidate:
                            obj_path = str(candidate)
                            break

    if not obj_path:
        return asset_name, asset_name

    return asset_name, _strip_objectpath_trailing_dotnum(obj_path).lstrip('/\\')




_RE_BLENDER_DUPLICATE_SUFFIX = re.compile(r"\.\d{3}$")


def _strip_blender_duplicate_suffix(name: str) -> str:
    if not name:
        return ""
    return _RE_BLENDER_DUPLICATE_SUFFIX.sub("", name)


def _normalize_mesh_lookup_name(name: str) -> str:
    name = _strip_blender_duplicate_suffix(str(name or "").strip())
    if name.lower().endswith('.mo'):
        name = name[:-3]
    return name


def _candidate_mesh_lookup_names(obj: bpy.types.Object) -> list[str]:
    names: list[str] = []

    def _push(value: t.Any):
        value = str(value or "").strip()
        if not value:
            return
        base = os.path.basename(value.replace('\\', '/'))
        base = os.path.splitext(base)[0]
        norm = _normalize_mesh_lookup_name(base)
        if norm and norm not in names:
            names.append(norm)

    try:
        _push(obj.get("_umodel_mesh_object_path", ""))
    except Exception:
        pass
    try:
        _push(getattr(getattr(obj, 'data', None), 'name', ''))
    except Exception:
        pass
    _push(getattr(obj, 'name', ''))
    return names


def _index_static_mesh_jsons(scan_root: str) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for root, _dirs, files in os.walk(scan_root):
        for filename in files:
            if not filename.lower().endswith('.json'):
                continue
            stem = os.path.splitext(filename)[0]
            key = stem.lower()
            index.setdefault(key, []).append(os.path.join(root, filename))
    return index

def _derive_prop_import_asset_path(item, umodel_export_dir: str) -> str:
    """Resolve the asset path to pass into AssetImporter._load_asset().

    Prefer the scanned mesh path relative to the global UModel export dir, because the
    JSON-derived ObjectPath can be incomplete for some prop dumps. Fall back to the
    JSON-derived asset path if needed. Returns a path without extension.
    """
    mesh_path = str(getattr(item, 'mesh_path', '') or '')
    if mesh_path:
        mesh_noext = os.path.splitext(os.path.normpath(mesh_path))[0]
        export_root = os.path.normpath(umodel_export_dir)
        try:
            common = os.path.commonpath([export_root, mesh_noext])
        except ValueError:
            common = ''
        if common == export_root:
            rel_noext = os.path.relpath(mesh_noext, export_root)
            if rel_noext and rel_noext not in {'.', ''}:
                return rel_noext.lstrip('/\\')

    asset_path = str(getattr(item, 'asset_path', '') or '')
    if asset_path:
        return os.path.normpath(asset_path).lstrip('/\\')

    asset_name = str(getattr(item, 'asset_name', '') or '')
    return os.path.splitext(asset_name)[0]

def _load_landscape_compiler_module():
    """
    Loads:
        umodel_tools/game_profiles/mindseye/landscape_material_compiler.py

    using a direct file loader so we do not conflict with the existing
    game_profiles/mindseye.py module.
    """
    compiler_path = Path(__file__).parent / "game_profiles" / "mindseye" / "landscape_material_compiler.py"

    if not compiler_path.is_file():
        raise FileNotFoundError(f"Landscape compiler not found: {compiler_path}")

    module_name = "umodel_tools._mindseye_landscape_material_compiler"

    spec = importlib.util.spec_from_file_location(module_name, compiler_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create module spec for: {compiler_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UMODEL_OT_scan_bpp_dir(bpy.types.Operator):
    bl_idname = "umodel.scan_bpp_dir"
    bl_label = "Scan BPP Directory"
    bl_description = "Scan a directory for BPP_*.json files and add them to the list"

    def execute(self, context):
        scene = context.scene

        if not hasattr(scene, "umodel_bpp_scan_results"):
            self.report({'ERROR'}, "BPP scan results property not registered. Re-enable addon or restart Blender.")
            return {'CANCELLED'}

        scan_dir: str = _resolve_dir_path(scene.umodel_bpp_scan_dir)
        if not scan_dir:
            self.report({'ERROR'}, "Set a BPP JSON Directory first")
            return {'CANCELLED'}

        if not os.path.isdir(scan_dir):
            self.report({'ERROR'}, f"Directory not found: {scan_dir}")
            return {'CANCELLED'}

        existing_paths = {item.bpp_path for item in scene.umodel_bpp_scan_results}

        found = 0
        added = 0
        for root, _dirs, files in os.walk(scan_dir):
            for filename in files:
                if not filename.lower().endswith(".json"):
                    continue
                if not filename.startswith("BPP_"):
                    continue

                found += 1
                json_path = os.path.join(root, filename)
                if json_path in existing_paths:
                    continue

                item = scene.umodel_bpp_scan_results.add()
                item.bpp_name = os.path.splitext(os.path.basename(json_path))[0]
                item.bpp_path = json_path
                existing_paths.add(json_path)
                added += 1

        self.report({'INFO'}, f"BPP scan complete: found {found} file(s), added {added} new")
        return {'FINISHED'}


class UMODEL_OT_clear_bpp_scan_results(bpy.types.Operator):
    bl_idname = "umodel.clear_bpp_scan_results"
    bl_label = "Clear BPP Scan Results"
    bl_description = "Clear the BPP scan results list"

    def execute(self, context):
        scene = context.scene
        scene.umodel_bpp_scan_results.clear()
        scene.umodel_bpp_scan_index = 0
        return {'FINISHED'}


class UMODEL_OT_build_bpp_selected(map_importer.MapImporter, bpy.types.Operator):
    bl_idname = "umodel.build_bpp_selected"
    bl_label = "Build Selected BPP(s)"
    bl_description = "Build selected BPP prefabs by importing their instanced meshes"

    def _extract_bpp_collection_name(self, json_obj, fallback_name: str) -> str:
        # Prefer the BlueprintGeneratedClass Name (ends with _C)
        for ent in json_obj:
            if ent.get("Type") == "BlueprintGeneratedClass":
                name = ent.get("Name")
                if name:
                    return name
        return fallback_name

    def _unique_collection_name(self, base: str) -> str:
        if base not in bpy.data.collections:
            return base
        i = 1
        while f"{base}.{i:03d}" in bpy.data.collections:
            i += 1
        return f"{base}.{i:03d}"

    def _import_bpp(self,
                    context: bpy.types.Context,
                    bpp_path: str,
                    umodel_export_dir: str,
                    asset_dir: str,
                    game_profile: str,
                    db: asset_db.AssetDB) -> bool:

        if not os.path.isfile(bpp_path):
            self._warn_print(f"Warning: BPP file not found: {bpp_path}")
            return False

        with open(bpp_path, mode='r', encoding='utf-8') as f:
            json_object = json.load(f)


        # Lazy import to avoid circular imports
        from .map_importer import StaticMesh  # pylint: disable=import-outside-toplevel

        base_name = os.path.splitext(os.path.basename(bpp_path))[0]
        collection_name = self._extract_bpp_collection_name(json_object, base_name)
        collection_name = self._unique_collection_name(collection_name)

        import_collection = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(import_collection)

        imported_any = False

        with utils.std_out_err_redirect_tqdm() as orig_stdout:
            tqdm_desc = f'Building BPP "{collection_name}"'
            for entity in tqdm.tqdm(json_object,
                                    desc=tqdm_desc,
                                    file=orig_stdout,
                                    dynamic_ncols=True,
                                    ascii=True):
                entity_type = entity.get("Type")
                if not entity_type:
                    continue

                # Use same static mesh handling as UMAP importer
                if entity_type in StaticMesh.static_mesh_types:
                    static_mesh = StaticMesh(json_object, entity, entity_type)

                    if static_mesh.invalid:
                        continue

                    # Import/link the mesh object via asset library (same path as UMAP workflow)
                    obj = self._load_asset(
                        context=context,
                        asset_dir=asset_dir,
                        asset_path=static_mesh.asset_path,
                        umodel_export_dir=umodel_export_dir,
                        load=True,
                        db=db,
                        game_profile=game_profile
                    )

                    if not obj:
                        self._warn_print(f"Warning: Failed to import asset {static_mesh.asset_path}")
                        continue

                    static_mesh.link_object_instance(
                        self,
                        obj,
                        import_collection,
                        umodel_export_dir,
                        asset_dir,
                        game_profile,
                        db=db
                    )
                    imported_any = True

        if imported_any and self.import_materials:
            # Run the same post-import reload + re-apply step used by UMAP import.
            # This fixes cases where linked material pointers end up blank after linking.
            try:
                self._post_import_reload_and_reapply(
                    collection_name=import_collection.name,
                    umodel_export_dir=umodel_export_dir,
                    asset_dir=asset_dir,
                    game_profile=game_profile,
                    apply_override_materials=getattr(self, 'apply_override_materials', True)
                )
            except Exception as e:
                self._warn_print(f"[umodel_tools] Warning: BPP post-import material repair failed: {e}")

        return imported_any

    def execute(self, context: bpy.types.Context) -> set[str]:
        profile = preferences.get_addon_preferences().get_active_profile()
        if profile is None:
            return self._op_message('ERROR', "You need to have an active game profile selected.")

        umodel_export_dir = os.path.normpath(bpy.path.abspath(profile.umodel_export_dir))
        if not umodel_export_dir or not os.path.isdir(umodel_export_dir):
            return self._op_message('ERROR', f"Invalid UModel export dir: {umodel_export_dir}")

        asset_dir = os.path.normpath(bpy.path.abspath(profile.asset_dir))
        if not asset_dir or not os.path.isdir(asset_dir):
            return self._op_message('ERROR', f"Invalid asset dir: {asset_dir}")

        scene = context.scene

        # BPP Builder has its own UI toggle (scene property) for applying OverrideMaterials.
        # MapImporter.apply_override_materials is an operator property, so we must copy the
        # scene setting into the operator instance before we start importing/linking meshes.
        try:
            self.import_materials = bool(getattr(scene, "umodel_import_materials", True))
        except Exception:
            self.import_materials = True
        try:
            # Prefer unified General setting; fall back to legacy BPP-only toggle if present
            self.apply_override_materials = self.import_materials and bool(getattr(scene, "umodel_apply_override_materials", getattr(scene, "umodel_bpp_apply_override_materials", False)))
        except Exception:
            # If anything goes wrong, fall back to not applying overrides.
            self.apply_override_materials = False
        try:
            self.load_pbr_maps = self.import_materials and bool(getattr(scene, "umodel_load_pbr_maps", True))
        except Exception:
            self.load_pbr_maps = True

        # build list of selected entries; if none checked, use active index
        selected_items = [it for it in scene.umodel_bpp_scan_results if getattr(it, "selected", False)]

        if not selected_items and len(scene.umodel_bpp_scan_results) > 0:
            idx = int(scene.umodel_bpp_scan_index)
            if 0 <= idx < len(scene.umodel_bpp_scan_results):
                selected_items = [scene.umodel_bpp_scan_results[idx]]

        if not selected_items:
            return self._op_message('ERROR', "No BPPs selected (check items in list or select one).")

        db = asset_db.AssetDB(asset_dir)
        built = 0

        for item in selected_items:
            if self._import_bpp(
                context=context,
                bpp_path=item.bpp_path,
                umodel_export_dir=umodel_export_dir,
                asset_dir=asset_dir,
                game_profile=profile.game,
                db=db
            ):
                built += 1

        db.save_db()

        return self._op_message('INFO', f"Built {built}/{len(selected_items)} BPP(s).")


class UMODEL_OT_scan_prop_dir(bpy.types.Operator):
    bl_idname = "umodel.scan_prop_dir"
    bl_label = "Scan Prop Directory"
    bl_description = "Scan a directory for prop .psk/.pskx files with matching .json files"

    @classmethod
    def poll(cls, _context):
        return _profile_feature_enabled("ENABLE_PROP_BUILDER")

    def execute(self, context):
        scene = context.scene

        if not hasattr(scene, "umodel_prop_scan_results"):
            self.report({'ERROR'}, "Prop scan results property not registered. Re-enable addon or restart Blender.")
            return {'CANCELLED'}

        scan_dir = _resolve_dir_path(getattr(scene, 'umodel_prop_scan_dir', ''))
        if not scan_dir:
            self.report({'ERROR'}, "Set a Prop directory first")
            return {'CANCELLED'}
        if not os.path.isdir(scan_dir):
            self.report({'ERROR'}, f"Prop directory does not exist: {scan_dir}")
            return {'CANCELLED'}

        scene.umodel_prop_scan_results.clear()
        scene.umodel_prop_scan_index = 0

        found = 0

        for root, _, files in os.walk(scan_dir):
            lower_files = {f.lower(): f for f in files}
            for filename in files:
                base, ext = os.path.splitext(filename)
                if ext.lower() not in {'.psk', '.pskx'}:
                    continue

                json_name = lower_files.get((base + '.json').lower())
                if not json_name:
                    continue

                json_path = os.path.join(root, json_name)
                asset_name, asset_path = _extract_prop_asset_path_from_json(json_path)
                if not asset_path:
                    continue

                rel_root = os.path.relpath(root, scan_dir)
                category = rel_root.split(os.sep, 1)[0] if rel_root and rel_root != '.' else ''

                item = scene.umodel_prop_scan_results.add()
                item.selected = False
                item.asset_name = asset_name or base
                item.asset_path = asset_path
                item.json_path = json_path
                item.mesh_path = os.path.join(root, filename)
                item.category = category
                item.category_root = os.path.join(scan_dir, category) if category else scan_dir
                found += 1

        try:
            scene.umodel_prop_category = '__ALL__'
        except Exception:
            pass

        self.report({'INFO'}, f"Prop scan complete: found {found} asset(s)")
        return {'FINISHED'}


class UMODEL_OT_clear_prop_scan_results(bpy.types.Operator):
    bl_idname = "umodel.clear_prop_scan_results"
    bl_label = "Clear Prop Scan Results"
    bl_description = "Clear the prop scan results list"

    @classmethod
    def poll(cls, _context):
        return _profile_feature_enabled("ENABLE_PROP_BUILDER")

    def execute(self, context):
        scene = context.scene
        scene.umodel_prop_scan_results.clear()
        scene.umodel_prop_scan_index = 0
        try:
            scene.umodel_prop_category = "__ALL__"
        except Exception:
            pass
        return {'FINISHED'}


class UMODEL_OT_import_prop_selected(map_importer.MapImporter, bpy.types.Operator):
    bl_idname = "umodel.import_prop_selected"
    bl_label = "Import Selected Props"
    bl_description = "Import selected props from the scanned directory"

    @classmethod
    def poll(cls, _context):
        return _profile_feature_enabled("ENABLE_PROP_BUILDER")

    def _get_or_create_collection(self, name: str) -> bpy.types.Collection:
        coll = bpy.data.collections.get(name)
        if coll is None:
            coll = bpy.data.collections.new(name)
            bpy.context.scene.collection.children.link(coll)
        return coll

    def _filtered_items(self, context):
        scene = context.scene
        category = str(getattr(scene, "umodel_prop_category", "__ALL__") or "__ALL__")
        needle = ""
        uilist = None
        try:
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    uilist = None
                    break
        except Exception:
            pass
        uilist_cls = getattr(bpy.types, "UMODELTOOLS_UL_prop_scan_results", None)
        try:
            needle = str(getattr(uilist_cls, "filter_name", "") or "").strip().lower()
        except Exception:
            needle = ""
        items = []
        for it in scene.umodel_prop_scan_results:
            if category not in {"", "__ALL__"} and str(getattr(it, "category", "")) != category:
                continue
            if needle and needle not in str(getattr(it, "asset_name", "")).lower():
                continue
            items.append(it)
        return items

    def _resolve_selected_items(self, context):
        scene = context.scene
        filtered = self._filtered_items(context)
        selected = [it for it in filtered if getattr(it, "selected", False)]
        if not selected and filtered:
            idx = int(scene.umodel_prop_scan_index)
            if 0 <= idx < len(scene.umodel_prop_scan_results):
                active = scene.umodel_prop_scan_results[idx]
                if active in filtered:
                    selected = [active]
        return selected

    def execute(self, context: bpy.types.Context) -> set[str]:
        profile = preferences.get_addon_preferences().get_active_profile()
        if profile is None:
            return self._op_message('ERROR', "You need to have an active game profile selected.")

        umodel_export_dir = _resolve_dir_path(profile.umodel_export_dir)
        if not umodel_export_dir or not os.path.isdir(umodel_export_dir):
            return self._op_message('ERROR', f"Invalid UModel export dir: {umodel_export_dir}")

        asset_dir = _resolve_dir_path(profile.asset_dir)
        if not asset_dir or not os.path.isdir(asset_dir):
            return self._op_message('ERROR', f"Invalid asset dir: {asset_dir}")

        scene = context.scene
        try:
            self.import_materials = bool(getattr(scene, 'umodel_import_materials', True))
        except Exception:
            self.import_materials = True
        try:
            self.apply_override_materials = self.import_materials and bool(getattr(scene, 'umodel_apply_override_materials', False))
        except Exception:
            self.apply_override_materials = False
        try:
            self.load_pbr_maps = self.import_materials and bool(getattr(scene, 'umodel_load_pbr_maps', True))
        except Exception:
            self.load_pbr_maps = True

        selected_items = self._resolve_selected_items(context)

        if not selected_items:
            return self._op_message('ERROR', "No props selected.")

        db = asset_db.AssetDB(asset_dir)
        built = 0
        touched_collections = set()

        with utils.std_out_err_redirect_tqdm() as orig_stdout:
            for item in tqdm.tqdm(selected_items,
                                  desc='Importing Props',
                                  file=orig_stdout,
                                  dynamic_ncols=True,
                                  ascii=True):
                asset_path = _derive_prop_import_asset_path(item, umodel_export_dir)
                obj = self._load_asset(
                    context=context,
                    asset_dir=asset_dir,
                    asset_path=asset_path,
                    umodel_export_dir=umodel_export_dir,
                    load=True,
                    db=db,
                    game_profile=profile.game
                )
                if obj is None:
                    self._warn_print(f"Warning: Failed to import prop asset {asset_path}")
                    continue

                inst_name = str(item.asset_name or Path(asset_path).stem)
                unique_name = inst_name
                if bpy.data.objects.get(unique_name) is not None:
                    i = 1
                    while bpy.data.objects.get(f"{inst_name}.{i:03d}") is not None:
                        i += 1
                    unique_name = f"{inst_name}.{i:03d}"

                instance = bpy.data.objects.new(unique_name, obj.data)
                instance.umodel_tools_asset.enabled = True
                instance.umodel_tools_asset.asset_path = asset_path
                instance["_umodel_mesh_object_path"] = asset_path
                instance["_umodel_prop_source_json"] = str(item.json_path)
                coll_name = str(getattr(item, "category", "") or "Props")
                import_collection = self._get_or_create_collection(coll_name)
                import_collection.objects.link(instance)
                touched_collections.add(import_collection.name)
                built += 1

        db.save_db()

        if self.import_materials:
            for collection_name in sorted(touched_collections):
                try:
                    self._post_import_reload_and_reapply(
                        collection_name=collection_name,
                        umodel_export_dir=umodel_export_dir,
                        asset_dir=asset_dir,
                        game_profile=profile.game,
                        apply_override_materials=self.apply_override_materials
                    )
                except Exception as e:
                    self._warn_print(f"[umodel_tools] Warning: Prop post-import material repair failed for {collection_name}: {e}")

        self._print_unrecognized_textures()
        return self._op_message('INFO', f"Imported {built}/{len(selected_items)} prop asset(s).")


class UMODEL_OT_import_prop_all(map_importer.MapImporter, bpy.types.Operator):
    bl_idname = "umodel.import_prop_all"
    bl_label = "Import All Props"
    bl_description = "Import all scanned props matching the current category/search filter"

    @classmethod
    def poll(cls, _context):
        return _profile_feature_enabled("ENABLE_PROP_BUILDER")

    def _get_or_create_collection(self, name: str) -> bpy.types.Collection:
        coll = bpy.data.collections.get(name)
        if coll is None:
            coll = bpy.data.collections.new(name)
            bpy.context.scene.collection.children.link(coll)
        return coll

    def _filtered_items(self, context):
        scene = context.scene
        category = str(getattr(scene, "umodel_prop_category", "__ALL__") or "__ALL__")
        needle = ""
        uilist_cls = getattr(bpy.types, "UMODELTOOLS_UL_prop_scan_results", None)
        try:
            needle = str(getattr(uilist_cls, "filter_name", "") or "").strip().lower()
        except Exception:
            needle = ""

        items = []
        for it in scene.umodel_prop_scan_results:
            if category not in {"", "__ALL__"} and str(getattr(it, "category", "")) != category:
                continue
            if needle and needle not in str(getattr(it, "asset_name", "")).lower():
                continue
            items.append(it)
        return items

    def execute(self, context: bpy.types.Context) -> set[str]:
        profile = preferences.get_addon_preferences().get_active_profile()
        if profile is None:
            return self._op_message('ERROR', "You need to have an active game profile selected.")

        umodel_export_dir = _resolve_dir_path(profile.umodel_export_dir)
        if not umodel_export_dir or not os.path.isdir(umodel_export_dir):
            return self._op_message('ERROR', f"Invalid UModel export dir: {umodel_export_dir}")

        asset_dir = _resolve_dir_path(profile.asset_dir)
        if not asset_dir or not os.path.isdir(asset_dir):
            return self._op_message('ERROR', f"Invalid asset dir: {asset_dir}")

        scene = context.scene
        try:
            self.import_materials = bool(getattr(scene, 'umodel_import_materials', True))
        except Exception:
            self.import_materials = True
        try:
            self.apply_override_materials = self.import_materials and bool(getattr(scene, 'umodel_apply_override_materials', False))
        except Exception:
            self.apply_override_materials = False
        try:
            self.load_pbr_maps = self.import_materials and bool(getattr(scene, 'umodel_load_pbr_maps', True))
        except Exception:
            self.load_pbr_maps = True

        selected_items = self._filtered_items(context)
        if not selected_items:
            return self._op_message('ERROR', "No props found in the current category/filter.")

        db = asset_db.AssetDB(asset_dir)
        built = 0
        touched_collections = set()

        with utils.std_out_err_redirect_tqdm() as orig_stdout:
            for item in tqdm.tqdm(selected_items,
                                  desc='Importing All Props',
                                  file=orig_stdout,
                                  dynamic_ncols=True,
                                  ascii=True):
                asset_path = _derive_prop_import_asset_path(item, umodel_export_dir)
                obj = self._load_asset(
                    context=context,
                    asset_dir=asset_dir,
                    asset_path=asset_path,
                    umodel_export_dir=umodel_export_dir,
                    load=True,
                    db=db,
                    game_profile=profile.game
                )
                if obj is None:
                    self._warn_print(f"Warning: Failed to import prop asset {asset_path}")
                    continue

                inst_name = str(item.asset_name or Path(asset_path).stem)
                unique_name = inst_name
                if bpy.data.objects.get(unique_name) is not None:
                    i = 1
                    while bpy.data.objects.get(f"{inst_name}.{i:03d}") is not None:
                        i += 1
                    unique_name = f"{inst_name}.{i:03d}"

                instance = bpy.data.objects.new(unique_name, obj.data)
                instance.umodel_tools_asset.enabled = True
                instance.umodel_tools_asset.asset_path = asset_path
                instance["_umodel_mesh_object_path"] = asset_path
                instance["_umodel_prop_source_json"] = str(item.json_path)
                coll_name = str(getattr(item, "category", "") or "Props")
                import_collection = self._get_or_create_collection(coll_name)
                import_collection.objects.link(instance)
                touched_collections.add(import_collection.name)
                built += 1

        db.save_db()

        if self.import_materials:
            for collection_name in sorted(touched_collections):
                try:
                    self._post_import_reload_and_reapply(
                        collection_name=collection_name,
                        umodel_export_dir=umodel_export_dir,
                        asset_dir=asset_dir,
                        game_profile=profile.game,
                        apply_override_materials=self.apply_override_materials
                    )
                except Exception as e:
                    self._warn_print(f"[umodel_tools] Warning: Prop post-import material repair failed for {collection_name}: {e}")

        self._print_unrecognized_textures()
        return self._op_message('INFO', f"Imported {built}/{len(selected_items)} prop asset(s).")

    def _resolve_selected_items(self, context):
        return self._filtered_items(context)


class UMODEL_OT_build_selected_materials(asset_importer.AssetImporter, bpy.types.Operator):
    bl_idname = "umodel.build_selected_materials"
    bl_label = "Build Materials"
    bl_description = "Build and assign materials for the selected objects using their mesh JSON files"
    bl_options = {'REGISTER', 'UNDO'}

    def _load_material_descriptor_direct(self, material_name: str, material_path_local_no_ext: str, umodel_export_dir: str):
        props_path = os.path.join(umodel_export_dir, material_path_local_no_ext) + '.props.txt'
        json_path = os.path.join(umodel_export_dir, material_path_local_no_ext) + '.json'

        if os.path.isfile(props_path):
            desc_ast, texture_infos, base_prop_overrides = props_txt_parser.parse_props_txt(props_path, mode='MATERIAL')
            if (not texture_infos) and os.path.isfile(json_path):
                desc_ast, texture_infos, base_prop_overrides = fmodel_json_parser.parse_fmodel_json(json_path, mode='MATERIAL')
        elif os.path.isfile(json_path):
            desc_ast, texture_infos, base_prop_overrides = fmodel_json_parser.parse_fmodel_json(json_path, mode='MATERIAL')
        else:
            raise FileNotFoundError(f'Could not find descriptor for material "{material_name}".')
        return desc_ast, texture_infos, base_prop_overrides

    def _get_or_create_local_material_from_descriptor(self, material_name: str, material_path_local_no_ext: str, umodel_export_dir: str, game_profile: str):
        game_profile_impl = game_profiles.GAME_HANDLERS.get(game_profile)
        if game_profile_impl is None:
            raise NotImplementedError(f"Requested game profile {game_profile} is not implemented/available.")

        desc_ast, texture_infos, base_prop_overrides = self._load_material_descriptor_direct(
            material_name=material_name,
            material_path_local_no_ext=material_path_local_no_ext,
            umodel_export_dir=umodel_export_dir,
        )

        local_material_name = f"{material_name}__MB"
        existing = bpy.data.materials.get(local_material_name)
        if existing is not None and getattr(existing, 'library', None) is None:
            new_mat = existing
            if new_mat.node_tree is None:
                new_mat.use_nodes = True
        else:
            new_mat = bpy.data.materials.new(local_material_name)
            new_mat.use_nodes = True

        new_mat["_umodel_material_builder"] = 1
        new_mat["_umodel_source_material_name"] = material_name
        new_mat["_umodel_source_material_path"] = material_path_local_no_ext
        new_mat.node_tree.links.clear()
        new_mat.node_tree.nodes.clear()
        game_profile_impl.process_material(mat=new_mat, desc_ast=desc_ast, use_pbr=self.load_pbr_maps)

        out = new_mat.node_tree.nodes.new('ShaderNodeOutputMaterial')
        if self.load_pbr_maps:
            special_blend_mode = None
            if base_prop_overrides is not None:
                if (blend_mode := base_prop_overrides.get('BlendMode')) is not None:
                    match blend_mode:
                        case 'BLEND_Opaque (0)':
                            pass
                        case 'BLEND_Masked (1)':
                            new_mat.blend_method = 'CLIP'
                        case 'BLEND_Translucent (2)':
                            new_mat.blend_method = 'BLEND'
                        case 'BLEND_Additive (3)':
                            special_blend_mode = enums.SpecialBlendingMode.Add
                            new_mat.blend_method = 'BLEND'
                        case 'BLEND_Modulate (4)':
                            special_blend_mode = enums.SpecialBlendingMode.Mod
                            new_mat.blend_method = 'BLEND'
                        case _:
                            self._warn_print(f"Warning: Unknown blending mode '{blend_mode}' found on importing material \"{material_name}\".")
                if self.import_backface_culling and (two_sided := base_prop_overrides.get('TwoSided')) is not None:
                    new_mat.use_backface_culling = not two_sided
                if (alpha_threshold := base_prop_overrides.get('OpacityMaskClipValue')) is not None:
                    new_mat.alpha_threshold = alpha_threshold
            elif self.import_backface_culling:
                new_mat.use_backface_culling = True

            bsdf = new_mat.node_tree.nodes.new('ShaderNodeBsdfPrincipled')
            ao_mix = new_mat.node_tree.nodes.new('ShaderNodeMix')
            ao_mix.data_type = 'RGBA'
            ao_mix.blend_type = 'MULTIPLY'
            ao_mix.inputs[6].default_value = (1, 1, 1, 1)
            ao_mix.inputs[7].default_value = (1, 1, 1, 1)
            new_mat.node_tree.links.new(ao_mix.outputs[2], bsdf.inputs['Base Color'])

            match special_blend_mode:
                case None:
                    new_mat.node_tree.links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
                case enums.SpecialBlendingMode.Add:
                    transparent_bsdf = new_mat.node_tree.nodes.new('ShaderNodeBsdfTransparent')
                    add_shader = new_mat.node_tree.nodes.new('ShaderNodeAddShader')
                    new_mat.node_tree.links.new(bsdf.outputs['BSDF'], add_shader.inputs[0])
                    new_mat.node_tree.links.new(transparent_bsdf.outputs['BSDF'], add_shader.inputs[1])
                    new_mat.node_tree.links.new(add_shader.outputs[0], out.inputs['Surface'])
                case enums.SpecialBlendingMode.Mod:
                    shader_to_rgb = new_mat.node_tree.nodes.new('ShaderNodeShaderToRGB')
                    transparent_bsdf = new_mat.node_tree.nodes.new('ShaderNodeBsdfTransparent')
                    new_mat.node_tree.links.new(bsdf.outputs['BSDF'], shader_to_rgb.inputs[0])
                    new_mat.node_tree.links.new(shader_to_rgb.outputs['Color'], transparent_bsdf.inputs['Color'])
                    new_mat.node_tree.links.new(transparent_bsdf.outputs['BSDF'], out.inputs['Surface'])
        else:
            bsdf = new_mat.node_tree.nodes.new('ShaderNodeBsdfDiffuse')
            ao_mix = None
            new_mat.node_tree.links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

        for tex_type, tex_path_and_name in texture_infos.items():
            tex_path_no_ext, tex_short_name = os.path.splitext(tex_path_and_name)
            if not self.load_pbr_maps and not game_profile_impl.is_diffuse_tex_type(tex_type, tex_short_name):
                continue
            if not game_profile_impl.do_process_texture(tex_type, tex_short_name):
                self._unrecognized_texture_types.add(tex_type)
                continue
            tex_path_no_ext = os.path.normpath(tex_path_no_ext)
            if tex_path_no_ext.startswith(os.sep):
                tex_path_no_ext = tex_path_no_ext[1:]
            tex_path = tex_path_no_ext + self.texture_format
            tex_path_abs = os.path.join(umodel_export_dir, tex_path)
            if not os.path.isfile(tex_path_abs):
                self._warn_print(f'Warning: Material "{material_name}" referenced texture "{tex_path}", but it does not exist in the UModel export path.')
                continue
            try:
                img = bpy.data.images.load(filepath=tex_path_abs, check_existing=True)
            except Exception as exc:
                self._warn_print(f'[umodel_tools] Failed loading texture for {material_name}: {tex_path_abs} ({exc})')
                continue
            img_node = new_mat.node_tree.nodes.new('ShaderNodeTexImage')
            img_node.image = img
            if self.load_pbr_maps:
                game_profile_impl.handle_material_texture_pbr(mat=new_mat, tex_type=tex_type, tex_short_name=tex_short_name, img_node=img_node, ao_mix_node=ao_mix, bsdf_node=bsdf, out_node=out)
            else:
                game_profile_impl.handle_material_texture_simple(mat=new_mat, tex_type=tex_type, tex_short_name=tex_short_name, img_node=img_node, bsdf_node=bsdf)

        game_profile_impl.end_process_material(new_mat)
        return new_mat

    @classmethod
    def poll(cls, context):
        return _profile_feature_enabled("ENABLE_MATERIAL_BUILDER")

    def _resolve_mesh_json_path(self, obj: bpy.types.Object, umodel_export_dir: str, json_index: dict[str, list[str]]) -> tuple[str | None, str | None]:
        stored_json = str(obj.get("_umodel_prop_source_json", "") or "")
        if stored_json and os.path.isfile(stored_json):
            return stored_json, _normalize_mesh_lookup_name(Path(stored_json).stem)

        for lookup_name in _candidate_mesh_lookup_names(obj):
            matches = json_index.get(lookup_name.lower(), [])
            if matches:
                if len(matches) > 1:
                    self._warn_print(
                        f"[umodel_tools] Multiple mesh JSON matches for {obj.name} ({lookup_name}); using {matches[0]}"
                    )
                return matches[0], lookup_name

        return None, None

    def execute(self, context):
        profile = preferences.get_addon_preferences().get_active_profile()
        if profile is None:
            return self._op_message('ERROR', "You need to have an active game profile selected.")

        selected_objects = [obj for obj in context.selected_objects if getattr(obj, 'type', None) == 'MESH']
        if not selected_objects:
            return self._op_message('ERROR', "Select one or more mesh objects first.")

        umodel_export_dir = _resolve_dir_path(profile.umodel_export_dir)
        if not umodel_export_dir:
            return self._op_message('ERROR', "You need to specify an Export Directory in the active profile.")
        if not os.path.isdir(umodel_export_dir):
            return self._op_message('ERROR', f"Path to export dir does not exist: {umodel_export_dir}")

        material_builder_root = _resolve_dir_path(getattr(context.scene, 'umodel_material_builder_root', ''))
        if material_builder_root:
            if not os.path.isdir(material_builder_root):
                return self._op_message('ERROR', f"Search Root Dir does not exist: {material_builder_root}")
            search_root = material_builder_root
        else:
            search_root = umodel_export_dir

        asset_dir = _resolve_dir_path(profile.asset_dir)
        if not asset_dir:
            return self._op_message('ERROR', "You need to specify an Asset Directory in the active profile.")
        if not os.path.isdir(asset_dir):
            return self._op_message('ERROR', f"Path to asset dir does not exist: {asset_dir}")

        try:
            self.load_pbr_maps = bool(getattr(context.scene, 'umodel_load_pbr_maps', True))
        except Exception:
            pass

        self._unrecognized_texture_types.clear()
        db = asset_db.AssetDB(asset_dir)
        json_index = _index_static_mesh_jsons(search_root)

        built_count = 0
        failed_count = 0

        context.window_manager.progress_begin(0, len(selected_objects))
        try:
            for i, obj in enumerate(selected_objects, start=1):
                context.window_manager.progress_update(i)

                json_path, lookup_name = self._resolve_mesh_json_path(obj, umodel_export_dir, json_index)
                if not json_path:
                    failed_count += 1
                    self._warn_print(f"[umodel_tools] Mesh JSON not found for selected object: {obj.name}")
                    continue

                try:
                    _json_obj, mat_descriptors_paths = fmodel_json_parser.parse_fmodel_json(json_path, mode='MESH')
                except Exception as exc:
                    failed_count += 1
                    self._warn_print(f"[umodel_tools] Failed parsing mesh JSON for {obj.name}: {json_path} ({exc})")
                    continue

                if not mat_descriptors_paths:
                    failed_count += 1
                    self._warn_print(f"[umodel_tools] No StaticMaterials found in mesh JSON for {obj.name}: {json_path}")
                    continue

                self._warn_print(
                    f"[MaterialBuilder] Object: {obj.name} | Mesh JSON: {json_path} | StaticMaterials found: {len(mat_descriptors_paths)}"
                )

                new_materials: list[bpy.types.Material] = []
                for slot_index, mat_desc_path in enumerate(mat_descriptors_paths):
                    material_path_local_no_ext, material_name = os.path.splitext(mat_desc_path)
                    material_name = material_name[1:]
                    material_object_path = os.path.normpath(material_path_local_no_ext)
                    if material_object_path.startswith(os.sep):
                        material_object_path = material_object_path.replace('\\', '/')

                    self._warn_print(
                        f"[MaterialBuilder]   Slot {slot_index}: descriptor={mat_desc_path} | material_name={material_name} | object_path={material_object_path}"
                    )

                    try:
                        mat = map_importer.override_ops.get_or_link_material_from_objectpath(
                            importer=self,
                            material_name=material_name,
                            material_object_path=material_object_path,
                            umodel_export_dir=umodel_export_dir,
                            asset_dir=asset_dir,
                            game_profile=profile.game,
                            db=db,
                        )
                    except Exception as exc:
                        self._warn_print(
                            f"[MaterialBuilder]   Slot {slot_index} FAILED building {material_name} for {obj.name}: {exc}"
                        )
                        mat = None

                    if mat is None:
                        placeholder_name = f"{material_name}_Placeholder"
                        self._warn_print(
                            f"[MaterialBuilder]   Slot {slot_index} using placeholder material: {placeholder_name}"
                        )
                        mat = bpy.data.materials.get(placeholder_name) or bpy.data.materials.new(placeholder_name)
                    else:
                        try:
                            node_count = len(mat.node_tree.nodes) if getattr(mat, 'node_tree', None) else 0
                        except Exception:
                            node_count = -1
                        self._warn_print(
                            f"[MaterialBuilder]   Slot {slot_index} built OK: {mat.name} | nodes={node_count}"
                        )
                    new_materials.append(mat)

                if obj.data is None:
                    failed_count += 1
                    self._warn_print(f"[umodel_tools] Selected object has no mesh data: {obj.name}")
                    continue

                existing_slot_count = len(obj.material_slots)
                self._warn_print(
                    f"[MaterialBuilder] Assigning {len(new_materials)} material(s) to {obj.name} | existing_slots={existing_slot_count}"
                )

                while len(obj.data.materials) < len(new_materials):
                    obj.data.materials.append(None)

                for slot_index, mat in enumerate(new_materials):
                    obj.material_slots[slot_index].material = mat
                    self._warn_print(
                        f"[MaterialBuilder]   Assigned slot {slot_index} -> {mat.name if mat else 'None'}"
                    )

                try:
                    obj["_umodel_prop_source_json"] = json_path
                    obj["_umodel_material_builder_source"] = lookup_name or Path(json_path).stem
                except Exception:
                    pass

                built_count += 1

            db.save_db()
        finally:
            context.window_manager.progress_end()

        self._print_unrecognized_textures()

        if failed_count:
            msg = f"Built materials for {built_count}/{len(selected_objects)} selected object(s). Check console for warnings."
            return self._op_message('WARNING', msg)

        return self._op_message('INFO', f"Built materials for {built_count} selected object(s).")

class UMODEL_OT_bake_tints_to_attr(bpy.types.Operator):
    bl_idname = "umodel.bake_tints_to_attr"
    bl_label = "Bake Tints to attr"
    bl_description = "Bake palette tints to the BakedTint color attribute, convert palette materials to use it, then merge duplicate materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            processed_objects, converted_materials = bake_tints_to_attr_on_selected(context)
            self.report(
                {'INFO'},
                f"Baked tints for {processed_objects} object(s), converted {converted_materials} material(s)"
            )
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, f"Bake Tints to attr failed: {exc}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}


def _get_active_bound_parent_name(scene) -> str:
    bound = utils.get_active_import_bound(scene)
    if bound is None:
        return ""
    return str(getattr(bound, "name", "") or "").strip()


def _bounds_phase_parent_name(scene, phase_index: int) -> str:
    base = _get_active_bound_parent_name(scene) or "bound"
    return f"{base}_{phase_index}"


def _hide_collection_if_exists(name: str) -> None:
    col = bpy.data.collections.get(name)
    if col is None:
        return
    try:
        col.hide_viewport = True
    except Exception:
        pass
    try:
        col.hide_render = True
    except Exception:
        pass


class UMODEL_OT_import_scanned_umap_selected(map_importer.MapImporter, bpy.types.Operator):
    bl_idname = "umodel.import_scanned_umap_selected"
    bl_label = "Import Selected Scanned UMAP"
    bl_description = "Import the selected UMAP from the scan results using the same importer logic"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene

        if not utils.apply_active_import_bound_to_scene(scene):
            self.report({'ERROR'}, "Create and select an import bound first")
            return {'CANCELLED'}

        include_bpps = bool(getattr(scene, "umodel_import_bounds_only_bpps", False))
        self._bounds_parent_collection_name = _get_active_bound_parent_name(scene)

        # Apply general scene import options
        try:
            self.import_materials = bool(getattr(scene, 'umodel_import_materials', True))
        except Exception:
            self.import_materials = True
        try:
            self.apply_override_materials = self.import_materials and bool(getattr(scene, 'umodel_apply_override_materials', False))
        except Exception:
            self.apply_override_materials = False
        try:
            self.load_pbr_maps = self.import_materials and bool(getattr(scene, 'umodel_load_pbr_maps', True))
        except Exception:
            self.load_pbr_maps = False if not getattr(self, 'import_materials', True) else True

        if not hasattr(scene, "umodel_umap_scan_results") or len(scene.umodel_umap_scan_results) == 0:
            self.report({'ERROR'}, "No scan results to import.")
            return {'CANCELLED'}

        idx = int(scene.umodel_umap_scan_index)
        if idx < 0 or idx >= len(scene.umodel_umap_scan_results):
            self.report({'ERROR'}, "No scan result selected.")
            return {'CANCELLED'}

        item = scene.umodel_umap_scan_results[idx]
        map_path = item.map_path

        profile = preferences.get_addon_preferences().get_active_profile()
        if profile is None:
            return self._op_message('ERROR', "You need to have an active game profile selected.")

        umodel_export_dir: str = _resolve_dir_path(profile.umodel_export_dir)

        if not umodel_export_dir:
            return self._op_message('ERROR', "You need to specify a UModel export dir in Scene properties.")
        if not os.path.isdir(umodel_export_dir):
            return self._op_message('ERROR', f"Path to UModel export dir {umodel_export_dir} does not exist.")

        asset_dir: str = _resolve_dir_path(profile.asset_dir)

        if not asset_dir:
            return self._op_message('ERROR', "You need to specify an asset dir in Scene properties.")
        if not os.path.isdir(asset_dir):
            return self._op_message('ERROR', f"Path to asset dir {asset_dir} does not exist.")

        if not os.path.isfile(map_path):
            return self._op_message('ERROR', f"Map file not found: {map_path}")

        self._unrecognized_texture_types.clear()

        db = asset_db.AssetDB(asset_dir)

        # Import exactly one map, and optionally include placed BPPs too.
        try:
            scene.umodel_use_vertex_bounds = True
            # Bounds import session-wide instance de-dupe (MindsEye has cross-UMAP duplicates)
            # This is only used by the 'Import UMAPs with bounds' workflow.
            self._bounds_dedupe_enabled = True
            self._bounds_seen_keys = set()

            ok = self._import_map(
                context=context,
                map_path=map_path,
                umodel_export_dir=umodel_export_dir,
                asset_dir=asset_dir,
                game_profile=profile.game,
                db=db,
                map_index=1,
                map_total=1
            )

            if include_bpps:
                bpp_ok = self._import_bpps_from_map(
                    context=context,
                    map_path=map_path,
                    umodel_export_dir=umodel_export_dir,
                    asset_dir=asset_dir,
                    game_profile=profile.game,
                    db=db,
                    map_index=1,
                    map_total=1
                )
                ok = bool(ok or bpp_ok)
        finally:
            # Clear bounds de-dupe state for this operator session
            try:
                self._bounds_dedupe_enabled = False
                self._bounds_seen_keys = set()
            except Exception:
                pass
            self._bounds_parent_collection_name = ""
            scene.umodel_use_vertex_bounds = False

        db.save_db()

        self._print_unrecognized_textures()

        if self._has_warnings:
            self._op_message('WARNING', "Map import had warnings. Check console for details.")

        return {'FINISHED'} if ok else {'CANCELLED'}

class UMODEL_OT_import_scanned_umap_all(map_importer.MapImporter, bpy.types.Operator):
    bl_idname = "umodel.import_scanned_umap_all"
    bl_label = "Import All Scanned UMAPs"
    bl_description = "Import all UMAPs from scan results using the same importer logic"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene

        if not utils.apply_active_import_bound_to_scene(scene):
            self.report({'ERROR'}, "Create and select an import bound first")
            return {'CANCELLED'}

        # Apply general scene import options
        try:
            self.import_materials = bool(getattr(scene, 'umodel_import_materials', True))
        except Exception:
            self.import_materials = True
        try:
            self.apply_override_materials = self.import_materials and bool(getattr(scene, 'umodel_apply_override_materials', False))
        except Exception:
            self.apply_override_materials = False
        try:
            self.load_pbr_maps = self.import_materials and bool(getattr(scene, 'umodel_load_pbr_maps', True))
        except Exception:
            self.load_pbr_maps = False if not getattr(self, 'import_materials', True) else True

        include_bpps = bool(getattr(scene, "umodel_import_bounds_only_bpps", False))

        if not hasattr(scene, "umodel_umap_scan_results") or len(scene.umodel_umap_scan_results) == 0:
            self.report({'ERROR'}, "No scan results to import.")
            return {'CANCELLED'}

        profile = preferences.get_addon_preferences().get_active_profile()
        if profile is None:
            return self._op_message('ERROR', "You need to have an active game profile selected.")

        umodel_export_dir: str = _resolve_dir_path(profile.umodel_export_dir)

        if not umodel_export_dir:
            return self._op_message('ERROR', "You need to specify a UModel export dir in Scene properties.")
        if not os.path.isdir(umodel_export_dir):
            return self._op_message('ERROR', f"Path to UModel export dir {umodel_export_dir} does not exist.")

        asset_dir: str = _resolve_dir_path(profile.asset_dir)

        if not asset_dir:
            return self._op_message('ERROR', "You need to specify an asset dir in Scene properties.")
        if not os.path.isdir(asset_dir):
            return self._op_message('ERROR', f"Path to asset dir {asset_dir} does not exist.")

        self._unrecognized_texture_types.clear()

        items = list(scene.umodel_umap_scan_results)
        total = len(items)
        db = asset_db.AssetDB(asset_dir)
        phase_enabled = bool(getattr(scene, "umodel_bounds_phased_import", False))
        phase_size = max(1, int(getattr(scene, "umodel_bounds_phase_size", 100) or 100))

        context.window_manager.progress_begin(0, total)

        prefs_edit = context.preferences.edit
        old_global_undo = bool(getattr(prefs_edit, "use_global_undo", True))
        old_lock_interface = bool(getattr(scene.render, "use_lock_interface", False))

        try:
            try:
                prefs_edit.use_global_undo = False
            except Exception:
                pass
            try:
                scene.render.use_lock_interface = True
            except Exception:
                pass

            scene.umodel_use_vertex_bounds = True
            # Bounds import session-wide instance de-dupe (MindsEye has cross-UMAP duplicates)
            self._bounds_dedupe_enabled = True
            self._bounds_seen_keys = set()

            imported = 0
            total_phases = max(1, math.ceil(total / phase_size)) if phase_enabled else 1

            for phase_idx, start_idx in enumerate(range(0, total, phase_size if phase_enabled else total), start=1):
                phase_items = items[start_idx:start_idx + (phase_size if phase_enabled else total)]
                phase_parent_name = _bounds_phase_parent_name(scene, phase_idx) if phase_enabled else _get_active_bound_parent_name(scene)
                self._bounds_parent_collection_name = phase_parent_name

                if phase_enabled:
                    phase_start = start_idx + 1
                    phase_end = start_idx + len(phase_items)
                    print(f"[PHASE {phase_idx}/{total_phases}] Importing UMAPs {phase_start}-{phase_end} of {total} into {phase_parent_name}")
                    self.report({'INFO'}, f"Phase {phase_idx}/{total_phases}: importing {phase_start}-{phase_end} into {phase_parent_name}")

                for local_i, item in enumerate(phase_items, start=1):
                    i = start_idx + local_i
                    map_path = item.map_path
                    percent = (i / total) * 100.0

                    print(f"[UMAP IMPORT] {i}/{total} ({percent:.1f}%) - {os.path.basename(map_path)}")
                    if i == 1 or i % 5 == 0 or i == total:
                        self.report({'INFO'}, f"Importing scanned UMAPs: {i}/{total} ({percent:.1f}%) B:{scene.umodel_use_vertex_bounds}")

                    context.window_manager.progress_update(i)

                    if not os.path.isfile(map_path):
                        continue

                    ok = self._import_map(
                        context=context,
                        map_path=map_path,
                        umodel_export_dir=umodel_export_dir,
                        asset_dir=asset_dir,
                        game_profile=profile.game,
                        db=db,
                        map_index=i,
                        map_total=total
                    )

                    if include_bpps:
                        bpp_ok = self._import_bpps_from_map(
                            context=context,
                            map_path=map_path,
                            umodel_export_dir=umodel_export_dir,
                            asset_dir=asset_dir,
                            game_profile=profile.game,
                            db=db,
                            map_index=i,
                            map_total=total
                        )
                        ok = bool(ok or bpp_ok)

                    if ok:
                        imported += 1

                if phase_enabled:
                    _hide_collection_if_exists(phase_parent_name)
                    try:
                        gc.collect()
                    except Exception:
                        pass
                    try:
                        context.view_layer.update()
                    except Exception:
                        pass
                    print(f"[PHASE {phase_idx}/{total_phases}] Complete. Hid collection '{phase_parent_name}' and refreshed scene state.")
                    self.report({'INFO'}, f"Phase {phase_idx}/{total_phases} complete: hid {phase_parent_name}")

        finally:
            context.window_manager.progress_end()
            # Clear bounds de-dupe state for this operator session
            try:
                self._bounds_dedupe_enabled = False
                self._bounds_seen_keys = set()
            except Exception:
                pass
            self._bounds_parent_collection_name = ""
            scene.umodel_use_vertex_bounds = False
            try:
                prefs_edit.use_global_undo = old_global_undo
            except Exception:
                pass
            try:
                scene.render.use_lock_interface = old_lock_interface
            except Exception:
                pass

        db.save_db()

        self._print_unrecognized_textures()

        if self._has_warnings:
            self._op_message('WARNING', "Map import had warnings. Check console for details.")

        self.report({'INFO'}, f"Imported {imported}/{total} scanned maps.")
        return {'FINISHED'}

class UMODEL_OT_build_landscape_materials(bpy.types.Operator):
    bl_idname = "umodel.build_landscape_materials"
    bl_label = "Build Landscape Materials"
    bl_description = "Build landscape materials for selected landscape meshes"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            compiler = _load_landscape_compiler_module()
            return compiler.compile_selected_landscapes(context, report_cb=self.report)
        except Exception as exc:
            self.report({'ERROR'}, f"Landscape material build failed: {exc}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
