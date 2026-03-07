import json
import os
import typing as t

import bpy
import bpy_extras.io_utils

from .. import asset_db
from .. import map_importer
from .. import preferences
from ..utils import _resolve_dir_path

class UMODELTOOLS_OT_import_unreal_map(map_importer.MapImporter, bpy.types.Operator, bpy_extras.io_utils.ImportHelper):
    bl_idname = "umodel_tools.import_unreal_map"
    bl_label = "Import Unreal Map"
    bl_description = "Imports an Unreal Engine map"
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