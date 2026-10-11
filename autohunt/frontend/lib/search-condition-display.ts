type Condition = {
  op?: string; text?: string; mode?: string; children?: Condition[];
  salary?: { unit?: string; minimum?: number | null; maximum?: number | null } | null;
};

// Display is a projection of the typed tree, never another natural-language parser.
export function filterText(value: unknown): string {
  if (!value || typeof value !== "object") return "";
  const node = value as Condition;
  if (node.op === "condition") {
    const prefix = node.mode === "allow" ? "允许包含（非必选）：" : node.mode === "prefer" ? "优先（非必选）：" : "";
    return prefix + (node.text ?? "");
  }
  const children = (node.children ?? []).map(filterText);
  return node.op === "not" ? `不满足（${children[0]}）` : `（${children.join(node.op === "all" ? " 且 " : " 或 ")}）`;
}

export function monthlySalaryText(filters: unknown, legacy: number | null): string {
  if (!filters || typeof filters !== "object") return legacy ? `${legacy.toLocaleString("zh-CN")} 元` : "不限";
  const bounds: number[] = [];
  let hardMonthly = false;
  let ownsSalaryScope = false;
  const visit = (node: Condition, negated = false) => {
    if (node.op === "condition" && (node.mode ?? "require") === "require" && ["monthly", "negotiable"].includes(node.salary?.unit ?? "")) ownsSalaryScope = true;
    if (node.op === "condition" && (node.mode ?? "require") === "require" && !negated) {
      if (node.salary?.unit === "monthly") {
        hardMonthly = true;
        if (typeof node.salary.minimum === "number") bounds.push(node.salary.minimum);
      }
    }
    for (const child of node.children ?? []) visit(child, negated || node.op === "not");
  };
  visit(filters as Condition);
  const distinct = [...new Set(bounds)].sort((a, b) => a - b);
  if (distinct.length) return distinct.map(value => `${value.toLocaleString("zh-CN")} 元`).join(" / ") + "（适用范围及例外见下方）";
  if (!ownsSalaryScope && legacy) return `${legacy.toLocaleString("zh-CN")} 元（适用范围及例外见下方）`;
  return hardMonthly ? "未设置下限（见下方条件）" : "见下方条件";
}
