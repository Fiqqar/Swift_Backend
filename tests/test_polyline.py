from app.schemas.pathfinding import (
    OptimizedDeliveryLeg,
    RouteIncident,
    RouteOption,
    RouteResponse,
)
from app.services.polyline import decode_polyline, encode_polyline


def test_encode_decode_roundtrip_precision5():
    coords = [(-6.8048, 110.8385), (-6.8052, 110.8390), (-6.8100, 110.8500)]
    encoded = encode_polyline(coords)
    assert isinstance(encoded, str)
    decoded = decode_polyline(encoded)
    assert len(decoded) == len(coords)
    for (a_lat, a_lng), (b_lat, b_lng) in zip(coords, decoded):
        assert abs(a_lat - b_lat) < 1e-4
        assert abs(a_lng - b_lng) < 1e-4


def test_encode_polyline_single_point():
    decoded = decode_polyline(encode_polyline([(0.0, 0.0)]))
    assert decoded == [(0.0, 0.0)]


def test_encode_polyline_empty():
    assert encode_polyline([]) == ""


def test_decode_known_vector():
    # ((38.5, -120.2), (40.7, -120.95), (43.252, -126.453)) -> precision 5
    coords = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    encoded = encode_polyline(coords)
    decoded = decode_polyline(encoded)
    assert len(decoded) == len(coords)
    for (a_lat, a_lng), (b_lat, b_lng) in zip(coords, decoded):
        assert abs(a_lat - b_lat) < 1e-4
        assert abs(a_lng - b_lng) < 1e-4


def test_route_response_python_vs_json_serialization():
    coords = [(-6.8048, 110.8385), (-6.8100, 110.8500)]
    response = RouteResponse(
        status="success",
        total_distance_meters=1234.0,
        route_coordinates=coords,
    )
    py_dump = response.model_dump()
    assert py_dump["route_coordinates"] == coords
    json_dump = response.model_dump(mode="json")
    assert isinstance(json_dump["route_coordinates"], str)
    assert decode_polyline(json_dump["route_coordinates"])[0] == (
        -6.8048, 110.8385)


def test_traffic_segments_indices_aligned_after_encode():
    coords = [(-6.8048, 110.8385), (-6.8052, 110.8390), (-6.8100, 110.8500),
              (-6.8120, 110.8520)]
    response = RouteResponse(
        status="success",
        total_distance_meters=2000.0,
        route_coordinates=coords,
    )
    json_dump = response.model_dump(mode="json")
    decoded = decode_polyline(json_dump["route_coordinates"])
    assert len(decoded) == len(coords)


def test_route_option_geometry_serialized():
    coords = [(-6.8048, 110.8385), (-6.8100, 110.8500)]
    opt = RouteOption(
        route_id=1, is_best=True, distance_km=1.0, duration_mins=2.0,
        total_distance_meters=1000.0, route_coordinates=coords,
    )
    assert opt.model_dump()["route_coordinates"] == coords
    assert isinstance(opt.model_dump(mode="json")["route_coordinates"], str)


def test_incident_coordinates_serialized():
    inc = RouteIncident(
        type="congestion",
        location=(-6.8070, 110.8420),
        coordinates=[(-6.8070, 110.8420), (-6.8075, 110.8425)],
        multiplier=1.8,
        description="Kemacetan",
    )
    json_dump = inc.model_dump(mode="json")
    assert isinstance(json_dump["coordinates"], str)
    assert json_dump["location"] == [-6.8070, 110.8420]
    assert len(decode_polyline(json_dump["coordinates"])) == 2


def test_optimized_delivery_leg_geometry_serialized():
    leg = OptimizedDeliveryLeg(
        leg_index=0,
        stop_sequence_number=1,
        package_id=1,
        recipient_name="Budi",
        geometry=[(-6.8048, 110.8385), (-6.8100, 110.8500)],
        distance_km=1.0,
        duration_mins=2.0,
    )
    assert leg.model_dump()["geometry"] == [(-6.8048, 110.8385),
                                            (-6.8100, 110.8500)]
    assert isinstance(leg.model_dump(mode="json")["geometry"], str)
