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
    ColorPallete = enum.auto()
    BaseColor = enum.auto()
    Normal = enum.auto()
    MRO = enum.auto()  # In MindsEye this is typically an ORM packed mask


#: Translates names retrieved from descriptors into sensible texture map types
TEXTURE_PARAM_NAME_TRS = {

    # color pallete used with some materials
    "T_ColorPallet_01": TextureMapTypes.ColorPallete,

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

    # Layers
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
    base_tex_node: t.Optional[bpy.types.ShaderNodeTexImage] = None
    normal_tex_node: t.Optional[bpy.types.ShaderNodeTexImage] = None
    orm_tex_node: t.Optional[bpy.types.ShaderNodeTexImage] = None
    normal_map_node: t.Optional[bpy.types.ShaderNodeNormalMap] = None
    orm_split_node: t.Optional[bpy.types.Node] = None
    palette_tex_node: t.Optional[bpy.types.ShaderNodeTexImage] = None
    tint_value_node: t.Optional[bpy.types.ShaderNodeValue] = None
    palette_mix_node: t.Optional[bpy.types.Node] = None


_state_buffer: dict[bpy.types.Material, MaterialContext] = {}


def _clear_links(socket: bpy.types.NodeSocket):
    """Remove all links from an input socket."""
    if socket.is_linked:
        # copy because removing mutates the list
        for l in list(socket.links):
            socket.node.id_data.links.remove(l)



def _layout_pbr_nodes(mat_ctx: MaterialContext,
                      ao_mix_node: bpy.types.Node | None,
                      bsdf_node: bpy.types.Node | None,
                      out_node: bpy.types.Node | None):
    """Deterministic, clean node placement.

    - Image nodes are stacked vertically.
    - Vector/utility nodes sit immediately to the left of the image they drive.
    - The palette math chain sits to the left of the palette image node.
    Anchors off the BSDF when available.
    """
    if bsdf_node is None:
        bsdf_node = mat_ctx.bsdf_node
    if bsdf_node is None:
        return

    bx, by = bsdf_node.location

    # Keep BSDF and output aligned
    try:
        bsdf_node.location = (bx, by)
    except Exception:
        pass
    if out_node is not None:
        try:
            out_node.location = (bx + 320, by)
        except Exception:
            pass

    # Column X positions
    x_img = bx - 900
    x_mid = bx - 560
    x_mix = bx - 260

    # Row Y positions (stacked)
    y_palette = by + 520
    y_base    = by + 200
    y_normal  = by - 120
    y_orm     = by - 440

    # Place image nodes
    if mat_ctx.palette_tex_node is not None:
        try: mat_ctx.palette_tex_node.location = (x_img, y_palette)
        except Exception: pass
    if mat_ctx.base_tex_node is not None:
        try: mat_ctx.base_tex_node.location = (x_img, y_base)
        except Exception: pass
    if mat_ctx.normal_tex_node is not None:
        try: mat_ctx.normal_tex_node.location = (x_img, y_normal)
        except Exception: pass
    if mat_ctx.orm_tex_node is not None:
        try: mat_ctx.orm_tex_node.location = (x_img, y_orm)
        except Exception: pass

    # Place utility nodes near their images
    if mat_ctx.normal_map_node is not None:
        try: mat_ctx.normal_map_node.location = (x_mid, y_normal)
        except Exception: pass
    if mat_ctx.orm_split_node is not None:
        try: mat_ctx.orm_split_node.location = (x_mid, y_orm)
        except Exception: pass

    # Palette mix (if present) between BaseColor and AO/BSDF
    if mat_ctx.palette_mix_node is not None:
        try: mat_ctx.palette_mix_node.location = (x_mix, y_base)
        except Exception: pass

    # AO mix (if present) between BaseColor and BSDF
    if ao_mix_node is not None:
        try: ao_mix_node.location = (x_mix, y_base + 10)
        except Exception: pass


def _ensure_palette_pipeline(mat: bpy.types.Material,
                             mat_ctx: MaterialContext,
                             ao_mix_node: bpy.types.ShaderNodeMix | None,
                             anchor_node: bpy.types.Node | None):
    """Create (if needed) the standard 16x16 palette sampling + mix chain.

    This is only created when a ColorPallete texture is present. It is wired to the BaseColor chain when both BaseColor and ColorPallete are available.

    Layout is anchored off anchor_node (usually the Principled BSDF) so it works even when
    an ORM/AO chain node is absent.
    """
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    if mat_ctx.palette_tex_node is None:
        return

    pal_tex = mat_ctx.palette_tex_node

    # --- Node layout -------------------------------------------------------
    # Keep graphs readable: place the palette chain to the left of the main shader.
    # Anchor off the BSDF when possible (ORM/AO nodes might not exist for some materials).
    if anchor_node is None:
        anchor_node = mat_ctx.bsdf_node or ao_mix_node

    # Always compute an anchor point for downstream placement.
    # (The palette image node may already have been positioned, but we still
    # need ax/ay for placing mix + image nodes deterministically.)
    if anchor_node is not None:
        try:
            ax, ay = anchor_node.location
        except Exception:
            ax, ay = (0.0, 0.0)
    else:
        ax, ay = (0.0, 0.0)
    # If the palette image node has already been placed by _layout_pbr_nodes, build the math chain relative to it.
    try:
        px, py = pal_tex.location
    except Exception:
        px, py = (None, None)

    if px is None or py is None:
        # Fallback: place palette chain left of the main shader
        px, py = (ax - 900, ay + 520)
        try:
            pal_tex.location = (px, py)
        except Exception:
            pass

    base_x = px - 1350
    base_y = py

    def _loc(n: bpy.types.Node, x: float, y: float):
        try:
            n.location = (x, y)
        except Exception:
            pass

    # Configure palette sampling
    pal_tex.label = pal_tex.label or 'T_ColorPallet_01'
    try:
        # ensure treated as color
        if pal_tex.image:
            pal_tex.image.colorspace_settings.name = 'sRGB'
    except Exception:
        pass
    try:
        pal_tex.interpolation = 'Closest'
    except Exception:
        pass

    # Create TintID value node (default 0; will be overwritten later by json extraction)
    if mat_ctx.tint_value_node is None:
        tint_node = nodes.new('ShaderNodeValue')
        tint_node.label = 'TintID'
        tint_node.outputs[0].default_value = 0.0
        mat_ctx.tint_value_node = tint_node
    else:
        tint_node = mat_ctx.tint_value_node

    _loc(tint_node, base_x, base_y)

    # Palette UV math nodes (from legacy addon)
    # col = TintID % 16
    col_math = nodes.new('ShaderNodeMath')
    col_math.operation = 'MODULO'
    col_math.inputs[1].default_value = 16.0
    links.new(tint_node.outputs[0], col_math.inputs[0])

    _loc(col_math, base_x + 220, base_y)

    # row = floor(TintID / 16)
    row_div = nodes.new('ShaderNodeMath')
    row_div.operation = 'DIVIDE'
    row_div.inputs[1].default_value = 16.0
    links.new(tint_node.outputs[0], row_div.inputs[0])

    _loc(row_div, base_x + 220, base_y - 140)

    row_floor = nodes.new('ShaderNodeMath')
    row_floor.operation = 'FLOOR'
    links.new(row_div.outputs[0], row_floor.inputs[0])

    _loc(row_floor, base_x + 440, base_y - 140)

    # row_inv = 15 - row
    sub_row = nodes.new('ShaderNodeMath')
    sub_row.operation = 'SUBTRACT'
    sub_row.inputs[0].default_value = 15.0
    links.new(row_floor.outputs[0], sub_row.inputs[1])

    _loc(sub_row, base_x + 660, base_y - 140)

    # Build UVs: (col/16 + 0.03125, row_inv/16 + 0.03125)
    div_col = nodes.new('ShaderNodeMath')
    div_col.operation = 'DIVIDE'
    div_col.inputs[1].default_value = 16.0
    links.new(col_math.outputs[0], div_col.inputs[0])

    _loc(div_col, base_x + 440, base_y)

    div_row = nodes.new('ShaderNodeMath')
    div_row.operation = 'DIVIDE'
    div_row.inputs[1].default_value = 16.0
    links.new(sub_row.outputs[0], div_row.inputs[0])

    _loc(div_row, base_x + 880, base_y - 140)

    add_col = nodes.new('ShaderNodeMath')
    add_col.operation = 'ADD'
    add_col.inputs[1].default_value = 0.03125
    links.new(div_col.outputs[0], add_col.inputs[0])

    _loc(add_col, base_x + 660, base_y)

    add_row = nodes.new('ShaderNodeMath')
    add_row.operation = 'ADD'
    add_row.inputs[1].default_value = 0.03125
    links.new(div_row.outputs[0], add_row.inputs[0])

    _loc(add_row, base_x + 1100, base_y - 140)

    comb = nodes.new('ShaderNodeCombineXYZ')
    links.new(add_col.outputs[0], comb.inputs['X'])
    links.new(add_row.outputs[0], comb.inputs['Y'])
    links.new(comb.outputs['Vector'], pal_tex.inputs['Vector'])

    _loc(comb, base_x + 1320, base_y - 40)

    # Mix basecolor with palette color
    if mat_ctx.palette_mix_node is None:
        mix = nodes.new('ShaderNodeMix')
        mix.data_type = 'RGBA'
        mix.blend_type = 'MIX'
        mix.label = 'PaletteMix'
        try:
            mix.use_clamp = True
        except Exception:
            pass
        try:
            mix.inputs[0].default_value = 1.0
        except Exception:
            pass
        mat_ctx.palette_mix_node = mix
    else:
        mix = mat_ctx.palette_mix_node

    _loc(mix, ax - 420, ay + 40)

    # Palette texture node placement (it already exists; we just move it to a sane spot)
    _loc(pal_tex, ax - 820, ay + 200)

    # Wire palette texture into mix Color2 (B)
    _clear_links(mix.inputs[7])
    links.new(pal_tex.outputs['Color'], mix.inputs[7])

    # If we have basecolor already, wire it into mix Color1 (A) and
    # route mix output into the AO multiply chain.
    if mat_ctx.base_tex_node is not None:
        _clear_links(mix.inputs[6])
        links.new(mat_ctx.base_tex_node.outputs['Color'], mix.inputs[6])

        if ao_mix_node is not None:
            _clear_links(ao_mix_node.inputs[6])
            links.new(mix.outputs[2], ao_mix_node.inputs[6])
        elif mat_ctx.bsdf_node is not None:
            # Fallback when no AO/multiply chain exists: feed straight into BSDF base color.
            _clear_links(mat_ctx.bsdf_node.inputs['Base Color'])
            links.new(mix.outputs[2], mat_ctx.bsdf_node.inputs['Base Color'])



def _try_wire_palette(mat: bpy.types.Material,
                      mat_ctx: MaterialContext,
                      ao_mix_node: bpy.types.ShaderNodeMix | None,
                      bsdf_node: bpy.types.Node | None,
                      out_node: bpy.types.Node | None):
    """If both palette + basecolor exist, ensure palette pipeline is wired."""
    if mat_ctx.palette_tex_node is None:
        return
    if mat_ctx.base_tex_node is None:
        return
    _ensure_palette_pipeline(mat, mat_ctx, ao_mix_node, bsdf_node)
    _layout_pbr_nodes(mat_ctx, ao_mix_node, bsdf_node, out_node)


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
        case TextureMapTypes.ColorPallete:
            # Store palette node and build palette pipeline when possible.
            mat_ctx.palette_tex_node = img_node
            _layout_pbr_nodes(mat_ctx, ao_mix_node, bsdf_node, out_node)
            _ensure_palette_pipeline(mat, mat_ctx, ao_mix_node, mat_ctx.bsdf_node)
            # If base color was already connected directly, this will rewire it through PaletteMix.
            _try_wire_palette(mat, mat_ctx, ao_mix_node, bsdf_node, out_node)

        case TextureMapTypes.BaseColor:
            # Mix node is set to MULTIPLY in the importer; factor controls AO strength.
            # 0.5 matches your "multiply AO with basecolor at 0.5" expectation.
            try:
                ao_mix_node.inputs[0].default_value = 0.5
            except Exception:  # pylint: disable=broad-except
                pass

            mat_ctx.base_tex_node = img_node
            _layout_pbr_nodes(mat_ctx, ao_mix_node, bsdf_node, out_node)

            # If palette exists for this material, route base color through palette mix.
            if mat_ctx.palette_tex_node is not None:
                _try_wire_palette(mat, mat_ctx, ao_mix_node, bsdf_node, out_node)
            else:
                if ao_mix_node is not None:
                    mat.node_tree.links.new(img_node.outputs['Color'], ao_mix_node.inputs[6])
                else:
                    mat.node_tree.links.new(img_node.outputs['Color'], bsdf_node.inputs['Base Color'])
            mat.node_tree.links.new(img_node.outputs['Alpha'], bsdf_node.inputs['Alpha'])
            img_node.select = True
            mat.node_tree.nodes.active = img_node
            mat_ctx.diffuse_connected = True

        case TextureMapTypes.Normal:
            if img_node.image and img_node.image.library is not None:
                img_node.image.make_local()
                img_node.image.colorspace_settings.is_data = True
            mat_ctx.normal_tex_node = img_node
            normal_map_node = mat.node_tree.nodes.new('ShaderNodeNormalMap')
            mat_ctx.normal_map_node = normal_map_node
            mat.node_tree.links.new(img_node.outputs['Color'], normal_map_node.inputs['Color'])
            mat.node_tree.links.new(normal_map_node.outputs['Normal'], bsdf_node.inputs['Normal'])
            _layout_pbr_nodes(mat_ctx, ao_mix_node, bsdf_node, out_node)

        case TextureMapTypes.MRO:
            # MindsEye packed mask is typically ORM:
            #   R = Ambient Occlusion
            #   G = Roughness
            #   B = Metallic
            if img_node.image and img_node.image.library is not None:
                img_node.image.make_local()
                img_node.image.colorspace_settings.is_data = True
            mat_ctx.orm_tex_node = img_node
            orm_split = mat.node_tree.nodes.new('ShaderNodeSeparateColor')
            mat_ctx.orm_split_node = orm_split
            mat.node_tree.links.new(img_node.outputs['Color'], orm_split.inputs['Color'])

            # AO into multiply chain (ao_mix_node input 7)
            mat.node_tree.links.new(orm_split.outputs['Red'], ao_mix_node.inputs[7])

            # Roughness / Metallic
            mat.node_tree.links.new(orm_split.outputs['Green'], bsdf_node.inputs['Roughness'])
            mat.node_tree.links.new(orm_split.outputs['Blue'], bsdf_node.inputs['Metallic'])
            _layout_pbr_nodes(mat_ctx, ao_mix_node, bsdf_node, out_node)


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
