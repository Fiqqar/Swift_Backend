/* Encoded Polyline Algorithm (Google Maps / Mapbox), precision 5.
 * Decode string polyline (lat,lng) menjadi array [[lat,lng], ...]. */
function decodePolyline(encoded, precision) {
  precision = (precision === undefined) ? 5 : precision;
  var factor = Math.pow(10, precision);
  var coords = [];
  var index = 0;
  var lat = 0;
  var lng = 0;
  var len = encoded.length;
  while (index < len) {
    var result;
    result = decodePolylineSignedValue(encoded, index);
    lat += result.value;
    index = result.index;
    result = decodePolylineSignedValue(encoded, index);
    lng += result.value;
    index = result.index;
    coords.push([lat / factor, lng / factor]);
  }
  return coords;
}

function decodePolylineSignedValue(str, index) {
  var result = 0;
  var shift = 0;
  var b;
  do {
    b = str.charCodeAt(index) - 63;
    index += 1;
    result |= (b & 0x1f) << shift;
    shift += 5;
  } while (b >= 0x20);
  var value = (result & 1) ? ~(result >> 1) : (result >> 1);
  return { value: value, index: index };
}

/* Encode array [[lat,lng], ...] menjadi string polyline (precision 5).
 * Kebalikan decodePolyline — dipakai untuk membangun geometri leg darurat
 * di sisi klien bila backend tidak mengirim legs lengkap. */
function encodePolyline(coords, precision) {
  precision = (precision === undefined) ? 5 : precision;
  var factor = Math.pow(10, precision);
  var out = [];
  var prevLat = 0;
  var prevLng = 0;
  function encValue(value) {
    value = value << 1;
    if (value < 0) value = ~value;
    while (value >= 0x20) {
      out.push(String.fromCharCode((0x20 | (value & 0x1f)) + 63));
      value >>= 5;
    }
    out.push(String.fromCharCode(value + 63));
  }
  coords.forEach(function (c) {
    var lat = Math.round(c[0] * factor);
    var lng = Math.round(c[1] * factor);
    encValue(lat - prevLat);
    encValue(lng - prevLng);
    prevLat = lat;
    prevLng = lng;
  });
  return out.join('');
}
