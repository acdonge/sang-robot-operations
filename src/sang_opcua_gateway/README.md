# SANG OPC UA Gateway

`sang_opcua_gateway` is a loopback-only, read-only ROS 2 to OPC UA Edge sidecar. Phase 1 supports
the frozen `bi_so101_01` contract: 12 follower state values, 12 observed leader-action values and
five camera metadata/health sources. It never retains camera bytes and exposes no application
Method or writable Variable.

```bash
ros2 run sang_opcua_gateway sang_opcua_gateway \
  --agent-id workcell-soarm-01 \
  --endpoint opc.tcp://127.0.0.1:4840/sang/ \
  --preview-base-url http://127.0.0.1:8101
```

Phase 1 intentionally uses `SecurityPolicy=None` only on loopback. Certificates, trust lists and
remote binding are Phase 3 work and must not be enabled by changing the bind address alone.

With the physical read-only adapters and gateway running, capture bounded correlation evidence:

```bash
ros2 run sang_opcua_gateway sang_opcua_validate \
  --duration-s 10 \
  --report /ws/output/validation/opcua-phase1-live.json
```
