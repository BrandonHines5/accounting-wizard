// UI-side mirror of the entity registry (config/entities.yaml) for the QBO
// Connections page. The Python side remains the source of truth; keep this in sync
// when entities are added/removed — same convention as lib/ruleLegend.js.
//
// `onQBO: false` marks entities still on QuickBooks Desktop (Hines Homes, Titan
// House). They're listed so you can authorize them from the same page the moment
// they migrate — just flip the flag here (cosmetic) and click Connect.
export const ENTITIES = [
  { id: "hines-homes", name: "Hines Homes LLC", onQBO: false },
  { id: "titan-house", name: "Titan House", onQBO: false },
  { id: "hope-filled", name: "Hope Filled Homes", onQBO: true },
  { id: "l2f-ventures", name: "L2F Ventures", onQBO: true },
  { id: "blue-tree-realty", name: "Blue Tree Realty", onQBO: true },
  { id: "stonebrook-poa", name: "Stonebrook POA", onQBO: true },
  { id: "mojuva", name: "Mojuva", onQBO: true },
  { id: "13525wm", name: "13525WM", onQBO: true },
];
