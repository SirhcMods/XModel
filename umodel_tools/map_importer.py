import json
import math
import os
import typing as t
import enum
import re
import functools

import mathutils as mu
import bpy
import tqdm

from . import asset_db
from .ops import override_materials as override_ops
from . import asset_importer
from . import utils
from . import fmodel_json_parser
from . import game_profiles
from . import color_palette_unwrapper  # MindsEye palette tint support (optional)
from .utils import static_mesh_has_instance_in_bounds


def split_object_path(object_path):
    # For some reason ObjectPaths end with a period and a digit.
    # This is kind of a sucky way to split that out.

    path_parts = object_path.split(".")

    if len(path_parts) > 1:
        # Usually works, but will fail If the path contains multiple periods.
        return path_parts[0]

    # Nothing to do
    return object_path


_RE_TRAILING_OBJPATH_DOTNUM = re.compile(r"\.(\d+)$")  # ends with .0/.1/etc



def _is_palette_unwrapper_enabled(game_profile) -> bool:
    """True if the active game profile enables MindsEye palette tint unwrapper.

    In most call sites `game_profile` is the profile key string (e.g. 'mindseye')."""
    impl = game_profiles.GAME_HANDLERS.get(game_profile) if isinstance(game_profile, str) else game_profile
    return bool(getattr(impl, 'ENABLE_COLOR_PALETTE_UNWRAPPER', False) or getattr(impl, 'use_color_palette_unwrapper', False))

def strip_objectpath_trailing_dotnum(object_path: str) -> str:
    """Strip trailing ".<digits>" from UE ObjectPath while preserving inner periods."""
    if not object_path:
        return object_path
    m = _RE_TRAILING_OBJPATH_DOTNUM.search(object_path)
    if m:
        return object_path[:m.start()]
    return object_path


def strip_ue_quoted_name(s: str) -> str:
    """Extract the inner name from UE formatted ObjectName strings.

    Examples:
      "MaterialInstanceConstant'MI_Name'" -> "MI_Name"
      "StaticMesh'SM_Foo'" -> "SM_Foo"
    """
    if not s:
        return ""
    if "'" in s:
        parts = s.split("'")
        if len(parts) >= 2:
            return parts[1].strip()
    return s.strip()


def _extract_ref_path(value: t.Any) -> str:
    """Extract an Unreal-style object path from common FModel reference shapes."""
    if not value:
        return ""

    # Prefer the parser's normalization / common-case extraction.
    try:
        p = fmodel_json_parser._extract_object_path(value)  # type: ignore[attr-defined]
    except Exception:
        p = None
    if isinstance(p, str) and p:
        return p

    # Extra nested cases seen in some exports.
    if isinstance(value, dict):
        op = value.get("ObjectPath")
        if isinstance(op, dict):
            apn = op.get("AssetPathName")
            if isinstance(apn, str) and apn:
                try:
                    return fmodel_json_parser._normalize_object_path(apn)  # type: ignore[attr-defined]
                except Exception:
                    return apn
        apn = value.get("AssetPathName")
        if isinstance(apn, str) and apn:
            try:
                return fmodel_json_parser._normalize_object_path(apn)  # type: ignore[attr-defined]
            except Exception:
                return apn

    return ""


def _extract_ref_name(value: t.Any) -> str:
    """Extract the short asset name (MI_*, SM_*) from common FModel reference shapes."""
    if not value:
        return ""
    if isinstance(value, str):
        return strip_ue_quoted_name(value)

    if isinstance(value, dict):
        for k in ("ObjectName", "Name", "AssetName"):
            v = value.get(k)
            if isinstance(v, str) and v:
                return strip_ue_quoted_name(v)

        # If only a path is present, derive the name.
        p = _extract_ref_path(value)
        if p:
            p2 = strip_objectpath_trailing_dotnum(p)
            return p2.rsplit('/', 1)[-1]

    return ""


def _timer_post_import_reload_and_reapply(collection_name: str,
                                         umodel_export_dir: str,
                                         asset_dir: str,
                                         game_profile: str,
                                         apply_override_materials: bool = True) -> t.Optional[float]:

    # MindsEye: per-instance palette tint ids extracted from PerInstanceSMCustomData (optional)
    per_instance_tint_ids: t.Optional[list[list[int]]] = None
    per_instance_packet_width: int = 0
    """Timer callback to reload libraries and re-apply OverrideMaterials.

    IMPORTANT: This must NOT capture an operator instance.
    Some call sites execute MapImporter methods on a bpy.types.Operator subclass via
    multiple inheritance. Blender frees operator StructRNA right after execution, and
    timer callbacks would crash if they reference `self`.
    """
    try:
        helper = MapImporter()
        return helper._post_import_reload_and_reapply(
            collection_name,
            umodel_export_dir,
            asset_dir,
            game_profile,
            apply_override_materials=apply_override_materials
        )
    except Exception as e:
        # Don't crash the timer loop; just log.
        utils.verbose_print(f"OverrideMaterials post-import timer failed: {e}")
        return None


def parse_ue_object_name(obj_name: str) -> tuple[str, str, str]:
    obj_type, obj_path, _ = obj_name.split('\'')
    _, obj_path = obj_path.split(':')

    names = obj_path.split('.')
    assert len(names) >= 2

    return obj_type, names[-2], names[-1]

def is_within_import_bounds(pos):
    scene = bpy.context.scene
    if not scene.umodel_use_vertex_bounds:
        return True

    return (
        scene.umodel_min_x <= pos.x <= scene.umodel_max_x and
        scene.umodel_min_y <= pos.y <= scene.umodel_max_y
    )

class InstanceTransform:
    pos: tuple[float, float, float]
    rot_euler: tuple[float, float, float]
    scale: tuple[float, float, float]

    def __init__(self,
                 pos: tuple[float, float, float] = (0.0, 0.0, 0.0),
                 rot_euler: tuple[float, float, float] = (0.0, 0.0, 0.0),
                 scale: tuple[float, float, float] = (1.0, 1.0, 1.0)) -> None:
        self.pos = pos
        self.rot_euler = rot_euler
        self.scale = scale

    @property
    def matrix_4x4(self) -> mu.Matrix:
        return mu.Matrix.LocRotScale(mu.Vector(self.pos),
                                     mu.Euler(self.rot_euler, 'XYZ'),
                                     mu.Vector(self.scale))


def get_parent_transform_matrix(json_obj,
                                obj_type: str,
                                obj_outer: str,
                                obj_name: str) -> mu.Matrix:

    for entity in json_obj:
        if (((entity_type := entity.get("Type", None)) is None or entity_type != obj_type)
           or ((entity_outer := entity.get("Outer", None)) is None or entity_outer != obj_outer)
           or ((entity_name := entity.get("Name", None)) is None or entity_name != obj_name)):
            continue

        props = entity.get("Properties", None)
        if props is None:
            return InstanceTransform().matrix_4x4

        # obtain the parent's relative matrix
        trs = InstanceTransform()

        if (pos := props.get("RelativeLocation", None)) is not None:
            trs.pos = [pos.get("X") / 100, pos.get("Y") / -100, pos.get("Z") / 100]

        if (scale := props.get("RelativeScale3D", None)) is not None:
            trs.scale = [scale.get("X", 1), scale.get("Y", 1), scale.get("Z", 1)]

        match obj_type:
            case 'SpotLightComponent' | 'PointLightComponent':
                if (rot := props.get("RelativeRotation", None)) is not None:
                    trs.rot_euler = (rot.get("Roll") + 90,
                                     -rot.get("Pitch") - 90,
                                     rot.get("Yaw"))
            case _:
                if (rot := props.get("RelativeRotation", None)) is not None:
                    trs.rot_euler = (math.radians(rot.get("Roll")),
                                     math.radians(-rot.get("Pitch")),
                                     math.radians(-rot.get("Yaw")))

        # obtain the parent's parent transform
        if ((parent := props.get("AttachParent", None)) is not None
           and (obj_name := parent.get("ObjectName", None)) is not None):
            return get_parent_transform_matrix(json_obj, *parse_ue_object_name(obj_name)) @ trs.matrix_4x4

        # return the absolute transform if no parent
        return trs.matrix_4x4

    return InstanceTransform().matrix_4x4


class StaticMesh:
    static_mesh_types = [
        'StaticMeshComponent',
        'InstancedStaticMeshComponent',
        'HierarchicalInstancedStaticMeshComponent'
    ]

    entity_name: str = ""
    asset_path: str = ""
    transform: InstanceTransform
    instance_transforms: list[InstanceTransform]
    parent_mtx: t.Optional[mu.Matrix] = None

    # Per-component material overrides (slot-index aligned):
    # list entries are either None (no override) or (material_name, material_object_path)
    override_materials: t.Optional[list[t.Optional[tuple[str, str]]]] = None

    # these are just properties to help with debugging
    no_entity: bool = False
    no_mesh: bool = False
    no_path: bool = False
    no_per_instance_data: bool = False
    base_shape: bool = False
    is_instanced: bool = False
    not_rendered: bool = False
    invisible: bool = False

    def __init__(self, json_obj: t.Any, json_entity: t.Any, entity_type: str) -> None:
        self.entity_name = json_entity.get("Outer", 'Error')
        self.instance_transforms = []

        if not (props := json_entity.get("Properties", None)):
            self.no_entity = True
            return

        # Capture per-component OverrideMaterials (if any). This is per-instance data.
        self.override_materials = None
        if (override_list := props.get("OverrideMaterials", None)) is not None and isinstance(override_list, list):
            norm: list[t.Optional[tuple[str, str]]] = []
            for entry in override_list:
                if entry is None or not isinstance(entry, dict):
                    norm.append(None)
                    continue
                mat_name = strip_ue_quoted_name(entry.get("ObjectName", ""))
                mat_path = entry.get("ObjectPath", "")
                if not mat_name or not mat_path:
                    norm.append(None)
                    continue
                norm.append((mat_name, mat_path))
            self.override_materials = norm

        if not props.get("StaticMesh", None):
            self.no_mesh = True
            return

        if not (object_path := props.get("StaticMesh").get("ObjectPath", None)) or object_path == '':
            self.no_path = True
            return

        # Keep original object path around so we can reconstruct base materials later if
        # Blender reload wipes linked slot pointers.
        # Example: "/MindsEye/Content/.../SM_Foo.0"
        self.mesh_object_path = object_path

        if 'BasicShapes' in object_path:
            # What is a BasicShape? Do we need these?
            self.base_shape = True
            return

        if (render_in_main_pass := props.get("bRenderInMainPass", None)) is not None and not render_in_main_pass:
            self.not_rendered = True
            return

        if (is_visbile := props.get("bVisible", None)) is not None and not is_visbile:
            self.invisible = True

        if ((parent := props.get("AttachParent", None)) is not None
           and (obj_name := parent.get("ObjectName", None)) is not None):
            self.parent_mtx = get_parent_transform_matrix(json_obj, *parse_ue_object_name(obj_name))

        objpath = split_object_path(object_path)

        self.asset_path = os.path.normpath(objpath + ".uasset")
        self.asset_path = self.asset_path[1:] if self.asset_path.startswith(os.sep) else self.asset_path

        match entity_type:
            case 'StaticMeshComponent':
                trs = InstanceTransform()

                if (pos := props.get("RelativeLocation", None)) is not None:
                    trs.pos = (pos.get("X") / 100, pos.get("Y") / -100, pos.get("Z") / 100)

                if (rot := props.get("RelativeRotation", None)) is not None:
                    trs.rot_euler = (math.radians(rot.get("Roll")),
                                     math.radians(-rot.get("Pitch")),
                                     math.radians(-rot.get("Yaw")))

                if (scale := props.get("RelativeScale3D", None)) is not None:
                    trs.scale = (scale.get("X", 1), scale.get("Y", 1), scale.get("Z", 1))

                self.transform = trs

            case 'InstancedStaticMeshComponent' | 'HierarchicalInstancedStaticMeshComponent':
                self.is_instanced = True

                if (instances := json_entity.get("PerInstanceSMData", None)) is None:
                    self.no_per_instance_data = True
                    return

                trs = InstanceTransform()

                if (pos := props.get("RelativeLocation", None)) is not None:
                    trs.pos = (pos.get("X") / 100, pos.get("Y") / -100, pos.get("Z") / 100)

                if (rot := props.get("RelativeRotation", None)) is not None:
                    trs.rot_euler = (math.radians(rot.get("Roll")),
                                     math.radians(-rot.get("Pitch")),
                                     math.radians(-rot.get("Yaw")))

                if (scale := props.get("RelativeScale3D", None)) is not None:
                    trs.scale = (scale.get("X", 1), scale.get("Y", 1), scale.get("Z", 1))

                self.transform = trs

                for instance in instances:
                    trs = InstanceTransform()

                    if (trs_data := instance.get("TransformData", None)) is not None:
                        if (pos := trs_data.get("Translation", None)) is not None:
                            trs.pos = (pos.get("X") / 100, pos.get("Y") / -100, pos.get("Z") / 100)

                        if (rot := trs_data.get("Rotation", None)) is not None:
                            rot_quat = mu.Quaternion((rot.get("W"), rot.get("X"), rot.get("Y"), rot.get("Z")))
                            quat_to_euler: mu.Euler = rot_quat.to_euler()  # pylint: disable=no-value-for-parameter
                            trs.rot_euler = (-quat_to_euler.x, quat_to_euler.y, -quat_to_euler.z)

                        if (scale := trs_data.get("Scale3D", None)) is not None:
                            trs.scale = (scale.get("X", 1), scale.get("Y", 1), scale.get("Z", 1))

                    self.instance_transforms.append(trs)

                # MindsEye optional: extract per-instance palette tint ids from PerInstanceSMCustomData
                try:
                    width = None
                    ncf = props.get('NumCustomDataFloats', None) if isinstance(props, dict) else None
                    if isinstance(ncf, (int, float)):
                        width = int(ncf)
                    custom_flat = json_entity.get('PerInstanceSMCustomData', None)
                    if custom_flat is None and isinstance(props, dict):
                        custom_flat = props.get('PerInstanceSMCustomData', None)
                    if width and isinstance(custom_flat, list) and custom_flat:
                        self.per_instance_packet_width = width
                        packets = color_palette_unwrapper.chunk_custom_data(custom_flat, len(instances), width)
                        self.per_instance_tint_ids = [color_palette_unwrapper.extract_tint_ids_from_packet(p, width) for p in packets]
                except Exception:
                    self.per_instance_tint_ids = None

    @property
    def invalid(self) -> bool:
        return (self.no_path or self.no_entity or self.base_shape or self.no_mesh or self.no_per_instance_data
                or self.not_rendered or self.invisible)

    def link_object_instance(self,
                             importer: "MapImporter",
                             obj: bpy.types.Object,
                             collection: bpy.types.Collection,
                             umodel_export_dir: str,
                             asset_dir: str,
                             game_profile: str,
                             db: t.Optional[asset_db.AssetDB] = None) -> list[bpy.types.Object]:
        if self.invalid:
            print(f'Refusing to import {self.entity_name} due to failed checks.')
            return []

        objects = []
        trs = self.transform

        if self.is_instanced:
            for _inst_idx, instance_trs in enumerate(self.instance_transforms):
                mat_world = trs.matrix_4x4 @ instance_trs.matrix_4x4
                new_obj = bpy.data.objects.new(obj.name, object_data=obj.data)
                new_obj.rotation_mode = 'XYZ'

                # MindsEye palette tint support: persist instance identity + tint props
                use_unwrapper = _is_palette_unwrapper_enabled(game_profile)
                if use_unwrapper:
                    try:
                        new_obj['_umodel_source_outer'] = self.entity_name
                        new_obj['_umodel_instance_index'] = int(_inst_idx)
                    except Exception:
                        pass
                    try:
                        if self.per_instance_tint_ids and _inst_idx < len(self.per_instance_tint_ids):
                            color_palette_unwrapper.store_tint_custom_props(new_obj, self.per_instance_tint_ids[_inst_idx], packet_width=self.per_instance_packet_width)
                    except Exception:
                        pass

                # Persist mesh object path for base-material reconstruction.
                try:
                    new_obj["_umodel_mesh_object_path"] = getattr(self, "mesh_object_path", "")
                except Exception:
                    pass

                if self.parent_mtx is None:
                    new_obj.matrix_world = mat_world
                else:
                    new_obj.matrix_world = self.parent_mtx @ mat_world

                collection.objects.link(new_obj)
                # Persist base slot names/material names for post-reload repair.
                importer._persist_base_material_slots(new_obj)
                # Some meshes arrive with blank/nameless slots even without OverrideMaterials so we repair them
                importer._repair_base_material_slots(
                    new_obj,
                    umodel_export_dir=umodel_export_dir,
                    asset_dir=asset_dir,
                    game_profile=game_profile,
                    db=db,
                    force_reassign=True
                )
                if getattr(importer, 'apply_override_materials', True):
                    importer._apply_override_materials_to_object(
                        new_obj,
                        self.override_materials,
                        umodel_export_dir=umodel_export_dir,
                        asset_dir=asset_dir,
                        game_profile=game_profile,
                        db=db
                    )
                objects.append(new_obj)

        else:
            new_obj = bpy.data.objects.new(obj.name, object_data=obj.data)

            # Persist mesh object path for base-material reconstruction.
            try:
                new_obj["_umodel_mesh_object_path"] = getattr(self, "mesh_object_path", "")
            except Exception:
                pass

            if self.parent_mtx is None:
                new_obj.scale = (trs.scale[0], trs.scale[1], trs.scale[2])
                new_obj.location = (trs.pos[0], trs.pos[1], trs.pos[2])
                new_obj.rotation_mode = 'XYZ'
                new_obj.rotation_euler = mu.Euler((trs.rot_euler[0], trs.rot_euler[1], trs.rot_euler[2]), 'XYZ')
            else:
                new_obj.matrix_world = self.parent_mtx @ trs.matrix_4x4

            collection.objects.link(new_obj)
            # Persist base slot names/material names for post-reload repair.
            importer._persist_base_material_slots(new_obj)
            importer._repair_base_material_slots(
                new_obj,
                umodel_export_dir=umodel_export_dir,
                asset_dir=asset_dir,
                game_profile=game_profile,
                db=db,
                force_reassign=True
            )
            if getattr(importer, 'apply_override_materials', True):
                importer._apply_override_materials_to_object(
                    new_obj,
                    self.override_materials,
                    umodel_export_dir=umodel_export_dir,
                    asset_dir=asset_dir,
                    game_profile=game_profile,
                    db=db
                )
            objects.append(new_obj)


        # MindsEye: final tint relink pass (only when enabled)
        use_unwrapper = _is_palette_unwrapper_enabled(game_profile)
        if use_unwrapper:
            try:
                color_palette_unwrapper.relink_tinted_materials_by_tint_id(objects)
            except Exception:
                pass
        return objects


class GameLight:

    #: all entity types this reader supports
    light_types = [
        'SpotLightComponent',
        'RectLightComponent',
        'PointLightComponent'
    ]

    #: maps UE light types to Blender's light types
    light_type_mapping = {
        'SpotLightComponent': 'SPOT',
        'RectLightComponent': 'AREA',
        'PointLightComponent': 'POINT'
    }

    class IntensityUnits(enum.Enum):
        """All light intensity units supported by UE lights.
        """
        Unitless = enum.auto()
        Candelas = enum.auto()
        Lumens = enum.auto()

    #: maps intensity unit type json values to the enum
    light_intensity_units_mapping = {
        'ELightUnits::Unitless': IntensityUnits.Unitless,
        'ELightUnits::Candelas': IntensityUnits.Candelas,
        'ELightUnits::Lumens': IntensityUnits.Lumens
    }

    type: str = ""

    entity_name: str = ""
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rot: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    parent_mtx: t.Optional[mu.Matrix] = None
    intensity: float = math.pi
    intensity_units: IntensityUnits = IntensityUnits.Unitless
    cone_angle: float
    inner_cone_angle: float = 0.0
    cast_shadows: bool = False
    source_radius: bool = 0.0
    attenuation_radius: float = 0.0
    source_width: float = 0.0
    source_height: float = 0.0

    no_entity = False

    color_temp_table_r = [
        [2.52432244e+03, -1.06185848e-03, 3.11067539e+00],
        [3.37763626e+03, -4.34581697e-04, 1.64843306e+00],
        [4.10671449e+03, -8.61949938e-05, 6.41423749e-01],
        [4.66849800e+03, 2.85655028e-05, 1.29075375e-01],
        [4.60124770e+03, 2.89727618e-05, 1.48001316e-01],
        [3.78765709e+03, 9.36026367e-06, 3.98995841e-01],
    ]

    color_temp_table_g = [
        [-7.50343014e+02, 3.15679613e-04, 4.73464526e-01],
        [-1.00402363e+03, 1.29189794e-04, 9.08181524e-01],
        [-1.22075471e+03, 2.56245413e-05, 1.20753416e+00],
        [-1.42546105e+03, -4.01730887e-05, 1.44002695e+00],
        [-1.18134453e+03, -2.18913373e-05, 1.30656109e+00],
        [-5.00279505e+02, -4.59745390e-06, 1.09090465e+00],
    ]

    color_temp_table_b = [
        [0.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0],
        [-2.02524603e-11, 1.79435860e-07, -2.60561875e-04, -1.41761141e-02],
        [-2.22463426e-13, -1.55078698e-08, 3.81675160e-04, -7.30646033e-01],
        [6.72595954e-13, -2.73059993e-08, 4.24068546e-04, -7.52204323e-01],
    ]

    @staticmethod
    def temp_to_color(temp: float) -> tuple[float, float, float]:
        """Convert kelvin temperature to lamp color

        :param temp: Temperature in Kelvin.
        :return: Color.
        """
        if temp >= 12000.0:
            return (0.826270103, 0.994478524, 1.56626022)
        if temp < 965.0:
            return (4.70366907, 0.0, 0.0)

        i = 0
        if temp >= 6365.0:
            i = 5
        elif temp >= 3315.0:
            i = 4
        elif temp >= 1902.0:
            i = 3
        elif temp >= 1449.0:
            i = 2
        elif temp >= 1167.0:
            i = 1
        else:
            i = 0

        r = GameLight.color_temp_table_r[i]
        g = GameLight.color_temp_table_g[i]
        b = GameLight.color_temp_table_b[i]

        temp_inv = 1 / temp
        return (r[0] * temp_inv + r[1] * temp + r[2],
                g[0] * temp_inv + g[1] * temp + g[2],
                ((b[0] * temp + b[1]) * temp + b[2]) * temp + b[3])

    @staticmethod
    def quaternion_to_euler(quaternion: mu.Quaternion) -> tuple[float, float, float]:
        w, y, x, z = quaternion
        roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
        pitch = math.asin(max(min(2 * (w * y - z * x), 1), -1))
        yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
        return roll, pitch, yaw

    @staticmethod
    def normalize_rotation(x, y, z) -> tuple[float, float, float]:
        """
        Convert rotation from UE's coordinate system to Blender's coordinate system.
        This code seems to be specific for lights.

        :param x: X component.
        :param y: Y component.
        :param z: Z component.
        :return: Euler angle as tuple in Blender's coordinate space.
        """

        euler = mu.Euler((
            math.radians(x),
            math.radians(y),
            math.radians(z)
        ))

        quat = euler.to_quaternion()  # pylint: disable=assignment-from-no-return

        # swizzle the quaternion
        quat = mu.Quaternion([quat.w, quat.x, quat.y, -quat.z])

        x, y, z = GameLight.quaternion_to_euler(quat)

        x = math.degrees(-x) - 90
        y = math.degrees(-y)
        z = math.degrees(z) - 270

        return math.radians(x), math.radians(y), math.radians(z)

    @staticmethod
    def srgb_to_linear(s: int):
        """Converts a color channel from SRGB to linear color space.

        :param s: Color channel in SRGB color space.
        :return: Color channel in linear color space.
        """
        if s <= 0.0404482362771082:
            lin = s / 12.92
        else:
            lin = pow(((s + 0.055) / 1.055), 2.4)
        return lin

    @staticmethod
    def get_linear_rgb(color_prop: dict) -> tuple[float, float, float]:
        """Converts JSON color property to Blender color in linear color space.

        :param color_prop: Color property from .json.
        :return: Blender color in linear color space.
        """
        return (
            GameLight.srgb_to_linear(color_prop["R"] / 255),
            GameLight.srgb_to_linear(color_prop["G"] / 255),
            GameLight.srgb_to_linear(color_prop["B"] / 255)
        )

    @property
    def invalid(self) -> bool:
        return self.no_entity

    def __init__(self, json_obj, json_entity) -> None:
        self.entity_name = json_entity.get("Outer", 'Error')
        self.type = json_entity.get("Type", None)

        if not self.type:
            self.no_entity = True
            return None

        props = json_entity.get("Properties", None)
        if not props:
            print(f"Invalid Entity {self.entity_name}. Lacking properties.")
            self.no_entity = True
            return None

        if (pos := props.get("RelativeLocation", None)) is not None:
            self.pos = [pos.get("X") / 100, pos.get("Y") / -100, pos.get("Z") / 100]

        if (rot := props.get("RelativeRotation", None)) is not None:
            self.rot = GameLight.normalize_rotation(rot.get("Roll"), rot.get("Pitch"), rot.get("Yaw"))

        if (scale := props.get("RelativeScale3D", None)) is not None:
            self.scale = [scale.get("X", 1), scale.get("Y", 1), scale.get("Z", 1)]

        if ((parent := props.get("AttachParent", None)) is not None
           and (obj_name := parent.get("ObjectName", None)) is not None):
            self.parent_mtx = get_parent_transform_matrix(json_obj, *parse_ue_object_name(obj_name))

        if (temp := props.get("Temperature", None)) is not None:
            self.color = self.temp_to_color(temp)

        # TODO: for now color overrides the temperature based setting if present. Check if they're mutually exclusive.
        if (color := props.get("LightColor", None)) is not None:
            self.color = self.get_linear_rgb(color)

        if (intensity := props.get("Intensity", None)) is not None:
            self.intensity = intensity

        if (intensity_units := props.get("IntensityUnits", None)) is not None:
            self.intensity_units = GameLight.light_intensity_units_mapping.get(intensity_units)

        self.cone_angle = 44.0 if self.type == 'SpotLightComponent' else 90.0

        if (cone_angle := props.get("OuterConeAngle", None)) is not None:
            self.cone_angle = cone_angle

        if (inner_cone_angle := props.get("InnerConeAngle", None)) is not None:
            self.inner_cone_angle = inner_cone_angle

        if (source_radius := props.get("SourceRadius", None)) is not None:
            self.source_radius = source_radius

        if (cast_shadows := props.get("CastShadows", None)) is not None:
            self.cast_shadows = cast_shadows

        if (attenuation_radius := props.get("AttenuationRadius", None)) is not None:
            self.attenuation_radius = attenuation_radius

        if (source_width := props.get("SourceWidth", None)) is not None:
            self.source_width = source_width

        if (source_height := props.get("SourceHeight", None)) is not None:
            self.source_height = source_height

        return None

    def import_light(self, collection) -> bool:
        if self.no_entity:
            print(f"Refusing to import {self.entity_name} due to failed checks.")
            return False

        light_data = bpy.data.lights.new(name=self.entity_name, type=self.light_type_mapping.get(self.type))
        light_obj = bpy.data.objects.new(name=self.entity_name, object_data=light_data)

        if self.parent_mtx is None:
            light_obj.scale = (self.scale[0], self.scale[1], self.scale[2])
            light_obj.location = (self.pos[0], self.pos[1], self.pos[2])
            light_obj.rotation_mode = 'XYZ'
            light_obj.rotation_euler = mu.Euler((self.rot[0], self.rot[1], self.rot[2]), 'XYZ')
        else:
            local_mtx = InstanceTransform()
            local_mtx.pos = self.pos
            local_mtx.rot_euler = self.rot
            local_mtx.scale = self.scale

            light_obj.matrix_world = self.parent_mtx @ local_mtx.matrix_4x4

        light_data.use_custom_distance = True
        light_data.cutoff_distance = 1000 * 0.01  # default value

        if light_data.type == 'SPOT':
            light_data.spot_size = math.radians(self.cone_angle)
            light_data.spot_blend = 1.0 - (math.radians(self.inner_cone_angle) / math.radians(self.cone_angle))

        match light_data.type:
            case 'SPOT' | 'POINT':
                match self.intensity_units:
                    case GameLight.IntensityUnits.Unitless:
                        light_data.energy = (99.5 * (1 - math.cos(self.cone_angle / 2))) * self.intensity
                    case GameLight.IntensityUnits.Candelas:
                        light_data.energy = self.intensity * 683 / (4 * math.pi)
                    case GameLight.IntensityUnits.Lumens:
                        light_data.energy = self.intensity / 683

            case 'AREA':
                match self.intensity_units:
                    case GameLight.IntensityUnits.Unitless:
                        light_data.energy = self.intensity * 199 / 683
                    case GameLight.IntensityUnits.Candelas:
                        light_data.energy = self.intensity * 683 / (4 * math.pi)
                    case GameLight.IntensityUnits.Lumens:
                        light_data.energy = self.intensity / 683

        light_data.color = self.color
        light_data.shadow_soft_size = self.source_radius * 0.01
        light_data.use_shadow = self.cast_shadows

        if hasattr(light_data, "cycles"):
            light_data.cycles.cast_shadow = self.cast_shadows

        if self.attenuation_radius:
            light_data.use_custom_distance = True
            light_data.cutoff_distance = self.attenuation_radius * 0.01

        if light_data.type == 'AREA':
            light_data.shape = 'RECTANGLE'
            light_data.size = self.source_width * 0.01
            light_data.size_y = self.source_height * 0.01

        collection.objects.link(light_obj)
        bpy.context.scene.collection.objects.link(light_obj)

        return True


class MapImporter(asset_importer.AssetImporter):
    """Imports Unreal Engine map (FModel .json output). Assets are imported from UModel output directory.
    """

    apply_override_materials: bpy.props.BoolProperty(
        name="Use OverrideMaterials from UMAP",
        description="Apply per-component OverrideMaterials from UMAP JSON",
        default=False
    )

    def _encode_override_materials(self, override_materials: t.Optional[list[t.Optional[tuple[str, str]]]]) -> str:
        return override_ops.encode_override_materials(override_materials)

    def _decode_override_materials(self, s: str) -> t.Optional[list[t.Optional[tuple[str, str]]]]:
        return override_ops.decode_override_materials(s)

    def _encode_base_material_slots(self, obj: bpy.types.Object) -> str:
        """Encode current per-object material slot names + material names.

        This is used to repair rare cases where Blender library reload invalidates
        some slot pointers and/or slot names.

        We intentionally do NOT store ObjectPaths here because base materials are
        already imported/linked by the normal mesh pipeline. The repair pass simply
        re-assigns by name if the material datablock exists.
        """
        try:
            slots = []
            for slot in obj.material_slots:
                slots.append({
                    "sn": slot.name or "",
                    "mn": slot.material.name if slot.material else ""
                })
            return json.dumps(slots, ensure_ascii=False)
        except Exception:
            return "[]"

    def _decode_base_material_slots(self, s: str) -> t.Optional[list[dict[str, str]]]:
        if not s:
            return None
        try:
            arr = json.loads(s)
        except Exception:
            return None
        if not isinstance(arr, list):
            return None
        out: list[dict[str, str]] = []
        for entry in arr:
            if isinstance(entry, dict):
                out.append({
                    "sn": str(entry.get("sn", "")) if entry.get("sn", "") is not None else "",
                    "mn": str(entry.get("mn", "")) if entry.get("mn", "") is not None else "",
                })
        return out

    def _load_base_slots_from_mesh_json(self,
                                       obj: bpy.types.Object,
                                       umodel_export_dir: str,
                                       asset_dir: str,
                                       game_profile: str,
                                       db: t.Optional[asset_db.AssetDB] = None
                                       ) -> t.Optional[list[dict[str, str]]]:
        """Fallback: reconstruct base slots from the mesh JSON's StaticMaterials.

        This handles rare meshes where Blender reload wipes linked material slot pointers
        and we did not successfully persist base slot info during instance creation.

        Returns list entries: {"si": slot_index, "sn": slot_name, "mn": material_name, "op": material_object_path}
        """
        try:
            mesh_objpath = obj.get("_umodel_mesh_object_path", "")
            if not mesh_objpath:
                return None

            mesh_objpath = strip_objectpath_trailing_dotnum(str(mesh_objpath))
            rel = os.path.normpath(str(mesh_objpath).lstrip('/'))
            json_path = os.path.join(umodel_export_dir, rel) + ".json"
            if not os.path.isfile(json_path):
                return None

            with open(json_path, mode='r', encoding='utf-8') as f:
                data = json.load(f)

            # FModel mesh exports are not always a single dict.
            # Some exports are a list of objects, where the useful data lives under
            # entry["Properties"]["StaticMaterials"].
            static_mats = None
            if isinstance(data, dict):
                static_mats = data.get("StaticMaterials", None)
                if not isinstance(static_mats, list):
                    props = data.get("Properties")
                    if isinstance(props, dict):
                        static_mats = props.get("StaticMaterials")
            elif isinstance(data, list):
                for ent in data:
                    if not isinstance(ent, dict):
                        continue
                    props = ent.get("Properties")
                    if not isinstance(props, dict):
                        continue
                    sm = props.get("StaticMaterials")
                    if isinstance(sm, list) and sm:
                        static_mats = sm
                        break

            if not isinstance(static_mats, list) or not static_mats:
                return None

            out: list[dict[str, str]] = []
            for sm in static_mats:
                if not isinstance(sm, dict):
                    continue
                mi = sm.get("MaterialInterface", None)
                mat_name = ""
                mat_op = ""
                if isinstance(mi, dict) or isinstance(mi, str):
                    mat_name = _extract_ref_name(mi)
                    mat_op = _extract_ref_path(mi)

                # Some exports omit/garble ObjectName but include ObjectPath.
                # Derive the name from the path in that case.
                if not mat_name and mat_op:
                    mat_name = strip_objectpath_trailing_dotnum(mat_op).rsplit('/', 1)[-1]

                # Normalize ObjectPath by stripping trailing .<digits>
                if mat_op:
                    mat_op = strip_objectpath_trailing_dotnum(mat_op)

                # NOTE:
                # In UE/FModel exports, MaterialSlotName is a *name*, not a guaranteed dense index.
                # It's very common to see names like "0" and "16" even when the mesh has only
                # two slots (array order defines indices 0..N-1). Treat the JSON list order as
                # the authoritative slot index for Blender assignment.
                slot_name = sm.get("MaterialSlotName", "") or sm.get("ImportedMaterialSlotName", "") or ""

                out.append({
                    # Keep "si" empty so repair uses sequential order.
                    "si": "",
                    "sn": str(slot_name) if slot_name is not None else "",
                    "mn": str(mat_name) if mat_name is not None else "",
                    "op": str(mat_op) if mat_op is not None else "",
                })


            # Ensure base materials exist/are linked so repair can assign them.
            for e in out:
                mn = e.get("mn", "")
                op = e.get("op", "")
                if not mn or not op:
                    continue
                if bpy.data.materials.get(mn) is not None:
                    continue

                # If Blender already has MI_Name.### because of name collisions,
                # prefer reusing that instead of importing/linking a new one.
                alt = None
                prefix = mn + "."
                for m in bpy.data.materials:
                    if m.name.startswith(prefix) and m.name[len(prefix):].isdigit():
                        alt = m
                        break
                if alt is not None:
                    continue
                self._get_or_link_material_from_objectpath(
                    material_name=mn,
                    material_object_path=op,
                    umodel_export_dir=umodel_export_dir,
                    asset_dir=asset_dir,
                    game_profile=game_profile,
                    db=db
                )

            return out
        except Exception:
            return None

    def _persist_base_material_slots(self, obj: bpy.types.Object) -> None:
        """Store base slot names/material names once per object.

        We try to persist from the current slots first. If the slots are present but
        effectively blank (no names, no materials), we attempt a JSON fallback to avoid
        persisting useless data.
        """
        if obj.type != "MESH":
            return
        if obj.get("_umodel_base_material_slots", ""):
            return
        if len(obj.material_slots) == 0:
            return

        encoded = self._encode_base_material_slots(obj)

        # If we captured nothing useful (common symptom: all slots empty after a link/reload),
        # try to reconstruct from mesh JSON if available.
        try:
            decoded = self._decode_base_material_slots(encoded) or []
            useful = any((e.get("sn") or e.get("mn")) for e in decoded)
        except Exception:
            useful = True

        if not useful:
            try:
                # We don't know the import paths here, so defer persistence; the post-reload
                # repair step will use JSON fallback when it has paths.
                return
            except Exception:
                return

        obj["_umodel_base_material_slots"] = encoded

    def _repair_base_material_slots(self,
                                   obj: bpy.types.Object,
                                   umodel_export_dir: str,
                                   asset_dir: str,
                                   game_profile: str,
                                   db: t.Optional[asset_db.AssetDB] = None,
                                   force_reassign: bool = False) -> bool:
        """Repair wiped/blank slots.

        Primary source: persisted base info captured at instance creation.
        Fallback source: mesh JSON StaticMaterials (when persistence was not possible / was blank).

        Returns True if any repair action was taken.
        """
        if obj.type != "MESH" or len(obj.material_slots) == 0:
            return False

        def _is_placeholder_material(mat: t.Optional[bpy.types.Material]) -> bool:
            """Heuristic for placeholder mats/slots that should be treated as empty.

            Some meshes arrive with numeric slot/material labels like "0" / "16".
            Those are UE slot *names* and are not usable material identities in Blender.
            Treat them as placeholders so we can replace them by the real MI_* materials.
            """
            if mat is None:
                return True
            n = (getattr(mat, "name", "") or "").strip()
            if not n:
                return True
            if n.isdigit():
                return True
            return False

        s = obj.get("_umodel_base_material_slots", "")
        data = self._decode_base_material_slots(s) if s else None

        # If persisted data is missing or useless, try JSON fallback.
        if not data or not any((e.get("sn") or e.get("mn")) for e in data):
            fallback = self._load_base_slots_from_mesh_json(
                obj,
                umodel_export_dir=umodel_export_dir,
                asset_dir=asset_dir,
                game_profile=game_profile,
                db=db
            )
            if fallback:
                utils.verbose_print(f"Base slot JSON fallback for {obj.name}: slots={len(fallback)}")
                # Convert fallback into the same persisted format (sn/mn only) for later.
                try:
                    obj["_umodel_base_material_slots"] = json.dumps(
                        [{"sn": e.get("sn", ""), "mn": e.get("mn", "")} for e in fallback],
                        ensure_ascii=False
                    )
                except Exception:
                    pass
                # For repair we want object paths too.
                data_with_paths = fallback
            else:
                # If the object has blank slots and we can't find JSON, log it.
                try:
                    mesh_objpath = obj.get("_umodel_mesh_object_path", "")
                    mesh_objpath = strip_objectpath_trailing_dotnum(str(mesh_objpath)) if mesh_objpath else ""
                    rel = os.path.normpath(str(mesh_objpath).lstrip('/')) if mesh_objpath else ""
                    json_path = (os.path.join(umodel_export_dir, rel) + ".json") if rel else ""
                    utils.verbose_print(
                        f"Base slot repair failed for {obj.name}: no persisted slots and no JSON fallback "
                        f"(mesh_objpath={mesh_objpath!r}, json_exists={os.path.isfile(json_path) if json_path else False})"
                    )
                except Exception:
                    pass
                return False
        else:
            data_with_paths = None

        # If nothing looks broken and we aren't forcing, skip.
        if (not force_reassign and
                not any((slot.material is None) or (not slot.name) or _is_placeholder_material(slot.material)
                        for slot in obj.material_slots)):
            return False

        repaired = False

        # Use per-object slots so we can fix without touching shared mesh datablocks.
        self._ensure_object_slot_overrides(obj)

        entries = (data_with_paths if data_with_paths is not None else (data or []))

        # Assign strictly by sequential order.
        # MaterialSlotName in UE/FModel exports is a name string and can look numeric ("0", "16", etc.)
        # even when the mesh only has N slots in array order.
        for seq_i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue

            desired_sn = entry.get("sn", "")
            desired_mn = entry.get("mn", "")
            desired_op = entry.get("op", "")

            slot_i = seq_i

            if slot_i < 0 or slot_i >= len(obj.material_slots):
                continue

            slot = obj.material_slots[slot_i]

            # Don't pollute Blender slot labels with numeric-only UE slot labels.
            if desired_sn and not slot.name and not str(desired_sn).strip().isdigit():
                try:
                    slot.name = desired_sn
                    repaired = True
                except Exception:
                    pass

            # If forced, always assign. Otherwise only replace empty/placeholder.
            should_assign = bool(desired_mn) and (force_reassign or _is_placeholder_material(slot.material))
            if should_assign:
                mat = bpy.data.materials.get(desired_mn)
                if mat is None:
                    # Blender may have auto-suffixed the name (MI_Name.###)
                    prefix = desired_mn + "."
                    for m in bpy.data.materials:
                        if m.name.startswith(prefix) and m.name[len(prefix):].isdigit():
                            mat = m
                            break
                if mat is None and desired_op:
                    mat = self._get_or_link_material_from_objectpath(
                        material_name=desired_mn,
                        material_object_path=desired_op,
                        umodel_export_dir=umodel_export_dir,
                        asset_dir=asset_dir,
                        game_profile=game_profile,
                        db=db
                    )
                if mat is not None:
                    slot.material = mat
                    repaired = True
        if repaired:
            # If any slot is still blank, report it for debugging.
            still_blank = sum(1 for s in obj.material_slots if (s.material is None) or (not s.name))
            if still_blank:
                utils.verbose_print(f"Base slot repair incomplete for {obj.name}: still_blank={still_blank}")

                try:
                    want = [e.get('mn', '') for e in entries[:len(obj.material_slots)] if isinstance(e, dict)]
                    have = [(s.name, getattr(s.material, 'name', None)) for s in obj.material_slots]
                    utils.verbose_print(f"  wanted_mats={want}")
                    utils.verbose_print(f"  have_slots={have}")
                except Exception:
                    pass

        return repaired

    def _post_import_reload_and_reapply(self,
                                       collection_name: str,
                                       umodel_export_dir: str,
                                       asset_dir: str,
                                       game_profile: str,
                                       apply_override_materials: bool = True) -> t.Optional[float]:
        """Reload linked libraries and re-apply per-object OverrideMaterials.

        Blender library reload can invalidate some linked material pointers on some objects.
        We persist each object's override list in a custom property, and re-apply after reload
        to eliminate the 'blank slot' cases.
        """
        # 1) Reload all linked libraries (existing addon behavior)
        for lib in bpy.data.libraries:
            try:
                lib.reload()
            except Exception:
                pass

        # 2) Re-apply overrides for objects that recorded them
        coll = bpy.data.collections.get(collection_name)
        if coll is None:
            return None

        def _iter_objects_recursive(c: bpy.types.Collection):
            for o in c.objects:
                yield o
            for cc in c.children:
                yield from _iter_objects_recursive(cc)

        if apply_override_materials:
                    reapplied = 0
                    for obj in _iter_objects_recursive(coll):
                        # Persist base slots before reload may wipe them.
                        self._persist_base_material_slots(obj)
            
                        s = obj.get("_umodel_override_materials", "")
                        if not s:
                            continue
                        overrides = self._decode_override_materials(s)
                        if not overrides or not any(x is not None for x in overrides):
                            continue
                        self._apply_override_materials_to_object(
                            obj,
                            overrides,
                            umodel_export_dir=umodel_export_dir,
                            asset_dir=asset_dir,
                            game_profile=game_profile,
                            db=None
                        )
                        reapplied += 1
            
                    if reapplied:
                        utils.verbose_print(f"OverrideMaterials re-applied after library reload: objects={reapplied}")

        # 3) Repair base materials for objects that had no overrides but got wiped by reload.
        repaired = 0
        for obj in _iter_objects_recursive(coll):
            has_overrides = False
            try:
                s_ov = obj.get("_umodel_override_materials", "")
                if s_ov:
                    ovs = self._decode_override_materials(s_ov)
                    has_overrides = bool(ovs and any(x is not None for x in ovs))
            except Exception:
                has_overrides = False

            if self._repair_base_material_slots(
                obj,
                umodel_export_dir=umodel_export_dir,
                asset_dir=asset_dir,
                game_profile=game_profile,
                db=None,
                # If the object has no overrides, force a full base reassign. This prevents
                # stubborn placeholder/incorrect mats from surviving reload.
                force_reassign=(not has_overrides)
            ):
                repaired += 1
        if repaired:
            utils.verbose_print(f"Base material slots repaired after library reload: objects={repaired}")


        # MindsEye: re-link tinted materials after library reload (only when enabled)
        use_unwrapper = _is_palette_unwrapper_enabled(game_profile)
        if use_unwrapper:
            try:
                objs = list(_iter_objects_recursive(coll))
                color_palette_unwrapper.relink_tinted_materials_by_tint_id(objs)
            except Exception as e:
                utils.verbose_print(f'Tint relink pass failed: {e}')
        return None

    def _get_or_link_material_from_objectpath(self,
                                              material_name: str,
                                              material_object_path: str,
                                              umodel_export_dir: str,
                                              asset_dir: str,
                                              game_profile: str,
                                              db: t.Optional[asset_db.AssetDB] = None
                                              ) -> t.Optional[bpy.types.Material]:
        return override_ops.get_or_link_material_from_objectpath(
            importer=self,
            material_name=material_name,
            material_object_path=material_object_path,
            umodel_export_dir=umodel_export_dir,
            asset_dir=asset_dir,
            game_profile=game_profile,
            db=db,
        )

        # already present
        mat = bpy.data.materials.get(material_name)
        if mat is not None:
            return mat

        # Convert UE object path -> library relative path (no ext, strip trailing .0)
        stripped = strip_objectpath_trailing_dotnum(material_object_path)
        rel_no_ext = os.path.normpath(stripped.lstrip('/'))
        material_lib_path = os.path.join(asset_dir, rel_no_ext) + ".blend"

        try:
            # Ensure the library .blend exists. If not, import it using the addon pipeline.
            if not os.path.isfile(material_lib_path):
                if db is None:
                    db = asset_db.AssetDB(db_root_path=asset_dir)
                self._import_material_to_library(
                    material_name=material_name,
                    material_path_local_no_ext=rel_no_ext,
                    db=db,
                    umodel_export_dir=umodel_export_dir,
                    asset_library_dir=asset_dir,
                    game_profile=game_profile
                )

            # already linked from that library?
            existing = utils.linked_libraries_search(material_lib_path, bpy.types.Material)
            if existing is not None:
                return existing

            # link it
            with utils.redirect_cstdout():
                with bpy.data.libraries.load(filepath=material_lib_path, link=True) as (data_from, data_to):
                    # Prefer exact name match (some .blend files may contain multiple materials)
                    chosen_name = None
                    for n in data_from.materials:
                        if n == material_name:
                            chosen_name = n
                            break
                    if chosen_name is None and data_from.materials:
                        chosen_name = data_from.materials[0]
                    data_to.materials = [chosen_name] if chosen_name else []

                return data_to.materials[0] if data_to.materials else None

        except Exception as e:
            self._warn_print(f'Warning: Override material "{material_name}" failed to load: {e}')
            return None

    def _ensure_object_slot_overrides(self, obj: bpy.types.Object) -> None:
        override_ops.ensure_object_slot_overrides(obj)

    def _apply_override_materials_to_object(self,
                                           obj: bpy.types.Object,
                                           override_materials: t.Optional[list[t.Optional[tuple[str, str]]]],
                                           umodel_export_dir: str,
                                           asset_dir: str,
                                           game_profile: str,
                                           db: t.Optional[asset_db.AssetDB] = None) -> None:
        return override_ops.apply_override_materials_to_object(
            importer=self,
            obj=obj,
            override_materials=override_materials,
            umodel_export_dir=umodel_export_dir,
            asset_dir=asset_dir,
            game_profile=game_profile,
            db=db,
        )

    def _import_map(self,
                    context: bpy.types.Context,
                    map_path: str,
                    umodel_export_dir: str,
                    asset_dir: str,
                    game_profile: str,
                    db: t.Optional[asset_db.AssetDB] = None,
                    map_index: int = 1,       # new: which map number we are importing
                    map_total: int = 1        # new: total number of maps
    ) -> bool:
        """Imports map placements to the current scene.

        :param map_path: Path to FModel .json output representing a .umap file.
        :param umodel_export_dir: UModel output directory.
        :param asset_dir: Asset library directory.
        :param game_profile: Current game profile.
        :param db: Asset database.
        :return: True if succesful, else False.
        """

        if not os.path.exists(map_path):
            print(f"Error: File {map_path} not found. Skipping.")
            return False

        json_filename = os.path.basename(map_path)
        import_collection = bpy.data.collections.new(json_filename)

        bpy.context.scene.collection.children.link(import_collection)

        with open(map_path, mode='r', encoding='utf-8') as file:
            json_object = json.load(file)

            # handle the different entity types (mehses, lights, etc)
            with utils.std_out_err_redirect_tqdm() as orig_stdout:
                map_name = os.path.splitext(os.path.basename(map_path))[0]
                tqdm_desc = f"[{map_index}/{map_total} B:{bpy.context.scene.umodel_use_vertex_bounds}] Importing map \"{map_name}\""
                for entity in tqdm.tqdm(json_object,
                                        desc=tqdm_desc,
                                        file=orig_stdout,
                                        dynamic_ncols=True,
                                        ascii=True):
                    if not entity.get('Type', None):
                        continue

                    entity_type = entity.get('Type')

                    # static meshes
                    if entity_type in StaticMesh.static_mesh_types:
                        static_mesh = StaticMesh(json_object, entity, entity_type)

                        if static_mesh.invalid:
                            utils.verbose_print(f"Info: Skipping instance of {static_mesh.entity_name}. "
                                                "Invalid property.")
                            continue
                        if not static_mesh_has_instance_in_bounds(static_mesh):
                            continue

                        if (obj := self._load_asset(
                            context=context,
                            asset_dir=asset_dir,
                            asset_path=static_mesh.asset_path,
                            umodel_export_dir=umodel_export_dir,
                            load=True,
                            db=db,
                            game_profile=game_profile
                        )) is None:
                            self._warn_print(f"Warning: Skipping instance of {static_mesh.entity_name} due to import "
                                             "failure.")
                            continue

                        if static_mesh.override_materials and any(x is not None for x in static_mesh.override_materials):
                            non_null = sum(1 for x in static_mesh.override_materials if x is not None)
                            utils.verbose_print(
                                f"OverrideMaterials detected for {static_mesh.entity_name}: "
                                f"slots_in_override={len(static_mesh.override_materials)} non_null={non_null}"
                            )

                        static_mesh.link_object_instance(
                            importer=self,
                            obj=obj,
                            collection=import_collection,
                            umodel_export_dir=umodel_export_dir,
                            asset_dir=asset_dir,
                            game_profile=game_profile,
                            db=db
                        )

                    # lights
                    elif entity_type in GameLight.light_types:
                        light = GameLight(json_object, entity)

                        if light.invalid:
                            utils.verbose_print(f"Info: Skipping instance of {static_mesh.entity_name}. "
                                                "Invalid property.")
                            continue

                        light.import_light(import_collection)

        # TODO: required due to unknown reason, blender bug? Otherwise, some meshes have None materials.
        bpy.app.timers.register(
            functools.partial(
                _timer_post_import_reload_and_reapply,
                import_collection.name,
                umodel_export_dir,
                asset_dir,
                game_profile,
                getattr(self, 'apply_override_materials', True),
            ),
            first_interval=0.010
        )

        return True