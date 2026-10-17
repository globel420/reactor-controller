---
name: reactor-scada-grafana
description: >-
  Authoring, provisioning, and troubleshooting Grafana SCADA dashboards for the Reactor Controller
  using the SQLite datasource, cyberpunk visual styling, and continuous time-series telemetry.
---

# Reactor SCADA & Grafana Dashboard Runbook

Use this skill when modifying, adding panels to, or troubleshooting the Grafana SCADA analytics suite for `reactor-controller`.

## 1. frser-sqlite-datasource Constraints & Rules

The Grafana instance connects directly to the SQLite WAL database (`reactor_logs.db`) using the `frser-sqlite-datasource` plugin. To ensure panels load reliably without "No data" errors:

1. **Query Text Format**:
   - Always use `queryText`. Do NOT use `rawSql`.
   - Specify timeColumns in the panel target: `timeColumns: ["time"]`.
2. **Timestamp Formatting**:
   - The plugin requires epoch seconds for time series graphs:
     ```sql
     SELECT 
       CAST(strftime('%s', timestamp) AS INT) AS time,
       temp_liq_reac AS "Liquid Core °C",
       temp_gas_reac_int AS "Gas Headspace °C",
       temp_feed_pre AS "Preheater °C"
     FROM logs_1s
     ORDER BY timestamp ASC
     ```
3. **Macro Restrictions**:
   - Do NOT use `$__timeFilter(timestamp)` — this macro is unsupported by the SQLite plugin.
   - Grafana automatically filters the visible time range on the client side using the returned `time` epoch seconds column.
4. **Template Variables**:
   - Avoid multi-value variables in live operator panels unless using explicit SQL formatting (`IN (${run:csv})`). For live 1Hz panels, query `logs_1s` directly without filtering by run to guarantee live telemetry is always displayed.

---

## 2. Dark Cyberpunk SCADA Aesthetic Guidelines

When designing or updating panels in `reactor_operator_scada.json`:
* **Backgrounds**: Deep charcoal/slate (`#0b0f19` / `#111827`).
* **KPI Accent Colors**:
  * Liquid Core: Neon Cyan (`#06b6d4` / `#22d3ee`).
  * Gas Headspace: Neon Purple / Violet (`#a855f7` / `#c084fc`).
  * Preheater: Vibrant Amber / Orange (`#f59e0b` / `#fbbf24`).
  * Hydrogen MQ-8: Acid Emerald (`#10b981`) turning Crimson (`#ef4444`) on alert.
  * Reagent Mass / Dosing: Electric Rose (`#f43f5e`).
* **Waveform Styling**:
  * Fill opacity: 15% - 25% gradient area fill.
  * Line width: 2px smoothed splines.
  * Tooltip: Shared crosshair tooltip showing all thermal zones simultaneously.

---

## 3. Auto-Provisioning & Reload Workflow

Dashboards and datasources are provisioned automatically via Docker volume mounts:
* Datasource config: `grafana/provisioning/datasources/sqlite.yaml`
* Dashboards: `grafana/provisioning/dashboards/*.json`
* Provider manifest: `grafana/provisioning/dashboards/dashboards.yaml`

To reload dashboard changes without restarting Grafana:
```bash
# Push modified JSON to the running container or restart container:
docker restart reactor_grafana
```
