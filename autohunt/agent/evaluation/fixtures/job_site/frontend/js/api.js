// 公共 API 封装。页面同源部署，使用相对路径。
const API = '/api';
const TEST_MANAGER_EMAIL = 'autohunt-employer@example.com';

function formatSalary(job) {
  if (job.salary_text) return job.salary_text;
  // Compatibility while an already-running server still uses the old schema.
  const section = (String(job.description || '').match(/【薪资福利】([^\n]*)/) || [])[1] || '';
  if (section.includes('薪资面议')) return '面议';
  const daily = section.match(/实习日薪\s*(\d+)\s*-\s*(\d+)\s*元\/天/);
  if (daily && Number(daily[1]) > 0 && Number(daily[2]) >= Number(daily[1])) return `${daily[1]}-${daily[2]}元/天`;
  return job.salary_min === 0 && job.salary_max === 0 ? '薪资未注明' : `${job.salary_min}-${job.salary_max}K`;
}

function getIdentity() {
  return {
    userId: localStorage.getItem('userId') || '',
    userRole: localStorage.getItem('userRole') || 'applicant',
    isTestManager: localStorage.getItem('isTestManager') === 'true'
  };
}

function setIdentity(userId, userRole, userEmail = '') {
  localStorage.setItem('userId', userId);
  localStorage.setItem('userRole', userRole);
  localStorage.setItem('isTestManager', String(userRole === 'employer' && String(userEmail).toLowerCase() === TEST_MANAGER_EMAIL));
}

function clearIdentity() {
  localStorage.removeItem('userId');
  localStorage.removeItem('userRole');
  localStorage.removeItem('isTestManager');
}

function authHeaders() {
  const id = getIdentity();
  return {
    'Content-Type': 'application/json',
    'X-User-Id': id.userId,
    'X-User-Role': id.userRole
  };
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  // 如果传了 headers 也不覆盖认证头
  const auth = authHeaders();
  for (const [k, v] of Object.entries(auth)) {
    if (!headers[k]) headers[k] = v;
  }
  const resp = await fetch(API + path, Object.assign({}, options, { headers }));
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      if (data && data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  if (resp.status === 204) return null;
  return resp.json();
}

// 认证
function registerUser(role, name, email, phone = null, company_name = null) {
  return api('/auth/register', {
    method: 'POST',
    body: JSON.stringify({ role, name, email, phone, company_name })
  });
}
function requestMfa(email) {
  return api('/auth/mfa/request', { method: 'POST', body: JSON.stringify({ email }) });
}
function verifyMfa(challengeId, code) {
  const normalizedCode = String(code || '').trim();
  if (!/^\d{6}$/.test(normalizedCode)) {
    return Promise.reject(new Error('请输入页面显示的 6 位模拟验证码'));
  }
  return api('/auth/mfa/verify', {
    method: 'POST',
    body: JSON.stringify({ challenge_id: challengeId, code: normalizedCode })
  });
}

// 职位
function getJobs(params = {}) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, v);
  });
  return api(`/jobs?${qs.toString()}`);
}
function getJob(id) { return api(`/jobs/${id}`); }
function createJob(body) { return api('/jobs', { method: 'POST', body: JSON.stringify(body) }); }

// 简历
function getMyResumes() { return api('/resumes/mine'); }
function createResume(body) { return api('/resumes', { method: 'POST', body: JSON.stringify(body) }); }

// Automation may use ONLY its bound resume. Manual sends use the visible choice.
// No demo creation or silent fallback to another resume is permitted.
function selectedResumeId(available, selected) {
  const bound = window.autohuntResumeBinding;
  if (bound) {
    const identity = getIdentity();
    if (Number(identity.userId) !== bound.applicantId || identity.userRole !== 'applicant')
      throw new Error('账号与授权简历不一致');
    if (!available.some(r => r.id === bound.resumeId) || String(selected) !== String(bound.resumeId))
      throw new Error('请选择已授权的主简历，不会改用其他简历');
    return bound.resumeId;
  }
  const chosen = available.find(r => String(r.id) === String(selected));
  if (!chosen) throw new Error('请先创建并选择一份真实简历');
  return chosen.id;
}

function resumeExperienceLabel(resume) {
  const unknown = (resume.work_history || []).some(item =>
    (item.unverified_fields || []).includes('experience_years'));
  return unknown ? '未核实（以简历正文为准）' : `${resume.experience_years ?? 0} 年`;
}

// 投递
function applyJob(body) { return api('/applications', { method: 'POST', body: JSON.stringify(body) }); }
function applyJobDirect(body) { return api('/applications/direct', { method: 'POST', body: JSON.stringify(body) }); }
function getMyApplications() { return api('/applications/mine'); }
function getEmployerApplications(params = {}) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, v);
  });
  return api(`/applications?${qs.toString()}`);
}
function updateApplicationStatus(id, status) {
  return api(`/applications/${id}/status`, { method: 'PATCH', body: JSON.stringify({ status }) });
}

// 聊天
function sendMessage(body) { return api('/messages', { method: 'POST', body: JSON.stringify(body) }); }
function getMessages(withUserId, jobId = null) {
  const qs = new URLSearchParams({ with_user_id: withUserId });
  if (jobId) qs.set('job_id', jobId);
  return api(`/messages?${qs.toString()}`);
}
function getConversations() { return api('/messages/conversations'); }

// 表单
function createForm(body) { return api('/forms', { method: 'POST', body: JSON.stringify(body) }); }
function getForms() { return api('/forms'); }
function getForm(id) { return api(`/forms/${id}`); }
function submitForm(id, answers) { return api(`/forms/${id}/submit`, { method: 'POST', body: JSON.stringify({ answers }) }); }
function getFormResponses(id) { return api(`/forms/${id}/responses`); }

// 统计
function getStats() { return api('/stats/summary'); }

// 工具
function escapeHtml(str) {
  return String(str ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function toast(msg) {
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}
