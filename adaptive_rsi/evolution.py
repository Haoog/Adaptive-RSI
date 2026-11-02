import json
import math
import shutil
import time
from pathlib import Path

from .config import compatibility, fingerprint, merge, validate
from .gitops import git, repository, snapshot
from .process import Cancelled, scrub_tree
from .store import encode, uid
from .usage_report import one_snapshot


FEEDBACK_LABELS = ("false_positive", "confirmed_regression", "rejected")
FEEDBACK_SOURCE_PREFIX = "human:"
# 受控消融沿用 attribution() 的四配置命名，避免报告与试验标签不一致。
ABLATION_CONFIGURATIONS = ("baseline", "strategy_only", "skill_only", "combined")


def human_feedback(store, project=None, candidate=None):
    """Read accepted human labels, ignoring unreadable or unsupported rows.

    标签只来自事件日志：损坏的 JSON、未支持的标签或非人工来源都不计入汇总，
    既不中断只读视图，也不能借由事件注入把模型信号当作人工校准依据。
    """
    clause, args = "kind='monitor_feedback'", []
    if project:
        clause += " AND project=?"
        args.append(project)
    labels = []
    for row in store.query("SELECT data FROM events WHERE " + clause, tuple(args)):
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("label") not in FEEDBACK_LABELS:
            continue
        source = data.get("source")
        if not isinstance(source, str) or not source.startswith(FEEDBACK_SOURCE_PREFIX):
            continue
        if candidate is not None and data.get("candidate") != candidate:
            continue
        labels.append(data)
    return labels


@one_snapshot()
def feedback_overview(store, project=None):
    """Read-only summary of human monitoring labels for manual threshold calibration.

    The view reports its own selection bias and never writes candidate evidence,
    sealed trial outcomes or monitoring thresholds. Operators decide separately
    whether a threshold is worth changing after reading these counts.
    """
    rows = human_feedback(store, project)
    labels = {label: sum(row["label"] == label for row in rows) for label in FEEDBACK_LABELS}
    sources = {}
    for row in rows:
        sources[row["source"]] = sources.get(row["source"], 0) + 1
    clause, args = (" WHERE project=?", (project,)) if project else ("", ())
    candidates = {row["id"] for row in store.query("SELECT id FROM candidates" + clause, args)}
    labeled = {row.get("candidate") for row in rows if row.get("candidate")} & candidates
    bias = ("Optional, self-selected human reports; " + str(len(labeled)) + " of " + str(len(candidates)) +
            " candidates carry a label, so unlabeled and unreported failures are invisible; report mix reflects "
            "who chose to label, not a random sample; labels never overwrite sealed trials or monitoring thresholds")
    return {"project": project or None, "read_only": True, "samples": len(rows), "labels": labels,
            "sources": {key: sources[key] for key in sorted(sources)},
            "labeled_candidates": len(labeled), "unlabeled_candidates": len(candidates - labeled),
            "bias": bias, "automatic_threshold_changes": False}


def paired_summary(trials):
    families = {}
    for trial in trials:
        if trial.get("case"):
            families.setdefault(trial["case"], []).append(trial)
    wins = losses = ties = 0
    cohorts = {}
    for family, rows in families.items():
        complete = [row for row in rows if "stable" in row and "candidate" in row]
        if not complete:
            continue
        lost = any(row["stable"]["pass"] and not row["candidate"]["pass"] for row in complete)
        won = not lost and any(not row["stable"]["pass"] and row["candidate"]["pass"] for row in complete)
        wins += won
        losses += lost
        ties += not won and not lost
        cohorts[family] = {"source": complete[0].get("source"), "split": complete[0].get("split"),
                           "outcome": "loss" if lost else "win" if won else "tie", "repetitions": len(complete)}
    n = wins + losses
    summary = {"wins": wins, "losses": losses, "ties": ties, "unit": "independent_case_family",
               "unknown_identity_trials": sum(not trial.get("case") for trial in trials),
               "estimate": wins / n if n else None, "cohorts": cohorts}
    if n:
        z = 1.959963984540054
        p = wins / n
        divisor = 1 + z * z / n
        center = (p + z * z / (2 * n)) / divisor
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / divisor
        summary["wilson_95"] = [max(0, center - half), min(1, center + half)]
    return summary


def case_spec(case):
    """Decode a stored case spec; a malformed row is skipped, never fatal."""
    try:
        spec = json.loads(case["spec"])
    except (TypeError, ValueError):
        return None
    return spec if isinstance(spec, dict) else None


def case_stratum(case):
    spec = case_spec(case) or {}
    length = len(spec.get("task", ""))
    return (spec.get("source", "unknown"), "small" if length < 1000 else "medium" if length < 10000 else "large")


def select_cases(cases, count, seed):
    strata = {}
    for case in cases:
        strata.setdefault(case_stratum(case), []).append(case)
    groups = []
    for stratum in sorted(strata, key=lambda value: fingerprint({"stratum": value, "seed": seed})):
        groups.append(sorted(strata[stratum], key=lambda case: (case["uses"], fingerprint({"id": case["id"], "seed": seed}))))
    selected = []
    while groups and len(selected) < count:
        for group in groups[:]:
            selected.append(group.pop(0))
            if not group:
                groups.remove(group)
            if len(selected) == count:
                break
    return selected


class Evolution:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self._plan_keys = {}

    def verify_inputs(self, project, manifest):
        mismatches = []
        for item in manifest:
            row = self.store.one("SELECT spec FROM cases WHERE id=? AND project=?", (item["case"], project))
            try:
                spec = json.loads(row["spec"]) if row else {}
                actual = {"spec": fingerprint(spec), "task": fingerprint(spec["task"]),
                          "checks": fingerprint(spec["checks"]),
                          "tree": git(spec["repository"], "rev-parse", spec["base"] + "^{tree}"),
                          "overlay": self.overlay_digest(spec.get("overlay"))}
                changed = [key for key, value in actual.items() if item.get(key) != value]
            except (KeyError, ValueError, RuntimeError, OSError):
                changed = ["unavailable"]
            if changed:
                mismatches.append({"case": item["case"], "fields": changed})
        return {"valid": bool(manifest) and not mismatches, "mismatches": mismatches}

    @staticmethod
    def verify_plan(sealed, trials):
        data = sealed.get("data", {})
        if fingerprint(data) != sealed.get("sha256"):
            return {"valid": False, "reason": "plan_hash_mismatch"}
        expected = {(sample["id"], sample["split"], repeat)
                    for sample in data.get("samples", []) for repeat in range(data.get("repeats", 0))}
        actual = [(row.get("case"), row.get("split"), row.get("repetition")) for row in trials
                  if "stable" in row and "candidate" in row]
        valid = bool(expected) and set(actual) == expected and len(actual) == len(expected) == len(trials)
        return {"valid": valid, "reason": "complete_preregistered_analysis" if valid else "incomplete_or_unplanned_trials",
                "analysis": "preregistered" if valid else "exploratory", "expected": len(expected), "observed": len(actual)}

    def update_case(self, project, case, *, active, split=None, source):
        """Retire instead of deleting; exposure identities and all prior uses survive."""
        if type(active) is not bool or not isinstance(source, str) or not source.strip():
            raise ValueError("Lifecycle changes require an explicit state and provenance")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM cases WHERE id=? AND project=?", (case, project)).fetchone()
            if not row:
                raise ValueError("Case not found")
            target = split or row["split"]
            if target not in ("development", "validation", "holdout"):
                raise ValueError("Unknown evaluation split")
            if target == "holdout" and row["split"] != "holdout":
                raise ValueError("A previously classified case cannot become fresh holdout")
            spec = json.loads(row["spec"])
            history = spec.setdefault("lifecycle", [])
            history.append({"from": row["split"], "to": target, "active": active, "uses": row["uses"],
                            "source": source, "created": time.time()})
            spec["active"] = active
            db.execute("UPDATE cases SET split=?,spec=? WHERE id=?", (target, encode(spec), case))
            db.execute("INSERT INTO events(project,kind,message,data,created) VALUES (?,?,?,?,?)",
                       (project, "case_lifecycle", "Evaluation case retired or reclassified; exposure retained",
                        encode({"case": case, **history[-1]}), time.time()))

    def eligible_cases(self, project, limit):
        """Fresh-enough cases for one experiment, filtered inside SQLite in one query.

        与逐行 json.loads 过滤完全同义：损坏 spec 与缺失/空/假值字段的行被跳过；
        available/active 缺省视为 True，标量、对象或 "0" 这类非布尔值视为不可用。
        CASE WHEN json_valid 先行，保证 json_type/json_each 永不求值于损坏 JSON。
        """
        condition = ("CASE WHEN json_valid(spec) THEN (json_type(spec)='object' "
                     "AND json_type(spec,'$.task')='text' AND json_type(spec,'$.repository')='text' "
                     "AND json_type(spec,'$.base')='text' "
                     "AND (json_type(spec,'$.available') IS NULL OR (EXISTS(SELECT 1 FROM json_each(spec,'$.available')) "
                     "AND json_extract(spec,'$.available') NOT IN (0,''))) "
                     "AND (json_type(spec,'$.active') IS NULL OR (EXISTS(SELECT 1 FROM json_each(spec,'$.active')) "
                     "AND json_extract(spec,'$.active') NOT IN (0,'')))) ELSE 0 END")
        return self.store.query("SELECT * FROM cases WHERE project=? AND uses<? AND split IN ('validation','holdout') "
                                "AND " + condition + " ORDER BY created,id", (project, limit))

    def record_feedback(self, candidate, label, source, run=None, note=""):
        if label not in ("false_positive", "confirmed_regression", "rejected") or not source or not source.startswith("human:"):
            raise ValueError("Feedback requires a supported label and human provenance")
        row = self.store.one("SELECT project FROM candidates WHERE id=?", (candidate,))
        if not row:
            raise ValueError("Candidate not found")
        self.store.event("monitor_feedback", "Optional human monitoring label", row["project"], run,
                         {"candidate": candidate, "label": label, "source": source, "note": note[:4000]})
        return self.feedback_summary(candidate)

    def feedback_overview(self, project=None):
        return feedback_overview(self.store, project)

    def feedback_summary(self, candidate):
        rows = human_feedback(self.store, candidate=candidate)
        sources = {}
        for row in rows:
            sources[row["source"]] = sources.get(row["source"], 0) + 1
        return {"samples": len(rows), "labels": {label: sum(row["label"] == label for row in rows)
                for label in FEEDBACK_LABELS}, "sources": {key: sources[key] for key in sorted(sources)},
                "read_only": True,
                "bias": "Optional, self-selected human reports; not representative and never overwrite sealed trials",
                "automatic_threshold_changes": False}

    @staticmethod
    def attribution(rows):
        groups = {}
        variants = {"baseline", "strategy_only", "skill_only", "combined"}
        for row in rows:
            if not row.get("input") or row.get("variant") not in variants or type(row.get("pass")) is not bool:
                return {"state": "insufficient_evidence"}
            group = groups.setdefault(row["input"], {})
            if row["variant"] in group:
                return {"state": "insufficient_evidence"}
            group[row["variant"]] = row["pass"]
        if not groups or any(set(group) != variants for group in groups.values()):
            return {"state": "insufficient_evidence", "reason": "Four matched configurations required"}
        return {"state": "complete", "samples": len(groups),
                "strategy_wins": [key for key, group in groups.items() if not group["baseline"] and group["strategy_only"]],
                "skill_wins": [key for key, group in groups.items() if not group["baseline"] and group["skill_only"]],
                "interaction_only": [key for key, group in groups.items() if group["combined"] and not any(group[v] for v in variants - {"combined"})],
                "limitation": "Descriptive matched outcomes; no causal attribution without controlled execution"}

    def experiment_config(self, candidate_row, evidence):
        """Configuration of the sealed experiment run for export-time metadata.

        The recorded run configuration is authoritative; candidates without a
        stored run fall back to the current project configuration.
        """
        run_id = evidence.get("run") if isinstance(evidence, dict) else None
        if isinstance(run_id, str) and run_id:
            run_row = self.store.one("SELECT config FROM runs WHERE id=?", (run_id,))
            if run_row:
                try:
                    config = json.loads(run_row["config"])
                except (TypeError, ValueError):
                    config = None
                if isinstance(config, dict):
                    return config
        try:
            config = self.runtime.config(candidate_row["project"])
        except Exception:
            return {}
        return config if isinstance(config, dict) else {}

    @staticmethod
    def provider_requirements(config):
        """Non-secret provider requirements: provider id plus credential env var name.

        Mirrors readiness semantics: a provider needs an environment credential
        only when its env_key names one; login-based or local providers carry an
        empty env_key and must never imply OPENAI_API_KEY.
        """
        if not isinstance(config, dict):
            return []
        providers = {p.get("id"): p for p in (config.get("providers") or []) if isinstance(p, dict)}
        requirements = []
        seen = set()
        models = config.get("models")
        routes = models.values() if isinstance(models, dict) else []
        for route in routes:
            if not isinstance(route, dict):
                continue
            for item in [route, route.get("fallback")]:
                if not isinstance(item, dict):
                    continue
                provider_id = item.get("provider", "openai")
                if not isinstance(provider_id, str) or provider_id in seen:
                    continue
                seen.add(provider_id)
                entry = providers.get(provider_id) or {}
                env_key = entry.get("env_key")
                requirements.append({"id": provider_id, "env_key": env_key if isinstance(env_key, str) else ""})
        return requirements
    @staticmethod
    def ablation_required(proposal):
        """策略候选同时写入可检索技能；引导与触发词都非空时两者才会共同生效。

        triggers 为空的候选虽然也会写技能行，但检索永远不会命中，报告无从区分
        两个模块，因此不排入消融，保留原来的 insufficient_evidence。
        """
        if not isinstance(proposal, dict) or proposal.get("kind") not in ("strategy", "meta_strategy", "adapter"):
            return False
        guidance = proposal.get("guidance")
        triggers = proposal.get("triggers")
        return (isinstance(guidance, str) and bool(guidance.strip()) and isinstance(triggers, list) and
                any(isinstance(trigger, str) and trigger.strip() for trigger in triggers))

    @staticmethod
    def skill_relevant(triggers, text):
        """与 runtime.memories 相同的字面量触发规则，用于挑选真正会命中技能的输入。"""
        lower = (text or "").lower()
        # 与 runtime.memories 逐字一致：先按 strip 过滤空触发词，再按原样小写匹配，
        # 否则带有空白字符的触发词会在消融里命中、在真实检索里命中不了。
        return any(trigger.lower() in lower for trigger in triggers
                   if isinstance(trigger, str) and trigger.strip())

    def ablate(self, project, directory, run, config, current, proposal, candidate, trials):
        """在出现组合收益的输入上补跑 skill_only 与 combined 两种配置。

        配对试验已经按同一输入执行过 baseline（旧策略+旧技能）与 strategy_only
        （新策略、未带技能），因此只补跑缺少的两种配置，并把四行结果交给
        attribution()。任何缺口都保留 insufficient_evidence，不自动归因。
        """
        record = {"state": "insufficient_evidence", "configurations": list(ABLATION_CONFIGURATIONS),
                  "scope": "validation inputs with a demonstrated joint gain", "sampled": 0,
                  "rows": [], "trials": [], "partial": False,
                  "attribution": {"state": "insufficient_evidence",
                                  "reason": "Controlled ablation has not run"},
                  "limitation": "Descriptive matched outcomes on sampled inputs; the learned skill is injected "
                                "only where its triggers match; no automatic attribution"}
        limit = config["evolution"].get("ablation_cases", 0)
        if not limit:
            record["reason"] = "Controlled ablation is disabled (evolution.ablation_cases=0)"
            return record
        wins, seen = [], set()
        for trial in trials:
            # 按输入去重：attribution 要求每个输入各配置恰有一行，同一案例的多次重复
            # 若都补跑会生成重复配置行，使四配置比较永久停在 insufficient_evidence。
            case = trial.get("case")
            if (trial.get("split") != "validation" or case in seen or
                    trial.get("stable", {}).get("pass") is not False or
                    trial.get("candidate", {}).get("pass") is not True):
                continue
            seen.add(case)
            wins.append(trial)
        if not wins:
            record["reason"] = "No joint validation gain was observed, so no ablation input was scheduled"
            return record
        skill = {"content": str(proposal.get("guidance", ""))[:2000],
                 "evidence": {"candidate": candidate, "ablation": True},
                 "triggers": [trigger for trigger in proposal["triggers"] if isinstance(trigger, str)],
                 "created": time.time()}
        guide = [c for c in current if c["kind"] != proposal["kind"]] + [
            {"kind": proposal["kind"], "content": proposal}]
        # 四配置的既有技能按实验开始时的基线策略固定，不从各变体的 guide 推导：
        # baseline/skill_only 用 current，strategy_only/combined 换成 guide，
        # 只有这样才能保证技能集合只差「是否注入候选技能」。
        baseline_ids = {c["id"] for c in current if c.get("id")}
        error, invisible = None, False
        try:
            for trial in wins:
                if len({row["input"] for row in record["rows"]}) >= limit:
                    break
                row = self.store.one("SELECT * FROM cases WHERE id=? AND project=?", (trial["case"], project["id"]))
                spec = case_spec(row) if row else None
                if spec is None or not self.skill_relevant(proposal["triggers"], spec.get("task", "")):
                    continue
                # 候选技能必须在生产检索（runtime.memories → context.related）下可见，
                # 否则 skill_only 与 baseline 实际相同，四行结果只是在为看不见的技能报数。
                if not self.skill_retrievable(project["id"], spec.get("task", ""), skill):
                    invisible = True
                    continue
                if self.runtime.is_cancelled(run):
                    raise Cancelled("Evolution cancelled")
                skill_only = self.run_configuration(run, project, row, spec, "skill_only", trial["repetition"],
                                                    current, directory, candidate, config, branch="ablation",
                                                    experiment_skills=[skill], baseline_ids=baseline_ids)
                if self.runtime.is_cancelled(run):
                    raise Cancelled("Evolution cancelled")
                combined = self.run_configuration(run, project, row, spec, "combined", trial["repetition"],
                                                  guide, directory, candidate, config, branch="ablation",
                                                  experiment_skills=[skill], baseline_ids=baseline_ids)
                record["rows"].extend([
                    {"input": trial["case"], "variant": "baseline", "pass": trial["stable"]["pass"]},
                    {"input": trial["case"], "variant": "strategy_only", "pass": trial["candidate"]["pass"]},
                    {"input": trial["case"], "variant": "skill_only", "pass": skill_only["pass"]},
                    {"input": trial["case"], "variant": "combined", "pass": combined["pass"]}])
                record["trials"].append({"case": trial["case"], "repetition": trial["repetition"],
                                         "skill_only": skill_only, "combined": combined})
        except Cancelled:
            raise
        except Exception as exc:
            error = "Controlled ablation did not complete: " + str(exc)[:300]
        record["sampled"] = len({row["input"] for row in record["rows"]})
        if error:
            # 后续输入抛异常时已完成的四行只是部分证据，不能因为 attribution() 能
            # 凑齐一个输入就报 complete；晋升评审引用同一份不足证据。
            record["state"] = "insufficient_evidence"
            record["attribution"] = {"state": "insufficient_evidence", "reason": error}
            record["reason"] = error
            record["partial"] = bool(record["rows"])
        else:
            record["attribution"] = self.attribution(record["rows"])
            record["state"] = record["attribution"].get("state", "insufficient_evidence")
            if record["state"] != "complete":
                record["reason"] = ("Four matched configurations are unavailable for the sampled inputs; " +
                                    ("the candidate skill is not visible under production retrieval" if invisible
                                     else "the learned skill may not be retrieved by any sampled task"))
        return record

    def skill_retrievable(self, project, text, skill):
        """候选技能是否能被生产检索选中（与晋升后真实任务同一条 related() 规则）。"""
        evidence = skill.get("evidence", {})
        retrieved = self.runtime.memories(project, text, extra=[skill])
        return any(item.get("evidence", {}).get("candidate") == evidence.get("candidate") and
                   item.get("evidence", {}).get("ablation") for item in retrieved)

    def export_bundle(self, candidate, destination):
        """Export sealed local inputs, never credentials or arbitrary evidence paths."""
        import hashlib
        row = self.store.one("SELECT * FROM candidates WHERE id=?", (candidate,))
        if not row:
            raise ValueError("Candidate not found")
        root = Path(destination)
        root.mkdir(parents=True, exist_ok=False)
        evidence = json.loads(row["evidence"])
        try:
            # 非机密 provider 需求随包导出：只记录 provider id 与所需环境变量名，不含密钥值。
            # 证据中已登记的列表逐项裁剪为 {id, env_key}，任何多余字段（可能夹带密钥值）
            # 一律丢弃；未登记时才从实验 run 配置计算。
            providers_meta = evidence.get("providers")
            if isinstance(providers_meta, list):
                evidence["providers"] = [
                    {"id": item.get("id") if isinstance(item.get("id"), str) else "",
                     "env_key": item.get("env_key") if isinstance(item.get("env_key"), str) else ""}
                    for item in providers_meta if isinstance(item, dict)]
            else:
                evidence["providers"] = self.provider_requirements(self.experiment_config(row, evidence))
            (root / "evidence.json").write_text(encode(evidence), encoding="utf-8")
            (root / "candidate.json").write_text(row["content"], encoding="utf-8")
            limitations = ["Credentials are excluded; external services, model availability and host tools must be supplied independently",
                           "Integrity verification is not proof of isolated-machine reproduction"]
            for item in evidence.get("dataset", {}).get("manifest", []):
                if item["case"] != Path(item["case"]).name or item["case"] in (".", ".."):
                    raise ValueError("Invalid sealed case identity")
                case = self.store.one("SELECT spec FROM cases WHERE id=? AND project=?", (item["case"], row["project"]))
                if not case:
                    limitations.append("Missing case " + item["case"])
                    continue
                spec = json.loads(case["spec"])
                target = root / "cases" / item["case"]
                target.mkdir(parents=True)
                snapshot(spec["repository"], target / "repository", spec["base"])
                if spec.get("overlay"):
                    scrub_tree(spec["overlay"])
                    shutil.copytree(spec["overlay"], target / "overlay", symlinks=True)
                spec = {**spec, "repository": "repository", "overlay": "overlay" if spec.get("overlay") else None}
                (target / "case.json").write_text(encode(spec), encoding="utf-8")
            files = {}
            for path in sorted(root.rglob("*")):
                if ".git" in path.relative_to(root).parts:
                    continue
                if path.is_symlink():
                    raise ValueError("Reproduction bundles require regular files")
                if path.is_file():
                    files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest = {"version": 1, "files": files, "limitations": limitations}
            (root / "bundle.json").write_text(encode(manifest), encoding="utf-8")
            return manifest
        except Exception:
            shutil.rmtree(root)
            raise

    @staticmethod
    def verify_bundle(directory):
        import hashlib
        root = Path(directory).resolve()
        manifest = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
        mismatches = []
        for relative, digest in manifest["files"].items():
            path = root / relative
            if Path(relative).is_absolute() or ".." in Path(relative).parts or any(p.is_symlink() for p in [path] + list(path.parents)):
                mismatches.append(relative)
            elif not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                mismatches.append(relative)
        return {"valid": not mismatches, "mismatches": mismatches, "limitations": manifest.get("limitations", [])}

    @staticmethod
    def reproduce_bundle(directory, env=None, which=None, required_credentials=None, run_checks=False):
        """Inspect and verify offline reproduction readiness for a sealed bundle.

        Verifies input integrity and itemizes all missing tool dependencies and
        credentials without failing fast on the first missing requirement.
        """
        import os
        import shutil
        import subprocess

        root = Path(directory).resolve()
        bundle_file = root / "bundle.json"
        if not bundle_file.is_file():
            return {
                "valid": False,
                "integrity": {"valid": False, "mismatches": ["bundle.json"]},
                "dependencies": {"available": [], "missing": []},
                "credentials": {"available": [], "missing": []},
                "limitations": ["Missing bundle.json: target is not a valid reproduction bundle directory"],
                "reproducible": False,
                "executed_checks": [],
            }

        try:
            integrity = Evolution.verify_bundle(root)
        except Exception as exc:
            integrity = {"valid": False, "mismatches": [f"bundle.json: {exc}"], "limitations": []}

        manifest = {}
        try:
            manifest = json.loads(bundle_file.read_text(encoding="utf-8"))
        except Exception:
            pass

        limitations = list(manifest.get("limitations") or integrity.get("limitations") or [])

        evidence = {}
        if (root / "evidence.json").is_file():
            try:
                evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
            except Exception:
                pass

        env_dict = os.environ if env is None else env
        path_env = env["PATH"] if (env is not None and "PATH" in env) else os.environ.get("PATH")
        lookup = which or shutil.which

        available_tools = []
        missing_tools = []

        # 1. VCS dependency (git)
        git_found = lookup("git", path=path_env) if path_env else lookup("git")
        if git_found:
            available_tools.append({"name": "git", "type": "vcs", "path": str(git_found)})
        else:
            missing_tools.append({"name": "git", "type": "vcs", "reason": "git executable not found in PATH"})

        # 2. Inspect case check dependencies across verified manifest entries only.
        # 未登记的用例目录一律不读、不执行；用例路径出现符号链接则按 verify_bundle
        # 同一套规则判 integrity 失效（防 verify 与读取之间被调包）。
        case_items = []
        invalid_cases = []
        if integrity.get("valid"):
            files = manifest.get("files")
            if isinstance(files, dict):
                for relative in sorted(files):
                    parts = Path(relative).parts
                    if len(parts) != 3 or parts[0] != "cases" or parts[2] != "case.json":
                        continue
                    case_json = root.joinpath(*parts)
                    if any(p.is_symlink() for p in [case_json] + list(case_json.parents)):
                        invalid_cases.append(relative)
                        continue
                    try:
                        spec = json.loads(case_json.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        invalid_cases.append(relative)
                        continue
                    if isinstance(spec, dict):
                        case_items.append((parts[1], case_json.parent, spec))
                # 未登记的用例目录与符号链接条目按 verify_bundle 同一规则判失效：
                # 只做存在性检查，不读取其内容，更不执行。
                cases_dir = root / "cases"
                if cases_dir.is_dir():
                    for entry in sorted(cases_dir.iterdir()):
                        if entry.is_symlink():
                            invalid_cases.append(f"cases/{entry.name}")
                        elif (entry / "case.json").is_file() and f"cases/{entry.name}/case.json" not in files:
                            invalid_cases.append(f"cases/{entry.name}/case.json")
            if invalid_cases:
                integrity = {**integrity, "valid": False,
                             "mismatches": list(integrity.get("mismatches", [])) + invalid_cases}
                case_items = []

        seen_tools = {}
        for case_id, case_entry, spec in case_items:
            for check in spec.get("checks", []):
                argv = check.get("argv", [])
                if not argv or not isinstance(argv, list):
                    continue
                cmd = argv[0]
                check_name = check.get("name", "check")
                if cmd not in seen_tools:
                    found = lookup(cmd, path=path_env) if path_env else lookup(cmd)
                    if found:
                        info = {"name": cmd, "type": "executable", "path": str(found),
                                "cases": [case_id], "checks": [check_name]}
                        seen_tools[cmd] = ("available", info)
                    else:
                        info = {"name": cmd, "type": "executable",
                                "reason": f"Command '{cmd}' not found in PATH",
                                "cases": [case_id], "checks": [check_name]}
                        seen_tools[cmd] = ("missing", info)
                else:
                    status, info = seen_tools[cmd]
                    if case_id not in info["cases"]:
                        info["cases"].append(case_id)
                    if check_name not in info["checks"]:
                        info["checks"].append(check_name)

        for status, info in seen_tools.values():
            if status == "available":
                available_tools.append(info)
            else:
                missing_tools.append(info)

        # 3. Model credentials check, derived from exported provider requirements
        credential_limitation = None
        if required_credentials is not None:
            needed_creds = list(required_credentials)
        else:
            providers_meta = evidence.get("providers") if isinstance(evidence, dict) else None
            if isinstance(providers_meta, list):
                # 导出包已记录非机密 provider 需求：只有 env_key 指向环境变量的
                # provider 才需要凭据；login 或本地 provider 不暗示任何默认密钥。
                needed_creds = []
                for prov in providers_meta:
                    if isinstance(prov, dict):
                        env_key = prov.get("env_key")
                        if isinstance(env_key, str) and env_key and env_key not in needed_creds:
                            needed_creds.append(env_key)
            else:
                # 导出包未记录 provider 需求：凭据可用性无法判定，不再默认要求 OPENAI_API_KEY。
                needed_creds = []
                credential_limitation = ("Bundle does not record provider requirements; "
                                         "credential availability cannot be determined")

        available_creds = []
        missing_creds = []
        for cred_key in sorted(needed_creds):
            val = env_dict.get(cred_key)
            if val:
                available_creds.append({"name": cred_key, "type": "env_key"})
            else:
                missing_creds.append({"name": cred_key, "type": "env_key",
                                      "reason": f"Credential environment variable '{cred_key}' is not set"})

        # 4. Formulate limitations
        repro_limitations = list(limitations)
        if credential_limitation and credential_limitation not in repro_limitations:
            repro_limitations.append(credential_limitation)
        if missing_creds:
            msg = f"Missing credentials: {', '.join(c['name'] for c in missing_creds)} not configured; online model generation cannot be reproduced without external provider access"
            if msg not in repro_limitations:
                repro_limitations.append(msg)
        if missing_tools:
            msg = f"Missing host tools: {len(missing_tools)} required executable(s) missing ({', '.join(t['name'] for t in missing_tools)})"
            if msg not in repro_limitations:
                repro_limitations.append(msg)
        offline_note = "Isolated offline environment: external model APIs cannot be contacted without network egress and valid credentials"
        if offline_note not in repro_limitations:
            repro_limitations.append(offline_note)

        is_reproducible = (
            bool(integrity.get("valid")) and
            len(missing_tools) == 0 and
            len(missing_creds) == 0 and
            credential_limitation is None
        )

        # 5. Optional check execution
        # 检查命令在一次性临时副本上执行：先复制 repository，再把封存 overlay
        # 叠上去（与 evaluator 同一套 scrub + 符号链接拒绝语义），bundle 原文件保持不变。
        executed_checks = []
        if run_checks and integrity.get("valid") and not missing_tools:
            import tempfile
            for case_id, case_entry, spec in case_items:
                repo_dir = case_entry / "repository"
                overlay_dir = case_entry / "overlay"
                staging_tmp = None
                try:
                    staging_tmp = tempfile.mkdtemp(prefix="bundle-check-")
                    staging = Path(staging_tmp) / "repository"
                    overlay_applied = False
                    try:
                        shutil.copytree(repo_dir, staging, symlinks=True)
                        if overlay_dir.is_dir():
                            scrub_tree(overlay_dir)
                            for parent, dirs, files in os.walk(overlay_dir, followlinks=False):
                                for name in files:
                                    source = Path(parent) / name
                                    target = staging / source.relative_to(overlay_dir)
                                    if target.is_symlink() or any(p.is_symlink() for p in target.parents):
                                        raise RuntimeError("Symlink overlaps reproduction overlay")
                                    target.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.copy2(source, target)
                                    overlay_applied = True
                    except Exception as exc:
                        executed_checks.append({
                            "case": case_id, "check": None, "argv": [],
                            "exit_code": -1, "passed": False, "error": str(exc),
                        })
                        continue
                    for check in spec.get("checks", []):
                        argv = check.get("argv", [])
                        if not argv:
                            continue
                        cwd = (staging / check.get("cwd", ".")).resolve()
                        run_cwd = cwd if cwd.is_dir() else staging
                        try:
                            proc = subprocess.run(
                                argv,
                                cwd=run_cwd,
                                capture_output=True,
                                text=True,
                                timeout=60,
                                env=env_dict,
                            )
                            executed_checks.append({
                                "case": case_id,
                                "check": check.get("name"),
                                "argv": argv,
                                "exit_code": proc.returncode,
                                "passed": proc.returncode == 0,
                                "overlay_applied": overlay_applied,
                                "stdout": proc.stdout[:1000],
                                "stderr": proc.stderr[:1000],
                            })
                        except Exception as exc:
                            executed_checks.append({
                                "case": case_id,
                                "check": check.get("name"),
                                "argv": argv,
                                "exit_code": -1,
                                "passed": False,
                                "overlay_applied": overlay_applied,
                                "error": str(exc),
                            })
                except Exception as exc:
                    executed_checks.append({
                        "case": case_id, "check": None, "argv": [],
                        "exit_code": -1, "passed": False, "error": str(exc),
                    })
                finally:
                    if staging_tmp:
                        shutil.rmtree(staging_tmp, ignore_errors=True)

        return {
            "valid": bool(integrity.get("valid")),
            "integrity": integrity,
            "dependencies": {
                "available": available_tools,
                "missing": missing_tools,
            },
            "credentials": {
                "available": available_creds,
                "missing": missing_creds,
            },
            "limitations": repro_limitations,
            "reproducible": is_reproducible,
            "executed_checks": executed_checks,
        }

    def import_case(self, project, manifest):
        self.runtime.project(project)
        required = {"family", "split", "repository", "base", "task", "checks", "source"}
        if not isinstance(manifest, dict) or not required.issubset(manifest):
            raise ValueError("Case requires family, split, repository, base, task, checks, source")
        if manifest["split"] not in ["development", "validation", "holdout"]:
            raise ValueError("Unknown evaluation split")
        if manifest["source"] not in ["real", "synthetic"]:
            raise ValueError("Case source must be real or synthetic")
        if not manifest["family"] or not manifest["task"] or not manifest["checks"]:
            raise ValueError("Empty evaluation case")
        source = repository(manifest["repository"])
        base = git(source, "rev-parse", "--verify", manifest["base"] + "^{commit}")
        validate(merge(self.runtime.config(project), {"checks": manifest["checks"]}))
        signature = fingerprint({"task": manifest["task"], "tree": git(source, "rev-parse", base + "^{tree}")})
        for row in self.store.query("SELECT family,spec FROM cases WHERE project=?", (project,)):
            if row["family"] == manifest["family"] or json.loads(row["spec"])["signature"] == signature:
                raise ValueError("Same task/defect family cannot enter multiple splits")
        case = uid()
        directory = self.store.root / "suites" / case
        directory.mkdir(parents=True)
        try:
            snapshot(source, directory / "repository", base)
            overlay = manifest.get("overlay")
            if overlay:
                overlay = Path(overlay).expanduser().resolve()
                if not overlay.is_dir():
                    raise ValueError("Overlay must be a directory")
                scrub_tree(overlay)
                shutil.copytree(overlay, directory / "overlay", symlinks=True)
            spec = {"task": manifest["task"], "base": base, "checks": manifest["checks"],
                    "source": manifest["source"], "signature": signature,
                    "repository": str(directory / "repository"),
                    "overlay": str(directory / "overlay") if overlay else None}
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                duplicate = db.execute("SELECT 1 FROM cases WHERE project=? AND (family=? OR "
                                       "(json_valid(spec) AND json_extract(spec,'$.signature')=?))",
                                       (project, manifest["family"], signature)).fetchone()
                if duplicate:
                    raise ValueError("Same task/defect family cannot enter multiple splits")
                db.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
                           (case, project, manifest["family"], manifest["split"], encode(spec), 0, time.time()))
        except Exception:
            shutil.rmtree(directory)
            raise
        self.store.event("case", "Protected evaluation case imported", project,
                         data={"case": case, "split": manifest["split"], "source": manifest["source"]})
        return case

    def proposal_inputs(self, project):
        """Read the local inputs shared by proposal prompting and repeat suppression."""
        observations = self.store.query("SELECT content,evidence,status FROM skills WHERE project=? "
                                        "ORDER BY created DESC LIMIT 10", (project,))
        failures = []
        for row in self.store.query("SELECT r.state,r.result FROM runs r JOIN tasks t ON r.task=t.id "
                                    "WHERE r.project=? AND r.state='failed' AND t.source NOT LIKE 'system_%' "
                                    "ORDER BY r.started DESC LIMIT 5", (project,)):
            try:
                result = json.loads(row["result"]) if row["result"] else {}
            except (ValueError, TypeError):
                result = {}
            failures.append({"state": row["state"], "reason": result.get("reason", "")[:1000]})
        development = []
        for row in self.store.query("SELECT spec FROM cases WHERE project=? AND split='development' "
                                    "AND (NOT json_valid(spec) OR json_extract(spec,'$.active') IS NOT 0) "
                                    "ORDER BY created DESC, id LIMIT 10", (project,)):
            try:
                task = json.loads(row["spec"])["task"]
            except (TypeError, ValueError, KeyError):
                continue
            if isinstance(task, str):
                development.append(task)
        return {"observations": observations, "failures": failures, "development": development,
                "current": self.runtime.active_strategies(project),
                "archive": self.store.query("SELECT id,kind,content,status FROM candidates WHERE project=? "
                                            "ORDER BY created DESC LIMIT 20", (project,)),
                "cases": self.store.query("SELECT * FROM cases WHERE project=? ORDER BY id", (project,))}

    def input_signature(self, project, config, inputs=None):
        """Fingerprint the evidence, samples, policy and strategies that frame one experiment."""
        from .build import BUILD_ID
        inputs = inputs or self.proposal_inputs(project)
        cases = []
        for case in inputs["cases"]:
            spec = case_spec(case)
            cases.append({"id": case["id"], "split": case["split"], "family": case["family"],
                          "spec": fingerprint(spec) if spec is not None else None,
                          "overlay": self.overlay_digest(spec.get("overlay")) if spec else None,
                          "active": spec.get("active", True) if spec else None,
                          "available": spec.get("available", True) if spec else None})
        return fingerprint({"build": BUILD_ID, "limits": config["limits"],
            "observations": inputs["observations"], "failures": inputs["failures"],
            "development": inputs["development"],
            "strategies": [{"id": c["id"], "kind": c["kind"], "content": c["content"]} for c in inputs["current"]],
            "cases": cases,
            "policy": {"triage": config["triage"]["policy"],
                       "compatibility": compatibility(config),
                       "evolution": {key: config["evolution"][key] for key in
                                     ("min_cases_per_split", "repeats", "max_holdout_uses", "max_cost_ratio")}}})

    def attempt_due(self, project, config):
        signature = self.input_signature(project, config)
        attempt = self.store.setting("evolution-attempt:" + project, {})
        return not (isinstance(attempt, dict) and attempt.get("signature") == signature), signature, attempt

    def record_attempt(self, project, signature, status, candidate=None, reason=None):
        self.store.put_setting("evolution-attempt:" + project, {
            "signature": signature, "status": status, "candidate": candidate,
            "reason": str(reason or "")[:1000], "at": time.time()})

    def run_configuration(self, run, project, case, spec, variant, repetition, guide, directory, candidate, config,
                          branch="experiments", experiment_skills=None, baseline_ids=None):
        """Run one controlled configuration through the normal experimental pipeline.

        配对试验与消融试验共用这条路径：同样的案例曝光上限、取消检查、预算记录和
        overlay 评估，不存在绕开评估器的第二通道。``baseline_ids`` 是实验开始时
        ``active_strategies`` 的策略 id 集合，用于把四配置的既有技能固定成同一批。
        """
        trial_dir = directory / branch / (case["id"] + "-" + str(repetition)) / variant
        trial_dir.mkdir(parents=True)
        worker = snapshot(spec["repository"], trial_dir / "workspace", spec["base"])
        before = self.store.one("SELECT coalesce(sum(tokens),0) AS n, count(*) AS calls, coalesce(sum(usage_known IS NOT 1),0) AS unknown FROM calls WHERE run=?", (run["id"],))
        started = time.monotonic()
        trial_config = merge(config, {"checks": spec["checks"]})
        new_context = getattr(self.runtime, "new_trial_context", None)
        trial_run = new_context(run) if new_context else {**run}
        trial_run.update({"config": encode(trial_config),
                          "_case_exposure": {"case": case["id"], "experiment": candidate,
                                             "limit": config["evolution"]["max_holdout_uses"]}})
        trial_task = {"id": run["task"], "title": spec["task"].splitlines()[0], "body": spec["task"],
                      "source": "evaluation", "evidence": "[]", "acceptance": "[]"}
        outcome = self.runtime.execute_candidate(trial_run, {**project, "path": spec["repository"]},
            trial_task, spec["repository"], spec["base"], trial_dir, worker, guide,
            experiment=True, experiment_skills=experiment_skills, baseline_ids=baseline_ids)
        if hasattr(self.runtime, "propagate_deadline"):
            self.runtime.propagate_deadline(run, trial_run)
        # A case overlay is host-owned and remains outside model-visible workspaces.
        if spec.get("overlay") and (trial_dir / "candidate").exists():
            overlay_result = self.runtime.evaluator.evaluate(
                trial_dir / "baseline", trial_dir / "candidate", spec["checks"],
                trial_dir / "protected-evaluation", trial_config,
                lambda: self.runtime.is_cancelled(run), overlay=spec["overlay"])
            if overlay_result["decision"] != "pass":
                outcome = {**(outcome or {}), "decision": overlay_result["decision"]}
        after = self.store.one("SELECT coalesce(sum(tokens),0) AS n, count(*) AS calls, coalesce(sum(usage_known IS NOT 1),0) AS unknown FROM calls WHERE run=?", (run["id"],))
        return {"pass": bool(outcome and outcome.get("decision") == "pass"),
                "review": (outcome or {}).get("review", {}), "reason": (outcome or {}).get("reason", "No change"),
                "seconds": time.monotonic() - started, "tokens": after["n"] - before["n"],
                "usage_known": after["calls"] > before["calls"] and after["unknown"] == before["unknown"],
                "report": str(trial_dir / "evaluation" / "report.json")}

    def propose_and_evaluate(self, project, workspace, directory, run, force=False):
        config = json.loads(run["config"])
        inputs = self.proposal_inputs(project["id"])
        signature = self.input_signature(project["id"], config, inputs)
        attempt = self.store.setting("evolution-attempt:" + project["id"], {})
        started = attempt.get("at") if isinstance(attempt, dict) else None
        in_flight = (isinstance(attempt, dict) and attempt.get("status") == "running"
                     and isinstance(started, (int, float)) and time.time() - started <= 3600)
        if not force and not in_flight and isinstance(attempt, dict) and attempt.get("signature") == signature:
            reason = ("Identical experiment inputs were already evaluated with status "
                      + str(attempt.get("status", "insufficient_evidence")) +
                      "; change evidence, cases, policy or strategies, or retry explicitly with force")
            self.store.event("evolution_not_repeated", reason, project["id"], run["id"],
                             {"signature": signature, "previous": attempt.get("status")})
            previous = attempt.get("status")
            return {"reason": reason,
                    "status": previous if previous in ("insufficient_evidence", "fail", "failed")
                    else "insufficient_evidence", "repeated": True,
                    "code": "evolution_inputs_unchanged"}
        limit = config["evolution"]["max_holdout_uses"]
        cases = self.eligible_cases(project["id"], limit)
        minimum = config["evolution"]["min_cases_per_split"]
        validation_cases = [case for case in cases if case["split"] == "validation"]
        # 晋升要求未曝光的 holdout；先按用途过滤新鲜样本，再在新鲜样本内分层选择。
        fresh_holdout = [case for case in cases if case["split"] == "holdout" and case["uses"] == 0]
        if len(validation_cases) < minimum or len(fresh_holdout) < minimum:
            # 复用过的样本即使仍在 max_holdout_uses 内也必然在最终审核失败，因此在
            # 提案模型调用前直接跳过，不生成无用 candidate，也不消耗付费调用。
            reason = ("Fresh holdout cases are required for promotion; import new, previously unused cases "
                      "(raising evolution.max_holdout_uses cannot refresh exposed holdout cases)")
            self.record_attempt(project["id"], signature, "insufficient_evidence", reason=reason)
            return {"reason": reason, "status": "insufficient_evidence",
                    "code": "evolution_fresh_holdout_required"}
        cases = validation_cases + fresh_holdout
        reserved = sorted(case["id"] for case in fresh_holdout)
        reservation = uid()
        manifest = []
        for case in cases:
            spec = case_spec(case) or {}
            repository, base = spec.get("repository"), spec.get("base")
            if not isinstance(repository, str) or not isinstance(base, str) or not isinstance(spec.get("task"), str):
                continue
            manifest.append({"case": case["id"], "split": case["split"], "source": spec.get("source", "unknown"),
                             "signature": spec.get("signature", ""),
                             "tree": git(repository, "rev-parse", base + "^{tree}"),
                             "checks": fingerprint(spec.get("checks", [])), "family": case["family"],
                             "task": fingerprint(spec["task"]), "spec": fingerprint(spec),
                             "overlay": self.overlay_digest(spec.get("overlay")),
                             "environment": fingerprint(config["environment"])})
        # 本函数此前只读取本地状态；预留只覆盖即将开始的付费提案窗口。
        observations = inputs["observations"]
        failures = inputs["failures"]
        development = inputs["development"]
        current = inputs["current"]
        archive = inputs["archive"]
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # 过期预留不得永久阻塞；一小时与旧有的阻塞窗口保持一致。
            db.execute("UPDATE candidates SET status='interrupted' WHERE project=? AND status='reserved' AND created<=?",
                       (project["id"], now - 3600))
            if db.execute("SELECT 1 FROM candidates WHERE project=? AND status='reserved'",
                          (project["id"],)).fetchone():
                return {"reason": "Another proposal currently reserves the holdout cases",
                        "status": "insufficient_evidence", "code": "evolution_reservation_busy"}
            db.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) VALUES (?,?,?,?,?,?,?)",
                       (reservation, project["id"], "strategy", "reserved",
                        encode({"guidance": "", "triggers": []}),
                        encode({"reservation": {"holdout": reserved}, "reason": "reserved"}), now))
        self.record_attempt(project["id"], signature, "running", reason="Proposal started")
        try:
            proposal = self.runtime.invoke("evolution", "Propose one small, testable improvement to agent execution, "
                                       "opportunity discovery/meta-planning, or toolchain/context adapter guidance. "
                                       "Do not change evaluator, budgets, permissions, or promotion rules. "
                                       "Guidance is a retrieved instruction, never executable controller code. "
                                       "Use only observations and development examples; held-out tasks are unavailable.\n" +
                                       encode({"observations": observations, "failures": failures,
                                               "development_tasks": development, "current": current,
                                               "candidate_archive": archive}), workspace, run)
        except Exception as exc:
            self.store.execute("UPDATE candidates SET status='insufficient_evidence', evidence=? WHERE id=?",
                (encode({"reservation": {"holdout": reserved}, "reason": str(exc)}), reservation))
            self.record_attempt(project["id"], signature, "insufficient_evidence", reason=str(exc))
            raise
        if not isinstance(proposal, dict) or "kind" not in proposal:
            reason = "Model proposal must be an object with kind and content"
            self.store.execute("UPDATE candidates SET status='insufficient_evidence', evidence=? WHERE id=?",
                (encode({"reservation": {"holdout": reserved}, "reason": reason}), reservation))
            self.record_attempt(project["id"], signature, "insufficient_evidence", reason=reason)
            raise RuntimeError(reason)
        candidate = uid()
        previous = self.store.one("SELECT id FROM candidates WHERE project=? AND kind=? AND status='stable' ORDER BY promoted DESC LIMIT 1",
                                  (project["id"], proposal["kind"]))
        parent = previous["id"] if previous else None
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # 删除预留与写入候选同一事务：支付成功后不留下阻塞后续提案的孤儿预留。
            db.execute("DELETE FROM candidates WHERE id=?", (reservation,))
            db.execute("INSERT INTO candidates(id,project,kind,parent,status,content,created) VALUES (?,?,?,?,?,?,?)",
                       (candidate, project["id"], proposal["kind"], parent, "evaluating",
                        encode(proposal), time.time()))
        evidence = {"run": run["id"], "config_hash": fingerprint(config), "compatibility": compatibility(config),
                    "experiment_inputs": signature,
                    "dataset": {"version": 1, "manifest": sorted(manifest, key=lambda item: item["case"]),
                                "holdout_hash": fingerprint([item for item in manifest if item["split"] == "holdout"])},
                    "trials": [], "monitor": {"passed": 0, "failed": 0}}
        evidence["selection_attempts"] = self.store.one(
            "SELECT count(*) AS n FROM candidates WHERE project=? AND kind=? AND "
            "json_valid(evidence) AND json_extract(evidence,'$.compatibility')=?",
            (project["id"], proposal["kind"], evidence["compatibility"]))["n"] + 1
        # 新鲜 holdout 已经在上面单独筛出，分层抽样只作用于这组未曝光样本。
        selection = {"validation": select_cases(validation_cases, minimum, run["id"]),
                     "holdout": select_cases(fresh_holdout, minimum, run["id"])}
        plan = {"version": 1, "created": time.time(), "repeats": config["evolution"]["repeats"],
                "metric": "independent_case_family_paired_pass", "max_cost_ratio": config["evolution"]["max_cost_ratio"],
                "stop_rules": ["validation_failure", "cancelled", "resource_budget", "complete_planned_trials"],
                "samples": [{"id": case["id"], "split": split, "stratum": case_stratum(case), "prior_uses": case["uses"]}
                            for split, rows in selection.items() for case in rows],
                "proposal_hash": fingerprint(proposal), "baseline_hash": fingerprint(current),
                "configuration_hash": fingerprint(config),
                "proposal_inputs_hash": fingerprint({"observations": observations, "failures": failures,
                    "development_tasks": development, "current": current, "candidate_archive": archive})}
        ablation = self.ablation_required(proposal)
        if ablation:
            # 策略与技能共同变化时把受控消融一并封存进计划；采样上限沿用配置的
            # ablation_cases，不额外增加试验规模或绕过任何评估边界。
            plan["ablation"] = {"configurations": list(ABLATION_CONFIGURATIONS),
                                "sampling": "validation inputs with a demonstrated joint gain",
                                "limit": config["evolution"].get("ablation_cases", 0)}
        evidence["preregistered_plan"] = {"data": plan, "sha256": fingerprint(plan)}
        evidence["promotion_review"] = {
            "baseline": current, "proposal": proposal, "experiment": candidate,
            "changed_kind": proposal["kind"], "plan_hash": fingerprint(plan),
            "covered_samples": plan["samples"],
            "limitations": ["Only sampled task families, sources and size strata are covered",
                            "Strategy and retrieved skills are jointly applied; individual causal effects are unproven"],
            "attribution": {"state": "insufficient_evidence",
                            "reason": "Four matched ablation configurations were not executed" if not ablation else
                                      "Controlled ablation is scheduled but has not completed"}}
        if ablation:
            evidence["ablation"] = {"state": "scheduled", "configurations": list(ABLATION_CONFIGURATIONS),
                                    "scope": "validation inputs with a demonstrated joint gain", "sampled": 0,
                                    "rows": [], "trials": [],
                                    "attribution": {"state": "insufficient_evidence",
                                                    "reason": "Controlled ablation has not run"}}
        evidence["final_review"] = {"split": "holdout", "purpose": "final_confirmation_only",
                                    "proposal_hash": fingerprint(proposal),
                                    "cases": [case["id"] for case in selection["holdout"]],
                                    "fresh": all(case["uses"] == 0 for case in selection["holdout"])}
        self.save(candidate, "evaluating", evidence)
        # 四配置的既有技能必须来自同一批基线策略：稳定版与候选版 guide 不同，
        # 若各自推导技能集合，strategy_only 会连带丢掉被替换策略的旧技能。
        baseline_ids = {c["id"] for c in current if c.get("id")}
        try:
            for split in ["validation", "holdout"]:
                selected = selection[split]
                for case in selected:
                    spec = case_spec(case)
                    if spec is None:
                        continue
                    for repetition in range(config["evolution"]["repeats"]):
                        trial = {"case": case["id"], "split": split, "source": spec.get("source", "unknown"), "family": case["family"], "stratum": case_stratum(case), "repetition": repetition}
                        order = ["stable", "candidate"] if len(evidence["trials"]) % 2 == 0 else ["candidate", "stable"]
                        trial["order"] = order
                        for variant in order:
                            if self.runtime.is_cancelled(run):
                                raise Cancelled("Evolution cancelled")
                            guide = current if variant == "stable" else [c for c in current if c["kind"] != proposal["kind"]] + [{"kind": proposal["kind"], "content": proposal}]
                            trial[variant] = self.run_configuration(run, project, case, spec, variant, repetition,
                                                                    guide, directory, candidate, config,
                                                                    baseline_ids=baseline_ids)
                        evidence["trials"].append(trial)
                        self.save(candidate, "evaluating", evidence)
                if split == "validation":
                    validation = self.split_decision(evidence["trials"], config)
                    if validation["decision"] != "pass":
                        evidence.update(validation)
                        if "ablation" in evidence:
                            # 验证未通过时消融不会执行：明确标记证据不足，避免把
                            # “已排入”长期留在已封存证据里冒充待补比较。
                            evidence["ablation"].update(
                                state="insufficient_evidence",
                                reason="Controlled ablation was scheduled but not executed because validation did not pass")
                            evidence["promotion_review"]["attribution"] = evidence["ablation"]["attribution"]
                        self.save(candidate, validation["decision"], evidence)
                        self.record_attempt(project["id"], signature, validation["decision"],
                                            candidate, evidence["reason"])
                        return {"reason": evidence["reason"], "candidate": candidate, "status": evidence["decision"]}
            evidence.update(self.decide(evidence["trials"], config))
            evidence["plan_verification"] = self.verify_plan(evidence["preregistered_plan"], evidence["trials"])
            if not evidence["plan_verification"]["valid"]:
                evidence.update(decision="insufficient_evidence", reason="Preregistered trial plan is incomplete or changed")
            evidence["input_verification"] = self.verify_inputs(project["id"], manifest)
            if not evidence["input_verification"]["valid"]:
                evidence.update(decision="insufficient_evidence", reason="Sealed experiment inputs changed during execution")
            if evidence["decision"] == "pass" and not evidence["final_review"]["fresh"]:
                evidence.update(decision="insufficient_evidence", reason="Final promotion requires previously unexposed holdout cases")
            if "sign_test_p" in evidence:
                evidence["selection_adjusted_p"] = min(1, evidence["sign_test_p"] * evidence["selection_attempts"])
                evidence["selection_warning"] = "Adaptive reuse and candidate selection limit generalization; adjusted p is a conservative attempt-count diagnostic, not independent replication"
            evidence["win_rate"] = paired_summary(evidence["trials"])
            if "ablation" in evidence:
                if evidence["decision"] == "pass":
                    # 只有出现组合收益才补齐消融；否则保留 insufficient_evidence，
                    # 不把未执行的比较写成归因结论。
                    evidence["ablation"] = self.ablate(project, directory, run, config, current,
                                                       proposal, candidate, evidence["trials"])
                else:
                    evidence["ablation"].update(
                        state="insufficient_evidence",
                        reason="Controlled ablation was scheduled but not executed because no joint gain passed")
                evidence["promotion_review"]["attribution"] = evidence["ablation"]["attribution"]
                self.save(candidate, "evaluating", evidence)
            state = evidence["decision"]
            if state == "pass" and config["evolution"]["auto_promote"]:
                with self.runtime.dispatch_lock:
                    if self.runtime.is_cancelled(run):
                        raise Cancelled("Evolution cancelled before promotion")
                    live = self.runtime.config(project["id"])
                    if not live["evolution"]["auto_promote"] or compatibility(live) != evidence["compatibility"]:
                        evidence["reason"] = "Configuration changed; candidate retained for explicit revalidation"
                        self.save(candidate, "ready", evidence)
                        self.record_attempt(project["id"], signature, "ready", candidate, evidence["reason"])
                        return {"reason": evidence["reason"], "candidate": candidate, "status": "ready"}
                    promoted = time.time()
                    with self.store.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        self.activate(db, candidate, promoted, evidence)
                        db.execute("INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?)",
                                   self.skill_values(project["id"], candidate, proposal, config, promoted))
                self.store.event("promotion", "Candidate passed paired validation and holdout; limited operational monitoring started",
                                 project["id"], run["id"], {"candidate": candidate})
                state = "stable"
                if hasattr(self, "_plan_keys"):
                    self._plan_keys.pop(candidate, None)
            else:
                self.save(candidate, "ready" if state == "pass" else state, evidence)
            self.record_attempt(project["id"], signature, state, candidate, evidence["reason"])
            return {"reason": evidence["reason"], "candidate": candidate, "status": state}
        except Exception as exc:
            evidence.update(decision="insufficient_evidence", reason=str(exc))
            outcome = "interrupted" if isinstance(exc, Cancelled) else "insufficient_evidence"
            self.save(candidate, outcome, evidence)
            self.record_attempt(project["id"], signature, outcome, candidate, str(exc))
            raise

    @staticmethod
    def activate(db, candidate, now, evidence=None):
        """在调用方事务内启用候选，并把 parent 重绑到刚被替换的稳定版。"""
        row = db.execute("SELECT project,kind FROM candidates WHERE id=?", (candidate,)).fetchone()
        previous = db.execute(
            "SELECT id FROM candidates WHERE project=? AND kind=? AND status='stable' "
            "ORDER BY coalesce(promoted,created) DESC, created DESC LIMIT 1",
            (row["project"], row["kind"])).fetchone()
        parent = previous["id"] if previous else None
        db.execute("UPDATE candidates SET status='superseded' WHERE project=? AND kind=? AND status='stable'",
                   (row["project"], row["kind"]))
        if evidence is None:
            db.execute("UPDATE candidates SET status='stable',promoted=?,parent=? WHERE id=?",
                       (now, parent, candidate))
        else:
            db.execute("UPDATE candidates SET status='stable',promoted=?,parent=?,evidence=? WHERE id=?",
                       (now, parent, encode(evidence), candidate))
        return parent

    @staticmethod
    def skill_values(project, candidate, content, config, now):
        """自动晋升与手动晋升写同一种已验证技能，避免两条路径产生不同字段。"""
        return (uid(), project, None, "verified", encode(content["triggers"]), content["guidance"],
                encode({"candidate": candidate, "config_hash": fingerprint(config)}),
                now + 30 * 86400, now)

    def promote(self, candidate):
        """启用一个已通过配对验证但等待人工确认的候选。"""
        row = self.store.one("SELECT * FROM candidates WHERE id=?", (candidate,))
        if not row:
            raise ValueError("Candidate not found")
        if not self.store.one("SELECT id FROM projects WHERE id=?", (row["project"],)):
            raise ValueError("Project not found")
        config = self.runtime.config(row["project"])
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM candidates WHERE id=?", (candidate,)).fetchone()
            if not row:
                raise ValueError("Candidate not found")
            if row["status"] != "ready":
                raise ValueError("Only a verified ready candidate can be promoted")
            if not db.execute("SELECT id FROM projects WHERE id=?", (row["project"],)).fetchone():
                raise ValueError("Project not found")
            if row["kind"] not in ("strategy", "meta_strategy", "adapter"):
                raise ValueError("Unknown candidate kind")
            content = json.loads(row["content"] or "{}")
            if (not isinstance(content, dict) or not isinstance(content.get("guidance"), str)
                    or not content["guidance"].strip() or not isinstance(content.get("triggers"), list)):
                raise ValueError("Candidate content is incomplete")
            # 与 active_strategies 相同：旧证据无兼容指纹时仍可用，有指纹则必须匹配。
            sealed_evidence = json.loads(row["evidence"])
            if sealed_evidence.get("preregistered_plan"):
                verification = self.verify_plan(sealed_evidence["preregistered_plan"], sealed_evidence.get("trials", []))
                if not verification["valid"] or not sealed_evidence.get("final_review", {}).get("fresh"):
                    raise ValueError("Candidate lacks complete sealed trials and fresh final review")
            expected = sealed_evidence.get("compatibility")
            if expected and expected != compatibility(config):
                raise ValueError("Candidate evidence is incompatible with current configuration")
            # 与自动晋升一致：同项目同类型的旧稳定版让位，候选保留原有证据。
            self.activate(db, candidate, now)
            db.execute("INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?)",
                       self.skill_values(row["project"], candidate, content, config, now))
        self.store.event("promotion", "Ready candidate manually promoted; limited operational monitoring started",
                         row["project"], data={"candidate": candidate})
        return {"candidate": candidate, "status": "stable"}

    @staticmethod
    def split_decision(trials, config):
        if not trials or any("stable" not in t or "candidate" not in t for t in trials):
            return {"decision": "insufficient_evidence", "reason": "Incomplete paired trials"}
        if any(t["stable"]["pass"] and not t["candidate"]["pass"] for t in trials):
            return {"decision": "fail", "reason": "Candidate regressed on previously passing cases"}
        for split in {t["split"] for t in trials}:
            group = [t for t in trials if t["split"] == split]
            if not any(not t["stable"]["pass"] and t["candidate"]["pass"] for t in group):
                return {"decision": "insufficient_evidence", "reason": "No demonstrated improvement on each evaluated split"}
        return {"decision": "pass", "reason": "Paired quality improved without observed regressions; usage recorded separately"}

    @staticmethod
    def decide(trials, config):
        result = Evolution.split_decision(trials, config)
        if result["decision"] != "pass":
            return result
        if {t["split"] for t in trials} != {"validation", "holdout"}:
            return {"decision": "insufficient_evidence", "reason": "Both validation and holdout are required"}
        families = {}
        for i, trial in enumerate(trials):
            families.setdefault(trial.get("case", str(i)), []).append(trial)
        expected = set(range(config["evolution"]["repeats"]))
        complete = all(len(g) == len(expected) and {t.get("repetition") for t in g} == expected and
                       len({t["split"] for t in g}) == 1 for g in families.values())
        enough = all(sum(g[0]["split"] == split for g in families.values()) >= config["evolution"]["min_cases_per_split"]
                     for split in ["validation", "holdout"])
        wins = sum(all(not t["stable"]["pass"] and t["candidate"]["pass"] for t in group) for group in families.values())
        # Families, not repeated executions, are the independent units. With no losses,
        # a one-sided exact sign test still needs at least five consistent wins.
        probability = 0.5 ** wins
        repeated = config["evolution"]["repeats"] >= 2 and complete and enough
        if probability > 0.05 or not repeated:
            return {"decision": "insufficient_evidence", "reason": "Too few independent, repeated improvements for promotion",
                    "consistent_family_wins": wins, "sign_test_p": probability}
        stable_tokens = sum(trial["stable"].get("tokens", 0) for trial in trials)
        candidate_tokens = sum(trial["candidate"].get("tokens", 0) for trial in trials)
        stable_seconds = sum(trial["stable"].get("seconds", 0) for trial in trials)
        candidate_seconds = sum(trial["candidate"].get("seconds", 0) for trial in trials)
        cost_known = all(trial[variant].get("usage_known", trial[variant].get("tokens", 0) > 0)
                         for trial in trials for variant in ("stable", "candidate"))
        cost_ratio = candidate_tokens / stable_tokens if stable_tokens and cost_known else None
        latency_ratio = candidate_seconds / stable_seconds if stable_seconds else None
        if cost_ratio is not None and cost_ratio > config["evolution"]["max_cost_ratio"]:
            return {"decision": "insufficient_evidence", "reason": "Candidate exceeds the configured token cost ratio",
                    "cost_ratio": cost_ratio, "latency_ratio": latency_ratio}
        result = {**result, "cost_ratio": cost_ratio, "latency_ratio": latency_ratio,
                  "cost_comparable": cost_ratio is not None,
                  "cost_state": "known" if cost_ratio is not None else "zero_baseline" if cost_known else "unknown_usage",
                  "cost_warning": "" if cost_ratio is not None else "Token cost cannot be compared; quality evidence does not establish lower cost"}
        return {**result, "consistent_family_wins": wins, "sign_test_p": probability,
                "reason": "Repeated paired improvement on both splits without observed regressions"}

    def save(self, candidate, status, evidence):
        if not hasattr(self, "_plan_keys"):
            self._plan_keys = {}
        plan = evidence.get("preregistered_plan")
        plan_key = fingerprint(plan) if plan else None
        sentinel = object()
        plan_changed = (self._plan_keys.get(candidate, sentinel) != plan_key) or (bool(plan) and "plan_verification" not in evidence)
        is_terminal = (status != "evaluating")

        if is_terminal or plan_changed or "win_rate" not in evidence:
            if plan:
                evidence["plan_verification"] = self.verify_plan(plan, evidence.get("trials", []))
                if status == "evaluating" and not evidence["plan_verification"]["valid"]:
                    evidence["plan_verification"]["analysis"] = "planned_in_progress"
            evidence["win_rate"] = paired_summary(evidence.get("trials", []))
            self._plan_keys[candidate] = plan_key
        if is_terminal:
            self._plan_keys.pop(candidate, None)
        self.store.execute("UPDATE candidates SET status=?,evidence=? WHERE id=?", (status, encode(evidence), candidate))

    @staticmethod
    def overlay_digest(directory):
        if not directory:
            return None
        import hashlib
        import os
        import stat
        digest = hashlib.sha256()
        root = Path(directory)
        scrub_tree(root)
        def field(value):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        for path in [root] + sorted(root.rglob("*")):
            info = path.lstat()
            field(encode({"path": path.relative_to(root).as_posix(), "mode": stat.S_IMODE(info.st_mode),
                          "type": "link" if path.is_symlink() else "directory" if path.is_dir() else "file"}).encode())
            if path.is_symlink():
                field(os.readlink(path).encode("utf-8", "surrogateescape"))
            elif stat.S_ISREG(info.st_mode):
                field(str(info.st_size).encode())
                with path.open("rb") as source:
                    for block in iter(lambda: source.read(65536), b""):
                        digest.update(block)
            elif not path.is_dir():
                raise ValueError("Overlay must contain regular files and directories")
        return digest.hexdigest()

    def monitor(self, project, passed, run=None, attributable=False):
        config = self.runtime.config(project)
        if not run or not (passed or attributable):
            return
        recorded = self.store.one("SELECT strategy,config FROM runs WHERE id=? AND project=?", (run["id"], project))
        if not recorded:
            return
        active = {c["id"] for c in json.loads(recorded["strategy"] or "[]") if c.get("id")}
        if not passed and len(active) != 1:
            self.store.event("strategy_inconclusive", "Regression cannot be attributed to a single strategy; no automatic rollback",
                             project, run["id"], {"strategies": sorted(active)})
            return
        for row in self.store.query("SELECT * FROM candidates WHERE project=? AND status='stable'", (project,)):
            if row["id"] not in active:
                continue
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT evidence FROM candidates WHERE id=? AND status='stable'", (row["id"],)).fetchone()
                if not current:
                    continue
                evidence = json.loads(current["evidence"])
                if evidence.get("compatibility") and evidence["compatibility"] != compatibility(json.loads(recorded["config"])):
                    continue
                monitor = evidence.setdefault("monitor", {"passed": 0, "failed": 0})
                seen = monitor.setdefault("runs", [])
                if run["id"] in seen:
                    continue
                sentinel = monitor["passed"] >= config["evolution"]["monitor_tasks"]
                # Continue observing attributable failures indefinitely. Successful
                # post-window runs are sampled deterministically to bound history.
                if sentinel and passed and int(fingerprint(run["id"])[:8], 16) % 10:
                    continue
                seen.append(run["id"])
                del seen[:-512]
                monitor["phase"] = "sentinel" if sentinel else "initial"
                monitor["sampling"] = "all_attributable_failures_and_one_in_ten_successes" if sentinel else "all"
                monitor["passed" if passed else "failed"] += 1
                monitor["last_run"] = run["id"]
                monitor["last_checked"] = time.time()
                total = monitor["passed"] + monitor["failed"]
                if not passed or (total and monitor["passed"] / total < 0.8):
                    evidence["drift"] = {"state": "watch", "phase": monitor["phase"],
                                         "reason": "Attributable operational regression" if not passed else "Observed pass ratio below 0.8",
                                         "passed": monitor["passed"], "failed": monitor["failed"]}
                db.execute("UPDATE candidates SET evidence=? WHERE id=? AND status='stable'", (encode(evidence), row["id"]))
            if not passed:
                self.rollback(row["id"])

    def rollback(self, candidate):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM candidates WHERE id=?", (candidate,)).fetchone()
            if not row:
                raise ValueError("Candidate not found")
            if row["status"] == "rolled_back":
                # 重复回滚是幂等操作；监控线程与用户操作并发时不产生误导性错误事件。
                return
            if row["status"] != "stable":
                raise ValueError("Only an active strategy can be rolled back")
            db.execute("UPDATE candidates SET status='rolled_back' WHERE id=?", (candidate,))
            if row["parent"]:
                db.execute("UPDATE candidates SET status='stable' WHERE id=?", (row["parent"],))
            # Roll back learned instructions as well as the strategy pointer.
            for skill in db.execute("SELECT id,evidence FROM skills WHERE project=? AND status='verified'", (row["project"],)).fetchall():
                if json.loads(skill["evidence"]).get("candidate") == candidate:
                    db.execute("UPDATE skills SET status='invalidated' WHERE id=?", (skill["id"],))
        self.store.event("rollback", "Restored previous stable strategy and invalidated its learned skill", row["project"], data={"candidate": candidate})


reproduce_bundle = Evolution.reproduce_bundle
verify_bundle = Evolution.verify_bundle

