import json
import typing as t

from . import utils


@t.overload
def parse_fmodel_json(json_path: str, mode: t.Literal['MESH']) -> tuple[t.Any, list[str]]:
    ...


@t.overload
def parse_fmodel_json(json_path: str,
                      mode: t.Literal['MATERIAL']
                      ) -> tuple[t.Any, dict[str, str], dict[str, str | float | bool] | None]:
    ...


def parse_fmodel_json(json_path: str,
                      mode: t.Literal['MESH'] | t.Literal['MATERIAL']
                      ) -> tuple[t.Any, list[str]] | tuple[t.Any, dict[str, str], dict[str, str | float | bool] | None]:
    """Parse FModel JSON package export and extract mesh/material references.

    :param json_path: Path to FModel JSON file.
    :param mode: Parse mode, either mesh properties or material properties.
    :raises RuntimeError: Raised when JSON decoding fails.
    :raises NotImplementedError: Raised for unsupported mode.
    """
    utils.verbose_print(f"Parsing {json_path}...")

    with open(json_path, mode='r', encoding='utf-8') as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"ERROR: Failed parsing {json_path}.") from e

    match mode:
        case 'MESH':
            return data, _get_material_paths(data)
        case 'MATERIAL':
            texture_infos = _get_texture_infos(data)
            return data, texture_infos, _get_base_property_overrides(data)
        case _:
            raise NotImplementedError()


def _iter_exports(data: t.Any) -> t.Iterator[dict[str, t.Any]]:
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item
    elif isinstance(data, dict):
        exports = data.get('Exports')
        if isinstance(exports, list):
            for item in exports:
                if isinstance(item, dict):
                    yield item
        else:
            yield data


def _extract_object_path(value: t.Any) -> str | None:
    if isinstance(value, str):
        return _normalize_object_path(value)

    if not isinstance(value, dict):
        return None

    for key in ('ObjectPath', 'ObjectName', 'AssetPathName'):
        if isinstance((obj_path := value.get(key)), str):
            return _normalize_object_path(obj_path)

    return None


def _normalize_object_path(path: str) -> str:
    # strip class prefix: Class'/Game/Path.Asset'
    if "'" in path:
        path = path.split("'", 1)[1]
    path = path.strip("'\" ")

    # ensure absolute unreal path style
    if not path.startswith('/') and path.startswith('Game/'):
        path = '/' + path

    return path


def _get_material_paths(data: t.Any) -> list[str]:
    """Return material descriptor paths for a StaticMesh export.

    AssetImporter expects each entry in the form:
        /Some/Unreal/Path/MI_Name.MI_Name
    where the left side is the descriptor path (no extension) and the right side
    is the material slot name (MI_*).
    """
    material_paths: list[str] = []

    for export in _iter_exports(data):
        # FModel "object properties" json nests StaticMaterials under Properties
        static_materials = export.get('StaticMaterials')
        if not isinstance(static_materials, list):
            props = export.get('Properties')
            if isinstance(props, dict):
                static_materials = props.get('StaticMaterials')

        if not isinstance(static_materials, list):
            continue

        for static_material in static_materials:
            if not isinstance(static_material, dict):
                continue

            mat_interface = static_material.get('MaterialInterface', static_material)
            material_path = _extract_object_path(mat_interface)

            if not isinstance(material_path, str) or not material_path:
                continue

            # Strip trailing ".<digits>" (e.g. ".0") that FModel appends.
            left, dot, right = material_path.rpartition('.')
            if dot and right.isdigit():
                material_path = left

            material_name = material_path.rsplit('/', 1)[-1]
            if not material_name:
                continue

            material_paths.append(f"{material_path}.{material_name}")

    return material_paths
def _strip_unreal_duplicate_suffix(path: str) -> str:
    # /Path/T_Name.T_Name -> /Path/T_Name
    head, sep, tail = path.rpartition('.')
    if not sep:
        return path
    last_segment = head.rsplit('/', 1)[-1]
    return head if tail == last_segment else path


def _get_texture_infos(data: t.Any) -> dict[str, str]:
    texture_infos: dict[str, str] = {}

    for export in _iter_exports(data):
        props = export.get("Properties")
        src = props if isinstance(props, dict) else export

        tex_param_values = src.get("TextureParameterValues")
        used_key = "TextureParameterValues"

        if not isinstance(tex_param_values, list):
            tex_param_values = src.get("Textures")
            used_key = "Textures"

        utils.verbose_print(
            f"[FModel MATERIAL] keys={list(src.keys())[:25]} "
            f"picked={used_key} type={type(tex_param_values).__name__}"
        )

        # New format: "Textures": { "BaseLayer_BaseColor": "/Path/T.T", ... }
        if used_key == "Textures" and isinstance(tex_param_values, dict):
            for param_name, raw_path in tex_param_values.items():
                if not isinstance(param_name, str):
                    continue
                if isinstance(raw_path, str) and raw_path:
                    texture_infos[param_name] = _strip_unreal_duplicate_suffix(raw_path)
                elif isinstance(raw_path, dict):
                    tex_path = _extract_object_path(raw_path)
                    if tex_path:
                        texture_infos[param_name] = _strip_unreal_duplicate_suffix(tex_path)
            continue

        # Old format: list of dicts with ParameterInfo/ParameterValue
        if not isinstance(tex_param_values, list):
            continue

        for tex_param in tex_param_values:
            if not isinstance(tex_param, dict):
                continue

            tex_path = _extract_object_path(tex_param.get("ParameterValue"))
            if tex_path is None:
                continue

            param_name = None
            if isinstance((param_info := tex_param.get("ParameterInfo")), dict):
                if isinstance((name := param_info.get("Name")), str):
                    param_name = name

            if param_name is None and isinstance((name := tex_param.get("ParameterName")), str):
                param_name = name

            if param_name is None:
                continue

            texture_infos[param_name] = _strip_unreal_duplicate_suffix(tex_path)

    utils.verbose_print(
        f"[FModel MATERIAL] total textures extracted: {len(texture_infos)} "
        f"sample={list(texture_infos.items())[:5]}"
    )
    return texture_infos


def _get_base_property_overrides(data: t.Any) -> dict[str, str | float | bool] | None:
    for export in _iter_exports(data):
        base_props = export.get('BasePropertyOverrides')

        if not isinstance(base_props, dict):
            continue

        result: dict[str, str | float | bool] = {}

        if isinstance((blend_mode := base_props.get('BlendMode')), str):
            result['BlendMode'] = blend_mode

        if isinstance((two_sided := base_props.get('TwoSided')), bool):
            result['TwoSided'] = two_sided

        if isinstance((clip := base_props.get('OpacityMaskClipValue')), (int, float)):
            result['OpacityMaskClipValue'] = float(clip)

        return result

    return None