use std::sync::{atomic::{AtomicU64, Ordering}, Mutex};

// Both the generation and the update are observed or changed while holding the
// update mutex. A check may not publish over a download that started afterwards.
pub(crate) fn inspect<T, R>(state: &Mutex<Option<T>>, generation: &AtomicU64,
                            preserve: impl FnOnce(&T) -> Option<R>) -> (Option<R>, u64) {
    let current = state.lock().unwrap();
    (current.as_ref().and_then(preserve), generation.load(Ordering::Relaxed))
}

pub(crate) fn publish<T, R>(state: &Mutex<Option<T>>, generation: &AtomicU64, observed: u64,
                            result: Result<Option<T>, R>,
                            preserve: impl FnOnce(&T) -> bool,
                            render: impl Fn(&T) -> R) -> Option<R> {
    let mut current = state.lock().unwrap();
    if generation.load(Ordering::Relaxed) != observed
        || current.as_ref().is_some_and(preserve) {
        return current.as_ref().map(render);
    }
    match result {
        Ok(update) => { *current = update; current.as_ref().map(render) }
        Err(error) => Some(error),
    }
}

pub(crate) fn started(generation: &AtomicU64) {
    // Called with the update mutex held, after the download gate is acquired.
    generation.fetch_add(1, Ordering::Relaxed);
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, mpsc};
    use std::thread;

    #[derive(Clone, Debug, PartialEq, Eq)]
    struct Update { stage: &'static str, progress: u32 }

    #[test]
    fn delayed_checks_preserve_concurrent_download_through_completion() {
        for stage in ["downloading", "verifying", "installing", "success", "failure"] {
            for response in [Ok(Some(Update { stage: "new", progress: 0 })), Ok(None), Err("check failed")] {
                let state = Arc::new(Mutex::new(Some(Update { stage: "available", progress: 0 })));
                let generation = Arc::new(AtomicU64::new(0));
                let (ready_tx, ready_rx) = mpsc::channel();
                let (resume_tx, resume_rx) = mpsc::channel();
                let (s, g) = (state.clone(), generation.clone());
                let worker = thread::spawn(move || {
                    let (_, observed) = inspect(&s, &g, |_| None::<String>);
                    ready_tx.send(()).unwrap();
                    resume_rx.recv().unwrap();
                    publish(&s, &g, observed, response.map_err(str::to_string), |_| false,
                            |u| format!("{}:{}", u.stage, u.progress))
                });
                ready_rx.recv().unwrap();
                {
                    let mut current = state.lock().unwrap();
                    started(&generation);
                    *current = Some(Update { stage, progress: 46 });
                }
                resume_tx.send(()).unwrap();
                assert_eq!(worker.join().unwrap(), Some(format!("{stage}:46")));
                assert_eq!(*state.lock().unwrap(), Some(Update { stage, progress: 46 }));
            }
        }
    }

    #[test]
    fn failed_download_retry_blocks_old_checks_and_allows_a_later_check() {
        let state = Mutex::new(Some(Update { stage: "available", progress: 0 }));
        let generation = AtomicU64::new(0);
        let (_, before_first) = inspect(&state, &generation, |_| None::<String>);
        {
            let mut current = state.lock().unwrap();
            started(&generation);
            *current = Some(Update { stage: "failure", progress: 46 });
        }
        let (_, before_retry) = inspect(&state, &generation, |_| None::<String>);
        {
            let mut current = state.lock().unwrap();
            started(&generation);
            *current = Some(Update { stage: "downloading", progress: 12 });
        }
        for observed in [before_first, before_retry] {
            assert_eq!(publish(&state, &generation, observed, Err("check failed".to_string()),
                               |_| false, |u| format!("{}:{}", u.stage, u.progress)),
                       Some("downloading:12".into()));
        }
        {
            let mut current = state.lock().unwrap();
            *current = Some(Update { stage: "failure", progress: 12 });
        }
        let (_, after_retry) = inspect(&state, &generation, |_| None::<String>);
        assert_eq!(publish(&state, &generation, after_retry, Ok(None), |_| false,
                           |u| u.stage.to_string()), None);
    }

    #[test]
    fn sequential_downloads_and_new_process_allow_fresh_checks() {
        let state = Mutex::new(Some(Update { stage: "failure", progress: 46 }));
        let generation = AtomicU64::new(0);
        for _ in 0..2 {
            let (_, observed) = inspect(&state, &generation, |_| None::<String>);
            { let _guard = state.lock().unwrap(); started(&generation); }
            assert_eq!(publish(&state, &generation, observed, Ok(None), |_| false,
                               |u| u.stage.to_string()), Some("failure".into()));
        }
        let (_, observed) = inspect(&state, &generation, |_| None::<String>);
        assert_eq!(publish(&state, &generation, observed, Ok(None), |_| false,
                           |u| u.stage.to_string()), None);
        let restarted = AtomicU64::new(0);
        let (_, observed) = inspect(&state, &restarted, |_| None::<String>);
        assert_eq!(publish(&state, &restarted, observed, Ok(Some(Update { stage: "new", progress: 0 })),
                           |_| false, |u| u.stage.to_string()), Some("new".into()));
    }
}
