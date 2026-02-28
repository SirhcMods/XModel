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
        from .panels import UMODELTOOLS_PG_umap_scan_result
        register_bounds_props(UMODELTOOLS_PG_umap_scan_result)		
    except Exception:  # pylint: disable=broad-exception-caught
        traceback.print_exc()


def unregister():
    try:
        unregister_bounds_props()
        auto_load.unregister()
    except Exception:  # pylint: disable=broad-exception-caught
        traceback.print_exc()

def register_bounds_props(umap_result_pg_type):
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
        default=""
    )
    bpy.types.Scene.umodel_umap_scan_results = bpy.props.CollectionProperty(
        type=umap_result_pg_type
    )
    bpy.types.Scene.umodel_umap_scan_index = bpy.props.IntProperty(default=0)

def unregister_bounds_props():
    del bpy.types.Scene.umodel_use_vertex_bounds
    del bpy.types.Scene.umodel_min_x
    del bpy.types.Scene.umodel_max_x
    del bpy.types.Scene.umodel_min_y
    del bpy.types.Scene.umodel_max_y

    del bpy.types.Scene.umodel_umap_scan_dir
    del bpy.types.Scene.umodel_umap_scan_results
    del bpy.types.Scene.umodel_umap_scan_index

    del bpy.types.Scene.umodel_import_materials


__all__ = (
    'bl_info',
    'register',
    'unregister',
    'PACKAGE_NAME'
)


if __name__ == "__main__":
    register()
