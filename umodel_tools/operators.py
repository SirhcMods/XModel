
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

        profile = preferences.get_addon_preferences().get_active_profile()
        if profile is None:
            return self._op_message('ERROR', "You need to have an active game profile selected.")

        umodel_export_dir: str = os.path.normpath(profile.umodel_export_dir)
        umodel_export_dir = umodel_export_dir[1:] if umodel_export_dir.startswith(os.sep) else umodel_export_dir

        if not umodel_export_dir:
            return self._op_message('ERROR', "You need to specify a UModel export dir in Scene properties.")

        if not os.path.isdir(umodel_export_dir):
            return self._op_message('ERROR', f"Path to UModel export dir {umodel_export_dir} does not exist.")

        asset_dir: str = os.path.normpath(profile.asset_dir)
        asset_dir = asset_dir[1:] if asset_dir.startswith(os.sep) else asset_dir

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

        umodel_export_dir: str = os.path.normpath(profile.umodel_export_dir)
        umodel_export_dir = umodel_export_dir[1:] if umodel_export_dir.startswith(os.sep) else umodel_export_dir

        if not umodel_export_dir:
            return self._op_message('ERROR', "You need to specify a UModel export dir in Scene properties.")

        if not os.path.isdir(umodel_export_dir):
            return self._op_message('ERROR', f"Path to UModel export dir {umodel_export_dir} does not exist.")

        asset_dir: str = os.path.normpath(profile.asset_dir)
        asset_dir = asset_dir[1:] if asset_dir.startswith(os.sep) else asset_dir

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

        umodel_export_dir: str = os.path.normpath(profile.umodel_export_dir)
        umodel_export_dir = umodel_export_dir[1:] if umodel_export_dir.startswith(os.sep) else umodel_export_dir

        if not umodel_export_dir:
            return self._op_message('ERROR', "You need to specify a UModel export dir in Scene properties.")

        if not os.path.isdir(umodel_export_dir):
            return self._op_message('ERROR', f"Path to UModel export dir {umodel_export_dir} does not exist.")

        asset_dir: str = os.path.normpath(profile.asset_dir)
        asset_dir = asset_dir[1:] if asset_dir.startswith(os.sep) else asset_dir

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

        # Lazy import to avoid circular import issues / heavy import cost
        from .map_importer import StaticMesh  # pylint: disable=import-outside-toplevel

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

        for idx, json_path in enumerate(json_files, start=1):
            percent = (idx / total) * 100.0

            # Console logging (cheap, safe)
            print(f"[UMAP SCAN] {idx}/{total} ({percent:.1f}%) - {os.path.basename(json_path)}")

            # Occasional status bar update (don’t spam)
            if idx == 1 or idx % 10 == 0 or idx == total:
                self.report({'INFO'}, f"Scanning UMAPs: {idx}/{total} ({percent:.1f}%)")

            context.window_manager.progress_update(idx)

            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    json_obj = json.load(f)
            except Exception:
                continue

            if not isinstance(json_obj, list):
                continue

            # If any StaticMesh/ISM/HISM instance is in bounds, record the map once.
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

        self.report({'INFO'}, f"UMAP scan complete: {matches} / {total} maps intersect bounds")
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


class UMODEL_OT_copy_umap_scan_results(bpy.types.Operator):
    bl_idname = "umodel.copy_umap_scan_results"
    bl_label = "Copy UMAP Scan Results"
    bl_description = "Copy the found map paths to clipboard"

    def execute(self, context):
        scene = context.scene
        if not scene.umodel_umap_scan_results:
            self.report({'WARNING'}, "No results to copy")
            return {'CANCELLED'}

        text = "\n".join(item.map_path for item in scene.umodel_umap_scan_results)
        context.window_manager.clipboard = text
        self.report({'INFO'}, "Copied map list to clipboard")
        return {'FINISHED'}

class UMODEL_OT_import_scanned_umap_selected(map_importer.MapImporter, bpy.types.Operator):
    bl_idname = "umodel.import_scanned_umap_selected"
    bl_label = "Import Selected Scanned UMAP"
    bl_description = "Import the selected UMAP from the scan results using the same importer logic"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene

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

        umodel_export_dir: str = os.path.normpath(profile.umodel_export_dir)
        umodel_export_dir = umodel_export_dir[1:] if umodel_export_dir.startswith(os.sep) else umodel_export_dir
        if not umodel_export_dir:
            return self._op_message('ERROR', "You need to specify a UModel export dir in Scene properties.")
        if not os.path.isdir(umodel_export_dir):
            return self._op_message('ERROR', f"Path to UModel export dir {umodel_export_dir} does not exist.")

        asset_dir: str = os.path.normpath(profile.asset_dir)
        asset_dir = asset_dir[1:] if asset_dir.startswith(os.sep) else asset_dir
        if not asset_dir:
            return self._op_message('ERROR', "You need to specify an asset dir in Scene properties.")
        if not os.path.isdir(asset_dir):
            return self._op_message('ERROR', f"Path to asset dir {asset_dir} does not exist.")

        if not os.path.isfile(map_path):
            return self._op_message('ERROR', f"Map file not found: {map_path}")

        self._unrecognized_texture_types.clear()

        db = asset_db.AssetDB(asset_dir)

        # Import exactly one map using the same internal importer your menu operator uses
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

        if not hasattr(scene, "umodel_umap_scan_results") or len(scene.umodel_umap_scan_results) == 0:
            self.report({'ERROR'}, "No scan results to import.")
            return {'CANCELLED'}

        profile = preferences.get_addon_preferences().get_active_profile()
        if profile is None:
            return self._op_message('ERROR', "You need to have an active game profile selected.")

        umodel_export_dir: str = os.path.normpath(profile.umodel_export_dir)
        umodel_export_dir = umodel_export_dir[1:] if umodel_export_dir.startswith(os.sep) else umodel_export_dir
        if not umodel_export_dir:
            return self._op_message('ERROR', "You need to specify a UModel export dir in Scene properties.")
        if not os.path.isdir(umodel_export_dir):
            return self._op_message('ERROR', f"Path to UModel export dir {umodel_export_dir} does not exist.")

        asset_dir: str = os.path.normpath(profile.asset_dir)
        asset_dir = asset_dir[1:] if asset_dir.startswith(os.sep) else asset_dir
        if not asset_dir:
            return self._op_message('ERROR', "You need to specify an asset dir in Scene properties.")
        if not os.path.isdir(asset_dir):
            return self._op_message('ERROR', f"Path to asset dir {asset_dir} does not exist.")

        self._unrecognized_texture_types.clear()

        total = len(scene.umodel_umap_scan_results)
        db = asset_db.AssetDB(asset_dir)

        context.window_manager.progress_begin(0, total)

        imported = 0
        for i, item in enumerate(scene.umodel_umap_scan_results, start=1):
            map_path = item.map_path
            percent = (i / total) * 100.0

            print(f"[UMAP IMPORT] {i}/{total} ({percent:.1f}%) - {os.path.basename(map_path)}")
            if i == 1 or i % 5 == 0 or i == total:
                self.report({'INFO'}, f"Importing scanned UMAPs: {i}/{total} ({percent:.1f}%)")

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
            if ok:
                imported += 1

        context.window_manager.progress_end()

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
