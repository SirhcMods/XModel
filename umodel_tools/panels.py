import bpy

from .preferences import get_addon_preferences
from . import game_profiles


def _profile_feature_enabled(feature_name: str) -> bool:
    prefs = get_addon_preferences()
    profile = prefs.get_active_profile() if prefs else None
    if profile is None:
        return False
    impl = game_profiles.GAME_HANDLERS.get(profile.game)
    return bool(getattr(impl, feature_name, False)) if impl else False


class UMODELTOOLS_PT_asset(bpy.types.Panel):
    bl_region_type = 'WINDOW'
    bl_space_type = 'PROPERTIES'
    bl_context = "object"
    bl_label = "=XModel Asset"

    @classmethod
    def poll(cls, context: bpy.types.Context):
        return (context.scene is not None
                and context.object is not None
                and context.object.type == 'MESH')

    def draw_header(self, context: bpy.types.Context):
        return self.layout.prop(data=context.object.umodel_tools_asset, property='enabled', text="")

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        layout.enabled = context.object.umodel_tools_asset.enabled

        layout.prop(data=context.object.umodel_tools_asset, property='asset_path')


class UMODELTOOLS_PG_asset(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(
        name="Enabled",
        description="Toggles whether the object is treated as an Unreal asset",
        default=False
    )

    asset_path: bpy.props.StringProperty(
        name="Asset path",
        description="Path of the asset in the Unreal engine game"
    )


class UMODEL_PT_general(bpy.types.Panel):
    bl_label = "General"
    bl_idname = "UMODEL_PT_general"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'XModel'
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        prefs = get_addon_preferences()

        col = layout.column(align=True)
        if hasattr(scene, "umodel_apply_override_materials"):
            col.prop(scene, "umodel_apply_override_materials")
        if hasattr(scene, "umodel_load_pbr_maps"):
            col.prop(scene, "umodel_load_pbr_maps")
        # Verbose is an addon preference (global)
        col.prop(prefs, "verbose")


class UMODEL_PT_profile_settings(bpy.types.Panel):
    bl_label = "Import UMAP"
    bl_idname = "UMODEL_PT_profile_settings"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'XModel'
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        prefs = get_addon_preferences()

        layout.label(text="Game profiles:")

        row = layout.row()
        row.template_list(
            "UMODELTOOLS_UL_game_profiles",
            "",
            prefs,
            "profiles",
            prefs,
            "active_profile_index",
        )

        col = row.column(align=True)
        col.operator("umodel_tools.list_action", icon='ADD', text="").action = 'ADD'
        col.operator("umodel_tools.list_action", icon='REMOVE', text="").action = 'REMOVE'
        col.separator()
        col.operator("umodel_tools.list_action", icon='TRIA_UP', text="").action = 'UP'
        col.operator("umodel_tools.list_action", icon='TRIA_DOWN', text="").action = 'DOWN'

        profile = prefs.get_active_profile()
        if profile is None:
            layout.label(text="No active profile.")
            return

        layout.separator()
        layout.label(text="Active profile settings:")
        layout.prop(profile, "game")
        layout.prop(profile, "umodel_export_dir")
        layout.prop(profile, "asset_dir")
        layout.prop(context.scene, "umodel_asset_path_filter")
		
        layout.separator()

        layout.operator_context = 'INVOKE_DEFAULT'
        layout.operator("umodel_tools.import_unreal_map", text="Import Unreal Map", icon='IMPORT')


class UMODEL_PT_import_bounds(bpy.types.Panel):
    bl_label = "Import UMAP with Bounds"
    bl_idname = "UMODEL_PT_import_bounds"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'XModel'
    bl_order = 2
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        layout.operator(
            "umodel.calculate_import_bounds",
            icon='MESH_CUBE'
        )

        col = layout.column(align=True)

        col.prop(scene, "umodel_min_x")
        col.prop(scene, "umodel_max_x")
        col.prop(scene, "umodel_min_y")
        col.prop(scene, "umodel_max_y")

        # Optional mode: only import placed BPP LevelInstances
        if hasattr(scene, "umodel_import_bounds_only_bpps"):
            col.separator()
            col.prop(scene, "umodel_import_bounds_only_bpps")
		
        box = layout.box()
        box.label(text="Path to UMAPS")

        row = box.row(align=True)
        row.prop(scene, "umodel_umap_scan_dir", text="")
        row.operator("umodel.scan_umap_bounds", text="Scan", icon='VIEWZOOM')

        box.template_list(
            "UMODELTOOLS_UL_umap_scan_results",
            "",
            scene,
            "umodel_umap_scan_results",
            scene,
            "umodel_umap_scan_index",
            rows=6
        )

        row = box.row(align=True)
        row.operator("umodel.clear_umap_scan_results", text="Clear", icon='X')
        row = box.row(align=True)
        row.operator("umodel.import_scanned_umap_selected", text="Import Selected")
        row.operator("umodel.import_scanned_umap_all", text="Import All")

class UMODEL_PT_bpp_builder(bpy.types.Panel):
    bl_label = "BPP Builder"
    bl_idname = "UMODEL_PT_bpp_builder"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'XModel'
    bl_order = 3
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        box = layout.box()
        box.label(text="Path to BPPs")

        row = box.row(align=True)
        row.prop(scene, "umodel_bpp_scan_dir", text="")
        row.operator("umodel.scan_bpp_dir", text="Scan", icon='VIEWZOOM')

        box.template_list(
            "UMODELTOOLS_UL_bpp_scan_results",
            "",
            scene,
            "umodel_bpp_scan_results",
            scene,
            "umodel_bpp_scan_index",
            rows=6
        )

        row = box.row(align=True)
        row.operator("umodel.clear_bpp_scan_results", text="Clear", icon='X')

        row = box.row(align=True)
        row.operator("umodel.build_bpp_selected", text="Build", icon='PLAY')




class UMODEL_PT_prop_builder(bpy.types.Panel):
    bl_label = "Prop Builder"
    bl_idname = "UMODEL_PT_prop_builder"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'XModel'
    bl_order = 4
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _profile_feature_enabled("ENABLE_PROP_BUILDER")

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        box = layout.box()
        row = box.row(align=True)
        row.prop(scene, "umodel_prop_scan_dir", text="")
        row.operator("umodel.scan_prop_dir", text="Scan", icon='VIEWZOOM')

        box.prop(scene, "umodel_prop_category", text="Category")

        box.template_list(
            "UMODELTOOLS_UL_prop_scan_results",
            "",
            scene,
            "umodel_prop_scan_results",
            scene,
            "umodel_prop_scan_index",
            rows=12
        )

        row = box.row(align=True)
        row.operator("umodel.clear_prop_scan_results", text="Clear", icon='X')

        row = box.row(align=True)
        row.operator("umodel.import_prop_selected", text="Import Selected", icon='IMPORT')
        row.operator("umodel.import_prop_all", text="Import All", icon='PLAY')


class UMODELTOOLS_PG_prop_scan_result(bpy.types.PropertyGroup):
    selected: bpy.props.BoolProperty(name="Selected", default=False)
    asset_name: bpy.props.StringProperty(name="Asset")
    asset_path: bpy.props.StringProperty(name="Asset Path")
    json_path: bpy.props.StringProperty(name="JSON Path")
    mesh_path: bpy.props.StringProperty(name="Mesh Path")
    category: bpy.props.StringProperty(name="Category")
    category_root: bpy.props.StringProperty(name="Category Root")


class UMODELTOOLS_UL_prop_scan_results(bpy.types.UIList):
    bl_idname = "UMODELTOOLS_UL_prop_scan_results"

    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, _index):
        row = layout.row(align=True)
        row.prop(item, "selected", text="", emboss=False, icon='CHECKBOX_HLT' if item.selected else 'CHECKBOX_DEHLT')
        row.label(text=item.asset_name)

    def draw_filter(self, context, layout):
        row = layout.row(align=True)
        row.prop(self, "filter_name", text="")

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flags = []
        cat = getattr(context.scene, "umodel_prop_category", "__ALL__")
        needle = (self.filter_name or "").strip().lower()
        bitflag = self.bitflag_filter_item
        for item in items:
            show = True
            if cat not in {"", "__ALL__"}:
                show = str(getattr(item, "category", "")) == cat
            if show and needle:
                show = needle in str(getattr(item, "asset_name", "")).lower()
            flags.append(bitflag if show else 0)
        return flags, []


class UMODELTOOLS_PG_bpp_scan_result(bpy.types.PropertyGroup):
    selected: bpy.props.BoolProperty(name="Selected", default=False)
    bpp_name: bpy.props.StringProperty(name="BPP")
    bpp_path: bpy.props.StringProperty(name="Path")


class UMODELTOOLS_UL_bpp_scan_results(bpy.types.UIList):
    bl_idname = "UMODELTOOLS_UL_bpp_scan_results"

    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, _index):
        row = layout.row(align=True)
        row.prop(item, "selected", text="")
        row.label(text=item.bpp_name)

class UMODELTOOLS_PG_umap_scan_result(bpy.types.PropertyGroup):
    map_name: bpy.props.StringProperty(name="Map")
    map_path: bpy.props.StringProperty(name="Path")

class UMODELTOOLS_UL_umap_scan_results(bpy.types.UIList):
    bl_idname = "UMODELTOOLS_UL_umap_scan_results"

    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, _index):
        # item is UMODELTOOLS_PG_umap_scan_result
        layout.label(text=item.map_name)

def topbar_menu_func(menu: bpy.types.Menu, context: bpy.types.Context):
    if context.region.alignment != 'RIGHT':
        return

    prefs = get_addon_preferences()

    if not prefs.display_cur_profile:
        return

    cur_profile = prefs.get_active_profile()
    menu.layout.label(text=f"UMT Active profile: {cur_profile.name if cur_profile else None}")


def bl_register() -> None:
    # pylint: disable=assignment-from-no-return

    bpy.types.Object.umodel_tools_asset = bpy.props.PointerProperty(type=UMODELTOOLS_PG_asset)
    bpy.types.TOPBAR_HT_upper_bar.append(topbar_menu_func)


def bl_unregister() -> None:
    del bpy.types.Object.umodel_tools_asset
    bpy.types.TOPBAR_HT_upper_bar.remove(topbar_menu_func)
