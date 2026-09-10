import {
  LockOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { Alert, Button, Card, Col, Descriptions, Form, Input, Row, Space, Typography } from "antd";
import { useState } from "react";

const { Paragraph, Text, Title } = Typography;

interface LoginValues {
  password: string;
  principal_id: string;
}

function destination() {
  const requested = new URLSearchParams(window.location.search).get("next");
  if (!requested) return "/";
  try {
    const parsed = new URL(requested, window.location.origin);
    return parsed.origin === window.location.origin ? parsed.href : "/";
  } catch {
    return "/";
  }
}

export default function LoginApp() {
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit(values: LoginValues) {
    setSubmitting(true);
    setError(null);
    try {
      const response = await fetch("/api/v1/auth/login", {
        body: JSON.stringify(values),
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        method: "POST",
      });
      const payload = await response.json().catch(() => ({})) as { error?: string };
      if (!response.ok) throw new Error(payload.error || "Sign-in failed.");
      window.location.replace(destination());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Sign-in failed.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="login-page">
      <Row className="login-frame" gutter={0}>
        <Col className="login-identity" xs={24} lg={12}>
          <div className="login-brand"><RobotOutlined /><strong>SANG</strong><span>Fleet Console</span></div>
          <div className="login-copy">
            <Text>SECURE CENTRAL ACCESS</Text>
            <Title>Open observation.<br />Keep control closed.</Title>
            <Paragraph>Authenticate to inspect robot, camera, recording, and Task Run operations across the lab.</Paragraph>
          </div>
          <Descriptions
            className="login-contract"
            column={1}
            items={[
              { key: "scope", label: "Scope", children: "Robot and camera operations" },
              { key: "boundary", label: "Boundary", children: "Observation and bounded capture" },
              { key: "session", label: "Session", children: "8 hours · Strict cookie" },
            ]}
            size="small"
          />
        </Col>
        <Col className="login-form-column" xs={24} lg={12}>
          <Card className="login-card" bordered={false}>
            <Space className="login-card-heading" direction="vertical" size={4}>
              <SafetyCertificateOutlined />
              <Text className="page-kicker">AUTHORIZED PERSONNEL</Text>
              <Title level={2}>Central sign in</Title>
              <Text type="secondary">Use a registered lab account.</Text>
            </Space>
            {error ? <Alert closable message={error} onClose={() => setError(null)} showIcon type="error" /> : null}
            <Form<LoginValues> layout="vertical" onFinish={submit} requiredMark={false} size="large">
              <Form.Item label="User ID" name="principal_id" rules={[{ required: true, message: "Enter your user ID" }]}>
                <Input autoComplete="username" prefix={<UserOutlined />} />
              </Form.Item>
              <Form.Item label="Password" name="password" rules={[{ min: 12, required: true, message: "Enter your password" }]}>
                <Input.Password autoComplete="current-password" prefix={<LockOutlined />} />
              </Form.Item>
              <Button block htmlType="submit" loading={submitting} type="primary">Start session</Button>
            </Form>
            <Text className="login-safety" type="secondary">Signing in grants no robot command authority. Configuration and capture writes remain role-gated and audited.</Text>
          </Card>
        </Col>
      </Row>
    </main>
  );
}
