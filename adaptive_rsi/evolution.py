import json
import shutil
import time
from pathlib import Path

from .config import compatibility, fingerprint, merge, validate
from .gitops import changes, git, repository, snapshot
from .process import Cancelled, scrub_tree
from .review import REVIEW_PROMPT, enforce_review
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
        # Evolution uses its own per-experiment budget, still bounded by the project's daily budget.
        config = merge(config, {"limits": {"run_calls": config["evolution"]["max_calls"]}})
        run = {**run, "config": encode(config)}
        self.store.execute("UPDATE runs SET config=? WHERE id=?", (run["config"], run["id"]))
        observations = self.store.query("SELECT content,evidence,status FROM skills WHERE project=? "
                                        "ORDER BY created DESC LIMIT 20", (project["id"],))
        failures = self.store.query("SELECT state,result FROM runs WHERE project=? AND state='failed' "
                                    "ORDER BY started DESC LIMIT 10", (project["id"],))
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
            cases = self.store.query("SELECT * FROM cases WHERE project=? AND split IN ('validation','holdout') "
                                     "AND uses<? ORDER BY created", (project["id"], config["evolution"]["max_holdout_uses"]))
            minimum = config["evolution"]["min_cases_per_split"]
            if any(sum(c["split"] == split for c in cases) < minimum for split in ["validation", "holdout"]):
                evidence.update(decision="insufficient_evidence", reason="Need independent validation and protected holdout cases; stable strategy unchanged")
                self.save(candidate, "insufficient_evidence", evidence)
                return {"reason": evidence["reason"], "candidate": candidate}
            selected = []
            for split in ["validation", "holdout"]:
                selected += [c for c in cases if c["split"] == split][:minimum]
            # Consume protected tasks on exposure, including interrupted/failed experiments.
            for case in selected:
                self.store.execute("UPDATE cases SET uses=uses+1 WHERE id=?", (case["id"],))
            for repetition in range(config["evolution"]["repeats"]):
                for case in selected:
                    spec = json.loads(case["spec"])
                    trial = {"case": case["id"], "split": case["split"], "source": spec["source"], "repetition": repetition}
                    # Alternate order to reduce cache/time bias between variants.
                    order = ["stable", "candidate"] if (len(evidence["trials"]) + repetition) % 2 == 0 else ["candidate", "stable"]
                    for variant in order:
                        if self.runtime.is_cancelled(run):
                            raise Cancelled("Evolution cancelled")
                        guide = current if variant == "stable" else [c for c in current if c["kind"] != proposal["kind"]] + [{"kind": proposal["kind"], "content": proposal}]
                        trial_dir = directory / "experiments" / (case["id"] + "-" + str(repetition)) / variant
                        trial_dir.mkdir(parents=True)
                        worker = snapshot(spec["repository"], trial_dir / "workspace", spec["base"])
                        before = self.store.one("SELECT coalesce(sum(tokens),0) AS n FROM calls WHERE run=?", (run["id"],))["n"]
                        started = time.time()
                        plan = None
                        if proposal["kind"] == "meta_strategy":
                            plan = self.runtime.invoke("planning", "Verify and plan this evaluation task using the supplied strategy.\n" +
                                                       encode({"task": spec["task"], "strategy": guide}), worker, run)
                        self.runtime.invoke("implementation", "Solve this evaluation task. Do not access holdout tests.\n" +
                                            encode({"task": spec["task"], "strategy": guide, "plan": plan}), worker, run)
                        _, patch = changes(worker)
                        sealed = snapshot(spec["repository"], trial_dir / "candidate", spec["base"])
                        if patch:
                            patch_file = trial_dir / "candidate.patch"
                            patch_file.write_text(patch + "\n", encoding="utf-8")
                            git(sealed, "apply", "--index", str(patch_file))
                        baseline = snapshot(spec["repository"], trial_dir / "baseline", spec["base"])
                        result = self.runtime.evaluator.evaluate(baseline, sealed, spec["checks"], trial_dir / "evaluation", config,
                                                                 lambda: self.runtime.is_cancelled(run), spec.get("overlay"))
                        # Both variants pay for the same review gate, including failing baselines.
                        review = enforce_review(self.runtime.invoke("review", REVIEW_PROMPT +
                            encode({"task": spec["task"], "plan": plan,
                                    "check_decision": result["decision"]}), sealed, run), config)
                        (trial_dir / "review.json").write_text(encode(review), encoding="utf-8")
                        after = self.store.one("SELECT coalesce(sum(tokens),0) AS n FROM calls WHERE run=?", (run["id"],))["n"]
                        trial[variant] = {"pass": result["decision"] == "pass" and review["decision"] == "pass",
                                          "review": review, "seconds": time.time() - started,
                                          "tokens": after - before, "report": str(trial_dir / "evaluation" / "report.json")}
                    evidence["trials"].append(trial)
                    # Persist detailed evidence in protected state, never in proposal prompts.
                    self.save(candidate, "evaluating", evidence)
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
    def decide(trials, config):
        if not trials or any("stable" not in t or "candidate" not in t for t in trials):
            return {"decision": "insufficient_evidence", "reason": "Incomplete paired trials"}
        regressions = any(t["stable"]["pass"] and not t["candidate"]["pass"] for t in trials)
        improvements = all(any(not t["stable"]["pass"] and t["candidate"]["pass"] for t in trials if t["split"] == split)
                           for split in ["validation", "holdout"])
        stable_tokens = sum(t["stable"]["tokens"] for t in trials)
        candidate_tokens = sum(t["candidate"]["tokens"] for t in trials)
        stable_time = sum(t["stable"]["seconds"] for t in trials)
        candidate_time = sum(t["candidate"]["seconds"] for t in trials)
        cost_known = stable_tokens > 0 and candidate_tokens > 0
        affordable = cost_known and candidate_tokens <= stable_tokens * config["evolution"]["max_cost_ratio"] and \
            candidate_time <= stable_time * config["evolution"]["max_cost_ratio"]
        if regressions:
            return {"decision": "fail", "reason": "Candidate regressed on previously passing cases"}
        if not improvements or not cost_known:
            return {"decision": "insufficient_evidence", "reason": "No demonstrated improvement on both splits, or usage data unavailable"}
        if not affordable:
            return {"decision": "fail", "reason": "Quality gain exceeded time/token cost allowance"}
        return {"decision": "pass", "reason": "Paired quality improved on both splits without observed regressions and within cost limits"}

    def save(self, candidate, status, evidence):
        self.store.execute("UPDATE candidates SET status=?,evidence=? WHERE id=?", (status, encode(evidence), candidate))

    def monitor(self, project, passed):
        config = self.runtime.config(project)
        active = {c["id"] for c in self.runtime.active_strategies(project)}
        for row in self.store.query("SELECT * FROM candidates WHERE project=? AND status='stable'", (project,)):
            if row["id"] not in active:
                continue
            evidence = json.loads(row["evidence"])
            monitor = evidence.setdefault("monitor", {"passed": 0, "failed": 0})
            if monitor["passed"] >= config["evolution"]["monitor_tasks"]:
                continue
            monitor["passed" if passed else "failed"] += 1
            self.save(row["id"], "stable", evidence)
            if not passed:
                self.rollback(row["id"])

    def rollback(self, candidate):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM candidates WHERE id=?", (candidate,)).fetchone()
            if not row or row["status"] != "stable":
                raise ValueError("Only an active strategy can be rolled back")
            db.execute("UPDATE candidates SET status='rolled_back' WHERE id=?", (candidate,))
            if row["parent"]:
                db.execute("UPDATE candidates SET status='stable' WHERE id=?", (row["parent"],))
            # Roll back learned instructions as well as the strategy pointer.
            for skill in db.execute("SELECT id,evidence FROM skills WHERE project=? AND status='verified'", (row["project"],)).fetchall():
                if json.loads(skill["evidence"]).get("candidate") == candidate:
                    db.execute("UPDATE skills SET status='invalidated' WHERE id=?", (skill["id"],))
        self.store.event("rollback", "Restored previous stable strategy and invalidated its learned skill", row["project"], data={"candidate": candidate})
