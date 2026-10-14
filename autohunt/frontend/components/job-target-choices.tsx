"use client";

import { useState } from "react";
import type { AgentRun, TargetChoice } from "@/lib/agent-runtime";

export function JobTargetChoices({ runs, onChoose, stopping, notice }: {
  runs: AgentRun[]; stopping: boolean; notice: (message: string) => void;
  onChoose: (run: AgentRun, choice: TargetChoice, ids: number[]) => Promise<AgentRun>;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [selected, setSelected] = useState<Record<string, number[]>>({});
  const [feedback, setFeedback] = useState<{ runId: string; text: string } | null>(null);
  async function choose(run: AgentRun, choice: TargetChoice, ids: number[]) {
    setBusy(choice.id);
    setFeedback(null);
    try {
      const next = await onChoose(run, choice, ids);
      const text = next.status === "waiting_user" ? "已选定岗位，任务仍暂停" : "已选定岗位，原任务已接续";
      setFeedback({ runId: run.id, text }); notice(text);
    } catch (error) {
      const text = error instanceof Error ? error.message : "选择未提交，请刷新后重试";
      setFeedback({ runId: run.id, text }); notice(text);
    }
    finally { setBusy(null); }
  }
  return <>{runs.flatMap(run => (run.target_choices ?? []).map(choice => {
    const multiple = choice.selection_mode === "matching";
    const picked = selected[choice.id] ?? [];
    return <section className="job-choice-panel" key={`${run.id}:${choice.id}`} aria-label="选择目标岗位">
      <h3>{multiple ? "选择要处理的岗位" : "你指的是哪个岗位？"}</h3>
      <p>{multiple ? "勾选后确认；只处理选中的岗位。" : "点击一个岗位继续，无需输入序号。"} 目标未确认时不会执行依赖它的移除或投递。</p>
      <div className="job-choice-options">
        {choice.options.map((option, index) => <button type="button" key={option.id}
          className={`job-choice-card${picked.includes(option.id) ? " selected" : ""}`}
          aria-label={`选择${index + 1}：${option.title}，${option.company}，${option.score ?? "未评分"}分`}
          aria-pressed={multiple ? picked.includes(option.id) : undefined}
          disabled={busy !== null || stopping}
          onClick={() => multiple ? setSelected(current => ({ ...current, [choice.id]: picked.includes(option.id)
            ? picked.filter(id => id !== option.id) : [...picked, option.id] })) : void choose(run, choice, [option.id])}>
          <span className="job-choice-number">{index + 1}</span>
          <span className="job-choice-info"><strong>{option.title}</strong><span>{option.company}</span>
            <small>{[option.location, option.salary, option.score == null ? "未评分" : `匹配 ${option.score} 分`].filter(Boolean).join(" / ")}</small></span>
          <span className="job-choice-action">{busy === choice.id ? "提交中…" : multiple ? picked.includes(option.id) ? "已选" : "勾选" : "选择此岗位"}</span>
        </button>)}
      </div>
      {multiple && <button className="button primary compact" type="button" disabled={!picked.length || busy !== null || stopping}
        onClick={() => void choose(run, choice, picked)}>确认选中的 {picked.length} 个岗位</button>}
      <small className="job-choice-note">也可以继续用文字补充条件；选择绑定本次候选列表，不随看板排序变化。</small>
    </section>;
  }))}{feedback && runs.some(run => run.id === feedback.runId) && <p className="job-choice-feedback" role="status">{feedback.text}</p>}</>;
}
