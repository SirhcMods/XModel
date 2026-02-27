"""This module implements support for MindsEye (2025) game.
Known issues:
    - Blended materials are not properly supported. Currently the first texture is used.
"""

import enum
import typing as t
import dataclasses

import bpy
import lark


GAME_NAME = "MindsEye"
GAME_DESCRIPTION = "MindsEye (2025) by BARB"


class TextureMapTypes(enum.Enum):
    """All texture map types supported by the material generator."""
    BaseColor = enum.auto()
    Normal = enum.auto()
    MRO = enum.auto()  # In MindsEye this is typically an ORM packed mask


#: Translates names retrieved from descriptors into sensible texture map types
TEXTURE_PARAM_NAME_TRS = {
    # common
    "Albedo": TextureMapTypes.BaseColor,
    "AlbedoTexture": TextureMapTypes.BaseColor,
    "Base Color": TextureMapTypes.BaseColor,
    "Base Colour": TextureMapTypes.BaseColor,
    "Base_Colour": TextureMapTypes.BaseColor,
    "BaseColor": TextureMapTypes.BaseColor,
    "BaseColour": TextureMapTypes.BaseColor,
    "Diffuse": TextureMapTypes.BaseColor,

    "NormalTexture": TextureMapTypes.Normal,
    "Normal Map": TextureMapTypes.Normal,
    "Base_Normal": TextureMapTypes.Normal,
    "Normal": TextureMapTypes.Normal,
    "Normals": TextureMapTypes.Normal,

    # packed masks
    "Masks": TextureMapTypes.MRO,
    "MaskTexture": TextureMapTypes.MRO,
    "Base_Mask": TextureMapTypes.MRO,
    "Mask": TextureMapTypes.MRO,

    # MindsEye naming from FModel MI jsons
    "BaseLayer_BaseColor": TextureMapTypes.BaseColor,
    "BaseLayer_Normal": TextureMapTypes.Normal,
    "BaseLayer_Masks": TextureMapTypes.MRO,
}

# Normalize keys for case-insensitive lookup
TEXTURE_PARAM_NAME_TRS = {k.lower(): v for k, v in TEXTURE_PARAM_NAME_TRS.items()}


@dataclasses.dataclass
class MaterialContext:
    bsdf_node: t.Optional[bpy.types.ShaderNodeBsdfPrincipled | bpy.types.ShaderNodeBsdfDiffuse]
    desc_ast: lark.Tree | dict[str, t.Any] | list[t.Any]
    use_pbr: bool
    diffuse_connected: bool = dataclasses.field(default=False)
    linked_maps: set[TextureMapTypes] = dataclasses.field(default_factory=set)


_state_buffer: dict[bpy.types.Material, MaterialContext] = {}


def process_material(mat: bpy.types.Material,
                     desc_ast: lark.Tree | dict[str, t.Any] | list[t.Any],
                     use_pbr: bool):  # pylint: disable=unused-argument
    _state_buffer[mat] = MaterialContext(bsdf_node=None, desc_ast=desc_ast, use_pbr=use_pbr)


def do_process_texture(tex_type: str, tex_short_name: str) -> bool:  # pylint: disable=unused-argument
    return isinstance(tex_type, str) and (tex_type.lower() in TEXTURE_PARAM_NAME_TRS)


def is_diffuse_tex_type(tex_type: str, tex_short_name: str) -> bool:  # pylint: disable=unused-argument
    return TEXTURE_PARAM_NAME_TRS.get(tex_type.lower()) == TextureMapTypes.BaseColor


def handle_material_texture_pbr(mat: bpy.types.Material,
                                tex_type: str,
                                tex_short_name: str,  # pylint: disable=unused-argument
                                img_node: bpy.types.ShaderNodeTexImage,
                                ao_mix_node: bpy.types.ShaderNodeMix,
                                bsdf_node: bpy.types.ShaderNodeBsdfPrincipled,
                                out_node: bpy.types.ShaderNodeOutputMaterial):
    mat_ctx = _state_buffer[mat]
    mat_ctx.bsdf_node = bsdf_node

    bl_tex_type = TEXTURE_PARAM_NAME_TRS.get(tex_type.lower())
    if bl_tex_type is None:
        return

    # do not connect the same texture twice
    if bl_tex_type in mat_ctx.linked_maps:
        return
    mat_ctx.linked_maps.add(bl_tex_type)

    match bl_tex_type:
        case TextureMapTypes.BaseColor:
            # Mix node is set to MULTIPLY in the importer; factor controls AO strength.
            # 0.5 matches your "multiply AO with basecolor at 0.5" expectation.
            try:
                ao_mix_node.inputs[0].default_value = 0.5
            except Exception:  # pylint: disable=broad-except
                pass

            mat.node_tree.links.new(img_node.outputs['Color'], ao_mix_node.inputs[6])
            mat.node_tree.links.new(img_node.outputs['Alpha'], bsdf_node.inputs['Alpha'])
            img_node.select = True
            mat.node_tree.nodes.active = img_node
            mat_ctx.diffuse_connected = True

        case TextureMapTypes.Normal:
            if img_node.image and img_node.image.library is not None:
                img_node.image.make_local()
                img_node.image.colorspace_settings.is_data = True
            normal_map_node = mat.node_tree.nodes.new('ShaderNodeNormalMap')
            mat.node_tree.links.new(img_node.outputs['Color'], normal_map_node.inputs['Color'])
            mat.node_tree.links.new(normal_map_node.outputs['Normal'], bsdf_node.inputs['Normal'])

        case TextureMapTypes.MRO:
            # MindsEye packed mask is typically ORM:
            #   R = Ambient Occlusion
            #   G = Roughness
            #   B = Metallic
            orm_split = mat.node_tree.nodes.new('ShaderNodeSeparateColor')
            mat.node_tree.links.new(img_node.outputs['Color'], orm_split.inputs['Color'])

            # AO into multiply chain (ao_mix_node input 7)
            mat.node_tree.links.new(orm_split.outputs['Red'], ao_mix_node.inputs[7])

            # Roughness / Metallic
            mat.node_tree.links.new(orm_split.outputs['Green'], bsdf_node.inputs['Roughness'])
            mat.node_tree.links.new(orm_split.outputs['Blue'], bsdf_node.inputs['Metallic'])


def handle_material_texture_simple(mat: bpy.types.Material,
                                   tex_type: str,  # pylint: disable=unused-argument
                                   tex_short_name: str,  # pylint: disable=unused-argument
                                   img_node: bpy.types.ShaderNodeTexImage,
                                   bsdf_node: bpy.types.ShaderNodeBsdfDiffuse):
    _state_buffer[mat].bsdf_node = bsdf_node
    mat.node_tree.links.new(img_node.outputs['Color'], bsdf_node.inputs['Color'])
    img_node.select = True
    mat.node_tree.nodes.active = img_node

def end_process_material(mat: bpy.types.Material):
    del _state_buffer[mat]
