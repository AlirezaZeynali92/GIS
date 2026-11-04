import L from "leaflet";

// leaflet-draw is a classic script that expects a global `L`; expose ours before it loads.
window.L = L;

export default L;
