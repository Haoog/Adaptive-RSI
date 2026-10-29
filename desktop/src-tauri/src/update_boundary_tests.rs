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

#[test]
fn resumed_segment_requests_only_the_missing_bytes() {
    assert_eq!(native_update::resume_offset(4096, Some(8192)), 4096);
    assert_eq!(native_update::resume_offset(0, None), 0);
    assert_eq!(native_update::resume_offset(8192, Some(8192)), 8192);
    assert_eq!(native_update::resume_offset(900, Some(800)), 0);
    assert_eq!(native_update::resume_offset(native_update::MAX_PACKAGE as u64 + 1, None), 0);
    assert_eq!(native_update::range_header(0), None);
    assert_eq!(native_update::range_header(4096).as_deref(), Some("bytes=4096-"));
}

#[cfg(unix)]
#[test]
fn identity_file_never_writes_through_links() {
    let folder = tempfile::tempdir().unwrap();
    let target = folder.path().join("outside");
    let meta = native_update::part_meta_path(folder.path());
    std::fs::write(&target, b"outside").unwrap();
    std::os::unix::fs::symlink(&target, &meta).unwrap();
    native_update::write_part_identity(&meta, "identity").unwrap();
    assert_eq!(std::fs::read(&target).unwrap(), b"outside");
    assert_eq!(std::fs::read_to_string(&meta).unwrap(), "identity");
    assert!(std::fs::symlink_metadata(&meta).unwrap().file_type().is_file());

    std::fs::remove_file(&meta).unwrap();
    std::fs::hard_link(&target, &meta).unwrap();
    native_update::write_part_identity(&meta, "second").unwrap();
    assert_eq!(std::fs::read(&target).unwrap(), b"outside");
    assert_eq!(std::fs::read_to_string(&meta).unwrap(), "second");
}

#[test]
fn segment_identity_is_bound_to_version_and_url() {
    let folder = tempfile::tempdir().unwrap();
    let meta = native_update::part_meta_path(folder.path());
    assert_eq!(meta.file_name().unwrap(), "staged-update.part.json");
    let current = native_update::part_identity("1.2.3", "https://example.test/a");
    assert!(native_update::identity_matches(Some(&current), "1.2.3", "https://example.test/a"));
    assert!(!native_update::identity_matches(Some(&current), "1.2.4", "https://example.test/a"));
    assert!(!native_update::identity_matches(Some(&current), "1.2.3", "https://example.test/b"));
    assert!(!native_update::identity_matches(None, "1.2.3", "https://example.test/a"));
}

#[test]
fn segment_decision_follows_status_and_content_range() {
    use native_update::{parse_content_range, segment_action, ContentRange, SegmentAction};
    let resumed = parse_content_range("bytes 4096-8191/8192").unwrap();
    assert_eq!(resumed, ContentRange { start: 4096, total: Some(8192) });
    assert_eq!(parse_content_range("bytes 0-99/*").unwrap().total, None);
    assert_eq!(parse_content_range("attachment"), None);
    assert_eq!(segment_action(4096, 206, Some(&resumed)), SegmentAction::Append);
    // A 206 that answers a different start must not be appended.
    assert_eq!(segment_action(4096, 206, Some(&ContentRange { start: 0, total: Some(8192) })),
               SegmentAction::Restart);
    // The server ignored Range: truncate and download from zero.
    assert_eq!(segment_action(4096, 200, None), SegmentAction::Restart);
    assert_eq!(segment_action(0, 200, None), SegmentAction::Restart);
    // An unsatisfiable range and any other status invalidate the segment.
    assert_eq!(segment_action(4096, 416, None), SegmentAction::Rejected);
    assert_eq!(segment_action(0, 500, None), SegmentAction::Rejected);
}

#[test]
fn untrusted_segments_are_discarded_and_never_resumed() {
    let folder = tempfile::tempdir().unwrap();
    let part = native_update::part_path(folder.path());
    assert_eq!(part.file_name().unwrap(), "staged-update.part");
    assert_eq!(native_update::cached_path(folder.path()).file_name().unwrap(), "staged-update.bin");
    std::fs::write(&part, b"partial").unwrap();
    assert_eq!(native_update::part_bytes(&part), 7);
    // Resuming appends after the persisted bytes instead of overwriting them.
    {
        use std::io::Write;
        let mut file = native_update::open_part(&part, 7).unwrap();
        file.write_all(b"more").unwrap();
        file.sync_all().unwrap();
    }
    assert_eq!(std::fs::read(&part).unwrap(), b"partialmore");
    assert!(native_update::discards_segment("update_download_or_signature_failed"));
    assert!(native_update::discards_segment("update_range_rejected"));
    assert!(native_update::discards_segment("update_size_limit"));
    assert!(!native_update::discards_segment("update_cancelled"));
    native_update::discard_part(&part);
    assert!(!part.exists());
    // A non-file entry is never treated as resumable state.
    std::fs::create_dir(&part).unwrap();
    assert_eq!(native_update::part_bytes(&part), 0);
    assert!(!part.exists());
    #[cfg(unix)] {
        std::fs::write(folder.path().join("target"), b"outside").unwrap();
        std::os::unix::fs::symlink(folder.path().join("target"), &part).unwrap();
        assert_eq!(native_update::part_bytes(&part), 0);
        assert_eq!(std::fs::read(folder.path().join("target")).unwrap(), b"outside");
        assert!(!part.exists());
        std::fs::hard_link(folder.path().join("target"), &part).unwrap();
        assert_eq!(native_update::part_bytes(&part), 0);
        assert_eq!(std::fs::read(folder.path().join("target")).unwrap(), b"outside");
        assert!(!part.exists());
        // Direct open must not follow or append through either link type.
        std::os::unix::fs::symlink(folder.path().join("target"), &part).unwrap();
        {
            use std::io::Write;
            let mut file = native_update::open_part(&part, 0).unwrap();
            file.write_all(b"new").unwrap();
        }
        assert_eq!(std::fs::read(folder.path().join("target")).unwrap(), b"outside");
        assert_eq!(std::fs::read(&part).unwrap(), b"new");
        native_update::discard_part(&part);
        std::fs::hard_link(folder.path().join("target"), &part).unwrap();
        {
            use std::io::Write;
            let mut file = native_update::open_part(&part, 0).unwrap();
            file.write_all(b"new").unwrap();
        }
        assert_eq!(std::fs::read(folder.path().join("target")).unwrap(), b"outside");
        native_update::discard_part(&part);
    }
    // A stale empty segment must not make exclusive creation fail forever.
    std::fs::write(&part, b"").unwrap();
    {
        use std::io::Write;
        let mut file = native_update::open_part(&part, 0).unwrap();
        file.write_all(b"fresh").unwrap();
    }
    assert_eq!(std::fs::read(&part).unwrap(), b"fresh");
    native_update::discard_part(&part);
    // A segment whose length changed under the recorded offset is not appended.
    std::fs::write(&part, b"123").unwrap();
    assert_eq!(native_update::open_part(&part, 4).unwrap_err(), "update_range_rejected");
    assert!(!part.exists());
}

#[test]
fn asset_target_is_preflighted_before_download() {
    use native_update::{detect_target, require_compatible_asset, Arch, Platform};
    assert_eq!(detect_target("desktop_1.2.3_amd64_linux"),
               (Some(Platform::Linux), Some(Arch::X86_64)));
    assert_eq!(detect_target("desktop-1.2.3-aarch64-apple-darwin.tar.gz"),
               (Some(Platform::Macos), Some(Arch::Aarch64)));
    let mac_arm = (Some(Platform::Macos), Some(Arch::Aarch64));
    assert!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/desktop-1.2.3-aarch64-apple-darwin.tar.gz",
        None, mac_arm).is_ok());
    assert_eq!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/desktop-1.2.3-x86_64-pc-windows-msvc.zip",
        None, mac_arm).unwrap_err(), "update_incompatible_platform");
    assert_eq!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/desktop-1.2.3-arm64-apple-darwin.zip",
        None, (Some(Platform::Macos), Some(Arch::X86_64))).unwrap_err(), "update_incompatible_platform");
    // The asset name can carry the marker when the URL does not.
    assert_eq!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/package.zip",
        Some("desktop-1.2.3-x86_64-unknown-linux-gnu.zip"),
        mac_arm).unwrap_err(), "update_incompatible_platform");
    // A universal/multi-architecture label is ambiguous, so it is not rejected.
    assert_eq!(detect_target("desktop-1.2.3-macos-arm64-x86_64.zip"),
               (Some(Platform::Macos), None));
    assert!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/desktop-1.2.3-macos-arm64-x86_64.zip",
        None, mac_arm).is_ok());
    // Unlabelled assets and an unrecognised host stay eligible.
    assert!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/desktop-latest.zip", None, mac_arm).is_ok());
    assert!(require_compatible_asset(
        "https://example.test/releases/download/v1.2.3/desktop-1.2.3-x86_64-pc-windows-msvc.zip",
        None, (None, None)).is_ok());
}

#[test]
fn snapshot_reports_host_target_and_explicit_unknowns() {
    let mut downloading = serde_json::json!({
        "state":"downloading","stage":"downloading","bytes":512,"total":1024
    });
    native_update::describe_snapshot(&mut downloading, "0.5.127", "macos", "aarch64");
    assert_eq!(downloading["current_version"], serde_json::json!("0.5.127"));
    assert_eq!(downloading["platform"], serde_json::json!("macos"));
    assert_eq!(downloading["arch"], serde_json::json!("aarch64"));
    assert_eq!(downloading["bytes"], serde_json::json!(512));
    assert_eq!(downloading["total"], serde_json::json!(1024));
    assert_eq!(downloading["bytes_per_second"], Value::Null);
    let mut idle = serde_json::json!({"state":"idle"});
    native_update::describe_snapshot(&mut idle, "1.0.0", "windows", "x86_64");
    for key in ["stage", "bytes", "total", "bytes_per_second"] {
        assert_eq!(idle[key], Value::Null, "{key} must be explicit for the panel");
    }
    assert_eq!(native_update::host_platform(), std::env::consts::OS);
    assert_eq!(native_update::host_arch(), std::env::consts::ARCH);
}

#[test]
fn progress_is_throttled_and_rates_are_derived() {
    let start = std::time::Instant::now();
    let mut progress = native_update::Progress::new(start, 1000);
    assert!(!progress.due(start + Duration::from_millis(100)));
    assert!(progress.due(start + Duration::from_millis(250)));
    assert!(!progress.due(start + Duration::from_millis(499)));
    assert!(progress.due(start + Duration::from_millis(500)));
    assert_eq!(native_update::PROGRESS_INTERVAL, Duration::from_millis(250));
    // Only bytes received in this session count towards the rate.
    assert_eq!(progress.rate(3000, start + Duration::from_secs(2)), Some(1000));
    assert_eq!(native_update::bytes_per_second(1000, Duration::from_secs(2)), Some(500));
    assert_eq!(native_update::bytes_per_second(0, Duration::from_secs(2)), None);
    assert_eq!(native_update::bytes_per_second(1000, Duration::ZERO), None);
}

#[test]
fn transfer_deadlines_keep_their_previous_bounds() {
    assert_eq!(native_update::UPDATE_CONNECT_TIMEOUT, Duration::from_secs(15));
    assert_eq!(native_update::UPDATE_RESPONSE_TIMEOUT, Duration::from_secs(30));
    assert_eq!(native_update::UPDATE_IDLE_TIMEOUT, Duration::from_secs(30));
    assert_eq!(native_update::UPDATE_TOTAL_TIMEOUT, Duration::from_secs(600));
    assert_eq!(native_update::MAX_PACKAGE, 512 * 1024 * 1024);
}