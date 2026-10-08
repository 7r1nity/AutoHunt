export type AgentStatus = "idle" | "running" | "paused" | "waiting";

export type MockWorkflowSettings = {
  applicant_id: number | null;
  monitor_enabled: boolean;
  auto_submit: boolean;
  auto_submit_enabled_at: string | null;
  daily_enabled: boolean;
  daily_time: string;
  min_score: number;
  max_contacts: number;
  last_run_date: string | null;
  last_run_id: string | null;
  last_reported_run_id: string | null;
  last_error: string | null;
};

export type JobStage = "review" | "applying" | "waiting" | "interview" | "outcome_indeterminate";

export type Job = {
  id: number;
  role: string;
  company: string;
  location: string;
  salary: string;
  source: string;
  score: number;
  stage: JobStage;
  mode: "自动投递" | "人工确认" | "人工已授权" | "需要人工核验";
  tags: string[];
  reason: string;
  posted: string;
  sourceUrl?: string;
  recruiter_reply?: string | null;
  recruiter_reply_at?: string | null;
  review_approved?: boolean;
};

export type BrowserTask = {
  id: string | null;
  status: "idle" | "starting" | "running" | "paused" | "waiting_for_user" | "completed" | "failed" | "stopped";
  source: string;
  query: string;
  detail: string;
  takeover_reason: string | null;
  browser_mode: "auto" | "cdp" | "managed" | "unknown";
  selector_version: string | null;
  checkpoint: string | null;
  current_url: string | null;
  screenshot_path: string | null;
  login_evidence: {
    authenticated_signal?: boolean;
    verification_visible?: boolean;
    loading_visible?: boolean;
    login_control_visible?: boolean;
    card_count?: number;
  };
  discovered: number;
  stored: number;
  duplicates: number;
  matched: number;
  dry_run: boolean;
  dry_run_candidates: number;
  dry_run_contacted: number;
  dry_run_replied: number;
  dry_run_submitted: number;
  dry_run_waiting_reply: number;
    dry_run_failed: number;
    dry_run_report: string;
    adapter_version: string | null;
    funnel: {
      links_seen: number;
      cards_parsed: number;
      unique_jobs: number;
      limited: number;
      visited: number;
      filtered: number;
      stored: number;
      duplicates: number;
      errors: number;
      cancelled: number;
      reason_counts: Record<string, number>;
      samples: Array<{ stage: string; reason_code: string; detail: string; title?: string; company?: string; reason?: string }>;
    };
  };

export type SearchReport = {
  message?: string;
  covered_jobs: number;
  salary_distribution: Record<string, number>;
  employment_distribution: Record<string, number>;
  benefit_distribution: Record<string, number>;
  highlights: Array<{
    id: number;
    title: string;
    company: string;
    location: string;
    salary: string;
    score: number;
    employment_type: string;
    benefits: string[];
    jd_excerpt: string;
      jd_summary?: string;
      source_url?: string;
  }>;
};

export type AutomationStats = {
  discovered: number;
  stored: number;
  duplicates: number;
  matched: number;
  review: number;
  applying: number;
  waiting: number;
  interview: number;
  indeterminate: number;
  dry_run_candidates: number;
  dry_run_contacted: number;
  dry_run_replied: number;
  dry_run_submitted: number;
  dry_run_waiting_reply: number;
  dry_run_failed: number;
};

export type Insights = {
  partial_tasks?: number;
  employment_distribution?: Record<string, number>;
  benefit_distribution?: Record<string, number>;
  jd_overview?: { responsibilities: Array<{ text: string; count: number }>; requirements: Array<{ text: string; count: number }> };
  scope: "all_history" | "active_resume" | "latest_legacy_run";
  tasks_total: number;
  discovered_total: number;
  duplicates_total: number;
  stored_total: number;
  rejected: number;
  cities: { city: string; count: number }[];
  salaries: { bucket: string; count: number }[];
  tags: { tag: string; count: number }[];
  avg_score: number;
  score_high: number;
  score_good: number;
  score_fair: number;
};

export type LearningGap = {
  id: string;
  category: "technical_skill" | "internship_direction" | "domain_knowledge" | "professional_skill";
  name: string;
  priority: "high" | "medium" | "low";
  assessment: "resume_not_shown" | "evidenced_but_needs_depth";
  resume_quote: string;
  jd_quotes: Array<{ job_id: number; quote: string }>;
  frequency: number;
  rationale: string;
  objective: string;
  estimated_hours: number;
  prerequisites: string[];
  steps: string[];
  practice: string;
  acceptance: string[];
  resume_target: "skill" | "project" | "none";
  status: "todo" | "completed_user_reported";
  completion_evidence: string;
  resume_proposal_id: string | null;
};

export type LearningPlan = {
  id: string;
  resume_id: string;
  resume_version: number;
  title: string;
  summary: string;
  jobs_analyzed: number;
  job_sources: Array<{ id: number; title: string; source_url: string }>;
  model: string;
  created_at: string;
  gaps: LearningGap[];
};

  export type DeliveryTask = {
    id: string | null;
    preview_id: string | null;
    job_id: number | null;
    target_url: string | null;
    resume_document_id: string | null;
    resume_version: number | null;
    resume_digest: string | null;
    action: string | null;
  status: "idle" | "starting" | "running" | "waiting_for_user" | "waiting_for_reply" | "completed" | "failed" | "outcome_indeterminate" | "cancelled";
  detail: string;
  takeover_reason: string | null;
  current_url: string | null;
  submitted: boolean;
  recruiter_reply: string | null;
  recruiter_reply_at: string | null;
    last_checked_at: string | null;
    checkpoint: string | null;
    started_at: string | null;
    updated_at: string;
};
export type DeliveryPreview = {
  id: string;
  job_id: number;
  title: string;
  company: string;
  source_url: string;
  resume_label: string;
  resume_document_id: string;
  resume_version: number;
  resume_digest: string;
  action: "submit_resume";
  action_summary: string;
  payload_digest: string;
  expires_at: string;
};

export type Material = {
  id: number | string;
  name: string;
  type: string;
  status: "已解析" | "处理中" | "失败";
};

export type ResumeProfile = {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  uploaded_at: string;
  status: "parsed";
  extracted_characters: number;
  keywords: string[];
  role_suggestions: string[];
  suggested_query: string;
};

export type PreferenceListField = {
  values: string[];
  source: "default" | "resume" | "evidence" | "inferred" | "user_confirmed";
  confidence: number;
  updated_at: string;
};

export type CandidateState = {
  profile: {
    resume_id: string | null;
    skills: string[];
    role_suggestions: string[];
    industries: string[];
    seniority: string | null;
    years_experience: number | null;
    summary: string;
    evidence: Array<{
      id: string;
      kind: "resume" | "course" | "project" | "code" | "internship" | "experience";
      title: string;
      summary: string;
      architecture: string;
      skills: string[];
      role_suggestions: string[];
      features: string[];
      optimizations: string[];
      achievements: string[];
      source: "file" | "conversation";
      confidence: number;
      created_at: string;
    }>;
    extractor: string;
    updated_at: string;
  };
  preferences: {
    version: number;
    target_roles: PreferenceListField;
    skills: PreferenceListField;
    locations: PreferenceListField;
    search_keywords: PreferenceListField;
    excluded_keywords: PreferenceListField;
    work_modes: PreferenceListField;
    salary_min: { value: number | null; source: PreferenceListField["source"]; confidence: number; updated_at: string };
    currency: string;
    updated_at: string;
  };
  prompt_context: string;
  resume_draft: string;
};

export type ResumeIngestResult = {
  resume: ResumeProfile;
  candidate_state: CandidateState;
  pipeline: string[];
};

export type ResumeDocumentMeta = {
  source_mode: "structured" | "verbatim";
  source_filename: string;
  version?: number;
  layout?: ResumeLayoutReport | null;
};

export type ResumeLayoutReport = {
  status: "validated" | "needs_review" | "fallback" | "stale";
  source_hash: string;
  prompt_version: string;
  schema_version: string;
  provider: string;
  model: string;
  attempts: number;
  issues: string[];
  checks: string[];
  blocks: Array<{ id: string; start: number; end: number; text: string }>;
  nodes: Array<{ role: "title" | "section" | "entry" | "field" | "paragraph" | "unassigned"; refs: string[]; label: string }>;
};

export type CandidateMaterial = {
  id: string;
  filename: string;
  kind: "course" | "project" | "code";
  content_type: string;
  size: number;
  uploaded_at: string;
  status: "parsed";
  extracted_characters: number;
};

export type ResumePatchOperation = {
  id: string;
  operation: "add" | "replace" | "remove";
  path: string;
  label: string;
  old_value: unknown;
  new_value: unknown;
  evidence_ids: string[];
};

export type ResumeProposal = {
  id: string;
  document_id: string;
  document_version: number;
  instruction: string;
  reason: string;
  status: "pending" | "approved" | "partially_approved" | "rejected";
  operations: ResumePatchOperation[];
  created_at: string;
  decided_at: string | null;
};

export type MaterialIngestResult = {
  material: CandidateMaterial;
  candidate_state: CandidateState;
  pipeline: string[];
  resume_proposal: ResumeProposal | null;
};

export type DocumentIngestResult = {
  classification: {
    document_type: "resume" | "material";
    material_kind: "course" | "project" | "code" | null;
    confidence: number;
    reason: string;
    classifier: string;
  };
  document_type: "resume" | "material";
  resume?: ResumeProfile;
  material?: CandidateMaterial;
  candidate_state: CandidateState;
  resume_proposal?: ResumeProposal | null;
  pipeline: string[];
};

export type ClarifyOption = {
  intent: string;
  label: string;
};

export type Message = {
  id: string;
  author: "agent" | "user";
  text: string;
  time: string;
  action?: "confirm_login" | "open_board" | "resume_proposal" | "clarify";
  actionQuery?: string;
  actionQueries?: string[];
  actionState?: "checking" | "pending" | "started" | "completed" | "cancelled" | "failed";
  actionTaskId?: string;
  resumeProposal?: ResumeProposal;
  selectedOperationIds?: string[];
  proposalState?: "pending" | "approving" | "approved" | "rejected" | "failed";
  actionLocations?: string[];
  actionDryRun?: boolean;
  clarifyOptions?: ClarifyOption[];
};
