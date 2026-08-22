"""Turn-by-turn maneuver extraction from route node sequence."""

import math
from typing import TypedDict

from app.services.pathfinding.core_a_star import edge_id, haversine_distance
from app.services.polyline import encode_polyline


class TurnInstruction(TypedDict):
    distance_m: float
    instruction: str
    type: str
    street_name: str | None
    bearing_change: int
    road_class: str | None
    is_exit: bool


class RouteStep(TypedDict):
    distance_m: float
    duration_s: float
    instruction: TurnInstruction
    polyline: str


_TURN_TYPES = {
    "left": "turn_left",
    "right": "turn_right",
    "slight_left": "turn_slight_left",
    "slight_right": "turn_slight_right",
    "straight": "continue",
    "roundabout": "roundabout_exit",
    "uturn": "uturn",
    "arrive": "arrive",
}

_CLASS_LABELS = {
    "motorway": "Jalan Tol",
    "motorway_link": "Jalan Tol",
    "trunk": "Arteri Utama",
    "trunk_link": "Arteri Utama",
    "primary": "Jalan Nasional",
    "primary_link": "Jalan Nasional",
    "secondary": "Jalan Provinsi",
    "secondary_link": "Jalan Provinsi",
    "tertiary": "Jalan Kabupaten",
    "tertiary_link": "Jalan Kabupaten",
    "unclassified": "Jalan Lokal",
    "residential": "Jalan Lokal",
    "service": "Jalan Lokal",
    "living_street": "Jalan Lokal",
    "road": "Jalan Lokal",
}


def _calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate bearing from point 1 to point 2 in degrees (0-360)."""
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlon_rad = math.radians(lon2 - lon1)

    y = math.sin(dlon_rad) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - \
        math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon_rad)

    bearing = math.degrees(math.atan2(y, x))
    return (bearing + 360) % 360


def _bearing_change(b1: float, b2: float) -> float:
    """Calculate signed bearing change from b1 to b2 (-180 to 180)."""
    diff = (b2 - b1 + 180) % 360 - 180
    return diff


def _classify_turn(change: float) -> str:
    """Classify bearing change into turn type."""
    if change < -135:
        return "uturn"
    elif change < -60:
        return "left"
    elif change < -20:
        return "slight_left"
    elif change < 20:
        return "straight"
    elif change < 60:
        return "slight_right"
    elif change < 135:
        return "right"
    else:
        return "uturn"


def _generate_instruction(turn_type: str, street_name: str | None,
                          road_class: str | None, is_exit: bool = False,
                          exit_number: int | None = None) -> str:
    """Generate human-readable turn instruction in Indonesian."""
    if turn_type == "arrive":
        return "Anda telah tiba di tujuan"

    base_phrases = {
        "turn_left": "Belok kiri",
        "turn_right": "Belok kanan",
        "turn_slight_left": "Belok sedikit kiri",
        "turn_slight_right": "Belok sedikit kanan",
        "continue": "Lurus",
        "roundabout_exit": "Keluar bundaran",
        "uturn": "Putar balik",
    }

    base = base_phrases.get(turn_type, "Lanjutkan")

    if is_exit and exit_number:
        return f"{base} ke keluar {exit_number}"

    parts = [base]

    if street_name:
        parts.append(f"ke {street_name}")
    elif road_class:
        label = _CLASS_LABELS.get(road_class, road_class.replace("_", " ").title())
        parts.append(f"ke {label}")

    return " ".join(parts)


def extract_steps(node_sequence: list[int],
                  route_coords: list[tuple[float, float]],
                  edge_classes: dict[int, str],
                  edge_names: dict[int, str],
                  speed_kmh: float = 40.0,
                  penalties: dict | None = None) -> list[RouteStep]:
    """
    Extract turn-by-turn steps from a node sequence and route coordinates.

    Args:
        node_sequence: List of node IDs from pathfinding
        route_coords: List of (lat, lon) coordinates along the route
        edge_classes: Dict mapping edge_id -> highway class
        edge_names: Dict mapping edge_id -> street name
        speed_kmh: Average speed for duration estimation
        penalties: Traffic penalties for duration calculation

    Returns:
        List of RouteStep with turn instructions
    """
    if not node_sequence or len(node_sequence) < 2:
        return []

    if not route_coords or len(route_coords) < 2:
        return []

    steps: list[RouteStep] = []

    coord_idx = 0
    total_nodes = len(node_sequence)

    for i in range(total_nodes - 1):
        u, v = node_sequence[i], node_sequence[i + 1]
        eid = edge_id(u, v)

        road_class = edge_classes.get(eid)
        street_name = edge_names.get(eid)

        start_coord = route_coords[coord_idx] if coord_idx < len(route_coords) else None
        end_coord = route_coords[coord_idx + 1] if coord_idx + 1 < len(route_coords) else None

        if not start_coord or not end_coord:
            coord_idx += 1
            continue

        seg_dist = haversine_distance(start_coord, end_coord)
        seg_time = seg_dist / (speed_kmh / 3.6)

        if penalties and eid in penalties:
            mult = penalties[eid]
            if mult == float("inf"):
                seg_time = float("inf")
            else:
                seg_time *= mult

        if i == total_nodes - 2:
            turn_type = "arrive"
            bearing_change = 0
        else:
            _ = node_sequence[i + 1], node_sequence[i + 2]

            next_coord = route_coords[coord_idx + 2] if coord_idx + 2 < len(route_coords) else end_coord

            b1 = _calculate_bearing(start_coord[0], start_coord[1], end_coord[0], end_coord[1])
            b2 = _calculate_bearing(end_coord[0], end_coord[1], next_coord[0], next_coord[1])

            change = _bearing_change(b1, b2)
            turn_type = _classify_turn(change)
            bearing_change = round(change)

        instruction: TurnInstruction = {
            "distance_m": round(seg_dist, 1),
            "instruction": _generate_instruction(
                _TURN_TYPES.get(turn_type, "continue"),
                street_name,
                road_class
            ),
            "type": _TURN_TYPES.get(turn_type, "continue"),
            "street_name": street_name,
            "bearing_change": bearing_change,
            "road_class": road_class,
            "is_exit": False,
        }

        step_coords = [start_coord, end_coord]
        if coord_idx + 2 < len(route_coords):
            step_coords.append(route_coords[coord_idx + 2])

        step_polyline = encode_polyline(step_coords, 5)

        steps.append({
            "distance_m": round(seg_dist, 1),
            "duration_s": round(seg_time, 1),
            "instruction": instruction,
            "polyline": step_polyline,
        })

        coord_idx += 1

    return steps


def find_current_step(steps: list[RouteStep],
                      current_lat: float, current_lon: float,
                      traveled_distance_m: float) -> int:
    """
    Find the current step index based on traveled distance.

    Args:
        steps: List of route steps
        current_lat: Current latitude
        current_lon: Current longitude
        traveled_distance_m: Distance traveled from route start

    Returns:
        Index of current step (0-based)
    """
    if not steps:
        return 0

    accumulated = 0.0
    for i, step in enumerate(steps):
        accumulated += step["distance_m"]
        if accumulated >= traveled_distance_m:
            return i

    return len(steps) - 1


def get_next_maneuver(steps: list[RouteStep], current_step_idx: int) -> TurnInstruction | None:
    """Get the next turn instruction after current step."""
    if current_step_idx + 1 < len(steps):
        return steps[current_step_idx + 1]["instruction"]
    return None


def get_traffic_level(penalties: dict | None, current_edge_id: int) -> str:
    """Determine traffic level based on penalty multiplier."""
    if not penalties or current_edge_id not in penalties:
        return "free"

    mult = penalties[current_edge_id]
    if mult == float("inf") or mult >= 2.5:
        return "heavy"
    elif mult >= 1.4:
        return "moderate"
    else:
        return "free"