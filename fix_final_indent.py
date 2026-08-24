with open('app/api/v1/endpoints/pathfinding.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = '''        route_coords[0] = start_proj
        route_coords[-1] = goal_proj
    total_distance += haversine_distance((lat1, lon1), start_proj)
    total_distance += haversine_distance((lat2, lon2), goal_proj)

    response = RouteResponse('''

new_block = '''        route_coords[0] = start_proj
        route_coords[-1] = goal_proj
        total_distance += haversine_distance((lat1, lon1), start_proj)
        total_distance += haversine_distance((lat2, lon2), goal_proj)

    response = RouteResponse('''

with open('app/api/v1/endpoints/pathfinding.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace(old, new)

with open('app/api/v1/endpoints/pathfinding.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Fixed!')