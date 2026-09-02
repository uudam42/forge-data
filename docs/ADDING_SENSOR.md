# Adding a new sensor to Forge Data

This is a practical, step-by-step guide, not a design document — it
walks through exactly what a robotics engineer implements to add a new
sensor type, using the built-in **Force/Torque** plugin as the worked
example. If you're looking for *why* the architecture is shaped this
way, see `docs/DETAILED_GUIDE.md § Sensor plugin architecture (v2.3)`
instead.

**The target**: one new plugin package, one registration line, and
zero changes to synchronization, cleaning, QC, packaging, or catalog
code. If you find yourself editing any of those five to add your
sensor, stop — that's a sign the pipeline needs a genuinely new
capability (open an issue), not that your sensor is unusual.

## Checklist

- [ ] 1. Define sensor metadata
- [ ] 2. Define the schema (`app/resources/schemas/<sensor_type>_v1.json`)
- [ ] 3. Define integrity rules
- [ ] 4. Define canonical units
- [ ] 5. Define the normalization profile
- [ ] 6. Define field aliases (if any)
- [ ] 7. Add a feature extractor (optional)
- [ ] 8. Register the plugin
- [ ] 9. Run the contract test suite
- [ ] 10. Run an end-to-end test

## Worked example: Force/Torque

### 1. Define sensor metadata

Decide your `sensor_type` (a lowercase, stable identifier — this is
also your schema name, your synchronization stream name, and your
feature-extractor key; one identity, everywhere). For Force/Torque:
`sensor_type = "force_torque"`.

### 2. Define the schema

Add `app/resources/schemas/force_torque_v1.json` — the same JSON shape every existing
schema uses (see `app/resources/schemas/imu_v1.json` for reference):

```json
{
  "schema_name": "force_torque",
  "schema_version": "1.0.0",
  "record_type": "tabular",
  "fields": {
    "timestamp": {"type": "datetime", "required": true, "nullable": false, "format": "iso8601"},
    "force_x": {"type": "float", "required": true, "nullable": false},
    "force_y": {"type": "float", "required": true, "nullable": false},
    "force_z": {"type": "float", "required": true, "nullable": false},
    "torque_x": {"type": "float", "required": true, "nullable": false},
    "torque_y": {"type": "float", "required": true, "nullable": false},
    "torque_z": {"type": "float", "required": true, "nullable": false},
    "device_id": {"type": "string", "required": false, "nullable": true}
  },
  "allow_extra_fields": false,
  "metadata_requirements": {"sensor_type": "force_torque"}
}
```

This alone makes CSV/JSONL structural validation and integrity checking
*work* for your sensor — `SchemaRegistry` loads every `.json` file under
`app/resources/schemas/` automatically; nothing else needs to know this file exists
yet.

**Boundary to respect**: only structural correctness belongs here
(required fields, types, timestamp format). Physical plausibility
(finite values, reasonable magnitude) is Step 3's job — see the next
step.

### 3. Define integrity rules

Create `app/sensors/<sensor_type>/integrity.py`, subclassing
`IntegrityChecker` (`app/integrity/checks/base.py`). Reuse the existing
generic building blocks — `check_finite`, `TimestampSequenceChecker` —
rather than reimplementing them:

```python
class ForceTorqueIntegrityChecker(IntegrityChecker):
    def check_stream(self, records, accumulator):
        timestamp_checker = TimestampSequenceChecker()
        for record_number, record in records:
            ...  # per-field finiteness checks
            accumulator.add_all(timestamp_checker.check(record_number, record))
```

**Do not invent universal physical limits.** If your sensor benefits
from an extreme-value plausibility check, make the threshold
**optional and configurable** (default `None` = disabled), and make an
exceeded threshold a **warning**, never a hard failure — real hardware
varies far too much for this project to assert a "correct" range on
your behalf. See `ForceTorqueThresholds` for the pattern.

Add any new issue codes to `IntegrityErrorCode`
(`app/integrity/models.py`) — a plain additive enum entry, e.g.
`FORCE_TORQUE_FORCE_EXTREME`.

### 4. Define canonical units

If your sensor has physical units, add a `UnitDimension` per dimension
to `app/normalization/transforms/units.py` — this file is generic
infrastructure (a table of linear conversion factors), so this is a
*data* addition, not sensor-specific logic:

```python
FORCE = UnitDimension(name="force", canonical_unit="N", factors={"N": 1.0, "kN": 1000.0, "lbf": 4.4482216152605})
TORQUE = UnitDimension(name="torque", canonical_unit="N·m", factors={"N*m": 1.0, "mN*m": 0.001, "lbf*ft": 1.3558179483314004})
```

Only add unit aliases you can name an exact, unambiguous conversion
factor for, and write a test for each one. Never infer a unit from
numeric magnitude — a caller must always supply `source_units`
explicitly.

### 5. Define the normalization profile

Create `app/sensors/<sensor_type>/normalization.py` with a plain,
**declarative** `NormalizationProfile` instance — there is no new engine
code to write; `RecordNormalizer` (`app/normalization/profiles/base.py`)
already interprets any profile generically:

```python
FORCE_TORQUE_CANONICAL_V1 = NormalizationProfile(
    schema_name="force_torque",
    schema_version="1.0.0",
    profile_name="force_torque_canonical",
    profile_version="1.0.0",
    transform_version="1.0.0",
    field_aliases={"fx": "force_x", "fy": "force_y", "fz": "force_z", "tx": "torque_x", "ty": "torque_y", "tz": "torque_z"},
    field_dimensions={"force_x": "force", "force_y": "force", "force_z": "force", "torque_x": "torque", "torque_y": "torque", "torque_z": "torque"},
    dimensions={"force": FORCE, "torque": TORQUE},
    timestamp_field="timestamp",
)
```

### 6. Define field aliases

Aliases (step 5's `field_aliases`) map alternate **raw record field
names** to your canonical schema names — explicit only, never fuzzy
matching, and a genuine collision (both an alias and its canonical name
present in one record) fails loudly
(`AmbiguousFieldMappingError`), never silently picks one. Note: a raw
*uploaded file* must already use canonical column names to pass Step 2
schema validation in the first place — aliases matter once you're
working with parsed records from a source that used different names
(e.g. re-normalizing an export from a tool with its own naming
convention).

### 7. Add a feature extractor (optional)

If your sensor should contribute features to Step 7's transformation
stage, create `app/sensors/<sensor_type>/features.py` subclassing
`FeatureExtractor` (`app/transformation/features/base.py`). Reuse
`compute_statistic`/`validate_statistic_names`
(`app/transformation/features/statistics.py`) for standard statistics;
only add a *derived* feature if it's deterministic, clearly defined,
and doesn't invent a label:

```python
class ForceTorqueFeatureExtractor(FeatureExtractor):
    stream_name = "force_torque"  # must equal sensor_type
    def validate_config(self, config): ...
    def extract(self, rows, config): ...  # raw sequences, statistics, force_magnitude, torque_magnitude
```

This step is optional — a plugin with `feature_extractor=None` is
still fully valid for ingestion → validation → integrity →
normalization → synchronization → cleaning; it just won't produce
Step 7 features.

### 8. Register the plugin

Create `app/sensors/<sensor_type>/plugin.py` bundling everything above
into one `SensorPlugin`:

```python
FORCE_TORQUE_PLUGIN = SensorPlugin(
    sensor_type="force_torque",
    plugin_version="1.0.0",
    display_name="6-axis Force/Torque sensor",
    schema_version="1.0.0",
    integrity_checker=ForceTorqueIntegrityChecker(),
    normalization_profile=FORCE_TORQUE_CANONICAL_V1,
    feature_extractor=ForceTorqueFeatureExtractor(),
    numeric_fields=("force_x", "force_y", "force_z", "torque_x", "torque_y", "torque_z"),
    required_fields=("timestamp", "force_x", "force_y", "force_z", "torque_x", "torque_y", "torque_z"),
    canonical_units={"force": "N", "torque": "N·m"},
)
```

Then add exactly **one line** to `register_builtin_plugins()`
(`app/sensors/registry.py`):

```python
for plugin in (IMU_PLUGIN, GPS_PLUGIN, FORCE_TORQUE_PLUGIN):  # <- add yours here
    registry.register(plugin)
```

That's it. `IntegrityCheckerRegistry`, `NormalizationProfileRegistry`,
and `MultimodalWindowProfile`'s feature-extractor resolution all build
themselves from this registry — none of them need editing.

### 9. Run the contract test suite

Every built-in plugin is run through the same shared assertions in
`tests/sensors/contract.py` — add your plugin to the parametrized list
in `tests/sensors/test_plugin_registry.py`:

```python
_BUILTINS = (IMU_PLUGIN, GPS_PLUGIN, FORCE_TORQUE_PLUGIN, YOUR_PLUGIN)
```

`assert_plugin_contract()` checks: unique key, valid version metadata,
loadable schema, a declared timestamp field, consistent numeric-field
declarations, a resolvable integrity checker and normalization profile,
and (if present) a feature extractor whose `stream_name` matches your
`sensor_type`.

### 10. Run an end-to-end test

Push one record through every stage and confirm it reaches packaging.
`tests/sensors/pipeline_helpers.py` has reusable helpers
(`pipeline_to_normalized`, `synchronize`, `clean`, `transform`, `qc`,
`package`) already wired for three sensors — add a fourth CSV constant
and stream entry and the same helpers work unchanged. At minimum,
verify:

- validation/integrity/normalization succeed with your schema/profile
- synchronization includes your stream in `coverage`
- (if you added a feature extractor) your features appear in
  transformed samples with no NaN/Inf
- QC's report includes your features under `features.<sensor_type>.*`
  with no code change to QC
- packaging and catalog rebuild/verify succeed with no code change to
  either

## What you should NOT need to touch

If your sensor is a normal tabular time-series stream, you should not
need to edit:

- `app/synchronization/**` (alignment is driven by the schema's own
  field types — numeric fields interpolate, others use nearest — not by
  sensor identity)
- `app/cleaning/**` (rules operate on generic `row["streams"][name]`
  payloads)
- `app/qc/**` (feature discovery is recursive over whatever transformed
  samples contain)
- `app/packaging/**` (grouping/splitting operate on sample metadata,
  never sensor content)
- `app/catalog/**` (artifacts are indexed by `artifact_type`, never by
  sensor modality)

If you find yourself needing to, that's real signal the pipeline is
missing a genuine general capability — please open an issue describing
the gap rather than adding a sensor-specific branch to one of these
files.
