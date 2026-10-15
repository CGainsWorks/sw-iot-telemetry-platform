# Cost model

**Every figure here comes from a published price list, not an invoice.** Nothing
in [`infra/terraform/`](../infra/terraform/) has ever been applied, so these are
estimates from the AWS and Google Cloud pricing pages for `eu-west-2` (London)
and `europe-west2`, as of the versions pinned in this repository. Treat them as
an order of magnitude and a guide to what dominates, not as a quote.

Running the platform locally costs nothing, which is the point of the Compose
stack.

## The fleet being costed

| | |
| --- | --- |
| Devices | 500 |
| Publish interval | every 10 seconds |
| Readings per message | 5 |
| Message size | ~350 bytes |
| Messages | 50/s, 130 million/month |
| Readings | 250/s, 648 million/month |
| Raw data | ~45 GB/month before compression |

## Monthly estimate

Two scenarios, because the difference between them is the single most important
cost decision in the design.

| Item | Unbatched, 10s publishes | Batched, 6 samples per minute |
| --- | --- | --- |
| **IoT Core messaging** | $156 | $26 |
| IoT Core connectivity | $2 | $2 |
| IoT Core rules engine | $23 | $4 |
| **Kinesis shards** (2, provisioned) | $26 | $26 |
| Kinesis PUT payload units | $3 | $1 |
| Firehose ingestion | $2 | $2 |
| S3 archive (steady state, 730d) | $2 | $2 |
| **EKS control plane** | $73 | $73 |
| **EKS nodes** (3 × m7g.large, on demand) | $208 | $208 |
| NAT gateway (single) | $33 | $33 |
| EBS gp3, 200 GB, tuned IOPS | $42 | $42 |
| ECR storage | $3 | $3 |
| Secrets Manager (3 secrets) | $1 | $1 |
| CloudWatch Logs | $12 | $12 |
| BigQuery storage and queries | $5 | $5 |
| **Total** | **~$591** | **~$440** |

## What dominates, and what to do about it

### Compute is half the bill, and EKS is the wrong choice at this scale

EKS control plane plus three nodes is **$281 a month**, 48 per cent of the total,
to run five small stateless services and one database.

The control plane alone is $73 a month regardless of how little is running on
it. At this scale that is a large fixed overhead for an orchestrator that is not
being asked to orchestrate much. Honest alternatives:

- **Two EC2 instances running Docker Compose**: around $70 a month for two
  `t4g.medium`, and the entire local stack in this repository would run on them
  unchanged. For 500 devices this is probably the right answer.
- **ECS on Fargate**: no control plane charge, pay per task. Around $120 a month
  for these services, and it keeps the container boundary without the Kubernetes
  one.
- **EKS**: earns its cost somewhere north of a few thousand devices and several
  teams deploying to the same cluster, where the scheduling, the autoscaling and
  the shared-platform story start to matter.

EKS is in this repository because the brief asked for Kubernetes and because it
is what the problem grows into. It is not what the problem starts as, and
pretending otherwise would be the wrong kind of answer.

### Batching saves a quarter of the bill

IoT Core charges per message, in 5 KB blocks. A 350 byte message and a 2 KB
message cost exactly the same. Publishing six samples in one message every
minute instead of one sample every ten seconds therefore cuts messaging and rule
costs by six, saving **about $150 a month, 26 per cent of the total**.

The costs: up to a minute of extra latency, and six samples lost rather than one
if a publish fails. For environmental monitoring that is an easy trade. For the
vibration monitors on a 2 second cadence it is not, because the whole point of
the trend rule is to catch a change early.

The firmware already buffers samples, so the change is small: publish an array
of samples rather than one. That is a **minor** schema version bump, additive,
and consumers built for `1.1` would ignore the new field. It is not implemented
here, and it would be the first thing to do before this ran at scale.

### Provisioned Kinesis, not on-demand

Two provisioned shards are $26 a month. On-demand removes capacity planning and
costs several times more per GB at steady load.

A telemetry fleet has an almost perfectly flat rate, which is exactly the case
provisioned mode is for. On-demand earns its premium for spiky, unpredictable
traffic, which this is not. One shard handles 1 MB/s or 1000 records/s, so 50
records/s needs one; the second is headroom for replaying a backlog.

### NAT gateways and VPC endpoints

One NAT gateway is $33 a month. Three, one per availability zone, is $99, and
that is the default in most VPC modules.

`single_nat_gateway` defaults to `true` here, which saves $66 a month and makes
egress a single point of failure. Correct for dev, wrong for prod, and the
variable documents exactly that.

The S3 gateway endpoint and the Kinesis interface endpoint matter more than they
look. Without them every byte of archive and every stream read leaves through
the NAT and is charged at $0.045 per GB. At 45 GB a month that is only $2, but
it scales linearly with the fleet while the endpoint cost does not: the S3
gateway endpoint is free, and the Kinesis interface endpoint is about $8 a month
for three zones.

### The EBS tuning costs more than the storage

200 GB of gp3 is $18.56. The manifest asks for 6000 IOPS and 250 MB/s, which
adds **$23 more** than the 3000 IOPS and 125 MB/s baseline:

| | Baseline | Tuned |
| --- | --- | --- |
| 200 GB storage | $18.56 | $18.56 |
| IOPS above 3000 | — | $17.40 |
| Throughput above 125 MB/s | — | $5.80 |
| | **$18.56** | **$41.76** |

Worth it under sustained write load with compression and continuous aggregate
refreshes competing for the same volume. Not worth it for a development cluster,
which is why the dev overlay drops the volume to 20 GB.

### Storage is almost free, and that is the retention ladder working

The entire storage bill, EBS aside, is about $9 a month: $2 for the S3 archive,
$2 for Firehose, $5 for BigQuery.

That is what the retention ladder buys. Full-resolution telemetry is kept for 30
days and compressed after 7; minute rollups for 180 days; hour rollups for 2
years. Keeping raw samples for two years instead would mean roughly 1 TB on gp3,
about $95 a month, to answer questions that the hourly rollup answers exactly as
well.

Compression does most of the work. `segmentby device_id, metric` groups every
sample of one series into one compressed batch, which typically gets 10 to 20x
on telemetry of this shape. That figure is from TimescaleDB's published
benchmarks, not measured here: this repository has never run long enough for a
chunk to reach the 7 day compression threshold.

### BigQuery is a rounding error, if the tables are partitioned

1.8 million hourly rollup rows a month is about 180 MB. Storage is $0.02 per GB.
The queries scan well under a gigabyte each because every table is partitioned by
day and clustered on the columns the queries filter by.

Get the partitioning wrong and this is the line item that surprises people: an
unpartitioned table means every query scans everything, and at $6.25 per TB a
dashboard refreshing every minute against a year of data becomes the largest
cost in the system.

Note the partition choice: daily, not hourly, even though the data is hourly.
BigQuery caps a table at 10,000 partitions, which is under 14 months of hours.
Daily partitioning with clustering gives the same pruning without the ceiling.

## Scaling

Roughly, holding the architecture fixed and assuming batching:

| Devices | Messages/month | Notes | Estimate |
| --- | --- | --- | --- |
| 50 | 2M | 2 EC2 instances, no EKS, 1 Kinesis shard | ~$120 |
| 500 | 22M | as costed above | ~$440 |
| 5,000 | 216M | 2 shards still, larger nodes, read replica | ~$900 |
| 50,000 | 2.2B | 8 shards, MSK worth reconsidering, Timescale Cloud, multi-AZ | ~$4,500 |

Costs grow sublinearly with fleet size because the fixed overhead, the control
plane, the NAT, the minimum node count, dominates at the small end. The
crossover where EKS starts making sense is somewhere around the 5,000 row.

## What is not costed

- **Data transfer out.** Dashboard traffic, and anything pulling from the API.
  Depends entirely on usage.
- **Timescale Cloud**, which is the production recommendation for the database.
  Roughly $150 a month for an instance sized for this fleet, replacing the $42
  of EBS and removing the operational burden.
- **The free tier.** A new account gets 12 months of several of these, which
  makes the first year considerably cheaper and the thirteenth month a surprise.
- Support plans, reserved instances or savings plans. A one-year compute savings
  plan would take roughly 30 per cent off the node cost.
- Anyone's time.

## Tear-down

**The most important section here.** An EKS cluster left running costs about
$280 a month doing nothing, and a NAT gateway keeps charging whether traffic
flows or not.

### Local

```bash
# Stop the stack, keep the data.
make down

# Stop it and delete everything: volumes, certificates, exported data.
make teardown
```

`make teardown` runs `docker compose down -v --remove-orphans` and removes
`certs/` and `data/`. Nothing persists outside the project directory and the
Docker volumes.

### AWS

```bash
cd infra/terraform/aws

# Always look first. destroy is not reversible and the plan is the only
# opportunity to notice it is about to remove the wrong environment.
terraform plan -destroy

terraform destroy
```

Four things `terraform destroy` will not remove, each on purpose:

1. **The S3 archive bucket**, if it has objects in it. `force_destroy` is
   deliberately not set, because an accidental `destroy` should not silently
   delete the raw record of everything the fleet ever sent. Empty it first:
   ```bash
   aws s3 rm "s3://$(terraform output -raw archive_bucket)" --recursive
   terraform destroy
   ```

2. **The EBS volume behind TimescaleDB.** Its StorageClass has
   `reclaimPolicy: Retain`, for the same reason. Remove it after confirming you
   do not want the data:
   ```bash
   kubectl -n telemetry delete pvc data-timescaledb-0
   aws ec2 describe-volumes --filters "Name=tag:kubernetes.io/created-for/pvc/name,Values=data-timescaledb-0"
   aws ec2 delete-volume --volume-id vol-...
   ```

3. **CloudWatch log groups.** The EKS cluster log group is managed by Terraform
   and goes; the ones the EKS add-ons create themselves are not:
   ```bash
   aws logs describe-log-groups --log-group-name-prefix /aws/eks/iot-telemetry
   ```

4. **Certificates registered with IoT Core.** A certificate must be deactivated
   and detached before it can be deleted, and `destroy` does not do it:
   ```bash
   for arn in $(aws iot list-certificates --query 'certificates[].certificateArn' --output text); do
     id="${arn##*/}"
     aws iot update-certificate --certificate-id "$id" --new-status INACTIVE
     aws iot delete-certificate --certificate-id "$id" --force-delete
   done
   ```

### Verifying there is nothing left running

```bash
# The expensive things, in one place.
aws eks list-clusters --region eu-west-2
aws ec2 describe-nat-gateways --region eu-west-2 \
  --filter "Name=state,Values=available" --query 'NatGateways[].NatGatewayId'
aws kinesis list-streams --region eu-west-2
aws ec2 describe-volumes --region eu-west-2 \
  --filters "Name=status,Values=available" --query 'Volumes[].VolumeId'
aws elbv2 describe-load-balancers --region eu-west-2 --query 'LoadBalancers[].LoadBalancerName'
```

An unattached EBS volume and a load balancer orphaned by a deleted Service are
the two things that most often survive a tear-down and keep charging. Both are
in that list.

### GCP

```bash
cd infra/terraform/gcp
terraform plan -destroy
terraform destroy
```

The BigQuery dataset will refuse to be deleted while it contains tables, and
`force_destroy` is not set on the landing bucket either. Both for the same
reason as the S3 bucket.
