<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError, api, type Project, type RunCreated, type RunRecord, type Validation } from "../api";

const props = withDefaults(defineProps<{ embedded?: boolean; projectId?: string }>(), {
  embedded: false,
  projectId: "",
});
const router = useRouter();
const route = useRoute();

const fieldLabels: Record<string, string> = {
  repo_path: "本地仓库",
  branch: "分支",
  git_remote: "Git 远程名",
  yunxiao_entry: "云效入口",
  yunxiao_organization_id: "组织 ID",
  yunxiao_browser_app: "云效浏览器应用",
  base_url: "测试站 base_url",
  allowed_hosts: "允许的域名",
  allowed_databases: "库名单",
};

const errorFields: Record<string, string> = {
  repo_path: "repo_path",
  branch: "branch",
  git_remote: "git_remote",
  yunxiao_entry: "yunxiao_entry",
  yunxiao_edition: "yunxiao_edition",
  base_url: "base_url",
  base_url_host: "base_url",
  allowed_hosts: "allowed_hosts",
  allowed_databases: "allowed_databases",
  ocu_unavailable: "yunxiao_browser_app",
};

const statusText: Record<string, string> = {
  running: "执行中",
  review: "待确认",
  paused: "暂停",
  blocked: "阻塞",
  complete: "报告已生成",
  interrupted: "已停下",
};

const projects = ref<Project[]>([]);
const current = ref<Project | null>(null);
const resume = ref<RunRecord | null>(null);
const validation = ref<Validation | null>(null);
const loaded = ref(false);
const action = ref("");
const message = ref("");
const tone = ref<"ok" | "bad" | "">("");
const form = reactive({
  name: "",
  repo_path: "",
  branch: "",
  git_remote: "origin",
  yunxiao_entry: "",
  yunxiao_organization_id: "",
  yunxiao_edition: "center",
  yunxiao_browser_app: "",
  product_browser: "edge",
  base_url: "",
  allowed_hosts: "",
  allowed_databases: "oms3_onlinepublish_biz",
});

const busy = computed(() => action.value !== "");
const resumeLatest = computed(() => {
  const events = resume.value?.events ?? [];
  return events.length ? events[events.length - 1] : null;
});
const tokenOn = computed(() => (validation.value?.token_configured ?? current.value?.token_configured) === true);
const orgRequired = computed(() => form.yunxiao_edition !== "region");
const appRequired = computed(() => !tokenOn.value);

function lines(value: string): string[] {
  return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
}

function fill(project: Project) {
  form.name = project.name;
  form.repo_path = project.repo_path;
  form.branch = project.branch;
  form.git_remote = project.git_remote || "origin";
  form.yunxiao_entry = project.yunxiao_entry;
  form.yunxiao_organization_id = project.yunxiao_organization_id;
  form.yunxiao_edition = project.yunxiao_edition || "center";
  form.yunxiao_browser_app = project.yunxiao_browser_app;
  form.product_browser = project.product_browser === "chrome" ? "chrome" : "edge";
  form.base_url = project.base_url;
  form.allowed_hosts = project.allowed_hosts.join("\n");
  form.allowed_databases = project.allowed_databases.join("\n");
}

function apply(project: Project) {
  current.value = project;
  fill(project);
  const index = projects.value.findIndex((item) => item.id === project.id);
  if (index >= 0) {
    projects.value[index] = project;
  }
}

function payload() {
  return {
    name: form.name,
    repo_path: form.repo_path,
    branch: form.branch,
    git_remote: form.git_remote,
    yunxiao_entry: form.yunxiao_entry,
    yunxiao_organization_id: form.yunxiao_organization_id,
    yunxiao_edition: form.yunxiao_edition,
    yunxiao_browser_app: form.yunxiao_browser_app,
    product_browser: form.product_browser,
    base_url: form.base_url,
    allowed_hosts: lines(form.allowed_hosts),
    allowed_databases: lines(form.allowed_databases),
  };
}

function invalid(field: string): boolean {
  const result = validation.value;
  if (!result) return false;
  if (result.missing.includes(field)) return true;
  return result.errors.some((item) => errorFields[item.code] === field);
}

function isValidation(body: unknown): body is Validation {
  return typeof body === "object" && body !== null && Array.isArray((body as Validation).missing);
}

function rememberedRun(): string {
  const queryRun = String(route.query.run || "");
  if (/^run-[a-f0-9]{12}$/.test(queryRun)) return queryRun;
  try {
    const raw = sessionStorage.getItem("qa-last-run");
    if (!raw) return "";
    const saved = JSON.parse(raw) as { id?: string };
    return typeof saved.id === "string" && /^run-[a-f0-9]{12}$/.test(saved.id) ? saved.id : "";
  } catch {
    return "";
  }
}

let resumeTicket = 0;

async function loadResume() {
  const ticket = ++resumeTicket;
  const projectId = current.value?.id;
  const runId = rememberedRun();
  if (!projectId || !runId) {
    resume.value = null;
    return;
  }
  try {
    const record = await api<RunRecord>(`/api/runs/${runId}`);
    if (ticket !== resumeTicket) return;
    resume.value = record.project_id === projectId ? record : null;
  } catch {
    if (ticket === resumeTicket) resume.value = null;
  }
}

async function openProject(id: string) {
  const project = await api<Project>(`/api/projects/${id}`);
  apply(project);
  validation.value = await api<Validation>(`/api/projects/${id}/validate`, { method: "POST" });
  message.value = "";
  tone.value = "";
  await loadResume();
}

async function refresh(selectId?: string) {
  const data = await api<{ projects: Project[] }>("/api/projects");
  projects.value = data.projects;
  const id = selectId ?? (props.projectId || current.value?.id);
  const found = data.projects.find((item) => item.id === id) ?? data.projects[0];
  if (!found) {
    current.value = null;
    validation.value = null;
    return;
  }
  await openProject(found.id);
}

async function persist(): Promise<Project> {
  const id = current.value?.id;
  if (!id) throw new ApiError(400, { error: "还没有项目" });
  const saved = await api<Project>(`/api/projects/${id}`, {
    method: "PUT",
    body: JSON.stringify(payload()),
  });
  apply(saved);
  validation.value = await api<Validation>(`/api/projects/${saved.id}/validate`, { method: "POST" });
  return saved;
}

async function createProject() {
  action.value = "create";
  try {
    const created = await api<Project>("/api/projects", { method: "POST", body: "{}" });
    await refresh(created.id);
    message.value = "已新建项目";
    tone.value = "ok";
  } catch (error) {
    message.value = error instanceof ApiError ? error.message : "新建失败";
    tone.value = "bad";
  } finally {
    action.value = "";
  }
}

async function save() {
  action.value = "save";
  try {
    await persist();
    message.value = "已保存";
    tone.value = "ok";
  } catch (error) {
    message.value = error instanceof ApiError ? error.message : "保存失败";
    tone.value = "bad";
  } finally {
    action.value = "";
  }
}

async function startRun() {
  action.value = "start";
  message.value = "";
  try {
    const saved = await persist();
    const created = await api<RunCreated>(`/api/projects/${saved.id}/runs`, { method: "POST" });
    await router.push(`/runs/${created.run}`);
  } catch (error) {
    tone.value = "bad";
    if (error instanceof ApiError && error.status === 409 && isValidation(error.body)) {
      validation.value = error.body;
      message.value = "配置不齐，没有创建运行。";
    } else {
      message.value = error instanceof ApiError ? error.message : "创建运行失败";
    }
  } finally {
    action.value = "";
  }
}

async function removeProject() {
  if (!current.value) return;
  const label = current.value.name || current.value.id;
  if (!window.confirm(`删除项目 ${label}？已结束的运行记录会留在磁盘上。`)) return;
  action.value = "delete";
  try {
    await api<void>(`/api/projects/${current.value.id}`, { method: "DELETE" });
    current.value = null;
    validation.value = null;
    await refresh();
    message.value = "已删除项目";
    tone.value = "ok";
  } catch (error) {
    message.value = error instanceof ApiError ? error.message : "删除失败";
    tone.value = "bad";
  } finally {
    action.value = "";
  }
}

onMounted(async () => {
  try {
    await refresh();
  } catch (error) {
    message.value = error instanceof ApiError ? error.message : "读取项目失败";
    tone.value = "bad";
  } finally {
    loaded.value = true;
  }
});
</script>

<template>
  <div :class="embedded ? 'embedded-config' : 'layout'">
    <aside v-if="!embedded" class="panel">
      <div class="panel-head">
        <h2>项目</h2>
        <button type="button" class="btn secondary" :disabled="busy" @click="createProject">新建</button>
      </div>
      <p v-if="projects.length === 0" class="hint">还没有项目。</p>
      <ul v-else class="project-list">
        <li v-for="project in projects" :key="project.id">
          <button
            type="button"
            :aria-current="project.id === current?.id ? 'true' : undefined"
            @click="openProject(project.id)"
          >
            <strong>{{ project.name || "未命名项目" }}</strong>
            <small>{{ project.id }}</small>
          </button>
        </li>
      </ul>
    </aside>

    <section>
      <p v-if="!loaded">正在读取项目…</p>
      <div v-else-if="!current" class="card empty">
        <h1>项目配置</h1>
        <p>先建一个项目，再填写仓库、分支、云效入口、测试站地址和域名。缺任何一项都不能创建运行。</p>
        <button type="button" class="btn primary" :disabled="busy" @click="createProject">新建项目</button>
      </div>
      <form v-else class="card" @submit.prevent="save">
        <div class="form-head">
          <div>
            <h1>{{ form.name || "未命名项目" }}</h1>
            <p class="lede">{{ current.id }} · 保存的是整份配置，没填的字段会清空。</p>
          </div>
        </div>

        <dl class="facts">
          <div>
            <dt>令牌</dt>
            <dd>{{ tokenOn ? "已配置" : "未配置" }}</dd>
          </div>
          <div>
            <dt>当前分支</dt>
            <dd>{{ current.current_branch ?? "—" }}</dd>
          </div>
          <div>
            <dt>当前 commit</dt>
            <dd><code>{{ current.current_commit ?? "—" }}</code></dd>
          </div>
          <div>
            <dt>ocu</dt>
            <dd>{{ validation?.ocu_available ? "可找到" : "未找到" }}</dd>
          </div>
        </dl>
        <p v-if="current.git_error" class="warn">{{ current.git_error }}</p>

        <fieldset>
          <legend>仓库</legend>
          <div class="grid">
            <label class="wide">
              项目名称
              <input id="project-name" v-model="form.name" autocomplete="off">
            </label>
            <label class="wide">
              本地仓库
              <input id="repo-path" v-model="form.repo_path" autocomplete="off" spellcheck="false" :aria-invalid="invalid('repo_path')">
              <small>本机 Git 工作树的路径，不是远程地址。</small>
            </label>
            <label>
              分支
              <input id="branch" v-model="form.branch" autocomplete="off" spellcheck="false" :aria-invalid="invalid('branch')">
            </label>
            <label>
              Git 远程名
              <input id="git-remote" v-model="form.git_remote" autocomplete="off" spellcheck="false" :aria-invalid="invalid('git_remote')">
            </label>
          </div>
        </fieldset>

        <fieldset>
          <legend>云效</legend>
          <div class="grid">
            <label class="wide">
              云效入口
              <input id="yunxiao-entry" v-model="form.yunxiao_entry" autocomplete="off" spellcheck="false" placeholder="https://devops.aliyun.com/..." :aria-invalid="invalid('yunxiao_entry')">
            </label>
            <label>
              版本
              <select id="yunxiao-edition" v-model="form.yunxiao_edition" :aria-invalid="invalid('yunxiao_edition')">
                <option value="center">center · 中心版</option>
                <option value="region">region · 区域版</option>
              </select>
            </label>
            <label>
              组织 ID
              <input id="organization-id" v-model="form.yunxiao_organization_id" autocomplete="off" spellcheck="false" :aria-invalid="invalid('yunxiao_organization_id')">
              <small>{{ orgRequired ? "中心版必填。" : "区域版可以留空。" }}</small>
            </label>
            <label class="wide">
              云效浏览器应用
              <input id="browser-app" v-model="form.yunxiao_browser_app" autocomplete="off" :aria-invalid="invalid('yunxiao_browser_app')">
              <small>{{ appRequired ? "没有令牌时必填，填已登录浏览器的显示名或 bundle id。" : "已配置令牌时可以留空。" }}</small>
            </label>
          </div>
        </fieldset>

        <fieldset>
          <legend>测试站</legend>
          <div class="grid">
            <label>
              执行浏览器
              <select id="product-browser" v-model="form.product_browser">
                <option value="edge">Edge</option>
                <option value="chrome">Chrome</option>
              </select>
              <small>执行时只打开选定的浏览器。请先把它打开并登录测试站。</small>
            </label>
            <label class="wide">
              base_url
              <input id="base-url" v-model="form.base_url" autocomplete="off" spellcheck="false" placeholder="http://127.0.0.1:9000" :aria-invalid="invalid('base_url')">
              <small>只写协议、主机和可选端口，不要带路径。</small>
            </label>
            <label>
              允许的域名
              <textarea id="allowed-hosts" v-model="form.allowed_hosts" spellcheck="false" :aria-invalid="invalid('allowed_hosts')"></textarea>
              <small>一行一个 host，不含协议、路径和端口。必须包含 base_url 的 host。</small>
            </label>
            <label>
              库名单
              <textarea id="allowed-databases" v-model="form.allowed_databases" spellcheck="false" :aria-invalid="invalid('allowed_databases')"></textarea>
              <small>一行一个库名。默认 oms3_onlinepublish_biz。</small>
            </label>
          </div>
        </fieldset>

        <div v-if="validation && !validation.ok" class="issues" role="alert">
          <p>还不能创建运行。</p>
          <ul>
            <li v-for="field in validation.missing" :key="field">缺少{{ fieldLabels[field] || field }}</li>
            <li v-for="item in validation.errors" :key="item.code">{{ item.message }}</li>
          </ul>
        </div>
        <p v-else-if="validation?.ok && !resume" class="banner ok">配置已齐，可以创建运行。</p>
        <p v-if="resume" class="banner">
          这次运行还在，状态是{{ statusText[resume.status] || resume.status }}。
          <template v-if="resumeLatest">{{ resumeLatest.step }}。{{ resumeLatest.basis }}</template>
          回到这次运行不会重新读需求，也不会重新生成方案。
        </p>
        <p v-if="validation?.dirty" class="warn">工作区有未提交改动。创建运行不会拉取或覆盖这些文件。</p>
        <p v-if="message" class="banner" :class="tone" role="status">{{ message }}</p>

        <div class="actions">
          <button type="button" class="btn danger" :disabled="busy" @click="removeProject">删除项目</button>
          <button type="submit" class="btn secondary" :disabled="busy">保存</button>
          <button type="button" :class="resume ? 'btn secondary' : 'btn primary'" :disabled="busy" @click="startRun">
            {{ resume ? "新开一次运行" : "开始运行" }}
          </button>
          <router-link v-if="resume && !embedded" class="btn primary" :to="`/runs/${resume.id}`">回到这次运行</router-link>
        </div>
      </form>
    </section>
  </div>
</template>
