# Cluster Task

A GitHub composite action that submits and manages framework-agnostic distributed jobs
(from a single card up to multi-node, multi-card) on a Kubernetes PPU cluster.

The action runs on a CPU-only orchestration runner and consumes no accelerator itself.
All PPU resources are requested by the worker pods it creates.

## Features

- Declarative inputs: node count, cards per node, image and command.
- Gang scheduling through `PodGroup` with a dedicated `ppu-scheduler` and
  multi-node pod anti-affinity.
- Standard distributed environment contract injected into every worker pod.
- Automatic source staging: `$GITHUB_WORKSPACE` (plus optional artifacts) is packed and
  extracted inside the worker pod.
- Default CPU/memory limits derived from board type and card count.
- Separate scheduling and workload-log steps: the first waits for every pod to be scheduled; the second reports image pull/startup and streams worker-0 logs from the beginning until completion. Both share the `timeout_minutes` budget, and cleanup runs on failure or cancellation.
- One-line `PPU usage` notice on the run page plus a collapsed usage summary in the job log, covering queue time and occupied/execution PPU card-hours; the GitHub Job Summary is left untouched.
- One `PPU queue` annotation emitted after the job ends, listing each pod's queue time (creation until scheduling). It is a warning with the latest scheduling failure reason when any pod queued for five minutes or more or never scheduled, and a notice otherwise. Apart from these two and errors, the action creates no run annotations.
- Optional kernel log (`dmesg`) collection from worker nodes.

## Usage

```yaml
jobs:
  train:
    runs-on: <cpu-orchestration-runner-label>
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false

      - uses: flytiger-eco/shared-workflows/cluster-task@main
        with:
          image: <registry>/<image>:<tag>
          nnodes: 2
          nproc_per_node: 8
          node_selector: board-type=ZW-M890P
          timeout_minutes: 120
          command: |
            torchrun \
              --nnodes=$NNODES --nproc_per_node=$NPROC_PER_NODE \
              --node_rank=$NODE_RANK \
              --master_addr=$MASTER_ADDR --master_port=$MASTER_PORT \
              train.py
```

### Execution modes

| `nnodes` | Mode | Behavior |
|----------|------|----------|
| `0` (default) | Single pod | One pod, no `PodGroup` or headless `Service` |
| `1..N` | Gang | `N` worker pods, a `PodGroup` (`minMember=N`) and a headless `Service` for rendezvous |

### Source staging

Git configuration files are excluded from the source archive, including repository,
submodule and worktree configs that may contain checkout credentials. Git objects,
refs and `HEAD` remain available for version metadata such as `git describe --always`.
The original workspace configuration is not changed. Set any custom Git configuration
on the worker instead. Use `persist-credentials: false` with checkout as shown above.

Additional artifacts are copied by basename when that name is not already present in
the workspace. Existing destinations are left unchanged, and missing paths are skipped
with a warning.

## Inputs

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `image` | yes | | Container image with job code and dependencies |
| `command` | yes | | Shell command executed in each worker pod (`/bin/bash -c`) |
| `nnodes` | no | `0` | `0` selects one standalone pod; positive values set the gang worker count |
| `nproc_per_node` | no | `1` | PPU cards requested per pod (`alibabacloud.com/ppu`) |
| `namespace` | no | `default` | Kubernetes namespace |
| `timeout_minutes` | no | `60` | Overall scheduling and execution budget; expiry reports `timeout` and triggers cleanup |
| `host_volumes` | no | | `/host/a:/ctr/a,/host/b:/ctr/b`; NAS mounts are added by default |
| `extra_env` | no | | `KEY1=VAL1,KEY2=VAL2` |
| `master_port` | no | `29500` | Rendezvous port |
| `cleanup_policy` | no | `always` | Compatibility input, ignored; all resources are always cleaned up |
| `source_dir` | no | `/workspace/source` | Path where staged source is extracted in the pod |
| `node_selector` | no | | `KEY1=VAL1,...`, merged into `spec.nodeSelector` |
| `container_options` | no | | YAML snippet, see below |
| `artifact_paths` | no | | Extra paths (one per line) staged with the source |
| `job_suffix` | no | | Readable segment of resource names; a unique hash is always appended |
| `dmesg_policy` | no | `never` | `never` disables collection; `on_failure` and `always` both collect, print and retain logs on NAS |

`cleanup_policy` does not retain resources: cleanup always runs after success, failure,
timeout or cancellation. Currently `on_failure` and `always` have identical dmesg
behavior for every outcome, including successful jobs; neither limits NAS retention
to failed runs.

### `container_options`

| Key | Maps to |
|-----|---------|
| `privileged` | container `securityContext.privileged` |
| `host_ipc` | pod `spec.hostIPC` |
| `host_network` | pod `spec.hostNetwork` (sets `dnsPolicy: ClusterFirstWithHostNet`) |
| `dns_policy` | pod `spec.dnsPolicy` |
| `shm_size` | `/dev/shm` emptyDir `sizeLimit` (default `64Gi`) |
| `cap_add` | `securityContext.capabilities.add`, comma separated |
| `cpus` / `cpu` | `resources.limits.cpu` |
| `memory` | `resources.limits.memory`; docker units `b/k/m/g/t` are converted |

When `cpus` or `memory` is not set, limits are computed per card:
`ZW-M890P` uses 31 cores and 256Gi; `OAM-810E` and `ZW-810E` both use 11 cores and 112Gi.
Without a `board-type` selector, `OAM-810E` values are used.
The requested `board-type` value is preserved in the pod's node selector.

## Environment contract

Each worker pod receives:

| Variable | Value |
|----------|-------|
| `NNODES` / `WORLD_SIZE` | Number of worker pods |
| `NPROC_PER_NODE` | Cards per pod |
| `NODE_RANK` / `RANK` | Pod index |
| `LOCAL_RANK` | `0` |
| `MASTER_ADDR` | `127.0.0.1` in single-pod mode; worker-0 DNS via the headless `Service` in gang mode |
| `MASTER_PORT` | `master_port` input |
| `NODE_NAME` | Kubernetes node name |

Variables declared in the workflow or job `env` context are forwarded automatically.

## Outputs

| Name | Description |
|------|-------------|
| `job_name` | Resource name of the job |
| `job_status` | `succeeded`, `failed`, `timeout`, `cancelled` |
| `duration_seconds` | Job duration |
| `queue_duration_seconds` | Time until all PPU pods are scheduled |
| `occupied_ppu_card_hours` | PPU card-hours from pod scheduling until completion, including image pulling and startup |
| `execution_ppu_card_hours` | PPU card-hours while worker containers are running |
| `task_pods` | Comma-separated worker pod names |

The queue duration is measured from the first pod creation until every pod is scheduled. For a pod
that never schedules, it is measured until the job ends. Both card-hours metrics sum each
pod independently, so multi-node jobs are accounted for correctly.

## Action layout

```
action.yml          Composite action definition
scripts/            Naming, submission, cleanup and dmesg scripts
tests/              Lifecycle and staging regression tests
```
