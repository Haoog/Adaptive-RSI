import json
import math
import shutil
import time
from pathlib import Path

from .config import compatibility, fingerprint, merge, validate
from .gitops import git, repository, snapshot
from .process import Cancelled, scrub_tree
from .store import encode, uid


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

    def record_feedback(self, candidate, label, source, run=None, note=""):
        if label not in ("false_positive", "confirmed_regression", "rejected") or not source or not source.startswith("human:"):
            raise ValueError("Feedback requires a supported label and human provenance")
        row = self.store.one("SELECT project FROM candidates WHERE id=?", (candidate,))
        if not row:
            raise ValueError("Candidate not found")
        self.store.event("monitor_feedback", "Optional human monitoring label", row["project"], run,
                         {"candidate": candidate, "label": label, "source": source, "note": note[:4000]})
        return self.feedback_summary(candidate)

    def feedback_summary(self, candidate):
        rows = [json.loads(row["data"]) for row in self.store.query("SELECT data FROM events WHERE kind='monitor_feedback'")]
        rows = [row for row in rows if row.get("candidate") == candidate]
        return {"samples": len(rows), "labels": {label: sum(row["label"] == label for row in rows)
                for label in ("false_positive", "confirmed_regression", "rejected")},
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

    def propose_and_evaluate(self, project, workspace, directory, run):
        config = json.loads(run["config"])
        limit = config["evolution"]["max_holdout_uses"]
        available = self.store.query("SELECT * FROM cases WHERE project=? AND split IN ('validation','holdout') "
                                     "ORDER BY created,id", (project["id"],))
        cases = []
        for case in available:
            spec = case_spec(case)
            if (case["uses"] < limit and spec is not None and
                    spec.get("available", True) and spec.get("active", True) and
                    isinstance(spec.get("task"), str) and
                    isinstance(spec.get("repository"), str) and isinstance(spec.get("base"), str)):
                cases.append(case)
        minimum = config["evolution"]["min_cases_per_split"]
        if any(sum(c["split"] == split for c in cases) < minimum for split in ["validation", "holdout"]):
            # 曝光耗尽的样本仍在案例表中；若只报“样本不足”，界面会同时显示样本数量，
            # 让人误以为导入没有生效。区分两种情况并给出可操作说明。
            exhausted = any(case["uses"] >= limit for case in available)
            reason = ("Validation or holdout exposure limit reached; import new cases or raise "
                      "evolution.max_holdout_uses") if exhausted else \
                     "Need independent validation/holdout cases before proposing"
            candidate = uid()
            self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) VALUES (?,?,?,?,?,?,?)",
                (candidate, project["id"], "strategy", "insufficient_evidence", encode({"guidance": "", "triggers": []}),
                 encode({"reason": reason}), time.time()))
            return {"reason": reason, "candidate": candidate, "status": "insufficient_evidence"}
        reserved = sorted(case["id"] for case in cases if case["split"] == "holdout")
        if self.store.one("SELECT id FROM candidates WHERE project=? AND status='reserved' AND created>?",
                          (project["id"], time.time() - 3600)):
            return {"reason": "Another proposal currently reserves the holdout cases", "status": "insufficient_evidence"}
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
        observations = self.store.query("SELECT content,evidence,status FROM skills WHERE project=? "
                                        "ORDER BY created DESC LIMIT 10", (project["id"],))
        failures = []
        for r in self.store.query("SELECT r.state,r.result FROM runs r JOIN tasks t ON r.task=t.id "
                                  "WHERE r.project=? AND r.state='failed' AND t.source NOT LIKE 'system_%' "
                                  "ORDER BY r.started DESC LIMIT 5", (project["id"],)):
            try:
                res = json.loads(r["result"]) if r["result"] else {}
            except (ValueError, TypeError):
                res = {}
            failures.append({"state": r["state"], "reason": res.get("reason", "")[:1000]})
        development = [json.loads(row["spec"])["task"] for row in self.store.query(
            "SELECT spec FROM cases WHERE project=? AND split='development' "
            "AND (NOT json_valid(spec) OR json_extract(spec,'$.active') IS NOT 0) "
            "ORDER BY created DESC, id LIMIT 10", (project["id"],))]
        current = self.runtime.active_strategies(project["id"])
        archive = self.store.query("SELECT id,kind,content,status FROM candidates WHERE project=? "
                                   "ORDER BY created DESC LIMIT 20", (project["id"],))
        # Reserve only for the paid call window: manifest or observation reads may
        # fail and must not leave a blocking reservation behind.
        self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) VALUES (?,?,?,?,?,?,?)",
            (reservation, project["id"], "strategy", "reserved", encode({"guidance": "", "triggers": []}),
             encode({"reservation": {"holdout": reserved}, "reason": "reserved"}), time.time()))
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
            raise
        self.store.execute("DELETE FROM candidates WHERE id=?", (reservation,))
        if not isinstance(proposal, dict) or "kind" not in proposal:
            raise RuntimeError("Model proposal must be an object with kind and content")
        candidate = uid()
        previous = self.store.one("SELECT id FROM candidates WHERE project=? AND kind=? AND status='stable' ORDER BY promoted DESC LIMIT 1",
                                  (project["id"], proposal["kind"]))
        parent = previous["id"] if previous else None
        self.store.execute("INSERT INTO candidates(id,project,kind,parent,status,content,created) VALUES (?,?,?,?,?,?,?)",
                           (candidate, project["id"], proposal["kind"], parent, "evaluating", encode(proposal), time.time()))
        evidence = {"run": run["id"], "config_hash": fingerprint(config), "compatibility": compatibility(config),
                    "dataset": {"version": 1, "manifest": sorted(manifest, key=lambda item: item["case"]),
                                "holdout_hash": fingerprint([item for item in manifest if item["split"] == "holdout"])},
                    "trials": [], "monitor": {"passed": 0, "failed": 0}}
        evidence["selection_attempts"] = self.store.one(
            "SELECT count(*) AS n FROM candidates WHERE project=? AND kind=? AND "
            "json_valid(evidence) AND json_extract(evidence,'$.compatibility')=?",
            (project["id"], proposal["kind"], evidence["compatibility"]))["n"] + 1
        selection = {split: select_cases([case for case in cases if case["split"] == split], minimum, run["id"])
                     for split in ("validation", "holdout")}
        plan = {"version": 1, "created": time.time(), "repeats": config["evolution"]["repeats"],
                "metric": "independent_case_family_paired_pass", "max_cost_ratio": config["evolution"]["max_cost_ratio"],
                "stop_rules": ["validation_failure", "cancelled", "resource_budget", "complete_planned_trials"],
                "samples": [{"id": case["id"], "split": split, "stratum": case_stratum(case), "prior_uses": case["uses"]}
                            for split, rows in selection.items() for case in rows],
                "proposal_hash": fingerprint(proposal), "baseline_hash": fingerprint(current),
                "configuration_hash": fingerprint(config),
                "proposal_inputs_hash": fingerprint({"observations": observations, "failures": failures,
                    "development_tasks": development, "current": current, "candidate_archive": archive})}
        evidence["preregistered_plan"] = {"data": plan, "sha256": fingerprint(plan)}
        evidence["promotion_review"] = {
            "baseline": current, "proposal": proposal, "experiment": candidate,
            "changed_kind": proposal["kind"], "plan_hash": fingerprint(plan),
            "covered_samples": plan["samples"],
            "limitations": ["Only sampled task families, sources and size strata are covered",
                            "Strategy and retrieved skills are jointly applied; individual causal effects are unproven"],
            "attribution": {"state": "insufficient_evidence", "reason": "Four matched ablation configurations were not executed"}}
        evidence["final_review"] = {"split": "holdout", "purpose": "final_confirmation_only",
                                    "proposal_hash": fingerprint(proposal),
                                    "cases": [case["id"] for case in selection["holdout"]],
                                    "fresh": all(case["uses"] == 0 for case in selection["holdout"])}
        self.save(candidate, "evaluating", evidence)
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
                            trial_dir = directory / "experiments" / (case["id"] + "-" + str(repetition)) / variant
                            trial_dir.mkdir(parents=True)
                            worker = snapshot(spec["repository"], trial_dir / "workspace", spec["base"])
                            before = self.store.one("SELECT coalesce(sum(tokens),0) AS n, count(*) AS calls, coalesce(sum(usage_known IS NOT 1),0) AS unknown FROM calls WHERE run=?", (run["id"],))
                            started = time.monotonic()
                            trial_config = merge(config, {"checks": spec["checks"]})
                            new_context = getattr(self.runtime, "new_trial_context", None)
                            trial_run = new_context(run) if new_context else {**run}
                            trial_run.update({"config": encode(trial_config),
                                              "_case_exposure": {"case": case["id"], "experiment": candidate, "limit": limit}})
                            trial_task = {"id": run["task"], "title": spec["task"].splitlines()[0], "body": spec["task"],
                                          "source": "evaluation", "evidence": "[]", "acceptance": "[]"}
                            outcome = self.runtime.execute_candidate(trial_run, {**project, "path": spec["repository"]},
                                trial_task, spec["repository"], spec["base"], trial_dir, worker, guide, experiment=True)
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
                            trial[variant] = {"pass": bool(outcome and outcome.get("decision") == "pass"),
                                "review": (outcome or {}).get("review", {}), "reason": (outcome or {}).get("reason", "No change"),
                                "seconds": time.monotonic() - started, "tokens": after["n"] - before["n"],
                                "usage_known": after["calls"] > before["calls"] and after["unknown"] == before["unknown"],
                                "report": str(trial_dir / "evaluation" / "report.json")}
                        evidence["trials"].append(trial)
                        self.save(candidate, "evaluating", evidence)
                if split == "validation":
                    validation = self.split_decision(evidence["trials"], config)
                    if validation["decision"] != "pass":
                        evidence.update(validation)
                        self.save(candidate, validation["decision"], evidence)
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
            state = evidence["decision"]
            if state == "pass" and config["evolution"]["auto_promote"]:
                with self.runtime.dispatch_lock:
                    if self.runtime.is_cancelled(run):
                        raise Cancelled("Evolution cancelled before promotion")
                    live = self.runtime.config(project["id"])
                    if not live["evolution"]["auto_promote"] or compatibility(live) != evidence["compatibility"]:
                        evidence["reason"] = "Configuration changed; candidate retained for explicit revalidation"
                        self.save(candidate, "ready", evidence)
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
            else:
                self.save(candidate, "ready" if state == "pass" else state, evidence)
            return {"reason": evidence["reason"], "candidate": candidate, "status": state}
        except Exception as exc:
            evidence.update(decision="insufficient_evidence", reason=str(exc))
            self.save(candidate, "interrupted" if isinstance(exc, Cancelled) else "insufficient_evidence", evidence)
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
        if evidence.get("preregistered_plan"):
            evidence["plan_verification"] = self.verify_plan(evidence["preregistered_plan"], evidence.get("trials", []))
            if status == "evaluating" and not evidence["plan_verification"]["valid"]:
                evidence["plan_verification"]["analysis"] = "planned_in_progress"
        evidence["win_rate"] = paired_summary(evidence.get("trials", []))
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
