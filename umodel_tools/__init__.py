# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

import os
import sys
import traceback

import bpy


def _abs_dir_path(value: str) -> str:
    """Resolve Blender '//' paths to absolute OS paths and normalize."""
    if not value:
        return ""
    value = bpy.path.abspath(value)
    import os as _os
    return _os.path.normpath(_os.path.abspath(value))


def _make_abs_update(prop_name: str):
    """Create an update callback that forces a DIR_PATH property to store an absolute path."""
    def _upd(self, _context):
        cur = getattr(self, prop_name, "") or ""
        if not cur:
            return
        abs_p = _abs_dir_path(cur)
        if abs_p != cur:
            setattr(self, prop_name, abs_p)
    return _upd



# include custom lib vendoring dir
parent_dir = os.path.abspath(os.path.dirname(__file__))
vendor_dir = os.path.join(parent_dir, 'third_party')

sys.path.append(vendor_dir)

from . import auto_load  # nopep8 pylint: disable=wrong-import-position


#: Addon description for Blender. Displayed in settings.
bl_info = {
    "name": "XModel",
    "author": "Skarn, iSrirachaa",
    "version": (1, 0),
    "blender": (3, 40, 0),
    "description": "Import Unreal Engine games scenes and assets into Blender.",
    "category": "Import-Export"
}

#: Name of the addon recognizeable by Blender
PACKAGE_NAME = __package__


def register():
    auto_load.init()

    try:
        auto_load.register()
        from .panels import UMODELTOOLS_PG_umap_scan_result, UMODELTOOLS_PG_bpp_scan_result
        register_bounds_props(UMODELTOOLS_PG_umap_scan_result, UMODELTOOLS_PG_bpp_scan_result)		
    except Exception:  # pylint: disable=broad-exception-caught
        traceback.print_exc()


def unregister():
    try:
        unregister_bounds_props()
        auto_load.unregister()
    except Exception:  # pylint: disable=broad-exception-caught
        traceback.print_exc()

def register_bounds_props(umap_result_pg_type, bpp_result_pg_type):
    bpy.types.Scene.umodel_use_vertex_bounds = bpy.props.BoolProperty(
        name="Import Within Map Bounds",
        description="Only import actors within the calculated vertex bounds",
        default=False
    )
    bpy.types.Scene.umodel_min_x = bpy.props.FloatProperty(name="Min X")
    bpy.types.Scene.umodel_max_x = bpy.props.FloatProperty(name="Max X")
    bpy.types.Scene.umodel_min_y = bpy.props.FloatProperty(name="Min Y")
    bpy.types.Scene.umodel_max_y = bpy.props.FloatProperty(name="Max Y")
	
    bpy.types.Scene.umodel_umap_scan_dir = bpy.props.StringProperty(
        name="UMAP JSON Directory",
        description="Folder containing exported .umap .json files (scanned recursively)",
        subtype='DIR_PATH',
        default="",
        update=_make_abs_update("umodel_umap_scan_dir"),
    )
    bpy.types.Scene.umodel_umap_scan_results = bpy.props.CollectionProperty(
        type=umap_result_pg_type
    )
    bpy.types.Scene.umodel_umap_scan_index = bpy.props.IntProperty(default=0)
	
    bpy.types.Scene.umodel_bpp_scan_dir = bpy.props.StringProperty(
        name="BPP JSON Directory",
        description="Folder containing exported BPP_*.json files (scanned recursively)",
        subtype='DIR_PATH',
        default="",
        update=_make_abs_update("umodel_bpp_scan_dir"),
    )
    bpy.types.Scene.umodel_bpp_scan_results = bpy.props.CollectionProperty(
        type=bpp_result_pg_type
    )
    bpy.types.Scene.umodel_bpp_scan_index = bpy.props.IntProperty(default=0)

    bpy.types.Scene.umodel_bpp_apply_override_materials = bpy.props.BoolProperty(
        name="Use OverrideMaterials from BPP",
        description="Apply per-component OverrideMaterials from BPP JSON during build",
        default=False
    )


    # General import options (apply to single UMAP import, bounds import, and BPP builder)
    bpy.types.Scene.umodel_apply_override_materials = bpy.props.BoolProperty(
        name="Use OverrideMaterials",
        description="Apply per-component OverrideMaterials when available (UMAP bounds import + single UMAP import + BPP builder)",
        default=False
    )

    bpy.types.Scene.umodel_load_pbr_maps = bpy.props.BoolProperty(
        name="Use PBR Maps",
        description="Load PBR textures (normal/ORM/etc) into materials when available",
        default=True
    )

    bpy.types.Scene.umodel_asset_path_filter = bpy.props.StringProperty(
        name="Import Filter Path",
        description="Only import meshes whose Unreal asset path starts with this prefix (leave blank for no filter)",
        subtype='DIR_PATH',
        default=""
    )


def unregister_bounds_props():
    del bpy.types.Scene.umodel_use_vertex_bounds
    del bpy.types.Scene.umodel_min_x
    del bpy.types.Scene.umodel_max_x
    del bpy.types.Scene.umodel_min_y
    del bpy.types.Scene.umodel_max_y

    del bpy.types.Scene.umodel_umap_scan_dir
    del bpy.types.Scene.umodel_umap_scan_results
    del bpy.types.Scene.umodel_umap_scan_index

    del bpy.types.Scene.umodel_bpp_scan_dir
    del bpy.types.Scene.umodel_bpp_scan_results
    del bpy.types.Scene.umodel_bpp_scan_index
    del bpy.types.Scene.umodel_bpp_apply_override_materials
    del bpy.types.Scene.umodel_apply_override_materials
    del bpy.types.Scene.umodel_load_pbr_maps

    del bpy.types.Scene.umodel_asset_path_filter

__all__ = (
    'bl_info',
    'register',
    'unregister',
    'PACKAGE_NAME'
)


if __name__ == "__main__":
    register()
