import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AutoHunt · 求职智能体",
  description: "从个人材料到职位申请的智能求职工作台",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>{children}</body>
    </html>
  );
}

