-- 调度循环每 tick 用 `runs WHERE ended IS NULL`（scheduler_projects / recover /
-- _add_project / updates.work_busy 等）查活跃 run；全表扫描成本随 run 数线性增长。
-- 部分索引只覆盖未结束行，绝大多数历史 run 不进索引。
CREATE INDEX IF NOT EXISTS open_runs ON runs(project) WHERE ended IS NULL;
