#!/usr/bin/env bash
set -euo pipefail

RB5_DOCKER_IMAGE="${RB5_DOCKER_IMAGE:-sang-fleet:jazzy}"
RB5_HOST_CIDR="${RB5_HOST_CIDR:-10.0.2.100/24}"
RB5_MANAGEMENT_GATEWAY="${RB5_MANAGEMENT_GATEWAY:-192.168.0.1}"
RB5_NET_CONTAINER="${RB5_NET_CONTAINER:-sang-rb5-net}"
RB5_ADAPTER_CONTAINER="${RB5_ADAPTER_CONTAINER:-sang-rb5-adapter}"
RB5_AGENT_CONTAINER="${RB5_AGENT_CONTAINER:-sang-rb5-agent}"

RB5_INTERFACE="${RB5_INTERFACE:-$(
    ip route get "${RB5_MANAGEMENT_GATEWAY}" 2>/dev/null |
        awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}'
)}"

for container in "${RB5_AGENT_CONTAINER}" "${RB5_ADAPTER_CONTAINER}"
do
    if docker container inspect "${container}" >/dev/null 2>&1
    then
        docker stop --time 3 "${container}" >/dev/null
        docker rm "${container}" >/dev/null
    fi
done

if docker container inspect "${RB5_NET_CONTAINER}" >/dev/null 2>&1
then
    docker stop --time 3 "${RB5_NET_CONTAINER}" >/dev/null
    docker rm "${RB5_NET_CONTAINER}" >/dev/null
fi

if [[ -n "${RB5_INTERFACE}" ]] && \
   docker image inspect "${RB5_DOCKER_IMAGE}" >/dev/null 2>&1
then
    docker run --rm --network host --cap-add NET_ADMIN \
        "${RB5_DOCKER_IMAGE}" \
        ip address del "${RB5_HOST_CIDR}" dev "${RB5_INTERFACE}" \
        >/dev/null 2>&1 || true
fi

echo "RB5 read-only monitoring stopped."
