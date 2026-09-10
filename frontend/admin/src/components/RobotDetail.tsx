import {
  ArrowLeftOutlined,
  CameraOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  RobotOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Avatar,
  Badge,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Flex,
  Progress,
  Result,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Timeline,
  Typography,
  type TableColumnsType,
} from "antd";
import MjpegCameraPanel from "./MjpegCameraPanel";
import type {
  AgentSnapshot,
  FleetEvent,
  FleetRoute,
  FleetSummary,
  RecordingSession,
  RobotSource,
  TopicTelemetry,
} from "../types";
import {
  EVENT_LABELS,
  detailHash,
  formatAge,
  formatBytes,
  formatClock,
  formatLatency,
  formatNumber,
  formatRate,
  formatSessionTime,
  sourceHealth,
  statusColor,
} from "../utils";

const { Text, Title } = Typography;

interface RobotDetailProps {
  agents: AgentSnapshot[];
  events: FleetEvent[];
  route: Extract<FleetRoute, { view: "robot" }>;
  summary: FleetSummary | null;
}

interface TelemetryRow extends TopicTelemetry {
  kind: string;
  required: boolean;
  rowKey: string;
  sourceFresh: boolean;
  sourceKey: string;
  sourceRateOk: boolean | null;
}

function resolveRobot(agents: AgentSnapshot[], route: Extract<FleetRoute, { view: "robot" }>) {
  if (!route.agentId || !route.robotId) return null;
  const agent = agents.find((item) => item.agent_id === route.agentId);
  const robot = agent?.robots.find((item) => item.robot_id === route.robotId);
  return agent && robot ? { agent, robot } : null;
}

export default function RobotDetail({ agents, events, route, summary }: RobotDetailProps) {
  const resolved = resolveRobot(agents, route);
  const allRobots = agents.flatMap((agent) => agent.robots.map((robot) => ({ agent, robot })));
  const identity = resolved ? `${resolved.agent.agent_id}:${resolved.robot.robot_id}` : "unselected";
  const cameras = resolved?.robot.sources.filter((source) => source.kind === "camera") || [];
  const recording = resolved?.robot.recording;
  const recordingPercent = recording?.episodes_total
    ? Math.min(100, ((recording.episode_index + 1) / recording.episodes_total) * 100)
    : 0;

  const telemetryRows: TelemetryRow[] = resolved?.robot.sources.flatMap((source) => source.topics.map((topic, index) => ({
    ...topic,
    kind: source.kind,
    required: source.required,
    rowKey: `${source.key}:${topic.topic}:${index}`,
    sourceFresh: source.fresh,
    sourceKey: source.key,
    sourceRateOk: source.rate_ok,
  }))) || [];

  const telemetryColumns: TableColumnsType<TelemetryRow> = [
    { dataIndex: "sourceKey", key: "source", title: "Source", width: 150 },
    { dataIndex: "kind", key: "kind", render: (kind: string) => <Tag>{kind.toUpperCase()}</Tag>, title: "Kind", width: 90 },
    { dataIndex: "topic", ellipsis: true, key: "topic", render: (topic: string) => <Text code>{topic}</Text>, title: "ROS 2 topic" },
    { dataIndex: "required", key: "required", render: (required: boolean) => required ? "Required" : "Optional", title: "Contract", width: 100 },
    { key: "rate", render: (_, row) => formatRate(row.rate_hz, row.minimum_rate_hz), title: "Rate", width: 120 },
    { dataIndex: "age_ms", key: "age", render: (age: number | null) => formatLatency(age), title: "Age", width: 90 },
    { key: "health", render: (_, row) => { const health = sourceHealth({ fresh: row.sourceFresh, rate_ok: row.sourceRateOk } as RobotSource); return <Tag color={health.color}>{health.label}</Tag>; }, title: "Health", width: 100 },
  ];

  const recordingColumns: TableColumnsType<RecordingSession> = [
    { dataIndex: "session_id", ellipsis: true, key: "session", render: (value: string) => <Text code>{value}</Text>, title: "Session" },
    { dataIndex: "state", key: "state", render: (value: string) => <Tag color={value === "complete" ? "success" : "error"}>{value.toUpperCase()}</Tag>, title: "State", width: 100 },
    { dataIndex: "total_size_bytes", key: "size", render: formatBytes, title: "Size", width: 100 },
    { dataIndex: "message_count", key: "messages", render: (value: number | null) => formatNumber(value), title: "Messages", width: 110 },
    { dataIndex: "transport_messages_lost", key: "loss", render: (value: number | null) => formatNumber(value), title: "Loss", width: 80 },
    { dataIndex: "started_at", key: "started", render: formatSessionTime, title: "Started", width: 150 },
  ];

  function selectRobot(value: string) {
    window.location.hash = value;
  }

  return (
    <div className="page-stack robot-page-react">
      <section className="page-heading robot-page-heading">
        <div>
          <Button href="#fleet" icon={<ArrowLeftOutlined />} type="link">Fleet overview</Button>
          <Text className="page-kicker">ROBOT / LIVE DOSSIER</Text>
          <Title level={2}>{resolved?.robot.robot_id || "Robot details"}</Title>
          <Text type="secondary">{resolved ? `${resolved.robot.model} · ${resolved.agent.agent_name}` : "Select a robot to inspect its live observation contract."}</Text>
        </div>
        <Space direction="vertical" align="end">
          <Select
            onChange={selectRobot}
            options={allRobots.map(({ agent, robot }) => ({
              label: `${agent.agent_name} · ${robot.robot_id}`,
              value: detailHash(agent.agent_id, robot.robot_id),
            }))}
            placeholder="Select a robot"
            showSearch
            style={{ minWidth: 320 }}
            value={resolved ? detailHash(resolved.agent.agent_id, resolved.robot.robot_id) : undefined}
          />
          <Text type="secondary">Updated {formatClock(summary?.generated_at)}</Text>
        </Space>
      </section>

      {!route.agentId || !route.robotId ? (
        <Card><Empty description="Select a robot from the list above or from Fleet overview." /></Card>
      ) : !resolved ? (
        <Card><Result status="404" title="Robot not found" subTitle="The agent has not sent a heartbeat, or the robot is no longer in the current Fleet snapshot." /></Card>
      ) : (
        <>
          <Card
            className="robot-overview-card"
            extra={<Space><Tag color={statusColor(resolved.agent.status)}>{resolved.agent.status.toUpperCase()}</Tag><Tag>{resolved.robot.phase}</Tag></Space>}
            title={<Space><Avatar icon={<RobotOutlined />} shape="square" /><span>{resolved.robot.model}</span></Space>}
          >
            <Descriptions
              column={{ xs: 1, sm: 2, lg: 4 }}
              items={[
                { key: "state", label: "State", children: resolved.robot.recording.active ? "CAPTURING" : resolved.robot.fsm_state },
                { key: "runtime", label: "Runtime", children: resolved.robot.runtime_kind || "Not declared" },
                { key: "sources", label: "Sources", children: resolved.robot.sources_fresh ? <Tag color="success">Fresh</Tag> : <Tag color="error">Needs attention</Tag> },
                { key: "rate", label: "Rate contract", children: resolved.robot.rates_ok === false ? <Tag color="warning">Below minimum</Tag> : <Tag color="success">Healthy</Tag> },
              ]}
            />
            {recording?.active ? (
              <div className="capture-progress">
                <Flex align="center" justify="space-between" wrap>
                  <Space><Badge status="processing" /><strong>Capture in progress</strong></Space>
                  <Text type="secondary">Episode {recording.episode_index + 1} / {recording.episodes_total || "--"}</Text>
                </Flex>
                <Progress percent={recordingPercent} showInfo={Boolean(recording.episodes_total)} status="active" />
                <Descriptions
                  column={{ xs: 1, sm: 3 }}
                  items={[
                    { key: "frames", label: "Frames", children: formatNumber(recording.frames) },
                    { key: "elapsed", label: "Elapsed", children: `${formatNumber(recording.elapsed_s, 1)} s` },
                    { key: "instruction", label: "Instruction", children: recording.current_instruction || "Not provided" },
                  ]}
                  size="small"
                />
              </div>
            ) : null}
            {resolved.robot.fault ? <Alert description={resolved.robot.fault} message="Observation contract degraded" showIcon type="warning" /> : null}
          </Card>

          <Card className="camera-card" title={<Space><CameraOutlined />Camera preview</Space>}>
            <MjpegCameraPanel cameras={cameras.map((source) => ({ agentStatus: resolved.agent.status, id: source.key, label: source.key, robotId: resolved.robot.robot_id, source }))} identity={identity} />
          </Card>

          <Card title={<Space><CloudServerOutlined />ROS 2 source telemetry</Space>}>
            <Table<TelemetryRow>
              columns={telemetryColumns}
              dataSource={telemetryRows}
              pagination={false}
              rowKey="rowKey"
              scroll={{ x: 900 }}
              size="small"
            />
          </Card>

          <Row gutter={[16, 16]}>
            <Col xs={24} xl={8}>
              <Card className="detail-side-card" title="Host agent">
                <Descriptions
                  column={1}
                  items={[
                    { key: "name", label: "Agent", children: resolved.agent.agent_name },
                    { key: "id", label: "Agent ID", children: <Text code>{resolved.agent.agent_id}</Text> },
                    { key: "software", label: "Software", children: resolved.agent.sw_version },
                    { key: "heartbeat", label: "Heartbeat age", children: formatAge(resolved.agent.age_s) },
                    { key: "cpu", label: "CPU", children: resolved.agent.storage.cpu_pct == null ? "--" : `${formatNumber(resolved.agent.storage.cpu_pct, 1)}%` },
                    { key: "ram", label: "RAM free", children: resolved.agent.storage.ram_free_gb == null ? "--" : `${formatNumber(resolved.agent.storage.ram_free_gb, 1)} GB` },
                    { key: "disk", label: "Disk free", children: resolved.agent.storage.disk_free_gb == null ? "--" : `${formatNumber(resolved.agent.storage.disk_free_gb, 1)} GB` },
                    { key: "clock", label: "Clock skew", children: `${formatNumber(resolved.agent.clock_skew_s, 2)} s` },
                  ]}
                  size="small"
                />
              </Card>
            </Col>
            <Col xs={24} xl={16}>
              <Card className="detail-side-card" title={<Space><DatabaseOutlined />Local recording sessions</Space>}>
                <Table<RecordingSession>
                  columns={recordingColumns}
                  dataSource={resolved.agent.recordings.recent.filter((session) => session.robot_id === resolved.robot.robot_id)}
                  locale={{ emptyText: "No local recording sessions for this robot" }}
                  pagination={{ hideOnSinglePage: true, pageSize: 5 }}
                  rowKey="session_id"
                  scroll={{ x: 720 }}
                  size="small"
                />
              </Card>
            </Col>
          </Row>

          <Card title="Retained health events">
            {events.length ? (
              <Timeline
                items={events.filter((event) => event.agent_id === resolved.agent.agent_id && (!event.robot_id || event.robot_id === resolved.robot.robot_id)).slice(0, 20).map((event) => ({
                  children: <div className="timeline-entry"><strong>{EVENT_LABELS[event.event_type] || event.event_type}</strong><span>{event.source_key || event.robot_id || event.agent_id}</span><small>{formatClock(event.occurred_at)} · {event.message}</small></div>,
                  color: event.severity === "critical" ? "red" : event.severity === "warning" ? "orange" : "green",
                }))}
              />
            ) : <Empty description="No retained health events" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
          </Card>
        </>
      )}
    </div>
  );
}
