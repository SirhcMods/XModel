import bpy

from .preferences import get_addon_preferences
from . import game_profiles
from .utils import _profile_feature_enabled


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
        if hasattr(scene, "umodel_asset_loading_mode"):
            col.prop(scene, "umodel_asset_loading_mode")
        if hasattr(scene, "umodel_import_materials"):
            col.prop(scene, "umodel_import_materials")
        if hasattr(scene, "umodel_apply_override_materials"):
            row = col.row()
            row.enabled = bool(getattr(scene, "umodel_import_materials", True))
            row.prop(scene, "umodel_apply_override_materials")
        if hasattr(scene, "umodel_load_pbr_maps"):
            row = col.row()
            row.enabled = bool(getattr(scene, "umodel_import_materials", True))
            row.prop(scene, "umodel_load_pbr_maps")
        # Verbose is an addon preference (global)
        col.prop(prefs, "verbose")

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
        col.separator()
        col.operator("umodel_tools.list_action", icon='FILE_TICK', text="").action = 'SAVE'

        profile = prefs.get_active_profile()
        if profile is None:
            layout.label(text="No active profile.")
            return

        layout.separator()
        layout.label(text="Active profile settings:")
        layout.prop(profile, "game")
        layout.prop(profile, "umodel_export_dir")
        layout.prop(profile, "asset_dir")
        layout.prop(profile, "asset_path_filter")
        if hasattr(context.scene, "umodel_asset_keyword_filter"):
            layout.prop(context.scene, "umodel_asset_keyword_filter")
        if hasattr(context.scene, "umodel_asset_ignore_folders"):
            layout.prop(context.scene, "umodel_asset_ignore_folders")


class UMODEL_PT_import_umap(bpy.types.Panel):
    bl_label = "Import UMAP"
    bl_idname = "UMODEL_PT_import_umap"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'XModel'
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        prefs = get_addon_preferences()

        layout.operator_context = 'INVOKE_DEFAULT'
        layout.operator("umodel_tools.import_unreal_map", text="Import Unreal Map", icon='IMPORT')


class UMODELTOOLS_PG_import_bound(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Bound", default="bound0")
    min_x: bpy.props.FloatProperty(name="Min X")
    max_x: bpy.props.FloatProperty(name="Max X")
    min_y: bpy.props.FloatProperty(name="Min Y")
    max_y: bpy.props.FloatProperty(name="Max Y")
    min_z: bpy.props.FloatProperty(name="Min Z")
    max_z: bpy.props.FloatProperty(name="Max Z")


class UMODELTOOLS_UL_import_bounds(bpy.types.UIList):
    bl_idname = "UMODELTOOLS_UL_import_bounds"

    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, _index):
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=True, icon='MESH_CUBE')


class UMODEL_PT_import_bounds(bpy.types.Panel):
    bl_label = "Import UMAP with Bounds"
    bl_idname = "UMODEL_PT_import_bounds"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'XModel'
    bl_order = 2
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _profile_feature_enabled("ENABLE_IMPORT_UMAP_WITH_BOUNDS")

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        box = layout.box()
        box.label(text="Import Bounds")

        row = box.row(align=True)
        row.operator(
            "umodel.calculate_import_bounds",
            text="Generate from Selection",
            icon='MESH_CUBE'
        )
        row.operator("umodel.remove_import_bound", text="", icon='REMOVE')

        box.template_list(
            "UMODELTOOLS_UL_import_bounds",
            "",
            scene,
            "umodel_import_bounds",
            scene,
            "umodel_import_bounds_index",
            rows=4
        )

        active_bound = None
        if 0 <= int(getattr(scene, "umodel_import_bounds_index", -1)) < len(scene.umodel_import_bounds):
            active_bound = scene.umodel_import_bounds[scene.umodel_import_bounds_index]

        if active_bound is not None:
            col = box.column(align=True)
            col.prop(active_bound, "name", text="Name")
            col.prop(active_bound, "min_x")
            col.prop(active_bound, "max_x")
            col.prop(active_bound, "min_y")
            col.prop(active_bound, "max_y")
            col.prop(active_bound, "min_z")
            col.prop(active_bound, "max_z")

        # Optional mode: also include placed BPP LevelInstances
        if hasattr(scene, "umodel_import_bounds_only_bpps"):
            box.separator()
            box.prop(scene, "umodel_import_bounds_only_bpps")

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

        if hasattr(scene, "umodel_bounds_phased_import"):
            box.prop(scene, "umodel_bounds_phased_import")
            row = box.row(align=True)
            row.enabled = bool(getattr(scene, "umodel_bounds_phased_import", False))
            row.prop(scene, "umodel_bounds_phase_size")

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

    @classmethod
    def poll(cls, context):
        return _profile_feature_enabled("ENABLE_BPP_BUILDER")

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




class UMODEL_PT_material_builder(bpy.types.Panel):
    bl_label = "Material Builder"
    bl_idname = "UMODEL_PT_material_builder"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'XModel'
    bl_order = 5
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _profile_feature_enabled("ENABLE_MATERIAL_BUILDER")

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        col = layout.column(align=True)
        col.prop(scene, "umodel_material_builder_root", text="Search Root Dir")
        col.label(text=f"Selected mesh objects: {sum(1 for obj in context.selected_objects if getattr(obj, 'type', None) == 'MESH')}")
        col.operator("umodel.build_selected_materials", text="Build Materials", icon='MATERIAL')
        if _profile_feature_enabled("ENABLE_COLOR_PALETTE_UNWRAPPER"):
            col.operator("umodel.bake_tints_to_attr", text="Bake Tints to attr", icon='GROUP_VCOL')


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

class UMODEL_PT_landscape_material_compiler(bpy.types.Panel):
    bl_label = "Landscape Material Compiler"
    bl_idname = "UMODEL_PT_landscape_material_compiler"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'XModel'
    bl_order = 6
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _profile_feature_enabled("ENABLE_LANDSCAPE_MATERIAL_COMPILER")

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        col = layout.column(align=True)
        col.prop(scene, "umodel_landscape_umap_dir", text="UMAP Folder")
        col.prop(scene, "umodel_landscape_weightmap_dir", text="Weightmap Folder")

        layout.separator()
        layout.operator("umodel.build_landscape_materials", text="Build Material", icon='MATERIAL')
