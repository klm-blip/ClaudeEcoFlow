# EcoFlow Home Energy Dashboard — UI Redesign Spec

**Purpose**: This document specifies a complete UI redesign of the EcoFlow Energy Dashboard.
Hand this file to Claude Code along with `PROJECT_SNAPSHOT.txt` and `ecoflow_dashboard.py` for full context.

**Current state**: Working tkinter dashboard (`ecoflow_dashboard.py`, ~1600 lines) with MQTT telemetry,
ComEd pricing, automated charge/discharge control, and manual controls. All backend logic is proven and functional.

**Goal**: Rebuild the frontend as a responsive web UI served from a Raspberry Pi, while preserving
all existing backend logic (MQTT, protobuf commands, ComEd polling, automation controller).

---

## 1. Architecture

### Overview

```
┌─────────────────────────────────────────────────┐
│  Raspberry Pi                                   │
│                                                 │
│  ┌──────────────────────────────────────────┐   │
│  │  Python backend (Flask)                  │   │
│  │  ├── MQTT handler (paho-mqtt)            │   │
│  │  ├── Protobuf command builder            │   │
│  │  ├── ComEd price poller                  │   │
│  │  ├── Automation controller               │   │
│  │  ├── Threshold persistence (JSON file)   │   │
│  │  └── WebSocket server (flask-sock)       │   │
│  └──────────────────────────────────────────┘   │
│           │ WebSocket (ws://pi:5000/ws)          │
│  ┌──────────────────────────────────────────┐   │
│  │  Static files served by Flask            │   │
│  │  └── index.html (single file, no build)  │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
         │
         │  LAN / Tailscale VPN
         ▼
┌─────────────────────┐
│  Browser (any device)│
│  Phone / tablet /    │
│  desktop / TV        │
└─────────────────────┘
```

### Key architectural decisions

- **Flask + flask-sock**, not FastAPI. Simpler, lighter on the Pi, sufficient for this use case.
- **Single HTML file** with inline CSS and JS. No React, no npm, no build step. Vanilla JS.
- **WebSocket for all real-time data**. The backend pushes state updates; the frontend renders them.
  The frontend sends commands (mode switch, charge start/stop, threshold changes) via WebSocket messages.
- **No CDN dependencies**. Everything works on a local network with no internet required.
  The only external calls the *backend* makes are to ComEd's pricing API and EcoFlow's MQTT broker.
- **Responsive design via CSS media queries**. One HTML file serves both desktop and mobile layouts.
  The breakpoint is `768px`. Above = desktop layout. Below = tabbed mobile layout.

### Remote access

Use **Tailscale** for remote access. Install on the Pi and on phones/laptops.
Access via Tailscale IP (e.g., `http://100.x.x.x:5000`).
No port forwarding, no public exposure, no DNS required. Free for personal use.
This is a deployment concern, not an app concern — the dashboard code doesn't change.

### Backend refactor approach

The existing `ecoflow_dashboard.py` contains both backend logic and tkinter UI code interleaved.
The refactor should:

1. **Extract backend logic** into a clean Python module (or small set of modules):
   - `mqtt_handler.py` — MQTT connection, telemetry parsing, command publishing
   - `protobuf_commands.py` — all protobuf command builders (mode, charge, power, etc.)
   - `comed_poller.py` — ComEd API polling, price classification, trend calculation
   - `automation.py` — AutoController, AutoThresholds, decision logic
   - `app.py` — Flask app, WebSocket handler, serves index.html

2. **Preserve all existing logic exactly**. The protobuf encoding, MQTT topics, telemetry parsing,
   credential loading, automation decision logic — all of this is battle-tested and should not be
   rewritten, only reorganized.

3. **State management**: The backend maintains a single `DashboardState` object that holds:
   - `PowerState` (grid_w, load_w, battery_w, soc_pct, volt_a, volt_b, op_mode)
   - `PriceState` (price_5min, price_hour, running_hour_avg, effective_price, trend, tier, history_5min)
   - `AutoThresholds` (all threshold values, persisted to JSON)
   - `AutoController` state (enabled, last_decision, last_cmd_ts)
   - `HistoryBuffer` (15 min of power readings)
   - `commands_live` flag (dry run vs live)
   - `command_log` (recent command entries)
   - Connection status

   On every state change (MQTT telemetry, price update, automation action), the backend
   serializes the full state to JSON and pushes it over the WebSocket.

### WebSocket API

**Server → Client (state push)**:
```json
{
  "type": "state",
  "power": {
    "grid_w": 1840.0,
    "load_w": 1840.0,
    "battery_w": 3000.0,
    "soc_pct": 62.0,
    "volt_a": 121.4,
    "volt_b": 121.8,
    "op_mode": 1,
    "mode_label": "BACKUP",
    "stale": false
  },
  "price": {
    "price_5min": 3.2,
    "price_hour": 3.4,
    "running_hour_avg": 3.1,
    "effective_price": 3.4,
    "trend": "flat",
    "trend_slope": 0.05,
    "tier": "LOW",
    "tier_color": "#3fb950",
    "history_5min": [[1710700000, 3.1], [1710699700, 3.3], ...],
    "error": ""
  },
  "thresholds": {
    "discharge_above": 8.0,
    "soc_emergency": 10.0,
    "rate_emergency": 3000,
    "emergency_max_price": 15.0,
    "low_soc_ceil": 20.0,
    "low_charge_below": 2.0,
    "low_rate": 6000,
    "mid_soc_ceil": 60.0,
    "mid_charge_below": 1.5,
    "mid_rate": 3000,
    "high_soc_max": 80.0,
    "high_charge_below": -1.0,
    "high_rate": 1500
  },
  "auto": {
    "enabled": true,
    "last_decision": "mid band, charging 3kW — price 3.2c < 1.5c threshold"
  },
  "history": {
    "times": [...],
    "grid": [...],
    "load": [...],
    "battery": [...]
  },
  "commands_live": false,
  "command_log": [
    {"ts": "14:32:08", "live": false, "text": "AUTO: mid band, charging 3kW"},
    ...
  ],
  "mqtt_connected": true,
  "solar": null
}
```

**Client → Server (commands)**:
```json
{"cmd": "mode", "value": "backup"}
{"cmd": "mode", "value": "self_powered"}
{"cmd": "charge_start", "rate": 3000, "max_soc": 80}
{"cmd": "charge_stop"}
{"cmd": "apply_rate", "rate": 3000, "max_soc": 80}
{"cmd": "toggle_auto"}
{"cmd": "toggle_live"}
{"cmd": "set_threshold", "key": "low_charge_below", "value": 2.5}
```

---

## 2. UI Layout — Desktop (>768px)

The desktop layout is a single-column vertical stack that fills the viewport.
No horizontal sidebar. Everything flows top to bottom.

### Section order (top to bottom):

```
┌─────────────────────────────────────────────────────┐
│  TOP BAR: title, connection status, dry/live badge,  │
│           clock                                      │
├─────────┬─────────┬─────────────────────────────────┤
│  PRICE  │ BATTERY │  POWER FLOW                     │
│  pill   │  pill   │  pill                            │
│  (1/3)  │  (1/3)  │  (1/3)                          │
├─────────┴─────────┴─────────────────────────────────┤
│                                                      │
│  POWER FLOW DIAGRAM                                  │
│  Grid(left) ──→ House(center) ──→ Home(right)        │
│  Battery(bottom-left) ↔ House                        │
│  Solar(top-center, dimmed until data available)      │
│  Animated dashed lines showing active flows          │
│  [ Future: house photo replaces geometric house ]    │
│                                                      │
├──────────────────────────────────────────────────────┤
│  AUTOMATION STRIP: one-line status of what auto is   │
│  doing and why                                       │
├──────────────────────┬───────────────────────────────┤
│  SOC CHARGE BANDS    │  MODE / CHARGE / READINGS     │
│  (tap-to-expand)     │                               │
│  ┌── High 60→80% ──┐│  [Backup] [Self-Powered]      │
│  │  <-1.0c · 1.5kW ││  Rate: ====○========= 3,000W  │
│  └──────────────────┘│  SOC:  ========○===== 80%     │
│  ┌── Mid 20→60% ◄──┐│  [Start Charge] [Stop Charge] │
│  │  expanded detail ││                               │
│  │  +/- adjusters   ││  Automation: [Enabled] [Off]  │
│  └──────────────────┘│                               │
│  ┌── Low 10→20% ────┐│  Grid:    1,840 W             │
│  │  <2.0c · 6kW    ││  Home:    1,840 W             │
│  └──────────────────┘│  Battery: +3,000 W            │
│  ┌── Emerg 0→10% ───┐│  SOC:     62%                 │
│  │  <15.0c · 3kW   ││  L1/L2:   121.4 / 121.8V     │
│  └──────────────────┘│  Solar:   —                   │
│  Discharge above 8.0c│                               │
├──────────────────────┴───────────────────────────────┤
│  HISTORY CHART (15-min, lines for grid/home/batt/solar)│
├──────────────────────────────────────────────────────┤
│  COMMAND LOG (scrollable, monospace, last ~20 entries)│
└──────────────────────────────────────────────────────┘
```

---

## 3. UI Layout — Mobile (<768px)

Tabbed layout. Three tabs: **Status**, **Controls**, **History**.

### Tab 1: Status (default)

This is the "glance at your phone" view. No controls, just information.

```
┌────────────────────────────┐
│  Home energy    [Live]     │
│  [Status] [Controls] [History]
├────────────────────────────┤
│  ┌──────┬──────┬──────┐   │
│  │ 3.2c │ 62%  │1840W │   │
│  │ Low  │ +3kW │Grid  │   │
│  └──────┴──────┴──────┘   │
├────────────────────────────┤
│                            │
│  Flow diagram              │
│  (portrait-optimized,      │
│   same 4-node layout)     │
│                            │
├────────────────────────────┤
│  ● Auto · Charging 3kW ·  │
│    Mid band · 3.2c < 1.5c │
├────────────────────────────┤
│  Grid draw      1,840 W   │
│  Home load      1,840 W   │
│  Battery       +3,000 W   │
│  L1 / L2    121.4/121.8V  │
│  Solar              —     │
│  Hour avg          3.4c   │
└────────────────────────────┘
```

### Tab 2: Controls

Full-width, generous touch targets (minimum 44px tap areas).

```
┌────────────────────────────┐
│  [Status] [Controls] [History]
├────────────────────────────┤
│  OPERATING MODE            │
│  [  Backup  ] [Self-Powered]│
├────────────────────────────┤
│  CHARGE CONTROL            │
│  Rate: ====○======= 3,000W│
│  SOC:  =======○====== 80% │
│  [Start Charge] [Stop Chg] │
├────────────────────────────┤
│  AUTOMATION                │
│  [ Enabled ] [ Disabled ]  │
├────────────────────────────┤
│  SOC CHARGE BANDS          │
│  (tap to expand, same as   │
│   desktop but full-width,  │
│   bigger +/- buttons)      │
│                            │
│  Discharge above    8.0c   │
│                  [−] [+]   │
└────────────────────────────┘
```

### Tab 3: History

```
┌────────────────────────────┐
│  [Status] [Controls] [History]
├────────────────────────────┤
│  POWER HISTORY (15 min)    │
│  ┌────────────────────┐    │
│  │  taller chart       │    │
│  │  (~120px height)    │    │
│  └────────────────────┘    │
├────────────────────────────┤
│  PRICE HISTORY (1 hour)    │
│  ┌────────────────────┐    │
│  │  price sparkline    │    │
│  │  (~80px height)     │    │
│  └────────────────────┘    │
├────────────────────────────┤
│  COMMAND LOG               │
│  ┌────────────────────┐    │
│  │  scrollable log     │    │
│  │  monospace text     │    │
│  └────────────────────┘    │
└────────────────────────────┘
```

---

## 4. Color System

### Principles

- No red for normal battery operations. Red = danger/emergency only.
- Colors should feel neutral-to-positive during normal operation.
- Dark background with light text (dark theme, like the current dashboard).
- The specific hex values below are starting points; maintain the semantic meaning even if exact shades change.

### Semantic color assignments

| Element               | Color       | Hex       | Usage                                     |
|-----------------------|-------------|-----------|-------------------------------------------|
| Grid power            | Blue        | `#378ADD` | Grid import/export lines and labels       |
| Home load             | Teal        | `#1D9E75` | Power flowing to home circuits            |
| Battery charging      | Green       | `#639922` | Energy flowing into battery               |
| Battery discharging   | Amber/Gold  | `#EF9F27` | Energy flowing out of battery to home     |
| Battery idle          | Gray        | `#888780` | No significant charge or discharge        |
| Solar production      | Amber/Gold  | `#EF9F27` | Solar energy (same hue family as discharge)|
| Emergency / danger    | Red         | `#E24B4A` | Emergency SOC band, connection lost       |
| Success / connected   | Green       | `#639922` | Connection status, automation enabled     |
| Warning / dry run     | Amber       | `#EF9F27` | Dry run badge, caution states             |

### Price tier colors

| Tier       | Condition          | Color     |
|------------|--------------------|-----------|
| NEGATIVE   | < 0 c/kWh          | `#639922` (bright green) |
| VERY LOW   | 0–3 c/kWh          | `#639922` (green)        |
| LOW        | 3–6 c/kWh          | `#378ADD` (blue)         |
| MODERATE   | 6–9.6 c/kWh        | `#EF9F27` (amber)        |
| HIGH       | 9.6–14 c/kWh       | `#E24B4A` (red)          |
| SPIKE      | > 14 c/kWh         | `#E24B4A` (bright red)   |

### Flow line states

| State                     | Style                                | Color          |
|---------------------------|--------------------------------------|----------------|
| Active (energy flowing)   | Animated dashed, 2.5–3px stroke      | Semantic color |
| Idle (no flow)            | Static dashed, 1.5px, 20% opacity    | Gray           |
| Future (no data yet)      | Static dashed, 1px, 15% opacity      | Gray           |

Flow line animation: CSS `@keyframes` animating `stroke-dashoffset`. Direction of animation
indicates direction of energy flow. Speed can scale with wattage if desired (subtle, not distracting).

---

## 5. Component Specifications

### 5.1 Top Bar

Sticky at top. Contains:
- App title: "Home energy"
- Connection badge: "Live" (green) / "Connected" (amber) / "Connecting..." (red)
- Commands badge: "Dry run" (amber) / "Live" (green) — clickable to toggle
- Current time (HH:MM)

### 5.2 Three Pillars (summary strip)

Three equal-width cells in a horizontal grid. Each contains:

**Price pill**:
- Hero number: effective price in large text, colored by tier
- Subtitle: tier label + trend arrow (↑ rising, → stable, ↓ falling)
- Detail line: hour avg + running avg
- Mini sparkline: last 12 five-minute prices as vertical bars

**Battery pill**:
- Hero number: SOC percentage, colored green/amber/red by level
- Subtitle: charge state ("Charging +3,000W" / "Discharging -2,400W" / "Idle")
  - Charging text in green, discharging in amber, idle in gray
- Detail line: "Delta Pro Ultra · [band name]"
- Mini SOC bar: horizontal bar showing band colors with current SOC marker

**Power flow pill**:
- Hero number: home load wattage
- Subtitle: primary power source ("Grid: 1,840W" / "Solar: 2,100W + Grid: 400W")
- Detail line: line voltages + current mode

### 5.3 Power Flow Diagram

SVG-based, four-node layout.

**Nodes**:
- **Grid** (left): Circle, blue border. Shows "Import" / "Export" / "Idle".
- **Solar** (top-center): Rectangle with panel-line detail, amber border.
  Initially rendered at reduced opacity with dashed border and "coming soon" label.
  When Enphase data is available, lights up with production wattage.
- **House** (center): Geometric house shape (roof triangle + body rectangle).
  This is a placeholder — will eventually be replaced with a stylized photo of the actual house.
  Shows "SHP3" label and current operating mode.
- **Battery** (bottom-left): Rounded rectangle with fill indicator.
  Shows SOC percentage and charge state label.
- **Home load** (right): Circle, teal border. Shows total home wattage.

**Flow lines**: Curved SVG paths between nodes with animated dashes.
See Color System §Flow line states for styling.
Wattage labels on each active flow line.

**Color legend**: Small legend at bottom of flow area showing line color meanings.

### 5.4 Automation Status Strip

Single horizontal bar between the flow diagram and controls.
Contains:
- Green dot (enabled) or gray dot (disabled)
- One-line description of current automation state
- Example: "Auto enabled · Charging 3,000W · SOC in mid band (20–60%) · Price 3.2c below 1.5c threshold"
- When disabled: "Automation disabled — manual control"

### 5.5 SOC Charge Bands (tap-to-expand)

A vertical stack of band rows, ordered top to bottom: High → Mid → Low → Emergency.

Each band row (collapsed) shows:
- Band name + SOC range (e.g., "Mid · 20→60%")
- Price threshold + charge rate (e.g., "<1.5c · 3kW")
- If current SOC is in this band: small marker badge showing SOC % with green accent

Tapping a band expands it to reveal +/- adjusters for each parameter.
Only one band is expanded at a time (accordion behavior).

**Band parameters (expanded view)**:

| Band      | Adjustable fields                              | Notes                                |
|-----------|-------------------------------------------------|--------------------------------------|
| High      | Ceiling (max SOC), Price <, Rate                | Floor = mid band's ceiling (linked)  |
| Mid       | Ceiling, Price <, Rate                          | Floor = low band's ceiling (linked)  |
| Low       | Ceiling, Price <, Rate                          | Floor = emergency ceiling (linked)   |
| Emergency | Ceiling, Max price, Rate                        | Floor = 0% (fixed)                   |

**Linked threshold chain**: The four SOC boundaries are:
1. Emergency ceiling (= Low floor)
2. Low ceiling (= Mid floor)
3. Mid ceiling (= High floor)
4. High ceiling (= Max SOC, stop charging above this)

When any ceiling is adjusted, the adjacent band's floor updates automatically.
Validation: each ceiling must be > the one below it. The UI should prevent inversions
(e.g., don't let mid ceiling go below low ceiling). Clamp or block the adjustment.

**Emergency band**: Unlike other bands which charge at "any price below X",
emergency has a `max_price` field with a high default (e.g., 15c or 20c).
The intent is: "charge when SOC is critically low, but not if electricity costs a fortune."
Label this clearly — "Max price" not "Charge below" — to differentiate from the other bands.

**+/- button behavior**: Each tap adjusts by a fixed step:
- SOC thresholds: step = 5%
- Price thresholds: step = 0.5c
- Charge rates: step = 500W (or 100W for finer control — consider which is better for touch)

On any threshold change:
1. Update the threshold state on the backend
2. Persist to `ecoflow_thresholds.json`
3. Trigger immediate automation re-evaluation (if auto is enabled)

**Discharge threshold**: Shown below the bands as a separate row.
"Discharge above [8.0c] [-] [+]"
This controls when the system switches to self-powered mode (battery powers home).

### 5.6 Mode & Charge Controls

**Operating mode**: Two toggle buttons — "Backup" and "Self-Powered".
Active mode is highlighted (blue accent). Tapping sends the mode command immediately.

**Charge control**:
- Rate slider: 600W to 12,000W, step 100W. Shows current value.
- Max SOC slider: 50% to 100%, step 5%. Shows current value.
- "Start charge" button (green accent): sends charge_start with current rate and max SOC.
- "Stop charge" button (amber accent, NOT red): sends charge_stop.

**Slider behavior**: Debounce auto-apply. When the user moves a slider, wait 3–5 seconds
after they stop, then auto-send the rate/SOC update. Also have an explicit "Apply" option
if desired, but the debounced auto-apply is the primary mechanism.

### 5.7 Live Readings

Simple two-column grid:
- Grid draw: W
- Home load: W
- Battery: +/- W (green if charging, amber if discharging, gray if idle)
- SOC: % (green > 50%, amber 20–50%, red < 20%)
- L1 / L2: voltages
- Solar: W (gray/dash until Enphase integration)

### 5.8 History Chart

Canvas or SVG line chart showing last 15 minutes.
Three (eventually four) series: Grid (blue), Home (teal), Battery (green), Solar (amber, future).

Y-axis: watts, auto-scaled to data range.
X-axis: time labels at -15m, -12m, -9m, -6m, -3m, now.

Zero line visible when data crosses zero (battery charge/discharge).

### 5.9 Command Log

Scrollable monospace text area showing recent commands.
Each entry: `HH:MM:SS [LIVE] command description` or `HH:MM:SS [DRY] command description`
Cap at ~30 entries, oldest roll off the top.

---

## 6. SOC Band Logic — Updated

### Current model (in ecoflow_dashboard.py)

The existing code uses `soc_emergency`, `low_soc_ceil`, `mid_soc_ceil`, `high_soc_max`
as ceiling-based thresholds. This is being reworked to a floor-based reading
(see `project_dashboard_todo.md` item 1) but the underlying data model is similar.

### New model (this spec)

Four boundary values define the bands:

```
100% ┬─────────────────────┐
     │  Above max: stop     │
max  ├─────────────────────┤  e.g., 80%
     │  HIGH band           │
mid  ├─────────────────────┤  e.g., 60%
     │  MID band            │
low  ├─────────────────────┤  e.g., 20%
     │  LOW band            │
emg  ├─────────────────────┤  e.g., 10%
     │  EMERGENCY band      │
  0% └─────────────────────┘
```

**Threshold fields** (persisted in `ecoflow_thresholds.json`):

```json
{
  "soc_emergency_ceil": 10,
  "low_soc_ceil": 20,
  "mid_soc_ceil": 60,
  "high_soc_max": 80,

  "emergency_max_price": 15.0,
  "emergency_rate": 3000,

  "low_charge_below": 2.0,
  "low_rate": 6000,

  "mid_charge_below": 1.5,
  "mid_rate": 3000,

  "high_charge_below": -1.0,
  "high_rate": 1500,

  "discharge_above": 8.0,
  "trend_lookahead": true
}
```

**Decision logic** (pseudocode):

```
given: soc (current %), price (effective ¢/kWh)

if soc >= high_soc_max:
    → stop charging (above max)

else if soc >= mid_soc_ceil:
    → HIGH band: charge if price < high_charge_below, at high_rate

else if soc >= low_soc_ceil:
    → MID band: charge if price < mid_charge_below, at mid_rate

else if soc >= soc_emergency_ceil:
    → LOW band: charge if price < low_charge_below, at low_rate

else:
    → EMERGENCY: charge if price < emergency_max_price, at emergency_rate

if price >= discharge_above and soc > soc_emergency_ceil:
    → switch to self-powered (discharge battery to home)
```

**Linked ceiling validation**: When adjusting a ceiling, enforce:
`soc_emergency_ceil < low_soc_ceil < mid_soc_ceil < high_soc_max`

If the user tries to raise `low_soc_ceil` to equal or exceed `mid_soc_ceil`,
block the change (don't adjust). Minimum gap between adjacent ceilings: 5%.

---

## 7. Future Integrations

### 7.1 House Photo

The geometric house in the flow diagram is a placeholder.
Eventually replace with a stylized/processed photo of the actual house.
The image should be:
- A cropped/simplified version of a real photo
- Processed to work on dark backgrounds (possibly with a subtle outline or glow)
- Stored as a static asset served by Flask
- Referenced in the SVG flow diagram as an `<image>` element

This is a cosmetic enhancement. Do not block any functionality on it.

### 7.2 Enphase Solar Integration

The Enphase IQ microinverter system is grid-tied and reports production data.
The Enphase API (or local Envoy device) can provide:
- Current production (watts)
- Daily/monthly production totals

Integration plan:
- Add an `EnphasePoller` class similar to `ComedPoller`
- Add solar production to the state object (`solar_w` field)
- The flow diagram's Solar node lights up when data is available
- A solar→house flow line animates showing production
- The history chart gains a fourth series (solar, in amber)
- The readings table shows solar production

This is informational only — no control over the Enphase system.
The Solar node in the UI should be present from day one (dimmed/placeholder)
so the layout doesn't shift when solar data arrives.

---

## 8. Existing Code Reference

### Files to preserve logic from

| File | What to extract |
|------|----------------|
| `ecoflow_dashboard.py` lines 1–180 | Config, credential loading, constants |
| `ecoflow_dashboard.py` PowerState, PriceState classes | State dataclasses |
| `ecoflow_dashboard.py` parse_payload() | MQTT telemetry parser (protobuf decoding) |
| `ecoflow_dashboard.py` HistoryBuffer | Time-series ring buffer |
| `ecoflow_dashboard.py` ComedPoller | ComEd API polling + price analysis |
| `ecoflow_dashboard.py` AutoThresholds | Threshold persistence |
| `ecoflow_dashboard.py` AutoController.decide() | Core automation decision logic |
| `ecoflow_dashboard.py` build_*_command() functions | Protobuf command builders |
| `ecoflow_dashboard.py` MQTTHandler | MQTT connection management |

### What to discard

Everything in the `Dashboard` class related to tkinter: `_build_window()`, `_build_topbar()`,
`_build_flow_canvas()`, `_build_history_canvas()`, `_build_controls()`, `_draw_flow()`,
`_draw_history()`, all tk widget references. The `_tick()` loop is replaced by WebSocket pushes.

### Known issues to fix during refactor

1. **MIN_HOLD too long**: Currently 120 seconds between commands. Consider making this configurable
   (default 30–60 seconds) and exposed in the UI or thresholds file.
2. **Re-evaluation frequency**: Currently only re-evaluates on price updates (every 5 min) or
   telemetry messages. Add a periodic 30-second re-evaluation timer independent of data events.
3. **Debounced slider**: Current tkinter slider has a 5-second debounce. Keep similar behavior
   in the web UI (3–5 second debounce after slider stops moving).

---

## 9. Implementation Order

Suggested phasing for Claude Code:

### Phase 1: Backend refactor
- Extract Python backend modules from `ecoflow_dashboard.py`
- Create Flask app with WebSocket endpoint
- Verify MQTT, ComEd, automation all work headless (no UI)
- Serve a minimal test page that shows raw JSON state

### Phase 2: Desktop UI
- Build `index.html` with the desktop layout
- Implement WebSocket connection and state rendering
- Power flow SVG diagram with animated lines
- Three pillars, automation strip, readings
- SOC band controls with tap-to-expand
- Mode and charge controls
- History chart and command log

### Phase 3: Mobile responsive
- Add CSS media query breakpoint at 768px
- Implement tab switching (Status / Controls / History)
- Optimize touch targets and layout for portrait phone

### Phase 4: Polish
- Flow line animation tuning (speed scaling with wattage)
- Price sparkline with threshold reference lines
- History chart with auto-scaling Y axis
- Connection loss handling and reconnection UI
- Error states and edge cases

### Phase 5: Future
- House photo integration
- Enphase solar data integration
- Tailscale deployment documentation
- Auto-auth renewal (JWT refresh)
