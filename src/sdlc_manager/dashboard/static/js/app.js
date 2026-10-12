// ============================================================
// THEME MANAGEMENT (Dark / Light Mode)
// ============================================================
function initTheme() {
    const saved = localStorage.getItem('sdlc_theme') || 'dark';
    applyTheme(saved);
}

function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    if (theme === 'light') {
        document.body.classList.add('light-theme');
    } else {
        document.body.classList.remove('light-theme');
    }
    localStorage.setItem('sdlc_theme', theme);
    
    const sun = document.getElementById('sunIcon');
    const moon = document.getElementById('moonIcon');
    if (sun && moon) {
        if (theme === 'light') {
            sun.classList.add('hidden');
            moon.classList.remove('hidden');
        } else {
            sun.classList.remove('hidden');
            moon.classList.add('hidden');
        }
    }
}

function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    if (typeof showToast === 'function') {
        showToast(`Switched to ${next} theme`, 'info');
    }
}

window.toggleTheme = toggleTheme;
window.applyTheme = applyTheme;
window.initTheme = initTheme;

/* SDLC Manager — View routing, API wiring, and rendering */

/* ============================================================
   HOSTED MODE — LOCAL HELPER ROUTING
   When the dashboard is served from a real domain (Cloud Run),
   repo/file operations are routed to the developer's local
   helper agent on 127.0.0.1. Auth stays with the hosted origin.
   When served from localhost (developer runs the full server),
   everything behaves exactly as before.
   ============================================================ */

const HELPER_DEFAULT_URL = 'http://127.0.0.1:8765';
let helperConnected = false;

function isHostedMode() {
    return !['localhost', '127.0.0.1', ''].includes(location.hostname);
}

function helperUrl() {
    return (localStorage.getItem('sdlc_helper_url') || HELPER_DEFAULT_URL).replace(/\/$/, '');
}

function helperToken() {
    return localStorage.getItem('sdlc_helper_token') || '';
}

// All requests to the helper go through here. targetAddressSpace: 'loopback'
// is required by Chrome's Local Network Access rules — without it, an HTTPS
// page's request to 127.0.0.1 is denied before the permission prompt appears.
function helperFetch(path, opts = {}) {
    const headers = Object.assign({}, opts.headers || {}, { 'X-Helper-Token': helperToken() });
    return fetch(helperUrl() + path, Object.assign({}, opts, { headers, targetAddressSpace: 'loopback' }));
}

function apiFetch(path, opts = {}) {
    if (helperConnected && path.startsWith('/api/v1/') && !path.startsWith('/api/v1/auth')) {
        return helperFetch(path, opts);
    }
    return fetch(path, opts);
}

async function checkHelper(manual = false) {
    if (!isHostedMode()) {
        updateHelperPill('local');
        return;
    }
    try {
        const res = await helperFetch('/helper/health');
        const data = await res.json();
        if (data.status === 'ok' && data.paired) {
            const wasConnected = helperConnected;
            helperConnected = true;
            updateHelperPill('connected', data.workspace);
            if (manual) showToast('Local helper connected — repository features enabled', 'good', 3500);
            closeHelperModal();
            if (!wasConnected) refreshDashboard();
        } else if (data.status === 'ok') {
            helperConnected = false;
            updateHelperPill('unpaired');
            if (manual) showToast('Helper is running, but the pairing token does not match', 'bad', 5000);
        }
    } catch (_) {
        helperConnected = false;
        updateHelperPill('missing');
        if (manual) showToast('No local helper found at ' + helperUrl(), 'bad', 5000);
    }
}

function updateHelperPill(state, workspace) {
    const pill = document.getElementById('helperPill');
    if (!pill) return;
    if (state === 'local') { pill.classList.add('hidden'); return; }
    pill.classList.remove('hidden');
    if (state === 'connected') {
        pill.className = 'status-pill status-pill-good !text-[10.5px] !py-1 !px-2.5 cursor-pointer';
        pill.innerHTML = '<span class="pulse-dot"></span> Local helper: connected';
        pill.title = 'Repository operations run on your machine: ' + (workspace || '');
    } else if (state === 'unpaired') {
        pill.className = 'status-pill status-pill-warn !text-[10.5px] !py-1 !px-2.5 cursor-pointer';
        pill.innerHTML = 'Local helper: pairing needed';
        pill.title = 'Click to enter the pairing token shown in the helper window.';
    } else {
        pill.className = 'status-pill status-pill-bad !text-[10.5px] !py-1 !px-2.5 cursor-pointer';
        pill.innerHTML = 'Local helper: not found';
        pill.title = 'Click for one-time setup instructions.';
    }
}

function openHelperModal() {
    const urlInput = document.getElementById('helperUrlInput');
    const tokenInput = document.getElementById('helperTokenInput');
    if (urlInput) urlInput.value = helperUrl();
    if (tokenInput) tokenInput.value = helperToken();
    renderHelperSetupCmds();
    document.getElementById('helperModal')?.classList.remove('hidden');
}

function closeHelperModal() {
    document.getElementById('helperModal')?.classList.add('hidden');
}

function renderHelperSetupCmds() {
    // The commands are derived from wherever this page was actually served -
    // a hardcoded host would point every other deployment at the wrong one.
    const base = window.location.origin;
    const ps = document.getElementById('helperSetupCmd');
    if (ps) ps.innerText = `powershell -c "irm ${base}/setup.ps1 | iex"`;
    const sh = document.getElementById('helperSetupCmdSh');
    if (sh) sh.innerText = `curl -fsSL ${base}/setup.sh | bash`;
}

async function copyHelperSetupCmd(btn, elementId = 'helperSetupCmd') {
    const cmd = document.getElementById(elementId)?.innerText || '';
    if (!cmd) return;
    try {
        await navigator.clipboard.writeText(cmd);
        btn.innerText = 'Copied';
        setTimeout(() => { btn.innerText = 'Copy'; }, 2000);
    } catch {
        showToast('Could not access the clipboard — select the command and copy it manually', 'bad');
    }
}

function saveHelperSettings() {
    const url = document.getElementById('helperUrlInput')?.value.trim() || HELPER_DEFAULT_URL;
    const token = document.getElementById('helperTokenInput')?.value.trim() || '';
    localStorage.setItem('sdlc_helper_url', url);
    localStorage.setItem('sdlc_helper_token', token);
    checkHelper(true);
}

/* ---------- per-developer GitHub identity ---------- */

async function checkGithubStatus() {
    const pill = document.getElementById('ghPill');
    if (!pill) return;
    try {
        // The OAuth cookie lives on the hosted origin; local git credentials live
        // on the helper. Check both and prefer whichever is connected.
        // Both probes are independent — fire them together instead of
        // paying two sequential round trips on every check.
        const [viaHelper, origin] = await Promise.all([
            helperConnected
                ? helperFetch('/api/v1/auth/github/status').then(r => r.json()).catch(() => null)
                : Promise.resolve(null),
            fetch('/api/v1/auth/github/status').then(r => r.json()).catch(() => null),
        ]);
        const data = (origin && origin.connected) ? origin : (viaHelper && viaHelper.connected) ? viaHelper : (origin || viaHelper);
        if (!data) { pill.classList.add('hidden'); return; }

        pill.classList.remove('hidden');
        if (data.connected) {
            pill.className = 'status-pill status-pill-good !text-[10.5px] !py-1 !px-2.5';
            pill.innerHTML = `<span class="pulse-dot"></span> GitHub: ${escapeHtml(data.login || 'connected')}`;
            pill.title = data.source === 'oauth'
                ? 'Authorized via GitHub — actions run as your account'
                : 'Using your local git credentials — actions run as your account';
            pill.onclick = null;
        } else if (data.oauth_configured) {
            pill.className = 'status-pill status-pill-warn !text-[10.5px] !py-1 !px-2.5 cursor-pointer';
            pill.innerHTML = 'Connect GitHub';
            pill.title = 'One click: authorize GitHub so pull requests are created as you.';
            pill.onclick = () => { location.href = '/api/v1/auth/github/login'; };
        } else {
            pill.className = 'status-pill status-pill-bad !text-[10.5px] !py-1 !px-2.5';
            pill.innerHTML = 'GitHub: not signed in';
            pill.title = 'Sign in to GitHub with git on this machine (run one git push), or configure GitHub OAuth on the server.';
            pill.onclick = null;
        }
    } catch (_) {
        pill.classList.add('hidden');
    }
}

// Set by the Code Validator's "Commit changes" handoff; consumed by loadBranches().
let pendingBranchPair = null;

const PAGE_META = {
    dashboard: ['Dashboard', 'Repository health and platform overview'],
    validate:  ['Code Validator', 'Automated code checks — fixes reviewed by you before applied'],
    git:       ['Git & PR Studio', 'Working tree, diffs and pull-request safety'],
    terraform: ['Infrastructure Ops', 'Infrastructure plan inspection (read-only)'],
    review:    ['Architecture Review', 'Prioritized recommendations for a single file'],
    chat:      ['Ask SDLC Manager', 'Ask questions about your repository and manage workflows'],
};

/* ---------- utilities ---------- */

function escapeHtml(str) {
    const div = document.createElement('div');
    div.innerText = String(str ?? '');
    return div.innerHTML;
}

function workspace() {
    // The working directory is anchored by the backend itself: the folder the
    // local helper (or server) was started in. "." always refers to it.
    return '.';
}

function showToast(message, type = 'info', timeout = 4200) {
    const icons = {
        good: '<svg class="w-4 h-4 shrink-0 mt-0.5" style="color:var(--success)" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
        bad:  '<svg class="w-4 h-4 shrink-0 mt-0.5" style="color:var(--danger)" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
        info: '<svg class="w-4 h-4 shrink-0 mt-0.5" style="color:var(--accent)" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
    };
    const stack = document.getElementById('toastStack');
    if (!stack) return;
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `${icons[type] || icons.info}<span>${escapeHtml(message)}</span>`;
    stack.appendChild(toast);
    setTimeout(() => {
        toast.classList.add('toast-out');
        setTimeout(() => toast.remove(), 250);
    }, timeout);
}

function setBusy(btnId, isBusy, busyText = 'Running...') {
    const btn = document.getElementById(btnId);
    if (!btn) return;
    if (isBusy) {
        btn.dataset.origText = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = `<svg class="w-3.5 h-3.5 spin" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"/><path d="M12 2a10 10 0 0110 10"/></svg> <span>${escapeHtml(busyText)}</span>`;
    } else {
        btn.disabled = false;
        if (btn.dataset.origText) btn.innerHTML = btn.dataset.origText;
    }
}

function addFeedItem(agent, msg, kind = 'brand') {
    const feed = document.getElementById('activityFeed');
    if (!feed) return;
    const li = document.createElement('li');
    li.className = 'feed-item view-enter';
    const now = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    li.innerHTML = `
        <span class="feed-dot feed-dot-${kind}"></span>
        <div>
            <p class="feed-agent">${escapeHtml(agent)}</p>
            <p class="feed-msg">${escapeHtml(msg)}</p>
            <p class="feed-time">${now}</p>
        </div>`;
    feed.prepend(li);
    while (feed.children.length > 8) feed.lastElementChild.remove();
}

/* ---------- navigation ---------- */

let currentTab = 'dashboard';

function switchTab(tabId) {
    currentTab = tabId;
    const views = ['dashboard', 'validate', 'git', 'terraform', 'review', 'chat'];
    views.forEach(v => {
        const el = document.getElementById('view-' + v);
        const nav = document.getElementById('nav-' + v);
        if (el) el.classList.add('hidden');
        if (nav) nav.classList.remove('active');
    });

    const activeEl = document.getElementById('view-' + tabId);
    if (activeEl) activeEl.classList.remove('hidden');

    // The dashboard is a fixed viewport-height composition with its own inner
    // scroll areas — the page itself must never scroll there. Other screens
    // are long workflows and keep normal page scrolling.
    const vc = document.getElementById('viewContainer');
    if (vc) vc.style.overflowY = (tabId === 'dashboard') ? 'hidden' : '';

    const nav = document.getElementById('nav-' + tabId);
    if (nav) nav.classList.add('active');

    const meta = PAGE_META[tabId];
    if (meta) {
        const titleEl = document.getElementById('headerTitle') || document.getElementById('pageTitle');
        const subEl = document.getElementById('headerSubtitle') || document.getElementById('pageSubtitle');
        if (titleEl) titleEl.innerText = meta[0];
        if (subEl) subEl.innerText = meta[1];
    }

    if (tabId === 'git') loadBranches();
    if (tabId === 'validate') initValidator();
    if (tabId === 'dashboard') refreshDashboard();
}

/* ---------- dashboard ---------- */

function animateScoreRing(score) {
    const ring = document.getElementById('scoreRing');
    const label = document.getElementById('kpiScore');
    const sub = document.getElementById('scoreSubtext');
    if (!ring || !label) return;

    const circ = 188.5;
    const target = circ - (circ * score / 100);
    const start = performance.now();
    const duration = 1000;

    function step(now) {
        const t = Math.min((now - start) / duration, 1);
        const ease = 1 - Math.pow(1 - t, 3);
        ring.style.strokeDashoffset = circ - (circ - target) * ease;
        label.innerText = Math.round(score * ease);
        if (t < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);

    if (sub) {
        sub.classList.remove('value-good', 'value-warn', 'value-critical');
        if (score >= 85) {
            sub.innerText = 'Release quality';
            sub.classList.add('value-good');
        } else if (score >= 60) {
            sub.innerText = 'Needs attention';
            sub.classList.add('value-warn');
        } else {
            sub.innerText = 'Blockers present';
            sub.classList.add('value-critical');
        }
    }
}

function renderSecrets(secretsRes) {
    const value = document.getElementById('kpiSecrets');
    const note = document.getElementById('secretsSubtext');
    const chip = document.getElementById('secretsIconChip');
    if (!value) return;

    const clean = secretsRes && secretsRes.status === 'success';
    const findings = secretsRes?.findings || [];
    const count = findings.length;

    value.classList.remove('value-good', 'value-critical');

    if (!clean || count > 0) {
        value.innerText = `${count} found`;
        value.classList.add('value-critical');
        if (note) note.innerText = 'Commits blocked — click for details';
        if (chip) chip.className = 'icon-chip icon-chip-bad';
    } else {
        value.innerText = '0';
        value.classList.add('value-good');
        if (note) note.innerText = 'Scanned locally — click for details';
        if (chip) chip.className = 'icon-chip icon-chip-good';
    }
}

/* ---------- score & secrets explanation modals ---------- */

const SECRET_TYPE_HELP = {
    'GCP API Key': 'Matches the Google Cloud API key format (starts with "AIza").',
    'AWS Access Key ID': 'Matches the AWS access key format (starts with "AKIA").',
    'Generic Secret Assignment': 'A password / api key / token assigned directly in code, e.g. password = "...".',
    'Private Key Header': 'Contains a cryptographic private key block (a "BEGIN PRIVATE KEY" marker).',
    'High Entropy Token': 'A long, random-looking string. Real credentials look like statistical noise, so these are flagged — occasionally this catches a harmless hash.',
};

function maskSecretSnippet(snippet) {
    // Locate the finding without ever exposing the full value:
    // any long token keeps its first 3 and last 2 characters only.
    return (snippet || '').replace(/[A-Za-z0-9_\-/+.]{10,}/g,
        t => t.slice(0, 3) + '•'.repeat(Math.min(t.length - 5, 12)) + t.slice(-2));
}

function openScoreModal() {
    const body = document.getElementById('scoreModalBody');
    const modal = document.getElementById('scoreModal');
    if (!body || !modal) return;
    modal.classList.remove('hidden');

    if (!lastSummary) {
        body.innerHTML = '<p>No scan data yet — refresh the dashboard first.</p>';
        return;
    }
    const rows = (lastSummary.score_breakdown || []).map(item => `
        <tr>
            <td class="py-1.5 pr-3 text-[var(--ink-1)]">${escapeHtml(item.label)}
                ${item.detail ? `<p class="text-[10px] text-[var(--ink-3)] mt-0.5">${escapeHtml(item.detail)}</p>` : ''}
            </td>
            <td class="py-1.5 text-right font-mono font-bold ${item.delta >= 0 ? 'text-[var(--success)]' : 'text-[var(--danger)]'}">${item.delta >= 0 ? '+' : ''}${item.delta}</td>
        </tr>`).join('');
    const scope = lastSummary.scan_scope || {};
    body.innerHTML = `
        <p>The score is computed <b>on your machine</b> from the workspace <span class="font-mono text-[var(--ink-1)]">${escapeHtml(scope.workspace || workspace())}</span> every time the dashboard refreshes:</p>
        <table class="w-full border-collapse">
            ${rows}
            <tr class="border-t border-[var(--border-strong)]">
                <td class="py-2 font-bold text-[var(--ink-1)]">Quality score</td>
                <td class="py-2 text-right font-mono font-extrabold text-[var(--accent)]">${lastSummary.quality_score}</td>
            </tr>
        </table>
        <p class="text-[10.5px] text-[var(--ink-3)]">Notes: secret findings cap the score at 50 until removed. Schema checks that "could not verify" never reduce the score. Syntax was checked on ${scope.syntax_checked_files ?? 0} changed file(s).</p>`;
}

function closeScoreModal() {
    document.getElementById('scoreModal')?.classList.add('hidden');
}

function openSecretsModal() {
    const body = document.getElementById('secretsModalBody');
    const modal = document.getElementById('secretsModal');
    if (!body || !modal) return;
    modal.classList.remove('hidden');
    renderSecretsModalBody();
}

function renderSecretsModalBody() {
    const body = document.getElementById('secretsModalBody');
    if (!body) return;
    if (!lastSummary) {
        body.innerHTML = '<p>No scan data yet — refresh the dashboard first.</p>';
        return;
    }
    const secrets = lastSummary.secrets || {};
    const findings = secrets.findings || [];
    const scope = lastSummary.scan_scope || {};

    const scopeLine = `
        <div class="p-3 rounded-lg bg-[var(--surface-2)] border border-[var(--border)]">
            <p><b>Where this scan ran:</b> ${scope.scanned_files ?? '?'} files under
            <span class="font-mono text-[var(--ink-1)]">${escapeHtml(scope.workspace || workspace())}</span> — on your machine. No file contents leave your computer.</p>
            ${scope.excluded_files ? `<p class="text-[10.5px] text-[var(--ink-3)] mt-1">${scope.excluded_files} file(s) in excluded folder(s) (${escapeHtml((scope.excluded_dirs || []).join(', '))}) were skipped — typically test fixtures with deliberate fake secrets.</p>` : ''}
        </div>`;

    if (!findings.length) {
        body.innerHTML = scopeLine + '<p class="text-[var(--success)] font-semibold mt-2">No hardcoded secrets detected. Commits are not blocked.</p>';
        return;
    }

    body.innerHTML = scopeLine + `
        <p class="font-semibold text-[var(--ink-1)]">${findings.length} finding(s) — each shows the file, line, and a masked preview:</p>
        ${findings.map(f => `
            <div class="p-3 rounded-lg bg-[var(--surface-2)] border border-[rgba(239,68,68,0.35)]">
                <p class="font-mono text-xs text-[var(--ink-1)]">${escapeHtml(f.file)} — line ${f.line_number}</p>
                <p class="text-[10.5px] mt-1"><b class="text-[var(--danger)]">${escapeHtml(f.secret_type)}</b> — ${escapeHtml(SECRET_TYPE_HELP[f.secret_type] || f.description || '')}</p>
                <pre class="font-mono text-[10.5px] mt-1.5 p-2 rounded bg-[var(--surface)] border border-[var(--border)] overflow-x-auto">${escapeHtml(maskSecretSnippet(f.snippet))}</pre>
            </div>`).join('')}`;
}

function closeSecretsModal() {
    document.getElementById('secretsModal')?.classList.add('hidden');
}

async function rescanSecrets() {
    setBusy('rescanSecretsBtn', true, 'Scanning…');
    try {
        await refreshDashboard();
        renderSecretsModalBody();
    } finally {
        setBusy('rescanSecretsBtn', false);
    }
}

let lastSummary = null;

async function refreshDashboard(manual = false) {
    // Boot, helper-connect, and tab-switch can all request this bundle within
    // the same second — one run serves them all. Manual refresh always runs.
    const now = Date.now();
    if (!manual && window.__lastDashRefresh && now - window.__lastDashRefresh < 2000) return;
    window.__lastDashRefresh = now;

    // Independent panels start immediately — the summary await below must
    // not serialize them behind its own round trip.
    loadWorkingTreePanel();
    loadRecentCommits();
    loadRepoGovernance();
    try {
        const res = await apiFetch(`/api/v1/dashboard/summary?working_directory=${encodeURIComponent(workspace())}`);
        const data = await res.json();

        if (data.status === 'success') {
            lastSummary = data;
            animateScoreRing(data.quality_score ?? 0);
            renderSecrets(data.secrets);

            const branchEl = document.getElementById('kpiBranch');
            const branchSub = document.getElementById('branchSubtext');
            const gitLines = (data.git?.stdout || '').split('\n').filter(l => l.trim());

            if (branchEl) branchEl.innerText = data.git?.branch || 'main';
            if (branchSub) branchSub.innerText = `${gitLines.length} modified file${gitLines.length === 1 ? '' : 's'}`;

            if (manual) showToast('Workspace refreshed', 'good', 2500);
        } else if (manual) {
            showToast(data.message || 'Refresh returned an error', 'bad');
        }
    } catch (e) {
        console.error('Dashboard refresh error:', e);
        if (manual) showToast('Could not reach the platform service', 'bad');
    }
}

async function loadRepoGovernance() {
    const banner = document.getElementById('govBanner');
    if (!banner) return;
    try {
        const res = await apiFetch(`/api/v1/gate/repo-readiness?working_directory=${encodeURIComponent(workspace())}`);
        const data = await res.json();
        if (!data.lefthook) {
            banner.classList.add('hidden');
            banner.innerHTML = '';
            return;
        }

        if (data.ready) {
            banner.innerHTML = `
                <div class="p-3 rounded-lg border border-[rgba(34,197,94,0.35)] bg-[rgba(34,197,94,0.06)]">
                    <p class="text-xs text-[var(--ink-1)]">🛡 <b>Repository governance active</b></p>
                </div>`;
        } else if ((data.missing_files || []).length) {
            // Missing FILES: setup cannot create these — they must come from
            // the governance source repository.
            const fileList = data.missing_files.map(f =>
                `<li class="font-mono text-[11px]">${escapeHtml(f)}</li>`).join('');
            banner.innerHTML = `
                <div class="p-3 rounded-lg border border-[rgba(239,68,68,0.4)] bg-[rgba(239,68,68,0.06)] space-y-1.5">
                    <p class="text-xs text-[var(--ink-1)]">🛡 <b>Repository governance is incomplete — required files are missing.</b>
                    This repository's lefthook.yml references files that are not in the repository.</p>
                    <ul class="list-disc ml-5 text-[var(--danger)]">${fileList}</ul>
                </div>`;
        } else {
            // Environment gaps only: everything missing here is exactly what
            // the repository's setup script exists to install.
            const missing = [...(data.missing_tools || []),
                ...(data.commitlint_installed ? [] : ['commitlint (npm ci)']),
                ...(data.hooks_wired ? [] : ['git hooks (lefthook install)'])];
            const setupBtn = data.has_setup_script
                ? `<button type="button" class="btn-primary shrink-0" onclick="runRepoSetup(this)">Run setup now</button>` : '';
            const setupNote = data.has_setup_script
                ? `Your machine is missing prerequisites this repository's setup script can install: ${escapeHtml(missing.join(', '))}. "Run setup now" detects your OS and runs the repository's matching setup script (setup.ps1 on Windows, setup.sh otherwise). It can take several minutes.`
                : escapeHtml(data.setup_hint || 'Install the missing tools by hand, then refresh.');
            banner.innerHTML = `
                <div class="p-3 rounded-lg border border-[rgba(234,179,8,0.4)] bg-[rgba(234,179,8,0.08)] space-y-2">
                    <div class="flex items-center justify-between gap-3">
                        <p class="text-xs text-[var(--ink-1)]">🛡 <b>Repository governance detected — your environment needs one-time setup.</b>
                        Until it runs, commits and PRs here are blocked by the gate.</p>
                        ${setupBtn}
                    </div>
                    <p class="text-[10.5px] text-[var(--ink-3)]" id="govSetupStatus">${setupNote}</p>
                </div>`;
        }
        banner.classList.remove('hidden');
    } catch {
        banner.classList.add('hidden');
    }
}

async function loadRecentCommits() {
    const list = document.getElementById('recentCommitsList');
    const sub = document.getElementById('recentCommitsSub');
    if (!list) return;
    try {
        const res = await apiFetch(`/api/v1/git/recent-commits?working_directory=${encodeURIComponent(workspace())}&branch=main&limit=5`);
        const data = await res.json();
        const commits = data.commits || [];
        if (sub) sub.innerText = 'The 5 latest commits on main';
        if (!commits.length) {
            list.innerHTML = '<p class="text-xs text-[var(--ink-3)]">No commits on main yet.</p>';
            return;
        }
        list.innerHTML = commits.map(c => `
            <div class="flex items-start gap-3 py-2 px-1.5 -mx-1.5 rounded-md border-b border-[var(--border)] last:border-0 hover:bg-[rgba(148,163,184,0.06)] transition">
                <span class="commit-hash">${escapeHtml(c.hash)}</span>
                <div class="min-w-0 flex-1">
                    <p class="text-xs text-[var(--ink-1)] leading-snug truncate" title="${escapeHtml(c.message)}">${escapeHtml(c.message)}</p>
                    <p class="text-[10px] text-[var(--ink-3)] mt-0.5">${escapeHtml(c.author)} • ${escapeHtml(c.relative_time)}</p>
                </div>
            </div>
        `).join('');
    } catch {
        list.innerHTML = '<p class="text-xs text-[var(--ink-3)]">Could not load commit history.</p>';
    }
}

async function loadWorkingTreePanel() {
    const list = document.getElementById('wtPanelList');
    if (!list) return;
    try {
        const res = await apiFetch('/api/v1/validate/changed-files', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ working_directory: workspace(), base_branch: 'main' }),
        });
        const data = await res.json();
        const files = data.files || [];
        if (!files.length) {
            list.innerHTML = '<p class="text-xs text-[var(--ink-3)]">Working tree is clean — nothing uncommitted.</p>';
            return;
        }
        const dotColor = { added: 'var(--success)', modified: 'var(--accent)', deleted: 'var(--danger)' };
        const shown = files.slice(0, 8);
        list.innerHTML = shown.map(f => `
            <div class="flex items-center justify-between gap-2 py-1.5 px-1.5 -mx-1.5 rounded-md border-b border-[var(--border)] last:border-0 hover:bg-[rgba(148,163,184,0.06)] transition">
                <div class="flex items-center gap-2 min-w-0">
                    <span class="w-1.5 h-1.5 rounded-full shrink-0" style="background:${dotColor[f.change_type] || 'var(--accent)'}"></span>
                    <span class="font-mono text-[11px] text-[var(--ink-1)] truncate" title="${escapeHtml(f.file)}">${escapeHtml(f.file)}</span>
                </div>
                <span class="text-[10px] font-mono shrink-0">
                    <span style="color:var(--success)">+${f.additions ?? 0}</span>
                    <span style="color:var(--danger)">-${f.deletions ?? 0}</span>
                </span>
            </div>
        `).join('') + (files.length > shown.length
            ? `<p class="text-[10.5px] text-[var(--ink-3)] pt-2">…and ${files.length - shown.length} more — open the Code Validator to see all</p>` : '');
    } catch {
        list.innerHTML = '<p class="text-xs text-[var(--ink-3)]">Could not read the working tree.</p>';
    }
}

async function refreshAll() {
    const icon = document.getElementById('topRefreshIcon');
    icon?.classList.add('spin');
    // Spin for at least 600ms so the click always gives visible feedback,
    // and for as long as the refresh actually takes beyond that.
    const minSpin = new Promise(resolve => setTimeout(resolve, 600));
    try {
        if (currentTab === 'git') {
            await loadBranches(true);
        } else if (currentTab === 'validate') {
            await Promise.all([
                vSelectedBranch ? loadVChangedFiles() : Promise.resolve(),
                loadSchemaCacheStatus(),
            ]);
            showToast('Validator data refreshed', 'good', 2000);
        } else if (currentTab === 'review') {
            showToast('Pick a file to run a fresh review', 'info', 2500);
        } else {
            await refreshDashboard(true);
        }
    } finally {
        await minSpin;
        icon?.classList.remove('spin');
    }
}

/* ---------- code validator (staged workflow) ---------- */

let vBranches = [];
let vSelectedBranch = null;
let vChangedFiles = [];
let vSelectedFiles = new Set();
let vResults = {};
let vRejectedFixes = {};
let vValidating = false;

function initValidator() {
    if (!vBranches.length) loadVBranches(false);
    else if (vSelectedBranch) loadVChangedFiles();  // fresh changed-files list on every visit
    loadSchemaCacheStatus();
}

/* --- schema cache pill --- */

function schemaAge(sec) {
    if (sec == null) return 'fetched on demand';
    if (sec < 60) return `refreshed ${sec}s ago`;
    if (sec < 3600) return `refreshed ${Math.floor(sec / 60)}m ago`;
    return `refreshed ${Math.floor(sec / 3600)}h ago`;
}

function updateSchemaPill(cache) {
    const pill = document.getElementById('vSchemaPill');
    if (!pill) return;
    if (!cache) {
        pill.className = 'status-pill !text-[10px] !py-1 !px-2.5';
        pill.innerText = 'Schema data: status unknown';
        return;
    }
    if (!cache.available) {
        pill.className = 'status-pill status-pill-warn !text-[10px] !py-1 !px-2.5';
        pill.innerText = 'Schema checks unavailable';
        pill.title = (cache.reason || 'The schema source is not configured.') + ' Secret and syntax checks still run.';
    } else {
        pill.className = 'status-pill status-pill-good !text-[10px] !py-1 !px-2.5';
        pill.innerText = `Schema data: ${cache.cached_tables > 0 ? schemaAge(cache.newest_age_seconds) : 'live, fetched on demand'}`;
        pill.title = `${cache.cached_tables} table schema(s) cached. Cache expires after ${Math.round((cache.ttl_seconds || 900) / 60)} minutes.`;
    }
}

async function loadSchemaCacheStatus() {
    try {
        const res = await apiFetch('/api/v1/validate/schema-cache');
        const data = await res.json();
        updateSchemaPill(data.cache);
    } catch (_) {
        updateSchemaPill(null);
    }
}

async function refreshSchemaCache() {
    try {
        const res = await apiFetch('/api/v1/validate/schema-cache/refresh', { method: 'POST' });
        const data = await res.json();
        updateSchemaPill(data.cache);
        showToast(data.message || 'Schema cache cleared', 'good', 3500);
    } catch (e) {
        showToast('Could not refresh schema data: ' + e, 'bad');
    }
}

/* --- step 1: branch selection --- */

async function loadVBranches(force = false) {
    const state = document.getElementById('vBranchState');
    const icon = document.getElementById('vBranchRefreshIcon');
    if (icon) icon.classList.add('spin');
    if (state) state.innerText = 'Loading branches…';

    try {
        const res = await apiFetch(`/api/v1/git/branches?working_directory=${encodeURIComponent(workspace())}`);
        const data = await res.json();
        if (data.status === 'success' && Array.isArray(data.branches) && data.branches.length) {
            vBranches = data.branches;
            if (state) state.innerText = '';
            if (!vSelectedBranch) {
                selectVBranch(data.default_branch || 'main');
            }
            if (force) showToast('Branch list refreshed', 'good', 2000);
        } else {
            if (state) state.innerHTML = 'No branches found in this repository. <button class="v-link" onclick="loadVBranches(true)">Try again</button>';
        }
    } catch (e) {
        if (state) state.innerHTML = 'Could not load branches from the repository. <button class="v-link" onclick="loadVBranches(true)">Try again</button>';
    } finally {
        if (icon) setTimeout(() => icon.classList.remove('spin'), 300);
    }
}

function toggleVBranchPopover() {
    const pop = document.getElementById('vBranchPopover');
    if (!pop) return;
    if (!pop.classList.contains('hidden')) { pop.classList.add('hidden'); return; }
    pop.classList.remove('hidden');
    const search = document.getElementById('vBranchSearch');
    if (search) { search.value = ''; search.focus(); }
    if (!vBranches.length) loadVBranches(false);
    renderVBranchList('');
}

function closeVBranchPopover() {
    document.getElementById('vBranchPopover')?.classList.add('hidden');
}

function filterVBranches(q) { renderVBranchList(q); }

function renderVBranchList(query = '') {
    const list = document.getElementById('vBranchList');
    if (!list) return;
    const q = query.trim().toLowerCase();
    const filtered = vBranches.filter(b => !q || b.name.toLowerCase().includes(q) || (b.last_commit || '').toLowerCase().includes(q));
    if (!filtered.length) {
        list.innerHTML = '<div class="p-4 text-center text-xs text-[var(--ink-3)]">No matching branches</div>';
        return;
    }
    list.innerHTML = filtered.map(b => `
        <div class="branch-item ${b.name === vSelectedBranch ? 'selected' : ''}" onclick="selectVBranch('${escapeHtml(b.name)}')">
            <div class="truncate flex-1">
                <div class="flex items-center gap-1.5">
                    <span class="font-mono text-xs font-medium truncate">${escapeHtml(b.name)}</span>
                    ${b.is_current ? '<span class="branch-badge-curr">current</span>' : ''}
                    ${b.is_remote ? '<span class="branch-badge-remote">remote</span>' : ''}
                </div>
                <p class="text-[10px] text-[var(--ink-3)] truncate mt-0.5">${escapeHtml(b.last_commit || '')} • ${escapeHtml(b.relative_time || '')}</p>
            </div>
        </div>
    `).join('');
}

function selectVBranch(name) {
    vSelectedBranch = name;
    const label = document.getElementById('vBranchLabel');
    if (label) label.innerText = name;
    const btn = document.getElementById('vBranchBtn');
    if (btn) btn.title = name;
    closeVBranchPopover();
    resetVResults();
    loadVChangedFiles();
}

/* --- step 2: changed files --- */

function resetVResults() {
    vResults = {};
    vRejectedFixes = {};
    const step3 = document.getElementById('vStep3');
    const commit = document.getElementById('vCommitArea');
    if (step3) step3.style.display = 'none';
    if (commit) commit.style.display = 'none';
}

async function loadVChangedFiles() {
    const area = document.getElementById('vFilesArea');
    if (!area || !vSelectedBranch) return;
    area.innerHTML = '<p class="text-xs text-[var(--ink-3)]"><span class="w-2.5 h-2.5 rounded-full bg-[var(--accent)] spin inline-block mr-2"></span>Finding changes against <span class="font-mono">' + escapeHtml(vSelectedBranch) + '</span>…</p>';

    try {
        const res = await apiFetch('/api/v1/validate/changed-files', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ working_directory: workspace(), base_branch: vSelectedBranch })
        });
        const data = await res.json();
        if (!res.ok || data.status !== 'success') {
            area.innerHTML = `<p class="text-xs text-[var(--danger)]">${escapeHtml(data.detail || 'Could not read changes.')} <button class="v-link" onclick="loadVChangedFiles()">Try again</button></p>`;
            return;
        }

        vChangedFiles = data.files || [];
        vSelectedFiles = new Set(vChangedFiles.filter(f => f.validatable).map(f => f.file));

        if (!vChangedFiles.length) {
            area.innerHTML = `<div class="empty-state !py-8"><h4 class="text-sm font-semibold">No changes to validate</h4><p class="text-xs text-[var(--ink-3)] mt-1">Your local files match <span class="font-mono text-[var(--ink-1)]">${escapeHtml(vSelectedBranch)}</span>.</p></div>`;
            return;
        }
        renderVFiles();
    } catch (e) {
        area.innerHTML = `<p class="text-xs text-[var(--danger)]">Could not reach the platform service. <button class="v-link" onclick="loadVChangedFiles()">Try again</button></p>`;
    }
}

const V_CHANGE_META = {
    added:    ['A', 'git-badge-a', 'Added'],
    modified: ['M', 'git-badge-m', 'Modified'],
    deleted:  ['D', 'git-badge-d', 'Deleted'],
};

function renderVFiles() {
    const area = document.getElementById('vFilesArea');
    if (!area) return;
    const selectable = vChangedFiles.filter(f => f.validatable).length;

    const rows = vChangedFiles.map((f, i) => {
        const [letter, cls, label] = V_CHANGE_META[f.change_type] || ['?', 'git-badge-u', f.change_type];
        const checked = vSelectedFiles.has(f.file);
        return `
        <div class="vfile-row ${f.validatable ? '' : 'vfile-row-disabled'}" title="${escapeHtml(f.skip_reason || f.file)}">
            <input type="checkbox" ${checked ? 'checked' : ''} ${f.validatable ? '' : 'disabled'}
                   onchange="toggleVFile(${i}, this.checked)" class="rounded border-[var(--border-strong)] text-[var(--accent)] focus:ring-0 shrink-0">
            <span class="git-badge ${cls} shrink-0">${letter}</span>
            <span class="text-[10px] font-semibold uppercase tracking-wide w-[58px] shrink-0 ${f.change_type === 'deleted' ? 'text-[var(--danger)]' : 'text-[var(--ink-3)]'}">${label}</span>
            <span class="font-mono text-xs text-[var(--ink-1)] truncate flex-1">${escapeHtml(f.file)}</span>
            ${f.change_type !== 'deleted' ? `
                <span class="flex items-center gap-1 shrink-0 font-mono text-[10px]">
                    ${f.additions > 0 ? `<span class="file-stat-chip file-stat-add">+${f.additions}</span>` : ''}
                    ${f.deletions > 0 ? `<span class="file-stat-chip file-stat-del">-${f.deletions}</span>` : ''}
                </span>` : '<span class="text-[10px] text-[var(--ink-3)] shrink-0">nothing to validate</span>'}
        </div>`;
    }).join('');

    area.innerHTML = `
        <div class="space-y-2">
            <div class="flex flex-wrap items-center justify-between gap-2">
                <p class="text-[11px] text-[var(--ink-2)]">${vChangedFiles.length} changed file(s) vs <span class="font-mono text-[var(--ink-1)]">${escapeHtml(vSelectedBranch)}</span> — ${vSelectedFiles.size} of ${selectable} selected for validation</p>
                <div class="flex items-center gap-2">
                    <button class="btn-ghost !text-xs !py-1" onclick="setAllVFiles(true)">Select all</button>
                    <button class="btn-ghost !text-xs !py-1" onclick="setAllVFiles(false)">Deselect</button>
                    <button id="vRunBtn" class="btn-primary !text-xs" onclick="runVValidation()" ${vSelectedFiles.size ? '' : 'disabled'}>Validate ${vSelectedFiles.size} file(s)</button>
                </div>
            </div>
            <div class="space-y-1.5 max-h-[280px] overflow-y-auto pr-1">${rows}</div>
        </div>`;
}

function toggleVFile(idx, checked) {
    const f = vChangedFiles[idx];
    if (!f || !f.validatable) return;
    if (checked) vSelectedFiles.add(f.file); else vSelectedFiles.delete(f.file);
    renderVFiles();
}

function setAllVFiles(on) {
    vSelectedFiles = on ? new Set(vChangedFiles.filter(f => f.validatable).map(f => f.file)) : new Set();
    renderVFiles();
}

/* --- step 3: run validation with per-file progress --- */

function vChip(state, label) {
    const icons = {
        pass: '<svg class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>',
        fail: '<svg class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/></svg>',
        warn: '<svg class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01"/></svg>',
        na:   '<svg class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M20 12H4"/></svg>',
    };
    return `<span class="vchip vchip-${state}">${icons[state] || ''} ${escapeHtml(label)}</span>`;
}

async function runVValidation() {
    const files = Array.from(vSelectedFiles);
    if (!files.length || vValidating) return;
    vValidating = true;
    vResults = {};
    vRejectedFixes = {};

    const step3 = document.getElementById('vStep3');
    const resultsArea = document.getElementById('vResultsArea');
    const commit = document.getElementById('vCommitArea');
    if (step3) step3.style.display = '';
    if (commit) commit.style.display = 'none';
    if (resultsArea) {
        resultsArea.innerHTML = files.map(f => `
            <div class="proposal-card" id="vcard-${vCardId(f)}">
                <p class="text-xs text-[var(--ink-2)]"><span class="w-2.5 h-2.5 rounded-full bg-[var(--accent)] spin inline-block mr-2"></span>Waiting to validate <span class="font-mono">${escapeHtml(f)}</span>…</p>
            </div>`).join('');
    }
    setBusy('vRunBtn', true, 'Validating…');

    let vDone = 0;
    const validateOne = async (f) => {
        const card = document.getElementById(`vcard-${vCardId(f)}`);
        if (card) card.innerHTML = `<p class="text-xs text-[var(--ink-2)]"><span class="w-2.5 h-2.5 rounded-full bg-[var(--accent)] spin inline-block mr-2"></span>Validating <span class="font-mono">${escapeHtml(f)}</span>…</p>`;
        try {
            const res = await apiFetch('/api/v1/validate/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ working_directory: workspace(), files: [f] })
            });
            const data = await res.json();
            if (res.ok && data.results && data.results[0]) {
                vResults[f] = data.results[0];
                if (data.schema_cache) updateSchemaPill(data.schema_cache);
            } else {
                vResults[f] = { file: f, status: 'error', message: data.detail || 'Validation failed for this file.' };
            }
        } catch (e) {
            vResults[f] = { file: f, status: 'error', message: 'Validation request failed. Check that the platform service is running.' };
        }
        vDone++;
        renderVFileCard(f);
    };
    // Bounded concurrency: 4 in flight — wall time is the slowest few files,
    // not the sum of all of them, and each card still lands as it finishes.
    const queue = [...files];
    await Promise.all(Array.from({ length: Math.min(4, queue.length) }, async () => {
        while (queue.length) await validateOne(queue.shift());
    }));

    vValidating = false;
    setBusy('vRunBtn', false);
    renderVCommitArea();
    addFeedItem('Code Validator', `Validated ${files.length} changed file(s) against ${vSelectedBranch}`, 'brand');
}

function vCardId(file) {
    return file.replace(/[^a-zA-Z0-9]/g, '_');
}

function renderVFileCard(file) {
    const card = document.getElementById(`vcard-${vCardId(file)}`);
    const r = vResults[file];
    if (!card || !r) return;

    if (r.status === 'error') {
        card.innerHTML = `
            <div class="flex items-center justify-between gap-3">
                <p class="font-mono text-xs font-bold text-[var(--ink-1)] truncate">${escapeHtml(file)}</p>
                ${vChip('warn', 'Could not validate')}
            </div>
            <p class="text-xs text-[var(--ink-2)] mt-2">${escapeHtml(r.message || '')}</p>
            <button class="v-link mt-1" onclick="revalidateVFile('${escapeHtml(file)}')">Try this file again</button>`;
        return;
    }

    const secrets = r.secrets || [];
    const syntax = r.syntax || { status: 'not_applicable', errors: [] };
    const schema = r.schema || { status: 'not_applicable', issues: [], unverified: [] };
    const rejected = vRejectedFixes[file] || new Set();
    const fixes = (r.fixes || []).filter(fx => !rejected.has(fx.fix_id));

    // Layer chips
    const secretChip = secrets.length ? vChip('fail', `Secrets: ${secrets.length} found`) : vChip('pass', 'Secrets: none');
    let syntaxChip;
    if (r.status === 'blocked') syntaxChip = vChip('na', 'Syntax: not run');
    else if (syntax.status === 'failed') syntaxChip = vChip('fail', `Syntax: ${syntax.errors.length} error(s)`);
    else if (syntax.status === 'unverified') syntaxChip = vChip('warn', 'Syntax: could not verify');
    else if (syntax.status === 'not_applicable') syntaxChip = vChip('na', 'Syntax: not applicable');
    else syntaxChip = vChip('pass', 'Syntax: valid');
    let schemaChip;
    if (r.status === 'blocked') schemaChip = vChip('na', 'Schema: not run');
    else if (schema.status === 'failed' || schema.status === 'partial') schemaChip = vChip('fail', `Schema: ${schema.issues.length} issue(s)`);
    else if (schema.status === 'unverified') schemaChip = vChip('warn', 'Schema: could not verify');
    else if (schema.status === 'not_applicable') schemaChip = vChip('na', 'Schema: not applicable');
    else schemaChip = vChip('pass', 'Schema: verified');

    const overall = {
        blocked: vChip('fail', 'Blocked — secret detected'),
        failed: vChip('fail', 'Issues found'),
        review: vChip('warn', `${fixes.length} fix(es) proposed`),
        unverified: vChip('warn', 'Passed — schema could not be verified'),
        clean: vChip('pass', 'Validated'),
    }[r.status] || '';

    let body = '';

    if (secrets.length) {
        body += `<div class="v-issue-block v-issue-blocked">
            <p class="text-xs font-bold text-[var(--danger)]">This file is blocked. Remove the hardcoded secret(s) below, save the file, then validate again.</p>
            ${secrets.map(s => `<p class="text-xs text-[var(--ink-2)] mt-1.5"><b>Line ${s.line_number}</b> — ${escapeHtml(s.secret_type)}: ${escapeHtml(s.description)}<br><span class="font-mono text-[10px] text-[var(--ink-3)]">${escapeHtml(s.snippet)}</span></p>`).join('')}
        </div>`;
    }

    // Issue cards: every issue carries (or can request) a suggested fix with Accept / Reject.
    if (!r._issueIndex) {
        r._issueIndex = {};
        (syntax.errors || []).forEach((e, i) => {
            r._issueIndex[`syn${i}`] = { lineLabel: e.line ? `Line ${e.line}` : 'Syntax', line: e.line, message: e.message, fix: null, rejected: false };
        });
        (schema.issues || []).forEach((iss, i) => {
            const msg = iss.message + (iss.suggestion && !iss.fix ? ` Did you mean '${iss.suggestion}'?` : '');
            r._issueIndex[`sch${i}`] = { lineLabel: iss.line ? `Line ${iss.line}` : 'Schema', line: iss.line, message: msg, fix: iss.fix || null, rejected: false };
        });
    }
    const issueKeys = Object.keys(r._issueIndex);
    if (issueKeys.length) {
        body += `<div class="mt-3 space-y-2">
            <p class="text-[11px] font-semibold text-[var(--ink-2)]">Issues found — review each suggestion; nothing is changed until you accept</p>
            ${issueKeys.map(k => vIssueCardHtml(file, k, r._issueIndex[k])).join('')}
        </div>`;
    }

    if (schema.unverified && schema.unverified.length && r.status !== 'blocked') {
        body += `<div class="v-issue-block v-issue-unverified">
            <p class="text-[11px] font-semibold text-[var(--ink-2)]">Could not verify (does not block commit):</p>
            ${schema.unverified.map(u => `<p class="text-xs text-[var(--ink-3)] mt-1">• ${escapeHtml(u.table)} — ${escapeHtml(u.reason)}</p>`).join('')}
        </div>`;
    }

    if (fixes.length) {
        body += `<div class="mt-3 space-y-2">
            <div class="flex items-center justify-between">
                <p class="text-[11px] font-semibold text-[var(--ink-2)]">Proposed fixes — nothing is changed until you accept</p>
                ${fixes.length > 1 ? `<button class="btn-secondary !text-xs !py-1" onclick="acceptAllVFixes('${escapeHtml(file)}')">Accept all ${fixes.length}</button>` : ''}
            </div>
            ${fixes.map(fx => `
                <div class="v-fix-card">
                    <div class="flex items-start justify-between gap-3">
                        <div class="min-w-0">
                            <p class="text-xs text-[var(--ink-1)]"><b>Line ${fx.line_number}</b> — ${escapeHtml(fx.description)}</p>
                            <p class="text-[10px] text-[var(--ink-3)] mt-0.5">${escapeHtml(fx.category)} · ${escapeHtml(fx.severity)}</p>
                        </div>
                        <div class="flex items-center gap-2 shrink-0">
                            <button class="btn-secondary !text-xs !py-1 !px-2.5" onclick="rejectVFix('${escapeHtml(file)}', '${escapeHtml(fx.fix_id)}')">Reject</button>
                            <button class="btn-primary !text-xs !py-1 !px-2.5" onclick="acceptVFix('${escapeHtml(file)}', '${escapeHtml(fx.fix_id)}')">Accept</button>
                        </div>
                    </div>
                    <div class="v-fix-diff">
                        <pre class="v-diff-before">- ${escapeHtml(fx.original_code)}</pre>
                        <pre class="v-diff-after">+ ${escapeHtml(fx.proposed_code)}</pre>
                    </div>
                </div>`).join('')}
        </div>`;
    }

    card.innerHTML = `
        <div class="flex flex-wrap items-center justify-between gap-2">
            <p class="font-mono text-xs font-bold text-[var(--ink-1)] truncate">${escapeHtml(file)}</p>
            <div class="flex items-center gap-2">
                <button class="btn-secondary !text-[10.5px] !py-1 !px-2.5" id="vdiffbtn-${vCardId(file)}" onclick="toggleVDiff('${escapeHtml(file)}')">View comparison</button>
                ${overall}
            </div>
        </div>
        <div class="flex flex-wrap items-center gap-1.5 mt-2">${secretChip}${syntaxChip}${schemaChip}</div>
        <div id="vdiff-${vCardId(file)}" class="hidden mt-3"></div>
        ${body}`;
}

/* --- issue suggestions: draft, accept, reject --- */

function vIssueCardHtml(file, key, item) {
    const btnId = `vsug-${vCardId(file)}-${key}`;
    let actions = '';
    let fixBlock = '';

    if (item.fix && !item.rejected) {
        fixBlock = `
            <div class="v-fix-diff mt-2">
                <pre class="v-diff-before">- ${escapeHtml(item.fix.original_code)}</pre>
                <pre class="v-diff-after">+ ${escapeHtml(item.fix.proposed_code)}</pre>
            </div>
            <div class="flex items-center justify-between gap-2 mt-2">
                <p class="text-[10.5px] text-[var(--ink-3)]">${escapeHtml(item.fix.explanation || '')}</p>
                <div class="flex items-center gap-2 shrink-0">
                    <button class="btn-secondary !text-xs !py-1 !px-2.5" onclick="rejectVSuggestion('${escapeHtml(file)}', '${key}')">Reject</button>
                    <button class="btn-primary !text-xs !py-1 !px-2.5" onclick="acceptVSuggestion('${escapeHtml(file)}', '${key}')">Accept fix</button>
                </div>
            </div>`;
    } else {
        actions = `<button id="${btnId}" class="btn-secondary !text-xs !py-1 !px-2.5 shrink-0" onclick="suggestVFix('${escapeHtml(file)}', '${key}')">Suggest a fix</button>`;
    }

    return `
        <div class="v-fix-card">
            <div class="flex items-start justify-between gap-3">
                <p class="text-xs text-[var(--ink-1)]"><b>${escapeHtml(item.lineLabel)}</b> — ${escapeHtml(item.message)}</p>
                ${actions}
            </div>
            ${fixBlock}
            ${item.rejected ? '<p class="text-[10px] text-[var(--ink-3)] mt-1.5">Suggestion rejected — your code is unchanged. The issue still blocks the commit until it is fixed.</p>' : ''}
        </div>`;
}

async function suggestVFix(file, key) {
    const item = vResults[file]?._issueIndex?.[key];
    if (!item) return;
    const btnId = `vsug-${vCardId(file)}-${key}`;
    setBusy(btnId, true, 'Drafting…');
    try {
        const res = await apiFetch('/api/v1/validate/suggest-fix', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                working_directory: workspace(),
                file_path: file,
                line: item.line || null,
                message: item.message,
            })
        });
        const data = await res.json();
        if (res.ok && data.status === 'success' && data.fix) {
            item.fix = data.fix;
            item.rejected = false;
            renderVFileCard(file);
        } else {
            showToast(data.detail || 'A suggestion could not be drafted for this issue.', 'bad', 5000);
            setBusy(btnId, false);
        }
    } catch (e) {
        showToast('Could not draft a suggestion: ' + e, 'bad');
        setBusy(btnId, false);
    }
}

async function acceptVSuggestion(file, key) {
    const item = vResults[file]?._issueIndex?.[key];
    if (!item || !item.fix) return;
    try {
        const res = await apiFetch('/api/v1/validate/apply-suggestion', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                working_directory: workspace(),
                file_path: file,
                original_code: item.fix.original_code,
                proposed_code: item.fix.proposed_code,
            })
        });
        const data = await res.json();
        if (res.ok && data.status === 'success') {
            showToast(`Fix accepted — ${file} updated. Re-validating…`, 'good', 3000);
            addFeedItem('Code Validator', `Accepted suggested fix in ${file}`, 'good');
            await revalidateVFile(file);
        } else {
            showToast(data.detail || data.message || 'Could not apply the fix', 'bad', 6000);
        }
    } catch (e) {
        showToast('Could not apply the fix: ' + e, 'bad');
    }
}

function rejectVSuggestion(file, key) {
    const item = vResults[file]?._issueIndex?.[key];
    if (!item) return;
    item.fix = null;
    item.rejected = true;
    showToast('Suggestion rejected — your code is unchanged', 'info', 2500);
    renderVFileCard(file);
}

/* --- side-by-side comparison with issue lines highlighted --- */

function collectVIssueLines(file) {
    const r = vResults[file] || {};
    const map = {}; // line -> [messages]
    const add = (line, msg) => {
        if (!line) return;
        if (!map[line]) map[line] = [];
        map[line].push(msg);
    };
    (r.secrets || []).forEach(s => add(s.line_number, `${s.secret_type}: ${s.description}`));
    ((r.syntax || {}).errors || []).forEach(e => add(e.line, e.message));
    ((r.schema || {}).issues || []).forEach(i => add(i.line, i.message + (i.suggestion ? ` Did you mean '${i.suggestion}'?` : '')));
    return map;
}

async function toggleVDiff(file) {
    const container = document.getElementById(`vdiff-${vCardId(file)}`);
    const btn = document.getElementById(`vdiffbtn-${vCardId(file)}`);
    if (!container) return;

    if (!container.classList.contains('hidden')) {
        container.classList.add('hidden');
        if (btn) btn.innerText = 'View comparison';
        return;
    }
    container.classList.remove('hidden');
    if (btn) btn.innerText = 'Hide comparison';

    container.innerHTML = '<p class="text-xs text-[var(--ink-3)]"><span class="w-2.5 h-2.5 rounded-full bg-[var(--accent)] spin inline-block mr-2"></span>Loading comparison…</p>';
    try {
        const res = await apiFetch('/api/v1/git/file-diff', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                working_directory: workspace(),
                base_branch: vSelectedBranch,
                compare_branch: LOCAL_BRANCH,
                file_path: file,
            })
        });
        const data = await res.json();
        if (!res.ok || data.status !== 'success') {
            container.innerHTML = `<p class="text-xs text-[var(--danger)]">${escapeHtml(data.detail || 'Could not load the comparison for this file.')}</p>`;
            return;
        }
        container.innerHTML = vRenderDiffGrid(data.blocks || [], collectVIssueLines(file), data.base_exists);
    } catch (e) {
        container.innerHTML = '<p class="text-xs text-[var(--danger)]">Could not load the comparison. Check that the platform service is running.</p>';
    }
}

function vDiffLines(lines, startNo, cls, issueMap) {
    if (!lines.length) return '<div class="merge-line merge-line-empty">&nbsp;</div>';
    return lines.map((line, i) => {
        const no = startNo + i;
        const msgs = issueMap ? issueMap[no] : null;
        let html = `<div class="merge-line ${cls} ${msgs ? 'v-line-issue' : ''}"><span class="merge-lineno">${no}</span><span class="merge-code">${escapeHtml(line) || '&nbsp;'}</span></div>`;
        if (msgs) {
            html += msgs.map(m => `<div class="v-line-note">⚠ ${escapeHtml(m)}</div>`).join('');
        }
        return html;
    }).join('');
}

function vRenderDiffGrid(blocks, issueMap, baseExists) {
    const CONTEXT = 3;
    const hasIssueIn = (start, count) => {
        for (let n = start; n < start + count; n++) if (issueMap[n]) return true;
        return false;
    };

    const rows = blocks.map(b => {
        if (b.type === 'equal') {
            let leftHtml, rightHtml;
            const flagged = hasIssueIn(b.compare_start, b.compare_lines.length);
            if (!flagged && b.base_lines.length > CONTEXT * 2 + 2) {
                const hidden = b.base_lines.length - CONTEXT * 2;
                const note = `<div class="merge-collapsed">··· ${hidden} unchanged lines ···</div>`;
                leftHtml = vDiffLines(b.base_lines.slice(0, CONTEXT), b.base_start, '', null) + note +
                           vDiffLines(b.base_lines.slice(-CONTEXT), b.base_start + b.base_lines.length - CONTEXT, '', null);
                rightHtml = vDiffLines(b.compare_lines.slice(0, CONTEXT), b.compare_start, '', issueMap) + note +
                            vDiffLines(b.compare_lines.slice(-CONTEXT), b.compare_start + b.compare_lines.length - CONTEXT, '', issueMap);
            } else {
                leftHtml = vDiffLines(b.base_lines, b.base_start, '', null);
                rightHtml = vDiffLines(b.compare_lines, b.compare_start, '', issueMap);
            }
            return `<div class="merge-row"><div class="merge-cell">${leftHtml}</div><div class="merge-gutter"></div><div class="merge-cell">${rightHtml}</div></div>`;
        }
        const leftHtml = vDiffLines(b.base_lines, b.base_start, b.base_lines.length ? 'merge-line-del' : 'merge-line-empty', null);
        const rightHtml = vDiffLines(b.compare_lines, b.compare_start, b.compare_lines.length ? 'merge-line-add' : 'merge-line-empty', issueMap);
        return `<div class="merge-row merge-row-change"><div class="merge-cell">${leftHtml}</div><div class="merge-gutter"></div><div class="merge-cell">${rightHtml}</div></div>`;
    }).join('');

    return `
        <div class="merge-pane-headers">
            <div class="merge-pane-head"><span class="merge-side-tag merge-side-base">BRANCH</span> <span class="font-mono text-[11px]">${escapeHtml(vSelectedBranch)}</span>${baseExists === false ? ' <span class="text-[10px] text-[var(--ink-3)]">— file does not exist on this branch</span>' : ''}</div>
            <div></div>
            <div class="merge-pane-head"><span class="merge-side-tag merge-side-compare">YOUR LOCAL FILE</span> <span class="text-[10px] text-[var(--ink-3)]">— flagged lines are marked ⚠</span></div>
        </div>
        <div class="merge-grid">${rows}</div>`;
}

/* --- fixes: accept / reject --- */

async function acceptVFix(file, fixId) {
    try {
        const res = await apiFetch('/api/v1/validate/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ working_directory: workspace(), file_path: file, accepted_fix_ids: [fixId] })
        });
        const data = await res.json();
        if (res.ok && data.status === 'success') {
            showToast(`Fix accepted — ${escapeHtml(file)} updated`, 'good', 3000);
            addFeedItem('Code Validator', `Accepted fix ${fixId} in ${file}`, 'good');
            await revalidateVFile(file);
        } else {
            showToast(data.detail || data.message || 'Could not apply the fix', 'bad');
        }
    } catch (e) {
        showToast('Could not apply the fix: ' + e, 'bad');
    }
}

async function acceptAllVFixes(file) {
    const r = vResults[file];
    if (!r || !r.fixes) return;
    const rejected = vRejectedFixes[file] || new Set();
    const ids = r.fixes.filter(fx => !rejected.has(fx.fix_id)).map(fx => fx.fix_id);
    if (!ids.length) return;
    try {
        const res = await apiFetch('/api/v1/validate/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ working_directory: workspace(), file_path: file, accepted_fix_ids: ids })
        });
        const data = await res.json();
        if (res.ok && data.status === 'success') {
            showToast(`${data.applied_count ?? ids.length} fix(es) accepted — ${escapeHtml(file)} updated`, 'good', 3000);
            await revalidateVFile(file);
        } else {
            showToast(data.detail || data.message || 'Could not apply the fixes', 'bad');
        }
    } catch (e) {
        showToast('Could not apply the fixes: ' + e, 'bad');
    }
}

function rejectVFix(file, fixId) {
    if (!vRejectedFixes[file]) vRejectedFixes[file] = new Set();
    vRejectedFixes[file].add(fixId);
    showToast('Fix rejected — your original code is untouched', 'info', 2500);
    renderVFileCard(file);
    renderVCommitArea();
}

async function revalidateVFile(file) {
    try {
        const res = await apiFetch('/api/v1/validate/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ working_directory: workspace(), files: [file] })
        });
        const data = await res.json();
        if (res.ok && data.results && data.results[0]) {
            vResults[file] = data.results[0];
            if (data.schema_cache) updateSchemaPill(data.schema_cache);
        }
    } catch (_) { /* keep previous result */ }
    renderVFileCard(file);
    renderVCommitArea();
}

/* --- step 4: commit handoff --- */

function renderVCommitArea() {
    const area = document.getElementById('vCommitArea');
    if (!area) return;
    const entries = Object.values(vResults);
    if (!entries.length) { area.style.display = 'none'; return; }
    area.style.display = '';

    const blockers = entries.filter(r => r.status === 'blocked' || r.status === 'failed' || r.status === 'error');
    const unverifiedCount = entries.filter(r => r.status === 'unverified').length;
    const pendingFixes = entries.reduce((n, r) => {
        const rej = vRejectedFixes[r.file] || new Set();
        return n + ((r.fixes || []).filter(fx => !rej.has(fx.fix_id)).length);
    }, 0);

    if (!blockers.length) {
        area.innerHTML = `
            <div class="flex flex-wrap items-center justify-between gap-3 p-4 rounded-xl border border-[rgba(34,197,94,0.3)] bg-[rgba(34,197,94,0.06)]">
                <div class="flex items-center gap-2.5 min-w-0">
                    <svg class="w-4 h-4 text-[var(--success)] shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                    <div class="text-xs text-[var(--ink-1)]">
                        <b>Ready to commit.</b> ${entries.length} file(s) validated — no secrets, no syntax errors, no confirmed schema errors.
                        ${unverifiedCount ? `<span class="text-[var(--ink-3)]"> ${unverifiedCount} file(s) had schema references that could not be verified — this does not block.</span>` : ''}
                        ${pendingFixes ? `<span class="text-[var(--ink-3)]"> ${pendingFixes} optional fix proposal(s) remain unreviewed.</span>` : ''}
                    </div>
                </div>
                <button class="btn-primary !text-xs shrink-0" onclick="commitValidatedChanges()">Commit changes</button>
            </div>`;
    } else {
        area.innerHTML = `
            <div class="p-4 rounded-xl border border-[rgba(239,68,68,0.3)] bg-[rgba(239,68,68,0.05)] space-y-1.5">
                <div class="flex items-center gap-2.5">
                    <svg class="w-4 h-4 text-[var(--danger)] shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
                    <p class="text-xs font-bold text-[var(--ink-1)]">Commit is unavailable — ${blockers.length} file(s) still have blocking issues:</p>
                </div>
                ${blockers.map(r => `<p class="text-xs text-[var(--ink-2)] ml-6"><span class="font-mono">${escapeHtml(r.file)}</span> — ${
                    r.status === 'blocked' ? 'hardcoded secret must be removed'
                    : r.status === 'error' ? escapeHtml(r.message || 'could not be validated')
                    : 'fix the syntax/schema errors above, then validate again'}</p>`).join('')}
            </div>`;
    }
}

function commitValidatedChanges() {
    pendingBranchPair = { base: vSelectedBranch, compare: LOCAL_BRANCH };
    switchTab('git');
    showToast('Continuing in Git & PR Studio — move your validated changes to a branch and raise the PR', 'info', 5000);
}

/* ---------- git & pr studio ---------- */

function colorizeDiff(text) {
    if (!text) return '';
    return text.split('\n').map(line => {
        if (line.startsWith('+') && !line.startsWith('+++'))
            return `<span class="diff-add">${escapeHtml(line)}</span>`;
        if (line.startsWith('-') && !line.startsWith('---'))
            return `<span class="diff-del">${escapeHtml(line)}</span>`;
        if (line.startsWith('@@'))
            return `<span style="color:var(--accent)">${escapeHtml(line)}</span>`;
        return escapeHtml(line);
    }).join('\n');
}

/* ---------- infrastructure ops ---------- */

async function runTfPlan() {
    setBusy('tfPlanBtn', true, 'Planning...');
    const out = document.getElementById('tfOutput');
    if (out) out.innerText = 'Running infrastructure plan...';

    const dir = document.getElementById('tfDir')?.value.trim() || undefined;

    try {
        const res = await apiFetch('/api/v1/terraform/plan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                working_directory: workspace(),
                terraform_dir: dir
            })
        });
        const data = await res.json();
        const stdout = data.stdout || data.output || data.message || 'No output.';
        if (out) out.innerText = stdout;

        const addEl = document.getElementById('tfAdd');
        const changeEl = document.getElementById('tfChange');
        const destroyEl = document.getElementById('tfDestroy');
        if (addEl) addEl.innerText = `+${data.add || 0}`;
        if (changeEl) changeEl.innerText = `~${data.change || 0}`;
        if (destroyEl) destroyEl.innerText = `-${data.destroy || 0}`;

        const kpiTf = document.getElementById('kpiTf');
        if (kpiTf) kpiTf.innerText = `${data.add || 0} add, ${data.destroy || 0} del`;

        addFeedItem('Infrastructure', `Plan completed: +${data.add || 0} ~${data.change || 0} -${data.destroy || 0}`, 'good');
    } catch (e) {
        if (out) out.innerText = 'Plan execution failed: ' + e;
        showToast('Infrastructure plan failed', 'bad');
    } finally {
        setBusy('tfPlanBtn', false);
    }
}

/* ---------- architecture review (advisory only) ---------- */
let rvInspect = null;      // /review/inspect result for the selected file
let rvFindings = [];
let rvSummary = null;
let rvRunning = false;
let rvCoverageWarn = false;
let rvFilterPriority = 'all';
let rvFilterCategory = 'all';

const RV_PRIORITY_ORDER = { High: 0, Medium: 1, Low: 2 };

// Fast endpoints (browse/inspect) must answer quickly or explain themselves —
// never an indefinite "Loading…". 404 means the backend is an older version.
async function rvFetch(path, body, timeoutMs = 20000) {
    const res = await apiFetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(timeoutMs),
    });
    if (res.status === 404) {
        throw new Error(helperConnected
            ? 'Your local helper is running an older version. Close its window and start it again (uv run helper), then retry.'
            : 'The server is running an older version without this feature. Restart it (uv run server), wait ~30 seconds, then retry.');
    }
    return res;
}

function rvErrorText(e) {
    if (e && (e.name === 'TimeoutError' || e.name === 'AbortError')) {
        return 'No response after 20 seconds. If the server or helper was just started, it may still be warming up — wait a moment and try again.';
    }
    return e?.message || 'Could not reach the platform service. Try again.';
}

function rvHumanSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

/* --- file browser --- */

function openRvBrowser() {
    document.getElementById('rvBrowseModal')?.classList.remove('hidden');
    rvBrowse('');
}

function closeRvBrowser() {
    document.getElementById('rvBrowseModal')?.classList.add('hidden');
}

let rvBrowseCurrent = '';

async function rvBrowse(path) {
    const list = document.getElementById('rvBrowseList');
    const crumb = document.getElementById('rvBrowsePath');
    if (!list) return;
    list.innerHTML = '<p class="text-xs text-[var(--ink-3)] p-2">Loading…</p>';
    try {
        const res = await rvFetch('/api/v1/review/browse', { working_directory: workspace(), path: path });
        const data = await res.json();
        if (!res.ok) {
            list.innerHTML = `<p class="text-xs text-[var(--danger)] p-2">${escapeHtml(data.detail || 'Folder could not be read.')}</p>`;
            return;
        }
        rvBrowseCurrent = path;
        const atRoot = !path || path === '.';
        if (crumb) crumb.innerText = atRoot ? 'Workspace root' : data.path;
        const upBtn = document.getElementById('rvBrowseUpBtn');
        if (upBtn) {
            upBtn.disabled = atRoot;
            upBtn.title = atRoot ? 'Already at the top of the workspace' : 'Up one folder';
        }

        const dirRows = data.dirs.map(d => `
            <button type="button" class="rv-browse-row" onclick="rvBrowse('${escapeHtml(d.path)}')">
                <svg class="w-3.5 h-3.5 text-[var(--warning)] shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/></svg>
                <span class="truncate flex-1 text-left">${escapeHtml(d.name)}</span>
            </button>`).join('');
        const fileRows = data.files.map(f => `
            <button type="button" class="rv-browse-row" onclick="rvPickFile('${escapeHtml(f.path)}')">
                <svg class="w-3.5 h-3.5 text-[var(--ink-3)] shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
                <span class="truncate flex-1 text-left">${escapeHtml(f.name)}</span>
                <span class="text-[10px] text-[var(--ink-3)] shrink-0">${escapeHtml(f.type)} · ${rvHumanSize(f.size)}</span>
            </button>`).join('');
        list.innerHTML = dirRows + fileRows ||
            '<p class="text-xs text-[var(--ink-3)] p-2">No folders or reviewable files here.</p>';
    } catch (e) {
        list.innerHTML = `<p class="text-xs text-[var(--danger)] p-2">${escapeHtml(rvErrorText(e))}</p>
            <button type="button" class="v-link ml-2" onclick="rvBrowse('${escapeHtml(path)}')">Try again</button>`;
    }
}

function rvBrowseUp() {
    if (!rvBrowseCurrent) return;
    const parts = rvBrowseCurrent.split('/');
    parts.pop();
    rvBrowse(parts.join('/'));
}

function rvPickFile(path) {
    const input = document.getElementById('rvFilePath');
    if (input) input.value = path;
    closeRvBrowser();
    inspectReviewFile();
}

/* --- file inspection (validate BEFORE running) --- */

async function inspectReviewFile() {
    const filePath = document.getElementById('rvFilePath')?.value.trim();
    const area = document.getElementById('rvFileArea');
    if (!area) return;
    if (!filePath) {
        showToast('Enter or browse to a file first', 'bad');
        return;
    }
    rvInspect = null;
    rvResetResults();
    area.innerHTML = '<p class="text-xs text-[var(--ink-3)]"><span class="w-2.5 h-2.5 rounded-full bg-[var(--accent)] spin inline-block mr-2"></span>Checking the file…</p>';

    try {
        const res = await rvFetch('/api/v1/review/inspect', { working_directory: workspace(), file_path: filePath });
        const data = await res.json();
        if (!res.ok) {
            area.innerHTML = `
                <div class="p-3.5 rounded-xl border border-[rgba(239,68,68,0.35)] bg-[rgba(239,68,68,0.05)]">
                    <p class="text-xs font-semibold text-[var(--danger)]">This file can't be reviewed</p>
                    <p class="text-xs text-[var(--ink-2)] mt-1">${escapeHtml(data.detail || 'The path could not be used.')}</p>
                </div>`;
            return;
        }
        rvInspect = data;
        area.innerHTML = `
            <div class="flex flex-wrap items-center justify-between gap-3 p-3.5 rounded-xl bg-[var(--surface-2)] border border-[var(--border-strong)]">
                <div class="min-w-0">
                    <p class="font-mono text-xs font-bold text-[var(--ink-1)] truncate">${escapeHtml(data.file_path)}</p>
                    <p class="text-[11px] text-[var(--ink-3)] mt-0.5">${escapeHtml(data.type)} · ${rvHumanSize(data.size_bytes)} · ${data.line_count} lines
                        ${data.chunked ? ` · <span class="text-[var(--warning)]">large file — will be reviewed in ${data.chunks.length} sections (the whole file is analyzed)</span>` : ''}
                    </p>
                </div>
                <button type="button" id="rvRunBtn" class="btn-primary" onclick="runArchReview()">
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z"/></svg>
                    Run review
                </button>
            </div>`;
        const results = document.getElementById('rvResultsArea');
        if (results) results.innerHTML = '<p class="text-xs text-[var(--ink-3)] text-center py-6">File confirmed — press <b>Run review</b> to analyze it. A deep review typically takes 30–90 seconds; you\'ll see progress while it runs.</p>';
    } catch (e) {
        area.innerHTML = `<p class="text-xs text-[var(--danger)]">${escapeHtml(rvErrorText(e))}</p>`;
    }
}

function rvResetResults() {
    rvFindings = [];
    rvCoverageWarn = false;
    rvSummary = null;
    rvFilterPriority = 'all';
    rvFilterCategory = 'all';
    const progress = document.getElementById('rvProgressArea');
    if (progress) { progress.classList.add('hidden'); progress.innerHTML = ''; }
    const results = document.getElementById('rvResultsArea');
    if (results) results.innerHTML = '';
}

/* --- run the review with real progress --- */

async function runArchReview() {
    if (!rvInspect || rvRunning) return;
    rvRunning = true;
    rvResetResults();
    setBusy('rvRunBtn', true, 'Reviewing…');

    const chunks = rvInspect.chunks;
    const progress = document.getElementById('rvProgressArea');
    if (progress) progress.classList.remove('hidden');

    const renderProgress = (done) => {
        if (!progress) return;
        const pct = Math.round((done / chunks.length) * 100);
        progress.innerHTML = `
            <div class="p-3.5 rounded-xl bg-[var(--surface-2)] border border-[var(--border)] space-y-2">
                <div class="flex items-center justify-between text-xs text-[var(--ink-2)]">
                    <span><span class="w-2.5 h-2.5 rounded-full bg-[var(--accent)] spin inline-block mr-2"></span>
                        Reading the complete file — ${chunks.length > 1 ? `section ${Math.min(done + 1, chunks.length)} of ${chunks.length}` : 'analyzing'}…</span>
                </div>
                <div class="rv-bar"><div class="rv-bar-fill" style="width:${Math.max(pct, 4)}%"></div></div>
            </div>`;
    };
    renderProgress(0);

    try {
        // Two chunks in flight: halves the wall time of large-file reviews
        // while staying inside the AI endpoint's rate limit. Findings are
        // collected per-chunk and ordered after completion.
        const chunkFindings = new Array(chunks.length);
        let done = 0;
        const reviewOne = async (i) => {
            const res = await apiFetch('/api/v1/review/run', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    working_directory: workspace(),
                    file_path: rvInspect.file_path,
                    line_start: chunks[i].line_start,
                    line_end: chunks[i].line_end,
                    chunk_index: i + 1,
                    chunk_count: chunks.length,
                })
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'The review failed.');
            chunkFindings[i] = data.findings || [];
            if (data.coverage_warning) rvCoverageWarn = true;
            if (!rvSummary && data.summary && data.summary.purpose) rvSummary = data.summary;
            done++;
            renderProgress(done);
        };
        const rvQueue = chunks.map((_, i) => i);
        await Promise.all(Array.from({ length: Math.min(2, rvQueue.length) }, async () => {
            while (rvQueue.length) await reviewOne(rvQueue.shift());
        }));
        for (const list of chunkFindings) if (list) rvFindings.push(...list);
        renderRvResults();
        addFeedItem('Architecture Review', `Reviewed ${rvInspect.file_path}: ${rvFindings.length} recommendation(s)`, 'brand');
    } catch (e) {
        const results = document.getElementById('rvResultsArea');
        if (results) {
            results.innerHTML = `
                <div class="p-4 rounded-xl border border-[rgba(239,68,68,0.35)] bg-[rgba(239,68,68,0.05)] space-y-2">
                    <p class="text-xs font-semibold text-[var(--danger)]">The review could not be completed</p>
                    <p class="text-xs text-[var(--ink-2)]">${escapeHtml(e.message || String(e))}</p>
                    <button type="button" class="btn-secondary !text-xs" onclick="runArchReview()">Try again</button>
                </div>`;
        }
    } finally {
        if (progress) { progress.classList.add('hidden'); progress.innerHTML = ''; }
        setBusy('rvRunBtn', false);
        rvRunning = false;
    }
}

/* --- results rendering --- */

function rvPriorityTag(priority) {
    const map = {
        High:   ['rv-pri-high',   '<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M12 4l9 16H3z"/></svg>'],
        Medium: ['rv-pri-medium', '<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><rect x="5" y="10" width="14" height="5" rx="1"/></svg>'],
        Low:    ['rv-pri-low',    '<svg class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><circle cx="12" cy="12" r="7"/></svg>'],
    };
    const [cls, icon] = map[priority] || map.Medium;
    return `<span class="rv-pri ${cls}">${icon} ${priority}</span>`;
}

function renderRvResults() {
    const results = document.getElementById('rvResultsArea');
    if (!results) return;

    const counts = { High: 0, Medium: 0, Low: 0 };
    rvFindings.forEach(f => { counts[f.priority] = (counts[f.priority] || 0) + 1; });

    const coverageNote = rvCoverageWarn
        ? `<p class="text-[10.5px] text-[var(--warning)] mt-2">Findings cluster near the start of the file — re-run the review to confirm the later portion was fully covered.</p>` : '';
    const chunkNote = rvInspect.chunked
        ? `<p class="text-[10.5px] text-[var(--warning)] mt-2">Large file: analyzed completely, in ${rvInspect.chunks.length} sections.</p>` : '';

    const summaryCard = `
        <div class="p-4 rounded-xl bg-[var(--surface-2)] border border-[var(--border-strong)] space-y-2">
            <div class="flex flex-wrap items-start justify-between gap-3">
                <div class="min-w-0 flex-1">
                    <p class="text-[10.5px] font-semibold uppercase tracking-wider text-[var(--ink-3)]">Summary</p>
                    ${rvSummary?.purpose ? `<p class="text-xs text-[var(--ink-1)] mt-1.5 leading-relaxed">${escapeHtml(rvSummary.purpose)}</p>` : ''}
                    ${rvSummary?.condition ? `<p class="text-xs text-[var(--ink-2)] mt-1 leading-relaxed">${escapeHtml(rvSummary.condition)}</p>` : ''}
                    ${chunkNote}${coverageNote}
                </div>
            </div>
        </div>`;

    if (!rvFindings.length) {
        results.innerHTML = summaryCard + `
            <div class="empty-state !py-10 mt-3">
                <div class="empty-icon !text-[var(--success)] !bg-[rgba(34,197,94,0.08)] !border-[rgba(34,197,94,0.2)]">
                    <svg class="w-6 h-6" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                </div>
                <h4>This file is in good shape</h4>
                <p>The complete file was reviewed across correctness, performance, structure, standards,<br>security, and documentation — no significant recommendations came back.</p>
            </div>`;
        return;
    }

    const categories = [...new Set(rvFindings.map(f => f.category))];
    results.innerHTML = `
        ${summaryCard}
        <div class="flex flex-wrap items-center justify-between gap-2 mt-4">
            <div class="flex items-center gap-1.5 flex-wrap" id="rvPriFilters">
                <button class="rv-filter ${rvFilterPriority === 'all' ? 'active' : ''}" onclick="rvSetPriority('all')">All (${rvFindings.length})</button>
                <button class="rv-filter ${rvFilterPriority === 'High' ? 'active' : ''}" onclick="rvSetPriority('High')">High (${counts.High})</button>
                <button class="rv-filter ${rvFilterPriority === 'Medium' ? 'active' : ''}" onclick="rvSetPriority('Medium')">Medium (${counts.Medium})</button>
                <button class="rv-filter ${rvFilterPriority === 'Low' ? 'active' : ''}" onclick="rvSetPriority('Low')">Low (${counts.Low})</button>
            </div>
            <label class="flex items-center gap-2 text-[11px] text-[var(--ink-3)]">Filter by category<select id="rvCategoryFilter" onchange="rvSetCategory(this.value)" class="bg-[var(--surface)] border border-[var(--border-strong)] rounded-lg px-2.5 py-1.5 text-xs text-[var(--ink-1)] outline-none">
                <option value="all">All categories</option>
                ${categories.map(c => `<option value="${escapeHtml(c)}" ${rvFilterCategory === c ? 'selected' : ''}>${escapeHtml(c)}</option>`).join('')}
            </select></label>
        </div>
        <p class="text-[10.5px] text-[var(--ink-3)] mt-1.5">Priority reflects how serious the issue is if left unaddressed — each item explains why.</p>
        <div class="space-y-2 mt-3" id="rvFindingsList"></div>`;
    renderRvFindingsList();
}

function rvSetPriority(p) { rvFilterPriority = p; renderRvResults(); }
function rvSetCategory(c) { rvFilterCategory = c; renderRvFindingsList(); }

function renderRvFindingsList() {
    const list = document.getElementById('rvFindingsList');
    if (!list) return;
    const filtered = rvFindings
        .filter(f => rvFilterPriority === 'all' || f.priority === rvFilterPriority)
        .filter(f => rvFilterCategory === 'all' || f.category === rvFilterCategory)
        .sort((a, b) => (RV_PRIORITY_ORDER[a.priority] - RV_PRIORITY_ORDER[b.priority]) || (a.line_start - b.line_start));

    if (!filtered.length) {
        list.innerHTML = '<p class="text-xs text-[var(--ink-3)] text-center py-4">No recommendations match the current filters.</p>';
        return;
    }

    list.innerHTML = filtered.map(f => {
        const lines = f.line_start === f.line_end ? `Line ${f.line_start}` : `Lines ${f.line_start}–${f.line_end}`;
        return `
        <div class="rv-finding">
            <button type="button" class="rv-finding-head" onclick="this.parentElement.classList.toggle('open')">
                ${rvPriorityTag(f.priority)}
                <span class="rv-cat">${escapeHtml(f.category)}</span>
                <span class="font-mono text-[10.5px] text-[var(--ink-3)] shrink-0">${lines}</span>
                <span class="text-xs text-[var(--ink-1)] truncate flex-1 text-left">${escapeHtml(f.summary)}</span>
                <svg class="w-3.5 h-3.5 text-[var(--ink-3)] shrink-0 rv-chev" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"/></svg>
            </button>
            <div class="rv-detail">
                <p class="text-[11px] font-semibold text-[var(--ink-2)]">Why it matters</p>
                <p class="text-xs text-[var(--ink-1)] leading-relaxed mt-1">${escapeHtml(f.why)}</p>
                <p class="text-[11px] font-semibold text-[var(--ink-2)] mt-3">Recommendation</p>
                <pre class="rv-reco">${escapeHtml(f.recommendation)}</pre>
            </div>
        </div>`;
    }).join('');
}

/* ---------- assistant chat ---------- */

// One conversation = one agent session. The id is sent with every message so
// the agent keeps context across turns; "New chat" rotates it for a clean slate.
let chatSessionId = `web-${crypto.randomUUID()}`;
let chatGreetingHtml = null;

function startNewChat() {
    chatSessionId = `web-${crypto.randomUUID()}`;
    const msgs = document.getElementById('chatMessages');
    if (msgs && chatGreetingHtml !== null) msgs.innerHTML = chatGreetingHtml;
    const input = document.getElementById('chatInput');
    if (input) { input.value = ''; input.focus(); }
    showToast('New chat started', 'info', 2500);
}

function appendChatRow(isUser) {
    const msgs = document.getElementById('chatMessages');
    if (!msgs) return null;

    const row = document.createElement('div');
    row.className = `chat-row ${isUser ? 'chat-row-user' : ''}`;

    const avatar = document.createElement('div');
    avatar.className = `chat-avatar ${isUser ? 'chat-avatar-user' : 'chat-avatar-bot'}`;
    avatar.innerHTML = isUser ? 'You' : '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>';

    const bubble = document.createElement('div');
    bubble.className = `chat-bubble ${isUser ? 'chat-bubble-user' : 'chat-bubble-bot'}`;

    if (!isUser) {
        const author = document.createElement('p');
        author.className = 'chat-author';
        author.innerText = 'SDLC Manager';
        bubble.appendChild(author);
    }

    const body = document.createElement('div');
    bubble.appendChild(body);

    row.appendChild(avatar);
    row.appendChild(bubble);
    msgs.appendChild(row);
    msgs.scrollTop = msgs.scrollHeight;

    return body;
}

function useSuggestion(btn) {
    const input = document.getElementById('chatInput');
    if (input) {
        input.value = btn.innerText;
        sendChatMessage();
    }
}

// Minimal markdown renderer for agent replies: bold, inline code, code
// fences, bullets, and headings. Input is HTML-escaped before any markup is
// added, so model output can never inject live HTML.
function renderChatMarkdown(raw) {
    const lines = escapeHtml(raw).split('\n');
    const out = [];
    let ulOpen = false, olOpen = false, inCode = false, codeBuf = [], tableBuf = [];
    const closeLists = () => {
        if (ulOpen) { out.push('</ul>'); ulOpen = false; }
        if (olOpen) { out.push('</ol>'); olOpen = false; }
    };
    const flushTable = () => {
        if (!tableBuf.length) return;
        const rows = tableBuf.filter(r => !/^\s*\|?[\s:|-]+\|?\s*$/.test(r));
        const cells = r => r.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
        if (rows.length) {
            const [head, ...body] = rows;
            out.push('<table class="chat-table"><thead><tr>'
                + cells(head).map(c => `<th>${c}</th>`).join('') + '</tr></thead><tbody>'
                + body.map(r => '<tr>' + cells(r).map(c => `<td>${c}</td>`).join('') + '</tr>').join('')
                + '</tbody></table>');
        }
        tableBuf = [];
    };
    for (const line of lines) {
        if (line.trim().startsWith('```')) {
            if (inCode) { out.push(`<pre class="chat-code">${codeBuf.join('\n')}</pre>`); codeBuf = []; }
            else { closeLists(); flushTable(); }
            inCode = !inCode;
            continue;
        }
        if (inCode) { codeBuf.push(line); continue; }
        const html = line
            .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
            .replace(/`([^`]+)`/g, '<code>$1</code>');
        if (/^\s*\|.*\|\s*$/.test(html)) { closeLists(); tableBuf.push(html); continue; }
        flushTable();
        const bullet = html.match(/^\s*[*•-]\s+(.*)$/);
        const numbered = html.match(/^\s*\d+[.)]\s+(.*)$/);
        const heading = html.match(/^\s*#{1,4}\s+(.*)$/);
        if (bullet) {
            if (olOpen) { out.push('</ol>'); olOpen = false; }
            if (!ulOpen) { out.push('<ul>'); ulOpen = true; }
            out.push(`<li>${bullet[1]}</li>`);
        } else if (numbered) {
            if (ulOpen) { out.push('</ul>'); ulOpen = false; }
            if (!olOpen) { out.push('<ol>'); olOpen = true; }
            out.push(`<li>${numbered[1]}</li>`);
        } else if (heading) {
            closeLists();
            out.push(`<p class="chat-h">${heading[1]}</p>`);
        } else {
            closeLists();
            out.push(html.trim() ? `<p>${html}</p>` : '');
        }
    }
    if (inCode) out.push(`<pre class="chat-code">${codeBuf.join('\n')}</pre>`);
    closeLists();
    flushTable();
    return out.join('');
}

async function sendChatMessage() {
    const input = document.getElementById('chatInput');
    if (!input) return;
    const msg = input.value.trim();
    if (!msg) return;

    // Snapshot the pristine greeting once, so "New chat" can restore it.
    if (chatGreetingHtml === null) {
        chatGreetingHtml = document.getElementById('chatMessages')?.innerHTML ?? '';
    }

    input.value = '';
    const userBody = appendChatRow(true);
    if (userBody) userBody.innerText = msg;

    const botBody = appendChatRow(false);
    if (!botBody) return;

    botBody.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>';
    const container = document.getElementById('chatMessages');

    try {
        const response = await apiFetch('/api/v1/agent/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: msg, working_directory: workspace(), session_id: chatSessionId })
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let fullText = '';
        let textEl = null;
        // Carry-over buffer: an SSE event can be split across chunks,
        // and multi-byte UTF-8 can split too ({stream: true} handles it).
        let sseBuffer = '';

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;

            sseBuffer += decoder.decode(value, { stream: true });
            const lines = sseBuffer.split('\n');
            sseBuffer = lines.pop() ?? '';
            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                try {
                    const data = JSON.parse(line.substring(6));
                    if (data.type === 'token') {
                        if (!textEl) {
                            // Text can arrive in segments (split by tool
                            // activity); each segment gets its own block.
                            botBody.querySelector('.typing-dots')?.remove();
                            textEl = document.createElement('div');
                            textEl.className = 'stream-text chat-md';
                            botBody.appendChild(textEl);
                            fullText = '';
                        }
                        fullText += data.text;
                        textEl.innerHTML = renderChatMarkdown(fullText);
                    } else if (data.type === 'tool_call') {
                        // Tool activity is intentionally not shown in chat —
                        // the typing indicator stays until the answer streams.
                        textEl = null;
                    } else if (data.type === 'error') {
                        botBody.querySelector('.typing-dots')?.remove();
                        const err = document.createElement('div');
                        err.className = 'stream-text chat-md';
                        err.innerText = data.message || 'The agent run failed.';
                        botBody.appendChild(err);
                        textEl = null;
                    }
                } catch (_) { /* ignore parse errors */ }
            }
            if (container) container.scrollTop = container.scrollHeight;
        }

        if (!fullText) {
            botBody.innerText = 'The turn was completed.';
        }
    } catch (e) {
        botBody.innerText = 'Unable to complete request: ' + e;
        showToast('Chat request failed', 'bad');
    }
}

/* ---------- boot ---------- */

async function loadUser() {
    try {
        const res = await apiFetch('/api/v1/auth/me');
        if (!res.ok) return;
        const data = await res.json();
        const user = data.user || data;
        if (user?.name) {
            const nameEl = document.getElementById('userName');
            const initEl = document.getElementById('userInitial');
            if (nameEl) nameEl.innerText = user.name;
            if (initEl) initEl.innerText = user.name.charAt(0).toUpperCase();
        }
        if (user?.email) {
            const emailEl = document.getElementById('userEmail');
            if (emailEl) emailEl.innerText = user.email;
        }
    } catch (_) { /* default client profile configured in HTML */ }
}

document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    // The dashboard tab is the landing view — suppress page scroll for it from the start.
    const vc = document.getElementById('viewContainer');
    if (vc) vc.style.overflowY = 'hidden';
    loadUser();
    checkHelper().then(() => checkGithubStatus());

    // Returning from the one-time GitHub authorization
    if (new URLSearchParams(location.search).get('github') === 'connected') {
        showToast('GitHub connected — pull requests will be created as you', 'good', 5000);
        history.replaceState(null, '', location.pathname);
    }
    refreshDashboard();

    // In hosted mode, keep looking for the helper until it appears (e.g. user just started it).
    if (isHostedMode()) {
        setInterval(() => { if (!helperConnected) checkHelper(); }, 15000);
    }

    const tab = new URLSearchParams(location.search).get('tab');
    if (tab && PAGE_META[tab]) switchTab(tab);
});



/* ---------- Compare & Create PR Studio ---------- */

let repoBranches = [];
let selectedBaseBranch = 'main';
let selectedCompareBranch = 'main';
let activePopoverTarget = null;
let currentComparison = null;

async function loadBranches(forceRefresh = false) {
    const icon = document.getElementById('branchRefreshIcon');
    if (icon) icon.classList.add('spin');

    try {
        const res = await apiFetch(`/api/v1/git/branches?working_directory=${encodeURIComponent(workspace())}`);
        const data = await res.json();

        if (data.status === 'success' && Array.isArray(data.branches)) {
            repoBranches = data.branches;
            
            const defBranch = data.default_branch || 'main';
            // Find a feature branch that is different from default branch
            const otherBranches = data.branches.filter(b => b.name !== defBranch);
            const autoCompare = otherBranches.length > 0 ? otherBranches[0].name : defBranch;

            selectedBaseBranch = defBranch;
            selectedCompareBranch = autoCompare;

            // Handoff from the Code Validator ("Commit changes") overrides the defaults.
            if (pendingBranchPair) {
                selectedBaseBranch = pendingBranchPair.base || defBranch;
                selectedCompareBranch = pendingBranchPair.compare || autoCompare;
                pendingBranchPair = null;
            }

            updateBranchLabels();
            runBranchComparison();

            if (forceRefresh) showToast('Branches synchronized with remote', 'good', 2000);
        } else {
            showToast('Could not load repository branches', 'bad');
        }
    } catch (e) {
        console.error('Branch load error:', e);
        showToast('Error loading branches: ' + e, 'bad');
    } finally {
        if (icon) setTimeout(() => icon.classList.remove('spin'), 300);
    }
}

// Pseudo-branch representing uncommitted working-tree changes (matches backend sentinel).
const LOCAL_BRANCH = '__local__';

function branchDisplay(name) {
    return name === LOCAL_BRANCH ? 'Local changes' : name;
}

function updateBranchLabels() {
    const baseLabel = document.getElementById('baseBranchLabel');
    const compareLabel = document.getElementById('compareBranchLabel');
    if (baseLabel) baseLabel.innerText = branchDisplay(selectedBaseBranch);
    if (compareLabel) compareLabel.innerText = branchDisplay(selectedCompareBranch);

    // Full name on hover in case a long branch name still truncates
    const baseBtn = document.getElementById('baseBranchBtn');
    const compareBtn = document.getElementById('compareBranchBtn');
    if (baseBtn) baseBtn.title = branchDisplay(selectedBaseBranch);
    if (compareBtn) compareBtn.title = branchDisplay(selectedCompareBranch);

    const identBase = document.getElementById('identicalBaseName');
    const identCompare = document.getElementById('identicalCompareName');
    if (identBase) identBase.innerText = branchDisplay(selectedBaseBranch);
    if (identCompare) identCompare.innerText = branchDisplay(selectedCompareBranch);

    const mergeBase = document.getElementById('mergeBaseLabel');
    const mergeCompare = document.getElementById('mergeCompareLabel');
    const paneBase = document.getElementById('mergePaneBaseName');
    const paneCompare = document.getElementById('mergePaneCompareName');
    if (mergeBase) mergeBase.innerText = branchDisplay(selectedBaseBranch);
    if (mergeCompare) mergeCompare.innerText = branchDisplay(selectedCompareBranch);
    if (paneBase) paneBase.innerText = branchDisplay(selectedBaseBranch);
    if (paneCompare) paneCompare.innerText = branchDisplay(selectedCompareBranch);
}

function toggleBranchPopover(target) {
    const popover = document.getElementById('branchPopover');
    if (!popover) return;

    if (activePopoverTarget === target && !popover.classList.contains('hidden')) {
        closeBranchPopover();
        return;
    }

    activePopoverTarget = target;
    popover.classList.remove('hidden');

    const searchInput = document.getElementById('branchSearchInput');
    if (searchInput) {
        searchInput.value = '';
        searchInput.focus();
    }

    if (!repoBranches.length) {
        loadBranches();
    } else {
        renderBranchPopoverList('');
    }
}

function closeBranchPopover() {
    const popover = document.getElementById('branchPopover');
    if (popover) popover.classList.add('hidden');
    activePopoverTarget = null;
}

function filterBranches(query) {
    renderBranchPopoverList(query);
}

function renderBranchPopoverList(query = '') {
    const list = document.getElementById('branchPopoverList');
    if (!list) return;

    const q = query.trim().toLowerCase();
    const currentSelected = (activePopoverTarget === 'base') ? selectedBaseBranch : selectedCompareBranch;

    const filtered = repoBranches.filter(b =>
        !q || b.name.toLowerCase().includes(q) || (b.last_commit || '').toLowerCase().includes(q)
    );

    // Offer the working tree as a "From" option so local edits can be compared against any branch.
    let specialItem = '';
    if (activePopoverTarget === 'compare' && (!q || 'local changes'.includes(q))) {
        const isSelected = selectedCompareBranch === LOCAL_BRANCH;
        specialItem = `
            <div class="branch-item ${isSelected ? 'selected' : ''}" onclick="selectBranch('${LOCAL_BRANCH}')">
                <div class="truncate flex-1">
                    <div class="flex items-center gap-1.5">
                        <span class="font-mono text-xs font-medium truncate">Local changes</span>
                        <span class="branch-badge-curr">working tree</span>
                    </div>
                    <p class="text-[10px] text-[var(--ink-3)] truncate mt-0.5">Your uncommitted edits, compared live against the "To" branch</p>
                </div>
                ${isSelected ? '<svg class="w-3.5 h-3.5 text-[var(--accent)] shrink-0" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>' : ''}
            </div>
        `;
    }

    if (!filtered.length && !specialItem) {
        list.innerHTML = '<div class="p-4 text-center text-xs text-[var(--ink-3)]">No matching branches</div>';
        return;
    }

    list.innerHTML = specialItem + filtered.map(b => {
        const isSelected = b.name === currentSelected;
        return `
            <div class="branch-item ${isSelected ? 'selected' : ''}" onclick="selectBranch('${escapeHtml(b.name)}')">
                <div class="truncate flex-1">
                    <div class="flex items-center gap-1.5">
                        <span class="font-mono text-xs font-medium truncate">${escapeHtml(b.name)}</span>
                        ${b.is_current ? '<span class="branch-badge-curr">current</span>' : ''}
                        ${b.is_remote ? '<span class="branch-badge-remote">remote</span>' : ''}
                    </div>
                    <p class="text-[10px] text-[var(--ink-3)] truncate mt-0.5">${escapeHtml(b.last_commit || 'Commit')} • ${escapeHtml(b.relative_time || '')}</p>
                </div>
                ${isSelected ? '<svg class="w-3.5 h-3.5 text-[var(--accent)] shrink-0" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>' : ''}
            </div>
        `;
    }).join('');
}

function selectBranch(branchName) {
    if (activePopoverTarget === 'base') {
        selectedBaseBranch = branchName;
        // If base and compare become identical and another branch exists, adjust compare
        if (selectedBaseBranch === selectedCompareBranch) {
            const alt = repoBranches.find(b => b.name !== branchName);
            if (alt) selectedCompareBranch = alt.name;
        }
    } else {
        selectedCompareBranch = branchName;
        // If compare and base become identical and another branch exists, adjust base
        if (selectedCompareBranch === selectedBaseBranch) {
            const alt = repoBranches.find(b => b.name !== branchName);
            if (alt) selectedBaseBranch = alt.name;
        }
    }

    closeBranchPopover();
    updateBranchLabels();
    runBranchComparison();
}

function setBranchPair(base, compare) {
    selectedBaseBranch = base;
    selectedCompareBranch = compare;
    updateBranchLabels();
    runBranchComparison();
}

function swapBranches() {
    if (selectedCompareBranch === LOCAL_BRANCH || selectedBaseBranch === LOCAL_BRANCH) {
        showToast('Local changes can only be on the "From" side', 'info');
        return;
    }
    const temp = selectedBaseBranch;
    selectedBaseBranch = selectedCompareBranch;
    selectedCompareBranch = temp;
    updateBranchLabels();
    runBranchComparison();
}

function setPrStep(step) {
    ['step1Chip', 'step2Chip', 'step3Chip'].forEach((id, i) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.classList.toggle('active', i + 1 === step);
        el.classList.toggle('done', i + 1 < step);
    });
}

function setStatsStripVisible(visible) {
    const strip = document.getElementById('compareStatsStrip');
    if (strip) strip.style.display = visible ? 'flex' : 'none';
}

// Banner shown when a compared local branch is behind origin — one click
// fast-forwards it (the server refuses if histories diverged).
function renderStaleBranchBanner(staleBranches) {
    const banner = document.getElementById('staleBranchBanner');
    if (!banner) return;
    if (!staleBranches.length) {
        banner.classList.add('hidden');
        banner.innerHTML = '';
        return;
    }
    banner.innerHTML = staleBranches.map(s => `
        <div class="flex items-center justify-between gap-3 p-3 rounded-lg border border-[rgba(234,179,8,0.4)] bg-[rgba(234,179,8,0.08)]">
            <p class="text-xs text-[var(--ink-1)]">
                There ${s.behind === 1 ? 'is' : 'are'} <b>${s.behind} new change${s.behind === 1 ? '' : 's'}</b> on
                <span class="font-mono font-semibold">${escapeHtml(s.branch)}</span> that your local copy doesn't have yet — pull ${s.behind === 1 ? 'it' : 'them'} to stay up to date.
            </p>
            <button type="button" class="btn-secondary shrink-0" data-branch="${escapeHtml(s.branch)}"
                onclick="pullLocalBranch(this.dataset.branch, this)">Pull changes</button>
        </div>
    `).join('');
    banner.classList.remove('hidden');
}

async function pullLocalBranch(branch, btn) {
    if (btn) { btn.disabled = true; btn.innerText = 'Updating...'; }
    try {
        const res = await apiFetch('/api/v1/git/pull-branch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ working_directory: workspace(), branch }),
        });
        const data = await res.json();
        if (!res.ok || (data.status !== 'success' && data.status !== 'partial')) {
            showToast(data.detail || data.message || 'Could not update the branch', 'bad', 7000);
            if (btn) { btn.disabled = false; btn.innerText = 'Pull changes'; }
            return;
        }
        showToast(data.message, data.status === 'partial' ? 'info' : 'good', data.status === 'partial' ? 8000 : 5000);
        addFeedItem('Git & PR Studio', data.message, data.status === 'partial' ? 'brand' : 'good');
        await runBranchComparison();
    } catch (e) {
        showToast('Update failed: ' + e, 'bad');
        if (btn) { btn.disabled = false; btn.innerText = 'Pull changes'; }
    }
}

async function runBranchComparison() {
    const identicalBox = document.getElementById('compareIdenticalState');
    const activeBox = document.getElementById('compareActiveState');
    if (!identicalBox || !activeBox) return;

    if (selectedBaseBranch === selectedCompareBranch) {
        identicalBox.classList.remove('hidden');
        activeBox.classList.add('hidden');
        setStatsStripVisible(false);
        setPrStep(1);
        return;
    }

    const diffPreviewEl = document.getElementById('prDiffPreview');
    if (diffPreviewEl) diffPreviewEl.innerText = 'Analyzing differences between branches...';

    try {
        const res = await apiFetch('/api/v1/git/compare', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                working_directory: workspace(),
                base_branch: selectedBaseBranch,
                compare_branch: selectedCompareBranch,
            })
        });

        const data = await res.json();
        currentComparison = data;
        closeMergeStudio();
        renderStaleBranchBanner(data.stale_branches || []);

        if (data.is_identical) {
            identicalBox.classList.remove('hidden');
            activeBox.classList.add('hidden');
            setStatsStripVisible(false);
            setPrStep(1);
            return;
        }

        identicalBox.classList.add('hidden');
        activeBox.classList.remove('hidden');
        setStatsStripVisible(true);
        setPrStep(2);

        // A PR needs a real branch — hide the Draft PR footer when comparing local changes.
        const isLocalCompare = selectedCompareBranch === LOCAL_BRANCH;
        document.getElementById('prFooterBar')?.classList.toggle('hidden', isLocalCompare);
        if (isLocalCompare) document.getElementById('prDraftSection')?.classList.add('hidden');

        // Render Stats Header
        const stats = data.stats || {};
        const commits = data.commits || [];
        const files = data.files || [];

        const statCommits = document.getElementById('prStatsCommits');
        const statFiles = document.getElementById('prStatsFiles');
        const statAdds = document.getElementById('prStatsAdds');
        const statDels = document.getElementById('prStatsDels');

        if (statCommits) statCommits.innerText = `${commits.length} commit${commits.length === 1 ? '' : 's'}`;
        if (statFiles) statFiles.innerText = `${files.length} file${files.length === 1 ? '' : 's'}`;
        if (statAdds) statAdds.innerText = `+${stats.additions || 0}`;
        if (statDels) statDels.innerText = `-${stats.deletions || 0}`;

        const commitsCount = document.getElementById('prCommitsCount');
        if (commitsCount) commitsCount.innerText = `(${commits.length})`;

        // Render Commits List
        const commitsList = document.getElementById('prCommitsList');
        if (commitsList) {
            if (!commits.length) {
                commitsList.innerHTML = selectedCompareBranch === LOCAL_BRANCH
                    ? '<p class="text-xs text-[var(--ink-3)]">Uncommitted edits — nothing committed yet</p>'
                    : '<p class="text-xs text-[var(--ink-3)]">No commit divergence</p>';
            } else {
                commitsList.innerHTML = commits.map(c => `
                    <div class="commit-item !items-start" title="${escapeHtml(c.message)}">
                        <span class="commit-hash">${escapeHtml(c.hash)}</span>
                        <div class="flex-1 min-w-0">
                            <p class="text-xs text-[var(--ink-1)] leading-snug break-words">${escapeHtml(c.message)}</p>
                            <p class="text-[10px] text-[var(--ink-3)] mt-0.5">${escapeHtml(c.author)} • ${escapeHtml(c.relative_time)}</p>
                        </div>
                    </div>
                `).join('');
            }
        }

        // Render Files List
        const filesList = document.getElementById('prFilesList');
        if (filesList) {
            if (!files.length) {
                filesList.innerHTML = '<p class="text-xs text-[var(--ink-3)]">No files modified</p>';
            } else {
                filesList.innerHTML = files.map((f, i) => {
                    const segments = f.file.split('/');
                    const baseName = segments.pop();
                    const dirName = segments.join('/');
                    return `
                    <div class="commit-item justify-between !items-start merge-file-row" id="merge-file-${i}" onclick="openFileDiff(${i})" title="${escapeHtml(f.file)} — click to open">
                        <div class="flex-1 min-w-0">
                            <p class="font-mono text-xs text-[var(--ink-1)] truncate">${escapeHtml(baseName)}</p>
                            ${dirName ? `<p class="text-[9.5px] text-[var(--ink-3)] truncate mt-0.5">${escapeHtml(dirName)}/</p>` : ''}
                        </div>
                        <div class="flex items-center gap-1 shrink-0">
                            ${f.additions > 0 ? `<span class="file-stat-chip file-stat-add">+${f.additions}</span>` : ''}
                            ${f.deletions > 0 ? `<span class="file-stat-chip file-stat-del">-${f.deletions}</span>` : ''}
                            <svg class="w-3 h-3 text-[var(--ink-3)]" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7"/></svg>
                        </div>
                    </div>`;
                }).join('');
            }
        }

        // Render Diff Preview
        if (diffPreviewEl) {
            diffPreviewEl.innerHTML = data.diff_preview ? colorizeDiff(data.diff_preview) : '<span class="text-[var(--ink-3)]">No diff content</span>';
        }

        // Auto-open the first changed file in the side-by-side Merge Studio
        if (files.length) openFileDiff(0);

    } catch (e) {
        console.error('Compare error:', e);
        showToast('Branch comparison failed: ' + e, 'bad');
    }
}

function showPrDraftSection(scrollTo = false) {
    const section = document.getElementById('prDraftSection');
    if (!section) return;
    section.classList.remove('hidden');
    document.getElementById('prFooterBar')?.classList.add('hidden');
    setPrStep(3);
    if (scrollTo) section.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function generatePrDescription() {
    if (!currentComparison) {
        showToast('Please compare branches first', 'bad');
        return;
    }

    setBusy('generateDescBtn', true, 'Drafting...');
    try {
        const res = await apiFetch('/api/v1/git/generate-pr-description', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                working_directory: workspace(),
                base_branch: selectedBaseBranch,
                compare_branch: selectedCompareBranch,
                commits: currentComparison.commits || [],
                files: currentComparison.files || [],
                diff_summary: currentComparison.diff_preview || '',
            })
        });

        const data = await res.json();
        if (data.status === 'success') {
            const titleInput = document.getElementById('prTitleInput');
            const descInput = document.getElementById('prDescInput');
            if (titleInput) titleInput.value = data.title || '';
            if (descInput) descInput.value = data.description || '';

            showPrDraftSection(true);
            showToast('PR drafted — review, edit, then approve', 'good', 3000);
            addFeedItem('Git & PR Studio', `Drafted PR description for ${selectedBaseBranch} ← ${selectedCompareBranch}`, 'brand');
        } else {
            showToast(data.message || 'Could not generate PR description', 'bad');
        }
    } catch (e) {
        showToast('Failed to generate description: ' + e, 'bad');
    } finally {
        setBusy('generateDescBtn', false);
    }
}

function getPrReviewers() {
    const raw = document.getElementById('prReviewersInput')?.value || '';
    return raw.split(',').map(r => r.trim().replace(/^@/, '')).filter(Boolean);
}

function openPrConfirmModal() {
    const title = document.getElementById('prTitleInput')?.value.trim();
    if (!title) {
        showToast('Please enter a Pull Request title', 'bad');
        return;
    }

    const routeEl = document.getElementById('modalMergeRoute');
    const titleEl = document.getElementById('modalPrTitle');
    const draftEl = document.getElementById('modalPrDraftMode');
    const reviewersEl = document.getElementById('modalPrReviewers');
    const isDraft = document.getElementById('prDraftCheck')?.checked || false;
    const reviewers = getPrReviewers();

    if (routeEl) routeEl.innerText = `${selectedCompareBranch} → ${selectedBaseBranch}`;
    if (titleEl) titleEl.innerText = title;
    if (draftEl) draftEl.innerText = isDraft ? 'Yes (Draft PR)' : 'No (Ready for review)';
    if (reviewersEl) reviewersEl.innerText = reviewers.length ? reviewers.join(', ') : 'None';

    resetGatePanel('prGatePanel');
    const modal = document.getElementById('prConfirmModal');
    if (modal) modal.classList.remove('hidden');
}

function closePrConfirmModal() {
    const modal = document.getElementById('prConfirmModal');
    if (modal) modal.classList.add('hidden');
}

async function submitCreatePR() {
    const title = document.getElementById('prTitleInput')?.value.trim();
    const description = document.getElementById('prDescInput')?.value.trim() || '';
    const isDraft = document.getElementById('prDraftCheck')?.checked || false;

    if (!title) {
        showToast('Title is required', 'bad');
        return;
    }

    // Quality gate: the PR is blocked until every changed file on the branch
    // and the PR title pass the checks.
    setBusy('confirmSubmitPrBtn', true, 'Running checks...');
    const gateOk = await runQualityGate('prGatePanel', {
        buildBody: () => ({
            working_directory: workspace(),
            base_branch: selectedBaseBranch,
            compare_branch: selectedCompareBranch,
            title: document.getElementById('prTitleInput')?.value.trim() || '',
        }),
        allowFileFix: false,
        titleInputId: 'prTitleInput',
    });
    if (!gateOk) {
        setBusy('confirmSubmitPrBtn', false);
        return;
    }

    setBusy('confirmSubmitPrBtn', true, 'Creating PR...');

    try {
        const res = await apiFetch('/api/v1/git/create-pr', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                working_directory: workspace(),
                base_branch: selectedBaseBranch,
                compare_branch: selectedCompareBranch,
                title: title,
                description: description,
                draft: isDraft,
                reviewers: getPrReviewers(),
                gate_passed: true,
            })
        });

        const data = await res.json();
        closePrConfirmModal();

        if (res.ok && (data.status === 'success' || data.code === 'pr_created')) {
            const prLabel = data.pr_number ? `PR #${data.pr_number}` : 'Pull Request';
            showToast(`${prLabel} created on ${data.repository || 'GitHub'}. ${data.message || ''}`, 'good', 6000);
            addFeedItem('Git & PR Studio', `Created ${prLabel}: "${title}" — ${data.pr_url || ''}`, 'good');

            // Show a success bar with a link — no auto-redirect
            const successBar = document.getElementById('prSuccessBar');
            const successText = document.getElementById('prSuccessText');
            const successLink = document.getElementById('prSuccessLink');
            if (successBar && successLink && data.pr_url) {
                if (successText) successText.innerText = `${prLabel} — "${title}"`;
                successLink.href = data.pr_url;
                successBar.classList.remove('hidden');
            }

            // Clear inputs and collapse the draft form for the next run
            document.getElementById('prTitleInput').value = '';
            document.getElementById('prDescInput').value = '';
            const revInput = document.getElementById('prReviewersInput');
            if (revInput) revInput.value = '';
            document.getElementById('prDraftSection')?.classList.add('hidden');
            document.getElementById('prFooterBar')?.classList.remove('hidden');
            loadBranches();
        } else {
            showToast(data.detail || data.message || 'Could not create pull request', 'bad', 8000);
        }
    } catch (e) {
        showToast('PR creation failed: ' + e, 'bad');
    } finally {
        setBusy('confirmSubmitPrBtn', false);
    }
}

/* ---------- Quality gate: pre-commit / pre-PR checks ----------
   Three native checks (hygiene, secrets, Conventional Commit message/title)
   run before "Move changes" and "Create Pull Request". Any failure hard-blocks
   the action; fixes are applied only on an explicit click. */

const gateContexts = {};

function resetGatePanel(panelId) {
    delete gateContexts[panelId];
    const panel = document.getElementById(panelId);
    if (panel) { panel.classList.add('hidden'); panel.innerHTML = ''; }
}

async function runQualityGate(panelId, ctx) {
    gateContexts[panelId] = ctx;
    const panel = document.getElementById(panelId);
    if (panel) {
        panel.classList.remove('hidden');
        panel.innerHTML = '<div class="p-3 rounded-lg bg-[var(--surface-2)] border border-[var(--border)] text-[11px] text-[var(--ink-3)]">Running quality checks...</div>';
    }
    try {
        const res = await apiFetch('/api/v1/gate/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(ctx.buildBody()),
        });
        const gate = await res.json();
        if (!res.ok || gate.status !== 'success') {
            throw new Error(gate.detail || gate.message || 'unexpected response');
        }
        ctx.lastGate = gate;
        renderGatePanel(panelId, gate, ctx);
        return gate.passed;
    } catch (e) {
        // Fail closed: a gate that cannot run must not wave changes through.
        if (panel) panel.innerHTML = `<div class="p-3 rounded-lg bg-[var(--surface-2)] border border-[var(--border)] text-[11px]" style="color:var(--danger)">Quality checks could not run: ${escapeHtml(String(e.message || e))}</div>`;
        return false;
    }
}

function renderGatePanel(panelId, gate, ctx) {
    const panel = document.getElementById(panelId);
    if (!panel) return;

    const isRepoGate = gate.source === 'lefthook';
    const sourceLine = isRepoGate
        ? '<p class="text-[10px] text-[var(--ink-3)] uppercase tracking-wider font-semibold">Repository checks — defined by this repo\'s lefthook.yml</p>'
        : '';
    const headline = gate.passed
        ? `<p class="text-[11px] font-semibold" style="color:var(--success)">All ${isRepoGate ? 'repository ' : ''}checks passed</p>`
        : '<p class="text-[11px] font-semibold" style="color:var(--danger)">Checks failed — fix the issues below, then confirm again</p>';

    const rows = gate.checks.map(c => {
        const chip = c.status === 'passed'
            ? '<span class="vchip vchip-pass">&#10003; passed</span>'
            : '<span class="vchip vchip-fail">&#10007; failed</span>';
        const note = c.note ? `<p class="text-[10px] text-[var(--ink-3)]">${escapeHtml(c.note)}</p>` : '';
        return `<div class="flex items-center justify-between gap-2"><div class="min-w-0"><span class="text-[11px] text-[var(--ink-2)]">${escapeHtml(c.name)}</span>${note}</div>${chip}</div>`;
    }).join('');

    const details = gate.checks.filter(c => c.status === 'failed')
        .map(c => renderGateCheckDetails(panelId, c, ctx)).join('');

    panel.classList.remove('hidden');
    panel.innerHTML = `<div class="p-3 rounded-lg bg-[var(--surface-2)] border border-[var(--border)] space-y-2">${sourceLine}${headline}${rows}${details}</div>`;
}

function renderGateCheckDetails(panelId, check, ctx) {
    // Repository (lefthook) checks carry the tool's own output — offered as an
    // expandable log alongside whatever structured findings were parsed out.
    const outputBlock = (check.output && check.status === 'failed') ? `
        <details>
            <summary class="text-[10.5px] font-semibold text-[var(--accent)] cursor-pointer">Show full output</summary>
            <pre class="text-[10px] font-mono whitespace-pre-wrap leading-relaxed text-[var(--ink-3)] max-h-[260px] overflow-y-auto mt-1">${escapeHtml(check.output)}</pre>
        </details>` : '';

    // Commit message / PR title: list the rule breaks and offer the rewrite.
    if (check.id === 'message' || check.id === 'title') {
        const errs = check.findings.map(f => `<li>${escapeHtml(f.message)}</li>`).join('');
        let sugg = '';
        if (check.suggestion) {
            sugg = `<div class="flex items-center gap-2 pt-1">
                <code class="text-[11px] font-mono text-[var(--ink-1)] bg-[var(--surface)] border border-[var(--border)] rounded px-2 py-1 truncate">${escapeHtml(check.suggestion)}</code>
                <button type="button" class="btn-secondary shrink-0" onclick="gateUseSuggestion('${panelId}','${check.id}')">Use this</button>
            </div>`;
        }
        return `<div class="p-2 rounded-lg bg-[var(--surface)] border border-[var(--border)] space-y-1">
            <ul class="list-disc ml-4 text-[11px] text-[var(--ink-2)] space-y-0.5">${errs}</ul>${sugg}${outputBlock}</div>`;
    }

    // Nothing parsed to file level: show the failing output directly.
    const fileFindings = (check.findings || []).filter(f => f.file);
    if (check.output && !fileFindings.length) {
        const reason = (check.findings || [])[0]?.message || 'Check failed.';
        return `<div class="p-2 rounded-lg bg-[var(--surface)] border border-[var(--border)] space-y-1.5">
            <p class="text-[11px] font-semibold text-[var(--ink-1)]">${escapeHtml(check.name)}</p>
            <pre class="text-[10.5px] font-mono whitespace-pre-wrap leading-relaxed text-[var(--ink-2)] max-h-[180px] overflow-y-auto">${escapeHtml(reason)}</pre>
            ${outputBlock}
        </div>`;
    }

    // Findings grouped per file, with fix actions.
    const byFile = {};
    for (const f of check.findings) (byFile[f.file || ''] ||= []).push(f);

    // The repo's formatter fixes every unformatted file in one run.
    let formatFix = '';
    if (ctx.allowFileFix && check.id.startsWith('format')) {
        formatFix = `<div class="pt-1"><button type="button" class="btn-secondary" onclick="gateFixFormat('${panelId}', this)">Fix formatting — run the repo's formatter</button></div>`;
    }

    return Object.entries(byFile).map(([file, findings]) => {
        const shown = findings.slice(0, 6);
        const lines = shown.map((f) => {
            const loc = f.line ? `line ${f.line}: ` : '';
            let action = '';
            if (ctx.allowFileFix && !f.auto_fixable) {
                const idx = check.findings.indexOf(f);
                action = ` <button type="button" class="text-[10.5px] font-semibold text-[var(--accent)] hover:underline" onclick="gateSuggestFix('${panelId}','${check.id}',${idx})">Suggest a fix</button>`;
            }
            return `<li>${escapeHtml(loc)}${escapeHtml(f.message)}${action}
                <div id="${panelId}-sugg-${check.id}-${check.findings.indexOf(f)}"></div></li>`;
        }).join('');
        const more = findings.length > shown.length
            ? `<li class="text-[var(--ink-3)]">…and ${findings.length - shown.length} more in this file</li>` : '';

        let fixBtn = '';
        if (ctx.allowFileFix && findings.some(f => f.auto_fixable)) {
            fixBtn = `<button type="button" class="btn-secondary" data-file="${escapeHtml(file)}" onclick="gateAutoFix('${panelId}', this.dataset.file, this)">Fix automatically</button>`;
        }
        return `<div class="p-2 rounded-lg bg-[var(--surface)] border border-[var(--border)] space-y-1">
            <div class="flex items-center justify-between gap-2">
                <p class="text-[11px] font-mono font-semibold text-[var(--ink-1)] truncate">${escapeHtml(file)}</p>${fixBtn}
            </div>
            <ul class="list-disc ml-4 text-[11px] text-[var(--ink-2)] space-y-0.5">${lines}${more}</ul>
        </div>`;
    }).join('') + formatFix + outputBlock;
}

async function runRepoSetup(btn) {
    if (btn) { btn.disabled = true; btn.innerText = 'Running setup…'; }
    const status = document.getElementById('govSetupStatus');
    if (status) status.innerText = 'Installing the toolchain and wiring the hooks — this can take several minutes. Leave this page open.';
    try {
        const res = await apiFetch('/api/v1/gate/run-setup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ working_directory: workspace() }),
        });
        const data = await res.json();
        showToast(data.message || (data.status === 'success' ? 'Setup completed' : 'Setup reported errors'),
            data.status === 'success' ? 'good' : 'bad', 9000);
        if (status && data.steps?.length) {
            status.innerHTML = escapeHtml(data.steps.map(s => `${s.ok ? '✓' : '✕'} ${s.step}`).join('  ·  ') + ' — ' + (data.message || ''))
                + data.steps.map(s => `
                    <details class="mt-1">
                        <summary class="cursor-pointer font-semibold text-[var(--accent)]">${s.ok ? '✓' : '✕'} ${escapeHtml(s.step)} — output</summary>
                        <pre class="text-[10px] font-mono whitespace-pre-wrap leading-relaxed max-h-[220px] overflow-y-auto mt-1">${escapeHtml(s.output || '')}</pre>
                    </details>`).join('');
        }
        await loadRepoGovernance();
    } catch (e) {
        showToast('Setup failed: ' + e, 'bad');
        if (btn) { btn.disabled = false; btn.innerText = 'Run setup now'; }
    }
}

async function gateFixFormat(panelId, btn) {
    const ctx = gateContexts[panelId];
    if (!ctx) return;
    if (btn) { btn.disabled = true; btn.innerText = 'Formatting...'; }
    try {
        const res = await apiFetch('/api/v1/gate/fix-format', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ working_directory: workspace() }),
        });
        const data = await res.json();
        if (!res.ok || data.status !== 'success') {
            showToast(data.detail || data.message || 'The formatter could not run', 'bad', 7000);
            if (btn) { btn.disabled = false; btn.innerText = "Fix formatting — run the repo's formatter"; }
            return;
        }
        showToast(data.message, 'good');
        await runQualityGate(panelId, ctx);
    } catch (e) {
        showToast('Formatting failed: ' + e, 'bad');
        if (btn) { btn.disabled = false; btn.innerText = "Fix formatting — run the repo's formatter"; }
    }
}

function gateUseSuggestion(panelId, checkId) {
    const ctx = gateContexts[panelId];
    const check = ctx?.lastGate?.checks?.find(c => c.id === checkId);
    if (!ctx || !check?.suggestion) return;

    const input = document.getElementById(checkId === 'message' ? ctx.messageInputId : ctx.titleInputId);
    if (input) input.value = check.suggestion;
    if (checkId === 'title') {
        const titleEl = document.getElementById('modalPrTitle');
        if (titleEl) titleEl.innerText = check.suggestion;
    }
    runQualityGate(panelId, ctx);
}

async function gateAutoFix(panelId, filePath, btn) {
    const ctx = gateContexts[panelId];
    if (!ctx) return;
    if (btn) { btn.disabled = true; btn.innerText = 'Fixing...'; }
    try {
        const res = await apiFetch('/api/v1/gate/fix-hygiene', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ working_directory: workspace(), file_path: filePath }),
        });
        const data = await res.json();
        if (!res.ok || data.status !== 'success') {
            showToast(data.detail || data.message || 'Could not fix the file', 'bad');
            if (btn) { btn.disabled = false; btn.innerText = 'Fix automatically'; }
            return;
        }
        showToast(data.message, 'good');
        await runQualityGate(panelId, ctx);
    } catch (e) {
        showToast('Fix failed: ' + e, 'bad');
        if (btn) { btn.disabled = false; btn.innerText = 'Fix automatically'; }
    }
}

async function gateSuggestFix(panelId, checkId, findingIdx) {
    const ctx = gateContexts[panelId];
    const check = ctx?.lastGate?.checks?.find(c => c.id === checkId);
    const finding = check?.findings?.[findingIdx];
    const slot = document.getElementById(`${panelId}-sugg-${checkId}-${findingIdx}`);
    if (!ctx || !finding || !slot) return;

    slot.innerHTML = '<p class="text-[10.5px] text-[var(--ink-3)] pt-1">Drafting a fix...</p>';
    try {
        const res = await apiFetch('/api/v1/validate/suggest-fix', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                working_directory: workspace(),
                file_path: finding.file,
                line: finding.line,
                message: finding.message,
            }),
        });
        const data = await res.json();
        if (!res.ok || data.status !== 'success') {
            slot.innerHTML = `<p class="text-[10.5px] pt-1" style="color:var(--danger)">${escapeHtml(data.detail || 'No reliable suggestion — fix it manually.')}</p>`;
            return;
        }
        ctx.aiFixes = ctx.aiFixes || {};
        ctx.aiFixes[`${checkId}-${findingIdx}`] = { file: finding.file, ...data.fix };
        slot.innerHTML = `<div class="mt-1 p-2 rounded bg-[var(--surface-2)] border border-[var(--border)] space-y-1">
            <p class="text-[10.5px] text-[var(--ink-2)]">${escapeHtml(data.fix.explanation)}</p>
            <pre class="text-[10.5px] font-mono whitespace-pre-wrap" style="color:var(--danger)">- ${escapeHtml(data.fix.original_code)}</pre>
            <pre class="text-[10.5px] font-mono whitespace-pre-wrap" style="color:var(--success)">+ ${escapeHtml(data.fix.proposed_code)}</pre>
            <button type="button" class="btn-secondary" onclick="gateApplySuggestion('${panelId}','${checkId}',${findingIdx}, this)">Apply this fix</button>
        </div>`;
    } catch (e) {
        slot.innerHTML = `<p class="text-[10.5px] pt-1" style="color:var(--danger)">Suggestion failed: ${escapeHtml(String(e))}</p>`;
    }
}

async function gateApplySuggestion(panelId, checkId, findingIdx, btn) {
    const ctx = gateContexts[panelId];
    const fix = ctx?.aiFixes?.[`${checkId}-${findingIdx}`];
    if (!ctx || !fix) return;
    if (btn) { btn.disabled = true; btn.innerText = 'Applying...'; }
    try {
        const res = await apiFetch('/api/v1/validate/apply-suggestion', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                working_directory: workspace(),
                file_path: fix.file,
                original_code: fix.original_code,
                proposed_code: fix.proposed_code,
            }),
        });
        const data = await res.json();
        if (!res.ok || data.status !== 'success') {
            showToast(data.detail || data.message || 'Could not apply the fix', 'bad');
            if (btn) { btn.disabled = false; btn.innerText = 'Apply this fix'; }
            return;
        }
        showToast(`Fix applied to ${fix.file}`, 'good');
        await runQualityGate(panelId, ctx);
    } catch (e) {
        showToast('Apply failed: ' + e, 'bad');
        if (btn) { btn.disabled = false; btn.innerText = 'Apply this fix'; }
    }
}

/* ---------- Merge Studio: side-by-side block transfer ---------- */

let currentFileDiff = null;
let stagedBlockIds = new Set();

async function openFileDiff(fileIndex) {
    const fileEntry = currentComparison?.files?.[fileIndex];
    if (!fileEntry) return;

    document.querySelectorAll('.merge-file-row').forEach(el => el.classList.remove('active'));
    const row = document.getElementById(`merge-file-${fileIndex}`);
    if (row) row.classList.add('active');

    const studio = document.getElementById('mergeStudio');
    const grid = document.getElementById('mergeGrid');
    const placeholder = document.getElementById('mergeEmptyState');
    if (studio) studio.classList.remove('hidden');
    if (placeholder) placeholder.classList.add('hidden');
    if (grid) grid.innerHTML = '<div class="p-6 text-center text-xs text-[var(--ink-3)]">Loading side-by-side diff...</div>';

    try {
        const res = await apiFetch('/api/v1/git/file-diff', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                working_directory: workspace(),
                base_branch: selectedBaseBranch,
                compare_branch: selectedCompareBranch,
                file_path: fileEntry.file,
            })
        });
        const data = await res.json();
        if (data.status !== 'success') {
            showToast(data.detail || data.message || 'Could not load file diff', 'bad');
            return;
        }

        currentFileDiff = data;
        stagedBlockIds = new Set();

        const fileLabel = document.getElementById('mergeFileLabel');
        if (fileLabel) fileLabel.innerText = data.file_path;
        updateBranchLabels();
        renderMergeGrid();
        updateStagedUi();
        if (studio) studio.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (e) {
        showToast('Failed to load file diff: ' + e, 'bad');
    }
}

function closeMergeStudio() {
    const studio = document.getElementById('mergeStudio');
    const placeholder = document.getElementById('mergeEmptyState');
    if (studio) studio.classList.add('hidden');
    if (placeholder) placeholder.classList.remove('hidden');
    currentFileDiff = null;
    stagedBlockIds = new Set();
    document.querySelectorAll('.merge-file-row').forEach(el => el.classList.remove('active'));
}

function renderMergeLines(lines, startNo, cls) {
    if (!lines.length) return '<div class="merge-line merge-line-empty">&nbsp;</div>';
    return lines.map((line, i) => `
        <div class="merge-line ${cls}">
            <span class="merge-lineno">${startNo + i}</span><span class="merge-code">${escapeHtml(line) || '&nbsp;'}</span>
        </div>`).join('');
}

function renderMergeGrid() {
    const grid = document.getElementById('mergeGrid');
    if (!grid || !currentFileDiff) return;

    const blocks = currentFileDiff.blocks || [];
    if (!blocks.some(b => b.type !== 'equal')) {
        grid.innerHTML = '<div class="p-6 text-center text-xs text-[var(--ink-3)]">No differences remain in this file — all changes are already on the base branch.</div>';
        return;
    }

    const CONTEXT = 3;
    grid.innerHTML = blocks.map(b => {
        // Layout: FROM (compare / source) on the LEFT, TO (base / target) on the RIGHT.
        if (b.type === 'equal') {
            let baseHtml, compareHtml, collapsedNote = '';
            if (b.base_lines.length > CONTEXT * 2 + 2) {
                const headB = b.base_lines.slice(0, CONTEXT);
                const tailB = b.base_lines.slice(-CONTEXT);
                const headC = b.compare_lines.slice(0, CONTEXT);
                const tailC = b.compare_lines.slice(-CONTEXT);
                const hidden = b.base_lines.length - CONTEXT * 2;
                collapsedNote = `<div class="merge-collapsed">··· ${hidden} unchanged lines ···</div>`;
                baseHtml = renderMergeLines(headB, b.base_start, '') + collapsedNote +
                           renderMergeLines(tailB, b.base_start + b.base_lines.length - CONTEXT, '');
                compareHtml = renderMergeLines(headC, b.compare_start, '') + collapsedNote +
                              renderMergeLines(tailC, b.compare_start + b.compare_lines.length - CONTEXT, '');
            } else {
                baseHtml = renderMergeLines(b.base_lines, b.base_start, '');
                compareHtml = renderMergeLines(b.compare_lines, b.compare_start, '');
            }
            return `
                <div class="merge-row">
                    <div class="merge-cell">${compareHtml}</div>
                    <div class="merge-gutter"></div>
                    <div class="merge-cell">${baseHtml}</div>
                </div>`;
        }

        const staged = stagedBlockIds.has(b.id);
        const sourceInner = renderMergeLines(b.compare_lines, b.compare_start, b.compare_lines.length ? 'merge-line-add' : 'merge-line-empty');
        const targetInner = staged
            ? renderMergeLines(b.compare_lines, b.base_start, 'merge-line-incoming')
            : renderMergeLines(b.base_lines, b.base_start, b.base_lines.length ? 'merge-line-del' : 'merge-line-empty');

        return `
            <div class="merge-row merge-row-change ${staged ? 'staged' : ''}" id="merge-row-${b.id}">
                <div class="merge-cell merge-cell-source" draggable="true" ondragstart="mergeDragStart(event, ${b.id})" title="Drag this change onto the right pane to select it">
                    ${sourceInner}
                </div>
                <div class="merge-gutter">
                    <button type="button" class="merge-arrow-btn ${staged ? 'staged' : ''}" onclick="toggleBlockStaged(${b.id})"
                            title="${staged ? 'Deselect this change' : 'Move this change into ' + escapeHtml(selectedBaseBranch)}">
                        ${staged
                            ? '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>'
                            : '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M13 5l7 7-7 7M5 5l7 7-7 7"/></svg>'}
                    </button>
                </div>
                <div class="merge-cell merge-cell-target"
                     ondragover="mergeDragOver(event, ${b.id})" ondragleave="mergeDragLeave(event, ${b.id})" ondrop="mergeDrop(event, ${b.id})">
                    ${targetInner}
                </div>
            </div>`;
    }).join('');
}

function toggleBlockStaged(blockId) {
    if (stagedBlockIds.has(blockId)) stagedBlockIds.delete(blockId);
    else stagedBlockIds.add(blockId);
    renderMergeGrid();
    updateStagedUi();
}

function stageAllBlocks() {
    if (!currentFileDiff) return;
    (currentFileDiff.blocks || []).forEach(b => { if (b.type !== 'equal') stagedBlockIds.add(b.id); });
    renderMergeGrid();
    updateStagedUi();
}

function clearStagedBlocks() {
    stagedBlockIds = new Set();
    renderMergeGrid();
    updateStagedUi();
}

function updateStagedUi() {
    const chip = document.getElementById('stagedCountChip');
    const text = document.getElementById('stagedCountText');
    const btn = document.getElementById('applyBlocksBtn');
    const n = stagedBlockIds.size;
    if (chip) chip.classList.toggle('hidden', n === 0);
    if (text) text.innerText = `${n} selected`;
    if (btn) {
        btn.disabled = n === 0;
        btn.innerText = n > 0 ? `Move ${n} change${n === 1 ? '' : 's'} to ${selectedBaseBranch}` : 'Move changes';
    }
}

/* Drag & drop staging */
function mergeDragStart(e, blockId) {
    e.dataTransfer.setData('text/plain', String(blockId));
    e.dataTransfer.effectAllowed = 'copy';
}

function mergeDragOver(e, blockId) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    const row = document.getElementById(`merge-row-${blockId}`);
    if (row) row.classList.add('drop-target');
}

function mergeDragLeave(e, blockId) {
    const row = document.getElementById(`merge-row-${blockId}`);
    if (row) row.classList.remove('drop-target');
}

function mergeDrop(e, blockId) {
    e.preventDefault();
    const row = document.getElementById(`merge-row-${blockId}`);
    if (row) row.classList.remove('drop-target');
    const dragged = parseInt(e.dataTransfer.getData('text/plain'), 10);
    if (!Number.isNaN(dragged) && !stagedBlockIds.has(dragged)) {
        stagedBlockIds.add(dragged);
        renderMergeGrid();
        updateStagedUi();
        showToast('Change selected — press "Move changes" when ready', 'info', 2200);
    }
}

/* Apply staged blocks (with confirmation gate) */
function openMergeConfirmModal() {
    if (!currentFileDiff || !stagedBlockIds.size) return;

    const fileEl = document.getElementById('modalMergeFile');
    const dirEl = document.getElementById('modalMergeDirection');
    const countEl = document.getElementById('modalMergeBlockCount');
    const msgEl = document.getElementById('mergeCommitMessage');

    if (fileEl) fileEl.innerText = currentFileDiff.file_path;
    if (dirEl) dirEl.innerText = `${branchDisplay(selectedCompareBranch)} → ${selectedBaseBranch}`;
    if (countEl) countEl.innerText = `${stagedBlockIds.size} selected`;
    if (msgEl && !msgEl.value.trim()) {
        msgEl.value = `merge(${currentFileDiff.file_path}): apply ${stagedBlockIds.size} change block(s) from ${branchDisplay(selectedCompareBranch)}`;
    }

    // Feature-branch-first workflow: local changes must land on a feature branch, not directly on main.
    const isLocal = selectedCompareBranch === LOCAL_BRANCH;
    const destSection = document.getElementById('mergeDestSection');
    if (destSection) {
        destSection.classList.toggle('hidden', !isLocal);
        if (isLocal) {
            const prot = document.getElementById('destProtectedName');
            if (prot) prot.innerText = selectedBaseBranch;

            const nameInput = document.getElementById('newBranchNameInput');
            if (nameInput && !nameInput.value.trim()) {
                const slug = (currentFileDiff.file_path.split('/').pop() || 'change')
                    .replace(/\.[^.]+$/, '').replace(/[^\w-]+/g, '-').toLowerCase();
                nameInput.value = `feature/${slug}-updates`;
            }

            const select = document.getElementById('destBranchSelect');
            if (select) {
                const options = repoBranches.filter(b => b.name !== selectedBaseBranch);
                select.innerHTML = options.length
                    ? options.map(b => `<option value="${escapeHtml(b.name)}">${escapeHtml(b.name)}</option>`).join('')
                    : '<option value="">(no other branches yet)</option>';
            }
            updateDestInputs();
        }
    }
    const note = document.getElementById('mergeModalNote');
    if (note) {
        note.innerText = isLocal
            ? 'Your working files stay untouched — the selected changes are committed to the feature branch only, so you can raise a PR from it.'
            : 'The selected changes will be committed to the "To" branch. Nothing else in your workspace is touched.';
    }

    resetGatePanel('mergeGatePanel');
    const modal = document.getElementById('mergeConfirmModal');
    if (modal) modal.classList.remove('hidden');
}

function updateDestInputs() {
    const isNew = document.getElementById('destNewRadio')?.checked ?? true;
    const nameInput = document.getElementById('newBranchNameInput');
    const select = document.getElementById('destBranchSelect');
    if (nameInput) nameInput.disabled = !isNew;
    if (select) select.disabled = isNew;
}

function closeMergeConfirmModal() {
    const modal = document.getElementById('mergeConfirmModal');
    if (modal) modal.classList.add('hidden');
}

async function submitApplyBlocks() {
    if (!currentFileDiff || !stagedBlockIds.size) return;

    const message = document.getElementById('mergeCommitMessage')?.value.trim() || '';
    const filePath = currentFileDiff.file_path;

    // Local changes go to a feature branch (new or existing), never straight onto the To branch.
    const isLocal = selectedCompareBranch === LOCAL_BRANCH;
    let destBranch = null;
    let createBranch = false;
    let pushBranch = false;
    if (isLocal) {
        const isNew = document.getElementById('destNewRadio')?.checked ?? true;
        if (isNew) {
            destBranch = document.getElementById('newBranchNameInput')?.value.trim() || '';
            createBranch = true;
            if (!destBranch || /\s/.test(destBranch)) {
                showToast('Enter a valid feature branch name (no spaces)', 'bad');
                return;
            }
        } else {
            destBranch = document.getElementById('destBranchSelect')?.value || '';
            if (!destBranch) {
                showToast('Pick an existing feature branch', 'bad');
                return;
            }
        }
        pushBranch = document.getElementById('destPushCheck')?.checked || false;
    }

    // Quality gate: the commit is blocked until hygiene, secrets, and the
    // commit message all pass. Fixes are one click away in the panel below.
    setBusy('confirmApplyBlocksBtn', true, 'Running checks...');
    const gateOk = await runQualityGate('mergeGatePanel', {
        buildBody: () => ({
            working_directory: workspace(),
            files: [filePath],
            message: document.getElementById('mergeCommitMessage')?.value.trim() || '',
            ...(isLocal ? {} : { compare_branch: selectedCompareBranch }),
        }),
        allowFileFix: isLocal,
        messageInputId: 'mergeCommitMessage',
    });
    if (!gateOk) {
        setBusy('confirmApplyBlocksBtn', false);
        return;
    }

    setBusy('confirmApplyBlocksBtn', true, 'Applying...');

    try {
        const res = await apiFetch('/api/v1/git/apply-diff-blocks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                working_directory: workspace(),
                base_branch: selectedBaseBranch,
                compare_branch: selectedCompareBranch,
                file_path: filePath,
                block_ids: Array.from(stagedBlockIds),
                commit_message: message,
                commit_to_branch: destBranch,
                create_branch: createBranch,
                push: pushBranch,
                gate_passed: true,
            })
        });
        const data = await res.json();
        closeMergeConfirmModal();

        if (res.ok && data.status === 'success') {
            const target = data.branch || selectedBaseBranch;
            showToast(data.message || `Changes applied to ${target}`, 'good', 5000);
            addFeedItem('Git & PR Studio', `Applied ${data.applied_blocks} block(s) of ${filePath} to ${target} (${data.commit || 'commit'})`, 'good');
            const msgEl = document.getElementById('mergeCommitMessage');
            if (msgEl) msgEl.value = '';
            const nameInput = document.getElementById('newBranchNameInput');
            if (nameInput) nameInput.value = '';

            if (isLocal && target !== selectedBaseBranch) {
                // Guide to the next step: compare the feature branch against main and draft the PR.
                if (!repoBranches.some(b => b.name === target)) {
                    repoBranches.unshift({ name: target, ref: target, is_remote: false, is_current: false, relative_time: 'just now', last_commit: 'Created from Merge Studio' });
                }
                showToast(`Now comparing ${target} → ${selectedBaseBranch} — draft your PR when ready`, 'info', 5000);
                setBranchPair(selectedBaseBranch, target);
            } else {
                await runBranchComparison();
                // Re-open the file that was just merged (index 0 is auto-opened by the comparison)
                const idx = (currentComparison?.files || []).findIndex(f => f.file === filePath);
                if (idx > 0) openFileDiff(idx);
            }
        } else {
            showToast(data.detail || data.message || 'Could not apply changes', 'bad', 7000);
        }
    } catch (e) {
        showToast('Apply failed: ' + e, 'bad');
    } finally {
        setBusy('confirmApplyBlocksBtn', false);
    }
}

// Escape closes the working-tree drawer and any open modal
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeBranchPopover();
    }
});

// Global click listener to close popovers
document.addEventListener('click', (e) => {
    const popover = document.getElementById('branchPopover');
    const baseBtn = document.getElementById('baseBranchBtn');
    const compBtn = document.getElementById('compareBranchBtn');
    if (popover && !popover.classList.contains('hidden')) {
        if (!popover.contains(e.target) && !baseBtn?.contains(e.target) && !compBtn?.contains(e.target)) {
            closeBranchPopover();
        }
    }
    const vPop = document.getElementById('vBranchPopover');
    const vBtn = document.getElementById('vBranchBtn');
    if (vPop && !vPop.classList.contains('hidden')) {
        if (!vPop.contains(e.target) && !vBtn?.contains(e.target)) {
            closeVBranchPopover();
        }
    }
});
