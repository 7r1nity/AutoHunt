import { Fragment } from "react";

// Small, text-only renderer for repository reports. Raw HTML and links are not
// executed; the downloadable report retains the original Markdown.
function inline(text: string) {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g).map((part, index) =>
    part.startsWith("**") ? <strong key={index}>{part.slice(2, -2)}</strong> :
      part.startsWith("`") ? <code key={index}>{part.slice(1, -1)}</code> :
        <Fragment key={index}>{part.replace(/^\[([^\]]+)\]\([^)]+\)$/, "$1")}</Fragment>);
}

export function EvaluationDocument({ text }: { text: string }) {
  const lines = text.split(/\r?\n/);
  const blocks = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;
    if (line.startsWith("```")) {
      const code = [];
      while (++i < lines.length && !lines[i].startsWith("```")) code.push(lines[i]);
      blocks.push(<pre key={i}>{code.join("\n")}</pre>);
    } else if (line.startsWith("|")) {
      const rows = [line];
      while (i + 1 < lines.length && lines[i + 1].trim().startsWith("|")) rows.push(lines[++i].trim());
      const cells = (row: string) => row.replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
      const body = rows.slice(1).filter((row) => !/^\|[\s:|\-]+\|$/.test(row));
      blocks.push(<div className="metrics-table-wrap" key={i}><table><thead><tr>{cells(rows[0]).map((cell, j) => <th key={j}>{inline(cell)}</th>)}</tr></thead><tbody>{body.map((row, j) => <tr key={j}>{cells(row).map((cell, k) => <td key={k}>{inline(cell)}</td>)}</tr>)}</tbody></table></div>);
    } else if (/^#{1,6}\s/.test(line)) {
      blocks.push(<h4 key={i}>{inline(line.replace(/^#+\s*/, ""))}</h4>);
    } else {
      blocks.push(<p key={i}>{inline(line)}</p>);
    }
  }
  return <div className="evaluation-document">{blocks}</div>;
}
