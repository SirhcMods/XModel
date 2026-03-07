
import os
import typing as t

import numpy as np
import tqdm
import tqdm.contrib
import bpy
import bpy_extras.io_utils
import json
from pathlib import Path
import mathutils as mu

from . import utils
from . import asset_importer
from . import asset_db
from . import map_importer
from . import preferences

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

        if imported_any:
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
            # Prefer unified General setting; fall back to legacy BPP-only toggle if present
            self.apply_override_materials = bool(getattr(scene, "umodel_apply_override_materials", getattr(scene, "umodel_bpp_apply_override_materials", False)))
        except Exception:
            # If anything goes wrong, fall back to not applying overrides.
            self.apply_override_materials = False

            try:
                self.load_pbr_maps = bool(getattr(scene, "umodel_load_pbr_maps", True))
            except Exception:
                pass

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
            self.apply_override_materials = bool(getattr(scene, 'umodel_apply_override_materials', False))
        except Exception:
            self.apply_override_materials = False
        try:
            self.load_pbr_maps = bool(getattr(scene, 'umodel_load_pbr_maps', True))
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
            self.apply_override_materials = bool(getattr(scene, 'umodel_apply_override_materials', False))
        except Exception:
            self.apply_override_materials = False
        try:
            self.load_pbr_maps = bool(getattr(scene, 'umodel_load_pbr_maps', True))
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


class UMODEL_OT_import_scanned_umap_selected(map_importer.MapImporter, bpy.types.Operator):
    bl_idname = "umodel.import_scanned_umap_selected"
    bl_label = "Import Selected Scanned UMAP"
    bl_description = "Import the selected UMAP from the scan results using the same importer logic"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene

        only_bpps = bool(getattr(scene, "umodel_import_bounds_only_bpps", False))

        # Apply general scene import options
        try:
            self.apply_override_materials = bool(getattr(scene, 'umodel_apply_override_materials', False))
        except Exception:
            pass
        try:
            self.load_pbr_maps = bool(getattr(scene, 'umodel_load_pbr_maps', True))
        except Exception:
            pass

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

        # Import exactly one map. If BPP-only mode is enabled, only build placed BPPs.
        try:
            scene.umodel_use_vertex_bounds = True
            if only_bpps:
                ok = self._import_bpps_from_map(
                    context=context,
                    map_path=map_path,
                    umodel_export_dir=umodel_export_dir,
                    asset_dir=asset_dir,
                    game_profile=profile.game,
                    db=db,
                    map_index=1,
                    map_total=1
                )
            else:
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
        finally:
            # Clear bounds de-dupe state for this operator session
            try:
                self._bounds_dedupe_enabled = False
                self._bounds_seen_keys = set()
            except Exception:
                pass
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

        # Apply general scene import options
        try:
            self.apply_override_materials = bool(getattr(scene, 'umodel_apply_override_materials', False))
        except Exception:
            pass
        try:
            self.load_pbr_maps = bool(getattr(scene, 'umodel_load_pbr_maps', True))
        except Exception:
            pass

        only_bpps = bool(getattr(scene, "umodel_import_bounds_only_bpps", False))

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

        total = len(scene.umodel_umap_scan_results)
        db = asset_db.AssetDB(asset_dir)

        context.window_manager.progress_begin(0, total)

        try:
            scene.umodel_use_vertex_bounds = True
            # Bounds import session-wide instance de-dupe (MindsEye has cross-UMAP duplicates)
            # This is only used by the 'Import UMAPs with bounds' workflow.
            self._bounds_dedupe_enabled = True
            self._bounds_seen_keys = set()

            imported = 0
            for i, item in enumerate(scene.umodel_umap_scan_results, start=1):
                map_path = item.map_path
                percent = (i / total) * 100.0

                print(f"[UMAP IMPORT] {i}/{total} ({percent:.1f}%) - {os.path.basename(map_path)}")
                if i == 1 or i % 5 == 0 or i == total:
                    self.report({'INFO'}, f"Importing scanned UMAPs: {i}/{total} ({percent:.1f}%) B:{scene.umodel_use_vertex_bounds}")

                context.window_manager.progress_update(i)

                if not os.path.isfile(map_path):
                    continue


                if only_bpps:
                    ok = self._import_bpps_from_map(
                        context=context,
                        map_path=map_path,
                        umodel_export_dir=umodel_export_dir,
                        asset_dir=asset_dir,
                        game_profile=profile.game,
                        db=db,
                        map_index=i,
                        map_total=total
                    )
                else:
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
			
                if ok:
                    imported += 1

            context.window_manager.progress_end()
        finally:
            # Clear bounds de-dupe state for this operator session
            try:
                self._bounds_dedupe_enabled = False
                self._bounds_seen_keys = set()
            except Exception:
                pass
            scene.umodel_use_vertex_bounds = False

        db.save_db()

        self._print_unrecognized_textures()

        if self._has_warnings:
            self._op_message('WARNING', "Map import had warnings. Check console for details.")

        self.report({'INFO'}, f"Imported {imported}/{total} scanned maps.")
        return {'FINISHED'}
