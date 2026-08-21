from fleet_control_plane.status import FleetTTLs

from test_api import heartbeat, make_client


def receive_until(websocket, predicate, *, attempts=10):
    for _ in range(attempts):
        payload = websocket.receive_json()
        if predicate(payload):
            return payload
    raise AssertionError("expected WebSocket fleet state was not observed")


def test_websocket_pushes_heartbeat_and_timer_reclassifies_silent_agent(tmp_path):
    client, _, clock = make_client(tmp_path)
    with client.websocket_connect("/api/v1/fleet") as websocket:
        initial = websocket.receive_json()
        assert initial["type"] == "fleet"
        assert initial["agents"] == []

        assert client.post("/api/v1/agents/heartbeat", json=heartbeat()).status_code == 202
        after_heartbeat = receive_until(
            websocket,
            lambda payload: (
                bool(payload["agents"])
                and payload["agents"][0]["status"] == "online"
            ),
        )
        assert after_heartbeat["agents"][0]["status"] == "online"
        assert after_heartbeat["events"][0]["event_type"] == "agent_online"

        clock.advance(FleetTTLs().offline_s + 1)
        after_silence = receive_until(
            websocket,
            lambda payload: (
                bool(payload["agents"])
                and payload["agents"][0]["status"] == "offline"
            ),
        )
        assert after_silence["agents"][0]["status"] == "offline"
        assert after_silence["summary"]["agents"]["offline"] == 1
        assert after_silence["events"][0]["event_type"] == "agent_offline"
