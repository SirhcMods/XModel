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
        from .panels import UMODELTOOLS_PG_umap_scan_result, UMODELTOOLS_PG_bpp_scan_result, UMODELTOOLS_PG_prop_scan_result
        from .preferences import load_profiles_from_disk
        register_bounds_props(UMODELTOOLS_PG_umap_scan_result, UMODELTOOLS_PG_bpp_scan_result, UMODELTOOLS_PG_prop_scan_result)
        load_profiles_from_disk()
    except Exception:  # pylint: disable=broad-exception-caught
        traceback.print_exc()


def unregister():
    try:
        unregister_bounds_props()
        auto_load.unregister()
    except Exception:  # pylint: disable=broad-exception-caught
        traceback.print_exc()

def _prop_category_items(self, context):
    scene = getattr(context, "scene", None) if context is not None else None
    if scene is None:
        return [("__ALL__", "All", "Show props from all categories")]

    items = [("__ALL__", "All", "Show props from all categories")]
    cats = []
    try:
        for item in getattr(scene, "umodel_prop_scan_results", []):
            cat = str(getattr(item, "category", "") or "")
            if cat and cat not in cats:
                cats.append(cat)
    except Exception:
        pass

    for cat in sorted(cats, key=lambda x: x.lower()):
        items.append((cat, cat, f"Show props in category: {cat}"))
    return items


def register_bounds_props(umap_result_pg_type, bpp_result_pg_type, prop_result_pg_type):
    bpy.types.Scene.umodel_use_vertex_bounds = bpy.props.BoolProperty(
        name="Import Within Map Bounds",
        description="Only import actors within the calculated vertex bounds",
        default=False
    )
    bpy.types.Scene.umodel_min_x = bpy.props.FloatProperty(name="Min X")
    bpy.types.Scene.umodel_max_x = bpy.props.FloatProperty(name="Max X")
    bpy.types.Scene.umodel_min_y = bpy.props.FloatProperty(name="Min Y")
    bpy.types.Scene.umodel_max_y = bpy.props.FloatProperty(name="Max Y")

    bpy.types.Scene.umodel_import_bounds_only_bpps = bpy.props.BoolProperty(
        name="Import only BPPs",
        description="When enabled, bounds scan/import will only consider placed BPP LevelInstances (BPP_*)",
        default=False
    )
	
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



    bpy.types.Scene.umodel_prop_scan_dir = bpy.props.StringProperty(
        name="Prop Directory",
        description="Folder containing prop .psk/.pskx files with adjacent .json descriptors (scanned recursively)",
        subtype='DIR_PATH',
        default="",
        update=_make_abs_update("umodel_prop_scan_dir"),
    )
    bpy.types.Scene.umodel_prop_scan_results = bpy.props.CollectionProperty(
        type=prop_result_pg_type
    )
    bpy.types.Scene.umodel_prop_scan_index = bpy.props.IntProperty(default=0)
    bpy.types.Scene.umodel_prop_category = bpy.props.EnumProperty(
        name="Category",
        description="First-level subfolder under the Prop Directory used to filter scanned props",
        items=_prop_category_items,
        default=0
    )

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

    bpy.types.Scene.umodel_asset_keyword_filter = bpy.props.StringProperty(
        name="Filter Keywords",
        description="Optional keyword filter for mesh names. Use commas to match any of multiple keywords",
        default=""
    )


    bpy.types.Scene.umodel_material_builder_root = bpy.props.StringProperty(
        name="Search Root Dir",
        description="Optional root directory to search for mesh JSON files. If empty, the active profile Export Directory will be used",
        subtype='DIR_PATH',
        default="",
        update=_make_abs_update("umodel_material_builder_root"),
    )

    bpy.types.Scene.umodel_landscape_umap_dir = bpy.props.StringProperty(
        name="Landscape UMAP Folder",
        description="Folder containing exported landscape UMAP JSON files",
        subtype='DIR_PATH',
        default="",
        update=_make_abs_update("umodel_landscape_umap_dir"),
    )

    bpy.types.Scene.umodel_landscape_weightmap_dir = bpy.props.StringProperty(
        name="Weightmap Folder",
        description="Folder containing weightmap PNGs in UMAP-id subfolders",
        subtype='DIR_PATH',
        default="",
        update=_make_abs_update("umodel_landscape_weightmap_dir"),
    )


def unregister_bounds_props():
    del bpy.types.Scene.umodel_use_vertex_bounds
    del bpy.types.Scene.umodel_min_x
    del bpy.types.Scene.umodel_max_x
    del bpy.types.Scene.umodel_min_y
    del bpy.types.Scene.umodel_max_y

    del bpy.types.Scene.umodel_import_bounds_only_bpps

    del bpy.types.Scene.umodel_umap_scan_dir
    del bpy.types.Scene.umodel_umap_scan_results
    del bpy.types.Scene.umodel_umap_scan_index

    del bpy.types.Scene.umodel_bpp_scan_dir
    del bpy.types.Scene.umodel_bpp_scan_results
    del bpy.types.Scene.umodel_bpp_scan_index
    del bpy.types.Scene.umodel_prop_scan_dir
    del bpy.types.Scene.umodel_prop_scan_results
    del bpy.types.Scene.umodel_prop_scan_index
    del bpy.types.Scene.umodel_prop_category

    del bpy.types.Scene.umodel_bpp_apply_override_materials
    del bpy.types.Scene.umodel_apply_override_materials
    del bpy.types.Scene.umodel_load_pbr_maps

    del bpy.types.Scene.umodel_asset_path_filter
    del bpy.types.Scene.umodel_asset_keyword_filter
    del bpy.types.Scene.umodel_material_builder_root

    del bpy.types.Scene.umodel_landscape_umap_dir
    del bpy.types.Scene.umodel_landscape_weightmap_dir

__all__ = (
    'bl_info',
    'register',
    'unregister',
    'PACKAGE_NAME'
)


if __name__ == "__main__":
    register()
