"use client";

import { ChangeEvent, FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { ImportedResume, ImportedResumeEditor } from "@/components/imported-resume";
import { useAgentRuntime } from "@/lib/use-agent-runtime";
import { RuntimePanel } from "@/components/runtime-panel";
import { terminalRun } from "@/lib/agent-runtime";
import { createChatTurn, createMessageId } from "@/lib/chat-message";
import { inventoryRequest, inventoryError } from "@/lib/inventory-request";
import { shortJobDate } from "@/lib/job-display";
import type { AgentStatus, AutomationStats, BrowserTask, CandidateMaterial, CandidateState, ClarifyOption, DeliveryPreview, DeliveryTask, Insights, Job, JobStage, LearningPlan, Material, MaterialIngestResult, DocumentIngestResult, Message, MockWorkflowSettings, ResumeDocumentMeta, ResumeIngestResult, ResumeProfile, ResumeProposal, SearchReport } from "@/lib/types";

const stageMeta: Record<JobStage, { title: string; hint: string }> = {
  review: { title: "待审核", hint: "找到但尚未申请" },
  applying: { title: "等待回复 / 正在申请", hint: "已沟通，等待 HR 回复或准备发简历" },
  waiting: { title: "已投递", hint: "简历已发送，等待后续进展" },
  interview: { title: "面试进展", hint: "需要你的关注" },
  outcome_indeterminate: { title: "待人工核验", hint: "结果不确定，禁止重试" },
};

const navItems = [
  ["overview", "⌁", "工作台"],
  ["chat", "✦", "Agent 对话"],
  ["board", "▦", "申请看板"],
  ["profile", "◫", "简历"],
  ["insights", "⌗", "统计"],
  ["plans", "✓", "成长计划"],
  ["settings", "⚙", "模型设置"],
] as const;

function now() {
  return new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

function scoreClass(score: number) {
  if (score >= 90) return "score score-high";
  if (score >= 85) return "score score-good";
  return "score";
}

function sameSnapshot(left: unknown, right: unknown) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function inlineRichText(text: string) {
  return text.split(/(\*\*.*?\*\*|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g).map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
    const link = part.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
    return link ? <a key={index} href={link[2]} target="_blank" rel="noopener noreferrer">{link[1]}</a> : part;
  });
}

function renderRichText(text: string) {
  const blocks = text.split(/\n+/);
  return blocks.map((block, index) => {
    const trimmed = block.trim();
    if (!trimmed) return null;
    if (/^#{1,3}\s/.test(trimmed)) {
      return <h4 className="md-heading" key={index}>{inlineRichText(trimmed.replace(/^#{1,3}\s/, ""))}</h4>;
    }
    if (/^[-•*]\s+/.test(trimmed)) {
      return <div className="md-li" key={index}><span className="md-bullet">•</span><span>{inlineRichText(trimmed.replace(/^[-•*]\s+/, ""))}</span></div>;
    }
    const numbered = trimmed.match(/^(\d+)[.、]\s*(.*)$/);
    if (numbered) {
      return <div className="md-li" key={index}><span className="md-bullet">{numbered[1]}.</span><span>{inlineRichText(numbered[2])}</span></div>;
    }
    return <p className="md-p" key={index}>{inlineRichText(trimmed)}</p>;
  });
}

type ResumeDraftItem = { label: string; value: string };
type ResumeDraftEntry = { title: string; items: ResumeDraftItem[]; paragraphs: string[] };
type ResumeDraftSection = { title: string; items: ResumeDraftItem[]; paragraphs: string[]; entries: ResumeDraftEntry[] };

function cleanResumeText(value: string) {
  return value
    .replace(/Tra\s+nsformer/gi, "Transformer")
    .replace(/ResNet-FP\s+N/gi, "ResNet-FPN")
    .replace(/\bS\s+VM\b/g, "SVM")
    .replace(/PDF\s+导\s+出/gi, "PDF 导出")
    .replace(/无\s+需/g, "无需")
    .replace(/\s+([，。；：、])/g, "$1")
    .replace(/[、，]\s*[。、，]+$/g, "")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function parseResumeDocument(draft: string) {
  let title = "个人简历";
  const sections: ResumeDraftSection[] = [];
  let currentSection: ResumeDraftSection | null = null;
  let currentEntry: ResumeDraftEntry | null = null;

  for (const rawLine of draft.split("\n")) {
    const line = rawLine.trim();
    if (!line) continue;
    if (line.startsWith("# ")) {
      title = cleanResumeText(line.slice(2)) || title;
      continue;
    }
    if (line.startsWith("## ")) {
      currentSection = { title: cleanResumeText(line.slice(3)), items: [], paragraphs: [], entries: [] };
      sections.push(currentSection);
      currentEntry = null;
      continue;
    }
    if (line.startsWith("### ")) {
      if (!currentSection) {
        currentSection = { title: "项目经历", items: [], paragraphs: [], entries: [] };
        sections.push(currentSection);
      }
      currentEntry = { title: cleanResumeText(line.slice(4)), items: [], paragraphs: [] };
      currentSection.entries.push(currentEntry);
      continue;
    }
    if (line.startsWith("- ")) {
      const content = cleanResumeText(line.slice(2));
      const labeled = content.match(/^([^：:]{1,16})[：:]\s*(.*)$/);
      const item = labeled
        ? { label: cleanResumeText(labeled[1]), value: cleanResumeText(labeled[2]) }
        : { label: "要点", value: content };
      if (currentEntry) currentEntry.items.push(item);
      else if (currentSection) currentSection.items.push(item);
      continue;
    }
    if (currentEntry) currentEntry.paragraphs.push(cleanResumeText(line));
    else if (currentSection) currentSection.paragraphs.push(cleanResumeText(line));
  }
  return { title, sections };
}

function splitResumeSkills(value: string) {
  const seen = new Set<string>();
  return value
    .split(/[、,，;；/]+/)
    .map(cleanResumeText)
    .filter((skill) => {
      const key = skill.toLocaleLowerCase();
      if (!skill || /^[。.、，;；]+$/.test(skill) || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

function groupResumeSkills(skills: string[]) {
  const definitions = [
    { title: "编程与应用开发", test: /Python|TypeScript|JavaScript|React|Node|Express|\bC\b|前端|后端|API/i },
    { title: "大模型与 Agent", test: /LLM|大语言|Transformer|BERT|LangChain|LangGraph|RAG|Agent|MCP|Ollama|Embedding|提示工程|Hugging/i },
    { title: "机器学习与视觉", test: /PyTorch|TensorFlow|Keras|OpenCV|CNN|ResNet|ViT|YOLO|Faster|ConvNeXt|torchvision|CUDA|目标检测|深度学习|计算机视觉/i },
    { title: "NLP 与数据科学", test: /NLP|NLTK|spaCy|WordNet|FastText|gensim|pandas|scikit|LightGBM|SentiWordNet|情感分析|特征工程|模型评估/i },
    { title: "工程化与工具", test: /Docker|Git|Linux|Jest|Gradio|Matplotlib|Seaborn|Puppeteer|自动化测试|容器化|VexFlow|MusicXML/i },
  ];
  const groups = definitions.map((item) => ({ title: item.title, skills: [] as string[] }));
  const other: string[] = [];
  for (const skill of skills) {
    const match = definitions.findIndex((item) => item.test.test(skill));
    if (match >= 0) groups[match].skills.push(skill);
    else other.push(skill);
  }
  if (other.length) groups.push({ title: "其他能力", skills: other });
  return groups.filter((group) => group.skills.length);
}

function resumeLabel(label: string) {
  if (/项目简介|经历简介|简介|项目概述|项目描述/.test(label)) return "项目概述";
  if (/基础架构|模型架构|系统架构/.test(label)) return "系统架构";
  if (/项目特点|核心功能|亮点|工程实现/.test(label)) return "核心实现";
  if (/调优|优化|增强|消融|评估/.test(label)) return "调优与评估";
  if (/成果|结论|成绩/.test(label)) return "成果";
  if (/技术栈/.test(label)) return "技术栈";
  return label;
}

function renderResumeEntry(entry: ResumeDraftEntry, index: number) {
  const grouped = new Map<string, string[]>();
  for (const item of entry.items) {
    const label = resumeLabel(item.label);
    grouped.set(label, [...(grouped.get(label) ?? []), item.value]);
  }
  const summaries = grouped.get("项目概述") ?? entry.paragraphs;
  const skillText = grouped.get("技术栈")?.join("、") ?? "";
  grouped.delete("项目概述");
  grouped.delete("技术栈");

  return (
    <article className="resume-entry-card" key={`${entry.title}-${index}`}>
      <header className="resume-entry-header">
        <div>
          <span>PROJECT {String(index + 1).padStart(2, "0")}</span>
          <h3 className="resume-entry-title">{entry.title}</h3>
        </div>
      </header>
      {summaries.map((paragraph, paragraphIndex) => <p className="resume-entry-summary" key={paragraphIndex}>{paragraph}</p>)}
      {skillText && (
        <div className="resume-entry-stack">
          <span>技术栈</span>
          <div>{splitResumeSkills(skillText).map((skill) => <b key={skill}>{skill}</b>)}</div>
        </div>
      )}
      <div className="resume-entry-details">
        {Array.from(grouped.entries()).map(([label, values]) => (
          <section className={`resume-detail-group ${label === "系统架构" ? "architecture" : ""}`} key={label}>
            <h4>{label}</h4>
            <ul>{values.map((value, valueIndex) => <li key={valueIndex}>{value}</li>)}</ul>
          </section>
        ))}
      </div>
    </article>
  );
}

function renderResumeDocument(draft: string, forceVerbatim = false, layout?: ResumeDocumentMeta["layout"]) {
  const verbatim = forceVerbatim || !/^#{1,3}\s+/m.test(draft);
  if (verbatim || layout?.status === "validated" || layout?.status === "needs_review") {
    return <ImportedResume text={draft} layout={layout} />;
  }
  const document = parseResumeDocument(draft);
  return (
    <>
      <header className="resume-document-header">
        <span className="resume-document-kicker">PROFESSIONAL PROFILE</span>
        <h1 className="resume-title">{document.title}</h1>
        <p>结构化候选人简历 · 基于已验证材料生成</p>
      </header>
      <div className="resume-document-body">
        {document.sections.map((section, sectionIndex) => {
          const isSkills = section.title.includes("技能") || section.title.includes("能力");
          const isSummary = section.title.includes("摘要") || section.title.includes("简介");
          const skillValues = splitResumeSkills([
            ...section.paragraphs,
            ...section.items.map((item) => item.value),
          ].join("、"));
          return (
            <section className={`resume-section ${isSkills ? "skills-section" : ""}`} key={`${section.title}-${sectionIndex}`}>
              <div className="resume-section-heading">
                <h2 className="resume-section-title">{section.title}</h2>
                <span>{String(sectionIndex + 1).padStart(2, "0")}</span>
              </div>
              {isSummary && section.paragraphs.length > 0 && (
                <div className="resume-summary-block"><i>“</i><p>{section.paragraphs.join(" ")}</p></div>
              )}
              {isSkills && (
                <div className="resume-skill-matrix">
                  {groupResumeSkills(skillValues).map((group) => (
                    <div key={group.title}><strong>{group.title}</strong><p>{group.skills.join(" · ")}</p></div>
                  ))}
                </div>
              )}
              {!isSummary && !isSkills && section.paragraphs.map((paragraph, index) => <p className="resume-paragraph" key={index}>{paragraph}</p>)}
              {!isSummary && !isSkills && section.items.map((item, index) => (
                <div className="resume-bullet" key={index}><i /><p><strong>{resumeLabel(item.label)}</strong><span>{item.value}</span></p></div>
              ))}
              {section.entries.length > 0 && <div className="resume-entry-list">{section.entries.map(renderResumeEntry)}</div>}
            </section>
          );
        })}
      </div>
    </>
  );
}


function formatProposalValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "（无）";
  if (Array.isArray(value)) return value.length ? value.join("；") : "（空列表）";
  if (typeof value === "object") {
    const entry = value as { title?: string; summary?: string; highlights?: string[] };
    return [entry.title, entry.summary, ...(entry.highlights ?? [])].filter(Boolean).join(" · ");
  }
  return String(value);
}


function preferredSearchQuery(state: CandidateState) {
  const confirmedRole = state.preferences.target_roles.source === "user_confirmed" ? state.preferences.target_roles.values[0] : undefined;
  const resumeRole = state.profile.resume_id ? state.profile.role_suggestions[0] : undefined;
  return confirmedRole || resumeRole || state.preferences.search_keywords.values[0] || "";
}

function summarizeSearch(task: BrowserTask, report: SearchReport | null) {
  if (report?.message) return report.message; // Same evidence/report/order as the runtime summary.
  const funnel = task.funnel;
  const lines = [
    `**搜索完成：${task.query || "目标岗位"}** · Run ${task.id?.slice(0, 8) ?? "—"}`,
    `本轮发现 ${funnel.unique_jobs} 个不同职位；新增看板 ${task.stored} 个，已有职位去重 ${task.duplicates} 个。`,
  ];
  if (report?.covered_jobs) {
    const distribution = (items: Record<string, number>) => Object.entries(items)
      .sort((a, b) => b[1] - a[1])
      .map(([label, count]) => `${label} ${count} 个`).join("、") || "JD 未明确标注";
    lines.push(
      "",
      "### 本轮岗位画像（含重复职位）",
      `- **薪资分布**：${distribution(report.salary_distribution)}`,
      `- **招聘类型**：${distribution(report.employment_distribution)}`,
      `- **JD 明确提及的福利**：${distribution(report.benefit_distribution)}`,
      "",
      "### 值得查看的职位",
    );
    report.highlights.forEach((job, index) => {
      lines.push(
        `${index + 1}. **${job.title}** · ${job.company} · ${job.location} · ${job.salary} · ${job.employment_type} · 匹配 ${job.score} 分`,
        `   JD：${job.jd_excerpt || "详情未读取；请打开职位核对"}`,
        `   福利：${job.benefits.join("、") || "JD 未明确标注"}`,
      );
    });
    lines.push("", "薪资、招聘类型和福利仅依据本轮读到的职位信息；未标注不代表没有。");
  } else {
    lines.push("本轮没有可核对的职位详情；无法可靠总结薪资、福利或 JD。");
  }
  lines.push("", `采集情况：链接 ${funnel.links_seen}，解析 ${funnel.cards_parsed}，详情读取 ${funnel.visited}，初筛跳过 ${funnel.filtered}，错误 ${funnel.errors}。`);
  const reasons = Object.entries(funnel.reason_counts)
    .filter(([reason]) => !["stored", "duplicate"].includes(reason))
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);
  if (reasons.length) lines.push(`- **主要损耗原因**：${reasons.map(([reason, count]) => `${reason} ${count}`).join("；")}`);
  const samples = funnel.samples.filter((item) => !["stored", "duplicate"].includes(item.reason_code)).slice(0, 3);
  if (samples.length) lines.push("", "### 损耗样例", ...samples.map((item) => `- ${item.title ? `${item.title}：` : ""}${item.detail}${item.reason ? `（${item.reason}）` : ""}`));
  if (task.stored === 0) {
    lines.push("- 本次没有新增可审核职位；可能是详情链接失效、读取失败、命中排除词或方向明显不符，不代表发现的全部链接都被初筛跳过。");
  }
  if (task.dry_run) {
    lines.push(
      "",
      "### 模拟站 Dry Run 报告",
      `- **高匹配候选**：${task.dry_run_candidates} 个`,
      `- **已自动发起沟通**：${task.dry_run_contacted} 个`,
      `- **检测到明确回复**：${task.dry_run_replied} 个`,
      `- **已点击发送简历并确认成功**：${task.dry_run_submitted} 个`,
      `- **等待招聘方回复**：${task.dry_run_waiting_reply} 个`,
      `- **失败或待核验**：${task.dry_run_failed} 个`,
    );
    if (task.dry_run_report) lines.push("", task.dry_run_report);
    lines.push("", "本轮只操作本地模拟招聘网站，没有访问或投递任何真实招聘平台。");
  }
  lines.push("可点击下方按钮打开**申请看板**查看职位详情。");
  return lines.join("\n");
}

export default function Home() {
  const runtime = useAgentRuntime();
  const [activeView, setActiveView] = useState<(typeof navItems)[number][0]>("overview");
  const [conversationLoading, setConversationLoading] = useState(true);
  const [chatDetailsOpen, setChatDetailsOpen] = useState(false);
  const [agentStatus, setAgentStatus] = useState<AgentStatus>("idle");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [materials, setMaterials] = useState<Material[]>([]);
  const [resumes, setResumes] = useState<ResumeProfile[]>([]);
  const [candidateMaterials, setCandidateMaterials] = useState<CandidateMaterial[]>([]);
  const [candidateState, setCandidateState] = useState<CandidateState | null>(null);
  const [resumeSourceMode, setResumeSourceMode] = useState<ResumeDocumentMeta["source_mode"]>("structured");
  const [resumeDocument, setResumeDocument] = useState<ResumeDocumentMeta | null>(null);
  const [layoutBusy, setLayoutBusy] = useState(false);
  const [resumeViewMode, setResumeViewMode] = useState<"original" | "text">("text");
  const [resumeDraft, setResumeDraft] = useState("");
  const [resumeSavedDraft, setResumeSavedDraft] = useState("");
  const [resumeEditing, setResumeEditing] = useState(false);
  const [resumeSaveBusy, setResumeSaveBusy] = useState(false);
  const [resumeBusy, setResumeBusy] = useState(false);
  const [uploadFeedback, setUploadFeedback] = useState<{ kind: "progress" | "success" | "error"; text: string } | null>(null);
  useEffect(() => {
    if (!candidateState) return;
    const controller = new AbortController();
    void fetch("/api/agent/documents", { cache: "no-store", signal: controller.signal })
      .then(async (response) => { if (response.ok) { const meta = await response.json(); if (!controller.signal.aborted) setResumeDocument(meta); } })
      .catch(() => {});
    return () => controller.abort();
  }, [candidateState]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [chatText, setChatText] = useState("");
  const [notice, setNotice] = useState("在 Agent 对话中发送「搜索岗位」或「搜索 + 关键词」即可开始；浏览器会自动拉起");
  const [llmProvider, setLlmProvider] = useState<"mock" | "deepseek">("mock");
  const [llmModel, setLlmModel] = useState("mock-autohunt-v1");
  const [llmApiKey, setLlmApiKey] = useState("");
  const [llmConfigured, setLlmConfigured] = useState(false);
  const [llmBusy, setLlmBusy] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [browserTask, setBrowserTask] = useState<BrowserTask | null>(null);
  const [browserActionBusy, setBrowserActionBusy] = useState(false);
  const [deliveryPreview, setDeliveryPreview] = useState<DeliveryPreview | null>(null);
  const [deliveryTask, setDeliveryTask] = useState<DeliveryTask | null>(null);
  const [workflowSettings, setWorkflowSettings] = useState<MockWorkflowSettings | null>(null);
  const [workflowBusy, setWorkflowBusy] = useState(false);
  const [workflowNotice, setWorkflowNotice] = useState("");
  const [automationStats, setAutomationStats] = useState<AutomationStats>({ discovered: 0, stored: 0, duplicates: 0, matched: 0, review: 0, applying: 0, waiting: 0, interview: 0, indeterminate: 0, dry_run_candidates: 0, dry_run_contacted: 0, dry_run_replied: 0, dry_run_submitted: 0, dry_run_waiting_reply: 0, dry_run_failed: 0 });
  const [insights, setInsights] = useState<Insights | null>(null);
  const [learningPlans, setLearningPlans] = useState<LearningPlan[]>([]);
  const [planBusy, setPlanBusy] = useState(false);
  const [planNotice, setPlanNotice] = useState("");
  const [planFocus, setPlanFocus] = useState("");
  const [completionEvidence, setCompletionEvidence] = useState<Record<string, string>>({});
  const [completionTargets, setCompletionTargets] = useState<Record<string, "skill" | "project" | "none">>({});
  const [completionTitles, setCompletionTitles] = useState<Record<string, string>>({});
  const [planProposals, setPlanProposals] = useState<Record<string, ResumeProposal>>({});
  const uploadRef = useRef<HTMLInputElement>(null);
  const materialUploadRef = useRef<HTMLInputElement>(null);
  const chatUploadRef = useRef<HTMLInputElement>(null);
  const [materialKind, setMaterialKind] = useState<"course" | "project" | "code">("project");
  const [detailJobId, setDetailJobId] = useState<number | null>(null);
  const announcedTaskIdsRef = useRef<Set<string>>(new Set());
  const ownedTaskIdsRef = useRef<Set<string>>(new Set());
  const runtimeTaskIdsRef = useRef<Set<string>>(new Set());
  const runtimeNotificationIdsRef = useRef<Set<string>>(new Set());
  const statisticsSnapshotRef = useRef("");
  const activeTaskIdRef = useRef<string | null>(null);
  const deliveryTaskIdRef = useRef<string | null>(null);
  const prevTaskStatusRef = useRef<BrowserTask["status"] | null>(null);
  const deliveryStatusRef = useRef<DeliveryTask["status"] | null>(null);

  useEffect(() => {
    if (candidateState && !resumeEditing) {
      setResumeDraft(candidateState.resume_draft);
      setResumeSavedDraft(candidateState.resume_draft);
    }
  }, [candidateState, resumeEditing]);


  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const [resumeResponse, materialResponse, stateResponse, documentResponse] = await Promise.all([
          inventoryRequest("/api/agent/resumes", controller.signal),
          inventoryRequest("/api/agent/materials", controller.signal),
          inventoryRequest("/api/agent/candidate-state", controller.signal),
          inventoryRequest("/api/agent/documents", controller.signal),
        ]);
        if (controller.signal.aborted) return;
        if (!resumeResponse.ok) return;
        const stored: ResumeProfile[] = await resumeResponse.json();
        const storedMaterials: CandidateMaterial[] = materialResponse.ok ? await materialResponse.json() : [];
        setResumes(stored);
        setCandidateMaterials(storedMaterials);
        setMaterials([
          ...stored.map((resume) => ({ id: resume.id, name: resume.filename, type: "简历", status: "已解析" as const })),
          ...storedMaterials.map((material) => ({ id: material.id, name: material.filename, type: material.kind === "course" ? "课程报告" : material.kind === "code" ? "代码" : "项目材料", status: "已解析" as const })),
        ]);
        if (stateResponse.ok) {
          const state: CandidateState = await stateResponse.json();
          setCandidateState(state);
          setSearchQuery(preferredSearchQuery(state));
        } else if (stored[0]?.suggested_query) setSearchQuery(stored[0].suggested_query);
        if (documentResponse.ok) {
          const document: ResumeDocumentMeta = await documentResponse.json();
          setResumeSourceMode(document.source_mode);
          setResumeDocument(document);
        }
      } catch {
        // The rest of the dashboard remains usable while the backend starts.
      }
    })();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setConversationLoading(true);
    setMessages([]);
    ownedTaskIdsRef.current.clear();
    activeTaskIdRef.current = null;
    deliveryTaskIdRef.current = null;
    void fetch(`/api/agent/chat?conversation_id=${runtime.sessionId}`, { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) return;
        const history = await response.json() as { messages?: Array<{ role: "user" | "assistant"; content: string; created_at: string; response?: { resume_proposal?: ResumeProposal } }> };
        if (controller.signal.aborted) return;
        setMessages((history.messages ?? []).map((item, index) => ({
          id: `history:${runtime.sessionId}:${index}`, author: item.role === "assistant" ? "agent" : "user", text: item.content,
          time: new Date(item.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }),
          ...(item.response?.resume_proposal ? { action: "resume_proposal" as const, resumeProposal: item.response.resume_proposal,
            proposalState: item.response.resume_proposal.status === "pending" ? "pending" as const : item.response.resume_proposal.status === "rejected" ? "rejected" as const : "approved" as const,
            selectedOperationIds: item.response.resume_proposal.operations.map((operation) => operation.id) } : {}),
        })));
      }).catch(() => undefined).finally(() => { if (!controller.signal.aborted) setConversationLoading(false); });
    return () => controller.abort();
  }, [runtime.sessionId]);

  useEffect(() => {
    runtime.runs.filter((run) => run.browser_id).forEach((run) => {
      runtimeTaskIdsRef.current.add(run.browser_id!);
      ownedTaskIdsRef.current.add(run.browser_id!);
      if (!terminalRun(run.status)) activeTaskIdRef.current = run.browser_id;
    });
  }, [runtime.runs]);

  useEffect(() => {
    runtime.notifications.filter((notification) => notification.sessionId === runtime.sessionId).forEach(({ runId, event }) => {
      const key = `${runId}:${event.seq}`;
      if (runtimeNotificationIdsRef.current.has(key)) return;
      runtimeNotificationIdsRef.current.add(key);
      if (event.kind === "browser_summary") void fetchInsights();
      if (event.kind === "workflow_summary") {
        void fetchInsights();
        if (event.data.plans_changed) void fetchLearningPlans();
      }
      if (event.kind === "delivery") {
        const task = event.data.task as DeliveryTask | undefined;
        if (task?.id && (["starting", "running", "waiting_for_user"].includes(task.status)
          || !deliveryTaskIdRef.current || deliveryTaskIdRef.current === task.id)) {
          deliveryTaskIdRef.current = task.id;
          deliveryStatusRef.current = task.status;
          setDeliveryTask(task);
        }
        void fetchInsights();
      }
      const text = event.kind === "browser_summary" || event.kind === "workflow_summary" ? event.data.message : (event.data.result as { message?: string })?.message;
      if (typeof text !== "string") return;
      const proposal = event.kind === "response" ? (event.data.result as { resume_proposal?: ResumeProposal })?.resume_proposal : undefined;
      setMessages((current) => current.some((message) => message.author === "agent" && message.text === text) ? current : [...current, {
        id: `event:${runId}:${event.seq}`, author: "agent", text, time: now(),
        action: event.kind === "browser_summary" ? "open_board" : proposal ? "resume_proposal" : undefined,
        resumeProposal: proposal, proposalState: proposal ? "pending" : undefined,
        selectedOperationIds: proposal?.operations.map((operation) => operation.id),
      }]);
    });
  }, [runtime.notifications, runtime.sessionId]);

  useEffect(() => {
    if (activeView !== "profile") return;
    const controller = new AbortController();
    void refreshCandidateInventory(controller.signal);
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeView]);

  async function refreshCandidateInventory(signal?: AbortSignal) {
    try {
      const [resumeResponse, materialResponse, stateResponse] = await Promise.all([
        inventoryRequest("/api/agent/resumes", signal),
        inventoryRequest("/api/agent/materials", signal),
        inventoryRequest("/api/agent/candidate-state", signal),
      ]);
      if (signal?.aborted) return;
      const failed = [resumeResponse, materialResponse, stateResponse].find((response) => !response.ok);
      if (failed) throw new Error(await inventoryError(failed));
      const stored: ResumeProfile[] = await resumeResponse.json();
      const storedMaterials: CandidateMaterial[] = await materialResponse.json();
      setResumes(stored);
      setCandidateMaterials(storedMaterials);
      setMaterials((current) => [
        ...current.filter((item) => item.status === "处理中" || item.status === "失败"),
        ...stored.map((resume) => ({ id: resume.id, name: resume.filename, type: "简历", status: "已解析" as const })),
        ...storedMaterials.map((material) => ({ id: material.id, name: material.filename, type: material.kind === "course" ? "课程报告" : material.kind === "code" ? "代码" : "项目材料", status: "已解析" as const })),
      ]);
      if (stateResponse.ok) setCandidateState(await stateResponse.json());
      setUploadFeedback(null);
    } catch (error) {
      if (signal?.aborted) return;
      setUploadFeedback({ kind: "error", text: error instanceof Error ? error.message : "材料列表加载失败" });
    }
  }

  useEffect(() => {
    if (activeView !== "settings" && activeView !== "chat") return;
    void (async () => {
      try {
        const response = await fetch("/api/agent/llm-config", { cache: "no-store" });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail);
        setLlmProvider(payload.provider);
        setLlmModel(payload.model);
        setLlmConfigured(payload.credential_configured);
      } catch (error) {
        setNotice(error instanceof Error ? error.message : "无法读取模型设置");
      }
    })();
  }, [activeView]);

  useEffect(() => {
    if (activeView !== "settings" && activeView !== "board") return;
    void fetch("/api/agent/mock-workflow", { cache: "no-store" })
      .then(async (response) => {
        if (response.ok) setWorkflowSettings(await response.json());
      }).catch(() => {});
  }, [activeView]);

  useEffect(() => {
    if (activeView !== "insights" && activeView !== "overview") return;
    void fetchInsights();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeView]);

  useEffect(() => {
    if (activeView !== "plans") return;
    void fetchLearningPlans();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeView, candidateState?.profile.resume_id]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    let inFlight = false;

    const poll = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      const status = await refreshAutomation();
      inFlight = false;
      if (cancelled) return;
      const deliveryActive = ["starting", "running", "waiting_for_reply"].includes(deliveryStatusRef.current ?? "");
      const delay = document.hidden
        ? 60_000
        : status === "running" || status === "starting" || deliveryActive
          ? 5_000
          : status === "waiting_for_user" || status === "paused" || deliveryStatusRef.current === "waiting_for_user"
            ? 15_000
            : 30_000;
      timer = window.setTimeout(poll, delay);
    };

    const handleVisibility = () => {
      if (timer) window.clearTimeout(timer);
      timer = undefined;
      if (!document.hidden) void poll();
    };

    void poll();
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [browserTask?.status]);

  const counts = useMemo(() => ({
    review: jobs.filter((job) => job.stage === "review").length,
    applying: jobs.filter((job) => job.stage === "applying").length,
    waiting: jobs.filter((job) => job.stage === "waiting").length,
    interview: jobs.filter((job) => job.stage === "interview").length,
    outcome_indeterminate: jobs.filter((job) => job.stage === "outcome_indeterminate").length,
  }), [jobs]);

  const detailJob = jobs.find((job) => job.id === detailJobId) ?? null;
  const activeResume = resumes.find((resume) => resume.id === candidateState?.profile.resume_id) ?? resumes[0] ?? null;

  const statusText = agentStatus === "running" ? "搜索运行中" : agentStatus === "paused" ? "任务已暂停" : agentStatus === "waiting" ? "等待登录确认" : "空闲 · 在对话中发送搜索指令";

  async function refreshAutomation(): Promise<BrowserTask["status"] | null> {
    const sessionAtPoll = runtime.currentSession.current;
    try {
      let taskStatus: BrowserTask["status"] | null = null;
      let completedTask: BrowserTask | null = null;
      const [taskResponse, jobsResponse, statsResponse, deliveryResponse] = await Promise.all([
        fetch(`/api/agent/browser${activeTaskIdRef.current ? `?run_id=${encodeURIComponent(activeTaskIdRef.current)}` : ""}`, { cache: "no-store" }),
        fetch("/api/agent/jobs", { cache: "no-store" }),
        fetch("/api/agent/stats", { cache: "no-store" }),
        fetch(`/api/agent/delivery${deliveryTaskIdRef.current ? `?run_id=${encodeURIComponent(deliveryTaskIdRef.current)}` : ""}`, { cache: "no-store" }),
      ]);
      if (taskResponse.ok) {
        const task: BrowserTask = await taskResponse.json();
        if (task.id && !activeTaskIdRef.current) activeTaskIdRef.current = task.id;
        taskStatus = task.status;
        setBrowserTask((current) => sameSnapshot(current, task) ? current : task);
        if (["completed", "stopped", "failed"].includes(task.status)) {
          const statisticsKey = `${task.id}:${task.status}:${task.stored}:${task.duplicates}`;
          if (statisticsSnapshotRef.current !== statisticsKey) {
            statisticsSnapshotRef.current = statisticsKey;
            void fetchInsights();
          }
        }
        if (task.status === "running" || task.status === "starting") setAgentStatus("running");
        else if (task.status === "paused") setAgentStatus("paused");
        else if (task.status === "waiting_for_user") setAgentStatus("waiting");
        else setAgentStatus("idle");
        if (task.status !== "idle") setNotice(task.takeover_reason ? `${task.detail} · ${task.takeover_reason}` : task.detail);
        const previous = prevTaskStatusRef.current;
        prevTaskStatusRef.current = task.status;
        const isOwnedTask = Boolean(task.id && ownedTaskIdsRef.current.has(task.id));
        if (isOwnedTask && task.id) {
          const taskId = task.id;
          const cardState: Message["actionState"] = task.status === "waiting_for_user" ? "pending"
            : task.status === "completed" ? "completed"
            : task.status === "failed" ? "failed"
            : task.status === "stopped" ? "cancelled"
            : task.status === "starting" ? "checking" : "started";
          setMessages((current) => {
            const hasCard = current.some((message) => message.action === "confirm_login" && message.actionTaskId === taskId);
            const updated = current.map((message) => message.action === "confirm_login" && message.actionTaskId === taskId
              ? { ...message, actionState: cardState }
              : message);
            if (hasCard || task.status !== "waiting_for_user") return updated;
            return [...updated, { id: createMessageId(), author: "agent", text: `需要你在 AutoHunt Chrome 中完成登录、验证码或 MFA 后继续：${task.takeover_reason ?? task.detail}`, time: now(), action: "confirm_login", actionQuery: task.query, actionQueries: task.query ? [task.query] : undefined, actionState: "pending", actionTaskId: taskId }];
          });
        }
        if (isOwnedTask && task.status === "completed" && task.id && !runtimeTaskIdsRef.current.has(task.id) && !announcedTaskIdsRef.current.has(task.id)) {
          announcedTaskIdsRef.current.add(task.id);
          completedTask = task;
          setNotice(`搜索完成：${task.stored} 个职位已自动放入申请看板，重复 ${task.duplicates} 个`);
        }
        if (isOwnedTask && previous && previous !== task.status && task.status === "failed") {
          setMessages((current) => [...current, { id: createMessageId(), author: "agent", text: `搜索失败：${task.detail}`, time: now() }]);
        }
      }
      if (deliveryResponse.ok) {
        const task: DeliveryTask = await deliveryResponse.json();
        if (task.id && !deliveryTaskIdRef.current) deliveryTaskIdRef.current = task.id;
        setDeliveryTask((current) => sameSnapshot(current, task) ? current : task);
        deliveryStatusRef.current = task.status;
        if (task.status !== "idle") {
          setNotice(task.takeover_reason ? `${task.detail} · ${task.takeover_reason}` : task.detail);
        }
      }
      let latestJobs: Job[] = [];
      if (jobsResponse.ok) {
        const stored = await jobsResponse.json();
        latestJobs = stored.filter((job: { stage: string }) => job.stage !== "rejected").map((job: Record<string, unknown>) => ({ ...job, sourceUrl: job.source_url })) as Job[];
        setJobs((current) => sameSnapshot(current, latestJobs) ? current : latestJobs);
      }
      if (statsResponse.ok) {
        const nextStats: AutomationStats = await statsResponse.json();
        setAutomationStats((current) => sameSnapshot(current, nextStats) ? current : nextStats);
      }
      if (completedTask) {
        void fetchInsights();
        let report: SearchReport | null = null;
        try {
          const response = await fetch(`/api/agent/browser-report?run_id=${encodeURIComponent(completedTask.id ?? "")}`, { cache: "no-store" });
          if (response.ok) report = await response.json() as SearchReport;
        } catch {
          // A report failure must not hide the completed search or its funnel.
        }
        if (runtime.currentSession.current === sessionAtPoll) setMessages((current) => [...current, { id: createMessageId(), author: "agent", text: summarizeSearch(completedTask, report), time: now(), action: "open_board" }]);
      }
      return taskStatus;
    } catch {
      setNotice("浏览器执行器后端暂时不可用");
      return null;
    }
  }

  async function fetchInsights() {
    try {
      const response = await fetch("/api/agent/insights", { cache: "no-store" });
      if (!response.ok) return;
      setInsights(await response.json());
    } catch {
      // Statistics stay on the last loaded snapshot while the backend restarts.
    }
  }

  async function fetchLearningPlans() {
    try {
      const [plansResponse, proposalsResponse] = await Promise.all([
        fetch("/api/agent/learning-plans", { cache: "no-store" }),
        fetch("/api/agent/resume-proposals?status=pending", { cache: "no-store" }),
      ]);
      if (!plansResponse.ok) throw new Error("学习计划读取失败，请确认后端已启动");
      setLearningPlans(await plansResponse.json() as LearningPlan[]);
      if (proposalsResponse.ok) {
        const pending = await proposalsResponse.json() as ResumeProposal[];
        setPlanProposals(Object.fromEntries(pending.map((item) => [item.id, item])));
      }
    } catch (error) {
      setPlanNotice(error instanceof Error ? error.message : "学习计划读取失败");
    }
  }

  async function generateLearningPlan() {
    if (!window.confirm("将当前简历正文和本简历搜索命中的最多 20 份 JD 发送给已配置模型，分析岗位差距并生成学习计划。继续吗？")) return;
    setPlanBusy(true);
    setPlanNotice("正在核对 JD 原文并生成逐步计划；模型响应较慢时最长等待约 90 秒…");
    try {
      const payload = await runtime.execute("learning_plan", { focus: planFocus.trim() }, (event) => {
        if (typeof event.data.detail === "string") setPlanNotice(event.data.detail);
      }) as Partial<LearningPlan>;
      setLearningPlans((current) => [payload as LearningPlan, ...current]);
      setPlanNotice(`已生成 ${payload.gaps?.length ?? 0} 项有 JD 原文依据的学习任务。`);
    } catch (error) {
      setPlanNotice(error instanceof Error ? error.message : "生成计划失败");
    } finally { setPlanBusy(false); }
  }

  async function completeLearningGap(planId: string, gapId: string, defaultTarget: "skill" | "project" | "none") {
    const evidence = (completionEvidence[gapId] ?? "").trim();
    const resumeTarget = completionTargets[gapId] ?? defaultTarget;
    if (evidence.length < 30) { setPlanNotice("请先写至少 30 字的真实完成证据；仅勾选不能证明已经掌握。 "); return; }
    if (!window.confirm("标记完成并保存这段实践证据？若补充简历，会先生成可审核的修改提案。")) return;
    setPlanBusy(true);
    try {
      const response = await fetch(`/api/agent/learning-plans/${planId}/gaps/${gapId}/complete`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ evidence, resume_target: resumeTarget, project_title: completionTitles[gapId] ?? "" }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "标记完成失败");
      setLearningPlans((current) => current.map((item) => item.id === planId ? payload.plan as LearningPlan : item));
      if (payload.resume_proposal) {
        const proposal = payload.resume_proposal as ResumeProposal;
        setPlanProposals((current) => ({ ...current, [proposal.id]: proposal }));
      }
      setPlanNotice(payload.resume_proposal ? "已标记完成；下方简历补充提案待你审核。" : "已标记完成，主简历未被修改。");
    } catch (error) {
      setPlanNotice(error instanceof Error ? error.message : "标记完成失败");
    } finally { setPlanBusy(false); }
  }

  async function decideLearningProposal(proposal: ResumeProposal, decision: "approve" | "reject") {
    setPlanBusy(true);
    try {
      const response = await fetch("/api/agent/resume-proposals", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: proposal.id, decision, operation_ids: proposal.operations.map((item) => item.id) }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "处理简历提案失败");
      if (decision === "approve" && payload.candidate_state) setCandidateState(payload.candidate_state);
      setPlanProposals((current) => { const next = { ...current }; delete next[proposal.id]; return next; });
      setPlanNotice(decision === "approve" ? "已审核并写入主简历新版本。" : "已拒绝补充，主简历保持不变。 ");
    } catch (error) { setPlanNotice(error instanceof Error ? error.message : "处理简历提案失败"); }
    finally { setPlanBusy(false); }
  }

  async function stopAgent() {
    if (browserActionBusy) return;
    setBrowserActionBusy(true);
    try {
      const response = await fetch("/api/agent/browser", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "stop" }) });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "停止失败");
      setNotice(payload.detail);
      await refreshAutomation();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "停止浏览器任务失败");
    } finally {
      setBrowserActionBusy(false);
    }
  }

  async function startChatSearch(id: Message["id"], queries: string[], locations: string[] = [], dryRun = false) {
    const safeQueries = (queries.length ? queries : [searchQuery]).slice(0, 6);
    const safeLocations = locations.filter(Boolean).slice(0, 3);
    if (agentStatus === "waiting") {
      // A task is already waiting for login; reuse it instead of starting another.
      setSearchQuery(safeQueries[0]);
      setNotice("浏览器已拉起，请在 AutoHunt Chrome 中完成登录后点击「确认登录」");
      setMessages((current) => current.map((message) => message.id === id
        ? { ...message, action: undefined }
        : message));
      return;
    }
    if (agentStatus !== "idle") {
      setMessages((current) => current.map((message) => message.id === id
        ? { ...message, text: "已有搜索任务正在执行，请先等待完成或点击停止后再发起新搜索。", action: undefined }
        : message));
      return;
    }
    setSearchQuery(safeQueries[0]);
    try {
      const response = await fetch("/api/agent/browser", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "start", query: safeQueries[0], queries: safeQueries, locations: safeLocations, max_jobs: dryRun ? 15 : 30, browser_mode: "cdp", dry_run: dryRun, dry_run_min_score: 75, dry_run_max_contacts: 3, dry_run_simulate_reply: true }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "搜索启动失败");
      if (typeof payload.id === "string") {
        ownedTaskIdsRef.current.add(payload.id);
        activeTaskIdRef.current = payload.id;
        setMessages((current) => current.map((message) => message.id === id
          ? { ...message, actionTaskId: payload.id, actionState: payload.status === "waiting_for_user" ? "pending" : "checking" }
          : message));
      }
      setNotice("浏览器任务已启动，正在检查登录状态并搜索职位");
      await refreshAutomation();
    } catch (error) {
      setMessages((current) => current.map((message) => message.id === id ? { ...message, actionState: "failed" } : message));
      setNotice(error instanceof Error ? error.message : "搜索启动失败");
    }
  }

  async function confirmLogin(id: Message["id"]) {
    const target = messages.find((message) => message.id === id);
    if (!target) return;
    try {
      const check = await fetch(`/api/agent/browser${target.actionTaskId ? `?run_id=${encodeURIComponent(target.actionTaskId)}` : ""}`, { cache: "no-store" });
      if (!check.ok) throw new Error("无法获取浏览器任务状态");
      const task: BrowserTask = await check.json();
      if (task.id !== target.actionTaskId || task.status !== "waiting_for_user") {
        const staleCard = Boolean(target.actionTaskId && target.actionTaskId !== task.id);
        const currentState: Message["actionState"] = staleCard || task.status === "stopped" ? "cancelled"
          : task.status === "completed" ? "completed"
          : task.status === "failed" ? "failed" : "started";
        setMessages((current) => current.map((message) => message.id === id
          ? { ...message, actionState: currentState }
          : message));
        await refreshAutomation();
        setNotice(task.status === "completed" ? "搜索已经完成，请查看搜索摘要和申请看板" : staleCard ? "这是旧任务的登录确认卡片，请查看当前任务状态" : "浏览器任务正在执行，无需重复确认登录");
        return;
      }
      setMessages((current) => current.map((message) => message.id === id ? { ...message, actionState: "checking" } : message));
      const response = await fetch("/api/agent/browser", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "resume" }) });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "继续失败");
      setNotice(payload.detail ?? "已提交登录确认，正在检查页面状态");
      await refreshAutomation();
    } catch (error) {
      setMessages((current) => current.map((message) => message.id === id ? { ...message, actionState: "pending" } : message));
      setNotice(error instanceof Error ? error.message : "继续搜索失败");
    }
  }

  async function cancelChatSearch(id: Message["id"]) {
    try {
      const response = await fetch("/api/agent/browser", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "stop" }) });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "停止失败");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "停止失败");
    }
    setMessages((current) => current.map((message) => message.id === id ? { ...message, actionState: "cancelled" } : message));
    await refreshAutomation();
  }

  async function importMockJob() {
    const response = await fetch("/api/agent/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "import_mock", external_id: 696, flow: "direct" }),
    });
    const payload = await response.json();
    if (!response.ok) { setNotice(payload.detail ?? "导入模拟职位失败"); return; }
    setNotice("已把直接投递模拟职位导入待审核列，可测试授权、MFA 接管、投递后会话与状态同步");
    await refreshAutomation();
  }
  async function advanceJob(id: number) {
    const currentJob = jobs.find((job) => job.id === id);
    if (!currentJob) return;
    if (currentJob.stage === "review") {
      const response = await fetch("/api/agent/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "preview", id }) });
      const payload = await response.json();
      if (!response.ok) { setNotice(payload.detail ?? "生成投递预览失败"); return; }
      setDeliveryPreview(payload);
      setNotice("投递预览已生成；确认前不会执行任何平台操作");
      return;
    }
    const next: Record<JobStage, JobStage> = { review: "review", applying: "applying", waiting: "interview", interview: "interview", outcome_indeterminate: "outcome_indeterminate" };
    if (next[currentJob.stage] === currentJob.stage) return;
    const response = await fetch("/api/agent/jobs", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id, stage: next[currentJob.stage] }) });
    if (!response.ok) { setNotice("职位状态更新失败"); return; }
    setJobs((current) => current.map((job) => {
      if (job.id !== id) return job;
      return { ...job, stage: next[job.stage] };
    }));
    setNotice("看板状态已更新并写入本次 Demo 会话");
  }

  async function authorizeDelivery() {
    if (!deliveryPreview) return;
    const response = await fetch("/api/agent/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "authorize", preview_id: deliveryPreview.id, conversation_id: runtime.sessionId }),
    });
    const payload = await response.json();
    if (!response.ok) { setNotice(payload.detail ?? "授权失败"); return; }
    setDeliveryPreview(null);
    if (payload.delivery_task) {
      const task = payload.delivery_task as DeliveryTask;
      if (task.id) deliveryTaskIdRef.current = task.id;
      setDeliveryTask(task);
    }
    setNotice("一次性授权已生成并由本地投递执行器消费；正在打开职位页面");
    await refreshAutomation();
  }

  async function resumeDelivery() {
    const response = await fetch("/api/agent/delivery", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) { setNotice(payload.detail ?? "继续投递失败"); return; }
    setDeliveryTask(payload as DeliveryTask);
    setNotice(payload.detail ?? "正在继续投递");
    await refreshAutomation();
  }

  async function rejectJob(id: number) {
    const response = await fetch("/api/agent/jobs", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id, stage: "rejected" }) });
    if (!response.ok) { setNotice("跳过职位失败"); return; }
    setJobs((current) => current.filter((job) => job.id !== id));
    setNotice("职位已跳过，后续推荐将降低相似岗位权重");
  }

  function editResumeText() {
    setResumeViewMode("text");
    setResumeEditing(true);
    window.setTimeout(() => document.getElementById("resume-print-area")?.scrollIntoView({ behavior: "smooth", block: "start" }), 0);
  }

  async function activateUploadedResume(resume: ResumeProfile) {
    setResumeBusy(true);
    try {
      const response = await fetch("/api/agent/resumes", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: resume.id }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "设为主简历失败");
      setCandidateState(payload.candidate_state);
      setResumeSourceMode("verbatim");
      setResumeViewMode("text");
      setResumeEditing(false);
      if (payload.candidate_state) setSearchQuery(preferredSearchQuery(payload.candidate_state as CandidateState));
      setNotice(`已将 ${resume.filename} 设为主简历并自动排版；保留正文内容，可在页面中编辑`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "设为主简历失败");
    } finally {
      setResumeBusy(false);
    }
  }

  async function deleteUploadedResume(resume: ResumeProfile) {
    if (!window.confirm(`删除简历“${resume.filename}”？原始文件与提取文本会被删除；已单独确认的项目经历会保留。`)) return;
    setResumeBusy(true);
    try {
      const response = await fetch("/api/agent/resumes", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: resume.id }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "删除简历失败");
      setResumes((current) => current.filter((item) => item.id !== resume.id));
      setMaterials((current) => current.filter((item) => item.id !== resume.id));
      if (payload.candidate_state) setCandidateState(payload.candidate_state);
      if (payload.resume_document?.source_mode) setResumeSourceMode(payload.resume_document.source_mode);
      setResumeViewMode("text");
      setResumeEditing(false);
      setNotice(`已删除 ${resume.filename}；已单独确认的项目材料和项目经历未删除`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "删除简历失败");
    } finally {
      setResumeBusy(false);
    }
  }

  async function handleUpload(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    if (!files.length) return;
    event.target.value = "";
    setResumeBusy(true);
    const pendingPrefix = `resume-pending-${Date.now()}`;
    setUploadFeedback({ kind: "progress", text: `正在上传并解析 ${files.length} 份简历，请稍候…` });
    setMaterials((current) => [
      ...files.map((file, index) => ({ id: `${pendingPrefix}-${index}`, name: file.name, type: "简历", status: "处理中" as const })),
      ...current,
    ]);
    try {
      const uploaded: ResumeProfile[] = [];
      for (const file of files) {
        const form = new FormData();
        form.set("file", file);
        const response = await fetch("/api/agent/resumes", { method: "POST", body: form });
        const payload = await response.json();
        if (!response.ok) throw new Error(response.status === 503 ? "Agent 后端不可用，请启动后端后重新选择文件" : String(payload.detail ?? `${file.name} 上传失败`));
        const result = payload as ResumeIngestResult;
        uploaded.push(result.resume);
        setCandidateState(result.candidate_state);
        setSearchQuery(preferredSearchQuery(result.candidate_state));
        setResumeSourceMode("verbatim");
        setResumeViewMode("text");
        setResumeEditing(false);
      }
      const newestFirst = [...uploaded].reverse();
      setResumes((current) => [...newestFirst, ...current.filter((item) => !uploaded.some((next) => next.id === item.id))]);
      setMaterials((current) => [
        ...newestFirst.map((resume) => ({ id: resume.id, name: resume.filename, type: "已上传简历", status: "已解析" as const })),
        ...current.filter((item) => !String(item.id).startsWith(pendingPrefix)),
      ]);
      const latest = newestFirst[0];
      setNotice(`简历内容已导入并自动排版；通用找工作指令将优先参考当前主简历的岗位方向，具体搜索词仍可修改`);
      setUploadFeedback({ kind: "success", text: `已上传 ${uploaded.length} 份简历并完成解析` });
    } catch (error) {
      const detail = error instanceof Error ? error.message : "简历上传失败";
      setMaterials((current) => current.map((item) => String(item.id).startsWith(pendingPrefix) ? { ...item, status: "失败" } : item));
      setUploadFeedback({ kind: "error", text: `简历上传失败：${detail}。请重新选择文件。` });
      setNotice(detail);
    } finally {
      setResumeBusy(false);
    }
  }

  async function handleChatAttachment(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    if (!files.length) return;
    event.target.value = "";
    const prompt = chatText.trim();
    setChatText("");
    setResumeBusy(true);
    const userMessageId = createMessageId();
    const attachmentNames = files.map((file) => file.name).join("、");
    setMessages((current) => [...current, {
      id: userMessageId,
      author: "user",
      text: (prompt ? prompt + "\n" : "") + "📎 已上传：" + attachmentNames,
      time: now(),
    }]);
    setNotice("正在解析 " + files.length + " 个对话附件…");

    try {
      const replies: Message[] = [];
      for (const file of files) {
        const form = new FormData();
        form.set("file", file);
        form.set("user_hint", prompt);
        const response = await fetch("/api/agent/documents", { method: "POST", body: form });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail ?? file.name + " 解析失败");
        const result = payload as DocumentIngestResult;
        setCandidateState(result.candidate_state);

        if (result.document_type === "resume" && result.resume) {
          setResumes((current) => [
            result.resume!,
            ...current.filter((item) => item.id !== result.resume!.id),
          ]);
          setMaterials((current) => [
            { id: result.resume!.id, name: result.resume!.filename, type: "完整简历", status: "已解析" as const },
            ...current.filter((item) => item.id !== result.resume!.id),
          ]);
          const proposal = result.resume_proposal ?? undefined;
          replies.push({
            id: createMessageId(),
            author: "agent",
            text: "已识别为完整简历（置信度 " + Math.round(result.classification.confidence * 100) + "%）。是否用它更新当前主简历？接受前不会替换。",
            time: now(),
            action: proposal ? "resume_proposal" : undefined,
            resumeProposal: proposal,
            selectedOperationIds: proposal?.operations.map((operation) => operation.id),
            proposalState: proposal ? "pending" : undefined,
          });
        } else if (result.material) {
          const material = result.material;
          const kind = material.kind;
          setCandidateMaterials((current) => [
            material,
            ...current.filter((item) => item.id !== material.id),
          ]);
          setMaterials((current) => [
            {
              id: material.id,
              name: material.filename,
              type: kind === "course" ? "课程报告" : kind === "code" ? "代码" : "项目材料",
              status: "已解析" as const,
            },
            ...current.filter((item) => item.id !== material.id),
          ]);
          if (result.resume_proposal) {
            replies.push({
              id: createMessageId(),
              author: "agent",
              text: "已自动识别为" + (kind === "course" ? "课程报告" : kind === "code" ? "代码项目" : "项目文档") + "。是否把提取出的项目经历和技术栈加入主简历？请接受或拒绝下方修改提案。",
              time: now(),
              action: "resume_proposal",
              resumeProposal: result.resume_proposal,
              selectedOperationIds: result.resume_proposal.operations.map((operation) => operation.id),
              proposalState: "pending",
            });
          }
        }
      }

      if (prompt) {
        const payload = await runtime.execute("chat", { message: prompt }) as { message: string; action?: string; resume_proposal?: ResumeProposal };
        const proposal = payload.action === "resume_proposal" ? payload.resume_proposal as ResumeProposal : undefined;
        replies.push({
          id: createMessageId(),
          author: "agent",
          text: payload.message,
          time: now(),
          action: proposal ? "resume_proposal" : undefined,
          resumeProposal: proposal,
          selectedOperationIds: proposal?.operations.map((operation) => operation.id),
          proposalState: proposal ? "pending" : undefined,
        });
      }

      setMessages((current) => [...current, ...replies]);
      setNotice("已解析 " + files.length + " 个对话附件" + (prompt ? "并完成问题分析" : ""));
    } catch (error) {
      const detail = error instanceof Error ? error.message : "附件上传失败";
      setMessages((current) => [...current, {
        id: createMessageId(),
        author: "agent",
        text: "附件处理失败：" + detail,
        time: now(),
      }]);
      setNotice(detail);
    } finally {
      setResumeBusy(false);
    }
  }

  async function handleMaterialUpload(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    if (!files.length) return;
    event.target.value = "";
    setResumeBusy(true);
    setUploadFeedback({ kind: "progress", text: `正在上传并解析 ${files.length} 份材料，请稍候…` });
    const pendingPrefix = `material-pending-${Date.now()}`;
    setMaterials((current) => [
      ...files.map((file, index) => ({ id: `${pendingPrefix}-${index}`, name: file.name, type: materialKind === "course" ? "课程报告" : materialKind === "code" ? "代码" : "项目材料", status: "处理中" as const })),
      ...current,
    ]);
    try {
      const uploaded: CandidateMaterial[] = [];
      const proposals: ResumeProposal[] = [];
      for (const file of files) {
        const form = new FormData();
        form.set("file", file);
        form.set("kind", materialKind);
        const response = await fetch("/api/agent/materials", { method: "POST", body: form });
        const payload = await response.json();
        if (!response.ok) throw new Error(response.status === 503 ? "Agent 后端不可用，请启动后端后重新选择文件" : String(payload.detail ?? `${file.name} 解析失败`));
        const result = payload as MaterialIngestResult;
        uploaded.push(result.material);
        if (result.resume_proposal) proposals.push(result.resume_proposal);
        setCandidateState(result.candidate_state);
      }
      const newestFirst = [...uploaded].reverse();
      setCandidateMaterials((current) => [...newestFirst, ...current]);
      setMaterials((current) => [
        ...newestFirst.map((material) => ({ id: material.id, name: material.filename, type: material.kind === "course" ? "课程报告" : material.kind === "code" ? "代码" : "项目材料", status: "已解析" as const })),
        ...current.filter((item) => !String(item.id).startsWith(pendingPrefix)),
      ]);
      if (proposals.length) {
        setMessages((current) => [
          ...current,
          ...proposals.map((proposal) => ({
            id: createMessageId(),
            author: "agent" as const,
            text: `已解析上传材料，并调用 resume_propose_patch 生成 ${proposal.operations.length} 项主简历修改。请审核后决定是否写入。`,
            time: now(),
            action: "resume_proposal" as const,
            resumeProposal: proposal,
            selectedOperationIds: proposal.operations.map((operation) => operation.id),
            proposalState: "pending" as const,
          })),
        ]);
        setActiveView("chat");
      }
      setNotice(proposals.length
        ? `已解析 ${uploaded.length} 份材料并生成简历修改提案；请在对话中接受后写入项目经历，技术栈画像已更新`
        : `已解析 ${uploaded.length} 份材料；未生成新的简历修改项`);
      setUploadFeedback({ kind: "success", text: `已上传 ${uploaded.length} 份材料${proposals.length ? "；修改提案已放入 Agent 对话" : ""}` });
    } catch (error) {
      const detail = error instanceof Error ? error.message : "候选人材料上传失败";
      setMaterials((current) => current.map((item) => String(item.id).startsWith(pendingPrefix) ? { ...item, status: "失败" } : item));
      setUploadFeedback({ kind: "error", text: `材料上传失败：${detail}。请重新选择文件。` });
      setNotice(detail);
    } finally {
      setResumeBusy(false);
    }
  }

  async function saveResumeDraft() {
    const content = resumeDraft;
    if (!content.trim()) {
      setNotice("简历草稿不能为空");
      return;
    }
    setResumeSaveBusy(true);
    try {
      const response = await fetch("/api/agent/candidate-state", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "保存失败");
      setCandidateState(payload);
      setResumeDraft(payload.resume_draft);
      setResumeSavedDraft(payload.resume_draft);
      setResumeEditing(false);
      setNotice("简历草稿已加密保存");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "简历草稿保存失败");
    } finally {
      setResumeSaveBusy(false);
    }
  }

  async function organizeResume() {
    setLayoutBusy(true);
    try {
      const [configResponse, documentResponse] = await Promise.all([fetch("/api/agent/llm-config", { cache: "no-store" }), fetch("/api/agent/documents", { cache: "no-store" })]);
      if (!configResponse.ok || !documentResponse.ok) throw new Error("无法读取模型或简历，请先启动后端");
      const config = await configResponse.json();
      const document: ResumeDocumentMeta = await documentResponse.json();
      if (config.provider !== "deepseek" || !config.credential_configured) throw new Error("请先在模型设置中配置 DeepSeek，当前未发送简历");
      if (!window.confirm(`是否将当前简历正文发送给 DeepSeek（${config.model}）整理结构？\n电话、邮箱及密钥模式将脱敏，其他正文仍可能包含个人或工作信息。\n只整理归属，不改写正文。最多一次纠错重试；校验失败保留原文。`)) return;
      const response = await fetch("/api/agent/documents", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ consent: true, provider: config.provider, model: config.model, version: document.version }) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail ?? "结构整理失败，原文保留");
      setResumeDocument(result);
      setResumeViewMode("text");
      setNotice(result.layout?.status === "validated" ? "结构整理完成，原文引用与完整性校验通过；请核对经历归属" : "原文已保留；请查看结构校验状态");
    } catch (error) { setNotice(error instanceof Error ? error.message : "结构整理失败，原文保留"); }
    finally { setLayoutBusy(false); }
  }

  function exportResumePdf() {
    if (!resumeDraft.trim()) {
      setNotice("暂无可导出的简历草稿");
      return;
    }
    setResumeEditing(false);
    setResumeViewMode("text");
    window.setTimeout(() => {
      const previousTitle = document.title;
      const cleanup = () => {
        document.body.classList.remove("printing-resume");
        document.title = previousTitle;
      };
      document.body.classList.add("printing-resume");
      document.title = "候选人简历草稿";
      window.addEventListener("afterprint", cleanup, { once: true });
      window.print();
      window.setTimeout(cleanup, 1_000);
    }, 0);
  }


  function toggleProposalOperation(messageId: Message["id"], operationId: string) {
    setMessages((current) => current.map((message) => {
      if (message.id !== messageId || !message.resumeProposal) return message;
      const selected = new Set(message.selectedOperationIds ?? message.resumeProposal.operations.map((item) => item.id));
      if (selected.has(operationId)) selected.delete(operationId); else selected.add(operationId);
      return { ...message, selectedOperationIds: [...selected] };
    }));
  }

  async function decideResumeProposal(messageId: Message["id"], decision: "approve" | "reject") {
    const target = messages.find((message) => message.id === messageId);
    if (!target?.resumeProposal) return;
    const operationIds = target.selectedOperationIds ?? target.resumeProposal.operations.map((item) => item.id);
    if (decision === "approve" && operationIds.length === 0) {
      setNotice("请至少选择一项简历修改");
      return;
    }
    setMessages((current) => current.map((message) => message.id === messageId ? { ...message, proposalState: "approving" } : message));
    try {
      const response = await fetch("/api/agent/resume-proposals", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: target.resumeProposal.id, decision, operation_ids: operationIds }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "处理简历提案失败");
      if (decision === "approve") {
        if (payload.candidate_state) setCandidateState(payload.candidate_state);
        if (payload.document?.source_mode) setResumeSourceMode(payload.document.source_mode);
        setResumeViewMode("text");
        setNotice(`已接受 ${operationIds.length} 项修改，主简历已生成新版本`);
      } else {
        setNotice("已拒绝本次简历修改，主简历未改变");
      }
      setMessages((current) => current.map((message) => message.id === messageId ? {
        ...message,
        proposalState: decision === "approve" ? "approved" : "rejected",
      } : message));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "处理简历提案失败");
      setMessages((current) => current.map((message) => message.id === messageId ? { ...message, proposalState: "failed" } : message));
    }
  }


  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    const value = chatText.trim();
    if (!value) return;
    await deliver(value);
  }

  async function deliver(value: string) {
    if (runtime.busyRun || conversationLoading) return;
    const sessionAtStart = runtime.sessionId;
    const turn = createChatTurn(value, now());
    const messageId = turn[1].id;
    setMessages((current) => [...current, ...turn]);
    setChatText("");
    let responseText = "后端 Agent 暂时不可用，请先启动 FastAPI 服务并配置服务端 Token。";
    let action: Message["action"] = undefined;
    let actionQuery: string | undefined;
    let actionQueries: string[] | undefined;
    let actionLocations: string[] | undefined;
    let actionDryRun = false;
    let clarifyOptions: ClarifyOption[] | undefined;
    let resumeProposal: ResumeProposal | undefined;
    try {
      let streamed = "";
      const payload = await runtime.execute("chat", { message: value }, (event) => {
        if (runtime.currentSession.current !== sessionAtStart) return;
        if (event.kind === "delta") {
          streamed += String(event.data.text ?? "");
          setMessages((current) => current.map((item) => item.id === messageId ? { ...item, text: streamed } : item));
        }
        if (event.kind === "browser") {
          const task = event.data.task as BrowserTask;
          if (task.id) {
            ownedTaskIdsRef.current.add(task.id);
            runtimeTaskIdsRef.current.add(task.id);
            activeTaskIdRef.current = task.id;
            setMessages((current) => current.map((item) => item.id === messageId ? { ...item,
              action: "confirm_login", actionTaskId: task.id!, actionQuery: task.query, actionQueries: [task.query],
              actionState: task.status === "waiting_for_user" ? "pending" : task.status === "completed" ? "completed" : "started",
            } : item));
            void refreshAutomation();
          }
        }
      }) as { message?: string; lane?: string; action?: string; action_query?: string; action_queries?: string[];
        action_locations?: string[]; action_dry_run?: boolean; clarify_options?: ClarifyOption[]; resume_proposal?: ResumeProposal };
      if (runtime.currentSession.current !== sessionAtStart) return;
      if (typeof payload.message === "string") {
        responseText = payload.message;
        setNotice(`后端已响应 · ${payload.lane ?? "orchestrator"}`);
        if (payload.action === "confirm_search" && typeof payload.action_query === "string") {
          action = "confirm_login";
          actionQuery = payload.action_query;
          actionQueries = Array.isArray(payload.action_queries) && payload.action_queries.length
            ? payload.action_queries
            : [payload.action_query];
          actionLocations = Array.isArray(payload.action_locations)
            ? payload.action_locations
            : undefined;
          actionDryRun = payload.action_dry_run === true;
        }
        if (payload.action === "clarify" && Array.isArray(payload.clarify_options) && payload.clarify_options.length) {
          action = "clarify";
          clarifyOptions = payload.clarify_options
            .map((option: { intent?: unknown; label?: unknown }) => ({ intent: String(option.intent ?? ""), label: String(option.label ?? "") }))
            .filter((option: ClarifyOption) => option.intent && option.label);
        }
        if (payload.action === "resume_proposal" && payload.resume_proposal?.id) {
          action = "resume_proposal";
          resumeProposal = payload.resume_proposal as ResumeProposal;
        }
        if (payload.lane === "candidate") {
          const stateResponse = await fetch("/api/agent/candidate-state", { cache: "no-store" });
          if (stateResponse.ok) setCandidateState(await stateResponse.json());
        }
        if (payload.lane === "growth" && /(?:生成|制定|创建|规划|做一份)(?:.{0,12})(?:学习计划|行动方案)/.test(value)) {
          await fetchLearningPlans();
          setActiveView("plans");
        }
      }
    } catch (error) {
      responseText = error instanceof Error ? error.message : "连接已中断，请查看运行记录";
      setNotice(responseText);
    }
    if (runtime.currentSession.current !== sessionAtStart) return;
    setMessages((current) => current.map((item) => item.id !== messageId ? item : { ...item,
      id: messageId,
      author: "agent",
      text: responseText,
      time: now(),
      action: action ?? item.action,
      actionQuery: actionQuery ?? item.actionQuery,
      actionQueries: actionQueries ?? item.actionQueries,
      actionLocations,
      actionDryRun,
      actionState: action === "confirm_login" ? "checking" : item.actionState,
      resumeProposal,
      clarifyOptions,
      selectedOperationIds: resumeProposal?.operations.map((operation) => operation.id),
      proposalState: resumeProposal ? "pending" : undefined,
    }));
    if (action === "confirm_login" && actionQueries?.length) {
      // The instruction is a search command: start the browser task right away
      // and let the 确认登录 card drive the login handoff.
      void startChatSearch(messageId, actionQueries, actionLocations, actionDryRun);
    }
  }

  async function saveWorkflowSettings(event: FormEvent) {
    event.preventDefault();
    if (!workflowSettings) return;
    setWorkflowBusy(true);
    try {
      const response = await fetch("/api/agent/mock-workflow", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          monitor_enabled: workflowSettings.monitor_enabled,
          auto_submit: workflowSettings.auto_submit,
          daily_enabled: workflowSettings.daily_enabled,
          daily_time: workflowSettings.daily_time,
          min_score: workflowSettings.min_score,
          max_contacts: workflowSettings.max_contacts,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "保存模拟站任务设置失败");
      setWorkflowSettings(payload);
      setWorkflowNotice("已保存；后台将按设置监测模拟站会话与每日任务。");
    } catch (error) {
      setWorkflowNotice(error instanceof Error ? error.message : "保存失败");
    } finally {
      setWorkflowBusy(false);
    }
  }

  async function syncMockWorkflow() {
    setWorkflowBusy(true);
    try {
      const response = await fetch("/api/agent/mock-workflow", { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "会话同步失败");
      setWorkflowNotice(`已同步 ${payload.conversations} 个会话；等待回复 ${payload.awaiting_reply}，已投递 ${payload.submitted}，检测到 HR 回复 ${payload.replied}。`);
      await refreshAutomation();
    } catch (error) {
      setWorkflowNotice(error instanceof Error ? error.message : "会话同步失败");
    } finally {
      setWorkflowBusy(false);
    }
  }

  async function saveLlmConfig(event: FormEvent) {
    event.preventDefault();
    setLlmBusy(true);
    try {
      const response = await fetch("/api/agent/llm-config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: llmProvider, model: llmModel, api_key: llmProvider === "deepseek" && llmApiKey ? llmApiKey : null }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "保存失败");
      setLlmConfigured(payload.credential_configured);
      setLlmApiKey("");
      setNotice(`已启用 ${payload.provider} / ${payload.model}`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "模型设置保存失败");
    } finally {
      setLlmBusy(false);
    }
  }

  async function testLlmConnection() {
    setLlmBusy(true);
    try {
      const response = await fetch("/api/agent/llm-test", { method: "POST" });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail ?? "连接测试失败");
      setNotice(`${payload.provider} / ${payload.model}：${payload.message}`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "连接测试失败");
    } finally {
      setLlmBusy(false);
    }
  }

  return (
    <main className={`app-shell${activeView === "chat" ? " chat-layout" : ""}`}>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">A</span>
          <div><strong>AutoHunt</strong><small>求职智能体</small></div>
        </div>
        <nav>
          {navItems.map(([id, icon, label]) => (
            <button key={id} className={activeView === id ? "nav-item active" : "nav-item"} onClick={() => setActiveView(id)}>
              <span aria-hidden="true">{icon}</span>{label}
            </button>
          ))}
        </nav>
        <div className="sidebar-card">
          <span className="eyebrow">待审核职位</span>
          <strong>{counts.review}</strong>
          <small>高质量申请优先于数量</small>
        </div>
        <div className="profile-mini">
          <span className="avatar">A</span>
          <div><strong>本地候选人</strong><small>{candidateState?.preferences.locations.values.join(" / ") || "尚未设置城市"}</small></div>
          <button aria-label="设置" onClick={() => setActiveView("settings")}>•••</button>
        </div>
      </aside>

      <section className={`workspace${activeView === "chat" ? " chat-workspace" : ""}`}>
        <header className="topbar">
          <div>
            <span className="mobile-brand">AutoHunt</span>
            <h1>{activeView === "overview" ? "你的求职工作台" : navItems.find((item) => item[0] === activeView)?.[2]}</h1>
            <p>{activeView === "chat" ? "规划、搜索与申请，围绕你的目标展开。" : activeView === "overview" ? "你的求职 Agent 已准备好继续工作。" : "Demo 数据会在当前页面会话中即时更新。"}</p>
          </div>
          <div className="agent-control">
            <span className={`pulse ${agentStatus}`} />
            <div><small>Agent 状态</small><strong>{statusText}</strong></div>
            {activeView === "chat" && <span className="chat-mobile-status">{agentStatus === "idle" ? "空闲" : agentStatus === "running" ? "搜索中" : agentStatus === "waiting" ? "等待接管" : "已暂停"}</span>}
            {agentStatus !== "idle" && <button className="stop-agent" onClick={stopAgent} disabled={browserActionBusy}>停止</button>}
          </div>
        </header>

        {activeView !== "chat" && <div className="notice"><span>i</span>{notice}</div>}

        {activeView !== "chat" && browserTask && browserTask.status !== "idle" && <div className="browser-evidence panel">
          <div><small>浏览器</small><strong>{browserTask.browser_mode === "cdp" ? "现有 Chrome（CDP）" : browserTask.browser_mode === "managed" ? "Agent 专用浏览器" : "正在选择"}</strong></div>
          <div><small>搜索 Run</small><strong>{browserTask.id?.slice(0, 8) ?? "—"}</strong></div>
          <div><small>恢复点</small><strong>{browserTask.checkpoint ?? "尚未记录"}</strong></div>
          <div><small>登录证据</small><strong>{browserTask.login_evidence?.verification_visible ? "等待真人验证" : browserTask.login_evidence?.login_control_visible ? "等待登录/MFA" : browserTask.login_evidence?.authenticated_signal ? "登录已确认" : browserTask.login_evidence?.loading_visible ? "页面加载中" : "检查中"}</strong></div>
          <div><small>Adapter</small><strong>{browserTask.adapter_version ?? browserTask.selector_version ?? "—"}</strong></div>
        </div>}

        {activeView !== "chat" && deliveryTask && deliveryTask.status !== "idle" && <div className="browser-evidence panel">
          <div><small>投递任务</small><strong>{deliveryTask.status}</strong></div>
          <div><small>投递 Run</small><strong>{deliveryTask.id?.replace("delivery_", "").slice(0, 8) ?? "—"}</strong></div>
          <div><small>职位 ID</small><strong>{deliveryTask.job_id ?? "—"}</strong></div>
          <div><small>执行结果</small><strong>{deliveryTask.submitted ? "已确认提交" : deliveryTask.detail}</strong></div>
          <div><small>恢复点</small><strong>{deliveryTask.checkpoint ?? "尚未记录"}</strong></div>
          <div><small>招聘方回复</small><strong>{deliveryTask.recruiter_reply ?? "尚未收到"}</strong></div>
          <div>
            <small>人工接管</small>
            {(deliveryTask.status === "waiting_for_user" || deliveryTask.status === "waiting_for_reply")
              ? <button className="button secondary" onClick={resumeDelivery}>{deliveryTask.status === "waiting_for_user" ? "验证完成，继续" : "检查回复并继续"}</button>
              : <strong>{deliveryTask.takeover_reason ?? "无需操作"}</strong>}
          </div>
        </div>}
        {(activeView === "overview" || activeView === "chat" || activeView === "board") && (
          <>
            {activeView === "overview" && <section className="stats-grid">
              <article><span className="stat-icon mint">⌕</span><div><small>本次发现职位</small><strong>{automationStats.discovered}</strong><em>真实浏览器事件</em></div></article>
              <article><span className="stat-icon peach">◎</span><div><small>去重后保存</small><strong>{automationStats.stored}</strong><em>{automationStats.duplicates} 个重复</em></div></article>
              <article><span className="stat-icon blue">↗</span><div><small>建议审核</small><strong>{automationStats.matched}</strong><em>{automationStats.review} 个在看板</em></div></article>
              <article><span className="stat-icon violet">✦</span><div><small>面试进展</small><strong>{automationStats.interview}</strong><em>{automationStats.waiting} 个等待回复</em></div></article>
            </section>}

            {(activeView === "overview" || activeView === "chat") && <section className={activeView === "chat" ? "main-grid chat-only" : "main-grid insight-only"}>
              {activeView === "chat" && <div className={`chat-panel chat-page${chatDetailsOpen ? " details-open" : ""}`}>
                <div className="chat-page-header">
                  <div className="chat-agent-identity"><span className="bot-avatar">A</span><div><strong>AutoHunt Agent</strong><small>求职规划与申请助理</small></div></div>
                  <div className="chat-session-controls">
                    <select aria-label="当前会话" value={runtime.sessionId} onChange={(event) => runtime.setSessionId(event.target.value)}>
                      {runtime.sessions.length ? runtime.sessions.map((session) => <option key={session.id} value={session.id}>{session.title}</option>) : <option value="default">默认会话</option>}
                    </select>
                    <button className="button secondary compact" onClick={() => void runtime.newSession().catch((error) => setNotice(error.message))}>新会话</button>
                    <span className="model-badge">{llmProvider === "deepseek" ? llmModel.replace("deepseek-", "DeepSeek ") : "Mock"}</span>
                    <button className="button secondary compact chat-details-toggle" aria-expanded={chatDetailsOpen} aria-controls="chat-runtime-panel" onClick={() => setChatDetailsOpen((open) => !open)}>{chatDetailsOpen ? "收起详情" : "任务与上下文"}</button>
                  </div>
                </div>
                <div className="chat-body">
                <div className="chat-main">
                <div className="messages chat-thread" role="log" aria-label="对话消息">
                  {messages.length === 0 && <div className="chat-empty"><span className="bot-avatar">A</span><div><small>发送「搜索 + 关键词」开始职位搜索；例如：搜索深圳的大模型算法岗位</small></div></div>}
                  {messages.map((message) => (
                    <div className={`message ${message.author}`} key={message.id}>
                      {message.author === "agent" && <span className="bot-avatar">A</span>}
                      <div>
                        {message.author === "agent" ? <div className="md-body">{renderRichText(message.text)}</div> : <p>{message.text}</p>}
                        <time>{message.time}</time>
                        {message.action === "confirm_login" && (
                          <div className="chat-confirm-card">
                            <div className="confirm-query">⌕ 搜索关键词：<strong>{(message.actionQueries?.length ? message.actionQueries : [message.actionQuery]).filter(Boolean).join(" · ")}</strong></div>
                            {message.actionDryRun && <div className="confirm-query">◎ 执行模式：<strong>模拟站 Dry Run · 高匹配岗位自动沟通 · 有明确回复才发简历</strong></div>}
                            {!!message.actionLocations?.length && <div className="confirm-query">⌖ 网页城市筛选：<strong>{message.actionLocations.join(" · ")}</strong></div>}
                            {message.actionState === "checking" && <p className="confirm-result">正在检查浏览器登录状态；如需人工验证，这里会出现“确认登录”按钮。</p>}
                            {message.actionState === "pending" && (
                              <>
                                <p className="confirm-result">浏览器正在等待人工接管；请在其窗口中完成登录、验证码或 MFA。</p>
                                <div className="confirm-actions">
                                  <button className="button primary compact" onClick={() => confirmLogin(message.id)}>确认登录</button>
                                  <button className="button secondary compact" onClick={() => cancelChatSearch(message.id)}>停止搜索</button>
                                </div>
                              </>
                            )}
                            {message.actionState === "started" && <p className="confirm-result ok">✓ 浏览器任务正在执行搜索，结果将自动进入申请看板</p>}
                            {message.actionState === "completed" && <p className="confirm-result ok">✓ 搜索完成，请查看下方摘要或申请看板</p>}
                            {message.actionState === "failed" && (
                              <div className="confirm-actions">
                                <p className="confirm-result error">启动失败，请检查浏览器状态后重试</p>
                                <button className="button secondary compact" onClick={() => startChatSearch(message.id, message.actionQueries?.length ? message.actionQueries : [message.actionQuery ?? ""], message.actionLocations, message.actionDryRun)}>重试</button>
                              </div>
                            )}
                            {message.actionState === "cancelled" && <p className="confirm-result">已取消本次搜索</p>}
                          </div>
                        )}
                        {message.action === "resume_proposal" && message.resumeProposal && (
                          <div className="resume-proposal-card">
                            <div className="proposal-heading">
                              <div><span className="eyebrow">RESUME PATCH</span><strong>主简历 v{message.resumeProposal.document_version} 修改提案</strong></div>
                              <span>{message.resumeProposal.operations.length} 项</span>
                            </div>
                            <p className="proposal-reason">{message.resumeProposal.reason}</p>
                            <div className="proposal-operations">
                              {message.resumeProposal.operations.map((operation) => {
                                const selected = (message.selectedOperationIds ?? []).includes(operation.id);
                                const locked = message.proposalState === "approved" || message.proposalState === "rejected" || message.proposalState === "approving";
                                return (
                                  <label className={selected ? "proposal-operation selected" : "proposal-operation"} key={operation.id}>
                                    <input type="checkbox" checked={selected} disabled={locked} onChange={() => toggleProposalOperation(message.id, operation.id)} />
                                    <div>
                                      <strong>{operation.label}</strong>
                                      <div className="proposal-diff">
                                        {operation.operation !== "add" && <p className="diff-old"><span>−</span>{formatProposalValue(operation.old_value)}</p>}
                                        {operation.operation !== "remove" && <p className="diff-new"><span>＋</span>{formatProposalValue(operation.new_value)}</p>}
                                      </div>
                                      {operation.evidence_ids.length > 0 && <small>依据：{operation.evidence_ids.length} 条候选人证据</small>}
                                    </div>
                                  </label>
                                );
                              })}
                            </div>
                            {(message.proposalState === "pending" || message.proposalState === "failed") && (
                              <div className="proposal-actions">
                                <button className="button secondary compact" onClick={() => void decideResumeProposal(message.id, "reject")}>拒绝全部</button>
                                <button className="button primary compact" onClick={() => void decideResumeProposal(message.id, "approve")}>接受已选修改</button>
                              </div>
                            )}
                            {message.proposalState === "approving" && <p className="confirm-result">正在写入主简历新版本…</p>}
                            {message.proposalState === "approved" && <p className="confirm-result ok">✓ 已写入主简历，未选择的修改不会应用</p>}
                            {message.proposalState === "rejected" && <p className="confirm-result">已拒绝，主简历未改变</p>}
                            {message.proposalState === "failed" && <p className="confirm-result error">处理失败，可重新选择后重试</p>}
                          </div>
                        )}
                        {message.action === "open_board" && (
                          <div className="chat-confirm-card">
                            <div className="confirm-actions">
                              <button className="button primary compact" onClick={() => setActiveView("board")}>打开申请看板</button>
                            </div>
                          </div>
                        )}
                        {message.action === "clarify" && !!message.clarifyOptions?.length && (
                          <div className="chat-confirm-card">
                            <p className="confirm-result">请选择你的意图：</p>
                            <div className="confirm-actions">
                              {message.clarifyOptions.map((option) => (
                                <button key={option.intent} className="button primary compact" onClick={() => void deliver(`【澄清】选择：${option.label}`)}>{option.label}</button>
                              ))}
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
                <div className="chat-input-dock">
                  <div className="quick-actions">
                    <button onClick={() => setChatText("搜索：")}>开始职位搜索</button>
                    <button onClick={() => setChatText("生成一份针对 Frontend Engineer 的简历")}>生成针对性简历</button>
                    <button onClick={() => setChatText("我在实习中完成了一个项目：请把下面的经历加入简历，并提取技能与成果：")}>描述经历并更新简历</button>
                    <button onClick={() => setChatText("查看当前申请进度")}>查看申请进度</button>
                    <button onClick={() => setChatText("根据最近的 JD 生成学习计划")}>生成学习计划</button>
                  </div>
                  <form className="composer" onSubmit={sendMessage}>
                    <button type="button" onClick={() => chatUploadRef.current?.click()} aria-label="上传简历或候选人材料">＋</button>
                    <input value={chatText} onChange={(event) => setChatText(event.target.value)} placeholder="给 AutoHunt Agent 发消息…" />
                    <button type="submit" className="send" aria-label="发送" disabled={Boolean(runtime.busyRun) || conversationLoading}>↑</button>
                  </form>
                  <small className="chat-disclaimer">Agent 可能出错；投递前请核对职位与申请内容。</small>
                </div>
                </div>
                <aside className="chat-runtime-sidebar" id="chat-runtime-panel" aria-label="任务与上下文" hidden={!chatDetailsOpen}>
                  <div className="chat-runtime-heading"><div><span className="eyebrow">SESSION INSPECTOR</span><h2>任务与上下文</h2></div><button type="button" className="chat-inspector-close" aria-label="关闭任务详情" onClick={() => setChatDetailsOpen(false)}>×</button></div>
                  <RuntimePanel runtime={runtime} notice={setNotice} showSession={false} />
                  <div className="chat-runtime-status" role="status"><small>最近状态</small><p>{notice}</p></div>
                  {browserTask && browserTask.status !== "idle" && <section className="chat-task-evidence"><h3>浏览器任务</h3><dl><div><dt>搜索词</dt><dd>{browserTask.query || "—"}</dd></div><div><dt>状态</dt><dd>{browserTask.detail || browserTask.status}</dd></div><div><dt>恢复点</dt><dd>{browserTask.checkpoint || "—"}</dd></div><div><dt>登录证据</dt><dd>{browserTask.login_evidence?.authenticated_signal ? "登录已确认" : browserTask.takeover_reason || "尚未确认"}</dd></div><div><dt>Adapter</dt><dd>{browserTask.adapter_version || browserTask.selector_version || "—"}</dd></div></dl></section>}
                  {deliveryTask && deliveryTask.status !== "idle" && <section className="chat-task-evidence"><h3>投递任务</h3><p>{deliveryTask.detail}</p><p>{deliveryTask.recruiter_reply || "尚未收到招聘方回复"}</p>{["waiting_for_user", "waiting_for_reply"].includes(deliveryTask.status) && <button className="button secondary compact" onClick={resumeDelivery}>{deliveryTask.status === "waiting_for_user" ? "验证完成，继续" : "检查回复并继续"}</button>}</section>}
                </aside>
                </div>
                <input ref={chatUploadRef} className="hidden" type="file" multiple onChange={handleChatAttachment} accept=".pdf,.docx,.md,.txt,.csv,.zip,.py,.ts,.tsx,.js,.jsx,.java,.go,.rs,.c,.cpp,.h,.hpp,.cs,.sql,.json,.yaml,.yml,.toml,.html,.css" />
              </div>}

              {activeView === "overview" && <div className="panel insights-panel">
                <div className="panel-heading"><div><span className="eyebrow">CURRENT RESUME</span><h2>当前简历搜索职位的技能分布</h2></div><button className="text-button" onClick={() => setActiveView("insights")}>完整报告 →</button></div>
                <div className="skill-bars">
                  {(insights?.tags ?? []).slice(0, 5).map(({ tag, count }) => (
                    <div className="skill-row" key={tag}><div><span>{tag}</span><strong>{count} 个职位</strong></div><div className="bar"><i style={{ width: `${Math.min(100, count / Math.max(insights?.stored_total ?? 0, 1) * 100)}%` }} /></div></div>
                  ))}
                  {!insights?.tags.length && <div className="empty-state">暂无职位统计，完成搜索后生成。</div>}
                </div>
                <div className="insight-callout"><span>⌗</span><p>仅统计本地已保存职位，不代表整个招聘市场的趋势。</p></div>
              </div>}
            </section>}

            {activeView === "board" && <section className="board-section panel standalone-board">
              <div className="section-heading"><div><span className="eyebrow">APPLICATION PIPELINE</span><h2>自动申请看板</h2><div className="board-actions"><button className="button secondary compact" onClick={() => void importMockJob()}>导入直接投递职位</button><button className="button secondary compact" disabled={workflowBusy} onClick={() => void syncMockWorkflow()}>同步模拟站会话</button></div></div><div className="legend"><span><i className="auto" />允许自动投递</span><span><i />需要人工确认</span><span className="click-hint">点击卡片查看详情</span></div></div>
              {workflowNotice && <p className="workflow-feedback">{workflowNotice}</p>}
              <div className="kanban">
                {(Object.keys(stageMeta) as JobStage[]).map((stage) => (
                  <div className="kanban-column" key={stage}>
                    <header><div><h3>{stageMeta[stage].title}<b>{counts[stage]}</b></h3><p>{stageMeta[stage].hint}</p></div><button aria-label="更多">•••</button></header>
                    <div className="card-stack">
                      {jobs.filter((job) => job.stage === stage).map((job) => (
                        <article className="job-card" key={job.id} onClick={() => setDetailJobId(job.id)} title="点击查看职位详情">
                          <div className="job-top"><span className={scoreClass(job.score)}>{job.score}</span><span className={job.mode === "自动投递" ? "mode auto" : "mode"}>{job.review_approved && job.stage === "review" ? "审核通过 · 待授权" : job.mode}</span></div>
                          <h4>{job.role}</h4><p className="company">{job.company} · {job.location}</p><strong className="salary">{job.salary}</strong>
                          <div className="tags">{job.tags.slice(0, 2).map((tag) => <span key={tag}>{tag}</span>)}</div>
                          <p className="reason">{job.reason}</p>
                          {job.recruiter_reply && <p className="confirm-result ok">HR 回复：{job.recruiter_reply}</p>}
                          <footer><small>{job.source} · <time dateTime={job.posted} title={job.posted}>{shortJobDate(job.posted)}</time></small>{(stage === "review" || stage === "waiting") && <button onClick={(event) => { event.stopPropagation(); advanceJob(job.id); }}>{stage === "review" ? "审阅投递" : "记录面试"} →</button>}</footer>
                          {stage === "review" && <button className="reject" onClick={(event) => { event.stopPropagation(); rejectJob(job.id); }}>跳过这个职位</button>}
                        </article>
                      ))}
                      {jobs.filter((job) => job.stage === stage).length === 0 && <div className="empty-state">暂无职位</div>}
                    </div>
                  </div>
                ))}
              </div>
            </section>}
          </>
        )}

        {activeView === "profile" && (
          <section className="detail-layout">
            <div className="panel profile-hero"><span className="avatar large">AI</span><div><span className="eyebrow">候选人结构化画像</span><h2>{candidateState?.profile.role_suggestions[0] ?? "等待简历解析"}</h2><p>{candidateState?.profile.summary ?? "上传简历后，Agent 将提取有证据支持的职业信息。"}</p><div className="tags">{candidateState?.profile.skills.slice(0, 8).map((skill) => <span key={skill}>{skill}</span>)}</div></div><span className="preference-version">偏好 v{candidateState?.preferences.version ?? 0}</span></div>
            <div className="two-columns">
              <div className="panel">
                <div className="panel-heading"><div><h2>候选人材料中心</h2><p>{resumes.length} 份简历 · {candidateMaterials.length} 份课程/项目/代码材料</p></div><div className="material-actions"><button className="button secondary compact" onClick={() => uploadRef.current?.click()} disabled={resumeBusy}>{resumeBusy ? "上传中…" : "上传简历"}</button><select value={materialKind} onChange={(event) => setMaterialKind(event.target.value as "course" | "project" | "code")} disabled={resumeBusy} aria-label="材料类型"><option value="course">课程报告</option><option value="project">项目材料</option><option value="code">代码/ZIP</option></select><button className="button primary compact" onClick={() => materialUploadRef.current?.click()} disabled={resumeBusy}>{resumeBusy ? "解析中…" : "上传材料"}</button></div></div>
                {uploadFeedback && <div className={`material-upload-feedback ${uploadFeedback.kind}`} role={uploadFeedback.kind === "error" ? "alert" : "status"}>{uploadFeedback.text}</div>}
                <div className="material-list">
                  {materials.map((item) => {
                    const resume = resumes.find((candidate) => candidate.id === item.id);
                    const isActive = resume?.id === candidateState?.profile.resume_id;
                    return <div key={item.id} className="material-row"><span className="file-icon">□</span><div><strong>{item.name}</strong><small>{isActive ? `${item.type} · 当前主简历` : item.type}</small></div>{resume ? <div className="material-row-actions"><a className="text-button" href={`/api/agent/resumes?id=${encodeURIComponent(resume.id)}&original=1`} target="_blank" rel="noreferrer">查看原件</a>{isActive ? <button className="text-button" type="button" onClick={editResumeText}>编辑文本版</button> : <button className="text-button" type="button" onClick={() => void activateUploadedResume(resume)} disabled={resumeBusy}>设为主简历</button>}<button className="text-button danger" type="button" onClick={() => void deleteUploadedResume(resume)} disabled={resumeBusy}>删除</button></div> : <em className={item.status === "失败" ? "material-failed" : ""}>{item.status}</em>}</div>;
                  })}
                  {materials.length === 0 && <div className="empty-state">上传简历、课程报告、项目文档、单个代码文件或项目 ZIP</div>}
                </div>
              </div>
              <div className="panel search-profile"><div className="panel-heading"><div><h2>持久化求职偏好</h2><p>最多保存 5 条搜索关键词；点击任意历史关键词可切换本次搜索</p></div></div>{candidateState ? <div className="resume-search-config"><div><small>多来源画像 · {candidateState.profile.evidence.length} 条证据</small><strong>{resumes[0]?.filename ?? "尚未上传简历，可先使用项目与对话证据"}</strong></div><div className="preference-source"><span>目标岗位</span><em>{candidateState.preferences.target_roles.source === "user_confirmed" ? "用户已确认" : "证据推导 · 待确认"}</em></div><div className="tags">{candidateState.profile.role_suggestions.map((role) => <button key={role} type="button" onClick={() => setSearchQuery(role)}>{role}</button>)}{candidateState.profile.skills.slice(0, 8).map((keyword) => <button key={keyword} type="button" onClick={() => setSearchQuery((current) => `${current} ${keyword}`.trim().slice(0, 120))}>{keyword}</button>)}</div><div className="saved-keywords"><small>已保存关键词 {candidateState.preferences.search_keywords.values.length}/5</small><div className="tags">{candidateState.preferences.search_keywords.values.map((keyword) => <button key={keyword} type="button" onClick={() => setSearchQuery(keyword)}>{keyword}</button>)}</div></div><label htmlFor="resume-search-query">本次模拟网站搜索关键词</label><input id="resume-search-query" value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} maxLength={120} /><small>普通搜索不会自动改写持久化偏好；通用“帮我找工作”优先参考当前主简历岗位。</small><button className="button primary" type="button" onClick={() => { setActiveView("chat"); setChatText(`搜索：${searchQuery}`); }} disabled={!searchQuery.trim()}>到对话中开始搜索</button></div> : <div className="empty-state resume-empty">上传材料或在 Agent 对话中描述经历后生成候选人画像</div>}</div>
            </div>
            <div className="panel resume-draft-panel">
              <div className="panel-heading resume-draft-heading">
                <div>
                  <span className="eyebrow">EDITABLE RESUME</span>
                  <h2>动态简历草稿</h2>
                  <p>{resumeSourceMode === "verbatim" ? "保留上传简历的内容，自动统一标题、字体与间距。点击编辑即可在排版页面中修改并保存，不自动润色或新增经历。" : "按职业摘要、核心能力与项目成果分区；自动区分架构、技术栈、核心实现与调优结果。"}</p>
                </div>
                <div className="resume-actions">
                  {!resumeEditing && <button className="button secondary compact" type="button" onClick={() => void organizeResume()} disabled={layoutBusy || resumeBusy || !resumeDraft.trim()}>{layoutBusy ? "整理与校验中…" : "AI 整理结构"}</button>}
                  {resumeSourceMode === "verbatim" && activeResume && !resumeEditing && <>
                    <button className={`button compact ${resumeViewMode === "text" ? "primary" : "secondary"}`} type="button" onClick={() => setResumeViewMode("text")}>排版简历</button>
                    <button className={`button compact ${resumeViewMode === "original" ? "primary" : "secondary"}`} type="button" onClick={() => setResumeViewMode("original")}>上传原件（备份）</button>
                  </>}
                  {resumeEditing ? (
                    <>
                      <button className="button secondary compact" type="button" onClick={() => { setResumeDraft(resumeSavedDraft); setResumeEditing(false); }} disabled={resumeSaveBusy}>取消</button>
                      <button className="button primary compact" type="button" onClick={() => void saveResumeDraft()} disabled={resumeSaveBusy || !resumeDraft.trim()}>{resumeSaveBusy ? "保存中…" : "保存修改"}</button>
                    </>
                  ) : (
                    <>
                      <button className="button secondary compact" type="button" onClick={editResumeText} disabled={!candidateState || layoutBusy}>编辑</button>
                      <button className="button primary compact" type="button" onClick={exportResumePdf} disabled={!resumeDraft.trim()}>保存为 PDF</button>
                    </>
                  )}
                </div>
              </div>
              <div className="resume-layout-status" role="status">
                {layoutBusy ? "正在整理结构并核对原文引用，不修改简历正文…" : !resumeDocument?.layout ? "基础排版 · 尚未进行 AI 结构整理。整理前会确认模型及数据发送。" : resumeDocument.layout.status === "validated" ? "引用与完整性校验通过 · 分类是否合理仍需核对，不代表事实已被外部证实。" : resumeDocument.layout.status === "needs_review" ? "原文完整保留 · 有待核对内容，请检查高亮段落的归属。" : resumeDocument.layout.status === "stale" ? "内容已修改 · 旧结构已失效，可重新整理；当前保留基础排版。" : "结构校验未通过或模型不可用 · 已保留原文，未采用模型输出。"}
                {resumeDocument?.layout && <small>{resumeDocument.layout.prompt_version} · {resumeDocument.layout.model || "未调用有效模型"} · 尝试 {resumeDocument.layout.attempts} 次{resumeDocument.layout.issues.length ? ` · ${resumeDocument.layout.issues.join(" / ")}` : ""}</small>}
              </div>
              {!resumeEditing && resumeViewMode === "original" && activeResume ? (
                activeResume.content_type === "application/pdf" || activeResume.filename.toLowerCase().endsWith(".pdf")
                  ? <iframe className="resume-original-frame" src={`/api/agent/resumes?id=${encodeURIComponent(activeResume.id)}&original=1`} title={`原始简历 ${activeResume.filename}`} />
                  : <div className="resume-original-card"><span className="file-icon">□</span><div><strong>{activeResume.filename}</strong><p>浏览器无法内嵌此格式，但服务器保存的是上传时的原始字节。</p></div><a className="button primary compact" href={`/api/agent/resumes?id=${encodeURIComponent(activeResume.id)}&original=1`} target="_blank" rel="noreferrer">打开原件</a></div>
              ) : resumeEditing ? (
                resumeSourceMode === "verbatim" ? <>
                  <p className="resume-edit-hint">点击任意文字即可修改，Enter 换行；保存后自动沿用简历样式。取消不会保存本次修改。</p>
                  <article className="resume-document verbatim" id="resume-print-area"><ImportedResumeEditor text={resumeDraft} layout={resumeDocument?.layout} disabled={resumeSaveBusy} onChange={setResumeDraft} /></article>
                </> : <textarea id="resume-print-area" className="resume-editor" value={resumeDraft} onChange={(event) => setResumeDraft(event.target.value)} spellCheck={false} aria-label="编辑简历草稿" />
              ) : (
                <article className={`resume-document ${resumeSourceMode === "verbatim" ? "verbatim" : ""}`} id="resume-print-area">
                  {resumeDraft ? renderResumeDocument(resumeDraft, resumeSourceMode === "verbatim", resumeDocument?.layout) : <div className="empty-state">尚无可生成的候选人证据。</div>}
                </article>
              )}
            </div>
            <input ref={uploadRef} className="hidden" type="file" multiple onChange={handleUpload} accept=".pdf,.docx,.md,.txt" />
            <input ref={materialUploadRef} className="hidden" type="file" multiple onChange={handleMaterialUpload} accept=".pdf,.docx,.md,.txt,.csv,.zip,.py,.ts,.tsx,.js,.jsx,.java,.go,.rs,.c,.cpp,.h,.hpp,.cs,.sql,.json,.yaml,.yml,.toml,.html,.css" />
          </section>
        )}

        {activeView === "insights" && (
          <section className="detail-layout">
            <div className="panel report-hero">
              <div><span className="eyebrow">CURRENT RESUME · {insights?.tasks_total ?? 0} 次搜索</span><h2>搜索统计</h2><p>{insights?.scope === "latest_legacy_run" ? "旧版搜索任务未记录简历关联：暂仅展示当前简历启用后的最近一次搜索；下一轮搜索起将按主简历累计。" : "仅统计当前主简历对应的搜索结果；包含中断或失败前已记录的职位，其他简历的历史结果不会混入。"}</p></div>
            </div>
            {insights ? (
              <>
                <section className="stats-grid">
                  <article><span className="stat-icon mint">⌕</span><div><small>本简历搜索发现</small><strong>{insights.discovered_total}</strong><em>所选搜索任务发现总数</em></div></article>
                  <article><span className="stat-icon peach">◎</span><div><small>涉及的唯一职位</small><strong>{insights.stored_total}</strong><em>重复命中 {insights.duplicates_total} 次</em></div></article>
                  <article><span className="stat-icon blue">↗</span><div><small>平均匹配分</small><strong>{insights.avg_score}</strong><em>高匹配 {insights.score_high} 个</em></div></article>
                  <article><span className="stat-icon violet">✦</span><div><small>看板已拒绝职位</small><strong>{insights.rejected}</strong><em>不含未入库的初筛跳过</em></div></article>
                </section>
                <div className="insight-grid">
                  <div className="panel large-chart">
                    <div className="panel-heading"><div><h2>整体 JD 画像</h2><p>按已核对的唯一职位汇总原文；含 {insights.partial_tasks ?? 0} 次未完整结束的搜索。</p></div></div>
                    <h3>主要职责</h3>
                    {(insights.jd_overview?.responsibilities ?? []).map((item) => <p key={item.text}>{item.text} · {item.count} 个岗位</p>)}
                    <h3>任职要求</h3>
                    {(insights.jd_overview?.requirements ?? []).map((item) => <p key={item.text}>{item.text} · {item.count} 个岗位</p>)}
                    {!insights.jd_overview?.responsibilities.length && <p>暂无可核对的 JD 职责。</p>}
                  </div>
                  <div className="panel large-chart">
                    <div className="panel-heading"><div><h2>招聘类型与福利</h2><p>仅统计 JD 明确提及的信息</p></div></div>
                    {Object.entries(insights.employment_distribution ?? {}).map(([name, count]) => <p key={name}>{name} · {count} 个岗位</p>)}
                    {Object.entries(insights.benefit_distribution ?? {}).map(([name, count]) => <p key={name}>{name} · {count} 个岗位</p>)}
                  </div>
                  <div className="panel large-chart">
                    <div className="panel-heading"><div><h2>城市分布</h2><p>保存职位数量最多的城市</p></div></div>
                    {insights.cities.length === 0 && <div className="empty-state">暂无数据，完成一次搜索后生成</div>}
                    {insights.cities.map(({ city, count }) => (
                      <div className="market-row" key={city}><span>{city}</span><div className="bar"><i className="owned" style={{ width: `${Math.round((count / Math.max(...insights.cities.map((item) => item.count), 1)) * 100)}%` }} /></div><strong>{count}</strong></div>
                    ))}
                  </div>
                  <div className="panel large-chart">
                    <div className="panel-heading"><div><h2>薪资分布</h2><p>保存职位的薪资区间统计</p></div></div>
                    {insights.salaries.length === 0 && <div className="empty-state">暂无数据</div>}
                    {insights.salaries.map(({ bucket, count }) => (
                      <div className="market-row" key={bucket}><span>{bucket}</span><div className="bar"><i className="owned" style={{ width: `${Math.round((count / Math.max(...insights.salaries.map((item) => item.count), 1)) * 100)}%` }} /></div><strong>{count}</strong></div>
                    ))}
                  </div>
                  <div className="panel large-chart">
                    <div className="panel-heading"><div><h2>技能标签 TOP 10</h2><p>出现次数最多的技能关键词</p></div></div>
                    {insights.tags.length === 0 && <div className="empty-state">暂无数据</div>}
                    {insights.tags.map(({ tag, count }) => (
                      <div className="market-row" key={tag}><span>{tag}</span><div className="bar"><i className="owned" style={{ width: `${Math.round((count / Math.max(...insights.tags.map((item) => item.count), 1)) * 100)}%` }} /></div><strong>{count}</strong></div>
                    ))}
                  </div>
                  <div className="panel large-chart">
                    <div className="panel-heading"><div><h2>匹配分分布</h2><p>高 ≥90 · 中 80–89 · 一般 &lt;80</p></div></div>
                    <div className="market-row"><span>高匹配 ≥90</span><div className="bar"><i className="owned" style={{ width: `${Math.round((insights.score_high / Math.max(insights.stored_total, 1)) * 100)}%` }} /></div><strong>{insights.score_high}</strong></div>
                    <div className="market-row"><span>中匹配 80–89</span><div className="bar"><i style={{ width: `${Math.round((insights.score_good / Math.max(insights.stored_total, 1)) * 100)}%` }} /></div><strong>{insights.score_good}</strong></div>
                    <div className="market-row"><span>一般 &lt;80</span><div className="bar"><i className="gap" style={{ width: `${Math.round((insights.score_fair / Math.max(insights.stored_total, 1)) * 100)}%` }} /></div><strong>{insights.score_fair}</strong></div>
                  </div>
                </div>
              </>
            ) : <div className="panel"><div className="empty-state">正在加载统计…</div></div>}
          </section>
        )}

        {activeView === "plans" && (
          <section className="detail-layout learning-page">
            <RuntimePanel runtime={runtime} notice={setPlanNotice} />
            <div className="panel plan-header">
              <div><span className="eyebrow">JD EVIDENCE · CURRENT RESUME</span><h2>成长计划</h2><p>对照当前主简历与真实 JD，优先分析专业技术、行业知识和实习方向。明确点击后才调用模型生成计划；岗位原文可追溯，完成时需提交实践证据。</p></div>
              <div className="plan-generate"><input aria-label="计划关注方向" value={planFocus} maxLength={400} onChange={(event) => setPlanFocus(event.target.value)} placeholder="可选：本次优先关注的岗位或方向" /><button className="button primary" disabled={planBusy || !candidateState?.profile.resume_id} onClick={() => void generateLearningPlan()}>{planBusy ? "处理中…" : "分析差距并生成计划"}</button></div>
            </div>
            {planNotice && <div className="panel workflow-feedback">{planNotice}</div>}
            {!learningPlans.length && <div className="panel empty-state">暂无当前简历的学习计划。先完成职位搜索，再点击上方按钮；不会凭空生成技能差距。</div>}
            {learningPlans.map((plan) => (
              <article className="panel learning-plan" key={plan.id}>
                <div className="learning-plan-head"><div><span className="eyebrow">{new Date(plan.created_at).toLocaleDateString("zh-CN")} · {plan.jobs_analyzed} 份 JD · 简历 v{plan.resume_version}</span><h3>{plan.title}</h3><p>{plan.summary}</p></div><strong>{plan.gaps.filter((gap) => gap.status === "completed_user_reported").length}/{plan.gaps.length} 已完成</strong></div>
                <div className="learning-gap-list">{plan.gaps.map((gap) => {
                  const target = completionTargets[gap.id] ?? gap.resume_target;
                  const proposal = gap.resume_proposal_id ? planProposals[gap.resume_proposal_id] : undefined;
                  return <div className="learning-gap" key={gap.id}>
                    <div className="learning-gap-head"><div><small>{gap.category === "internship_direction" ? "实习方向" : gap.category === "technical_skill" ? "专业技术" : gap.category === "domain_knowledge" ? "行业知识" : "职业能力"} · {gap.priority === "high" ? "高优先级" : gap.priority === "medium" ? "中优先级" : "补充项"}</small><h4>{gap.name}</h4></div><span>{gap.status === "completed_user_reported" ? "已标记完成" : `${gap.estimated_hours} 小时`}</span></div>
                    <p>{gap.rationale}</p>
                    <p className="learning-assessment">{gap.assessment === "resume_not_shown" ? "当前简历未体现，不能据此断定不会" : `简历已有证据：${gap.resume_quote}`}</p>
                    <div className="learning-jd"><strong>JD 原文依据 · {gap.frequency} 个岗位</strong>{gap.jd_quotes.map((quote, index) => {
                      const source = plan.job_sources.find((job) => job.id === quote.job_id);
                      return <p key={`${quote.job_id}-${index}`}>#{quote.job_id} {source && /^https?:\/\//.test(source.source_url) ? <a href={source.source_url} target="_blank" rel="noreferrer">{source.title}</a> : source?.title ?? "岗位"}：{quote.quote}</p>;
                    })}</div>
                    <details><summary>查看详细学习任务与验收标准</summary><div className="learning-detail"><p><strong>目标：</strong>{gap.objective}</p>{gap.prerequisites.length > 0 && <p><strong>前置知识：</strong>{gap.prerequisites.join("、")}</p>}<ol>{gap.steps.map((step, index) => <li key={index}>{step}</li>)}</ol><p><strong>实践产物：</strong>{gap.practice}</p><strong>验收标准</strong><ul>{gap.acceptance.map((item, index) => <li key={index}>{item}</li>)}</ul></div></details>
                    {gap.status === "todo" ? <div className="learning-complete"><label>完成证据：具体做了什么、产物与可验证结果<textarea value={completionEvidence[gap.id] ?? ""} onChange={(event) => setCompletionEvidence((current) => ({ ...current, [gap.id]: event.target.value }))} placeholder="不要只写‘已学会’。请描述练习、项目、产物、结果或链接。" /></label><div className="learning-complete-row"><label>写入简历<select value={target} onChange={(event) => setCompletionTargets((current) => ({ ...current, [gap.id]: event.target.value as "skill" | "project" | "none" }))}><option value="none">不补充</option><option value="skill">补充简洁技能</option><option value="project">补充项目经历</option></select></label>{target === "project" && <label>真实项目名称<input value={completionTitles[gap.id] ?? ""} onChange={(event) => setCompletionTitles((current) => ({ ...current, [gap.id]: event.target.value }))} placeholder="例如：信用风险评分模型" /></label>}<button className="button primary compact" disabled={planBusy} onClick={() => void completeLearningGap(plan.id, gap.id, gap.resume_target)}>标记完成</button></div></div> : <div className="learning-done">已保存你的完成说明；这属于用户报告，简历内容仍需审核。{gap.resume_proposal_id && !proposal && <span> 简历提案已处理或待刷新。</span>}</div>}
                    {proposal && <div className="learning-proposal"><strong>简历补充提案 · 待审核</strong><p>{proposal.reason}</p>{proposal.operations.map((operation) => <div key={operation.id}><b>{operation.label}</b><p>{formatProposalValue(operation.new_value)}</p></div>)}<div className="learning-complete-row"><button className="button secondary compact" disabled={planBusy} onClick={() => void decideLearningProposal(proposal, "reject")}>不写入简历</button><button className="button primary compact" disabled={planBusy} onClick={() => void decideLearningProposal(proposal, "approve")}>核对无误，写入简历</button></div></div>}
                  </div>;
                })}</div>
              </article>
            ))}
          </section>
        )}

        {activeView === "settings" && (
          <section className="detail-layout settings-layout">
            <div className="panel settings-hero">
              <div><span className="eyebrow">BRING YOUR OWN KEY</span><h2>模型连接</h2><p>API Key 只发送到本机后端并加密保存；页面不会读取或回显已保存的 Key。</p></div>
              <span className={llmConfigured ? "connection-badge ready" : "connection-badge"}>{llmConfigured ? "已配置" : "未配置"}</span>
            </div>
            <form className="panel settings-form" onSubmit={saveLlmConfig}>
              <div className="field">
                <label htmlFor="llm-provider">模型提供商</label>
                <select id="llm-provider" value={llmProvider} onChange={(event) => {
                  const provider = event.target.value as "mock" | "deepseek";
                  setLlmProvider(provider);
                  setLlmModel(provider === "deepseek" ? "deepseek-v4-flash" : "mock-autohunt-v1");
                  setLlmConfigured(provider === "mock");
                }}><option value="mock">Mock（无需联网）</option><option value="deepseek">DeepSeek</option></select>
              </div>
              <div className="field">
                <label htmlFor="llm-model">模型</label>
                <select id="llm-model" value={llmModel} onChange={(event) => setLlmModel(event.target.value)}>
                  {llmProvider === "deepseek" ? <><option value="deepseek-v4-flash">DeepSeek V4 Flash</option><option value="deepseek-v4-pro">DeepSeek V4 Pro</option></> : <option value="mock-autohunt-v1">Mock AutoHunt v1</option>}
                </select>
              </div>
              {llmProvider === "deepseek" && <div className="field full"><label htmlFor="llm-api-key">DeepSeek API Key</label><input id="llm-api-key" type="password" autoComplete="new-password" value={llmApiKey} onChange={(event) => setLlmApiKey(event.target.value)} placeholder={llmConfigured ? "已保存；留空则保持原 Key" : "输入 DeepSeek API Key"} /><small>Key 不会写入 localStorage、日志、memory.md 或技能文件。</small></div>}
              <div className="settings-actions full"><button type="submit" className="button primary" disabled={llmBusy}>{llmBusy ? "处理中…" : "保存设置"}</button><button type="button" className="button secondary" disabled={llmBusy || !llmConfigured} onClick={testLlmConnection}>测试连接</button></div>
            </form>
            <form className="panel settings-form workflow-settings" onSubmit={saveWorkflowSettings}>
              <div className="field full"><span className="eyebrow">LOCAL SIMULATOR ONLY</span><h2>模拟站会话与每日任务</h2><p>默认关闭。仅操作本地模拟招聘网站；验证码和 MFA 仍由你人工完成。自动投递需单独开启，并为每个岗位生成一次性授权；仅处理开启后收到的回复。定时任务要求 Agent 后端持续运行。</p></div>
              <div className="field full"><label className="workflow-toggle"><input type="checkbox" checked={workflowSettings?.monitor_enabled ?? false} onChange={(event) => setWorkflowSettings((current) => current ? { ...current, monitor_enabled: event.target.checked, auto_submit: event.target.checked && current.auto_submit, daily_enabled: event.target.checked && current.daily_enabled } : current)} />监测手动沟通和 HR 回复，自动同步申请看板</label></div>
              <div className="field full"><label className="workflow-toggle"><input type="checkbox" checked={workflowSettings?.auto_submit ?? false} onChange={(event) => setWorkflowSettings((current) => current ? { ...current, monitor_enabled: event.target.checked || current.monitor_enabled, auto_submit: event.target.checked } : current)} />HR 明确回复后，自动在模拟站发送简历</label></div>
              <div className="field full"><label className="workflow-toggle"><input type="checkbox" checked={workflowSettings?.daily_enabled ?? false} onChange={(event) => setWorkflowSettings((current) => current ? { ...current, monitor_enabled: event.target.checked || current.monitor_enabled, daily_enabled: event.target.checked } : current)} />每天定时搜索并联系高匹配岗位</label></div>
              <div className="field"><label htmlFor="workflow-time">每天执行时间（北京时间）</label><input id="workflow-time" type="time" value={workflowSettings?.daily_time ?? "09:00"} onChange={(event) => setWorkflowSettings((current) => current ? { ...current, daily_time: event.target.value } : current)} /></div>
              <div className="field"><label htmlFor="workflow-score">自动沟通最低匹配分</label><input id="workflow-score" type="number" min="0" max="100" value={workflowSettings?.min_score ?? 75} onChange={(event) => setWorkflowSettings((current) => current ? { ...current, min_score: Number(event.target.value) } : current)} /></div>
              <div className="field"><label htmlFor="workflow-contacts">每天最多联系岗位</label><input id="workflow-contacts" type="number" min="1" max="5" value={workflowSettings?.max_contacts ?? 3} onChange={(event) => setWorkflowSettings((current) => current ? { ...current, max_contacts: Number(event.target.value) } : current)} /></div>
              <div className="field"><small>当前绑定的模拟站求职者账号：{workflowSettings?.applicant_id ?? "未绑定；先登录模拟站并完成一次搜索"}。上次执行：{workflowSettings?.last_run_date ?? "暂无"}。</small>{workflowSettings?.last_error && <small className="workflow-error">最近错误：{workflowSettings.last_error}</small>}</div>
              <div className="settings-actions full"><button className="button primary" disabled={workflowBusy || !workflowSettings} type="submit">保存每日任务设置</button><button className="button secondary" disabled={workflowBusy || !workflowSettings?.applicant_id} type="button" onClick={() => void syncMockWorkflow()}>立即同步一次</button></div>
              {workflowNotice && <p className="workflow-feedback full">{workflowNotice}</p>}
            </form>
            <div className="panel security-note"><span>🔒</span><div><h3>本地安全边界</h3><p>前端只调用 Next.js 同源接口；真正的 DeepSeek 请求由 FastAPI 后端发出。后端仅允许官方固定地址和两个模型 ID。</p></div></div>
          </section>
        )}

        {detailJob && (
          <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="职位详情" onClick={() => setDetailJobId(null)}>
            <div className="delivery-modal job-detail-modal panel" onClick={(event) => event.stopPropagation()}>
              <div className="job-detail-top">
                <span className="eyebrow">JOB DETAIL · {stageMeta[detailJob.stage].title}</span>
                <button className="text-button" onClick={() => setDetailJobId(null)}>关闭 ✕</button>
              </div>
              <h2>{detailJob.role}</h2>
              <p className="detail-company">{detailJob.company} · {detailJob.location}</p>
              <div className="job-detail-score">
                <span className={scoreClass(detailJob.score)}>{detailJob.score}</span>
                <span className={detailJob.mode === "自动投递" ? "mode auto" : "mode"}>{detailJob.mode}</span>
                <strong>{detailJob.salary}</strong>
              </div>
              <dl>
                <div><dt>来源</dt><dd>{detailJob.source} · {detailJob.posted}</dd></div>
                <div><dt>标签</dt><dd className="detail-tags">{detailJob.tags.map((tag) => <span key={tag}>{tag}</span>)}</dd></div>
                <div><dt>推荐理由</dt><dd>{detailJob.reason}</dd></div>
                {detailJob.review_approved && <div><dt>人工审核</dt><dd>已通过本地审核；沟通与简历投递仍需要单独授权。</dd></div>}
                {detailJob.recruiter_reply && <div><dt>HR 回复</dt><dd>{detailJob.recruiter_reply}{detailJob.recruiter_reply_at ? ` (${new Date(detailJob.recruiter_reply_at).toLocaleString("zh-CN")})` : ""}</dd></div>}
                {detailJob.sourceUrl && <div><dt>职位链接</dt><dd><a href={detailJob.sourceUrl} target="_blank" rel="noreferrer">打开职位原文 ↗</a></dd></div>}
              </dl>
              <div className="modal-actions">
                {detailJob.stage === "review" && <button className="button secondary" onClick={() => { rejectJob(detailJob.id); setDetailJobId(null); }}>跳过这个职位</button>}
                {(detailJob.stage === "review" || detailJob.stage === "waiting") && <button className="button primary" onClick={() => { const id = detailJob.id; setDetailJobId(null); advanceJob(id); }}>{detailJob.stage === "review" ? "生成投递预览" : "记录面试"}</button>}
                {detailJob.stage === "applying" && <button className="button primary" onClick={() => { setDetailJobId(null); void resumeDelivery(); }}>继续投递</button>}
                <button className="button secondary" onClick={() => setDetailJobId(null)}>关闭</button>
              </div>
            </div>
          </div>
        )}

        {deliveryPreview && <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="投递预览">
          <div className="delivery-modal panel">
            <span className="eyebrow">DELIVERY PREVIEW</span><h2>确认本次投递</h2>
            <dl><div><dt>职位</dt><dd>{deliveryPreview.title}</dd></div><div><dt>公司</dt><dd>{deliveryPreview.company}</dd></div><div><dt>简历</dt><dd>{deliveryPreview.resume_label} · v{deliveryPreview.resume_version}</dd></div><div><dt>简历快照</dt><dd>{deliveryPreview.resume_digest.slice(0, 12)}…</dd></div><div><dt>动作</dt><dd>{deliveryPreview.action_summary}</dd></div></dl>
            <p>授权只绑定这一个职位、当前简历版本和“发送简历”动作，30 分钟过期且只能消费一次。确认后若简历变化，系统会拒绝旧预览。验证码、MFA 或结果无法确认时必须暂停。</p>
            <div className="modal-actions"><button className="button secondary" onClick={() => setDeliveryPreview(null)}>取消</button><button className="button primary" onClick={authorizeDelivery}>确认并生成一次性授权</button></div>
          </div>
        </div>}
      </section>
    </main>
  );
}
