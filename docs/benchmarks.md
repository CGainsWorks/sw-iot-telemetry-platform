# Throughput and latency

Every number here comes from [`bench/benchmark.py`](../bench/benchmark.py)
against the full Compose stack. Reproduce with:

```bash
make demo
make bench BENCH_DEVICES=50 BENCH_RATE=10 BENCH_DURATION=60
```

Results are written to `bench/results/` as JSON and Markdown.

## The machine

| | |
| --- | --- |
| CPU | AMD Ryzen 7 5700X, 8 cores / 16 threads |
| RAM | 48 GB |
| Disk | Samsung 970 EVO Plus NVMe SSD |
| OS | Windows 11 Pro, Docker Desktop with the WSL2 backend |

One machine running the load generator, the broker, Redpanda, TimescaleDB and
every service at once. That is not how any of this would be deployed, and it
matters: the figures below are a floor, not a ceiling, because the load
generator is competing with the system it is measuring for the same 16 threads.

## Results

Five readings per message throughout, QoS 1, `acks=all`.

| Fleet | Offered | Stored | Loss | Device → ingest | Publish → queryable |
| --- | --- | --- | --- | --- | --- |
| 50 devices × 10 Hz | 499 msg/s | **2,494 readings/s** | 0.000% | p50 24 ms, p99 44 ms | p50 60 ms, p99 169 ms |
| 50 devices × 20 Hz | 972 msg/s | **4,858 readings/s** | 0.000% | p50 29 ms, p99 44 ms | p50 70 ms, p99 138 ms |
| 100 devices × 25 Hz | 2,323 msg/s | 10,825 readings/s | **6.797%** | p50 25.4 s, p99 26.6 s | p50 8.8 s, p99 16.0 s |

**The knee is between roughly 4,900 and 10,800 readings per second** on this
hardware. Below it, nothing is lost and latency is tens of milliseconds. Above
it, the pipeline becomes backlogged: ingest lag grows to twenty-five seconds and
6.8 per cent of readings had still not reached storage when the thirty second
drain window expired.

That last figure is worth reading carefully. The 6.8 per cent is not data the
platform threw away. It is data still queued in Redpanda when the benchmark
stopped waiting. Nothing was dropped, because QoS 1 and `acks=all` mean the
broker and the stream both hold what they have accepted, and the processor was
still working through it. The failure mode of overload here is **growing
latency, not loss**, which is the correct failure mode for telemetry and is a
direct consequence of putting a durable stream between ingest and storage.

A fleet of 500 devices publishing every ten seconds is 250 readings per second,
a tenth of the comfortable figure. A fleet of 5,000 would sit at the knee.

## What is measured, and how

### Device to ingest

`ingest_ts - ts`, read straight out of the stored rows. Exact, because both
stamps are recorded, and it covers the MQTT hop and the validation step.

This is the number that goes to twenty-five seconds under overload, and it is
the clearest signal that the stack is behind: it means messages are sitting in
the broker's queue rather than being consumed.

### Publish to queryable

Measured with probe messages. A probe is published carrying a marker value, then
the database is polled until the row appears, so the figure covers the whole
path: MQTT, validation, Redpanda, the processor's batch window, the write, and
commit.

Polling bounds the resolution to the poll interval, 20 ms, which is small
against latencies in the tens to hundreds of milliseconds.

Measured this way rather than by adding a `stored_at` column, because that
column would cost eight bytes on every row of the largest table in the system,
forever, to answer a question that is only asked during a benchmark.

### Loss

Published minus stored, counted exactly. Both sides are bounded by **sequence
number**, not by timestamp: each publisher connection owns a disjoint slice of
the fleet and records the first and last sequence number it counted, and the
query counts rows for those devices within that range.

This matters more than it sounds. Bounding by timestamp lets a message stamped
just either side of the window edge be counted in one place and not the other.
The first version of this benchmark did exactly that and reported 0.266 per cent
loss that had never happened; fixing the ordering so the counting decision is
taken before the timestamp is stamped, then switching to sequence ranges
entirely, is what makes 0.000 per cent believable.

## Where the latency goes

At 50 devices and 10 Hz, publish-to-queryable has a p50 of 60 ms. Roughly:

| Stage | Contribution | Governed by |
| --- | --- | --- |
| MQTT publish and broker hop | ~5 ms | network, TLS record size |
| Validation and explode | <1 ms | `ingest_handle_seconds` histogram |
| Kafka produce with `acks=all` | ~20 ms | `KAFKA_LINGER_MS`, default 20 |
| Consumer batch wait | ~0–250 ms | `--batch-timeout-ms`, default 250 |
| Insert | ~5 ms | one `unnest` round trip per batch |

**The consumer batch window dominates.** `--batch-timeout-ms 250` means the
processor waits up to a quarter of a second for a batch to fill before writing
what it has. At low message rates that wait is most of the latency; at high
rates the batch fills before the timeout and the term disappears.

Lowering it would cut p50 latency and cost throughput, because the round trip
per batch is then amortised over fewer rows. 250 ms is chosen for throughput on
the grounds that the dashboard's own refresh is slower than that anyway, and
nothing downstream cares about a quarter of a second. Lower it if something
does.

Similarly `linger_ms 20` on the producer: batching at the Kafka layer trades
20 ms of latency for a materially better compression ratio and fewer round
trips.

## What the benchmark does not cover

Stated plainly, because a benchmark that is read as more than it is does more
harm than none at all.

- **One machine.** The load generator shares 16 threads with everything it
  measures. On separate hardware the numbers would be better, and the knee would
  move.
- **Short runs.** Sixty seconds. Compression kicks in after seven days and
  continuous aggregate refreshes compete with writes for the same volume. The
  interesting failures happen after a week, not a minute.
- **One broker, one Redpanda node, one database.** Replication factor is 1
  throughout, so nothing here exercises partition leadership, in-sync replica
  behaviour, or what happens when a node dies mid-write.
- **Few connections.** Ten to sixteen MQTT connections drive the load, not one
  per device. The simulator does open one per device and that is what the fleet
  service uses, but the benchmark deliberately does not, so that it measures the
  pipeline rather than its own TLS handshakes.
- **No Kinesis, no MSK, no EKS.** The AWS path has never been deployed, so none
  of these figures say anything about it. See
  [limitations.md](limitations.md#no-aws-account).
- **Not a tuning exercise.** Every parameter is at its committed default. No
  attempt was made to find the configuration that produces the best number.

## Reproducing

```bash
# The three runs in the table above.
make bench BENCH_DEVICES=50  BENCH_RATE=10 BENCH_DURATION=60 BENCH_LABEL=laptop-50dev-10hz
make bench BENCH_DEVICES=50  BENCH_RATE=20 BENCH_DURATION=45 BENCH_LABEL=laptop-50dev-20hz
make bench BENCH_DEVICES=100 BENCH_RATE=25 BENCH_DURATION=45 BENCH_LABEL=laptop-100dev-25hz
```

The benchmark deletes its own rows before and after each run, so the dashboard
is not left full of devices that do not exist, and a previous run's sequence
numbers cannot be counted by the next one.

It exits non-zero if nothing reached storage, so a broken stack cannot be
published as a result of zero.

## Observability during a run

Every service exposes Prometheus metrics. The ones worth watching:

| Metric | Service | Tells you |
| --- | --- | --- |
| `ingest_lag_seconds` | ingest | device clock to platform receipt |
| `ingest_rejected_total` | ingest | rejections, by reason |
| `ingest_schema_ahead_total` | ingest | firmware newer than the platform, so fields are being dropped |
| `processing_end_to_end_lag_seconds` | processing | sample time to durable storage |
| `processing_batch_rows` | processing | whether batches are filling or timing out |
| `processing_open_windows` | processing | rollup windows held in memory |
| `processing_late_samples_total` | processing | samples that missed their live window |
| `alerting_cycle_seconds` | alerting | whether rule evaluation keeps up with its interval |

```bash
curl -s localhost:9101/metrics | grep -E '^ingest_(lag|rejected)'
curl -s localhost:9102/metrics | grep -E '^processing_(batch|open|late)'
```

`processing_batch_rows` is the one to read first when latency looks wrong. If
the histogram is concentrated at the low end, batches are timing out rather than
filling, and the batch timeout is the thing to adjust.
