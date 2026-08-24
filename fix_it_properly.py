with open('app/api/v1/endpoints/pathfinding.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Fix lines 521-524 (0-indexed: 520-523)
# Line 521: 'route_coords[-1] = goal_proj\n' - should be 8 spaces
# Line 522: '    total_distance += haversine_distance((lat1, lon1), start_proj)\n' - should be 8 spaces
# Line 523: '    total_distance += haversine_distance((lat2, lon2), goal_proj)\n' - should be 8 spaces
# Line 524: ' \n' - should be empty line
# Line 525: '    response = RouteResponse(\n' - should be 8 spaces

lines[520] = '        route_coords[-1] = goal_proj\n'
lines[521] = '        total_distance += haversine_distance((lat1, lon1), start_proj)\n'
lines[522] = '        total_distance += haversine_distance((lat2, lon2), goal_proj)\n'
lines[523] = '\n'
lines[524] = '    response = RouteResponse(\n'

with open('app/api/v1/endpoints/pathfinding.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)

print('Fixed!')