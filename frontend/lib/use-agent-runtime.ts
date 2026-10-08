"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AgentRun, RunEvent, createRun, followRun, terminalRun } from "./agent-runtime";

export type SessionSearchState = { queries: string[]; locations: string[]; result_limit: number | null;
  salary_min: number | null; query_source: string; revision: number };
export type DialogueView = { active_intent: string; revision: number; idle_expired: boolean;
  frames: Record<string, { missing_slots: string[]; last_question: string }>;
  pending: Array<{ id: string; description: string; status: string }>;
  tasks: Record<string, string>; trace: Array<{ kind: string; source: string; tool?: string; status?: string }> };

export function useAgentRuntime() {
  const [sessionId, setSessionId] = useState("default");
  const [sessions, setSessions] = useState<Array<{ id: string; title: string }>>([]);
  const [searchState, setSearchState] = useState<SessionSearchState | null>(null);
  const [dialogue, setDialogue] = useState<DialogueView | null>(null);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [progress, setProgress] = useState<Record<string, string>>({});
  const [busyRun, setBusyRun] = useState<string | null>(null);
  const [notifications, setNotifications] = useState<Array<{ sessionId: string; runId: string; event: RunEvent }>>([]);
  const currentSession = useRef(sessionId);
  const connections = useRef(new Map<string, AbortController>());
  const mounted = useRef(true);

  const refresh = useCallback(async (session: string) => {
    const [response, conditions, context] = await Promise.all([
      fetch(`/api/agent/runs?conversation_id=${session}`, { cache: "no-store" }),
      fetch(`/api/agent/conversations/${session}/search-state`, { cache: "no-store" }),
      fetch(`/api/agent/conversations/${session}/context`, { cache: "no-store" }),
    ]);
    if (response.ok && currentSession.current === session) setRuns(await response.json());
    if (conditions.ok && currentSession.current === session) setSearchState(await conditions.json());
    if (context.ok && currentSession.current === session) setDialogue(await context.json());
  }, []);

  const observe = useCallback((run: AgentRun, onEvent?: (event: RunEvent) => void, onError?: (error: unknown) => void) => {
    if (connections.current.has(run.id) && !onEvent) return;
    connections.current.get(run.id)?.abort();
    const controller = new AbortController();
    connections.current.set(run.id, controller);
    void followRun(run.id, (event) => {
      if (event.kind === "browser_summary" || event.kind === "workflow_summary" || event.kind === "response" && !onEvent) {
        setNotifications((current) => [...current.slice(-99), { sessionId: run.session_id, runId: run.id, event }]);
      }
      if (currentSession.current === run.session_id && mounted.current) {
        if (event.kind === "search_state") setSearchState(event.data.conditions as SessionSearchState);
        if (typeof event.data.detail === "string") setProgress((current) => ({ ...current, [run.id]: String(event.data.detail) }));
        if (event.kind === "browser") {
          const task = event.data.task as { detail?: string };
          setProgress((current) => ({ ...current, [run.id]: task.detail ?? "浏览器任务执行中" }));
        }
        if (event.kind === "status" || event.kind === "browser" || event.kind === "delivery" || event.kind === "confirmation") {
          void refresh(run.session_id);
        }
        if (event.kind === "response") {
          setBusyRun((current) => current === run.id ? null : current);
          void refresh(run.session_id);
        }
        if (event.kind === "status" && terminalRun(String(event.data.status))) setBusyRun((current) => current === run.id ? null : current);
      }
      onEvent?.(event);
    }, controller.signal).catch((error) => {
      if (controller.signal.aborted) return;
      if (mounted.current) {
        setProgress((current) => ({ ...current, [run.id]: "连接已中断；任务记录已保存，点击重新连接查看" }));
        setBusyRun((current) => current === run.id ? null : current);
      }
      onError?.(error);
    }).finally(() => {
      if (connections.current.get(run.id) === controller) connections.current.delete(run.id);
    });
  }, [refresh]);

  useEffect(() => {
    mounted.current = true;
    const saved = localStorage.getItem("autohunt-conversation");
    void fetch("/api/agent/conversations", { cache: "no-store" }).then(async (response) => {
      if (!response.ok) return;
      const items = await response.json() as Array<{ id: string; title: string }>;
      if (!mounted.current) return;
      setSessions(items);
      if (saved && items.some((item) => item.id === saved)) setSessionId(saved);
    });
    return () => {
      mounted.current = false;
      connections.current.forEach((controller) => controller.abort());
      connections.current.clear();
    };
  }, []);

  useEffect(() => {
    currentSession.current = sessionId;
    localStorage.setItem("autohunt-conversation", sessionId);
    setBusyRun(null);
    setRuns([]);
    setSearchState(null);
    setDialogue(null);
    void refresh(sessionId).catch(() => undefined);
    void (async () => {
      const response = await fetch(`/api/agent/runs?conversation_id=${sessionId}`, { cache: "no-store" });
      if (!response.ok || currentSession.current !== sessionId) return;
      const items = await response.json() as AgentRun[];
      setRuns(items);
      items.filter((run) => !terminalRun(run.status)).forEach((run) => observe(run));
    })();
  }, [sessionId, observe, refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void fetch(`/api/agent/runs?conversation_id=${sessionId}`, { cache: "no-store" }).then(async (response) => {
        if (!response.ok || currentSession.current !== sessionId) return;
        const items = await response.json() as AgentRun[];
        setRuns(items);
        items.filter((run) => !terminalRun(run.status) && !connections.current.has(run.id)).forEach((run) => observe(run));
      }).catch(() => undefined);
    }, 20_000);
    return () => window.clearInterval(timer);
  }, [sessionId, observe]);

  async function newSession() {
    const response = await fetch("/api/agent/conversations", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: `会话 ${sessions.length + 1}` }) });
    const session = await response.json();
    if (!response.ok) throw new Error(session.detail ?? "创建会话失败");
    setSessions((current) => [session, ...current]);
    setSessionId(session.id);
  }

  async function execute(kind: AgentRun["kind"], payload: Record<string, string>, onEvent?: (event: RunEvent) => void): Promise<Record<string, unknown>> {
    setBusyRun("starting");
    let run: AgentRun;
    try {
      run = await createRun(sessionId, kind, payload);
    } catch (error) {
      setBusyRun(null);
      throw error;
    }
    if (currentSession.current === run.session_id) {
      setRuns((current) => [run, ...current]);
      setBusyRun(run.id);
    }
    return new Promise((resolve, reject) => observe(run, (event) => {
      onEvent?.(event);
      if (event.kind === "response") resolve(event.data.result as Record<string, unknown>);
      if (event.kind === "status" && terminalRun(String(event.data.status)) && event.data.status !== "completed") {
        reject(new Error(String(event.data.detail ?? "任务已停止")));
      }
    }, reject));
  }

  async function control(run: AgentRun, action: "cancel" | "resume") {
    const response = await fetch(`/api/agent/runs/${run.id}/${action}`, { method: "POST" });
    const next = await response.json() as AgentRun & { detail?: string };
    if (!response.ok) throw new Error(next.detail ?? "任务操作失败");
    if (!terminalRun(next.status)) observe(next);
    await refresh(sessionId);
  }

  async function resetSearchState() {
    const session = sessionId;
    const response = await fetch(`/api/agent/conversations/${session}/search-state`, { method: "POST" });
    const value = await response.json();
    if (!response.ok) throw new Error(value.detail ?? "重置失败");
    if (currentSession.current === session) setSearchState(value);
  }

  async function resetContext() {
    const session = sessionId;
    const response = await fetch(`/api/agent/conversations/${session}/context`, { method: "POST" });
    const value = await response.json();
    if (!response.ok) throw new Error(value.detail ?? "重置失败");
    if (currentSession.current === session) { setDialogue(value); setSearchState(value.conditions); }
  }

  return { sessionId, setSessionId, sessions, searchState, resetSearchState, dialogue, resetContext, runs, progress, busyRun, notifications, currentSession, newSession, execute, control, observe };
}
