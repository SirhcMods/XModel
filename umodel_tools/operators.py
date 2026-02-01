
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


def _index_mindseye_mi_json(export_dir: str) -> dict[str, str]:
    """Build a mapping of MI material name -> absolute json path.

    We index by filename without extension (e.g. "MI_LayeringME_ConcreteBare_01").
    """
    out: dict[str, str] = {}
    for root, _dirs, files in os.walk(export_dir):
        for fn in files:
            if not fn.endswith('.json'):
                continue
            # We mostly care about material instances; indexing all json is cheap enough.
            key = os.path.splitext(fn)[0]
            if key not in out:
                out[key] = os.path.join(root, fn)
    return out


def _unwrap_mi_json(mi_raw: t.Any) -> dict:
    """FModel sometimes exports a list of exports; choose the dict that looks like an MI."""
    if isinstance(mi_raw, dict):
        return mi_raw
    if isinstance(mi_raw, list):
        for entry in mi_raw:
            if isinstance(entry, dict) and ('Textures' in entry or 'Parameters' in entry):
                return entry
        # fallback: first dict
        for entry in mi_raw:
            if isinstance(entry, dict):
                return entry
    return {}


def _mi_tex_objectpath_to_relpath(tex_obj: str) -> str:
    """Convert Unreal ObjectPath-like string to a relative file path base (no extension).

    Example:
      "/Game/.../T_def_White_d.T_def_White_d" -> "Game/.../T_def_White_d"
      "/MindsEye/Content/.../T_def_White_d.T_def_White_d" -> "MindsEye/Content/.../T_def_White_d"
    """
    tex_obj = tex_obj.strip()
    if tex_obj.startswith('Texture'):
        # Sometimes values come as "Texture2D'...path...'". Strip wrapper.
        q1 = tex_obj.find("'")
        q2 = tex_obj.rfind("'")
        if q1 != -1 and q2 != -1 and q2 > q1:
            tex_obj = tex_obj[q1 + 1:q2]

    # Drop leading slash
    tex_obj = tex_obj.lstrip('/')
    # Keep part before first dot (the package path)
    if '.' in tex_obj:
        tex_obj = tex_obj.split('.', 1)[0]
    return os.path.normpath(tex_obj)


def _remap_game_root(rel_no_ext: str) -> str:
    # Mindseye: FModel exports under MindsEye/Content rather than /Game
    rel_no_ext = rel_no_ext.replace('\\', '/')
    if rel_no_ext.startswith('Game/'):
        return 'MindsEye/Content/' + rel_no_ext[len('Game/'):]
    return rel_no_ext


def _new_node(nodes, node_type: str, loc: tuple[float, float]):
    n = nodes.new(node_type)
    n.location = loc
    return n


def _build_nodes_from_mi(material: bpy.types.Material,
                         mi: dict,
                         export_dir: str,
                         texture_ext: str,
                         verbose: bool = False) -> tuple[int, int]:
    """Build a principled material from a Mindseye MI json.

    Returns (textures_used, textures_missing).
    """
    material.use_nodes = True
    nt = material.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = _new_node(nodes, 'ShaderNodeOutputMaterial', (600, 0))
    bsdf = _new_node(nodes, 'ShaderNodeBsdfPrincipled', (250, 0))
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

    tex_dict = mi.get('Textures') or {}
    if not isinstance(tex_dict, dict):
        tex_dict = {}

    used = 0
    missing = 0

    def load_image_for_objectpath(obj_path: str) -> bpy.types.Image | None:
        rel_no_ext = _mi_tex_objectpath_to_relpath(obj_path)
        rel_no_ext = _remap_game_root(rel_no_ext)
        rel = rel_no_ext + texture_ext
        abs_path = os.path.join(export_dir, rel)
        if not os.path.isfile(abs_path):
            return None
        try:
            return bpy.data.images.load(abs_path, check_existing=True)
        except Exception:
            return None

    def tex_basename_from_objectpath(obj_path: str) -> str:
        # '/Path/Foo.Bar' -> 'Foo'
        rel_no_ext = _mi_tex_objectpath_to_relpath(obj_path)
        return os.path.basename(rel_no_ext).replace('\\', '/').split('/')[-1]

    def base_prefix(name: str) -> str:
        # Strip common surface suffixes so 'T_ConcreteBare_01_D' and '..._M' match.
        for suf in ('_DN', '_D', '_N'):
            if name.endswith(suf):
                return name[:-len(suf)]
        return name

    # -------------------------------------------------------
    # Mindseye: keep it simple.
    # - BaseColor -> Base Color
    # - Normal -> Normal
    # - Packed map: only include a *_M whose prefix matches the BaseColor texture prefix
    #   (e.g. T_ConcreteBare_01_D -> T_ConcreteBare_01_M)
    # -------------------------------------------------------

    base_obj = tex_dict.get('BaseColor')
    norm_obj = tex_dict.get('Normal')

    base_prefix_key = None
    if isinstance(base_obj, str) and base_obj:
        base_name = tex_basename_from_objectpath(base_obj)
        base_prefix_key = base_prefix(base_name)

    packed_obj = None
    packed_key = None
    if base_prefix_key:
        # Find a texture asset whose basename ends with '_M' and matches the base prefix.
        for k, v in tex_dict.items():
            if not isinstance(v, str) or not v:
                continue
            n = tex_basename_from_objectpath(v)
            if not n.endswith('_M'):
                continue
            if base_prefix(n) == base_prefix_key:
                packed_obj = v
                packed_key = k
                break

    if verbose:
        print(
            f"[BuildMaterials] {material.name}: BaseColor={'OK' if base_obj else 'None'} "
            f"Normal={'OK' if norm_obj else 'None'} PackedM={'OK' if packed_obj else 'None'}"
        )
        if packed_obj and packed_key and packed_key != 'Mask':
            print(f"[BuildMaterials] {material.name}: Using packed _M from key '{packed_key}'")

    y = 0

    # BaseColor
    if isinstance(base_obj, str) and base_obj:
        img = load_image_for_objectpath(base_obj)
        if img is None:
            missing += 1
            if verbose:
                print(f"[BuildMaterials] Missing BaseColor for {material.name}: {base_obj}")
        else:
            n = _new_node(nodes, 'ShaderNodeTexImage', (-500, y))
            n.image = img
            n.label = 'BaseColor'
            links.new(n.outputs['Color'], bsdf.inputs['Base Color'])
            used += 1
            y -= 260

    # Normal
    if isinstance(norm_obj, str) and norm_obj:
        img = load_image_for_objectpath(norm_obj)
        if img is None:
            missing += 1
            if verbose:
                print(f"[BuildMaterials] Missing Normal for {material.name}: {norm_obj}")
        else:
            img.colorspace_settings.name = 'Non-Color'
            n = _new_node(nodes, 'ShaderNodeTexImage', (-500, y))
            n.image = img
            n.label = 'Normal'
            nmap = _new_node(nodes, 'ShaderNodeNormalMap', (-250, y))
            links.new(n.outputs['Color'], nmap.inputs['Color'])
            links.new(nmap.outputs['Normal'], bsdf.inputs['Normal'])
            used += 1
            y -= 260

    # Packed _M (assume G=Roughness, B=Metallic)
    if isinstance(packed_obj, str) and packed_obj:
        img = load_image_for_objectpath(packed_obj)
        if img is None:
            missing += 1
            if verbose:
                print(f"[BuildMaterials] Missing packed _M for {material.name}: {packed_obj}")
        else:
            img.colorspace_settings.name = 'Non-Color'
            n = _new_node(nodes, 'ShaderNodeTexImage', (-500, y))
            n.image = img
            n.label = 'Packed_M'
            sep = _new_node(nodes, 'ShaderNodeSeparateRGB', (-250, y - 40))
            links.new(n.outputs['Color'], sep.inputs['Image'])
            links.new(sep.outputs['G'], bsdf.inputs['Roughness'])
            links.new(sep.outputs['B'], bsdf.inputs['Metallic'])
            used += 1
            y -= 260

    return used, missing


class UMODELTOOLS_OT_build_materials_selected(bpy.types.Operator):
    """Build Mindseye/FModel materials for selected objects from MI json exports."""

    bl_idname = "umodel_tools.build_materials_selected"
    bl_label = "Build Materials"
    bl_description = "For selected meshes, locate MaterialInstance json exports and build shader nodes"
    bl_options = {'REGISTER', 'UNDO'}

    texture_format: bpy.props.EnumProperty(
        name="Texture format",
        description="Format of textures expected in the FModel export directory.",
        items=[
            ('.png', '.png', '', 0),
            ('.dds', '.dds', '', 1),
            ('.tga', '.tga', '', 2)
        ],
        default='.png'
    )

    rebuild_existing: bpy.props.BoolProperty(
        name="Rebuild existing",
        description="If enabled, rebuild node trees even if they already have nodes",
        default=True
    )

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context: bpy.types.Context):
        prefs = preferences.get_addon_preferences()
        profile = prefs.get_active_profile()
        if profile is None:
            self.report({'ERROR'}, "No active game profile selected")
            return {'CANCELLED'}

        export_dir = os.path.normpath(profile.umodel_export_dir)
        export_dir = export_dir[1:] if export_dir.startswith(os.sep) else export_dir
        if not export_dir or not os.path.isdir(export_dir):
            self.report({'ERROR'}, "Export Directory is not set or does not exist")
            return {'CANCELLED'}

        verbose = bool(prefs.verbose)

        # build MI json index once
        if verbose:
            print(f"[BuildMaterials] Indexing json files under: {export_dir}")
        mi_index = _index_mindseye_mi_json(export_dir)
        if verbose:
            print(f"[BuildMaterials] Indexed {len(mi_index)} json files")

        sel = [o for o in context.selected_objects if o and o.type == 'MESH']
        if not sel:
            self.report({'WARNING'}, "No selected mesh objects")
            return {'CANCELLED'}

        total_mats = 0
        built_mats = 0
        used_total = 0
        missing_total = 0

        for obj in sel:
            if obj.data is None:
                continue

            for mat in obj.data.materials:
                if mat is None:
                    continue
                total_mats += 1

                # Skip if already has a principled setup and rebuild_existing is False
                if not self.rebuild_existing and mat.use_nodes and mat.node_tree and mat.node_tree.nodes:
                    continue

                # Find MI json by material name
                mi_path = mi_index.get(mat.name)
                if mi_path is None:
                    # fallback: strip numeric suffix like ".001"
                    base = mat.name.split('.', 1)[0]
                    mi_path = mi_index.get(base)

                if mi_path is None:
                    if verbose:
                        print(f"[BuildMaterials] No MI json found for material '{mat.name}'")
                    continue

                try:
                    with open(mi_path, 'r', encoding='utf-8') as f:
                        mi_raw = json.load(f)
                    mi = _unwrap_mi_json(mi_raw)
                except Exception as e:
                    if verbose:
                        print(f"[BuildMaterials] Failed reading '{mi_path}': {e}")
                    continue

                used, missing = _build_nodes_from_mi(
                    material=mat,
                    mi=mi,
                    export_dir=export_dir,
                    texture_ext=self.texture_format,
                    verbose=verbose
                )
                built_mats += 1
                used_total += used
                missing_total += missing

        self.report({'INFO'}, f"Built {built_mats}/{total_mats} material(s). Textures used={used_total}, missing={missing_total}")
        return {'FINISHED'}


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
        finally:
            scene.umodel_use_vertex_bounds = False

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
        try:
            scene.umodel_use_vertex_bounds = True
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
