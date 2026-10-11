import type { CSSProperties } from "react";

const paths = {
  overview: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  chat: "M20 11a8 8 0 0 1-8 8H7l-4 3v-7a8 8 0 1 1 17-4Z M8 10h8 M8 14h5",
  board: "M3 4h18v16H3z M9 4v16 M15 4v16 M5.5 8h1 M11.5 8h1 M17.5 8h1 M5.5 12h1 M11.5 12h1",
  profile: "M6 3h8l4 4v14H6z M14 3v5h4 M9 12h6 M9 16h6",
  insights: "M4 3v17h17 M8 16v-5 M13 16V7 M18 16V4",
  plans: "M4 5h16v16H4z M8 3v4 M16 3v4 M8 14l3 3 5-6",
  settings: "M4 6h16 M4 12h16 M4 18h16 M9 3v6 M16 9v6 M8 15v6",
  arrow: "M4 12h16 M14 6l6 6-6 6",
  search: "M16 16l5 5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z",
  check: "M5 12l4 4L19 6",
  send: "M12 20V4 M5 11l7-7 7 7",
  plus: "M12 5v14 M5 12h14",
} as const;

export type WorkspaceIconName = keyof typeof paths;

export function WorkspaceIcon({ name, className, style }: {
  name: WorkspaceIconName; className?: string; style?: CSSProperties;
}) {
  return <svg className={["workspace-icon", className].filter(Boolean).join(" ")} style={style} width="20" height="20" viewBox="0 0 24 24"
    fill="none" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"
    aria-hidden="true" focusable="false"><path d={paths[name]} /></svg>;
}
