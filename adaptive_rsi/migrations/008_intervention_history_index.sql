-- 干预历史查询按 (project, task, seq) 定位；表达式索引让每次分页只读取该任务的事件。
-- json_valid 作为部分索引条件：非法 JSON 行照旧可写入，只是不进入索引。
CREATE INDEX IF NOT EXISTS events_intervention_task
    ON events(project, json_extract(data,'$.task'), seq DESC) WHERE json_valid(data);
