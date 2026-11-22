"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { InterviewPreparationReport, Job } from "@/lib/types";
import styles from "./interview-preparation.module.css";

const categoryLabels = { requirements: "岗位要求", project: "项目经历", depth: "专业深度", gap: "证据缺口", collaboration: "协作交付" };

function sourceLink(value: string) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

export function InterviewPreparation({ job, onClose }: { job: Job; onClose: () => void }) {
  const titleId = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const listRequest = useRef<AbortController | null>(null);
  const generationRequest = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  const [reports, setReports] = useState<InterviewPreparationReport[]>([]);
  const [selected, setSelected] = useState("");
  const [focus, setFocus] = useState("");
  const [loading, setLoading] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const report = reports.find((item) => item.id === selected) ?? reports[0];

  const refresh = useCallback(async () => {
    listRequest.current?.abort();
    const controller = new AbortController();
    listRequest.current = controller;
    setLoading(true); setError("");
    try {
      const response = await fetch(`/api/agent/interview-preparations?job_id=${job.id}`, { cache: "no-store", signal: controller.signal });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail ?? "报告尚未读取，请稍后刷新。");
      if (!Array.isArray(result)) throw new Error("报告列表格式不正确，请刷新核对。");
      if (controller.signal.aborted || !mounted.current) return;
      const related = (result as InterviewPreparationReport[]).filter((item) => item.job_id === job.id)
        .sort((left, right) => right.created_at.localeCompare(left.created_at));
      setReports(related);
      setSelected(related[0]?.id ?? "");
    } catch (reason) {
      if (!controller.signal.aborted && mounted.current) setError(reason instanceof Error ? reason.message : "无法读取面试准备报告。");
    } finally {
      if (!controller.signal.aborted && mounted.current) setLoading(false);
    }
  }, [job.id]);

  useEffect(() => {
    mounted.current = true;
    dialog.current?.showModal();
    void refresh();
    return () => {
      mounted.current = false;
      listRequest.current?.abort();
      generationRequest.current?.abort();
    };
  }, [refresh]);

  async function generate() {
    if (generating) return;
    listRequest.current?.abort();
    setLoading(false);
    const controller = new AbortController();
    generationRequest.current = controller;
    setGenerating(true); setError(""); setNotice("");
    try {
      const response = await fetch("/api/agent/interview-preparations", { method: "POST", signal: controller.signal,
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ job_id: job.id, focus: focus.trim() }) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail ?? "没有取得生成结果，请刷新报告核对。");
      if (result.job_id !== job.id || !Array.isArray(result.questions)) throw new Error("返回报告与当前岗位不一致，已忽略；请刷新核对。");
      if (controller.signal.aborted || !mounted.current) return;
      const created = result as InterviewPreparationReport;
      setReports((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setSelected(created.id);
      setNotice(created.generation_mode === "template" ? "已保存模板准备建议。" : "已保存本岗位的面试准备报告。");
    } catch (reason) {
      if (!controller.signal.aborted && mounted.current) setError(reason instanceof Error ? reason.message : "未取得面试准备结果，请刷新核对。");
    } finally {
      if (mounted.current && generationRequest.current === controller) {
        setGenerating(false);
        generationRequest.current = null;
      }
    }
  }

  function stopWaiting() {
    generationRequest.current?.abort();
    generationRequest.current = null;
    setGenerating(false);
    setNotice("已停止等待。后台生成可能仍在继续，可稍后刷新报告核对；没有取消投递或其他任务。");
  }

  const url = report ? sourceLink(report.source_url) : null;
  return <dialog ref={dialog} className={styles.dialog} aria-labelledby={titleId}
    onCancel={(event) => { event.preventDefault(); onClose(); }}
    onClick={(event) => {
      if (event.target !== event.currentTarget) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose();
    }}>
    <header className={styles.header}>
      <div><span className="eyebrow">面试准备</span><h2 id={titleId}>预测面试问题</h2></div>
      <button type="button" className="text-button" autoFocus onClick={onClose} aria-label="关闭面试准备">关闭 ✕</button>
    </header>
    <p className={styles.job}><strong>{job.role}</strong><span>{job.company} · {job.location}</span></p>
    <p className={styles.help}>根据该岗位 JD 与当前简历推测可能的问题、追问和准备方向。这里不是招聘方的实际题目，也不预测录用概率。</p>
    <form className={styles.controls} onSubmit={(event) => { event.preventDefault(); void generate(); }}>
      <label>关注方向（可选）<input value={focus} onChange={(event) => setFocus(event.target.value)} maxLength={400}
        placeholder="例如：项目细节、业务理解、专业技能" disabled={generating} /></label>
      <div className={styles.actions}>
        <button className="button primary" type="submit" disabled={generating}>{generating ? "正在生成…" : report ? "按当前简历重新生成" : "生成本岗位面试准备"}</button>
        {generating && <button type="button" className="button secondary" onClick={stopWaiting}>停止等待</button>}
        <button type="button" className="button secondary" disabled={loading || generating} onClick={() => void refresh()}>{loading ? "读取中…" : "刷新已保存报告"}</button>
      </div>
    </form>
    {error && <p className={styles.error} role="alert">{error}</p>}
    {(notice || loading || generating) && <p className={styles.status} role="status">{generating ? "正在核对岗位要求与简历证据，尚未完成。" : loading ? "正在读取当前简历的已保存报告…" : notice}</p>}
    {!report && !loading && <p className={styles.empty}>这个岗位还没有当前简历的面试准备报告。生成后会保留岗位来源和简历版本。</p>}
    {reports.length > 1 && <label className={styles.history}>已保存版本<select value={report?.id ?? ""} onChange={(event) => setSelected(event.target.value)}>
      {reports.map((item) => <option key={item.id} value={item.id}>简历 v{item.resume_version} · {new Date(item.created_at).toLocaleString("zh-CN")}{item.stale ? " · 旧快照" : ""}</option>)}
    </select></label>}
    {report && <section className={styles.report} aria-label="面试准备报告">
      <div className={styles.reportMeta}><strong>{report.generation_mode === "template" ? "模板准备建议" : "基于证据的面试预测"}</strong><span>简历 v{report.resume_version} · {new Date(report.created_at).toLocaleString("zh-CN")}</span></div>
      {report.stale && <p className={styles.stale} role="status">简历或 JD 已有变化，这份报告使用旧快照。请按当前简历重新生成。</p>}
      <p>{report.summary}</p>
      {report.notice && <p className={styles.help}>{report.notice}</p>}
      {report.focus && <p className={styles.help}>关注方向：{report.focus}</p>}
      {url && <a className={styles.source} href={url} target="_blank" rel="noopener noreferrer">打开岗位原文 ↗</a>}
      <ol className={styles.questions}>{report.questions.map((question, index) => <li key={question.id}>
        <span className={styles.category}>{categoryLabels[question.category]} · {index + 1}</span>
        <h3>{question.question}</h3>
        {question.followups.length > 0 && <div><h4>可能追问</h4><ul>{question.followups.map((text, item) => <li key={item}>{text}</li>)}</ul></div>}
        {question.preparation.length > 0 && <div><h4>准备要点</h4><ul>{question.preparation.map((text, item) => <li key={item}>{text}</li>)}</ul></div>}
        <details className={styles.evidence}><summary>查看问题依据</summary>
          <h4>JD 原文</h4><blockquote>{question.jd_quote}</blockquote>
          <h4>简历证据</h4>{question.resume_quote ? <blockquote>{question.resume_quote}</blockquote> : <p>简历中尚未体现对应证据，不代表你不会；请准备真实经历或说明实践计划。</p>}
          {question.candidate_source && <p className={styles.help}>证据来源：{question.candidate_source}</p>}
        </details>
      </li>)}</ol>
    </section>}
  </dialog>;
}
