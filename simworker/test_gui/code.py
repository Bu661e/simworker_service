def run(robot, objects):
    # Find red cube and blue cube
    red_cube = None
    blue_cube = None
    
    for obj in objects:
        if obj["id"] == "red_cube":
            red_cube = obj
        elif obj["id"] == "blue_cube":
            blue_cube = obj
    
    if red_cube is None or blue_cube is None:
        return
    
    # Pick up red cube from its current position
    red_pick_position = red_cube["pose"]["position_xyz_m"]
    
    # Calculate placement position on top of blue cube
    blue_center = blue_cube["pose"]["position_xyz_m"]
    cube_height = blue_cube["bbox_size_xyz_m"][2]  # 0.06m
    
    # Place red cube on top of blue cube
    # Center of red cube should be at blue center x,y but higher z
    # Blue cube top surface: blue_center[2] + cube_height/2
    # Red cube center on top: blue_center[2] + cube_height/2 + cube_height/2
    place_z = blue_center[2] + cube_height
    place_position = [blue_center[0], blue_center[1], place_z]
    
    # Execute pick and place
    robot.pick_and_place(
        pick_position=red_pick_position,
        place_position=place_position
    )