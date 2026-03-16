import os
import sys
import typing as t
import tempfile
import contextlib

import bpy
import tqdm

from mathutils import Vector
from . import preferences


tmphandle, tmppath = tempfile.mkstemp()
#: Determines whether the OS's filesystem is case sensitive or not
FS_CASE_INSENSITIVE = os.path.exists(tmppath.upper())
os.close(tmphandle)
os.remove(tmppath)


def copy_object(obj: bpy.types.Object) -> bpy.types.Object:
    """Copies an object and its mesh. No linking is performed.

    :param obj: Blender object.
    :return: Copied object.
    """
    copied_obj = obj.copy()
    copied_obj.data = obj.data.copy()
    return copied_obj


def compare_meshes(first: bpy.types.Mesh, second: bpy.types.Mesh) -> bool:
    """Compare two meshes on basic geometric similarity.

    :param first: First mesh.
    :param second: Second mesh.
    :return: Returns True if number of vertices, edges, faces and loops is equal.
    """

    return (len(first.vertices) == len(second.vertices)
            and len(first.polygons) == len(second.polygons)
            and len(first.loops) == len(second.loops)
            and len(first.edges) == len(second.edges))


def compare_paths(first: str, second: str) -> bool:
    """Compares that to paths are identical. Respects OS case sensitivity rules for the filesystem.

    :param first: First path.
    :param second: Second path.
    :return: True if paths are identical, else False.
    """
    first = os.path.realpath(first)
    second = os.path.realpath(second)

    return (first.lower() == second.lower()) if FS_CASE_INSENSITIVE else (first == second)


DataBlock: t.TypeAlias = bpy.types.Object | bpy.types.Material | bpy.types.Image


def linked_libraries_search(lib_filepath: str, dtype: t.Type[DataBlock]) -> t.Optional[DataBlock]:
    """Check already linked libraries for the associated data block and return it.

    :param lib_filepath: Filepath of the library.
    :param dtype: Datablock type.
    :return: None or data-block (if found).
    """

    for lib in bpy.data.libraries:
        if compare_paths(lib.filepath, lib_filepath):
            for id_data in lib.users_id:
                if isinstance(id_data, dtype):
                    return id_data

    return None


def verbose_print(*args: t.Any):
    """Prints to stdout, if addon has verbose setting enabled.

    :args: Arguments to internal print() call.
    """
    if preferences.get_addon_preferences().verbose:
        print(*args)


@contextlib.contextmanager
def std_out_err_redirect_tqdm():
    """Redirect stdout and stderr for tqdm.
    """
    orig_out_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = map(tqdm.contrib.DummyTqdmFile, orig_out_err)
        yield orig_out_err[0]
    # Relay exceptions
    except Exception as exc:
        raise exc
    # Always restore sys.stdout/err if necessary
    finally:
        sys.stdout, sys.stderr = orig_out_err


@contextlib.contextmanager
def redirect_cstdout(to=os.devnull):
    """Redirect stdout from C/C++ parts of Blender and external libaries.
    We use this to suppress library reading and linking messages.

    :param to: _description_, defaults to os.devnull
    :yield: _description_
    """

    # disable the whole redirect in debug mode
    if preferences.get_addon_preferences().debug:
        yield
        return None

    fd = sys.stdout.fileno()

    def _redirect_stdout(to):
        os.dup2(to.fileno(), fd)  # fd writes to 'to' file

    with os.fdopen(os.dup(fd), 'w') as old_stdout:
        with open(to, 'w') as file:  # pylint: disable=unspecified-encoding
            _redirect_stdout(to=file)
        try:
            yield  # allow code to be run with the redirected stdout
        finally:
            _redirect_stdout(to=old_stdout)  # restore stdout

    return None

def get_selected_vertex_world_bounds():
    obj = bpy.context.object

    if not obj or obj.type != 'MESH':
        return None

    mesh = obj.data
    mat = obj.matrix_world

    xs, ys, zs = [], [], []

    # Edit mode: support selected verts / edges / faces from the live BMesh.
    if obj.mode == 'EDIT':
        import bmesh
        bm = bmesh.from_edit_mesh(mesh)
        selected_verts = {v for v in bm.verts if v.select}
        for e in bm.edges:
            if e.select:
                selected_verts.update(e.verts)
        for f in bm.faces:
            if f.select:
                selected_verts.update(f.verts)

        for v in selected_verts:
            wp = mat @ v.co
            xs.append(wp.x)
            ys.append(wp.y)
            zs.append(wp.z)
    else:
        for v in mesh.vertices:
            if v.select:
                wp = mat @ v.co
                xs.append(wp.x)
                ys.append(wp.y)
                zs.append(wp.z)

    if not xs:
        return None

    return {
        "min_x": min(xs),
        "max_x": max(xs),
        "min_y": min(ys),
        "max_y": max(ys),
        "min_z": min(zs),
        "max_z": max(zs),
    }


def get_active_import_bound(scene):
    bounds = getattr(scene, "umodel_import_bounds", None)
    if bounds is None or len(bounds) == 0:
        return None

    idx = int(getattr(scene, "umodel_import_bounds_index", -1))
    if idx < 0 or idx >= len(bounds):
        return None
    return bounds[idx]


def apply_active_import_bound_to_scene(scene) -> bool:
    bound = get_active_import_bound(scene)
    if bound is None:
        return False

    scene.umodel_min_x = float(bound.min_x)
    scene.umodel_max_x = float(bound.max_x)
    scene.umodel_min_y = float(bound.min_y)
    scene.umodel_max_y = float(bound.max_y)
    return True

from mathutils import Vector

def static_mesh_has_instance_in_bounds(static_mesh) -> bool:
    from .map_importer import is_within_import_bounds

    trs = static_mesh.transform

    # ---------- Non-instanced ----------
    if not static_mesh.is_instanced:
        if static_mesh.parent_mtx is None:
            pos = Vector(trs.pos)
        else:
            pos = (static_mesh.parent_mtx @ trs.matrix_4x4).to_translation()

        return is_within_import_bounds(pos)

    # ---------- Instanced ----------
    for inst_trs in static_mesh.instance_transforms:
        mat = trs.matrix_4x4 @ inst_trs.matrix_4x4

        if static_mesh.parent_mtx is not None:
            mat = static_mesh.parent_mtx @ mat

        pos = mat.to_translation()

        if is_within_import_bounds(pos):
            return True

    return False

def _profile_feature_enabled(feature_name: str) -> bool:
    prefs = preferences.get_addon_preferences()
    profile = prefs.get_active_profile() if prefs else None
    if profile is None:
        return False
    impl = getattr(__import__("umodel_tools.game_profiles", fromlist=['GAME_HANDLERS']), 'GAME_HANDLERS', {}).get(profile.game)
    return bool(getattr(impl, feature_name, False)) if impl else False

def _resolve_dir_path(value: str) -> str:
    """Resolve Blender-style paths (including //) to a normalized absolute OS path."""
    if not value:
        return ""
    value = bpy.path.abspath(value)
    value = os.path.normpath(os.path.abspath(value))
    # Blender on Windows can yield paths like "\\C:\\..." or "/C:/..."; strip that leading slash.
    if os.name == 'nt' and len(value) >= 3 and value[0] in ('/', '\\') and value[1].isalpha() and value[2] == ':':
        value = value[1:]
    return value

