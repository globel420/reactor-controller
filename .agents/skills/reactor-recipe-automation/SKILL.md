---
name: reactor-recipe-automation
description: >-
  Designing, validating, and executing automated multi-stage chemical reactor recipes (Ramp, Soak, Dwell, Gravimetric Dosing)
  with condition-based stage branching and safety boundary enforcement.
---

# Reactor Recipe Automation Runbook

Use this skill when designing, encoding, or scheduling multi-step thermal and dosing reaction profiles.

## 1. Recipe Architecture & Stages

A chemical reactor run recipe consists of sequential stages executed under strict safety bounds:

```yaml
recipe_name: "Supercritical Methanol Esterification Batch 01"
version: "1.0"
safety_envelope:
  max_temp_preheater: 200.0   # °C
  max_temp_liquid: 230.0      # °C
  max_temp_gas: 260.0         # °C
  max_h2_ppm: 200.0           # ppm
  max_dosing_rate: 15.0       # g/min

stages:
  - stage: 1
    name: "Initial System Warmup"
    action: "ramp"
    setpoints:
      feed_pre: 120.0
      liq_reac: 160.0
      gas_reac: 180.0
    ramp_rates:
      feed_pre: 10.0   # °C/min
      liq_reac: 8.0
      gas_reac: 12.0
    completion_criteria:
      type: "stability"
      tolerance: 3.0   # ±3°C
      dwell_time: 120  # seconds

  - stage: 2
    name: "Preheat Soak & Prime"
    action: "dwell"
    duration_minutes: 15.0
    pump:
      mode: "manual"
      speed: 60        # RPM prime

  - stage: 3
    name: "Active Reaction & Gravimetric Dosing"
    action: "dose_and_heat"
    setpoints:
      feed_pre: 150.0
      liq_reac: 210.0
      gas_reac: 230.0
    pump:
      mode: "auto"
      target_rate_g_min: 5.5
      total_target_mass_g: 250.0
    completion_criteria:
      type: "mass_depleted"
      target_grams: 250.0

  - stage: 4
    name: "Reaction Dwell / Cook"
    action: "dwell"
    duration_minutes: 45.0
    pump:
      mode: "stop"

  - stage: 5
    name: "Controlled Cooldown to Standby"
    action: "ramp_down"
    setpoints:
      feed_pre: 40.0
      liq_reac: 50.0
      gas_reac: 50.0
    ramp_rates:
      feed_pre: -5.0
      liq_reac: -4.0
      gas_reac: -5.0
    completion_criteria:
      type: "all_below"
      threshold: 60.0
    final_state: 0     # Retreat to STANDBY
```

---

## 2. Recipe Execution Guardrails

1. **Pre-Flight Validation**:
   - Verify all stage setpoints fall strictly below the `safety_envelope` maximums.
   - Verify that reagent balance weight is sufficient to supply the total target dosing mass plus 10% reserve.
   - Verify that all PT100 RTDs report 0 errors in the preceding 60 seconds.
2. **Transition Triggers**:
   - `stability`: Advances only when all specified thermal zones remain within $\pm \text{tolerance}$ for the full `dwell_time`.
   - `mass_depleted`: Ingests real-time HX711 delta weight: $\Delta W = W_{\text{stage\_start}} - W_{\text{current}} \ge \text{target\_grams}$.
   - `duration`: Simple countdown timer with watchdog validation.
3. **Emergency Abort**:
   - Any hardware fault or safety interlock trip immediately aborts the active recipe, zeroes heaters, stops the pump, and preserves the partial batch state in `runs_metadata`.
