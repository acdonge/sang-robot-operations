import {
  AlertOutlined,
  DatabaseOutlined,
  HddOutlined,
  ReloadOutlined,
  RobotOutlined,
} from "@ant-design/icons";
import {
  Avatar,
  Badge,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Divider,
  Empty,
  Input,
  List,
  Progress,
  Row,
  Segmented,
  Space,
  Statistic,
  Table,
  Tag,
  Timeline,
  Tooltip,
  Typography,
  type TableColumnsType,
} from "antd";
import { useDeferredValue, useState } from "react";
import type { AgentSnapshot, FleetEvent, FleetSummary, RobotSnapshot } from "../types";
import {
  EVENT_LABELS,
  agentNeedsAttention,
  detailHash,
  formatAge,
  formatBytes,
  formatClock,
  formatNumber,
  recordingInventory,
  statusColor,
} from "../utils";

const { Text, Title } = Typography;

type FilterValue = "all" | "online" | "attention";

interface FleetOverviewProps {
  agents: AgentSnapshot[];
  events: FleetEvent[];
  onRefresh: () => void;
  onSelectAgent: (agentId: string) => void;
  refreshing: boolean;
  selectedAgentId: string | null;
  summary: FleetSummary | null;
}

function robotHealth(robot: RobotSnapshot) {
  if (robot.fault || !robot.sources_fresh || robot.rates_ok === false) {
    return { color: "error", label: "Needs attention" } as const;
  }
  return { color: "success", label: "Healthy" } as const;
}

function SummaryStrip({ summary }: { summary: FleetSummary | null }) {
  const recordingIssues = (summary?.recordings?.failed || 0) + (summary?.recordings?.invalid || 0);
  const metrics = [
    {
      key: "agents",
      prefix: <RobotOutlined />,
      suffix: `/ ${summary?.agents.total || 0}`,
      title: "Agents online",
      value: summary?.agents.online || 0,
    },
    {
      key: "robots",
      prefix: <RobotOutlined />,
      title: "Robots observed",
      value: summary?.robots.total || 0,
    },
    {
      key: "capture",
      prefix: <DatabaseOutlined />,
      title: "Active capture",
      value: summary?.robots.recording || 0,
    },
    {
      key: "attention",
      prefix: <AlertOutlined />,
      status: (summary?.agents.needs_attention || 0) > 0 ? "danger" : "normal",
      title: "Needs attention",
      value: summary?.agents.needs_attention || 0,
    },
  ];

  return (
    <Card className="summary-card" styles={{ body: { padding: 0 } }}>
      <Row>
        {metrics.map((metric) => (
          <Col className="summary-cell" key={metric.key} xs={12} xl={6}>
            <Statistic
              className={metric.status === "danger" ? "stat-danger" : undefined}
              prefix={metric.prefix}
              suffix={metric.suffix}
              title={metric.title}
              value={metric.value}
            />
          </Col>
        ))}
      </Row>
      <div className="secondary-metrics">
        <span><HddOutlined /> Min disk free <strong>{summary?.storage.disk_free_gb_min == null ? "--" : `${formatNumber(summary.storage.disk_free_gb_min, 1)} GB`}</strong></span>
        <span><DatabaseOutlined /> Local MCAP <strong>{formatNumber(summary?.recordings?.total || 0)}</strong></span>
        <span><AlertOutlined /> Capture issues <strong>{formatNumber(recordingIssues)}</strong></span>
        <span>Sync plane <Tag>{summary?.sync?.supported ? "Enabled" : "Not enabled"}</Tag></span>
      </div>
    </Card>
  );
}

function RobotInspectorItem({ agent, robot }: { agent: AgentSnapshot; robot: RobotSnapshot }) {
  const health = robotHealth(robot);
  const healthySources = robot.sources.filter((source) => source.fresh && source.rate_ok !== false).length;
  return (
    <List.Item className="inspector-robot-item">
      <div className="inspector-robot">
        <Space align="start">
          <Avatar icon={<RobotOutlined />} shape="square" />
          <div>
            <a href={detailHash(agent.agent_id, robot.robot_id)}><strong>{robot.robot_id}</strong></a>
            <Text type="secondary">{robot.model}</Text>
          </div>
        </Space>
        <Tag color={health.color}>{health.label}</Tag>
        <Progress
          percent={robot.sources.length ? Math.round((healthySources / robot.sources.length) * 100) : 0}
          showInfo={false}
          size="small"
          status={health.color === "error" ? "exception" : "success"}
        />
        <Text className="source-ratio" type="secondary">{healthySources} / {robot.sources.length} sources healthy</Text>
        {robot.fault ? <Text className="fault-copy" type="danger">{robot.fault}</Text> : null}
      </div>
    </List.Item>
  );
}

function AgentInspector({ agent, events }: { agent: AgentSnapshot | null; events: FleetEvent[] }) {
  if (!agent) {
    return (
      <Card className="inspector-card">
        <Empty description="Select an agent to inspect its live contract." image={Empty.PRESENTED_IMAGE_SIMPLE} />
      </Card>
    );
  }

  const recordings = recordingInventory(agent);
  const retainedEvents = events.filter((event) => event.agent_id === agent.agent_id).slice(0, 8);
  const eventItems = retainedEvents.map((event) => ({
    children: (
      <div className="timeline-entry">
        <strong>{EVENT_LABELS[event.event_type] || event.event_type}</strong>
        <span>{event.source_key || event.robot_id || event.agent_id}</span>
        <small>{formatClock(event.occurred_at)} · {event.message}</small>
      </div>
    ),
    color: event.severity === "critical" ? "red" : event.severity === "warning" ? "orange" : "green",
  }));

  const collapseItems = [
    {
      children: recordings.enabled ? (
        <List
          dataSource={recordings.recent.slice(0, 4)}
          locale={{ emptyText: "No recent recordings" }}
          renderItem={(session) => (
            <List.Item>
              <List.Item.Meta
                description={`${formatBytes(session.total_size_bytes)} · ${session.topic_count ?? "--"} topics`}
                title={<Space><Tag color={session.state === "complete" ? "success" : "error"}>{session.state.toUpperCase()}</Tag><Text code>{session.session_id}</Text></Space>}
              />
            </List.Item>
          )}
          size="small"
        />
      ) : <Empty description="Recording inventory is not configured" image={Empty.PRESENTED_IMAGE_SIMPLE} />,
      key: "recordings",
      label: `Local recordings · ${recordings.scanned_count} scanned`,
    },
    {
      children: eventItems.length ? <Timeline items={eventItems} /> : <Empty description="No retained events" image={Empty.PRESENTED_IMAGE_SIMPLE} />,
      key: "events",
      label: `Health history · ${retainedEvents.length} recent`,
    },
  ];

  return (
    <Card
      className="inspector-card"
      extra={<Tag color={statusColor(agent.status)}>{agent.status.toUpperCase()}</Tag>}
      title={(
        <Space>
          <Avatar icon={<RobotOutlined />} shape="square" />
          <span className="agent-card-title"><strong>{agent.agent_name}</strong><small>{agent.agent_id} · v{agent.sw_version}</small></span>
        </Space>
      )}
    >
      <Descriptions
        className="resource-descriptions"
        column={2}
        items={[
          { key: "cpu", label: "CPU", children: agent.storage.cpu_pct == null ? "--" : `${formatNumber(agent.storage.cpu_pct, 1)}%` },
          { key: "ram", label: "RAM free", children: agent.storage.ram_free_gb == null ? "--" : `${formatNumber(agent.storage.ram_free_gb, 1)} GB` },
          { key: "disk", label: "Disk free", children: agent.storage.disk_free_gb == null ? "--" : `${formatNumber(agent.storage.disk_free_gb, 1)} GB` },
          { key: "clock", label: "Clock skew", children: `${formatNumber(agent.clock_skew_s, 2)} s` },
        ]}
        size="small"
      />
      <Divider orientationMargin={0} titlePlacement="start">Attached robots</Divider>
      <List
        dataSource={agent.robots}
        locale={{ emptyText: "No robots attached" }}
        renderItem={(robot) => <RobotInspectorItem agent={agent} robot={robot} />}
        split={false}
      />
      <Collapse className="inspector-collapse" ghost items={collapseItems} />
    </Card>
  );
}

export default function FleetOverview({
  agents,
  events,
  onRefresh,
  onSelectAgent,
  refreshing,
  selectedAgentId,
  summary,
}: FleetOverviewProps) {
  const [filter, setFilter] = useState<FilterValue>("all");
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query.trim().toLowerCase());
  const selectedAgent = agents.find((agent) => agent.agent_id === selectedAgentId) || null;
  const priority = { online: 0, stale: 1, offline: 2 };
  const filteredAgents = agents.filter((agent) => {
    if (filter === "online" && agent.status !== "online") return false;
    if (filter === "attention" && !agentNeedsAttention(agent)) return false;
    if (!deferredQuery) return true;
    const searchText = [
      agent.agent_id,
      agent.agent_name,
      ...agent.robots.flatMap((robot) => [robot.robot_id, robot.model]),
    ].join(" ").toLowerCase();
    return searchText.includes(deferredQuery);
  }).sort((left, right) => priority[left.status] - priority[right.status] || left.agent_name.localeCompare(right.agent_name));

  const columns: TableColumnsType<AgentSnapshot> = [
    {
      dataIndex: "agent_name",
      key: "agent",
      render: (_, agent) => (
        <Space>
          <Badge status={agent.status === "online" ? "success" : agent.status === "stale" ? "warning" : "default"} />
          <span className="table-identity"><strong>{agent.agent_name}</strong><small>{agent.agent_id}</small></span>
        </Space>
      ),
      title: "Agent",
    },
    {
      key: "activity",
      render: (_, agent) => {
        const robot = agent.robots[0];
        return robot ? <span className="table-identity"><strong>{robot.recording.active ? "CAPTURING" : robot.fsm_state}</strong><small>{robot.model}</small></span> : <Text type="secondary">No robot</Text>;
      },
      title: "Activity",
      width: 150,
    },
    {
      key: "sources",
      render: (_, agent) => {
        const robot = agent.robots[0];
        if (!robot) return <Tag>No robot</Tag>;
        const health = robotHealth(robot);
        return (
          <a href={detailHash(agent.agent_id, robot.robot_id)} onClick={(event) => event.stopPropagation()}>
            <Tag color={health.color}>{health.label}</Tag>
          </a>
        );
      },
      title: "Sources",
      width: 150,
    },
    {
      key: "storage",
      render: (_, agent) => {
        const recordings = recordingInventory(agent);
        return <span className="table-identity"><strong>{agent.storage.disk_free_gb == null ? "--" : `${formatNumber(agent.storage.disk_free_gb)} GB`}</strong><small>{recordings.enabled ? `${recordings.complete_count} MCAP · ${recordings.failed_count + recordings.invalid_count} issues` : "Inventory off"}</small></span>;
      },
      title: "Storage",
      width: 170,
    },
    {
      dataIndex: "age_s",
      key: "seen",
      render: (age: number) => <Text type="secondary">{formatAge(age)}</Text>,
      title: "Seen",
      width: 74,
    },
  ];

  return (
    <div className="page-stack fleet-page">
      <section className="page-heading">
        <div>
          <Text className="page-kicker">REAL-TIME OPERATIONS</Text>
          <Title level={2}>Fleet overview</Title>
          <Text type="secondary">Monitor robot, camera, storage, and capture health across every connected edge.</Text>
        </div>
        <Space>
          <Tooltip title="Latest central snapshot">
            <Text type="secondary">Updated {formatClock(summary?.generated_at)}</Text>
          </Tooltip>
          <Button icon={<ReloadOutlined spin={refreshing} />} loading={refreshing} onClick={onRefresh}>Refresh</Button>
        </Space>
      </section>

      <SummaryStrip summary={summary} />

      <div className="fleet-workspace">
        <Card
          className="fleet-table-card"
          extra={(
            <Space wrap>
              <Input.Search allowClear onChange={(event) => setQuery(event.target.value)} placeholder="Search agents or robots" value={query} />
              <Segmented<FilterValue>
                onChange={setFilter}
                options={[
                  { label: "All", value: "all" },
                  { label: "Online", value: "online" },
                  { label: "Needs attention", value: "attention" },
                ]}
                value={filter}
              />
            </Space>
          )}
          title={<span><strong>Agent registry</strong><Text type="secondary"> {filteredAgents.length} of {agents.length}</Text></span>}
        >
          <Table<AgentSnapshot>
            columns={columns}
            dataSource={filteredAgents}
            locale={{ emptyText: <Empty description="No agents match the current view" image={Empty.PRESENTED_IMAGE_SIMPLE} /> }}
            onRow={(agent) => ({ onClick: () => onSelectAgent(agent.agent_id) })}
            pagination={false}
            rowClassName={(agent) => agent.agent_id === selectedAgentId ? "selected-agent-row" : ""}
            rowKey="agent_id"
            scroll={{ x: 820 }}
            size="middle"
          />
        </Card>
        <AgentInspector agent={selectedAgent} events={events} />
      </div>
    </div>
  );
}
