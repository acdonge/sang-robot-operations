import { Flex, Typography } from "antd";
import type { ReactNode } from "react";

const { Text, Title } = Typography;

export default function PageHeader({
  actions,
  description,
  eyebrow,
  title,
}: {
  actions?: ReactNode;
  description: string;
  eyebrow: string;
  title: string;
}) {
  return (
    <section className="page-heading">
      <div>
        <Text className="page-kicker">{eyebrow}</Text>
        <Title level={2}>{title}</Title>
        <Text type="secondary">{description}</Text>
      </div>
      {actions ? <Flex align="center" gap={12} wrap>{actions}</Flex> : null}
    </section>
  );
}
