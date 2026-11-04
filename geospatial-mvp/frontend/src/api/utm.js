/**
 * WGS84 → UTM conversion for map-drawn placements.
 *
 * The backend only accepts projected CRS overrides, because a pixel size in
 * degrees is not a usable ground resolution. A drawn rectangle therefore gets
 * projected into its local UTM zone (metres) before submission.
 *
 * This is a standard transverse Mercator formula (WGS84 ellipsoid). It is
 * accurate to well under a metre inside a zone, which is far beyond the
 * precision of a hand-drawn rectangle — good enough for placement, wrong for
 * survey work, which is stated in the UI.
 */

const WGS84_A = 6378137.0;
const WGS84_F = 1 / 298.257223563;
const UTM_K0 = 0.9996;
const E2 = WGS84_F * (2 - WGS84_F);
const EP2 = E2 / (1 - E2);

export function utmZoneFor(latitude, longitude) {
  const zone = Math.min(Math.max(Math.floor((longitude + 180) / 6) + 1, 1), 60);
  const epsg = (latitude >= 0 ? 32600 : 32700) + zone;
  return { zone, epsg, centralMeridian: (zone - 1) * 6 - 180 + 3, southern: latitude < 0 };
}

export function latLonToUtm(latitude, longitude) {
  const { epsg, centralMeridian, southern } = utmZoneFor(latitude, longitude);
  const lat = (latitude * Math.PI) / 180;
  const lon = (longitude * Math.PI) / 180;
  const lonOrigin = (centralMeridian * Math.PI) / 180;

  const sinLat = Math.sin(lat);
  const cosLat = Math.cos(lat);
  const tanLat = Math.tan(lat);
  const n = WGS84_A / Math.sqrt(1 - E2 * sinLat * sinLat);
  const t = tanLat * tanLat;
  const c = EP2 * cosLat * cosLat;
  const a = cosLat * (lon - lonOrigin);

  const m =
    WGS84_A *
    ((1 - E2 / 4 - (3 * E2 * E2) / 64 - (5 * E2 ** 3) / 256) * lat -
      ((3 * E2) / 8 + (3 * E2 * E2) / 32 + (45 * E2 ** 3) / 1024) * Math.sin(2 * lat) +
      ((15 * E2 * E2) / 256 + (45 * E2 ** 3) / 1024) * Math.sin(4 * lat) -
      ((35 * E2 ** 3) / 3072) * Math.sin(6 * lat));

  const easting =
    UTM_K0 *
      n *
      (a +
        ((1 - t + c) * a ** 3) / 6 +
        ((5 - 18 * t + t * t + 72 * c - 58 * EP2) * a ** 5) / 120) +
    500000.0;

  let northing =
    UTM_K0 *
    (m +
      n *
        tanLat *
        ((a * a) / 2 +
          ((5 - t + 9 * c + 4 * c * c) * a ** 4) / 24 +
          ((61 - 58 * t + t * t + 600 * c - 330 * EP2) * a ** 6) / 720));

  if (southern) northing += 10000000.0;
  return { easting, northing, epsg };
}

/**
 * Turn a drawn north-west / south-east WGS84 box into the backend's override
 * payload: projected CRS, upper-left pixel centre, pixel size in metres.
 */
export function rectangleToOverride({ northWest, southEast, width, height }) {
  const topLeft = latLonToUtm(northWest.lat, northWest.lng);
  const bottomRight = latLonToUtm(southEast.lat, southEast.lng);
  const midLat = (northWest.lat + southEast.lat) / 2;
  const midLon = (northWest.lng + southEast.lng) / 2;
  const centre = latLonToUtm(midLat, midLon);

  const pixelSizeX = Math.abs(bottomRight.easting - topLeft.easting) / width;
  const pixelSizeY = Math.abs(topLeft.northing - bottomRight.northing) / height;
  if (!(pixelSizeX > 0) || !(pixelSizeY > 0)) return null;

  // The raster origin is the centre of its upper-left pixel.
  const originX = topLeft.easting + pixelSizeX / 2;
  const originY = topLeft.northing - pixelSizeY / 2;

  return {
    payload: {
      crs: `EPSG:${topLeft.epsg}`,
      origin_x: Number(originX.toFixed(3)),
      origin_y: Number(originY.toFixed(3)),
      pixel_size_x: Number(pixelSizeX.toFixed(4)),
      pixel_size_y: Number(pixelSizeY.toFixed(4)),
    },
    summary: {
      crs: `EPSG:${topLeft.epsg}`,
      pixelSizeX,
      pixelSizeY,
      midLat,
      midLon,
      zoneWarning: topLeft.epsg !== centre.epsg ? "Rectangle crosses a UTM zone boundary." : null,
    },
  };
}
