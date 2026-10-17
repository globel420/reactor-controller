import os
import sys
import sqlite3
import argparse
from datetime import datetime, timedelta

def parse_sqlite_timestamp(ts_str):
    if not ts_str:
        return datetime.utcnow()
    # Normalize ISO string
    s = str(ts_str).replace('T', ' ').replace('Z', '').strip()
    if '.' in s:
        format_str = "%Y-%m-%d %H:%M:%S.%f"
    else:
        format_str = "%Y-%m-%d %H:%M:%S"
    try:
        return datetime.strptime(s, format_str)
    except Exception:
        # Fallback: slice to 19 chars
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")

def run_backfill_and_prune(db_path: str, raw_retention_hours: float = 1.5, rollup_1m_retention_hours: float = 8.0):
    if not os.path.isabs(db_path):
        db_path = os.path.abspath(db_path)

    print("=== Database Backfill & Retention Policy Migration ===")
    print(f"Target Database: {db_path}")

    if not os.path.exists(db_path):
        print(f"Error: Database file does not exist at {db_path}")
        sys.exit(1)

    initial_size = os.path.getsize(db_path)
    print(f"Initial DB Size: {initial_size / (1024 * 1024):.2f} MB ({initial_size:,} bytes)")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Check tables exist
        for table in ["logs_1s", "logs_1m", "logs_10m"]:
            cursor.execute(f"SELECT count(*) FROM sqlite_master WHERE type='table' AND name='{table}'")
            if cursor.fetchone()[0] == 0:
                print(f"Error: Required table '{table}' does not exist in {db_path}")
                return

        cursor.execute("SELECT count(*) FROM logs_1s")
        initial_1s_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM logs_1m")
        initial_1m_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM logs_10m")
        initial_10m_count = cursor.fetchone()[0]

        print("\n--- Initial Row Counts ---")
        print(f"  Logs1s (1-second raw):     {initial_1s_count:,}")
        print(f"  Logs1m (1-minute rollup):   {initial_1m_count:,}")
        print(f"  Logs10m (10-minute rollup): {initial_10m_count:,}")

        if initial_1s_count == 0 and initial_1m_count == 0:
            print("\nNo historical logs found in database. Exiting.")
            return

        # 1. Backfill 1-Minute Rollups from 1-Second Logs
        print("\n[Step 1/4] Aggregating 1-second raw data into 1-minute rollup buckets...")
        sql_1m = """
            INSERT INTO logs_1m (
                run_id, timestamp, uptime, control_state, temp_feed_res, temp_feed_pre, temp_liq_reac,
                temp_gas_reac_int, temp_gas_reac_ext, h2_ppm, weight, pump_speed,
                heater_feed_pre, heater_liq_reac, heater_gas_reac,
                sp_feed_pre, sp_liq_reac, sp_gas_reac,
                ramp_feed_pre, ramp_liq_reac, ramp_gas_reac, dosing_rate_g_min, energy_kwh
            )
            SELECT 
                COALESCE(MAX(run_id), 1), MAX(timestamp), AVG(uptime), MAX(control_state), AVG(temp_feed_res), AVG(temp_feed_pre), AVG(temp_liq_reac),
                AVG(temp_gas_reac_int), AVG(temp_gas_reac_ext), AVG(h2_ppm), AVG(weight), AVG(pump_speed),
                AVG(heater_feed_pre), AVG(heater_liq_reac), AVG(heater_gas_reac),
                AVG(sp_feed_pre), AVG(sp_liq_reac), AVG(sp_gas_reac),
                AVG(ramp_feed_pre), AVG(ramp_liq_reac), AVG(ramp_gas_reac), AVG(dosing_rate_g_min), MAX(energy_kwh)
            FROM logs_1s
            GROUP BY strftime('%Y-%m-%d %H:%M:00', timestamp)
            HAVING MAX(timestamp) IS NOT NULL
        """
        cursor.execute(sql_1m)
        conn.commit()

        cursor.execute("SELECT count(*) FROM logs_1m")
        post_1m_count = cursor.fetchone()[0]
        print(f"  -> Generated {post_1m_count - initial_1m_count:,} new 1-minute rollup entries (Total: {post_1m_count:,}).")

        # 2. Backfill 10-Minute Rollups from 1-Minute Rollups
        print("\n[Step 2/4] Aggregating 1-minute data into 10-minute rollup buckets...")
        sql_10m = """
            INSERT INTO logs_10m (
                run_id, timestamp, uptime, control_state, temp_feed_res, temp_feed_pre, temp_liq_reac,
                temp_gas_reac_int, temp_gas_reac_ext, h2_ppm, weight, pump_speed,
                heater_feed_pre, heater_liq_reac, heater_gas_reac,
                sp_feed_pre, sp_liq_reac, sp_gas_reac,
                ramp_feed_pre, ramp_liq_reac, ramp_gas_reac, dosing_rate_g_min, energy_kwh
            )
            SELECT 
                COALESCE(MAX(run_id), 1), MAX(timestamp), AVG(uptime), MAX(control_state), AVG(temp_feed_res), AVG(temp_feed_pre), AVG(temp_liq_reac),
                AVG(temp_gas_reac_int), AVG(temp_gas_reac_ext), AVG(h2_ppm), AVG(weight), AVG(pump_speed),
                AVG(heater_feed_pre), AVG(heater_liq_reac), AVG(heater_gas_reac),
                AVG(sp_feed_pre), AVG(sp_liq_reac), AVG(sp_gas_reac),
                AVG(ramp_feed_pre), AVG(ramp_liq_reac), AVG(ramp_gas_reac), AVG(dosing_rate_g_min), MAX(energy_kwh)
            FROM logs_1m
            GROUP BY (strftime('%s', timestamp) / 600)
            HAVING MAX(timestamp) IS NOT NULL
        """
        cursor.execute(sql_10m)
        conn.commit()

        cursor.execute("SELECT count(*) FROM logs_10m")
        post_10m_count = cursor.fetchone()[0]
        print(f"  -> Generated {post_10m_count - initial_10m_count:,} new 10-minute rollup entries (Total: {post_10m_count:,}).")

        # 3. Apply Retention Truncation
        print("\n[Step 3/4] Applying Data Retention Policy...")
        cursor.execute("SELECT MAX(timestamp) FROM logs_1s")
        latest_ts_raw = cursor.fetchone()[0]
        if not latest_ts_raw:
            cursor.execute("SELECT MAX(timestamp) FROM logs_1m")
            latest_ts_raw = cursor.fetchone()[0]

        latest_dt = parse_sqlite_timestamp(latest_ts_raw)
        raw_cutoff = latest_dt - timedelta(hours=raw_retention_hours)
        rollup_cutoff = latest_dt - timedelta(hours=rollup_1m_retention_hours)

        raw_cutoff_str = raw_cutoff.strftime("%Y-%m-%d %H:%M:%S")
        rollup_cutoff_str = rollup_cutoff.strftime("%Y-%m-%d %H:%M:%S")

        print(f"  Latest Data Timestamp:        {latest_dt.strftime('%Y-%m-%d %H:%M:%S')}Z")
        print(f"  1-Second Raw Cutoff (-{raw_retention_hours}h):    {raw_cutoff_str}Z")
        print(f"  1-Minute Rollup Cutoff (-{rollup_1m_retention_hours}h): {rollup_cutoff_str}Z")

        cursor.execute("DELETE FROM logs_1s WHERE timestamp < ?", (raw_cutoff_str,))
        del_1s = cursor.rowcount
        cursor.execute("DELETE FROM logs_1m WHERE timestamp < ?", (rollup_cutoff_str,))
        del_1m = cursor.rowcount
        conn.commit()

        print(f"  -> Pruned {del_1s:,} expired 1-second raw records.")
        print(f"  -> Pruned {del_1m:,} expired 1-minute rollup records.")

        cursor.execute("SELECT count(*) FROM logs_1s")
        final_1s_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM logs_1m")
        final_1m_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM logs_10m")
        final_10m_count = cursor.fetchone()[0]

    finally:
        conn.close()

    # 4. Reclaim disk space via VACUUM
    print("\n[Step 4/4] Executing VACUUM to reclaim disk space and defragment database...")
    vacuum_conn = sqlite3.connect(db_path, isolation_level=None)
    vacuum_conn.execute("VACUUM;")
    vacuum_conn.close()

    final_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0

    print("\n=== Migration Complete ===")
    print(f"Final DB Size: {final_size / (1024 * 1024):.2f} MB ({final_size:,} bytes) [Space Saved: {(initial_size - final_size) / (1024 * 1024):.2f} MB]")
    print("Final Records Retained:")
    print(f"  Logs1s (Last {raw_retention_hours} hours):     {final_1s_count:,}")
    print(f"  Logs1m (Last {rollup_1m_retention_hours} hours):     {final_1m_count:,}")
    print(f"  Logs10m (Long-Term History): {final_10m_count:,}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One-off backfill and retention compaction for reactor logs (Zero-dependency SQLite3).")
    parser.add_argument("--db-path", default="./reactor_logs.db", help="Path to SQLite reactor logs database.")
    parser.add_argument("--raw-retention-hours", type=float, default=1.5, help="Hours of 1-second raw data to retain.")
    parser.add_argument("--rollup-1m-retention-hours", type=float, default=8.0, help="Hours of 1-minute rollup data to retain.")
    args = parser.parse_args()

    run_backfill_and_prune(args.db_path, args.raw_retention_hours, args.rollup_1m_retention_hours)
