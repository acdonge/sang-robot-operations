import socket
import threading
import time

from xr_teleop_bridge.mock_sender import sample_frame, send_frames
from xr_teleop_bridge.protocol import encode_frame
from xr_teleop_bridge.server import TcpFrameServer


def wait_for(predicate, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


def test_server_handles_fragmentation_validation_ordering_and_reconnect():
    received = []
    lock = threading.Lock()

    def on_frame(frame, received_monotonic):
        with lock:
            received.append((frame.sequence, received_monotonic))

    server = TcpFrameServer("127.0.0.1", 0, on_frame)
    server.start()
    try:
        first = encode_frame(sample_frame(1, 0.0, "test"))
        third = encode_frame(sample_frame(3, 0.1, "test"))
        second = encode_frame(sample_frame(2, 0.2, "test"))
        with socket.create_connection(("127.0.0.1", server.bound_port)) as client:
            client.sendall(first[:13])
            client.sendall(first[13:] + b"not-json\n" + third + second)
        wait_for(lambda: server.stats().out_of_order_frames == 1)

        # A new connection starts a new device sequence epoch.
        with socket.create_connection(("127.0.0.1", server.bound_port)) as client:
            client.sendall(first)
        wait_for(lambda: server.stats().accepted_frames == 3)

        with lock:
            assert [sequence for sequence, _ in received] == [1, 3, 1]
        stats = server.stats()
        assert stats.connections_total == 2
        assert stats.malformed_frames == 1
        assert stats.out_of_order_frames == 1
        assert stats.sequence_gaps == 1
        assert stats.bytes_received > 0
    finally:
        server.stop()

    assert not server.stats().listening
    assert not server.stats().connected


def test_mock_sender_streams_at_the_real_tcp_boundary():
    received = []
    server = TcpFrameServer(
        "127.0.0.1",
        0,
        lambda frame, _received: received.append(frame.sequence),
    )
    server.start()
    try:
        send_frames(
            "127.0.0.1",
            server.bound_port,
            hz=200.0,
            frames=5,
            source="mock_test",
        )
        wait_for(lambda: server.stats().accepted_frames == 5)
    finally:
        server.stop()

    assert received == [1, 2, 3, 4, 5]
