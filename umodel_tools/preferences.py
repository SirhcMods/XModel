import typing as t

import bpy
import os
from pathlib import Path


from . import PACKAGE_NAME
from . import game_profiles


def _make_abs_update(prop_name: str):
    """Create an update callback that forces a directory property to absolute."""

    def _upd(self, _context):
        # Guard against recursion
        if getattr(self, "_umodeltools_path_update_lock", False):
            return

        cur = getattr(self, prop_name, "") or ""
        if not cur:
            return

        from umodel_tools.utils import _resolve_dir_path
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


def get_profile_store_dir() -> Path:
    """Return the external profile storage directory in the user's Documents/XModel folder."""
    return Path.home() / "Documents" / "XModel"


def _sanitize_profile_filename(name: str) -> str:
    name = (name or "New Profile").strip() or "New Profile"
    invalid = '<>:"/\\|?*'
    return "".join("_" if ch in invalid else ch for ch in name)


def _profile_file_path(profile_name: str) -> Path:
    return get_profile_store_dir() / f"{_sanitize_profile_filename(profile_name)}_profile.txt"


def _sync_scene_filter_from_active_profile(context: t.Optional[bpy.types.Context] = None) -> None:
    ctx = context or bpy.context
    scene = getattr(ctx, "scene", None)
    if scene is None or not hasattr(scene, "umodel_asset_path_filter"):
        return

    addon_prefs = get_addon_preferences()
    profile = addon_prefs.get_active_profile()
    scene.umodel_asset_path_filter = getattr(profile, "asset_path_filter", "") if profile else ""


def _update_profile_asset_filter(self, context):
    scene = getattr(context, "scene", None) if context is not None else getattr(bpy.context, "scene", None)
    if scene is None or not hasattr(scene, "umodel_asset_path_filter"):
        return

    addon_prefs = get_addon_preferences()
    active_profile = addon_prefs.get_active_profile()
    if active_profile and active_profile.as_pointer() == self.as_pointer():
        scene.umodel_asset_path_filter = self.asset_path_filter


def _update_active_profile_index(self, context):
    _sync_scene_filter_from_active_profile(context)


def save_profile_to_disk(profile: 'UMODELTOOLS_PG_game_profile') -> Path:
    store_dir = get_profile_store_dir()
    store_dir.mkdir(parents=True, exist_ok=True)

    profile_name = (profile.name or "New Profile").strip() or "New Profile"
    profile_path = _profile_file_path(profile_name)

    lines = [
        f"ProfileName={profile_name}",
        f"Game={profile.game or ''}",
        f"ExportDirectory={profile.umodel_export_dir or ''}",
        f"AssetDirectory={profile.asset_dir or ''}",
        f"ImportFilterDirectory={profile.asset_path_filter or ''}",
    ]
    profile_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return profile_path


def _parse_profile_file(profile_path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        for raw_line in profile_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    except Exception:
        return {}
    return data


def load_profiles_from_disk() -> int:
    addon_prefs = get_addon_preferences()
    store_dir = get_profile_store_dir()
    if not store_dir.exists():
        return 0

    addon_prefs.profiles.clear()
    loaded_count = 0
    active_index = 0

    for profile_path in sorted(store_dir.glob("*_profile.txt"), key=lambda p: p.name.lower()):
        data = _parse_profile_file(profile_path)
        if not data:
            continue

        profile = addon_prefs.profiles.add()
        profile.name = data.get("ProfileName", profile_path.stem.replace("_profile", "")) or "New Profile"

        game_id = data.get("Game", "")
        supported_game_ids = {item[0] for item in game_profiles.SUPPORTED_GAMES}
        if game_id in supported_game_ids:
            profile.game = game_id

        from umodel_tools.utils import _resolve_dir_path
        profile.umodel_export_dir = _resolve_dir_path(data.get("ExportDirectory", "")) if data.get("ExportDirectory") else ""
        profile.asset_dir = _resolve_dir_path(data.get("AssetDirectory", "")) if data.get("AssetDirectory") else ""
        profile.asset_path_filter = data.get("ImportFilterDirectory", "") or ""

        loaded_count += 1

    if loaded_count > 0:
        addon_prefs.active_profile_index = min(active_index, loaded_count - 1)
        _sync_scene_filter_from_active_profile()
    else:
        addon_prefs.active_profile_index = 0

    return loaded_count


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

    asset_path_filter: bpy.props.StringProperty(
        name="Import Filter Directory",
        description="Only import meshes whose Unreal asset path starts with this prefix",
        subtype='DIR_PATH',
        default="",
        update=_update_profile_asset_filter,
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
    bl_label = "Actions"
    bl_description = "Move profiles up and down, add and remove, or save profile"
    bl_options = {'REGISTER', 'INTERNAL', 'UNDO'}

    action: bpy.props.EnumProperty(
        items=(
            ('UP', "Up", ""),
            ('DOWN', "Down", ""),
            ('REMOVE', "Remove", ""),
            ('ADD', "Add", ""),
            ('SAVE', "Save", "")
        )
    )

    def invoke(self, context: bpy.types.Context, _event: bpy.types.Event) -> set[str]:
        addon_prefs = get_addon_preferences()
        idx = addon_prefs.active_profile_index

        try:
            active_profile = addon_prefs.profiles[idx]
        except IndexError:
            active_profile = None
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
                _sync_scene_filter_from_active_profile(context)

            elif self.action == 'SAVE' and active_profile is not None:
                active_profile.asset_path_filter = getattr(context.scene, "umodel_asset_path_filter", "") or ""
                profile_path = save_profile_to_disk(active_profile)
                self.report({'INFO'}, f"Saved profile to {profile_path}")

        if self.action == 'ADD':
            profile = addon_prefs.profiles.add()
            profile.name = "New Profile"
            addon_prefs.active_profile_index = len(addon_prefs.profiles) - 1
            _sync_scene_filter_from_active_profile(context)

        elif self.action in {'UP', 'DOWN'}:
            _sync_scene_filter_from_active_profile(context)

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
        default=0,
        update=_update_active_profile_index
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
