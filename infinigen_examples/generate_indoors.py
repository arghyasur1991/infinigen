# Copyright (c) Princeton University.
# This source code is licensed under the BSD 3-Clause license found in the LICENSE file in the root directory
# of this source tree.

import argparse
from pathlib import Path
import logging
from time import time
from numpy import deg2rad
import pprint
import copy

logging.basicConfig(
    format='[%(asctime)s.%(msecs)03d] [%(module)s] [%(levelname)s] | %(message)s',
    datefmt='%H:%M:%S', 
    level=logging.INFO
)

import bpy
from mathutils import Vector
import gin
import numpy as np
import trimesh
from numpy import deg2rad

from infinigen.assets import (lighting)
from infinigen.assets.wall_decorations.skirting_board import make_skirting_board
from infinigen.assets.utils.decorate import read_co
from infinigen.terrain import Terrain
from infinigen.assets.materials import invisible_to_camera

from infinigen.core.constraints import (
    constraint_language as cl, 
    reasoning as r,
    example_solver,
    checks, 
    usage_lookup
)

from infinigen.assets import (
    fluid, 
    cactus, 
    cactus, 
    trees, 
    monocot, 
    rocks, 
    underwater, 
    creatures, 
    lighting,
    weather
)
from infinigen.assets.scatters import grass, pebbles
from infinigen.assets.utils.decorate import read_co

from infinigen.core.placement import density, camera as cam_util, split_in_view

from infinigen_examples.indoor_constraint_examples import home_constraints
from infinigen.core import (
    execute_tasks, 
    surface, 
    init, 
    placement, 
    tags as t, 
    tagging
)
from infinigen.core.util import (blender as butil, pipeline)

from infinigen.core.constraints.example_solver.room import decorate as room_dec, constants
from infinigen.core.constraints.example_solver import state_def, greedy, populate, Solver

from infinigen.core.constraints.example_solver.room.constants import WALL_HEIGHT
from infinigen.core.util.camera import points_inview

from infinigen_examples.generate_nature import compose_nature # so gin can find it
from infinigen_examples.util import constraint_util as cu
from infinigen_examples.util.generate_indoors_util import (
    create_outdoor_backdrop,
    place_cam_overhead,
    overhead_view,
    hide_other_rooms,
    restrict_solving,
    apply_greedy_restriction
)

logger = logging.getLogger(__name__)

def default_greedy_stages():

    """Returns descriptions of what will be covered by each greedy stage of the solver.

    Any domain containing one or more VariableTags is greedy: it produces many separate domains, 
        one for each possible assignment of the unresolved variables.
    """

    on_floor = cl.StableAgainst({}, cu.floortags)
    on_wall = cl.StableAgainst({}, cu.walltags)
    on_ceiling = cl.StableAgainst({}, cu.ceilingtags)
    side = cl.StableAgainst({}, cu.side)

    all_room = r.Domain({t.Semantics.Room, -t.Semantics.Object})
    all_obj = r.Domain({t.Semantics.Object, -t.Semantics.Room})

    all_obj_in_room = all_obj.with_relation(cl.AnyRelation(), all_room.with_tags(cu.variable_room))
    primary = all_obj_in_room.with_relation(-cl.AnyRelation(), all_obj)

    greedy_stages = {}

    greedy_stages['rooms'] = all_room        
    
    greedy_stages['on_floor'] = primary.with_relation(on_floor, all_room)
    greedy_stages['on_wall'] = (
        primary.with_relation(-on_floor, all_room)
        .with_relation(-on_ceiling, all_room)
        .with_relation(on_wall, all_room)
    )
    greedy_stages['on_ceiling'] = (
        primary.with_relation(-on_floor, all_room)
        .with_relation(on_ceiling, all_room)
        .with_relation(-on_wall, all_room)
    )

    secondary = all_obj.with_relation(cl.AnyRelation(), primary.with_tags(cu.variable_obj)) 

    greedy_stages['side_obj'] = secondary.with_relation(side, all_obj)
    nonside = secondary.with_relation(-side, all_obj)

    greedy_stages['obj_ontop_obj'] = nonside.with_relation(cu.ontop, all_obj).with_relation(-cu.on, all_obj)
    greedy_stages['obj_on_support'] = nonside.with_relation(cu.on, all_obj).with_relation(-cu.ontop, all_obj)


    return greedy_stages


all_vars = [cu.variable_room, cu.variable_obj]

@gin.configurable
def compose_indoors(output_folder: Path, scene_seed: int, add_bottle=True, focus_on_bottle=True, **overrides):
    p = pipeline.RandomStageExecutor(scene_seed, output_folder, overrides)

    logger.debug(overrides)

    def add_coarse_terrain():
        terrain = Terrain(
            scene_seed, 
            surface.registry, 
            task='coarse',
            on_the_fly_asset_folder=output_folder / "assets"
        )
        terrain_mesh = terrain.coarse_terrain()
        # placement.density.set_tag_dict(terrain.tag_dict)
        return terrain, terrain_mesh

    terrain, terrain_mesh = p.run_stage('terrain', add_coarse_terrain, use_chance=False, default=(None, None))
    p.run_stage('sky_lighting', lighting.sky_lighting.add_lighting, use_chance=False)    

    consgraph = home_constraints()
    stages = default_greedy_stages()
    checks.check_all(consgraph, stages, all_vars)

    stages, consgraph, limits = restrict_solving(stages, consgraph)

    if overrides.get('restrict_single_supported_roomtype', False):
        restrict_parent_rooms = {
            np.random.choice([

                # Only these roomtypes have constraints written in home_constraints. 
                # Others will be empty-ish besides maybe storage and plants
                # TODO: add constraints to home_constraints for garages, offices, balconies, etc

                t.Semantics.Bedroom, 
                t.Semantics.LivingRoom, 
                t.Semantics.Kitchen, 
                t.Semantics.Bathroom,
                t.Semantics.DiningRoom
            ])
        }
        logger.info(f'Restricting to {restrict_parent_rooms}')
        apply_greedy_restriction(stages, restrict_parent_rooms, cu.variable_room)

    solver = Solver(output_folder=output_folder)
    def solve_rooms():
        return solver.solve_rooms(scene_seed, consgraph, stages['rooms'])
    state: state_def.State = p.run_stage('solve_rooms', solve_rooms, use_chance=False)

    def solve_large():
        assignments = greedy.iterate_assignments(
            stages['on_floor'], state, all_vars, limits, nonempty=True
        )
        for i, vars in enumerate(assignments):
            solver.solve_objects(
                consgraph, 
                stages['on_floor'], 
                var_assignments=vars, 
                n_steps=overrides['solve_steps_large'], 
                desc=f"on_floor_{i}", 
                abort_unsatisfied=overrides.get('abort_unsatisfied_large', False)
            )
        return solver.state
    state = p.run_stage('solve_large', solve_large, use_chance=False, default=state)

    solved_rooms = [
        state.objs[assignment[cu.variable_room]].obj
        for assignment in greedy.iterate_assignments(
            stages['on_floor'], state, [cu.variable_room], limits
        )
    ]    
    solved_bound_points = np.concatenate([butil.bounds(r) for r in solved_rooms])
    solved_bbox = (np.min(solved_bound_points, axis=0), np.max(solved_bound_points, axis=0))

    house_bbox = np.concatenate([butil.bounds(obj) for obj in solver.get_bpy_objects(r.Domain({t.Semantics.Room}))])
    house_bbox = (np.min(house_bbox, axis=0), np.max(house_bbox, axis=0))

    def add_bottle_to_scene(state):
        # Find a table or surface to place the bottle on
        surfaces = [obj for obj in state.objs.values() if t.Semantics.Table in obj.tags or t.Semantics.Surface in obj.tags]
        if not surfaces:
            logger.warning("No suitable surface found for placing the bottle.")
            return None

        surface = random.choice(surfaces)

        # Create a simple bottle mesh
        bpy.ops.mesh.primitive_cylinder_add(radius=0.05, depth=0.2)
        bottle = bpy.context.active_object
        bottle.name = "FocusBottle"

        # Position the bottle on the surface
        surface_bounds = butil.bounds(surface.obj)
        bottle.location = (
            random.uniform(surface_bounds[0][0], surface_bounds[1][0]),
            random.uniform(surface_bounds[0][1], surface_bounds[1][1]),
            surface_bounds[1][2] + 0.1  # Place slightly above the surface
        )

        # Add the bottle to the scene state
        state.objs['focus_bottle'] = state_def.ObjectSpec(obj=bottle, tags={t.Semantics.Bottle, t.Semantics.FocusObject})

        return bottle

    bottle = p.run_stage('add_bottle', add_bottle_to_scene, state, use_chance=False)

    camera_rigs = placement.camera.spawn_camera_rigs()

    def pose_cameras():
        nonroom_objs = [
            o.obj for o in state.objs.values() if t.Semantics.Room not in o.tags
        ]
        scene_objs = solved_rooms + nonroom_objs

        scene_preprocessed = placement.camera.camera_selection_preprocessing(
            terrain=None, 
            scene_objs=scene_objs
        )

        solved_floor_surface = butil.join_objects([
            tagging.extract_tagged_faces(o, {t.Subpart.SupportSurface})
            for o in solved_rooms
        ])
        
        placement.camera.configure_cameras(
            camera_rigs,
            scene_preprocessed=scene_preprocessed,
            init_surfaces=solved_floor_surface
        )

        return scene_preprocessed, nonroom_objs
    
    scene_preprocessed, nonroom_objs = p.run_stage('pose_cameras', pose_cameras, use_chance=False)

    # Select an object to follow (bottle if present, otherwise a small object)
    if focus_on_bottle and bottle:
        object_to_follow = bottle
    else:
        small_objects = [
            obj for obj in nonroom_objs 
            if obj.dimensions.length < 1.0 and any(t.Subpart.SupportSurface in parent.tags for parent in state.get_parents(obj))
        ]
        
        if not small_objects:
            small_objects = [obj for obj in nonroom_objs if obj.dimensions.length < 1.0]

        object_to_follow = random.choice(small_objects) if small_objects else None

    def animate_cameras():
        cam_util.animate_cameras(
            camera_rigs, 
            solved_bbox, 
            scene_preprocessed, 
            pois=[], 
            object_to_follow=object_to_follow
        )
    p.run_stage('animate_cameras', animate_cameras, use_chance=False, prereq='pose_cameras')

    def set_visibility_mode(mode):
        if bottle:
            bottle.hide_render = bottle.hide_viewport = (mode != 'BOTTLE_ONLY' and mode != 'FULL')
        for obj in bpy.data.objects:
            if obj != bottle and obj.type == 'MESH':
                obj.hide_render = obj.hide_viewport = (mode == 'BOTTLE_ONLY')

    # Add custom property to scene for visibility mode
    bpy.types.Scene.visibility_mode = bpy.props.EnumProperty(
        items=[
            ('BOTTLE_ONLY', "Render Only Bottle", "Render only the bottle"),
            ('ROOM_ONLY', "Render Only Room", "Render only the room and other objects"),
            ('FULL', "Render Full Scene", "Render the full scene")
        ],
        name="Visibility Mode",
        description="Control what parts of the scene are visible",
        default='FULL',
        update=lambda self, context: set_visibility_mode(self.visibility_mode)
    )

    # Set initial visibility
    set_visibility_mode('FULL')

    p.run_stage(
        'populate_intermediate_pholders', 
        populate.populate_state_placeholders, 
        solver.state,
        filter=t.Semantics.AssetPlaceholderForChildren, 
        final=False,
        use_chance=False
    )
    
    def solve_medium():
        n_steps = overrides['solve_steps_medium']
        for i, vars in enumerate(greedy.iterate_assignments(stages['on_wall'], state, all_vars, limits)):
            solver.solve_objects(consgraph, stages['on_wall'], vars, n_steps, desc=f"on_wall_{i}")
        for i, vars in enumerate(greedy.iterate_assignments(stages['on_ceiling'], state, all_vars, limits)):
            solver.solve_objects(consgraph, stages['on_ceiling'], vars, n_steps, desc=f"on_ceiling_{i}")
        for i, vars in enumerate(greedy.iterate_assignments(stages['side_obj'], state, all_vars, limits)):
            solver.solve_objects(consgraph, stages['side_obj'], vars, n_steps, desc=f"side_obj_{i}")
        return solver.state
    state = p.run_stage('solve_medium', solve_medium, use_chance=False, default=state)

    def solve_small():
        n_steps = overrides['solve_steps_small']
        for i, vars in enumerate(greedy.iterate_assignments(stages['obj_ontop_obj'], state, all_vars, limits)):
            solver.solve_objects(consgraph, stages['obj_ontop_obj'], vars, n_steps, desc=f"obj_ontop_obj_{i}")
        for i, vars in enumerate(greedy.iterate_assignments(stages['obj_on_support'], state, all_vars, limits)):
            solver.solve_objects(consgraph, stages['obj_on_support'], vars, n_steps, desc=f"obj_on_support_{i}")
        #for i, vars in enumerate(greedy.iterate_assignments(stages['tertiary'], state, all_vars, limits)):
        #    solver.solve_objects(consgraph, stages['tertiary'], vars, n_steps, desc=f"tertiary_{i}")
        return solver.state
    state = p.run_stage('solve_small', solve_small, use_chance=False, default=state)

    p.run_stage('populate_assets', populate.populate_state_placeholders, state, use_chance=False)
    
    door_filter = r.Domain({t.Semantics.Door}, [(cl.AnyRelation(), stages['rooms'])])
    window_filter = r.Domain({t.Semantics.Window}, [(cl.AnyRelation(), stages['rooms'])])    
    p.run_stage('room_doors', lambda: room_dec.populate_doors(solver.get_bpy_objects(door_filter)), use_chance=False)
    p.run_stage('room_windows', lambda: room_dec.populate_windows(solver.get_bpy_objects(window_filter)), use_chance=False)

    room_meshes = solver.get_bpy_objects(r.Domain({t.Semantics.Room}))
    p.run_stage('room_stairs', lambda: room_dec.room_stairs(state, room_meshes), use_chance=False)
    p.run_stage('skirting_floor', lambda: make_skirting_board(room_meshes, t.Subpart.SupportSurface))
    p.run_stage('skirting_ceiling', lambda: make_skirting_board(room_meshes, t.Subpart.Ceiling))

    rooms_meshed = butil.get_collection('placeholders:room_meshes')
    rooms_split = room_dec.split_rooms(list(rooms_meshed.objects))

    p.run_stage('room_walls', room_dec.room_walls, rooms_split['wall'].objects, use_chance=False)
    p.run_stage('room_pillars', room_dec.room_pillars, state, rooms_split['wall'].objects, use_chance=False)
    p.run_stage('room_floors', room_dec.room_floors, rooms_split['floor'].objects, use_chance=False)
    p.run_stage('room_ceilings', room_dec.room_ceilings, rooms_split['ceiling'].objects, use_chance=False)

    #state.print()
    state.to_json(output_folder / 'solve_state.json')

    cam = cam_util.get_camera(0, 0)
    
    def turn_off_lights():
        for o in bpy.data.objects:
            if o.type == 'LIGHT' and not o.data.cycles.is_portal:
                print(f'Deleting {o.name}')
                butil.delete(o)
    p.run_stage('lights_off', turn_off_lights)

    def invisible_room_ceilings():
        rooms_split['exterior'].hide_viewport = True
        rooms_split['exterior'].hide_render = True
        invisible_to_camera.apply(list(rooms_split['ceiling'].objects))        
        invisible_to_camera.apply([o for o in bpy.data.objects if 'CeilingLight' in o.name])
    p.run_stage('invisible_room_ceilings', invisible_room_ceilings, use_chance=False)

    p.run_stage(
        'overhead_cam', 
        place_cam_overhead, 
        cam=camera_rigs[0], 
        bbox=solved_bbox,
        use_chance=False
    )

    p.run_stage(
        'hide_other_rooms',
        hide_other_rooms,
        state, 
        rooms_split, 
        keep_rooms=[r.name for r in solved_rooms],
        use_chance=False
    )

    height = p.run_stage(
        'nature_backdrop', 
        create_outdoor_backdrop, 
        terrain, 
        house_bbox=house_bbox,
        cam=cam,
        p=p, 
        params=overrides,
        use_chance=False,
        prereq='terrain',
        default=0,
    )

    if overrides.get('topview', False):
        rooms_split['exterior'].hide_viewport = True
        rooms_split['ceiling'].hide_viewport = True
        rooms_split['exterior'].hide_render = True
        rooms_split['ceiling'].hide_render = True
        for group in ['wall', 'floor']:
            for wall in rooms_split[group].objects:
                for mat in wall.data.materials:
                    for n in mat.node_tree.nodes:
                        if n.type == 'BSDF_PRINCIPLED':
                            n.inputs['Alpha'].default_value = overrides.get('alpha_walls', 1.)
        bbox = np.concatenate([read_co(r) + np.array(r.location)[np.newaxis, :] for r in rooms_meshed.objects])
        camera = camera_rigs[0].children[0]
        camera_rigs[0].location = 0, 0, 0
        camera_rigs[0].rotation_euler = 0, 0, 0
        bpy.contexScene.camera = camera
        rot_x = deg2rad(overrides.get('topview_rot_x', 0))
        rot_z = deg2rad(overrides.get('topview_rot_z', 0))
        camera.rotation_euler = rot_x, 0, rot_z
        mean = np.mean(bbox, 0)
        for cam_dist in np.exp(np.linspace(1., 5., 500)):
            camera.location = mean[0] + cam_dist * np.sin(rot_x) * np.sin(rot_z), mean[1] - cam_dist * np.sin(
                rot_x) * np.cos(rot_z), mean[2] - WALL_HEIGHT / 2 + cam_dist * np.cos(rot_x)
            bpy.context.view_layer.update()
            inview = points_inview(bbox, camera)
            if inview.all():
                for area in bpy.contexScreen.areas:
                    if area.type == 'VIEW_3D':
                        area.spaces.active.region_3d.view_perspective = 'CAMERA'
                        break
                break
    
    return {
        "height_offset": height,
        "whole_bbox": house_bbox,
    }
    


def main(args):
    scene_seed = init.apply_scene_seed(args.seed)
    init.apply_gin_configs(
        configs=args.configs, 
        overrides=args.overrides,
        configs_folder='infinigen_examples/configs_indoor'
    )
    constants.initialize_constants()

    execute_tasks.main(compose_scene_func=compose_indoors, input_folder=args.input_folder,
                       output_folder=args.output_folder, task=args.task, task_uniqname=args.task_uniqname,
                       scene_seed=scene_seed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--output_folder', type=Path)
    parser.add_argument('--input_folder', type=Path, default=None)
    parser.add_argument('-s', '--seed', default=None, help="The seed used to generate the scene")
    parser.add_argument('-t', '--task', nargs='+', default=['coarse'],
                        choices=['coarse', 'populate', 'fine_terrain', 'ground_truth', 'render', 'mesh_save', 'export'])
    parser.add_argument('-g', '--configs', nargs='+', default=['base'],
                        help='Set of config files for gin (separated by spaces) '
                             'e.g. --gin_config file1 file2 (exclude .gin from path)')
    parser.add_argument('-p', '--overrides', nargs='+', default=[],
                        help='Parameter settings that override config defaults '
                             'e.g. --gin_param module_1.a=2 module_2.b=3')
    parser.add_argument('--task_uniqname', type=str, default=None)
    parser.add_argument('-d', '--debug', type=str, nargs='*', default=None)

    args = init.parse_args_blender(parser)
    logging.getLogger("infinigen").setLevel(logging.INFO)
    logging.getLogger("infinigen.core.nodes.node_wrangler").setLevel(logging.CRITICAL)

    if args.debug is not None:
        for name in logging.root.manager.loggerDict:
            if not name.startswith('infinigen'):
                continue
            if len(args.debug) == 0 or any(name.endswith(x) for x in args.debug):
                logging.getLogger(name).setLevel(logging.DEBUG)

    main(args)