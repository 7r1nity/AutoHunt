"use client";

import { Fragment, ReactNode, useLayoutEffect, useRef, useState } from "react";
import type { ResumeLayoutReport } from "@/lib/types";

const sections = /^(个人优势|个人简介|个人信息|基本信息|职业摘要|专业技能|专业能力|技能|技能特长|核心技能|技术技能|工作经历|工作经验|实习经历|实习经验|代表项目|项目经历|项目经验|课内项目|课程项目|课程与研究|代码与工程证据|已确认的补充经历|教育经历|教育背景|在校经历|研究经历|教学经历|作品集|荣誉奖项|获奖经历|证书|资格证书|自我评价|其他信息|其他|求职意向|Summary|Experience|Education|Skills|Projects)[:：]?$/i;
const labels = /^(编程基础|编程语言|深度学习框架|计算机视觉|LLM\s*算法|LLM\s*应用|数据科学与机器学习|自然语言处理|开发工具|工程能力|技术栈|项目简介|项目描述|项目职责|项目成果|主要职责|工作内容|个人贡献|项目亮点|项目背景)[:：]?$/i;

type ResumeLine = { content: string; lineBreak: string; kind: string; display?: string; explicitLabel?: string };
type LayoutHint = { kind: string; explicitLabel?: string };
const skillHeading = /^(LLM\s*\/\s*NLP|RAG\s*\/\s*Agent|训练与评测|部署优化|数据工程|编程基础|编程语言|深度学习框架|计算机视觉|LLM\s*算法|LLM\s*应用|数据科学与机器学习|自然语言处理|开发工具|工程能力|客户管理|销售能力|财务管理|教学能力|设计工具)(?=[：:\s]|$)/i;
const keywordNoise = (text: string) => /^[A-Za-z0-9\s·-]+$/.test(text) && /(?:[A-Za-z0-9-]\s*·\s*){4}/.test(text);

function cleanKeywordNoise(text: string) {
  return text.split(/(?:\s*·\s*){3,}/).map((part) => part.replace(/[·\s]/g, "")).filter(Boolean).join(" · ");
}

function referencedLines(text: string, report?: ResumeLayoutReport | null): ResumeLine[] | null {
  if (!report || !["validated", "needs_review"].includes(report.status)) return null;
  const refs = report.nodes.flatMap((node) => node.refs);
  if (refs.join("|") !== report.blocks.map((block) => block.id).join("|")) return null;
  if (report.blocks.map((block) => block.text).join("") !== text) return null;
  const blocks = new Map(report.blocks.map((block) => [block.id, block.text]));
  const kinds = { title: "title", section: "section", entry: "entry-title", field: "detail", paragraph: "body", unassigned: "unassigned" };
  const hints = new Map<number, LayoutHint>();
  let offset = 0;
  for (const node of report.nodes) {
    const raw = node.refs.map((ref) => blocks.get(ref) ?? "").join("");
    const kind = raw.trim() ? kinds[node.role] : "space";
    const explicitLabel = node.role === "field" && raw.trimStart().startsWith(node.label) ? node.label : undefined;
    hints.set(offset, { kind, explicitLabel });
    offset += raw.length;
  }
  // Coverage validation is not semantic validation. Retain model hints while
  // refining explicit source boundaries, including older saved layout plans.
  return resumeLines(text, hints);
}

// Only known resume headings create fields. A colon in a model tag, URL,
// timestamp, code snippet or explanatory sentence is not a section boundary.
const fieldNames = "项目概述|项目简介|项目描述|项目背景|简介|技术栈|技术选型|核心技术|模型架构|系统架构|基础架构|核心功能实现|核心功能|工程实现|实现要点|主要职责|工作内容|个人贡献|项目职责|项目成果|项目亮点|项目特点|调优与消融实验|调优与评估|优化与评估|调优|问题解决|解决方案|成果|结论|需求分析与项目管理|工程质量与部署";
const fieldPrefix = new RegExp(`^(?:[•●·*-][ \\t]*)?(${fieldNames})(?:[：:][ \\t]*|(?=\\r?\\n|$))`);
// A technology list does not prove that the preceding line is a project title:
// it may be the last wrapped line of the current project's description.
const descriptionStart = /^(?:[•●·*-]\s*)?(?:项目概述|项目简介|项目描述|项目背景|简介)(?:[：:]|$)/;
const bulletStart = /^(?:[•●·*-]|\d+[.、])\s*/;

// Remove PDF wrapping, not words. Explicit paragraphs/fields are kept separate.
function reflow(content: string) {
  const parts = content.split(/\r\n|\r|\n/);
  let value = parts[0].trim();
  for (const part of parts.slice(1)) {
    const next = part.trim();
    // Join Chinese/digits directly. Join split English tokens only when known;
    // otherwise retain a word boundary rather than merging unrelated words.
    const left = value.match(/[A-Za-z]+$/)?.[0] ?? "";
    const right = next.match(/^[A-Za-z]+/)?.[0] ?? "";
    const knownWord = /^(transformers?|huggingface|langchain|pytorch|tensorflow|typescript|javascript|puppeteer|musicxml|convnext|resnet|scikit|learning|classification|svm|fpn|sampling)$/i.test(left + right);
    const space = left && right && !knownWord ? " " : "";
    value += space + next;
  }
  return value;
}

function reflowField(content: string, kind: string) {
  const prefix = kind === "detail" ? content.trimStart().match(fieldPrefix) : null;
  if (!prefix) return reflow(content);
  // Standalone headings keep a newline so editing/re-importing cannot glue
  // the heading to the first word. This is layout whitespace, not added text.
  return prefix[0] + (/[：:]/.test(prefix[0]) ? "" : "\n") + reflow(content.trimStart().slice(prefix[0].length));
}

// Presentation only: retain every source character, including unknown sections.
function resumeLines(text: string, hints = new Map<number, LayoutHint>()): ResumeLine[] {
  const lines = text.match(/[^\r\n]*(?:\r\n|\n|\r|$)/g)?.filter(Boolean) ?? [];
  const firstContent = lines.findIndex((line) => line.trim());
  let offset = 0;
  const parsed: ResumeLine[] = lines.map((raw, index) => {
    const value = raw.trim();
    const lineBreak = raw.match(/(?:\r\n|\n|\r)$/)?.[0] ?? "";
    const content = raw.slice(0, raw.length - lineBreak.length);
    const hint = hints.get(offset);
    offset += raw.length;
    const markdown = value.match(/^(#{1,3})\s+(.+)$/);
    let kind = hint?.kind ?? "body";
    if (!value) kind = "space";
    else if (markdown) kind = markdown[1].length === 1 ? "title" : markdown[1].length === 2 ? "section" : "entry-title";
    else if (sections.test(value)) kind = "section";
    else if (index === firstContent && value.length <= 32 && !/[@：:。；;|]/.test(value)) kind = "title";
    else if (labels.test(value)) kind = "label";
    return { content, lineBreak, kind, display: markdown?.[2], explicitLabel: hint?.explicitLabel };
  });
  // A PDF table category can occur on its second visual line. Only attach a
  // leading list when it ends in an explicit list separator. Keep source order.
  let skills = false;
  for (let index = 0; index < parsed.length; index++) {
    const line = parsed[index];
    if (line.kind === "section") skills = /优势|技能|能力|Skills/i.test(line.display ?? line.content);
    if (!skills || line.kind === "section") continue;
    const match = line.content.trim().match(skillHeading);
    if (!match || line.content.trim() === match[1]) continue;
    const previous = parsed[index - 1];
    const start = previous?.kind === "body" && /[、，,]\s*$/.test(previous.content) ? previous : line;
    start.kind = "skill";
    start.explicitLabel = match[1];
    if (start !== line) line.kind = "body";
  }
  let entrySection = false;
  let skillSection = false;
  let hasTitle = false;
  let hasDetails = false;
  const result: ResumeLine[] = [];
  for (let index = 0; index < parsed.length; index++) {
    const line = parsed[index];
    const value = line.content.trim();
    if (line.kind === "section") {
      entrySection = /项目|工作|实习|研究|教育|教学|作品|经历|Experience|Projects|Education/i.test(line.display ?? value);
      skillSection = /优势|技能|能力|Skills/i.test(line.display ?? value);
      hasTitle = false;
      hasDetails = false;
    }
    if (line.kind === "entry-title") { hasTitle = true; hasDetails = false; }
    if (keywordNoise(value)) {
      const previous = result[result.length - 1];
      if (previous?.kind === "keywords") {
        previous.content += previous.lineBreak + line.content;
        previous.lineBreak = line.lineBreak;
        previous.display = cleanKeywordNoise(previous.content);
        continue;
      }
      line.kind = "keywords";
      line.display = cleanKeywordNoise(line.content);
    }
    if (entrySection && ["body", "label", "detail"].includes(line.kind)) {
      const next = parsed.slice(index + 1).find((item) => item.kind !== "space");
      const datedTitle = /(?:19|20)\d{2}(?:[.年/-]\d{1,2}月?)?\s*[-–—~至]\s*(?:(?:19|20)\d{2}|至?今)/.test(value) && !/^(负责|采用|完成|实现|基于|提升)/.test(value);
      const delimitedTitle = /\s[|｜]\s/.test(value) && next && bulletStart.test(next.content.trim());
      const titleCandidate = value.length <= 110 && !/[。；：:]/.test(value) && !bulletStart.test(value)
        && (datedTitle || delimitedTitle || (next && descriptionStart.test(next.content.trim())));
      if (fieldPrefix.test(value)) {
        line.kind = "detail";
        hasDetails = true;
      } else if (line.kind === "label") {
        hasDetails = true;
      } else if (titleCandidate) {
        line.kind = !hasTitle || hasDetails ? "entry-title" : "subtitle";
        if (line.kind === "entry-title") hasDetails = false;
        hasTitle = true;
      } else { hasDetails = true; }
    }
    if (line.kind === "body" && bulletStart.test(value)) line.kind = "bullet";
    if (line.kind === "body" && /^(示例简历\s*[-—]|注[：:]|备注[：:])/.test(value)) line.kind = "note";
    if (line.kind === "body") {
      const previous = result[result.length - 1];
      if (previous && ["body", "detail", "bullet", "skill"].includes(previous.kind) && (entrySection || skillSection || previous.kind === "bullet")) {
        previous.content += previous.lineBreak + line.content;
        previous.lineBreak = line.lineBreak;
        previous.display = reflowField(previous.content, previous.kind);
        continue;
      }
    }
    result.push(line);
  }
  return result;
}

type IndexedLine = ResumeLine & { index: number };
type LineRenderer = (line: IndexedLine, kind?: string, omitLabel?: boolean) => ReactNode;

function detailLabel(line: ResumeLine) {
  if (line.kind !== "detail") return "";
  return (line.explicitLabel ?? line.content.trim().match(fieldPrefix)?.[1] ?? "").replace(/[：:]\s*$/, "");
}

// Both modes share this layout. Grouping adds visual containers only; all source
// lines remain in order, including unrecognized headings and paragraphs.
function ResumeLayout({ lines, renderLine, editing = false }: { lines: ResumeLine[]; renderLine: LineRenderer; editing?: boolean }) {
  const indexed = lines.map((line, index) => ({ ...line, index }));
  const titleIndex = indexed.findIndex((line) => line.kind === "title");
  const headerLines = titleIndex < 0 ? [] : indexed.slice(0, titleIndex + 1);
  const rest = indexed.slice(titleIndex + 1);
  const preamble: IndexedLine[] = [];
  const groups: { heading: IndexedLine; content: IndexedLine[] }[] = [];
  for (const line of rest) {
    if (line.kind === "section") groups.push({ heading: line, content: [] });
    else if (groups.length) groups[groups.length - 1].content.push(line);
    else preamble.push(line);
  }
  const render = (line: IndexedLine, kind?: string, omitLabel = false) => <Fragment key={line.index}>{renderLine(line, kind, omitLabel)}</Fragment>;
  const renderChunk = (chunk: IndexedLine[]) => {
    const result: ReactNode[] = [];
    for (let index = 0; index < chunk.length; index++) {
      const line = chunk[index];
      const label = detailLabel(line);
      const run = [line];
      while (label && index + 1 < chunk.length && detailLabel(chunk[index + 1]) === label) run.push(chunk[++index]);
      if (run.length === 1) result.push(render(line));
      else result.push(<section className="imported-detail-group" key={line.index}>
        <h4>{label}</h4>
        <ul>{run.map((item) => <li key={item.index}>{render(item, "group-item", !editing)}</li>)}</ul>
      </section>);
    }
    return result;
  };
  const renderGroup = (heading: string, content: IndexedLine[]) => {
    const skills = /优势|技能|能力|Skills/i.test(heading);
    const entries = content.some((line) => line.kind === "entry-title") || /项目|工作|实习|研究|教育|Experience|Projects|Education/i.test(heading);
    const chunks: IndexedLine[][] = [];
    for (const line of content) {
      const entryStart = entries && line.kind === "entry-title";
      if (!chunks.length || (skills && ["label", "skill", "detail"].includes(line.kind)) || entryStart) chunks.push([]);
      chunks[chunks.length - 1].push(line);
    }
    return <div className={skills ? "resume-skill-matrix imported-skill-matrix" : "resume-entry-list"}>
      {chunks.map((chunk, index) => <div className={chunk.every((line) => line.kind === "space") ? "imported-empty-group" : skills ? "imported-skill-group" : "resume-entry-card imported-entry-card"} key={chunk[0].index}>
        {entries && <span className="imported-entry-kicker" aria-hidden="true">{ /项目|Projects/i.test(heading) ? "PROJECT" : "EXPERIENCE" } {String(index + 1).padStart(2, "0")}</span>}
        {renderChunk(chunk)}
      </div>)}
    </div>;
  };
  return <div className={`imported-resume${editing ? " imported-resume-editing" : ""}`} role={editing ? "group" : undefined} aria-label={editing ? "排版简历编辑器" : undefined}>
    <header className="resume-document-header">
      <span className="resume-document-kicker" aria-hidden="true">PROFESSIONAL PROFILE</span>
      {headerLines.length ? headerLines.map((line) => render(line)) : <h1 className="resume-title">简历草稿</h1>}
      <p>个人简历 · 保留原文内容</p>
    </header>
    <div className="resume-document-body">
      {preamble.length > 0 && <div className="resume-summary-block imported-preamble"><i aria-hidden="true">“</i><div>{preamble.map((line) => render(line))}</div></div>}
      {groups.map(({ heading, content }, index) => <section className="resume-section" key={heading.index}>
        <div className="resume-section-heading">{render(heading)}<span aria-hidden="true">{String(index + 1).padStart(2, "0")}</span></div>
        {renderGroup(heading.display ?? heading.content, content)}
      </section>)}
    </div>
  </div>;
}

function lineClass(kind: string) {
  return `imported-resume-${kind} ${kind === "title" ? "resume-title" : kind === "section" ? "resume-section-title" : kind === "entry-title" ? "resume-entry-title" : ""}`;
}

export function ImportedResume({ text, layout }: { text: string; layout?: ResumeLayoutReport | null }) {
  return <ResumeLayout lines={referencedLines(text, layout) ?? resumeLines(text)} renderLine={(line, presentationKind, omitLabel) => {
    const kind = presentationKind ?? line.kind;
    const Tag = kind === "title" ? "h1" : kind === "section" ? "h2" : kind === "label" || kind === "entry-title" ? "h3" : "div";
    const display = line.display ?? (kind === "detail" ? reflowField(line.content, kind) : line.content);
    const prefix = line.explicitLabel && kind !== "skill" ? [line.explicitLabel, line.explicitLabel.replace(/[：:]\s*$/, "")] : line.kind === "detail" ? display.trimStart().match(fieldPrefix) : null;
    const skillLabel = kind === "skill" ? line.explicitLabel : undefined;
    const skillText = skillLabel ? reflow(line.content.replace(skillLabel, "")).replace(/^\s*[:：]\s*/, "") : "";
    return <><Tag className={lineClass(kind)} data-field-label={prefix?.[1]} data-resume-source={line.index} data-source-content={line.content} data-line-break={line.lineBreak}>{skillLabel ? <><strong className="imported-skill-label">{skillLabel}</strong><span>{skillText}</span></> : prefix ? <>{!omitLabel && <strong className="imported-field-label">{prefix[0]}</strong>}<span>{display.trimStart().slice(prefix[0].length).trim()}</span></> : display}</Tag>{line.lineBreak}</>;
  }} />;
}

function EditableLine({ line, index, disabled, onChange }: { line: ResumeLine; index: number; disabled: boolean; onChange: (value: string) => void }) {
  const field = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const element = field.current;
    if (!element) return;
    const resize = () => {
      element.style.height = "0px";
      element.style.height = `${element.scrollHeight + 2}px`;
    };
    resize();
    // Height changes must not retrigger the observer indefinitely.
    let width = element.getBoundingClientRect().width;
    const observer = new ResizeObserver(() => {
      const nextWidth = element.getBoundingClientRect().width;
      if (width !== nextWidth) { width = nextWidth; resize(); }
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [line.content, line.display]);
  return <textarea ref={field} className={`imported-resume-field ${lineClass(line.kind)}`} rows={1} value={line.display ?? line.content} disabled={disabled} onChange={(event) => onChange(event.target.value)} aria-label={`简历第 ${index + 1} 段`} spellCheck={false} />;
}

// Stable paragraph fields preserve the caret, support plain-text paste and never
// serialize editable HTML. Saving uses the existing encrypted text draft API.
export function ImportedResumeEditor({ text, layout, disabled, onChange }: { text: string; layout?: ResumeLayoutReport | null; disabled: boolean; onChange: (text: string) => void }) {
  const [lines, setLines] = useState(() => referencedLines(text, layout) ?? resumeLines(text));
  const update = (index: number, content: string) => {
    const next = lines.map((line, lineIndex) => lineIndex === index ? { ...line, content, display: undefined } : line);
    setLines(next);
    onChange(next.map((line) => line.content + line.lineBreak).join(""));
  };
  return <ResumeLayout lines={lines} editing renderLine={(line, presentationKind) => line.kind === "space"
    ? <div className="imported-resume-space" />
    : <EditableLine line={{ ...line, kind: presentationKind ?? line.kind }} index={line.index} disabled={disabled} onChange={(content) => update(line.index, content)} /> } />;
}
