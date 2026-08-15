"""Encoded Polyline Algorithm (Google Maps / Mapbox), precision 5.

Input/output koordinat dalam urutan (lat, lng) — konsisten dengan konvensi
internal project (List[Tuple[float, float]]).
"""


def _encode_value(value: int) -> str:
    value = value << 1
    if value < 0:
        value = ~value
    out = []
    while value >= 0x20:
        out.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    out.append(chr(value + 63))
    return "".join(out)


def encode_polyline(coords, precision: int = 5) -> str:
    """Encode daftar titik (lat, lng) menjadi string polyline.

    Default precision 5 (standar Google Maps / Mapbox). Titik berupa iterable
    berisi tuple/sequence (lat, lng).
    """
    factor = 10 ** precision
    parts = []
    prev_lat = 0
    prev_lng = 0
    for lat, lng in coords:
        lat_e = round(lat * factor)
        lng_e = round(lng * factor)
        parts.append(_encode_value(lat_e - prev_lat))
        parts.append(_encode_value(lng_e - prev_lng))
        prev_lat, prev_lng = lat_e, lng_e
    return "".join(parts)


def _decode_delta(encoded: str, index: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        b = ord(encoded[index]) - 63
        index += 1
        result |= (b & 0x1F) << shift
        shift += 5
        if b < 0x20:
            break
    return (~(result >> 1) if (result & 1) else (result >> 1)), index


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode string polyline kembali ke list (lat, lng).

    Untuk keperluan test dan parity dengan frontend.
    """
    factor = 10 ** precision
    coords = []
    index = 0
    lat = 0
    lng = 0
    length = len(encoded)
    while index < length:
        dlat, index = _decode_delta(encoded, index)
        dlng, index = _decode_delta(encoded, index)
        lat += dlat
        lng += dlng
        coords.append((lat / factor, lng / factor))
    return coords
