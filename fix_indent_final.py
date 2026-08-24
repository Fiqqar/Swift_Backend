with open('app/api/v1/endpoints/pathfinding.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = """        route_coords[-1] = goal_proj
        total_distance += haversine_distance((lat1, lon1), start_proj)
        total_distance += haversine_distance((lat2, lon2), goal_proj)

    response = RouteResponse(
        status="success",
        total_distance_meters=round(total_distance, 2),
        route_coordinates=route_coords,
        source=pg.source,
        warning=pg.warning,
        graph_radius_meters=pg.radius,
        traffic_segments=_traffic_segments(node_sequence, penalties),
        legs=_build_legs_from_response(pg, node_sequence, route_coords, lat1, lon1, lat2, lon2),
    )
    await set_route(redis, key, response.model_dump())
    return response, node_sequence"""

new_block = """        route_coords[-1] = goal_proj
        total_distance += haversine_distance((lat1, lon1), start_proj)
        total_distance += haversine_distance((lat2, lon2), goal_proj)

    response = RouteResponse(
        status="success",
        total_distance_meters=round(total_distance, 2),
        route_coordinates=route_coords,
        source=pg.source,
        warning=pg.warning,
        graph_radius_meters=pg.radius,
        traffic_segments=_traffic_segments(node_sequence, penalties),
        legs=_build_legs_from_response(pg, node_sequence, route_coords, lat1, lon1, lat2, lon2),
    )
    await set_route(redis, key, response.model_dump())
    return response, node_sequence"""

with open('app/api/v1/endpoints/pathfinding.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(old, new)

with open('app/api/v1/endpoints/pathfinding.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Fixed!')