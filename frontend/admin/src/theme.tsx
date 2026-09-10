import { App as AntApp, ConfigProvider, type ThemeConfig } from "antd";
import type { ReactNode } from "react";

export const sangTheme: ThemeConfig = {
  components: {
    Button: {
      controlHeight: 36,
    },
    Card: {
      headerBg: "#ffffff",
    },
    Layout: {
      bodyBg: "#f4f6f8",
      headerBg: "#ffffff",
      siderBg: "#001529",
    },
    Menu: {
      darkItemBg: "#001529",
      darkItemSelectedBg: "#1677ff",
    },
    Table: {
      headerBg: "#fafafa",
      rowHoverBg: "#f5f9ff",
    },
  },
  token: {
    borderRadius: 8,
    colorBgLayout: "#f4f6f8",
    colorError: "#ff4d4f",
    colorPrimary: "#1677ff",
    colorSuccess: "#52c41a",
    colorWarning: "#faad14",
    fontFamily: '"Ubuntu", "Avenir Next", "Segoe UI", sans-serif',
  },
};

export function UiProvider({ children }: { children: ReactNode }) {
  return (
    <ConfigProvider componentSize="middle" theme={sangTheme}>
      <AntApp>{children}</AntApp>
    </ConfigProvider>
  );
}
