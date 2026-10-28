use super::*;

#[test]
fn project_import_preserves_the_runtime_clone_deadline() {
    assert_eq!(transport::request_timeout("projects/import"), Duration::from_secs(210));
    assert_eq!(transport::request_timeout("projects/import?fixture=1"), Duration::from_secs(210));
    assert_eq!(transport::request_timeout("model-center/probe"), Duration::from_secs(960));
    assert_eq!(transport::request_timeout("state"), Duration::from_secs(60));
}

#[test]
fn updater_feed_wait_can_be_cancelled_before_transport_deadline() {
    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    runtime.block_on(async {
        let cancel = AtomicBool::new(false);
        let operation = async {
            tokio::time::sleep(Duration::from_millis(20)).await;
            cancel.store(true, Ordering::SeqCst);
            std::future::pending::<Result<(), std::io::Error>>().await
        };
        let started = std::time::Instant::now();
        assert_eq!(native_update::cancellable(operation, &cancel, "update_check_failed").await.unwrap_err(), "update_cancelled");
        assert!(started.elapsed() < Duration::from_secs(1));
    });
}

#[test]
fn install_confirmation_is_bound_to_the_exact_feed_artifact() {
    let approved = json!({"version":"1.2.3","url":"https://example.test/a","signature":"one"});
    assert!(native_update::require_same_artifact(&approved, Some(&approved)).is_ok());
    assert_eq!(native_update::require_same_artifact(&approved, None).unwrap_err(), "update_target_changed");
    for field in ["version", "url", "signature"] {
        let mut changed = approved.clone();
        changed[field] = json!("different");
        assert_eq!(native_update::require_same_artifact(&approved, Some(&changed)).unwrap_err(), "update_target_changed");
    }
}

#[test]
fn space_check_includes_cache_application_and_extraction_volumes() {
    let mut checked = Vec::new();
    let paths = [std::path::Path::new("/cache"), std::path::Path::new("/app"), std::path::Path::new("/extract")];
    assert_eq!(native_update::require_space(&paths, 100, |path| {
        checked.push(path.to_path_buf());
        Ok(if path == paths[2] { 99 } else { 100 })
    }).unwrap_err(), "update_disk_space");
    assert_eq!(checked, paths);
    assert!(native_update::require_space(&paths, 100, |_| Ok(100)).is_ok());
    assert_eq!(native_update::require_space(&paths, 100, |_| Err(std::io::Error::other("unavailable"))).unwrap_err(), "update_disk_unavailable");
}

#[cfg(unix)]
#[test]
fn extraction_volume_matches_the_updaters_same_device_selection() {
    let folder = tempfile::tempdir().unwrap();
    let executable = folder.path().join("Fixture.app/Contents/MacOS/fixture");
    std::fs::create_dir_all(executable.parent().unwrap()).unwrap();
    std::fs::write(&executable, b"fixture").unwrap();
    assert_eq!(native_update::extraction_root(&executable, folder.path()).unwrap(), folder.path());
    assert_eq!(native_update::extraction_root(&executable, &folder.path().join("missing")).unwrap(), folder.path());
}

#[test]
fn announced_package_size_bounds_preflight_and_falls_back_when_unknown() {
    assert_eq!(native_update::announced_size(&serde_json::json!({"size": 1024})), 1024);
    assert_eq!(native_update::announced_size(&serde_json::json!({"size": 0})), native_update::MAX_PACKAGE as u64);
    assert_eq!(native_update::announced_size(&serde_json::json!({})), native_update::MAX_PACKAGE as u64);
}