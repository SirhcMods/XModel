from __future__ import annotations

import bpy
import re


PALETTE_IMAGE_NAME_HINT = "T_ColorPallet_01"
COLOR_ATTR_NAME = "BakedTint"
TINT_PROP_NAME = "TintID"
DEBUG = True
DO_SRGB_TO_LINEAR = True


SUFFIX_RE = re.compile(r"\.\d{3}$")


def srgb_to_linear(c: float) -> float:
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def base_name(name: str) -> str:
    return SUFFIX_RE.sub("", name)


def find_palette_image(selected_objs):
    if PALETTE_IMAGE_NAME_HINT:
        img = bpy.data.images.get(PALETTE_IMAGE_NAME_HINT)
        if img:
            return img

    keywords = ["colorpallet", "colourpallet", "palette", "pallet"]
    best = None
    best_score = -1

    for img in bpy.data.images:
        n = (img.name or "").lower()
        score = sum(10 for k in keywords if k in n)
        if score > best_score:
            best_score = score
            best = img

    if best and best_score > 0:
        return best

    for obj in selected_objs:
        for slot in obj.material_slots:
            mat = slot.material
            if not mat or not mat.use_nodes or not mat.node_tree:
                continue
            for n in mat.node_tree.nodes:
                if n.type == 'TEX_IMAGE' and getattr(n, "image", None):
                    nn = (n.image.name or "").lower()
                    if any(k in nn for k in keywords):
                        return n.image

    return None

def material_uses_color_palette(mat: bpy.types.Material) -> bool:
    if not mat or not mat.use_nodes or not mat.node_tree:
        return False

    for node in mat.node_tree.nodes:
        if node.type == 'TEX_IMAGE' and getattr(node, "image", None):
            img = node.image
            if img and img.name == PALETTE_IMAGE_NAME_HINT:
                return True

            img_name = (img.name or "").lower()
            if "colorpallet" in img_name or "colourpallet" in img_name or "palette" in img_name:
                return True

    return find_palette_mix_node(mat) is not None


def sample_palette_16x16_like_addon(img, tint_id: int):
    tid = max(0, min(255, int(tint_id)))

    col = tid % 16
    row = tid // 16
    row_inv = 15 - row

    u = (col / 16.0) + (0.5 / 16.0)
    v = (row_inv / 16.0) + (0.5 / 16.0)

    w, h = img.size[0], img.size[1]
    if w < 1 or h < 1:
        return (1.0, 1.0, 1.0)

    x = max(0, min(w - 1, int(u * w)))
    y = max(0, min(h - 1, int(v * h)))

    idx = (y * w + x) * 4
    px = img.pixels
    r, g, b = px[idx + 0], px[idx + 1], px[idx + 2]

    if DO_SRGB_TO_LINEAR:
        r, g, b = srgb_to_linear(r), srgb_to_linear(g), srgb_to_linear(b)

    return (r, g, b)


def get_tintid_for_slot(obj, mat, slot_index: int) -> int:
    if TINT_PROP_NAME in obj:
        try:
            vals = list(obj[TINT_PROP_NAME])
            if 0 <= slot_index < len(vals):
                return int(vals[slot_index])
        except Exception:
            pass

    if mat and (TINT_PROP_NAME in mat):
        try:
            return int(mat[TINT_PROP_NAME])
        except Exception:
            pass

    return 0


def ensure_color_attribute(mesh: bpy.types.Mesh, name: str):
    ca = mesh.color_attributes.get(name)
    if not ca:
        ca = mesh.color_attributes.new(name=name, type='FLOAT_COLOR', domain='CORNER')
    else:
        if ca.domain != 'CORNER':
            mesh.color_attributes.remove(ca)
            ca = mesh.color_attributes.new(name=name, type='FLOAT_COLOR', domain='CORNER')
    return ca


def find_palette_mix_node(mat: bpy.types.Material):
    if not mat or not mat.use_nodes or not mat.node_tree:
        return None

    for node in mat.node_tree.nodes:
        nm = (node.name or "").lower()
        lb = (node.label or "").lower()
        if "palettemix" in nm or "palette mix" in nm or "palettemix" in lb or "palette mix" in lb:
            return node

    return None


def remove_palette_lookup_chain_and_replace_with_attr(mat: bpy.types.Material, attr_name: str = COLOR_ATTR_NAME):
    if not mat or not mat.use_nodes or not mat.node_tree:
        return False

    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links

    palette_mix = find_palette_mix_node(mat)
    if not palette_mix:
        if DEBUG:
            print(f"[BakeTint] No PaletteMix node found in {mat.name}")
        return False

    # We only want Color2 on PaletteMix
    if "Color2" in palette_mix.inputs:
        target_input = palette_mix.inputs["Color2"]
    elif "B" in palette_mix.inputs:
        target_input = palette_mix.inputs["B"]
    elif len(palette_mix.inputs) > 2:
        target_input = palette_mix.inputs[2]
    else:
        if DEBUG:
            print(f"[BakeTint] Could not determine PaletteMix Color2 input in {mat.name}")
        return False

    # Remove only the existing link going into Color2
    removed_any = False
    for link in list(target_input.links):
        try:
            links.remove(link)
            removed_any = True
        except Exception:
            pass

    # Add a Color Attribute node, not generic Attribute
    color_attr = nodes.new("ShaderNodeVertexColor")
    color_attr.layer_name = attr_name
    color_attr.location = (palette_mix.location.x - 260, palette_mix.location.y - 40)

    links.new(color_attr.outputs["Color"], target_input)

    if DEBUG:
        print(
            f"[BakeTint] {mat.name}: disconnected old PaletteMix Color2 input "
            f"and connected Color Attribute '{attr_name}'"
        )

    return True


def merge_material_slots_on_object(obj: bpy.types.Object):
    if obj.type != 'MESH' or not obj.data:
        return

    me = obj.data
    if not me.materials:
        return

    canonical_by_base = {}
    canonical_for_index = {}

    for idx, mat in enumerate(me.materials):
        if mat is None:
            continue
        b = base_name(mat.name)
        if b not in canonical_by_base:
            canonical_by_base[b] = mat
        canonical_for_index[idx] = canonical_by_base[b]

    canonical_index = {}
    for mat in canonical_by_base.values():
        try:
            j = list(me.materials).index(mat)
        except ValueError:
            me.materials.append(mat)
            j = len(me.materials) - 1
        canonical_index[mat] = j

    for poly in me.polygons:
        old_i = poly.material_index
        mat = me.materials[old_i] if old_i < len(me.materials) else None
        if mat is None:
            continue
        b = base_name(mat.name)
        canon_mat = canonical_by_base.get(b)
        if canon_mat is None:
            continue
        poly.material_index = canonical_index[canon_mat]

    ctx = bpy.context
    view_layer = ctx.view_layer
    prev_active = view_layer.objects.active
    prev_sel = [o for o in view_layer.objects if o.select_get()]

    try:
        for o in prev_sel:
            o.select_set(False)
        obj.select_set(True)
        view_layer.objects.active = obj
        bpy.ops.object.material_slot_remove_unused()
    finally:
        for o in view_layer.objects:
            o.select_set(False)
        for o in prev_sel:
            o.select_set(True)
        view_layer.objects.active = prev_active


def bake_tints_to_attr_on_selected(context):
    selected = [o for o in context.selected_objects if o.type == 'MESH']
    if not selected:
        raise RuntimeError("No selected mesh objects.")

    img = find_palette_image(selected)
    if not img:
        raise RuntimeError("Palette image not found. Load it into Blender or ensure it exists in a selected material.")

    if not img.has_data:
        img.pixels[0]

    if DEBUG:
        print(f"[BakeTint] Using palette: {img.name} size={img.size[0]}x{img.size[1]}")

    processed_objects = 0
    converted_materials = 0

    for obj in selected:
        me = obj.data
        ca = ensure_color_attribute(me, COLOR_ATTR_NAME)
        col_data = ca.data

        if DEBUG:
            print(f"[BakeTint] Object: {obj.name} slots={len(obj.material_slots)} has_obj_TintID={'TintID' in obj}")

        slot_rgba = {}
        for si, slot in enumerate(obj.material_slots):
            mat = slot.material

            if mat and material_uses_color_palette(mat):
                tid = get_tintid_for_slot(obj, mat, si)
                r, g, b = sample_palette_16x16_like_addon(img, tid)
                slot_rgba[si] = (r, g, b, 1.0)

                if DEBUG:
                    mn = mat.name if mat else "None"
                    print(
                        f"[BakeTint]   slot {si:02d} mat={mn} "
                        f"USES palette | TintID={tid} -> ({r:.3f}, {g:.3f}, {b:.3f})"
                    )

                changed = remove_palette_lookup_chain_and_replace_with_attr(mat, COLOR_ATTR_NAME)
                if changed:
                    converted_materials += 1
            else:
                slot_rgba[si] = (0.0, 0.0, 0.0, 1.0)

                if DEBUG:
                    mn = mat.name if mat else "None"
                    print(f"[BakeTint]   slot {si:02d} mat={mn} NO palette -> (0, 0, 0)")

        for poly in me.polygons:
            rgba = slot_rgba.get(poly.material_index, (1.0, 1.0, 1.0, 1.0))
            for li in poly.loop_indices:
                col_data[li].color = rgba

        me.update()
        merge_material_slots_on_object(obj)
        processed_objects += 1

    print(f'[BakeTint] Done. Baked palette tint into Color Attribute "{COLOR_ATTR_NAME}".')
    print(f"[BakeTint] Processed {processed_objects} object(s), converted {converted_materials} material(s).")
    return processed_objects, converted_materials