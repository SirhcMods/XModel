import typing as t

import bpy
import os

from . import PACKAGE_NAME
from . import game_profiles


def _resolve_dir_path(value: str) -> str:
    """Resolve Blender-style paths to a normalized absolute directory path.

    Blender may store paths as blend-relative ("//..."). Always resolve those
    to an OS absolute path for stability.
    """
    if not value:
        return ""

    # Resolve Blender's "//" relative paths (relative to the .blend location)
    value = bpy.path.abspath(value)

    # Normalize and collapse .. segments
    value = os.path.normpath(os.path.abspath(value))

    # Blender on Windows can sometimes yield paths like "\\C:\\..." or "/C:/...".
    # Strip the leading slash/backslash in that specific case.
    if os.name == 'nt' and len(value) >= 3 and value[0] in ('/', '\\') and value[1].isalpha() and value[2] == ':':
        value = value[1:]

    return value


def _make_abs_update(prop_name: str):
    """Create an update callback that forces a directory property to absolute."""

    def _upd(self, _context):
        # Guard against recursion
        if getattr(self, "_umodeltools_path_update_lock", False):
            return

        cur = getattr(self, prop_name, "") or ""
        if not cur:
            return

        abs_p = _resolve_dir_path(cur)
        if abs_p and abs_p != cur:
            setattr(self, "_umodeltools_path_update_lock", True)
            try:
                setattr(self, prop_name, abs_p)
            finally:
                setattr(self, "_umodeltools_path_update_lock", False)

    return _upd


def get_addon_preferences() -> 'UMODELTOOLS_AP_addon_preferences':
    """Returns this addon's preferences.

    :return: Addon preferences.
    """
    return bpy.context.preferences.addons[PACKAGE_NAME].preferences


class UMODELTOOLS_PG_game_profile(bpy.types.PropertyGroup):
    """Game profile settings
    """

    name: bpy.props.StringProperty(
        name="Name",
        description="Name of the profile"
    )

    game: bpy.props.EnumProperty(
        name="Game",
        description="Game of this profile",
        items=game_profiles.SUPPORTED_GAMES,
        default=0
    )

    umodel_export_dir: bpy.props.StringProperty(
        name="Export Directory",
        description="Path to the export directory with game assets",
        subtype='DIR_PATH',
        update=_make_abs_update("umodel_export_dir"),
    )

    asset_dir: bpy.props.StringProperty(
        name="Asset Directory",
        description="Path to the directory where the assets for current project are stored",
        subtype='DIR_PATH',
        update=_make_abs_update("asset_dir"),
    )


class UMODELTOOLS_UL_game_profiles(bpy.types.UIList):
    """UIlist for displaying game profiles."""

    def draw_item(self,
                  _context: bpy.types.Context,
                  layout: bpy.types.UILayout,
                  _prefs: 'UMODELTOOLS_AP_addon_preferences',
                  game_profile: UMODELTOOLS_PG_game_profile,
                  icon: str,
                  _active_prefs: 'UMODELTOOLS_AP_addon_preferences',
                  _active_propname: str,
                  _index: int,
                  _flt_flag: int):
        layout.prop(game_profile, "name", text="", emboss=False, icon_value=icon)


class UMODELTOOLS_OT_actions(bpy.types.Operator):
    """Move items up and down, add and remove"""

    bl_idname = "umodel_tools.list_action"
    bl_label = "List Actions"
    bl_description = "Move items up and down, add and remove"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    action: bpy.props.EnumProperty(
        items=(
            ('UP', "Up", ""),
            ('DOWN', "Down", ""),
            ('REMOVE', "Remove", ""),
            ('ADD', "Add", "")
        )
    )

    def invoke(self, _context: bpy.types.Context, _event: bpy.types.Event) -> set[str]:
        addon_prefs = get_addon_preferences()
        idx = addon_prefs.active_profile_index

        try:
            addon_prefs.profiles[idx]
        except IndexError:
            pass
        else:
            if self.action == 'DOWN' and idx < len(addon_prefs.profiles) - 1:
                addon_prefs.profiles.move(idx, idx + 1)
                addon_prefs.active_profile_index += 1

            elif self.action == 'UP' and idx >= 1:
                addon_prefs.profiles.move(idx, idx - 1)
                addon_prefs.active_profile_index -= 1

            elif self.action == 'REMOVE':
                addon_prefs.profiles.remove(idx)
                if addon_prefs.active_profile_index != 0:
                    addon_prefs.active_profile_index -= 1

        if self.action == 'ADD':
            profile = addon_prefs.profiles.add()
            profile.name = "New Profile"
            addon_prefs.active_profile_index = len(addon_prefs.profiles) - 1

        return {"FINISHED"}


class UMODELTOOLS_AP_addon_preferences(bpy.types.AddonPreferences):
    """Implements preferences storage for the addon.
    """

    bl_idname = PACKAGE_NAME

    profiles: bpy.props.CollectionProperty(
        name="Profiles",
        description="Saved game profiles",
        type=UMODELTOOLS_PG_game_profile
    )

    active_profile_index: bpy.props.IntProperty(
        default=0
    )

    display_cur_profile: bpy.props.BoolProperty(
        name="Display current profile",
        description="Display current profile on top of Blender's window",
        default=True
    )

    verbose: bpy.props.BoolProperty(
        name="Verbose import",
        description="Print detailed logging information on import",
        default=False
    )

    debug: bpy.props.BoolProperty(
        name="Debug",
        description="Enables debugging output, intended for developers only",
        default=False
    )

    def get_active_profile(self) -> t.Optional[UMODELTOOLS_PG_game_profile]:
        try:
            return self.profiles[self.active_profile_index]
        except IndexError:
            return None

    def draw(self, context: bpy.types.Context):
        layout = self.layout
        layout.prop(self, "verbose")

        if context.preferences.view.show_developer_ui:
            layout.prop(self, "debug")
