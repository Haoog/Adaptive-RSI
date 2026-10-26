"use strict";
window.RSIExperience = (() => {
  const waiting = {
    dependency: "等待前置任务合并",
    backoff: "等待连接重试时间",
    paused: "项目已暂停",
    running: "等待当前任务结束",
    ready: "可开始",
    working_hours: "等待工作时段",
    capacity: "等待运行资源",
  };
  let liveTimer,
    liveBusy = false,
    lastDetail = "",
    inboxBusy = false,
    inboxAt = 0;
  const queueSaves = new Map();
  let queueEditor;
  const queueKey = task => JSON.stringify([task.project, task.id]);
  function queueEditorCurrent(editor) {
    return queueEditor === editor && $("detail-dialog").open &&
      projectId === editor.project && detailTask?.id === editor.task;
  }
  const notificationKey = "rsi-notifications:" + location.origin;
  function readSaved(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem(key)) || fallback;
    } catch {
      return fallback;
    }
  }
  let seen = readSaved(notificationKey + ":seen", {});
  let seenInstance = '';
  const phaseNames = {preparation:'准备运行环境', implementation:'正在修改代码', review:'正在评审改动', acceptance:'验收设计'};
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
    const ticket = detailSequence, navigation = navigationSequence;
    const current = () => ticket === detailSequence && navigation === navigationSequence && projectId === task.project;
    await refresh();
    if (!current()) return;
    const metadata = await api('task?id='+encodeURIComponent(task.id)+'&project='+encodeURIComponent(task.project));
    if (current()) await showTask(metadata);
  }
  async function inbox() {
    const data = await api("attention"),
      box = dialog("所有项目待办");
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
      if (seenInstance !== data.instance) {
        const next = readSaved(notificationKey + ':seen:' + data.instance, {});
        if (seenInstance) localStorage.removeItem(notificationKey + ':seen:' + seenInstance);
        localStorage.removeItem(notificationKey + ':seen');
        seen = next;
        seenInstance = data.instance;
      }
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
        "regressed",
        "unverified_merge",
        "release_failed",
        "deploy_failed",
      ]);
      const next = {};
      for (const task of data.notifications || []) {
        let result = {};
        try {
          result = JSON.parse(task.result || "{}");
        } catch {}
        const status = task.display_state || task.state,
          signature = JSON.stringify([
            task.run,
            status,
            result.failure?.code,
            result.failure?.detail,
            result.reason,
            result.feedback?.state,
            result.feedback?.head,
            result.automation?.release?.state,
            result.automation?.deploy?.state,
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
            window.RSIDesktop.invoke("notify", { title, body, task: task.id, project: task.project }).catch(() => {});
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
      localStorage.setItem(notificationKey + ":seen:" + seenInstance, JSON.stringify(seen));
    } catch {
    } finally {
      inboxBusy = false;
    }
  }
  function enhanceOutcome(outcome, data) {
    const result = data.result;
    if (result.evaluation?.criteria?.length) {
      const criteria = el("details");
      criteria.append(el("summary", t("验收标准与覆盖")));
      for (const [index, criterion] of result.evaluation.criteria.entries()) {
        const checks = (result.evaluation.coverage || []).filter(c => c.criteria?.includes(index)).map(c => c.name);
        criteria.append(el("p", (index + 1) + ". " + criterion + " · " + (checks.join(", ") || t("覆盖记录未知"))));
      }
      outcome.append(criteria);
    }
    if (data.state === "awaiting_approval" && data.ended) {
      const days = currentProject()?.config?.limits?.retention_days;
      if (days) outcome.append(el("p", t("候选清理时间") + " · " + stamp(data.ended + days * 86400), "muted"));
    }
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
              t(phaseNames[phase.phase] || statusNames[phase.phase] || phase.phase) +
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
            data.result?.restore_invalidation?.reason ===
              "candidate_not_in_backup"
              ? "候选因从备份恢复而失效：数据库备份不包含候选文件，旧批准不能复用。请基于最新代码重新运行并重新批准。"
              : "候选文件已按保留策略清理。重试会基于最新代码重新检查，旧批准不能复用。",
          ),
          "muted",
        ),
      );
  }
  async function taskControls(task, body, ticket, queueDraft, editor) {
    const project = projectId, baselineVersion = editor.version;
    const current = () => projectId === project && ticket === detailSequence && body.isConnected;
    async function refreshTaskDetail() {
      await refresh();
      if (!current()) return;
      const updated = await api("task?id=" + encodeURIComponent(task.id) + "&project=" + encodeURIComponent(task.project));
      if (current()) await showTask(updated);
    }
    const metadata = await api(
      "task?id=" +
        encodeURIComponent(task.id) +
        "&project=" +
        encodeURIComponent(task.project),
    );
    if (!current()) return;
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
            if (!current()) return;
            await api("retry", { task: task.id });
            await refreshTaskDetail();
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
        "obsolete",
      ].includes(metadata.state) &&
      !task.source.startsWith("system_")
    ) {
      const form = el("form"),
        label = el("label", t("补充需求（保留在当前任务）")),
        input = el("textarea");
      input.rows = 3;
      input.maxLength = 10000;
      input.required = true;
        input.value = draftStore.get('task-supplement', task.id) || '';
        input.addEventListener('input', () => draftStore.set('task-supplement', task.id, input.value));
      label.append(input);
      const submit = el("button", t("保存补充并重新评估"), "button");
      submit.type = "submit";
      form.append(label, submit);
      form.onsubmit = (event) => {
        event.preventDefault();
        safe(async () => {
          if (!current()) return;
          submit.disabled = true;
          try {
            const value = input.value;
            await api("task/revise", {
              task: task.id,
              supplement: value,
            });
            if (draftStore.get('task-supplement', task.id) === value) draftStore.remove('task-supplement', task.id);
            await refreshTaskDetail();
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
      if (!editor.baseline || (!queueSaves.has(editor.key) && editor.version === baselineVersion))
        editor.baseline = {dependency: metadata.dependency?.dependency || "", urgency: !!metadata.urgency};
      queueDraft = readQueueDraft();
      const dependency = editor.baseline.dependency;
      const pager = el("div", undefined, "task-pagination"),
        pageNote = el("span");
      pageNote.setAttribute("aria-live", "polite");
      let offset = 0, hasMore = false, loading = false;
      const previous = action(t("上一页"), () => loadCandidates(Math.max(0, offset - 200)), "text-button"),
        next = action(t("下一页"), () => loadCandidates(offset + 200), "text-button");
      previous.type = next.type = "button";
      pager.append(previous, pageNote, next);
      function pagingState() {
        previous.disabled = loading || offset === 0;
        next.disabled = loading || !hasMore;
      }
      async function loadCandidates(requestedOffset) {
        if (!current() || loading) return;
        loading = true;
        pagingState();
        try {
          const candidates = await api(
            "state?project=" + encodeURIComponent(task.project) +
            "&limit=200&offset=" + requestedOffset + "&filter=all&system=false&sections=core",
          );
          if (!current()) return;
          // Read at response time so edits made while a page loads remain selected.
          const selected = select.value || "", pinned = new Map();
          for (const option of select.children)
            if (option.value && [dependency, selected].includes(option.value))
              pinned.set(option.value, {id: option.value, title: option.textContent});
          const choices = new Map(pinned);
          for (const other of candidates.tasks || [])
            if (other.id !== task.id) choices.set(other.id, other);
          select.replaceChildren(none);
          for (const other of choices.values()) {
            const option = el("option", other.title);
            option.value = other.id;
            select.append(option);
          }
          select.value = selected;
          offset = requestedOffset;
          hasMore = !!candidates.pagination?.has_more;
          pageNote.textContent = t("前置任务候选页") + " " + (offset / 200 + 1);
        } finally {
          loading = false;
          if (current()) pagingState();
        }
      }
      // Seed saved/draft selections before loading, including an off-page parent.
      for (const id of new Set([dependency, queueDraft?.dependency])) {
        if (!id) continue;
        const option = el("option", id);
        option.value = id;
        select.append(option);
      }
      select.value = queueDraft?.dependency ?? dependency;
      // Resolve seed titles once; subsequent pages pin only the saved and selected parent.
      for (const option of [...select.children]) {
        if (!option.value) continue;
        const parent = await api("task?id=" + encodeURIComponent(option.value) + "&project=" + encodeURIComponent(task.project));
        if (!current()) return;
        option.textContent = parent.title;
      }
      await loadCandidates(0);
      if (!current()) return;
      label.append(select);
      const urgencyLabel = el("label", t("优先处理")),
        urgency = el("input");
      urgency.type = "checkbox";
      urgency.checked = queueDraft?.urgency ?? editor.baseline.urgency;
      editor.read = () => ({dependency: select.value, urgency: urgency.checked});
      urgencyLabel.prepend(urgency);
      const submit = el("button", t("保存排队设置"), "button");
      submit.type = "submit";
      editor.submit = submit;
      submit.disabled = queueSaves.has(editor.key);
      form.append(label, urgencyLabel, submit, pager);
      form.onsubmit = (event) => {
        event.preventDefault();
        safe(async () => {
          if (!current() || queueSaves.has(editor.key)) return;
          const submitted = {task: task.id, dependency: select.value || null, urgency: urgency.checked ? 1 : 0};
          queueSaves.set(editor.key, submitted);
          submit.disabled = true;
          try {
            await api("task/schedule", submitted);
            // Rebase even if this form was rebuilt. Its editor still owns newer edits.
            editor.baseline = {dependency: submitted.dependency || "", urgency: !!submitted.urgency};
            editor.version++;
            if (!queueEditorCurrent(editor)) return;
            await refresh();
            if (queueEditorCurrent(editor)) toast(t("排队设置已保存"));
          } finally {
            queueSaves.delete(editor.key);
            if (queueEditor?.key === editor.key && queueEditor.submit)
              queueEditor.submit.disabled = false;
          }
        }, form);
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
  function readQueueDraft() {
    if (!queueEditor || !queueEditorCurrent(queueEditor)) return;
    const values = queueEditor.read?.();
    if (!values) return queueEditor.draft;
    if (queueSaves.has(queueEditor.key)) return values;
    return Object.fromEntries(Object.entries(values).filter(([key, value]) => value !== queueEditor.baseline[key]));
  }
  function taskOpened(task, queueDraft) {
    const key = queueKey(task);
    if (queueEditor?.key !== key)
      queueEditor = {key, project: task.project, task: task.id, version: 0};
    queueEditor.draft = queueDraft;
    queueEditor.read = null;
    queueEditor.submit = null;
    clearTimeout(liveTimer);
    lastDetail = "";
    const url = new URL(location.href);
    url.hash = new URLSearchParams({
      project: task.project,
      task: task.id,
    }).toString();
    history.replaceState(null, "", url);
    const ticket = detailSequence;
    safe(() => taskControls(task, $("detail-body"), ticket, queueDraft, queueEditor));
    liveTimer = setTimeout(liveDetail, 5000);
  }
  function detailsSnapshot(root) {
    const key = node => {
      const labels = [];
      for (let item=node; item && item!==root; item=item.parentElement) {
        if (item.tagName==='DETAILS') labels.unshift(item.querySelector(':scope > summary')?.textContent || '');
      }
      return labels.join('\n');
    };
    const values = new Map([...root.querySelectorAll('details')].map(node => [key(node), node.open]));
    return target => {
      for (const detail of target.querySelectorAll('details')) {
        const saved = values.get(key(detail));
        if (saved !== undefined) detail.open = saved;
      }
    };
  }
  async function liveDetail() {
    if (liveBusy || !detailTask || !$("detail-dialog").open) return;
    liveBusy = true;
    const task = detailTask,
      ticket = detailSequence;
    const current = () => ticket === detailSequence && projectId === task.project && $("detail-dialog").open;
    try {
      const metadata = await api(
        "task?id=" + task.id + "&project=" + task.project,
      );
      if (!current()) return;
      // Issue sync may revise title, body or evidence while the task stays queued.
      // Rebuild only when the visible input changed so unchanged polls keep drafts and DOM.
      const snapshot = task => JSON.stringify([task.state, task.title, task.body, task.evidence, task.updated]);
      if (snapshot(metadata) !== snapshot(task)) {
        const scroll = $("detail-dialog").scrollTop, restore = detailsSnapshot($("detail-body"));
        const queueDraft = readQueueDraft();
        await showTask(metadata, restore, queueDraft);
        if (detailSequence === ticket + 1 && projectId === task.project && $("detail-dialog").open) {
          restore($("detail-body"));
          $("detail-dialog").scrollTop = scroll;
        }
        return;
      }
      const history = await api(
        "task-runs?task=" + task.id + "&project=" + task.project,
      );
      if (!current()) return;
      const latest = history[0];
      if (latest) {
        const data = await api("run?id=" + latest.id);
        if (!current()) return;
        const signature = JSON.stringify([
          latest.id,
          data.state,
          data.result,
          data.stages,
          data.phases,
          data.phases?.some(phase => !phase.ended) ? Math.floor(Date.now() / 5000) : null,
        ]);
        const previous = $("live-task-outcome");
        if (signature !== lastDetail && previous) {
          const scroll = $("detail-dialog").scrollTop;
          const restore = detailsSnapshot(previous);
          const outcome = taskOutcome(data);
          outcome.id = "live-task-outcome";
          enhanceOutcome(outcome, data);
          previous.replaceWith(outcome);
          await renderReleaseOutcome(outcome, data, task.project);
          if (!current()) return;
          restore(outcome);
          $("detail-dialog").scrollTop = scroll;
          lastDetail = signature;
        }
      }
    } catch {
    } finally {
      liveBusy = false;
      if (detailTask && $("detail-dialog").open) {
        clearTimeout(liveTimer);
        liveTimer = setTimeout(liveDetail, 5000);
      }
    }
  }
  async function showDiff(run) {
    const response = await fetch("/api/patch?id=" + encodeURIComponent(run));
    if (!response.ok) throw Error(t("代码差异不可用"));
    const patch = await response.text(),
      box = dialog("阅读代码差异");
    if (patch.split("\n").length > 2000) {
      box.append(
        el("p", t("差异超过 2000 行，已折叠为纯文本。"), "muted"),
        el("pre", patch, "readable-diff"),
      );
      return;
    }
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
    if (!/^https?:\/\//.test(data.install_url)) throw Error(t("安装地址不可用"));
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
    const data = await api("storage"),
      box = dialog("数据与诊断");
    box.append(
      el(
        "p",
        t("数据库、运行文件与备份占用") +
          " · " +
          Object.entries(data.bytes)
            .map(
              ([key, value]) =>
                t(({database:'数据库',runs:'运行文件',backups:'备份'})[key] || key) + ": " + (value / 1048576).toFixed(1) + " MB",
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
    const backupButton = action(t("创建一致性备份"), async () => {
      backupButton.disabled = true;
      try {
        const saved = await api("backup", {}),
          link = el("a", t("下载备份") + " · " + saved.id, "button");
        link.href = "/api/backup?id=" + saved.id;
        link.download = "adaptive-rsi-" + saved.id + ".sqlite3";
        desktopDownload(link, saved.id);
        box.append(link);
      } finally {
        backupButton.disabled = false;
      }
    });
    box.append(backupButton);
    // 会话轮换：怀疑链接泄漏时立即作废旧链接，server.json 同步更新，重启后仍有效。
    const rotateButton = action(t("轮换访问链接"), async () => {
      if (
        !(await uiConfirm(
          t("轮换后旧的登录链接立即失效，所有已打开的会话需要用新链接重新登录。继续？"),
        ))
      )
        return;
      rotateButton.disabled = true;
      try {
        const saved = await api("session/rotate", { confirm: true });
        if (state) state.csrf = saved.csrf;
        const field = el("input");
        field.readOnly = true;
        field.value = saved.url;
        field.className = "rotate-url";
        field.setAttribute("aria-label", t("新的认证链接"));
        const copy = action(t("复制链接"), async () => {
          try {
            await navigator.clipboard.writeText(saved.url);
            copy.textContent = t("已复制");
          } catch {
            field.select();
            document.execCommand("copy");
            copy.textContent = t("已复制");
          }
        }, "text-button");
        const note = el("p", t("已轮换。请立即保存新的认证链接，旧链接已失效："));
        box.append(note, field, copy);
        field.focus();
        field.select();
      } finally {
        rotateButton.disabled = false;
      }
    });
    box.append(rotateButton);
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
    if (data.delivering?.length)
      box.append(
        el(
          "p",
          t("已批准候选正在交付，不再计入待批准；交付完成前保留审批记录。") +
            " " +
            data.delivering.length,
          "muted",
        ),
      );
    if (data.delivered?.length)
      box.append(
        el(
          "p",
          t("已交付候选保留在历史记录中，不再计入待批准。") +
            " " +
            data.delivered.length,
          "muted",
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
    const data = await api("workflow-metrics?project=" + projectId),
      box = dialog("流程质量与耗时");
    for (const [key, label] of [
      ["tasks", "任务数"],
      ["accepted", "验收与评审通过"],
      ["delivered", "已合并"],
      ["first_pass", "首次代码尝试通过"],
      ["infrastructure_failures", "连接与环境故障"],
      ["repeated_failures", "反复失败任务"],
      ["requirements_revised", "补充需求任务"],
      ["stage_reused", "阶段复用"],
      ["interventions", "用户介入次数"],
      ["stale_rework", "旧基线返工次数"],
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
          t(phaseNames[phase] || statusNames[phase] || phase) +
            " · " +
            Math.round(seconds) +
            " " +
            t("秒"),
        ),
      );
    for (const [key, label] of [["to_pr", "到 PR 的平均耗时"], ["to_merge", "到合并的平均耗时"]]) {
      box.append(el("p", t(label) + " · " + (data[key + "_samples"] ? Math.round(data[key + "_seconds"] / data[key + "_samples"]) + " " + t("秒") : t("未知")) + " · n=" + data[key + "_samples"]));
    }
    box.append(el("p", t("每个已合并任务的已记录 tokens") + " · " + (data.delivered ? Math.round(data.delivered_tokens / data.delivered) : t("未知")) + " · " + data.delivered_unknown_calls + " " + t("次调用用量未知")));
    for (const [reason, seconds] of Object.entries(data.remote_wait_seconds)) {
      box.append(el("p", t(deliveryNames[reason] || reason) + " · " + Math.round(seconds) + " " + t("秒")));
    }
    box.append(el("p", t("缓存与重评失效记录") + " · " + Object.entries(data.invalidations).map(([key, value]) => t(({stage_invalidated: "阶段输入变化", requirements_revised: "需求修订", triage_recheck: "暂缓唤醒", issue_revision: "Issue 更新"})[key]) + ": " + value).join(" / ")));
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
        safe(() => openTask({id: task, project}));
    }
  }
  document.addEventListener("DOMContentLoaded", () => {
    window.addEventListener('rsi-notification-open', event => {
      if (window.RSIDesktop && event.detail?.task && event.detail?.project)
        safe(() => openTask({id:event.detail.task,project:event.detail.project}));
    });
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
  return { queueCopy, queueDraft: readQueueDraft, taskOpened, taskClosed() { queueEditor = null; }, enhanceOutcome, showDiff, refreshed, detailsSnapshot };
})();

'use strict';
// Presentation only: server evidence and delivery checks remain authoritative.
window.RSIProduct = (() => {
  const saveStates=new Map();
  const saveState=()=>saveStates.get(settingsKey())||{};
  function raw(value, title='原始证据') {
    const node=el('details',undefined,'raw-evidence');
    node.append(el('summary',t(title)),el('pre',typeof value==='string'?value:JSON.stringify(value,null,2)));
    return node;
  }
  function checkList(groups) {
    const container=el('div',undefined,'check-results');
    for(const [stage, rows] of Object.entries(groups||{})) {
      if(!Array.isArray(rows))continue;
      const section=el('section',undefined,'check-stage');
      section.append(el('h4',t(evaluationNames[stage]||stage)+' · '+rows.filter(row=>row.passed===true).length+'/'+rows.length));
      if(!rows.length)section.append(el('p',t('没有检查记录，不能据此认定通过。'),'muted'));
      for(const check of rows) {
        const card=el('article',undefined,'check-result');
        const header=el('div',undefined,'check-result-heading');
        header.append(el('strong',check.name||t('未命名检查')),badge(check.passed===true?'pass':check.passed===false?'fail':'insufficient_evidence'));
        card.append(header);
        const meta=[];
        if(check.exit_code!==undefined&&check.exit_code!==null)meta.push(t('退出码')+' '+check.exit_code);
        if(Number.isFinite(check.seconds))meta.push(elapsedCopy(check.seconds));
        if(check.cwd)meta.push(t('运行目录')+' '+check.cwd);
        if(meta.length)card.append(el('p',meta.join(' · '),'muted'));
        const command=check.argv||check.command;
        if(command)card.append(el('code',Array.isArray(command)?command.map(arg=>JSON.stringify(arg)).join(' '):String(command)));
        if(check.passed===false)card.append(el('p',t('此检查未通过。展开输出定位原因，修正后重新运行。'),'form-error'));
        card.append(raw(check,'命令与完整输出'));section.append(card);
      }
      container.append(section);
    }
    return container;
  }
  function deliveryMode(config) {
    return t(config?.delivery?.mode==='pr'?(config.delivery.auto_merge?'检查通过后自动合并':'自动提交修改，等待合并'):'先保存，暂不提交');
  }
  function approval(data) {
    const result=data.result||{}, live=currentProject(), frozen=data.config||{};
    const box=el('section',undefined,'approval-summary');box.append(el('h3',t('交付前核对')));
    const list=el('dl');
    for(const [label,value] of [
      ['当前项目',live?.name||data.project||t('未知')],
      ['GitHub 仓库',frozen.github?.repository||t('未配置')],
      ['主线分支',frozen.delivery?.base_branch||t('仓库默认分支（交付时复核）')],
      ['本次检查基线',data.base||t('未知')],
      ['本次交付设置',deliveryMode(frozen)],
      ['当前交付授权',deliveryMode(live?.config)],
      ['项目运行状态',t(live?.paused?'已暂停':'运行中')],
      ['运行检查',t(result.evaluation?.decision==='pass'?'通过':'尚未通过')],
      ['代码评审',t(result.review?.decision==='pass'?'通过':'尚未通过')],
      ['问题解决证据',t(result.evaluation?.improvement?.decision==='pass'?'通过':'未提供通过证据')],
    ])list.append(el('dt',t(label)),el('dd',value));
    const current=live?.config||{},merge=!!(frozen.delivery?.auto_merge&&current.delivery?.auto_merge);
    list.append(el('dt',t('后续自动合并')),el('dd',t(merge?'本次与当前均已授权：门禁通过后可自动合并。':'本次与当前未同时授权：不会自动合并。')));
    for(const [kind,title] of [['release','发布'],['deploy','部署']]){
      const saved=frozen.delivery?.[kind],now=current.delivery?.[kind];
      const enabled=!!(saved?.enabled&&now?.enabled&&current.delivery?.mode==='pr'&&frozen.github?.repository===current.github?.repository&&sameValue(saved,now));
      list.append(el('dt',t(title)),el('dd',t(enabled?'本次与当前工作流授权一致：合并并验证后可自动执行。':'未授权或授权已变化：不会自动执行。')+(enabled?' · '+saved.workflow+' @ '+saved.ref+' · '+saved.sha:'')));
    }
    box.append(list,el('p',t('批准会推送本次候选并创建 PR；后续可能按下列有效授权自动合并、发布或部署。只有本次与当前授权同时允许时才执行。仓库、分支、基线和候选在执行前复核，GitHub 检查与必需评审仍须通过。'),'muted'));
    if(live?.paused)box.append(el('p',t('项目已暂停：批准后只会排队，启动项目后才执行。'),'muted'));
    if(frozen.github?.repository!==live?.config?.github?.repository||frozen.delivery?.base_branch!==live?.config?.delivery?.base_branch)
      box.append(el('p',t('当前交付目标与本次运行不同，请重新运行以获取最新基线。'),'form-error'));
    return box;
  }
  function dirtyStatus() {
    if(!settingsBaseline)return;
    const {savedAt='',saving=false}=saveState();
    const dirty=JSON.stringify(settingsValues())!==settingsClean.get(settingsContext);
    const node=$('save-note');if(!node)return;
    node.textContent=t(saving?'正在保存设置…':dirty?'有未保存的修改；点击保存后才会生效。':savedAt?'设置已保存；新任务使用新配置，运行中任务保留原配置。':'当前显示已保存设置；新任务使用这些配置。')+(savedAt&&!dirty&&!saving?' · '+savedAt:'');
    node.dataset.state=saving?'saving':dirty?'dirty':'saved';
    const scope=$('config-scope').value==='project'?(currentProject()?.name||t('当前项目')):t('通用默认设置');
    $('settings-effect').textContent=scope+' · '+t('扫描时间保存后重新安排；模型分工单独保存，从新任务生效。交付授权在执行前复核。无需重启。');
  }
  function savingStatus(value,success=false,key=settingsKey()) {const prior=saveStates.get(key)||{};saveStates.set(key,{saving:value,savedAt:success?new Date().toLocaleTimeString(language):prior.savedAt});if(key===settingsKey())dirtyStatus();}
  function renderEditor() {
    const host=$('checks-editor');if(!host)return;
    let checks;try{checks=JSON.parse($('checks-json').value||'[]');}catch{return;}
    host.replaceChildren();
    checks.forEach((check,index)=>{
      const card=el('fieldset',undefined,'check-editor-row');card.append(el('legend',t('固定检查')+' '+(index+1)));
      for(const [key,label,value] of [['name','检查名称',check.name||''],['cwd','运行目录',check.cwd??'.']]){
        const wrap=el('label',t(label)),input=el('input');input.value=value;input.dataset.checkField=key;
        input.setAttribute('aria-label',t(label)+' '+(index+1));wrap.append(input);card.append(wrap);
        input.addEventListener('input',syncEditor);
      }
      const args=el('div',undefined,'check-arguments');
      const addArgument=value=>{
        const row=el('div',undefined,'check-argument'),label=el('label'),input=el('textarea');
        const number=args.children.length,title=number===0?'执行程序':'独立参数';
        label.append(el('span',t(title)+' '+(number||'')));input.value=value;input.rows=2;input.spellcheck=false;input.dataset.checkArg='';input.setAttribute('aria-label',t(title)+' '+(index+1)+' / '+(number+1));
        label.append(input);row.append(label);input.addEventListener('input',syncEditor);
        if(number)row.append(action(t('删除参数'),()=>{row.remove();syncEditor();},'text-button'));args.append(row);
      };
      for(const argument of check.argv||[''])addArgument(argument);
      card.append(args,action(t('添加参数'),()=>{addArgument('');syncEditor();args.lastElementChild.querySelector('textarea').focus();},'button'));
      card.append(action(t('删除检查'),()=>{card.remove();syncEditor();},'text-button'));host.append(card);
    });
    if(!checks.length)host.append(el('p',t('未指定固定检查，将按项目自动识别；识别不到时会阻塞交付。'),'muted'));
  }
  function syncEditor() {
    const checks=[...$('checks-editor').querySelectorAll('fieldset')].map(card=>{
      const read=key=>card.querySelector('[data-check-field="'+key+'"]').value;
      return {name:read('name'),cwd:read('cwd'),argv:[...card.querySelectorAll('[data-check-arg]')].map(input=>input.value)};
    });
    $('checks-json').value=JSON.stringify(checks);$('checks-json').dispatchEvent(new Event('input',{bubbles:true}));
  }
  function validateChecks(checks) {
    if(!Array.isArray(checks))throw Error(t('固定检查必须是检查列表。'));
    for(const check of checks){
      if(!check.name?.trim()||!Array.isArray(check.argv)||!check.argv.length||!check.argv[0]?.trim())throw Error(t('请填写检查名称和程序；每个输入框是一个参数，无需 shell 引号。'));
      if(check.argv.some(arg=>typeof arg!=='string'||arg.includes('\0')||arg.length>=2000))throw Error(t('检查参数不可含空字符，且每项必须少于 2000 字符。'));
      const cwd=check.cwd??'.';if(typeof cwd!=='string'||cwd.startsWith('/')||cwd.includes('\\')||cwd.includes('\0')||cwd.split('/').some(part=>part==='..'||part==='.git'))throw Error(t('运行目录必须在项目内，例如 . 或 tests。'));
    }
    return checks;
  }
  function health() {
    const node=$('service-status');if(!node)return;
    node.textContent=$('connection').textContent;
    const project=currentProject(), result=readinessCache.get(projectId);
    const counts=state?.task_counts||[],blocked=counts.filter(row=>['blocked','failed','insufficient_evidence','interrupted','ci_failed','release_failed','deploy_failed','regressed'].includes(row.state)).reduce((sum,row)=>sum+row.count,0);
    $('workspace-health').textContent=t(project?.configuration_error?'设置异常':result?.loading?'正在检查运行条件…':result&&!result.ready?'运行条件受阻':result?.ready?'基础条件已就绪，实际效果将在任务中验证。':'运行条件尚未检查')+' · '+t('阻塞或失败任务')+' '+blocked;
    const host=$('workspace-health-evidence');if(!host)return;
    const restore=window.RSIExperience?.detailsSnapshot?.(host);host.replaceChildren();
    const models=el('details');models.append(el('summary',t('模型连接与验证证据')));
    if(!result?.model_evidence?.length)models.append(el('p',t('未知：尚未取得当前模型的验证记录。'),'muted'));
    for(const proof of result?.model_evidence||[])models.append(el('p',t(roleNames[proof.role]||proof.role)+' · '+t(proof.task_verified?'真实任务已验证':proof.tool_tested?'基础工具测试通过':proof.configured?'连接已配置':'需要检查连接')));
    models.append(el('p',t('连接已配置不代表模型调用成功；获取目录与运行准备检查不会启动付费测试。'),'muted'));host.append(models);
    const recent=(state?.runs||[]).filter(run=>run.project===projectId&&(run.failure||['failed','blocked','insufficient_evidence','interrupted'].includes(run.state))).slice(0,3);
    if(recent.length){const failures=el('details');failures.append(el('summary',t('近期失败（当前已载入记录）')));for(const run of recent){failures.append(el('p',stamp(run.ended||run.started)+' · '+(run.failure?failureCopy(run.failure):t(statusNames[run.state]||run.state))));failures.append(raw({run:run.id,failure:run.failure,reason:run.reason},'查看原始诊断'));}host.append(failures);}
    restore?.(host);
  }
  document.addEventListener('DOMContentLoaded',()=>{
    $('checks-add').addEventListener('click',()=>{
      const checks=JSON.parse($('checks-json').value||'[]');checks.push({name:'',cwd:'.',argv:['']});$('checks-json').value=JSON.stringify(checks);renderEditor();$('checks-json').dispatchEvent(new Event('input',{bubbles:true}));$('checks-editor').lastElementChild.querySelector('input').focus();
    });
    $('settings-form').addEventListener('input',dirtyStatus);$('settings-form').addEventListener('change',dirtyStatus);
    new MutationObserver(health).observe($('connection'),{childList:true,characterData:true,subtree:true});
  });
  return {raw,checkList,approval,renderEditor,validateChecks,dirtyStatus,savingStatus,health};
})();
