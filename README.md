# EcoFlow Home Energy Automation

Autonomous home energy management for EcoFlow battery systems, built to optimize electricity costs on real-time hourly pricing plans.

## What This Does

Controls an EcoFlow Delta Pro Ultra X + Smart Home Panel 3 to automatically charge the battery when grid electricity is cheap and discharge to power the home when prices are high. The system was built because EcoFlow's built-in TOU (time-of-use) scheduling doesn't support real-time hourly rate plans like ComEd's BESH.

## Architecture

- **Dashboard** (`ecoflow_web/`) — Flask + WebSocket web UI for monitoring and manual control. Dark-themed, responsive, runs on desktop and mobile. Handles MQTT communication with EcoFlow hardware, tracks energy usage, battery costs, and solar production.
- **The Arbiter** (`arbiter/`) — Autonomous decision engine running as a separate container. Polls the dashboard for system state, evaluates charge/discharge profitability using a willingness model that considers price spreads, battery SOC, time-of-day patterns, and outage reserves. Runs in dry-run mode by default for safe evaluation before going live.
- **Deployment** — Docker Compose on a Raspberry Pi 5, accessible locally and remotely via Tailscale.

## Key Features

- Real-time ComEd 5-minute price monitoring with hourly average tracking
- SOC-banded charging (different price thresholds at different battery levels)
- Discharge willingness model (SOC-aware, time-of-day aware, outage reserve protected)
- Battery cost tracking with full roundtrip efficiency accounting (AC-DC-AC losses)
- Hourly energy/cost accumulation with CSV logging and daily summaries
- Kia EV9 charge control integration (price-tiered)
- Enphase solar production monitoring
- Telegram notifications for mode changes, charge events, and price spikes
- Arbiter vs manual decision comparison tracking

## Hardware

- EcoFlow Delta Pro Ultra X (8 batteries, 49 kWh)
- EcoFlow Smart Home Panel 3 (gateway)
- Raspberry Pi 5 (controller)
- Enphase IQ8+ microinverters (solar, grid-tied)

## Background

This entire project was built collaboratively with Claude (Anthropic's AI). The EcoFlow control protocol is undocumented — figuring out the MQTT protobuf command structure required significant reverse engineering of the mobile app. The `memory/` directory contains detailed notes on the protobuf format, authentication flow, and hardware topology for anyone working with similar EcoFlow equipment.

Currently hardwired for ComEd BESH (real-time hourly pricing) but the architecture could be adapted to other utilities and rate plans.
