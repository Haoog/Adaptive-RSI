"use strict";
window.RSIExperience = (() => {
  const waiting = {
    dependency: "等待前置任务合并",
    backoff: "等待连接重试时间",
    paused: "项目已暂停",
    running: "等待当前任务结束",
    ready: "可开始",
    working_hours: "等待工作时段",
  };
  let liveTimer,
    liveBusy = false,
    lastDetail = "",
    inboxBusy = false,
    inboxAt = 0;
  const notificationKey = "rsi-notifications:" + location.origin;
  function readSaved(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem(key)) || fallback;
    } catch {
      return fallback;
    }
  }
  let seen = readSaved(notificationKey + ":seen", {});
  function queueCopy(task) {
    return [
      t(waiting[task.wait_reason] || task.wait_reason),
      task.queue_position ? t("队列位置") + " " + task.queue_position : "",
      task.available > Date.now() / 1000 ? stamp(task.available) : "",
      task.dependency?.title || "",
      task.urgency ? t("紧急") : t("常规"),
    ]
      .filter(Boolean)
      .join(" · ");
  }
  function dialog(title) {
    const box = el("dialog", undefined, "experience-dialog"),
      heading = el("div", undefined, "dialog-heading");
    heading.append(
      el("h2", t(title)),
      action(t("关闭"), () => box.close(), "text-button"),
    );
    box.append(heading);
    box.addEventListener("close", () => box.remove());
    document.body.append(box);
    box.showModal();
    return box;
  }
  async function openTask(task) {
    stashSettings();
    stashGoalDraft();
    closeTaskDetail();
    projectId = task.project;
    localStorage.setItem("rsi-project", projectId);
    restoreGoalDraft();
    settingsBaseline = null;
    taskOffset = 0;
    showView("overview");
    await refresh();
    await showTask(task);
  }
  async function inbox() {
    const box = dialog("所有项目待办"),
      data = await api("attention");
    if (!box.isConnected) return;
    box.append(el("p", t("查看通知不会替你处理任务。"), "muted"));
    const notify = action(t("启用系统通知"), async () => {
      if (window.RSIDesktop) {
        localStorage.setItem(notificationKey, "true");
        toast(t("通知已启用"));
        return;
      }
      if (!("Notification" in window)) {
        toast(t("浏览器不支持系统通知"));
        return;
      }
      const result = await Notification.requestPermission();
      localStorage.setItem(
        notificationKey,
        JSON.stringify(result === "granted"),
      );
      toast(
        t(
          result === "granted"
            ? "通知已启用"
            : "通知未获授权，仍可在待办中处理。",
        ),
      );
    });
    box.append(
      notify,
      action(
        t("关闭系统通知"),
        () => {
          localStorage.setItem(notificationKey, "false");
          toast(t("系统通知已关闭"));
        },
        "text-button",
      ),
    );
    for (const task of data.tasks) {
      const row = el("article", undefined, "inbox-row"),
        project = data.projects.find((p) => p.id === task.project);
      row.append(
        el("small", project?.name || task.project),
        action(
          task.title,
          () => {
            box.close();
            return openTask(task);
          },
          "task-title",
        ),
        badge(task.display_state || task.state),
      );
      box.append(row);
    }
    if (!data.tasks.length) box.append(el("p", t("所有项目暂无待处理事项。")));
    if (data.pagination.has_more)
      box.append(el("p", t("仅显示最近一页，请进入各项目查看全部待办。")));
  }
  async function pollInbox() {
    if (inboxBusy || Date.now() - inboxAt < 15000) return;
    inboxBusy = true;
    try {
      const data = await api("attention");
      inboxAt = Date.now();
      $("attention-inbox").textContent =
        t("所有项目待办") + " · " + data.pagination.total;
      const attention = new Set([
        "awaiting_approval",
        "blocked",
        "failed",
        "insufficient_evidence",
        "interrupted",
        "expired",
        "merged",
        "completed",
      ]);
      const next = {};
      for (const task of data.notifications || []) {
        let result = {};
        try {
          result = JSON.parse(task.result || "{}");
        } catch {}
        const status =
            result.delivery_status === "ci_failed" ? "ci_failed" : task.state,
          signature = JSON.stringify([
            task.run,
            status,
            result.failure?.code,
            result.failure?.detail,
            result.reason,
          ]);
        next[task.id] = signature;
        if (
          seen[task.id] !== signature &&
          (attention.has(status) || status === "ci_failed") &&
          readSaved(notificationKey, false)
        ) {
          const body = t(statusNames[status] || status),
            title =
              "Adaptive RSI · " +
              (data.projects.find((p) => p.id === task.project)?.name || "");
          if (window.RSIDesktop)
            window.RSIDesktop.invoke("notify", { title, body }).catch(() => {});
          else if (
            "Notification" in window &&
            Notification.permission === "granted"
          ) {
            const notification = new Notification(title, {
              body,
              tag: task.id,
            });
            notification.onclick = () => {
              window.focus();
              safe(() => openTask(task));
              notification.close();
            };
          }
        }
      }
      seen = Object.fromEntries(Object.entries({ ...seen, ...next }).slice(-5000));
      localStorage.setItem(notificationKey + ":seen", JSON.stringify(seen));
    } catch {
    } finally {
      inboxBusy = false;
    }
  }
  function enhanceOutcome(outcome, data) {
    const result = data.result;
    const checks = result.evaluation?.checks || {},
      before = checks.acceptance_baseline || [],
      after = checks.acceptance_candidate || [];
    if (before.length || after.length) {
      const table = el("table", undefined, "evidence-table"),
        head = el("tr");
      for (const label of ["独立验收", "修改前", "修改后"])
        head.append(el("th", t(label)));
      table.append(head);
      for (let i = 0; i < Math.max(before.length, after.length); i++) {
        const row = el("tr");
        row.append(
          el("td", after[i]?.name || before[i]?.name || String(i + 1)),
          el(
            "td",
            t(before[i] ? (before[i].passed ? "通过" : "未通过") : "未知"),
          ),
          el(
            "td",
            t(after[i] ? (after[i].passed ? "通过" : "未通过") : "未知"),
          ),
        );
        table.append(row);
      }
      outcome.append(table);
    }
    for (const finding of result.review?.findings || []) {
      const card = el("div", undefined, "blocking-finding");
      card.append(
        el("strong", finding.title || t("评审问题")),
        el(
          "p",
          (finding.path || "") +
            ":" +
            finding.line +
            " · " +
            (finding.reason || ""),
        ),
        el("p", finding.suggestion || ""),
      );
      outcome.append(card);
    }
    if (data.phases?.length) {
      const timeline = el("details");
      timeline.append(el("summary", t("阶段耗时")));
      for (const phase of data.phases) {
        const seconds = Math.max(
          0,
          (phase.ended || Date.now() / 1000) - phase.started,
        );
        timeline.append(
          el(
            "p",
            t(statusNames[phase.phase] || phase.phase) +
              " · " +
              Math.round(seconds) +
              " " +
              t("秒") +
              (phase.ended ? "" : " · " + t("进行中")),
          ),
        );
      }
      outcome.append(timeline);
    }
    if (!data.ended) {
      outcome.append(
        el(
          "p",
          t("显示的是已保存的检查点；模型输出和检查日志可能晚于实际执行。"),
          "muted",
        ),
      );
      const latest = data.stages?.at(-1)?.created || data.started;
      if (Date.now() / 1000 - latest > 180)
        outcome.append(
          el(
            "p",
            t(
              "暂时没有新检查点。可以检查模型连接和执行记录，或取消任务；系统不会因此跳过检查。",
            ),
            "muted",
          ),
        );
    }
    if (data.state === "expired")
      outcome.append(
        el(
          "p",
          t(
            "候选文件已按保留策略清理。重试会基于最新代码重新检查，旧批准不能复用。",
          ),
          "muted",
        ),
      );
  }
  async function taskControls(task, body, ticket) {
    const metadata = await api(
      "task?id=" +
        encodeURIComponent(task.id) +
        "&project=" +
        encodeURIComponent(task.project),
    );
    if (ticket !== detailSequence || !body.isConnected) return;
    const controls = el("section", undefined, "task-controls");
    controls.append(el("h3", t("下一步")));
    if (
      [
        "failed",
        "blocked",
        "interrupted",
        "expired",
        "insufficient_evidence",
        "deferred",
        "rejected",
        "cancelled",
      ].includes(metadata.state)
    )
      controls.append(
        action(
          t("基于当前输入重新检查"),
          async () => {
            await api("retry", { task: task.id });
            await refresh();
            await showTask(
              await api("task?id=" + task.id + "&project=" + task.project),
            );
          },
          "button",
        ),
      );
    if (
      [
        "queued",
        "deferred",
        "blocked",
        "failed",
        "insufficient_evidence",
        "rejected",
        "interrupted",
        "expired",
        "cancelled",
      ].includes(metadata.state) &&
      !task.source.startsWith("system_")
    ) {
      const form = el("form"),
        label = el("label", t("补充需求（保留在当前任务）")),
        input = el("textarea");
      input.rows = 3;
      input.maxLength = 10000;
      input.required = true;
      label.append(input);
      const submit = el("button", t("保存补充并重新评估"), "button");
      submit.type = "submit";
      form.append(label, submit);
      form.onsubmit = (event) => {
        event.preventDefault();
        safe(async () => {
          submit.disabled = true;
          try {
            await api("task/revise", {
              task: task.id,
              supplement: input.value,
            });
            await refresh();
            await showTask(
              await api("task?id=" + task.id + "&project=" + task.project),
            );
          } finally {
            submit.disabled = false;
          }
        });
      };
      controls.append(form);
    }
    if (["queued", "deferred", "blocked"].includes(metadata.state)) {
      const form = el("form"),
        label = el("label", t("前置任务（合并后再开始）")),
        select = el("select"),
        none = el("option", t("无前置任务"));
      none.value = "";
      select.append(none);
      const candidates = await api(
        "state?project=" +
          task.project +
          "&limit=200&filter=all&system=false&sections=core",
      );
      if (ticket !== detailSequence) return;
      for (const other of candidates.tasks || [])
        if (other.id !== task.id) {
          const option = el("option", other.title);
          option.value = other.id;
          select.append(option);
        }
      select.value = metadata.dependency?.dependency || "";
      label.append(select);
      const urgencyLabel = el("label", t("优先处理")),
        urgency = el("input");
      urgency.type = "checkbox";
      urgency.checked = !!metadata.urgency;
      urgencyLabel.prepend(urgency);
      const submit = el("button", t("保存排队设置"), "button");
      submit.type = "submit";
      form.append(label, urgencyLabel, submit);
      form.onsubmit = (event) => {
        event.preventDefault();
        safe(async () => {
          await api("task/schedule", {
            task: task.id,
            dependency: select.value || null,
            urgency: urgency.checked ? 1 : 0,
          });
          await refresh();
          toast(t("排队设置已保存"));
        });
      };
      controls.append(form);
    }
    if (metadata.decision) {
      const decision = el("details");
      decision.append(
        el("summary", t("暂缓决定与唤醒条件")),
        el("pre", JSON.stringify(JSON.parse(metadata.decision.data), null, 2)),
        el(
          "p",
          t("输入、策略或相关代码变化后重新评估，也可手动补充需求。"),
          "muted",
        ),
      );
      controls.append(decision);
    }
    if (metadata.revisions?.length) {
      const revisions = el("details");
      revisions.append(el("summary", t("需求修订记录")));
      for (const revision of metadata.revisions)
        revisions.append(
          el("p", stamp(revision.created) + " · " + revision.supplement),
        );
      controls.append(revisions);
    }
    body.append(controls);
  }
  function taskOpened(task) {
    clearTimeout(liveTimer);
    lastDetail = "";
    const url = new URL(location.href);
    url.hash = new URLSearchParams({
      project: task.project,
      task: task.id,
    }).toString();
    history.replaceState(null, "", url);
    const ticket = detailSequence;
    safe(() => taskControls(task, $("detail-body"), ticket));
    liveTimer = setTimeout(liveDetail, 5000);
  }
  async function liveDetail() {
    if (liveBusy || !detailTask || !$("detail-dialog").open) return;
    liveBusy = true;
    const task = detailTask,
      ticket = detailSequence;
    try {
      const metadata = await api(
          "task?id=" + task.id + "&project=" + task.project,
        ),
        history = await api(
          "task-runs?task=" + task.id + "&project=" + task.project,
        );
      if (ticket !== detailSequence) return;
      const latest = history[0];
      if (latest) {
        const data = await api("run?id=" + latest.id);
        if (ticket !== detailSequence) return;
        const signature = JSON.stringify([
          latest.id,
          data.state,
          data.result,
          data.stages,
        ]);
        const previous = $("live-task-outcome");
        if (signature !== lastDetail && previous) {
          const outcome = taskOutcome(data);
          outcome.id = "live-task-outcome";
          enhanceOutcome(outcome, data);
          previous.replaceWith(outcome);
          lastDetail = signature;
        }
        if (metadata.state !== task.state) {
          detailTask = metadata;
          const scroll = $("detail-dialog").scrollTop,
            opened = [
              ...$("detail-body").querySelectorAll("details[open]"),
            ].map((d) => d.querySelector("summary")?.textContent);
          await showTask(metadata);
          for (const detail of $("detail-body").querySelectorAll("details"))
            if (opened.includes(detail.querySelector("summary")?.textContent))
              detail.open = true;
          $("detail-dialog").scrollTop = scroll;
        }
      }
    } catch {
    } finally {
      liveBusy = false;
      if (detailTask && $("detail-dialog").open)
        liveTimer = setTimeout(liveDetail, 5000);
    }
  }
  async function showDiff(run) {
    const box = dialog("阅读代码差异"),
      response = await fetch("/api/patch?id=" + encodeURIComponent(run));
    if (!response.ok) throw Error(t("代码差异不可用"));
    const patch = await response.text();
    if (!box.isConnected) return;
    const pre = el("pre", undefined, "readable-diff");
    for (const line of patch.split("\n")) {
      const row = el("div", line);
      if (line.startsWith("+")) row.className = "diff-add";
      else if (line.startsWith("-")) row.className = "diff-remove";
      else if (line.startsWith("@@")) row.className = "diff-location";
      pre.append(row);
    }
    box.append(pre);
  }
  async function mobile() {
    const data = await api("mobile"),
      box = dialog("手机安装");
    box.append(el("p", t("手机需要能访问运行机器的 HTTPS 地址。"), "muted"));
    const link = el("a", data.install_url, "button");
    link.href = data.install_url;
    link.target = "_blank";
    link.rel = "noreferrer";
    box.append(link);
    if (!data.remote)
      box.append(
        el(
          "p",
          t(
            "当前是本机地址。请先配置 HTTPS，再把 HTTPS 安装地址发到自己的手机。",
          ),
        ),
      );
    box.append(
      el(
        "p",
        t("Android：浏览器菜单安装应用。iPhone：Safari 分享，再添加到主屏幕。"),
      ),
    );
  }
  async function storage() {
    const box = dialog("数据与诊断"),
      data = await api("storage");
    box.append(
      el(
        "p",
        t("数据库、运行文件与备份占用") +
          " · " +
          Object.entries(data.bytes)
            .map(
              ([key, value]) =>
                key + ": " + (value / 1048576).toFixed(1) + " MB",
            )
            .join(" / "),
      ),
      el(
        "p",
        t(
          "备份包含私有项目配置和任务内容，请妥善保管。恢复仅支持新的状态目录，项目保持暂停；凭据需重新连接，候选文件不在数据库备份中。",
        ),
        "muted",
      ),
    );
    box.append(
      action(t("创建一致性备份"), async () => {
        const saved = await api("backup", {}),
          link = el("a", t("下载备份") + " · " + saved.id, "button");
        link.href = "/api/backup?id=" + saved.id;
        link.download = "adaptive-rsi-" + saved.id + ".sqlite3";
        desktopDownload(link, saved.id);
        box.append(link);
      }),
    );
    for (const saved of data.backups) {
      const link = el(
        "a",
        t("下载备份") + " · " + stamp(saved.created),
        "text-button",
      );
      link.href = "/api/backup?id=" + saved.id;
      link.download = "adaptive-rsi-" + saved.id + ".sqlite3";
      desktopDownload(link, saved.id);
      box.append(link);
    }
    box.append(
      el("h3", t("恢复命令")),
      el(
        "pre",
        "adaptive-rsi --state-dir /NEW/STATE restore /PATH/BACKUP.sqlite3",
      ),
    );
    const diagnostics = await api("diagnostics"),
      preview = el("details");
    preview.append(
      el("summary", t("诊断导出预览")),
      el(
        "p",
        t(
          "仅包含版本、数量、故障代码和占用。不含凭据、路径、代码、任务正文或完整提示。",
        ),
        "muted",
      ),
      el("pre", JSON.stringify(diagnostics, null, 2)),
    );
    box.append(
      preview,
      action(t("下载诊断摘要"), () => {
        const blob = new Blob([JSON.stringify(diagnostics, null, 2)], {
            type: "application/json",
          }),
          url = URL.createObjectURL(blob),
          link = el("a");
        link.href = url;
        link.download = "adaptive-rsi-diagnostics.json";
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }),
    );
    if (data.expiring.length)
      box.append(
        el(
          "p",
          t("待批准候选会在项目保留期限到达后清理；请及时交付或重新运行。") +
            " " +
            data.expiring.length,
        ),
      );
  }
  function desktopDownload(link, id) {
    if (!window.RSIDesktop) return;
    link.onclick = (event) => {
      event.preventDefault();
      safe(async () => {
        const parts = [];
        let offset = 0, size;
        do {
          const chunk = await api("backup?id=" + id + "&format=json&offset=" + offset);
          const data = Uint8Array.from(atob(chunk.data), c => c.charCodeAt(0));
          parts.push(data);
          offset += data.length;
          size = chunk.size;
          if (!data.length && offset < size) throw Error(t("备份读取未完成"));
        } while (offset < size);
        const url = URL.createObjectURL(new Blob(parts, { type: "application/vnd.sqlite3" }));
        const download = el("a");
        download.href = url;
        download.download = link.download;
        download.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      });
    };
  }
  async function metrics() {
    const box = dialog("流程质量与耗时"),
      data = await api("workflow-metrics?project=" + projectId);
    for (const [key, label] of [
      ["tasks", "任务数"],
      ["accepted", "验收与评审通过"],
      ["delivered", "已合并"],
      ["first_pass", "首次代码尝试通过"],
      ["infrastructure_failures", "连接与环境故障"],
      ["repeated_failures", "反复失败任务"],
      ["requirements_revised", "补充需求任务"],
      ["stage_reused", "阶段复用"],
    ])
      box.append(el("p", t(label) + " · " + data[key]));
    box.append(
      el(
        "p",
        t("从入队到首次验收通过的平均耗时") +
          " · " +
          (data.elapsed_samples
            ? Math.round(data.elapsed_seconds / data.elapsed_samples) +
              " " +
              t("秒")
            : t("未知")) +
          " · n=" +
          data.elapsed_samples,
      ),
      el(
        "p",
        t("每个验收通过任务的已记录 tokens") +
          " · " +
          (data.accepted
            ? Math.round(data.accepted_tokens / data.accepted)
            : t("未知")) +
          " · " +
          data.accepted_unknown_calls +
          " " +
          t("次调用用量未知"),
      ),
    );
    for (const [phase, seconds] of Object.entries(data.phase_seconds))
      box.append(
        el(
          "p",
          t(statusNames[phase] || phase) +
            " · " +
            Math.round(seconds) +
            " " +
            t("秒"),
        ),
      );
    box.append(
      el(
        "p",
        t(
          "最近 30 天创建的逻辑任务。阶段时间为各运行累计，含并行任务，不代表总体墙钟时间；未知用量不按零计算。",
        ),
        "muted",
      ),
    );
  }
  let deepLinkOpened = false;
  function refreshed() {
    safe(pollInbox);
    if (!deepLinkOpened) {
      deepLinkOpened = true;
      const params = new URLSearchParams(location.hash.slice(1)),
        task = params.get("task"),
        project = params.get("project");
      if (task && project && state.projects.some((p) => p.id === project))
        safe(async () =>
          openTask(
            await api(
              "task?id=" +
                encodeURIComponent(task) +
                "&project=" +
                encodeURIComponent(project),
            ),
          ),
        );
    }
  }
  document.addEventListener("DOMContentLoaded", () => {
    $("attention-inbox").onclick = () => safe(inbox);
    $("mobile-install").onclick = () => safe(mobile);
    $("storage-tools").onclick = () => safe(storage);
    const button = action(t("流程质量与耗时"), metrics, "text-button");
    $("view-usage").prepend(button);
    $("detail-dialog").addEventListener("close", () => {
      clearTimeout(liveTimer);
      history.replaceState(null, "", location.pathname + location.search);
    });
    if (!window.RSIDesktop && "serviceWorker" in navigator && isSecureContext)
      navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
  return { queueCopy, taskOpened, enhanceOutcome, showDiff, refreshed };
})();
