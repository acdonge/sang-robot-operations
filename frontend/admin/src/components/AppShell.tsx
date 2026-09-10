import {
  AppstoreOutlined,
  BranchesOutlined,
  CodeOutlined,
  ControlOutlined,
  DatabaseOutlined,
  MenuOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { Badge, Button, Drawer, Layout, Menu, Space, Typography } from "antd";
import { useState, type ReactNode } from "react";
import type { ConnectionState } from "../types";

const { Header, Sider, Content } = Layout;
const { Text } = Typography;

interface AppShellProps {
  activeKey: NavigationKey;
  children: ReactNode;
  connection: ConnectionState;
  contextTitle: string;
  scopeDetail?: string;
  scopeTitle?: string;
}

export type NavigationKey = "fleet" | "robot" | "setup" | "actions" | "workflow" | "catalog" | "operator";

function Navigation({ activeKey, onNavigate }: { activeKey: NavigationKey; onNavigate?: () => void }) {
  const items = [
    {
      icon: <AppstoreOutlined />,
      key: "fleet",
      label: <a href="/#fleet">Fleet overview</a>,
    },
    {
      icon: <RobotOutlined />,
      key: "robot",
      label: <a href="/#robot">Robot details</a>,
    },
    {
      icon: <SettingOutlined />,
      key: "setup",
      label: <a href="/config.html">Operations setup</a>,
    },
    {
      icon: <CodeOutlined />,
      key: "actions",
      label: <a href="/actions.html">Action Library</a>,
    },
    {
      icon: <BranchesOutlined />,
      key: "workflow",
      label: <a href="/workflow.html">Workflow Studio</a>,
    },
    {
      icon: <DatabaseOutlined />,
      key: "catalog",
      label: <a href="/catalog.html">Data catalog</a>,
    },
    {
      icon: <ControlOutlined />,
      key: "operator",
      label: <a href="/operator/">Operator station</a>,
    },
    {
      disabled: true,
      icon: <SafetyCertificateOutlined />,
      key: "policies",
      label: "Policy releases",
    },
  ];

  return (
    <Menu
      className="app-menu"
      items={items}
      mode="inline"
      onClick={onNavigate}
      selectedKeys={[activeKey]}
      theme="dark"
    />
  );
}

function Brand() {
  return (
    <a className="brand-lockup" href="/#fleet" aria-label="SANG Fleet Console">
      <span className="brand-symbol" aria-hidden="true"><RobotOutlined /></span>
      <span>
        <strong>SANG</strong>
        <small>Fleet Console</small>
      </span>
    </a>
  );
}

function SafetyBoundary({ detail, title }: { detail: string; title: string }) {
  return (
    <div className="safety-boundary">
      <SafetyCertificateOutlined />
      <div>
        <strong>{title}</strong>
        <span>{detail}</span>
      </div>
    </div>
  );
}

export default function AppShell({
  activeKey,
  children,
  connection,
  contextTitle,
  scopeDetail = "No robot command path",
  scopeTitle = "Read-only monitor",
}: AppShellProps) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const badgeStatus = connection.kind === "live"
    ? "success"
    : connection.kind === "error" ? "error" : "processing";

  return (
    <Layout className="app-shell">
      <Sider className="desktop-sider" width={248}>
        <Brand />
        <Navigation activeKey={activeKey} />
        <SafetyBoundary detail={scopeDetail} title={scopeTitle} />
      </Sider>

      <Drawer
        className="mobile-navigation"
        onClose={() => setMobileOpen(false)}
        open={mobileOpen}
        placement="left"
        title={<Brand />}
        width={280}
      >
        <Navigation activeKey={activeKey} onNavigate={() => setMobileOpen(false)} />
        <SafetyBoundary detail={scopeDetail} title={scopeTitle} />
      </Drawer>

      <Layout className="workspace-layout">
        <Header className="app-header">
          <Space size={12}>
            <Button
              aria-label="Open navigation"
              className="mobile-menu-button"
              icon={<MenuOutlined />}
              onClick={() => setMobileOpen(true)}
              type="text"
            />
            <div className="header-context">
              <Text type="secondary">LAB OPERATIONS</Text>
              <strong>{contextTitle}</strong>
            </div>
          </Space>
          <div className="connection-readout">
            <Badge status={badgeStatus} />
            <span>
              <strong>{connection.label}</strong>
              <small>{connection.detail}</small>
            </span>
          </div>
        </Header>
        <Content className="app-content">{children}</Content>
      </Layout>
    </Layout>
  );
}
