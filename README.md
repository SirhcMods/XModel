# XModel

A [Blender](https://blender.org) add-on featuring import of [Unreal Engine](https://www.unrealengine.com)
games static meshes (stored in-game as .uasset) and maps (stored in-game as .umap). It provides basic support for many
UE games, while special support is needed for others.

Please note that is a fork and refactor of the original addon from skarndev. Please refer to the original docs to [add
support for a game](https://skarndev.github.io/umodel_tools/create_game_profile.html). You can also checkout [skarndevs
original source code](https://github.com/skarndev/umodel_tools). This version of the original addon is heavily
WIP and still contains issues. This is not a production ready pipeline and requires python knowledge.

![](/docs/source/images/demo.jpeg?raw=true "Demo")
From Hogwarts Legacy

![](/docs/source/images/demo_2.png?raw=true "Demo_2")
From MindsEye (WIP)

# Features
- Unreal Engine games map (.umap) and (.uasset) import.
- Creation of asset library in Blender out of game's assets.
- PBR materials.
- Lights (experimental support).
# New Features from this fork
- Support for BPP (Packed Level Instance Blueprints)

# Improvements from this fork
In addition to the core implemention of the original addon I've migrated to a GUI panel that hosts a slew of options
to assist in map recreation.
- General Panel

    This panel contains global options that control all aspects of the addon.
	
	![](/docs/source/images/xmodel_panel_01.png?raw=true "xmodel_panel_01")
	- Asset Loading Mode
	   This controls how assets (specifically materials) are processed during import. Local Cache (Default) and Linked Library.
	   Local Cache stores assets in a temporary cache during the import process and feeds it back to operator to help with quicker
	   imports as well as more precise material creation. Its still WIP.
	   Linked Libary stores all assets in a blender library on import, slower import but better for asset management. This is the original
	   addons pipeline from skarndev.
	- Import Materials: When enabled it imports materials within the import operations, if disabled, well it doesnt. You can build materials
	  later with the material builder panel.
	- Use OverrideMaterials: For each mesh asset it parses the OverrideMaterials field in the umap json and replaces the base material with it.
	- Use PBR Maps: Attempts to create a PBR setup if the maps for it exists, otherwise it will fallback to a generic diffuse/base color setup.
	- Verbose Import: Provides verbose logging in the blender console
	- Game Profiles: [You can refer to skarndev's docs on how game profiles work](https://skarndev.github.io/umodel_tools/create_game_profile.html)
	- Game: Select a game from the list. If a game is not on the list then support must be added for it. You can also refer to the game profiles docs
	  to learn how to add a game
	- Export directory: Set the root export directory where Umodel/Fmodel exports assets to. This will be the anchor point for importing all assets
	- Asset Directory: Where created library assets are stored
	- Import Filter Directory: When a path is set the importer is limited to that directory and its subfolders. When not set, it starts from the export directory
	- Filter Keywords: Another filtering option to filter whats imported by keywords
	- Another filtering option to filter whats imported by folders. Set folder names like (Folder1, Folder2) and the importer will on look in those folders.

- Import Umap

    ![](/docs/source/images/xmodel_panel_02.png?raw=true "xmodel_panel_02")
	
    The original pipeline to import a single UMAP
	
- Import UMAP with Bounds

    ![](/docs/source/images/xmodel_panel_03.png?raw=true "xmodel_panel_03")
	
    This panel allows you to create a bound from an edge loop. The purpose is to import multiple umaps at once but within a section of the environment
	one at a time. You can create a simple plane mesh, select the edges and generate a bound which the importer will find assets in the umap that are
	within the min/max xy ranges of that bound. Importing a game map in one go is nearly impossible for blender to handle, so this allows you to import in chunks.
	- Mode: Bounding Box mode is pretty much a simple bound box structure. Take a plane, place it over an area you wish to import, scale it if needed, apply scale.
	        Select all four edges of the plane and click "Generate from selection" and itll generate the bounds.
			Footprint mode is the same concept but you can reshape the plane to have more precise bound generation. When bounds are generated itll add it to the list.
	- Path to UMAPS: Set the path to where all the games UMAPs are location. This will be where the importer will iterate and find any assets within bounds.
	- Phased Import: Is a simple optimization technique to not overload blender for big imports. It imports a specific amounts of UMAPs in phase. Default is 100 per phase
	  and in between phases, it process each batch to imports to its own collection thats hidden from blender as well as background tasks to keep blender from slowing down.
	- Build potential BPPs: If a game uses BPPs to manage assets alongside umaps, you can build those too and it will place them in their correct locations.

- BPP Builder

    ![](/docs/source/images/xmodel_panel_04.png?raw=true "xmodel_panel_04")
	
    Scans a folder of BPPs, then builds and imports them.

- Prop Builder

    ![](/docs/source/images/xmodel_panel_05.png?raw=true "xmodel_panel_05")
	
    A small manager to import specifically props form a game

- Material Builder

    ![](/docs/source/images/xmodel_panel_06.png?raw=true "xmodel_panel_06")
	
    Builds materials for selected mesh objects in the scene. The objects must maintain their original names for the builder to find them in games directory
	- Bake tints to attr: For materials that use tint, bakes the tint to a color attr. Only Mindseye supports this feature for now

- Landscape Material Compiler

    ![](/docs/source/images/xmodel_panel_07.png?raw=true "xmodel_panel_07")
	
    Builds materials for landscapes. Only MindsEye supports this currently until more landscape infrastructures are discovered

# Feature Framework
Currently some features/panels will only appear based on the selected game as some are only support for certain games. You will need to add support for the game
you're working on to have these features as well as any modifications required to use them, and then override them with True in the games python file. Current features flags:

- ENABLE_BPP_BUILDER: Whether the game has BPPs and support BPP importing
- ENABLE_COLOR_PALETTE_UNWRAPPER: Used if the game use tint materials. The palette unwrapper contains logic to unwrap and process tint values. (This is very MindsEye specific, generic support is needed)
- ENABLE_IMPORT_UMAP_WITH_BOUNDS = Whether the game can use bound based importing. This is only useful for certain types of games (Like games with city blocks)
- ENABLE_PROP_BUILDER = Enabled if the game has accessible props you can import
- ENABLE_MATERIAL_BUILDER = Enabled if the games infrastructure support building materials after import
- ENABLE_LANDSCAPE_MATERIAL_COMPILER = Whether the game has the infrastructure to support the current landscape material logic (Currently MindsEye only)

# Roadmap
- Adding more games. 
- Improving support for asset management.
- Improving map import.
- Improving lights import.
- Improving overall framework

# Credits
- Gildor, for creating [UEViewer](https://www.gildor.org/en/projects/umodel).
- Developers of [FModel](https://fmodel.app).
- Developers of these [import scripts](https://github.com/Ganonmaster/Blender-Scripts/tree/master/ue4map-tools).
- Befzz for developing the [.psk/.pskx importer](https://github.com/Befzz/blender3d_import_psk_psa).
- Loveslove for early testing.

# Disclaimer
3D assets and maps used by most of the games are copyrighted property of game's owners.
This software does not promote asset piracy and is intended for artistic and research purposes only.
