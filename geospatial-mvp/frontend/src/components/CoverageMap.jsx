import { useEffect, useRef, useState } from "react";
import L from "../leaflet";
import "leaflet/dist/leaflet.css";
import { api } from "../api/client";

const IRAN_CENTRE = [32.4, 53.7];

/**
 * Shows the footprint of every georeferenced image on a map of Iran.
 * Refreshes whenever `version` changes, so each batch upload fills the map in.
 */
export default function CoverageMap({ version }) {
  const container = useRef(null);
  const mapRef = useRef(null);
  const layerRef = useRef(null);
  const [count, setCount] = useState(0);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!container.current || mapRef.current) return undefined;
    const map = L.map(container.current).setView(IRAN_CENTRE, 5);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 19,
    }).addTo(map);
    layerRef.current = L.featureGroup().addTo(map);
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
      layerRef.current = null;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .listFootprints()
      .then(({ items }) => {
        if (cancelled || !layerRef.current) return;
        layerRef.current.clearLayers();
        const bounds = [];
        for (const { image_id: imageId, footprint: fp } of items) {
          const rect = [
            [fp.south, fp.west],
            [fp.north, fp.east],
          ];
          L.rectangle(rect, { color: "#2563eb", weight: 1, fillOpacity: 0.3 })
            .bindTooltip(imageId)
            .addTo(layerRef.current);
          bounds.push(rect);
        }
        setCount(items.length);
        setError(null);
        if (bounds.length && mapRef.current) {
          mapRef.current.fitBounds(bounds, { padding: [24, 24], maxZoom: 14 });
        }
      })
      .catch((err) => !cancelled && setError(err.message));
    return () => {
      cancelled = true;
    };
  }, [version]);

  return (
    <>
      <div ref={container} id="coverage-map" />
      <p className="map-note">
        {error ? `Could not load footprints: ${error}` : `${count} georeferenced footprint(s) shown.`}
      </p>
    </>
  );
}
