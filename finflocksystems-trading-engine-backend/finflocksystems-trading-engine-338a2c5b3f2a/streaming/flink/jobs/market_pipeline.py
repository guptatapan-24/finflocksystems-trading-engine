"""The whole market-data path as a single Flink job.

The five stages used to be submitted as five jobs. Each one cost the
TaskManager a slot, a user classloader in metaspace, and -- because every stage
runs PyFlink UDFs -- its own Beam Python harness process. Measured resident
sizes for those five harnesses were 331, 297, 244, 209 and 207MB: ~1.26GB of
interpreter for pipelines whose own state is tiny (stream_health is a single
``map``).

Submitted as one job the five stages share one slot and therefore one harness,
and the TaskManager no longer needs the enlarged metaspace and off-heap pools
that five classloaders and five sets of Beam gRPC channels required.

What is deliberately *unchanged*:

* Every stage still reads and writes the same Kafka topics under the same
  consumer groups, so the topology is identical from outside and the backend's
  consumers see no difference. The stages are linked by Kafka, not by an
  in-memory ``DataStream``, exactly as before.
* Because the five pipelines stay disconnected in the job graph, Flink's
  default ``region`` failover strategy still restarts them independently -- one
  stage failing does not restart the other four.

What this does change: the five stages now share one JobID, one checkpoint
coordinator and one lifecycle, so they are cancelled and resubmitted together.
``scripts/ensure_flink_jobs.py`` manages this one job instead of five.
"""

from jobs import (
    bar_aggregation,
    indicator_computation,
    market_normalization,
    stale_price_detection,
    stream_health,
)
from jobs.runtime import environment


JOB_NAME = "market-pipeline-v1"

# Declaration order follows the Kafka chain (raw -> canonical -> bars ->
# indicators, and canonical -> quality -> health). Flink does not care, but a
# reader tracing a record through the topology should not have to jump around.
STAGES = (
    market_normalization,
    bar_aggregation,
    indicator_computation,
    stale_price_detection,
    stream_health,
)


def main():
    env = environment(JOB_NAME)
    for stage in STAGES:
        stage.build(env)
    env.execute(JOB_NAME)


if __name__ == "__main__":
    main()
