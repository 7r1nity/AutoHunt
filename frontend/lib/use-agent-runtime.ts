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
  const [stopping, setStopping] = useState(false);
  const [notifications, setNotifications] = useState<Array<{ sessionId: string; runId: string; event: RunEvent }>>([]);
  const currentSession = useRef(sessionId);
  const connections = useRef(new Map<string, AbortController>());
  const mounted = useRef(true);
  const pendingStart = useRef<{ session: string; cancel: boolean; ready: Promise<AgentRun> } | null>(null);
  const controls = useRef(new Map<string, Promise<AgentRun>>());
  const settlements = useRef(new Map<string, (run: AgentRun) => void>());
  const stopRequest = useRef<{ session: string; promise: Promise<void> } | null>(null);

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
        if (event.kind === "status" || event.kind === "browser" || event.kind === "delivery" || event.kind === "confirmation" || event.kind === "plan_step") {
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
    setStopping(false);
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
    const session = sessionId;
    if (pendingStart.current?.session === session || stopRequest.current?.session === session) throw new Error("当前请求正在创建或停止，请稍候");
    setBusyRun("starting");
    const start = { session, cancel: false, ready: createRun(session, kind, payload) };
    pendingStart.current = start;
    let run: AgentRun;
    try {
      run = await start.ready;
    } catch (error) {
      if (currentSession.current === session) setBusyRun(null);
      throw error;
    } finally {
      if (pendingStart.current === start) pendingStart.current = null;
    }
    if (currentSession.current === run.session_id) {
      setRuns((current) => [run, ...current]);
      setBusyRun(run.id);
    }
    const result = new Promise<Record<string, unknown>>((resolve, reject) => {
      const settle = (next: AgentRun) => {
        if (!terminalRun(next.status)) return;
        settlements.current.delete(run.id);
        if (next.status === "completed" && next.result) resolve(next.result);
        else reject(new Error(next.error ?? "任务已停止，已完成的步骤保留"));
      };
      settlements.current.set(run.id, settle);
      observe(run, (event) => {
      onEvent?.(event);
      if (event.kind === "response") {
        settlements.current.delete(run.id);
        resolve(event.data.result as Record<string, unknown>);
      }
      if (event.kind === "status" && terminalRun(String(event.data.status)) && event.data.status !== "completed") {
        settlements.current.delete(run.id);
        reject(new Error(String(event.data.detail ?? "任务已停止")));
      }
      }, (error) => { settlements.current.delete(run.id); reject(error); });
    });
    // Stop may be clicked before POST /runs has returned an ID. Do not abort
    // that POST (it could already have created a durable task); cancel by ID.
    if (start.cancel) void control(run, "cancel").catch(() => undefined);
    return result;
  }

  async function control(run: AgentRun, action: "cancel" | "resume") {
    const key = `${run.id}:${action}`;
    const existing = controls.current.get(key);
    if (existing) return existing;
    const request = (async () => {
      const response = await fetch(`/api/agent/runs/${run.id}/${action}`, { method: "POST" });
      const next = await response.json() as AgentRun & { detail?: string };
      if (!response.ok) throw new Error(next.detail ?? "任务操作失败");
      if (currentSession.current === run.session_id) {
        setRuns((current) => current.map((item) => item.id === next.id ? next : item));
        if (terminalRun(next.status)) setBusyRun((current) => current === run.id ? null : current);
      }
      settlements.current.get(run.id)?.(next);
      if (!terminalRun(next.status)) observe(next);
      void refresh(run.session_id).catch(() => undefined);
      return next;
    })();
    controls.current.set(key, request);
    try { return await request; }
    finally { if (controls.current.get(key) === request) controls.current.delete(key); }
  }

  async function interrupt(scope: "session" | "all" = "session") {
    const session = sessionId;
    const stopKey = scope === "all" ? "*" : session;
    if (stopRequest.current?.session === stopKey) return stopRequest.current.promise;
    setStopping(true);
    const promise = (async () => {
      const start = pendingStart.current?.session === session ? pendingStart.current : null;
      if (start) {
        start.cancel = true;
        // A lost create response is not proof that the backend did not create a run.
        await start.ready.catch(() => undefined);
      }
      const endpoint = `/api/agent/runs/control/stop?conversation_id=${encodeURIComponent(session)}&scope=${scope}`;
      let response = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: session, scope }) });
      let result = await response.json() as { status: string; message: string };
      if (!response.ok) throw new Error(result.message ?? "停止请求未被接受");
      const deadline = Date.now() + 30_000;
      while (result.status === "stopping" && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        response = await fetch(endpoint, { cache: "no-store" });
        result = await response.json();
        if (!response.ok) throw new Error("无法核对停止状态；后台清理仍会继续");
      }
      await refresh(session);
      if (result.status !== "stopped") throw new Error(result.message || "停止尚未确认，请核对运行记录；后台清理不会因页面断开而取消");
      if (currentSession.current === session) setBusyRun(null);
    })();
    stopRequest.current = { session: stopKey, promise };
    try { await promise; }
    finally {
      if (stopRequest.current?.promise === promise) stopRequest.current = null;
      if (currentSession.current === session) setStopping(false);
    }
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

  // Queued deliveries can outlive their completed chat run; stop must remain available.
  const canInterrupt = true;
  return { sessionId, setSessionId, sessions, searchState, resetSearchState, dialogue, resetContext, runs, progress, busyRun, stopping, canInterrupt, interrupt, notifications, currentSession, newSession, execute, control, observe };
}
