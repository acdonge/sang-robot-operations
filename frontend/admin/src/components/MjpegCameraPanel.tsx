import { CameraOutlined, PlayCircleOutlined, StopOutlined } from "@ant-design/icons";
import { Button, Empty, Flex, Segmented, Space, Tag, Typography } from "antd";
import { useEffect, useState } from "react";
import { useMjpegPreview } from "../preview";
import type { AgentStatus, RobotSource } from "../types";
import { formatLatency, formatRate, sourceHealth } from "../utils";

const { Text } = Typography;

export interface PreviewCamera {
  agentStatus: AgentStatus;
  id: string;
  label: string;
  robotId: string;
  source: RobotSource;
}

export default function MjpegCameraPanel({ cameras, identity }: { cameras: PreviewCamera[]; identity: string }) {
  const [selectedId, setSelectedId] = useState(cameras[0]?.id || "");
  const preview = useMjpegPreview(identity);
  const selected = cameras.find((camera) => camera.id === selectedId) || cameras[0] || null;

  useEffect(() => {
    if (!cameras.some((camera) => camera.id === selectedId)) setSelectedId(cameras[0]?.id || "");
  }, [cameras, selectedId]);

  if (!selected) return <Empty description="No camera sources are available" image={Empty.PRESENTED_IMAGE_SIMPLE} />;

  const health = sourceHealth(selected.source);
  const topic = selected.source.topics[0];
  const streamMatches = preview.sourceKey === selected.source.key;
  const visualStatus = streamMatches ? preview.status : "idle";
  const statusLabel = visualStatus === "loading" ? "Connecting"
    : visualStatus === "live" ? "Live"
      : visualStatus === "error" ? "Preview failed" : health.label;

  function selectCamera(value: string | number) {
    preview.stop();
    setSelectedId(String(value));
  }

  return (
    <div className="camera-panel">
      <Flex align="center" gap={12} justify="space-between" wrap>
        <Segmented
          onChange={selectCamera}
          options={cameras.map((camera) => ({ label: <Space size={6}><CameraOutlined />{camera.label}</Space>, value: camera.id }))}
          value={selected.id}
        />
        <Space><Tag color={visualStatus === "error" ? "error" : visualStatus === "live" ? "processing" : health.color}>{statusLabel}</Tag><Text type="secondary">1 stream max</Text></Space>
      </Flex>
      <div className={`camera-stage camera-stage-${visualStatus}`}>
        {streamMatches && preview.frameUrl ? <img alt={`Live ${selected.label} camera feed for ${selected.robotId}`} src={preview.frameUrl} /> : (
          <div className="camera-placeholder"><CameraOutlined /><strong>{selected.source.preview_url ? "Preview is off" : "Preview unavailable"}</strong><span>{selected.source.preview_url ? "Camera bytes are requested only after you start this view." : "The agent does not expose a preview endpoint for this source."}</span></div>
        )}
      </div>
      <div className="camera-toolbar">
        <div><strong>{selected.label}</strong><Text code>{topic?.topic || "No topic"}</Text></div>
        <Space wrap>
          <Text type="secondary">{formatRate(topic?.rate_hz ?? null, topic?.minimum_rate_hz ?? null)}</Text>
          <Text type="secondary">{formatLatency(topic?.age_ms)}</Text>
          {preview.active && streamMatches ? <Button danger icon={<StopOutlined />} onClick={() => preview.stop()}>Stop preview</Button> : (
            <Button disabled={selected.agentStatus === "offline" || !selected.source.preview_url} icon={<PlayCircleOutlined />} loading={preview.status === "loading" && streamMatches} onClick={() => void preview.start(selected.source)} type="primary">{preview.status === "error" && streamMatches ? "Try again" : "Start preview"}</Button>
          )}
        </Space>
      </div>
    </div>
  );
}
