"use strict";

const byId = id => document.getElementById(id);
const controls = new Map();
const labels = {
  running: "运行中", succeeded: "已完成", failed: "运行失败",
  cancelled: "已取消", timed_out: "已超时", interrupted: "已中断",
};
const dateFormat = new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "medium" });
let config;
let selected;
let busy = false;
let sending = false;
let revision = 0;
let historyKey = "";
let displayKey = "";
let logKey = null;
let pollTimer;

function element(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}

async function request(path, body) {
  const response = await fetch(path, body === undefined ? undefined : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败。");
  return result;
}

function showError(message = "") {
  byId("error").textContent = message;
  byId("error").hidden = !message;
}

function updateButtons() {
  byId("start").disabled = !config || busy || sending;
  byId("replay").disabled = busy || sending;
  byId("cancel").disabled = sending;
}

function createField(field) {
  const wrap = element("div", undefined, "field");
  const label = element("label", field.label);
  const control = element(field.type === "select" ? "select" : "input");
  control.id = "field-" + field.id;
  label.htmlFor = control.id;
  controls.set(field.id, { control, sample: null });
  wrap.append(label, control);

  if (field.type === "boolean") {
    wrap.classList.add("toggle");
    control.type = "checkbox";
    control.checked = field.default;
    wrap.prepend(control);
  } else if (field.type === "select") {
    for (const option of field.options) control.append(element("option", option));
    control.value = field.default;
  } else if (field.type === "file") {
    control.type = "file";
    control.required = true;
    control.setAttribute("aria-describedby", control.id + "-hint");
    if (field.accept) control.accept = field.accept.join(",");
    const tools = element("div", undefined, "file-tools");
    const hint = element("span", "单个文件不超过 4 MiB", "hint");
    hint.id = control.id + "-hint";
    if (field.sample) {
      const sample = element("button", "使用示例");
      sample.type = "button";
      sample.addEventListener("click", async () => {
        showError();
        sample.disabled = true;
        try {
          const data = await request("/api/sample/" + field.id);
          controls.get(field.id).sample = data;
          control.value = "";
          control.required = false;
          hint.textContent = "已选示例 " + data.name;
        } catch (error) {
          showError(error.message);
        } finally {
          sample.disabled = false;
        }
      });
      tools.append(sample);
    }
    control.addEventListener("change", () => {
      controls.get(field.id).sample = null;
      control.required = true;
      hint.textContent = "单个文件不超过 4 MiB";
    });
    tools.append(hint);
    wrap.append(tools);
  } else {
    const numeric = field.type === "integer" || field.type === "number";
    control.type = numeric ? "number" : "text";
    control.value = field.default;
    if (numeric) {
      control.required = true;
      control.step = field.type === "integer" ? "1" : "any";
      if (field.min !== undefined) control.min = field.min;
      if (field.max !== undefined) control.max = field.max;
    } else {
      control.maxLength = 4000;
    }
  }
  return wrap;
}

function fileData(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, data: reader.result.split(",", 2)[1] });
    reader.onerror = () => reject(new Error("无法读取 " + file.name));
    reader.readAsDataURL(file);
  });
}

async function formValues() {
  const entries = await Promise.all(config.fields.map(async field => {
    const { control, sample } = controls.get(field.id);
    let value = control.value;
    if (field.type === "file") {
      const file = control.files[0];
      if (!file && !sample) throw new Error("请选择「" + field.label + "」的文件。");
      if (file && file.size > config.file_limit) throw new Error("单个文件不能超过 4 MiB。");
      value = file ? await fileData(file) : sample;
    } else if (field.type === "number" || field.type === "integer") {
      value = control.valueAsNumber;
    } else if (field.type === "boolean") {
      value = control.checked;
    }
    return [field.id, value];
  }));
  return Object.fromEntries(entries);
}

function renderHistory(history) {
  const key = JSON.stringify([selected, history.map(item => [item.id, item.status])]);
  if (historyKey === key) return;
  historyKey = key;
  byId("history-empty").hidden = history.length > 0;
  const items = history.map(run => {
    const button = element("button");
    button.type = "button";
    button.dataset.run = run.id;
    if (run.id === selected) button.setAttribute("aria-current", "true");
    button.append(
      element("span", dateFormat.format(new Date(run.started)) + " · " + run.id.slice(0, 6)),
      element("span", labels[run.status], "hint"),
    );
    button.addEventListener("click", () => {
      select(run.id);
      refresh().catch(error => showError(error.message));
    });
    const row = element("li");
    row.append(button);
    return row;
  });
  const focused = document.activeElement?.dataset.run;
  byId("history").replaceChildren(...items);
  if (focused) byId("history").querySelector("[data-run='" + focused + "']")?.focus({ preventScroll: true });
}

function renderRun(run) {
  byId("empty").hidden = true;
  byId("details").hidden = false;
  const key = [run.id, run.status].join("/");
  if (displayKey !== key) {
    displayKey = key;
    const status = byId("status");
    status.dataset.state = run.status;
    status.textContent = labels[run.status];
    const runInfo = [dateFormat.format(new Date(run.started)), run.id.slice(0, 6)];
    if (run.finished) {
      const seconds = (new Date(run.finished) - new Date(run.started)) / 1000;
      runInfo.push("耗时 " + seconds.toFixed(1) + " 秒");
    }
    if (run.exit_code !== null && run.exit_code !== 0) runInfo.push("退出码 " + run.exit_code);
    if (run.replay_of) runInfo.push("复跑自 " + run.replay_of.slice(0, 6));
    byId("run-time").textContent = runInfo.join(" · ");
    byId("cancel").hidden = run.status !== "running";
    byId("replay").hidden = run.status === "running";
    byId("parameters").replaceChildren(...Object.entries(run.parameters).map(([id, value]) => {
      const row = element("div");
      const title = config.fields.find(field => field.id === id)?.label || id;
      const text = typeof value === "object" ? value.name
        : typeof value === "boolean" ? (value ? "是" : "否") : String(value);
      row.append(element("dt", title), element("dd", text));
      return row;
    }));
    byId("outputs-empty").hidden = run.outputs.length > 0;
    byId("outputs-empty").textContent = run.status === "running" ? "等待脚本写完结果。" : "这次运行没有输出文件。";
    byId("outputs").replaceChildren(...run.outputs.map(output => {
      const row = element("li");
      const link = element("a", output.name);
      link.href = "/api/runs/" + run.id + "/outputs/" + output.name.split("/").map(encodeURIComponent).join("/");
      link.download = "";
      row.append(link, element("span", output.size.toLocaleString("zh-CN") + " B", "hint"));
      return row;
    }));
  }
  const log = byId("log");
  const message = (run.log_truncated ? "只显示最后 32 KiB。\n\n" : "") + (run.log || "暂无日志。");
  if (logKey !== message) {
    const follow = log.scrollHeight - log.scrollTop - log.clientHeight < 20;
    logKey = message;
    log.textContent = message;
    if (follow) log.scrollTop = log.scrollHeight;
  }
}

function select(id) {
  selected = id;
  revision += 1;
}

async function refresh() {
  const currentRevision = revision;
  const history = await request("/api/runs");
  if (currentRevision !== revision) return;
  busy = history.some(run => run.status === "running");
  if (!selected && history.length) selected = history[0].id;
  renderHistory(history);
  updateButtons();
  if (selected) {
    const run = await request("/api/runs/" + selected);
    if (currentRevision === revision) renderRun(run);
  }
}

async function runAction(action) {
  if (sending) return;
  showError();
  sending = true;
  updateButtons();
  try {
    const run = await action();
    if (run.id) select(run.id);
    await refresh();
  } catch (error) {
    showError(error.message);
  } finally {
    sending = false;
    updateButtons();
    schedulePoll();
  }
}

function schedulePoll() {
  window.clearTimeout(pollTimer);
  pollTimer = window.setTimeout(poll, busy ? 200 : 1500);
}

async function poll() {
  try {
    await refresh();
  } catch (error) {
    showError(error.message);
  }
  schedulePoll();
}

async function initialize() {
  try {
    config = await request("/api/config");
    byId("title").textContent = config.title;
    document.title = config.title + " · Script Run Viewer";
    byId("description").textContent = config.description;
    byId("fields").append(...config.fields.map(createField));
    byId("form").addEventListener("submit", event => {
      event.preventDefault();
      runAction(async () => request("/api/runs", await formValues()));
    });
    byId("cancel").addEventListener("click", () => runAction(() => request("/api/runs/" + selected + "/cancel", {})));
    byId("replay").addEventListener("click", () => runAction(() => request("/api/runs/" + selected + "/replay", {})));
    poll();
  } catch (error) {
    showError(error.message);
  }
}

initialize();
