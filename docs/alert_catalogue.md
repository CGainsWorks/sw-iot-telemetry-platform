# Alert catalogue

Thirteen rules, defined in [`config/alert_rules.yaml`](../config/alert_rules.yaml),
loaded and validated at startup, synchronised into `alert_rules` so the API can
join an alert to the rule that produced it.

A rule that fails to parse stops the service starting. An alert that silently
never fires looks exactly like everything being fine, which makes it the worst
outcome available to an alerting system.

## The rules

| id | Severity | Condition | Hold | Channels |
| --- | --- | --- | --- | --- |
| `high-temperature` | warning | mean `temperature_c` > 35 | 2m | log, webhook |
| `critical-temperature` | critical | max `temperature_c` > 55 | 1m | log, webhook |
| `humidity-out-of-band` | info | mean `humidity_pct` > 75 | 10m | log |
| `co2-poor-air` | warning | mean `co2_ppm` > 1400 | 5m | log |
| `low-battery` | warning | mean `battery_v` < 3.55 | 15m | log, webhook |
| `critical-battery` | critical | mean `battery_v` < 3.40 | 10m | log, webhook |
| `weak-signal` | info | mean `rssi_dbm` < -85 | 10m | log |
| `device-offline` | warning | no telemetry for 10m | — | log, webhook |
| `sensor-flatline` | warning | `temperature_c` range ≤ 0.05 over 20m | 20m | log, webhook |
| `vibration-trend` | warning | idle floor of `vibration_rms_g` rising > 0.0005 g/min over 90m | — | log, webhook |
| `high-vibration` | critical | mean `vibration_rms_g` > 2.0 | 5m | log, webhook |
| `dead-letter-spike` | warning | > 50 rejected messages in 15m | — | log, webhook |
| `certificate-expiring` | warning | certificate expires within 30 days | — | log, webhook |

## The three fields that decide whether this is useful

### `for`: how long the condition must hold

Without it, one noisy sample pages someone. With it, the rule fires only when
**every** one of the most recent `for`-worth of one-minute buckets breaches; a
single recovering bucket resets the clock.

Note how the threshold query selects those buckets: it ranks by recency and takes
the newest N, rather than filtering on `bucket > now() - for`. A two-minute hold
filtered by time spans exactly 120 seconds, which contains one or two one-minute
buckets depending on where the clock happens to sit, so the rule would fire or
not according to alignment. This was a real bug, caught by the integration test
that injects a sustained breach and asserts an alert appears.

### `recovery`: how long it must stop holding before resolving

Without hysteresis, a value sitting on the threshold produces an endless stream
of fire-and-resolve pairs, and the first thing anyone does with such a feed is
mute it. Every rule here has a non-zero recovery, and a test asserts that.

Resolution is decided by how far `last_seen_at` has fallen behind `now()`, which
means the hysteresis needs no state in the alerting process at all.

### `renotify`: minimum gap between repeat notifications

A firing alert notifies again at most this often, so a long-running problem
stays visible without repeating every 15 seconds. `low-battery` is 12 hours
because it is a maintenance ticket; `critical-temperature` is 15 minutes because
it is an incident.

## Deduplication

One live alert per rule per device per metric, enforced by a partial unique
index:

```sql
CREATE UNIQUE INDEX alerts_active_uniq_idx
    ON alerts (rule_id, device_id, COALESCE(metric, ''))
    WHERE state = 'firing';
```

A breach that persists for an hour is one row, updated, not two hundred and
forty. In the database rather than in the process, so two replicas racing on the
same breach produce one alert rather than needing a lock.
`test_a_sustained_breach_fires_exactly_one_alert` asserts it after several
evaluation cycles.

## Why some of these rules look odd

### `sensor-flatline` exists because nothing else catches a stuck sensor

A stuck sensor reads 21.4 degC in a room that is 21.4 degC. The value never
leaves the plausible range and never crosses a threshold. The only thing that
gives it away is that a real sensor always shows some noise, so a reading that
has not moved by more than 0.05 degC in twenty minutes is stuck rather than
stable.

Implemented as `max(max_value) - min(min_value) <= threshold` across the window.
A range is exact whatever the sample count, whereas a pooled standard deviation
across buckets of unequal size needs care to mean anything, and the question
here is simply whether the reading moved at all. The rule also requires most of
the window to be present, because a device that reported twice and went quiet has
a tiny range for reasons that have nothing to do with a stuck sensor.

### `vibration-trend` tracks the idle floor, not the average

This one was wrong twice before it was right, and both mistakes are instructive.

The machines are duty cycled: the mean vibration swings between roughly 0.012 g
idle and 0.32 g running, every few minutes. The first version took the slope of
the **mean** between the first and last bucket of the window, and fired on every
single motor start. It produced two confident alerts about developing bearing
faults on devices that had no fault at all.

The second version switched to `min_value`, reasoning that duty cycling leaves
the floor alone. That was still wrong: a one-minute bucket during which the
motor ran for the whole minute has a `min_value` at the **running** level, so
per-bucket minima track the duty cycle just as faithfully as the mean does.

The third version takes `min(min_value)` **across each half of the window** and
compares the two halves. That is genuinely the idle floor, provided each half
contains an idle period. Comparing halves rather than endpoints matters on its
own too: endpoint sampling is at the mercy of whatever those two particular
minutes contained.

And it was still not enough. The window was 45 minutes, giving 22 minute halves,
against duty cycles of 20 to 40 minutes. A 22 minute half can sit entirely inside
a 30 minute running stretch, at which point its minimum is the running level and
the rule fires on a healthy machine for the third time. It did, once more, on a
device with no fault.

So the window is now 90 minutes, making each half 45, comfortably longer than the
longest duty cycle in the fleet. And because "comfortably longer" is a statement
about this fleet rather than a guarantee, a `spread_ratio` of 0.6 requires each
half to have seen the signal vary by at least 60 per cent of its own peak before
its floor is believed at all. A half that only ever saw the motor running has not
measured a floor, and the rule declines to judge rather than guessing.

All three directions are tested against the running stack:
`test_a_duty_cycled_machine_does_not_trip_the_vibration_trend_rule`,
`test_a_rising_idle_floor_does_trip_the_vibration_trend_rule` and
`test_a_half_window_that_never_went_idle_is_not_judged`.

### `device-offline` waits ten minutes, not one

The slowest device profile publishes every thirty seconds. A couple of missed
publishes is a flaky link, not a dead device. Ten minutes is twenty missed
publishes for the slowest profile and three hundred for the fastest.

It reads the device registry rather than the telemetry table, because the absence
of rows cannot be found by querying rows.

### `dead-letter-spike` is the only rule about the platform rather than a device

A botched firmware rollout publishing an unsupported schema version shows up
nowhere in device-level rules: every device is online, reporting on time, and
looks perfectly healthy. The only symptom is that the platform is rejecting
everything they send.

Fifty rejections in fifteen minutes. The fleet injects faults deliberately, so
the floor is not zero.

### `critical-temperature` uses max where the warning uses mean

The warning is about a room that is too warm, so it averages: a single
sun-struck sample should not fire it. The critical rule is about something being
damaged, where a brief excursion above 55 degC still matters, so it takes the
maximum and holds for a shorter period.

## Rule kinds

Six kinds, each answering a question the platform can already answer from stored
data. There is no general expression language, which would be more flexible and
would also mean every rule needs its own query planner and its own failure modes.

| Kind | Reads | Fires when |
| --- | --- | --- |
| `threshold` | `telemetry_1m_live` | the newest `for`-worth of buckets all breach |
| `flatline` | `telemetry_1m_live` | the range across the window is below a limit |
| `rate_of_change` | `telemetry_1m_live` | the slope between window halves exceeds a limit, and both halves are usable |
| `offline` | `devices` | `now() - last_seen` exceeds a limit |
| `dead_letter_rate` | `dead_letters` | the count in a window exceeds a limit |
| `certificate_expiry` | `devices` | `certificate_expires_at` is within N days |

`aggregation` chooses which column of the rollup a rule reads: `avg`, `min`,
`max` or `last`. It is a closed set because the value is interpolated into SQL;
so is the operator. Nothing a rule author writes reaches the query text except
through one of those enumerations, and
`test_unknown_operator_is_refused` asserts that a rule containing
`; DROP TABLE alerts; --` fails to load.

## Channels

| Channel | Status | Notes |
| --- | --- | --- |
| `log` | implemented | structured JSON on the service log. Never fails. |
| `webhook` | implemented | POSTs the alert as JSON. Records `suppressed` rather than `sent` when no URL is configured. |
| `stream` | implemented | publishes to `alerts.fired`, which is what puts alerts on the dashboard's WebSocket without polling. Every alert goes here regardless of the rule's channels. |
| email, SNS, PagerDuty, Slack | **not implemented** | listed here as absent rather than stubbed out. |

Those four are deliberately not present. A notifier that returns success without
sending anything is precisely the failure the `notifications` table exists to
make visible, and adding one would be worse than having none.

Every delivery attempt is recorded in `notifications` with its outcome, so
`sent`, `failed` and `suppressed` are distinguishable. A test asserts that every
channel named by a rule in the shipped catalogue is one of the implemented three,
and that every `critical` rule reaches a human rather than only a log file.

## Operating it

```bash
# Validate the catalogue without starting anything.
python -m alerting.main --check --rules config/alert_rules.yaml

# What is firing now.
curl -s localhost:8000/api/v1/alerts | jq '.alerts[] | {rule_id, device_id, message}'

# The stored definitions, as the engine sees them.
curl -s localhost:8000/api/v1/alerts/rules | jq '.rules[].rule_id'
```

Rules are re-read from disk every 60 seconds, so editing the catalogue does not
need a restart. A bad edit logs an error and keeps the previous catalogue, rather
than leaving the platform with no rules at all.

## Reviewing whether the rules are any good

[`analytics/queries/alert_reliability.sql`](../analytics/queries/alert_reliability.sql)
reports, per rule: alerts raised, devices affected, alerts per device, median and
mean duration, and the share that resolved inside two minutes.

Two things to read it for. A high count with a short median duration means the
condition was never really there and the rule is noise. A high alerts-per-device
figure means flapping, and the recovery window needs widening. Both are failure
modes that are invisible from the alerts themselves.
