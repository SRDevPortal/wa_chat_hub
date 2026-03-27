# Phase 2 - Connector Architecture

## Goal
Support both official WhatsApp Business API and unofficial QR-based/personal WhatsApp through a normalized connector interface.

## Included in app code
- base connector abstraction
- official adapter
- personal adapter
- connector registry
- connector event ingestion API
- session doctype for QR/health tracking

## Design principle
ERPNext stores business truth; transport volatility stays behind adapters.

## Notes
This app now contains the architectural layer for Phase 2. Runtime session daemons, websocket engines, and provider-specific worker processes still need to be operated outside ERPNext during deployment.
