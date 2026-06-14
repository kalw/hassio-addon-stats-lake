# ha-stats-lake

Long-term storage for Home Assistant sensor data — packaged as a native
Home Assistant add-on, no extra server required.

The add-on samples a list of entities every 30 minutes, appends them to flat
per-entity monthly CSV files, and nightly:

- consolidates new rows into a [DuckLake](https://ducklake.select/) (Parquet
  on object storage) hosted on **Cloudflare R2**
- syncs the raw CSVs to any `rclone` compatible destination as a cold backup

Visualization happens later, on demand, using the
[DuckDB UI extension](https://duckdb.org/docs/extensions/ui) pointed directly
at R2 — no dashboard server to maintain.

```
Home Assistant (ha_stats_lake add-on)
  ├─ every 30 min  → sample tracked entities → CSV (/data/ha_stats_data/)
  ├─ nightly        → consolidate CSV → DuckLake (Parquet) on R2
  └─ nightly        → rclone sync CSV → OneDrive (cold backup)

Your laptop (on demand)
  └─ duckdb -ui  → query the DuckLake on R2 directly
```

## Why this design

- **Push, not pull** — HA pushes data out, no inbound connections to your
  home network.
- **Flat files** — CSV is human-readable, `grep`-able, trivially synced in
  either direction with `rclone`. No database to corrupt or migrate.
- **Typed automatically** — entity type (`gauge` / `counter` / `binary`),
  unit, and label are all inferred from the entity's own HA attributes.
  Nothing to maintain by hand.
- **Config lives in the HA UI** — which entities to track is a single
  **Group helper**. Add or remove members in
  `Settings → Helpers`, no YAML edits, no restarts.
- **Native add-on** — runs in its own supervised Docker container; no
  AppDaemon dependency.

## Repository layout

```
ha_stats/
  config.yaml           ← HA add-on manifest + options schema
  Dockerfile            ← multi-stage Alpine build
  run.sh                ← container entrypoint
  ha_stats.py           ← asyncio app
  requirements.txt

.github/workflows/
  ci.yaml               ← lint + build on every PR
  publish-docker.yaml   ← publish to GHCR on release tags
```

---

## 1. Add this repository to Home Assistant

1. Go to **Settings → Add-ons → Add-on store**.
2. Click the three-dot menu (⋮) → **Repositories**.
3. Add the URL of this repo and click **Add**.
4. The **HA Stats Lake** add-on will appear in the store.
5. Click **Install**.

---

## 2. Create the entity group helper

This is the **only step you'll repeat** when adding or removing tracked
sensors — done entirely in the HA UI.

1. Go to `Settings → Devices & services → Helpers`
2. Click **+ Create helper → Group**
3. Choose type **Entity**
4. Name it `HA Stats tracked entities`
   (this creates `group.ha_stats_tracked_entities`)
5. In **Entities**, add every sensor / binary_sensor / switch you want
   recorded — e.g.:
   - `sensor.power_consumption`
   - `sensor.energy_total`
   - `sensor.temperature_living`
   - `binary_sensor.door_front`
6. Save

To add or remove a tracked entity later, just edit this group's member list.
No restart needed — the app re-reads it on every sample.

### How type/unit/label are determined

| HA attribute | Result |
|---|---|
| domain is `binary_sensor`, `switch`, or `input_boolean` | type = `binary` |
| `state_class` is `total` or `total_increasing` | type = `counter` |
| anything else numeric | type = `gauge` |
| `unit_of_measurement` | used as the unit |
| `friendly_name` | used as the display label |

Storage key is the entity_id with `.` replaced by `_`
(e.g. `sensor.power_consumption` → `sensor_power_consumption`).

---

## 3. Configure the add-on

In the add-on's **Configuration** tab, set:

| Option | Description | Default |
|--------|-------------|---------|
| `group_entity` | Entity ID of the Group helper | `group.ha_stats_tracked_entities` |
| `sample_interval_seconds` | How often to sample | `1800` |
| `consolidate_time` | When to run nightly DuckLake consolidation | `02:00:00` |
| `onedrive_sync_time` | When to run nightly rclone sync | `03:00:00` |
| `r2_bucket` | S3 URI of the R2 bucket (empty = disable) | — |
| `r2_endpoint` | R2 endpoint URL | — |
| `r2_key_id` | R2 Access Key ID | — |
| `r2_secret` | R2 Secret Access Key | — |
| `onedrive_remote` | rclone remote path (empty = disable) | — |

CSV data is stored in the add-on's persistent data volume at
`/data/ha_stats_data/` — no manual path configuration needed.

---

## 4. (Optional) Cloudflare R2 setup

1. Create a bucket, e.g. `ha-stats`, in the Cloudflare dashboard under **R2**.
2. Create an API token with **read & write** access to that bucket. Note the
   Access Key ID, Secret Access Key, and your Account ID.
3. Fill in `r2_bucket` (`s3://ha-stats/lake/`), `r2_endpoint`
   (`https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com`), `r2_key_id`, and
   `r2_secret` in the add-on configuration.

The first nightly run creates the DuckLake catalog and table automatically.

---

## 5. (Optional) OneDrive backup via rclone

1. On any machine with `rclone`, run `rclone config` and create a remote named
   `onedrive` following the
   [rclone OneDrive guide](https://rclone.org/onedrive/).
2. Copy the resulting `rclone.conf` into the add-on's `/data/` volume at
   `/data/.config/rclone/rclone.conf`.
3. Set `onedrive_remote: "onedrive:ha-backup"` in the add-on configuration.

---

## 6. Start the add-on

Click **Start**. In the **Log** tab, look for:

```
ha_stats starting: csv_dir=/data/ha_stats_data, group=group.ha_stats_tracked_entities, interval=1800s
```

Thirty minutes later you should see:

```
sampled N entities
```

---

## 7. Visualizing the data

On any machine with DuckDB installed:

```bash
duckdb -ui
```

Then in the SQL console:

```sql
INSTALL ducklake;
LOAD ducklake;

CREATE SECRET r2 (
    TYPE S3,
    KEY_ID '<your-r2-access-key-id>',
    SECRET '<your-r2-secret-access-key>',
    ENDPOINT '<your-account-id>.r2.cloudflarestorage.com',
    REGION 'auto'
);

ATTACH 'ducklake:s3://ha-stats/lake/catalog.duckdb' AS lake (
    DATA_PATH 's3://ha-stats/lake/data/'
);
```

Example queries:

```sql
-- last 7 days, all entities
SELECT entity, ts, value
FROM lake.stats
WHERE ts > now() - INTERVAL 7 DAY
ORDER BY entity, ts;

-- daily average power
SELECT date_trunc('day', ts) AS day, avg(value) AS avg_w
FROM lake.stats
WHERE entity = 'sensor_power_consumption'
GROUP BY 1 ORDER BY 1;

-- daily energy delta from a cumulative counter
SELECT date_trunc('day', ts) AS day,
       max(value) - min(value) AS kwh
FROM lake.stats
WHERE entity = 'sensor_energy_total'
GROUP BY 1 ORDER BY 1;
```

---

## Troubleshooting

- **No data appearing** — check the add-on log for `sampled N entities`. If
  `N` is 0, verify the group helper exists and has members.
- **Consolidation errors** — usually R2 credentials or bucket path. Test the
  `ATTACH` statement manually in a local `duckdb` shell first.
- **rclone errors** — verify `rclone.conf` is present at
  `/data/.config/rclone/rclone.conf` inside the add-on container and the
  remote name matches `onedrive_remote`.

## License

MIT — see [LICENSE](LICENSE).
