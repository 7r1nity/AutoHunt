/** Presentation only: keep the original timestamp available in the UI. */
export function shortJobDate(value: string): string {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return value || "日期未注明";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(date);
}
