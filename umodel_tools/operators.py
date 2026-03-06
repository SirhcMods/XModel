
import os
import typing as t

import numpy as np
import tqdm
import tqdm.contrib
import bpy
import bpy_extras.io_utils
import json
import mathutils as mu

from . import utils
from . import asset_importer
from . import asset_db
from . import map_importer
from . import preferences

from .utils import get_selected_vertex_world_bounds


def _resolve_dir_path(value: str) -> str:
    """Resolve Blender-style paths (including //) to a normalized absolute OS path."""
    if not value:
        return ""
    value = bpy.path.abspath(value)
    value = os.path.normpath(os.path.abspath(value))
    # Blender on Windows can yield paths like "\\C:\\..." or "/C:/..."; strip that leading slash.
    if os.name == 'nt' and len(value) >= 3 and value[0] in ('/', '\\') and value[1].isalpha() and value[2] == ':':
        value = value[1:]
    return value


def _get_object_aabb_verts(obj: bpy.types.Object) -> list[tuple[float, float, float]]:
    return [obj.matrix_world @ mu.Vector(corner) for corner in obj.bound_box]


class UMODELTOOLS_OT_recover_unreal_asset(asset_importer.AssetImporter, bpy.types.Operator):
    bl_idname = "umodel_tools.recover_unreal_asset"
    bl_label = "Recover Unreal Asset"
    bl_description = "Replaces selected object with an Unreal Engine asset from UModel dir, or attempts " \
                     "to transfer data to it, such as UV maps and materials"
    bl_options = {'REGISTER', 'UNDO'}

    asset_path: bpy.props.StringProperty(
        name="Asset path",
        description="Path to an alleged asset within the game"
    )

    def invoke(self, context: bpy.types.Context, _: bpy.types.Event) -> set[int] | set[str]:
        wm: bpy.types.WindowManager = context.window_manager

        return wm.invoke_props_dialog(self)

    def execute(self, context: bpy.types.Context) -> set[str]:
        self._unrecognized_texture_types.clear()

        if not self.asset_path:
            return self._op_message('ERROR', "Asset path was not provided.")

        selected_objects: t.Sequence[selected_objects] = context.selected_objects

        # Apply general scene import options
        scene = context.scene
        try:
            self.apply_override_materials = bool(getattr(scene, 'umodel_apply_override_materials', False))
        except Exception:
            pass
        try:
            self.load_pbr_maps = bool(getattr(scene, 'umodel_load_pbr_maps', True))
        except Exception:
            pass

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

        asset_path = os.path.normpath(self.asset_path)
        asset_path = asset_path[1:] if asset_path.startswith(os.sep) else asset_path
        asset = self._load_asset(context=context, asset_dir=asset_dir, asset_path=asset_path,
                                 umodel_export_dir=umodel_export_dir, game_profile=profile.game)

        if asset is None:
            self._op_message('ERROR', "Failed to import asset.")
            return {'CANCELLED'}

        asset_mesh = asset.data

        # attempt replacing selected object with an asset
        if context.selected_objects:
            for obj in context.selected_objects:

                if utils.compare_meshes(asset_mesh, obj.data):
                    vtx_source = np.array([v.co for v in asset_mesh.vertices])
                    vtx_target = np.array([obj.matrix_world @ v.co for v in obj.data.vertices])
                else:
                    vtx_source = np.array(_get_object_aabb_verts(asset))
                    vtx_target = np.array(_get_object_aabb_verts(obj))

                pad = lambda x: np.hstack([x, np.ones((x.shape[0], 1))])
                X = pad(vtx_source)
                Y = pad(vtx_target)

                A, _, _, _ = np.linalg.lstsq(X, Y, rcond=1)

                obj.hide_set(True)

                new_obj = bpy.data.objects.new(name=f"{obj.name}_Replaced", object_data=asset_mesh)
                new_obj.matrix_world = A
                new_obj.umodel_tools_asset.enabled = True
                new_obj.umodel_tools_asset.asset_path = self.asset_path

                context.collection.objects.link(new_obj)

        # import the asset as a new object
        else:
            new_obj = bpy.data.objects.new(name=f"{asset.name}_Instance", object_data=asset_mesh)
            new_obj.umodel_tools_asset.enabled = True
            new_obj.umodel_tools_asset.asset_path = self.asset_path
            new_obj.location = context.scene.cursor.location
            new_obj.scale = (5, 5, 5)
            context.collection.objects.link(new_obj)
            new_obj.select_set(True)

        self._print_unrecognized_textures()

        if self._has_warnings:
            self._op_message('WARNING', "Asset import had warnnings. Check console for details.")

        return {'FINISHED'}


class UMODELTOOLS_OT_import_unreal_assets(asset_importer.AssetImporter, bpy.types.Operator):
    bl_idname = "umodel_tools.import_unreal_assets"
    bl_label = "Import Unreal Assets"
    bl_description = "Imports a subdirectory of assets to the specified asset directory"
    bl_options = {'REGISTER', 'UNDO'}

    asset_sub_dir: bpy.props.StringProperty(
        name="Asset subdir",
        description="Path to a subdirectory containing assets"
    )

    def invoke(self, context: bpy.types.Context, _: bpy.types.Event) -> set[int] | set[str]:
        wm: bpy.types.WindowManager = context.window_manager

        return wm.invoke_props_dialog(self)

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not self.asset_sub_dir:
            return self._op_message('ERROR', "Asset path was not provided.")

        self._unrecognized_texture_types.clear()

        selected_objects: t.Sequence[selected_objects] = context.selected_objects

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

        asset_sub_dir = os.path.normpath(self.asset_sub_dir)
        asset_sub_dir = asset_sub_dir[1:] if asset_sub_dir.startswith(os.sep) else asset_sub_dir
        asset_sub_dir_abs = os.path.join(umodel_export_dir, asset_sub_dir)

        if not os.path.isdir(asset_sub_dir_abs):
            return self._op_message('ERROR', f"Path {asset_sub_dir_abs} does not exist.")

        # count assets to be imported for progress bar display purposes
        total_models = 0
        for root, _, files in os.walk(asset_sub_dir_abs):
            for file in files:
                _, ext = os.path.splitext(file)
                if ext not in {'.psk', '.pskx'}:
                    continue

                total_models += 1

        db = asset_db.AssetDB(asset_dir)
        with utils.std_out_err_redirect_tqdm() as orig_stdout:
            with tqdm.tqdm(total=total_models, file=orig_stdout, dynamic_ncols=True, ascii=True,
                           desc="Importing assets") as progress_bar:
                for root, _, files in os.walk(asset_sub_dir_abs):
                    for file in files:
                        file_base, ext = os.path.splitext(file)
                        if ext not in {'.psk', '.pskx'}:
                            continue

                        file_abs = os.path.join(root, file_base) + '.uasset'
                        file_rel = os.path.relpath(file_abs, umodel_export_dir)

                        print(f"\n\nImporting asset {file_rel}...")
                        self._load_asset(context=context,
                                         asset_dir=asset_dir,
                                         asset_path=file_rel,
                                         umodel_export_dir=umodel_export_dir,
                                         load=False,
                                         db=db,
                                         game_profile=profile.game)

                        progress_bar.update(1)

        db.save_db()

        self._print_unrecognized_textures()

        if self._has_warnings:
            self._op_message('WARNING', "Asset import had warnnings. Check console for details.")

        return {'FINISHED'}


class UMODELTOOLS_OT_import_unreal_map(map_importer.MapImporter, bpy.types.Operator, bpy_extras.io_utils.ImportHelper):
    bl_idname = "umodel_tools.import_unreal_map"
    bl_label = "Import Unreal Map"
    bl_description = "Imports an Unreal Engine 4 map (.umap -> FModel .json)"
    bl_options = {'REGISTER', 'UNDO'}

    # ImportHelper

    filename_ext = ".json"

    filter_glob: bpy.props.StringProperty(
        default="*.json",
        options={'HIDDEN'},
        maxlen=255
    )

    files: bpy.props.CollectionProperty(
        name="Unreal Engine 4 map (FModel .json)",
        type=bpy.types.OperatorFileListElement,
    )

    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    # end ImportHelper

    def execute(self, context: bpy.types.Context) -> set[str]:
        self._unrecognized_texture_types.clear()
        selected_objects: t.Sequence[selected_objects] = context.selected_objects

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

        db = asset_db.AssetDB(asset_dir)

        total_files = len(self.files)

        for i, file in enumerate(self.files, start=1):
            self._import_map(
            context=context, 
            umodel_export_dir=umodel_export_dir, 
            asset_dir=asset_dir, 
            db=db,
            map_path=os.path.join(self.directory, file.name),
            game_profile=profile.game,
            map_index=i,
            map_total=total_files
        )

        db.save_db()

        self._print_unrecognized_textures()

        if self._has_warnings:
            self._op_message('WARNING', "Asset import had warnnings. Check console for details.")

        return {'FINISHED'}


class UMODELTOOLS_OT_realign_asset(bpy.types.Operator):
    bl_idname = "umodel_tools.realign_asset"
    bl_label = "Realign Unreal Asset"
    bl_description = "Attempt realigning the asset with a selected object boundary"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context: bpy.types.Context) -> set[str]:

        if not len(context.selected_objects) == 2:
            self.report({'ERROR'}, "Exactly 2 objects must be selected.")
            return {'CANCELLED'}

        asset_idx = None
        for i, obj in enumerate(context.selected_objects):
            if obj.umodel_tools_asset.enabled:
                asset_idx = i
                break

        if asset_idx is None:
            self.report({'ERROR'}, "One of the objects must be an Unreal asset.")
            return {'CANCELLED'}

        asset_obj = context.selected_objects[asset_idx]
        target_obj = context.selected_objects[int(not asset_idx)]

        bpy.ops.object.select_all(action='DESELECT')

        asset_obj_copy = utils.copy_object(asset_obj)
        target_obj_copy = utils.copy_object(target_obj)

        context.collection.objects.link(asset_obj_copy)
        context.collection.objects.link(target_obj_copy)

        asset_obj_copy.select_set(True)
        target_obj_copy.select_set(True)

        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

        vtx_source = np.array(_get_object_aabb_verts(asset_obj_copy))
        vtx_target = np.array(_get_object_aabb_verts(target_obj_copy))

        pad = lambda x: np.hstack([x, np.ones((x.shape[0], 1))])
        unpad = lambda x: x[:, :-1]
        X = pad(vtx_source)
        Y = pad(vtx_target)

        A, _, _, _ = np.linalg.lstsq(X, Y, rcond=1)

        transform = lambda x: unpad(pad(x) @ A)
        transformed_verts = transform(np.array([v.co for v in asset_obj_copy.data.vertices]))
        vtx_source_local = np.array([v.co for v in asset_obj.data.vertices])

        X = pad(vtx_source_local)
        Y = pad(transformed_verts)

        A, _, _, _ = np.linalg.lstsq(X, Y, rcond=1)

        target_obj.hide_set(True)
        asset_obj.matrix_world = A
        asset_obj.select_set(True)

        bpy.data.objects.remove(asset_obj_copy, do_unlink=True)
        bpy.data.objects.remove(target_obj_copy, do_unlink=True)

        return {'FINISHED'}

class UMODEL_OT_calculate_import_bounds(bpy.types.Operator):
    bl_idname = "umodel.calculate_import_bounds"
    bl_label = "Calculate Import Bounds"
    bl_description = "Calculate world-space bounds from selected vertices"

    def execute(self, context):
        bounds = get_selected_vertex_world_bounds()

        if not bounds:
            self.report({'ERROR'}, "Select mesh vertices first")
            return {'CANCELLED'}

        scene = context.scene
        scene.umodel_min_x = bounds["min_x"]
        scene.umodel_max_x = bounds["max_x"]
        scene.umodel_min_y = bounds["min_y"]
        scene.umodel_max_y = bounds["max_y"]

        self.report({'INFO'}, "Import bounds calculated from selection")
        return {'FINISHED'}

class UMODEL_OT_scan_umap_bounds(bpy.types.Operator):
    bl_idname = "umodel.scan_umap_bounds"
    bl_label = "Scan UMAPs for Bounds"
    bl_description = "Scan a directory for exported .umap .json files and list maps that intersect the current import bounds"

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

        if not hasattr(scene, "umodel_umap_scan_results"):
            self.report({'ERROR'}, "UMAP scan results property not registered. Re-enable addon or restart Blender.")
            return {'CANCELLED'}

        scan_dir = bpy.path.abspath(scene.umodel_umap_scan_dir) if scene.umodel_umap_scan_dir else ""
        if not scan_dir:
            self.report({'ERROR'}, "Set a UMAP JSON Directory first")
            return {'CANCELLED'}

        if not os.path.isdir(scan_dir):
            self.report({'ERROR'}, f"Directory not found: {scan_dir}")
            return {'CANCELLED'}

        # Clear existing results
        scene.umodel_umap_scan_results.clear()
        scene.umodel_umap_scan_index = 0

        # Lazy imports to avoid circular import issues / heavy import cost
        from .map_importer import StaticMesh, is_within_import_bounds  # pylint: disable=import-outside-toplevel
        from mathutils import Vector  # pylint: disable=import-outside-toplevel

        # Pre-count json files for progress reporting
        json_files = []
        for root, _dirs, files in os.walk(scan_dir):
            for filename in files:
                if filename.lower().endswith(".json"):
                    json_files.append(os.path.join(root, filename))

        total = len(json_files)
        if total == 0:
            self.report({'WARNING'}, "No .json files found in directory")
            return {'CANCELLED'}

        context.window_manager.progress_begin(0, total)

        matches = 0

        try:
            scene.umodel_use_vertex_bounds = True
            for idx, json_path in enumerate(json_files, start=1):
                percent = (idx / total) * 100.0

                # Console logging (cheap, safe)
                print(f"[UMAP SCAN] {idx}/{total} ({percent:.1f}%) - {os.path.basename(json_path)}")

                # Occasional status bar update (don’t spam)
                if idx == 1 or idx % 10 == 0 or idx == total:
                    self.report({'INFO'}, f"Scanning UMAPs: {idx}/{total} ({percent:.1f}%) B:{scene.umodel_use_vertex_bounds}")

                context.window_manager.progress_update(idx)

                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        json_obj = json.load(f)
                except Exception:
                    continue

                if not isinstance(json_obj, list):
                    continue

                # If any matching entity is in bounds, record the map once.
                if only_bpps:
                    for entity in json_obj:
                        try:
                            if entity.get("Type") != "LevelInstanceComponent":
                                continue
                            if entity.get("Name") != "Root":
                                continue

                            outer = entity.get("Outer", "") or ""
                            if not outer.startswith("BPP_"):
                                continue

                            props = entity.get("Properties") or {}
                            loc = props.get("RelativeLocation") or {}
                            # Convert UE cm -> Blender meters and flip Y to match existing importer.
                            pos = Vector((
                                float(loc.get("X", 0.0)) / 100.0,
                                float(loc.get("Y", 0.0)) / -100.0,
                                float(loc.get("Z", 0.0)) / 100.0,
                            ))

                            if is_within_import_bounds(pos):
                                item = scene.umodel_umap_scan_results.add()
                                item.map_name = os.path.splitext(os.path.basename(json_path))[0]
                                item.map_path = json_path
                                matches += 1
                                break
                        except Exception:
                            continue
                else:
                    for entity in json_obj:
                        entity_type = entity.get("Type")
                        if entity_type not in StaticMesh.static_mesh_types:
                            continue

                        try:
                            static_mesh = StaticMesh(json_obj, entity, entity_type)
                            if static_mesh.invalid:
                                continue

                            # Reuse your existing bounds logic
                            if utils.static_mesh_has_instance_in_bounds(static_mesh):
                                item = scene.umodel_umap_scan_results.add()
                                item.map_name = os.path.splitext(os.path.basename(json_path))[0]
                                item.map_path = json_path
                                matches += 1
                                break

                        except Exception:
                            # Never let a single bad entity kill the scan
                            continue

            context.window_manager.progress_end()
        finally:
            scene.umodel_use_vertex_bounds = False

        if only_bpps:
            self.report({'INFO'}, f"UMAP scan complete (BPP-only): {matches} / {total} maps with BPPs within bounds")
        else:
            self.report({'INFO'}, f"UMAP scan complete: {matches} / {total} maps within bounds")
        return {'FINISHED'}



class UMODEL_OT_clear_umap_scan_results(bpy.types.Operator):
    bl_idname = "umodel.clear_umap_scan_results"
    bl_label = "Clear UMAP Scan Results"
    bl_description = "Clear the UMAP scan results list"

    def execute(self, context):
        scene = context.scene
        scene.umodel_umap_scan_results.clear()
        scene.umodel_umap_scan_index = 0
        return {'FINISHED'}


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
            scene.umodel_use_vertex_bounds = False

        db.save_db()

        self._print_unrecognized_textures()

        if self._has_warnings:
            self._op_message('WARNING', "Map import had warnings. Check console for details.")

        self.report({'INFO'}, f"Imported {imported}/{total} scanned maps.")
        return {'FINISHED'}

def menu_func_object(menu: bpy.types.Menu, _: bpy.types.Context) -> None:
    menu.layout.operator(UMODELTOOLS_OT_recover_unreal_asset.bl_idname)
    menu.layout.operator(UMODELTOOLS_OT_import_unreal_assets.bl_idname)
    menu.layout.operator(UMODELTOOLS_OT_realign_asset.bl_idname)


def menu_func_import(menu: bpy.types.Menu, _: bpy.types.Context) -> None:
    menu.layout.operator(UMODELTOOLS_OT_import_unreal_map.bl_idname)


def bl_register() -> None:
    bpy.types.VIEW3D_MT_object.append(menu_func_object)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def bl_unregister() -> None:
    bpy.types.VIEW3D_MT_object.remove(menu_func_object)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
