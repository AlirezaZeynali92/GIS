import { useEffect, useRef, useState } from "react";
import L from "../leaflet";
import "leaflet/dist/leaflet.css";
import "leaflet-draw";
import "leaflet-draw/dist/leaflet.draw.css";
import { rectangleToOverride } from "../api/utm";

/**
 * Shows where the image can be placed.
 *
 * When the backend already knows a placement (embedded geotransform or an
 * estimated GPS footprint) the map is informational. When georeferencing is
 * missing, the user may draw a rectangle and have it projected into the local
 * UTM zone — the only honest way to place an image whose position is known
 * from outside the file.
 */
export default function PlacementMap({ upload, placement, onPlacementChange }) {
  const container = useRef(null);
  const mapRef = useRef(null);
  const drawnRef = useRef(null);
  const [ready, setReady] = useState(false);
  const [feedback, setFeedback] = useState(null);

  const known = upload.georeferencing_status !== "missing";
  const gps = upload.exif_summary.gps;

  useEffect(() => {
    if (!container.current || mapRef.current) return undefined;

    const start = gps ? [gps.latitude, gps.longitude] : [32.4, 53.7];
    const map = L.map(container.current).setView(start, gps ? 14 : 5);
    mapRef.current = map;
    setReady(true);

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19,
    }).addTo(map);

    if (gps) {
      L.circleMarker([gps.latitude, gps.longitude], {
        radius: 6,
        color: "#0f2d5c",
        fillColor: "#3b82f6",
        fillOpacity: 0.9,
        weight: 2,
      })
        .bindPopup("EXIF GPS fix (camera position, accuracy not measured)")
        .addTo(map);
    }

    const drawn = new L.FeatureGroup();
    map.addLayer(drawn);
    drawnRef.current = drawn;

    map.on(L.Draw.Event.CREATED, (event) => {
      drawn.clearLayers();
      drawn.addLayer(event.layer);
      setFeedback(null);
    });

    if (!known) {
      map.addControl(
        new L.Control.Draw({
          draw: {
            rectangle: true,
            polygon: false,
            polyline: false,
            circle: false,
            circlemarker: false,
            marker: false,
          },
          edit: { featureGroup: drawn, edit: true, remove: true },
        }),
      );
    }

    return () => {
      map.remove();
      mapRef.current = null;
      drawnRef.current = null;
      setReady(false);
    };
  }, [gps, known]);

  const applyRectangle = () => {
    const drawn = drawnRef.current;
    const layers = drawn?.getLayers() ?? [];
    if (!layers.length) {
      setFeedback({ kind: "warn", text: "Draw a rectangle on the map first." });
      return;
    }
    const bounds = layers[0].getBounds();
    const { width, height } = upload.validation;
    const result = rectangleToOverride({
      northWest: bounds.getNorthWest(),
      southEast: bounds.getSouthEast(),
      width,
      height,
    });
    if (!result) {
      setFeedback({ kind: "error", text: "Rectangle has zero area; draw a larger box." });
      return;
    }
    onPlacementChange({
      payload: result.payload,
      summary: result.summary,
      bounds: {
        north: bounds.getNorth(),
        south: bounds.getSouth(),
        east: bounds.getEast(),
        west: bounds.getWest(),
      },
    });
    setFeedback(null);
  };

  const clear = () => {
    drawnRef.current?.clearLayers();
    onPlacementChange(null);
    setFeedback(null);
  };

  return (
    <>
      <div ref={container} id="map" />

      {known && (
        <p className="map-note">
          Backend georeferencing source: <strong>{upload.georeferencing_status}</strong>. It will be
          used as-is; manual placement is not required.
        </p>
      )}

      {!known && (
        <>
          <p className="map-note">
            This image carries no position of its own. If you know where it belongs, draw a
            rectangle over the covered area — it is projected into the local UTM zone and pixel size
            is computed from the image dimensions. Leave it empty to process without a georeferenced
            raster.
          </p>
          <div className="actions">
            <button className="secondary" onClick={applyRectangle} disabled={!ready}>
              Use drawn rectangle
            </button>
            {placement && (
              <button className="link" onClick={clear}>
                Clear placement
              </button>
            )}
          </div>
          {placement && (
            <div className="alert info">
              <strong>{placement.summary.crs}</strong> · pixel ≈{" "}
              {placement.summary.pixelSizeX.toFixed(3)} m ×{" "}
              {placement.summary.pixelSizeY.toFixed(3)} m · origin{" "}
              {placement.payload.origin_x}, {placement.payload.origin_y}
              {placement.summary.zoneWarning && (
                <div style={{ marginTop: 6 }}>{placement.summary.zoneWarning}</div>
              )}
            </div>
          )}
          {feedback && <div className={`alert ${feedback.kind}`}>{feedback.text}</div>}
        </>
      )}
    </>
  );
}
