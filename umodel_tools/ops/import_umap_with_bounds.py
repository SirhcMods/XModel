import bpy
import json
import os
from .. import utils

class UMODEL_OT_calculate_import_bounds(bpy.types.Operator):
    bl_idname = "umodel.calculate_import_bounds"
    bl_label = "Calculate Import Bounds"
    bl_description = "Calculate world-space bounds from selected vertices"

    def execute(self, context):
        bounds = utils.get_selected_vertex_world_bounds()

        if not bounds:
            self.report({'ERROR'}, "Select mesh vertices first")
            return {'CANCELLED'}

        scene = context.scene
        scene.umodel_min_x = bounds["min_x"]
        scene.umodel_max_x = bounds["max_x"]
        scene.umodel_min_y = bounds["min_y"]
        scene.umodel_max_y = bounds["max_y"]

        self.report({'INFO'}, "Import bounds calculated from selection")
        return {'FINISHED'}

class UMODEL_OT_scan_umap_bounds(bpy.types.Operator):
    bl_idname = "umodel.scan_umap_bounds"
    bl_label = "Scan UMAPs for Bounds"
    bl_description = "Scan a directory for exported .umap .json files and list maps that intersect the current import bounds"

    def execute(self, context):
        scene = context.scene

        only_bpps = bool(getattr(scene, "umodel_import_bounds_only_bpps", False))

        # Apply general scene import options
        try:
            self.apply_override_materials = bool(getattr(scene, 'umodel_apply_override_materials', False))
        except Exception:
            pass
        try:
            self.load_pbr_maps = bool(getattr(scene, 'umodel_load_pbr_maps', True))
        except Exception:
            pass

        if not hasattr(scene, "umodel_umap_scan_results"):
            self.report({'ERROR'}, "UMAP scan results property not registered. Re-enable addon or restart Blender.")
            return {'CANCELLED'}

        scan_dir = bpy.path.abspath(scene.umodel_umap_scan_dir) if scene.umodel_umap_scan_dir else ""
        if not scan_dir:
            self.report({'ERROR'}, "Set a UMAP JSON Directory first")
            return {'CANCELLED'}

        if not os.path.isdir(scan_dir):
            self.report({'ERROR'}, f"Directory not found: {scan_dir}")
            return {'CANCELLED'}

        # Clear existing results
        scene.umodel_umap_scan_results.clear()
        scene.umodel_umap_scan_index = 0

        # Lazy imports to avoid circular import issues / heavy import cost
        from ..map_importer import StaticMesh, is_within_import_bounds  # pylint: disable=import-outside-toplevel
        from mathutils import Vector  # pylint: disable=import-outside-toplevel

        # Pre-count json files for progress reporting
        json_files = []
        for root, _dirs, files in os.walk(scan_dir):
            for filename in files:
                if filename.lower().endswith(".json"):
                    json_files.append(os.path.join(root, filename))

        total = len(json_files)
        if total == 0:
            self.report({'WARNING'}, "No .json files found in directory")
            return {'CANCELLED'}

        context.window_manager.progress_begin(0, total)

        matches = 0

        try:
            scene.umodel_use_vertex_bounds = True
            # Bounds import session-wide instance de-dupe (MindsEye has cross-UMAP duplicates)
            # This is only used by the 'Import UMAPs with bounds' workflow.
            self._bounds_dedupe_enabled = True
            self._bounds_seen_keys = set()

            for idx, json_path in enumerate(json_files, start=1):
                percent = (idx / total) * 100.0

                # Console logging (cheap, safe)
                print(f"[UMAP SCAN] {idx}/{total} ({percent:.1f}%) - {os.path.basename(json_path)}")

                # Occasional status bar update (don’t spam)
                if idx == 1 or idx % 10 == 0 or idx == total:
                    self.report({'INFO'}, f"Scanning UMAPs: {idx}/{total} ({percent:.1f}%) B:{scene.umodel_use_vertex_bounds}")

                context.window_manager.progress_update(idx)

                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        json_obj = json.load(f)
                except Exception:
                    continue

                if not isinstance(json_obj, list):
                    continue

                # If any matching entity is in bounds, record the map once.
                if only_bpps:
                    for entity in json_obj:
                        try:
                            if entity.get("Type") != "LevelInstanceComponent":
                                continue
                            if entity.get("Name") != "Root":
                                continue

                            outer = entity.get("Outer", "") or ""
                            if not outer.startswith("BPP_"):
                                continue

                            props = entity.get("Properties") or {}
                            loc = props.get("RelativeLocation") or {}
                            # Convert UE cm -> Blender meters and flip Y to match existing importer.
                            pos = Vector((
                                float(loc.get("X", 0.0)) / 100.0,
                                float(loc.get("Y", 0.0)) / -100.0,
                                float(loc.get("Z", 0.0)) / 100.0,
                            ))

                            if is_within_import_bounds(pos):
                                item = scene.umodel_umap_scan_results.add()
                                item.map_name = os.path.splitext(os.path.basename(json_path))[0]
                                item.map_path = json_path
                                matches += 1
                                break
                        except Exception:
                            continue
                else:
                    for entity in json_obj:
                        entity_type = entity.get("Type")
                        if entity_type not in StaticMesh.static_mesh_types:
                            continue

                        try:
                            static_mesh = StaticMesh(json_obj, entity, entity_type)
                            if static_mesh.invalid:
                                continue

                            # Reuse your existing bounds logic
                            if utils.static_mesh_has_instance_in_bounds(static_mesh):
                                item = scene.umodel_umap_scan_results.add()
                                item.map_name = os.path.splitext(os.path.basename(json_path))[0]
                                item.map_path = json_path
                                matches += 1
                                break

                        except Exception:
                            # Never let a single bad entity kill the scan
                            continue

            context.window_manager.progress_end()
        finally:
            # Clear bounds de-dupe state for this operator session
            try:
                self._bounds_dedupe_enabled = False
                self._bounds_seen_keys = set()
            except Exception:
                pass
            scene.umodel_use_vertex_bounds = False

        if only_bpps:
            self.report({'INFO'}, f"UMAP scan complete (BPP-only): {matches} / {total} maps with BPPs within bounds")
        else:
            self.report({'INFO'}, f"UMAP scan complete: {matches} / {total} maps within bounds")
        return {'FINISHED'}



class UMODEL_OT_clear_umap_scan_results(bpy.types.Operator):
    bl_idname = "umodel.clear_umap_scan_results"
    bl_label = "Clear UMAP Scan Results"
    bl_description = "Clear the UMAP scan results list"

    def execute(self, context):
        scene = context.scene
        scene.umodel_umap_scan_results.clear()
        scene.umodel_umap_scan_index = 0
        return {'FINISHED'}