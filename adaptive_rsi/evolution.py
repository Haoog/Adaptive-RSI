import json
import shutil
import time
from pathlib import Path

from .config import compatibility, fingerprint, merge, validate
from .gitops import git, repository, snapshot
from .process import Cancelled, scrub_tree
from .store import encode, uid


class Evolution:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store

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
            self.store.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
                               (case, project, manifest["family"], manifest["split"], encode(spec), 0, time.time()))
        except Exception:
            shutil.rmtree(directory)
            raise
        self.store.event("case", "Protected evaluation case imported", project,
                         data={"case": case, "split": manifest["split"], "source": manifest["source"]})
        return case

    def propose_and_evaluate(self, project, workspace, directory, run):
        config = json.loads(run["config"])
        cases = self.store.query("SELECT * FROM cases WHERE project=? AND split IN ('validation','holdout') "
                                 "AND uses<? ORDER BY created,id", (project["id"], config["evolution"]["max_holdout_uses"]))
        minimum = config["evolution"]["min_cases_per_split"]
        if any(sum(c["split"] == split for c in cases) < minimum for split in ["validation", "holdout"]):
            reason = "Need independent validation/holdout cases before proposing"
            candidate = uid()
            self.store.execute("INSERT INTO candidates(id,project,kind,status,content,evidence,created) VALUES (?,?,?,?,?,?,?)",
                (candidate, project["id"], "strategy", "insufficient_evidence", encode({"guidance": "", "triggers": []}),
                 encode({"reason": reason}), time.time()))
            return {"reason": reason, "candidate": candidate, "status": "insufficient_evidence"}
        observations = self.store.query("SELECT content,evidence,status FROM skills WHERE project=? "
                                        "ORDER BY created DESC LIMIT 10", (project["id"],))
        failures = self.store.query("SELECT r.state,r.result FROM runs r JOIN tasks t ON r.task=t.id "
                                    "WHERE r.project=? AND r.state='failed' AND t.source NOT LIKE 'system_%' "
                                    "ORDER BY r.started DESC LIMIT 5", (project["id"],))
        failures = [{"state": r["state"], "reason": json.loads(r["result"]).get("reason", "")[:1000]} for r in failures]
        development = [json.loads(row["spec"])["task"] for row in self.store.query(
            "SELECT spec FROM cases WHERE project=? AND split='development' LIMIT 10", (project["id"],))]
        current = self.runtime.active_strategies(project["id"])
        archive = self.store.query("SELECT id,kind,content,status FROM candidates WHERE project=? "
                                   "ORDER BY created DESC LIMIT 20", (project["id"],))
        proposal = self.runtime.invoke("evolution", "Propose one small, testable improvement to agent execution, "
                                       "opportunity discovery/meta-planning, or toolchain/context adapter guidance. "
                                       "Do not change evaluator, budgets, permissions, or promotion rules. "
                                       "Guidance is a retrieved instruction, never executable controller code. "
                                       "Use only observations and development examples; held-out tasks are unavailable.\n" +
                                       encode({"observations": observations, "failures": failures,
                                               "development_tasks": development, "current": current,
                                               "candidate_archive": archive}), workspace, run)
        candidate = uid()
        previous = self.store.one("SELECT id FROM candidates WHERE project=? AND kind=? AND status='stable' ORDER BY promoted DESC LIMIT 1",
                                  (project["id"], proposal["kind"]))
        parent = previous["id"] if previous else None
        self.store.execute("INSERT INTO candidates(id,project,kind,parent,status,content,created) VALUES (?,?,?,?,?,?,?)",
                           (candidate, project["id"], proposal["kind"], parent, "evaluating", encode(proposal), time.time()))
        evidence = {"run": run["id"], "config_hash": fingerprint(config), "compatibility": compatibility(config),
                    "trials": [], "monitor": {"passed": 0, "failed": 0}}
        try:
            for split in ["validation", "holdout"]:
                selected = [c for c in cases if c["split"] == split][:minimum]
                for case in selected:
                    # Exposure is charged just before first use; untouched holdouts stay protected.
                    self.store.execute("UPDATE cases SET uses=uses+1 WHERE id=?", (case["id"],))
                    for repetition in range(config["evolution"]["repeats"]):
                        spec = json.loads(case["spec"])
                        trial = {"case": case["id"], "split": split, "source": spec["source"], "repetition": repetition}
                        order = ["stable", "candidate"] if len(evidence["trials"]) % 2 == 0 else ["candidate", "stable"]
                        trial["order"] = order
                        for variant in order:
                            if self.runtime.is_cancelled(run):
                                raise Cancelled("Evolution cancelled")
                            guide = current if variant == "stable" else [c for c in current if c["kind"] != proposal["kind"]] + [{"kind": proposal["kind"], "content": proposal}]
                            trial_dir = directory / "experiments" / (case["id"] + "-" + str(repetition)) / variant
                            trial_dir.mkdir(parents=True)
                            worker = snapshot(spec["repository"], trial_dir / "workspace", spec["base"])
                            before = self.store.one("SELECT coalesce(sum(tokens),0) AS n FROM calls WHERE run=?", (run["id"],))["n"]
                            started = time.monotonic()
                            trial_config = merge(config, {"checks": spec["checks"]})
                            trial_run = {**run, "config": encode(trial_config)}
                            trial_task = {"id": run["task"], "title": spec["task"].splitlines()[0], "body": spec["task"],
                                          "source": "evaluation", "evidence": "[]", "acceptance": "[]"}
                            outcome = self.runtime.execute_candidate(trial_run, {**project, "path": spec["repository"]},
                                trial_task, spec["repository"], spec["base"], trial_dir, worker, guide, experiment=True)
                            # A case overlay is host-owned and remains outside model-visible workspaces.
                            if spec.get("overlay") and (trial_dir / "candidate").exists():
                                overlay_result = self.runtime.evaluator.evaluate(
                                    trial_dir / "baseline", trial_dir / "candidate", spec["checks"],
                                    trial_dir / "protected-evaluation", trial_config,
                                    lambda: self.runtime.is_cancelled(run), overlay=spec["overlay"])
                                if overlay_result["decision"] != "pass":
                                    outcome = {**(outcome or {}), "decision": overlay_result["decision"]}
                            after = self.store.one("SELECT coalesce(sum(tokens),0) AS n FROM calls WHERE run=?", (run["id"],))["n"]
                            trial[variant] = {"pass": bool(outcome and outcome.get("decision") == "pass"),
                                "review": (outcome or {}).get("review", {}), "reason": (outcome or {}).get("reason", "No change"),
                                "seconds": time.monotonic() - started, "tokens": after - before,
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
            state = evidence["decision"]
            if state == "pass" and config["evolution"]["auto_promote"]:
                if self.runtime.is_cancelled(run):
                    raise Cancelled("Evolution cancelled before promotion")
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("UPDATE candidates SET status='superseded' WHERE project=? AND kind=? AND status='stable'",
                               (project["id"], proposal["kind"]))
                    db.execute("UPDATE candidates SET status='stable',promoted=?,evidence=? WHERE id=?",
                               (time.time(), encode(evidence), candidate))
                self.store.execute("INSERT INTO skills VALUES (?,?,?,?,?,?,?,?,?)", (
                    uid(), project["id"], None, "verified", encode(proposal["triggers"]), proposal["guidance"],
                    encode({"candidate": candidate, "config_hash": fingerprint(config)}), time.time() + 30 * 86400, time.time()))
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
        return {**result, "consistent_family_wins": wins, "sign_test_p": probability,
                "reason": "Repeated paired improvement on both splits without observed regressions"}

    def save(self, candidate, status, evidence):
        self.store.execute("UPDATE candidates SET status=?,evidence=? WHERE id=?", (status, encode(evidence), candidate))

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
            evidence = json.loads(row["evidence"])
            if evidence.get("compatibility") and evidence["compatibility"] != compatibility(json.loads(recorded["config"])):
                continue
            monitor = evidence.setdefault("monitor", {"passed": 0, "failed": 0})
            seen = monitor.setdefault("runs", [])
            if run["id"] in seen:
                continue
            if monitor["passed"] >= config["evolution"]["monitor_tasks"]:
                continue
            seen.append(run["id"])
            monitor["passed" if passed else "failed"] += 1
            self.save(row["id"], "stable", evidence)
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
