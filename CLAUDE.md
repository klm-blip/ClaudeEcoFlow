# CLAUDE.md

## Development Workflow

### Branch Hygiene
- Before pushing a feature branch, always merge or rebase onto the latest master first.
- The Pi deployment runs whatever branch is checked out. If a feature branch is behind master, switching to it on the Pi will roll back important fixes (e.g., MQTT reliability, ESG command verification).
- Before switching branches on the Pi, check `git log --oneline HEAD..master` — if master is ahead, merge it first.

## Deployment (Raspberry Pi)

- Pi hostname: `kpi.local` (user: `pi`)
- Code location: `/home/pi/ecoflow`
- Deploy: `ssh pi@kpi.local && cd /home/pi/ecoflow && git pull && docker compose up -d --build`
- Docker services: `ecoflow-dashboard`, `arbiter`
- Dashboard URL: `http://kpi.local:5000`
