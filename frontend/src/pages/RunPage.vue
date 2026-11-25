<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { ApiError, api, type PlanDocument, type RunRecord } from "../api";
import ProjectConfig from "./ProjectConfig.vue";

const RETRY_STEPS = ["前置检查", "需求正文", "方案", "基线", "执行"];
const PIPELINE = [
  { id: "前置检查", title: "核对仓库" },
  { id: "需求正文", title: "读需求" },
  { id: "方案", title: "确认步骤" },
  { id: "基线", title: "列出数据" },
  { id: "执行", title: "按方案测" },
];

const route = useRoute();
const run = ref<RunRecord | null>(null);
const workspace = ref<"run" | "config">("run");
const retryChoice = ref("");
const browserChoice = ref<"edge" | "chrome">("edge");
const message = ref("");
const tab = ref<"ui" | "checks" | "open">("ui");
const decisions = ref<Record<string, string>>({});
const seedText = ref("");
const saving = ref(false);
const loadedReview = ref("");
const fixtures = ref<Record<string, { database: string; pk: Record<string, string> }>>({});
const sqlText = ref("");
const sqlReason = ref("");
const sqlParameters = ref("");
const sqlDatabase = ref("");
const sqlRows = ref("1");
const sqlUntouched = ref("");
let timer = 0;

const statusText: Record<string, string> = {
  running: "执行中",
  review: "待确认",
  paused: "暂停",
  blocked: "阻塞",
  stopped: "已停止",
  complete: "报告已生成",
  interrupted: "已停下",
};

const runId = computed(() => String(route.params.id || ""));
const label = computed(() => statusText[run.value?.status || ""] || run.value?.status || "—");
const counts = computed(() => {
  const items = run.value?.cases ?? [];
  return {
    passed: items.filter((item) => item.status === "passed").length,
    failed: items.filter((item) => item.status === "failed").length,
    blocked: items.filter((item) => item.status === "blocked").length,
    skipped: items.filter((item) => item.status === "skipped").length,
  };
});
const countText = computed(() => `通过 ${counts.value.passed} · 失败 ${counts.value.failed} · 跳过 ${counts.value.skipped} · 阻塞 ${counts.value.blocked}`);
const latest = computed(() => {
  const events = run.value?.events ?? [];
  return events.length ? events[events.length - 1] : null;
});
const progressEvents = computed(() => (run.value?.events ?? []).filter((event) =>
  !(event.basis.startsWith("看到「") && event.basis.includes("页面上没有找到"))
));
const progressEl = ref<HTMLElement | null>(null);
const stickToLatest = ref(true);
const reportOpen = ref(false);
const reportError = ref("");
const reportLines = ref<Array<{ kind: "h" | "p" | "img"; text: string; src?: string }>>([]);
const reportUrls: string[] = [];
const requirementShots = ref<string[]>([]);
const shotUrls: string[] = [];

function pageToken(): string {
  return document.querySelector<HTMLMetaElement>('meta[name="qa-token"]')?.content ?? "";
}

function releaseReport(): void {
  for (const url of reportUrls) URL.revokeObjectURL(url);
  reportUrls.length = 0;
  reportLines.value = [];
}

function releaseShots(): void {
  for (const url of shotUrls) URL.revokeObjectURL(url);
  shotUrls.length = 0;
  requirementShots.value = [];
}

async function loadRequirementShots() {
  releaseShots();
  const files = run.value?.requirement_images ?? [];
  if (!files.length) return;
  const headers = new Headers();
  headers.set("X-QA-Token", pageToken());
  const urls: string[] = [];
  for (const relative of files) {
    const shot = await fetch(`/api/runs/${runId.value}/artifacts/${relative}`, { headers });
    if (!shot.ok) continue;
    const src = URL.createObjectURL(await shot.blob());
    shotUrls.push(src);
    urls.push(src);
  }
  requirementShots.value = urls;
}

async function toggleReport() {
  if (reportOpen.value) {
    reportOpen.value = false;
    releaseReport();
    return;
  }
  reportOpen.value = true;
  reportError.value = "";
  const headers = new Headers();
  headers.set("X-QA-Token", pageToken());
  try {
    const response = await fetch(`/api/runs/${runId.value}/report`, { headers });
    const text = await response.text();
    if (!response.ok) {
      reportError.value = "报告还没有生成";
      return;
    }
    const lines: Array<{ kind: "h" | "p" | "img"; text: string; src?: string }> = [];
    for (const line of text.split("\n")) {
      const image = line.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
      if (image) {
        const relative = image[2];
        const shot = await fetch(`/api/runs/${runId.value}/artifacts/${relative}`, { headers });
        if (!shot.ok) {
          lines.push({ kind: "p", text: `证据缺失：${relative}` });
          continue;
        }
        const src = URL.createObjectURL(await shot.blob());
        reportUrls.push(src);
        lines.push({ kind: "img", text: image[1] || "截图", src });
        continue;
      }
      if (line.startsWith("# ")) lines.push({ kind: "h", text: line.slice(2) });
      else if (line.startsWith("## ")) lines.push({ kind: "h", text: line.slice(3) });
      else if (line.trim()) lines.push({ kind: "p", text: line.replace(/^- /, "") });
    }
    reportLines.value = lines;
  } catch {
    reportError.value = "报告没有打开";
  }
}

function onProgressScroll() {
  const el = progressEl.value;
  if (!el) return;
  stickToLatest.value = el.scrollHeight - el.scrollTop - el.clientHeight < 64;
}

async function revealLatest() {
  await nextTick();
  const el = progressEl.value;
  if (!el || !stickToLatest.value) return;
  el.scrollTop = el.scrollHeight;
}

function eventTone(basis: string): string {
  if (basis.startsWith("思考：")) return "think";
  if (basis.startsWith("想：")) return "want";
  return "act";
}

function eventLabel(event: { status: string; step: string; basis: string }): string {
  if (event.basis.startsWith("思考：")) return "思考";
  if (event.basis.startsWith("想：")) return "想";
  return `${event.status} · ${event.step}`;
}

function eventBody(basis: string): string {
  if (basis.startsWith("思考：")) return basis.slice("思考：".length);
  if (basis.startsWith("想：")) return basis.slice("想：".length);
  return basis;
}

watch(() => run.value?.events?.length ?? 0, () => {
  void revealLatest();
});
watch(() => (run.value?.requirement_images ?? []).join("|"), () => {
  void loadRequirementShots();
});
const pipeline = computed(() => {
  const events = run.value?.events ?? [];
  const latestStep = [...events].reverse().find((event) => RETRY_STEPS.includes(event.step))?.step || "";
  const currentIndex = RETRY_STEPS.indexOf(latestStep);
  return PIPELINE.map((step, index) => {
    const related = events.filter((event) => event.step === step.id);
    const last = related[related.length - 1];
    let state = "wait";
    if (index < currentIndex) state = "done";
    else if (index === currentIndex) {
      if (last?.status === "阻塞" || last?.status === "失败") state = "bad";
      else if (last?.status === "完成" && run.value?.status !== "running") state = "done";
      else state = "current";
    }
    return { ...step, state, detail: last?.basis || (state === "wait" ? "还没到" : "") };
  });
});
const retryChoices = computed(() => {
  const seen = new Set((run.value?.events ?? []).map((event) => event.step).filter(Boolean));
  const found = RETRY_STEPS.filter((step) => seen.has(step));
  return found.length ? found : RETRY_STEPS;
});
const caseText: Record<string, string> = {
  waiting: "等待",
  running: "执行中",
  paused: "暂停",
  passed: "通过",
  failed: "失败",
  blocked: "阻塞",
  skipped: "跳过",
};
const pendingPlan = computed(() => run.value?.review?.kind === "plan");
const plan = computed<PlanDocument | null>(() => {
  if (pendingPlan.value) return run.value?.review?.payload ?? null;
  const approved = [...(run.value?.approvals ?? [])].reverse().find((item) => item.kind === "plan");
  return approved?.payload ?? null;
});
const unusedCheckNames = computed(() => {
  const used = new Set((plan.value?.cases ?? []).map((item) => item.check_id).filter((item): item is string => Boolean(item)));
  return (plan.value?.checks ?? []).flatMap((item) => {
    const id = item.id;
    return id && !used.has(id) ? [id] : [];
  });
});

function locatorText(status?: string): string {
  return status === "pending" ? "待定位" : status === "ready" ? "可定位" : status || "—";
}

function stateSameAsKey(item: { pk?: string[]; state_field?: string }): boolean {
  const field = item.state_field || "";
  return field !== "" && (item.pk ?? []).includes(field);
}

function caseExpect(item: { expect_text?: string | string[] }): string {
  const value = item.expect_text;
  if (Array.isArray(value)) return value.map((part) => part.trim()).filter(Boolean).join("；");
  return typeof value === "string" ? value.trim() : "";
}

function caseOnlyReads(item: { steps?: Array<{ effect?: string }> }): boolean {
  const steps = item.steps ?? [];
  return steps.length > 0 && steps.every((step) => (step.effect || "read") === "read");
}

function checkOnlyReads(item: { id?: string }): boolean {
  const cases = (plan.value?.cases ?? []).filter((entry) => entry.check_id === item.id);
  return cases.length > 0 && cases.every((entry) => caseOnlyReads(entry));
}

const actionText: Record<string, string> = { click: "点击", type: "填写", select: "选择", set: "勾选" };
const avoidLine = "不要点重转、删除、作废、审核、手工转化、手工下载、提交，也不要点表格里的列名。";

function sampleValues(checkId: string): string[] {
  const payload = run.value?.review?.payload as { test_values?: Record<string, string[]> } | undefined;
  const values = payload?.test_values?.[checkId];
  if (!Array.isArray(values)) return [];
  return values.slice(0, 5).map((item) => String(item));
}

function sampleValue(value: string): string {
  const parts = value.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean);
  if (parts.length <= 3) return value;
  return `共 ${parts.length} 个，示例 ${parts.slice(0, 3).join(",")}`;
}

function caseGoal(item: { goal?: string; title?: string }): string {
  const goal = (item.goal || "").trim();
  if (goal) return goal;
  const title = (item.title || "").trim();
  if (title) return title;
  return "按下面的做法在当前页面上做完，并看到通过条件。";
}

function caseStart(item: { start?: string }): string {
  const start = (item.start || "").trim();
  if (start) return start;
  const page = (plan.value?.ui?.pages ?? []).find((entry) => (entry.title_text || entry.id || "").trim());
  const title = (page?.title_text || page?.id || "").trim();
  if (title) return `从「${title}」这一页开始。`;
  return "从当前已经打开的页面开始。";
}

function preparedValue(value?: string): boolean {
  const text = (value || "").trim();
  return text === "使用库里已有数据" || text === "使用库里已有交易号";
}

function intention(step: { phase?: string; action?: string; name?: string; value?: string; option?: string }): string {
  const name = step.name?.trim() || "（没有名称）";
  let line = `${actionText[step.action || ""] || "操作"}「${name}」`;
  if (step.action === "type") {
    const value = (step.value || "").trim();
    if (!value || preparedValue(value)) line = `填写「${name}」，用库里已有的数据`;
    else line = `填写「${name}」，填入 ${sampleValue(value)}`;
  } else if (step.action === "select") {
    const option = (step.option || "").trim();
    line = option ? `选择「${name}」为「${option}」` : `选择「${name}」`;
  } else if (step.action === "set") {
    line = `勾选「${name}」`;
  } else if (step.action === "click") {
    line = `点击「${name}」`;
  }
  return step.phase === "seed" ? `造数时，${line}` : line;
}

function openKey(item: { id?: string }, index: number): string {
  return item.id || `open-${index}`;
}

function openPrompt(item: { id?: string; topic?: string; question?: string }): string {
  return (item.question || item.topic || item.id || "").trim();
}

function openNeedsAnswer(item: { needs_decision?: boolean; question?: string }): boolean {
  return item.needs_decision === true || Boolean((item.question || "").trim());
}

watch(() => run.value?.review?.id ?? "", (id) => {
  if (!id || id === loadedReview.value) return;
  const kind = run.value?.review?.kind;
  const payload = run.value?.review?.payload;
  if (kind === "plan") {
    loadedReview.value = id;
    tab.value = "ui";
    const next: Record<string, string> = {};
    for (const [index, item] of (payload?.open ?? []).entries()) {
      next[openKey(item, index)] = item.decision ?? "";
    }
    decisions.value = next;
    const count = payload?.ui?.seed_object_count;
    seedText.value = typeof count === "number" ? String(count) : "";
    return;
  }
  if (kind === "fixture_keys") {
    loadedReview.value = id;
    const next: Record<string, { database: string; pk: Record<string, string> }> = {};
    for (const check of payload?.checks ?? []) {
      if (!check.id) continue;
      const pk: Record<string, string> = {};
      for (const column of check.pk ?? []) pk[column] = "";
      next[check.id] = { database: "", pk };
    }
    fixtures.value = next;
    return;
  }
  if (kind === "sql_offer") {
    loadedReview.value = id;
    sqlText.value = draftSql(payload);
    sqlReason.value = "";
    sqlParameters.value = "";
    sqlDatabase.value = "";
    sqlRows.value = "1";
    sqlUntouched.value = "";
  }
});

function draftSql(payload: PlanDocument | undefined): string {
  const tables = payload?.tables ?? [];
  const table = tables[0];
  const column = table?.pk?.[0] || "detail_id";
  const stateField = table?.state_field || "status";
  const name = table?.name || "refunds";
  const fixture = payload?.fixtures?.[table?.check_id || ""];
  const value = fixture?.pk?.[column] || "DETAIL-01";
  return `UPDATE \`${name}\` SET \`${stateField}\` = '' WHERE \`${column}\` = '${value}'`;
}

async function postApproval(body: Record<string, unknown>) {
  saving.value = true;
  try {
    await api(`/api/runs/${runId.value}/approvals`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    message.value = "";
    await load();
  } catch (error) {
    message.value = error instanceof ApiError ? error.message : "确认没有写入";
  } finally {
    saving.value = false;
  }
}

async function saveBrowser() {
  if (!run.value || run.value.status === "running") return;
  saving.value = true;
  try {
    await api(`/api/runs/${runId.value}/browser`, {
      method: "POST",
      body: JSON.stringify({ browser: browserChoice.value }),
    });
    message.value = "";
    await load();
  } catch (error) {
    message.value = error instanceof ApiError ? error.message : "浏览器没有记下";
  } finally {
    saving.value = false;
  }
}

async function stopRun() {
  if (run.value?.status !== "running") return;
  saving.value = true;
  try {
    await api(`/api/runs/${runId.value}/stop`, { method: "POST", body: "{}" });
    message.value = "";
    await load();
  } catch (error) {
    message.value = error instanceof ApiError ? error.message : "没有停下来";
  } finally {
    saving.value = false;
  }
}

async function continueRounds() {
  if (run.value?.status !== "running" || !run.value.round_prompt) return;
  saving.value = true;
  try {
    await api(`/api/runs/${runId.value}/continue`, { method: "POST", body: "{}" });
    message.value = "";
    await load();
  } catch (error) {
    message.value = error instanceof ApiError ? error.message : "没有继续";
  } finally {
    saving.value = false;
  }
}

async function retryStep() {
  const step = retryChoice.value;
  if (!step || run.value?.status === "running") return;
  if (!window.confirm(`从「${step}」重试？这一步和后面的结果会重做，更早的记录留下。`)) return;
  saving.value = true;
  try {
    await api(`/api/runs/${runId.value}/retry`, {
      method: "POST",
      body: JSON.stringify({ step, browser: browserChoice.value }),
    });
    message.value = "";
    workspace.value = "run";
    await load();
  } catch (error) {
    message.value = error instanceof ApiError ? error.message : "没有从这一步重试";
  } finally {
    saving.value = false;
  }
}

watch(run, (value) => {
  const saved = value?.product_browser;
  if (!saving.value && (saved === "edge" || saved === "chrome")) browserChoice.value = saved;
  if (retryChoice.value && retryChoices.value.includes(retryChoice.value)) return;
  const events = value?.events ?? [];
  const found = [...events].reverse().find((event) => RETRY_STEPS.includes(event.step));
  retryChoice.value = found?.step || retryChoices.value[retryChoices.value.length - 1] || "执行";
});

async function confirmSeen() {
  const id = run.value?.review?.id;
  if (!id) return;
  await postApproval({ review_id: id });
}

async function submitSql() {
  const rows = Number(sqlRows.value);
  if (!Number.isInteger(rows) || rows < 1 || rows > 20) {
    message.value = "预期行数必须是 1 到 20 的整数";
    return;
  }
  await api(`/api/runs/${runId.value}/sql-proposal`, {
    method: "POST",
    body: JSON.stringify({
      reason: sqlReason.value,
      sql: sqlText.value,
      parameters: sqlParameters.value,
      database: sqlDatabase.value,
      expected_rows: rows,
      untouched_reason: sqlUntouched.value,
    }),
  }).then(async () => {
    message.value = "";
    await load();
  }).catch((error: unknown) => {
    message.value = error instanceof ApiError ? error.message : "改数没有写入";
  });
}

async function submitFixtures() {
  const id = run.value?.review?.id;
  if (!id) return;
  const body: Record<string, unknown> = { review_id: id, fixtures: {} as Record<string, unknown> };
  const filled = body.fixtures as Record<string, unknown>;
  for (const [checkId, item] of Object.entries(fixtures.value)) {
    filled[checkId] = { database: item.database, pk: { ...item.pk } };
  }
  await postApproval(body);
}

async function confirmPlan() {
  const id = run.value?.review?.id;
  if (!id) return;
  for (const [index, item] of (plan.value?.open ?? []).entries()) {
    if (openNeedsAnswer(item) && !(decisions.value[openKey(item, index)] || "").trim()) {
      tab.value = "open";
      message.value = "还有待决定没有填写";
      return;
    }
  }
  const body: Record<string, unknown> = {
    review_id: id,
    open_decisions: { ...decisions.value },
  };
  if (seedText.value.trim() !== "") {
    const count = Number(seedText.value);
    if (!Number.isInteger(count) || count < 0) {
      message.value = "铺垫对象数必须是大于等于 0 的整数";
      return;
    }
    body.seed_object_count = count;
  }
  await postApproval(body);
}

function rememberRun(record: RunRecord) {
  if (!record.id || !record.project_id) return;
  sessionStorage.setItem("qa-last-run", JSON.stringify({ id: record.id, project_id: record.project_id }));
}

async function load() {
  try {
    run.value = await api<RunRecord>(`/api/runs/${runId.value}`);
    if (run.value) rememberRun(run.value);
    message.value = "";
  } catch (error) {
    message.value = error instanceof ApiError ? error.message : "读不到这次运行";
  }
}

function schedule() {
  window.clearTimeout(timer);
  if (document.visibilityState === "hidden") return;
  timer = window.setTimeout(async () => {
    await load();
    schedule();
  }, 1000);
}

function onVisibility() {
  if (document.visibilityState === "hidden") {
    window.clearTimeout(timer);
    return;
  }
  schedule();
}

onMounted(async () => {
  await load();
  document.addEventListener("visibilitychange", onVisibility);
  schedule();
});

onUnmounted(() => {
  window.clearTimeout(timer);
  document.removeEventListener("visibilitychange", onVisibility);
  releaseReport();
  releaseShots();
});
</script>

<template>
  <div class="workspace">
    <header class="flow-head">
      <div>
        <h1>运行 {{ runId }}</h1>
        <p class="lede" v-if="run">{{ label }} · {{ countText }}</p>
      </div>
      <div class="workspace-tabs" role="tablist">
        <button type="button" :aria-selected="workspace === 'run'" @click="workspace = 'run'">本次运行</button>
        <button type="button" :aria-selected="workspace === 'config'" @click="workspace = 'config'">项目配置</button>
      </div>
    </header>
    <p v-if="message" class="banner bad" role="alert">{{ message }}</p>
    <ProjectConfig v-show="workspace === 'config' && run" class="config-pane" embedded :project-id="run?.project_id || ''" />
    <div v-if="run" v-show="workspace === 'run'" class="run-split">
      <aside class="run-side" aria-label="步骤和需求">
        <section class="side-block">
          <h2>步骤</h2>
          <ol class="pipeline">
            <li v-for="(item, index) in pipeline" :key="item.id">
              <button type="button" :class="['pipe', item.state, { picked: retryChoice === item.id }]" @click="retryChoice = item.id">
                <span class="pipe-index">{{ index + 1 }}</span>
                <strong>{{ item.id }}</strong>
                <small>{{ item.detail || item.title }}</small>
              </button>
            </li>
          </ol>
        </section>
        <section class="side-block">
          <h2>需求</h2>
          <dl class="req-facts">
            <div><dt>编号</dt><dd>{{ run.requirement?.id || "—" }}</dd></div>
            <div><dt>标题</dt><dd>{{ run.requirement?.title || "—" }}</dd></div>
            <div><dt>来源</dt><dd>{{ run.requirement?.source || "—" }}</dd></div>
            <div><dt>分支</dt><dd>{{ run.branch || "—" }}</dd></div>
            <div><dt>提交</dt><dd><code>{{ run.commit || "—" }}</code></dd></div>
            <div><dt>地址</dt><dd>{{ run.base_url || "—" }}</dd></div>
          </dl>
        </section>
        <div class="side-work">
      <section v-if="run.review?.kind === 'commits_empty'" class="plan-block">
        <h2>{{ run.review.title || "没有找到提交" }}</h2>
        <p class="hint">确认只表示已经看到这次空结果，不会因此请求模型或开始下一轮运行。</p>
        <div class="actions">
          <button class="btn primary" type="button" :disabled="saving" @click="confirmSeen">确认已看到</button>
        </div>
      </section>
      <section v-else-if="plan" class="plan-block">
        <h2>方案</h2>
        <p v-if="pendingPlan" class="hint">确认前停在方案，不进入数据。</p>
        <p v-else class="hint">方案已确认。</p>
        <div class="tabs" role="tablist">
          <button class="btn secondary" type="button" role="tab" :aria-selected="tab === 'ui'" @click="tab = 'ui'">界面与用例</button>
          <button class="btn secondary" type="button" role="tab" :aria-selected="tab === 'checks'" @click="tab = 'checks'">库检查</button>
          <button class="btn secondary" type="button" role="tab" :aria-selected="tab === 'open'" @click="tab = 'open'">待决定</button>
        </div>
        <div v-if="tab === 'ui'" role="tabpanel">
          <p class="hint">每条用例按操作页面时要看的说明来写：目标、起点、做法、通过。做法是参考，打开页面后以眼前的控件为准。确认前没有查库。{{ plan.ui?.step_source ? `来源：${plan.ui.step_source}。` : "" }}</p>
          <p v-if="unusedCheckNames.length" class="hint">库检查里还有这些表没有对应用例：{{ unusedCheckNames.join("、") }}。</p>
          <p v-for="page in plan.ui?.pages ?? []" :key="page.id || page.path" class="hint">
            材料里的页面：{{ page.title_text || page.id || "未命名" }}，路径 {{ page.path || "还没有路径" }}。
          </p>
          <div v-if="requirementShots.length" class="plan-shots">
            <p class="hint">需求图</p>
            <img v-for="(src, index) in requirementShots" :key="index" class="plan-shot" :src="src" alt="需求图" />
          </div>
          <p v-if="(plan.ui?.surfaces ?? []).length" class="hint">图上的页签：{{ (plan.ui?.surfaces ?? []).join("、") }}</p>
          <article v-for="item in plan.cases ?? []" :key="item.id" class="plan-block">
            <h2>{{ item.id }}<template v-if="item.title"> {{ item.title }}</template></h2>
            <dl class="req-facts">
              <div><dt>目标</dt><dd>{{ caseGoal(item) }}</dd></div>
              <div><dt>起点</dt><dd>{{ caseStart(item) }}</dd></div>
              <div><dt>通过</dt><dd>{{ caseExpect(item) || "还没有写页面上应出现的文字。" }}</dd></div>
              <div><dt>不要点</dt><dd>{{ avoidLine }}</dd></div>
              <div><dt>对不上</dt><dd>重新看当前页面，换一个控件。做法只是参考。</dd></div>
            </dl>
            <p class="hint">做法</p>
            <ol class="step-list">
              <li v-for="(step, index) in item.steps ?? []" :key="index">
                <p>{{ intention(step) }}</p>
                <p v-if="step.action === 'type' && step.value && !preparedValue(step.value)" class="hint">「{{ sampleValue(step.value) }}」是模型编来填的，不是从库里查到的数据。</p>
                <p v-if="step.locator_status === 'pending'" class="hint">这个名字还没在打开的页面上对过。</p>
              </li>
            </ol>
            <p v-if="caseOnlyReads(item)" class="hint">这组操作用来查询或导出，点完不会改数据库。</p>
            <p class="hint">库核对在确认方案之后才查。检查 <code>{{ item.check_id || "—" }}</code>，列 <code>{{ (item.pk_columns ?? []).join(", ") || "—" }}</code>。现在还没有具体主键值。</p>
            <p v-if="item.origin" class="hint">来源：{{ item.origin }}</p>
          </article>
          <article v-for="note in plan.ui?.code_notes ?? []" :key="note.check_id" class="plan-block">
            <h2>页面线索 {{ note.check_id }}</h2>
            <p class="hint">{{ note.text }}</p>
          </article>
        </div>
        <div v-else-if="tab === 'checks'" role="tabpanel">
          <p class="hint">库检查是确认方案之后才做的事：页面操作前，按主键找出一行，记下状态列；操作后再查一次，看这一列变了没有。用来核对数据库里的记录，页面上看不见的变化靠它。</p>
          <p class="hint">现在这一栏还没有查库。表名、主键列、状态列是模型读这次提交的源码后写下的计划。主键的具体值也还没有，确认方案后才会让你填要盯的那一行。</p>
          <p v-if="(plan.ui?.seed_object_count ?? 0) === 0" class="hint">铺垫对象数是 0，确认后也不会先往库里准备数据。</p>
          <article v-for="item in plan.checks ?? []" :key="item.id" class="plan-block">
            <h2>{{ item.id }}</h2>
            <p>表 <code>{{ item.table || "—" }}</code>。模型计划以后去这张表查，现在还没核对这张表在不在。</p>
            <p>主键 <code>{{ (item.pk ?? []).join(", ") || "—" }}</code>。以后用这一列的值认定是哪一行。具体值现在是空的。</p>
            <p>
              状态 <code>{{ item.state_field || "—" }}</code>。以后用这一列比较操作前和操作后。
              <template v-if="stateSameAsKey(item)">这一列和主键相同，查完只能确认这行还在。</template>
              <template v-else-if="checkOnlyReads(item)">对上的用例只查询或导出，不会改这张表。这一列不会因此变化，查完看不出查询条件有没有生效。</template>
            </p>
            <p class="hint">出处 {{ item.source || "—" }}。模型说它是从这行源码推断的，不是从数据库查到的。</p>
          </article>
        </div>
        <div v-else role="tabpanel">
          <p v-if="!(plan.open ?? []).length" class="hint">没有待决定。</p>
          <p v-else class="hint">这些是确认方案前要你拍板的问题。写出来的问题必须填写，空着不能确认。</p>
          <label v-for="(item, index) in plan.open ?? []" :key="openKey(item, index)" class="wide">
            {{ openPrompt(item) || "这一项没有写出问题" }}
            <textarea
              v-if="pendingPlan"
              v-model="decisions[openKey(item, index)]"
              :aria-invalid="openNeedsAnswer(item) && !(decisions[openKey(item, index)] || '').trim()"
              rows="3"
            ></textarea>
            <span v-else>{{ item.decision || "—" }}</span>
          </label>
          <label v-if="pendingPlan">
            铺垫对象数
            <input v-model="seedText" inputmode="numeric" />
            <span class="hint">测试前要准备几条业务数据。0 表示这次不造数。库里如果没有对应行，后面仍会因为查不到而停住。</span>
          </label>
          <p v-else class="hint">铺垫对象数 {{ plan.ui?.seed_object_count ?? "—" }}</p>
        </div>
        <div v-if="pendingPlan" class="actions">
          <button class="btn primary" type="button" :disabled="saving" @click="confirmPlan">确认方案</button>
        </div>
      </section>
      <section v-if="run.review?.kind === 'fixture_keys'" class="plan-block">
        <h2>{{ run.review.title || "填写主键" }}</h2>
        <p class="hint">主键是表里用来认出某一行的编号。填这张表里真实存在的那一行的值。</p>
        <p class="hint">点「按这个编号去查」之后，才会按这个编号做查询。现在还没查库，也不会改表。</p>
        <article v-for="check in run.review.payload.checks ?? []" :key="check.id" class="plan-block">
          <h2>{{ check.id }} · {{ check.table }}</h2>
          <p class="hint">查到行之后，用 {{ check.state_field || "状态列" }} 这一列看操作前后有没有变。模型说列名来自 {{ check.source || "源码" }}。</p>
          <label>
            库
            <select v-if="fixtures[check.id || '']" v-model="fixtures[check.id || ''].database">
              <option value="">选择库</option>
              <option v-for="name in check.allowed_databases ?? []" :key="name" :value="name">{{ name }}</option>
            </select>
          </label>
          <label v-for="column in check.pk ?? []" :key="column">
            {{ column }}
            <span class="hint">这一列的具体值。填 {{ check.table || "这张表" }} 里那一行的 {{ column }}。</span>
            <input v-if="fixtures[check.id || '']" v-model="fixtures[check.id || ''].pk[column]" />
          </label>
          <p v-if="check.suggestions?.length" class="hint">参考：{{ check.suggestions.join("、") }}。这只是提示，不会自动填进上面的框。</p>
        </article>
        <div class="actions">
          <button class="btn primary" type="button" :disabled="saving" @click="submitFixtures">按这个编号去查</button>
        </div>
      </section>
      <section v-else-if="run.review?.kind === 'baseline'" class="plan-block">
        <h2>{{ run.review.title || "基线" }}</h2>
        <p v-if="run.review.payload.strategy === 'page_seed'" class="hint">数据库只能查询。查询为空时不会铺垫，也不会改数。</p>
        <p v-else class="hint">确认后才能测。这一步只查询，不点击、不改数。</p>
        <article v-for="(rows, checkId) in run.review.payload.records ?? {}" :key="checkId" class="plan-block">
          <h2>{{ checkId }}</h2>
          <p class="hint">{{ rows.length ? `${rows.length} 行` : "0 行" }}<template v-if="sampleValues(checkId).length">。示例：{{ sampleValues(checkId).join("、") }}</template></p>
          <p v-if="!rows.length" class="hint">没有查到行。请在页面准备数据，或在库外插入。平台不会执行插入。准备好后从「基线」重试。</p>
          <p v-if="run.review.payload.select_sql?.[checkId]"><code>{{ run.review.payload.select_sql[checkId] }}</code></p>
        </article>
        <ul v-if="run.review.payload.seed_steps?.length" class="step-list">
          <li v-for="(step, index) in run.review.payload.seed_steps" :key="index">
            {{ step.phase }} · {{ step.action }} · {{ step.role }} · {{ step.name || "（空）" }} · {{ locatorText(step.locator_status) }}
          </li>
        </ul>
        <div v-if="run.review.payload.strategy !== 'page_seed'" class="actions">
          <button class="btn primary" type="button" :disabled="saving" @click="confirmSeen">确认基线</button>
        </div>
      </section>
      <section v-else-if="run.review?.kind === 'sql_offer' || run.review?.kind === 'sql'" class="plan-block">
        <h2>不能改数</h2>
        <p class="hint">数据库只能查询。这里不能提交 INSERT 或 UPDATE，也不能确认执行改数。</p>
      </section>
      <section v-else-if="run.review?.kind === 'baseline_actual'" class="plan-block">
        <h2>改数后的实际行</h2>
        <article v-for="(rows, checkId) in run.review.payload.actual ?? {}" :key="checkId" class="plan-block">
          <h2>{{ checkId }}</h2>
          <p class="hint">{{ rows.length }} 行</p>
        </article>
        <div class="actions">
          <button class="btn primary" type="button" :disabled="saving" @click="confirmSeen">确认实际行</button>
        </div>
      </section>
      <section v-if="run.cases.length" class="plan-block">
        <h2>用例</h2>
        <p v-if="run.current_case" class="hint">正在跑 {{ run.current_case }}。这一轮看到：{{ run.current_seen || "—" }}</p>
        <article v-for="item in run.cases" :key="item.case_id" class="plan-block">
          <h2>{{ item.case_id }} {{ item.title }} · {{ caseText[item.status] || item.status }}</h2>
          <p v-if="item.observed && item.status === 'running'" class="hint">已观察，尚未封口</p>
          <p v-if="item.pending" class="hint">{{ item.pending.mark || "尚未核对" }}</p>
          <p v-if="item.blocked_reason" class="hint">{{ item.blocked_reason }}</p>
          <p v-if="item.seen" class="hint">这一轮看到：{{ item.seen }}</p>
        </article>
      </section>
        </div>
      </aside>
      <section class="run-feed" aria-label="运行进度">
        <div class="flow-tools">
          <p v-if="run.status === 'running' && run.round_prompt" class="banner">{{ run.round_prompt.question }}可以停止，也可以让它继续。</p>
          <p v-else-if="run.status === 'running'" class="banner">正在进行<template v-if="latest"> · {{ eventLabel(latest) }}</template></p>
          <div v-else-if="run.status === 'complete'" class="banner ok report-banner">
            <span>报告已生成 · {{ countText }}</span>
            <button class="btn secondary" type="button" @click="toggleReport">{{ reportOpen ? "收起报告" : "测试报告" }}</button>
          </div>
          <p v-else-if="run.status === 'blocked'" class="banner bad">已阻塞。<template v-if="latest">{{ latest.basis }}</template></p>
          <p v-else-if="run.status === 'paused'" class="banner">已暂停。<template v-if="latest">{{ latest.basis }}</template></p>
          <p v-else-if="run.status === 'stopped'" class="banner">已手动停止。可以从这一步重试。</p>
          <p v-else-if="run.status === 'review'" class="banner">等待确认。<template v-if="latest">{{ eventLabel(latest) }}</template></p>
          <div class="retry-row">
            <label>
              浏览器
              <select v-model="browserChoice" :disabled="saving || run.status === 'running'" @change="saveBrowser">
                <option value="edge">Edge</option>
                <option value="chrome">Chrome</option>
              </select>
            </label>
            <label>
              从这一步重试
              <select v-model="retryChoice" :disabled="saving || run.status === 'running'">
                <option v-for="step in retryChoices" :key="step" :value="step">{{ step }}</option>
              </select>
            </label>
            <button v-if="run.status === 'running' && run.round_prompt" class="btn primary" type="button" :disabled="saving" @click="continueRounds">继续</button>
            <button v-if="run.status === 'running'" class="btn secondary" type="button" :disabled="saving" @click="stopRun">停止</button>
            <button class="btn secondary" type="button" :disabled="saving || run.status === 'running' || !retryChoice" @click="retryStep">重试</button>
          </div>
        </div>
        <article v-if="reportOpen" class="report-sheet" aria-label="测试报告">
          <p v-if="reportError" class="hint">{{ reportError }}</p>
          <template v-for="(line, index) in reportLines" :key="index">
            <h2 v-if="line.kind === 'h'">{{ line.text }}</h2>
            <p v-else-if="line.kind === 'p'">{{ line.text }}</p>
            <figure v-else>
              <img :src="line.src" :alt="line.text" />
              <figcaption>{{ line.text }}</figcaption>
            </figure>
          </template>
        </article>
        <div ref="progressEl" class="run-progress" @scroll="onProgressScroll">
          <h2>进度</h2>
          <p v-if="!progressEvents.length" class="hint">还没有进度。</p>
          <ol v-else class="events">
            <li
              v-for="(event, index) in progressEvents"
              :key="`${event.time}-${index}`"
              :class="[eventTone(event.basis), { current: run.status === 'running' && index === progressEvents.length - 1 }]"
            >
              <time>{{ event.time }}</time>
              <strong>{{ eventLabel(event) }}</strong>
              <span>{{ eventBody(event.basis) }}</span>
            </li>
          </ol>
        </div>
      </section>
    </div>
  </div>
</template>
