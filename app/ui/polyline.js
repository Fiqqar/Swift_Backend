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
