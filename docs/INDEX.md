# Docs

## API

- [API Reference](API.md) — Dokumentasi endpoint untuk frontend (auth, pathfinding,
  traffic, batch, shipment, tracking).

## Plan

- [Response Bisnis Paket](plan-response-bisnis-paket.md) — Batch pengiriman + response
  API bisnis paket + hub + COD + billing + tracker + login kurir (JWT).

## Fitur

- [Real-time Navigation & Auto-Rerouting](feature/realtime_navigation.md) — WS
  `/api/v1/ws/navigation`: progress rute, off-route detection, auto-reroute traffic.

## Testcase

- [Gabungan Posisi Driver + Navigation & Auto-Reroute](testcase/runbook_ws_navigation_reroute.md)
  — Runbook manual satu skenario: posisi driver (WS tracking), snapshot rute,
  progress, off-route, auto-reroute, sinergi lintas-WS.
